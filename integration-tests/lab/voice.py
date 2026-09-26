"""PLT-05 (voice) on the lab: install a whisper model, transcribe a real recording
through the gateway, the portal and beside a language model."""
from lab import ADMIN, ADMIN_PASSWORD, ADMIN_USER, BASE, CONSOLE_LOG, HOSTNAME, REPO, SSH_KEY, SPEECH_SAMPLE_URL, WORK  # noqa: F401
import json
import time
from pathlib import Path

import httpx

S = WORK
B = BASE
SPOKEN = 'ask not what your country can do for you'
c = httpx.Client(base_url=B, verify=False, timeout=900)
results = {}


def check(name, ok, detail=''):
    results[name] = bool(ok)
    print(f"[{'OK ' if ok else 'BAD'}] VOICE   {name:52} {str(detail)[:150]}", flush=True)


def login():
    r = c.post('/api/v1/aios/auth/login', json={'username': ADMIN_USER, 'password': ADMIN_PASSWORD})
    c.headers['x-csrf-token'] = r.json()['csrf']


def api(path, method='GET', **kw):
    return c.request(method, '/api/v1/aios/' + path, **kw)


login()
sample = S / 'jfk.wav'
if not sample.exists():
    sample.write_bytes(httpx.get(SPEECH_SAMPLE_URL, follow_redirects=True, timeout=60).content)
audio = sample.read_bytes()

# The repository ships disabled, like every other one.
repos = {r['name']: r for r in api('repositories').json()['items']}
speech_repo = repos['Speech models (whisper)']
check('the speech repository ships disabled', not speech_repo['enabled'] and speech_repo['status'] == 'DISABLED', speech_repo['status'])
api(f"repositories/{speech_repo['id']}", 'PUT', json={'name': speech_repo['name'], 'provider': 'speech', 'url': speech_repo['url'],
                                                      'enabled': True, 'config': speech_repo['config']})
api(f"repositories/{speech_repo['id']}/sync", 'POST')
for _ in range(60):
    state = next(r for r in api('repositories').json()['items'] if r['id'] == speech_repo['id'])
    if state['status'] != 'SYNCING':
        break
    time.sleep(5)
check('whisper releases discovered', state['status'] == 'ONLINE' and state['found'] > 5, f"{state['found']} files, {state.get('error') or 'no error'}")

items = [i for off in (0, 200, 400) for i in api('catalog', params={'limit': 200, 'offset': off}).json()['items']]
speech_items = [i for i in items if i.get('kind') == 'speech']
check('speech models rated for this machine', speech_items and all(i['compatibility']['kind'] == 'speech' for i in speech_items),
      f"{len(speech_items)} models, e.g. {[(i['display_name'], i['compatibility']['classification']) for i in speech_items[:2]]}")
smallest = min(speech_items, key=lambda i: i['size'])
check('the smallest is a tiny model', 'tiny' in smallest['display_name'].lower(), f"{smallest['display_name']} {smallest['size'] // 2**20} MiB")

r = api(f"models/{smallest['id']}/install", 'POST', json={'accept_license': True, 'override_compatibility': True})
for _ in range(180):
    installed = {m['id']: m for m in api('models').json()['items']}
    if installed.get(smallest['id'], {}).get('state') in ('INSTALLED', 'PUBLISHED', 'FAILED'):
        break
    time.sleep(5)
entry = installed.get(smallest['id'], {})
check('installed with its checksum', entry.get('state') in ('INSTALLED', 'PUBLISHED'), f"{entry.get('state')} {entry.get('runtime_error') or ''}")
check('listed as a speech model', entry.get('kind') == 'speech', entry.get('kind'))

# From the portal: the session is enough, no inference key.
started = time.time()
r = c.post('/api/v1/aios/speech/transcribe', files={'file': ('jfk.wav', audio, 'audio/wav')}, data={'model': smallest['id']})
body = r.json() if r.status_code == 200 else {}
check('transcribed from the portal', r.status_code == 200 and SPOKEN in body.get('text', '').lower(),
      f"{r.status_code} in {time.time() - started:.1f} s: {str(body.get('text'))[:90]}")

# From a client, with its own key, through the OpenAI-compatible route.
api(f"models/{smallest['id']}", 'PATCH', json={'published': True})
key = api('apikeys', 'POST', json={'name': 'voice test', 'expires_days': 1}).json()['key']
r = httpx.post(B + '/v1/audio/transcriptions', headers={'Authorization': 'Bearer ' + key},
               files={'file': ('jfk.wav', audio, 'audio/wav')}, data={'language': 'en'}, verify=False, timeout=900)
check('transcribed through /v1/audio/transcriptions', r.status_code == 200 and SPOKEN in r.json().get('text', '').lower(),
      f"{r.status_code}: {r.text[:90]}")
r = httpx.post(B + '/v1/audio/transcriptions', files={'file': ('jfk.wav', audio, 'audio/wav')}, verify=False, timeout=60)
check('refused without a key', r.status_code == 401, r.status_code)

# The three kinds side by side.
models = json.load(open(S / 'lab_models.json'))
api(f"runtime/{models['text']}/start", 'POST')
for _ in range(90):
    runtimes = {x['model_id']: x for x in api('runtime').json()['items']}
    if runtimes.get(models['text'], {}).get('state') in ('RUNNING', 'FAILED'):
        break
    time.sleep(5)
r = httpx.post(B + '/v1/audio/transcriptions', headers={'Authorization': 'Bearer ' + key},
               files={'file': ('jfk.wav', audio, 'audio/wav')}, verify=False, timeout=900)
runtimes = {x['model_id']: x for x in api('runtime').json()['items']}
check('speech and language models loaded together', r.status_code == 200 and runtimes.get(models['text'], {}).get('state') == 'RUNNING'
      and runtimes.get(smallest['id'], {}).get('state') == 'RUNNING',
      [(x['display_name'][:28], x['kind'], x['state'], x['port']) for x in runtimes.values()])
check('each engine on its own port', {x['port'] for x in runtimes.values()} >= {8090, 8092}, sorted({x['port'] for x in runtimes.values()}))

print('\nRESULTS')
print(f"  VOICE   {'PASS' if all(results.values()) else 'FAIL'} ({sum(results.values())}/{len(results)})")
