import httpx
from aios import chat_auth


def mock_chat(monkeypatch):
    calls = []
    def signin(url, **kwargs):
        calls.append(kwargs)
        headers = kwargs['headers']
        return httpx.Response(200, json={'email': headers['X-AIOS-Email'], 'role': headers['X-AIOS-Role']}, request=httpx.Request('POST', url))
    monkeypatch.setattr(chat_auth.httpx, 'post', signin)
    chat_auth._ready.clear()
    return calls


def test_email_user_password_gate_and_revocation(admin, environment, monkeypatch):
    calls = mock_chat(monkeypatch)
    initial = 'Initial-password-437!'
    password = 'Personal-password-437!'
    response = admin.post('/api/v1/aios/users', json={'username': 'Person+chat@Example.org', 'password': initial, 'role': 'VIEWER'})
    assert response.status_code == 200
    key = response.json()['id']
    assert admin.post('/api/v1/aios/users', json={'username': 'PERSON+CHAT@example.org', 'password': initial, 'role': 'VIEWER'}).status_code == 409
    response = admin.post('/api/v1/aios/auth/login', json={'username': 'PERSON+CHAT@EXAMPLE.ORG', 'password': initial})
    assert response.status_code == 200
    assert 'Path=/' in response.headers['set-cookie']
    admin.headers['x-csrf-token'] = response.json()['csrf']
    assert admin.get('/_aios/chat-session').status_code == 403
    assert not calls
    assert admin.post('/api/v1/aios/auth/password', json={'current': initial, 'password': password}).status_code == 200
    assert admin.get('/_aios/chat-session').status_code == 401
    response = admin.post('/api/v1/aios/auth/login', json={'username': 'person+chat@example.org', 'password': password})
    admin.headers['x-csrf-token'] = response.json()['csrf']
    response = admin.get('/_aios/chat-session', headers={'X-AIOS-Email': 'spoof@example.org', 'X-AIOS-Role': 'admin'})
    assert response.status_code == 204
    assert response.headers['X-AIOS-Email'] == 'person+chat@example.org'
    assert response.headers['X-AIOS-Role'] == 'user'
    assert len(calls) == 1
    assert admin.get('/_aios/chat-session').status_code == 204
    assert len(calls) == 1
    environment.execute('UPDATE users SET disabled=1 WHERE id=?', (key,))
    assert admin.get('/_aios/chat-session').status_code == 401


def test_chat_failure_closed_and_role_reconciled(admin, environment, monkeypatch):
    calls = mock_chat(monkeypatch)
    response = admin.get('/_aios/chat-session')
    assert response.status_code == 204
    assert response.headers['X-AIOS-Email'].endswith('@users.aios.invalid')
    assert response.headers['X-AIOS-Role'] == 'admin'
    environment.execute("UPDATE users SET role='VIEWER'")
    response = admin.get('/_aios/chat-session')
    assert response.headers['X-AIOS-Role'] == 'user'
    assert len(calls) == 2
    chat_auth._ready.clear()
    def unavailable(*args, **kwargs):
        raise httpx.ConnectError('offline')
    monkeypatch.setattr(chat_auth.httpx, 'post', unavailable)
    assert admin.get('/_aios/chat-session').status_code == 503


def test_legacy_email_collision_fails_closed(admin, environment, monkeypatch):
    mock_chat(monkeypatch)
    environment.execute("UPDATE users SET username='same@example.org'")
    environment.execute("INSERT INTO users(id,username,password_hash,role) SELECT 'collision','SAME@example.org',password_hash,role FROM users LIMIT 1")
    assert admin.get('/_aios/chat-session').status_code == 403
    response = admin.post('/api/v1/aios/auth/login', json={'username': 'same@example.org', 'password': 'Testing-password-937!'})
    assert response.status_code == 409
