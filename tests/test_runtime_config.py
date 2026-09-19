"""A running model's displayed settings must describe the process, not a draft."""
import pytest


@pytest.fixture
def model(environment, discovered, admin):
    key = discovered()
    environment.execute(
        'INSERT INTO installed_models(id,path,sha256,installed_at,state,published,gguf,config) VALUES (?,?,?,?,?,?,?,?)',
        (key, str(environment.DATA / 'models' / f'{key}.gguf'), 'a' * 64, environment.now(),
         'PUBLISHED', 1, '{}', environment.encode({'context': 4096})))
    (environment.DATA / 'models').mkdir(parents=True, exist_ok=True)
    (environment.DATA / 'models' / f'{key}.gguf').write_bytes(b'x')
    return key


def running(environment, key, config):
    environment.execute(
        "INSERT INTO runtime_instances(id,model_id,state,port,config,desired) VALUES (?,?,'RUNNING',8090,?,'RUNNING')",
        (environment.uid(), key, config))


def test_start_refuses_to_pretend_a_change_was_applied(admin, environment, model):
    running(environment, model, environment.encode({'context': 4096}))
    admin.patch(f'/api/v1/aios/models/{model}', json={'config': {'context': 10000}})
    response = admin.post(f'/api/v1/aios/runtime/{model}/start')
    assert response.status_code == 409
    assert 'restart' in response.json()['error']['message'].lower()


def test_running_config_keeps_describing_the_live_process(admin, environment, model):
    running(environment, model, environment.encode({'context': 4096}))
    admin.patch(f'/api/v1/aios/models/{model}', json={'config': {'context': 10000}})
    admin.post(f'/api/v1/aios/runtime/{model}/start')
    item = admin.get('/api/v1/aios/runtime').json()['items'][0]
    # The process was launched with 4096; reporting 10000 here is what hid the bug.
    assert item['config']['context'] == 4096
    assert item['pending_restart'] is True


def test_restart_adopts_the_saved_configuration(admin, environment, model):
    running(environment, model, environment.encode({'context': 4096}))
    admin.patch(f'/api/v1/aios/models/{model}', json={'config': {'context': 10000}})
    assert admin.post(f'/api/v1/aios/runtime/{model}/restart').status_code == 200
    row = environment.one('SELECT config,desired FROM runtime_instances WHERE model_id=?', (model,))
    assert environment.json.loads(row['config'])['context'] == 10000
    assert row['desired'] == 'RESTART'


def test_no_pending_flag_when_nothing_changed(admin, environment, model):
    running(environment, model, environment.encode({'context': 4096}))
    assert admin.post(f'/api/v1/aios/runtime/{model}/start').status_code == 200
    item = admin.get('/api/v1/aios/runtime').json()['items'][0]
    assert item['pending_restart'] is False
