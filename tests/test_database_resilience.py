"""Transient database trouble must not crash long-running services or lose work."""
import asyncio
import sqlite3

import pytest

from aios import core


def test_wal_files_survive_other_connections_closing(environment):
    core.anchor()
    for _ in range(50):
        core.setting('probe', 1)  # each opens and closes its own connection
    wal, shm = core.DB.with_name(core.DB.name + '-wal'), core.DB.with_name(core.DB.name + '-shm')
    # Without the held connection SQLite deletes these on every last close, and
    # the next opener has to recreate them: the race behind "unable to open".
    assert wal.exists() and shm.exists()


def test_a_momentary_open_failure_is_retried(environment, monkeypatch):
    real, calls = sqlite3.connect, {'n': 0}

    class Flaky:
        def __init__(self, inner):
            self.inner = inner

        def execute(self, *args):
            calls['n'] += 1
            if calls['n'] <= 2:
                raise sqlite3.OperationalError('unable to open database file')
            return self.inner.execute(*args)

        def __getattr__(self, name):
            return getattr(self.inner, name)

        def __setattr__(self, name, value):
            if name == 'inner':
                object.__setattr__(self, name, value)
            else:
                setattr(self.inner, name, value)

    monkeypatch.setattr(core.sqlite3, 'connect', lambda *a, **k: Flaky(real(*a, **k)))
    monkeypatch.setattr(core.time, 'sleep', lambda s: None)
    assert core.one('SELECT 1 AS n')['n'] == 1


def test_real_query_errors_are_not_retried(environment):
    with pytest.raises(sqlite3.OperationalError, match='no such table'):
        core.one('SELECT * FROM table_that_does_not_exist')


def test_describe_keeps_the_reason():
    from aios.downloads import describe
    text = describe(sqlite3.OperationalError('unable to open database file'))
    assert 'OperationalError' in text and 'unable to open database file' in text


def test_download_worker_survives_a_database_outage(environment, monkeypatch):
    from aios import downloads
    failures = {'n': 0}
    real_setting = downloads.setting

    def flaky(key, default=None):
        failures['n'] += 1
        if failures['n'] == 1:
            raise sqlite3.OperationalError('unable to open database file')
        return real_setting(key, default)

    monkeypatch.setattr(downloads, 'setting', flaky)
    real_sleep = asyncio.sleep
    monkeypatch.setattr(downloads.asyncio, 'sleep', lambda s: real_sleep(0.01))

    async def scenario():
        task = asyncio.create_task(downloads.worker())
        await real_sleep(0.2)
        alive = not task.done()
        task.cancel()
        return alive, failures['n']

    alive, calls = asyncio.run(scenario())
    assert alive, 'the worker died on a transient database error'
    assert calls > 1, 'the worker did not carry on after the error'


def test_runtime_manager_keeps_running_models_through_a_database_error(environment, monkeypatch):
    from aios import runtime
    passes = {'n': 0}
    real_rows = runtime.rows

    def flaky(sql, args=()):
        if 'FROM runtime_instances' in sql and 'SELECT *' in sql:
            passes['n'] += 1
            if passes['n'] == 2:
                raise sqlite3.OperationalError('unable to open database file')
        return real_rows(sql, args)

    monkeypatch.setattr(runtime, 'rows', flaky)
    real_sleep = asyncio.sleep
    monkeypatch.setattr(runtime.asyncio, 'sleep', lambda s: real_sleep(0.01))

    async def scenario():
        task = asyncio.create_task(runtime.manager())
        await real_sleep(0.2)
        alive = not task.done()
        task.cancel()
        return alive

    assert asyncio.run(scenario()), 'the manager exited, which would stop every model'
    assert passes['n'] > 2
