"""The inference key is shown only to a verified administrator, from either menu."""
import subprocess
import sys

import pytest

from aios import auth
from aios.core import execute

KEY = 'f' * 64


def run(environment, stdin, channel='ssh'):
    env = {'AIOS_DATA': str(environment.DATA), 'AIOS_ETC': str(environment.ETC), 'PYTHONPATH': 'backend', 'PATH': '/usr/bin:/bin'}
    return subprocess.run([sys.executable, '-m', 'aios', 'api-key', '--channel', channel],
                          input=stdin, capture_output=True, text=True, env=env, cwd='.')


@pytest.fixture
def keyed(environment):
    (environment.ETC / 'secrets').mkdir(parents=True, exist_ok=True)
    (environment.ETC / 'secrets/inference-key').write_text(KEY + '\n')
    return environment


def user(environment, role, password='Key-password-2026!'):
    execute('INSERT INTO users(id,username,password_hash,role) VALUES (?,?,?,?)',
            (environment.uid(), role.lower() + '@example.org', auth.HASHER.hash(password), role))


def test_valid_administrator_sees_the_key(keyed):
    user(keyed, 'SUPERADMIN')
    result = run(keyed, 'superadmin@example.org\nKey-password-2026!\n')
    assert result.returncode == 0 and KEY in result.stdout
    assert keyed.one("SELECT count(*) AS n FROM audit_events WHERE payload LIKE '%api_key_disclosed%'")['n'] == 1


def test_wrong_password_never_reveals_it(keyed):
    user(keyed, 'ADMIN')
    result = run(keyed, 'admin@example.org\nnot-the-password\n')
    assert result.returncode == 1 and KEY not in result.stdout + result.stderr


@pytest.mark.parametrize('role', ['VIEWER', 'OPERATOR'])
def test_non_administrators_are_refused(keyed, role):
    user(keyed, role)
    result = run(keyed, f'{role.lower()}@example.org\nKey-password-2026!\n')
    assert result.returncode == 1 and KEY not in result.stdout


def test_without_an_administrator_there_is_nobody_to_verify(keyed):
    result = run(keyed, 'anyone@example.org\nwhatever\n')
    assert result.returncode == 1 and KEY not in result.stdout and 'administrator' in result.stdout


def test_guessing_over_ssh_does_not_lock_the_console(keyed):
    user(keyed, 'ADMIN')
    for _ in range(3):
        run(keyed, 'admin@example.org\nwrong\n', channel='ssh')
    assert 'locked' in run(keyed, 'admin@example.org\nKey-password-2026!\n', channel='ssh').stdout
    # The console keeps its own backoff and still accepts the right password.
    result = run(keyed, 'admin@example.org\nKey-password-2026!\n', channel='console')
    assert result.returncode == 0 and KEY in result.stdout


def test_recovery_account_gets_exactly_one_more_sudo_invocation(settings_factory=None):
    import importlib.util
    from pathlib import Path
    spec = importlib.util.spec_from_file_location('cfg', Path('installer/configure.py'))
    cfg = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cfg)
    import tempfile
    root = Path(tempfile.mkdtemp())
    cfg.subprocess.run = lambda *a, **k: None
    key = 'ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIJ9k3mKQdpWJ0Fv8dYrVqTt6uLmXcE2Rb1oPzN4hSgYw lab@example'
    cfg.apply(root, dict(hostname='aios', locale='it_IT.UTF-8', keyboard='it', interface='ens18', dhcp=True, address='',
                         gateway='', dns=[], timezone='Europe/Rome', ntp=True, ntp_servers=['ntp.ubuntu.com'], manual_time='',
                         ssh=True, ssh_user='aios-recovery', ssh_key=key))
    rule = (root / 'etc/sudoers.d/aios-recovery').read_text()
    commands = rule.split('NOPASSWD:', 1)[1].strip().split(', ')
    assert commands == ['/usr/local/sbin/aios-bootstrap-recovery show', '/usr/local/sbin/aios-bootstrap-recovery reset',
                        '/usr/local/sbin/aios-bootstrap-recovery apikey']
