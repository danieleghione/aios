"""The OpenAI-compatible gateway: loading a model on request, inference slots,
client keys and the requests forwarded to the engines."""
import asyncio
import contextlib
import hashlib
import hmac
import json
import sqlite3
import time
from collections import deque
from typing import Literal

import httpx
from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, Response, UploadFile
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from starlette.background import BackgroundTask

from . import imaging
from .core import ETC, audit, execute, now, one, rows, setting, uid
from .runtime import RuntimeConfig, kind_of, model_kind, model_metadata as runtime_metadata, parallel_slots, runtime_port, unusable
from .voice import DEFAULT_VOICE, VOICES
from .web import ACTIVE, LATENCY, REQUESTS, TOKENS, admin, operator, viewer

router = APIRouter()

_SLOTS: dict[tuple[str, int], asyncio.Semaphore] = {}


# Requests that have claimed a model, from the moment it is confirmed loaded until
# their slot is released. A model with any is never unloaded to make room.
_BUSY: dict[str, int] = {}
_LOAD_LOCK = asyncio.Lock()
LOAD_TIMEOUT = 900
# One image can take several minutes on a CPU; the gateway must wait for it.
IMAGE_TIMEOUT = 1800
SETTLE_POLL = 1.0


def claim(model_id):
    _BUSY[model_id] = _BUSY.get(model_id, 0) + 1
    mark_use(model_id)


def unclaim(model_id):
    _BUSY[model_id] = max(0, _BUSY.get(model_id, 0) - 1)
    mark_use(model_id)


def mark_use(model_id):
    """Tell the runtime manager, which unloads idle models, that this one is in
    use and when it was last. Bookkeeping: it never fails a request."""
    try:
        execute('UPDATE runtime_instances SET busy=?,last_used=? WHERE model_id=?', (_BUSY.get(model_id, 0), now(), model_id))
    except sqlite3.Error:
        pass


class Slot:
    """One inference slot. Release is idempotent so every exit path - normal end,
    upstream error, client disconnect before the stream starts, cancellation - can
    call it without ever freeing a slot twice."""
    def __init__(self, semaphore, model_id=''):
        self.semaphore, self.model_id, self.held = semaphore, model_id, True
        ACTIVE.inc()

    def release(self):
        if self.held:
            self.held = False
            self.semaphore.release()
            ACTIVE.dec()
            unclaim(self.model_id)


LIVE = ("SELECT r.* FROM runtime_instances r JOIN installed_models i ON i.id=r.model_id "
        "WHERE r.model_id=? AND i.published=1 AND r.state='RUNNING' AND r.desired='RUNNING'")
# The portal tests a model the operator installed, published or not: publishing
# decides what the chat and the API may use, not what its owner may try.
LIVE_ANY = ("SELECT r.* FROM runtime_instances r JOIN installed_models i ON i.id=r.model_id "
            "WHERE r.model_id=? AND r.state='RUNNING' AND r.desired='RUNNING'")


async def settle(model_id, states, timeout):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        row = one('SELECT state,error FROM runtime_instances WHERE model_id=?', (model_id,))
        if not row or row['state'] in states:
            return row
        await asyncio.sleep(SETTLE_POLL)
    return one('SELECT state,error FROM runtime_instances WHERE model_id=?', (model_id,))


