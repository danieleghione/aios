"""Health, dashboard, hardware, audit, logs, system settings and backups."""
import hashlib
import json
import os
import re
import socket
import subprocess
from pathlib import Path
from typing import Literal

import httpx
import psutil
from fastapi import APIRouter, Depends, HTTPException, Query, Response, UploadFile
from fastapi.responses import FileResponse
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from pydantic import BaseModel, Field, field_validator

from . import alerts as alerts_module
from . import backups as backup_store
from . import __version__, accelerators, notify, system_state
from .core import DATA, atomic_write, audit, now, one, rows, setting, set_setting, uid
from .hardware import benchmark, cached_profile, profile
from .platform import BackupRequest, NetworkConfig, SystemConfig, TLSRequest, enqueue
from .runtime import kind_of as runtime_kind, runtime_acceleration
from .web import CPU_HISTORY, HISTORY, REQUESTS, SYSTEM, ACTIVE, admin, operator, superadmin, viewer

router = APIRouter()

@router.get('/health')
def health():
    database_ok = bool(one('SELECT max(version) AS v FROM schema_migrations'))
    failures = one("SELECT count(*) AS n FROM runtime_instances WHERE state='FAILED'")['n']
    repository_errors = one("SELECT count(*) AS n FROM repositories WHERE enabled=1 AND status IN ('ERROR','AUTH REQUIRED','RATE LIMITED')")['n']
    try:
        webui_ok = httpx.get('http://127.0.0.1:8080/health', timeout=1, trust_env=False).status_code == 200
    except httpx.HTTPError:
        webui_ok = False
    return {'status': 'DEGRADED' if failures or repository_errors or not webui_ok else 'HEALTHY', 'database': database_ok, 'open_webui': webui_ok, 'runtime_failures': failures, 'repository_errors': repository_errors}

def active_models():
    """What is loaded right now, where it runs and how much memory it holds."""
    found = []
    for row in rows("SELECT r.id,r.model_id,r.pid,r.state,r.port FROM runtime_instances r WHERE r.state IN ('RUNNING','STARTING')"):
        described = one('SELECT metadata FROM discovered_models WHERE id=?', (row['model_id'],))
        metadata = json.loads(described['metadata']) if described else {}
        try:
            memory = psutil.Process(row['pid']).memory_info().rss if row['pid'] else None
        except (psutil.Error, ValueError):
            memory = None
        found.append({'model_id': row['model_id'], 'display_name': metadata.get('display_name', row['model_id']), 'state': row['state'],
                      'kind': runtime_kind(metadata), 'memory': memory, 'acceleration': runtime_acceleration(row['id'])})
    return found


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

@router.get('/api/v1/aios/system/dashboard')
def dashboard(user=Depends(viewer)):
    # Polled every five seconds; a full re-profile each time kept a core busy.
    hw = {**cached_profile(), 'aios_version': __version__}
    return {'hardware': hw, 'cpu_history': list(CPU_HISTORY), 'history': {name: list(values) for name, values in HISTORY.items()}, 'active_models': active_models(), 'installed': one('SELECT count(*) AS n FROM installed_models')['n'], 'running': one("SELECT count(*) AS n FROM runtime_instances WHERE state='RUNNING'")['n'], 'repositories': rows("SELECT name,CASE WHEN enabled=1 THEN status ELSE 'DISABLED' END AS status,last_sync,error FROM repositories ORDER BY enabled DESC,name"), 'alerts': rows('SELECT * FROM alerts WHERE resolved=0 ORDER BY created_at DESC LIMIT 50'), 'health': health(), 'metrics': {'requests': sum(sample.value for metric in REQUESTS.collect() for sample in metric.samples if sample.name.endswith('_total')), 'active': ACTIVE._value.get(), **runtime_measurements()}}

