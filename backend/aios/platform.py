"""Root-only fixed-operation broker. No user supplied commands are executed."""
import asyncio
import base64
import hashlib
import ipaddress
import json
import os
import re
import shutil
import subprocess
import tarfile
import urllib.error
import urllib.request
import tempfile
import time
from pathlib import Path
from typing import Literal
import yaml
from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from pydantic import BaseModel, Field, model_validator
from . import backups, system_files
from .core import DATA, ETC, atomic_write, audit, connection, encode, execute, now, rows, set_setting, setting, uid

class NetworkConfig(BaseModel):
    interface: str = Field(pattern=r'^[a-zA-Z0-9_.:-]{1,32}$')
    dhcp: bool = True
    address: str = ''
    gateway: str = ''
    dns: list[str] = Field(default_factory=list, max_length=6)
    @model_validator(mode='after')
    def check_addresses(self):
        if not self.dhcp:
            ipaddress.ip_interface(self.address)
            ipaddress.ip_address(self.gateway)
        for address in self.dns:
            ipaddress.ip_address(address)
        return self

class SystemConfig(BaseModel):
    hostname: str = Field(pattern=r'^[a-zA-Z0-9]([a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?$')
    timezone: str = Field(default='UTC', pattern=r'^[a-zA-Z0-9_+/-]{1,64}$')
    ntp: list[str] = Field(default_factory=lambda: ['ntp.ubuntu.com'], max_length=8)
    proxy: str = Field(default='', max_length=512)
    governor: Literal['unchanged', 'performance', 'powersave'] = 'unchanged'
    hugepages_2m: int = Field(default=0, ge=0, le=32768)
    @model_validator(mode='after')
    def validate_values(self):
        if '..' in self.timezone.split('/') or not Path('/usr/share/zoneinfo', self.timezone).is_file():
            raise ValueError('Invalid timezone')
        if any(not re.fullmatch(r'[a-zA-Z0-9.:-]{1,253}', host) for host in self.ntp):
            raise ValueError('Invalid NTP server')
        if self.proxy:
            from urllib.parse import urlparse
            url = urlparse(self.proxy)
            if url.scheme not in ('http', 'https') or not url.hostname or url.username or '\n' in self.proxy:
                raise ValueError('Proxy must be HTTP(S), without credentials')
        return self

class BackupRequest(BaseModel):
    include_models: bool = False
    scheduled: bool = False

class TLSRequest(BaseModel):
    certificate: str = Field(max_length=65536)
    private_key: str = Field(max_length=32768)


def run(args, timeout=120):
    return subprocess.run(args, check=True, capture_output=True, text=True, timeout=timeout).stdout.strip()

def enqueue(action, payload):
    key = uid()
    execute('INSERT INTO system_jobs VALUES (?,?,?,?,?,?)', (key, action, encode(payload), 'QUEUED', None, now()))
    return {'id': key, 'state': 'QUEUED', 'action': action}

def chat_voice():
    """Point the chat's read-aloud button at the published voice model, or back
    at the browser's voice; the chat reads the setting on its next request."""
    from . import webui_config
    database = DATA / 'webui' / 'webui.db'
    key = (ETC / 'secrets' / 'inference-key').read_text().strip()
    changed = webui_config.reconcile(database, key, bool(setting('default_voice_model', '')))
    # SQLite may have created its journal files as root: they belong to the chat.
    for path in (database, database.with_name('webui.db-wal'), database.with_name('webui.db-shm')):
        if path.exists():
            run(['chown', 'aios-webui:aios-webui', str(path)])
    return {'changed': changed}


def safe_archive(archive, target, max_size=100 * 1024 ** 3):
    total = 0
    with tarfile.open(archive, 'r:*') as tar:
        for member in tar:
            path = Path(member.name)
            if path.is_absolute() or '..' in path.parts or not (member.isfile() or member.isdir()):
                raise ValueError('Unsafe archive member')
            total += member.size
            if total > max_size:
                raise ValueError('Archive exceeds allowed size')
            tar.extract(member, target, filter='data')


def readable_tree(root):
    """Make an extracted release readable by the services that run it. The broker
    runs as root with a private umask, so the staged directory came out owned by
    root and mode 0700: every unit then failed with "changing to the requested
    working directory failed", and the release was rolled back for the wrong
    reason. Ownership from the archive is dropped too: those ids mean nothing here."""
    for path in [root, *root.rglob('*')]:
        try:
            os.chown(path, 0, 0)
            if path.is_dir():
                path.chmod(0o755)
            elif path.is_file():
                mode = path.stat().st_mode & 0o777
                path.chmod(0o755 if mode & 0o100 else 0o644)
        except OSError:
            continue

