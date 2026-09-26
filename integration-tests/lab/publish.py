from lab import ADMIN, ADMIN_PASSWORD, ADMIN_USER, BASE, CONSOLE_LOG, HOSTNAME, REPO, SSH_KEY, SPEECH_SAMPLE_URL, WORK  # noqa: F401
import httpx, json
m = json.load(open(WORK / 'lab_models.json'))
c = httpx.Client(base_url=BASE, verify=False, timeout=60)
r = c.post('/api/v1/aios/auth/login', json={'username': ADMIN_USER, 'password': ADMIN_PASSWORD}); c.headers['x-csrf-token'] = r.json()['csrf']
for key in (m['text'], m['image']):
    print('publish', c.patch(f'/api/v1/aios/models/{key}', json={'published': True}).status_code)
print('text runtime', [(x['display_name'][:30], x['state']) for x in c.get('/api/v1/aios/runtime').json()['items']])
