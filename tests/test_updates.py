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
