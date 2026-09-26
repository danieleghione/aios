"""The voice engine's loopback service: POST /v1/audio/speech, GET /health.

Started by the runtime manager like the other engines. Each request is split
into sentences, llama-tts speaks each part, and the parts are joined with a
short pause and encoded in the format asked for (ffmpeg, already in the image,
for anything but WAV). One text is spoken at a time; /health answers meanwhile.
"""
import argparse
import io
import json
import os
import re
import subprocess
import tempfile
import threading
import time
import wave
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from aios.voice import DEFAULT_VOICE, LANGUAGES, VOICES

MAX_INPUT = 4096
PART = 300
PAUSE_SECONDS = 0.25
PART_TIMEOUT = 900
FORMATS = {'wav': 'audio/wav', 'mp3': 'audio/mpeg', 'opus': 'audio/ogg', 'aac': 'audio/aac', 'flac': 'audio/flac', 'pcm': 'audio/pcm'}
FFMPEG = {'mp3': ['-f', 'mp3'], 'opus': ['-c:a', 'libopus', '-f', 'ogg'], 'aac': ['-c:a', 'aac', '-f', 'adts'],
          'flac': ['-f', 'flac'], 'pcm': ['-f', 's16le', '-acodec', 'pcm_s16le']}
# A few words that give a language away, for texts that arrive without one.
COMMON = {
    'it': {'il', 'che', 'di', 'non', 'per', 'una', 'sono', 'della', 'questo', 'gli', 'con', 'anche', 'come', 'è'},
    'en': {'the', 'and', 'is', 'of', 'to', 'that', 'this', 'with', 'for', 'are', 'you', 'not', 'it'},
    'de': {'der', 'die', 'und', 'das', 'ist', 'nicht', 'ein', 'eine', 'mit', 'ich', 'sie', 'zu'},
    'fr': {'le', 'la', 'les', 'et', 'est', 'une', 'des', 'pas', 'que', 'pour', 'dans', 'vous', 'je'},
    'es': {'el', 'la', 'los', 'y', 'es', 'una', 'que', 'por', 'para', 'con', 'no', 'del', 'las'},
    'pt': {'o', 'os', 'e', 'é', 'um', 'uma', 'que', 'não', 'para', 'com', 'do', 'da', 'você'},
}


class Rejected(ValueError):
    pass


def guess_language(text):
    """The language of a text, from its script or its most common words; None
    when nothing gives it away."""
    if re.search(r'[぀-ヿ]', text):
        return 'ja'
    if re.search(r'[가-힯]', text):
        return 'ko'
    if re.search(r'[一-鿿]', text):
        return 'zh'
    if re.search(r'[Ѐ-ӿ]', text):
        return 'ru'
    words = re.findall(r"[a-zàèéìòóùçñãõäöüß]+", text.lower())
    scores = {language: sum(word in common for word in words) for language, common in COMMON.items()}
    best = max(scores, key=scores.get)
    return best if scores[best] else None


# The chat sends an answer one sentence at a time, and "OK." or "Sì, 42." says
# nothing about its language: such a sentence keeps the language of the text
# spoken just before it.
REMEMBER_SECONDS = 600
_last = {'language': 'en', 'at': 0.0}


def language_for(payload, text, current=None):
    current = time.monotonic() if current is None else current
    for value in (payload.get('language'), payload.get('voice')):
        if isinstance(value, str) and value.lower()[:2] in LANGUAGES and len(value) in (2, 5):
            return value.lower()[:2]
    guessed = guess_language(text)
    if guessed:
        _last.update(language=guessed, at=current)
        return guessed
    return _last['language'] if current - _last['at'] < REMEMBER_SECONDS else 'en'


def voice_for(payload):
    """The reference voice: the one named, else the default. A language code
    given as the voice ("it") chooses the language, not the voice."""
    value = str(payload.get('voice') or '').lower()
    return value if value in VOICES else DEFAULT_VOICE


def parts(text, limit=PART):
    """Sentences grouped up to the limit; a longer sentence is cut at a comma or a space."""
    text = re.sub(r'\s+', ' ', text).strip()
    sentences = [s for s in re.split(r'(?<=[.!?;:。！？])\s+', text) if s]
    pieces = []
    for sentence in sentences:
        while len(sentence) > limit:
            cut = max(sentence.rfind(', ', 0, limit), sentence.rfind(' ', 0, limit))
            cut = cut if cut > limit // 3 else limit
            pieces.append(sentence[:cut].strip(' ,'))
            sentence = sentence[cut:].strip(' ,')
        if sentence:
            pieces.append(sentence)
    grouped = []
    for piece in pieces:
        if grouped and len(grouped[-1]) + 1 + len(piece) <= limit:
            grouped[-1] += ' ' + piece
        else:
            grouped.append(piece)
    return grouped


