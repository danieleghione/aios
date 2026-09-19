"""Vision models read images only with their multimodal projector (mmproj): it
must be found with the model, downloaded and verified with it, and passed to
llama-server, or every image sent to the chat is refused."""
import hashlib
import json
import struct

import httpx
import pytest
from aios.hardware import compatibility
from aios.providers import attach_projectors, huggingface_model, projector_candidate

GIB = 1024 ** 3


def gguf(architecture, kind=None):
    def string(value):
        value = value.encode()
        return struct.pack('<Q', len(value)) + value
    entries = [('general.architecture', architecture)] + ([('general.type', kind)] if kind else [])
    metadata = b''.join(string(key) + struct.pack('<I', 8) + string(value) for key, value in entries)
    tensor = string('fixture.weight') + struct.pack('<IQIQ', 1, 1, 0, 0)
    data = b'GGUF' + struct.pack('<IQQ', 3, 1, len(entries)) + metadata + tensor
    data += b'\0' * ((32 - len(data) % 32) % 32)
    return data + struct.pack('<f', 1.0)


def model_row(filename):
    return {'model_id': 'unsloth/Qwen3-VL-30B-A3B-Thinking-1M-GGUF', 'filename': filename, 'size': 10 * GIB}


def candidate(name, size=GIB):
    return projector_candidate(name, 'https://example.com/' + name, size)


def test_projectors_are_recognised_and_never_listed_as_models():
    assert candidate('mmproj-F16.gguf')['filename'] == 'mmproj-F16.gguf'
    assert candidate('Qwen3-VL-30B-A3B-Thinking-1M-UD-TQ1_0.gguf') is None
    assert candidate('mmproj-F16-00001-of-00002.gguf') is None


def test_f16_is_preferred_among_precisions():
    rows = [model_row('Qwen3-VL-30B-A3B-Thinking-1M-UD-TQ1_0.gguf')]
    attach_projectors(rows, [candidate('mmproj-F32.gguf', 2 * GIB), candidate('mmproj-BF16.gguf'), candidate('mmproj-F16.gguf')])
    assert rows[0]['projector']['filename'] == 'mmproj-F16.gguf'


def test_a_projector_named_for_another_model_is_not_attached():
    small, large = model_row('google_gemma-3-4b-it-Q4_K_M.gguf'), model_row('google_gemma-3-12b-it-Q4_K_M.gguf')
    projectors = [candidate('mmproj-google_gemma-3-4b-it-f16.gguf'), candidate('mmproj-google_gemma-3-12b-it-f16.gguf')]
    attach_projectors([small, large], projectors)
    assert small['projector']['filename'] == 'mmproj-google_gemma-3-4b-it-f16.gguf'
    assert large['projector']['filename'] == 'mmproj-google_gemma-3-12b-it-f16.gguf'


def test_text_models_get_no_projector():
    rows = [model_row('Qwen3-8B-Q4_K_M.gguf')]
    attach_projectors(rows, [])
    assert 'projector' not in rows[0]


@pytest.mark.asyncio
async def test_huggingface_discovery_carries_the_projector(environment, monkeypatch):
    from aios import providers
    repo = environment.one("SELECT * FROM repositories WHERE provider='huggingface'")
    sha = 'ab' * 32
    siblings = [{'rfilename': 'Qwen3-VL-30B-A3B-Thinking-1M-UD-TQ1_0.gguf', 'size': 8 * GIB},
                {'rfilename': 'mmproj-F16.gguf', 'size': GIB, 'lfs': {'sha256': sha, 'size': GIB}},
                {'rfilename': 'mmproj-F32.gguf', 'size': 2 * GIB}]

    async def fake(url, repository, *args, **kwargs):
        return {'id': 'unsloth/Qwen3-VL-30B-A3B-Thinking-1M-GGUF', 'sha': 'rev', 'siblings': siblings}
    monkeypatch.setattr(providers, 'request_json', fake)
    rows = await huggingface_model(repo, 'unsloth/Qwen3-VL-30B-A3B-Thinking-1M-GGUF')
    assert [r['filename'] for r in rows] == ['Qwen3-VL-30B-A3B-Thinking-1M-UD-TQ1_0.gguf']
    assert rows[0]['projector'] == {'filename': 'mmproj-F16.gguf', 'size': GIB, 'sha256': sha,
                                    'url': 'https://huggingface.co/unsloth/Qwen3-VL-30B-A3B-Thinking-1M-GGUF/resolve/rev/mmproj-F16.gguf'}


