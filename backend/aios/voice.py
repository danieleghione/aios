"""Text to speech: a fourth engine, on llama.cpp's own speech generation.

A voice model is a GGUF backbone plus a codec (an mmproj that turns the
backbone's codes into sound), both run by llama-tts from the language runtime's
build. llama-tts synthesises one text per run, so the engine the runtime
manager starts is voice_server.py: a small loopback service that answers the
OpenAI speech request, runs llama-tts for each part of the text and joins the
audio. Memory is used only while a text is being spoken.
"""
import os
import sys

from .core import DATA

BINARY = os.environ.get('AIOS_LLAMA_TTS', '/opt/aios/runtime/bin/llama-tts')
LIB = '/opt/aios/runtime/lib'
PORT = 8094
GIB = 1024 ** 3
# Measured with Qwen3-TTS 1.7B: llama.cpp keeps a repacked copy of about two
# thirds of the backbone for the CPU, and the codec's compute buffers take a
# little over a gigabyte beside the context.
REPACK_SHARE = 0.66
WORKING_MEMORY = int(1.3 * GIB)
# What Qwen3-TTS speaks; the request, the voice name or the runtime picks one.
LANGUAGES = ('en', 'it', 'de', 'fr', 'es', 'pt', 'zh', 'ja', 'ko', 'ru')
# A voice model without a reference recording invents a voice for every text,
# so each sentence of an answer came out in another voice. Every text is now
# spoken after one of these references: short samples the model itself
# generated (no real person's voice), under the names OpenAI clients and the
# chat already use. Described by their pitch, the one thing measured about them.
VOICES = {'alloy': 'Alloy · medium', 'echo': 'Echo · low', 'fable': 'Fable · medium-low',
          'onyx': 'Onyx · deep', 'nova': 'Nova · high', 'shimmer': 'Shimmer · bright'}
DEFAULT_VOICE = 'alloy'
VOICES_DIR = os.environ.get('AIOS_VOICES', '/opt/aios/app/config/voices')


def is_voice_model(metadata):
    """Whether a catalogue entry is a voice (text-to-speech) model."""
    return (metadata or {}).get('kind') == 'voice'


def is_voice_architecture(architecture):
    """GGUF architectures that generate speech rather than text: llama-server
    cannot answer a chat with them, so the language catalogue leaves them out."""
    return isinstance(architecture, str) and architecture.lower().endswith('tts')


def components(metadata):
    """The codec a voice model needs beside its backbone."""
    found = (metadata or {}).get('components')
    return [c for c in found if isinstance(c, dict) and c.get('role') == 'codec'] if isinstance(found, list) else []


def model_path(model_id, filename=''):
    return DATA / 'models' / (model_id + '.gguf')


def component_path(model_id, role='codec', filename=''):
    return DATA / 'models' / f'{model_id}.{role}.gguf'


def estimated_memory(metadata):
    """Backbone, its repacked copy, the codec and the working buffers."""
    size = int(metadata.get('size') or 0)
    codec = sum(int(c.get('size') or 0) for c in components(metadata))
    return int(size * (1 + REPACK_SHARE)) + codec + WORKING_MEMORY


def arguments(row, config, devices, metadata):
    """The voice service for this model on these devices."""
    if not os.path.isfile(BINARY):
        # An appliance updated in place keeps the language runtime it was
        # installed with; llama-tts arrived with the runtime of AIOS 1.11.
        raise ValueError('Text to speech needs llama-tts, part of the language runtime since AIOS 1.11: '
                         'install a runtime component release, or reinstall from a 1.11 image and restore a backup')
    threads = config.threads or (os.cpu_count() or 2)
    cmd = [sys.executable, '-m', 'aios.voice_server', '--model', str(model_path(row['model_id'])),
           '--codec', str(component_path(row['model_id'])), '--port', str(PORT), '--threads', str(threads),
           '--binary', BINARY, '--work', str(DATA / 'runtime'), '--voices', VOICES_DIR]
    if devices:
        cmd += ['--device', ','.join(d['name'] for d in devices)]
    return cmd


def environment(devices):
    # voice_server imports aios from the application; llama-tts finds its
    # libraries in the language runtime.
    from .accelerators import runtime_environment
    return {**runtime_environment(LIB), 'PYTHONPATH': os.environ.get('PYTHONPATH', '/opt/aios/app/backend')}
