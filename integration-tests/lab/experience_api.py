"""Round 2 on the lab: block 3 (API side), ENG-01..05. Run after backlog_api.py."""
from lab import ADMIN, ADMIN_PASSWORD, ADMIN_USER, BASE, CONSOLE_LOG, HOSTNAME, REPO, SSH_KEY, SPEECH_SAMPLE_URL, WORK  # noqa: F401
import json, threading, time
import httpx

S = str(WORK) + '/'
B = BASE
c = httpx.Client(base_url=B, verify=False, timeout=600)
results = {}


def check(item, name, ok, detail=''):
    results.setdefault(item, []).append(bool(ok))
    print(f"[{'OK ' if ok else 'BAD'}] {item:7} {name:58} {str(detail)[:150]}", flush=True)


def login():
    r = c.post('/api/v1/aios/auth/login', json={'username': ADMIN_USER, 'password': ADMIN_PASSWORD})
    c.headers['x-csrf-token'] = r.json()['csrf']


def api(path, method='GET', **kw):
    return c.request(method, '/api/v1/aios/' + path, **kw)


login()
models = json.load(open(S + 'lab_models.json'))

# UX-01 / UX-02: grouped catalogue, parameters from names
flat = api('catalog', params={'limit': 1}).json()['total']
grouped = api('catalog', params={'limit': 200, 'group': True}).json()
groups = grouped['items']
check('UX-01', 'one entry per model', 0 < grouped['total'] < flat, f"{grouped['total']} models for {flat} files")
check('UX-01', 'every group recommends one of its files', all(g['recommended'] in {v['id'] for v in g['variants']} for g in groups), '')
multi = [g for g in groups if len(g['variants']) > 1]
check('UX-01', 'recommended file has the best fit of its group', all(
    ['OPTIMAL', 'COMPATIBLE', 'LIMITED', 'NOT_RECOMMENDED', 'INCOMPATIBLE'].index(next(v for v in g['variants'] if v['id'] == g['recommended'])['compatibility']['classification'])
    == min(['OPTIMAL', 'COMPATIBLE', 'LIMITED', 'NOT_RECOMMENDED', 'INCOMPATIBLE'].index(v['compatibility']['classification']) for v in g['variants'])
    for g in multi), f'{len(multi)} groups with several files')
named = [g for g in groups if g.get('parameter_source') == 'name']
declared = [g for g in groups if g.get('parameter_count') and g.get('parameter_source') != 'name']
check('UX-02', 'parameters read from names where not declared', named, f"{len(named)} from name, {len(declared)} declared, {sum(1 for g in groups if not g.get('parameter_count'))} unknown; e.g. {[(g['model_id'], g['parameter_count']) for g in named[:3]]}")

# UX-05 audit names, UX-06 logs, UX-07 jobs, UX-09 TLS, UX-10 validation
audit = api('audit').json()['items']
check('UX-05', 'audit names the actor', audit and audit[0]['actor_name'] == ADMIN_USER, (audit[0]['event']['action'], audit[0]['actor_name']))
logs = api('logs', params={'source': 'control-plane'}).json()
times = [x['time'] for x in logs['items']]
check('UX-06', 'newest first', times and times == sorted(times, reverse=True), f'{len(times)} lines')
check('UX-06', 'more services', {'repository-sync', 'hardware-profiler', 'nvidia-driver', 'update-check', 'firstboot'} <= set(logs['sources']), logs['sources'])
owui = api('logs', params={'source': 'open-webui'}).json()['items']
check('UX-06', 'no raw byte arrays or colour codes', owui and not any(x['message'].startswith('[') and x['message'][1:4].isdigit() or '\x1b' in x['message'] for x in owui), owui[0]['message'][:80] if owui else '')
for source in logs['sources']:
    r = api('logs', params={'source': source})
    if r.status_code != 200:
        check('UX-06', f'source {source} readable', False, r.status_code)
