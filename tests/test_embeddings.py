"""The embedding model shipped in the image, served to API clients."""
import base64
import json
import struct
import threading
import urllib.request
from pathlib import Path

import httpx
import pytest

from aios import embedding_server, gateway

ROOT = Path(__file__).resolve().parents[1]


def fake_encode(texts):
    return [[float(len(t)), 0.5, -1.0] for t in texts]


def fake_count(texts):
    return sum(len(t.split()) for t in texts)


def test_the_openai_shape_for_one_or_many_inputs():
    one = embedding_server.answer({'input': 'hello world'}, fake_encode, fake_count)
    assert one['object'] == 'list' and one['model'] == 'all-MiniLM-L6-v2'
    assert one['data'] == [{'object': 'embedding', 'index': 0, 'embedding': [11.0, 0.5, -1.0]}]
    assert one['usage'] == {'prompt_tokens': 2, 'total_tokens': 2}
    many = embedding_server.answer({'input': ['a', 'bb']}, fake_encode, fake_count)
    assert [d['index'] for d in many['data']] == [0, 1]
    packed = embedding_server.answer({'input': 'abc', 'encoding_format': 'base64'}, fake_encode, fake_count)
    assert list(struct.unpack('<3f', base64.b64decode(packed['data'][0]['embedding']))) == [3.0, 0.5, -1.0]


@pytest.mark.parametrize('payload', [{}, {'input': []}, {'input': [1, 2]}, {'input': ['x'] * 257}, {'input': 'x', 'encoding_format': 'hex'}])
def test_malformed_requests_are_refused(payload):
    with pytest.raises(embedding_server.Rejected):
        embedding_server.answer(payload, fake_encode, fake_count)


def test_the_server_answers_over_http_and_exits_when_idle(monkeypatch):
    class Stub:
        encode = staticmethod(fake_encode)
        count = staticmethod(fake_count)
    monkeypatch.setattr(embedding_server, 'Model', Stub)
    monkeypatch.setattr(embedding_server, 'IDLE_SECONDS', 6)
    monkeypatch.setenv('AIOS_EMBEDDING_PORT', '18093')
    monkeypatch.delenv('LISTEN_FDS', raising=False)
    done = threading.Event()
    threading.Thread(target=lambda: (embedding_server.serve(), done.set()), daemon=True).start()
    for _ in range(50):
        try:
            request = urllib.request.Request('http://127.0.0.1:18093/v1/embeddings', data=json.dumps({'input': 'hi'}).encode(),
                                             headers={'Content-Type': 'application/json'})
            body = json.loads(urllib.request.urlopen(request, timeout=2).read())
            break
        except OSError:
            threading.Event().wait(0.1)
    assert body['data'][0]['embedding'] == [2.0, 0.5, -1.0]
    assert done.wait(20), 'the server did not exit after being idle'


def test_the_gateway_forwards_the_bundled_model(admin, environment, monkeypatch):
    seen = []
    original = httpx.AsyncClient

    def upstream(request):
        seen.append(str(request.url))
        return httpx.Response(200, json={'object': 'list', 'data': [], 'model': 'all-MiniLM-L6-v2', 'usage': {'prompt_tokens': 4, 'total_tokens': 4}})
    monkeypatch.setattr(gateway.httpx, 'AsyncClient', lambda **kw: original(transport=httpx.MockTransport(upstream), **kw))
    key = admin.post('/api/v1/aios/apikeys', json={'name': 'rag'}).json()
    r = admin.post('/v1/embeddings', headers={'Authorization': 'Bearer ' + key['key']}, json={'model': 'all-MiniLM-L6-v2', 'input': 'text'})
    assert r.status_code == 200 and seen == ['http://127.0.0.1:8093/v1/embeddings']
    assert admin.get('/api/v1/aios/apikeys').json()['items'][0]['requests'] == 1
    assert admin.get('/api/v1/aios/apikeys').json()['items'][0]['tokens'] == 4
    assert admin.post('/v1/embeddings', json={'model': 'all-MiniLM-L6-v2', 'input': 'text'}).status_code == 401


def test_the_service_starts_on_demand_as_the_chat_user():
    socket_unit = (ROOT / 'systemd/aios-embedding.socket').read_text()
    service = (ROOT / 'systemd/aios-embedding.service').read_text()
    assert 'ListenStream=127.0.0.1:8093' in socket_unit
    assert 'User=aios-webui' in service and 'embedding_server.py' in service
    assert 'HF_HUB_OFFLINE=1' in service
    assert 'aios-embedding.socket' in (ROOT / 'config/enabled-units').read_text().split()


def test_the_service_does_not_shadow_the_standard_library():
    # Run by path, Python would put aios/ first on the module path, and numpy's
    # "import platform" would load aios/platform.py instead of the standard one.
    unit = (ROOT / 'systemd/aios-embedding.service').read_text()
    assert 'ExecStart=/opt/aios/webui/bin/python -P /opt/aios/app/backend/aios/embedding_server.py' in unit