def join(waves):
    """One WAV from several with the same format, a short pause between them."""
    frames, params = [], None
    for data in waves:
        with wave.open(io.BytesIO(data)) as reader:
            if params is None:
                params = reader.getparams()
            elif reader.getparams()[:3] != params[:3]:
                raise ValueError('The parts of the audio have different formats')
            frames.append(reader.readframes(reader.getnframes()))
    silence = b'\0' * (int(params.framerate * PAUSE_SECONDS) * params.sampwidth * params.nchannels)
    output = io.BytesIO()
    with wave.open(output, 'wb') as writer:
        writer.setnchannels(params.nchannels)
        writer.setsampwidth(params.sampwidth)
        writer.setframerate(params.framerate)
        writer.writeframes(silence.join(frames))
    return output.getvalue()


def encode(data, fmt):
    if fmt == 'wav':
        return data
    result = subprocess.run(['ffmpeg', '-hide_banner', '-loglevel', 'error', '-f', 'wav', '-i', 'pipe:0', *FFMPEG[fmt], 'pipe:1'],
                            input=data, capture_output=True, timeout=300)
    if result.returncode:
        raise RuntimeError('Audio encoding failed: ' + result.stderr.decode(errors='replace').strip()[-200:])
    return result.stdout


class Voice:
    def __init__(self, options):
        self.options, self.lock = options, threading.Lock()

    def command(self, text, language, output, voice=DEFAULT_VOICE):
        o = self.options
        cmd = [o.binary, '-m', o.model, '-mm', o.codec, '-p', text, '--tts-lang', language, '-t', str(o.threads),
               '-c', '1024', '-b', '256', '-ub', '256', '-n', '1024', '-o', output]
        reference = os.path.join(o.voices, voice + '.wav') if o.voices else ''
        if reference and os.path.isfile(reference):
            cmd += ['--tts-speaker-file', reference]
        cmd += ['--device', o.device, '-ngl', '99'] if o.device else ['-ngl', '0', '--no-mmproj-offload']
        return cmd

    def speak(self, text, language, voice=DEFAULT_VOICE):
        with self.lock, tempfile.TemporaryDirectory(dir=self.options.work) as folder:
            waves = []
            for index, piece in enumerate(parts(text)):
                target = os.path.join(folder, f'{index}.wav')
                result = subprocess.run(self.command(piece, language, target, voice), capture_output=True, timeout=PART_TIMEOUT)
                if result.returncode or not os.path.exists(target):
                    lines = [line for line in result.stderr.decode(errors='replace').splitlines() if 'error' in line.lower() or 'failed' in line.lower()]
                    raise RuntimeError('llama-tts could not speak the text: ' + (lines[-1] if lines else f'exit {result.returncode}')[-300:])
                with open(target, 'rb') as reader:
                    waves.append(reader.read())
            return join(waves)


def answer(payload, voice):
    """The audio for one OpenAI speech request, and its media type."""
    text = payload.get('input')
    if not isinstance(text, str) or not text.strip():
        raise Rejected("'input' must be a non-empty string")
    if len(text) > MAX_INPUT:
        raise Rejected(f"'input' is limited to {MAX_INPUT} characters")
    fmt = payload.get('response_format') or 'mp3'
    if fmt not in FORMATS:
        raise Rejected("'response_format' must be one of " + ', '.join(FORMATS))
    return encode(voice.speak(text, language_for(payload, text), voice_for(payload)), fmt), FORMATS[fmt]


def handler(voice):
    class Handler(BaseHTTPRequestHandler):
        def reply(self, status, body, media='application/json'):
            data = body if isinstance(body, bytes) else json.dumps(body).encode()
            self.send_response(status)
            self.send_header('Content-Type', media)
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            self.reply(200, {'status': 'ok'}) if self.path == '/health' else self.reply(404, {'error': {'message': 'Not found'}})

        def do_POST(self):
            if self.path != '/v1/audio/speech':
                return self.reply(404, {'error': {'message': 'Not found'}})
            try:
                length = int(self.headers.get('Content-Length') or 0)
                if length > 1024 ** 2:
                    raise Rejected('Request too large')
                audio, media = answer(json.loads(self.rfile.read(length) or b'{}'), voice)
                self.reply(200, audio, media)
            except (Rejected, ValueError) as exc:
                self.reply(400, {'error': {'message': str(exc)}})
            except (RuntimeError, OSError, subprocess.SubprocessError) as exc:
                self.reply(500, {'error': {'message': str(exc)}})

        def log_message(self, *args):
            pass
    return Handler


def main(argv=None):
    parser = argparse.ArgumentParser()
    for name in ('--model', '--codec', '--binary', '--work'):
        parser.add_argument(name, required=True)
    parser.add_argument('--port', type=int, default=8094)
    parser.add_argument('--threads', type=int, default=2)
    parser.add_argument('--device', default='')
    parser.add_argument('--voices', default='')
    options = parser.parse_args(argv)
    for path in (options.model, options.codec, options.binary):
        if not os.path.exists(path):
            parser.error(f'{path} does not exist')
    ThreadingHTTPServer(('127.0.0.1', options.port), handler(Voice(options))).serve_forever()


if __name__ == '__main__':
    main()
