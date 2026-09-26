"""Repositories, catalogue, installed models, downloads and runtimes."""
import json
from datetime import datetime
from typing import Literal

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from pathlib import Path

from . import auth, imaging, revisions, voice
from .core import DATA, ETC, atomic_write, audit, encode, execute, now, one, projector_path, rows, setting, set_setting, uid
from .hardware import compatibility, resources
from .platform import enqueue
from .providers import matches, parameters_from_name, projector_for, sync, validate_url
from .runtime import RuntimeConfig, kind_of as runtime_kind, model_kind, parallel_slots, runtime_acceleration, unusable
from .web import admin, operator, viewer

router = APIRouter()

class RepositoryConfig(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    provider: Literal['huggingface', 'modelscope', 'github', 'http', 'internal', 'diffusion', 'speech', 'voice']
    url: str = Field(max_length=2048)
    enabled: bool = False
    config: dict = Field(default_factory=dict)
    token: str | None = Field(default=None, max_length=4096)

@router.get('/api/v1/aios/repositories')
def repositories(user=Depends(viewer)):
    # A disabled repository is DISABLED whatever its last test said: a card that
    # read "ONLINE" and "Disabled" at once left the operator guessing.
    return {'items': [{**row, 'status': row['status'] if row['enabled'] else 'DISABLED', 'config': json.loads(row['config']), 'has_token': bool(one('SELECT repository_id FROM repository_credentials WHERE repository_id=?', (row['id'],))), 'next_sync': (row['last_sync'] or now()) + 21600} for row in rows('SELECT * FROM repositories ORDER BY name')]}

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

@router.post('/api/v1/aios/repositories')
def add_repository(data: RepositoryConfig, user=Depends(admin)):
    return save_repo(data, uid(), user)

@router.put('/api/v1/aios/repositories/{key}')
def update_repository(key: str, data: RepositoryConfig, user=Depends(admin)):
    if not one('SELECT id FROM repositories WHERE id=?', (key,)):
        raise HTTPException(404, 'Repository not found')
    return save_repo(data, key, user)

@router.post('/api/v1/aios/repositories/{key}/{action}')
async def repository_action(key: str, action: Literal['sync', 'test'], tasks: BackgroundTasks, user=Depends(admin)):
    if not one('SELECT id FROM repositories WHERE id=?', (key,)):
        raise HTTPException(404, 'Repository not found')
    if action == 'test':
        return await sync(key, True)
    execute("UPDATE repositories SET status='SYNCING' WHERE id=?", (key,))
    tasks.add_task(sync, key)
    audit(user['id'], 'repository_sync_requested', key)
    return {'status': 'SYNCING'}

@router.get('/api/v1/aios/catalog')
def catalog(q: str = '', repository: str = '', author: str = '', architecture: str = '', quantization: str = '', license: str = '', max_size: int = 0, min_parameters: int = 0, max_parameters: int = 0, since: float = 0, fit: str = '', group: bool = False, limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0), user=Depends(viewer)):
    found = []
    hw = resources()
    # A disabled repository's offer is hidden; what was already installed stays.
    for row in rows('SELECT d.*,i.state,i.published FROM discovered_models d LEFT JOIN installed_models i ON i.id=d.id JOIN repositories r ON r.id=d.repository_id WHERE (r.enabled=1 OR i.id IS NOT NULL) ORDER BY d.discovered_at DESC'):
        metadata = json.loads(row.pop('metadata'))
        # "Released in the last N days" means published upstream, not synchronised
        # here: after the first sync every model used to count as new.
        if since and released_at(metadata, row['discovered_at']) < since:
            continue
        if not metadata.get('parameter_count'):
            # Rows synchronised before names were read: same rule as discovery.
            total, active = parameters_from_name(str(metadata.get('model_id', '')).split('/')[-1], str(metadata.get('filename', '')).rsplit('/', 1)[-1])
            if total:
                metadata.update({'parameter_count': total, 'parameter_source': 'name', **({'active_parameters': active} if active else {})})
        item = {**metadata, **row}
        if matches(item, {'keyword': q, 'repository_id': repository, 'author': author, 'architecture': architecture, 'quantization': quantization, 'license': license, 'max_size': max_size, 'min_parameters': min_parameters, 'max_parameters': max_parameters}):
            item['compatibility'] = compatibility(metadata, hw=hw)
            item['state'] = item['state'] or 'DISCOVERED'
            if fit and item['compatibility']['classification'] != fit:
                continue
            found.append(item)
    # Newest release first; discovery order says nothing about how recent a model is.
    found.sort(key=lambda item: str(item.get('release_date') or ''), reverse=True)
    if group:
        found = group_variants(found)
    return {'items': found[offset:offset + limit], 'total': len(found), 'offset': offset, 'limit': limit}


