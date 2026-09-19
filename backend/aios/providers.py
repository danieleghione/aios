"""Metadata-only provider adapters. All URLs are validated before requests."""
import asyncio
import ipaddress
import json
import logging
import re
import socket
import time
from urllib.parse import quote, urlencode, urljoin, urlparse
import httpx
from .gguf import draft_reason, not_a_model
from .core import ETC, audit, encode, execute, now, one, rows, setting, uid

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

    try:
        if bytes(take(4)) != b'GGUF' or number('I') not in (2, 3):
            return {}
        tensors = number('Q')
        for _ in range(min(number('Q'), 10000)):
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

    except (EOFError, ValueError, struct.error):
        pass
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
        if not_a_model(dimensions) or ('tensor_count' in dimensions and draft_reason(dimensions['general.architecture'], dimensions, dimensions['tensor_count'])):
            drafts.update(id(item) for item in group)
            continue
        for item in group:
            item['gguf'] = dimensions
            if item.get('architecture') in (None, '', 'unknown'):
                item['architecture'] = dimensions['general.architecture']
    items[:] = [item for item in items if id(item) not in drafts]


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


# Repositories that are a quantised copy of someone else's work with its safety
# tuning removed, or that are not chat models at all.
EXCLUDED_NAMES = ('uncensored', 'abliterated', 'heretic', 'nsfw', 'lewd', 'erotic', 'roleplay',
                  'dolphin', '-tts', '-asr', 'ocr-', '-ocr', 'embedding', 'reranker', '-base-gguf')


async def latest_from_publishers(repo, config):
    """GGUF repositories from named publishers, optionally narrowed by name, newest
    first. A fixed model list never sees a release that postdates it, and a plain
    name search across all of Hugging Face surfaces mostly anonymous re-uploads
    and modified copies; publishers plus a family keyword stays current and vetted."""
    base = repo['url'].rstrip('/') + '/api/models?'
    terms = config.get('search') or ['']
    terms = [terms] if isinstance(terms, str) else terms
    excluded = [e.lower() for e in config.get('exclude', EXCLUDED_NAMES)]
    found = {}
    for author in list(config['publishers'])[:12]:
        for term in terms[:8]:
            query = {'author': author, 'filter': 'gguf', 'sort': 'createdAt', 'direction': -1, 'limit': 40}
            if term:
                query['search'] = term
            for item in await request_json(base + urlencode(query), repo):
                name = item['id'].lower()
                if not any(word in name for word in excluded):
                    found.setdefault(item['id'], item.get('createdAt', ''))
    return sorted(found, key=found.get, reverse=True)


def model_name(repository):
    """The same model re-quantised by several publishers is one choice, not several."""
    return repository.rsplit('/', 1)[-1].lower()


async def tolerant_model(repo, model):
    """One model's files, or none if that model alone cannot be read. Upstream
    repositories change every day: a single deleted, gated or malformed entry
    used to fail the whole sync and leave the catalog empty. Access and rate
    limits concern every model, so a rate limit still stops the sync; a gated
    model answers AUTH REQUIRED on its own while the others stay public."""
    try:
        return await huggingface_model(repo, model)
    except RepositoryError as exc:
        if str(exc) == 'RATE LIMITED':
            raise
        logging.warning('repository %s: skipping %s: %s', repo.get('name'), model, exc)
    except (ValueError, KeyError, TypeError, AttributeError, httpx.HTTPStatusError) as exc:
        logging.warning('repository %s: skipping %s: %s', repo.get('name'), model, type(exc).__name__)
    return []