async def ensure_running(model_id, published_only=True):
    """Serve a model on request, loading it if needed. Choosing a model in the chat
    finds it ready even when nobody started it by hand. Returns with the model
    claimed, so a concurrent load of another model cannot unload it before this
    request reaches it. The portal may also try a model it has not published."""
    live = LIVE if published_only else LIVE_ANY
    row = one(live, (model_id,))
    if row:
        claim(model_id)  # no await since the check: nothing can interleave
        return row
    installed = one('SELECT * FROM installed_models WHERE id=?' + (' AND published=1' if published_only else ''), (model_id,))
    if not installed:
        raise HTTPException(404, 'Model is not installed and published' if published_only else 'Model is not installed')
    if unusable(installed):
        raise HTTPException(409, unusable(installed))
    async with _LOAD_LOCK:
        row = one(live, (model_id,))
        if row:
            claim(model_id)
            return row
        # V1 keeps a single model in memory: make room, but never under a model
        # that is still answering someone.
        for other in rows("SELECT model_id FROM runtime_instances WHERE model_id!=? AND (desired!='STOPPED' OR state IN ('RUNNING','STARTING'))", (model_id,)):
            # A language model and an image model use different engines and can be
            # loaded together; two of the same kind cannot.
            if model_kind(other['model_id']) != model_kind(model_id):
                continue
            if _BUSY.get(other['model_id']):
                raise HTTPException(409, 'Another model is answering requests; retry when it has finished')
            execute("UPDATE runtime_instances SET desired='STOPPED' WHERE model_id=?", (other['model_id'],))
            audit('gateway', 'runtime_unload_for_request', other['model_id'], {'requested': model_id})
            await settle(other['model_id'], ('STOPPED', 'FAILED'), 120)
        model = one('SELECT config FROM installed_models WHERE id=?', (model_id,))
        # A previous FAILED state must not be mistaken for the outcome of this attempt.
        execute("INSERT INTO runtime_instances(id,model_id,state,port,config,desired) VALUES (?,?,'STOPPED',8090,?,'RUNNING') "
                "ON CONFLICT(model_id) DO UPDATE SET desired='RUNNING',config=excluded.config,error=NULL,"
                "state=CASE WHEN state IN ('RUNNING','STARTING') THEN state ELSE 'STOPPED' END", (uid(), model_id, model['config']))
        audit('gateway', 'runtime_load_on_request', model_id)
        outcome = await settle(model_id, ('RUNNING', 'FAILED'), LOAD_TIMEOUT)
        row = one(live, (model_id,))
        if not row:
            reason = outcome.get('error') if outcome else None
            raise HTTPException(503, 'Model could not be loaded: ' + (reason or 'load did not finish in time'))
        claim(model_id)
        return row


@contextlib.asynccontextmanager
async def serving(model_id, published_only=True):
    """A model loaded and claimed for one request, released however it ends."""
    row = await ensure_running(model_id, published_only)
    try:
        yield row
    finally:
        unclaim(model_id)


async def acquire_slot(model_id, config):
    # Open WebUI sends several requests per message (answer, title, tags,
    # follow-ups). Rejecting all but the first made the user's own question fail
    # whenever a helper request got there first; queue them instead, up to the
    # configured request timeout. Semaphore.acquire is cancellation-safe.
    # As many as the engine was started with, AUTO's shared slots included.
    slots = parallel_slots(config, config.context)
    semaphore = _SLOTS.setdefault((model_id, slots), asyncio.Semaphore(slots))
    try:
        async with asyncio.timeout(config.timeout):
            await semaphore.acquire()
    except TimeoutError:
        raise HTTPException(429, f'Model busy: no inference slot freed within {config.timeout} s') from None
    return Slot(semaphore, model_id)


@router.get('/v1/models')
def public_models(request: Request):
    # The list names what this appliance runs: it is for clients holding a key.
    caller = caller_of(request)
    # Only language models answer chat: an image or speech model listed here would
    # sit in the chat's model selector, where it can only fail.
    return {'object': 'list', 'data': [{'id': r['id'], 'name': json.loads(r['metadata'])['display_name'], 'object': 'model', 'created': int(r['installed_at']), 'owned_by': 'aios'}
                                       for r in rows('SELECT i.id,i.installed_at,d.metadata FROM installed_models i JOIN discovered_models d ON d.id=i.id WHERE i.published=1')
                                       if kind_of(json.loads(r['metadata'])) == 'text' and (not caller['models'] or r['id'] in caller['models'])]}

class InferenceRequest(BaseModel):
    model: str = Field(default='', max_length=256)
    stream: bool = False
    model_config = {'extra': 'allow'}

