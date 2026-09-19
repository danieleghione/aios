"""GPU acceleration must work for any vendor, prefer the right device, count
memory honestly and never leave a model worse off than the CPU would."""
import json
import subprocess
from pathlib import Path

import pytest
from aios import accelerators
from aios.hardware import compatibility
from aios.runtime import RuntimeConfig, arguments, fit_context

ROOT = Path(__file__).parents[1]
GIB = 1024 ** 3
MiB = 1024 ** 2

LIST_DEVICES = """load_backend: loaded Vulkan backend from /opt/aios/runtime/lib/libggml-vulkan.so
Available devices:
  Vulkan0: NVIDIA GeForce RTX 3090 (24576 MiB, 23800 MiB free)
  Vulkan1: Intel(R) Iris(R) Xe Graphics (ADL GT2) (15872 MiB, 12000 MiB free)
"""

VULKANINFO = """==========
VULKANINFO
==========

Devices:
========
GPU0:
	apiVersion         = 1.3.274
	vendorID           = 0x8086
	deviceID           = 0x46a6
	deviceType         = PHYSICAL_DEVICE_TYPE_INTEGRATED_GPU
	deviceName         = Intel(R) Iris(R) Xe Graphics (ADL GT2)
	driverName         = Intel open-source Mesa driver
	driverInfo         = Mesa 25.2.8-0ubuntu0.24.04.2
GPU1:
	apiVersion         = 1.4.312
	vendorID           = 0x10de
	deviceID           = 0x2204
	deviceType         = PHYSICAL_DEVICE_TYPE_DISCRETE_GPU
	deviceName         = NVIDIA GeForce RTX 3090
	driverName         = NVIDIA
	driverInfo         = 580.178.04
GPU2:
	apiVersion         = 1.4.318
	vendorID           = 0x10005
	deviceID           = 0x0000
	deviceType         = PHYSICAL_DEVICE_TYPE_CPU
	deviceName         = llvmpipe (LLVM 20.1.2, 256 bits)
	driverName         = llvmpipe
	driverInfo         = Mesa 25.2.8-0ubuntu0.24.04.2 (LLVM 20.1.2)
"""


def devices():
    return accelerators.classify(accelerators.parse_devices(LIST_DEVICES), accelerators.parse_vulkaninfo(VULKANINFO))


def test_llama_devices_are_parsed_with_their_memory():
    parsed = accelerators.parse_devices(LIST_DEVICES)
    assert [d['name'] for d in parsed] == ['Vulkan0', 'Vulkan1']
    assert parsed[0]['total'] == 24576 * MiB and parsed[0]['free'] == 23800 * MiB


def test_no_device_listed_means_none():
    assert accelerators.parse_devices('Available devices:\n  (none)\n') == []


def test_each_device_gets_its_type_and_driver():
    found = {d['name']: d for d in devices()}
    assert found['Vulkan0']['type'] == 'discrete' and found['Vulkan0']['driver'] == 'NVIDIA'
    assert found['Vulkan1']['type'] == 'integrated'


def test_discrete_gpus_are_preferred_over_an_integrated_one():
    assert [d['name'] for d in accelerators.select('auto', devices())] == ['Vulkan0']


def test_an_integrated_gpu_is_used_when_it_is_the_only_one():
    only_igpu = [d for d in devices() if d['type'] == 'integrated']
    assert [d['name'] for d in accelerators.select('auto', only_igpu)] == ['Vulkan1']


def test_without_gpus_auto_runs_on_the_cpu_and_gpu_mode_refuses():
    assert accelerators.select('auto', []) == []
    with pytest.raises(ValueError, match='no usable GPU'):
        accelerators.select('gpu', [])


def test_cpu_mode_never_offloads():
    assert accelerators.select('cpu', devices()) == []


def test_an_unrecognised_device_is_still_used():
    """A driver whose name vulkaninfo reports differently must not lose its GPU."""
    unknown = accelerators.classify(accelerators.parse_devices(LIST_DEVICES), [])
    assert [d['name'] for d in accelerators.select('auto', unknown)] == ['Vulkan0', 'Vulkan1']


def test_only_discrete_memory_is_added_to_ram():
    chosen = devices()
    assert accelerators.extra_memory(chosen) == 23800 * MiB - accelerators.VRAM_MARGIN
    assert accelerators.extra_memory([d for d in chosen if d['type'] == 'integrated']) == 0


def test_runtime_arguments_name_the_devices_or_disable_offload():
    assert accelerators.device_arguments([]) == ['--device', 'none', '--n-gpu-layers', '0']
    assert accelerators.device_arguments(devices()[:1]) == ['--device', 'Vulkan0', '--n-gpu-layers', 'auto']