def test_the_projector_counts_in_the_memory_estimate():
    hw = {'ram': {'total': 16 * GIB, 'available': 12 * GIB}, 'model_storage': {'free': 100 * GIB}, 'physical_cores': 8, 'isa': ['avx2'], 'numa_nodes': {}}
    text = compatibility({'size': 4 * GIB, 'format': 'GGUF'}, hw=hw)
    vision = compatibility({'size': 4 * GIB, 'format': 'GGUF', 'projector': {'size': GIB}}, hw=hw)
    assert vision['estimated_ram'] - text['estimated_ram'] == GIB
    assert any('vision projector' in reason for reason in vision['reasons'])


def vision_model(environment, discovered, model_bytes, projector_bytes, projector_sha=None):
    key = discovered(size=len(model_bytes), sha=hashlib.sha256(model_bytes).hexdigest())
    row = environment.one('SELECT metadata FROM discovered_models WHERE id=?', (key,))
    metadata = json.loads(row['metadata'])
    metadata['projector'] = {'filename': 'mmproj-F16.gguf', 'url': 'https://example.com/mmproj-F16.gguf', 'size': len(projector_bytes),
                             'sha256': projector_sha or hashlib.sha256(projector_bytes).hexdigest()}
    environment.execute('UPDATE discovered_models SET metadata=? WHERE id=?', (environment.encode(metadata), key))
    return key


def serve(monkeypatch, files):
    from aios import downloads
    original = httpx.AsyncClient
    transport = httpx.MockTransport(lambda request: httpx.Response(200, content=files[request.url.path.rsplit('/', 1)[-1]]))
    monkeypatch.setattr(downloads.httpx, 'AsyncClient', lambda **kwargs: original(transport=transport, **kwargs))
    monkeypatch.setattr(downloads, 'request_target', lambda url, headers, *args: (url, headers, {}))


async def run_download(admin, environment, key):
    from aios import downloads
    response = admin.post(f'/api/v1/aios/models/{key}/install', json={'accept_license': True})
    assert response.status_code == 200, response.text
    job = environment.one('SELECT * FROM downloads WHERE id=?', (response.json()['id'],))
    environment.execute("UPDATE downloads SET state='DOWNLOADING' WHERE id=?", (job['id'],))
    await downloads.download(job)
    return environment.one('SELECT * FROM downloads WHERE id=?', (job['id'],))


@pytest.mark.asyncio
async def test_a_vision_model_installs_with_its_projector(admin, environment, discovered, monkeypatch):
    model, projector = gguf('llama'), gguf('clip', 'mmproj')
    key = vision_model(environment, discovered, model, projector)
    serve(monkeypatch, {'model.gguf': model, 'mmproj-F16.gguf': projector})
    job = await run_download(admin, environment, key)
    assert job['state'] == 'INSTALLED' and job['total'] == len(model) + len(projector) and not job['error']
    assert (environment.DATA / 'models' / f'{key}.mmproj.gguf').read_bytes() == projector
    listed = next(m for m in admin.get('/api/v1/aios/models').json()['items'] if m['id'] == key)
    assert listed['image_input'] is True

    from aios.runtime import RuntimeConfig, arguments
    cpu = arguments({'model_id': key, 'port': 8090}, RuntimeConfig())
    assert cpu[cpu.index('--mmproj') + 1].endswith(f'{key}.mmproj.gguf') and '--no-mmproj-offload' in cpu
    gpu = arguments({'model_id': key, 'port': 8090}, RuntimeConfig(), [{'name': 'Vulkan0', 'type': 'discrete', 'free': GIB, 'total': GIB}])
    assert '--mmproj' in gpu and '--no-mmproj-offload' not in gpu

    assert admin.delete(f'/api/v1/aios/models/{key}').status_code == 200
    assert not (environment.DATA / 'models' / f'{key}.mmproj.gguf').exists()


@pytest.mark.asyncio
async def test_a_bad_projector_does_not_cost_the_model(admin, environment, discovered, monkeypatch):
    model, projector = gguf('llama'), gguf('clip', 'mmproj')
    key = vision_model(environment, discovered, model, projector, projector_sha='0' * 64)
    serve(monkeypatch, {'model.gguf': model, 'mmproj-F16.gguf': projector})
    job = await run_download(admin, environment, key)
    assert job['state'] == 'INSTALLED' and 'without image input' in job['error']
    assert (environment.DATA / 'models' / f'{key}.gguf').exists()
    assert not (environment.DATA / 'models' / f'{key}.mmproj.gguf').exists()
    assert not list((environment.DATA / 'downloads').glob('*.part'))


