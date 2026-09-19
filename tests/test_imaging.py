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
    assert admin.get('/v1/models').json()['data'] == []
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
    assert runtime.same_kind_conflict([text_key], image=True) is None
    assert runtime.same_kind_conflict([image_key], image=False) is None
    assert 'image model is already loaded' in runtime.same_kind_conflict([image_key], image=True)
    assert 'language model is already loaded' in runtime.same_kind_conflict([text_key], image=False)


def test_the_recorded_port_is_the_one_the_engine_listens_on():
    """The portal and the gateway read the port from the row, so a runtime that
    listens on the image port must not be recorded on the language one."""
    source = (ROOT / 'backend/aios/runtime.py').read_text()
    assert 'SET pid=?,started_at=?,port=? WHERE id=?' in source
    assert "row = {**row, 'port': runtime_port(metadata or {})}" in source
