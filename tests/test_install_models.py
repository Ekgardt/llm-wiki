"""The weights arrive with the install: fetched once, verified, never twice.

See `docs/research/2026-09-10-the-weights-arrive-with-the-install.md`.
"""
from __future__ import annotations

import hashlib
import sys
from pathlib import Path
from types import ModuleType

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import install_models  # noqa: E402


class _FakeHub(ModuleType):
    """A Hub whose cache and downloads are directories under tmp_path."""

    def __init__(self, root: Path, contents: dict[str, bytes]) -> None:
        super().__init__("huggingface_hub")
        self.root = root
        self.contents = contents
        self.downloads: list[tuple[str, str, list[str]]] = []

    def _snapshot(self, repo_id: str, revision: str) -> Path:
        return self.root / repo_id.replace("/", "--") / revision

    def try_to_load_from_cache(self, repo_id, filename, *, revision):
        path = self._snapshot(repo_id, revision) / filename
        if path.is_file():
            return str(path)
        return None

    def snapshot_download(self, repo_id, *, revision, allow_patterns):
        self.downloads.append((repo_id, revision, list(allow_patterns)))
        snapshot = self._snapshot(repo_id, revision)
        snapshot.mkdir(parents=True, exist_ok=True)
        (snapshot / install_models.WEIGHTS_FILE).write_bytes(self.contents[repo_id])
        return str(snapshot)


def _pinned_to(contents: dict[str, bytes]) -> tuple[install_models.PinnedModel, ...]:
    return tuple(
        install_models.PinnedModel(
            repo_id, "f" * 40, hashlib.sha256(blob).hexdigest(), len(blob)
        )
        for repo_id, blob in contents.items()
    )


@pytest.fixture
def two_models(tmp_path, monkeypatch):
    contents = {"org/encoder": b"encoder weights", "org/reranker": b"reranker weights"}
    hub = _FakeHub(tmp_path / "hub", contents)
    pins = _pinned_to(contents)
    monkeypatch.setattr(install_models, "hub_library", lambda: hub)
    monkeypatch.setattr(install_models, "pinned_models", lambda: pins)
    return hub


def test_missing_weights_are_fetched_at_the_pinned_commit_and_verified(two_models, capsys):
    assert install_models.main([]) == 0

    assert [(repo, rev) for repo, rev, _ in two_models.downloads] == [
        ("org/encoder", "f" * 40),
        ("org/reranker", "f" * 40),
    ]
    assert all(patterns == list(install_models.ALLOW_PATTERNS) for _, _, patterns in two_models.downloads)
    assert capsys.readouterr().out.count("fetched") == 2


def test_present_verified_weights_are_not_fetched_again(two_models, capsys):
    install_models.main([])
    two_models.downloads.clear()

    assert install_models.main([]) == 0
    assert two_models.downloads == []
    assert capsys.readouterr().out.count("present") == 2


def test_a_file_that_does_not_match_its_pin_is_removed_and_the_run_fails(
    two_models, monkeypatch, capsys
):
    two_models.contents["org/reranker"] = b"not the pinned bytes"

    assert install_models.main([]) == install_models.EXIT_INCOMPLETE
    assert two_models.try_to_load_from_cache(
        "org/reranker", install_models.WEIGHTS_FILE, revision="f" * 40
    ) is None
    out = capsys.readouterr().out
    assert "mismatch org/reranker" in out and "fetched org/encoder" in out


def test_check_reports_without_downloading(two_models, capsys):
    assert install_models.main(["--check", "--json"]) == install_models.EXIT_INCOMPLETE
    assert two_models.downloads == []
    assert '"state": "missing"' in capsys.readouterr().out


def test_without_the_library_the_command_says_which_extra_to_install(monkeypatch, capsys):
    monkeypatch.setattr(install_models, "hub_library", lambda: None)

    assert install_models.main([]) == install_models.EXIT_NO_LIBRARY
    assert "uv sync --extra semantic" in capsys.readouterr().err


def test_the_pins_name_the_two_models_the_read_path_loads():
    import embedding_model
    import reranker

    encoder, cross_encoder = install_models.pinned_models()
    assert (encoder.repo_id, encoder.revision) == (
        embedding_model.EMBEDDING_MODEL,
        embedding_model.EMBEDDING_MODEL_REVISION,
    )
    assert (cross_encoder.repo_id, cross_encoder.revision) == (
        reranker.DEFAULT_RERANKER_MODEL,
        reranker.DEFAULT_RERANKER_REVISION,
    )
    assert all(len(model.weights_sha256) == 64 and model.weights_bytes > 0 for model in (encoder, cross_encoder))


def test_doctor_names_the_missing_weights_and_the_command(monkeypatch):
    import doctor

    hub = object()
    monkeypatch.setattr(install_models, "hub_library", lambda: hub)
    monkeypatch.setattr(
        install_models, "missing_models", lambda _hub: [install_models.pinned_models()[1]]
    )

    finding = doctor._models_check()

    assert finding["status"] == "degraded"
    assert "BAAI/bge-reranker-v2-m3@953dc6f6f85a" in finding["message"]
    assert "install_models.py" in finding["message"]
    assert finding["details"]["command"] == "uv run python scripts/install_models.py"


def test_doctor_expects_no_weights_without_the_semantic_extra(monkeypatch):
    import doctor

    monkeypatch.setattr(install_models, "hub_library", lambda: None)

    assert doctor._models_check()["status"] == "ok"


def test_the_nightly_fetches_missing_weights_after_its_index_work():
    import scheduled_nightly

    labels = [step.label for step in scheduled_nightly._post_compile_steps()]

    assert labels.index("models") > labels.index("search")
    step = next(step for step in scheduled_nightly._post_compile_steps() if step.label == "models")
    assert step.command[-1].endswith("install_models.py")
