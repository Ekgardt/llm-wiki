"""The runtime query path must not reach the network to answer a search.

The reranker and the retrieval benchmark had both loaded their weights with
`local_files_only=True` from the beginning. `search_memory._get_embedder` had
not, so every cold search opened a connection to huggingface.co before it read
a local byte — visible as the hub's own unauthenticated-request warning on
stderr during an ordinary query.
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
    monkeypatch.setattr(
        search_memory, "_embedder_unavailable_reason", None, raising=False
    )
    monkeypatch.setattr(search_memory, "_embedder_announced", set(), raising=False)


def _recording_module(calls):
    class Recorded:
        def __init__(self, model_id, **kwargs):
            calls.append((model_id, kwargs))

    module = types.ModuleType("sentence_transformers")
    module.SentenceTransformer = Recorded
    return module


def test_the_embedding_model_is_loaded_from_local_files_only(
    monkeypatch, _forgotten_model
):
    calls = []
    monkeypatch.setitem(
        sys.modules, "sentence_transformers", _recording_module(calls)
    )

    assert search_memory._get_embedder() is not None

    (model_id, kwargs) = calls[0]
    assert model_id == search_memory.EMBEDDING_MODEL
    assert kwargs["local_files_only"] is True
    assert kwargs["trust_remote_code"] is False
    assert kwargs["revision"] == search_memory.EMBEDDING_MODEL_REVISION


def test_weights_that_are_not_on_disk_degrade_to_no_dense_signal(
    monkeypatch, _forgotten_model
):
    module = types.ModuleType("sentence_transformers")

    def _absent(model_id, **kwargs):
        raise OSError(f"{model_id} is not in the local cache")

    module.SentenceTransformer = _absent
    monkeypatch.setitem(sys.modules, "sentence_transformers", module)

    assert search_memory._get_embedder() is None
    assert search_memory.embedder_unavailable_reason().startswith(
        "model_unavailable"
    )