def count_usage(value, caller=None):
    usage = value.get('usage') or {}
    prompt, completion = max(0, usage.get('prompt_tokens', 0) or 0), max(0, usage.get('completion_tokens', 0) or 0)
    TOKENS.labels('input').inc(prompt)
    TOKENS.labels('output').inc(completion)
    if caller and caller.get('id') and prompt + completion:
        try:
            execute('UPDATE api_keys SET tokens=tokens+? WHERE id=?', (prompt + completion, caller['id']))
        except sqlite3.Error:
            pass


# The chat's own key: no limit, every model.
CHAT = {'id': '', 'name': 'Open WebUI', 'rate_limit': 0, 'models': None}
_WINDOWS: dict[str, deque] = {}


def inference_authorised(header):
    """Who is calling: the chat, or a client key that is neither revoked nor
    expired. None when neither."""
    secret = ETC / 'secrets/inference-key'
    if secret.exists() and hmac.compare_digest(header, 'Bearer ' + secret.read_text().strip()):
        return CHAT
    if not header.startswith('Bearer aios_'):
        return None
    digest = hashlib.sha256(header[7:].encode()).hexdigest()
    row = one('SELECT id,name,expires_at,last_used,rate_limit,models FROM api_keys WHERE digest=? AND revoked_at IS NULL', (digest,))
    if not row or (row['expires_at'] and row['expires_at'] < now()):
        return None
    if not row['last_used'] or now() - row['last_used'] > 60:
        execute('UPDATE api_keys SET last_used=? WHERE id=?', (now(), row['id']))
    return {**row, 'models': json.loads(row['models']) if row['models'] else None}


def admit(caller):
    """Count a request against its key, within the key's requests per minute."""
    if not caller.get('id'):
        return
    limit = caller.get('rate_limit') or 0
    if limit:
        window = _WINDOWS.setdefault(caller['id'], deque())
        current = time.monotonic()
        while window and current - window[0] >= 60:
            window.popleft()
        if len(window) >= limit:
            raise HTTPException(429, f'This key is limited to {limit} requests per minute',
                                headers={'Retry-After': str(max(1, int(60 - (current - window[0]))))})
        window.append(current)
    try:
        execute('UPDATE api_keys SET requests=requests+1 WHERE id=?', (caller['id'],))
    except sqlite3.Error:
        pass


def permit(caller, model_id):
    """A key limited to some models reaches only those."""
    if caller.get('models') and model_id not in caller['models']:
        raise HTTPException(403, 'This key may not use this model')


def caller_of(request):
    caller = inference_authorised(request.headers.get('authorization', ''))
    if not caller:
        raise HTTPException(401, 'Inference API key required')
    return caller


class KeyLimits(BaseModel):
    # Requests per minute, 0 for no limit; the models the key may use, none for all.
    rate_limit: int = Field(default=0, ge=0, le=10000)
    models: list[str] = Field(default_factory=list, max_length=100)


class APIKeyRequest(KeyLimits):
    name: str = Field(min_length=1, max_length=80)
    expires_days: int = Field(default=0, ge=0, le=3650)


def known_models(models):
    unknown = [m for m in models if not one('SELECT id FROM installed_models WHERE id=?', (m,))]
    if unknown:
        raise HTTPException(422, 'Unknown model: ' + unknown[0])
    return json.dumps(sorted(set(models))) if models else None


@router.get('/api/v1/aios/apikeys')
def api_keys(user=Depends(admin)):
    return {'items': [{**r, 'models': json.loads(r['models']) if r['models'] else []}
                      for r in rows('SELECT id,name,prefix,created_at,expires_at,last_used,revoked_at,requests,tokens,rate_limit,models '
                                    'FROM api_keys ORDER BY revoked_at IS NOT NULL, created_at DESC')]}


