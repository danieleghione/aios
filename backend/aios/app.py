import asyncio
import hmac
import json
import logging
import os
import re
import sqlite3
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal
import httpx
import psutil
from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, Query, Request, Response, UploadFile
from fastapi.exceptions import RequestValidationError
from starlette.background import BackgroundTask
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel, Field
from prometheus_client import Counter, Gauge, Histogram, generate_latest, CONTENT_TYPE_LATEST
from . import auth, chat_auth
from .core import DATA, ETC, atomic_write, audit, connection, encode, execute, initialize, now, one, projector_path, rows, setting, set_setting, uid
from .gguf import DRAFT_MESSAGE, draft_reason, not_a_model
from . import accelerators
from .hardware import benchmark, compatibility, profile, resources, cached_profile
from . import imaging
from .providers import matches, projector_for, sync, validate_url
from .runtime import RuntimeConfig, model_metadata as runtime_metadata, runtime_port
from .platform import BackupRequest, NetworkConfig, SystemConfig, TLSRequest, enqueue

logging.basicConfig(level=logging.INFO, format='%(message)s')
logging.getLogger('httpx').setLevel(logging.WARNING)
logging.getLogger('httpcore').setLevel(logging.WARNING)
LOG = logging.getLogger('aios')
REQUESTS = Counter('aios_inference_requests', 'Inference requests', ['endpoint', 'status'])
ACTIVE = Gauge('aios_inference_active', 'Active inference requests')
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


def unclaim(model_id):
    _BUSY[model_id] = max(0, _BUSY.get(model_id, 0) - 1)


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


def model_kind(model_id):
    """'image' for a diffusion model, 'text' for a language model."""
    try:
        return 'image' if imaging.is_image_model(runtime_metadata(model_id)) else 'text'
    except (TypeError, ValueError, KeyError):
        return 'text'


async def settle(model_id, states, timeout):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        row = one('SELECT state,error FROM runtime_instances WHERE model_id=?', (model_id,))
        if not row or row['state'] in states:
            return row
        await asyncio.sleep(SETTLE_POLL)
    return one('SELECT state,error FROM runtime_instances WHERE model_id=?', (model_id,))


async def ensure_running(model_id):
    """Serve any published model on request, loading it if needed. Choosing a model
    in the chat used to fail with "not running" unless someone had first started
    that exact model by hand. Returns with the model claimed, so a concurrent load
    of another model cannot unload it before this request reaches it."""
    row = one(LIVE, (model_id,))
    if row:
        claim(model_id)  # no await since the check: nothing can interleave
        return row
    installed = one('SELECT * FROM installed_models WHERE id=? AND published=1', (model_id,))
    if not installed:
        raise HTTPException(404, 'Model is not installed and published')
    if unusable(installed):
        raise HTTPException(409, unusable(installed))
    async with _LOAD_LOCK:
        row = one(LIVE, (model_id,))
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
        row = one(LIVE, (model_id,))
        if not row:
            reason = outcome.get('error') if outcome else None
            raise HTTPException(503, 'Model could not be loaded: ' + (reason or 'load did not finish in time'))
        claim(model_id)
        return row


async def acquire_slot(model_id, config):
    # Open WebUI sends several requests per message (answer, title, tags,
    # follow-ups). Rejecting all but the first made the user's own question fail
    # whenever a helper request got there first; queue them instead, up to the
    # configured request timeout. Semaphore.acquire is cancellation-safe.
    semaphore = _SLOTS.setdefault((model_id, config.parallel), asyncio.Semaphore(config.parallel))
    try:
        async with asyncio.timeout(config.timeout):
            await semaphore.acquire()
    except TimeoutError:
        raise HTTPException(429, f'Model busy: no inference slot freed within {config.timeout} s') from None
    return Slot(semaphore, model_id)
LATENCY = Histogram('aios_inference_seconds', 'Inference request latency')
TOKENS = Counter('aios_inference_tokens', 'Tokens reported by runtime', ['direction'])
SYSTEM = Gauge('aios_system', 'Hardware measurements', ['measurement'])
ERRORS = Counter('aios_errors', 'Control plane errors', ['status'])

@asynccontextmanager
async def lifespan(app):
    initialize()
    yield

app = FastAPI(title='AIOS Control Plane', version='1.5.1', lifespan=lifespan, docs_url=None, openapi_url='/api/aios/openapi.json', redoc_url=None)
viewer, operator, admin, superadmin = [auth.require(role) for role in ('VIEWER', 'OPERATOR', 'ADMIN', 'SUPERADMIN')]

@app.exception_handler(HTTPException)
async def http_error(request, exc):
    ERRORS.labels(str(exc.status_code)).inc()
    return JSONResponse({'error': {'code': exc.status_code, 'message': str(exc.detail)}}, status_code=exc.status_code, headers=exc.headers)

@app.exception_handler(RequestValidationError)
async def validation_error(request, exc):
    return JSONResponse({'error': {'code': 422, 'message': 'Invalid request', 'fields': [{'location': list(e['loc']), 'message': e['msg']} for e in exc.errors()]}}, status_code=422)

@app.exception_handler(ValueError)
async def value_error(request, exc):
    return JSONResponse({'error': {'code': 400, 'message': str(exc)}}, status_code=400)

@app.exception_handler(sqlite3.IntegrityError)
async def integrity_error(request, exc):
    return JSONResponse({'error': {'code': 409, 'message': 'Conflicting resource or invalid lifecycle transition'}}, status_code=409)