async def huggingface_model(repo, model):
    data = await request_json(repo['url'].rstrip('/') + '/api/models/' + quote(model, safe='/') + '?blobs=true', repo)
    model = data.get('id', model)
    revision = data.get('sha', 'main')
    result, projectors = [], []
    for item in data.get('siblings', []):
        lfs = item.get('lfs') or {}
        name = item['rfilename']
        url = repo['url'].rstrip('/') + '/' + quote(model, safe='/') + '/resolve/' + quote(revision, safe='') + '/' + quote(name, safe='/')
        projector = projector_candidate(name, url, item.get('size', lfs.get('size', 0)), lfs.get('sha256'))
        if projector:
            projectors.append(projector)
            continue
        row = artifact(model, name, url, item.get('size', lfs.get('size', 0)), lfs.get('sha256'), revision, license=(data.get('cardData') or {}).get('license', 'unknown'), architecture=(data.get('gguf') or {}).get('architecture', 'unknown'), parameter_count=(data.get('gguf') or {}).get('total'), context=(data.get('gguf') or {}).get('context_length'), release_date=data.get('createdAt') or data.get('lastModified'), upstream_metadata={'tags': data.get('tags', []), 'gguf': data.get('gguf', {})})
        if row:
            result.append(row)
    attach_projectors(result, projectors)
    return result


async def huggingface(repo, emit=None):
    """Artifacts of the configured Hugging Face models. With emit, each model's
    files are handed over as soon as they are known, so a long sync fills the
    catalog as it goes instead of showing nothing until the very end."""
    config = json.loads(repo['config'])
    ids = config.get('models', [])
    if not ids and config.get('publishers'):
        # 'limit' counts distinct models this appliance can run: every publisher's
        # copy of a kept model stays (they differ in quantisations), while a model
        # with nothing runnable, such as one only published split into parts,
        # does not use up a place.
        wanted = max(1, min(100, int(config.get('limit', 30))))
        kept, result = set(), []
        for model in (await latest_from_publishers(repo, config))[:150]:
            name = model_name(model)
            if name not in kept and len(kept) >= wanted:
                continue
            rows_found = await tolerant_model(repo, model)
            if emit and rows_found:
                rows_found = await emit(rows_found)
            if rows_found:
                kept.add(name)
                result.extend(rows_found)
        return result
    if not ids:
        query = 'filter=gguf&sort=lastModified&direction=-1&limit=30'
        if config.get('author'):
            query += '&author=' + quote(config['author'], safe='')
        listing = await request_json(repo['url'].rstrip('/') + '/api/models?' + query, repo)
        ids = [item['id'] for item in listing]
    result = []
    for model in ids[:100]:
        rows_found = await tolerant_model(repo, model)
        if emit and rows_found:
            rows_found = await emit(rows_found)
        result.extend(rows_found)
    return result

# ModelScope has no per-author listing endpoint, but its search matches the
# publisher's name as well as the model's, so asking for "<publisher> GGUF"
# returns that publisher's releases, newest first. Checked September 2026.
MODELSCOPE_SEARCH = '/api/v1/dolphin/models'
MODELSCOPE_PUBLISHERS = ['Qwen', 'unsloth', 'lmstudio-community', 'ggml-org']


async def modelscope_latest(repo, config):
    """Newest GGUF repositories of the configured publishers, with their licence."""
    publishers = config.get('publishers') or MODELSCOPE_PUBLISHERS
    terms = config.get('search') or ['GGUF']
    terms = [terms] if isinstance(terms, str) else terms
    excluded = [word.lower() for word in config.get('exclude', EXCLUDED_NAMES)]
    found = {}
    for publisher in list(publishers)[:12]:
        for term in terms[:8]:
            # Newest first finds this week's releases; relevance finds the
            # established repositories that a date sort buries under the noise.
            for order in ('GmtModified', 'Default'):
                payload = {'PageSize': 40, 'PageNumber': 1, 'SortBy': order, 'Name': f'{publisher} {term}'.strip()}
                data = await request_json(repo['url'].rstrip('/') + MODELSCOPE_SEARCH, repo, method='PUT', payload=payload)
                for item in (((data.get('Data') or {}).get('Model') or {}).get('Models') or []):
                    path, name = str(item.get('Path') or ''), str(item.get('Name') or '')
                    if path.lower() != str(publisher).lower() or 'gguf' not in name.lower():
                        continue
                    if any(word in name.lower() for word in excluded):
                        continue
                    found[f'{path}/{name}'] = (item.get('CreatedTime') or 0, item.get('License') or 'unknown')
    # Newest first within each publisher, then one publisher after another, so a
    # limit never spends every place on whoever published most this week.
    by_publisher = {}
    for key in sorted(found, key=lambda key: found[key][0], reverse=True):
        by_publisher.setdefault(key.split('/')[0].lower(), []).append(key)
    ordered, queues = [], [by_publisher[p.lower()] for p in publishers if p.lower() in by_publisher]
    while queues:
        for queue in list(queues):
            ordered.append(queue.pop(0))
            if not queue:
                queues.remove(queue)
    return ordered, {key: found[key][1] for key in found}


