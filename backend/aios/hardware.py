import hashlib
import json
import os
import platform
import shutil
import socket
import subprocess
import time
from pathlib import Path
import psutil
from . import accelerators, imaging
from .core import DATA, atomic_write, encode, execute, now, uid

GIB = 1024 ** 3

def command(args):
    try:
        return subprocess.check_output(args, timeout=10, text=True, stderr=subprocess.DEVNULL).strip()
    except (OSError, subprocess.SubprocessError):
        return ''

def gpu_devices(max_age=600):
    """Probing starts llama.cpp and initialises every GPU driver, so the minute
    profiler reuses a recent probe; the runtime probes afresh before each start."""
    try:
        detected = json.loads((DATA / accelerators.CACHE).read_text())['detected_at']
        # A probe from before this boot may predate a GPU or its driver.
        age = time.time() - detected if detected > psutil.boot_time() else max_age
    except (OSError, ValueError, KeyError, TypeError):
        age = max_age
    return accelerators.cached() if age < max_age else accelerators.detect()


def profile(probe=True):
    """probe=False reuses the last GPU probe: only the profiler and the runtime
    manager may open GPU devices, and a probe without that access finds none."""
    cpu = {}
    for line in Path('/proc/cpuinfo').read_text().split('\n\n')[0].splitlines():
        if ':' in line:
            key, value = line.split(':', 1)
            cpu[key.strip()] = value.strip()
    memory = psutil.virtual_memory()
    meminfo = dict(line.split(':', 1) for line in Path('/proc/meminfo').read_text().splitlines())
    disks = []
    for part in psutil.disk_partitions():
        try:
            disks.append({'device': part.device, 'mount': part.mountpoint, 'filesystem': part.fstype, **psutil.disk_usage(part.mountpoint)._asdict()})
        except OSError:
            continue
    temps = {key: [x._asdict() for x in values] for key, values in psutil.sensors_temperatures().items()}
    topology = [dict(x.split(':', 1) for x in block.splitlines() if ':' in x) for block in Path('/proc/cpuinfo').read_text().split('\n\n') if block.strip()]
    sockets = {next((v.strip() for k, v in item.items() if k.strip() == 'physical id'), '0') for item in topology}
    flags = cpu.get('flags', '').split()
    result = {'hostname': socket.gethostname(), 'aios_version': '1.5.1', 'kernel': platform.release(), 'vendor': cpu.get('vendor_id'), 'model': cpu.get('model name'), 'sockets': len(sockets), 'physical_cores': psutil.cpu_count(logical=False), 'logical_threads': psutil.cpu_count(), 'frequency_mhz': psutil.cpu_freq()._asdict() if psutil.cpu_freq() else None, 'isa': [f for f in flags if any(f.startswith(s) for s in ('avx', 'fma', 'amx', 'sse', 'vnni'))], 'numa_nodes': {p.name: (p / 'cpulist').read_text().strip() for p in Path('/sys/devices/system/node').glob('node[0-9]*')}, 'cache': command(['lscpu', '--caches', '--json']), 'ram': memory._asdict(), 'hugepages': {k: v.strip() for k, v in meminfo.items() if 'Huge' in k}, 'hugepage_sizes': {p.name: {f.name: f.read_text().strip() for f in p.iterdir() if f.name in ('nr_hugepages', 'free_hugepages', 'surplus_hugepages')} for p in Path('/sys/kernel/mm/hugepages').glob('hugepages-*')}, 'disks': disks, 'model_storage': shutil.disk_usage(DATA)._asdict(), 'network': {k: [x._asdict() for x in v] for k, v in psutil.net_if_addrs().items()}, 'network_counters': psutil.net_io_counters()._asdict(), 'temperature': temps, 'virtualization': command(['systemd-detect-virt']), 'load': os.getloadavg(), 'cpu_percent': psutil.cpu_percent(interval=0.1), 'runtime_version': command([os.environ.get('AIOS_LLAMA', '/opt/aios/runtime/bin/llama-server'), '--version']), 'accelerators': gpu_devices() if probe else accelerators.cached(), 'nvidia_driver': accelerators.nvidia_driver(), 'updated_at': now()}
    atomic_write(DATA / 'system/hardware-profile.json', encode(result), 0o644)
    return result