FIT_ORDER = ['OPTIMAL', 'COMPATIBLE', 'LIMITED', 'NOT_RECOMMENDED', 'INCOMPATIBLE']
# The usual sweet spot between size and quality, in order of preference.
PREFERRED_QUANTS = ['Q4_K_M', 'Q5_K_M', 'Q4_K_S', 'Q6_K', 'Q8_0', 'Q4_0', 'IQ4_XS', 'Q4_K_XL', 'Q5_K_S']


def recommended_variant(variants):
    """The file to suggest: best fit for this machine first, then the usual
    quantisation, then the largest (least compressed) of the best fit."""
    def rank(item):
        fit = item['compatibility']['classification']
        quant = str(item.get('quantization', '')).upper()
        return (FIT_ORDER.index(fit) if fit in FIT_ORDER else len(FIT_ORDER),
                PREFERRED_QUANTS.index(quant) if quant in PREFERRED_QUANTS else len(PREFERRED_QUANTS), -item['size'])
    return min(variants, key=rank)


def group_variants(items):
    """One entry per model: a publisher ships a dozen quantisations of the same
    weights, and a card for each buried the choice under near-identical rows."""
    groups: dict = {}
    for item in items:
        groups.setdefault((item['repository_id'], item['model_id']), []).append(item)
    result = []
    for (repository_id, model_id), variants in groups.items():
        variants.sort(key=lambda item: item['size'])
        best = recommended_variant(variants)
        installed = [v for v in variants if v['state'] != 'DISCOVERED']
        result.append({'id': repository_id + ':' + model_id, 'model_id': model_id, 'repository_id': repository_id, 'recommended': best['id'],
                       'variants': variants, 'installed': len(installed),
                       'release_date': max(str(v.get('release_date') or '') for v in variants) or None,
                       **{key: best.get(key) for key in ('author', 'license', 'parameter_count', 'active_parameters', 'parameter_source', 'kind', 'architecture', 'compatibility')},
                       'projector': any(v.get('projector') for v in variants)})
    return result