async def modelscope_model(repo, model, revision, license_name):
    """One ModelScope repository's files, with the projector that belongs to them."""
    base = repo['url'].rstrip('/') + '/api/v1/models/' + quote(model, safe='/')
    data = await request_json(base + '/repo/files?Recursive=true&Revision=' + quote(revision, safe=''), repo)
    found, projectors = [], []
    for item in data.get('Data', {}).get('Files', []):
        name = item.get('Path', '')
        url = base + '/repo?Revision=' + quote(revision, safe='') + '&FilePath=' + quote(name, safe='')
        projector = projector_candidate(name, url, item.get('Size', 0), item.get('Sha256'))
        if projector:
            projectors.append(projector)
            continue
        row = artifact(model, name, url, item.get('Size', 0), item.get('Sha256'), item.get('Revision', revision), license=license_name)
        if row:
            found.append(row)
    attach_projectors(found, projectors)
    return found


async def modelscope(repo, emit=None):
    """ModelScope models. Without a configured list, the newest GGUF releases of
    the same trusted publishers the Hugging Face repositories follow."""
    config = json.loads(repo['config'])
    revision = config.get('revision', 'master')
    licences = {}
    models = config.get('models')
    if not models:
        wanted = max(1, min(100, int(config.get('limit', 20))))
        models, licences = await modelscope_latest(repo, config)
        models = models[:wanted]
    result = []
    for model in list(models)[:100]:
        try:
            found = await modelscope_model(repo, model, revision, licences.get(model, config.get('license', 'unknown')))
        except RepositoryError as exc:
            if str(exc) == 'RATE LIMITED':
                raise
            logging.warning('repository %s: skipping %s: %s', repo.get('name'), model, exc)
            continue
        if emit and found:
            found = await emit(found)
        result.extend(found)
    return result


async def github(repo):
    config = json.loads(repo['config'])
    result = []
    if not config.get('models'):
        raise RepositoryError('Configure organization/repository entries in models; GitHub releases are listed per repository')
    for model in config['models'][:100]:
        data = await request_json(repo['url'].rstrip('/') + '/repos/' + quote(model, safe='/') + '/releases?per_page=20', repo)
        for release in data:
            found, projectors = [], []
            for item in release.get('assets', []):
                digest = item.get('digest') or ''
                sha = digest.removeprefix('sha256:') if digest.startswith('sha256:') else None
                projector = projector_candidate(item['name'], item['browser_download_url'], item['size'], sha)
                if projector:
                    projectors.append(projector)
                    continue
                row = artifact(model, item['name'], item['browser_download_url'], item['size'], sha, release['tag_name'], license=config.get('license', 'unknown'), release_date=release.get('published_at'))
                if row:
                    found.append(row)
            attach_projectors(found, projectors)
            result.extend(found)
    return result

