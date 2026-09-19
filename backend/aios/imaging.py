"""Image generation: stable-diffusion.cpp as a second, separate runtime.

Language models and diffusion models share almost nothing but the appliance they
run on: different engine, different files (a diffusion model is often four), and
different limits. They do share the rules AIOS cares about — one process owned by
the runtime manager, devices chosen explicitly, memory estimated before the start,
and the CPU as the fallback that always works.
"""
import json
import os
import re
import subprocess

from .core import DATA, one

BINARY = os.environ.get('AIOS_SD_SERVER', '/opt/aios/imaging/bin/sd-server')
LIB = '/opt/aios/imaging/lib'
PORT = 8091
MiB = 1024 ** 2
GIB = 1024 ** 3

# Files a diffusion model is made of, and the option that loads each one.
COMPONENT_FLAGS = {'vae': '--vae', 'clip_l': '--clip_l', 'clip_g': '--clip_g', 't5xxl': '--t5xxl',
                   'llm': '--llm', 'tokenizer': '--tokenizer', 'taesd': '--taesd'}
# Model families and what they need to produce a reasonable picture by default.
FAMILIES = {
    'sd1': {'width': 512, 'height': 512, 'steps': 20, 'cfg_scale': 7.0, 'sampling': 'euler_a', 'split': False},
    'sdxl': {'width': 1024, 'height': 1024, 'steps': 20, 'cfg_scale': 7.0, 'sampling': 'euler_a', 'split': False},
    'sdxl_turbo': {'width': 512, 'height': 512, 'steps': 4, 'cfg_scale': 1.0, 'sampling': 'euler', 'split': False},
    'sd3': {'width': 1024, 'height': 1024, 'steps': 28, 'cfg_scale': 4.5, 'sampling': 'euler', 'split': True},
    'flux': {'width': 1024, 'height': 1024, 'steps': 4, 'cfg_scale': 1.0, 'sampling': 'euler', 'split': True},
    'qwen_image': {'width': 1024, 'height': 1024, 'steps': 20, 'cfg_scale': 2.5, 'sampling': 'euler', 'split': True},
}
DEFAULTS = FAMILIES['sd1']


def is_image_model(metadata):
    """Whether a catalogue entry is a diffusion model rather than a language model."""
    return (metadata or {}).get('kind') == 'image'


def family(metadata):
    return FAMILIES.get((metadata or {}).get('family'), DEFAULTS)


def components(metadata):
    """The extra files this model needs, as recorded by discovery."""
    found = (metadata or {}).get('components')
    return [c for c in found if isinstance(c, dict) and c.get('role') in COMPONENT_FLAGS] if isinstance(found, list) else []


def component_path(model_id, role, filename):
    """Where a component of an installed model lives. The suffix is kept so the
    engine can tell a GGUF apart from a safetensors file."""
    suffix = '.gguf' if str(filename).lower().endswith('.gguf') else '.safetensors'
    return DATA / 'models' / f'{model_id}.{role}{suffix}'


def model_path(model_id, filename):
    suffix = '.gguf' if str(filename).lower().endswith('.gguf') else '.safetensors'
    return DATA / 'models' / f'{model_id}{suffix}'


def estimated_memory(metadata):
    """Weights of every file plus the working buffers of a diffusion pass. The
    engine holds the whole model while it denoises, so nothing is memory-mapped
    away as it is for a language model."""
    total = int((metadata or {}).get('size') or 0) + sum(int(c.get('size') or 0) for c in components(metadata))
    return total + 1536 * MiB


def _run(command, timeout=60):
    try:
        environment = {**os.environ, 'LD_LIBRARY_PATH': LIB}
        return subprocess.run(command, capture_output=True, text=True, timeout=timeout, env=environment).stdout
    except (OSError, subprocess.SubprocessError):
        return ''


_DEVICE_LINE = re.compile(r'^(?P<name>\S+)\t(?P<description>.+)$')


def engine_devices():
    """Device names this engine accepts in --backend, with their descriptions."""
    return [match.groupdict() for match in map(_DEVICE_LINE.match, _run([BINARY, '--list-devices'], 60).splitlines()) if match]


def backend_argument(devices, listed=None):
    """The --backend value for the devices AIOS chose. The engine names devices
    itself, so the choice is matched by description and never by index alone."""
    if not devices:
        return 'cpu'
    listed = engine_devices() if listed is None else listed
    for device in devices:
        for entry in listed:
            if device.get('description') and device['description'] in entry['description']:
                return entry['name']
    for entry in listed:
        if not entry['name'].startswith('cpu'):
            return entry['name']
    return 'cpu'


def arguments(row, config, devices=(), metadata=None, listed=None):
    """The sd-server command line for an installed diffusion model."""
    model = one('SELECT * FROM installed_models WHERE id=?', (row['model_id'],))
    metadata = metadata if metadata is not None else json.loads(one('SELECT metadata FROM discovered_models WHERE id=?', (row['model_id'],))['metadata'])
    defaults = family(metadata)
    cmd = [BINARY, '--listen-ip', '127.0.0.1', '--listen-port', str(row.get('port') or PORT)]
    # A split model is loaded as a diffusion transformer plus its own encoders and
    # VAE; an all-in-one checkpoint carries them inside the same file.
    cmd += ['--diffusion-model' if defaults['split'] else '--model', model['path']]
    for component in components(metadata):
        path = component_path(row['model_id'], component['role'], component['filename'])
        if path.exists():
            cmd += [COMPONENT_FLAGS[component['role']], str(path)]
    threads = config.threads or 0
    if threads:
        cmd += ['--threads', str(threads)]
    # The OpenAI-compatible route carries only a prompt and a size, so everything
    # else a family needs must be the process default.
    cmd += ['--width', str(defaults['width']), '--height', str(defaults['height']), '--steps', str(defaults['steps']),
            '--cfg-scale', str(defaults['cfg_scale']), '--sampling-method', defaults['sampling']]
    backend = backend_argument(list(devices), listed)
    cmd += ['--backend', backend]
    if backend != 'cpu':
        # Weights live in RAM and are streamed to the GPU as each stage needs them:
        # a card with less memory than the model still runs it.
        free = min((int(d.get('free') or 0) for d in devices), default=0)
        if free and free < estimated_memory(metadata):
            cmd += ['--offload-to-cpu']
        cmd += ['--diffusion-fa']
    cmd += ['--mmap']
    return cmd


def generation_defaults(metadata):
    """What the portal and Open WebUI should ask for by default."""
    defaults = family(metadata)
    return {'width': defaults['width'], 'height': defaults['height'], 'steps': defaults['steps'],
            'cfg_scale': defaults['cfg_scale'], 'sampling_method': defaults['sampling']}
