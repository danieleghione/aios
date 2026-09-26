"""Hugging Face: the newest GGUF releases of named publishers, or a fixed list of models."""
import json
import logging
from urllib.parse import quote, urlencode
import httpx

from .. import providers as shared


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
            for item in await shared.request_json(base + urlencode(query), repo):
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
    except shared.RepositoryError as exc:
        if str(exc) == 'RATE LIMITED':
            raise
        logging.warning('repository %s: skipping %s: %s', repo.get('name'), model, exc)
    except (ValueError, KeyError, TypeError, AttributeError, httpx.HTTPStatusError) as exc:
        logging.warning('repository %s: skipping %s: %s', repo.get('name'), model, type(exc).__name__)
    return []


async def huggingface_model(repo, model):
    data = await shared.request_json(repo['url'].rstrip('/') + '/api/models/' + quote(model, safe='/') + '?blobs=true', repo)
    model = data.get('id', model)
    revision = data.get('sha', 'main')
    result, projectors = [], []
    for item in data.get('siblings', []):
        lfs = item.get('lfs') or {}
        name = item['rfilename']
        url = repo['url'].rstrip('/') + '/' + quote(model, safe='/') + '/resolve/' + quote(revision, safe='') + '/' + quote(name, safe='/')
        projector = shared.projector_candidate(name, url, item.get('size', lfs.get('size', 0)), lfs.get('sha256'))
        if projector:
            projectors.append(projector)
            continue
        row = shared.artifact(model, name, url, item.get('size', lfs.get('size', 0)), lfs.get('sha256'), revision, license=(data.get('cardData') or {}).get('license', 'unknown'), architecture=(data.get('gguf') or {}).get('architecture', 'unknown'), parameter_count=(data.get('gguf') or {}).get('total'), context=(data.get('gguf') or {}).get('context_length'), release_date=data.get('createdAt') or data.get('lastModified'), upstream_metadata={'tags': data.get('tags', []), 'gguf': data.get('gguf', {})})
        if row:
            result.append(row)
    shared.attach_projectors(result, projectors)
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
        listing = await shared.request_json(repo['url'].rstrip('/') + '/api/models?' + query, repo)
        ids = [item['id'] for item in listing]
    result = []
    for model in ids[:100]:
        rows_found = await tolerant_model(repo, model)
        if emit and rows_found:
            rows_found = await emit(rows_found)
        result.extend(rows_found)
    return result
