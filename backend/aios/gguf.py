"""Bounded, non-executing GGUF v2/v3 structural validation."""
import os
import struct

SCALARS = {0: 'B', 1: 'b', 2: 'H', 3: 'h', 4: 'I', 5: 'i', 6: 'f', 7: '?', 10: 'Q', 11: 'q', 12: 'd'}

DRAFT_MESSAGE = ('This file is a speculative-decoding draft (MTP, DFlash or EAGLE) that only works '
                 'alongside its main model and cannot answer on its own; install the main model instead')


def not_a_model(metadata):
    """The declared kind of a GGUF that is not a model (mmproj, imatrix, adapter),
    or None. Files written before general.type existed are models."""
    kind = metadata.get('general.type')
    return kind.lower() if isinstance(kind, str) and kind.lower() != 'model' else None


def draft_reason(architecture, metadata, tensor_count, blocks=None, names=None):
    """Why a GGUF is a draft rather than a model, or None. Repositories publish
    drafts next to the model they accelerate, under the same architecture, so
    only the file's structure tells them apart: llama.cpp marks a drafter by the
    target layers it reads from its main model, and a multi-token-prediction
    head carries just the last block(s) of a model that declares many."""
    if f'{architecture}.target_layers' in metadata:
        return 'reads layers of another model'
    layers = metadata.get(f'{architecture}.block_count')
    if isinstance(layers, int) and not isinstance(layers, bool) and layers > 0:
        # Every real block holds at least a norm and a weight.
        if tensor_count < 2 * layers:
            return f'{tensor_count} tensors for {layers} blocks'
        nextn = metadata.get(f'{architecture}.nextn_predict_layers')
        nextn = nextn if isinstance(nextn, int) and not isinstance(nextn, bool) and nextn > 0 else 0
        if blocks is not None and len(blocks) < layers - nextn:
            return f'{len(blocks)} of {layers} blocks present'
        if names is not None and not {'token_embd.weight', 'output.weight'} & names:
            return 'no token embeddings'
    return None


def read_structure(path):
    """Structural validation shared by models and their companion files."""
    size = os.path.getsize(path)
    with open(path, 'rb') as stream:
        # Bounds against hostile files, not a model-size policy. They must stay
        # above what real models need: the largest headers seen are ~10.4 MiB
        # (256k-token vocabularies) with ~280k-entry merge tables, and those keep
        # growing. llama.cpp itself imposes neither limit.
        budget = 64 * 1024 * 1024
        def read(length):
            nonlocal budget
            if length < 0 or length > budget:
                raise ValueError('GGUF metadata exceeds 64 MiB limit')
            budget -= length
            value = stream.read(length)
            if len(value) != length:
                raise ValueError('Truncated GGUF')
            return value
        def number(fmt):
            return struct.unpack('<' + fmt, read(struct.calcsize('<' + fmt)))[0]
        def string():
            length = number('Q')
            if length > 1024 * 1024:
                raise ValueError('Oversized GGUF string')
            return read(length).decode('utf-8', errors='strict')
        def value(kind, depth=0):
            if kind in SCALARS:
                return number(SCALARS[kind])
            if kind == 8:
                return string()
            if kind == 9 and depth == 0:
                child, count = number('I'), number('Q')
                if count > 4000000 or child == 9:
                    raise ValueError('Oversized or nested GGUF array')
                for _ in range(count):
                    value(child, depth + 1)
                return {'array_type': child, 'count': count}
            raise ValueError('Invalid GGUF metadata type')
        if read(4) != b'GGUF':
            raise ValueError('Not a GGUF file')
        version, tensors, count = number('I'), number('Q'), number('Q')
        if version not in (2, 3) or not 0 < tensors <= 100000 or count > 10000:
            raise ValueError('Unsupported GGUF version or invalid counts')
        metadata = {}
        for _ in range(count):
            key = string()
            if len(key) > 512 or key in metadata:
                raise ValueError('Invalid or duplicate GGUF key')
            metadata[key] = value(number('I'))
        offsets = []
        names = set()
        for _ in range(tensors):
            name, dims = string(), number('I')
            if name in names or dims not in range(1, 5):
                raise ValueError('Invalid tensor descriptor')
            names.add(name)
            shape = [number('Q') for _ in range(dims)]
            kind, offset = number('I'), number('Q')
            if any(x == 0 or x > 2 ** 32 for x in shape) or kind > 100:
                raise ValueError('Invalid tensor dimensions/type')
            offsets.append(offset)
        alignment = metadata.get('general.alignment', 32)
        if not isinstance(alignment, int) or alignment < 1 or alignment > 4096 or alignment & (alignment - 1):
            raise ValueError('Invalid GGUF alignment')
        start = (stream.tell() + alignment - 1) // alignment * alignment
        if any(offset % alignment or start + offset >= size for offset in offsets):
            raise ValueError('Tensor data outside file')
        return version, tensors, metadata, names, start


def inspect_gguf(path):
    version, tensors, metadata, names, start = read_structure(path)
    architecture = metadata.get('general.architecture')
    # Companion files ship alongside a model and carry no language weights of
    # their own: a vision projector cannot answer anything on its own, and
    # llama-server cannot load one as a model.
    kind = not_a_model(metadata)
    if architecture == 'clip' or kind == 'mmproj':
        raise ValueError('This file is a multimodal projector (mmproj), not a language model; install the model it accompanies instead')
    if kind:
        raise ValueError(f'This file is a GGUF of type "{kind}" (for example an importance matrix or a LoRA adapter), not a model; install the model itself instead')
    if not isinstance(architecture, str):
        raise ValueError('Missing architecture')
    blocks = {name.split('.')[1] for name in names if name.startswith('blk.') and name.split('.')[1].isdigit()}
    if draft_reason(architecture, metadata, tensors, blocks, names):
        raise ValueError(DRAFT_MESSAGE)
    return {'version': version, 'tensors': tensors, 'metadata': metadata, 'data_offset': start}


def is_projector(metadata):
    return metadata.get('general.architecture') == 'clip' or not_a_model(metadata) == 'mmproj'


def inspect_projector(path):
    """A multimodal projector: the vision (or audio) encoder llama.cpp loads with
    --mmproj beside the language model. Anything else in its place is refused."""
    version, tensors, metadata, _, start = read_structure(path)
    if not is_projector(metadata):
        raise ValueError('The multimodal projector published with this model is not a projector GGUF')
    return {'version': version, 'tensors': tensors, 'metadata': metadata, 'data_offset': start}
