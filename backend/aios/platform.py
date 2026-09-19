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
import tempfile
import time
from pathlib import Path
from typing import Literal
import yaml
from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from pydantic import BaseModel, Field, model_validator
from .core import DATA, ETC, atomic_write, audit, connection, encode, execute, now, rows, set_setting, uid

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
    hostname: str = Field(pattern=r'^[a-zA-Z0-9][a-zA-Z0-9-]{0,62}$')
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

class TLSRequest(BaseModel):
    certificate: str = Field(max_length=65536)
    private_key: str = Field(max_length=32768)


def run(args, timeout=120):
    return subprocess.run(args, check=True, capture_output=True, text=True, timeout=timeout).stdout.strip()

def enqueue(action, payload):
    key = uid()
    execute('INSERT INTO system_jobs VALUES (?,?,?,?,?,?)', (key, action, encode(payload), 'QUEUED', None, now()))
    return {'id': key, 'state': 'QUEUED'}

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

def backup(include_models):
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
    os.chmod(target, 0o600)
    os.chown(target, shutil._get_uid('aios'), shutil._get_gid('aios'))
    return {'file': target.name, 'sha256': hashlib.file_digest(target.open('rb'), 'sha256').hexdigest()}

def restore(filename):
    if not re.fullmatch(r'[a-f0-9-]{36}\.tar\.gz', filename):
        raise ValueError('Invalid backup filename')
    with tempfile.TemporaryDirectory(dir=DATA / 'backups') as temporary:
        stage = Path(temporary)
        safe_archive(DATA / 'backups' / filename, stage)
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

def update_release(payload):
    manifest = payload['manifest']
    signature = base64.b64decode(payload['signature'], validate=True)
    pub = serialization.load_pem_public_key(Path('/etc/aios-release.pub').read_bytes())
    if not isinstance(pub, Ed25519PublicKey):
        raise ValueError('Release key must be Ed25519')
    pub.verify(signature, encode(manifest).encode())
    component = manifest['component']
    if component not in ('application', 'runtime', 'open-webui'):
        raise ValueError('Platform updates require verified reinstallation and backup restore')
    filename = payload['file']
    if not re.fullmatch(r'[a-f0-9-]{36}\.tar\.gz', filename):
        raise ValueError('Invalid release filename')
    archive = DATA / 'backups' / filename
    with archive.open('rb') as stream:
        if hashlib.file_digest(stream, 'sha256').hexdigest() != manifest['sha256']:
            raise ValueError('Release checksum mismatch')
    target = Path('/opt/aios') / {'application': 'app', 'runtime': 'runtime', 'open-webui': 'webui'}[component]
    service = {'application': ['aios-control-plane', 'aios-download-worker', 'aios-runtime-manager'], 'runtime': ['aios-runtime-manager'], 'open-webui': ['aios-open-webui']}[component]
    with tempfile.TemporaryDirectory(dir=target.parent) as temporary:
        stage = Path(temporary) / 'content'
        stage.mkdir()
        safe_archive(archive, stage, 10 * 1024 ** 3)
        marker = {'application': 'backend/aios/app.py', 'runtime': 'bin/llama-server', 'open-webui': 'bin/open-webui'}[component]
        if not (stage / marker).is_file():
            raise ValueError('Release missing component entrypoint')
        recovery = target.with_name(target.name + '.previous')
        if recovery.exists():
            shutil.rmtree(recovery)
        run(['systemctl', 'stop', *service])
        target.rename(recovery)
        stage.rename(target)
        try:
            run(['systemctl', 'start', *service])
            time.sleep(10)
            for unit in service:
                run(['systemctl', 'is-active', unit])
        except subprocess.SubprocessError:
            run(['systemctl', 'stop', *service])
            shutil.rmtree(target)
            recovery.rename(target)
            run(['systemctl', 'start', *service])
            raise ValueError('Release health check failed; previous component restored')
    return {'component': component, 'version': manifest['version'], 'rollback': str(recovery)}

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
                    result = backup(BackupRequest.model_validate(payload).include_models)
                elif action == 'restore':
                    result = restore(payload['file'])
                elif action == 'update':
                    result = update_release(payload)
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
