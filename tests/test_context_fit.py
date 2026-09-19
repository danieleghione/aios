"""A model must run with a smaller context rather than be refused, unless the
operator chose that context; and the UI must show the context really in use."""
import json

import pytest

from aios import runtime
from aios.runtime import RuntimeConfig

GIB = 1024 ** 3
META = {'size': int(1.2 * GIB), 'format': 'GGUF', 'gguf': {
    'general.architecture': 'internlm2', 'internlm2.block_count': 24, 'internlm2.embedding_length': 2048,
    'internlm2.attention.head_count': 16, 'internlm2.attention.head_count_kv': 8}}


@pytest.fixture
def instance(environment, discovered):
    key = discovered()
    rid = environment.uid()
    environment.execute('INSERT INTO installed_models(id,path,sha256,installed_at,state,gguf) VALUES (?,?,?,?,?,?)',
                        (key, '/x.gguf', 'a' * 64, environment.now(), 'INSTALLED', '{}'))
    environment.execute("INSERT INTO runtime_instances(id,model_id,state,port,config,desired) VALUES (?,?,'STOPPED',8090,'{}','RUNNING')", (rid, key))
    return rid, key


def memory(monkeypatch, free_gib, total_gib=8):
    class Mem:
        available = int(free_gib * GIB)
        total = int(total_gib * GIB)
    monkeypatch.setattr(runtime.psutil, 'virtual_memory', lambda: Mem)
    monkeypatch.setattr(runtime, 'compatibility', lambda metadata, context: {'estimated_ram': int(1.3 * GIB + context * 95_000 + 512 * 1024 ** 2)})


def test_default_context_shrinks_until_the_model_fits(environment, instance, monkeypatch):
    rid, key = instance
    memory(monkeypatch, 2.25)
    config = runtime.fit_context(rid, key, '{}', RuntimeConfig(), META)
    assert config.context < RuntimeConfig().context and config.context >= runtime.MIN_CONTEXT
    stored = json.loads(environment.one('SELECT config FROM runtime_instances WHERE id=?', (rid,))['config'])
    assert stored['context'] == config.context  # the UI reads what really runs


def test_an_explicit_context_is_honoured_and_refused_plainly(environment, instance, monkeypatch):
    rid, key = instance
    memory(monkeypatch, 2.25)
    with pytest.raises(ValueError) as error:
        runtime.fit_context(rid, key, '{"context": 4096}', RuntimeConfig(context=4096), META)
    assert 'MiB needed at context 4096' in str(error.value) and 'lower the context' in str(error.value)


def test_a_model_bigger_than_memory_is_still_refused(environment, instance, monkeypatch):
    rid, key = instance
    memory(monkeypatch, 1.0)
    with pytest.raises(ValueError, match='larger than the memory'):
        runtime.fit_context(rid, key, '{}', RuntimeConfig(), META)


def test_the_full_default_window_is_used_when_memory_allows(environment, instance, monkeypatch):
    rid, key = instance
    memory(monkeypatch, 10_000)
    assert runtime.fit_context(rid, key, '{}', RuntimeConfig(), META).context == RuntimeConfig().context


def test_a_model_is_never_asked_for_more_than_it_was_trained_on(environment, instance, monkeypatch):
    rid, key = instance
    memory(monkeypatch, 10_000)
    trained = {**META, 'gguf': {**META['gguf'], 'internlm2.context_length': 32768}}
    assert runtime.fit_context(rid, key, '{}', RuntimeConfig(), trained).context == 32768


def test_an_operator_context_beyond_the_trained_window_is_still_honoured(environment, instance, monkeypatch):
    rid, key = instance
    memory(monkeypatch, 10_000)
    trained = {**META, 'gguf': {**META['gguf'], 'internlm2.context_length': 8192}}
    chosen = RuntimeConfig(context=16384)
    assert runtime.fit_context(rid, key, '{"context": 16384}', chosen, trained).context == 16384


def test_an_automatic_context_is_not_reported_as_a_pending_restart():
    from aios.app import config_differs
    assert not config_differs('{}', RuntimeConfig(context=1024).model_dump_json())


def test_an_explicit_change_still_is():
    from aios.app import config_differs
    assert config_differs('{"context": 10000}', RuntimeConfig(context=4096).model_dump_json())
    assert config_differs('{"threads": 4}', RuntimeConfig(threads=2).model_dump_json())
    assert not config_differs('{"context": 4096}', RuntimeConfig(context=4096).model_dump_json())


def test_weights_larger_than_physical_ram_are_refused_up_front(environment, instance, monkeypatch):
    rid, key = instance
    memory(monkeypatch, free_gib=16, total_gib=2)  # plenty "available", but 1.2 GiB of weights on 2 GiB
    with pytest.raises(ValueError, match='larger than the RAM'):
        runtime.fit_context(rid, key, '{}', RuntimeConfig(), META)


def test_the_measured_model_is_accepted():
    """EXAONE 2.4B, 1568 MiB, measured on the 3 GiB lab at context 4096: loaded in
    18 s, answered, no swap, 519 MiB left. The estimate must not refuse it."""
    from aios.hardware import compatibility
    hw = {'ram': {'total': 3 * GIB, 'available': 1824 * 1024 ** 2}, 'physical_cores': 2,
          'model_storage': {'free': 50 * GIB}, 'isa': ['avx2'], 'numa_nodes': {}}
    gguf = {'general.architecture': 'exaone', 'exaone.block_count': 30, 'exaone.embedding_length': 2560,
            'exaone.attention.head_count': 32, 'exaone.attention.head_count_kv': 8, 'exaone.attention.key_length': 80}
    rating = compatibility({'size': 1568 * 1024 ** 2, 'format': 'GGUF', 'gguf': gguf}, hw=hw)
    assert rating['classification'] != 'NOT_RECOMMENDED'
    assert rating['estimated_ram'] <= 1824 * 1024 ** 2 - 128 * 1024 ** 2
