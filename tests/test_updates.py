import base64
import hashlib
import io
import tarfile
import subprocess
from pathlib import Path
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.exceptions import InvalidSignature
from aios import platform

@pytest.fixture
def release(environment,monkeypatch,tmp_path):
    key=Ed25519PrivateKey.generate()
    public=tmp_path/'release.pub'
    public.write_bytes(key.public_key().public_bytes(serialization.Encoding.PEM,serialization.PublicFormat.SubjectPublicKeyInfo))
    opt=tmp_path/'opt'
    old=opt/'app/backend/aios/app.py'
    old.parent.mkdir(parents=True)
    old.write_text('original release content')
    archive=environment.DATA/'backups'/(environment.uid()+'.tar.gz')
    with tarfile.open(archive,'w:gz') as tar:
        content=b'updated release content'
        entry=tarfile.TarInfo('backend/aios/app.py')
        entry.size=len(content)
        tar.addfile(entry,io.BytesIO(content))
    manifest={'component':'application','version':'test-release','sha256':hashlib.sha256(archive.read_bytes()).hexdigest()}
    signature=key.sign(environment.encode(manifest).encode())
    original=Path
    def mapped(value):
        return public if value=='/etc/aios-release.pub' else opt if value=='/opt/aios' else original(value)
    monkeypatch.setattr(platform,'Path',mapped)
    monkeypatch.setattr(platform.time,'sleep',lambda seconds:None)
    # The health probe has no running services to reach in a test.
    monkeypatch.setattr(platform,'answers',lambda component,attempts=20:None)
    # The system files of a release are tested on their own (test_system_files.py).
    monkeypatch.setattr(platform.system_files,'sync',lambda *args,**kwargs:[])
    return {'manifest':manifest,'signature':base64.b64encode(signature).decode(),'file':archive.name}, old, opt

def test_signed_update_and_previous_release(environment,release,monkeypatch):
    payload,old,opt=release
    calls=[]
    monkeypatch.setattr(platform,'run',lambda args,**kwargs:calls.append(args) or 'active')
    result=platform.update_release(payload)
    assert result['version']=='test-release'
    assert old.read_text()=='updated release content'
    assert (opt/'app.previous/backend/aios/app.py').read_text()=='original release content'
    assert any('is-active' in command for command in calls)

def test_wrong_signature_never_stops_services(environment,release,monkeypatch):
    payload,old,opt=release
    payload['signature']=base64.b64encode(b'x'*64).decode()
    calls=[]
    monkeypatch.setattr(platform,'run',lambda args,**kwargs:calls.append(args))
    with pytest.raises(InvalidSignature):
        platform.update_release(payload)
    assert calls==[] and old.read_text()=='original release content'

def test_release_health_failure_rolls_back(environment,release,monkeypatch):
    payload,old,opt=release
    def command(args,**kwargs):
        if 'is-active' in args:
            raise subprocess.CalledProcessError(3,args)
        return ''
    monkeypatch.setattr(platform,'run',command)
    with pytest.raises(ValueError,match='previous component restored'):
        platform.update_release(payload)
    assert old.read_text()=='original release content'


