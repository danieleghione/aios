"""Functional checks of backlog blocks 1-2 against the freshly installed lab appliance."""
from lab import ADMIN, ADMIN_PASSWORD, ADMIN_USER, BASE, CONSOLE_LOG, HOSTNAME, REPO, SSH_KEY, SPEECH_SAMPLE_URL, WORK, throwaway_password  # noqa: F401

TESTER_PASSWORD, RESET_PASSWORD = throwaway_password(), throwaway_password()
import json, re, subprocess, sys, time
import httpx

BASE = BASE
ADMIN = (ADMIN_USER, ADMIN_PASSWORD)
KEY = str(SSH_KEY)
c = httpx.Client(base_url=BASE, verify=False, timeout=600)
results = {}


def ssh(command):
    return subprocess.run(['ssh', '-i', KEY, '-p', '28022', '-o', 'StrictHostKeyChecking=no', '-o', 'UserKnownHostsFile=/dev/null', '-o', 'LogLevel=ERROR',
                           'aios@127.0.0.1', command], capture_output=True, text=True, timeout=120).stdout.strip()


def check(item, name, ok, detail=''):
    results.setdefault(item, []).append(bool(ok))
    print(f"[{'OK ' if ok else 'BAD'}] {item:7} {name:55} {str(detail)[:140]}", flush=True)


def login(user=ADMIN):
    r = c.post('/api/v1/aios/auth/login', json={'username': user[0], 'password': user[1]})
    if r.status_code == 200:
        c.headers['x-csrf-token'] = r.json().get('csrf', '')
    return r


def api(path, method='GET', **kw):
    return c.request(method, '/api/v1/aios/' + path, **kw)


