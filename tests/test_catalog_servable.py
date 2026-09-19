"""The catalogue must only offer files this appliance can actually run."""
import pytest
from aios.providers import artifact, servable


def row(filename, **extra):
    return artifact('org/model', filename, 'https://example.org/' + filename, 1024, None, 'main', **extra)


@pytest.mark.parametrize('filename', [
    'mmproj-Qwen_Qwen3.5-4B-bf16.gguf',   # the projector that started this
    'MMPROJ-model.gguf',
    'model.mmproj.gguf',
    'repo/mmproj-f16.gguf',
    'model-00001-of-00005.gguf',          # a shard cannot load without its siblings
    'model-00003-of-00005.gguf'])
def test_companions_and_shards_are_not_listed(filename):
    assert servable(filename, {}) is False
    assert row(filename) is None


def test_projector_declared_only_in_metadata_is_not_listed():
    assert row('vision-tower.gguf', architecture='clip') is None


@pytest.mark.parametrize('filename', [
    'qwen2.5-0.5b-instruct-q4_k_m.gguf',
    'llama-3-8b-instruct.Q5_K_M.gguf',
    'tinyllamas/stories260K.gguf',
    'Qwen3.5-4B-Q4_K_M.gguf',
    'mistral-7b-v0.1.IQ3_XS.gguf'])
def test_real_models_are_still_listed(filename):
    assert servable(filename, {}) is True
    assert row(filename)['filename'] == filename


def test_architecture_filter_uses_the_runtime_manifest(tmp_path, monkeypatch):
    from aios import providers
    manifest = tmp_path / 'supported-architectures.json'
    manifest.write_text('["llama", "qwen2", "deepseek2"]')
    monkeypatch.setattr(providers, 'ETC', tmp_path)
    monkeypatch.setattr(providers, '_ARCHITECTURES', None)
    assert servable('good.gguf', {'architecture': 'qwen2'}) is True
    assert servable('exotic.gguf', {'architecture': 'somethingnew'}) is False
    # The repository often does not declare one; the GGUF header decides later.
    assert servable('undeclared.gguf', {}) is True
    assert servable('undeclared.gguf', {'architecture': 'unknown'}) is True


def test_nothing_is_filtered_without_a_manifest(tmp_path, monkeypatch):
    from aios import providers
    monkeypatch.setattr(providers, 'ETC', tmp_path)
    monkeypatch.setattr(providers, '_ARCHITECTURES', None)
    assert servable('exotic.gguf', {'architecture': 'somethingnew'}) is True
