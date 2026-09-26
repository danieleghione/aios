"""ModelScope: the same publishers as Hugging Face, from its own search."""
import json
from datetime import datetime, timezone
import logging
from urllib.parse import quote

from .hugging_face import EXCLUDED_NAMES
from .. import providers as shared


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
    found, created = {}, {}
    for publisher in list(publishers)[:12]:
        for term in terms[:8]:
            # Newest first finds this week's releases; relevance finds the
            # established repositories that a date sort buries under the noise.
            for order in ('GmtModified', 'Default'):
                payload = {'PageSize': 40, 'PageNumber': 1, 'SortBy': order, 'Name': f'{publisher} {term}'.strip()}
                data = await shared.request_json(repo['url'].rstrip('/') + MODELSCOPE_SEARCH, repo, method='PUT', payload=payload)
                for item in (((data.get('Data') or {}).get('Model') or {}).get('Models') or []):
                    path, name = str(item.get('Path') or ''), str(item.get('Name') or '')
                    if path.lower() != str(publisher).lower() or 'gguf' not in name.lower():
                        continue
                    if any(word in name.lower() for word in excluded):
                        continue
                    found[f'{path}/{name}'] = (item.get('CreatedTime') or 0, item.get('License') or 'unknown')
                    created[f'{path}/{name}'] = item.get('CreatedTime') or 0
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
    return ordered, {key: (found[key][1], created.get(key)) for key in found}


async def modelscope_model(repo, model, revision, license_name, released=None):
    """One ModelScope repository's files, with the projector that belongs to them."""
    base = repo['url'].rstrip('/') + '/api/v1/models/' + quote(model, safe='/')
    data = await shared.request_json(base + '/repo/files?Recursive=true&Revision=' + quote(revision, safe=''), repo)
    found, projectors = [], []
    for item in data.get('Data', {}).get('Files', []):
        name = item.get('Path', '')
        url = base + '/repo?Revision=' + quote(revision, safe='') + '&FilePath=' + quote(name, safe='')
        projector = shared.projector_candidate(name, url, item.get('Size', 0), item.get('Sha256'))
        if projector:
            projectors.append(projector)
            continue
        row = shared.artifact(model, name, url, item.get('Size', 0), item.get('Sha256'), item.get('Revision', revision), license=license_name,
                       release_date=datetime.fromtimestamp(released, timezone.utc).isoformat() if released else None)
        if row:
            found.append(row)
    shared.attach_projectors(found, projectors)
    return found


async def modelscope(repo, emit=None):
    """ModelScope models. Without a configured list, the newest GGUF releases of
    the same trusted publishers the Hugging Face repositories follow."""
    config = json.loads(repo['config'])
    revision = config.get('revision', 'master')
    licences: dict = {}
    models = config.get('models')
    if not models:
        wanted = max(1, min(100, int(config.get('limit', 20))))
        models, licences = await modelscope_latest(repo, config)
        models = models[:wanted]
    result = []
    for model in list(models)[:100]:
        try:
            licence, released = licences.get(model, (config.get('license', 'unknown'), None))
            found = await modelscope_model(repo, model, revision, licence, released)
        except shared.RepositoryError as exc:
            if str(exc) == 'RATE LIMITED':
                raise
            logging.warning('repository %s: skipping %s: %s', repo.get('name'), model, exc)
            continue
        if emit and found:
            found = await emit(found)
        result.extend(found)
    return result