async def generic(repo):
    if not (repo['url'] or '').strip():
        raise RepositoryError('Configure the HTTPS URL of an AIOS manifest for this repository')
    data = await request_json(repo['url'], repo)
    if data.get('schema_version') != 1 or not isinstance(data.get('models'), list) or len(data['models']) > 10000:
        raise RepositoryError('Invalid AIOS manifest')
    result, projectors = [], {}
    for item in data['models']:
        url = urljoin(repo['url'], item['url'])
        projector = projector_candidate(item['filename'], url, item['size'], item.get('sha256'))
        if projector:
            projectors.setdefault(item['model_id'], []).append(projector)
            continue
        row = artifact(item['model_id'], item['filename'], url, item['size'], item.get('sha256'), item.get('revision', ''), **{k: item[k] for k in ('license', 'architecture', 'parameter_count', 'author', 'family', 'context', 'release_date') if k in item})
        if row:
            result.append(row)
    for model_id, candidates in projectors.items():
        attach_projectors([row for row in result if row['model_id'] == model_id], candidates)
    return result

# Diffusion models are not language models: they are published as one checkpoint
# or as a transformer plus its text encoders and VAE, sometimes in another
# repository. Each entry below names what to offer and what it needs to run; a
# component with several sources uses the first one that can be read without
# credentials. Checked against Hugging Face in September 2026.
IMAGE_FAMILIES = [
    {'name': 'Stable Diffusion 1.5', 'family': 'sd1', 'repo': 'Comfy-Org/stable-diffusion-v1-5-archive',
     'files': ['v1-5-pruned-emaonly-fp16.safetensors', 'v1-5-pruned-emaonly.safetensors'], 'components': []},
    {'name': 'SDXL Turbo', 'family': 'sdxl_turbo', 'repo': 'stabilityai/sdxl-turbo',
     'files': ['sd_xl_turbo_1.0_fp16.safetensors'], 'components': []},
    {'name': 'FLUX.1 schnell', 'family': 'flux', 'repo': 'city96/FLUX.1-schnell-gguf',
     'files': ['flux1-schnell-Q4_K_S.gguf', 'flux1-schnell-Q5_K_S.gguf', 'flux1-schnell-Q8_0.gguf'],
     'components': [
         {'role': 'clip_l', 'sources': [('comfyanonymous/flux_text_encoders', 'clip_l.safetensors')]},
         {'role': 't5xxl', 'sources': [('comfyanonymous/flux_text_encoders', 't5xxl_fp8_e4m3fn.safetensors')]},
         {'role': 'vae', 'sources': [('Comfy-Org/Lumina_Image_2.0_Repackaged', 'split_files/vae/ae.safetensors'),
                                     ('black-forest-labs/FLUX.1-schnell', 'ae.safetensors')]}]},
]
IMAGE_EXTENSIONS = ('.safetensors', '.gguf')
IMAGE_MAX_BYTES = 128 * 1024 ** 3


def image_artifact(entry, model_repo, revision, item, components):
    """One catalogue row for a diffusion model file, with the components it needs."""
    name = item['name']
    if '..' in name.split('/') or '\\' in name or len(name) > 512 or not name.lower().endswith(IMAGE_EXTENSIONS):
        return None
    if not isinstance(item['size'], int) or not 0 < item['size'] <= IMAGE_MAX_BYTES:
        return None
    quantisation = re.search(r'(?:IQ|Q|BF|F)[0-9][A-Za-z0-9_]*', name.rsplit('/', 1)[-1], re.I)
    return {'model_id': model_repo, 'display_name': f"{entry['name']} / {name.rsplit('/', 1)[-1]}", 'filename': name,
            'url': item['url'], 'size': item['size'], 'sha256': item.get('sha256'), 'revision': revision,
            'format': 'GGUF' if name.lower().endswith('.gguf') else 'SAFETENSORS', 'kind': 'image',
            'family': entry['family'], 'architecture': entry['family'], 'author': model_repo.split('/')[0],
            'quantization': quantisation.group(0) if quantisation else 'FP16', 'parameter_count': None,
            'license': entry.get('license', 'unknown'), 'components': components, 'release_date': entry.get('release_date')}


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


