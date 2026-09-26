"""Separate runtime owner process: API writes desired state, manager owns children."""
import asyncio
import json
import sqlite3
import logging
import os
import signal
import subprocess
import time
from dataclasses import dataclass
from typing import Callable, Literal
import httpx
import psutil
from pydantic import BaseModel, Field
from .core import DATA, audit, execute, now, one, projector_path, rows, setting
from .gguf import DRAFT_MESSAGE, draft_reason, not_a_model
from . import accelerators, alerts, imaging, speech, voice
from .hardware import compatibility, trained_context

class RuntimeConfig(BaseModel):
    profile: Literal['AUTO', 'LOW_MEMORY', 'BALANCED', 'MAX_PERFORMANCE', 'CUSTOM'] = 'AUTO'
    # Models are offered the window they were trained for, up to this ceiling;
    # fit_context lowers it to what the machine's memory really allows.
    context: int = Field(default=101024, ge=128, le=1048576)
    threads: int = Field(default=0, ge=0, le=1024)
    batch: int = Field(default=512, ge=1, le=8192)
    parallel: int = Field(default=1, ge=1, le=16)
    affinity: list[int] = Field(default_factory=list, max_length=1024)
    numa: Literal['disabled', 'distribute', 'isolate', 'numactl'] = 'disabled'
    hugepages: bool = False
    mlock: bool = False
    embeddings: bool = False
    autostart: bool = False
    timeout: int = Field(default=300, ge=10, le=3600)
    # Thinking tokens: AUTO_REASONING picks from the context, -1 is unlimited, 0 turns thinking off.
    reasoning_budget: int = Field(default=-2, ge=-2, le=131072)
    # auto: the best GPU the machine has, otherwise the CPU; gpu: refuse to start
    # without one; cpu: never offload.
    acceleration: Literal['auto', 'gpu', 'cpu'] = 'auto'


AUTO_REASONING = -2
# Written in English because that is what reasoning models think in.
REASONING_BUDGET_MESSAGE = '\n\nI have reasoned enough; I will now give the final answer directly.\n'