@app.exception_handler(Exception)
async def unexpected_error(request, exc):
    LOG.error(encode({'event': 'unhandled_error', 'type': type(exc).__name__}))
    ERRORS.labels('500').inc()
    return JSONResponse({'error': {'code': 500, 'message': 'Internal operation failed; consult appliance logs'}}, status_code=500)

@app.middleware('http')
async def request_log(request, call_next):
    start = time.monotonic()
    response = await call_next(request)
    LOG.info(encode({'event': 'http', 'method': request.method, 'path': request.url.path, 'status': response.status_code, 'duration': time.monotonic() - start}))
    response.headers['X-Content-Type-Options'] = 'nosniff'
    return response

@app.get('/api/aios/docs', include_in_schema=False, response_class=HTMLResponse)
def offline_reference():
    import html
    schema = app.openapi()
    sections = []
    for path, operations in schema['paths'].items():
        for method, operation in operations.items():
            sections.append('<details><summary><b>' + html.escape(method.upper()) + '</b> ' + html.escape(path) + '</summary><pre>' + html.escape(json.dumps(operation, indent=2)) + '</pre></details>')
    return '<!doctype html><html lang="en"><meta charset="utf-8"><title>AIOS API reference</title><style>body{max-width:1100px;margin:40px auto;font-family:system-ui;padding:20px;color:#18332f}details{padding:14px;border-bottom:1px solid #ddd}pre{overflow:auto;background:#f3f6f5;padding:16px}summary{cursor:pointer}a{color:#17796d}</style><h1>AIOS API reference</h1><p>Offline reference · <a href="/api/aios/openapi.json">OpenAPI JSON</a> · <a href="/admin/">Admin portal</a></p>' + ''.join(sections) + '<h2>Schemas</h2><pre>' + html.escape(json.dumps(schema.get('components', {}), indent=2)) + '</pre></html>'

@app.get('/api/v1/aios/auth/status')
def auth_status():
    return {'initialized': bool(one('SELECT id FROM users LIMIT 1'))}

@app.post('/api/v1/aios/auth/bootstrap')
def bootstrap(data: auth.Bootstrap, request: Request):
    identity = 'bootstrap:' + (request.client.host if request.client else 'unknown')
    attempt = one('SELECT * FROM login_attempts WHERE identity=?', (identity,))
    if attempt and attempt['until'] > now():
        raise HTTPException(429, 'Try again later')
    execute('INSERT INTO login_attempts VALUES (?,1,?) ON CONFLICT(identity) DO UPDATE SET until=excluded.until', (identity, now() + 2))
    auth.setup_account(data)
    return {'initialized': True}

@app.post('/api/v1/aios/auth/login')
def login(data: auth.Login, request: Request, response: Response):
    token, csrf = auth.authenticate(data, (request.client.host if request.client else 'unknown') + ':' + data.username)
    response.delete_cookie('aios_session', path='/api')
    response.delete_cookie('token', path='/')
    response.set_cookie('aios_session', token, httponly=True, secure=True, samesite='strict', max_age=28800, path='/')
    return {'csrf': csrf}

@app.get('/api/v1/aios/auth/me')
def me(user=Depends(auth.current_user)):
    return {k: user[k] for k in ('id', 'username', 'role', 'must_change', 'csrf')}

@app.post('/api/v1/aios/auth/logout')
def logout(request: Request, response: Response, user=Depends(auth.current_user)):
    execute('DELETE FROM sessions WHERE token_hash=?', (auth.digest(request.cookies.get('aios_session', '')),))
    response.delete_cookie('aios_session', path='/api', secure=True, httponly=True, samesite='strict')
    response.delete_cookie('aios_session', path='/', secure=True, httponly=True, samesite='strict')
    response.delete_cookie('token', path='/')
    audit(user['id'], 'logout')
    return {'ok': True}

@app.post('/api/v1/aios/auth/password')
def change_password(data: auth.PasswordChange, user=Depends(auth.current_user)):
    if not auth.verify(data.current, user['password_hash']):
        raise HTTPException(403, 'Current password invalid')
    with connection() as db:
        db.execute('UPDATE users SET password_hash=?,must_change=0 WHERE id=?', (auth.HASHER.hash(data.password), user['id']))
        db.execute('DELETE FROM sessions WHERE user_id=?', (user['id'],))
    audit(user['id'], 'password_change')
    return {'login_required': True}

@app.get('/api/v1/aios/users')
def users(user=Depends(admin)):
    return {'items': rows('SELECT id,username,role,must_change,disabled FROM users ORDER BY username')}

@app.post('/api/v1/aios/users')
def create_user(data: auth.UserCreate, user=Depends(superadmin)):
    if len(data.password) < 12:
        raise HTTPException(422, 'Password requires 12 characters')
    key = uid()
    with connection() as db:
        db.execute('BEGIN IMMEDIATE')
        if '@' in data.username and db.execute('SELECT id FROM users WHERE lower(username)=?', (data.username,)).fetchone():
            raise HTTPException(409, 'Email already in use')
        db.execute('INSERT INTO users(id,username,password_hash,role,must_change) VALUES (?,?,?,?,1)', (key, data.username, auth.HASHER.hash(data.password), data.role))
    audit(user['id'], 'user_create', key)
    return {'id': key}

class UserUpdate(BaseModel):
    role: auth.Role
    disabled: bool

@app.patch('/api/v1/aios/users/{key}')
def update_user(key: str, data: UserUpdate, user=Depends(superadmin)):
    if key == user['id']:
        raise HTTPException(409, 'Cannot disable or demote your own account')
    execute('UPDATE users SET role=?,disabled=? WHERE id=?', (data.role, data.disabled, key))
    execute('DELETE FROM sessions WHERE user_id=?', (key,))
    audit(user['id'], 'user_update', key, data.model_dump())
    return {'ok': True}

