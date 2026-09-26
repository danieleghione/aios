"""The optional CUDA package: installed as a signed component, loaded by llama.cpp
from outside the runtime, preferred over Vulkan for the same card, removable."""
import base64
import hashlib
import io
import tarfile
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from aios import accelerators, platform

LISTED = """Available devices:
  Vulkan0: NVIDIA GeForce RTX 3060 (12288 MiB, 11800 MiB free)
  Vulkan1: Intel(R) UHD Graphics 770 (7800 MiB, 7800 MiB free)
  CUDA0: NVIDIA GeForce RTX 3060 (12288 MiB, 11900 MiB free)
"""


def test_a_card_seen_by_both_backends_is_used_once_through_cuda():
    devices = accelerators.prefer_cuda(accelerators.parse_devices(LISTED))
    assert [d['name'] for d in devices] == ['Vulkan1', 'CUDA0']
    assert accelerators.prefer_cuda(accelerators.parse_devices(LISTED.replace('  CUDA0', '  #'))) == \
        accelerators.parse_devices(LISTED.replace('  CUDA0', '  #'))


def test_llama_cpp_finds_the_package_only_when_it_is_there(tmp_path, monkeypatch):
    monkeypatch.setattr(accelerators, 'CUDA_DIR', str(tmp_path / 'cuda'))
    assert accelerators.runtime_environment() == {'LD_LIBRARY_PATH': '/opt/aios/runtime/lib'}
    assert accelerators.cuda_package() == {'installed': False, 'version': ''}
    (tmp_path / 'cuda/lib').mkdir(parents=True)
    (tmp_path / 'cuda/lib/libggml-cuda.so').write_bytes(b'\x7fELF')
    (tmp_path / 'cuda/VERSION').write_text('12.0-b1\n')
    env = accelerators.runtime_environment()
    assert env['GGML_BACKEND_PATH'] == str(tmp_path / 'cuda/lib/libggml-cuda.so')
    assert env['LD_LIBRARY_PATH'] == '/opt/aios/runtime/lib:' + str(tmp_path / 'cuda/lib')
    assert accelerators.cuda_package() == {'installed': True, 'version': '12.0-b1'}
    from aios.runtime import ENGINES
    assert ENGINES['text'].environment([])['GGML_BACKEND_PATH'].endswith('libggml-cuda.so')
    assert ENGINES['voice'].environment([])['GGML_BACKEND_PATH'].endswith('libggml-cuda.so')


@pytest.fixture
def cuda_release(environment, monkeypatch, tmp_path):
    key = Ed25519PrivateKey.generate()
    public = tmp_path / 'release.pub'
    public.write_bytes(key.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo))
    opt = tmp_path / 'opt'
    opt.mkdir()
    archive = environment.DATA / 'backups' / (environment.uid() + '.tar.gz')
    with tarfile.open(archive, 'w:gz') as tar:
        for name in ('lib/libggml-cuda.so', 'lib/libcudart.so.12'):
            entry = tarfile.TarInfo(name)
            entry.size = 4
            tar.addfile(entry, io.BytesIO(b'\x7fELF'))
    manifest = {'component': 'cuda', 'version': '12.0-b1', 'sha256': hashlib.sha256(archive.read_bytes()).hexdigest()}
    original = Path
    monkeypatch.setattr(platform, 'Path', lambda value: public if value == '/etc/aios-release.pub' else opt if value == '/opt/aios' else original(value))
    monkeypatch.setattr(platform.time, 'sleep', lambda seconds: None)
    monkeypatch.setattr(platform, 'answers', lambda component, attempts=20: None)
    calls = []
    monkeypatch.setattr(platform, 'run', lambda args, **kwargs: calls.append(list(args)) or 'active')
    payload = {'manifest': manifest, 'signature': base64.b64encode(key.sign(environment.encode(manifest).encode())).decode(), 'file': archive.name}
    return payload, opt, calls


