"""A mismatched installed model file is replaced in the same run; a good one is not re-read.

The mismatch branch once removed only a link, the Hub re-linked the same bad blob,
and every night re-hashed 2.3 GB of verified weights. See
docs/research/2026-09-25-a-bad-model-file-is-fetched-again-and-a-good-one-is-not-reread.md.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import install_models
import memory_state
import pytest

from tests.test_install_models import _FakeHub, _pinned_to


class _SkippingHub(_FakeHub):
    """Like the Hub with `local_dir`: a file already there is not downloaded again."""

    def snapshot_download(
        self, repo_id: str, *, revision: str, allow_patterns: list[str], local_dir: str
    ) -> str:
        missing = [name for name in allow_patterns if not (Path(local_dir) / name).is_file()]
        return super().snapshot_download(
            repo_id, revision=revision, allow_patterns=missing, local_dir=local_dir
        )


def _hub(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, contents: dict[str, bytes]
) -> _SkippingHub:
    monkeypatch.setattr(memory_state, "STATE_ROOT", tmp_path / "state")
    return _SkippingHub(contents)


def _weights(model: install_models.PinnedModel) -> Path:
    weights: Path = model.directory / model.weights_file
    return weights


def test_a_corrupted_installed_file_is_fetched_again_in_the_same_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    contents = {"org/encoder": b"encoder weights"}
    hub = _hub(tmp_path, monkeypatch, contents)
    model = _pinned_to(contents)[0]
    install_models.ensure(model, hub, download=True)
    _weights(model).write_bytes(b"encoder weightz")

    outcome = install_models.ensure(model, hub, download=True)

    assert (outcome["state"], _weights(model).read_bytes()) == (
        install_models.STATE_FETCHED,
        b"encoder weights",
    )


def test_a_verified_unchanged_file_is_not_read_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    contents = {"org/encoder": b"encoder weights"}
    hub = _hub(tmp_path, monkeypatch, contents)
    model = _pinned_to(contents)[0]
    hub.snapshot_download(
        model.repo_id, revision=model.revision, allow_patterns=list(model.read_files),
        local_dir=str(model.directory),
    )
    long_ago = _weights(model).stat().st_mtime - 60
    os.utime(_weights(model), (long_ago, long_ago))
    install_models.ensure(model, hub, download=True)
    reads: list[str] = []
    real_digest = install_models._digest

    def counted(path: Path, ceiling: int) -> str:
        reads.append(path.name)
        digest: str = real_digest(path, ceiling)
        return digest

    monkeypatch.setattr(install_models, "_digest", counted)

    outcome = install_models.ensure(model, hub, download=True)

    assert (outcome["state"], reads) == (install_models.STATE_PRESENT, [])


def test_a_missing_companion_file_does_not_cost_the_verified_weights(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    contents = {"org/encoder": b"encoder weights"}
    hub = _hub(tmp_path, monkeypatch, contents)
    model = _pinned_to(contents)[0]
    install_models.ensure(model, hub, download=True)
    companion = next(name for name in model.read_files if name != model.weights_file)
    (model.directory / companion).unlink()
    hub.downloads.clear()

    outcome = install_models.ensure(model, hub, download=True)

    assert (outcome["state"], hub.downloads) == (
        install_models.STATE_FETCHED,
        [(model.repo_id, model.revision, [companion])],
    )


def test_an_unreachable_hub_fails_one_model_and_the_next_is_still_tried(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    contents = {"org/encoder": b"encoder weights", "org/reranker": b"reranker weights"}
    hub = _hub(tmp_path, monkeypatch, contents)
    real_download = hub.snapshot_download

    def offline_for_the_encoder(repo_id: str, **options: Any) -> str:
        if repo_id == "org/encoder":
            raise OSError("connection refused")
        return real_download(repo_id, **options)

    monkeypatch.setattr(hub, "snapshot_download", offline_for_the_encoder)
    states = [
        install_models._settled_and_retired(model, hub, download=True)["state"]
        for model in _pinned_to(contents)
    ]

    assert states == [install_models.STATE_UNREACHABLE, install_models.STATE_FETCHED]