@app.get('/_aios/chat-session', include_in_schema=False)
def chat_session(request: Request, user=Depends(auth.current_user)):
    headers = chat_auth.ensure_identity(user, request.cookies.get('aios_session', ''))
    return Response(status_code=204, headers=headers)

@app.get('/health')
def health():
    database_ok = bool(one('SELECT max(version) AS v FROM schema_migrations'))
    failures = one("SELECT count(*) AS n FROM runtime_instances WHERE state='FAILED'")['n']
    repository_errors = one("SELECT count(*) AS n FROM repositories WHERE enabled=1 AND status IN ('ERROR','AUTH REQUIRED','RATE LIMITED')")['n']
    try:
        webui_ok = httpx.get('http://127.0.0.1:8080/health', timeout=1, trust_env=False).status_code == 200
    except httpx.HTTPError:
        webui_ok = False
    return {'status': 'DEGRADED' if failures or repository_errors or not webui_ok else 'HEALTHY', 'database': database_ok, 'open_webui': webui_ok, 'runtime_failures': failures, 'repository_errors': repository_errors}

def runtime_measurements():
    active = one("SELECT port,model_id FROM runtime_instances WHERE state='RUNNING'")
    values = {'tokens_per_second': None, 'model_load_seconds': None}
    if not active:
        return values
    load = one("SELECT json_extract(payload,'$.detail.load_seconds') AS seconds FROM audit_events WHERE json_extract(payload,'$.action')='runtime_start' AND json_extract(payload,'$.target')=? ORDER BY id DESC LIMIT 1", (active['model_id'],))
    if load:
        values['model_load_seconds'] = load['seconds']
    try:
        response = httpx.get(f'http://127.0.0.1:{active["port"]}/metrics', timeout=1, trust_env=False)
        for line in response.text.splitlines():
            if not line.startswith('#') and 'predicted_tokens_seconds ' in line:
                import math
                measured = float(line.split()[-1])
                values['tokens_per_second'] = measured if math.isfinite(measured) else None
    except (httpx.HTTPError, ValueError):
        pass
    return values

@app.get('/api/v1/aios/system/dashboard')
def dashboard(user=Depends(viewer)):
    # Polled every five seconds; a full re-profile each time kept a core busy.
    hw = cached_profile()
    return {'hardware': hw, 'installed': one('SELECT count(*) AS n FROM installed_models')['n'], 'running': one("SELECT count(*) AS n FROM runtime_instances WHERE state='RUNNING'")['n'], 'repositories': rows('SELECT name,status,last_sync,error FROM repositories'), 'alerts': rows('SELECT * FROM alerts WHERE resolved=0 ORDER BY created_at DESC LIMIT 50'), 'health': health(), 'metrics': {'requests': sum(sample.value for metric in REQUESTS.collect() for sample in metric.samples if sample.name.endswith('_total')), 'active': ACTIVE._value.get(), **runtime_measurements()}}

@app.get('/api/v1/aios/hardware/profile')
def get_hardware(user=Depends(viewer)):
    return profile(probe=False)

@app.get('/api/v1/aios/hardware/benchmarks')
def benchmarks(user=Depends(viewer)):
    return {'items': [{**row, 'result': json.loads(row['result'])} for row in rows('SELECT * FROM benchmark_results ORDER BY created_at DESC LIMIT 20')]}

@app.post('/api/v1/aios/hardware/benchmark')
def run_benchmark(user=Depends(operator)):
    result = benchmark()
    audit(user['id'], 'benchmark')
    return result

