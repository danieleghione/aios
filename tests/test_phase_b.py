"""Encrypted and scheduled backups, idle models given back, sessions per user,
and the dashboard's trends."""
import tarfile

import pytest


def archive(folder, name='a1b2c3d4-0000-4000-8000-000000000001.tar.gz', size=9 * 1024 ** 2):
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / name
    path.write_bytes(bytes(range(256)) * (size // 256))
    return path


def test_an_encrypted_backup_opens_only_with_its_passphrase(tmp_path):
    from aios import backups
    plain = archive(tmp_path)
    sealed = tmp_path / (plain.name + '.enc')
    backups.encrypt_file(plain, sealed, 'correct horse battery')
    assert backups.is_encrypted(sealed) and not backups.is_encrypted(plain)
    assert plain.read_bytes()[:4096] not in sealed.read_bytes()
    opened = tmp_path / 'opened.tar.gz'
    backups.decrypt_file(sealed, opened, 'correct horse battery')
    assert opened.read_bytes() == plain.read_bytes()
    with pytest.raises(ValueError, match='Wrong passphrase'):
        backups.decrypt_file(sealed, tmp_path / 'x', 'another passphrase')


@pytest.mark.parametrize('damage', ['flip', 'truncate', 'drop_last', 'append'])
def test_a_damaged_backup_does_not_decrypt(tmp_path, damage):
    from aios import backups
    plain = archive(tmp_path)
    sealed = tmp_path / 'sealed.enc'
    backups.encrypt_file(plain, sealed, 'correct horse battery')
    data = bytearray(sealed.read_bytes())
    if damage == 'flip':
        data[len(data) // 2] ^= 1
    elif damage == 'truncate':
        data = data[:len(data) - 100]
    elif damage == 'drop_last':
        # Three chunks: remove the final one whole, leaving a clean boundary.
        data = data[:len(backups.MAGIC) + 24 + 2 * (4 + backups.CHUNK + 16)]
    else:
        data += b'extra'
    sealed.write_bytes(bytes(data))
    with pytest.raises(ValueError):
        backups.decrypt_file(sealed, tmp_path / 'x', 'correct horse battery')


def test_an_empty_archive_round_trips(tmp_path):
    from aios import backups
    plain = tmp_path / 'empty.tar.gz'
    plain.write_bytes(b'')
    backups.encrypt_file(plain, tmp_path / 'e.enc', 'correct horse battery')
    backups.decrypt_file(tmp_path / 'e.enc', tmp_path / 'out', 'correct horse battery')
    assert (tmp_path / 'out').read_bytes() == b''


def test_the_schedule_prunes_only_its_own_backups(tmp_path):
    from aios import backups
    made = []
    for index in range(5):
        path = archive(tmp_path, f'a1b2c3d4-0000-4000-8000-00000000000{index}.tar.gz', 1024)
        backups.record(path, scheduled=index != 0)
        made.append(path)
    uploaded = archive(tmp_path, 'a1b2c3d4-0000-4000-8000-000000000009.tar.gz.enc', 1024)
    removed = backups.prune(2, tmp_path)
    assert len(removed) == 2
    left = {p.name for p in backups.archives(tmp_path)}
    assert made[0].name in left and uploaded.name in left and len(left) == 4
    assert backups.describe(uploaded)['kind'] == 'uploaded' and backups.describe(uploaded)['encrypted']


def test_the_daily_backup_runs_once_after_its_hour():
    import time
    from aios.backups import due
    morning = time.mktime((2026, 9, 24, 1, 30, 0, 0, 0, -1))
    later = time.mktime((2026, 9, 24, 3, 0, 0, 0, 0, -1))
    schedule = {'enabled': True, 'hour': 2}
    assert not due(schedule, '', morning)
    assert due(schedule, '2026-09-23', later)
    assert not due(schedule, '2026-09-24', later)
    assert not due({'enabled': False, 'hour': 2}, '', later) and not due(None, '', later)


class Opened(Exception):
    pass


def test_a_sealed_backup_is_restored_with_the_right_passphrase(environment, monkeypatch):
    from aios import backups, platform
    folder = environment.DATA / 'backups'
    folder.mkdir(parents=True, exist_ok=True)
    source = environment.DATA / 'payload.txt'
    source.write_text('accounts and chats')
    plain = folder / 'a1b2c3d4-0000-4000-8000-00000000000a.tar.gz'
    with tarfile.open(plain, 'w:gz') as tar:
        tar.add(source, arcname='payload.txt')
    sealed = folder / (plain.name + '.enc')
    backups.encrypt_file(plain, sealed, 'correct horse battery')
    plain.unlink()

    def safe_archive(archive, stage):
        with tarfile.open(archive) as tar:
            raise Opened(tar.getnames())

    monkeypatch.setattr(platform, 'safe_archive', safe_archive)
    with pytest.raises(ValueError, match='Wrong passphrase'):
        platform.restore(sealed.name, 'another passphrase')
    with pytest.raises(Opened, match='payload.txt'):
        platform.restore(sealed.name, 'correct horse battery')
    # Without one given, the appliance's own passphrase is tried.
    backups.set_passphrase('correct horse battery')
    with pytest.raises(Opened, match='payload.txt'):
        platform.restore(sealed.name)


def test_the_backup_settings_routes(admin, environment):
    from aios import backups
    listed = admin.get('/api/v1/aios/backups').json()
    assert listed['schedule']['enabled'] is False and listed['encryption'] is False
    assert admin.put('/api/v1/aios/backups/passphrase', json={'passphrase': 'short'}).status_code == 422
    assert admin.put('/api/v1/aios/backups/passphrase', json={'passphrase': 'correct horse battery'}).json() == {'encryption': True}
    assert backups.stored_passphrase() == 'correct horse battery'
    assert backups.passphrase_path().stat().st_mode & 0o777 == 0o600
    schedule = {'enabled': True, 'hour': 3, 'keep': 5, 'include_models': False}
    assert admin.put('/api/v1/aios/backups/schedule', json=schedule).status_code == 200
    listed = admin.get('/api/v1/aios/backups').json()
    assert listed['schedule'] == schedule and listed['encryption'] is True
    assert admin.put('/api/v1/aios/backups/schedule', json={**schedule, 'hour': 24}).status_code == 422
    assert admin.put('/api/v1/aios/backups/passphrase', json={'passphrase': ''}).json() == {'encryption': False}
    assert not backups.passphrase_path().exists()


def test_an_uploaded_sealed_backup_keeps_its_nature(admin, environment):
    from aios import backups
    plain = archive(environment.DATA / 'scratch', size=1024)
    backups.encrypt_file(plain, plain.with_suffix('.enc'), 'correct horse battery')
    (environment.DATA / 'backups').mkdir(parents=True, exist_ok=True)
    response = admin.post('/api/v1/aios/backups/upload', files={'file': ('x.enc', plain.with_suffix('.enc').read_bytes())})
    assert response.status_code == 200 and response.json()['file'].endswith('.tar.gz.enc')
    item = admin.get('/api/v1/aios/backups').json()['items'][0]
    assert item['encrypted'] and item['kind'] == 'uploaded'
    assert admin.get('/api/v1/aios/backups/' + response.json()['file']).status_code == 200


def running(environment, key, last_used, busy=0, autostart=False):
    environment.execute('INSERT INTO installed_models(id,path,sha256,installed_at,state,gguf,config) VALUES (?,?,?,?,?,?,?)',
                        (key, '/m.gguf', 'a' * 64, 0, 'INSTALLED', '{}', '{"autostart": true}' if autostart else '{}'))
    environment.execute("INSERT INTO runtime_instances(id,model_id,state,port,config,desired,last_used,busy,started_at) "
                        "VALUES (?,?,'RUNNING',8090,'{}','RUNNING',?,?,?)", (environment.uid(), key, last_used, busy, last_used))


def test_idle_models_are_unloaded_after_the_configured_time(environment, discovered):
    from aios import runtime
    idle, busy, pinned, recent = (discovered('https://example.com/%s.gguf' % name) for name in 'abcd')
    current = 100_000
    running(environment, idle, current - 3600)
    running(environment, busy, current - 3600, busy=1)
    running(environment, pinned, current - 3600, autostart=True)
    running(environment, recent, current - 60)
    assert runtime.unload_idle(current) == []  # 0 minutes: never
    environment.set_setting('idle_unload_minutes', 30)
    assert runtime.unload_idle(current) == [idle]
    desired = {row['model_id']: row['desired'] for row in environment.rows('SELECT model_id,desired FROM runtime_instances')}
    assert desired == {idle: 'STOPPED', busy: 'RUNNING', pinned: 'RUNNING', recent: 'RUNNING'}


def test_a_request_marks_its_model_in_use(environment, discovered):
    from aios import gateway
    key = discovered()
    running(environment, key, 0)
    gateway.claim(key)
    row = environment.one('SELECT busy,last_used FROM runtime_instances WHERE model_id=?', (key,))
    assert row['busy'] == 1 and row['last_used'] > 0
    gateway.unclaim(key)
    assert environment.one('SELECT busy FROM runtime_instances WHERE model_id=?', (key,))['busy'] == 0


def test_sessions_are_listed_ended_and_signed_out(admin, environment):
    from aios import auth
    listed = admin.get('/api/v1/aios/auth/sessions').json()['items']
    assert len(listed) == 1 and listed[0]['current'] and listed[0]['client']
    users = admin.get('/api/v1/aios/users').json()['items']
    assert users[0]['last_login'] and users[0]['sessions'] == 1
    assert admin.post('/api/v1/aios/users', json={'username': 'second', 'password': 'Second-password-81!', 'role': 'VIEWER'}).status_code == 200
    second = next(u for u in admin.get('/api/v1/aios/users').json()['items'] if u['username'] == 'second')
    assert second['last_login'] is None and second['sessions'] == 0
    auth.authenticate(auth.Login(username='second', password='Second-password-81!'), 'second-device', 'phone')
    second = next(u for u in admin.get('/api/v1/aios/users').json()['items'] if u['username'] == 'second')
    assert second['sessions'] == 1 and second['last_login']
    assert admin.post(f"/api/v1/aios/users/{second['id']}/signout").json() == {'ended': 1}
    assert admin.post(f"/api/v1/aios/users/{users[0]['id']}/signout").status_code == 409
    assert admin.delete('/api/v1/aios/auth/sessions/' + listed[0]['id'][:15]).status_code == 422
    assert admin.delete('/api/v1/aios/auth/sessions/' + '0' * 16).status_code == 404


def test_the_dashboard_carries_the_trends(admin, environment):
    from aios import web
    web.HISTORY['memory'].append(42.0)
    web.HISTORY['temperature'].append(None)
    trends = admin.get('/api/v1/aios/system/dashboard').json()['history']
    assert set(trends) == {'cpu', 'memory', 'gpu_memory', 'temperature', 'tokens_per_second'}
    assert trends['memory'][-1] == 42.0 and trends['temperature'][-1] is None