@router.patch('/api/v1/aios/apikeys/{key}')
def limit_api_key(key: str, data: KeyLimits, user=Depends(admin)):
    """Change what a key may do without handing out a new one."""
    if not one('SELECT id FROM api_keys WHERE id=? AND revoked_at IS NULL', (key,)):
        raise HTTPException(404, 'API key not found')
    execute('UPDATE api_keys SET rate_limit=?,models=? WHERE id=?', (data.rate_limit, known_models(data.models), key))
    _WINDOWS.pop(key, None)
    audit(user['id'], 'api_key_limited', key, data.model_dump())
    return {'id': key, **data.model_dump()}


@router.post('/api/v1/aios/apikeys')
def create_api_key(data: APIKeyRequest, user=Depends(admin)):
    import secrets
    key, value = uid(), 'aios_' + secrets.token_urlsafe(32)
    expires = now() + data.expires_days * 86400 if data.expires_days else None
    execute('INSERT INTO api_keys(id,name,prefix,digest,created_by,created_at,expires_at,rate_limit,models) VALUES (?,?,?,?,?,?,?,?,?)',
            (key, data.name, value[:12], hashlib.sha256(value.encode()).hexdigest(), user['id'], now(), expires, data.rate_limit, known_models(data.models)))
    audit(user['id'], 'api_key_created', key, {'name': data.name, 'expires_at': expires, 'rate_limit': data.rate_limit, 'models': data.models})
    # Shown once: only its digest is kept.
    return {'id': key, 'key': value, 'name': data.name, 'expires_at': expires}


@router.delete('/api/v1/aios/apikeys/{key}')
def revoke_api_key(key: str, user=Depends(admin)):
    row = one('SELECT name FROM api_keys WHERE id=? AND revoked_at IS NULL', (key,))
    if not row:
        raise HTTPException(404, 'API key not found')
    execute('UPDATE api_keys SET revoked_at=? WHERE id=?', (now(), key))
    audit(user['id'], 'api_key_revoked', key, {'name': row['name']})
    return {'id': key, 'revoked': True}


# Speech to text. Audio arrives as a file, so this route speaks multipart rather
# than JSON, and answers in the shape OpenAI clients expect: {"text": ...}.
AUDIO_TIMEOUT = 1800
AUDIO_MAX_BYTES = 200 * 1024 ** 2


async def transcribe(upload, model_id='', language='', prompt='', published_only=True):
    """Send one audio file to the speech engine and return what it heard."""
    model_id = model_id if model_kind(model_id) == 'speech' else setting('default_speech_model', '')
    if not model_id:
        raise HTTPException(409, 'No speech model is published; install one from the catalogue and publish it')
    audio = await upload.read()
    if not audio:
        raise HTTPException(422, 'The audio file is empty')
    if len(audio) > AUDIO_MAX_BYTES:
        raise HTTPException(413, f'The audio file exceeds {AUDIO_MAX_BYTES // 1024 ** 2} MiB')
    metadata = runtime_metadata(model_id)
    form = {'response_format': 'json', 'temperature': '0.0'}
    if language:
        form['language'] = language
    if prompt:
        form['prompt'] = prompt
    started = time.monotonic()
    async with serving(model_id, published_only), httpx.AsyncClient(timeout=httpx.Timeout(AUDIO_TIMEOUT, connect=10)) as client:
        try:
            response = await client.post(f'http://127.0.0.1:{runtime_port(metadata)}/inference',
                                         files={'file': (upload.filename or 'audio.wav', audio, upload.content_type or 'application/octet-stream')},
                                         data=form)
        except httpx.HTTPError:
            raise HTTPException(502, 'Speech runtime unavailable') from None
    if response.status_code != 200:
        raise HTTPException(502, f'Speech runtime error {response.status_code}: {response.text[:300]}')
    body = response.json()
    return {'text': (body.get('text') or '').strip(), 'model': model_id, 'seconds': round(time.monotonic() - started, 1)}


