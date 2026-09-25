"""The runtime query path must not reach the network to answer a search.

The encoder's files come from the local Hugging Face cache and nowhere else
(`scripts/onnx_encoder.py`); `scripts/install_models.py` is the one place that
fetches them. A search on a vault without them says `model_unavailable` and
answers by words. The hub stand-in below offers the cache lookup and nothing
else, so any attempt to download would fail as `load_failed` instead.
"""

import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import search_memory  # noqa: E402


@pytest.fixture
def _forgotten_model(monkeypatch):
    monkeypatch.setattr(search_memory, "_embedder_cache", None, raising=False)
    monkeypatch.setattr(search_memory, "_embedder_unavailable_reason", None, raising=False)
    monkeypatch.setattr(search_memory, "_embedder_announced", set(), raising=False)


def _cache_only_hub(lookups: list, found: dict) -> types.ModuleType:
    hub = types.ModuleType("huggingface_hub")

    def try_to_load_from_cache(repo_id, filename, *, revision):
        lookups.append((repo_id, filename, revision))
        return found.get(filename)

    hub.try_to_load_from_cache = try_to_load_from_cache
    return hub


def test_weights_that_are_not_on_disk_degrade_to_no_dense_signal(monkeypatch, _forgotten_model):
    lookups: list = []
    monkeypatch.setitem(sys.modules, "huggingface_hub", _cache_only_hub(lookups, {}))
    monkeypatch.setitem(sys.modules, "onnxruntime", types.ModuleType("onnxruntime"))

    assert search_memory._get_embedder() is None
    assert search_memory.embedder_unavailable_reason().startswith("model_unavailable")
    assert lookups == [
        (search_memory.EMBEDDING_MODEL, "onnx/model.onnx", search_memory.EMBEDDING_MODEL_REVISION)
    ]
