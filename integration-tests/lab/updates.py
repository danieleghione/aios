"""PLT-01/PLT-02 on the lab: install a release key, update the application from a
signed archive, then prove a broken release is rolled back on its own."""
from lab import ADMIN, ADMIN_PASSWORD, ADMIN_USER, BASE, CONSOLE_LOG, HOSTNAME, REPO, SSH_KEY, SPEECH_SAMPLE_URL, WORK  # noqa: F401
import base64
import hashlib
import json
import shutil
import subprocess
import tarfile
import tempfile
import time
from pathlib import Path

import httpx
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

S = WORK
B = BASE
results = {}
c = httpx.Client(base_url=B, verify=False, timeout=600)


def check(item, name, ok, detail=''):
    results.setdefault(item, []).append(bool(ok))
    print(f"[{'OK ' if ok else 'BAD'}] {item:7} {name:56} {str(detail)[:150]}", flush=True)


def login():
    for _ in range(60):
        try:
            r = c.post('/api/v1/aios/auth/login', json={'username': ADMIN_USER, 'password': ADMIN_PASSWORD})
            if r.status_code == 200:
                c.headers['x-csrf-token'] = r.json()['csrf']
                return True
        except httpx.HTTPError:
            pass
        time.sleep(5)
    return False


def api(path, method='GET', **kw):
    return c.request(method, '/api/v1/aios/' + path, **kw)


def job(key, limit=900):
    for _ in range(limit // 3):
        try:
            found = next((j for j in api('system/jobs').json()['items'] if j['id'] == key), None)
        except (httpx.HTTPError, KeyError, ValueError):
            login()
            continue
        if found and found['state'] in ('COMPLETED', 'FAILED'):
            return found
        time.sleep(3)
    return None


MARKER = 'X-AIOS-Release'


def application_tree(destination, broken=False, marker=None):
    """The same content build-image.sh copies to /opt/aios/app. With a marker,
    the release's NGINX site adds a response header, so the lab can see that a
    release installs the system files it carries (UPD-01)."""
    excluded = {'.git', '.venv', 'build', 'dist', 'node_modules', '__pycache__', '.pytest_cache',
                '.mypy_cache', '.ruff_cache', 'internal', 'tests', 'integration-tests', '.github', '.vscode'}
    shutil.copytree(REPO, destination, ignore=lambda folder, names: [n for n in names if n in excluded], dirs_exist_ok=True)
    (destination / 'frontend-admin' / 'dist').mkdir(parents=True, exist_ok=True)
    shutil.copytree(REPO / 'frontend-admin/dist', destination / 'frontend-admin/dist', dirs_exist_ok=True)
    if marker:
        site = destination / 'config/nginx.conf'
        text = site.read_text()
        anchor = '    add_header X-Content-Type-Options nosniff always;\n'
        site.write_text(text.replace(anchor, anchor + f'    add_header {MARKER} {marker} always;\n', 1))
    if broken:
        # Starts, imports, and refuses to serve: exactly what a health check is for.
        (destination / 'backend/aios/app.py').write_text('raise SystemExit("deliberately broken release")\n')


def archive_of(tree):
    handle = tempfile.NamedTemporaryFile(suffix='.tar.gz', delete=False)
    with tarfile.open(handle.name, 'w:gz') as tar:
        for path in sorted(tree.rglob('*')):
            tar.add(path, arcname=str(path.relative_to(tree)), recursive=False)
    return Path(handle.name)


def upload(archive):
    with archive.open('rb') as stream:
        response = c.post('/api/v1/aios/backups/upload', files={'file': (archive.name, stream, 'application/gzip')})
    response.raise_for_status()
    return response.json()['file']


def signed(archive, key, version):
    manifest = {'component': 'application', 'version': version, 'sha256': hashlib.sha256(archive.read_bytes()).hexdigest()}
    canonical = json.dumps(manifest, sort_keys=True, separators=(',', ':')).encode()
    return {'manifest': manifest, 'signature': base64.b64encode(key.sign(canonical)).decode()}


login()
key = Ed25519PrivateKey.generate()
pem = key.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo).decode()

# PLT-01: no trust anchor in the image
state = api('system/release-key').json()
check('PLT-01', 'the image ships no release key', state['installed'] is False, state)
with tempfile.TemporaryDirectory() as folder:
    tree = Path(folder) / 'app'
    application_tree(tree, marker='lab-1')
    good = archive_of(tree)
payload = signed(good, key, 'lab-1')
uploaded = upload(good)
refused = api('system/update', 'POST', json={**payload, 'file': uploaded})
outcome = job(refused.json()['id'], 120) if refused.status_code == 200 else None
check('PLT-01', 'without a key an update is refused', outcome and outcome['state'] == 'FAILED' and 'No release key' in json.dumps(outcome['result']), outcome and outcome['result'])

installed = api('system/release-key', 'PUT', json={'key': pem}).json()
check('PLT-01', 'the operator installs the public key', installed['installed'] and installed['algorithm'] == 'Ed25519', installed['fingerprint'])

wrong = signed(good, Ed25519PrivateKey.generate(), 'lab-bad')
outcome = job(api('system/update', 'POST', json={**wrong, 'file': uploaded}).json()['id'], 180)
check('PLT-01', 'a release signed by another key is refused', outcome and outcome['state'] == 'FAILED', outcome and outcome['result'])

# PLT-01: a real application update
before = api('system/dashboard').json()['hardware']['aios_version']
outcome = job(api('system/update', 'POST', json={**payload, 'file': uploaded}).json()['id'])
login()
check('PLT-01', 'a signed application release is installed', outcome and outcome['state'] == 'COMPLETED' and outcome['result'].get('version') == 'lab-1', outcome and outcome['result'])
check('PLT-01', 'the portal answers after the update', api('system/settings').status_code == 200, f'version {before}')
header = c.get('/api/v1/aios/auth/status').headers.get(MARKER)
check('UPD-01', "the release's NGINX site is installed and applied", header == 'lab-1', header)

# PLT-02: a release that does not answer is rolled back
with tempfile.TemporaryDirectory() as folder:
    tree = Path(folder) / 'app'
    application_tree(tree, broken=True, marker='lab-broken')
    bad = archive_of(tree)
broken_payload = signed(bad, key, 'lab-broken')
uploaded_bad = upload(bad)
outcome = job(api('system/update', 'POST', json={**broken_payload, 'file': uploaded_bad}).json()['id'])
back = login()
check('PLT-02', 'a release that cannot serve is refused', outcome and outcome['state'] == 'FAILED' and 'restored' in json.dumps(outcome['result']), outcome and outcome['result'])
check('PLT-02', 'the previous release is back and answering', back and api('system/settings').status_code == 200, '')
header = c.get('/api/v1/aios/auth/status').headers.get(MARKER)
check('UPD-01', 'after the rollback, the site of the release put back', header == 'lab-1', header)
check('PLT-02', 'models and settings survived the rollback', api('models').status_code == 200 and api('repositories').json()['items'], '')

print('\nRESULTS')
for item, oks in results.items():
    print(f"  {item:7} {'PASS' if all(oks) else 'FAIL'} ({sum(oks)}/{len(oks)})")
