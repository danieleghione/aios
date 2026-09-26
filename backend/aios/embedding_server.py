"""The embedding model shipped in the image, as an OpenAI-compatible endpoint.

Open WebUI already embeds documents with all-MiniLM-L6-v2 from /opt/aios/embedding;
this serves the same model to API clients through the gateway. It runs with the
chat's Python, which carries sentence-transformers, and is started by systemd on
the first request to 127.0.0.1:8093 (socket activation). After ten minutes
without a request it exits and gives its memory back; the socket stays, so the
next request starts it again. Standard library only, apart from the model.
"""
import base64
import json
import os
import socket
import struct
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

MODEL_DIR = os.environ.get('AIOS_EMBEDDING_MODEL', '/opt/aios/embedding/all-MiniLM-L6-v2')
NAME = 'all-MiniLM-L6-v2'
IDLE_SECONDS = int(os.environ.get('AIOS_EMBEDDING_IDLE', '600'))
MAX_INPUTS = 256
MAX_BODY = 4 * 1024 ** 2


class Rejected(ValueError):
    pass


def inputs_of(payload):
    """The texts to embed: one string or a list of strings, as OpenAI accepts."""
    value = payload.get('input')
    texts = [value] if isinstance(value, str) else value
    if not isinstance(texts, list) or not texts or not all(isinstance(t, str) for t in texts):
        raise Rejected("'input' must be a string or a list of strings")
    if len(texts) > MAX_INPUTS:
        raise Rejected(f'At most {MAX_INPUTS} inputs per request')
    return texts


def answer(payload, encode, count):
    """The OpenAI embeddings response for one request."""
    texts = inputs_of(payload)
    fmt = payload.get('encoding_format', 'float')
    if fmt not in ('float', 'base64'):
        raise Rejected("'encoding_format' must be float or base64")
    vectors = encode(texts)
    data = []
    for index, vector in enumerate(vectors):
        values = [float(x) for x in vector]
        embedding = base64.b64encode(struct.pack(f'<{len(values)}f', *values)).decode() if fmt == 'base64' else values
        data.append({'object': 'embedding', 'index': index, 'embedding': embedding})
    tokens = count(texts)
    return {'object': 'list', 'data': data, 'model': NAME, 'usage': {'prompt_tokens': tokens, 'total_tokens': tokens}}


class Model:
    """Loaded on the first request, used by one request at a time."""
    def __init__(self):
        self.lock, self.model = threading.Lock(), None

    def _load(self):
        if self.model is None:
            from sentence_transformers import SentenceTransformer
            self.model = SentenceTransformer(MODEL_DIR, device='cpu')
        return self.model

    def encode(self, texts):
        with self.lock:
            return self._load().encode(texts, normalize_embeddings=True).tolist()

    def count(self, texts):
        with self.lock:
            tokenizer = self._load().tokenizer
            return sum(len(tokenizer(text)['input_ids']) for text in texts)


def handler(model, touched):
    class Handler(BaseHTTPRequestHandler):
        def reply(self, status, body):
            data = json.dumps(body).encode()
            self.send_response(status)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            self.reply(200, {'status': 'ok', 'model': NAME} if self.path == '/health' else {'error': 'Not found'})

        def do_POST(self):
            touched[0] = time.monotonic()
            if self.path != '/v1/embeddings':
                return self.reply(404, {'error': {'message': 'Not found'}})
            length = int(self.headers.get('Content-Length') or 0)
            if length > MAX_BODY:
                return self.reply(413, {'error': {'message': 'Request too large'}})
            try:
                self.reply(200, answer(json.loads(self.rfile.read(length) or b'{}'), model.encode, model.count))
            except (Rejected, ValueError) as exc:
                self.reply(400, {'error': {'message': str(exc)}})
            touched[0] = time.monotonic()

        def log_message(self, *args):
            pass
    return Handler


def serve():
    touched = [time.monotonic()]
    Handler = handler(Model(), touched)
    if os.environ.get('LISTEN_FDS') == '1':
        # Socket activation: systemd holds the port and hands it over as fd 3.
        server = HTTPServer(('127.0.0.1', 0), Handler, bind_and_activate=False)
        server.socket = socket.socket(fileno=3)
    else:
        server = HTTPServer(('127.0.0.1', int(os.environ.get('AIOS_EMBEDDING_PORT', '8093'))), Handler)

    def watch():
        while time.monotonic() - touched[0] < IDLE_SECONDS:
            time.sleep(5)
        server.shutdown()
    threading.Thread(target=watch, daemon=True).start()
    server.serve_forever()
    return 0


if __name__ == '__main__':
    sys.exit(serve())
