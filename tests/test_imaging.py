"""Image generation is a second engine beside the language one: its models are
published as several files, it has its own process, port and memory rules, and it
must never be mistaken for a chat model."""
import hashlib
import json
from pathlib import Path

import httpx
import pytest
from aios import imaging
from aios.hardware import compatibility
from aios.providers import diffusion, image_artifact
from aios.runtime import RuntimeConfig, runtime_port

ROOT = Path(__file__).parents[1]
GIB = 1024 ** 3
MiB = 1024 ** 2


def hf(files, revision='rev'):
    return {'sha': revision, 'cardData': {'license': 'apache-2.0'},
            'siblings': [{'rfilename': name, 'size': size, 'lfs': {'sha256': 'ab' * 32, 'size': size}} for name, size in files]}


@pytest.mark.asyncio
async def test_a_split_model_is_offered_with_every_component(environment, monkeypatch):
    from aios import providers
    listings = {
        'city96/FLUX.1-schnell-gguf': hf([('flux1-schnell-Q4_K_S.gguf', 7 * GIB)]),
        'comfyanonymous/flux_text_encoders': hf([('clip_l.safetensors', 235 * MiB), ('t5xxl_fp8_e4m3fn.safetensors', 4667 * MiB)]),
        'Comfy-Org/Lumina_Image_2.0_Repackaged': hf([('split_files/vae/ae.safetensors', 320 * MiB)]),
    }

    async def fake(url, repo, *args, **kwargs):
        return listings[url.split('/api/models/')[1].split('?')[0]]
    monkeypatch.setattr(providers, 'request_json', fake)
    repo = {'id': 'r', 'url': 'https://huggingface.co', 'config': '{"families": null}', 'name': 'Image models'}
    repo['config'] = json.dumps({'families': [f for f in providers.IMAGE_FAMILIES if f['family'] == 'flux']})
    rows = await diffusion(repo)
    assert [row['filename'] for row in rows] == ['flux1-schnell-Q4_K_S.gguf']
    row = rows[0]
    assert row['kind'] == 'image' and row['family'] == 'flux' and row['format'] == 'GGUF'
    assert sorted(c['role'] for c in row['components']) == ['clip_l', 't5xxl', 'vae']
    assert row['url'].endswith('/city96/FLUX.1-schnell-gguf/resolve/rev/flux1-schnell-Q4_K_S.gguf')


@pytest.mark.asyncio
async def test_a_model_whose_component_nobody_publishes_is_not_offered(environment, monkeypatch):
    from aios import providers

    async def fake(url, repo, *args, **kwargs):
        if 'FLUX.1-schnell-gguf' in url:
            return hf([('flux1-schnell-Q4_K_S.gguf', 7 * GIB)])
        raise providers.RepositoryError('AUTH REQUIRED')
    monkeypatch.setattr(providers, 'request_json', fake)
    repo = {'id': 'r', 'url': 'https://huggingface.co', 'name': 'Image models',
            'config': json.dumps({'families': [f for f in providers.IMAGE_FAMILIES if f['family'] == 'flux']})}
    assert await diffusion(repo) == []


def test_only_image_files_are_listed():
    entry = {'name': 'Test', 'family': 'sd1'}
    item = {'name': 'model.safetensors', 'size': 2 * GIB, 'url': 'https://example.com/m', 'sha256': None}
    assert image_artifact(entry, 'org/repo', 'rev', item, [])['quantization'] == 'FP16'
    assert image_artifact(entry, 'org/repo', 'rev', {**item, 'name': 'README.md'}, []) is None
    assert image_artifact(entry, 'org/repo', 'rev', {**item, 'size': 0}, []) is None


def test_the_rating_counts_every_file_and_the_working_buffers():
    hw = {'ram': {'total': 16 * GIB, 'available': 12 * GIB}, 'model_storage': {'free': 100 * GIB},
          'physical_cores': 8, 'isa': ['avx2'], 'numa_nodes': {}, 'accelerators': []}
    metadata = {'kind': 'image', 'family': 'flux', 'size': 7 * GIB,
                'components': [{'role': 'vae', 'filename': 'ae.safetensors', 'size': GIB},
                               {'role': 't5xxl', 'filename': 't5.safetensors', 'size': 4 * GIB}]}
    rating = compatibility(metadata, hw=hw)
    assert rating['kind'] == 'image' and rating['estimated_ram'] == 12 * GIB + 1536 * MiB
    assert any('component file' in reason for reason in rating['reasons'])
    assert any('No GPU' in reason for reason in rating['reasons'])
    assert compatibility({**metadata, 'size': 40 * GIB}, hw=hw)['classification'] == 'NOT_RECOMMENDED'


