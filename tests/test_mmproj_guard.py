"""Companion artefacts carry no language weights and must not reach the runtime."""
import struct

import pytest
from aios.gguf import inspect_gguf


def gguf_bytes(**metadata):
    def string(value):
        value = value.encode()
        return struct.pack('<Q', len(value)) + value
    kv = b''.join(string(k) + struct.pack('<I', 8) + string(v) for k, v in metadata.items())
    tensor = string('fixture.weight') + struct.pack('<IQIQ', 1, 1, 0, 0)
    data = b'GGUF' + struct.pack('<IQQ', 3, 1, len(metadata)) + kv + tensor
    data += b'\0' * ((32 - len(data) % 32) % 32)
    return data + struct.pack('<f', 1.0)


@pytest.mark.parametrize('metadata', [
    {'general.architecture': 'clip', 'general.type': 'mmproj'},
    {'general.architecture': 'clip'},
    {'general.architecture': 'qwen2', 'general.type': 'mmproj'}])
def test_projector_is_refused_at_install(tmp_path, metadata):
    path = tmp_path / 'companion.gguf'
    path.write_bytes(gguf_bytes(**metadata))
    with pytest.raises(ValueError, match='mmproj'):
        inspect_gguf(path)


def test_a_real_model_still_installs(tmp_path, tiny_gguf):
    path = tmp_path / 'model.gguf'
    path.write_bytes(tiny_gguf)
    assert inspect_gguf(path)['metadata']['general.architecture'] == 'llama'


def install(environment, key, metadata):
    environment.execute(
        'INSERT INTO installed_models(id,path,sha256,installed_at,state,published,gguf,config) VALUES (?,?,?,?,?,?,?,?)',
        (key, str(environment.DATA / 'models' / f'{key}.gguf'), 'a' * 64, environment.now(),
         'INSTALLED', 0, environment.encode({'metadata': metadata}), '{}'))
    (environment.DATA / 'models').mkdir(parents=True, exist_ok=True)
    (environment.DATA / 'models' / f'{key}.gguf').write_bytes(b'x')


def test_projector_cannot_be_published_or_started(admin, environment, discovered):
    key = discovered()
    install(environment, key, {'general.architecture': 'clip', 'general.type': 'mmproj'})
    published = admin.patch(f'/api/v1/aios/models/{key}', json={'published': True})
    assert published.status_code == 409 and 'mmproj' in published.json()['error']['message']
    started = admin.post(f'/api/v1/aios/runtime/{key}/start')
    assert started.status_code == 409 and 'mmproj' in started.json()['error']['message']


def test_language_model_is_unaffected(admin, environment, discovered):
    key = discovered()
    install(environment, key, {'general.architecture': 'qwen2', 'general.type': 'model'})
    assert admin.patch(f'/api/v1/aios/models/{key}', json={'published': True}).status_code == 200
    assert admin.post(f'/api/v1/aios/runtime/{key}/start').status_code == 200
