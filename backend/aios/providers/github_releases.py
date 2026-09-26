"""GitHub: the GGUF files attached to the releases of each repository."""
import json
from urllib.parse import quote

from .. import providers as shared


async def github(repo):
    config = json.loads(repo['config'])
    result = []
    if not config.get('models'):
        raise shared.RepositoryError('Configure organization/repository entries in models; GitHub releases are listed per repository')
    for model in config['models'][:100]:
        data = await shared.request_json(repo['url'].rstrip('/') + '/repos/' + quote(model, safe='/') + '/releases?per_page=20', repo)
        for release in data:
            found, projectors = [], []
            for item in release.get('assets', []):
                digest = item.get('digest') or ''
                sha = digest.removeprefix('sha256:') if digest.startswith('sha256:') else None
                projector = shared.projector_candidate(item['name'], item['browser_download_url'], item['size'], sha)
                if projector:
                    projectors.append(projector)
                    continue
                row = shared.artifact(model, item['name'], item['browser_download_url'], item['size'], sha, release['tag_name'], license=config.get('license', 'unknown'), release_date=release.get('published_at'))
                if row:
                    found.append(row)
            shared.attach_projectors(found, projectors)
            result.extend(found)
    return result