def image_model(environment, discovered, family='sd1', components=()):
    key = discovered()
    metadata = {'kind': 'image', 'family': family, 'filename': 'model.safetensors', 'size': 2 * GIB,
                'display_name': 'Test image model', 'components': list(components), 'format': 'SAFETENSORS'}
    environment.execute('UPDATE discovered_models SET metadata=? WHERE id=?', (environment.encode(metadata), key))
    path = imaging.model_path(key, 'model.safetensors')
    path.write_bytes(b'checkpoint')
    environment.execute('INSERT INTO installed_models(id,path,sha256,installed_at,state,gguf) VALUES (?,?,?,?,?,?)',
                        (key, str(path), 'a' * 64, environment.now(), 'INSTALLED', json.dumps({'kind': 'image', 'family': family})))
    return key, metadata


def test_a_checkpoint_and_a_split_model_are_loaded_differently(environment, discovered):
    key, metadata = image_model(environment, discovered)
    row = {'model_id': key, 'port': imaging.PORT}
    cpu = imaging.arguments(row, RuntimeConfig(), [], metadata, listed=[])
    assert cpu[cpu.index('--model') + 1].endswith('.safetensors') and '--diffusion-model' not in cpu
    assert cpu[cpu.index('--backend') + 1] == 'cpu'
    assert cpu[cpu.index('--steps') + 1] == '20' and cpu[cpu.index('--width') + 1] == '512'

    component = {'role': 't5xxl', 'filename': 't5.safetensors', 'size': 4 * GIB}
    key, metadata = image_model(environment, discovered, family='flux', components=[component])
    imaging.component_path(key, 't5xxl', 't5.safetensors').write_bytes(b'encoder')
    row = {'model_id': key, 'port': imaging.PORT}
    devices = [{'name': 'Vulkan0', 'description': 'NVIDIA GeForce RTX 3090', 'type': 'discrete', 'free': GIB, 'total': 24 * GIB}]
    gpu = imaging.arguments(row, RuntimeConfig(), devices, metadata, listed=[{'name': 'Vulkan0', 'description': 'NVIDIA GeForce RTX 3090'}])
    assert gpu[gpu.index('--diffusion-model') + 1].endswith('.safetensors') and '--model' not in gpu
    assert gpu[gpu.index('--t5xxl') + 1].endswith('.t5xxl.safetensors')
    assert gpu[gpu.index('--backend') + 1] == 'Vulkan0'
    # The card has less free memory than the model: weights stay in RAM.
    assert '--offload-to-cpu' in gpu and gpu[gpu.index('--steps') + 1] == '4'


def test_each_engine_listens_on_its_own_port():
    assert runtime_port({'kind': 'image'}) == imaging.PORT
    assert runtime_port({}) == 8090 and imaging.PORT != 8090


@pytest.mark.asyncio
async def test_an_image_model_is_installed_with_its_components(admin, environment, discovered, monkeypatch):
    from aios import downloads
    checkpoint, encoder = b'checkpoint-bytes', b'encoder-bytes'
    key = discovered(size=len(checkpoint), sha=hashlib.sha256(checkpoint).hexdigest())
    metadata = {'kind': 'image', 'family': 'flux', 'filename': 'flux1-schnell-Q4_K_S.gguf', 'size': len(checkpoint),
                'display_name': 'FLUX.1 schnell', 'components': [
                    {'role': 't5xxl', 'filename': 't5xxl.safetensors', 'url': 'https://example.com/t5xxl.safetensors',
                     'size': len(encoder), 'sha256': hashlib.sha256(encoder).hexdigest()}]}
    environment.execute('UPDATE discovered_models SET metadata=? WHERE id=?', (environment.encode(metadata), key))
    original = httpx.AsyncClient
    files = {'model.gguf': checkpoint, 't5xxl.safetensors': encoder}
    transport = httpx.MockTransport(lambda request: httpx.Response(200, content=files[request.url.path.rsplit('/', 1)[-1]]))
    monkeypatch.setattr(downloads.httpx, 'AsyncClient', lambda **kwargs: original(transport=transport, **kwargs))
    monkeypatch.setattr(downloads, 'request_target', lambda url, headers, *args: (url, headers, {}))

    response = admin.post(f'/api/v1/aios/models/{key}/install', json={'accept_license': True, 'override_compatibility': True})
    assert response.status_code == 200, response.text
    job = environment.one('SELECT * FROM downloads WHERE id=?', (response.json()['id'],))
    assert job['total'] == len(checkpoint) + len(encoder)
    environment.execute("UPDATE downloads SET state='DOWNLOADING' WHERE id=?", (job['id'],))
    await downloads.download(job)
    assert environment.one('SELECT state FROM downloads WHERE id=?', (job['id'],))['state'] == 'INSTALLED'
    assert imaging.model_path(key, 'x.gguf').read_bytes() == checkpoint
    assert imaging.component_path(key, 't5xxl', 't5xxl.safetensors').read_bytes() == encoder

    # A diffusion model is not a chat model: it must not reach the chat selector.
    assert admin.patch(f'/api/v1/aios/models/{key}', json={'published': True}).status_code == 200
    (environment.ETC / 'secrets').mkdir(parents=True, exist_ok=True)
    (environment.ETC / 'secrets/inference-key').write_text('test-key')
    assert admin.get('/v1/models').status_code == 401
    assert admin.get('/v1/models', headers={'Authorization': 'Bearer test-key'}).json()['data'] == []
    assert environment.setting('default_image_model') == key

    assert admin.delete(f'/api/v1/aios/models/{key}').status_code == 200
    assert not imaging.model_path(key, 'x.gguf').exists()
    assert not imaging.component_path(key, 't5xxl', 't5xxl.safetensors').exists()


