"""The runtime query path must not reach the network to answer a search.

The encoder's files come from `cache/models/<repo>/<revision>/` and nowhere else
(`scripts/onnx_encoder.py`); `scripts/install_models.py` is the one place that
fetches them. A search on a vault without them says `model_unavailable` and
answers by words. The hub stand-in below fails on any use, so a download attempt
would end as `load_failed` instead.
"""

import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import model_files  # noqa: E402
import search_memory  # noqa: E402


@pytest.fixture
def _forgotten_model(monkeypatch):
    monkeypatch.setattr(search_memory, "_embedder_cache", None, raising=False)
    monkeypatch.setattr(search_memory, "_embedder_unavailable_reason", None, raising=False)
    monkeypatch.setattr(search_memory, "_embedder_announced", set(), raising=False)


class _NoHub(types.ModuleType):
    def __init__(self, touched: list) -> None:
        super().__init__("huggingface_hub")
        self._touched = touched

    def __getattr__(self, name):
        self._touched.append(name)
        raise AssertionError(f"the query path reached huggingface_hub.{name}")


def test_weights_that_are_not_on_disk_degrade_to_no_dense_signal(
    tmp_path, monkeypatch, _forgotten_model
):
    touched: list = []
    monkeypatch.setitem(sys.modules, "huggingface_hub", _NoHub(touched))
    monkeypatch.setattr(model_files, "models_root", lambda: tmp_path)

    assert search_memory._get_embedder() is None
    assert search_memory.embedder_unavailable_reason().startswith("model_unavailable")
    assert "is not installed" in search_memory.embedder_unavailable_reason()
    assert touched == []