def backup(include_models, scheduled=False):
    key = uid()
    target = DATA / 'backups' / (key + '.tar.gz')
    with tempfile.TemporaryDirectory(dir=DATA / 'backups') as temporary:
        stage = Path(temporary)
        with connection() as source:
            import sqlite3
            dest = sqlite3.connect(stage / 'aios.db')
            source.backup(dest)
            dest.close()
        # Consistent Open WebUI SQLite snapshot; stop it while copying other persistent files.
        run(['systemctl', 'stop', 'aios-open-webui.service'])
        try:
            with tarfile.open(target.with_suffix('.part'), 'w:gz') as tar:
                tar.add(stage / 'aios.db', arcname='database/aios.db')
                tar.add(ETC, arcname='etc')
                tar.add(DATA / 'webui', arcname='webui')
                tar.add(DATA / 'system/hardware-profile.json', arcname='system/hardware-profile.json')
                if include_models:
                    tar.add(DATA / 'models', arcname='models')
        finally:
            run(['systemctl', 'start', 'aios-open-webui.service'])
    os.replace(target.with_suffix('.part'), target)
    passphrase = backups.stored_passphrase()
    if passphrase:
        # Accounts, credentials and chats never rest unencrypted once a
        # passphrase is set: the plain archive is sealed, then removed.
        sealed = target.with_name(key + backups.SEALED)
        backups.encrypt_file(target, sealed, passphrase)
        target.unlink()
        target = sealed
    backups.fix_permissions(target)
    backups.fix_permissions(backups.record(target, scheduled=scheduled, include_models=include_models, encrypted=bool(passphrase)))
    removed = backups.prune(int((setting('backup_schedule') or {}).get('keep', 7))) if scheduled else []
    return {'file': target.name, 'encrypted': bool(passphrase), 'scheduled': scheduled, 'removed': removed,
            'sha256': hashlib.file_digest(target.open('rb'), 'sha256').hexdigest()}

def restore(filename, passphrase=''):
    if not re.fullmatch(backups.NAME, filename):
        raise ValueError('Invalid backup filename')
    with tempfile.TemporaryDirectory(dir=DATA / 'backups') as temporary:
        stage = Path(temporary)
        archive = DATA / 'backups' / filename
        if backups.is_encrypted(archive):
            # The passphrase given with the request, for an archive from another
            # appliance, or the one this appliance seals its own backups with.
            opened = stage / 'archive.tar.gz'
            backups.decrypt_file(archive, opened, passphrase or backups.stored_passphrase())
            archive = opened
        safe_archive(archive, stage)
        import sqlite3
        with sqlite3.connect(stage / 'database/aios.db') as db:
            if db.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                raise ValueError('Backup database integrity failed')
            if db.execute('SELECT max(version) FROM schema_migrations').fetchone()[0] != 1:
                raise ValueError('Unsupported backup schema')
            db.execute('DELETE FROM sessions')
        for path in (stage / 'etc', stage / 'webui'):
            if not path.is_dir():
                raise ValueError('Incomplete backup')
        run(['systemctl', 'stop', 'aios-control-plane', 'aios-download-worker', 'aios-runtime-manager', 'aios-open-webui'])
        try:
            recovery = DATA / 'backups' / ('pre-restore-' + str(int(now())))
            recovery.mkdir()
            shutil.copytree(DATA / 'database', recovery / 'database')
            shutil.copytree(ETC, recovery / 'etc')
            shutil.copytree(DATA / 'webui', recovery / 'webui')
            for source, target in [(stage / 'database', DATA / 'database'), (stage / 'etc', ETC), (stage / 'webui', DATA / 'webui')]:
                shutil.rmtree(target)
                shutil.copytree(source, target)
            if (stage / 'models').exists():
                shutil.copytree(stage / 'models', DATA / 'models', dirs_exist_ok=True)
            for model in rows('SELECT id,path FROM installed_models'):
                if not Path(model['path']).is_file():
                    execute("UPDATE installed_models SET state='DISABLED',published=0 WHERE id=?", (model['id'],))
            run(['chown', '-R', 'aios:aios', str(DATA / 'database'), str(ETC)])
            run(['chown', '-R', 'aios-webui:aios-webui', str(DATA / 'webui')])
            run(['chown', '-R', 'aios:aios', str(DATA / 'models')])
            # The restored chat keeps the settings it had; point it back at this
            # appliance before it starts, as a boot would.
            from .webui_config import reconcile
            try:
                reconcile(DATA / 'webui' / 'webui.db', (ETC / 'secrets' / 'inference-key').read_text().strip())
                run(['chown', '-R', 'aios-webui:aios-webui', str(DATA / 'webui')])
            except (OSError, ValueError, subprocess.SubprocessError):
                pass
            audit('platform', 'restore_complete', filename)
        finally:
            run(['systemctl', 'start', 'aios-control-plane', 'aios-download-worker', 'aios-runtime-manager', 'aios-open-webui'])
    return {'restored': filename}