def test_the_gateway_refuses_pictures_without_a_published_model(admin, environment):
    key_file = environment.ETC / 'secrets' / 'inference-key'
    key_file.parent.mkdir(parents=True, exist_ok=True)
    key_file.write_text('test-key')
    answer = admin.post('/v1/images/generations', headers={'Authorization': 'Bearer test-key'},
                        json={'model': 'anything', 'prompt': 'a cat'})
    assert answer.status_code == 409 and 'image model' in answer.json()['error']['message']
    assert admin.post('/api/v1/aios/images/generate', json={'prompt': 'a cat'}).status_code == 409


def test_the_chat_is_configured_to_ask_this_appliance_for_pictures():
    firstboot = (ROOT / 'scripts/firstboot.sh').read_text()
    for line in ('ENABLE_IMAGE_GENERATION=true', 'IMAGE_GENERATION_ENGINE=openai',
                 'IMAGES_OPENAI_API_BASE_URL=http://127.0.0.1:8081/v1', 'IMAGES_OPENAI_API_KEY=$INFERENCE_KEY'):
        assert line in firstboot, line
    # The Image button only generates directly with legacy function calling;
    # natively it waits for the model to call a tool, which small models never do.
    assert 'DEFAULT_MODEL_PARAMS=\'{"function_calling": "legacy"}\'' in firstboot
    assert 'ENABLE_IMAGE_PROMPT_GENERATION=false' in firstboot


def test_the_image_engine_is_built_and_verified_like_the_language_one():
    build = (ROOT / 'scripts/build-imaging.sh').read_text()
    assert '-DSD_VULKAN=ON' in build and '-DGGML_CPU_ALL_VARIANTS=ON' in build and '-DGGML_BACKEND_DL=ON' in build
    # Backends are only found next to the executable (see scripts/build-image.sh).
    assert 'imaging/lib/libggml-vulkan.so' in build and 'imaging/bin' in build
    image = (ROOT / 'scripts/build-image.sh').read_text()
    assert './scripts/build-imaging.sh' in image and 'sd-server --list-devices' in image


def test_one_model_of_each_kind_may_be_loaded_at_once(environment, discovered, monkeypatch):
    from aios import runtime
    image_key, _ = image_model(environment, discovered)
    text_key = discovered()
    environment.execute('INSERT INTO installed_models(id,path,sha256,installed_at,state,gguf) VALUES (?,?,?,?,?,?)',
                        (text_key, '/m.gguf', 'a' * 64, environment.now(), 'INSTALLED', '{"metadata": {}}'))
    # A language model does not block an image model, and neither blocks itself twice.
    assert runtime.same_kind_conflict([text_key], 'image') is None
    assert runtime.same_kind_conflict([image_key], 'text') is None
    assert runtime.same_kind_conflict([image_key, text_key], 'speech') is None
    assert 'image model is already loaded' in runtime.same_kind_conflict([image_key], 'image')
    assert 'language model is already loaded' in runtime.same_kind_conflict([text_key], 'text')


