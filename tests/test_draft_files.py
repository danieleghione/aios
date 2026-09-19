"""Speculative-decoding drafts sit next to the models they accelerate, under the
same architecture name, and cannot answer on their own."""
import json
import struct

import pytest
from aios import core, providers
from aios.gguf import DRAFT_MESSAGE, draft_reason, inspect_gguf


def text(value):
    value = value.encode()
    return struct.pack('<Q', len(value)) + value


def u32(key, value):
    return text(key) + struct.pack('<II', 4, value)


def gguf(arch, layers, tensor_names, extra=()):
    shape = [u32(f'{arch}.{key}', value) for key, value in
             (('embedding_length', 64), ('attention.head_count', 4), ('attention.head_count_kv', 4), ('context_length', 4096))]
    entries = [text('general.architecture') + struct.pack('<I', 8) + text(arch), u32(f'{arch}.block_count', layers), *shape, *extra]
    body = b'GGUF' + struct.pack('<IQQ', 3, len(tensor_names), len(entries)) + b''.join(entries)
    body += b''.join(text(name) + struct.pack('<IQIQ', 1, 1, 0, 32 * i) for i, name in enumerate(tensor_names))
    body += b'\0' * ((32 - len(body) % 32) % 32)
    return body + b'\0' * 32 * len(tensor_names)


def full_model(arch='qwen35', layers=4, nextn=0):
    names = ['token_embd.weight', 'output_norm.weight', 'output.weight']
    for block in range(layers):
        names += [f'blk.{block}.attn_norm.weight', f'blk.{block}.attn_q.weight', f'blk.{block}.ffn_up.weight']
    extra = [u32(f'{arch}.nextn_predict_layers', nextn)] if nextn else []
    return gguf(arch, layers, names, extra)


MTP_SHARED = gguf('qwen4exp', 49, [f'blk.48.nextn.{n}.weight' for n in ('eh_proj', 'enorm', 'hnorm', 'shared_head_norm')] + ['blk.48.attn_q.weight'])
MTP = gguf('deepseek4', 44, ['token_embd.weight', 'output.weight'] + [f'blk.43.t{i}.weight' for i in range(32)])
DFLASH = gguf('dflash', 5, ['fc.weight', 'output_norm.weight'] + [f'blk.{b}.t{i}.weight' for b in range(5) for i in range(11)],
              [text('dflash.target_layers') + struct.pack('<IIQ', 9, 5, 5) + struct.pack('<5I', 2, 17, 32, 47, 62)])


@pytest.mark.parametrize('blob', [MTP_SHARED, MTP, DFLASH], ids=['mtp-shared', 'mtp', 'dflash'])
def test_drafts_are_refused_at_install(tmp_path, blob):
    path = tmp_path / 'draft.gguf'
    path.write_bytes(blob)
    with pytest.raises(ValueError, match='speculative-decoding draft'):
        inspect_gguf(path)


@pytest.mark.parametrize('blob', [full_model(), full_model(layers=5, nextn=1)], ids=['plain', 'with-mtp-layer'])
def test_complete_models_install(tmp_path, blob):
    path = tmp_path / 'model.gguf'
    path.write_bytes(blob)
    assert inspect_gguf(path)['metadata']['general.architecture'] == 'qwen35'


def test_a_main_file_without_its_trailing_prediction_layer_still_installs(tmp_path):
    names = ['token_embd.weight', 'output.weight'] + [f'blk.{b}.t{i}.weight' for b in range(24) for i in range(3)]
    path = tmp_path / 'model.gguf'
    path.write_bytes(gguf('glm4moe', 25, names, [u32('glm4moe.nextn_predict_layers', 1)]))
    assert inspect_gguf(path)


@pytest.mark.parametrize('blob,draft', [(MTP_SHARED, True), (MTP, True), (DFLASH, True), (full_model(), False)])
def test_the_catalog_prefix_is_enough_to_tell(blob, draft):
    header = providers.header_dimensions(blob)
    assert bool(draft_reason(header['general.architecture'], header, header['tensor_count'])) is draft


