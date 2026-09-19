"""GPU discovery and selection for the inference runtime.

AIOS never assumes a vendor. llama.cpp is built with its CPU variants and the
Vulkan backend, and Vulkan reaches every GPU family through the driver the
machine has: Mesa for Intel and AMD (integrated or discrete), the NVIDIA driver
for NVIDIA cards. What llama.cpp itself can use is the only source of truth, so
the device list comes from `llama-server --list-devices`; `vulkaninfo` only adds
whether each device is integrated or discrete, which decides how its memory is
counted.
"""
import json
import logging
import os
import re
import subprocess
import time

from .core import DATA

LLAMA = os.environ.get('AIOS_LLAMA', '/opt/aios/runtime/bin/llama-server')
VULKANINFO = os.environ.get('AIOS_VULKANINFO', 'vulkaninfo')
CACHE = 'system/accelerators.json'
MiB = 1024 ** 2
# Left free on a discrete card for the driver, the compute buffers and the display.
VRAM_MARGIN = 512 * MiB

_DEVICE_LINE = re.compile(r'^\s*(?P<name>[A-Za-z]+\d+):\s*(?P<description>.+?)\s*\((?P<total>\d+) MiB,\s*(?P<free>\d+) MiB free\)\s*$')
_TYPES = {
    'PHYSICAL_DEVICE_TYPE_DISCRETE_GPU': 'discrete',
    'PHYSICAL_DEVICE_TYPE_INTEGRATED_GPU': 'integrated',
    'PHYSICAL_DEVICE_TYPE_VIRTUAL_GPU': 'virtual',
    'PHYSICAL_DEVICE_TYPE_CPU': 'software',
}


def _environment():
    env = dict(os.environ)
    env['LD_LIBRARY_PATH'] = '/opt/aios/runtime/lib'
    return env


def parse_devices(output):
    """Devices llama.cpp can offload to, from `llama-server --list-devices`."""
    devices = []
    for line in output.splitlines():
        match = _DEVICE_LINE.match(line)
        if match:
            devices.append({'name': match['name'], 'description': match['description'],
                            'total': int(match['total']) * MiB, 'free': int(match['free']) * MiB})
    return devices


def parse_vulkaninfo(output):
    """Physical devices from `vulkaninfo --summary`, in enumeration order."""
    devices, current = [], None
    for line in output.splitlines():
        if re.match(r'^GPU\d+:\s*$', line.strip()):
            current = {}
            devices.append(current)
            continue
        if current is None or '=' not in line:
            continue
        key, _, value = line.partition('=')
        current[key.strip()] = value.strip()
    return [{'name': d.get('deviceName', ''), 'type': _TYPES.get(d.get('deviceType', ''), 'unknown'),
             'vendor': d.get('vendorID', ''), 'device': d.get('deviceID', ''),
             'driver': d.get('driverName', ''), 'driver_info': d.get('driverInfo', '')} for d in devices]


def classify(devices, physical):
    """Attach type and driver to each llama.cpp device by matching names."""
    result = []
    for device in devices:
        match = next((p for p in physical if p['name'] and p['name'] in device['description']), None)
        result.append({**device, 'type': match['type'] if match else 'unknown',
                       'driver': match['driver'] if match else '', 'driver_info': match['driver_info'] if match else ''})
    return result


def _run(command, timeout):
    try:
        return subprocess.run(command, capture_output=True, text=True, timeout=timeout, env=_environment()).stdout
    except (OSError, subprocess.SubprocessError) as exc:
        logging.info('accelerator probe %s unavailable: %s', command[0], exc)
        return ''


def detect():
    """Probe now. Software renderers (llvmpipe) are never offered as a GPU: the
    CPU backend is faster than emulating a GPU on that same CPU. Setting
    AIOS_GPU_ALLOW_SOFTWARE=1 lets tests exercise the GPU path without one."""
    devices = parse_devices(_run([LLAMA, '--list-devices'], 60))
    physical = parse_vulkaninfo(_run([VULKANINFO, '--summary'], 30))
    found = classify(devices, physical)
    if os.environ.get('AIOS_GPU_ALLOW_SOFTWARE') != '1':
        found = [d for d in found if d['type'] != 'software']
    else:
        # Treated like an integrated GPU: it renders in system RAM, as one would.
        found = [{**d, 'type': 'integrated'} if d['type'] == 'software' else d for d in found]
    snapshot = {'devices': found, 'detected_at': time.time()}
    try:
        path = DATA / CACHE
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(snapshot))
    except OSError:
        pass
    return found


def cached():
    """The last probe, without starting any process."""
    try:
        return json.loads((DATA / CACHE).read_text()).get('devices', [])
    except (OSError, ValueError):
        return []


def nvidia_driver():
    """What aios-nvidia-driver did at boot; None on machines without an NVIDIA GPU."""
    try:
        return json.loads((DATA / 'system/nvidia-driver.json').read_text())
    except (OSError, ValueError):
        return None


def select(mode, devices):
    """Devices a runtime should use.

    auto: every discrete GPU when there is one — an integrated GPU would only slow
    a split across devices — otherwise the integrated GPU, otherwise the CPU.
    gpu: the same choice, but the start is refused when nothing is available.
    cpu: never offload."""
    if mode == 'cpu':
        return []
    discrete = [d for d in devices if d['type'] in ('discrete', 'unknown')]
    chosen = discrete or [d for d in devices if d['type'] == 'integrated']
    if mode == 'gpu' and not chosen:
        raise ValueError('GPU acceleration requested, but no usable GPU was found; choose Automatic or CPU')
    return chosen


def extra_memory(devices):
    """Memory the chosen devices add beyond system RAM. An integrated GPU shares
    the system RAM it would report, so only discrete cards add any."""
    return sum(max(0, d['free'] - VRAM_MARGIN) for d in devices if d['type'] in ('discrete', 'unknown'))


def device_arguments(devices):
    """llama-server options for the chosen devices. With devices, llama.cpp
    places as many layers as fit on them and keeps the rest in RAM."""
    if not devices:
        return ['--device', 'none', '--n-gpu-layers', '0']
    return ['--device', ','.join(d['name'] for d in devices), '--n-gpu-layers', 'auto']


_OFFLOADED = re.compile(r'offloaded (\d+)/(\d+) layers to GPU')


def offload_summary(log_text):
    """How much of the model the last load put on a GPU, from the llama.cpp log."""
    matches = _OFFLOADED.findall(log_text)
    if not matches:
        return None
    done, total = matches[-1]
    return {'layers': int(done), 'total': int(total)}