def network_apply(payload, key):
    config = NetworkConfig.model_validate(payload)
    if not Path('/sys/class/net', config.interface).exists():
        raise ValueError('Network interface does not exist')
    path = Path('/etc/netplan/10-aios.yaml')
    old = path.read_text() if path.exists() else ''
    config_net = {'dhcp4': config.dhcp, 'dhcp6': config.dhcp}
    if not config.dhcp:
        config_net.update({'addresses': [config.address], 'routes': [{'to': 'default', 'via': config.gateway}]})
    if config.dns:
        config_net['nameservers'] = {'addresses': config.dns}
    atomic_write(path, yaml.safe_dump({'network': {'version': 2, 'renderer': 'networkd', 'ethernets': {config.interface: config_net}}}))
    atomic_write('/var/lib/aios-network-rollback.json', encode({'previous': old, 'expires': now() + 120, 'job': key}))
    run(['netplan', 'generate'])
    run(['netplan', 'apply'])
    return {'confirmation_required': key, 'expires_in': 120}

def apply_system(payload):
    config = SystemConfig.model_validate(payload)
    run(['hostnamectl', 'set-hostname', config.hostname])
    run(['timedatectl', 'set-timezone', config.timezone])
    atomic_write('/etc/systemd/timesyncd.conf.d/aios.conf', '[Time]\nNTP=' + ' '.join(config.ntp) + '\n', 0o644)
    run(['systemctl', 'restart', 'systemd-timesyncd'])
    atomic_write(ETC / 'proxy.env', 'HTTP_PROXY=' + config.proxy + '\nHTTPS_PROXY=' + config.proxy + '\nNO_PROXY=localhost,127.0.0.1\n', 0o640)
    if config.governor != 'unchanged':
        for path in Path('/sys/devices/system/cpu').glob('cpu[0-9]*/cpufreq/scaling_governor'):
            supported = path.with_name('scaling_available_governors').read_text().split()
            if config.governor in supported:
                path.write_text(config.governor)
    if config.hugepages_2m:
        import psutil
        if config.hugepages_2m * 2 * 1024 ** 2 > psutil.virtual_memory().available // 2:
            raise ValueError('HugePage reservation exceeds half available RAM')
    Path('/proc/sys/vm/nr_hugepages').write_text(str(config.hugepages_2m))
    set_setting('system_config', config.model_dump())
    return config.model_dump()

def tls_apply(payload):
    config = TLSRequest.model_validate(payload)
    cert = x509.load_pem_x509_certificate(config.certificate.encode())
    key = serialization.load_pem_private_key(config.private_key.encode(), password=None)
    if cert.public_key().public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo) != key.public_key().public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo):
        raise ValueError('Certificate and private key mismatch')
    old_cert = Path('/etc/nginx/aios.crt').read_bytes()
    old_key = Path('/etc/nginx/aios.key').read_bytes()
    try:
        atomic_write('/etc/nginx/aios.crt', config.certificate, 0o644)
        atomic_write('/etc/nginx/aios.key', config.private_key)
        run(['nginx', '-t'])
        run(['systemctl', 'reload', 'nginx'])
    except (OSError, subprocess.SubprocessError):
        atomic_write('/etc/nginx/aios.crt', old_cert, 0o644)
        atomic_write('/etc/nginx/aios.key', old_key)
        raise ValueError('TLS configuration rejected; previous certificate restored')
    return {'updated': True}

# The appliance ships with no trust anchor: whoever runs it installs the public
# key of whoever they accept releases from, from the portal or the console.
def release_key_path():
    return DATA / 'system' / 'release.pub'


