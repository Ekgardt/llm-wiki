"""A missing embedding model must say so, not just answer with no vectors.

`_get_embedder` was `except Exception: return None`. Returning None is the
right answer — the generation reader treats an unusable query vector as "no
dense signal" by contract — but it was the *only* answer. A missing
encoder runtime, a model that was never downloaded, and a corrupt
weight file were indistinguishable from one another and from a vault that
simply had no vectors yet.

A LongMemEval worker run under the system `python3` instead of `uv run`
retrieved zero rows and looked exactly like a retrieval regression. The
generation carried `vector_state: absent`, which was true and said nothing.

The contract does not change: no dense signal is still not an exception. What
changes is that the reason has a name, is redacted and bounded, and is said
once per process rather than once per call.
"""

from __future__ import annotations

import onnx_encoder
import pytest
import search_memory


@pytest.fixture(autouse=True)
def _fresh_embedder_state(monkeypatch):
    """Each test starts with nothing loaded and nothing announced."""
    monkeypatch.setattr(search_memory, "_embedder_cache", None, raising=False)
    monkeypatch.setattr(
        search_memory, "_embedder_unavailable_reason", None, raising=False
    )
    monkeypatch.setattr(search_memory, "_embedder_announced", set(), raising=False)


def _loader_raises(monkeypatch, exc: BaseException):
    def _raise(*_args, **_kwargs):
        raise exc

    monkeypatch.setattr(onnx_encoder, "load_encoder", _raise)


def test_a_missing_dependency_names_itself_and_still_yields_no_dense_signal(
    monkeypatch,
):
    _loader_raises(monkeypatch, ImportError("No module named 'onnxruntime'"))

    assert search_memory._get_embedder() is None
    reason = search_memory.embedder_unavailable_reason()
    assert reason is not None
    assert "import_failed" in reason


def test_a_model_that_is_not_on_disk_is_not_the_same_as_a_missing_package(
    monkeypatch,
):
    _loader_raises(monkeypatch, FileNotFoundError("onnx/model.onnx is not in the local cache"))

    assert search_memory._get_embedder() is None
    reason = search_memory.embedder_unavailable_reason()
    assert reason is not None
    assert "model_unavailable" in reason
    assert "import_failed" not in reason


def test_any_other_load_failure_is_named_a_load_failure(monkeypatch):
    _loader_raises(monkeypatch, RuntimeError("weights are corrupt"))

    assert search_memory._get_embedder() is None
    reason = search_memory.embedder_unavailable_reason()
    assert reason is not None
    assert "load_failed" in reason
    assert "corrupt" in reason


def test_the_reason_is_said_once_not_once_per_call(monkeypatch, capsys):
    _loader_raises(monkeypatch, RuntimeError("weights are corrupt"))

    for _ in range(5):
        assert search_memory._get_embedder() is None

    said = capsys.readouterr().err
    assert said.count("load_failed") == 1


def test_the_reason_is_redacted_and_bounded(monkeypatch):
    leak = "x" * 3000 + " token=sk-abcdefghijklmnopqrstuvwxyz012345"
    _loader_raises(monkeypatch, RuntimeError(leak))

    assert search_memory._get_embedder() is None
    reason = search_memory.embedder_unavailable_reason()
    assert "sk-abcdefghijklmnopqrstuvwxyz012345" not in reason
    assert len(reason) <= search_memory.EMBEDDER_REASON_MAX_CHARS + 64


def test_the_import_probe_records_the_same_reason(monkeypatch):
    monkeypatch.setattr(search_memory, "ENCODER_PACKAGES", ("a_package_nobody_installed",))

    assert search_memory._have_encoder_runtime() is False
    reason = search_memory.embedder_unavailable_reason() or ""
    assert "import_failed" in reason and "a_package_nobody_installed" in reason


def test_no_dense_signal_is_still_not_an_exception(monkeypatch):
    """The generation encoder keeps its contract: it returns nothing, quietly."""
    _loader_raises(monkeypatch, RuntimeError("weights are corrupt"))

    encode = search_memory._lazy_generation_query_encoder()

    assert encode(["a question"]) == []
