"""Image models: diffusion checkpoints and the components they need."""
import json
import logging
import re

from .. import providers as shared


# Diffusion models are not language models: they are published as one checkpoint
# or as a transformer plus its text encoders and VAE, sometimes in another
# repository. Each entry below names what to offer and what it needs to run; a
# component with several sources uses the first one that can be read without
# credentials. Checked against Hugging Face in September 2026.
IMAGE_FAMILIES = [
    {'name': 'Stable Diffusion 1.5', 'family': 'sd1', 'repo': 'Comfy-Org/stable-diffusion-v1-5-archive',
     'files': ['v1-5-pruned-emaonly-fp16.safetensors', 'v1-5-pruned-emaonly.safetensors'], 'components': []},
    {'name': 'SDXL Turbo', 'family': 'sdxl_turbo', 'repo': 'stabilityai/sdxl-turbo',
     'files': ['sd_xl_turbo_1.0_fp16.safetensors'], 'components': []},
    {'name': 'FLUX.1 schnell', 'family': 'flux', 'repo': 'city96/FLUX.1-schnell-gguf',
     'files': ['flux1-schnell-Q4_K_S.gguf', 'flux1-schnell-Q5_K_S.gguf', 'flux1-schnell-Q8_0.gguf'],
     'components': [
         {'role': 'clip_l', 'sources': [('comfyanonymous/flux_text_encoders', 'clip_l.safetensors')]},
         {'role': 't5xxl', 'sources': [('comfyanonymous/flux_text_encoders', 't5xxl_fp8_e4m3fn.safetensors')]},
         {'role': 'vae', 'sources': [('Comfy-Org/Lumina_Image_2.0_Repackaged', 'split_files/vae/ae.safetensors'),
                                     ('black-forest-labs/FLUX.1-schnell', 'ae.safetensors')]}]},
]
IMAGE_EXTENSIONS = ('.safetensors', '.gguf')
IMAGE_MAX_BYTES = 128 * 1024 ** 3


def image_artifact(entry, model_repo, revision, item, components):
    """One catalogue row for a diffusion model file, with the components it needs."""
    name = item['name']
    if '..' in name.split('/') or '\\' in name or len(name) > 512 or not name.lower().endswith(IMAGE_EXTENSIONS):
        return None
    if not isinstance(item['size'], int) or not 0 < item['size'] <= IMAGE_MAX_BYTES:
        return None
    quantisation = re.search(r'(?:IQ|Q|BF|F)[0-9][A-Za-z0-9_]*', name.rsplit('/', 1)[-1], re.I)
    return {'model_id': model_repo, 'display_name': f"{entry['name']} / {name.rsplit('/', 1)[-1]}", 'filename': name,
            'url': item['url'], 'size': item['size'], 'sha256': item.get('sha256'), 'revision': revision,
            'format': 'GGUF' if name.lower().endswith('.gguf') else 'SAFETENSORS', 'kind': 'image',
            'family': entry['family'], 'architecture': entry['family'], 'author': model_repo.split('/')[0],
            'quantization': quantisation.group(0) if quantisation else 'FP16', 'parameter_count': None,
            'license': entry.get('license', 'unknown'), 'components': components, 'release_date': entry.get('release_date')}


async def diffusion(repo, emit=None):
    """Image models. Metadata only: nothing is downloaded until someone installs one."""
    config = json.loads(repo['config'])
    entries = config.get('families') or IMAGE_FAMILIES
    listings, result = {}, []

    async def listing(model_repo):
        if model_repo not in listings:
            listings[model_repo] = await shared.hugging_face_files(repo, model_repo)
        return listings[model_repo]

    for entry in entries[:40]:
        try:
            revision, files, license_name, released = await listing(entry['repo'])
        except shared.RepositoryError as exc:
            logging.warning('image repository %s: skipping %s: %s', repo.get('name'), entry['repo'], exc)
            continue
        components = []
        missing = None
        for component in entry.get('components', []):
            resolved = None
            for source_repo, source_file in component['sources']:
                try:
                    _, source_files, _, _ = await listing(source_repo)
                except shared.RepositoryError:
                    continue
                found = source_files.get(source_file)
                if found and found['size']:
                    resolved = {'role': component['role'], 'filename': source_file, 'url': found['url'],
                                'size': found['size'], 'sha256': found.get('sha256'), 'repository': source_repo}
                    break
            if not resolved:
                missing = component['role']
                break
            components.append(resolved)
        if missing:
            # Without every component the model cannot run, so it is not offered.
            logging.warning('image repository %s: %s needs a %s nobody published openly', repo.get('name'), entry['name'], missing)
            continue
        found_rows = []
        for filename in entry['files']:
            item = files.get(filename)
            if not item:
                continue
            row = image_artifact({**entry, 'license': license_name, 'release_date': released}, entry['repo'], revision, item, components)
            if row:
                found_rows.append(row)
        if emit and found_rows:
            found_rows = await emit(found_rows)
        result.extend(found_rows)
    return result