def test_the_recorded_port_is_the_one_the_engine_listens_on():
    """The portal and the gateway read the port from the row, so a runtime that
    listens on the image port must not be recorded on the language one."""
    source = (ROOT / 'backend/aios/runtime.py').read_text()
    assert 'SET pid=?,started_at=?,port=? WHERE id=?' in source
    assert "row = {**row, 'port': engine.port}" in source


def test_documents_in_the_chat_have_an_offline_embedding_model():
    # Open WebUI runs offline: a model name is looked up online and never found,
    # and every uploaded file failed with "No embedding model is loaded".
    build = (ROOT / 'scripts/build-image.sh').read_text()
    firstboot = (ROOT / 'scripts/firstboot.sh').read_text()
    assert "local_dir='/opt/aios/embedding/all-MiniLM-L6-v2'" in build and 'embedding model verified offline' in build
    assert 'RAG_EMBEDDING_MODEL=/opt/aios/embedding/all-MiniLM-L6-v2' in firstboot


# Speech to text: the third engine, with the same rules as the other two.

def speech_metadata(size=141 * 1024 ** 2, filename='ggml-base.en.bin'):
    return {'kind': 'speech', 'family': 'whisper', 'filename': filename, 'size': size, 'display_name': 'Whisper base (English)'}


def test_a_speech_model_is_recognised_and_stored_apart():
    from aios import speech
    assert speech.is_speech_model(speech_metadata()) and not speech.is_speech_model({'kind': 'image'})
    assert str(speech.model_path('abc')).endswith('models/abc.bin')
    assert speech.estimated_memory(speech_metadata(size=100)) == 100 + speech.WORKING_MEMORY


def test_the_speech_engine_is_started_for_the_devices_it_has():
    from aios.runtime import RuntimeConfig
    from aios import speech
    row = {'model_id': 'abc'}
    config = RuntimeConfig(threads=3)
    cpu = speech.arguments(row, config, [], speech_metadata())
    assert cpu[0] == speech.BINARY and '--no-gpu' in cpu and cpu[cpu.index('--threads') + 1] == '3'
    assert '--convert' in cpu and cpu[cpu.index('--port') + 1] == str(speech.PORT)
    # The service cannot write to its working directory: transcoding needs a path it owns.
    assert cpu[cpu.index('--tmp-dir') + 1].endswith('/runtime')
    # An ".en" model transcribes English only, and says so to the engine.
    assert cpu[cpu.index('--language') + 1] == 'en'
    multilingual = speech.arguments(row, config, [], speech_metadata(filename='ggml-large-v3-turbo.bin'))
    assert '--language' not in multilingual
    gpu = speech.arguments(row, config, [{'name': 'Vulkan0', 'description': 'GPU', 'type': 'discrete'}], speech_metadata())
    assert '--no-gpu' not in gpu
    assert speech.environment([{'name': 'Vulkan1'}])['GGML_VK_VISIBLE_DEVICES'] == '1'
    assert 'GGML_VK_VISIBLE_DEVICES' not in speech.environment([])


def test_three_kinds_live_side_by_side(environment, discovered):
    from aios import runtime
    image_key, _ = image_model(environment, discovered)
    speech_key = discovered()
    environment.execute('UPDATE discovered_models SET metadata=? WHERE id=?', (environment.encode(speech_metadata()), speech_key))
    environment.execute('INSERT INTO installed_models(id,path,sha256,installed_at,state,gguf) VALUES (?,?,?,?,?,?)',
                        (speech_key, '/m.bin', 'a' * 64, environment.now(), 'INSTALLED', '{}'))
    assert runtime.kind_of(runtime.model_metadata(speech_key)) == 'speech'
    assert runtime.runtime_port(runtime.model_metadata(speech_key)) == 8092
    assert runtime.same_kind_conflict([image_key], 'speech') is None
    assert 'speech model is already loaded' in runtime.same_kind_conflict([speech_key], 'speech')


def test_the_catalogue_reads_whisper_releases():
    from aios.providers import SPEECH_REPO, speech_artifact, speech_name
    assert speech_name('ggml-large-v3-turbo-q5_0.bin') == 'Whisper large v3 turbo · Q5_0'
    assert speech_name('ggml-base.en.bin') == 'Whisper base (English)'
    item = {'name': 'ggml-small.en-q8_0.bin', 'size': 82 * 1024 ** 2, 'sha256': 'b' * 64, 'url': 'https://huggingface.co/x'}
    row = speech_artifact(SPEECH_REPO, 'rev', item, 'MIT', '2026-01-01')
    assert row['kind'] == 'speech' and row['family'] == 'whisper' and row['quantization'] == 'Q8_0'
    assert row['display_name'] == 'Whisper small (English) · Q8_0' and row['format'] == 'GGML'
    # Superseded and experimental builds are left out, as are files of other kinds.
    assert speech_artifact(SPEECH_REPO, 'rev', {**item, 'name': 'ggml-large-v2.bin'}, 'MIT', None) is None
    assert speech_artifact(SPEECH_REPO, 'rev', {**item, 'name': 'ggml-small-tdrz.bin'}, 'MIT', None) is None
    assert speech_artifact(SPEECH_REPO, 'rev', {**item, 'name': 'model.gguf'}, 'MIT', None) is None


