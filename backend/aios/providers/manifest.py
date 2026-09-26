"""AIOS manifests (http and internal): a JSON list of files, optionally signed."""
import base64
import binascii
import json
from urllib.parse import urljoin


from .. import providers as shared


def verify_manifest(data, config):
    """A manifest can be signed by whoever publishes it, so an appliance can take
    its catalogue from a mirror it does not have to trust. The key belongs to the
    operator: none ships in the image. Unsigned manifests keep working for a
    repository with no key configured."""
    pem = config.get('public_key')
    if not pem:
        return
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    try:
        key = serialization.load_pem_public_key(str(pem).encode())
    except (ValueError, TypeError):
        raise shared.RepositoryError('Configured public_key is not a PEM public key') from None
    if not isinstance(key, Ed25519PublicKey):
        raise shared.RepositoryError('Manifest signatures must be Ed25519')
    signature = data.get('signature')
    if not isinstance(signature, str) or not signature:
        raise shared.RepositoryError('This repository requires a signed manifest, and this one carries no signature')
    payload = {k: v for k, v in data.items() if k != 'signature'}
    try:
        key.verify(base64.b64decode(signature, validate=True), json.dumps(payload, sort_keys=True, separators=(',', ':')).encode())
    except (InvalidSignature, ValueError, binascii.Error):
        raise shared.RepositoryError('Manifest signature does not match the configured key') from None


async def generic(repo):
    if not (repo['url'] or '').strip():
        raise shared.RepositoryError('Configure the HTTPS URL of an AIOS manifest for this repository')
    data = await shared.request_json(repo['url'], repo)
    if data.get('schema_version') != 1 or not isinstance(data.get('models'), list) or len(data['models']) > 10000:
        raise shared.RepositoryError('Invalid AIOS manifest')
    verify_manifest(data, json.loads(repo['config']))
    result, projectors = [], {}
    for item in data['models']:
        url = urljoin(repo['url'], item['url'])
        projector = shared.projector_candidate(item['filename'], url, item['size'], item.get('sha256'))
        if projector:
            projectors.setdefault(item['model_id'], []).append(projector)
            continue
        row = shared.artifact(item['model_id'], item['filename'], url, item['size'], item.get('sha256'), item.get('revision', ''), **{k: item[k] for k in ('license', 'architecture', 'parameter_count', 'author', 'family', 'context', 'release_date') if k in item})
        if row:
            result.append(row)
    for model_id, candidates in projectors.items():
        shared.attach_projectors([row for row in result if row['model_id'] == model_id], candidates)
    return result
