"""Speech to text: whisper.cpp as a third, separate runtime.

Same rules as the other two engines — one process owned by the runtime manager,
devices chosen explicitly, memory estimated before the start, and the CPU as the
fallback that always works — with its own prefix so the three ggml builds never
load each other's backends.
"""
import os
import re

from .core import DATA

BINARY = os.environ.get('AIOS_WHISPER_SERVER', '/opt/aios/voice/bin/whisper-server')
LIB = '/opt/aios/voice/lib'
PORT = 8092
MiB = 1024 ** 2
GIB = 1024 ** 3
# Whisper keeps the whole model in memory and needs room for the mel spectrogram,
# the encoder state and the decoders; measured around half a gigabyte on the
# small models and a little more on the large ones.
WORKING_MEMORY = 512 * MiB
EXTENSION = '.bin'


def is_speech_model(metadata):
    """Whether a catalogue entry is a speech model rather than a language or image one."""
    return (metadata or {}).get('kind') == 'speech'


def model_path(model_id, filename=''):
    """Where the weights of a speech model are stored."""
    return DATA / 'models' / (model_id + EXTENSION)


def estimated_memory(metadata):
    """Weights plus the engine's working memory."""
    return int(metadata.get('size') or 0) + WORKING_MEMORY


def language_of(metadata):
    """The language a model is limited to, or None when it is multilingual.
    Whisper's ".en" builds transcribe English only and are faster for it."""
    return 'en' if re.search(r'\.en\b|-en-', str(metadata.get('filename') or '')) else None


def arguments(row, config, devices, metadata):
    """The whisper-server command line for this model on these devices."""
    threads = config.threads or (os.cpu_count() or 2)
    cmd = [BINARY, '--model', str(model_path(row['model_id'])), '--host', '127.0.0.1', '--port', str(PORT),
           '--threads', str(threads), '--inference-path', '/inference',
           # Browsers record WebM or MP4, not WAV; the server converts with the
           # ffmpeg already in the image.
           # Its default temporary directory is the working directory, which the
           # service cannot write to: transcoding goes where the runtime logs go.
           '--convert', '--tmp-dir', str(DATA / 'runtime'), '--no-timestamps']
    language = language_of(metadata or {})
    if language:
        cmd += ['--language', language]
    if not devices:
        cmd.append('--no-gpu')
    return cmd


def environment(devices):
    """Which GPU whisper.cpp may use. It takes no device option of its own, so the
    choice travels in the variable the Vulkan backend reads; the names are the
    ones llama.cpp reports (Vulkan0, Vulkan1...)."""
    values = {'LD_LIBRARY_PATH': LIB}
    indices = [match.group(1) for match in (re.search(r'Vulkan(\d+)', device.get('name', '')) for device in devices) if match]
    if indices:
        values['GGML_VK_VISIBLE_DEVICES'] = ','.join(indices)
    return values
