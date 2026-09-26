"""Operations: upload limits, updatable engines, built-in repositories, the chat's
settings, alerts, and what the portal shows about downloads and defaults."""
import json
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_recordings_up_to_the_gateway_limit_pass_nginx():
    conf = (ROOT / 'config/nginx.conf').read_text()
    for location in ('location /v1/audio/ {', 'location = /api/v1/aios/speech/transcribe {'):
        block = conf.split(location, 1)[1].split('}', 1)[0]
        assert 'client_max_body_size 200m;' in block, location
    from aios.gateway import AUDIO_MAX_BYTES
    assert AUDIO_MAX_BYTES == 200 * 1024 ** 2


def test_every_engine_can_be_updated_from_a_signed_release():
    from aios.platform import RELEASE_COMPONENTS
    assert set(RELEASE_COMPONENTS) == {'application', 'runtime', 'imaging', 'voice', 'open-webui', 'cuda'}
    assert RELEASE_COMPONENTS['voice']['entrypoint'] == 'bin/whisper-server'
    assert RELEASE_COMPONENTS['imaging']['services'] == ['aios-runtime-manager']
    signer = (ROOT / 'scripts/sign-release.py').read_text()
    assert "choices=['application','runtime','imaging','voice','open-webui','cuda']" in signer


def test_repositories_added_later_reach_an_existing_installation(environment):
    names = {row['name'] for row in environment.rows('SELECT name FROM repositories')}
    assert {'Speech models (whisper)', 'Image models (diffusion)', 'Hugging Face'} <= names
    # A database from before the speech engine, with one repository an
    # administrator configured and enabled.
    environment.execute("DELETE FROM repositories WHERE name='Speech models (whisper)'")
    environment.execute("UPDATE repositories SET enabled=1, config='{\"models\": [\"x/y\"]}' WHERE name='Hugging Face'")
    environment.initialize()
    rows = {row['name']: row for row in environment.rows('SELECT * FROM repositories')}
    assert rows['Speech models (whisper)']['enabled'] == 0 and rows['Speech models (whisper)']['provider'] == 'speech'
    assert rows['Hugging Face']['enabled'] == 1 and json.loads(rows['Hugging Face']['config']) == {'models': ['x/y']}
    assert len([name for name in rows if name == 'Speech models (whisper)']) == 1


def webui_database(path, values):
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE config (key TEXT PRIMARY KEY, value JSON NOT NULL, updated_at BIGINT)')
        for key, value in values.items():
            # SQLite keeps a number written to a JSON column as a number.
            db.execute('INSERT INTO config VALUES (?, ?, 0)', (key, value if isinstance(value, int) and not isinstance(value, bool) else json.dumps(value)))
    return path


def stored(path):
    with sqlite3.connect(path) as db:
        from aios.webui_config import decoded
        return {row[0]: decoded(row[1]) for row in db.execute('SELECT key, value FROM config')}


def test_the_chat_is_pointed_back_at_this_appliance(tmp_path):
    from aios.webui_config import EMBEDDING_MODEL, GATEWAY, reconcile
    db = webui_database(tmp_path / 'webui.db', {
        'openai.api_base_urls': [GATEWAY, 'https://other.example/v1'], 'openai.api_keys': ['old-key', 'their-key'],
        'audio.stt.engine': '', 'audio.stt.openai.api_key': 'old-key',
        'rag.embedding_model': 'sentence-transformers/all-MiniLM-L6-v2', 'models.default_params': {'temperature': 0.5},
        'image_generation.prompt.enable': True, 'rag.top_k': 5, 'ui.banner': None})
    changed = reconcile(db, 'new-key')
    values = stored(db)
    assert values['openai.api_keys'] == ['new-key', 'their-key'] and values['openai.api_base_urls'][1] == 'https://other.example/v1'
    assert values['audio.stt.engine'] == 'openai' and values['audio.stt.openai.api_key'] == 'new-key'
    assert values['rag.embedding_model'] == EMBEDDING_MODEL
    assert values['models.default_params'] == {'temperature': 0.5, 'function_calling': 'legacy'}
    # An administrator's own choice outside the connection stays.
    assert values['image_generation.prompt.enable'] is True and values['rag.top_k'] == 5
    assert 'image_generation.engine' not in values  # never saved: the environment applies
    assert reconcile(db, 'new-key') == [] and changed


def test_a_chat_that_never_saved_its_settings_is_left_alone(tmp_path):
    from aios.webui_config import reconcile
    assert reconcile(tmp_path / 'missing.db', 'key') == []
    db = webui_database(tmp_path / 'webui.db', {'models.default_params': {'function_calling': 'native'}})
    assert reconcile(db, 'key') == [] and stored(db)['models.default_params'] == {'function_calling': 'native'}


def test_firstboot_reconciles_the_chat_before_it_starts():
    firstboot = (ROOT / 'scripts/firstboot.sh').read_text()
    reconcile = firstboot.index('-m aios.webui_config /var/lib/aios/webui/webui.db')
    assert reconcile < firstboot.index('chown -R aios-webui:aios-webui /var/lib/aios/webui')


