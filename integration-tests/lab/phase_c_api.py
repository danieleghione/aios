"""Phase C on the lab: notifications reach a webhook and a mailbox on the lab
host, API keys are counted and limited, the bundled embedding model answers,
and installed models report their revision state."""
import hashlib
import hmac
import json
import socketserver
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

import httpx

from lab import ADMIN_PASSWORD, ADMIN_USER, BASE, WORK

# The lab VM reaches its host at 10.0.2.2 (QEMU user networking).
HOST_FROM_VM = '10.0.2.2'
WEBHOOK_PORT, SMTP_PORT = 28180, 28125
SECRET = 'lab webhook secret'
c = httpx.Client(base_url=BASE, verify=False, timeout=900)
results = {}
hooks, mails = [], []


def check(item, name, ok, detail=''):
    results.setdefault(item, []).append(bool(ok))
    print(f"[{'OK ' if ok else 'BAD'}] {item:7} {name:56} {str(detail)[:150]}", flush=True)


def login():
    r = c.post('/api/v1/aios/auth/login', json={'username': ADMIN_USER, 'password': ADMIN_PASSWORD})
    c.headers['x-csrf-token'] = r.json()['csrf']


def api(path, method='GET', **kw):
    return c.request(method, '/api/v1/aios/' + path, **kw)


class Hook(BaseHTTPRequestHandler):
    def do_POST(self):
        body = self.rfile.read(int(self.headers['Content-Length']))
        hooks.append((dict(self.headers), body))
        self.send_response(204)
        self.end_headers()

    def log_message(self, *args):
        pass


class Mail(socketserver.StreamRequestHandler):
    """Just enough SMTP to accept one message."""
    def do(self, line):
        self.wfile.write(line.encode() + b'\r\n')

    def handle(self):
        self.do('220 lab ESMTP')
        data, lines = False, []
        for raw in self.rfile:
            line = raw.decode(errors='replace').rstrip('\r\n')
            if data:
                if line == '.':
                    mails.append('\n'.join(lines))
                    data, lines = False, []
                    self.do('250 queued')
                else:
                    lines.append(line)
                continue
            verb = line[:4].upper()
            if verb == 'EHLO':
                self.do('250 lab')
            elif verb == 'DATA':
                data = True
                self.do('354 go ahead')
            elif verb == 'QUIT':
                self.do('221 bye')
                return
            else:
                self.do('250 ok')


class Threaded(socketserver.ThreadingMixIn, socketserver.TCPServer):
    allow_reuse_address = True
    daemon_threads = True


for server in (HTTPServer(('0.0.0.0', WEBHOOK_PORT), Hook), Threaded(('0.0.0.0', SMTP_PORT), Mail)):
    threading.Thread(target=server.serve_forever, daemon=True).start()

login()
models = json.load(open(WORK / 'lab_models.json'))

# SEC-02: the webhook cannot reach the appliance's own services
for url in ('http://127.0.0.1:8090/v1', 'http://169.254.169.254/latest'):
    refused = api('system/notifications', 'PUT', json={'webhook_url': url})
    check('SEC-02', f'a webhook to {url.split("/")[2]} is refused', refused.status_code == 422, refused.text[:80])

# FEA-04: notifications
saved = api('system/notifications', 'PUT', json={
    'min_severity': 'WARNING', 'webhook_url': f'http://{HOST_FROM_VM}:{WEBHOOK_PORT}/aios', 'webhook_secret': SECRET,
    'email_to': ['ops@example.org'], 'email_from': 'aios@example.org', 'smtp_host': HOST_FROM_VM, 'smtp_port': SMTP_PORT,
    'smtp_security': 'none'})
check('FEA-04', 'webhook and e-mail saved, secrets not returned', saved.status_code == 200 and SECRET not in saved.text and saved.json()['webhook_secret_set'], saved.text[:80])
tested = api('system/notifications/test', 'POST').json()
check('FEA-04', 'a test reaches both', tested.get('results') == {'webhook': {'ok': True, 'error': ''}, 'email': {'ok': True, 'error': ''}}, tested)
check('FEA-04', 'the test webhook is signed', hooks and hooks[-1][0].get('X-AIOS-Signature') == 'sha256=' + hmac.new(SECRET.encode(), hooks[-1][1], hashlib.sha256).hexdigest(), '')
check('FEA-04', 'the test e-mail names the appliance', mails and 'Test message' in mails[-1] and 'Subject: [AIOS' in mails[-1], mails[-1][:80] if mails else '')
before = len(hooks)
created = api('repositories', 'POST', json={'name': 'Lab notified mirror', 'provider': 'http', 'url': 'https://huggingface.co/aios-lab/none/manifest.json',
                                           'enabled': True, 'config': {}})