@pytest.mark.asyncio
async def test_a_file_that_is_not_a_projector_is_refused(admin, environment, discovered, monkeypatch):
    model = gguf('llama')
    key = vision_model(environment, discovered, model, model)
    serve(monkeypatch, {'model.gguf': model, 'mmproj-F16.gguf': model})
    job = await run_download(admin, environment, key)
    assert job['state'] == 'INSTALLED' and 'not a projector' in job['error']
    assert not (environment.DATA / 'models' / f'{key}.mmproj.gguf').exists()


@pytest.mark.asyncio
async def test_a_model_installed_without_projector_gets_it_on_request(admin, environment, discovered, monkeypatch):
    model, projector = gguf('llama'), gguf('clip', 'mmproj')
    key = vision_model(environment, discovered, model, projector)
    target = environment.DATA / 'models' / f'{key}.gguf'
    target.write_bytes(model)
    environment.execute('INSERT INTO installed_models(id,path,sha256,installed_at,state,gguf) VALUES (?,?,?,?,?,?)',
                        (key, str(target), hashlib.sha256(model).hexdigest(), environment.now(), 'INSTALLED', '{"metadata": {}}'))
    listed = next(m for m in admin.get('/api/v1/aios/models').json()['items'] if m['id'] == key)
    assert listed['image_input'] is False and listed['projector']

    def only_projector(request):
        assert request.url.path.endswith('mmproj-F16.gguf'), 'the installed model must not be downloaded again'
        return httpx.Response(200, content=projector)
    from aios import downloads
    original = httpx.AsyncClient
    monkeypatch.setattr(downloads.httpx, 'AsyncClient', lambda **kwargs: original(transport=httpx.MockTransport(only_projector), **kwargs))
    monkeypatch.setattr(downloads, 'request_target', lambda url, headers, *args: (url, headers, {}))
    job = await run_download(admin, environment, key)
    assert job['state'] == 'INSTALLED' and job['total'] == len(projector)
    assert (environment.DATA / 'models' / f'{key}.mmproj.gguf').exists()
    assert admin.post(f'/api/v1/aios/models/{key}/install', json={'accept_license': True}).status_code == 409


def test_a_text_model_cannot_be_installed_twice(admin, environment, discovered):
    key = discovered()
    environment.execute('INSERT INTO installed_models(id,path,sha256,installed_at,state,gguf) VALUES (?,?,?,?,?,?)',
                        (key, '/m.gguf', 'a' * 64, environment.now(), 'INSTALLED', '{}'))
    assert admin.post(f'/api/v1/aios/models/{key}/install', json={'accept_license': True}).status_code == 409


def test_an_installed_model_finds_its_projector_in_a_newer_revision(environment, discovered):
    from aios.providers import projector_for
    key = discovered()
    old = environment.one('SELECT * FROM discovered_models WHERE id=?', (key,))
    newer = json.loads(old['metadata'])
    newer['projector'] = {'filename': 'mmproj-F16.gguf', 'url': 'https://example.com/mmproj-F16.gguf', 'size': GIB, 'sha256': None}
    environment.execute('INSERT INTO discovered_models VALUES (?,?,?,?,?,?,?)', (environment.uid(), old['repository_id'], old['upstream_key'], 'commit-2', environment.encode(newer), environment.now(), environment.now()))
    assert projector_for(key)['filename'] == 'mmproj-F16.gguf'
    assert projector_for(discovered()) is None


def test_a_quantised_projector_is_preferred_over_bfloat16():
    """ggml-org publishes Q8_0 and BF16 projectors: Q8_0 loads on every backend
    and is about half the size."""
    rows = [model_row('Qwen3-Omni-30B-A3B-Instruct-Q4_K_M.gguf')]
    attach_projectors(rows, [candidate('mmproj-Qwen3-Omni-30B-A3B-Instruct-bf16.gguf', 2105 * 2 ** 20),
                             candidate('mmproj-Qwen3-Omni-30B-A3B-Instruct-Q8_0.gguf', 1264 * 2 ** 20)])
    assert rows[0]['projector']['filename'] == 'mmproj-Qwen3-Omni-30B-A3B-Instruct-Q8_0.gguf'