class RepositoryConfig(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    provider: Literal['huggingface', 'modelscope', 'github', 'http', 'internal', 'diffusion']
    url: str = Field(max_length=2048)
    enabled: bool = False
    config: dict = Field(default_factory=dict)
    token: str | None = Field(default=None, max_length=4096)

@app.get('/api/v1/aios/repositories')
def repositories(user=Depends(viewer)):
    return {'items': [{**row, 'config': json.loads(row['config']), 'has_token': bool(one('SELECT repository_id FROM repository_credentials WHERE repository_id=?', (row['id'],))), 'next_sync': (row['last_sync'] or now()) + 21600} for row in rows('SELECT * FROM repositories ORDER BY name')]}

def save_repo(data, key, user):
    if len(encode(data.config)) > 65536:
        raise HTTPException(422, 'Repository configuration too large')
    validate_url(data.url, data.config.get('allow_private', False))
    if data.provider in ('huggingface', 'modelscope', 'github'):
        from urllib.parse import urlparse
        expected = {'huggingface': 'huggingface.co', 'modelscope': 'modelscope.cn', 'github': 'api.github.com'}[data.provider]
        if urlparse(data.url).hostname != expected:
            raise HTTPException(422, 'Official provider hostname required')
    execute('INSERT INTO repositories(id,name,provider,url,enabled,config) VALUES (?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET name=excluded.name,provider=excluded.provider,url=excluded.url,enabled=excluded.enabled,config=excluded.config', (key, data.name, data.provider, data.url, data.enabled, encode(data.config)))
    if data.token is not None:
        path = ETC / 'secrets' / key
        if data.token:
            atomic_write(path, data.token)
            execute('INSERT INTO repository_credentials VALUES (?,?) ON CONFLICT(repository_id) DO UPDATE SET secret_ref=excluded.secret_ref', (key, key))
        else:
            path.unlink(missing_ok=True)
            execute('DELETE FROM repository_credentials WHERE repository_id=?', (key,))
        audit(user['id'], 'repository_token_change', key)
    audit(user['id'], 'repository_change', key)
    return {'id': key}

@app.post('/api/v1/aios/repositories')
def add_repository(data: RepositoryConfig, user=Depends(admin)):
    return save_repo(data, uid(), user)

@app.put('/api/v1/aios/repositories/{key}')
def update_repository(key: str, data: RepositoryConfig, user=Depends(admin)):
    if not one('SELECT id FROM repositories WHERE id=?', (key,)):
        raise HTTPException(404, 'Repository not found')
    return save_repo(data, key, user)

@app.post('/api/v1/aios/repositories/{key}/{action}')
async def repository_action(key: str, action: Literal['sync', 'test'], tasks: BackgroundTasks, user=Depends(admin)):
    if not one('SELECT id FROM repositories WHERE id=?', (key,)):
        raise HTTPException(404, 'Repository not found')
    if action == 'test':
        return await sync(key, True)
    execute("UPDATE repositories SET status='SYNCING' WHERE id=?", (key,))
    tasks.add_task(sync, key)
    audit(user['id'], 'repository_sync_requested', key)
    return {'status': 'SYNCING'}

@app.get('/api/v1/aios/catalog')
def catalog(q: str = '', repository: str = '', author: str = '', architecture: str = '', quantization: str = '', license: str = '', max_size: int = 0, min_parameters: int = 0, max_parameters: int = 0, since: float = 0, fit: str = '', limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0), user=Depends(viewer)):
    found = []
    hw = resources()
    # A disabled repository's offer is hidden; what was already installed stays.
    for row in rows('SELECT d.*,i.state,i.published FROM discovered_models d LEFT JOIN installed_models i ON i.id=d.id JOIN repositories r ON r.id=d.repository_id WHERE d.discovered_at>=? AND (r.enabled=1 OR i.id IS NOT NULL) ORDER BY d.discovered_at DESC', (since,)):
        metadata = json.loads(row.pop('metadata'))
        item = {**metadata, **row}
        if matches(item, {'keyword': q, 'repository_id': repository, 'author': author, 'architecture': architecture, 'quantization': quantization, 'license': license, 'max_size': max_size, 'min_parameters': min_parameters, 'max_parameters': max_parameters}):
            item['compatibility'] = compatibility(metadata, hw=hw)
            item['state'] = item['state'] or 'DISCOVERED'
            if fit and item['compatibility']['classification'] != fit:
                continue
            found.append(item)
    # Newest release first; discovery order says nothing about how recent a model is.
    found.sort(key=lambda item: str(item.get('release_date') or ''), reverse=True)
    return {'items': found[offset:offset + limit], 'total': len(found), 'offset': offset, 'limit': limit}

