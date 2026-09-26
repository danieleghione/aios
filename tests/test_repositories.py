import pytest



def test_a_repository_nobody_configured_is_not_reported_as_broken(environment):
    """An empty GitHub or manifest entry used to show ERROR, which reads as a
    defect of the appliance rather than something waiting for an operator."""
    import asyncio
    from aios import providers
    for provider, url in (('github', 'https://api.github.com'), ('internal', ''), ('http', '')):
        key = environment.uid()
        environment.execute('INSERT INTO repositories(id,name,provider,url,config,enabled) VALUES (?,?,?,?,?,1)',
                            (key, provider, provider, url, '{}'))
        result = asyncio.run(providers.sync(key))
        assert result['status'] == 'NOT CONFIGURED', (provider, result)
        assert 'Configure' in environment.one('SELECT error FROM repositories WHERE id=?', (key,))['error']


def signed_manifest(models, key=None):
    import base64
    import copy
    import json as _json
    models = copy.deepcopy(models)
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    key = key or Ed25519PrivateKey.generate()
    document = {'schema_version': 1, 'models': models}
    document['signature'] = base64.b64encode(key.sign(_json.dumps(document, sort_keys=True, separators=(',', ':')).encode())).decode()
    return document, key


def public_pem(key):
    from cryptography.hazmat.primitives import serialization
    return key.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo).decode()


MANIFEST_MODEL = [{'model_id': 'org/model', 'filename': 'model.gguf', 'url': 'https://example.com/model.gguf', 'size': 1000, 'license': 'MIT'}]


def run_generic(monkeypatch, repo_config, document):
    import asyncio
    from aios import providers
    monkeypatch.setattr(providers, 'request_json', lambda url, repo: _resolved(document))
    return asyncio.run(providers.generic({'id': 'r', 'url': 'https://example.com/manifest.json', 'config': __import__('json').dumps(repo_config)}))


def _resolved(value):
    import asyncio
    future = asyncio.Future()
    future.set_result(value)
    return future


def test_a_signed_catalogue_is_accepted(monkeypatch):
    document, key = signed_manifest(MANIFEST_MODEL)
    rows = run_generic(monkeypatch, {'public_key': public_pem(key)}, document)
    assert [r['model_id'] for r in rows] == ['org/model']


def test_a_tampered_catalogue_is_refused(monkeypatch):
    from aios.providers import RepositoryError
    document, key = signed_manifest(MANIFEST_MODEL)
    document['models'][0]['url'] = 'https://elsewhere.example/model.gguf'  # the copy, not the shared fixture
    with pytest.raises(RepositoryError, match='signature does not match'):
        run_generic(monkeypatch, {'public_key': public_pem(key)}, document)


def test_a_manifest_without_signature_is_refused_when_a_key_is_configured(monkeypatch):
    from aios.providers import RepositoryError
    _, key = signed_manifest(MANIFEST_MODEL)
    with pytest.raises(RepositoryError, match='carries no signature'):
        run_generic(monkeypatch, {'public_key': public_pem(key)}, {'schema_version': 1, 'models': MANIFEST_MODEL})


def test_another_key_cannot_sign_the_catalogue(monkeypatch):
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from aios.providers import RepositoryError
    document, _ = signed_manifest(MANIFEST_MODEL)
    with pytest.raises(RepositoryError, match='signature does not match'):
        run_generic(monkeypatch, {'public_key': public_pem(Ed25519PrivateKey.generate())}, document)


def test_an_unsigned_repository_keeps_working(monkeypatch):
    rows = run_generic(monkeypatch, {}, {'schema_version': 1, 'models': MANIFEST_MODEL})
    assert [r['model_id'] for r in rows] == ['org/model']


def test_the_signing_script_produces_a_manifest_the_appliance_accepts(tmp_path, monkeypatch):
    import json as _json
    import subprocess
    import sys
    from pathlib import Path
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    key = Ed25519PrivateKey.generate()
    private = tmp_path / 'catalogue.key'
    private.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    manifest = tmp_path / 'manifest.json'
    import copy
    manifest.write_text(_json.dumps({'schema_version': 1, 'models': copy.deepcopy(MANIFEST_MODEL)}))
    script = Path(__file__).resolve().parents[1] / 'scripts/sign-catalog.py'
    subprocess.run([sys.executable, str(script), str(manifest), '--private-key', str(private)], check=True, capture_output=True)
    rows = run_generic(monkeypatch, {'public_key': public_pem(key)}, _json.loads(manifest.read_text()))
    assert [r['model_id'] for r in rows] == ['org/model']