@router.post('/v1/audio/transcriptions')
async def audio_transcriptions(request: Request, file: UploadFile = File(...), model: str = Form(''),
                               language: str = Form(''), prompt: str = Form('')):
    """OpenAI-compatible transcription: the chat's microphone and any client that
    already speaks to /v1/audio/transcriptions."""
    caller = caller_of(request)
    permit(caller, model if model_kind(model) == 'speech' else setting('default_speech_model', ''))
    admit(caller)
    result = await transcribe(file, model, language, prompt)
    REQUESTS.labels('audio/transcriptions', '200').inc()
    return {'text': result['text']}


@router.post('/api/v1/aios/speech/transcribe')
async def portal_transcription(file: UploadFile = File(...), model: str = Form(''), language: str = Form(''),
                               user=Depends(operator)):
    """Transcribe one file from the portal, without the chat and without the
    inference key: the session already proves who is asking."""
    result = await transcribe(file, model, language, published_only=False)
    audit(user['id'], 'audio_transcribed', result['model'], {'seconds': result['seconds']})
    return result


# Text to speech, in the OpenAI shape: JSON in, audio out. Speaking takes a few
# times the length of the audio on a CPU, so a long text is given time.
VOICE_TIMEOUT = 1800


class SpeechRequest(BaseModel):
    model: str = Field(default='', max_length=256)
    input: str = Field(min_length=1, max_length=4096)
    # An OpenAI voice name, or a language code (it, en, de...) to speak in.
    voice: str = Field(default='', max_length=64)
    response_format: Literal['mp3', 'wav', 'opus', 'aac', 'flac', 'pcm'] = 'mp3'
    language: str = Field(default='', max_length=8)
    speed: float = Field(default=1.0, ge=0.25, le=4.0)


def voice_model(requested):
    """The voice model that answers: the one named, or the published one."""
    model_id = requested if requested and model_kind(requested) == 'voice' else setting('default_voice_model', '')
    if not model_id:
        raise HTTPException(409, 'No voice model is published; install one from the catalogue and publish it')
    return model_id


async def synthesize(data, published_only=True):
    """Speak a text with the voice engine: (audio, media type, model, seconds)."""
    model_id = voice_model(data.model)
    metadata = runtime_metadata(model_id)
    started = time.monotonic()
    payload = {'input': data.input, 'voice': data.voice, 'response_format': data.response_format, 'language': data.language}
    async with serving(model_id, published_only), httpx.AsyncClient(timeout=httpx.Timeout(VOICE_TIMEOUT, connect=10)) as client:
        try:
            response = await client.post(f'http://127.0.0.1:{runtime_port(metadata)}/v1/audio/speech', json=payload)
        except httpx.HTTPError:
            raise HTTPException(502, 'Voice runtime unavailable') from None
    if response.status_code != 200:
        try:
            message = response.json()['error']['message']
        except (ValueError, KeyError, TypeError):
            message = response.text[:300]
        raise HTTPException(422 if response.status_code == 400 else 502, f'Voice runtime: {message}')
    return response.content, response.headers.get('content-type', 'application/octet-stream'), model_id, round(time.monotonic() - started, 1)


@router.post('/v1/audio/speech')
async def audio_speech(data: SpeechRequest, request: Request):
    """OpenAI-compatible speech: the chat's read-aloud button and any client of /v1/audio/speech."""
    caller = caller_of(request)
    permit(caller, voice_model(data.model))
    admit(caller)
    audio, media, _, _ = await synthesize(data)
    REQUESTS.labels('audio/speech', '200').inc()
    return Response(audio, media_type=media)


def voice_list():
    return [{'id': key, 'name': name} for key, name in VOICES.items()]


# The chat asks for these without a key to fill its voice and model menus; they
# name what the appliance offers and nothing about who uses it.
@router.get('/v1/audio/voices')
def audio_voices():
    return {'voices': voice_list()}


@router.get('/v1/audio/models')
def audio_models():
    return {'models': [{'id': 'aios-voice'}]}


