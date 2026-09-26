"""Put the lab appliance in the state the demo screenshots show: a model
answering, backups on a schedule, notifications, a key for n8n, a second user,
and a recording made by the appliance's own voice. Every name and address is
an example; nothing belongs to a real account."""
import json
import secrets
import time

import httpx

from lab import ADMIN_PASSWORD, ADMIN_USER, BASE, WORK

c = httpx.Client(base_url=BASE, verify=False, timeout=1800)
r = c.post('/api/v1/aios/auth/login', json={'username': ADMIN_USER, 'password': ADMIN_PASSWORD})
c.headers['x-csrf-token'] = r.json()['csrf']
api = lambda path, method='GET', **kw: c.request(method, '/api/v1/aios/' + path, **kw)
CHAT_MODEL = ('granite-4.2-3b', 'Q4_K_M')


def install_chat_model():
    """A small conversational model answers the chat in the pictures."""
    items = [i for off in (0, 200, 400) for i in api('catalog', params={'limit': 200, 'offset': off}).json()['items']]
    wanted = next((i for i in items if CHAT_MODEL[0] in i['model_id'].lower() and i.get('quantization') == CHAT_MODEL[1]), None)
    if not wanted:
        return None
    if not any(m['id'] == wanted['id'] for m in api('models').json()['items']):
        api(f"models/{wanted['id']}/install", 'POST', json={'accept_license': True, 'override_compatibility': True})
        for _ in range(360):
            if any(m['id'] == wanted['id'] and m['state'] in ('INSTALLED', 'PUBLISHED') for m in api('models').json()['items']):
                break
            time.sleep(5)
    api(f"models/{wanted['id']}", 'PATCH', json={'published': True, 'default': True})
    return wanted['id']


print('chat model', install_chat_model())
# What the checks left behind is not part of the demo.
for key in api('apikeys').json()['items']:
    if not key['revoked_at'] and key['name'] != 'n8n':
        api(f"apikeys/{key['id']}", 'DELETE')
for repo in api('repositories').json()['items']:
    if repo['enabled'] and repo['status'] == 'NOT CONFIGURED':
        api(f"repositories/{repo['id']}", 'PUT', json={'name': repo['name'], 'provider': repo['provider'], 'url': repo['url'], 'enabled': False, 'config': repo['config']})
# The checks signed in many times: end every session but this one.
for session in api('auth/sessions').json()['items']:
    if not session['current']:
        api(f"auth/sessions/{session['id']}", 'DELETE')
        time.sleep(0.5)  # the sign-in area allows five requests a second
models = {}
for m in api('models').json()['items']:
    if m['kind'] not in models or m.get('is_default'):
        models[m['kind']] = m
print('models', {k: m['display_name'] for k, m in models.items()})
for kind in ('text', 'image', 'speech', 'voice'):
    if kind in models and not models[kind]['published']:
        api(f"models/{models[kind]['id']}", 'PATCH', json={'published': True})
api('backups/schedule', 'PUT', json={'enabled': True, 'hour': 2, 'keep': 7, 'include_models': False})
api('backups/passphrase', 'PUT', json={'passphrase': secrets.token_urlsafe(18)})
api('backups', 'POST', json={'include_models': False})
api('system/notifications', 'PUT', json={'min_severity': 'WARNING', 'webhook_url': 'https://n8n.example.org/webhook/aios-alerts',
                                          'email_to': ['it-team@example.org'], 'email_from': 'aios@example.org',
                                          'smtp_host': 'smtp.example.org', 'smtp_port': 587, 'smtp_security': 'starttls'})
names = [k['name'] for k in api('apikeys').json()['items'] if not k['revoked_at']]
if 'n8n' not in names:
    api('apikeys', 'POST', json={'name': 'n8n', 'expires_days': 365, 'rate_limit': 60, 'models': [models['text']['id']]})
users = [u['username'] for u in api('users').json()['items']]
if 'analyst@example.org' not in users:
    api('users', 'POST', json={'username': 'analyst@example.org', 'password': secrets.token_urlsafe(18), 'role': 'VIEWER'})
key = api('apikeys', 'POST', json={'name': 'scripts', 'expires_days': 30}).json()['key']
for question in ('Write a Python function that returns the sum of a list.', 'Explain in one sentence what a unit test is.'):
    httpx.post(BASE + '/v1/chat/completions', headers={'Authorization': 'Bearer ' + key}, verify=False, timeout=900,
               json={'model': models['text']['id'], 'messages': [{'role': 'user', 'content': question}], 'max_tokens': 120})
spoken = api('speech/synthesize', 'POST', json={'model': models['voice']['id'], 'voice': 'nova',
                                                 'input': "Good morning, everyone. Tomorrow's meeting has moved to half past ten, in the large room."})
(WORK / 'demo-recording.wav').write_bytes(spoken.content)
print('recording', spoken.status_code, len(spoken.content))
