import json
from urllib.parse import parse_qs, urlparse

import pytest
from aios import core, providers


def hub(listings):
    """Stand-in for the Hugging Face API: per-author listings, newest first."""
    calls = []

    async def request_json(url, repo, *args, **kwargs):
        calls.append(url)
        query = parse_qs(urlparse(url).query)
        if '/api/models?' in url:
            term = query.get('search', [''])[0].lower()
            return [item for item in listings.get(query['author'][0], []) if term in item['id'].lower()]
        model = urlparse(url).path.split('/api/models/', 1)[1]
        return {'id': model, 'sha': 'rev1', 'createdAt': '2026-01-01T00:00:00Z',
                'gguf': {'architecture': 'qwen3'},
                'siblings': [{'rfilename': 'model-Q4_K_M.gguf', 'size': 1024 ** 3, 'lfs': {'sha256': 'a' * 64}}]}
    return request_json, calls


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    async def no_header(items, repo):
        return None
    monkeypatch.setattr(providers, 'attach_dimensions', no_header)
    monkeypatch.setattr(providers, 'validate_url', lambda *args, **kwargs: None)


@pytest.mark.asyncio
async def test_publishers_are_merged_newest_first(environment, monkeypatch):
    request_json, calls = hub({
        'Qwen': [{'id': 'Qwen/Qwen3.8-8B-GGUF', 'createdAt': '2026-08-01'},
                 {'id': 'Qwen/Qwen2.5-7B-Instruct-GGUF', 'createdAt': '2024-09-01'}],
        'unsloth': [{'id': 'unsloth/Qwen3.8-27B-GGUF', 'createdAt': '2026-09-01'},
                    {'id': 'unsloth/Qwen3-4B-GGUF', 'createdAt': '2025-05-01'}],
    })
    monkeypatch.setattr(providers, 'request_json', request_json)
    repo = {'url': 'https://huggingface.co'}
    ids = await providers.latest_from_publishers(repo, {'publishers': ['Qwen', 'unsloth'], 'search': ['Qwen']})
    assert ids == ['unsloth/Qwen3.8-27B-GGUF', 'Qwen/Qwen3.8-8B-GGUF', 'unsloth/Qwen3-4B-GGUF', 'Qwen/Qwen2.5-7B-Instruct-GGUF']
    query = parse_qs(urlparse(calls[0]).query)
    assert query['sort'] == ['createdAt'] and query['direction'] == ['-1'] and query['filter'] == ['gguf']


@pytest.mark.asyncio
async def test_limit_counts_distinct_runnable_models(environment, monkeypatch):
    listings = {
        'ggml-org': [{'id': 'ggml-org/Huge-GGUF', 'createdAt': '2026-09-03'},
                     {'id': 'ggml-org/Small-GGUF', 'createdAt': '2026-09-02'}],
        'unsloth': [{'id': 'unsloth/Small-GGUF', 'createdAt': '2026-09-01'},
                    {'id': 'unsloth/Medium-GGUF', 'createdAt': '2026-08-01'},
                    {'id': 'unsloth/Old-GGUF', 'createdAt': '2025-01-01'}],
    }
    request_json, _ = hub(listings)

    async def split_huge(url, repo, *args, **kwargs):
        data = await request_json(url, repo, *args, **kwargs)
        if isinstance(data, dict) and data['id'].endswith('Huge-GGUF'):
            data['siblings'] = [{'rfilename': 'huge-Q4_K_M-00001-of-00003.gguf', 'size': 50 * 1024 ** 3}]
        return data
    monkeypatch.setattr(providers, 'request_json', split_huge)
    repo = {'url': 'https://huggingface.co', 'config': json.dumps({'publishers': ['ggml-org', 'unsloth'], 'limit': 2})}
    models = {row['model_id'] for row in await providers.huggingface(repo)}
    # The split model has nothing runnable and takes no place; both copies of Small count once.
    assert models == {'ggml-org/Small-GGUF', 'unsloth/Small-GGUF', 'unsloth/Medium-GGUF'}


@pytest.mark.asyncio
async def test_modified_copies_and_non_chat_models_are_left_out(environment, monkeypatch):
    request_json, _ = hub({'unsloth': [
        {'id': 'unsloth/Qwen3.8-8B-abliterated-GGUF', 'createdAt': '2026-09-03'},
        {'id': 'unsloth/Qwen3.8-8B-Uncensored-GGUF', 'createdAt': '2026-09-02'},
        {'id': 'unsloth/Qwen3-Embedding-4B-GGUF', 'createdAt': '2026-09-01'},
        {'id': 'unsloth/Qwen3.8-8B-GGUF', 'createdAt': '2026-08-01'},
    ]})
    monkeypatch.setattr(providers, 'request_json', request_json)
    ids = await providers.latest_from_publishers({'url': 'https://huggingface.co'}, {'publishers': ['unsloth'], 'search': 'Qwen'})
    assert ids == ['unsloth/Qwen3.8-8B-GGUF']


