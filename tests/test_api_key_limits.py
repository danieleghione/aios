"""Client keys: what each one used, how often it may call, which models it reaches."""
import json

import httpx
import pytest

from aios import gateway


@pytest.fixture
def upstream(monkeypatch):
    """A loaded model that answers with a usage block, without llama-server."""
    async def running(model_id, published_only=True):
        gateway.claim(model_id)
        return {'config': '{}', 'port': 8090}

    monkeypatch.setattr(gateway, 'ensure_running', running)
    original = httpx.AsyncClient
    answer = {'choices': [{'message': {'content': 'hi'}}], 'usage': {'prompt_tokens': 7, 'completion_tokens': 5}}
    monkeypatch.setattr(gateway.httpx, 'AsyncClient', lambda **kw: original(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=answer)), **kw))
    gateway._WINDOWS.clear()
    gateway._BUSY.clear()
    yield
    gateway._WINDOWS.clear()
    gateway._BUSY.clear()


def published(environment, discovered, url):
    key = discovered(url)
    environment.execute('INSERT INTO installed_models(id,path,sha256,installed_at,state,published,gguf,config) VALUES (?,?,?,?,?,1,?,?)',
                        (key, '/m.gguf', 'a' * 64, environment.now(), 'PUBLISHED', '{}', '{}'))
    return key


def chat(client, key, model):
    return client.post('/v1/chat/completions', headers={'Authorization': 'Bearer ' + key},
                       json={'model': model, 'messages': [{'role': 'user', 'content': 'hello'}]})


def test_each_key_counts_its_requests_and_tokens(admin, environment, discovered, upstream):
    model = published(environment, discovered, 'https://example.com/a.gguf')
    key = admin.post('/api/v1/aios/apikeys', json={'name': 'n8n'}).json()
    for _ in range(3):
        assert chat(admin, key['key'], model).status_code == 200
    item = admin.get('/api/v1/aios/apikeys').json()['items'][0]
    assert item['requests'] == 3 and item['tokens'] == 36 and item['rate_limit'] == 0 and item['models'] == []


def test_a_key_is_held_to_its_requests_per_minute(admin, environment, discovered, upstream):
    model = published(environment, discovered, 'https://example.com/a.gguf')
    key = admin.post('/api/v1/aios/apikeys', json={'name': 'script', 'rate_limit': 2}).json()
    assert [chat(admin, key['key'], model).status_code for _ in range(2)] == [200, 200]
    refused = chat(admin, key['key'], model)
    assert refused.status_code == 429 and '2 requests per minute' in refused.text and int(refused.headers['retry-after']) >= 1
    assert admin.get('/api/v1/aios/apikeys').json()['items'][0]['requests'] == 2
    # Raising the limit applies at once.
    assert admin.patch(f"/api/v1/aios/apikeys/{key['id']}", json={'rate_limit': 0, 'models': []}).status_code == 200
    assert chat(admin, key['key'], model).status_code == 200


def test_a_key_reaches_only_its_models(admin, environment, discovered, upstream):
    allowed = published(environment, discovered, 'https://example.com/a.gguf')
    other = published(environment, discovered, 'https://example.com/b.gguf')
    assert admin.post('/api/v1/aios/apikeys', json={'name': 'x', 'models': ['not-a-model']}).status_code == 422
    key = admin.post('/api/v1/aios/apikeys', json={'name': 'team', 'models': [allowed]}).json()
    assert chat(admin, key['key'], allowed).status_code == 200
    refused = chat(admin, key['key'], other)
    assert refused.status_code == 403 and 'may not use this model' in refused.text
    listed = admin.get('/v1/models', headers={'Authorization': 'Bearer ' + key['key']}).json()['data']
    assert [m['id'] for m in listed] == [allowed]
    assert admin.get('/api/v1/aios/apikeys').json()['items'][0]['models'] == [allowed]


def test_the_chat_key_is_never_limited(admin, environment, discovered, upstream):
    model = published(environment, discovered, 'https://example.com/a.gguf')
    (environment.ETC / 'secrets').mkdir(parents=True, exist_ok=True)
    (environment.ETC / 'secrets/inference-key').write_text('chat-key')
    assert all(chat(admin, 'chat-key', model).status_code == 200 for _ in range(5))
    assert gateway.inference_authorised('Bearer chat-key') is gateway.CHAT


def test_limits_of_a_revoked_key_cannot_change(admin):
    key = admin.post('/api/v1/aios/apikeys', json={'name': 'old'}).json()
    admin.delete(f"/api/v1/aios/apikeys/{key['id']}")
    assert admin.patch(f"/api/v1/aios/apikeys/{key['id']}", json={'rate_limit': 5}).status_code == 404
    assert admin.post('/api/v1/aios/apikeys', json={'name': 'x', 'rate_limit': -1}).status_code == 422
    assert json.dumps(admin.get('/api/v1/aios/audit').json()).count(key['key']) == 0
