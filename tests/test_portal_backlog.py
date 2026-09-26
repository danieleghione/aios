"""Portal defects found by the September 2026 audit (internal/BACKLOG.md)."""
import json
import re
import time
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / 'frontend-admin/src'


class _Portal:
    """Every source file of the portal, read as one text."""
    def read_text(self):
        return '\n'.join(p.read_text() for p in sorted(SRC.rglob('*.ts*')) if not p.name.endswith('.test.tsx'))


APP = _Portal()
PASSWORD = 'Testing-password-937!'


def add_user(environment, username, role='VIEWER'):
    from aios.auth import HASHER
    key = environment.uid()
    environment.execute('INSERT INTO users(id,username,password_hash,role) VALUES (?,?,?,?)', (key, username, HASHER.hash('Other-password-123'), role))
    return key


def test_portal_has_no_italian_left():
    source = APP.read_text()
    for word in ('salvato', 'assente', 'elementi', 'Ultimo sync', "'mai'", 'Durata', "'Nome'", 'Disabilita', 'Precedenti',
                 'Successivi', 'Dettaglio', 'Ripristina', 'Confermi', 'Servizio', 'Nessun messaggio'):
        assert word not in source, word


def test_username_pattern_is_valid_for_unicode_sets():
    # Browsers compile pattern with the v flag: an unescaped "-" at the end of a
    # class is a syntax error and the whole check is silently skipped.
    for pattern in re.findall(r'pattern="([^"]+)"', APP.read_text()):
        assert not re.search(r'[^\\]-\]', pattern), pattern


def test_not_configured_repository_is_not_a_failed_sync():
    assert "'NOT CONFIGURED'].includes(r.status)" in APP.read_text()


def test_disabled_repository_reports_disabled(admin, environment):
    key = environment.uid()
    environment.execute("INSERT INTO repositories(id,name,provider,url,config,enabled,status) VALUES (?,?,?,?,?,0,'ONLINE')",
                        (key, 'Old', 'http', 'https://example.com/index.json', '{}'))
    items = {r['id']: r for r in admin.get('/api/v1/aios/repositories').json()['items']}
    assert items[key]['status'] == 'DISABLED'


def test_catalogue_period_uses_release_date(admin, environment, discovered):
    old, new = discovered(url='https://example.com/old.gguf'), discovered(url='https://example.com/new.gguf')
    for key, released in ((old, '2024-01-01T00:00:00Z'), (new, time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()))):
        metadata = json.loads(environment.one('SELECT metadata FROM discovered_models WHERE id=?', (key,))['metadata'])
        environment.execute('UPDATE discovered_models SET metadata=? WHERE id=?', (environment.encode({**metadata, 'release_date': released}), key))
    found = admin.get('/api/v1/aios/catalog', params={'since': time.time() - 7 * 86400, 'limit': 50}).json()
    ids = {item['id'] for item in found['items']}
    assert new in ids and old not in ids


def test_released_at_falls_back_to_discovery():
    from aios.app import released_at
    assert released_at({}, 5.0) == 5.0
    assert released_at({'release_date': 'not a date'}, 5.0) == 5.0
    assert released_at({'release_date': '2025-01-01T00:00:00Z'}, 5.0) == 1735689600.0
    assert released_at({'release_date': 1735689600}, 5.0) == 1735689600.0


def test_superadmin_resets_another_password(admin, environment):
    key = add_user(environment, 'someone')
    response = admin.post(f'/api/v1/aios/users/{key}/password', json={'password': 'Temporary-pass-2026'})
    assert response.status_code == 200, response.text
    row = environment.one('SELECT must_change FROM users WHERE id=?', (key,))
    assert row['must_change'] == 1
    login = admin.post('/api/v1/aios/auth/login', json={'username': 'someone', 'password': 'Temporary-pass-2026'})
    assert login.status_code == 200 and login.json().get('csrf')


