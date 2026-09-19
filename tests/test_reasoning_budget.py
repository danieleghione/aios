import pytest
from aios.runtime import RuntimeConfig, arguments


@pytest.fixture
def installed(environment, discovered):
    key = discovered()
    environment.execute('INSERT INTO installed_models(id,path,sha256,installed_at,state,gguf) VALUES (?,?,?,?,?,?)',
                        (key, '/m.gguf', 'a' * 64, environment.now(), 'INSTALLED', '{}'))
    return {'model_id': key, 'port': 8090}


def option(cmd, name):
    return cmd[cmd.index(name) + 1] if name in cmd else None


def test_thinking_leaves_room_for_the_answer_by_default(installed):
    cmd = arguments(installed, RuntimeConfig(context=4096))
    assert option(cmd, '--reasoning-budget') == '2048'
    assert 'final answer' in option(cmd, '--reasoning-budget-message')


def test_budget_follows_the_context_each_request_gets(installed):
    cmd = arguments(installed, RuntimeConfig(context=8192, parallel=2))
    assert option(cmd, '--reasoning-budget') == '2048'


def test_low_memory_profile_budgets_from_the_context_it_actually_runs(installed):
    cmd = arguments(installed, RuntimeConfig(profile='LOW_MEMORY', context=8192))
    assert option(cmd, '--reasoning-budget') == '1024'


@pytest.mark.parametrize('chosen', [-1, 0, 300])
def test_an_explicit_choice_is_passed_through(installed, chosen):
    cmd = arguments(installed, RuntimeConfig(context=4096, reasoning_budget=chosen))
    assert option(cmd, '--reasoning-budget') == str(chosen)
    assert ('--reasoning-budget-message' in cmd) == (chosen > 0)


def test_budget_outside_the_accepted_range_is_rejected():
    with pytest.raises(ValueError):
        RuntimeConfig(reasoning_budget=-3)