@router.get('/api/v1/aios/speech/voices')
def portal_voices(user=Depends(viewer)):
    return {'voices': voice_list(), 'default': DEFAULT_VOICE}


@router.post('/api/v1/aios/speech/synthesize')
async def portal_speech(data: SpeechRequest, user=Depends(operator)):
    """Speak a text from the portal with an installed voice model, published or not."""
    audio, media, model_id, seconds = await synthesize(data.model_copy(update={'response_format': 'wav'}), published_only=False)
    audit(user['id'], 'speech_synthesized', model_id, {'seconds': seconds, 'characters': len(data.input)})
    return Response(audio, media_type=media, headers={'X-AIOS-Seconds': str(seconds)})


# The embedding model shipped in the image answers under these names, with no
# language model loaded; any other name goes to the model it names.
EMBEDDING_NAMES = ('all-MiniLM-L6-v2', 'sentence-transformers/all-MiniLM-L6-v2', 'aios-embedding')
EMBEDDING_URL = 'http://127.0.0.1:8093/v1/embeddings'


async def bundled_embeddings(data, caller):
    admit(caller)
    start = time.monotonic()
    # The first request starts the service and loads the model: allow for that.
    async with httpx.AsyncClient(timeout=httpx.Timeout(300, connect=30)) as client:
        try:
            upstream = await client.post(EMBEDDING_URL, json=data.model_dump())
        except httpx.HTTPError:
            REQUESTS.labels('embeddings', 'error').inc()
            raise HTTPException(502, 'Embedding service unavailable') from None
    LATENCY.observe(time.monotonic() - start)
    REQUESTS.labels('embeddings', str(upstream.status_code)).inc()
    if upstream.status_code == 200:
        count_usage(upstream.json(), caller)
    return Response(upstream.content, status_code=upstream.status_code, media_type='application/json')


@router.post('/v1/{endpoint:path}')
async def inference(endpoint: str, data: InferenceRequest, request: Request):
    # images/edits is multipart, which this JSON gateway cannot forward; it is
    # not offered rather than accepted and then refused upstream.
    if endpoint not in ('chat/completions', 'completions', 'embeddings', 'images/generations'):
        raise HTTPException(404, 'Unknown endpoint')
    images = endpoint.startswith('images/')
    caller = caller_of(request)
    if endpoint == 'embeddings' and data.model in EMBEDDING_NAMES:
        return await bundled_embeddings(data, caller)
    model_id = data.model or setting('default_image_model' if images else 'default_model', '')
    if images and model_kind(model_id) != 'image':
        # Clients send the name they were configured with; the appliance decides
        # which diffusion model answers.
        model_id = setting('default_image_model', '')
        if not model_id:
            raise HTTPException(409, 'No image model is published; install one from the catalogue and publish it')
    permit(caller, model_id)
    admit(caller)
    row = await ensure_running(model_id)
    config = RuntimeConfig.model_validate_json(row['config'])
    if images:
        # Denoising an image takes minutes on a CPU: the default request timeout
        # of a chat would abandon a picture that is still being produced.
        config = config.model_copy(update={'timeout': max(config.timeout, IMAGE_TIMEOUT)})
    try:
        slot = await acquire_slot(model_id, config)
    except BaseException:
        unclaim(model_id)  # the slot never existed to release the claim itself
        raise
    start = time.monotonic()
    client = httpx.AsyncClient(timeout=httpx.Timeout(config.timeout, connect=10))
    try:
        port = runtime_port(runtime_metadata(model_id)) if images else row['port']
        payload = {k: v for k, v in data.model_dump().items() if not (images and k in ('model', 'stream'))} if images else {**data.model_dump(), 'model': model_id}
        upstream = await client.send(client.build_request('POST', f'http://127.0.0.1:{port}/v1/{endpoint}', json=payload), stream=True)
    except BaseException as exc:
        # BaseException: a cancelled request must give its slot back too.
        slot.release()
        await client.aclose()
        if isinstance(exc, httpx.HTTPError):
            REQUESTS.labels(endpoint, 'error').inc()
            raise HTTPException(502, 'Inference runtime unavailable') from None
        raise
    if not data.stream or upstream.status_code != 200:
        try:
            body = await upstream.aread()
            if len(body) > 32 * 1024 ** 2:
                raise HTTPException(502, 'Runtime response too large')
            if upstream.status_code == 200:
                count_usage(json.loads(body), caller)
            REQUESTS.labels(endpoint, str(upstream.status_code)).inc()
            return Response(body, status_code=upstream.status_code, media_type='application/json')
        finally:
            slot.release()
            LATENCY.observe(time.monotonic() - start)
            await upstream.aclose()
            await client.aclose()
    async def stream():
        try:
            async for line in upstream.aiter_lines():
                if line.startswith('data: ') and line[6:] != '[DONE]':
                    try:
                        count_usage(json.loads(line[6:]), caller)
                    except ValueError:
                        pass
                yield (line + '\n').encode()
            REQUESTS.labels(endpoint, '200').inc()
        finally:
            slot.release()
            LATENCY.observe(time.monotonic() - start)
            await upstream.aclose()
            await client.aclose()
    # The generator's finally never runs if the client leaves before streaming
    # starts; the background task releases the slot in that case.
    return StreamingResponse(stream(), media_type='text/event-stream', headers={'X-Accel-Buffering': 'no', 'Cache-Control': 'no-cache'}, background=BackgroundTask(slot.release))

