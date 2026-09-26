"""Text to speech: the voice service, its catalogue, its rating, the gateway
route and the chat's read-aloud setting."""
import io
import json
import sqlite3
import wave

import httpx
import pytest


def tone(seconds=0.5, rate=24000):
    buffer = io.BytesIO()
    with wave.open(buffer, 'wb') as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(rate)
        writer.writeframes(b'\x01\x00' * int(rate * seconds))
    return buffer.getvalue()


def test_texts_are_split_into_sentences_and_languages_found():
    from aios.voice_server import guess_language, language_for, parts
    assert parts('One. Two! Three?') == ['One. Two! Three?']
    long = 'First sentence here. ' + 'word ' * 120
    pieces = parts(long, limit=100)
    assert all(len(p) <= 100 for p in pieces) and ' '.join(pieces).split() == long.split()
    assert guess_language('Questo è il test della voce') == 'it'
    assert guess_language('Das ist nicht der Test') == 'de'
    assert guess_language('これはテストです') == 'ja' and guess_language('Это тест') == 'ru'
    assert guess_language('12345') is None
    assert language_for({'voice': 'alloy'}, 'Il gatto è sul tavolo') == 'it'
    assert language_for({'voice': 'fr'}, 'hello') == 'fr' and language_for({'language': 'es-ES'}, 'hello') == 'es'


def test_parts_are_joined_with_a_pause():
    from aios.voice_server import PAUSE_SECONDS, join
    joined = join([tone(0.5), tone(0.25)])
    with wave.open(io.BytesIO(joined)) as reader:
        assert reader.getframerate() == 24000
        assert reader.getnframes() == int(24000 * (0.75 + PAUSE_SECONDS))
    with pytest.raises(ValueError):
        join([tone(0.1, 24000), tone(0.1, 16000)])


def test_a_request_is_answered_in_the_format_asked(monkeypatch):
    from aios import voice_server
    spoken = []

    class Voice:
        def speak(self, text, language, voice):
            spoken.append((text, language, voice))
            return tone()
    audio, media = voice_server.answer({'input': 'Buongiorno a tutti, questo è il test', 'response_format': 'wav'}, Voice())
    assert media == 'audio/wav' and audio[:4] == b'RIFF' and spoken[-1][1:] == ('it', 'alloy')
    calls = []
    monkeypatch.setattr(voice_server.subprocess, 'run', lambda cmd, **kw: calls.append(cmd) or type('R', (), {'returncode': 0, 'stdout': b'ID3mp3', 'stderr': b''})())
    audio, media = voice_server.answer({'input': 'Hello there'}, Voice())
    assert media == 'audio/mpeg' and audio == b'ID3mp3' and calls[0][0] == 'ffmpeg' and '-f' in calls[0]
    for bad in ({'input': ''}, {'input': 'x' * 5000}, {'input': 'hi', 'response_format': 'midi'}, {}):
        with pytest.raises(voice_server.Rejected):
            voice_server.answer(bad, Voice())


def test_llama_tts_runs_on_the_cpu_or_the_chosen_gpu():
    from argparse import Namespace
    from aios.voice_server import Voice
    options = Namespace(binary='/b/llama-tts', model='/m.gguf', codec='/c.gguf', threads=4, work='/w', device='', voices='')
    cpu = Voice(options).command('Hello', 'en', '/w/0.wav')
    assert cpu[:5] == ['/b/llama-tts', '-m', '/m.gguf', '-mm', '/c.gguf'] and '--no-mmproj-offload' in cpu and cpu[cpu.index('-ngl') + 1] == '0'
    assert cpu[cpu.index('--tts-lang') + 1] == 'en' and cpu[cpu.index('-o') + 1] == '/w/0.wav'
    gpu = Voice(Namespace(**{**vars(options), 'device': 'Vulkan0'})).command('Hello', 'it', '/w/0.wav')
    assert gpu[gpu.index('--device') + 1] == 'Vulkan0' and gpu[gpu.index('-ngl') + 1] == '99'