def test_software_renderers_are_never_offered(monkeypatch, environment):
    listed = LIST_DEVICES + '  Vulkan2: llvmpipe (LLVM 20.1.2, 256 bits) (7000 MiB, 7000 MiB free)\n'
    monkeypatch.setattr(accelerators, '_run', lambda command, timeout: listed if '--list-devices' in command else VULKANINFO)
    assert 'Vulkan2' not in [d['name'] for d in accelerators.detect()]
    monkeypatch.setenv('AIOS_GPU_ALLOW_SOFTWARE', '1')
    software = [d for d in accelerators.detect() if d['name'] == 'Vulkan2']
    assert software and accelerators.select('auto', software) == software


def test_detection_is_cached_for_cheap_readers(monkeypatch, environment):
    monkeypatch.setattr(accelerators, '_run', lambda command, timeout: LIST_DEVICES if '--list-devices' in command else VULKANINFO)
    accelerators.detect()
    assert [d['name'] for d in accelerators.cached()] == ['Vulkan0', 'Vulkan1']


def test_a_machine_without_the_tools_simply_has_no_gpu(monkeypatch, environment):
    monkeypatch.setattr(accelerators, 'LLAMA', '/nonexistent/llama-server')
    monkeypatch.setattr(accelerators, 'VULKANINFO', '/nonexistent/vulkaninfo')
    assert accelerators.detect() == []


def test_offloaded_layers_are_read_from_the_log():
    log = 'load_tensors: offloading 28 repeating layers to GPU\nload_tensors: offloaded 29/29 layers to GPU\n'
    assert accelerators.offload_summary(log) == {'layers': 29, 'total': 29}
    assert accelerators.offload_summary('no gpu here') is None


def test_the_command_line_always_states_where_the_model_runs(environment, discovered):
    key = discovered()
    environment.execute('INSERT INTO installed_models(id,path,sha256,installed_at,state,gguf) VALUES (?,?,?,?,?,?)',
                        (key, '/m.gguf', 'a' * 64, environment.now(), 'INSTALLED', '{}'))
    row = {'model_id': key, 'port': 8090}
    cpu = arguments(row, RuntimeConfig())
    assert cpu[cpu.index('--device') + 1] == 'none'
    gpu = arguments(row, RuntimeConfig(), devices()[:1])
    assert gpu[gpu.index('--device') + 1] == 'Vulkan0'


def test_gpu_memory_lets_a_model_larger_than_ram_start(environment, discovered, monkeypatch):
    from aios import runtime

    class Memory:
        total = 8 * GIB
        available = 6 * GIB
    monkeypatch.setattr(runtime.psutil, 'virtual_memory', lambda: Memory)
    monkeypatch.setattr(runtime, 'compatibility', lambda metadata, context: {'estimated_ram': 10 * GIB})
    big = {'size': 12 * GIB, 'format': 'GGUF', 'gguf': {}}
    with pytest.raises(ValueError, match='larger than the RAM'):
        fit_context('k', 'm', '{"context": 4096}', RuntimeConfig(context=4096), big)
    assert fit_context('k', 'm', '{"context": 4096}', RuntimeConfig(context=4096), big, extra=16 * GIB).context == 4096


def hardware(accelerators_list):
    return {'ram': {'total': 8 * GIB, 'available': 6 * GIB}, 'physical_cores': 8, 'model_storage': {'free': 500 * GIB},
            'isa': ['avx2'], 'numa_nodes': {}, 'accelerators': accelerators_list}


def test_the_catalogue_rates_a_model_that_fits_the_gpu():
    big = {'size': 14 * GIB, 'format': 'GGUF'}
    assert compatibility(big, hw=hardware([]))['classification'] == 'NOT_RECOMMENDED'
    rated = compatibility(big, hw=hardware(devices()))
    assert rated['classification'] != 'NOT_RECOMMENDED'
    assert any('NVIDIA GeForce RTX 3090' in reason for reason in rated['reasons'])


def test_an_integrated_gpu_adds_no_memory_to_the_rating():
    big = {'size': 14 * GIB, 'format': 'GGUF'}
    igpu = [d for d in devices() if d['type'] == 'integrated']
    assert compatibility(big, hw=hardware(igpu))['classification'] == 'NOT_RECOMMENDED'