class Watchlist(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    filters: dict

@app.get('/api/v1/aios/catalog/watchlists')
def watchlists(user=Depends(viewer)):
    return {'items': [{**r, 'filters': json.loads(r['filters'])} for r in rows('SELECT * FROM watchlists')]}

@app.post('/api/v1/aios/catalog/watchlists')
def create_watchlist(data: Watchlist, user=Depends(admin)):
    if len(encode(data.filters)) > 65536:
        raise HTTPException(422, 'Filters too large')
    key = uid()
    execute('INSERT INTO watchlists VALUES (?,?,?)', (key, data.name, encode(data.filters)))
    audit(user['id'], 'watchlist_create', key)
    return {'id': key}

@app.delete('/api/v1/aios/catalog/watchlists/{key}')
def delete_watchlist(key: str, user=Depends(admin)):
    execute('DELETE FROM watchlists WHERE id=?', (key,))
    audit(user['id'], 'watchlist_delete', key)
    return {'ok': True}

class InstallRequest(BaseModel):
    accept_license: bool
    override_compatibility: bool = False

@app.post('/api/v1/aios/models/{key}/install')
def install(key: str, data: InstallRequest, user=Depends(operator)):
    model = one('SELECT * FROM discovered_models WHERE id=?', (key,))
    if not model:
        raise HTTPException(404, 'Model not found')
    metadata = json.loads(model['metadata'])
    if not data.accept_license:
        raise HTTPException(422, 'License acknowledgement required')
    allowed = setting('approved_licenses', [])
    if allowed and metadata.get('license', 'unknown') not in allowed:
        raise HTTPException(403, 'License is blocked by appliance policy')
    installed = one('SELECT id FROM installed_models WHERE id=?', (key,))
    rating = compatibility(metadata)
    if not installed and rating['classification'] in ('NOT_RECOMMENDED', 'INCOMPATIBLE'):
        if not data.override_compatibility or auth.LEVEL[user['role']] < 2:
            raise HTTPException(409, 'Explicit administrator compatibility override required')
    projector = projector_for(key)
    components = imaging.components(metadata)
    if installed:
        # Installed before projectors travelled with their model: fetch just that.
        missing = ([projector] if projector and not projector_path(key).exists() else []) + \
                  [c for c in components if not imaging.component_path(key, c['role'], c['filename']).exists()]
        if not missing:
            raise HTTPException(409, 'Already installed')
        total = sum(int(item['size']) for item in missing)
    else:
        total = metadata['size'] + (projector['size'] if projector else 0) + sum(int(c['size']) for c in components)
    if one("SELECT id FROM downloads WHERE model_id=? AND state IN ('QUEUED','DOWNLOADING','VERIFYING','PAUSED')", (key,)):
        raise HTTPException(409, 'A download of this model is already in progress')
    download_id = uid()
    execute('INSERT INTO downloads(id,model_id,state,total,created_at,updated_at) VALUES (?,?,?,?,?,?)', (download_id, key, 'QUEUED', total, now(), now()))
    audit(user['id'], 'download_queue', key, {'license': metadata.get('license'), 'override': data.override_compatibility, 'projector': bool(projector)})
    return {'id': download_id, 'state': 'QUEUED'}

@app.get('/api/v1/aios/models')
def installed_models(user=Depends(viewer)):
    # image_input: the projector is installed; the metadata's projector says one is published.
    return {'items': [{**json.loads(r.pop('metadata')), **r, 'config': json.loads(r['config']), 'gguf': json.loads(r['gguf']), 'projector': projector_for(r['id']), 'image_input': projector_path(r['id']).exists()} for r in rows('SELECT d.metadata,i.*,r.state AS runtime_state,r.desired AS runtime_desired,r.error AS runtime_error FROM installed_models i JOIN discovered_models d ON d.id=i.id LEFT JOIN runtime_instances r ON r.model_id=i.id ORDER BY i.installed_at DESC')]}

class ModelUpdate(BaseModel):
    published: bool | None = None
    notes: str | None = Field(default=None, max_length=10000)
    config: RuntimeConfig | None = None
    default: bool = False

@app.patch('/api/v1/aios/models/{key}')
def model_update(key: str, data: ModelUpdate, user=Depends(operator)):
    model = one('SELECT * FROM installed_models WHERE id=?', (key,))
    if not model:
        raise HTTPException(404, 'Model not installed')
    if data.published is not None:
        if data.published and (not Path(model['path']).is_file() or model['state'] == 'DISABLED'):
            raise HTTPException(409, 'Model artifact unavailable')
        # Backstop for files installed before this was rejected at download time.
        if data.published and unusable(model):
            raise HTTPException(409, unusable(model))
        execute('UPDATE installed_models SET published=?,state=? WHERE id=?', (data.published, 'PUBLISHED' if data.published else 'INSTALLED', key))
        if model_kind(key) == 'image':
            # The chat asks for images by name; the published diffusion model is
            # the one that answers, and unpublishing it clears the choice.
            set_setting('default_image_model', key if data.published else '')
        audit(user['id'], 'model_publish' if data.published else 'model_unpublish', key)
    if data.notes is not None:
        execute('UPDATE installed_models SET notes=? WHERE id=?', (data.notes, key))
    if data.config:
        execute('UPDATE installed_models SET config=? WHERE id=?', (encode(data.config.model_dump()), key))
    if data.default:
        set_setting('default_model', key)
    audit(user['id'], 'model_update', key)
    return {'ok': True}

@app.delete('/api/v1/aios/models/{key}')
def delete_model(key: str, user=Depends(admin)):
    model = one('SELECT * FROM installed_models WHERE id=?', (key,))
    if not model:
        raise HTTPException(404, 'Model not installed')
    if one("SELECT id FROM runtime_instances WHERE model_id=? AND (desired!='STOPPED' OR state IN ('RUNNING','STARTING'))", (key,)):
        raise HTTPException(409, 'Stop runtime before deleting model')
    execute('DELETE FROM runtime_instances WHERE model_id=?', (key,))
    stored = Path(model['path'])
    # Weights and every companion file are named after the installation UUID.
    if stored.parent != DATA / 'models' or not stored.name.startswith(key):
        raise HTTPException(409, 'Invalid registry path')
    for path in (DATA / 'models').glob(key + '.*'):
        path.unlink(missing_ok=True)
    stored.unlink(missing_ok=True)
    execute('DELETE FROM installed_models WHERE id=?', (key,))
    audit(user['id'], 'model_delete', key)
    return {'ok': True}

@app.get('/api/v1/aios/downloads')
def downloads(user=Depends(viewer)):
    return {'items': [{**r, 'progress': round(100 * r['downloaded'] / max(1, r['total']), 1)} for r in rows('SELECT * FROM downloads ORDER BY created_at DESC LIMIT 200')]}

@app.post('/api/v1/aios/downloads/{key}/{action}')
def download_action(key: str, action: Literal['pause', 'resume', 'cancel', 'retry'], user=Depends(operator)):
    job = one('SELECT * FROM downloads WHERE id=?', (key,))
    if not job:
        raise HTTPException(404, 'Download not found')
    allowed = {'pause': ('QUEUED', 'DOWNLOADING'), 'resume': ('PAUSED',), 'cancel': ('QUEUED', 'DOWNLOADING', 'PAUSED'), 'retry': ('FAILED', 'CANCELLED')}
    if job['state'] not in allowed[action]:
        raise HTTPException(409, 'Invalid download transition')
    state = {'pause': 'PAUSED', 'resume': 'QUEUED', 'cancel': 'CANCELLED', 'retry': 'QUEUED'}[action]
    execute('UPDATE downloads SET state=?,next_retry=0,attempts=0 WHERE id=?', (state, key))
    if action == 'cancel' and job['state'] != 'DOWNLOADING':
        for suffix in ('.part', '.mmproj.part'):
            (DATA / 'downloads' / (key + suffix)).unlink(missing_ok=True)
    audit(user['id'], 'download_' + action, key)
    return {'state': state}

def config_differs(saved, launched):
    """Whether the saved settings are not what the process runs with. Settings the
    operator never chose are not compared: the manager may have picked a smaller
    context to fit memory, and that is not a change waiting for a restart."""
    chosen = json.loads(saved or '{}')
    running = RuntimeConfig.model_validate_json(launched or '{}').model_dump()
    wanted = RuntimeConfig.model_validate(chosen).model_dump()
    return any(running[k] != wanted[k] for k in wanted if k in chosen or k != 'context')


@app.get('/api/v1/aios/runtime')
def runtimes(user=Depends(viewer)):
    items = []
    for row in rows('SELECT r.*, i.config AS saved FROM runtime_instances r JOIN installed_models i ON i.id=r.model_id'):
        entry = dict(row)
        saved = entry.pop('saved')
        # config is what the running process was launched with, so a difference
        # against the saved settings means a restart is needed to adopt them.
        entry['pending_restart'] = entry['state'] in ('RUNNING', 'STARTING') and config_differs(saved, entry['config'])
        entry['config'] = json.loads(entry['config'])
        entry['acceleration'] = runtime_acceleration(entry['id']) if entry['state'] in ('RUNNING', 'STARTING', 'FAILED') else None
        items.append(entry)
    return {'items': items}


def runtime_acceleration(key):
    """Which devices the last start used and how many layers llama.cpp put on them."""
    try:
        facts = json.loads((DATA / 'runtime' / (key + '.accel.json')).read_text())
    except (OSError, ValueError):
        return None
    try:
        with (DATA / 'runtime' / (key + '.log')).open('rb') as log:
            log.seek(int(facts.get('log_offset') or 0))
            text = log.read(4 * 1024 * 1024).decode(errors='replace')
    except OSError:
        text = ''
    return {'devices': facts.get('devices', []), 'note': facts.get('note'), 'offload': accelerators.offload_summary(text)}

def desired_runs(action):
    return action in ('start', 'restart', 'load')

def unusable(model):
    """Why an installed file cannot serve requests, or None. Backstop for files
    installed before the download check refused them: vision projectors,
    importance matrices, adapters and speculative-decoding drafts all carry no
    complete model and fail inside the inference server with an obscure error."""
    stored = json.loads(model['gguf'] or '{}')
    metadata = stored.get('metadata', {})
    architecture = metadata.get('general.architecture')
    kind = not_a_model(metadata)
    if architecture == 'clip' or kind == 'mmproj':
        return 'This file is a multimodal projector (mmproj), not a language model; it cannot serve chat requests'
    if kind:
        return f'This file is a GGUF of type "{kind}", not a model; it cannot serve chat requests'
    if isinstance(architecture, str) and isinstance(stored.get('tensors'), int) and draft_reason(architecture, metadata, stored['tensors']):
        return DRAFT_MESSAGE
    return None

@app.post('/api/v1/aios/runtime/{key}/{action}')
def runtime_action(key: str, action: Literal['start', 'stop', 'restart', 'load', 'unload'], user=Depends(operator)):
    model = one('SELECT * FROM installed_models WHERE id=?', (key,))
    if not model:
        raise HTTPException(404, 'Model not installed')
    if desired_runs(action) and unusable(model):
        raise HTTPException(409, unusable(model))
    desired = 'STOPPED' if action in ('stop', 'unload') else ('RESTART' if action == 'restart' else 'RUNNING')
    if desired != 'STOPPED' and not Path(model['path']).is_file():
        raise HTTPException(409, 'Model file unavailable')
    if desired != 'STOPPED' and one("SELECT id FROM runtime_instances WHERE model_id!=? AND (desired!='STOPPED' OR state IN ('RUNNING','STARTING'))", (key,)):
        raise HTTPException(409, 'Stop the active model first')
    live = one("SELECT config FROM runtime_instances WHERE model_id=? AND state IN ('RUNNING','STARTING')", (key,))
    # Starting an already loaded model does nothing, so saying "started" while the
    # process keeps its old settings would report a change that never happened.
    if live and action in ('start', 'load') and config_differs(model['config'], live['config']):
        raise HTTPException(409, 'Configuration changed since this model was loaded; restart it to apply the new settings')
    # Leave the live configuration in place: it describes the running process.
    config = live['config'] if live and action != 'restart' else model['config']
    execute("INSERT INTO runtime_instances(id,model_id,state,port,config,desired) VALUES (?,?,'STOPPED',8090,?,?) ON CONFLICT(model_id) DO UPDATE SET desired=excluded.desired,config=excluded.config", (uid(), key, config, desired))
    audit(user['id'], 'runtime_' + action + '_requested', key)
    return {'desired': desired}

@app.get('/v1/models')
def public_models():
    # Diffusion models answer /v1/images/generations, not chat: listing them here
    # would put them in the chat's model selector, where they can only fail.
    return {'object': 'list', 'data': [{'id': r['id'], 'name': json.loads(r['metadata'])['display_name'], 'object': 'model', 'created': int(r['installed_at']), 'owned_by': 'aios'}
                                       for r in rows('SELECT i.id,i.installed_at,d.metadata FROM installed_models i JOIN discovered_models d ON d.id=i.id WHERE i.published=1')
                                       if not imaging.is_image_model(json.loads(r['metadata']))]}

class InferenceRequest(BaseModel):
    model: str = Field(default='', max_length=256)
    stream: bool = False
    model_config = {'extra': 'allow'}

def count_usage(value):
    usage = value.get('usage') or {}
    TOKENS.labels('input').inc(max(0, usage.get('prompt_tokens', 0)))
    TOKENS.labels('output').inc(max(0, usage.get('completion_tokens', 0)))

@app.post('/v1/{endpoint:path}')
async def inference(endpoint: str, data: InferenceRequest, request: Request):
    # images/edits is multipart, which this JSON gateway cannot forward; it is
    # not offered rather than accepted and then refused upstream.
    if endpoint not in ('chat/completions', 'completions', 'embeddings', 'images/generations'):
        raise HTTPException(404, 'Unknown endpoint')
    images = endpoint.startswith('images/')
    secret = ETC / 'secrets/inference-key'
    if not secret.exists() or not hmac.compare_digest(request.headers.get('authorization', ''), 'Bearer ' + secret.read_text().strip()):
        raise HTTPException(401, 'Inference API key required')
    model_id = data.model or setting('default_image_model' if images else 'default_model', '')
    if images and model_kind(model_id) != 'image':
        # Clients send the name they were configured with; the appliance decides
        # which diffusion model answers.
        model_id = setting('default_image_model', '')
        if not model_id:
            raise HTTPException(409, 'No image model is published; install one from the catalogue and publish it')
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
                count_usage(json.loads(body))
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
                        count_usage(json.loads(line[6:]))
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

@app.get('/metrics')
async def metrics():
    memory = psutil.virtual_memory()
    for key, value in {'cpu_percent': psutil.cpu_percent(), 'ram_available': memory.available, 'ram_total': memory.total, 'disk_free': psutil.disk_usage(DATA).free, 'load1': os.getloadavg()[0], 'network_received': psutil.net_io_counters().bytes_recv, 'network_sent': psutil.net_io_counters().bytes_sent, 'download_bytes': one('SELECT coalesce(sum(downloaded),0) AS n FROM downloads')['n']}.items():
        SYSTEM.labels(key).set(value)
    for source, readings in psutil.sensors_temperatures().items():
        for index, reading in enumerate(readings):
            SYSTEM.labels('temperature_' + source + '_' + str(index)).set(reading.current)
    for repo in rows('SELECT provider,status,duration,found FROM repositories'):
        SYSTEM.labels('repository_' + repo['provider'] + '_online').set(int(repo['status'] == 'ONLINE'))
        SYSTEM.labels('repository_' + repo['provider'] + '_sync_seconds').set(repo['duration'] or 0)
        SYSTEM.labels('repository_' + repo['provider'] + '_found').set(repo['found'])
    measurements = runtime_measurements()
    if measurements['model_load_seconds'] is not None:
        SYSTEM.labels('model_load_seconds').set(measurements['model_load_seconds'])
    SYSTEM.labels('download_speed').set(one('SELECT coalesce(sum(speed),0) AS n FROM downloads')['n'])
    result = generate_latest()
    active = one("SELECT port FROM runtime_instances WHERE state='RUNNING'")
    if active:
        try:
            async with httpx.AsyncClient(timeout=2) as client:
                response = await client.get(f'http://127.0.0.1:{active["port"]}/metrics')
                if response.status_code == 200:
                    result += response.content
        except httpx.HTTPError:
            pass
    return Response(result, media_type=CONTENT_TYPE_LATEST)

@app.get('/api/v1/aios/audit')
def audit_log(limit: int = Query(100, ge=1, le=500), offset: int = Query(0, ge=0), q: str = '', user=Depends(admin)):
    return {'items': [{**r, 'event': json.loads(r['payload'])} for r in rows('SELECT * FROM audit_events WHERE payload LIKE ? ORDER BY id DESC LIMIT ? OFFSET ?', ('%' + q + '%', limit, offset))], 'offset': offset}

@app.get('/api/v1/aios/logs')
def logs(source: str = 'control-plane', q: str = '', lines: int = Query(200, ge=1, le=1000), user=Depends(admin)):
    if source not in ('control-plane', 'runtime-manager', 'download-worker', 'platform', 'open-webui'):
        raise HTTPException(422, 'Unknown log source')
    import subprocess
    result = subprocess.run(['journalctl', '-u', 'aios-' + source, '-n', str(lines), '--no-pager', '-o', 'json'], capture_output=True, text=True, timeout=10)
    items = [json.loads(line) for line in result.stdout.splitlines() if q.lower() in line.lower()]
    return {'items': [{'time': x.get('__REALTIME_TIMESTAMP'), 'message': x.get('MESSAGE', '')} for x in items]}

class ImageRequest(BaseModel):
    model: str = Field(default='', max_length=64)
    prompt: str = Field(min_length=1, max_length=2000)
    negative_prompt: str = Field(default='', max_length=2000)
    width: int = Field(default=0, ge=0, le=2048)
    height: int = Field(default=0, ge=0, le=2048)
    steps: int = Field(default=0, ge=0, le=100)


@app.post('/api/v1/aios/images/generate')
async def generate_image(data: ImageRequest, user=Depends(operator)):
    """Generate one picture from the portal, without the chat and without the
    inference key: the session already proves who is asking."""
    model_id = data.model or setting('default_image_model', '')
    if not model_id or model_kind(model_id) != 'image':
        raise HTTPException(409, 'No image model is published; install one from the catalogue and publish it')
    metadata = runtime_metadata(model_id)
    defaults = imaging.generation_defaults(metadata)
    await ensure_running(model_id)  # loads the model if the portal asks before the chat does
    # The OpenAI route takes a prompt and a size; everything else travels inside
    # the prompt in the engine's own extension block, which it strips before use.
    extra: dict[str, object] = {'sample_params': {'sample_steps': data.steps or defaults['steps']}}
    if data.negative_prompt:
        extra['negative_prompt'] = data.negative_prompt
    payload = {'prompt': data.prompt + '<sd_cpp_extra_args>' + json.dumps(extra) + '</sd_cpp_extra_args>',
               'size': f"{data.width or defaults['width']}x{data.height or defaults['height']}", 'n': 1}
    started = time.monotonic()
    async with httpx.AsyncClient(timeout=httpx.Timeout(IMAGE_TIMEOUT, connect=10)) as client:
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


@app.get('/api/v1/aios/runtime/{key}/log')
def runtime_log(key: str, user=Depends(admin)):
    if not one('SELECT id FROM runtime_instances WHERE id=?', (key,)):
        raise HTTPException(404, 'Runtime not found')
    path = DATA / 'runtime' / (key + '.log')
    if not path.exists():
        return {'text': ''}
    with path.open('rb') as stream:
        stream.seek(max(0, path.stat().st_size - 65536))
        return {'text': stream.read().decode(errors='replace')}

@app.get('/api/v1/aios/system/settings')
def system_settings(user=Depends(admin)):
    return {'system': setting('system_config', {'hostname': os.uname().nodename, 'timezone': 'UTC', 'ntp': ['ntp.ubuntu.com'], 'proxy': '', 'governor': 'unchanged', 'hugepages_2m': 0}), 'download_concurrency': setting('download_concurrency', 2), 'approved_licenses': setting('approved_licenses', []), 'default_model': setting('default_model'), 'update_policy': 'manual-signed', 'ssh': 'disabled'}

@app.post('/api/v1/aios/system/config')
def configure_system(data: SystemConfig, user=Depends(superadmin)):
    audit(user['id'], 'system_config_requested')
    return enqueue('system', data.model_dump())

@app.post('/api/v1/aios/system/network')
def configure_network(data: NetworkConfig, user=Depends(superadmin)):
    audit(user['id'], 'network_config_requested', detail=data.model_dump())
    return enqueue('network', data.model_dump())

@app.post('/api/v1/aios/system/network/{key}/confirm')
def network_confirm(key: str, user=Depends(superadmin)):
    return enqueue('network-confirm', {'id': key})

@app.post('/api/v1/aios/system/tls')
def configure_tls(data: TLSRequest, user=Depends(superadmin)):
    audit(user['id'], 'tls_change_requested')
    return enqueue('tls', data.model_dump())

class Policy(BaseModel):
    download_concurrency: int = Field(ge=1, le=8)
    approved_licenses: list[str] = Field(max_length=100)

@app.put('/api/v1/aios/system/policy')
def policy(data: Policy, user=Depends(admin)):
    for key, value in data.model_dump().items():
        set_setting(key, value)
    audit(user['id'], 'policy_change', detail=data.model_dump())
    return {'ok': True}

@app.post('/api/v1/aios/system/power/{action}')
def power(action: Literal['reboot', 'shutdown'], user=Depends(superadmin)):
    audit(user['id'], action + '_requested')
    return enqueue(action, {})

@app.get('/api/v1/aios/system/jobs')
def system_jobs(user=Depends(admin)):
    return {'items': [{**r, 'result': json.loads(r['result']) if r['result'] else None} for r in rows('SELECT id,action,state,result,created_at FROM system_jobs ORDER BY created_at DESC LIMIT 50')]}

@app.get('/api/v1/aios/backups')
def backups(user=Depends(superadmin)):
    return {'items': [{'file': p.name, 'size': p.stat().st_size, 'created': p.stat().st_mtime} for p in (DATA / 'backups').glob('*.tar.gz')]}

@app.post('/api/v1/aios/backups')
def create_backup(data: BackupRequest, user=Depends(superadmin)):
    audit(user['id'], 'backup_requested')
    return enqueue('backup', data.model_dump())

@app.get('/api/v1/aios/backups/{filename}')
def fetch_backup(filename: str, user=Depends(superadmin)):
    if not re.fullmatch(r'[a-f0-9-]{36}\.tar\.gz', filename):
        raise HTTPException(422, 'Invalid backup filename')
    path = DATA / 'backups' / filename
    if not path.exists():
        raise HTTPException(404, 'Backup not found')
    return FileResponse(path, filename=filename, media_type='application/gzip')

@app.post('/api/v1/aios/backups/upload')
async def upload_backup(file: UploadFile, user=Depends(superadmin)):
    filename = uid() + '.tar.gz'
    path = DATA / 'backups' / filename
    size = 0
    try:
        with path.open('xb') as stream:
            os.chmod(path, 0o600)
            while chunk := await file.read(1024 * 1024):
                size += len(chunk)
                if size > 20 * 1024 ** 3:
                    raise HTTPException(413, 'Archive exceeds 20 GiB')
                stream.write(chunk)
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    audit(user['id'], 'archive_uploaded', filename)
    return {'file': filename, 'size': size}

class RestoreRequest(BaseModel):
    file: str = Field(pattern=r'^[a-f0-9-]{36}\.tar\.gz$')
    confirm: Literal['RESTORE']

@app.post('/api/v1/aios/backups/restore')
def restore_backup(data: RestoreRequest, user=Depends(superadmin)):
    audit(user['id'], 'restore_requested', data.file)
    return enqueue('restore', {'file': data.file})

class UpdateRequest(BaseModel):
    file: str = Field(pattern=r'^[a-f0-9-]{36}\.tar\.gz$')
    manifest: dict
    signature: str = Field(max_length=1024)

@app.post('/api/v1/aios/system/update')
def update(data: UpdateRequest, user=Depends(superadmin)):
    audit(user['id'], 'update_requested')
    return enqueue('update', data.model_dump())
