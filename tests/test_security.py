import io
import json
import sqlite3
import struct
import tarfile
import pytest
from aios.gguf import inspect_gguf
from aios.providers import RepositoryError, artifact, validate_url
from aios.platform import NetworkConfig, safe_archive

@pytest.mark.parametrize('url',['http://example.com/model.gguf','file:///etc/passwd','https://user:password@example.com/a','https://127.0.0.1/a','https://169.254.169.254/latest/meta-data','https://[::1]/a'])
def test_reject_dangerous_urls(url):
    with pytest.raises(RepositoryError):
        validate_url(url)

def test_loopback_not_allowed_even_internal():
    with pytest.raises(RepositoryError):
        validate_url('https://127.0.0.1/a', True)

def test_artifact_path_traversal():
    with pytest.raises(RepositoryError):
        artifact('a/b','../evil.gguf','https://example.com/a',100)

def test_oversized_metadata(tmp_path):
    file=tmp_path/'evil.gguf'
    file.write_bytes(b'GGUF'+struct.pack('<IQQQ',3,1,1,2**50))
    with pytest.raises(ValueError, match='Oversized'):
        inspect_gguf(file)

def test_truncated_gguf(tmp_path,tiny_gguf):
    file=tmp_path/'bad.gguf'
    file.write_bytes(tiny_gguf[:-10])
    with pytest.raises(ValueError):
        inspect_gguf(file)

def test_valid_gguf(tmp_path,tiny_gguf):
    file=tmp_path/'model.gguf'
    file.write_bytes(tiny_gguf)
    assert inspect_gguf(file)['metadata']['general.architecture']=='llama'

@pytest.mark.parametrize('name,kind',[('../outside',tarfile.REGTYPE),('/etc/passwd',tarfile.REGTYPE),('link',tarfile.SYMTYPE)])
def test_archive_safety(tmp_path,name,kind):
    archive=tmp_path/'bad.tar'
    with tarfile.open(archive,'w') as tar:
        member=tarfile.TarInfo(name)
        member.type=kind
        if kind==tarfile.SYMTYPE:
            member.linkname='/etc/passwd'
        tar.addfile(member,io.BytesIO())
    with pytest.raises(ValueError):
        safe_archive(archive,tmp_path/'out')

def test_invalid_network():
    with pytest.raises(ValueError):
        NetworkConfig(interface='eth0;reboot',dhcp=True)
    with pytest.raises(ValueError):
        NetworkConfig(interface='eth0',dhcp=False,address='bad',gateway='not-ip')

def test_auth_bypass(client):
    for path in ('users','models','catalog','repositories','system/settings','backups','audit','logs'):
        assert client.get('/api/v1/aios/'+path).status_code==401

def test_csrf_and_cookie_flags(admin):
    admin.headers.pop('x-csrf-token')
    assert admin.post('/api/v1/aios/hardware/benchmark').status_code==403
    cookie=next(iter(admin.cookies.jar))
    assert cookie.secure and cookie.has_nonstandard_attr('HttpOnly')

def test_one_time_bootstrap(admin,environment):
    assert not (environment.ETC/'secrets/bootstrap').exists()
    assert admin.post('/api/v1/aios/auth/bootstrap',json={'username':'attacker','password':'long-password-123','secret':'wrong'}).status_code in (409,429)

def test_audit_immutable(environment):
    environment.audit('user','test')
    with pytest.raises(sqlite3.IntegrityError):
        environment.execute('DELETE FROM audit_events')
    events=environment.rows('SELECT * FROM audit_events')
    assert json.loads(events[0]['payload'])['action']=='test'

def test_secret_redaction(admin,environment,monkeypatch):
    import aios.app
    monkeypatch.setattr(aios.app,'validate_url',lambda *args:None)
    secret='sensitive-unit-test-token-987'
    response=admin.post('/api/v1/aios/repositories',json={'name':'Private','provider':'http','url':'https://example.com/manifest.json','token':secret})
    assert response.status_code==200
    assert secret not in admin.get('/api/v1/aios/repositories').text
    assert secret not in admin.get('/api/v1/aios/audit').text
    assert (environment.ETC/'secrets'/response.json()['id']).stat().st_mode & 0o777 == 0o600

def test_login_lockout(client,environment):
    body={'username':'missing','password':'wrong'}
    assert client.post('/api/v1/aios/auth/login',json=body).status_code==401
    assert client.post('/api/v1/aios/auth/login',json=body).status_code==429

def test_viewer_rbac(admin,environment):
    from aios.auth import HASHER
    key=environment.uid()
    environment.execute('INSERT INTO users(id,username,password_hash,role) VALUES (?,?,?,?)',(key,'viewer',HASHER.hash('Viewer-password-12'),'VIEWER'))
    response=admin.post('/api/v1/aios/auth/login',json={'username':'viewer','password':'Viewer-password-12'})
    admin.headers['x-csrf-token']=response.json()['csrf']
    assert admin.get('/api/v1/aios/catalog').status_code==200
    assert admin.post('/api/v1/aios/users',json={'username':'bad','password':'long-password-123','role':'ADMIN'}).status_code==403

@pytest.mark.parametrize('username',['chat.user@example.com','user+tag@example.com','operator'])
def test_username_accepts_email_addresses(admin,username):
    assert admin.post('/api/v1/aios/users',json={'username':username,'password':'long-password-123','role':'OPERATOR'}).status_code==200
    assert admin.post('/api/v1/aios/auth/login',json={'username':username,'password':'long-password-123'}).status_code==200

@pytest.mark.parametrize('username',['bad user','drop;table','a'*255,'','user\nrole=ADMIN','<script>'])
def test_username_still_rejects_hostile_input(admin,username):
    assert admin.post('/api/v1/aios/users',json={'username':username,'password':'long-password-123','role':'OPERATOR'}).status_code==422

def test_sensitive_storage_permissions(environment):
    assert environment.DB.stat().st_mode & 0o777 == 0o600
    assert (environment.DATA/'database').stat().st_mode & 0o777 == 0o700
    assert (environment.DATA/'backups').stat().st_mode & 0o777 == 0o700

def test_dns_connection_is_pinned(monkeypatch):
    import socket
    from aios.providers import request_target
    monkeypatch.setattr(socket, 'getaddrinfo', lambda *args, **kwargs: [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('93.184.216.34', 443))])
    target, headers, extensions = request_target('https://models.example/model.gguf', {})
    assert target.host == '93.184.216.34'
    assert headers['Host'] == 'models.example'
    assert extensions['sni_hostname'] == 'models.example'

def test_offline_api_reference(client):
    response = client.get('/api/aios/docs')
    assert response.status_code == 200
    assert '/api/v1/aios/models' in response.text
    assert '<script src="https://' not in response.text

def test_transport_logging_does_not_expose_signed_urls(environment):
    import logging
    assert logging.getLogger('httpx').getEffectiveLevel() >= logging.WARNING

@pytest.mark.parametrize('metadata',[{'license':{'script':'bad'}},{'author':['bad']},{'parameter_count':-1},{'context':'unbounded'}])
def test_malicious_metadata_types_are_rejected(metadata):
    with pytest.raises(RepositoryError):
        artifact('org/model','model.gguf','https://example.com/model.gguf',100,**metadata)
