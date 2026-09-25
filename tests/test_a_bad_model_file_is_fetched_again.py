"""A cached model file that does not match is replaced in the same run; a good one is not re-read.

The mismatch branch removed only the link, the Hub re-linked the same bad blob,
and every night re-hashed 2.3 GB of verified weights. See
docs/research/2026-09-25-a-bad-model-file-is-fetched-again-and-a-good-one-is-not-reread.md.
"""

from __future__ import annotations

import os

import install_models
import memory_state

from tests.test_install_models import _FakeHub, _pinned_to


class _CachingHub(_FakeHub):
    """Like the Hub: a file already in the snapshot is not downloaded again."""

    def snapshot_download(self, repo_id, *, revision, allow_patterns):
        snapshot = self._snapshot(repo_id, revision)
        missing = [name for name in allow_patterns if not (snapshot / name).is_file()]
        return super().snapshot_download(repo_id, revision=revision, allow_patterns=missing)


def _hub(tmp_path, monkeypatch):
    monkeypatch.setattr(memory_state, "STATE_ROOT", tmp_path / "state")
    contents = {"org/encoder": b"encoder weights"}
    hub = _CachingHub(tmp_path / "hub", contents)
    return hub, _pinned_to(contents)[0]


def test_a_corrupted_cached_file_is_fetched_again_in_the_same_run(tmp_path, monkeypatch) -> None:
    hub, model = _hub(tmp_path, monkeypatch)
    install_models.ensure(model, hub, download=True)
    cached = install_models.cached_weights(model, hub)
    cached.write_bytes(b"encoder weightz")

    outcome = install_models.ensure(model, hub, download=True)

    assert (outcome["state"], cached.read_bytes()) == (install_models.STATE_FETCHED, b"encoder weights")


def test_a_verified_unchanged_file_is_not_read_again(tmp_path, monkeypatch) -> None:
    hub, model = _hub(tmp_path, monkeypatch)
    install_models.ensure(model, hub, download=False)
    hub.snapshot_download("org/encoder", revision=model.revision, allow_patterns=list(model.allow_patterns))
    long_ago = install_models.cached_weights(model, hub).stat().st_mtime - 60
    os.utime(install_models.cached_weights(model, hub), (long_ago, long_ago))
    install_models.ensure(model, hub, download=True)
    reads: list[str] = []
    real_digest = install_models._digest

    def counted(path, ceiling):
        reads.append(path.name)
        return real_digest(path, ceiling)

    monkeypatch.setattr(install_models, "_digest", counted)

    outcome = install_models.ensure(model, hub, download=True)

    assert (outcome["state"], reads) == (install_models.STATE_PRESENT, [])