def test_the_catalogue_offers_each_backbone_with_its_codec():
    from aios.providers import voice_artifacts, VOICE_MODELS
    base = 'https://huggingface.co/ggml-org/Qwen3-TTS-12Hz-1.7B-Base-GGUF/resolve/abc/'
    names = {'Qwen3-TTS-12Hz-1.7B-Base-Q4_K_M.gguf': 988, 'Qwen3-TTS-12Hz-1.7B-Base-Q8_0.gguf': 1762, 'Qwen3-TTS-12Hz-1.7B-Base-bf16.gguf': 3312,
             'mmproj-Qwen3-TTS-12Hz-1.7B-Base-Q8_0.gguf': 426, 'mmproj-Qwen3-TTS-12Hz-1.7B-Base-bf16.gguf': 638, '.gitattributes': 1}
    files = {n: {'name': n, 'size': s * 2 ** 20, 'sha256': 'a' * 64, 'url': base + n} for n, s in names.items()}
    rows = voice_artifacts(VOICE_MODELS[0], 'abc', files, 'unknown', '2026-08-03')
    assert [r['quantization'] for r in rows] == ['Q4_K_M', 'Q8_0']
    assert all(r['kind'] == 'voice' and r['license'] == 'apache-2.0' and r['architecture'] == 'qwen3tts' for r in rows)
    assert rows[0]['components'] == [{'role': 'codec', 'filename': 'mmproj-Qwen3-TTS-12Hz-1.7B-Base-Q8_0.gguf', 'url': base + 'mmproj-Qwen3-TTS-12Hz-1.7B-Base-Q8_0.gguf',
                                      'size': 426 * 2 ** 20, 'sha256': 'a' * 64, 'repository': 'ggml-org/Qwen3-TTS-12Hz-1.7B-Base-GGUF'}]
    assert voice_artifacts(VOICE_MODELS[0], 'abc', {k: v for k, v in files.items() if not k.startswith('mmproj')}, 'x', None) == []


def test_voice_backbones_stay_out_of_the_language_catalogue():
    from aios.voice import is_voice_architecture
    assert is_voice_architecture('qwen3tts') and is_voice_architecture('OuteTTS')
    assert not is_voice_architecture('qwen3') and not is_voice_architecture(None)


def test_a_voice_model_is_rated_on_what_it_holds_while_speaking():
    from aios.hardware import compatibility
    from aios.voice import estimated_memory
    model = {'kind': 'voice', 'size': 988 * 2 ** 20, 'components': [{'role': 'codec', 'size': 426 * 2 ** 20}]}
    needed = estimated_memory(model)
    assert 3.0 * 2 ** 30 < needed < 3.6 * 2 ** 30

    def hw(total, available):
        return {'ram': {'total': total * 2 ** 30, 'available': available * 2 ** 30}, 'model_storage': {'free': 100 * 2 ** 30}, 'accelerators': []}
    assert compatibility(model, hw=hw(16, 12))['classification'] == 'OPTIMAL'
    assert compatibility(model, hw=hw(4, 3))['classification'] == 'NOT_RECOMMENDED'
    assert compatibility({**model, 'components': []}, hw=hw(16, 12))['classification'] == 'INCOMPATIBLE'


def test_the_codec_is_downloaded_beside_the_backbone(environment):
    from aios.downloads import companions
    spec = {'role': 'codec', 'filename': 'mmproj.gguf', 'url': 'https://example.com/c', 'size': 10}
    found = companions('model-id', {'kind': 'voice', 'components': [spec]})
    assert found == [('codec', spec, environment.DATA / 'models' / 'model-id.codec.gguf')]


def install_voice(environment, published=1):
    repo = environment.uid()
    environment.execute('INSERT INTO repositories(id,name,provider,url,config,enabled) VALUES (?,?,?,?,?,1)', (repo, 'V', 'voice', 'https://huggingface.co', '{}'))
    key = environment.uid()
    metadata = {'display_name': 'Qwen3-TTS 1.7B · Q4_K_M', 'kind': 'voice', 'size': 100, 'components': [{'role': 'codec', 'size': 10}]}
    environment.execute('INSERT INTO discovered_models VALUES (?,?,?,?,?,?,?)', (key, repo, key, 'r', json.dumps(metadata), 0, 0))
    path = environment.DATA / 'models' / f'{key}.gguf'
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b'GGUF')
    environment.execute('INSERT INTO installed_models(id,path,sha256,installed_at,state,published,gguf) VALUES (?,?,?,?,?,?,?)',
                        (key, str(path), 'a' * 64, 0, 'PUBLISHED' if published else 'INSTALLED', published, '{"kind": "voice"}'))
    return key


