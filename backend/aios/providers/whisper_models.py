"""Speech models: whisper.cpp's own ggml builds."""
import json
import re

from .. import providers as shared


# Speech models: whisper.cpp's own ggml builds, published as one file each.
# Several sizes and quantisations of the same weights, so the catalogue groups
# them like any other model and the operator picks what the machine can run.
SPEECH_REPO = 'ggerganov/whisper.cpp'
# Superseded releases and the experimental diarisation builds are left out.
SPEECH_EXCLUDED = re.compile(r'tdrz|large-v1|large-v2', re.I)
SPEECH_MAX_BYTES = 8 * 1024 ** 3


def speech_name(filename):
    """"Whisper large-v3 turbo (Q5_0)" from "ggml-large-v3-turbo-q5_0.bin"."""
    stem = filename[len('ggml-'):-len('.bin')]
    quantisation = re.search(r'-(q[0-9][^-]*)$', stem, re.I)
    if quantisation:
        stem = stem[:quantisation.start()]
    english = stem.endswith('.en')
    name = 'Whisper ' + stem[:-3].replace('-', ' ') if english else 'Whisper ' + stem.replace('-', ' ')
    if english:
        name += ' (English)'
    return name + (f' · {quantisation.group(1).upper()}' if quantisation else '')


def speech_artifact(model_repo, revision, item, license_name, released):
    """One catalogue row for a whisper model file."""
    name = item['name']
    if not name.startswith('ggml-') or not name.endswith('.bin') or SPEECH_EXCLUDED.search(name):
        return None
    if not isinstance(item['size'], int) or not 0 < item['size'] <= SPEECH_MAX_BYTES:
        return None
    quantisation = re.search(r'-(q[0-9][A-Za-z0-9_]*)\.bin$', name, re.I)
    return {'model_id': model_repo, 'display_name': speech_name(name), 'filename': name, 'url': item['url'],
            'size': item['size'], 'sha256': item.get('sha256'), 'revision': revision, 'format': 'GGML',
            'kind': 'speech', 'family': 'whisper', 'architecture': 'whisper', 'author': model_repo.split('/')[0],
            'quantization': quantisation.group(1).upper() if quantisation else 'F16', 'parameter_count': None,
            'license': license_name, 'release_date': released}


async def speech(repo, emit=None):
    """Speech models. Metadata only: nothing is downloaded until someone installs one."""
    config = json.loads(repo['config'])
    model_repo = config.get('model_repo') or SPEECH_REPO
    revision, files, license_name, released = await shared.hugging_face_files(repo, model_repo)
    rows = [row for row in (speech_artifact(model_repo, revision, item, license_name, released) for item in files.values()) if row]
    rows.sort(key=lambda row: row['size'])
    return await emit(rows) if emit else rows