class ImageRequest(BaseModel):
    model: str = Field(default='', max_length=64)
    prompt: str = Field(min_length=1, max_length=2000)
    negative_prompt: str = Field(default='', max_length=2000)
    width: int = Field(default=0, ge=0, le=2048)
    height: int = Field(default=0, ge=0, le=2048)
    steps: int = Field(default=0, ge=0, le=100)


@router.post('/api/v1/aios/images/generate')
async def generate_image(data: ImageRequest, user=Depends(operator)):
    """Generate one picture from the portal, without the chat and without the
    inference key: the session already proves who is asking."""
    model_id = data.model if model_kind(data.model) == 'image' else setting('default_image_model', '')
    if not model_id or model_kind(model_id) != 'image':
        raise HTTPException(409, 'No image model is published; install one from the catalogue and publish it')
    metadata = runtime_metadata(model_id)
    defaults = imaging.generation_defaults(metadata)
    # The portal may try a model it has installed but not published yet.
    # The OpenAI route takes a prompt and a size; everything else travels inside
    # the prompt in the engine's own extension block, which it strips before use.
    extra: dict[str, object] = {'sample_params': {'sample_steps': data.steps or defaults['steps']}}
    if data.negative_prompt:
        extra['negative_prompt'] = data.negative_prompt
    payload = {'prompt': data.prompt + '<sd_cpp_extra_args>' + json.dumps(extra) + '</sd_cpp_extra_args>',
               'size': f"{data.width or defaults['width']}x{data.height or defaults['height']}", 'n': 1}
    started = time.monotonic()
    async with serving(model_id, published_only=False), httpx.AsyncClient(timeout=httpx.Timeout(IMAGE_TIMEOUT, connect=10)) as client:
        try:
            response = await client.post(f'http://127.0.0.1:{runtime_port(metadata)}/v1/images/generations', json=payload)
        except httpx.HTTPError:
            raise HTTPException(502, 'Image runtime unavailable') from None
    if response.status_code != 200:
        raise HTTPException(502, f'Image runtime error {response.status_code}: {response.text[:300]}')
    body = response.json()
    audit(user['id'], 'image_generated', model_id, {'seconds': round(time.monotonic() - started, 1)})
    return {'seconds': round(time.monotonic() - started, 1), 'image': (body.get('data') or [{}])[0].get('b64_json', ''),
            'model': model_id, 'parameters': payload}
