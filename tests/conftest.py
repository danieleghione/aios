import importlib
import struct
import pytest
from fastapi.testclient import TestClient

@pytest.fixture
def environment(tmp_path, monkeypatch):
    from aios import core
    data, etc = tmp_path/'data', tmp_path/'etc'
    monkeypatch.setattr(core, 'DATA', data)
    monkeypatch.setattr(core, 'ETC', etc)
    monkeypatch.setattr(core, 'DB', data/'database/aios.db')
    for name in ('auth','hardware','providers','downloads','runtime','platform','app','accelerators','imaging'):
        module = importlib.import_module('aios.' + name)
        if hasattr(module, 'DATA'):
            monkeypatch.setattr(module, 'DATA', data)
        if hasattr(module, 'ETC'):
            monkeypatch.setattr(module, 'ETC', etc)
    core.initialize()
    return core

@pytest.fixture
def client(environment):
    from aios.app import app
    with TestClient(app, base_url='https://testserver') as client:
        yield client

@pytest.fixture
def admin(client, environment):
    secret = (environment.ETC/'secrets/bootstrap').read_text()
    response = client.post('/api/v1/aios/auth/bootstrap', json={'username':'admin','password':'Testing-password-937!', 'secret':secret})
    assert response.status_code == 200, response.text
    response = client.post('/api/v1/aios/auth/login', json={'username':'admin','password':'Testing-password-937!'})
    assert response.status_code == 200
    client.headers['x-csrf-token'] = response.json()['csrf']
    return client

@pytest.fixture
def tiny_gguf():
    def string(value):
        value = value.encode()
        return struct.pack('<Q',len(value)) + value
    metadata = string('general.architecture') + struct.pack('<I',8) + string('llama')
    tensor = string('fixture.weight') + struct.pack('<IQIQ',1,1,0,0)
    data = b'GGUF' + struct.pack('<IQQ',3,1,1) + metadata + tensor
    data += b'\0' * ((32-len(data)%32)%32)
    return data + struct.pack('<f',1.0)

@pytest.fixture
def discovered(environment):
    core = environment
    repo = core.one("SELECT id FROM repositories WHERE provider='http'")
    if not repo:
        repo={'id':core.uid()}
        core.execute('INSERT INTO repositories(id,name,provider,url,config,enabled) VALUES (?,?,?,?,?,1)',(repo['id'],'Test','http','https://example.com/manifest.json','{}'))
    def create(url='https://example.com/model.gguf', size=100, sha=None):
        key=core.uid()
        metadata={'model_id':'test/model','display_name':'Test model','filename':'model.gguf','url':url,'size':size,'format':'GGUF','license':'MIT','architecture':'llama','author':'test','quantization':'F32','revision':'commit-1'}
        core.execute('INSERT INTO discovered_models VALUES (?,?,?,?,?,?,?)',(key,repo['id'],key,'commit-1',core.encode(metadata),core.now(),core.now()))
        core.execute('INSERT INTO model_artifacts(id,url,size,sha256) VALUES (?,?,?,?)',(key,url,size,sha))
        return key
    return create