@router.post('/api/v1/aios/alerts/{key}/dismiss')
def dismiss_alert(key: str, user=Depends(admin)):
    """Close an alert by hand; it opens again if the condition is seen again."""
    if not one('SELECT id FROM alerts WHERE id=? AND resolved=0', (key,)):
        raise HTTPException(404, 'Alert not found')
    alerts_module.resolve(key)
    audit(user['id'], 'alert_dismissed', key)
    return {'dismissed': key}


@router.get('/api/v1/aios/hardware/profile')
def get_hardware(user=Depends(viewer)):
    # The profile is cached by the profiler; the version is the one running now.
    return {**profile(probe=False), 'aios_version': __version__, 'cuda': accelerators.cuda_package()}

@router.get('/api/v1/aios/hardware/benchmarks')
def benchmarks(user=Depends(viewer)):
    return {'items': [{**row, 'result': json.loads(row['result'])} for row in rows('SELECT * FROM benchmark_results ORDER BY created_at DESC LIMIT 20')]}

@router.post('/api/v1/aios/hardware/benchmark')
def run_benchmark(user=Depends(operator)):
    result = benchmark()
    audit(user['id'], 'benchmark')
    return result

@router.get('/metrics')
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

@router.get('/api/v1/aios/audit')
def audit_log(limit: int = Query(100, ge=1, le=500), offset: int = Query(0, ge=0), q: str = '', user=Depends(admin)):
    names = {r['id']: r['username'] for r in rows('SELECT id,username FROM users')}
    items = []
    for r in rows('SELECT * FROM audit_events WHERE payload LIKE ? ORDER BY id DESC LIMIT ? OFFSET ?', ('%' + q + '%', limit, offset)):
        event = json.loads(r['payload'])
        # The chain stores identifiers; the page shows who that was. A deleted
        # account keeps its identifier, and the name recorded when it was removed.
        actor = event.get('actor') or ''
        items.append({**r, 'event': event, 'actor_name': names.get(actor) or ('system' if actor in ('', 'system') else actor[:8])})
    return {'items': items, 'offset': offset}


# Services the portal can show, and the systemd unit each one is.
LOG_SOURCES = {name: 'aios-' + name for name in ('control-plane', 'runtime-manager', 'download-worker', 'platform', 'open-webui', 'repository-sync',
                                                   'hardware-profiler', 'nvidia-driver', 'update-check', 'firstboot')}
ANSI = re.compile(r'\x1b\[[0-9;?]*[A-Za-z]')


def journal_message(value):
    """journald hands over a message with control characters (colours) as a list
    of bytes: decode it and drop the escapes instead of showing JSON numbers."""
    if isinstance(value, list):
        value = bytes(v for v in value if isinstance(v, int) and 0 <= v < 256).decode('utf-8', 'replace')
    return ANSI.sub('', str(value or '')).rstrip()


@router.get('/api/v1/aios/logs')
def logs(source: str = 'control-plane', q: str = '', lines: int = Query(200, ge=1, le=1000), user=Depends(admin)):
    if source not in LOG_SOURCES:
        raise HTTPException(422, 'Unknown log source')
    result = subprocess.run(['journalctl', '-u', LOG_SOURCES[source], '-n', str(lines), '--no-pager', '-o', 'json'], capture_output=True, text=True, timeout=10)
    items = []
    for line in result.stdout.splitlines():
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        message = journal_message(entry.get('MESSAGE'))
        if q.lower() in message.lower():
            priority = int(entry.get('PRIORITY', 6))
            items.append({'time': int(entry.get('__REALTIME_TIMESTAMP', 0)) / 1e6, 'message': message,
                          'level': 'error' if priority <= 3 else 'warning' if priority == 4 else 'info'})
    # Newest first: the last thing that happened is what the operator came for.
    return {'items': items[::-1], 'sources': list(LOG_SOURCES)}

@router.get('/api/v1/aios/system/settings')
def system_settings(user=Depends(admin)):
    stored = setting('system_config', {'proxy': ''})
    return {'system': system_state.observed_system(stored), 'download_concurrency': setting('download_concurrency', 2),
            'approved_licenses': setting('approved_licenses', []), 'idle_unload_minutes': setting('idle_unload_minutes', 0), 'default_model': setting('default_model'),
            'ssh': 'disabled' if Path('/etc/ssh/sshd_not_to_be_run').exists() else 'enabled'}