def test_alerts_open_once_refresh_and_close(environment):
    from aios import alerts
    assert alerts.raise_alert('disk', 'WARNING', 'first') is True
    assert alerts.raise_alert('disk', 'ERROR', 'worse') is False
    row = environment.one("SELECT * FROM alerts WHERE id='disk'")
    assert row['severity'] == 'ERROR' and row['message'] == 'worse' and row['resolved'] == 0
    alerts.resolve('disk')
    assert environment.one("SELECT resolved FROM alerts WHERE id='disk'")['resolved'] == 1
    assert alerts.raise_alert('disk', 'WARNING', 'again') is True


def test_the_disk_and_update_checks(environment, monkeypatch):
    from collections import namedtuple
    from aios import alerts
    usage = namedtuple('usage', 'total used free')
    monkeypatch.setattr(alerts.shutil, 'disk_usage', lambda path: usage(100 * 1024 ** 3, 98 * 1024 ** 3, 2 * 1024 ** 3))
    alerts.check_disk()
    assert 'almost full' in environment.one("SELECT message FROM alerts WHERE id='disk' AND resolved=0")['message']
    monkeypatch.setattr(alerts.shutil, 'disk_usage', lambda path: usage(100 * 1024 ** 3, 10 * 1024 ** 3, 90 * 1024 ** 3))
    alerts.check_disk()
    assert environment.one("SELECT resolved FROM alerts WHERE id='disk'")['resolved'] == 1
    (environment.DATA / 'system').mkdir(parents=True, exist_ok=True)
    (environment.DATA / 'system/updates.json').write_text('{"security": 3, "reboot_required": false}')
    alerts.check_updates()
    assert '3 security updates' in environment.one("SELECT message FROM alerts WHERE id='updates'")['message']
    (environment.DATA / 'system/updates.json').write_text('{"security": 0, "reboot_required": true}')
    alerts.check_updates()
    assert 'restart' in environment.one("SELECT message FROM alerts WHERE id='updates'")['message']


def test_a_failing_repository_raises_an_alert_and_clears_it(environment, monkeypatch):
    import asyncio
    from aios import providers
    key = environment.uid()
    environment.execute("INSERT INTO repositories(id,name,provider,url,config,enabled) VALUES (?,?,?,?,?,1)",
                        (key, 'Mirror', 'http', 'https://example.com/index.json', '{}'))

    async def broken(repo):
        raise providers.RepositoryError('Repository HTTP 500')

    monkeypatch.setitem(providers.PROVIDERS, 'http', broken)
    asyncio.run(providers.sync(key))
    alert = environment.one('SELECT * FROM alerts WHERE id=?', ('repository:' + key,))
    assert alert['resolved'] == 0 and 'Mirror' in alert['message'] and 'HTTP 500' in alert['message']

    async def working(repo):
        return []

    monkeypatch.setitem(providers.PROVIDERS, 'http', working)
    asyncio.run(providers.sync(key))
    assert environment.one('SELECT resolved FROM alerts WHERE id=?', ('repository:' + key,))['resolved'] == 1


def test_an_alert_can_be_dismissed(admin, environment):
    from aios import alerts
    alerts.raise_alert('updates', 'INFO', '2 security updates are available')
    listed = admin.get('/api/v1/aios/system/dashboard').json()['alerts']
    assert [a['id'] for a in listed] == ['updates']
    assert admin.post('/api/v1/aios/alerts/updates/dismiss').status_code == 200
    assert admin.get('/api/v1/aios/system/dashboard').json()['alerts'] == []
    assert admin.post('/api/v1/aios/alerts/updates/dismiss').status_code == 404


def test_downloads_are_listed_by_name(admin, environment, discovered):
    key = discovered()
    environment.execute("INSERT INTO downloads(id,model_id,state,downloaded,total,created_at,updated_at) VALUES (?,?,?,?,?,?,?)",
                        (environment.uid(), key, 'DOWNLOADING', 50, 100, environment.now(), environment.now()))
    item = admin.get('/api/v1/aios/downloads').json()['items'][0]
    assert item['display_name'] == 'Test model' and item['kind'] == 'text' and item['progress'] == 50.0


def test_the_default_model_is_shown_and_kept_per_kind(admin, environment, discovered):
    key = discovered()
    environment.execute('INSERT INTO installed_models(id,path,sha256,installed_at,state,gguf,config) VALUES (?,?,?,?,?,?,?)',
                        (key, '/m.gguf', 'a' * 64, environment.now(), 'INSTALLED', '{"metadata": {}}', '{}'))
    assert admin.get('/api/v1/aios/models').json()['items'][0]['is_default'] is False
    assert admin.patch(f'/api/v1/aios/models/{key}', json={'default': True}).status_code == 200
    item = admin.get('/api/v1/aios/models').json()['items'][0]
    assert item['is_default'] is True and item['kind'] == 'text'
    assert environment.setting('default_model') == key and not environment.setting('default_image_model')


def test_sign_in_attempts_beyond_the_limit_say_so():
    conf = (ROOT / 'config/nginx.conf').read_text()
    block = conf.split('location /api/v1/aios/auth/ {', 1)[1].split('}', 1)[0]
    assert 'limit_req zone=login' in block and 'limit_req_status 429;' in block