def test_the_release_key_is_installed_by_the_operator(admin, environment):
    """No trust anchor ships in the image: the appliance accepts releases only
    from the key whoever runs it installs."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from aios.platform import load_release_key, release_key_path
    assert admin.get('/api/v1/aios/system/release-key').json() == {'installed': False, 'fingerprint': None, 'path': str(release_key_path())}
    with pytest.raises(ValueError, match='No release key installed'):
        load_release_key()
    pem = Ed25519PrivateKey.generate().public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo).decode()
    body = admin.put('/api/v1/aios/system/release-key', json={'key': pem}).json()
    assert body['installed'] and body['algorithm'] == 'Ed25519' and len(body['fingerprint']) == 23
    assert release_key_path().read_text() == pem and load_release_key()
    assert admin.get('/api/v1/aios/system/release-key').json()['fingerprint'] == body['fingerprint']
    assert admin.delete('/api/v1/aios/system/release-key').json() == {'installed': False}
    assert not release_key_path().exists()


@pytest.mark.parametrize('key', ['not a key', '-----BEGIN PUBLIC KEY-----\nzzzz\n-----END PUBLIC KEY-----\n'])
def test_only_an_ed25519_public_key_is_accepted(admin, key):
    assert admin.put('/api/v1/aios/system/release-key', json={'key': key}).status_code == 422


def test_an_rsa_key_is_refused(admin):
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    pem = rsa.generate_private_key(public_exponent=65537, key_size=2048).public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo).decode()
    response = admin.put('/api/v1/aios/system/release-key', json={'key': pem})
    assert response.status_code == 422 and 'Ed25519' in response.text


def test_installing_a_key_needs_a_superadmin(admin, environment):
    from aios.auth import HASHER
    key = environment.uid()
    environment.execute('INSERT INTO users(id,username,password_hash,role) VALUES (?,?,?,?)', (key, 'plain', HASHER.hash('Plain-admin-pass-1'), 'ADMIN'))
    response = admin.post('/api/v1/aios/auth/login', json={'username': 'plain', 'password': 'Plain-admin-pass-1'})
    admin.headers['x-csrf-token'] = response.json()['csrf']
    assert admin.get('/api/v1/aios/system/release-key').status_code == 200
    assert admin.put('/api/v1/aios/system/release-key', json={'key': 'x'}).status_code == 403
    assert admin.delete('/api/v1/aios/system/release-key').status_code == 403


def test_a_release_that_does_not_answer_is_rolled_back(environment, release, monkeypatch):
    """systemctl says "active" for a process that serves nothing: the update is
    judged by the component answering again."""
    payload, old, opt = release
    calls = []
    monkeypatch.setattr(platform, 'run', lambda args, **kwargs: calls.append(args) or 'active')
    monkeypatch.setattr(platform, 'answers', lambda component, attempts=20: (_ for _ in ()).throw(ValueError('did not answer')))
    with pytest.raises(ValueError, match='previous component restored'):
        platform.update_release(payload)
    assert old.read_text() == 'original release content' and not (opt / 'app.previous').exists()
    assert calls[-1][:2] == ['systemctl', 'start']


def test_the_health_probe_waits_then_gives_up(monkeypatch):
    seen = []
    monkeypatch.setattr(platform.time, 'sleep', lambda seconds: None)
    monkeypatch.setattr(platform.urllib.request, 'urlopen', lambda url, timeout=5: seen.append(url) or (_ for _ in ()).throw(OSError('refused')))
    with pytest.raises(ValueError, match='did not answer on'):
        platform.answers('application', attempts=3)
    assert seen == ['http://127.0.0.1:8081/health'] * 3
    platform.answers('runtime')  # no endpoint of its own: the unit check is all there is


def test_an_extracted_release_is_readable_by_the_services(tmp_path, monkeypatch):
    """The broker extracts as root with a private umask: without this the staged
    directory is 0700 and every unit dies with "changing to the working directory
    failed", which looked exactly like a broken release."""
    import os
    tree = tmp_path / 'stage'
    (tree / 'backend/aios').mkdir(parents=True)
    tree.chmod(0o700)
    script = tree / 'backend/aios/app.py'
    script.write_text('x = 1\n')
    script.chmod(0o600)
    runner = tree / 'run.sh'
    runner.write_text('#!/bin/sh\n')
    runner.chmod(0o700)
    owned = []
    monkeypatch.setattr(platform.os, 'chown', lambda path, uid, gid: owned.append((str(path), uid, gid)))
    platform.readable_tree(tree)
    assert tree.stat().st_mode & 0o777 == 0o755
    assert (tree / 'backend').stat().st_mode & 0o777 == 0o755
    assert script.stat().st_mode & 0o777 == 0o644 and runner.stat().st_mode & 0o777 == 0o755
    assert owned and all(entry[1:] == (0, 0) for entry in owned)
    assert os.path.join(str(tree), 'run.sh') in [entry[0] for entry in owned]


def test_services_are_reset_before_being_started(monkeypatch):
    """A release that crashes fills systemd's restart counter; without a reset the
    rollback cannot start anything either."""
    calls = []
    monkeypatch.setattr(platform, 'run', lambda args, **kwargs: calls.append(list(args)) or '')
    platform.restart(['aios-control-plane', 'aios-download-worker'])
    assert calls[0][:2] == ['systemctl', 'reset-failed'] and calls[1][:2] == ['systemctl', 'start']


def test_the_rollback_starts_even_after_a_crash_loop(environment, release, monkeypatch):
    calls = []
    def run(args, **kwargs):
        calls.append(list(args))
        if args[:2] == ['systemctl', 'reset-failed'] and len([c for c in calls if c[:2] == ['systemctl', 'reset-failed']]) == 1:
            raise subprocess.SubprocessError('unit not failed')  # reset is best effort
        return 'active'
    monkeypatch.setattr(platform, 'run', run)
    monkeypatch.setattr(platform, 'answers', lambda component, attempts=20: (_ for _ in ()).throw(ValueError('did not answer')))
    payload, old, opt = release
    with pytest.raises(ValueError, match='previous component restored'):
        platform.update_release(payload)
    assert old.read_text() == 'original release content'
    assert calls[-2][:2] == ['systemctl', 'reset-failed'] and calls[-1][:2] == ['systemctl', 'start']