def test_a_speech_model_is_rated_on_its_own_terms():
    from aios.hardware import compatibility
    hw = {'ram': {'total': 4 * 1024 ** 3, 'available': 3 * 1024 ** 3}, 'physical_cores': 2, 'model_storage': {'free': 100 * 1024 ** 3},
          'isa': ['avx2'], 'numa_nodes': {}, 'accelerators': []}
    rated = compatibility(speech_metadata(), hw=hw)
    assert rated['kind'] == 'speech' and rated['classification'] in ('OPTIMAL', 'COMPATIBLE')
    assert any('Speech model' in reason for reason in rated['reasons'])
    huge = compatibility(speech_metadata(size=6 * 1024 ** 3), hw=hw)
    assert huge['classification'] == 'NOT_RECOMMENDED'


def test_the_gateway_refuses_transcription_without_a_published_model(admin, environment):
    (environment.ETC / 'secrets').mkdir(parents=True, exist_ok=True)
    (environment.ETC / 'secrets/inference-key').write_text('test-key')
    answer = admin.post('/v1/audio/transcriptions', headers={'Authorization': 'Bearer test-key'},
                        files={'file': ('a.wav', b'RIFF....WAVE', 'audio/wav')})
    assert answer.status_code == 409 and 'speech model' in answer.json()['error']['message']
    assert admin.post('/api/v1/aios/speech/transcribe', files={'file': ('a.wav', b'RIFF', 'audio/wav')}).status_code == 409
    assert admin.post('/v1/audio/transcriptions', files={'file': ('a.wav', b'RIFF', 'audio/wav')}).status_code == 401


def test_the_chat_microphone_is_pointed_at_the_appliance():
    firstboot = (ROOT / 'scripts/firstboot.sh').read_text()
    for line in ('AUDIO_STT_ENGINE=openai', 'AUDIO_STT_OPENAI_API_BASE_URL=http://127.0.0.1:8081/v1',
                 'AUDIO_STT_OPENAI_API_KEY=$INFERENCE_KEY'):
        assert line in firstboot, line


def test_the_speech_engine_is_built_like_the_other_two():
    build = (ROOT / 'scripts/build-voice.sh').read_text()
    assert '-DGGML_VULKAN=ON' in build and '-DGGML_CPU_ALL_VARIANTS=ON' in build and '-DGGML_BACKEND_DL=ON' in build
    assert 'ln -sf "../lib/$(basename "$backend")"' in build  # ggml looks beside the executable
    assert './scripts/build-voice.sh' in (ROOT / 'scripts/build-image.sh').read_text()


def test_the_portal_may_try_a_model_it_has_not_published(environment, discovered, monkeypatch):
    """Publishing decides what the chat and the API may use; the operator can
    still transcribe or draw with a model that is only installed."""
    import asyncio
    from aios import gateway
    key = discovered()
    environment.execute('UPDATE discovered_models SET metadata=? WHERE id=?', (environment.encode(speech_metadata()), key))
    environment.execute('INSERT INTO installed_models(id,path,sha256,installed_at,state,published,gguf,config) VALUES (?,?,?,?,?,0,?,?)',
                        (key, '/m.bin', 'a' * 64, environment.now(), 'INSTALLED', '{}', '{}'))
    loaded = []
    monkeypatch.setattr(gateway, 'settle', lambda model_id, states, timeout: _settled(loaded, model_id))
    with pytest.raises(Exception) as published_only:
        asyncio.run(gateway.ensure_running(key))
    assert '404' in str(published_only.value) or 'not installed' in str(published_only.value)
    # The same model, asked for the way the portal asks, gets as far as loading.
    with pytest.raises(Exception) as attempted:
        asyncio.run(gateway.ensure_running(key, published_only=False))
    assert loaded == [key] and '503' in str(attempted.value)


async def _settled(loaded, model_id):
    loaded.append(model_id)
    return {'state': 'FAILED', 'error': 'no engine in a test'}
