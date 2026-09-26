"""A newer revision of an installed model: noticed, installed on request, and
given the settings of the one it replaces."""
import json

import pytest


def entry(environment, repo, revision, sha, discovered_at, license='MIT', size=100):
    key = environment.uid()
    metadata = {'model_id': 'test/model', 'display_name': 'Test model', 'filename': 'model.gguf', 'url': 'https://example.com/model.gguf',
                'size': size, 'format': 'GGUF', 'license': license, 'architecture': 'llama', 'author': 'test', 'quantization': 'Q4_K_M',
                'revision': revision}
    environment.execute('INSERT INTO discovered_models VALUES (?,?,?,?,?,?,?)',
                        (key, repo, 'test/model/model.gguf', revision, environment.encode(metadata), discovered_at, discovered_at))
    environment.execute('INSERT INTO model_artifacts(id,url,size,sha256) VALUES (?,?,?,?)', (key, metadata['url'], size, sha))
    return key


def install(environment, key, sha, config='{}', published=0):
    environment.execute('INSERT INTO installed_models(id,path,sha256,installed_at,state,published,gguf,config) VALUES (?,?,?,?,?,?,?,?)',
                        (key, f'/models/{key}.gguf', sha, 0, 'PUBLISHED' if published else 'INSTALLED', published, '{"metadata": {}}', config))


def repository(environment):
    key = environment.uid()
    environment.execute('INSERT INTO repositories(id,name,provider,url,config,enabled) VALUES (?,?,?,?,?,1)', (key, 'Mirror', 'http', 'https://example.com/m.json', '{}'))
    return key


def test_only_a_later_revision_with_other_weights_is_an_update(environment):
    from aios import revisions
    repo = repository(environment)
    installed = entry(environment, repo, 'r1', 'a' * 64, 100)
    install(environment, installed, 'a' * 64)
    entry(environment, repo, 'r0', 'c' * 64, 50)  # older
    entry(environment, repo, 'r2', 'a' * 64, 200)  # model card changed, same weights
    assert revisions.newer(installed) is None
    later = entry(environment, repo, 'r3', 'b' * 64, 300)
    assert revisions.newer(installed)['id'] == later and revisions.newer(installed)['revision'] == 'r3'


def test_a_sync_opens_and_closes_the_notice(environment):
    from aios import revisions
    repo = repository(environment)
    installed = entry(environment, repo, 'r1', 'a' * 64, 100)
    install(environment, installed, 'a' * 64)
    assert revisions.check_all() == 0
    later = entry(environment, repo, 'r2', 'b' * 64, 200)
    assert revisions.check_all() == 1
    alert = environment.one('SELECT * FROM alerts WHERE id=?', ('revision:' + installed,))
    assert alert['resolved'] == 0 and 'Test model' in alert['message']
    environment.execute('DELETE FROM model_artifacts WHERE id=?', (later,))
    environment.execute('DELETE FROM discovered_models WHERE id=?', (later,))
    revisions.check_all()
    assert environment.one('SELECT resolved FROM alerts WHERE id=?', ('revision:' + installed,))['resolved'] == 1


def test_updating_queues_the_newer_revision(admin, environment):
    repo = repository(environment)
    installed = entry(environment, repo, 'r1', 'a' * 64, 100)
    install(environment, installed, 'a' * 64)
    assert admin.post(f'/api/v1/aios/models/{installed}/update', json={}).status_code == 409
    later = entry(environment, repo, 'r2', 'b' * 64, 200, size=150)
    item = admin.get('/api/v1/aios/models').json()['items'][0]
    assert item['update']['id'] == later and item['update']['size'] == 150
    queued = admin.post(f'/api/v1/aios/models/{installed}/update', json={})
    assert queued.status_code == 200 and queued.json()['revision'] == 'r2'
    download = environment.one('SELECT * FROM downloads WHERE id=?', (queued.json()['id'],))
    assert download['model_id'] == later and download['replaces'] == installed and download['total'] == 150
    assert admin.post(f'/api/v1/aios/models/{installed}/update', json={}).status_code == 409


def test_a_new_licence_must_be_accepted(admin, environment):
    repo = repository(environment)
    installed = entry(environment, repo, 'r1', 'a' * 64, 100)
    install(environment, installed, 'a' * 64)
    entry(environment, repo, 'r2', 'b' * 64, 200, license='other-licence')
    refused = admin.post(f'/api/v1/aios/models/{installed}/update', json={})
    assert refused.status_code == 422 and 'other-licence' in refused.text
    assert admin.post(f'/api/v1/aios/models/{installed}/update', json={'accept_license': True}).status_code == 200


def test_the_new_revision_takes_over_the_settings(environment):
    from aios import revisions
    repo = repository(environment)
    previous = entry(environment, repo, 'r1', 'a' * 64, 100)
    install(environment, previous, 'a' * 64, config='{"context": 8192}', published=1)
    environment.execute("UPDATE installed_models SET notes='team model' WHERE id=?", (previous,))
    environment.set_setting('default_model', previous)
    current = entry(environment, repo, 'r2', 'b' * 64, 200)
    install(environment, current, 'b' * 64)
    revisions.hand_over(previous, current)
    new, old = (environment.one('SELECT * FROM installed_models WHERE id=?', (key,)) for key in (current, previous))
    assert json.loads(new['config']) == {'context': 8192} and new['notes'] == 'team model' and new['published'] == 1 and new['state'] == 'PUBLISHED'
    assert old['published'] == 0 and old['state'] == 'INSTALLED' and old['replaced_by'] == current
    assert environment.setting('default_model') == current
    assert revisions.newer(current) is None


@pytest.mark.asyncio
async def test_the_download_worker_hands_over_after_installing(admin, environment, tiny_gguf, monkeypatch):
    import hashlib
    import httpx
    from aios import downloads
    repo = repository(environment)
    previous = entry(environment, repo, 'r1', 'a' * 64, 100)
    install(environment, previous, 'a' * 64, config='{"context": 4096}', published=1)
    current = entry(environment, repo, 'r2', hashlib.sha256(tiny_gguf).hexdigest(), 200, size=len(tiny_gguf))
    job = environment.one('SELECT * FROM downloads WHERE id=?', (admin.post(f'/api/v1/aios/models/{previous}/update', json={}).json()['id'],))
    environment.execute("UPDATE downloads SET state='DOWNLOADING' WHERE id=?", (job['id'],))
    original = httpx.AsyncClient
    monkeypatch.setattr(downloads.httpx, 'AsyncClient', lambda **kwargs: original(transport=httpx.MockTransport(lambda req: httpx.Response(200, content=tiny_gguf)), **kwargs))
    monkeypatch.setattr(downloads, 'request_target', lambda url, headers, *args: (url, headers, {}))
    await downloads.download(job)
    assert environment.one('SELECT state FROM downloads WHERE id=?', (job['id'],))['state'] == 'INSTALLED'
    new = environment.one('SELECT * FROM installed_models WHERE id=?', (current,))
    assert new['published'] == 1 and json.loads(new['config']) == {'context': 4096}
    assert environment.one('SELECT replaced_by FROM installed_models WHERE id=?', (previous,))['replaced_by'] == current