def test_password_reset_rules(admin, environment):
    me = admin.get('/api/v1/aios/auth/me').json()['id']
    assert admin.post(f'/api/v1/aios/users/{me}/password', json={'password': 'Temporary-pass-2026'}).status_code in (400, 409)
    other = add_user(environment, 'short')
    assert admin.post(f'/api/v1/aios/users/{other}/password', json={'password': 'short'}).status_code == 422


def test_delete_user(admin, environment):
    key = add_user(environment, 'leaving')
    assert admin.delete(f'/api/v1/aios/users/{key}').status_code == 200
    assert environment.one('SELECT id FROM users WHERE id=?', (key,)) is None
    me = admin.get('/api/v1/aios/auth/me').json()['id']
    assert admin.delete(f'/api/v1/aios/users/{me}').status_code in (400, 409)


def test_admin_cannot_reset_or_delete(admin, environment):
    from aios.auth import HASHER
    add_user(environment, 'target')
    key = environment.uid()
    environment.execute('INSERT INTO users(id,username,password_hash,role) VALUES (?,?,?,?)', (key, 'plainadmin', HASHER.hash('Plain-admin-pass-1'), 'ADMIN'))
    response = admin.post('/api/v1/aios/auth/login', json={'username': 'plainadmin', 'password': 'Plain-admin-pass-1'})
    admin.headers['x-csrf-token'] = response.json()['csrf']
    target = environment.one("SELECT id FROM users WHERE username='target'")['id']
    assert admin.delete(f'/api/v1/aios/users/{target}').status_code == 403
    assert admin.post(f'/api/v1/aios/users/{target}/password', json={'password': 'Temporary-pass-2026'}).status_code == 403


def test_system_settings_are_the_live_ones(admin, monkeypatch):
    from aios import system_state
    monkeypatch.setattr(system_state, 'timezone_name', lambda: 'Europe/Rome')
    system = admin.get('/api/v1/aios/system/settings').json()['system']
    assert system['timezone'] == 'Europe/Rome'
    assert 'hostname' in system and isinstance(system['ntp'], list)


def test_timezone_list(admin):
    zones = admin.get('/api/v1/aios/system/timezones').json()['items']
    assert 'UTC' in zones


def test_network_state_and_pending_change(admin, environment):
    body = admin.get('/api/v1/aios/system/network').json()
    assert {'interface', 'addresses', 'gateway', 'dns', 'dhcp', 'interfaces'} <= set(body['current'])
    assert body['pending'] is None
    from aios import system_state
    now = time.time()
    jobs = [{'id': 'b', 'action': 'network', 'state': 'COMPLETED', 'created_at': now - 10}]
    assert system_state.pending_network(jobs)['id'] == 'b'
    assert system_state.pending_network([{'id': 'c', 'action': 'network-confirm', 'state': 'QUEUED', 'created_at': now}, *jobs]) is None
    assert system_state.pending_network([{**jobs[0], 'created_at': now - 500}]) is None


def test_os_updates_status_and_jobs(admin, environment):
    status = admin.get('/api/v1/aios/system/updates').json()
    assert status['checked_at'] is None and status['packages'] == 0
    (environment.DATA / 'system').mkdir(parents=True, exist_ok=True)
    (environment.DATA / 'system/updates.json').write_text('{"checked_at": 1, "packages": 4, "security": 2, "reboot_required": false, "error": ""}')
    assert admin.get('/api/v1/aios/system/updates').json()['security'] == 2
    job = admin.post('/api/v1/aios/system/updates/check').json()
    row = environment.one('SELECT action,payload FROM system_jobs WHERE id=?', (job['id'],))
    assert row['action'] == 'os-update' and json.loads(row['payload'])['mode'] == 'check'
    assert admin.post('/api/v1/aios/system/updates/everything').status_code == 422


@pytest.mark.parametrize('mode', ['check', 'apply'])
def test_platform_runs_update_script(mode):
    source = (Path(__file__).resolve().parents[1] / 'backend/aios/platform.py').read_text()
    assert "action == 'os-update'" in source and '/opt/aios/app/installer/updates.sh' in source