failing = created.json().get('id', '')
api(f'repositories/{failing}/sync', 'POST')
for _ in range(40):
    if len(hooks) > before:
        break
    time.sleep(5)
alert = json.loads(hooks[-1][1]) if len(hooks) > before else {}
check('FEA-04', 'a real alert arrives by webhook within two minutes', alert.get('id') == 'repository:' + failing and alert.get('severity') == 'WARNING', alert)
check('FEA-04', 'and by e-mail', any('Lab notified mirror' in m for m in mails), len(mails))
api(f'repositories/{failing}', 'PUT', json={'name': 'Lab notified mirror', 'provider': 'http', 'url': 'https://huggingface.co/aios-lab/none/manifest.json',
                                           'enabled': False, 'config': {}})
api(f'alerts/repository:{failing}/dismiss', 'POST')
api('system/notifications', 'PUT', json={'webhook_url': '', 'webhook_secret': '', 'email_to': []})

# FEA-05: counted and limited keys
key = api('apikeys', 'POST', json={'name': 'limited', 'expires_days': 1, 'rate_limit': 3, 'models': [models['text']]}).json()
auth = {'Authorization': 'Bearer ' + key['key']}
listed = httpx.get(BASE + '/v1/models', headers=auth, verify=False).json()['data']
check('FEA-05', '/v1/models lists only the allowed model', [m['id'] for m in listed] == [models['text']], [m['id'][:8] for m in listed])
chat = {'model': models['text'], 'messages': [{'role': 'user', 'content': 'Say OK.'}], 'max_tokens': 8}
answers = [httpx.post(BASE + '/v1/chat/completions', headers=auth, json=chat, verify=False, timeout=900).status_code for _ in range(4)]
check('FEA-05', 'three requests a minute, the fourth refused', answers[:3] == [200, 200, 200] and answers[3] == 429, answers)
other = next((m['id'] for m in api('models').json()['items'] if m['id'] != models['text']), None)
if other:
    refused = httpx.post(BASE + '/v1/chat/completions', headers=auth, json={**chat, 'model': other}, verify=False, timeout=60)
    check('FEA-05', 'another model is refused', refused.status_code in (403, 429), refused.status_code)
row = next(k for k in api('apikeys').json()['items'] if k['id'] == key['id'])
check('FEA-05', 'requests and tokens counted', row['requests'] >= 3 and row['tokens'] > 0, {k: row[k] for k in ('requests', 'tokens', 'rate_limit')})
changed = api(f"apikeys/{key['id']}", 'PATCH', json={'rate_limit': 0, 'models': []})
check('FEA-05', 'limits changed without a new key', changed.status_code == 200 and
      httpx.post(BASE + '/v1/chat/completions', headers=auth, json=chat, verify=False, timeout=900).status_code == 200, changed.text[:60])

# FEA-07: embeddings from the bundled model
started = time.monotonic()
embedded = httpx.post(BASE + '/v1/embeddings', headers=auth, json={'model': 'all-MiniLM-L6-v2', 'input': ['a cat sits on the mat', 'a kitten rests on a rug', 'quarterly tax report']},
                      verify=False, timeout=600)
body = embedded.json() if embedded.status_code == 200 else {}
vectors = [d['embedding'] for d in body.get('data', [])]
check('FEA-07', 'three vectors of 384 dimensions', embedded.status_code == 200 and len(vectors) == 3 and all(len(v) == 384 for v in vectors),
      f'{embedded.status_code} {round(time.monotonic() - started, 1)} s {embedded.text[:80] if embedded.status_code != 200 else ""}')
if len(vectors) == 3:
    dot = lambda a, b: sum(x * y for x, y in zip(a, b))
    check('FEA-07', 'similar sentences are closer', dot(vectors[0], vectors[1]) > dot(vectors[0], vectors[2]),
          (round(dot(vectors[0], vectors[1]), 3), round(dot(vectors[0], vectors[2]), 3)))
    check('FEA-07', 'usage counted in tokens', body.get('usage', {}).get('prompt_tokens', 0) > 0, body.get('usage'))
api(f"apikeys/{key['id']}", 'DELETE')

# FEA-02: revision state of installed models
items = api('models').json()['items']
check('FEA-02', 'every installed model reports its revision state', items and all('update' in m and 'replaced_by' in m for m in items), len(items))
current = api(f"models/{models['text']}/update", 'POST', json={})
check('FEA-02', 'the newest revision is not updated again', current.status_code == 409 and 'newest revision' in current.text, current.text[:80])

print('\nRESULTS')
for item, oks in results.items():
    print(f"  {item:7} {'PASS' if all(oks) else 'FAIL'} ({sum(oks)}/{len(oks)})")
