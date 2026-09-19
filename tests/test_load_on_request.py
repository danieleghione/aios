"""Choosing any published model in the chat must be enough to use it."""
import asyncio

import pytest
from fastapi import HTTPException

from aios import app


@pytest.fixture
def fast(monkeypatch):
    monkeypatch.setattr(app, 'SETTLE_POLL', 0.02)
    monkeypatch.setattr(app, 'LOAD_TIMEOUT', 5)
    app._BUSY.clear()
    yield
    app._BUSY.clear()


def install(environment, discovered, published=1):
    key = discovered()
    environment.execute(
        'INSERT INTO installed_models(id,path,sha256,installed_at,state,published,gguf,config) VALUES (?,?,?,?,?,?,?,?)',
        (key, str(environment.DATA / 'models' / f'{key}.gguf'), 'a' * 64, environment.now(), 'PUBLISHED', published, '{}', '{}'))
    return key


def loaded(environment, key):
    environment.execute("INSERT INTO runtime_instances(id,model_id,state,port,config,desired) VALUES (?,?,'RUNNING',8090,'{}','RUNNING')", (environment.uid(), key))


async def manager(environment, stop, fail=None):
    """Stands in for the runtime manager: acts on desired state like the real one."""
    while not stop.is_set():
        for r in environment.rows('SELECT * FROM runtime_instances'):
            if r['desired'] == 'RUNNING' and r['state'] == 'STOPPED':
                if r['model_id'] == fail:
                    environment.execute("UPDATE runtime_instances SET state='FAILED',desired='STOPPED',error=? WHERE model_id=?",
                                        ('llama-server could not load the model: unknown architecture', r['model_id']))
                else:
                    environment.execute("UPDATE runtime_instances SET state='RUNNING' WHERE model_id=?", (r['model_id'],))
            elif r['desired'] == 'STOPPED' and r['state'] == 'RUNNING':
                environment.execute("UPDATE runtime_instances SET state='STOPPED' WHERE model_id=?", (r['model_id'],))
        await asyncio.sleep(0.01)


def run_with_manager(environment, coro, fail=None):
    async def scenario():
        stop = asyncio.Event()
        task = asyncio.create_task(manager(environment, stop, fail))
        try:
            return await coro
        finally:
            stop.set()
            await task
    return asyncio.run(scenario())


def test_a_published_model_that_is_not_loaded_is_loaded_on_request(admin, environment, discovered, fast):
    key = install(environment, discovered)
    row = run_with_manager(environment, app.ensure_running(key))
    assert row['model_id'] == key and row['state'] == 'RUNNING'
    assert app._BUSY[key] == 1  # claimed until the request's slot is released


def test_an_idle_model_makes_room_for_the_requested_one(admin, environment, discovered, fast):
    idle, wanted = install(environment, discovered), install(environment, discovered)
    loaded(environment, idle)
    run_with_manager(environment, app.ensure_running(wanted))
    states = {r['model_id']: r['state'] for r in environment.rows('SELECT model_id,state FROM runtime_instances')}
    assert states == {idle: 'STOPPED', wanted: 'RUNNING'}


def test_a_model_still_answering_someone_is_never_unloaded(admin, environment, discovered, fast):
    busy, wanted = install(environment, discovered), install(environment, discovered)
    loaded(environment, busy)
    app.claim(busy)
    with pytest.raises(HTTPException) as error:
        run_with_manager(environment, app.ensure_running(wanted))
    assert error.value.status_code == 409
    assert environment.one('SELECT state FROM runtime_instances WHERE model_id=?', (busy,))['state'] == 'RUNNING'


def test_a_load_failure_reports_the_runtimes_own_reason(admin, environment, discovered, fast):
    key = install(environment, discovered)
    with pytest.raises(HTTPException) as error:
        run_with_manager(environment, app.ensure_running(key), fail=key)
    assert error.value.status_code == 503 and 'unknown architecture' in error.value.detail


def test_an_earlier_failure_is_not_mistaken_for_this_attempt(admin, environment, discovered, fast):
    key = install(environment, discovered)
    environment.execute("INSERT INTO runtime_instances(id,model_id,state,port,config,desired,error) VALUES (?,?,'FAILED',8090,'{}','STOPPED','old')", (environment.uid(), key))
    row = run_with_manager(environment, app.ensure_running(key))
    assert row['state'] == 'RUNNING'


def test_unpublished_models_are_not_served(admin, environment, discovered, fast):
    key = install(environment, discovered, published=0)
    with pytest.raises(HTTPException) as error:
        run_with_manager(environment, app.ensure_running(key))
    assert error.value.status_code == 404


def test_releasing_the_slot_releases_the_claim(admin, environment, discovered, fast):
    from aios.runtime import RuntimeConfig
    key = install(environment, discovered)
    loaded(environment, key)
    async def scenario():
        await app.ensure_running(key)
        slot = await app.acquire_slot(key, RuntimeConfig.model_construct(parallel=1, timeout=1))
        slot.release()
    asyncio.run(scenario())
    assert app._BUSY[key] == 0