def test_speech_is_answered_and_the_model_released(admin, environment, monkeypatch):
    from aios import gateway
    (environment.ETC / 'secrets').mkdir(parents=True, exist_ok=True)
    (environment.ETC / 'secrets/inference-key').write_text('chat-key')
    auth = {'Authorization': 'Bearer chat-key'}
    assert admin.post('/v1/audio/speech', json={'input': 'hello'}, headers=auth).status_code == 409
    key = install_voice(environment)
    environment.set_setting('default_voice_model', key)
    loaded = []

    async def running(model_id, published_only=True):
        loaded.append((model_id, published_only))
        gateway.claim(model_id)
        return {'port': 8094}
    monkeypatch.setattr(gateway, 'ensure_running', running)
    sent = []
    original = httpx.AsyncClient

    def respond(request):
        sent.append(json.loads(request.content))
        return httpx.Response(200, content=tone(), headers={'content-type': 'audio/wav'})
    monkeypatch.setattr(gateway.httpx, 'AsyncClient', lambda **kw: original(transport=httpx.MockTransport(respond), **kw))
    answer = admin.post('/v1/audio/speech', json={'model': 'aios-voice', 'input': 'Ciao a tutti', 'voice': 'alloy', 'response_format': 'wav'}, headers=auth)
    assert answer.status_code == 200 and answer.content[:4] == b'RIFF' and answer.headers['content-type'] == 'audio/wav'
    assert loaded == [(key, True)] and sent[0]['input'] == 'Ciao a tutti' and sent[0]['voice'] == 'alloy'
    assert gateway._BUSY.get(key, 0) == 0
    portal = admin.post('/api/v1/aios/speech/synthesize', json={'model': key, 'input': 'Prova', 'response_format': 'mp3'})
    assert portal.status_code == 200 and loaded[-1] == (key, False) and sent[-1]['response_format'] == 'wav'
    assert gateway._BUSY.get(key, 0) == 0
    assert admin.post('/v1/audio/speech', json={'input': 'x' * 5000}, headers=auth).status_code == 422


def test_a_transcription_releases_its_model(admin, environment, monkeypatch):
    from aios import gateway
    repo = environment.uid()
    environment.execute('INSERT INTO repositories(id,name,provider,url,config,enabled) VALUES (?,?,?,?,?,1)', (repo, 'S', 'speech', 'https://huggingface.co', '{}'))
    key = environment.uid()
    environment.execute('INSERT INTO discovered_models VALUES (?,?,?,?,?,?,?)', (key, repo, key, 'r', json.dumps({'kind': 'speech', 'size': 1}), 0, 0))
    environment.execute('INSERT INTO installed_models(id,path,sha256,installed_at,state,published,gguf) VALUES (?,?,?,?,?,1,?)', (key, '/m', 'a' * 64, 0, 'PUBLISHED', '{}'))

    async def running(model_id, published_only=True):
        gateway.claim(model_id)
        return {'port': 8092}
    monkeypatch.setattr(gateway, 'ensure_running', running)
    original = httpx.AsyncClient
    monkeypatch.setattr(gateway.httpx, 'AsyncClient', lambda **kw: original(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={'text': ' hi '})), **kw))
    answer = admin.post('/api/v1/aios/speech/transcribe', files={'file': ('a.wav', tone(), 'audio/wav')}, data={'model': key})
    assert answer.status_code == 200 and answer.json()['text'] == 'hi'
    assert gateway._BUSY.get(key, 0) == 0


def test_publishing_a_voice_model_points_the_chat_at_it(admin, environment):
    key = install_voice(environment, published=0)
    assert admin.patch(f'/api/v1/aios/models/{key}', json={'published': True}).status_code == 200
    assert environment.setting('default_voice_model') == key
    jobs = [row['action'] for row in environment.rows('SELECT action FROM system_jobs')]
    assert jobs == ['chat-voice']
    admin.patch(f'/api/v1/aios/models/{key}', json={'published': False})
    assert environment.setting('default_voice_model') == '' and len(environment.rows('SELECT action FROM system_jobs')) == 2


def webui(path, values):
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE config (key TEXT PRIMARY KEY, value JSON NOT NULL, updated_at BIGINT)')
        for key, value in values.items():
            db.execute('INSERT INTO config VALUES (?, ?, 0)', (key, json.dumps(value)))
    return path


def read(path):
    with sqlite3.connect(path) as db:
        return {k: json.loads(v) for k, v in db.execute('SELECT key, value FROM config')}


def test_the_read_aloud_button_follows_the_published_voice(tmp_path):
    from aios.webui_config import GATEWAY, reconcile
    # What Open WebUI 0.11 saves by default: the browser speaks, OpenAI's address filled in.
    db = webui(tmp_path / 'webui.db', {'audio.tts.engine': '', 'audio.tts.openai.api_base_url': 'https://api.openai.com/v1', 'audio.tts.model': 'tts-1'})
    reconcile(db, 'key', voice=True)
    values = read(db)
    assert values['audio.tts.engine'] == 'openai' and values['audio.tts.openai.api_base_url'] == GATEWAY
    assert values['audio.tts.openai.api_key'] == 'key' and values['audio.tts.model'] == 'aios-voice'
    reconcile(db, 'key', voice=False)
    assert read(db)['audio.tts.engine'] == ''  # the browser's own voice
    assert reconcile(db, 'key', voice=False) == []
    # An engine an administrator chose elsewhere stays.
    other = webui(tmp_path / 'other.db', {'audio.tts.engine': 'elevenlabs'})
    reconcile(other, 'key', voice=True)
    assert read(other) == {'audio.tts.engine': 'elevenlabs'}
    openai = {'audio.tts.engine': 'openai', 'audio.tts.openai.api_base_url': 'https://api.openai.com/v1'}
    elsewhere = webui(tmp_path / 'openai.db', openai)
    reconcile(elsewhere, 'key', voice=True)
    assert read(elsewhere) == openai
    # None: leave the button as it is.
    untouched = webui(tmp_path / 'untouched.db', {})
    assert reconcile(untouched, 'key') == [] and read(untouched) == {}