@router.get('/api/v1/aios/system/timezones')
def system_timezones(user=Depends(admin)):
    return {'items': system_state.timezones()}


@router.get('/api/v1/aios/system/network')
def system_network(user=Depends(admin)):
    jobs = rows('SELECT id,action,state,created_at FROM system_jobs WHERE action IN (?,?) ORDER BY created_at DESC LIMIT 10', ('network', 'network-confirm'))
    return {'current': system_state.network(), 'pending': system_state.pending_network(jobs)}


@router.get('/api/v1/aios/system/tls')
def system_tls(user=Depends(admin)):
    return {'certificate': system_state.certificate()}


@router.get('/api/v1/aios/system/updates')
def system_updates(user=Depends(admin)):
    """Operating system updates, as the daily check and the console see them."""
    try:
        return json.loads((DATA / 'system' / 'updates.json').read_text())
    except (OSError, ValueError):
        return {'checked_at': None, 'packages': 0, 'security': 0, 'reboot_required': False, 'error': ''}


@router.post('/api/v1/aios/system/updates/{mode}')
def system_updates_run(mode: Literal['check', 'apply'], user=Depends(superadmin)):
    audit(user['id'], 'os_update_' + mode + '_requested')
    return enqueue('os-update', {'mode': mode})

@router.post('/api/v1/aios/system/config')
def configure_system(data: SystemConfig, user=Depends(superadmin)):
    audit(user['id'], 'system_config_requested')
    return enqueue('system', data.model_dump())

@router.post('/api/v1/aios/system/network')
def configure_network(data: NetworkConfig, user=Depends(superadmin)):
    audit(user['id'], 'network_config_requested', detail=data.model_dump())
    return enqueue('network', data.model_dump())

@router.post('/api/v1/aios/system/network/{key}/confirm')
def network_confirm(key: str, user=Depends(superadmin)):
    return enqueue('network-confirm', {'id': key})

@router.post('/api/v1/aios/system/tls')
def configure_tls(data: TLSRequest, user=Depends(superadmin)):
    audit(user['id'], 'tls_change_requested')
    return enqueue('tls', data.model_dump())

class Policy(BaseModel):
    download_concurrency: int = Field(ge=1, le=8)
    approved_licenses: list[str] = Field(max_length=100)
    # Minutes a model may stay loaded unused before its memory is given back; 0 keeps it.
    idle_unload_minutes: int = Field(default=0, ge=0, le=10080)

@router.put('/api/v1/aios/system/policy')
def policy(data: Policy, user=Depends(admin)):
    for key, value in data.model_dump().items():
        set_setting(key, value)
    audit(user['id'], 'policy_change', detail=data.model_dump())
    return {'ok': True}

@router.post('/api/v1/aios/system/power/{action}')
def power(action: Literal['reboot', 'shutdown'], user=Depends(superadmin)):
    audit(user['id'], action + '_requested')
    return enqueue(action, {})

@router.get('/api/v1/aios/system/jobs')
def system_jobs(user=Depends(admin)):
    return {'items': [{**r, 'result': json.loads(r['result']) if r['result'] else None} for r in rows('SELECT id,action,state,result,created_at FROM system_jobs ORDER BY created_at DESC LIMIT 50')]}

@router.get('/api/v1/aios/backups')
def backups(user=Depends(superadmin)):
    return {'items': [backup_store.describe(path) for path in backup_store.archives()],
            'schedule': setting('backup_schedule') or {'enabled': False, 'hour': 2, 'keep': 7, 'include_models': False},
            'last_scheduled': setting('backup_last_scheduled', ''), 'encryption': bool(backup_store.stored_passphrase())}


