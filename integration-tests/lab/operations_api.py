"""Phase A on the lab: upload limit for recordings, built-in repositories, alerts,
download names, the default model per kind, and what answers without a login."""
import json
import struct
import time

import httpx

from lab import ADMIN_PASSWORD, ADMIN_USER, BASE, WORK

c = httpx.Client(base_url=BASE, verify=False, timeout=900)
results = {}


def check(item, name, ok, detail=''):
    results.setdefault(item, []).append(bool(ok))
    print(f"[{'OK ' if ok else 'BAD'}] {item:7} {name:56} {str(detail)[:150]}", flush=True)


def login():
    r = c.post('/api/v1/aios/auth/login', json={'username': ADMIN_USER, 'password': ADMIN_PASSWORD})
    c.headers['x-csrf-token'] = r.json()['csrf']


def api(path, method='GET', **kw):
    return c.request(method, '/api/v1/aios/' + path, **kw)


def silence(seconds):
    """A WAV of silence, 16 kHz mono: large enough to cross the old 32 MB limit."""
    frames = 16000 * seconds
    header = b'RIFF' + struct.pack('<I', 36 + frames * 2) + b'WAVEfmt ' + struct.pack('<IHHIIHH', 16, 1, 1, 16000, 32000, 2, 16)
    return header + b'data' + struct.pack('<I', frames * 2) + b'\0' * (frames * 2)


login()
models = json.load(open(WORK / 'lab_models.json'))

# SEC-01: nothing about the API answers without a session or a key
anonymous = httpx.Client(base_url=BASE, verify=False, timeout=60)
check('SEC-01', 'API reference needs a session', anonymous.get('/api/aios/docs').status_code == 401, '')
check('SEC-01', 'OpenAPI schema needs a session', anonymous.get('/api/aios/openapi.json').status_code == 401, '')
check('SEC-01', '/v1/models needs a key', anonymous.get('/v1/models').status_code == 401, '')
check('SEC-01', 'signed in, the reference is there', c.get('/api/aios/docs').status_code == 200, '')

# FIX-05: downloads by name
downloads = api('downloads').json()['items']
check('FIX-05', 'downloads listed by model name', downloads and all(d.get('display_name') and d['display_name'] != d['model_id'] for d in downloads),
      [d.get('display_name', '')[:40] for d in downloads[:2]])

# FIX-06: one default per kind, shown on the model
api(f"models/{models['text']}", 'PATCH', json={'default': True})
listed = {m['id']: m for m in api('models').json()['items']}
check('FIX-06', 'default language model marked', listed[models['text']]['is_default'] is True, listed[models['text']]['display_name'][:40])
defaults = [m for m in listed.values() if m['is_default'] and m['kind'] == 'text']
check('FIX-06', 'exactly one default language model', len(defaults) == 1, [m['display_name'][:30] for m in defaults])

# FIX-02: every built-in repository is there
names = {r['name'] for r in api('repositories').json()['items']}
check('FIX-02', 'built-in repositories present', {'Speech models (whisper)', 'Image models (diffusion)', 'Hugging Face', 'ModelScope'} <= names, sorted(names))

# FIX-04: a repository that fails raises an alert, which can be dismissed. The
# address resolves (saving checks it) but serves no AIOS manifest.
created = api('repositories', 'POST', json={'name': 'Lab failing mirror', 'provider': 'http', 'url': 'https://huggingface.co/aios-lab/missing/manifest.json',
                                           'enabled': True, 'config': {}})
check('FIX-04', 'a mirror with a reachable address is saved', created.status_code == 200, created.text[:80])
failing = created.json().get('id', '')
api(f"repositories/{failing}/sync", 'POST')
mine, alerts = [], []
for _ in range(40):
    alerts = api('system/dashboard').json()['alerts']
    mine = [a for a in alerts if a['id'] == 'repository:' + failing]
    if mine:
        break
    time.sleep(3)
check('FIX-04', 'a failing repository raises an alert', mine and 'Lab failing mirror' in mine[0]['message'], mine[0]['message'][:100] if mine else alerts)
if mine:
    dismissed = api(f"alerts/repository:{failing}/dismiss", 'POST')
    still = [a for a in api('system/dashboard').json()['alerts'] if a['id'] == 'repository:' + failing]
    check('FIX-04', 'the alert can be dismissed', dismissed.status_code == 200 and not still, dismissed.status_code)
api(f"repositories/{failing}", 'PUT', json={'name': 'Lab failing mirror', 'provider': 'http', 'url': 'https://huggingface.co/aios-lab/missing/manifest.json',
                                          'enabled': False, 'config': {}})

# FIX-01: a recording above the old 32 MB limit reaches the speech engine
speech = [m for m in api('models').json()['items'] if m.get('kind') == 'speech']
if speech:
    key = api('apikeys', 'POST', json={'name': 'large upload', 'expires_days': 1}).json()['key']
    audio = silence(1100)  # about 35 MB
    r = httpx.post(BASE + '/v1/audio/transcriptions', headers={'Authorization': 'Bearer ' + key},
                   files={'file': ('long.wav', audio, 'audio/wav')}, verify=False, timeout=1800)
    check('FIX-01', f'a {len(audio) // 2**20} MiB recording is accepted', r.status_code == 200, f'{r.status_code} {r.text[:80]}')
    portal = c.post('/api/v1/aios/speech/transcribe', files={'file': ('long.wav', audio, 'audio/wav')}, data={'model': speech[0]['id']})
    check('FIX-01', 'and from the portal', portal.status_code == 200, f'{portal.status_code} {portal.text[:80]}')
else:
    check('FIX-01', 'a speech model is installed for the upload test', False, 'run voice.py first')

print('\nRESULTS')
for item, oks in results.items():
    print(f"  {item:7} {'PASS' if all(oks) else 'FAIL'} ({sum(oks)}/{len(oks)})")