def test_the_boot_reads_whether_a_voice_is_published(tmp_path):
    from aios.webui_config import voice_published
    database = tmp_path / 'aios.db'
    assert voice_published(database) is False
    with sqlite3.connect(database) as db:
        db.execute('CREATE TABLE settings(key TEXT PRIMARY KEY, value TEXT NOT NULL)')
        db.execute("INSERT INTO settings VALUES ('default_voice_model', '\"abc\"')")
    assert voice_published(database) is True


def test_an_older_runtime_says_what_to_update(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from aios import voice
    config = SimpleNamespace(threads=2)
    monkeypatch.setattr(voice, 'BINARY', str(tmp_path / 'llama-tts'))
    with pytest.raises(ValueError, match='runtime component release'):
        voice.arguments({'model_id': 'm'}, config, [], {})
    (tmp_path / 'llama-tts').write_bytes(b'')
    command = voice.arguments({'model_id': 'm'}, config, [{'name': 'Vulkan0'}], {})
    assert command[1:3] == ['-m', 'aios.voice_server'] and command[command.index('--device') + 1] == 'Vulkan0'
    assert command[command.index('--binary') + 1] == str(tmp_path / 'llama-tts')


def test_a_short_sentence_keeps_the_language_of_the_one_before(monkeypatch):
    from aios import voice_server
    monkeypatch.setattr(voice_server, '_last', {'language': 'en', 'at': 0.0})
    assert voice_server.language_for({'voice': 'alloy'}, 'Sì, 42.', current=1000) == 'en'  # nothing before it
    assert voice_server.language_for({'voice': 'alloy'}, 'Questa è la risposta della chat.', current=1000) == 'it'
    assert voice_server.language_for({'voice': 'alloy'}, 'Sì, 42.', current=1010) == 'it'
    assert voice_server.language_for({'voice': 'alloy'}, 'OK.', current=1000 + voice_server.REMEMBER_SECONDS + 1) == 'en'


def test_the_portal_may_play_the_audio_it_receives():
    # The portal plays spoken text from a blob: URL; without media-src the
    # browser falls back to default-src 'self' and the player stays grey.
    from pathlib import Path
    conf = (Path(__file__).resolve().parents[1] / 'config/nginx.conf').read_text()
    policy = next(line for line in conf.splitlines() if 'Content-Security-Policy' in line)
    assert "media-src 'self' blob:" in policy


def test_every_text_is_spoken_with_one_chosen_voice(tmp_path):
    from argparse import Namespace
    from pathlib import Path
    from aios.voice import DEFAULT_VOICE, VOICES
    from aios.voice_server import Voice, voice_for
    assert voice_for({'voice': 'Nova'}) == 'nova' and voice_for({'voice': 'it'}) == DEFAULT_VOICE and voice_for({}) == DEFAULT_VOICE
    assert voice_for({'voice': 'someone-else'}) == DEFAULT_VOICE
    (tmp_path / 'nova.wav').write_bytes(tone())
    options = Namespace(binary='/b/llama-tts', model='/m.gguf', codec='/c.gguf', threads=4, work='/w', device='', voices=str(tmp_path))
    command = Voice(options).command('Ciao.', 'it', '/w/0.wav', 'nova')
    assert command[command.index('--tts-speaker-file') + 1] == str(tmp_path / 'nova.wav')
    # A voice whose sample is missing speaks without one rather than failing.
    assert '--tts-speaker-file' not in Voice(options).command('Ciao.', 'it', '/w/0.wav', 'onyx')
    # The appliance ships a sample for every voice it offers: mono 16-bit WAV, a few seconds long.
    folder = Path(__file__).resolve().parents[1] / 'config/voices'
    for name in VOICES:
        with wave.open(str(folder / f'{name}.wav')) as sample:
            assert sample.getnchannels() == 1 and sample.getsampwidth() == 2 and 4 < sample.getnframes() / sample.getframerate() < 10, name


def test_the_chat_and_the_portal_list_the_voices(admin, client):
    from aios.voice import VOICES
    listed = client.get('/v1/audio/voices').json()['voices']
    assert [v['id'] for v in listed] == list(VOICES) and listed[0]['name'] == 'Alloy · medium'
    assert client.get('/v1/audio/models').json() == {'models': [{'id': 'aios-voice'}]}
    portal = admin.get('/api/v1/aios/speech/voices').json()
    assert portal['default'] == 'alloy' and len(portal['voices']) == len(VOICES)
