"""Phase D on the lab: the voice engine. A voice model is discovered, installed
with its codec and published; it speaks through the OpenAI route and from the
portal, and the speech model installed earlier hears what it said. Run on a VM
with enough memory for the voice model (see run.sh)."""
import io
import json
import time
import wave

import httpx

from lab import ADMIN_PASSWORD, ADMIN_USER, BASE, WORK

c = httpx.Client(base_url=BASE, verify=False, timeout=900)
results = {}
SENTENCE = 'Good morning. This appliance can read your answers aloud.'


def check(item, name, ok, detail=''):
    results.setdefault(item, []).append(bool(ok))
    print(f"[{'OK ' if ok else 'BAD'}] {item:7} {name:56} {str(detail)[:150]}", flush=True)


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


def api(path, method='GET', **kw):
    return c.request(method, '/api/v1/aios/' + path, **kw)


def pitch(audio):
    """Median fundamental frequency of a WAV, in Hz: enough to tell one voice
    from another without listening. Plain Python, at 8 kHz."""
    with wave.open(io.BytesIO(audio)) as reader:
        rate, raw = reader.getframerate(), reader.readframes(reader.getnframes())
    step = max(1, rate // 8000)
    samples = [int.from_bytes(raw[i:i + 2], 'little', signed=True) for i in range(0, len(raw) - 1, 2 * step)]
    rate //= step
    frame, found = int(0.04 * rate), []
    for start in range(0, len(samples) - frame, int(0.01 * rate)):
        seg = samples[start:start + frame]
        mean = sum(seg) / frame
        seg = [x - mean for x in seg]
        energy = sum(x * x for x in seg)
        if energy / frame < 300 ** 2:
            continue
        best, lag = 0.0, 0
        for candidate in range(rate // 400, rate // 70):
            value = sum(seg[i] * seg[i + candidate] for i in range(frame - candidate))
            if value > best:
                best, lag = value, candidate
        if lag and best > 0.3 * energy:
            found.append(rate / lag)
    found.sort()
    return found[len(found) // 2] if found else 0


def seconds_of(audio):
    with wave.open(io.BytesIO(audio)) as reader:
        return reader.getnframes() / reader.getframerate()


login()
repos = {r['name']: r for r in api('repositories').json()['items']}
voice_repo = repos.get('Voice models (text to speech)')
check('FEA-06', 'the voice repository ships disabled', voice_repo and not voice_repo['enabled'], voice_repo and voice_repo['status'])
api(f"repositories/{voice_repo['id']}", 'PUT', json={'name': voice_repo['name'], 'provider': 'voice', 'url': voice_repo['url'],
                                                     'enabled': True, 'config': voice_repo['config']})
api(f"repositories/{voice_repo['id']}/sync", 'POST')
for _ in range(60):
    state = next(r for r in api('repositories').json()['items'] if r['id'] == voice_repo['id'])
    if state['status'] != 'SYNCING' and state.get('last_sync'):
        break
    time.sleep(5)
check('FEA-06', 'voice models discovered', state['status'] == 'ONLINE' and state['found'] >= 1, f"{state['found']} {state.get('error') or ''}")
items = [i for off in (0, 200, 400) for i in api('catalog', params={'limit': 200, 'offset': off}).json()['items']]
voices = [i for i in items if i.get('kind') == 'voice']
check('FEA-06', 'rated as voice models, with their codec', voices and all(i['compatibility']['kind'] == 'voice' for i in voices),
      [(i['display_name'], i['compatibility']['classification']) for i in voices[:2]])
check('FEA-06', 'no voice backbone among the language models', not [i for i in items if i.get('kind', 'text') == 'text' and str(i.get('architecture', '')).lower().endswith('tts')], '')
smallest = min(voices, key=lambda i: i['size'])
api(f"models/{smallest['id']}/install", 'POST', json={'accept_license': True, 'override_compatibility': True})
entry = {}
for _ in range(360):
    entry = {m['id']: m for m in api('models').json()['items']}.get(smallest['id'], {})
    failed = [d for d in api('downloads').json()['items'] if d['model_id'] == smallest['id'] and d['state'] == 'FAILED']
    if entry.get('state') in ('INSTALLED', 'PUBLISHED') or failed:
        break
    time.sleep(5)
check('FEA-06', 'installed with its codec', entry.get('state') in ('INSTALLED', 'PUBLISHED') and entry.get('kind') == 'voice',
      f"{entry.get('state')} {smallest['display_name']}")

# From the portal, published or not.
started = time.time()
r = api('speech/synthesize', 'POST', json={'model': smallest['id'], 'input': 'Buongiorno. Questa è la voce della appliance.', 'language': 'it'}, timeout=1800)
check('FEA-06', 'spoken from the portal (Italian)', r.status_code == 200 and r.content[:4] == b'RIFF' and seconds_of(r.content) > 1.5,
      f"{r.status_code} {round(time.time() - started)} s, {seconds_of(r.content) if r.status_code == 200 else r.text[:80]}")

# Published: the chat's read-aloud button points at it.
api(f"models/{smallest['id']}", 'PATCH', json={'published': True})
job = None
for _ in range(30):
    job = next((j for j in api('system/jobs').json()['items'] if j['action'] == 'chat-voice'), None)
    if job and job['state'] in ('COMPLETED', 'FAILED'):
        break
    time.sleep(3)
check('FEA-06', 'the chat is pointed at the voice', job and job['state'] == 'COMPLETED' and 'audio.tts.engine' in json.dumps(job.get('result')), job)

# Through the OpenAI route, and heard again by the speech model.
key = api('apikeys', 'POST', json={'name': 'voice round trip', 'expires_days': 1}).json()['key']
auth = {'Authorization': 'Bearer ' + key}
started = time.time()
r = httpx.post(BASE + '/v1/audio/speech', headers=auth, json={'model': 'aios-voice', 'input': SENTENCE, 'voice': 'alloy', 'response_format': 'wav'}, verify=False, timeout=1800)
spoken = r.content if r.status_code == 200 else b''
check('FEA-06', '/v1/audio/speech answers a WAV', spoken[:4] == b'RIFF' and seconds_of(spoken) > 2,
      f"{r.status_code} {round(time.time() - started)} s {seconds_of(spoken) if spoken else r.text[:80]}")
if spoken:
    heard = httpx.post(BASE + '/v1/audio/transcriptions', headers=auth, files={'file': ('spoken.wav', spoken, 'audio/wav')}, data={'language': 'en'},
                       verify=False, timeout=900)
    text = heard.json().get('text', '').lower() if heard.status_code == 200 else ''
    check('FEA-06', 'the speech model hears what was said', 'morning' in text and 'aloud' in text, text[:120] or heard.text[:120])
# One voice for a whole text: the chat sends sentence by sentence.
listed_voices = [v['id'] for v in httpx.get(BASE + '/v1/audio/voices', verify=False).json()['voices']]
check('FEA-06', 'six voices offered to the chat', listed_voices == ['alloy', 'echo', 'fable', 'onyx', 'nova', 'shimmer'], listed_voices)
pitches = {}
for name in ('onyx', 'nova'):
    for sentence in ('Oggi presentiamo il nuovo progetto.', 'Le prime prove sono andate bene.'):
        r = httpx.post(BASE + '/v1/audio/speech', headers=auth, json={'input': sentence, 'voice': name, 'response_format': 'wav'}, verify=False, timeout=1800)
        pitches.setdefault(name, []).append(round(pitch(r.content)) if r.status_code == 200 else 0)
# Intonation moves a voice by up to about a sixth from one sentence to the
# next (more for the high voices); without a reference the voice itself
# changed, from low to high. Each voice stays in its own band, apart from the other.
steady = all(v and min(v) > 0 and (max(v) - min(v)) <= 0.2 * max(v) for v in pitches.values())
apart = min(pitches['nova']) - max(pitches['onyx']) > 40
check('FEA-06', 'each voice keeps its pitch from sentence to sentence', steady and apart, pitches)
mp3 = httpx.post(BASE + '/v1/audio/speech', headers=auth, json={'input': 'Hello.'}, verify=False, timeout=1800)
check('FEA-06', 'MP3 by default, as OpenAI clients expect', mp3.status_code == 200 and mp3.headers.get('content-type') == 'audio/mpeg' and len(mp3.content) > 1000,
      f"{mp3.status_code} {mp3.headers.get('content-type')} {len(mp3.content)}")
listed = httpx.get(BASE + '/v1/models', headers=auth, verify=False).json()['data']
kinds = {m['id']: m.get('kind') for m in api('models').json()['items']}
check('COD-03', 'the chat lists language models only', listed and all(kinds.get(m['id']) == 'text' for m in listed), [kinds.get(m['id']) for m in listed])

# PLT-05 without an NVIDIA card: no package, nothing to remove, the engines as before.
cuda = api('hardware/profile').json().get('cuda')
check('PLT-05', 'the CUDA package is reported as not installed', cuda == {'installed': False, 'version': ''}, cuda)
check('PLT-05', 'removing an absent package is refused', api('system/update/cuda/remove', 'POST').status_code == 409, '')

print('\nRESULTS')
for item, oks in results.items():
    print(f"  {item:7} {'PASS' if all(oks) else 'FAIL'} ({sum(oks)}/{len(oks)})")