@pytest.mark.asyncio
async def test_a_release_newer_than_the_shipped_configuration_is_found(environment, monkeypatch):
    listings = {'Qwen': [{'id': 'Qwen/Qwen3.8-8B-GGUF', 'createdAt': '2026-08-01'}]}
    request_json, _ = hub(listings)
    monkeypatch.setattr(providers, 'request_json', request_json)
    repo = core.one("SELECT * FROM repositories WHERE name='Qwen (GGUF)'")
    await providers.sync(repo['id'])
    listings['Qwen'].insert(0, {'id': 'Qwen/Qwen4-9B-GGUF', 'createdAt': '2026-09-10'})
    await providers.sync(repo['id'])
    found = {json.loads(r['metadata'])['model_id'] for r in core.rows('SELECT metadata FROM discovered_models WHERE repository_id=?', (repo['id'],))}
    assert 'Qwen/Qwen4-9B-GGUF' in found


@pytest.mark.asyncio
async def test_sync_forgets_what_the_repository_no_longer_offers(environment, monkeypatch):
    listings = {'Qwen': [{'id': 'Qwen/Old-GGUF', 'createdAt': '2025-01-01'},
                         {'id': 'Qwen/Kept-GGUF', 'createdAt': '2025-02-01'}]}
    request_json, _ = hub(listings)
    monkeypatch.setattr(providers, 'request_json', request_json)
    repo = core.one("SELECT * FROM repositories WHERE name='Qwen (GGUF)'")
    await providers.sync(repo['id'])
    kept = core.one("SELECT id FROM discovered_models WHERE upstream_key LIKE 'Qwen/Kept-GGUF/%'")['id']
    core.execute("INSERT INTO installed_models(id,path,sha256,installed_at,state,gguf) VALUES (?,?,?,?,?,?)",
                 (kept, '/m', 'a' * 64, 1, 'INSTALLED', '{}'))
    listings['Qwen'] = [{'id': 'Qwen/New-GGUF', 'createdAt': '2026-01-01'}]
    await providers.sync(repo['id'])
    keys = {r['upstream_key'].split('/model')[0] for r in core.rows('SELECT upstream_key FROM discovered_models WHERE repository_id=?', (repo['id'],))}
    assert keys == {'Qwen/New-GGUF', 'Qwen/Kept-GGUF'}


def test_retired_fixed_lists_become_discovery_queries(environment):
    name = 'Mistral (GGUF)'
    core.execute('UPDATE repositories SET config=?, enabled=1 WHERE name=?', (core.encode({'models': core._RETIRED_CURATED[name]}), name))
    core.initialize()
    row = core.one('SELECT config,enabled FROM repositories WHERE name=?', (name,))
    assert json.loads(row['config']) == core.CURATED[name] and row['enabled'] == 1


def test_an_edited_curated_list_is_left_alone(environment):
    name = 'Qwen (GGUF)'
    mine = {'models': ['Qwen/Qwen2.5-7B-Instruct-GGUF']}
    core.execute('UPDATE repositories SET config=? WHERE name=?', (core.encode(mine), name))
    core.initialize()
    assert json.loads(core.one('SELECT config FROM repositories WHERE name=?', (name,))['config']) == mine


def test_catalog_lists_newest_release_first_and_hides_disabled_repositories(admin, environment):
    on = core.one("SELECT id FROM repositories WHERE name='Qwen (GGUF)'")['id']
    off = core.one("SELECT id FROM repositories WHERE name='Mistral (GGUF)'")['id']
    core.execute('UPDATE repositories SET enabled=1 WHERE id=?', (on,))
    for key, repo, released in (('a', on, '2024-01-01'), ('b', on, '2026-09-01'), ('c', off, '2026-09-05')):
        item = {'model_id': 'org/' + key, 'display_name': 'org/' + key, 'filename': key + '.gguf', 'size': 10,
                'release_date': released, 'author': 'org', 'architecture': 'qwen3', 'quantization': 'Q4', 'license': 'x'}
        core.execute('INSERT INTO discovered_models VALUES (?,?,?,?,?,?,?)', (key, repo, key, 'r', core.encode(item), 1, 1))
    items = admin.get('/api/v1/aios/catalog').json()['items']
    assert [item['id'] for item in items] == ['b', 'a']


@pytest.mark.asyncio
async def test_one_broken_model_does_not_empty_the_catalog(environment, monkeypatch):
    request_json, _ = hub({'Qwen': [{'id': 'Qwen/Gone-GGUF', 'createdAt': '2026-09-02'},
                                    {'id': 'Qwen/Good-GGUF', 'createdAt': '2026-09-01'}]})

    async def flaky(url, repo, *args, **kwargs):
        if url.split('?')[0].endswith('/Gone-GGUF'):
            raise providers.RepositoryError('Repository HTTP 404')
        return await request_json(url, repo, *args, **kwargs)
    monkeypatch.setattr(providers, 'request_json', flaky)
    repo = core.one("SELECT * FROM repositories WHERE name='Qwen (GGUF)'")
    result = await providers.sync(repo['id'])
    assert result['status'] == 'ONLINE' and result['found'] == 1


