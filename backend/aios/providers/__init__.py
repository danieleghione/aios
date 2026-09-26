"""Metadata-only provider adapters. All URLs are validated before requests."""
import asyncio
import ipaddress
import json
import re
import socket
import time
from urllib.parse import quote, urljoin, urlparse
import httpx
from ..gguf import draft_reason, not_a_model
from ..voice import is_voice_architecture
from .. import alerts, revisions
from ..core import ETC, audit, encode, execute, now, one, rows, setting, uid

MAX_METADATA = 8 * 1024 * 1024

class RepositoryError(ValueError):
    pass

def resolve_url(url, allow_private=False):
    parsed = urlparse(url)
    if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password or parsed.fragment:
        raise RepositoryError('Only HTTPS URLs without credentials or fragments are allowed')
    try:
        addresses = socket.getaddrinfo(parsed.hostname, parsed.port or 443, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise RepositoryError('Repository DNS resolution failed') from exc
    for address in addresses:
        ip = ipaddress.ip_address(address[4][0])
        if ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_unspecified or (not allow_private and not ip.is_global):
            raise RepositoryError('Repository address is not permitted')
    return parsed, addresses

def validate_url(url, allow_private=False):
    return resolve_url(url, allow_private)[0]

def request_target(url, headers, allow_private=False):
    # Connect to the exact validated IP, preserving TLS SNI and HTTP Host.
    # This prevents a second DNS lookup from rebinding to a prohibited address.
    parsed, addresses = resolve_url(url, allow_private)
    address = next((a for a in addresses if a[0] == socket.AF_INET), addresses[0])[4][0]
    target = httpx.URL(url).copy_with(host=address)
    outgoing = dict(headers)
    outgoing['Host'] = parsed.netloc
    return target, outgoing, {'sni_hostname': parsed.hostname}

def credential(repo_id):
    ref = one('SELECT secret_ref FROM repository_credentials WHERE repository_id=?', (repo_id,))
    return (ETC / 'secrets' / ref['secret_ref']).read_text().strip() if ref else None

async def request_json(url, repo, method='GET', payload=None):
    config = json.loads(repo['config'])
    headers = {'Accept': 'application/json', 'User-Agent': 'AIOS/1.0'}
    token = credential(repo['id'])
    if token and urlparse(url).hostname == urlparse(repo['url']).hostname:
        headers['Authorization'] = 'Bearer ' + token
    async with httpx.AsyncClient(timeout=30, follow_redirects=False, trust_env=False, proxy=setting('system_config', {}).get('proxy') or None) as client:
        for _ in range(6):
            target, outgoing, extensions = request_target(url, headers, config.get('allow_private', False))
            async with client.stream(method, target, headers=outgoing, json=payload, extensions=extensions) as response:
                if response.is_redirect:
                    next_url = urljoin(url, response.headers['location'])
                    if urlparse(next_url).hostname != urlparse(url).hostname:
                        headers.pop('Authorization', None)
                    url = next_url
                    continue
                if response.status_code in (401, 403):
                    raise RepositoryError('AUTH REQUIRED')
                if response.status_code == 429:
                    raise RepositoryError('RATE LIMITED')
                if response.status_code != 200:
                    raise RepositoryError(f'Repository HTTP {response.status_code}')
                content = bytearray()
                async for chunk in response.aiter_bytes():
                    content.extend(chunk)
                    if len(content) > MAX_METADATA:
                        raise RepositoryError('Repository metadata exceeds 8 MiB')
                return json.loads(content)
        raise RepositoryError('Too many metadata redirects')

HEADER_PREFIX = 2 * 1024 * 1024
_SCALARS = {0: 'B', 1: 'b', 2: 'H', 3: 'h', 4: 'I', 5: 'i', 6: 'f', 7: '?', 10: 'Q', 11: 'q', 12: 'd'}
_DIMENSIONS = ('block_count', 'embedding_length', 'context_length', 'attention.head_count',
               'attention.head_count_kv', 'attention.key_length', 'attention.value_length')
# Not sizes, but what marks a file as a draft for another model (see gguf.draft_reason).
_STRUCTURE = ('target_layers', 'nextn_predict_layers')
# Files differing only in quantisation share one layout, so one header read covers them.
QUANTISATION = re.compile(r'(?:IQ|TQ|MXFP|BF|FP|Q|F)[0-9][A-Za-z0-9_]*', re.I)
HEADER_READS_PER_MODEL = 16
NO_ARCHITECTURE = 'no architecture'


def header_dimensions(data):
    """Read the sizes that decide a model's KV cache from the start of a GGUF
    file. They come before the tokenizer, so a small prefix is enough; truncation
    just ends the scan with whatever was reached. Nothing here is executed."""
    import struct
    view, position, found = memoryview(data), 0, {}

    def take(length):
        nonlocal position
        if length < 0 or position + length > len(view):
            raise EOFError
        chunk = view[position:position + length]
        position += length
        return chunk

    def number(fmt):
        return struct.unpack('<' + fmt, take(struct.calcsize('<' + fmt)))[0]

    def text():
        length = number('Q')
        if length > 1024 * 1024:
            raise EOFError
        return bytes(take(length)).decode('utf-8', 'replace')

    def value(kind, keep):
        if kind in _SCALARS:
            return number(_SCALARS[kind])
        if kind == 8:
            return text()
        if kind == 9:
            child, count = number('I'), number('Q')
            items = [value(child, keep and child in _SCALARS) for _ in range(count)]
            # Per-layer arrays (hybrid attention) only matter as their maximum.
            return max(items) if keep and items else None
        raise EOFError

    scanned = False
    try:
        if bytes(take(4)) != b'GGUF' or number('I') not in (2, 3):
            return {}
        tensors = number('Q')
        count = number('Q')
        for _ in range(min(count, 10000)):
            key = text()
            architecture = found.get('general.architecture')
            # The vocabulary is the bulk of a header; everything that tells a model's
            # shape, and whether it is a draft, is written before it.
            if key == 'tokenizer.ggml.tokens' and architecture and all(f'{architecture}.{d}' in found for d in _DIMENSIONS[:5]):
                break
            wanted = key in ('general.architecture', 'general.type') or (architecture and key in {f'{architecture}.{d}' for d in _DIMENSIONS + _STRUCTURE})
            result = value(number('I'), wanted)
            if wanted and result is not None:
                found[key] = result
        scanned = count <= 10000
    except (EOFError, ValueError, struct.error):
        pass
    if scanned and 'general.architecture' not in found and not not_a_model(found):
        # Every key was read and none names an architecture: a GGUF written for
        # another engine (diffusion models converted for stable-diffusion.cpp
        # carry tensors and no metadata). llama.cpp cannot load it.
        return {'general.type': NO_ARCHITECTURE, 'tensor_count': tensors}
    if 'general.architecture' not in found and not not_a_model(found):
        return {}
    found['tensor_count'] = tensors
    return found


async def request_prefix(url, repo, length=HEADER_PREFIX):
    """GET only the first bytes of an artifact, with the same address pinning and
    redirect rules as metadata requests. Large-file hosts redirect to a CDN."""
    config = json.loads(repo['config'])
    headers = {'User-Agent': 'AIOS/1.0', 'Range': f'bytes=0-{length - 1}'}
    token = credential(repo['id'])
    if token and urlparse(url).hostname == urlparse(repo['url']).hostname:
        headers['Authorization'] = 'Bearer ' + token
    async with httpx.AsyncClient(timeout=30, follow_redirects=False, trust_env=False, proxy=setting('system_config', {}).get('proxy') or None) as client:
        for _ in range(6):
            target, outgoing, extensions = request_target(url, headers, config.get('allow_private', False))
            async with client.stream('GET', target, headers=outgoing, extensions=extensions) as response:
                if response.is_redirect:
                    next_url = urljoin(url, response.headers['location'])
                    if urlparse(next_url).hostname != urlparse(url).hostname:
                        headers.pop('Authorization', None)
                    url = next_url
                    continue
                if response.status_code not in (200, 206):
                    raise RepositoryError(f'Artifact HTTP {response.status_code}')
                content = bytearray()
                async for chunk in response.aiter_bytes():
                    content.extend(chunk)
                    if len(content) >= length:
                        break
                return bytes(content[:length])
        raise RepositoryError('Too many artifact redirects')


def complete(header):
    architecture = header.get('general.architecture')
    return bool(not_a_model(header) or (architecture and all(f'{architecture}.{d}' in header for d in _DIMENSIONS[:5])))


async def read_header(url, repo):
    """The shape normally sits in the first few KiB, before the vocabulary; a
    long chat template can push it further. Start small and widen only when
    needed: a slow link downloads a fraction of what a fixed 2 MiB read took."""
    header = {}
    for length in (256 * 1024, HEADER_PREFIX):
        header = header_dimensions(await request_prefix(url, repo, length))
        if complete(header):
            break
    return header


def known_header(url):
    """The header recorded by an earlier sync of this exact file, if any."""
    row = one('SELECT d.metadata FROM discovered_models d JOIN model_artifacts a ON a.id=d.id WHERE a.url=?', (url,))
    header = json.loads(row['metadata']).get('gguf') if row else None
    return header if isinstance(header, dict) and 'tensor_count' in header else None


async def attach_dimensions(items, repo):
    """Read each file layout's header once: it sizes the RAM estimate and shows
    which files are drafts for another model. Drafts are dropped here, since the
    catalog must only offer what can run on its own. Every quantisation of one
    layout shares the answer; a read that fails leaves the files listed, and the
    installer checks the full header again."""
    groups = {}
    for item in items:
        layout = QUANTISATION.sub('', item['filename']).lower()
        groups.setdefault((item['model_id'], layout), []).append(item)
    reads, drafts = {}, set()
    for (model, _), group in groups.items():
        sample = min(group, key=lambda item: item['size'])
        dimensions = known_header(sample['url'])
        if dimensions is None:
            if reads.get(model, 0) >= HEADER_READS_PER_MODEL:
                continue
            reads[model] = reads.get(model, 0) + 1
            try:
                dimensions = await read_header(sample['url'], repo)
            except (RepositoryError, ValueError, OSError, httpx.HTTPError):
                continue
        if not dimensions:
            continue
        # A voice backbone is a GGUF model too, but it speaks rather than chats:
        # the voice repository offers it with its codec.
        if not_a_model(dimensions) or is_voice_architecture(dimensions.get('general.architecture')) or \
                ('tensor_count' in dimensions and draft_reason(dimensions['general.architecture'], dimensions, dimensions['tensor_count'])):
            drafts.update(id(item) for item in group)
            continue
        for item in group:
            item['gguf'] = dimensions
            if item.get('architecture') in (None, '', 'unknown'):
                item['architecture'] = dimensions['general.architecture']
    items[:] = [item for item in items if id(item) not in drafts]


# "Qwen3-30B-A3B", "Mixtral-8x7B", "SmolLM2-360M", "gemma-3n-E4B": publishers
# put the size in the name far more often than in the metadata.
_PARAMS = re.compile(r'(?<![A-Za-z0-9.])(?:(\d+)x)?E?(\d+(?:\.\d+)?)([BM])(?:-A(\d+(?:\.\d+)?)B)?(?![A-Za-z0-9])', re.I)


def parameters_from_name(*names):
    """Total and active parameters read from a model or file name, or (None, None)."""
    for name in names:
        for match in _PARAMS.finditer(str(name or '').replace('_', '-')):
            experts, value, unit, active = match.groups()
            count = float(value) * (1e9 if unit.upper() == 'B' else 1e6)
            if unit.upper() == 'M' and count < 1e8:
                continue  # "1M" is a context length, not a model size
            if count > 5e12:
                continue
            count *= int(experts or 1)
            return int(count), int(float(active) * 1e9) if active else None
    return None, None


def artifact(model_id, filename, url, size, sha=None, revision='', **extra):
    if not isinstance(model_id, str) or not 1 <= len(model_id) <= 256:
        raise RepositoryError('Invalid model ID')
    if not isinstance(url, str) or len(url) > 8192 or not isinstance(revision, str) or len(revision) > 512:
        raise RepositoryError('Invalid artifact URL or revision')
    for key in ('license', 'author', 'family', 'architecture', 'release_date'):
        value = extra.get(key)
        if value is not None and (not isinstance(value, str) or len(value) > 1024):
            raise RepositoryError('Invalid model metadata field: ' + key)
    for key in ('parameter_count', 'context'):
        value = extra.get(key)
        if value is not None and (not isinstance(value, int) or isinstance(value, bool) or value < 0 or value > 10 ** 15):
            raise RepositoryError('Invalid numeric model metadata: ' + key)
    if not isinstance(filename, str) or not filename.lower().endswith('.gguf'):
        return None
    if not servable(filename, extra):
        return None
    if '..' in filename.split('/') or '\\' in filename or len(filename) > 512:
        raise RepositoryError('Unsafe artifact name')
    if not isinstance(size, int) or size <= 0 or size > 2 * 1024 ** 4:
        return None
    if sha and (not isinstance(sha, str) or not re.fullmatch('[0-9a-fA-F]{64}', sha)):
        raise RepositoryError('Invalid SHA256')
    quant = re.search(r'(?:IQ|TQ|MXFP|BF|FP|Q|F)[0-9][A-Za-z0-9_]*', filename, re.I)
    if extra.get('parameter_count') is None:
        total, active = parameters_from_name(model_id.split('/')[-1], filename.rsplit('/', 1)[-1])
        if total:
            extra = {**extra, 'parameter_count': total, 'parameter_source': 'name', **({'active_parameters': active} if active else {})}
    return {'model_id': model_id, 'display_name': model_id + ' / ' + filename.rsplit('/', 1)[-1], 'filename': filename, 'url': url, 'size': size, 'sha256': sha, 'revision': revision, 'format': 'GGUF', 'quantization': quant.group(0) if quant else 'unknown', 'author': model_id.split('/')[0], 'architecture': 'unknown', 'family': model_id, 'parameter_count': None, 'license': 'unknown', **extra}

# llama.cpp names vision projectors mmproj-*.gguf and shards *-00001-of-0000N.gguf.
_ARCHITECTURES: frozenset | None = None
COMPANION = re.compile(r'(?:^|[-_.])mmproj(?:[-_.]|$)', re.I)
SHARD = re.compile(r'-\d{5}-of-\d{5}\.gguf$', re.I)


def supported_architectures():
    """Names the bundled llama.cpp can load, recorded from its own sources at
    build time. Absent in a source checkout, where nothing is filtered."""
    global _ARCHITECTURES
    if _ARCHITECTURES is None:
        try:
            _ARCHITECTURES = frozenset(json.loads((ETC / 'supported-architectures.json').read_text()))
        except (OSError, ValueError):
            _ARCHITECTURES = frozenset()
    return _ARCHITECTURES


def servable(filename, extra):
    """Only list what this appliance can actually run on its own. A projector has
    no language weights, and a shard cannot be loaded without its siblings, which
    the single-file installer never brings along."""
    base = filename.rsplit('/', 1)[-1]
    if COMPANION.search(base) or SHARD.search(base):
        return False
    architecture = str(extra.get('architecture') or '').lower()
    if architecture == 'clip':
        return False
    # Only judge what the repository actually declared; 'unknown' stays listed
    # because the header is the authority and it is only read after download.
    known = supported_architectures()
    return not (known and architecture and architecture != 'unknown' and architecture not in known)


PROJECTOR_LIMIT = 64 * 1024 ** 3
# Precisions llama.cpp's projectors are published in, best first. F16 is what
# upstream converts to and every backend reads. Q8_0 comes next: it is supported
# everywhere and roughly half the size, while BF16 needs CPU or driver support
# that older hardware and some Vulkan drivers lack. F32 only doubles the memory.
_PRECISIONS = ('f16', 'q8_0', 'bf16', 'f32')
_PRECISION = re.compile(r'(?<![a-z0-9])(bf16|f16|f32|q8_0)(?![a-z0-9])', re.I)


def projector_candidate(filename, url, size, sha=None):
    """A multimodal projector file (mmproj) in a model repository, or None.
    Vision models read images only with it, so it travels with the model."""
    base = filename.rsplit('/', 1)[-1] if isinstance(filename, str) else ''
    if not base.lower().endswith('.gguf') or not COMPANION.search(base) or SHARD.search(base):
        return None
    if '..' in filename.split('/') or '\\' in filename or len(filename) > 512 or not isinstance(url, str) or len(url) > 8192:
        return None
    if not isinstance(size, int) or isinstance(size, bool) or not 0 < size <= PROJECTOR_LIMIT:
        return None
    if not (isinstance(sha, str) and re.fullmatch('[0-9a-fA-F]{64}', sha)):
        sha = None
    return {'filename': filename, 'url': url, 'size': size, 'sha256': sha}


def _name_tokens(name):
    base = name.rsplit('/', 1)[-1].lower().removesuffix('.gguf')
    return {token for token in re.split(r'[-_.]+', QUANTISATION.sub('', base)) if token and token != 'mmproj'}


def attach_projectors(items, candidates):
    """Give every model file of a repository the projector that belongs to it.
    Most repositories publish one projector in several precisions; a few publish
    one per model size, named after it, so a projector whose name carries words
    the model file does not is someone else's."""
    if not candidates:
        return
    for item in items:
        model_tokens = _name_tokens(item['filename'])

        def rank(candidate):
            own = _name_tokens(_PRECISION.sub('', candidate['filename']))
            if own - model_tokens:
                return None
            precision = _PRECISION.search(candidate['filename'].rsplit('/', 1)[-1])
            order = _PRECISIONS.index(precision.group(1).lower()) if precision else len(_PRECISIONS)
            return (-len(own), order, candidate['size'])
        ranked = [(rank(c), c) for c in candidates]
        ranked = [(r, c) for r, c in ranked if r is not None]
        if ranked:
            item['projector'] = min(ranked, key=lambda pair: pair[0])[1]


def projector_for(model_id):
    """The projector that belongs to a discovered model. A model installed before
    projectors were tracked keeps its old metadata row, while a later sync records
    the same file, now with its projector, under the newer revision."""
    row = one('SELECT repository_id,upstream_key,metadata FROM discovered_models WHERE id=?', (model_id,))
    if not row:
        return None
    projector = json.loads(row['metadata']).get('projector')
    if isinstance(projector, dict):
        return projector
    for newer in rows('SELECT metadata FROM discovered_models WHERE repository_id=? AND upstream_key=? AND id!=? ORDER BY last_checked DESC', (row['repository_id'], row['upstream_key'], model_id)):
        projector = json.loads(newer['metadata']).get('projector')
        if isinstance(projector, dict):
            return projector
    return None



async def hugging_face_files(repo, model_repo):
    """File sizes, checksums and the pinned revision of one Hugging Face repository."""
    data = await request_json(repo['url'].rstrip('/') + '/api/models/' + quote(model_repo, safe='/') + '?blobs=true', repo)
    revision = data.get('sha', 'main')
    files = {}
    for item in data.get('siblings', []):
        lfs = item.get('lfs') or {}
        name = item['rfilename']
        files[name] = {'name': name, 'size': item.get('size', lfs.get('size', 0)), 'sha256': lfs.get('sha256'),
                       'url': repo['url'].rstrip('/') + '/' + quote(model_repo, safe='/') + '/resolve/' + quote(revision, safe='') + '/' + quote(name, safe='/')}
    return revision, files, (data.get('cardData') or {}).get('license', 'unknown'), data.get('createdAt') or data.get('lastModified')


# One module per provider; they reach the helpers above through this package,
# so every provider uses the same validated requests.
from .hugging_face import EXCLUDED_NAMES, huggingface, huggingface_model, latest_from_publishers, model_name, tolerant_model  # noqa: E402,F401
from .model_scope import MODELSCOPE_PUBLISHERS, MODELSCOPE_SEARCH, modelscope, modelscope_latest, modelscope_model  # noqa: E402,F401
from .github_releases import github  # noqa: E402,F401
from .manifest import generic, verify_manifest  # noqa: E402,F401
from .diffusion_models import IMAGE_EXTENSIONS, IMAGE_FAMILIES, IMAGE_MAX_BYTES, diffusion, image_artifact  # noqa: E402,F401
from .whisper_models import SPEECH_EXCLUDED, SPEECH_MAX_BYTES, SPEECH_REPO, speech, speech_artifact, speech_name  # noqa: E402,F401
from .voice_models import VOICE_EXCLUDED, VOICE_MODELS, voice, voice_artifacts  # noqa: E402,F401

PROVIDERS = {'huggingface': huggingface, 'modelscope': modelscope, 'github': github, 'http': generic, 'internal': generic, 'diffusion': diffusion, 'speech': speech, 'voice': voice}

def matches(model, filters):
    for key in ('author', 'architecture', 'quantization', 'license', 'repository_id'):
        if filters.get(key) and model.get(key) != filters[key]:
            return False
    if filters.get('keyword') and filters['keyword'].lower() not in model['display_name'].lower():
        return False
    if filters.get('max_size') and model['size'] > filters['max_size']:
        return False
    params = model.get('parameter_count')
    if filters.get('min_parameters') and (not params or params < filters['min_parameters']):
        return False
    if filters.get('max_parameters') and (not params or params > filters['max_parameters']):
        return False
    return True

def prune(repo_id, seen):
    """Forget what the repository no longer offers, so a superseded release or a
    list the repository was reconfigured away from stops filling the catalog.
    Anything installed or being downloaded keeps its row: those reference it."""
    # A download that failed or was cancelled does not keep a file the
    # repository stopped offering, or no longer offers as runnable: it goes too.
    for row in rows('SELECT d.id,d.upstream_key,d.revision FROM discovered_models d WHERE d.repository_id=? '
                    'AND NOT EXISTS (SELECT 1 FROM installed_models i WHERE i.id=d.id) '
                    "AND NOT EXISTS (SELECT 1 FROM downloads w WHERE w.model_id=d.id AND w.state NOT IN ('FAILED','CANCELLED'))", (repo_id,)):
        if (row['upstream_key'], row['revision']) not in seen:
            execute('DELETE FROM downloads WHERE model_id=?', (row['id'],))
            execute('DELETE FROM model_artifacts WHERE id=?', (row['id'],))
            execute('DELETE FROM discovered_models WHERE id=?', (row['id'],))


async def sync(repo_id, test=False):
    repo = one('SELECT * FROM repositories WHERE id=?', (repo_id,))
    if not repo:
        raise RepositoryError('Repository not found')
    start = time.monotonic()
    config = json.loads(repo['config'])
    seen, counted = set(), [0]

    async def store(batch):
        batch = [item for item in batch if matches(item, config.get('filters', {}))]
        if not test:
            # Only language models carry a GGUF header worth reading; a diffusion
            # model is described by the catalogue entry that offers it.
            language = [item for item in batch if item.get('kind') not in ('image', 'speech', 'voice')]
            await attach_dimensions(language, repo)
            batch = language + [item for item in batch if item.get('kind') in ('image', 'speech', 'voice')]
        for item in batch:
            validate_url(item['url'], config.get('allow_private', False))
            if item.get('projector'):
                validate_url(item['projector']['url'], config.get('allow_private', False))
            for component in item.get('components') or []:
                validate_url(component['url'], config.get('allow_private', False))
            counted[0] += 1
            if test:
                continue
            key = item['model_id'] + '/' + item['filename']
            seen.add((key, item['revision']))
            existing = one('SELECT id FROM discovered_models WHERE repository_id=? AND upstream_key=? AND revision=?', (repo_id, key, item['revision']))
            model_id = existing['id'] if existing else uid()
            execute('INSERT INTO discovered_models VALUES (?,?,?,?,?,?,?) ON CONFLICT(repository_id,upstream_key,revision) DO UPDATE SET metadata=excluded.metadata,last_checked=excluded.last_checked', (model_id, repo_id, key, item['revision'], encode(item), now(), now()))
            execute('INSERT INTO model_artifacts(id,url,size,sha256) VALUES (?,?,?,?) ON CONFLICT(id) DO UPDATE SET url=excluded.url,size=excluded.size,sha256=excluded.sha256', (model_id, item['url'], item['size'], item['sha256']))
        if not test:
            execute('UPDATE repositories SET found=? WHERE id=?', (counted[0], repo_id))
        return batch
    try:
        if repo['provider'] == 'huggingface':
            await huggingface(repo, emit=store)
        elif repo['provider'] == 'diffusion':
            await diffusion(repo, emit=store)
        elif repo['provider'] == 'speech':
            await speech(repo, emit=store)
        elif repo['provider'] == 'voice':
            await voice(repo, emit=store)
        elif repo['provider'] == 'modelscope':
            await modelscope(repo, emit=store)
        else:
            await store(await PROVIDERS[repo['provider']](repo))
        if not test:
            prune(repo_id, seen)
            revisions.check_all()
        count = counted[0]
        execute('UPDATE repositories SET status=?,last_sync=?,duration=?,found=?,error=NULL WHERE id=?', ('ONLINE', now(), time.monotonic() - start, count, repo_id))
        alerts.resolve('repository:' + repo_id)
        audit('repository-worker', 'repository_test' if test else 'repository_sync', repo_id, {'found': count})
        return {'found': count, 'status': 'ONLINE'}
    except (ValueError, KeyError, httpx.HTTPError, OSError) as exc:
        message = str(exc) if isinstance(exc, RepositoryError) else type(exc).__name__
        # A repository nobody has configured yet is not a broken one: saying ERROR
        # made an empty GitHub or manifest entry look like a defect of the appliance.
        status = message if message in ('AUTH REQUIRED', 'RATE LIMITED') else ('NOT CONFIGURED' if message.startswith('Configure') else 'ERROR')
        # What was saved before the failure stays listed; nothing is pruned.
        execute('UPDATE repositories SET status=?,last_sync=?,duration=?,error=? WHERE id=?', (status, now(), time.monotonic() - start, message, repo_id))
        if status == 'NOT CONFIGURED' or test:
            alerts.resolve('repository:' + repo_id)
        else:
            alerts.raise_alert('repository:' + repo_id, 'WARNING', f"{repo['name']}: synchronisation failed ({message}). "
                               'Check the connection, DNS and proxy under System, then synchronise again.')
        return {'status': status, 'error': message}

async def sync_all():
    for repo in rows('SELECT id FROM repositories WHERE enabled=1'):
        await sync(repo['id'])
        await asyncio.sleep(.1)
