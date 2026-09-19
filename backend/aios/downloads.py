import asyncio
import logging
import sqlite3
import hashlib
import json
import os
import shutil
import time
from urllib.parse import urljoin, urlparse
import httpx
from .core import DATA, audit, encode, execute, now, one, projector_path, rows, setting
from . import imaging
from .gguf import inspect_gguf, inspect_projector
from .providers import credential, projector_for, request_target

def describe(exc):
    """The whole reason, not just the exception class: "OperationalError" alone
    told the operator nothing about why a model failed to install."""
    name = getattr(exc, 'sqlite_errorname', None)
    return (type(exc).__name__ + (f' {name}' if name else '') + f': {exc}')[:500]


def partial_files(job_id):
    """Every partial file a download job may leave: the model and its companions."""
    return list((DATA / 'downloads').glob(job_id + '*.part'))


def companions(model_id, metadata):
    """Files that must be installed beside the model: a language model's vision
    projector, or the encoders and VAE a diffusion model is split into. Each one
    is (role, specification, destination)."""
    wanted = []
    projector = projector_for(model_id)
    if projector:
        wanted.append(('mmproj', projector, projector_path(model_id)))
    for component in imaging.components(metadata):
        wanted.append((component['role'], component, imaging.component_path(model_id, component['role'], component['filename'])))
    return wanted


async def fetch(job, model, url, part, expected, done_before, total):
    """Stream one file into its .part, resuming what is already there. Returns the
    bytes on disk, or None when the job was paused or cancelled meanwhile."""
    offset = part.stat().st_size if part.exists() else 0
    if offset >= expected:
        return offset
    headers = {'Accept-Encoding': 'identity', 'User-Agent': 'AIOS/1.0'}
    token = credential(model['repository_id'])
    if token and urlparse(url).hostname == urlparse(model['repo_url']).hostname:
        headers['Authorization'] = 'Bearer ' + token
    if offset:
        headers['Range'] = f'bytes={offset}-'
    allow_private = json.loads(model['repo_config']).get('allow_private', False)
    start, last = time.monotonic(), time.monotonic()
    base_offset = offset
    async with httpx.AsyncClient(timeout=httpx.Timeout(60, read=120), follow_redirects=False, trust_env=False, proxy=setting('system_config', {}).get('proxy') or None) as client:
        for redirect in range(6):
            target_url, outgoing_headers, extensions = request_target(url, headers, allow_private)
            async with client.stream('GET', target_url, headers=outgoing_headers, extensions=extensions) as response:
                if response.is_redirect:
                    next_url = urljoin(url, response.headers['location'])
                    if urlparse(next_url).hostname != urlparse(url).hostname:
                        headers.pop('Authorization', None)
                    url = next_url
                    continue
                response.raise_for_status()
                if response.status_code == 206:
                    if not response.headers.get('content-range', '').startswith(f'bytes {offset}-'):
                        raise ValueError('Invalid HTTP resume range')
                elif response.status_code == 200:
                    offset = base_offset = 0
                else:
                    raise ValueError('Unexpected HTTP download status')
                with part.open('ab' if offset else 'wb') as stream:
                    async for chunk in response.aiter_bytes(1024 * 256):
                        offset += len(chunk)
                        if offset > expected:
                            raise ValueError('Download exceeds advertised size')
                        stream.write(chunk)
                        current = time.monotonic()
                        # State used to be read once per 256 KiB chunk: ~4,000
                        # connections per GB against a database shared with
                        # every other service. Twice a second is plenty to
                        # notice a cancellation.
                        if current - last > .5:
                            state = one('SELECT state FROM downloads WHERE id=?', (job['id'],))['state']
                            if state != 'DOWNLOADING':
                                if state == 'CANCELLED':
                                    stream.close()
                                    for leftover in partial_files(job['id']):
                                        leftover.unlink(missing_ok=True)
                                return None
                            speed = (offset - base_offset) / max(.001, current - start)
                            done = done_before + offset
                            execute('UPDATE downloads SET downloaded=?,speed=?,eta=?,updated_at=? WHERE id=?', (done, speed, (total - done) / max(1, speed), now(), job['id']))
                            last = current
                    stream.flush()
                    os.fsync(stream.fileno())
                return offset
    raise ValueError('Too many redirects')