def pci(tmp_path, name, *cards):
    for index, (vendor, klass, device) in enumerate(cards):
        folder = tmp_path / name / f'0000:0{index}:00.0'
        folder.mkdir(parents=True)
        (folder / 'vendor').write_text(vendor + '\n')
        (folder / 'class').write_text(klass + '\n')
        (folder / 'device').write_text(device + '\n')
    return tmp_path / name


@pytest.mark.parametrize('cards,flavour', [
    ([('0x10de', '0x030000', '0x2b85')], 'open'),                                   # RTX 5090: open modules only
    ([('0x10de', '0x030000', '0x2204')], 'open'),                                   # RTX 3090
    ([('0x10de', '0x030200', '0x1b38')], 'closed'),                                 # Tesla P40
    ([('0x10de', '0x030200', '0x1b38'), ('0x10de', '0x030000', '0x2204')], 'closed'),
    ([('0x8086', '0x030000', '0x46a6'), ('0x10de', '0x040300', '0x10fa')], 'none'),  # Intel GPU, NVIDIA audio only
    ([('0x1002', '0x030000', '0x73bf')], 'none'),                                   # AMD card: Mesa, no NVIDIA driver
])
def test_the_nvidia_module_flavour_follows_the_cards(tmp_path, cards, flavour):
    result = subprocess.run([ROOT / 'scripts/nvidia-driver.sh', '--decide'], capture_output=True, text=True,
                            env={'AIOS_PCI_DEVICES': str(pci(tmp_path, 'pci', *cards)), 'PATH': '/usr/bin:/bin'})
    assert result.stdout.strip() == flavour


def test_the_image_builds_and_ships_gpu_support():
    runtime = (ROOT / 'scripts/build-runtime.sh').read_text()
    assert '-DGGML_VULKAN=ON' in runtime and '-DGGML_BACKEND_DL=ON' in runtime and '-DGGML_CPU_ALL_VARIANTS=ON' in runtime
    image = (ROOT / 'scripts/build-image.sh').read_text()
    for package in ('mesa-vulkan-drivers', 'vulkan-tools', 'libnvidia-gl-$NVIDIA_BRANCH', 'nvidia-utils-$NVIDIA_BRANCH'):
        assert package in image, package
    assert 'drivers/nvidia/open' in image and 'drivers/nvidia/closed' in image
    assert 'systemctl enable aios-nvidia-driver' in image and 'apt-mark manual binutils' in image
    # A backend that is not next to the executable is silently never loaded.
    assert 'lib/libggml-vulkan.so; do' in image and "grep -q 'Vulkan0:'" in image


def test_the_kernel_is_new_enough_for_current_gpus():
    """The GA 6.8 kernel does not know Battlemage, Lunar Lake or Radeon RX 9000."""
    assert 'linux-image-generic-hwe-24.04' in (ROOT / 'scripts/prepare-rootfs.sh').read_text()
    image = (ROOT / 'scripts/build-image.sh').read_text()
    assert 'linux-image-generic-hwe-24.04' in image and 'open-generic-hwe-24.04' in image
    assert 'amdgpu si_support=1 cik_support=1' in image


def test_nvidia_cards_the_driver_dropped_still_get_a_chance():
    script = (ROOT / 'scripts/nvidia-driver.sh').read_text()
    assert script.index('modprobe nvidia ') < script.index('modprobe nouveau')


def test_the_runtime_services_may_open_gpu_devices():
    for unit in ('aios-runtime-manager.service', 'aios-hardware-profiler.service'):
        text = (ROOT / 'systemd' / unit).read_text()
        assert 'render' in text and 'video' in text and 'PrivateDevices=yes' not in text
    assert 'aios-nvidia-driver.service' in (ROOT / 'systemd/aios-runtime-manager.service').read_text()


def test_acceleration_is_part_of_the_runtime_configuration():
    assert RuntimeConfig().acceleration == 'auto'
    with pytest.raises(ValueError):
        RuntimeConfig(acceleration='cuda')
    assert json.loads(RuntimeConfig(acceleration='cpu').model_dump_json())['acceleration'] == 'cpu'


class FakeProcess:
    returncode = 0

    def poll(self):
        return None

    def send_signal(self, _):
        pass

    def terminate(self):
        pass

    def kill(self):
        pass

    def wait(self, *_):
        return 0


