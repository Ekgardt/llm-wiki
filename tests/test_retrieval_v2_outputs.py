"""The retrieval v2 model cache environment is scoped and offline by default."""
from __future__ import annotations

import os
import sys
from pathlib import Path

BENCHMARK = Path(__file__).resolve().parent.parent / "benchmark"
if str(BENCHMARK) not in sys.path:
    sys.path.insert(0, str(BENCHMARK))

import retrieval_v2_outputs  # noqa: E402


def test_model_cache_environment_is_scoped_and_offline_by_default(tmp_path, monkeypatch):
    monkeypatch.setenv("HF_HOME", "original")
    before = dict(os.environ)
    cache = tmp_path / "isolated"

    with retrieval_v2_outputs.model_cache_environment(cache, allow_download=False) as acquisition:
        assert acquisition == "offline-local-files-only"
        assert os.environ["HF_HUB_OFFLINE"] == "1"
        assert Path(os.environ["HF_HOME"]).is_relative_to(cache.resolve())
        assert Path(os.environ["TORCH_HOME"]).is_relative_to(cache.resolve())

    assert dict(os.environ) == before
    with retrieval_v2_outputs.model_cache_environment(cache, allow_download=True) as acquisition:
        assert acquisition == "download-allowed-explicit-cache"
        assert os.environ["HF_HUB_OFFLINE"] == "0"
    assert dict(os.environ) == before