class BackupSchedule(BaseModel):
    enabled: bool
    hour: int = Field(ge=0, le=23)
    keep: int = Field(ge=1, le=60)
    include_models: bool = False


@router.put('/api/v1/aios/backups/schedule')
def backup_schedule(data: BackupSchedule, user=Depends(superadmin)):
    """A daily backup at the chosen hour, keeping the newest ones."""
    set_setting('backup_schedule', data.model_dump())
    audit(user['id'], 'backup_schedule_changed', detail=data.model_dump())
    return data.model_dump()


class BackupPassphrase(BaseModel):
    passphrase: str = Field(default='', max_length=256)


@router.put('/api/v1/aios/backups/passphrase')
def backup_passphrase(data: BackupPassphrase, user=Depends(superadmin)):
    """Encrypt every backup from now on, or stop. Existing archives keep the
    passphrase they were made with: note it down before changing it."""
    if data.passphrase and len(data.passphrase) < 12:
        raise HTTPException(422, 'The passphrase needs at least 12 characters')
    backup_store.set_passphrase(data.passphrase)
    audit(user['id'], 'backup_encryption_on' if data.passphrase else 'backup_encryption_off')
    return {'encryption': bool(data.passphrase)}

@router.post('/api/v1/aios/backups')
def create_backup(data: BackupRequest, user=Depends(superadmin)):
    audit(user['id'], 'backup_requested')
    return enqueue('backup', data.model_dump())

@router.get('/api/v1/aios/backups/{filename}')
def fetch_backup(filename: str, user=Depends(superadmin)):
    if not re.fullmatch(backup_store.NAME, filename):
        raise HTTPException(422, 'Invalid backup filename')
    path = DATA / 'backups' / filename
    if not path.exists():
        raise HTTPException(404, 'Backup not found')
    return FileResponse(path, filename=filename, media_type='application/octet-stream' if filename.endswith('.enc') else 'application/gzip')

@router.post('/api/v1/aios/backups/upload')
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
    if backup_store.is_encrypted(path):
        # A sealed backup from this or another appliance: named for what it is.
        sealed = path.with_name(path.name + '.enc')
        path.rename(sealed)
        filename = sealed.name
    audit(user['id'], 'archive_uploaded', filename)
    return {'file': filename, 'size': size}

class RestoreRequest(BaseModel):
    file: str = Field(pattern=r'^[a-f0-9-]{36}\.tar\.gz(\.enc)?$')
    confirm: Literal['RESTORE']
    passphrase: str = Field(default='', max_length=256)

@router.post('/api/v1/aios/backups/restore')
def restore_backup(data: RestoreRequest, user=Depends(superadmin)):
    audit(user['id'], 'restore_requested', data.file)
    return enqueue('restore', {'file': data.file, 'passphrase': data.passphrase})

class ReleaseKey(BaseModel):
    key: str = Field(min_length=1, max_length=4096)


@router.get('/api/v1/aios/system/release-key')
def release_key(user=Depends(admin)):
    """Which key signed releases are accepted from. None ships in the image."""
    from cryptography.hazmat.primitives import serialization
    from .platform import load_release_key, release_key_path
    try:
        pub = load_release_key()
    except (ValueError, OSError):
        return {'installed': False, 'fingerprint': None, 'path': str(release_key_path())}
    raw = pub.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    digest = hashlib.sha256(raw).hexdigest().upper()
    return {'installed': True, 'algorithm': 'Ed25519', 'path': str(release_key_path()),
            'fingerprint': ':'.join(digest[i:i + 2] for i in range(0, 16, 2))}


@router.put('/api/v1/aios/system/release-key')
def install_release_key(data: ReleaseKey, user=Depends(superadmin)):
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    from .platform import release_key_path
    try:
        pub = serialization.load_pem_public_key(data.key.encode())
    except (ValueError, TypeError) as exc:
        raise HTTPException(422, 'Not a PEM public key: ' + str(exc)[:200]) from None
    if not isinstance(pub, Ed25519PublicKey):
        raise HTTPException(422, 'Release keys must be Ed25519')
    atomic_write(release_key_path(), data.key, 0o644)
    audit(user['id'], 'release_key_installed')
    return release_key(user)