def run_manager(environment, monkeypatch, acceleration, found, gpu_fails):
    """Run the real runtime manager loop against a stubbed llama-server start."""
    import asyncio
    from aios import runtime
    key = environment.uid()
    model = environment.uid()
    environment.execute('INSERT INTO discovered_models VALUES (?,?,?,?,?,?,?)',
                        (model, environment.one('SELECT id FROM repositories LIMIT 1')['id'], model, 'r',
                         environment.encode({'size': GIB, 'format': 'GGUF'}), 1, 1))
    environment.execute('INSERT INTO installed_models(id,path,sha256,installed_at,state,gguf) VALUES (?,?,?,?,?,?)',
                        (model, '/m.gguf', 'a' * 64, 1, 'INSTALLED', json.dumps({'metadata': {}})))
    environment.execute("INSERT INTO runtime_instances(id,model_id,state,port,config,desired) VALUES (?,?,'STOPPED',8090,?,'RUNNING')",
                        (key, model, RuntimeConfig(acceleration=acceleration).model_dump_json()))
    attempts = []

    async def start(key_, row, config, chosen, children, note=None, metadata=None):
        attempts.append({'devices': [d['name'] for d in chosen], 'note': note})
        if chosen and gpu_fails:
            children[key_] = FakeProcess()
            raise ValueError('llama-server could not load the model: ErrorOutOfDeviceMemory')
        children[key_] = FakeProcess()
        environment.execute("UPDATE runtime_instances SET state='RUNNING' WHERE id=?", (key_,))

    monkeypatch.setattr(runtime, 'start_runtime', start)
    monkeypatch.setattr(runtime, 'fit_context', lambda key_, model_id, stored, config, metadata, extra=0: config)
    monkeypatch.setattr(runtime.accelerators, 'detect', lambda: found)

    async def scenario():
        task = asyncio.create_task(runtime.manager())
        # The manager resets every desired state when it starts, as after a reboot.
        await asyncio.sleep(0.05)
        environment.execute("UPDATE runtime_instances SET desired='RUNNING' WHERE id=?", (key,))
        for _ in range(100):
            await asyncio.sleep(0.05)
            state = environment.one('SELECT state FROM runtime_instances WHERE id=?', (key,))['state']
            if state in ('RUNNING', 'FAILED'):
                break
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        return state
    return asyncio.run(scenario()), attempts


def test_a_gpu_that_fails_to_load_falls_back_to_the_cpu(environment, monkeypatch):
    state, attempts = run_manager(environment, monkeypatch, 'auto', devices(), gpu_fails=True)
    assert state == 'RUNNING'
    assert attempts[0]['devices'] == ['Vulkan0'] and attempts[1]['devices'] == []
    assert 'GPU start failed, running on the CPU' in attempts[1]['note']
    assert environment.one("SELECT count(*) AS n FROM audit_events WHERE payload LIKE '%runtime_gpu_fallback%'")['n'] == 1


def test_gpu_only_mode_reports_the_failure_instead_of_falling_back(environment, monkeypatch):
    state, attempts = run_manager(environment, monkeypatch, 'gpu', devices(), gpu_fails=True)
    assert state == 'FAILED' and len(attempts) == 1


def test_a_machine_without_gpu_starts_once_on_the_cpu(environment, monkeypatch):
    state, attempts = run_manager(environment, monkeypatch, 'auto', [], gpu_fails=True)
    assert state == 'RUNNING' and attempts == [{'devices': [], 'note': None}]


def test_the_nvidia_driver_outcome_reaches_the_hardware_page(environment):
    assert accelerators.nvidia_driver() is None
    path = accelerators.DATA / 'system/nvidia-driver.json'
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{"flavour": "closed", "kernel": "k", "status": "failed", "detail": "x", "checked_at": 1}')
    assert accelerators.nvidia_driver()['status'] == 'failed'


def test_a_probe_from_a_previous_boot_is_not_trusted(environment, monkeypatch):
    from aios import hardware
    monkeypatch.setattr(accelerators, '_run', lambda command, timeout: LIST_DEVICES if '--list-devices' in command else VULKANINFO)
    path = accelerators.DATA / accelerators.CACHE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({'devices': [], 'detected_at': hardware.psutil.boot_time() - 5}))
    assert [d['name'] for d in hardware.gpu_devices()] == ['Vulkan0', 'Vulkan1']


def test_the_control_plane_never_probes_gpus(environment, monkeypatch):
    """It has no access to GPU devices: a probe from there would find none and
    overwrite the profiler's list."""
    from aios import hardware
    monkeypatch.setattr(accelerators, 'detect', lambda: pytest.fail('the control plane probed the GPUs'))
    monkeypatch.setattr(hardware, 'command', lambda *args, **kwargs: '')
    assert hardware.profile(probe=False)['accelerators'] == []
    assert 'profile(probe=False)' in (ROOT / 'backend/aios/app.py').read_text()