def reasoning_budget(config, context):
    """A reasoning model can keep thinking until its context is full and then stop
    with no answer at all; small quantised ones do this even on trivial questions.
    Unless told otherwise, thinking gets half of what one request may use, so the
    answer always has room. Models that do not think are unaffected."""
    if config.reasoning_budget != AUTO_REASONING:
        return config.reasoning_budget
    return max(256, context // max(1, config.parallel) // 2)


AUTO_SLOTS = 4


def parallel_slots(config, context):
    """How many requests the model answers at once. With one slot, a second user
    of the chat waits for the first answer to end. In AUTO the model gets four
    slots over a shared KV cache, which costs no extra context memory; a small
    window stays at one slot, where sharing would starve a long answer."""
    if config.parallel > 1 or config.profile != 'AUTO':
        return config.parallel
    return AUTO_SLOTS if context >= 8192 else 1


def arguments(row, config, devices=()):
    physical = psutil.cpu_count(logical=False) or 1
    calibration = one('SELECT result FROM benchmark_results ORDER BY created_at DESC LIMIT 1')
    recommended = json.loads(calibration['result']).get('recommended_threads', physical) if calibration else physical
    threads = config.threads or (recommended if config.profile == 'AUTO' else physical)
    context, batch = config.context, config.batch
    if config.profile == 'LOW_MEMORY':
        context, batch = min(context, 2048), min(batch, 128)
    if config.profile == 'MAX_PERFORMANCE' and not config.threads:
        threads = physical
    available = os.sched_getaffinity(0)
    if config.affinity and not set(config.affinity).issubset(available):
        raise ValueError('CPU affinity includes unavailable CPU IDs')
    binary = os.environ.get('AIOS_LLAMA', '/opt/aios/runtime/bin/llama-server')
    model = one('SELECT * FROM installed_models WHERE id=?', (row['model_id'],))
    slots = parallel_slots(config, context)
    cmd = [binary, '--model', model['path'], '--alias', row['model_id'], '--host', '127.0.0.1', '--port', str(row['port']), '--ctx-size', str(context), '--threads', str(threads), '--batch-size', str(batch), '--parallel', str(slots), '--timeout', str(config.timeout), '--metrics', '--no-webui']
    # libllama reports how it loaded the model (devices, buffers, "offloaded N/M
    # layers to GPU") at trace level since the 2026 logging rework; at the default
    # verbosity the Runtime page lost the GPU layer count.
    cmd += ['--log-verbosity', '4']
    if slots > 1:
        # One KV buffer shared by the slots: the memory of a single context, and
        # any request may still use all of it. Split per slot, four users would
        # each get a quarter of the window.
        cmd.append('--kv-unified')
    cmd += accelerators.device_arguments(list(devices))
    projector = projector_path(row['model_id'])
    if projector.exists():
        # Without its projector a vision model answers text but refuses images.
        cmd += ['--mmproj', str(projector)]
        if not devices:
            # The projector picks a GPU on its own unless told not to.
            cmd += ['--no-mmproj-offload']
    budget = reasoning_budget(config, context)
    cmd += ['--reasoning-budget', str(budget)]
    if budget > 0:
        cmd += ['--reasoning-budget-message', REASONING_BUDGET_MESSAGE]
    if config.numa != 'disabled':
        cmd += ['--numa', config.numa]
    if config.mlock:
        cmd += ['--mlock']
    if config.embeddings:
        cmd += ['--embedding', '--pooling', 'mean']
    if config.affinity:
        cmd = ['taskset', '-c', ','.join(map(str, config.affinity)), *cmd]
    # llama.cpp uses mmap; THP is kernel-advised, explicit hugetlb is not supported.
    if config.hugepages:
        raise ValueError('Pinned llama.cpp does not support explicit HugeTLB allocation; use host THP policy')
    return cmd

# Run each engine through a one-line shell that raises its own OOM score and then
# execs it (same PID): if memory runs out, the kernel kills the model being loaded
# rather than the manager, which used to take every loaded model down with it.
EXPENDABLE = ['/bin/sh', '-c', 'echo 800 > /proc/self/oom_score_adj 2>/dev/null; exec "$0" "$@"']


def load_failure(log_path, returncode=None):
    """Report llama.cpp's own reason for refusing a model. Without this the
    operator only sees "consult runtime log" and has to go read the file to
    learn that, say, the architecture is unknown to the bundled runtime."""
    if returncode == -signal.SIGKILL:
        return 'The engine was stopped by the kernel while loading: the machine ran out of memory. Stop other models or choose a smaller one'
    generic = 'llama-server exited during load; consult runtime log'
    try:
        tail = log_path.read_text(errors='replace')[-16000:]
    except OSError:
        return generic
    errors = [line.split(' E ', 1)[-1].strip() for line in tail.splitlines() if ' E ' in line]
    if not errors:
        return generic
    # The first load error is the cause; later ones repeat or just announce exit.
    cause = next((e for e in errors if 'error loading model' in e or 'failed to load' in e), errors[0])
    if 'key not found in model' in cause or 'unknown model architecture' in cause:
        cause += ' — this architecture is not supported by the bundled llama.cpp build'
    return 'llama-server could not load the model: ' + cause[:400]

MIN_CONTEXT = 1024
TEXT_PORT = 8090


def model_metadata(model_id):
    """What the catalogue knows about an installed model, with its kind."""
    row = one('SELECT d.metadata,i.gguf FROM discovered_models d JOIN installed_models i ON i.id=d.id WHERE d.id=?', (model_id,))
    metadata = json.loads(row['metadata'])
    stored = json.loads(row['gguf'] or '{}')
    if kind_of(metadata) == 'text':
        metadata['gguf'] = stored.get('metadata', {})
    return metadata


@dataclass(frozen=True)
class Engine:
    """One inference engine: what it runs, where it listens, how it starts and
    how it says it is ready. Every place that depends on the kind of a model
    reads it from here."""
    kind: str
    name: str                     # in messages: "another <name> model is loaded"
    port: int                     # loopback port, one per engine
    health: str                   # path polled until the engine is ready
    ready: Callable[[int], bool]  # the status code that means ready
    arguments: Callable           # (row, config, devices, metadata) -> argv
    environment: Callable         # devices -> environment variables
    detect: Callable | None       # metadata -> True for this engine's models; None for the default


def _text_arguments(row, config, devices, metadata):
    return arguments(row, config, devices)


# Checked in order; a model none of them claims is a language model.
ENGINES = {
    'image': Engine('image', 'image', imaging.PORT, 'v1/models', lambda status: status == 200, imaging.arguments,
                    lambda devices: {'LD_LIBRARY_PATH': imaging.LIB}, imaging.is_image_model),
    # whisper-server answers 400 to a GET on its inference path: it is listening.
    'speech': Engine('speech', 'speech', speech.PORT, 'inference', lambda status: status < 500, speech.arguments,
                     speech.environment, speech.is_speech_model),
    'voice': Engine('voice', 'voice', voice.PORT, 'health', lambda status: status == 200, voice.arguments,
                    voice.environment, voice.is_voice_model),
    'text': Engine('text', 'language', TEXT_PORT, 'health', lambda status: status == 200, _text_arguments,
                   lambda devices: accelerators.runtime_environment(), None),
}
KIND_NAMES = {kind: engine.name for kind, engine in ENGINES.items()}
PORTS = {kind: engine.port for kind, engine in ENGINES.items()}


def display_name(model_id):
    """A model's name for messages, its identifier when the catalogue has none."""
    try:
        return model_metadata(model_id).get('display_name', model_id)
    except (TypeError, ValueError, KeyError):
        return model_id


def kind_of(metadata):
    """The engine a model belongs to: 'image', 'speech', or 'text' for a language model."""
    return next((kind for kind, engine in ENGINES.items() if engine.detect and engine.detect(metadata)), 'text')


def same_kind_conflict(active_model_ids, kind):
    """Why this model cannot be loaded now, or None. The three engines use
    different memory and different devices, so one of each may be loaded at the
    same time; two of a kind would compete for the same hardware."""
    for other in active_model_ids:
        if kind_of(model_metadata(other)) == kind:
            return f'Another {KIND_NAMES[kind]} model is already loaded; stop it before loading this one'
    return None


def runtime_port(metadata):
    """Each engine has its own loopback port, so all three can be loaded at once."""
    return PORTS[kind_of(metadata)]


async def start_runtime(key, row, config, devices, children, note=None, metadata=None):
    """Launch the engine this model needs and wait until it answers, recording
    which devices it uses. Language models answer /health when the weights are in
    memory; sd-server binds its port only after the model is loaded."""
    kind = kind_of(metadata or {})
    engine = ENGINES[kind]
    row = {**row, 'port': engine.port}
    cmd = engine.arguments(row, config, devices, metadata)
    # Each engine answers on the path it has: llama.cpp when the weights are in
    # memory, sd-server once it binds its port, whisper-server once it can load.
    health = f'http://127.0.0.1:{row["port"]}/' + engine.health
    execute("UPDATE runtime_instances SET state='STARTING',error=NULL WHERE id=?", (key,))
    start = time.monotonic()
    log_path = DATA / 'runtime' / (key + '.log')
    facts = {'devices': [{'name': d['name'], 'description': d['description'], 'type': d['type']} for d in devices],
             'note': note, 'log_offset': log_path.stat().st_size if log_path.exists() else 0}
    (DATA / 'runtime' / (key + '.accel.json')).write_text(json.dumps(facts))
    with log_path.open('ab') as log:
        # Each engine carries its own ggml: the image engine must not find the
        # language runtime's libraries first, and vice versa.
        environment = {**os.environ, **engine.environment(devices)}
        process = subprocess.Popen(EXPENDABLE + cmd, stdout=log, stderr=subprocess.STDOUT, start_new_session=True, env=environment)
    children[key] = process
    # The port depends on the engine, so the row must say where this model really listens.
    execute('UPDATE runtime_instances SET pid=?,started_at=?,port=? WHERE id=?', (process.pid, now(), row['port'], key))
    async with httpx.AsyncClient(timeout=2) as client:
        for _ in range(300):
            if process.poll() is not None:
                raise ValueError(load_failure(log_path, process.returncode))
            if one('SELECT desired FROM runtime_instances WHERE id=?', (key,))['desired'] != 'RUNNING':
                return
            try:
                response = await client.get(health)
                if engine.ready(response.status_code):
                    execute("UPDATE runtime_instances SET state='RUNNING' WHERE id=?", (key,))
                    alerts.resolve('runtime:' + row['model_id'])
                    audit('runtime-manager', 'runtime_start', row['model_id'],
                          {'load_seconds': time.monotonic() - start, 'devices': [d['description'] for d in devices]})
                    return
            except httpx.HTTPError:
                pass
            await asyncio.sleep(1)
    raise ValueError('Model load timeout')


def fit_context(key, model_id, stored, config, metadata, extra=0):
    """Make a model fit the memory there is. A context nobody chose is halved until
    the model fits, rather than refusing one that runs fine with a shorter window:
    that is what used to stop every model over about a gigabyte on a small VM. A
    context the operator set explicitly is honoured, and refused plainly if it
    cannot fit. The context actually used is recorded, so the UI shows the truth."""
    memory = psutil.virtual_memory()
    # A discrete GPU holds the layers it takes, so its free memory counts too.
    available = memory.available - 128 * 1024 ** 2 + extra
    size = int(metadata.get('size') or 0)
    capacity = memory.total - 1024 ** 3 + extra
    if size > capacity:
        where = 'the RAM and GPU memory' if extra else 'the RAM'
        raise ValueError(f'Model weights ({size // 2 ** 20} MiB) are larger than {where} this machine can hold beside its services '
                         f'({capacity // 2 ** 20} MiB); it would read from disk on every token')
    explicit = 'context' in json.loads(stored or '{}')
    context = config.context
    trained = trained_context(metadata)
    if not explicit and trained:
        # Asking for more than the model was trained on degrades its answers.
        context = min(context, trained)
    estimate = compatibility(metadata, context)
    while not explicit and context > MIN_CONTEXT and estimate['estimated_ram'] > available:
        context = max(MIN_CONTEXT, context // 2)
        estimate = compatibility(metadata, context)
    if estimate['estimated_ram'] > available:
        need, have = estimate['estimated_ram'] // 2 ** 20, max(0, available) // 2 ** 20
        advice = 'lower the context in the runtime settings or free memory' if explicit else 'the model is larger than the memory this machine has free'
        raise ValueError(f'Not enough RAM: about {need} MiB needed at context {context}, {have} MiB available; {advice}')
    if context != config.context:
        config = config.model_copy(update={'context': context})
        execute('UPDATE runtime_instances SET config=? WHERE id=?', (config.model_dump_json(), key))
        audit('runtime-manager', 'runtime_context_reduced', model_id, {'context': context, 'available_mib': available // 2 ** 20})
    return config


def initialize_runtime_state():
    execute("UPDATE runtime_instances SET state='STOPPED',pid=NULL,desired='STOPPED',busy=0")
    for row in rows('SELECT id,config FROM installed_models'):
        config = RuntimeConfig.model_validate_json(row['config'])
        if config.autostart:
            execute("UPDATE runtime_instances SET desired='RUNNING' WHERE model_id=?", (row['id'],))

def memory_needed(metadata, kind):
    """Memory the model needs to load: the engine's own estimate, or a text model
    at the smallest context the start would accept."""
    if kind == 'text':
        return compatibility(metadata, MIN_CONTEXT)['estimated_ram']
    return compatibility(metadata)['estimated_ram']


async def make_room(key, metadata, kind, children, extra=0):
    """One language and one image model may be loaded together, but on a small
    machine both do not fit: the chat asks for a title on the language model
    while the picture loads, and the kernel killed the whole runtime. Unload the
    model of the other kind first when memory is short."""
    needed = memory_needed(metadata, kind)
    if psutil.virtual_memory().available + extra >= needed:
        return
    for other, process in list(children.items()):
        if other == key or process.poll() is not None:
            continue
        row = one('SELECT model_id FROM runtime_instances WHERE id=?', (other,))
        process.send_signal(signal.SIGTERM)
        try:
            await asyncio.to_thread(process.wait, 20)
        except subprocess.TimeoutExpired:
            process.kill()
            await asyncio.to_thread(process.wait)
        children.pop(other)
        execute("UPDATE runtime_instances SET state='STOPPED',pid=NULL,desired='STOPPED' WHERE id=?", (other,))
        audit('runtime-manager', 'runtime_unloaded_for_memory', row['model_id'] if row else other,
              {'for': metadata.get('display_name'), 'needed_mib': needed // 2 ** 20})
    # The estimate is cautious (SD 1.5 loads in less than it claims): try anyway;
    # if the kernel stops the load, the reason is reported plainly.
    await asyncio.sleep(1)


def unload_idle(current=None):
    """Stop models nobody has used for the configured time, to give their memory
    back. A model answering a request, or one set to start with the appliance,
    stays; 0 minutes means never."""
    minutes = int(setting('idle_unload_minutes', 0) or 0)
    if minutes <= 0:
        return []
    limit = (current or now()) - minutes * 60
    unloaded = []
    for row in rows("SELECT r.id,r.model_id,r.last_used,r.started_at,r.busy,i.config FROM runtime_instances r "
                    "JOIN installed_models i ON i.id=r.model_id WHERE r.state='RUNNING' AND r.desired='RUNNING'"):
        if row['busy'] or json.loads(row['config'] or '{}').get('autostart'):
            continue
        if (row['last_used'] or row['started_at'] or 0) < limit:
            execute("UPDATE runtime_instances SET desired='STOPPED' WHERE id=?", (row['id'],))
            audit('runtime-manager', 'runtime_unloaded_idle', row['model_id'], {'idle_minutes': minutes})
            unloaded.append(row['model_id'])
    return unloaded


async def manager():
    children = {}
    initialize_runtime_state()
    idle_check = vram_check = 0.0
    try:
        while True:
            # A database error used to end this loop, and the finally below then
            # terminated every running llama-server: models stopped for no visible
            # reason. Skip this pass and try again instead.
            try:
                if time.monotonic() - idle_check > 30:
                    idle_check = time.monotonic()
                    unload_idle()
                if time.monotonic() - vram_check > 5:
                    vram_check = time.monotonic()
                    await asyncio.to_thread(accelerators.record_vram)
                for row in rows('SELECT * FROM runtime_instances'):
                    key = row['id']
                    process = children.get(key)
                    if process and (row['desired'] != 'RUNNING' or process.poll() is not None):
                        if process.poll() is None:
                            process.send_signal(signal.SIGTERM)
                            try:
                                await asyncio.to_thread(process.wait, 20)
                            except subprocess.TimeoutExpired:
                                process.kill()
                                await asyncio.to_thread(process.wait)
                        children.pop(key)
                        state = 'STOPPED' if row['desired'] != 'RUNNING' else 'FAILED'
                        execute('UPDATE runtime_instances SET state=?,pid=NULL,desired=? WHERE id=?', (state, 'RUNNING' if row['desired'] == 'RESTART' else 'STOPPED', key))
                        audit('runtime-manager', 'runtime_stop', row['model_id'], {'exit_code': process.returncode})
                        continue
                    if row['desired'] == 'RESTART' and not process:
                        execute("UPDATE runtime_instances SET desired='RUNNING' WHERE id=?", (key,))
                    if row['desired'] == 'RUNNING' and not process:
                        try:
                            config = RuntimeConfig.model_validate_json(row['config'])
                            metadata = model_metadata(row['model_id'])
                            kind = kind_of(metadata)
                            # One model of each kind: a language, an image and a
                            # speech model can be loaded together, two of a kind cannot.
                            active = [one('SELECT model_id FROM runtime_instances WHERE id=?', (other,))['model_id'] for other in children]
                            conflict = same_kind_conflict(active, kind)
                            if conflict:
                                raise ValueError(conflict)
                            if kind == 'text':
                                # Count the projector only if it is really there to be loaded.
                                projector = projector_path(row['model_id'])
                                metadata['projector'] = {'size': projector.stat().st_size} if projector.exists() else None
                            devices = accelerators.select(config.acceleration, await asyncio.to_thread(accelerators.detect))
                            await make_room(key, metadata, kind, children, accelerators.extra_memory(devices))
                            fallback = None
                            while True:
                                try:
                                    fitted = config if kind != 'text' else fit_context(key, row['model_id'], row['config'], config, metadata, accelerators.extra_memory(devices))
                                    await start_runtime(key, row, fitted, devices, children, fallback, metadata)
                                    break
                                except (ValueError, OSError) as exc:
                                    # A GPU that the driver cannot drive, or that runs out of
                                    # memory mid-load, must never cost the model: try once on
                                    # the CPU, as a machine without a GPU would run it.
                                    if not devices or config.acceleration != 'auto':
                                        raise
                                    process = children.pop(key, None)
                                    if process and process.poll() is None:
                                        process.kill()
                                        await asyncio.to_thread(process.wait)
                                    fallback = f'GPU start failed, running on the CPU: {exc}'
                                    audit('runtime-manager', 'runtime_gpu_fallback', row['model_id'], {'error': str(exc)})
                                    devices = []
                        except (ValueError, OSError) as exc:
                            if key in children:
                                children[key].terminate()
                            execute("UPDATE runtime_instances SET state='FAILED',error=?,desired='STOPPED' WHERE id=?", (str(exc), key))
                            audit('runtime-manager', 'runtime_failed', row['model_id'], {'error': str(exc)})
                            alerts.raise_alert('runtime:' + row['model_id'], 'ERROR', f'{display_name(row["model_id"])} could not start: {exc}')
            except sqlite3.Error as exc:
                logging.warning('runtime manager: database unavailable, retrying: %s', exc)
            await asyncio.sleep(1)
    finally:
        for process in children.values():
            if process.poll() is None:
                process.terminate()
        for process in children.values():
            try:
                await asyncio.to_thread(process.wait, 20)
            except subprocess.TimeoutExpired:
                process.kill()
        execute("UPDATE runtime_instances SET state='STOPPED',pid=NULL")

# Shared by the gateway and the model pages: what kind a model is, why a file
# cannot serve requests, and which devices its last start used.
def model_kind(model_id):
    """The kind of an installed model, 'text' when it cannot be determined."""
    try:
        return kind_of(model_metadata(model_id))
    except (TypeError, ValueError, KeyError):
        return 'text'


def unusable(model):
    """Why an installed file cannot serve requests, or None. Backstop for files
    installed before the download check refused them: vision projectors,
    importance matrices, adapters and speculative-decoding drafts all carry no
    complete model and fail inside the inference server with an obscure error."""
    stored = json.loads(model['gguf'] or '{}')
    metadata = stored.get('metadata', {})
    architecture = metadata.get('general.architecture')
    kind = not_a_model(metadata)
    if architecture == 'clip' or kind == 'mmproj':
        return 'This file is a multimodal projector (mmproj), not a language model; it cannot serve chat requests'
    if kind:
        return f'This file is a GGUF of type "{kind}", not a model; it cannot serve chat requests'
    if isinstance(architecture, str) and isinstance(stored.get('tensors'), int) and draft_reason(architecture, metadata, stored['tensors']):
        return DRAFT_MESSAGE
    return None


def runtime_acceleration(key):
    """Which devices the last start used and how many layers llama.cpp put on them."""
    try:
        facts = json.loads((DATA / 'runtime' / (key + '.accel.json')).read_text())
    except (OSError, ValueError):
        return None
    try:
        with (DATA / 'runtime' / (key + '.log')).open('rb') as log:
            log.seek(int(facts.get('log_offset') or 0))
            text = log.read(4 * 1024 * 1024).decode(errors='replace')
    except OSError:
        text = ''
    return {'devices': facts.get('devices', []), 'note': facts.get('note'), 'offload': accelerators.offload_summary(text)}
