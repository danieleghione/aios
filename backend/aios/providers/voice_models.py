"""Voice models: a GGUF backbone and its codec, spoken by llama-tts."""
import json
import logging
import re

from .. import providers as shared


# Voice models: a GGUF backbone and the codec (mmproj) that turns its codes
# into sound, spoken by llama-tts. Each entry names a repository published for
# llama.cpp; the backbone quantisations are offered, each with the codec.
VOICE_MODELS = [{'repo': 'ggml-org/Qwen3-TTS-12Hz-1.7B-Base-GGUF', 'name': 'Qwen3-TTS 1.7B', 'family': 'qwen3-tts',
                 'architecture': 'qwen3tts', 'parameters': 1_700_000_000, 'license': 'apache-2.0'}]
VOICE_EXCLUDED = re.compile(r'(?:^|[-_.])(?:bf16|f16|f32)\.gguf$', re.I)


def voice_artifacts(entry, revision, files, license_name, released):
    """Catalogue rows for one voice repository: every quantised backbone, each
    carrying the codec it needs."""
    codecs = sorted((f for name, f in files.items() if name.lower().startswith('mmproj') and name.endswith('.gguf') and f['size']),
                    key=lambda f: ('q8_0' not in f['name'].lower(), f['size']))
    if not codecs:
        return []
    codec = codecs[0]
    rows = []
    for name, item in files.items():
        if not name.endswith('.gguf') or name.lower().startswith('mmproj') or VOICE_EXCLUDED.search(name) or not item['size']:
            continue
        quantisation = shared.QUANTISATION.search(name)
        label = quantisation.group(0).strip('-_.').upper() if quantisation else 'GGUF'
        rows.append({'model_id': entry['repo'], 'display_name': f"{entry['name']} · {label}", 'filename': name, 'url': item['url'],
                     'size': item['size'], 'sha256': item.get('sha256'), 'revision': revision, 'format': 'GGUF', 'kind': 'voice',
                     'family': entry['family'], 'architecture': entry['architecture'], 'author': entry['repo'].split('/')[0],
                     'quantization': label, 'parameter_count': entry.get('parameters'),
                     'license': license_name if license_name not in (None, '', 'unknown') else entry.get('license', 'unknown'),
                     'release_date': released,
                     'components': [{'role': 'codec', 'filename': codec['name'], 'url': codec['url'], 'size': codec['size'],
                                     'sha256': codec.get('sha256'), 'repository': entry['repo']}]})
    return sorted(rows, key=lambda row: row['size'])


async def voice(repo, emit=None):
    """Voice (text-to-speech) models. Metadata only, like every catalogue."""
    config = json.loads(repo['config'])
    result = []
    for entry in (config.get('models') or VOICE_MODELS)[:20]:
        if isinstance(entry, str):
            entry = {'repo': entry, 'name': entry.split('/')[-1].replace('-GGUF', ''), 'family': 'voice', 'architecture': 'unknown'}
        try:
            revision, files, license_name, released = await shared.hugging_face_files(repo, entry['repo'])
        except shared.RepositoryError as exc:
            logging.warning('voice repository %s: skipping %s: %s', repo.get('name'), entry['repo'], exc)
            continue
        rows = voice_artifacts(entry, revision, files, license_name, released)
        if emit and rows:
            rows = await emit(rows)
        result.extend(rows)
    return result
