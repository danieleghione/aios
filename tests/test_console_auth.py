import pytest
from aios import auth
from aios.core import execute, one


def make_user(environment, role, password='Console-password-19!'):
    key = environment.uid()
    execute('INSERT INTO users(id,username,password_hash,role) VALUES (?,?,?,?)',
            (key, role.lower() + '@example.org', auth.HASHER.hash(password), role))
    execute("DELETE FROM login_attempts WHERE identity='console'")
    return key


def test_admin_password_authorises_a_console_action(admin, environment):
    key = make_user(environment, 'ADMIN')
    user, error = auth.console_verify('ADMIN@EXAMPLE.ORG', 'Console-password-19!')
    assert error is None and user['id'] == key


def test_wrong_password_is_refused_and_audited(admin, environment):
    make_user(environment, 'ADMIN')
    user, error = auth.console_verify('admin@example.org', 'not-the-password')
    assert user is None and error
    assert one("SELECT count(*) AS n FROM audit_events WHERE payload LIKE '%console_denied%'")['n'] == 1


@pytest.mark.parametrize('role', ['VIEWER', 'OPERATOR'])
def test_non_administrative_roles_cannot_drive_the_console(admin, environment, role):
    make_user(environment, role)
    user, error = auth.console_verify(role.lower() + '@example.org', 'Console-password-19!')
    assert user is None and error


def test_unknown_account_is_refused(admin, environment):
    execute("DELETE FROM login_attempts WHERE identity='console'")
    user, error = auth.console_verify('nobody@example.org', 'Console-password-19!')
    assert user is None and error


def test_repeated_failures_lock_the_console(admin, environment):
    make_user(environment, 'ADMIN')
    for _ in range(3):
        auth.console_verify('admin@example.org', 'wrong')
    # The correct password must not pass while the backoff is still running.
    user, error = auth.console_verify('admin@example.org', 'Console-password-19!')
    assert user is None and 'locked' in error


def test_disabled_account_cannot_authorise(admin, environment):
    key = make_user(environment, 'ADMIN')
    execute('UPDATE users SET disabled=1 WHERE id=?', (key,))
    user, error = auth.console_verify('admin@example.org', 'Console-password-19!')
    assert user is None and error


def test_curated_repositories_are_seeded_disabled_as_discovery_queries(environment):
    from aios.core import CURATED
    rows = environment.rows('SELECT name,provider,config,enabled FROM repositories')
    seeded = {r['name']: r for r in rows}
    for name, query in CURATED.items():
        assert name in seeded, name
        # Each is opted into on its own: none may come switched on with another.
        assert seeded[name]['enabled'] == 0 and seeded[name]['provider'] == 'huggingface'
        assert environment.json.loads(seeded[name]['config']) == query