@router.delete('/api/v1/aios/system/release-key')
def remove_release_key(user=Depends(superadmin)):
    from .platform import release_key_path
    release_key_path().unlink(missing_ok=True)
    audit(user['id'], 'release_key_removed')
    return {'installed': False}


class UpdateRequest(BaseModel):
    file: str = Field(pattern=r'^[a-f0-9-]{36}\.tar\.gz$')
    manifest: dict
    signature: str = Field(max_length=1024)

@router.post('/api/v1/aios/system/update')
def update(data: UpdateRequest, user=Depends(superadmin)):
    audit(user['id'], 'update_requested')
    return enqueue('update', data.model_dump())


@router.post('/api/v1/aios/system/update/cuda/remove')
def remove_cuda(user=Depends(superadmin)):
    """Take the optional CUDA package out; the GPUs go back to Vulkan."""
    if not accelerators.cuda_backend():
        raise HTTPException(409, 'The CUDA package is not installed')
    audit(user['id'], 'cuda_removal_requested')
    return enqueue('component-remove', {'component': 'cuda'})


class Notifications(BaseModel):
    min_severity: Literal['INFO', 'WARNING', 'ERROR'] = 'WARNING'
    webhook_url: str = Field(default='', max_length=2000, pattern=r'^(https?://\S+)?$')
    # None keeps the saved secret, an empty string removes it.
    webhook_secret: str | None = Field(default=None, max_length=256)
    email_to: list[str] = Field(default_factory=list, max_length=20)
    email_from: str = Field(default='', max_length=254)
    smtp_host: str = Field(default='', max_length=253, pattern=r'^[A-Za-z0-9.-]*$')
    smtp_port: int = Field(default=587, ge=1, le=65535)
    smtp_security: Literal['starttls', 'tls', 'none'] = 'starttls'
    smtp_username: str = Field(default='', max_length=254)
    smtp_password: str | None = Field(default=None, max_length=256)

    @field_validator('email_to')
    @classmethod
    def addresses(cls, value):
        for address in value:
            if not re.fullmatch(r'[^@\s]+@[^@\s]+\.[^@\s]+', address):
                raise ValueError(f'Not an e-mail address: {address}')
        return value


@router.get('/api/v1/aios/system/notifications')
def notifications(user=Depends(admin)):
    return notify.public_config()


@router.put('/api/v1/aios/system/notifications')
def save_notifications(data: Notifications, user=Depends(superadmin)):
    """Where alerts are sent, besides the dashboard."""
    values = data.model_dump(exclude={'webhook_secret', 'smtp_password'})
    refused = data.webhook_url and notify.forbidden_address(data.webhook_url)
    if refused:
        raise HTTPException(422, refused)
    set_setting('notifications', values)
    notify.store(notify.secret_path(), data.webhook_secret)
    notify.store(notify.password_path(), data.smtp_password)
    audit(user['id'], 'notifications_changed', detail={'channels': notify.channels(values), 'min_severity': data.min_severity})
    return notify.public_config()


@router.post('/api/v1/aios/system/notifications/test')
def test_notifications(user=Depends(superadmin)):
    """Send a test message on every configured channel and say how each went."""
    settings = notify.config()
    if not notify.channels(settings):
        raise HTTPException(409, 'Configure a webhook or an e-mail server first')
    outcome = notify.deliver({'event': 'test', 'id': 'test', 'severity': 'INFO', 'created_at': now(),
                              'message': 'Test message: notifications from this appliance reach you.',
                              'appliance': socket.gethostname()}, settings)
    notify.record(outcome)
    audit(user['id'], 'notifications_tested', detail={k: not v for k, v in outcome.items()})
    return {'results': {channel: {'ok': not error, 'error': error} for channel, error in outcome.items()}}