async def diffusion(repo, emit=None):
    """Image models. Metadata only: nothing is downloaded until someone installs one."""
    config = json.loads(repo['config'])
    entries = config.get('families') or IMAGE_FAMILIES
    listings, result = {}, []

    async def listing(model_repo):
        if model_repo not in listings:
            listings[model_repo] = await hugging_face_files(repo, model_repo)
        return listings[model_repo]

    for entry in entries[:40]:
        try:
            revision, files, license_name, released = await listing(entry['repo'])
        except RepositoryError as exc:
            logging.warning('image repository %s: skipping %s: %s', repo.get('name'), entry['repo'], exc)
            continue
        components = []
        missing = None
        for component in entry.get('components', []):
            resolved = None
            for source_repo, source_file in component['sources']:
                try:
                    _, source_files, _, _ = await listing(source_repo)
                except RepositoryError:
                    continue
                found = source_files.get(source_file)
                if found and found['size']:
                    resolved = {'role': component['role'], 'filename': source_file, 'url': found['url'],
                                'size': found['size'], 'sha256': found.get('sha256'), 'repository': source_repo}
                    break
            if not resolved:
                missing = component['role']
                break
            components.append(resolved)
        if missing:
            # Without every component the model cannot run, so it is not offered.
            logging.warning('image repository %s: %s needs a %s nobody published openly', repo.get('name'), entry['name'], missing)
            continue
        found_rows = []
        for filename in entry['files']:
            item = files.get(filename)
            if not item:
                continue
            row = image_artifact({**entry, 'license': license_name, 'release_date': released}, entry['repo'], revision, item, components)
            if row:
                found_rows.append(row)
        if emit and found_rows:
            found_rows = await emit(found_rows)
        result.extend(found_rows)
    return result


PROVIDERS = {'huggingface': huggingface, 'modelscope': modelscope, 'github': github, 'http': generic, 'internal': generic, 'diffusion': diffusion}

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
    Anything installed or ever downloaded keeps its row: those reference it."""
    for row in rows('SELECT d.id,d.upstream_key,d.revision FROM discovered_models d WHERE d.repository_id=? '
                    'AND NOT EXISTS (SELECT 1 FROM installed_models i WHERE i.id=d.id) '
                    'AND NOT EXISTS (SELECT 1 FROM downloads w WHERE w.model_id=d.id)', (repo_id,)):
        if (row['upstream_key'], row['revision']) not in seen:
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
            language = [item for item in batch if item.get('kind') != 'image']
            await attach_dimensions(language, repo)
            batch = language + [item for item in batch if item.get('kind') == 'image']
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
        elif repo['provider'] == 'modelscope':
            await modelscope(repo, emit=store)
        else:
            await store(await PROVIDERS[repo['provider']](repo))
        if not test:
            prune(repo_id, seen)
        count = counted[0]
        execute('UPDATE repositories SET status=?,last_sync=?,duration=?,found=?,error=NULL WHERE id=?', ('ONLINE', now(), time.monotonic() - start, count, repo_id))
        audit('repository-worker', 'repository_test' if test else 'repository_sync', repo_id, {'found': count})
        return {'found': count, 'status': 'ONLINE'}
    except (ValueError, KeyError, httpx.HTTPError, OSError) as exc:
        message = str(exc) if isinstance(exc, RepositoryError) else type(exc).__name__
        # A repository nobody has configured yet is not a broken one: saying ERROR
        # made an empty GitHub or manifest entry look like a defect of the appliance.
        status = message if message in ('AUTH REQUIRED', 'RATE LIMITED') else ('NOT CONFIGURED' if message.startswith('Configure') else 'ERROR')
        # What was saved before the failure stays listed; nothing is pruned.
        execute('UPDATE repositories SET status=?,last_sync=?,duration=?,error=? WHERE id=?', (status, now(), time.monotonic() - start, message, repo_id))
        return {'status': status, 'error': message}

async def sync_all():
    for repo in rows('SELECT id FROM repositories WHERE enabled=1'):
        await sync(repo['id'])
        await asyncio.sleep(.1)
