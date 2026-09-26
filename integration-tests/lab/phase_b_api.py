"""Phase B on the lab: encrypted and scheduled backups restored with their
passphrase, idle models unloaded, sessions per user, and the dashboard's trends."""
import json
import time

import httpx

from lab import ADMIN_PASSWORD, ADMIN_USER, BASE, WORK, throwaway_password

VIEWER_PASSWORD = throwaway_password()

c = httpx.Client(base_url=BASE, verify=False, timeout=900)
results = {}
PASSPHRASE = 'lab passphrase for backups'


def check(item, name, ok, detail=''):
    results.setdefault(item, []).append(bool(ok))
    print(f"[{'OK ' if ok else 'BAD'}] {item:7} {name:56} {str(detail)[:150]}", flush=True)


def login(client=c, agent='lab'):
    r = client.post('/api/v1/aios/auth/login', json={'username': ADMIN_USER, 'password': ADMIN_PASSWORD}, headers={'user-agent': agent})
    client.headers['x-csrf-token'] = r.json()['csrf']
    return r


def api(path, method='GET', **kw):
    return c.request(method, '/api/v1/aios/' + path, **kw)


def wait_job(job_id, limit=1800):
    """A restore restarts the control plane and ends every session."""
    for _ in range(limit // 5):
        try:
            job = next((j for j in api('system/jobs').json()['items'] if j['id'] == job_id), None)
        except (httpx.HTTPError, ValueError, KeyError):
            try:
                login()
            except (httpx.HTTPError, KeyError, ValueError):
                pass
            time.sleep(5)
            continue
        if job and job['state'] in ('COMPLETED', 'FAILED'):
            return job
        time.sleep(5)
    return None


def runtime_state(model_id):
    return next((r['state'] for r in api('runtime').json()['items'] if r['model_id'] == model_id), '')


login()
models = json.load(open(WORK / 'lab_models.json'))

# UX-17: sessions, last sign-in, sign out everywhere
other = httpx.Client(base_url=BASE, verify=False, timeout=60)
login(other, 'second-device')
mine = api('auth/sessions').json()['items']
check('UX-17', 'both sessions listed, this one marked', len(mine) >= 2 and sum(s['current'] for s in mine) == 1, [(s['client'] or '')[:30] for s in mine])
second = next((s for s in mine if 'second-device' in (s['client'] or '')), None)
check('UX-17', 'the other device named', second is not None, '')
if second:
    api(f"auth/sessions/{second['id']}", 'DELETE')
    check('UX-17', 'ended from here, the other device is signed out', other.get('/api/v1/aios/auth/me').status_code == 401, '')
me = next(u for u in api('users').json()['items'] if u['username'] == ADMIN_USER)
check('UX-17', 'last sign-in and sessions in the users list', me['last_login'] and me['sessions'] >= 1, me.get('sessions'))
api('users', 'POST', json={'username': 'lab-viewer', 'password': VIEWER_PASSWORD, 'role': 'VIEWER'})
viewer = httpx.Client(base_url=BASE, verify=False, timeout=60)
viewer.post('/api/v1/aios/auth/login', json={'username': 'lab-viewer', 'password': VIEWER_PASSWORD})
account = next(u for u in api('users').json()['items'] if u['username'] == 'lab-viewer')
ended = api(f"users/{account['id']}/signout", 'POST')
check('UX-17', 'a superadmin signs an account out everywhere', ended.status_code == 200 and viewer.get('/api/v1/aios/auth/me').status_code == 401, ended.text[:60])
api(f"users/{account['id']}", 'DELETE')

# UX-16: trends on the dashboard
history = api('system/dashboard').json().get('history', {})
check('UX-16', 'memory, temperature and speed trends sampled', set(history) >= {'memory', 'temperature', 'tokens_per_second'} and len(history['memory']) > 3,
      {k: len(v) for k, v in history.items()})

check('UX-18', 'a GPU memory trend, empty on a VM without a GPU', 'gpu_memory' in history and all(v is None for v in history['gpu_memory']),
      history.get('gpu_memory', [])[-3:])

# FEA-03: an idle model gives its memory back
api(f"runtime/{models['text']}/start", 'POST')
for _ in range(60):
    if runtime_state(models['text']) == 'RUNNING':
        break
    time.sleep(5)
settings = api('system/settings').json()
policy = {'download_concurrency': settings.get('download_concurrency', 2), 'approved_licenses': settings.get('approved_licenses', [])}
api('system/policy', 'PUT', json={**policy, 'idle_unload_minutes': 1})
state = ''
for _ in range(40):
    state = runtime_state(models['text'])
    if state == 'STOPPED':
        break
    time.sleep(5)
check('FEA-03', 'a model unused for a minute is unloaded', state == 'STOPPED', state)
audit = api('audit', params={'q': 'runtime_unloaded_idle'}).json()['items']
check('FEA-03', 'and the audit says why', bool(audit), len(audit))
api('system/policy', 'PUT', json={**policy, 'idle_unload_minutes': 0})

# FEA-01: sealed backups, the schedule, and a restore with the passphrase
check('FEA-01', 'a short passphrase is refused', api('backups/passphrase', 'PUT', json={'passphrase': 'short'}).status_code == 422, '')
api('backups/passphrase', 'PUT', json={'passphrase': PASSPHRASE})
job = wait_job(api('backups', 'POST', json={'include_models': False}).json()['id'])
check('FEA-01', 'a backup made with a passphrase', job and job['state'] == 'COMPLETED', job and str(job.get('result'))[:80])
listed = api('backups').json()
sealed = next((b for b in listed['items'] if b['encrypted'] and not b['scheduled']), None)
check('FEA-01', 'is sealed, and listed as such', sealed and sealed['file'].endswith('.tar.gz.enc') and listed['encryption'], sealed)
if sealed:
    body = api('backups/' + sealed['file']).content
    check('FEA-01', 'the download is ciphertext', body[:8] == b'AIOSENC1' and b'aios.db' not in body, len(body))
api('backups/schedule', 'PUT', json={'enabled': True, 'hour': 0, 'keep': 3, 'include_models': False})
scheduled = None
for _ in range(60):
    scheduled = next((b for b in api('backups').json()['items'] if b['scheduled']), None)
    if scheduled:
        break
    time.sleep(5)
check('FEA-01', 'the schedule makes its backup, sealed', scheduled and scheduled['encrypted'], scheduled)
check('FEA-01', 'and remembers the day it ran', bool(api('backups').json()['last_scheduled']), api('backups').json()['last_scheduled'])
api('backups/schedule', 'PUT', json={'enabled': False, 'hour': 2, 'keep': 7, 'include_models': False})
if sealed:
    wrong = wait_job(api('backups/restore', 'POST', json={'file': sealed['file'], 'confirm': 'RESTORE', 'passphrase': 'not the passphrase'}).json()['id'])
    check('FEA-01', 'a wrong passphrase restores nothing', wrong and wrong['state'] == 'FAILED' and 'passphrase' in json.dumps(wrong.get('result')).lower(), wrong)
    right = wait_job(api('backups/restore', 'POST', json={'file': sealed['file'], 'confirm': 'RESTORE', 'passphrase': PASSPHRASE}).json()['id'])
    check('FEA-01', 'the right one restores it', right and right['state'] == 'COMPLETED', right and str(right.get('result'))[:80])
    for _ in range(60):
        try:
            if login().status_code == 200:
                break
        except (httpx.HTTPError, KeyError, ValueError):
            pass
        time.sleep(5)
    check('FEA-01', 'signed in again after the restore', api('auth/me').status_code == 200, '')
api('backups/passphrase', 'PUT', json={'passphrase': ''})

print('\nRESULTS')
for item, oks in results.items():
    print(f"  {item:7} {'PASS' if all(oks) else 'FAIL'} ({sum(oks)}/{len(oks)})")