# What a signed release may replace, where it lives, what proves it is whole and
# which services run it. The three inference engines are children of the runtime
# manager, so restarting it is what puts a new engine to work.
RELEASE_COMPONENTS = {
    'application': {'directory': 'app', 'entrypoint': 'backend/aios/app.py',
                    'services': ['aios-control-plane', 'aios-download-worker', 'aios-runtime-manager']},
    'runtime': {'directory': 'runtime', 'entrypoint': 'bin/llama-server', 'services': ['aios-runtime-manager']},
    'imaging': {'directory': 'imaging', 'entrypoint': 'bin/sd-server', 'services': ['aios-runtime-manager']},
    'voice': {'directory': 'voice', 'entrypoint': 'bin/whisper-server', 'services': ['aios-runtime-manager']},
    'open-webui': {'directory': 'webui', 'entrypoint': 'bin/open-webui', 'services': ['aios-open-webui']},
    # Optional: llama.cpp's CUDA backend and the CUDA libraries, for NVIDIA cards.
    'cuda': {'directory': 'cuda', 'entrypoint': 'lib/libggml-cuda.so', 'services': ['aios-runtime-manager']},
}


def load_release_key():
    """The Ed25519 public key releases are verified against, or a clear refusal."""
    for path in (release_key_path(), Path('/etc/aios-release.pub')):
        try:
            data = path.read_bytes()
        except OSError:
            continue
        pub = serialization.load_pem_public_key(data)
        if not isinstance(pub, Ed25519PublicKey):
            raise ValueError('Release key must be Ed25519')
        return pub
    raise ValueError('No release key installed: add the public key of whoever signs your updates under System, Updates')


# A unit can be "active" with a process that answers nothing: an update that
# breaks the application must be caught here, not by the first user.
HEALTH = {'application': 'http://127.0.0.1:8081/health', 'open-webui': 'http://127.0.0.1:8080/health'}


def answers(component, attempts=20):
    """Wait for the updated component to serve requests again."""
    url = HEALTH.get(component)
    if not url:
        return
    for attempt in range(attempts):
        try:
            with urllib.request.urlopen(url, timeout=5) as response:
                if response.status == 200:
                    return
        except (urllib.error.URLError, OSError):
            pass
        time.sleep(3 if attempt else 1)
    raise ValueError('The updated component did not answer on ' + url)


def restart(units):
    """Start units after a failed release: systemd counts the crashes of the
    release being replaced, and "start request repeated too quickly" would leave
    the appliance with nothing running."""
    try:
        run(['systemctl', 'reset-failed', *units])
    except subprocess.SubprocessError:
        pass
    run(['systemctl', 'start', *units])


def update_release(payload):
    manifest = payload['manifest']
    signature = base64.b64decode(payload['signature'], validate=True)
    pub = load_release_key()
    pub.verify(signature, encode(manifest).encode())
    component = manifest['component']
    if component not in RELEASE_COMPONENTS:
        raise ValueError('Platform updates require verified reinstallation and backup restore')
    filename = payload['file']
    if not re.fullmatch(r'[a-f0-9-]{36}\.tar\.gz', filename):
        raise ValueError('Invalid release filename')
    archive = DATA / 'backups' / filename
    with archive.open('rb') as stream:
        if hashlib.file_digest(stream, 'sha256').hexdigest() != manifest['sha256']:
            raise ValueError('Release checksum mismatch')
    target = Path('/opt/aios') / RELEASE_COMPONENTS[component]['directory']
    service = RELEASE_COMPONENTS[component]['services']
    with tempfile.TemporaryDirectory(dir=target.parent) as temporary:
        stage = Path(temporary) / 'content'
        stage.mkdir()
        safe_archive(archive, stage, 10 * 1024 ** 3)
        readable_tree(stage)
        marker = RELEASE_COMPONENTS[component]['entrypoint']
        if not (stage / marker).is_file():
            raise ValueError('Release missing component entrypoint')
        recovery = target.with_name(target.name + '.previous')
        if recovery.exists():
            shutil.rmtree(recovery)
        run(['systemctl', 'stop', *service])
        # An optional component may be installed for the first time.
        first = not target.exists()
        if not first:
            target.rename(recovery)
        stage.rename(target)
        try:
            if component == 'application':
                # Units, the NGINX site and the firewall rules come with the code.
                system_files.sync(target)
            restart(service)
            time.sleep(10)
            for unit in service:
                run(['systemctl', 'is-active', unit])
            answers(component)
        except (subprocess.SubprocessError, ValueError) as exc:
            run(['systemctl', 'stop', *service])
            shutil.rmtree(target)
            if not first:
                recovery.rename(target)
            if component == 'application':
                try:
                    system_files.sync(target)
                except (ValueError, subprocess.SubprocessError, OSError):
                    pass
            # Without this the rollback inherits the failed release's restart
            # counter and systemd refuses to start anything at all.
            restart(service)
            raise ValueError(f'Release health check failed ({exc}); previous component restored') from None
    (target / 'VERSION').write_text(str(manifest['version']) + '\n')
    os.chmod(target / 'VERSION', 0o644)
    return {'component': component, 'version': manifest['version'], 'rollback': None if first else str(recovery)}