def test_the_package_is_installed_the_first_time_and_removed(cuda_release):
    payload, opt, calls = cuda_release
    result = platform.update_release(payload)
    assert result == {'component': 'cuda', 'version': '12.0-b1', 'rollback': None}
    assert (opt / 'cuda/lib/libggml-cuda.so').is_file() and (opt / 'cuda/VERSION').read_text() == '12.0-b1\n'
    assert ['systemctl', 'stop', 'aios-runtime-manager'] in calls
    removed = platform.remove_component('cuda')
    assert removed['removed'] and not (opt / 'cuda').exists() and (opt / 'cuda.previous/lib/libggml-cuda.so').is_file()
    with pytest.raises(ValueError, match='not installed'):
        platform.remove_component('cuda')
    with pytest.raises(ValueError, match='Only the CUDA package'):
        platform.remove_component('runtime')


def test_a_first_install_that_fails_leaves_nothing_behind(cuda_release, monkeypatch):
    payload, opt, _ = cuda_release
    monkeypatch.setattr(platform, 'answers', lambda component, attempts=20: (_ for _ in ()).throw(ValueError('did not answer')))
    with pytest.raises(ValueError, match='health check failed'):
        platform.update_release(payload)
    assert not (opt / 'cuda').exists()


def test_the_portal_shows_and_removes_the_package(admin, environment, tmp_path, monkeypatch):
    monkeypatch.setattr(accelerators, 'CUDA_DIR', str(tmp_path / 'cuda'))
    assert admin.get('/api/v1/aios/hardware/profile').json()['cuda'] == {'installed': False, 'version': ''}
    assert admin.post('/api/v1/aios/system/update/cuda/remove').status_code == 409
    (tmp_path / 'cuda/lib').mkdir(parents=True)
    (tmp_path / 'cuda/lib/libggml-cuda.so').write_bytes(b'\x7fELF')
    assert admin.get('/api/v1/aios/hardware/profile').json()['cuda']['installed'] is True
    queued = admin.post('/api/v1/aios/system/update/cuda/remove').json()
    assert queued['action'] == 'component-remove'
    assert environment.one('SELECT payload FROM system_jobs WHERE id=?', (queued['id'],))['payload'] == '{"component":"cuda"}'


def test_dedicated_gpu_memory_is_measured_and_shown(environment, tmp_path, monkeypatch):
    drm = tmp_path / 'drm'
    for name, used, total in (('card0', 2 * 2 ** 30, 8 * 2 ** 30), ('card0-DP-1', 2 * 2 ** 30, 8 * 2 ** 30)):
        (drm / name / 'device').mkdir(parents=True)
        (drm / name / 'device/mem_info_vram_used').write_text(str(used))
        (drm / name / 'device/mem_info_vram_total').write_text(str(total))
    (drm / 'card1/device').mkdir(parents=True)  # an Intel iGPU: no VRAM counters
    monkeypatch.setattr(accelerators, 'SYSFS_DRM', str(drm))
    monkeypatch.setattr(accelerators.os.path, 'exists', lambda path: path == '/proc/driver/nvidia/version' or Path(path).exists())
    monkeypatch.setattr(accelerators, '_run', lambda command, timeout: '1024, 12288\n512, 12288\n' if command[0] == accelerators.NVIDIA_SMI else '')
    assert accelerators.vram_usage() == {'used': (2 * 1024 + 1536) * 2 ** 20, 'total': (8 * 1024 + 24576) * 2 ** 20}
    accelerators.record_vram()
    assert accelerators.recorded_vram() == round(100 * 3584 / 32768, 1)
    monkeypatch.setattr(accelerators.time, 'time', lambda: 10 ** 12)
    assert accelerators.recorded_vram() is None  # a stale sample is not shown as current


def test_no_dedicated_memory_means_no_trend(environment, tmp_path, monkeypatch):
    monkeypatch.setattr(accelerators, 'SYSFS_DRM', str(tmp_path / 'none'))
    monkeypatch.setattr(accelerators, '_run', lambda command, timeout: '')
    assert accelerators.vram_usage() is None
    accelerators.record_vram()
    assert accelerators.recorded_vram() is None
