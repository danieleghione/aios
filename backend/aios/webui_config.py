"""Keep Open WebUI pointed at this appliance.

Open WebUI reads its settings from the environment only until they are saved in
its own database; from then on the database wins. An appliance restored from a
backup, or carried across releases, therefore keeps whatever the chat had —
another inference key, no microphone, a function-calling mode in which the Image
switch does nothing. At every boot, before the chat starts, the settings that
connect it to AIOS are written back into that database.
"""
import json
import sqlite3
import time
from pathlib import Path

GATEWAY = 'http://127.0.0.1:8081/v1'
EMBEDDING_MODEL = '/opt/aios/embedding/all-MiniLM-L6-v2'


def managed(key):
    """Settings that must always point at this appliance, with its current key."""
    return {
        'image_generation.engine': 'openai',
        'image_generation.openai.api_base_url': GATEWAY,
        'image_generation.openai.api_key': key,
        'audio.stt.engine': 'openai',
        'audio.stt.model': 'aios-speech',
        'audio.stt.openai.api_base_url': GATEWAY,
        'audio.stt.openai.api_key': key,
    }


def voice_settings(key, enabled, stored):
    """The chat's read-aloud button: this appliance's voice engine while a voice
    model is published, the browser's own voice otherwise. An engine an
    administrator chose elsewhere is left alone."""
    # Open WebUI saves OpenAI's own address as the default even while the
    # browser speaks: only an engine pointed somewhere else is a choice.
    engine = stored.get('audio.tts.engine')
    ours = engine in (None, '') or (engine == 'openai' and stored.get('audio.tts.openai.api_base_url') in (None, '', GATEWAY))
    if not ours:
        return {}
    if enabled:
        return {'audio.tts.engine': 'openai', 'audio.tts.model': 'aios-voice',
                'audio.tts.openai.api_base_url': GATEWAY, 'audio.tts.openai.api_key': key}
    return {'audio.tts.engine': ''} if stored.get('audio.tts.engine') == 'openai' else {}


def voice_published(database='/var/lib/aios/database/aios.db'):
    """Whether this appliance has a published voice model, read from its database."""
    try:
        with sqlite3.connect(f'file:{database}?mode=ro', uri=True) as db:
            row = db.execute("SELECT value FROM settings WHERE key='default_voice_model'").fetchone()
    except sqlite3.Error:
        return False
    return bool(row and json.loads(row[0]))


def decoded(value):
    """A stored value: JSON text, or a bare number or NULL, which SQLite keeps
    as such in a JSON column."""
    if isinstance(value, (bytes, str)):
        try:
            return json.loads(value)
        except ValueError:
            return value
    return value


def reconcile(database, key, voice=None):
    """Write the appliance's settings into Open WebUI's database. Returns the keys
    changed; does nothing when the chat has not created its database yet, since
    the environment then applies as it is. voice: whether a voice model is
    published, to point the read-aloud button at it; None leaves it as it is."""
    path = Path(database)
    if not path.exists():
        return []
    changed = []
    with sqlite3.connect(path) as db:
        if not db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='config'").fetchone():
            return []
        stored = {row[0]: decoded(row[1]) for row in db.execute('SELECT key, value FROM config')}
        wanted = dict(managed(key))
        # The chat's own connection: this appliance's entry keeps the current key,
        # any connection an administrator added stays where it is.
        urls = list(stored.get('openai.api_base_urls') or [])
        keys = list(stored.get('openai.api_keys') or [])
        if urls or keys:
            keys += [''] * (len(urls) - len(keys))
            if GATEWAY in urls:
                keys[urls.index(GATEWAY)] = key
            else:
                urls.insert(0, GATEWAY)
                keys.insert(0, key)
            wanted['openai.api_base_urls'] = urls
            wanted['openai.api_keys'] = keys[:len(urls)]
        # Defaults the chat needs offline, kept where an administrator chose
        # something that also works: a document model that is a name to look up
        # online cannot load here, and without legacy function calling the
        # Image switch waits for a tool call small models never make.
        model = stored.get('rag.embedding_model')
        if model is not None and not Path(str(model)).exists() and not stored.get('rag.embedding_engine'):
            wanted['rag.embedding_model'] = EMBEDDING_MODEL
        params = stored.get('models.default_params')
        if isinstance(params, dict) and 'function_calling' not in params:
            wanted['models.default_params'] = {**params, 'function_calling': 'legacy'}
        # Not carried by the environment: written whether or not the chat saved them.
        spoken = voice_settings(key, voice, stored) if voice is not None else {}
        wanted.update(spoken)
        now = int(time.time())
        for name, value in wanted.items():
            if name in stored and stored[name] == value:
                continue
            if name not in stored and name in managed(key) and name not in spoken:
                # Not saved yet: the environment already carries it.
                continue
            db.execute('INSERT INTO config(key, value, updated_at) VALUES (?, ?, ?) '
                       'ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at',
                       (name, json.dumps(value), now))
            changed.append(name)
    return changed


if __name__ == '__main__':
    import sys
    secret = Path(sys.argv[2]).read_text().strip()
    for name in reconcile(sys.argv[1], secret, voice_published()):
        print('Open WebUI setting reconciled:', name)