tls = api('system/tls').json()['certificate']
check('UX-09', 'current certificate read', tls and tls['days_left'] > 300 and tls['self_signed'], tls and {k: tls[k] for k in ('subject', 'days_left', 'names')})
system = api('system/settings').json()['system']
check('UX-10', 'hostname ending in a hyphen refused', api('system/config', 'POST', json={**system, 'hostname': 'lab-'}).status_code == 422, '')
check('UX-10', 'unknown time zone refused', api('system/config', 'POST', json={**system, 'timezone': 'Mars/Olympus'}).status_code == 422, '')

# UX-08 dashboard
dash = api('system/dashboard').json()
check('UX-08', 'CPU history available at open', len(dash['cpu_history']) >= 10, f"{len(dash['cpu_history'])} samples")

# ENG-01 memory rating on a 4 GiB machine
flat_items = api('catalog', params={'limit': 200}).json()['items']
slow = [i for i in flat_items if any('will be slow' in r for r in i['compatibility']['reasons'])]
too_big = [i for i in flat_items if i['size'] > 3 * 2**30 and i.get('kind') != 'image']
check('ENG-01', 'files that cannot stay resident are not OPTIMAL/COMPATIBLE', all(i['compatibility']['classification'] not in ('OPTIMAL', 'COMPATIBLE') for i in too_big), f'{len(too_big)} files over 3 GiB, {len(slow)} rated slow')

# ENG-04 client API keys (the gateway answers only published models)
api(f"models/{models['text']}", 'PATCH', json={'published': True})
created = api('apikeys', 'POST', json={'name': 'lab test', 'expires_days': 30}).json()
body = {'model': models['text'], 'messages': [{'role': 'user', 'content': 'Say OK.'}], 'max_tokens': 8}
r = httpx.post(B + '/v1/chat/completions', json=body, headers={'Authorization': 'Bearer ' + created['key']}, verify=False, timeout=600)
check('ENG-04', 'client key answers a chat request', r.status_code == 200, r.text[:100])
listed = api('apikeys').json()['items']
check('ENG-04', 'last use recorded, key never listed', listed[0]['last_used'] and created['key'] not in json.dumps(listed), listed[0]['prefix'])
api(f"apikeys/{created['id']}", 'DELETE')
r = httpx.post(B + '/v1/chat/completions', json=body, headers={'Authorization': 'Bearer ' + created['key']}, verify=False, timeout=60)
check('ENG-04', 'revoked key refused', r.status_code == 401, r.status_code)

# ENG-05 four slots over a shared KV cache, two requests at once
rt = {r['model_id']: r for r in api('runtime').json()['items']}[models['text']]
log = api(f"runtime/{rt['id']}/log").json()['text']
check('ENG-05', 'engine started with 4 slots and unified KV', rt.get('slots') == 4 and "n_slots = 4" in log and "kv_unified = 'true'" in log,
      [line[-80:] for line in log.splitlines() if 'n_slots' in line][-1:])
key = api('apikeys', 'POST', json={'name': 'parallel', 'expires_days': 1}).json()['key']
spans = []


def ask(n):
    t0 = time.time()
    r = httpx.post(B + '/v1/chat/completions', json={**body, 'messages': [{'role': 'user', 'content': f'Count from 1 to 20. ({n})'}], 'max_tokens': 60},
                   headers={'Authorization': 'Bearer ' + key}, verify=False, timeout=900)
    spans.append((t0, time.time(), r.status_code))


threads = [threading.Thread(target=ask, args=(n,)) for n in range(2)]
[t.start() for t in threads]
[t.join() for t in threads]
overlap = min(s[1] for s in spans) - max(s[0] for s in spans)
check('ENG-05', 'two requests served at the same time', all(s[2] == 200 for s in spans) and overlap > 0, f'overlap {overlap:.1f} s, durations {[round(s[1] - s[0], 1) for s in spans]}')

# ENG-02 libllama load messages visible again
check('ENG-02', 'libllama load messages in the runtime log', 'load_tensors' in log and 'print_info' in log,
      [line[-90:] for line in log.splitlines() if 'load_tensors' in line][:2])

print('\nRESULTS')
for item, oks in results.items():
    print(f"  {item:7} {'PASS' if all(oks) else 'FAIL'} ({sum(oks)}/{len(oks)})")
