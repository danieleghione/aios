"""RAM estimates must come from the model's real shape, read from a small GGUF
prefix, instead of assuming a large legacy model for everything."""
import struct

import pytest

from aios.hardware import compatibility
from aios.providers import header_dimensions

GIB = 1024 ** 3
HW = {'ram': {'total': 3 * GIB, 'available': 2 * GIB}, 'physical_cores': 2,
      'model_storage': {'free': 50 * GIB}, 'isa': ['avx2'], 'numa_nodes': {}}


def text(value):
    value = value.encode()
    return struct.pack('<Q', len(value)) + value


def kv(key, kind, payload):
    return text(key) + struct.pack('<I', kind) + payload


def header(entries, tensors=1):
    return b'GGUF' + struct.pack('<IQQ', 3, tensors, len(entries)) + b''.join(entries)


QWEN_05B = [
    kv('general.architecture', 8, text('qwen2')),
    kv('qwen2.block_count', 4, struct.pack('<I', 24)),
    kv('qwen2.context_length', 4, struct.pack('<I', 32768)),
    kv('qwen2.embedding_length', 4, struct.pack('<I', 896)),
    kv('qwen2.attention.head_count', 4, struct.pack('<I', 14)),
    kv('qwen2.attention.head_count_kv', 4, struct.pack('<I', 2)),
]


def test_reads_the_shape_that_sizes_the_kv_cache():
    dims = header_dimensions(header(QWEN_05B))
    assert dims['general.architecture'] == 'qwen2'
    assert dims['qwen2.block_count'] == 24 and dims['qwen2.attention.head_count_kv'] == 2


def test_real_shape_gives_a_realistic_estimate():
    rating = compatibility({'size': 491 * 1024 ** 2, 'format': 'GGUF', 'gguf': header_dimensions(header(QWEN_05B))}, hw=HW)
    # 24 layers x 2 KV heads x 64 dims x 4096 tokens x 2 x f16 = 48 MiB, not 2 GiB.
    assert rating['kv_bytes'] == 48 * 1024 ** 2
    assert rating['classification'] != 'NOT_RECOMMENDED'


def test_truncated_prefix_keeps_what_it_reached():
    blob = header(QWEN_05B)
    cut = blob[:blob.index(b'qwen2.attention.head_count') - 8]
    dims = header_dimensions(cut)
    assert dims['general.architecture'] == 'qwen2' and 'qwen2.attention.head_count' not in dims


def test_tokenizer_arrays_before_the_shape_are_skipped():
    vocab = kv('tokenizer.ggml.tokens', 9, struct.pack('<IQ', 8, 3) + text('a') + text('b') + text('c'))
    dims = header_dimensions(header([QWEN_05B[0], vocab, *QWEN_05B[1:]]))
    assert dims['qwen2.block_count'] == 24


def test_per_layer_kv_heads_use_the_largest_value():
    per_layer = kv('qwen2.attention.head_count_kv', 9, struct.pack('<IQ', 4, 3) + struct.pack('<III', 2, 8, 4))
    dims = header_dimensions(header([*QWEN_05B[:5], per_layer]))
    assert dims['qwen2.attention.head_count_kv'] == 8


@pytest.mark.parametrize('blob', [b'', b'NOPE' + b'\0' * 64, b'GGUF' + struct.pack('<I', 9) + b'\0' * 32])
def test_not_a_usable_header_yields_nothing(blob):
    assert header_dimensions(blob) == {}


def test_without_a_header_the_fallback_scales_with_the_file():
    small = compatibility({'size': 400 * 1024 ** 2, 'format': 'GGUF'}, hw=HW)
    assert small['kv_bytes'] < 256 * 1024 ** 2          # was a flat 2 GiB
    assert small['classification'] != 'NOT_RECOMMENDED'
    assert 'approximated from file size' in small['reasons'][0]


def test_a_model_too_big_for_the_machine_is_still_flagged():
    mistral = [kv('general.architecture', 8, text('llama')), kv('llama.block_count', 4, struct.pack('<I', 32)),
               kv('llama.embedding_length', 4, struct.pack('<I', 4096)), kv('llama.attention.head_count', 4, struct.pack('<I', 32)),
               kv('llama.attention.head_count_kv', 4, struct.pack('<I', 8))]
    rating = compatibility({'size': int(4.4 * GIB), 'format': 'GGUF', 'gguf': header_dimensions(header(mistral))}, hw=HW)
    assert rating['kv_bytes'] == 512 * 1024 ** 2
    assert rating['classification'] == 'NOT_RECOMMENDED'


def test_installed_metadata_arrays_do_not_crash_the_estimate():
    # inspect_gguf stores arrays as descriptors, not values.
    gguf = {'general.architecture': 'qwen2', 'qwen2.block_count': 24, 'qwen2.embedding_length': 896,
            'qwen2.attention.head_count': 14, 'qwen2.attention.head_count_kv': {'array_type': 4, 'count': 24}}
    rating = compatibility({'size': 491 * 1024 ** 2, 'format': 'GGUF', 'gguf': gguf}, hw=HW)
    assert rating['classification'] in ('OPTIMAL', 'COMPATIBLE', 'LIMITED')


def test_installer_accepts_vocabularies_beyond_the_old_limits(tmp_path):
    from aios.gguf import inspect_gguf
    # 600k tokens: over the former 500k array cap, far under any real bound.
    count = 600_000
    tokens = kv('tokenizer.ggml.tokens', 9, struct.pack('<IQ', 8, count) + text('t') * count)
    body = header([kv('general.architecture', 8, text('llama')), tokens])
    tensor = text('w') + struct.pack('<IQIQ', 1, 1, 0, 0)
    data = body + tensor
    data += b'\0' * ((32 - len(data) % 32) % 32) + struct.pack('<f', 1.0)
    path = tmp_path / 'big-vocab.gguf'
    path.write_bytes(data)
    assert inspect_gguf(path)['metadata']['general.architecture'] == 'llama'


@pytest.mark.asyncio
async def test_header_is_read_small_first_and_widened_only_when_needed(monkeypatch):
    from aios import providers
    lengths = []
    template = kv('tokenizer.chat_template', 8, text('x' * 300_000))
    blobs = {'short': header(QWEN_05B), 'long': header([QWEN_05B[0], template, *QWEN_05B[1:]])}

    async def prefix(url, repo, length):
        lengths.append(length)
        return blobs[url][:length]
    monkeypatch.setattr(providers, 'request_prefix', prefix)
    assert (await providers.read_header('short', {}))['qwen2.block_count'] == 24
    assert lengths == [256 * 1024]
    lengths.clear()
    assert (await providers.read_header('long', {}))['qwen2.block_count'] == 24
    assert lengths == [256 * 1024, providers.HEADER_PREFIX]