@pytest.mark.asyncio
async def test_sync_lists_the_model_but_not_its_drafts(environment, monkeypatch):
    files = {'Qwen3.6-27B-Q4_K_M.gguf': full_model(layers=64), 'Qwen3.6-27B-Q8_0.gguf': full_model(layers=64),
             'mtp-Qwen3.6-27B-Q4_0.gguf': MTP, 'mtp-Qwen3.6-27B-Q8_0.gguf': MTP, 'dflash-Qwen3.6-27B-Q8_0.gguf': DFLASH}
    prefixes = []

    async def listing(url, repo, *args, **kwargs):
        if '/api/models?' in url:
            return [{'id': 'ggml-org/Qwen3.6-27B-GGUF', 'createdAt': '2026-04-22'}]
        return {'id': 'ggml-org/Qwen3.6-27B-GGUF', 'sha': 'rev', 'siblings': [
            {'rfilename': name, 'size': len(blob) + i} for i, (name, blob) in enumerate(files.items())]}

    async def prefix(url, repo, length=0):
        prefixes.append(url)
        return files[url.rsplit('/', 1)[-1]]
    monkeypatch.setattr(providers, 'request_json', listing)
    monkeypatch.setattr(providers, 'request_prefix', prefix)
    monkeypatch.setattr(providers, 'validate_url', lambda *args, **kwargs: None)
    repo = core.one("SELECT * FROM repositories WHERE name='Qwen (GGUF)'")
    core.execute('UPDATE repositories SET config=? WHERE id=?', (core.encode({'publishers': ['ggml-org'], 'search': 'Qwen'}), repo['id']))
    await providers.sync(repo['id'])
    listed = sorted(json.loads(r['metadata'])['filename'] for r in core.rows('SELECT metadata FROM discovered_models WHERE repository_id=?', (repo['id'],)))
    assert listed == ['Qwen3.6-27B-Q4_K_M.gguf', 'Qwen3.6-27B-Q8_0.gguf']
    # One read per file layout, not per quantisation.
    assert len(prefixes) == 3
    first = len(prefixes)
    await providers.sync(repo['id'])
    # The model's layout is remembered; only the unlisted drafts are looked at again.
    assert len(prefixes) - first == 2


def test_message_names_what_to_install_instead():
    assert 'main model' in DRAFT_MESSAGE


def typed(kind, arch=None):
    entries = [text('general.type') + struct.pack('<I', 8) + text(kind), u32('imatrix.chunk_count', 100)]
    if arch:
        entries.insert(0, text('general.architecture') + struct.pack('<I', 8) + text(arch))
    body = b'GGUF' + struct.pack('<IQQ', 3, 1, len(entries)) + b''.join(entries)
    body += text('blk.0.attn_q.weight.in_sum2') + struct.pack('<IQIQ', 1, 1, 0, 0)
    body += b'\0' * ((32 - len(body) % 32) % 32)
    return body + b'\0' * 32


@pytest.mark.parametrize('blob', [typed('imatrix'), typed('adapter', 'llama')], ids=['imatrix', 'lora'])
def test_non_model_gguf_types_are_refused_at_install(tmp_path, blob):
    path = tmp_path / 'other.gguf'
    path.write_bytes(blob)
    with pytest.raises(ValueError, match='not a model'):
        inspect_gguf(path)


@pytest.mark.parametrize('blob', [typed('imatrix'), typed('adapter', 'llama')], ids=['imatrix', 'lora'])
def test_the_catalog_recognises_non_model_types(blob):
    from aios.gguf import not_a_model
    assert not_a_model(providers.header_dimensions(blob))


def test_a_draft_installed_before_the_check_cannot_be_published_started_or_chatted(admin, environment, discovered):
    key = discovered()
    path = environment.DATA / 'models' / f'{key}.gguf'
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b'x')
    header = {'version': 3, 'tensors': 32, 'metadata': {'general.architecture': 'qwen4exp', 'qwen4exp.block_count': 49}}
    environment.execute('INSERT INTO installed_models(id,path,sha256,installed_at,state,published,gguf,config) VALUES (?,?,?,?,?,?,?,?)',
                        (key, str(path), 'a' * 64, environment.now(), 'PUBLISHED', 1, environment.encode(header), '{}'))
    published = admin.patch(f'/api/v1/aios/models/{key}', json={'published': True})
    assert published.status_code == 409 and 'draft' in published.json()['error']['message']
    started = admin.post(f'/api/v1/aios/runtime/{key}/start')
    assert started.status_code == 409 and 'draft' in started.json()['error']['message']
    from aios.app import unusable
    assert unusable(environment.one('SELECT * FROM installed_models WHERE id=?', (key,))) == DRAFT_MESSAGE