def benchmark():
    binary = Path('/opt/aios/bin/aios-benchmark')
    if binary.exists():
        result = json.loads(subprocess.check_output([str(binary), str(psutil.cpu_count(logical=False) or 1)], timeout=90, text=True))
        execute('INSERT INTO benchmark_results VALUES (?,?,?)', (uid(), now(), encode(result)))
        return result
    # Bounded 64 MiB memory-copy and SHA256 workload, no model or disk writes.
    block = bytearray(os.urandom(32 * 1024 * 1024))
    target = bytearray(len(block))
    start = time.perf_counter()
    for _ in range(32):
        target[:] = block
    bandwidth = len(block) * 32 / (time.perf_counter() - start)
    start = time.perf_counter()
    for _ in range(8):
        hashlib.sha256(block).digest()
    result = {'memory_copy_bytes_sec': bandwidth, 'sha256_bytes_sec': len(block) * 8 / (time.perf_counter() - start), 'recommended_threads': max(1, psutil.cpu_count(logical=False) or 1), 'workload': '32MiB memory copy and SHA256, calibration only; not a token/s prediction'}
    execute('INSERT INTO benchmark_results VALUES (?,?,?)', (uid(), now(), encode(result)))
    return result

_STATIC: dict = {}


def resources():
    """What a compatibility estimate needs, without profile()'s cost: that one
    spawns llama-server and lscpu, sleeps 100 ms and writes a file, which turned
    every catalogue request into a multi-process job. Stable facts are read once;
    RAM and storage are read fresh because they change."""
    if not _STATIC:
        flags = next((line.split(':', 1)[1].split() for line in Path('/proc/cpuinfo').read_text().splitlines()
                      if line.startswith('flags')), [])
        _STATIC.update(
            physical_cores=psutil.cpu_count(logical=False),
            isa=[f for f in flags if any(f.startswith(s) for s in ('avx', 'fma', 'amx', 'sse', 'vnni'))],
            numa_nodes={n.name: (n / 'cpulist').read_text().strip() for n in Path('/sys/devices/system/node').glob('node[0-9]*')})
    return {**_STATIC, 'ram': psutil.virtual_memory()._asdict(), 'model_storage': shutil.disk_usage(DATA)._asdict(),
            'accelerators': accelerators.cached()}


def cached_profile():
    """The last full inventory written by the hardware profiler timer, refreshed
    with the values that move. Falls back to a live profile only if none exists."""
    try:
        stored = json.loads((DATA / 'system/hardware-profile.json').read_text())
    except (OSError, ValueError):
        return profile(probe=False)
    return {**stored, **resources(), 'load': os.getloadavg(), 'cpu_percent': psutil.cpu_percent(interval=None)}


def image_compatibility(metadata, hw):
    """A diffusion model is judged on different facts: every file is held in
    memory for the whole denoising pass, there is no KV cache and no context."""
    size = int(metadata.get('size') or 0)
    parts = imaging.components(metadata)
    required = imaging.estimated_memory(metadata)
    gpus = accelerators.select('auto', hw.get('accelerators') or [])
    vram = sum(max(0, d['total'] - accelerators.VRAM_MARGIN) for d in gpus if d['type'] != 'integrated')
    usable = max(0, hw['ram']['total'] - 1536 * 1024 ** 2)
    total_files = size + sum(int(c.get('size') or 0) for c in parts)
    reasons = [f'Diffusion model {size / GIB:.2f} GiB' + (f' plus {len(parts)} component file(s) {sum(int(c.get("size") or 0) for c in parts) / GIB:.2f} GiB' if parts else ''),
               'About 1.5 GiB for the working buffers of one image']
    if gpus:
        reasons.append('GPU acceleration: ' + ', '.join(d['description'] for d in gpus) + (f' (+{vram / GIB:.1f} GiB of GPU memory)' if vram else ' (shares system memory)'))
    else:
        reasons.append('No GPU: generating one image takes minutes on the CPU')
    rating = 'OPTIMAL'
    if not size:
        rating = 'INCOMPATIBLE'
        reasons.append('Unknown size')
    elif required > usable + vram:
        rating = 'NOT_RECOMMENDED'
        reasons.append('The model and its buffers do not fit in memory')
    elif required > hw['ram']['available'] * .9 + vram:
        rating = 'LIMITED'
        reasons.append('Insufficient currently available memory; stop other workloads')
    elif required > (usable + vram) * .6 or not gpus:
        rating = 'COMPATIBLE'
    if total_files > hw['model_storage']['free']:
        rating = 'INCOMPATIBLE'
        reasons.append('Insufficient storage')
    return {'classification': rating, 'reasons': reasons, 'estimated_ram': required, 'kv_bytes': 0, 'context': 0,
            'architecture': metadata.get('family', 'unknown'), 'kind': 'image'}


def resident_limit(hw):
    """Largest weights file that can stay entirely in memory next to the appliance's
    own services (about 1.25 GiB measured on a 3 GiB VM with Open WebUI running)."""
    return hw['ram']['total'] - 1024 ** 3