async def download(job):
    model = one('SELECT d.*,a.url,a.size,a.sha256,r.config AS repo_config,r.url AS repo_url FROM discovered_models d JOIN model_artifacts a ON a.id=d.id JOIN repositories r ON r.id=d.repository_id WHERE d.id=?', (job['model_id'],))
    part = DATA / 'downloads' / (job['id'] + '.part')
    parts = [part]
    try:
        metadata = json.loads(model['metadata'])
        image = imaging.is_image_model(metadata)
        # A model installed before its companions were tracked needs only those.
        installed = one('SELECT id FROM installed_models WHERE id=?', (model['id'],)) is not None
        target = imaging.model_path(model['id'], metadata.get('filename', '')) if image else DATA / 'models' / (model['id'] + '.gguf')
        pending = [(role, spec, destination) for role, spec, destination in companions(model['id'], metadata) if not destination.exists()]
        expected = 0 if installed else model['size']
        total = expected + sum(int(spec['size']) for _, spec, _ in pending)
        if not total:
            execute("UPDATE downloads SET state='INSTALLED',speed=0,eta=0,updated_at=? WHERE id=?", (now(), job['id']))
            return
        if total > setting('max_model_bytes', 1024 ** 4):
            raise ValueError('Model exceeds configured size limit')
        parts += [DATA / 'downloads' / f"{job['id']}.{role}.part" for role, _, _ in pending]
        on_disk = sum(p.stat().st_size for p in parts if p.exists())
        if total - on_disk + 64 * 1024 ** 2 > shutil.disk_usage(DATA).free:
            raise ValueError('Insufficient disk space')
        if not installed:
            offset = await fetch(job, model, model['url'], part, expected, 0, total)
            if offset is None:
                return
            if offset != expected:
                raise ValueError('Downloaded size mismatch')
        done = expected
        for (role, spec, _), companion_part in zip(pending, parts[1:]):
            received = await fetch(job, model, spec['url'], companion_part, int(spec['size']), done, total)
            if received is None:
                return
            if received != int(spec['size']):
                raise ValueError(f'Downloaded {role} size mismatch')
            done += received
        if execute("UPDATE downloads SET state='VERIFYING',downloaded=?,updated_at=? WHERE id=? AND state='DOWNLOADING'", (total, now(), job['id'])) != 1:
            return
        if not installed:
            sha = await asyncio.to_thread(hash_file, part)
            if model['sha256'] and sha != model['sha256'].lower():
                raise ValueError('SHA256 mismatch')
            # A diffusion checkpoint is not a GGUF language model; its structure is
            # checked by the engine when it loads it, the checksum by us.
            record = {'kind': 'image', 'family': metadata.get('family'), 'components': [c['role'] for c in imaging.components(metadata)]} \
                if image else await asyncio.to_thread(inspect_gguf, part)
        note = None
        for (role, spec, destination), companion_part in zip(pending, parts[1:]):
            try:
                companion_sha = await asyncio.to_thread(hash_file, companion_part)
                if spec.get('sha256') and companion_sha != spec['sha256'].lower():
                    raise ValueError(f'{role} SHA256 mismatch')
                if role == 'mmproj':
                    await asyncio.to_thread(inspect_projector, companion_part)
                os.chmod(companion_part, 0o440)
                os.replace(companion_part, destination)
            except ValueError as exc:
                companion_part.unlink(missing_ok=True)
                # A diffusion model without its encoders cannot run at all, while a
                # language model without its projector still answers text.
                if installed or role != 'mmproj':
                    raise
                note = f'Installed without image input: {exc}'
                audit('download-worker', 'projector_rejected', model['id'], {'error': str(exc)})
        if not installed:
            os.chmod(part, 0o440)
            os.replace(part, target)
            execute('INSERT INTO installed_models(id,path,sha256,installed_at,state,gguf) VALUES (?,?,?,?,?,?)', (model['id'], str(target), sha, now(), 'INSTALLED', encode(record)))
        execute("UPDATE downloads SET state='INSTALLED',speed=0,eta=0,error=?,updated_at=? WHERE id=?", (note, now(), job['id']))
        if installed:
            audit('download-worker', 'companions_installed', model['id'], {'roles': [role for role, _, _ in pending]})
        else:
            audit('download-worker', 'model_installed', model['id'], {'sha256': sha, 'upstream_checksum': bool(model['sha256']),
                                                                      'kind': 'image' if image else 'text', 'companions': [role for role, _, _ in pending]})
    except (ValueError, OSError, httpx.HTTPError, KeyError, sqlite3.Error) as exc:
        attempts = job['attempts'] + 1
        # A busy or momentarily unavailable database is contention, not a bad
        # artifact: keep the partial file and resume, as for a network error.
        transient = isinstance(exc, (httpx.TransportError, httpx.HTTPStatusError, sqlite3.OperationalError))
        if isinstance(exc, sqlite3.Error):
            logging.warning('download %s: database error, will retry: %s', job['id'], describe(exc))
        retry = transient and attempts < 5
        if not retry:
            for leftover in partial_files(job['id']):
                leftover.unlink(missing_ok=True)
        execute('UPDATE downloads SET state=?,attempts=?,next_retry=?,error=?,speed=0,updated_at=? WHERE id=?', ('QUEUED' if retry else 'FAILED', attempts, now() + 2 ** attempts * 5, describe(exc), now(), job['id']))
        audit('download-worker', 'download_retry' if retry else 'download_failed', job['model_id'])

def hash_file(path):
    with open(path, 'rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()

async def worker():
    execute("UPDATE downloads SET state='QUEUED' WHERE state IN ('DOWNLOADING','VERIFYING')")
    active = {}
    while True:
        # One failed query used to crash the whole worker, dropping every download
        # in flight; five crashes in two minutes made systemd give up on it for good.
        try:
            for key in list(active):
                if active[key].done():
                    try:
                        active.pop(key).result()
                    except Exception as exc:
                        logging.warning('download %s failed: %s', key, describe(exc))
                        execute("UPDATE downloads SET state='FAILED',error=? WHERE id=?", (describe(exc), key))
            concurrency = min(8, max(1, setting('download_concurrency', 2)))
            for job in rows("SELECT * FROM downloads WHERE state='QUEUED' AND next_retry<=? ORDER BY created_at LIMIT ?", (now(), max(0, concurrency - len(active)))):
                if job['id'] in active:
                    continue
                if execute("UPDATE downloads SET state='DOWNLOADING',error=NULL WHERE id=? AND state='QUEUED'", (job['id'],)):
                    active[job['id']] = asyncio.create_task(download(job))
        except sqlite3.Error as exc:
            logging.warning('download worker: database unavailable, retrying: %s', describe(exc))
        await asyncio.sleep(1)
