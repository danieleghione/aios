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


def test_auto_profile_shares_four_slots_over_one_kv_cache():
    from aios.runtime import RuntimeConfig, parallel_slots
    assert parallel_slots(RuntimeConfig(context=32768), 32768) == 4
    assert parallel_slots(RuntimeConfig(context=4096), 4096) == 1
    assert parallel_slots(RuntimeConfig(profile='BALANCED', context=32768), 32768) == 1
    assert parallel_slots(RuntimeConfig(parallel=2, context=32768), 32768) == 2


def test_an_engine_killed_for_memory_is_reported_plainly(tmp_path):
    import signal
    from aios.runtime import load_failure
    assert 'ran out of memory' in load_failure(tmp_path / 'missing.log', -signal.SIGKILL)


def test_the_kernel_picks_the_engine_not_the_manager():
    from pathlib import Path
    from aios.runtime import EXPENDABLE
    assert EXPENDABLE[:2] == ['/bin/sh', '-c'] and 'oom_score_adj' in EXPENDABLE[2] and 'exec "$0" "$@"' in EXPENDABLE[2]
    unit = (Path(__file__).resolve().parents[1] / 'systemd/aios-runtime-manager.service').read_text()
    assert 'OOMPolicy=continue' in unit


def test_short_memory_unloads_the_other_kind(monkeypatch):
    import asyncio
    from aios import runtime
    updates = []
    monkeypatch.setattr(runtime, 'one', lambda *a: {'model_id': 'm-text'})
    monkeypatch.setattr(runtime, 'execute', lambda sql, args=(): updates.append((sql, args)))
    monkeypatch.setattr(runtime, 'audit', lambda *a, **k: None)

    class Process:
        returncode = None
        def poll(self): return self.returncode
        def send_signal(self, _): self.returncode = 0
        def wait(self, *_): return 0

    children = {'text': Process()}
    memory = type('M', (), {'available': 1 * 2**30})
    monkeypatch.setattr(runtime.psutil, 'virtual_memory', lambda: memory)
    monkeypatch.setattr(runtime, 'memory_needed', lambda metadata, image: 3 * 2**30)
    real_sleep = asyncio.sleep
    monkeypatch.setattr(runtime.asyncio, 'sleep', lambda *_: real_sleep(0))
    asyncio.run(runtime.make_room('image', {'display_name': 'SD'}, True, children))
    assert children == {} and any("state='STOPPED'" in sql and args == ('text',) for sql, args in updates)
    memory.available = 8 * 2**30
    kept = {'text': Process()}
    asyncio.run(runtime.make_room('image', {}, True, kept))
    assert 'text' in kept
