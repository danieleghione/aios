"""Polled endpoints must not run the full hardware profile, which spawns
llama-server and lscpu, sleeps and writes a file on every call."""
import json


def forbid_profile(monkeypatch):
    from aios import app, hardware
    def boom():
        raise AssertionError('profile() called on a hot path')
    monkeypatch.setattr(hardware, 'profile', boom)
    monkeypatch.setattr(app, 'profile', boom)


def test_catalog_does_not_profile(admin, monkeypatch, discovered):
    discovered()
    forbid_profile(monkeypatch)
    response = admin.get('/api/v1/aios/catalog?since=0')
    assert response.status_code == 200
    assert response.json()['items'][0]['compatibility']['classification']


def test_dashboard_uses_the_stored_profile(admin, environment, monkeypatch):
    stored = environment.DATA / 'system/hardware-profile.json'
    stored.parent.mkdir(parents=True, exist_ok=True)
    stored.write_text(json.dumps({'model': 'Stored CPU', 'hostname': 'lab'}))
    forbid_profile(monkeypatch)
    hardware = admin.get('/api/v1/aios/system/dashboard').json()['hardware']
    assert hardware['model'] == 'Stored CPU'
    # The values that change are still fresh rather than frozen at profile time.
    assert hardware['ram']['total'] > 0 and 'available' in hardware['ram']


def test_resources_covers_what_compatibility_reads(environment):
    from aios.hardware import compatibility, resources
    hw = resources()
    for key in ('physical_cores', 'isa', 'numa_nodes', 'ram', 'model_storage'):
        assert key in hw, key
    rating = compatibility({'size': 1024 ** 3, 'format': 'GGUF'}, hw=hw)
    assert rating['classification'] in ('OPTIMAL', 'COMPATIBLE', 'LIMITED', 'NOT_RECOMMENDED', 'INCOMPATIBLE')


def test_installed_models_carry_their_runtime_state(admin, environment, discovered):
    key = discovered()
    environment.execute('INSERT INTO installed_models(id,path,sha256,installed_at,state,gguf) VALUES (?,?,?,?,?,?)',
                        (key, '/m.gguf', 'a' * 64, environment.now(), 'INSTALLED', '{}'))
    item = admin.get('/api/v1/aios/models').json()['items'][0]
    assert item['runtime_state'] is None and item['runtime_desired'] is None
    environment.execute("INSERT INTO runtime_instances(id,model_id,state,port,config,desired) VALUES (?,?,'RUNNING',8090,'{}','RUNNING')",
                        (environment.uid(), key))
    item = admin.get('/api/v1/aios/models').json()['items'][0]
    assert item['runtime_state'] == 'RUNNING' and item['runtime_desired'] == 'RUNNING'


def catalogue_entry(environment, key, size, architecture='llama'):
    repo = environment.one('SELECT id FROM repositories LIMIT 1')['id']
    environment.execute('UPDATE repositories SET enabled=1 WHERE id=?', (repo,))
    metadata = {'model_id': 'org/' + key, 'display_name': 'org/' + key, 'filename': key + '.gguf', 'size': size,
                'author': 'org', 'architecture': architecture, 'quantization': 'Q4', 'license': 'mit', 'format': 'GGUF'}
    environment.execute('INSERT INTO discovered_models VALUES (?,?,?,?,?,?,?)',
                        (key, repo, key, 'r', environment.encode(metadata), 1, 1))


def test_the_catalogue_can_be_filtered_by_fit_and_paged(admin, environment):
    for index in range(7):
        catalogue_entry(environment, f'small{index}', 40 * 1024 ** 2)
    catalogue_entry(environment, 'huge', 900 * 1024 ** 3)
    everything = admin.get('/api/v1/aios/catalog', params={'limit': 5}).json()
    assert everything['total'] == 8 and len(everything['items']) == 5
    second = admin.get('/api/v1/aios/catalog', params={'limit': 5, 'offset': 5}).json()
    assert len(second['items']) == 3 and second['total'] == 8
    assert not {item['id'] for item in everything['items']} & {item['id'] for item in second['items']}
    refused = admin.get('/api/v1/aios/catalog', params={'fit': 'INCOMPATIBLE'}).json()
    assert [item['id'] for item in refused['items']] == ['huge']
    assert all(item['compatibility']['classification'] == 'INCOMPATIBLE' for item in refused['items'])
    good = admin.get('/api/v1/aios/catalog', params={'fit': 'OPTIMAL'}).json()
    assert 'huge' not in [item['id'] for item in good['items']]