def wait_job(job_id, limit=3600):
    """Installing OS updates restarts nginx and the control plane: a dropped
    connection is part of the operation, not a failure of it."""
    for _ in range(limit // 5):
        try:
            job = next((j for j in api('system/jobs').json()['items'] if j['id'] == job_id), None)
        except (httpx.HTTPError, ValueError, KeyError):
            try:
                login()
            except httpx.HTTPError:
                pass
            time.sleep(5)
            continue
        if job and job['state'] in ('COMPLETED', 'FAILED'):
            return job
        time.sleep(5)
    return None


if len(sys.argv) > 1 and sys.argv[1] == 'bootstrap':
    log = open(CONSOLE_LOG, errors='replace').read()
    secret = re.findall(r'Admin bootstrap secret \(valid 24h\): (\S+)', log)[-1]
    print('bootstrap', c.post('/api/v1/aios/auth/bootstrap', json={'username': ADMIN[0], 'password': ADMIN[1], 'secret': secret}).status_code)
    sys.exit()

login()

# SYS-01: configuration shows what the system uses, applying it changes nothing else
real_tz = 'Europe/Rome'  # the installer's answer (default zone)
system = api('system/settings').json()['system']
check('SYS-01', 'time zone shown = real one', system['timezone'] == real_tz, f"portal={system['timezone']} system={real_tz}")
check('SYS-01', 'hostname shown = the installed one', system['hostname'] == HOSTNAME, system['hostname'])
zones = api('system/timezones').json()['items']
check('SYS-01', 'time zone list', 'Europe/Rome' in zones and len(zones) > 300, len(zones))
job = wait_job(api('system/config', 'POST', json=system).json()['id'], 120)
check('SYS-01', 'apply unchanged form keeps the time zone', job and job['state'] == 'COMPLETED' and api('system/settings').json()['system']['timezone'] == real_tz, job and job['state'])
target = 'Europe/Rome' if real_tz != 'Europe/Rome' else 'Europe/London'
job = wait_job(api('system/config', 'POST', json={**system, 'timezone': target}).json()['id'], 120)
after = api('system/settings').json()['system']['timezone']
check('SYS-01', f'change time zone to {target}', job and job['state'] == 'COMPLETED' and after == target, after)
wait_job(api('system/config', 'POST', json={**system, 'timezone': real_tz}).json()['id'], 120)

# SYS-02: current network state, pending change offered without a copied ID
net = api('system/network').json()
cur = net['current']
check('SYS-02', 'current connection', cur['interface'] and cur['addresses'] and cur['gateway'] == '10.0.2.2' and cur['dns'], cur)
check('SYS-02', 'DHCP detected', cur['dhcp'] is True, cur['dhcp'])
job = wait_job(api('system/network', 'POST', json={'interface': cur['interface'], 'dhcp': True, 'address': '', 'gateway': '', 'dns': []}).json()['id'], 180)
pending = api('system/network').json()['pending']
check('SYS-02', 'applied change shows as pending', job and job['state'] == 'COMPLETED' and pending and pending['id'] == job['id'], pending)
confirm = wait_job(api(f"system/network/{pending['id']}/confirm", 'POST').json()['id'], 120) if pending else None
check('SYS-02', 'confirm from the page clears it', confirm and confirm['state'] == 'COMPLETED' and api('system/network').json()['pending'] is None, confirm and confirm['state'])
time.sleep(130)
late = api('system/network').json()
check('SYS-02', 'still reachable, no rollback, 130 s after confirming', late['current']['addresses'] and late['pending'] is None and not any(j['action'] == 'network-rollback' for j in api('system/jobs').json()['items']), late['current'])

# SYS-03: operating system updates from the portal
job = wait_job(api('system/updates/check', 'POST').json()['id'], 900)
status = api('system/updates').json()
check('SYS-03', 'check now', job and job['state'] == 'COMPLETED' and status['checked_at'] and not status['error'], status)
if status['packages']:
    job = wait_job(api('system/updates/apply', 'POST').json()['id'], 3600)
    after = api('system/updates').json()
    check('SYS-03', f"install {status['packages']} updates", job and job['state'] == 'COMPLETED' and job['result']['exit_code'] == 0 and after['packages'] == 0,
          f"{after} {(job or {}).get('result', {}).get('output', '')[-200:]}")
else:
    check('SYS-03', 'install (nothing to install)', True, status)

# USR-01/02: reset another user's password, delete a user, never yourself
r = api('users', 'POST', json={'username': 'lab.tester@example.org', 'password': TESTER_PASSWORD, 'role': 'OPERATOR'})
uid = next(u['id'] for u in api('users').json()['items'] if u['username'] == 'lab.tester@example.org')
r = api(f'users/{uid}/password', 'POST', json={'password': RESET_PASSWORD})
check('USR-01', 'reset password', r.status_code == 200, r.text[:100])
r2 = login(('lab.tester@example.org', RESET_PASSWORD))
check('USR-01', 'sign in with the temporary password, must change', r2.status_code == 200 and bool(api('auth/me').json()['must_change']), r2.status_code)
check('USR-01', 'old password no longer works', login(('lab.tester@example.org', TESTER_PASSWORD)).status_code in (401, 429), '')
login()
me = api('auth/me').json()['id']
check('USR-01', 'own password cannot be reset here', api(f'users/{me}/password', 'POST', json={'password': RESET_PASSWORD}).status_code in (400, 409), '')
r = api(f'users/{uid}', 'DELETE')
check('USR-02', 'delete user', r.status_code == 200 and all(u['id'] != uid for u in api('users').json()['items']), r.status_code)
check('USR-02', 'deleted user cannot sign in', login(('lab.tester@example.org', RESET_PASSWORD)).status_code in (401, 429), '')
login()
check('USR-02', 'own account cannot be deleted', api(f'users/{me}', 'DELETE').status_code in (400, 409), '')

# UI-07 / UI-01 / CAT-01: repositories
repos = {r['name']: r for r in api('repositories').json()['items']}
check('UI-07', 'disabled repositories say DISABLED', all(r['status'] == 'DISABLED' for r in repos.values() if not r['enabled']), [(n, r['status']) for n, r in repos.items()])
github = next(r for r in repos.values() if r['provider'] == 'github')
api(f"repositories/{github['id']}", 'PUT', json={'name': github['name'], 'provider': 'github', 'url': github['url'], 'enabled': True, 'config': github['config']})
api(f"repositories/{github['id']}/test", 'POST')
status = next(r for r in api('repositories').json()['items'] if r['id'] == github['id'])['status']
check('UI-01', 'enabled GitHub without models is NOT CONFIGURED', status == 'NOT CONFIGURED', status)
for name in ('DeepSeek (GGUF)', 'Image models (diffusion)'):
    repo = repos[name]
    api(f"repositories/{repo['id']}", 'PUT', json={'name': repo['name'], 'provider': repo['provider'], 'url': repo['url'], 'enabled': True, 'config': repo['config']})
    api(f"repositories/{repo['id']}/sync", 'POST')
ms = next(r for r in repos.values() if r['provider'] == 'modelscope')
api(f"repositories/{ms['id']}", 'PUT', json={'name': ms['name'], 'provider': 'modelscope', 'url': ms['url'], 'enabled': True, 'config': ms['config']})
api(f"repositories/{ms['id']}/sync", 'POST')
for _ in range(120):
    if not any(r['status'] == 'SYNCING' for r in api('repositories').json()['items']):
        break
    time.sleep(5)
total = api('catalog', params={'limit': 200}).json()
week = api('catalog', params={'limit': 200, 'since': time.time() - 7 * 86400}).json()
year = api('catalog', params={'limit': 200, 'since': time.time() - 365 * 86400}).json()
def released(item):
    from datetime import datetime
    return datetime.fromisoformat(str(item['release_date']).replace('Z', '+00:00')).timestamp() if item.get('release_date') else item['discovered_at']
check('CAT-01', 'last 7 days < everything', week['total'] < total['total'], f"7d={week['total']} 365d={year['total']} all={total['total']}")
check('CAT-01', 'every 7-day item released in the last 7 days', all(released(i) >= time.time() - 7 * 86400 - 60 for i in week['items']), [i.get('release_date') for i in week['items'][:4]])
ms_items = [i for i in total['items'] if i['repository_id'] == ms['id']]
check('CAT-01', 'ModelScope rows carry a release date', ms_items and all(i.get('release_date') for i in ms_items), f"{len(ms_items)} rows, e.g. {ms_items[0].get('release_date') if ms_items else None}")

# Models for the runtime checks: the smallest text model and SD 1.5
items = [i for off in (0, 200, 400) for i in api('catalog', params={'limit': 200, 'offset': off}).json()['items']]
usable = [i for i in items if i['repository_id'] == repos['DeepSeek (GGUF)']['id'] and i['compatibility']['classification'] != 'INCOMPATIBLE' and i.get('kind') != 'image']
text = min([i for i in usable if i['quantization'].upper() == 'Q4_K_M'] or usable, key=lambda i: i['size'])
image = next((i for i in items if i.get('kind') == 'image' and 'Stable Diffusion 1.5' in i['display_name'] and 'fp16' in i['display_name']), None)
for item in (text, image):
    if item:
        r = api(f"models/{item['id']}/install", 'POST', json={'accept_license': True, 'override_compatibility': True})
        print('install', item['display_name'], item['size'] // 2**20, 'MiB', r.status_code, r.text[:80])
for _ in range(360):
    states = {m['id']: m['state'] for m in api('models').json()['items']}
    if all(states.get(i['id']) == 'INSTALLED' for i in (text, image) if i):
        break
    time.sleep(10)
print('installed', states)
json.dump({'text': text['id'], 'image': image and image['id'], 'text_name': text['display_name'], 'image_name': image and image['display_name']},
          open(WORK / 'lab_models.json', 'w'))
api(f"runtime/{text['id']}/start", 'POST')
for _ in range(60):
    rt = {r['model_id']: r for r in api('runtime').json()['items']}
    if rt.get(text['id'], {}).get('state') == 'RUNNING':
        break
    time.sleep(5)
entry = rt.get(text['id'], {})
check('UI-05', 'runtime lists display name and kind', entry.get('display_name') == text['display_name'] and entry.get('kind') == 'text', (entry.get('display_name'), entry.get('kind'), entry.get('state')))

print('\nRESULTS')
for item, oks in results.items():
    print(f"  {item:7} {'PASS' if all(oks) else 'FAIL'} ({sum(oks)}/{len(oks)})")