def trained_context(metadata):
    """The context length the model itself declares, or None when unknown."""
    raw = metadata.get('gguf') or {}
    architecture = raw.get('general.architecture')
    value = raw.get(f'{architecture}.context_length') if architecture else None
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else None


def compatibility(metadata, context=4096, hw=None):
    hw = hw or resources()
    if imaging.is_image_model(metadata):
        return image_compatibility(metadata, hw)
    size = int(metadata.get('size') or 0)
    raw = metadata.get('gguf') or {}
    arch = raw.get('general.architecture', metadata.get('architecture', 'unknown'))

    def dimension(name):
        # Installed-model metadata records arrays as {'array_type', 'count'};
        # only plain integers are usable as a size.
        value = raw.get(f'{arch}.{name}')
        return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else None

    layers, embedding = dimension('block_count'), dimension('embedding_length')
    heads = dimension('attention.head_count')
    if layers and embedding and heads:
        kv_heads = dimension('attention.head_count_kv') or heads
        head_dim = dimension('attention.key_length') or embedding // heads
        kv = context * layers * 2 * head_dim * kv_heads * 2
        basis = 'from the GGUF header'
    else:
        # No header read: scale with the weights. About 25 KiB per token for each
        # GiB of a 4-bit file matches current grouped-query models, where the old
        # fixed 32-layer, 32-KV-head shape claimed 2 GiB for a 0.5B model.
        kv = int(context * 25 * 1024 * (size / GIB))
        basis = 'approximated from file size'
    # llama.cpp maps the weights from the file, so the kernel can evict pages it
    # is not using and read them back: only the KV cache and compute buffers are
    # memory that must exist. Counting the whole file plus 10% plus a flat 512 MiB
    # refused a 1.57 GiB model that, measured on a 3 GiB VM at context 4096, loaded
    # in 18 s, answered, used no swap and left 519 MiB free. Require half of the
    # weights to be backed by memory, which still refuses models far larger than
    # the machine, where eviction would turn every token into disk reads.
    # A vision projector is read into memory whole, beside the language weights.
    projector = metadata.get('projector') if isinstance(metadata.get('projector'), dict) else {}
    projector_size = projector.get('size') if isinstance(projector.get('size'), int) and not isinstance(projector.get('size'), bool) else 0
    required = int(size * 0.5 + kv + projector_size + 192 * 1024 ** 2)
    gpus = accelerators.select('auto', hw.get('accelerators') or [])
    # Discrete GPU memory takes layers off the RAM; an integrated GPU shares it.
    vram = sum(max(0, d['total'] - accelerators.VRAM_MARGIN) for d in gpus if d['type'] != 'integrated')
    usable = max(0, hw['ram']['total'] - 1536 * 1024 ** 2) + vram
    reasons = [f'Weights {size / GIB:.2f} GiB; estimated KV {kv / GIB:.2f} GiB at context {context} ({basis})', '1.5 GiB reserved for OS/control plane']
    if projector_size:
        reasons.append(f'Image input: vision projector {projector_size / GIB:.2f} GiB')
    if gpus:
        names = ', '.join(d['description'] for d in gpus)
        reasons.append(f'GPU acceleration: {names}' + (f' (+{vram / GIB:.1f} GiB of GPU memory)' if vram else ' (shares system memory)'))
    rating = 'OPTIMAL'
    if not size or metadata.get('format', 'GGUF') != 'GGUF':
        rating = 'INCOMPATIBLE'
        reasons.append('Unknown size or unsupported format')
    elif size + projector_size > resident_limit(hw) + vram:
        # Every token reads every layer: weights that cannot all sit in RAM beside
        # the services turn inference into continuous disk reads.
        rating = 'NOT_RECOMMENDED'
        reasons.append('Model weights are larger than the memory this machine can give them')
    elif required > usable:
        rating = 'NOT_RECOMMENDED'
        reasons.append('Estimated working set exceeds physical RAM budget')
    elif required > hw['ram']['available'] * .9 + vram:
        rating = 'LIMITED'
        reasons.append('Insufficient currently available memory; stop other workloads')
    elif required > usable * .6 or (hw['physical_cores'] or 1) < 4:
        rating = 'COMPATIBLE'
    if size > hw['model_storage']['free']:
        rating = 'INCOMPATIBLE'
        reasons.append('Insufficient storage')
    if 'avx2' not in hw['isa'] and not gpus:
        reasons.append('Portable CPU backend selected; lower throughput expected')
    if len(hw['numa_nodes']) > 1:
        reasons.append('Multiple NUMA nodes: interleave or isolate policy recommended')
    return {'classification': rating, 'reasons': reasons, 'estimated_ram': required, 'kv_bytes': kv, 'context': context, 'architecture': arch}