class Watchlist(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    filters: dict

@router.get('/api/v1/aios/catalog/watchlists')
def watchlists(user=Depends(viewer)):
    return {'items': [{**r, 'filters': json.loads(r['filters'])} for r in rows('SELECT * FROM watchlists')]}

@router.post('/api/v1/aios/catalog/watchlists')
def create_watchlist(data: Watchlist, user=Depends(admin)):
    if len(encode(data.filters)) > 65536:
        raise HTTPException(422, 'Filters too large')
    key = uid()
    execute('INSERT INTO watchlists VALUES (?,?,?)', (key, data.name, encode(data.filters)))
    audit(user['id'], 'watchlist_create', key)
    return {'id': key}

@router.delete('/api/v1/aios/catalog/watchlists/{key}')
def delete_watchlist(key: str, user=Depends(admin)):
    execute('DELETE FROM watchlists WHERE id=?', (key,))
    audit(user['id'], 'watchlist_delete', key)
    return {'ok': True}

class InstallRequest(BaseModel):
    accept_license: bool
    override_compatibility: bool = False

@router.post('/api/v1/aios/models/{key}/install')
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
    components = imaging.components(metadata) + voice.components(metadata)
    if installed:
        # Installed before projectors travelled with their model: fetch just that.
        missing = ([projector] if projector and not projector_path(key).exists() else []) + \
                  [c for c in components if not (voice.component_path(key, c['role']) if voice.is_voice_model(metadata) else imaging.component_path(key, c['role'], c['filename'])).exists()]
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

@router.get('/api/v1/aios/models')
def installed_models(user=Depends(viewer)):
    # image_input: the projector is installed; the metadata's projector says one is published.
    defaults = {kind: setting(DEFAULT_SETTING[kind], '') for kind in DEFAULT_SETTING}
    items = []
    for r in rows('SELECT d.metadata,i.*,r.state AS runtime_state,r.desired AS runtime_desired,r.error AS runtime_error FROM installed_models i JOIN discovered_models d ON d.id=i.id LEFT JOIN runtime_instances r ON r.model_id=i.id ORDER BY i.installed_at DESC'):
        metadata = json.loads(r.pop('metadata'))
        kind = runtime_kind(metadata)
        items.append({**metadata, **r, 'kind': kind, 'is_default': defaults[kind] == r['id'], 'config': json.loads(r['config']),
                      'gguf': json.loads(r['gguf']), 'projector': projector_for(r['id']), 'image_input': projector_path(r['id']).exists(),
                      'update': None if r['replaced_by'] else revisions.newer(r['id'])})
    return {'items': items}


class RevisionUpdate(BaseModel):
    # Needed only when the newer revision comes under another licence.
    accept_license: bool = False

@router.post('/api/v1/aios/models/{key}/update')
def update_revision(key: str, data: RevisionUpdate, user=Depends(operator)):
    """Install the newest revision of a model; it takes over this one's settings."""
    model = one('SELECT i.id,d.metadata FROM installed_models i JOIN discovered_models d ON d.id=i.id WHERE i.id=?', (key,))
    if not model:
        raise HTTPException(404, 'Model not installed')
    update = revisions.newer(key)
    if not update:
        raise HTTPException(409, 'This is the newest revision')
    if update['license'] != json.loads(model['metadata']).get('license', 'unknown') and not data.accept_license:
        raise HTTPException(422, f"The newer revision comes under another licence ({update['license']}): accept it to update")
    allowed = setting('approved_licenses', [])
    if allowed and update['license'] not in allowed:
        raise HTTPException(403, 'License is blocked by appliance policy')
    if one("SELECT id FROM downloads WHERE model_id=? AND state IN ('QUEUED','DOWNLOADING','VERIFYING','PAUSED')", (update['id'],)):
        raise HTTPException(409, 'The update is already downloading')
    metadata = json.loads(one('SELECT metadata FROM discovered_models WHERE id=?', (update['id'],))['metadata'])
    projector = projector_for(update['id'])
    total = update['size'] + (projector['size'] if projector else 0) + sum(int(c['size']) for c in imaging.components(metadata) + voice.components(metadata))
    download_id = uid()
    execute('INSERT INTO downloads(id,model_id,state,total,created_at,updated_at,replaces) VALUES (?,?,?,?,?,?,?)',
            (download_id, update['id'], 'QUEUED', total, now(), now(), key))
    audit(user['id'], 'model_revision_update', key, {'revision': update['revision']})
    return {'id': download_id, 'state': 'QUEUED', 'revision': update['revision']}


# The model that answers when a request names none, one per kind.
DEFAULT_SETTING = {'text': 'default_model', 'image': 'default_image_model', 'speech': 'default_speech_model', 'voice': 'default_voice_model'}

class ModelUpdate(BaseModel):
    published: bool | None = None
    notes: str | None = Field(default=None, max_length=10000)
    config: RuntimeConfig | None = None
    default: bool = False

@router.patch('/api/v1/aios/models/{key}')
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
        kind = model_kind(key)
        if kind in ('image', 'speech', 'voice'):
            # The chat asks for a picture or a transcription by name; the
            # published model of that kind is the one that answers, and
            # unpublishing it clears the choice.
            set_setting(f'default_{kind}_model', key if data.published else '')
            if kind == 'voice':
                enqueue('chat-voice', {})
        audit(user['id'], 'model_publish' if data.published else 'model_unpublish', key)
    if data.notes is not None:
        execute('UPDATE installed_models SET notes=? WHERE id=?', (data.notes, key))
    if data.config:
        execute('UPDATE installed_models SET config=? WHERE id=?', (encode(data.config.model_dump()), key))
    if data.default:
        # The default of its own kind: marking a picture model as the chat's
        # default language model would send conversations to an image engine.
        set_setting(DEFAULT_SETTING[model_kind(key)], key)
    audit(user['id'], 'model_update', key)
    return {'ok': True}

@router.delete('/api/v1/aios/models/{key}')
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

@router.get('/api/v1/aios/downloads')
def downloads(user=Depends(viewer)):
    items = []
    for r in rows('SELECT d.*,m.metadata FROM downloads d LEFT JOIN discovered_models m ON m.id=d.model_id ORDER BY d.created_at DESC LIMIT 200'):
        metadata = json.loads(r.pop('metadata') or '{}')
        items.append({**r, 'display_name': metadata.get('display_name', r['model_id']), 'kind': runtime_kind(metadata),
                      'progress': round(100 * r['downloaded'] / max(1, r['total']), 1)})
    return {'items': items}

@router.post('/api/v1/aios/downloads/{key}/{action}')
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


@router.get('/api/v1/aios/runtime')
def runtimes(user=Depends(viewer)):
    items = []
    for row in rows('SELECT r.*, i.config AS saved FROM runtime_instances r JOIN installed_models i ON i.id=r.model_id'):
        entry = dict(row)
        saved = entry.pop('saved')
        # config is what the running process was launched with, so a difference
        # against the saved settings means a restart is needed to adopt them.
        entry['pending_restart'] = entry['state'] in ('RUNNING', 'STARTING') and config_differs(saved, entry['config'])
        entry['config'] = json.loads(entry['config'])
        entry['slots'] = parallel_slots(RuntimeConfig.model_validate(entry['config']), int(entry['config'].get('context') or 4096))
        entry['acceleration'] = runtime_acceleration(entry['id']) if entry['state'] in ('RUNNING', 'STARTING', 'FAILED') else None
        described = one('SELECT metadata FROM discovered_models WHERE id=?', (entry['model_id'],))
        metadata = json.loads(described['metadata']) if described else {}
        entry['display_name'] = metadata.get('display_name', entry['model_id'])
        entry['kind'] = runtime_kind(metadata)
        items.append(entry)
    return {'items': items}


def desired_runs(action):
    return action in ('start', 'restart', 'load')

def released_at(metadata, fallback):
    """When the model was published upstream, as a timestamp."""
    value = metadata.get('release_date')
    if isinstance(value, (int, float)) and value > 0:
        return float(value)
    if isinstance(value, str) and value:
        try:
            return datetime.fromisoformat(value.replace('Z', '+00:00')).timestamp()
        except ValueError:
            pass
    return fallback


@router.post('/api/v1/aios/runtime/{key}/{action}')
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

@router.get('/api/v1/aios/runtime/{key}/log')
def runtime_log(key: str, user=Depends(admin)):
    if not one('SELECT id FROM runtime_instances WHERE id=?', (key,)):
        raise HTTPException(404, 'Runtime not found')
    path = DATA / 'runtime' / (key + '.log')
    if not path.exists():
        return {'text': ''}
    with path.open('rb') as stream:
        stream.seek(max(0, path.stat().st_size - 65536))
        return {'text': stream.read().decode(errors='replace')}