def remove_component(component):
    """Take out an optional component; it is kept as .previous, like an update."""
    if component != 'cuda':
        raise ValueError('Only the CUDA package can be removed')
    target = Path('/opt/aios') / RELEASE_COMPONENTS[component]['directory']
    if not target.exists():
        raise ValueError('The CUDA package is not installed')
    service = RELEASE_COMPONENTS[component]['services']
    recovery = target.with_name(target.name + '.previous')
    if recovery.exists():
        shutil.rmtree(recovery)
    run(['systemctl', 'stop', *service])
    target.rename(recovery)
    restart(service)
    return {'component': component, 'removed': True, 'kept': str(recovery)}

async def broker():
    while True:
        rollback = Path('/var/lib/aios-network-rollback.json')
        if rollback.exists():
            state = json.loads(rollback.read_text())
            if state['expires'] < now():
                atomic_write('/etc/netplan/10-aios.yaml', state['previous'])
                try:
                    run(['netplan', 'apply'])
                    audit('platform', 'network_rollback', state['job'])
                    rollback.unlink()
                except subprocess.SubprocessError:
                    pass
        schedule = setting('backup_schedule') or {}
        if backups.due(schedule, setting('backup_last_scheduled', ''), now()):
            set_setting('backup_last_scheduled', time.strftime('%Y-%m-%d', time.localtime(now())))
            enqueue('backup', {'include_models': bool(schedule.get('include_models')), 'scheduled': True})
            audit('platform', 'backup_scheduled')
        for job in rows("SELECT * FROM system_jobs WHERE state='QUEUED' ORDER BY created_at"):
            execute("UPDATE system_jobs SET state='RUNNING' WHERE id=?", (job['id'],))
            try:
                payload = json.loads(job['payload'])
                action = job['action']
                if action == 'network':
                    result = network_apply(payload, job['id'])
                elif action == 'network-confirm':
                    state = json.loads(rollback.read_text())
                    if state['job'] != payload['id'] or state['expires'] < now():
                        raise ValueError('Network confirmation expired or wrong job')
                    rollback.unlink()
                    result = {'confirmed': True}
                elif action == 'system':
                    result = apply_system(payload)
                elif action == 'tls':
                    result = tls_apply(payload)
                elif action == 'backup':
                    request = BackupRequest.model_validate(payload)
                    result = backup(request.include_models, request.scheduled)
                elif action == 'component-remove':
                    result = remove_component(payload['component'])
                elif action == 'chat-voice':
                    result = chat_voice()
                elif action == 'restore':
                    result = restore(payload['file'], payload.get('passphrase', ''))
                elif action == 'update':
                    result = update_release(payload)
                elif action == 'os-update':
                    # The same script the console runs: check lists what Ubuntu
                    # offers, apply installs it and records whether to reboot.
                    mode = 'apply' if payload.get('mode') == 'apply' else 'check'
                    done = subprocess.run(['/opt/aios/app/installer/updates.sh', mode], capture_output=True, text=True, timeout=3600)
                    result = {'mode': mode, 'exit_code': done.returncode, 'output': (done.stdout + done.stderr)[-2000:]}
                elif action in ('reboot', 'shutdown'):
                    run(['shutdown', '-r' if action == 'reboot' else '-h', '+1'])
                    result = {'scheduled_in_seconds': 60}
                else:
                    raise ValueError('Unknown privileged operation')
                execute("INSERT INTO system_jobs(id,action,payload,state,result,created_at) VALUES (?,?,'{}','COMPLETED',?,?) ON CONFLICT(id) DO UPDATE SET state='COMPLETED',result=excluded.result,payload='{}'", (job['id'], action, encode(result), job['created_at']))
                audit('platform', action + '_complete', job['id'])
            except Exception as exc:
                execute("UPDATE system_jobs SET state='FAILED',result=?,payload='{}' WHERE id=?", (encode({'error': type(exc).__name__ + ': ' + (str(exc) if isinstance(exc, ValueError) else 'System operation failed; consult journal')}), job['id']))
                audit('platform', 'system_job_failed', job['id'])
        await asyncio.sleep(2)
