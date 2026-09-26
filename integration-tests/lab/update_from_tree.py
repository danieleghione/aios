"""Update the lab appliance in place with the working tree, as a signed
application release: a fresh key is installed, the tree is packed as
build-image.sh would, signed and applied. For trying a change on a running
appliance before building a new ISO."""
import base64
import hashlib
import json
import shutil
import tarfile
import tempfile
import time
from pathlib import Path

import httpx
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from lab import ADMIN_PASSWORD, ADMIN_USER, BASE, REPO

EXCLUDED = {'.git', '.venv', 'build', 'dist', 'node_modules', '__pycache__', '.pytest_cache', '.mypy_cache', '.ruff_cache',
            'internal', 'tests', 'integration-tests', '.github', '.vscode'}
c = httpx.Client(base_url=BASE, verify=False, timeout=600)


def login():
    for _ in range(60):
        try:
            r = c.post('/api/v1/aios/auth/login', json={'username': ADMIN_USER, 'password': ADMIN_PASSWORD})
            if r.status_code == 200:
                c.headers['x-csrf-token'] = r.json()['csrf']
                return
        except httpx.HTTPError:
            pass
        time.sleep(5)


login()
key = Ed25519PrivateKey.generate()
pem = key.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo).decode()
c.put('/api/v1/aios/system/release-key', json={'key': pem}).raise_for_status()
with tempfile.TemporaryDirectory() as folder:
    tree = Path(folder) / 'app'
    shutil.copytree(REPO, tree, ignore=lambda d, names: [n for n in names if n in EXCLUDED])
    shutil.copytree(REPO / 'frontend-admin/dist', tree / 'frontend-admin/dist', dirs_exist_ok=True)
    archive = Path(folder) / 'release.tar.gz'
    with tarfile.open(archive, 'w:gz') as tar:
        for path in sorted(tree.rglob('*')):
            tar.add(path, arcname=str(path.relative_to(tree)), recursive=False)
    version = (REPO / 'VERSION').read_text().strip()
    manifest = {'component': 'application', 'version': version, 'sha256': hashlib.sha256(archive.read_bytes()).hexdigest()}
    signature = base64.b64encode(key.sign(json.dumps(manifest, sort_keys=True, separators=(',', ':')).encode())).decode()
    with archive.open('rb') as stream:
        uploaded = c.post('/api/v1/aios/backups/upload', files={'file': ('release.tar.gz', stream, 'application/gzip')}).json()['file']
job = c.post('/api/v1/aios/system/update', json={'manifest': manifest, 'signature': signature, 'file': uploaded}).json()['id']
for _ in range(200):
    try:
        found = next((j for j in c.get('/api/v1/aios/system/jobs').json()['items'] if j['id'] == job), None)
    except (httpx.HTTPError, KeyError, ValueError):
        login()
        continue
    if found and found['state'] in ('COMPLETED', 'FAILED'):
        print(found['state'], found['result'])
        break
    time.sleep(3)
login()
print('running', c.get('/api/v1/aios/auth/me').json().get('version'))