@pytest.mark.asyncio
async def test_a_rate_limit_still_stops_the_sync_but_keeps_what_was_saved(environment, monkeypatch):
    request_json, _ = hub({'Qwen': [{'id': 'Qwen/First-GGUF', 'createdAt': '2026-09-02'},
                                    {'id': 'Qwen/Second-GGUF', 'createdAt': '2026-09-01'}]})

    async def limited(url, repo, *args, **kwargs):
        if url.split('?')[0].endswith('/Second-GGUF'):
            raise providers.RepositoryError('RATE LIMITED')
        return await request_json(url, repo, *args, **kwargs)
    monkeypatch.setattr(providers, 'request_json', limited)
    repo = core.one("SELECT * FROM repositories WHERE name='Qwen (GGUF)'")
    result = await providers.sync(repo['id'])
    assert result['status'] == 'RATE LIMITED'
    assert core.one('SELECT count(*) AS n FROM discovered_models WHERE repository_id=?', (repo['id'],))['n'] == 1


@pytest.mark.asyncio
async def test_models_appear_in_the_catalog_while_the_sync_runs(environment, monkeypatch):
    request_json, _ = hub({'Qwen': [{'id': 'Qwen/First-GGUF', 'createdAt': '2026-09-02'},
                                    {'id': 'Qwen/Second-GGUF', 'createdAt': '2026-09-01'}]})
    repo = core.one("SELECT * FROM repositories WHERE name='Qwen (GGUF)'")
    visible = []

    async def watching(url, repo_row, *args, **kwargs):
        if url.split('?')[0].endswith('/Second-GGUF'):
            visible.append(core.one('SELECT count(*) AS n FROM discovered_models WHERE repository_id=?', (repo['id'],))['n'])
        return await request_json(url, repo_row, *args, **kwargs)
    monkeypatch.setattr(providers, 'request_json', watching)
    await providers.sync(repo['id'])
    assert visible == [1]


@pytest.mark.asyncio
async def test_modelscope_finds_each_publisher_without_configuration(environment, monkeypatch):
    """ModelScope shipped inert: without a model list it refused to synchronise.
    It has no per-author endpoint, so discovery searches its index by publisher,
    with both orders — a date sort alone never surfaces established repositories."""
    from aios import providers
    catalogue = {
        ('Qwen', 'Default'): [{'Path': 'Qwen', 'Name': 'Qwen3-8B-GGUF', 'CreatedTime': 100, 'License': 'apache-2.0'},
                              {'Path': 'someone', 'Name': 'Qwen3-8B-GGUF-copy', 'CreatedTime': 900, 'License': 'other'}],
        ('Qwen', 'GmtModified'): [],
        ('unsloth', 'GmtModified'): [{'Path': 'unsloth', 'Name': 'Qwen3.8-27B-GGUF', 'CreatedTime': 900, 'License': 'apache-2.0'},
                                     {'Path': 'unsloth', 'Name': 'Something-abliterated-GGUF', 'CreatedTime': 950, 'License': 'apache-2.0'},
                                     {'Path': 'unsloth', 'Name': 'Not-a-quantisation', 'CreatedTime': 940, 'License': 'apache-2.0'}],
        ('unsloth', 'Default'): [],
    }

    async def fake(url, repo, method='GET', payload=None):
        if url.endswith(providers.MODELSCOPE_SEARCH):
            publisher = payload['Name'].split()[0]
            return {'Data': {'Model': {'Models': catalogue.get((publisher, payload['SortBy']), [])}}}
        return {'Data': {'Files': [{'Path': 'model-Q4_K_M.gguf', 'Size': 1024, 'Sha256': 'a' * 64}]}}
    monkeypatch.setattr(providers, 'request_json', fake)
    repo = {'id': 'r', 'url': 'https://modelscope.cn', 'name': 'ModelScope',
            'config': json.dumps({'publishers': ['Qwen', 'unsloth'], 'search': ['GGUF'], 'limit': 10})}
    rows = await providers.modelscope(repo)
    models = [row['model_id'] for row in rows]
    # Every configured publisher is represented, copies and excluded names are not.
    # One model per publisher in turn, in the order they are configured.
    assert models == ['Qwen/Qwen3-8B-GGUF', 'unsloth/Qwen3.8-27B-GGUF'], models
    assert rows[0]['url'].startswith('https://modelscope.cn/api/v1/models/Qwen/Qwen3-8B-GGUF/repo?Revision=master')
    assert rows[0]['license'] == 'apache-2.0'
