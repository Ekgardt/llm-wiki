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
        return self.root / repo_id.replace("/", "--") / "snapshots" / revision

    def try_to_load_from_cache(self, repo_id, filename, *, revision):
        path = self._snapshot(repo_id, revision) / filename
        if path.is_file():
            return str(path)
        return None

    def snapshot_download(self, repo_id, *, revision, allow_patterns):
        self.downloads.append((repo_id, revision, list(allow_patterns)))
        snapshot = self._snapshot(repo_id, revision)
        for name in allow_patterns:
            (snapshot / name).parent.mkdir(parents=True, exist_ok=True)
            (snapshot / name).write_bytes(self.contents[repo_id] if name == WEIGHTS else b"companion")
        return str(snapshot)


WEIGHTS = "onnx/model.onnx"
COMPANION = "tokenizer.json"


def _pinned_to(contents: dict[str, bytes]) -> tuple[install_models.PinnedModel, ...]:
    return tuple(
        install_models.PinnedModel(
            repo_id,
            "f" * 40,
            WEIGHTS,
            hashlib.sha256(blob).hexdigest(),
            len(blob),
            (WEIGHTS, COMPANION),
            ("model.safetensors",),
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
    assert all(patterns == [WEIGHTS, COMPANION] for _, _, patterns in two_models.downloads)
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
        "org/reranker", WEIGHTS, revision="f" * 40
    ) is None
    out = capsys.readouterr().out
    assert "mismatch org/reranker" in out and "fetched org/encoder" in out


def test_check_reports_without_downloading(two_models, capsys):
    assert install_models.main(["--check", "--json"]) == install_models.EXIT_INCOMPLETE
    assert two_models.downloads == []
    assert '"state": "missing"' in capsys.readouterr().out


def test_without_the_library_the_command_says_which_extra_to_install(monkeypatch, capsys):
    monkeypatch.setattr(install_models, "hub_library", lambda: None)
    # A runtime is installed but the hub library is not: the vault the test runs
    # in may have neither, and then the command has nothing to fetch at all.
    monkeypatch.setattr(install_models, "wanted_models", install_models.pinned_models)

    assert install_models.main([]) == install_models.EXIT_NO_LIBRARY
    assert "uv sync --locked --inexact --extra semantic" in capsys.readouterr().err


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

    steps = {step.label: step for step in scheduled_nightly._post_compile_steps()}
    labels = list(steps)

    assert (labels.index("models") > labels.index("lint"), steps["models"].command[-1].endswith("install_models.py")) == (
        True,
        True,
    )


def test_weights_without_their_companion_file_are_fetched_again(two_models):
    install_models.main([])
    (two_models._snapshot("org/encoder", "f" * 40) / COMPANION).unlink()
    two_models.downloads.clear()

    assert install_models.main([]) == 0
    assert [repo for repo, _, _ in two_models.downloads] == ["org/encoder"]


def _retired_link(hub: _FakeHub, repo_id: str) -> tuple[Path, Path]:
    """A retired file as the Hub cache holds it: a snapshot link to a blob."""
    blob = hub.root / repo_id.replace("/", "--") / "blobs" / "old-weights"  # the Hub's layout
    blob.parent.mkdir(parents=True, exist_ok=True)
    blob.write_bytes(b"torch weights")
    link = hub._snapshot(repo_id, "f" * 40) / "model.safetensors"
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(blob)
    return link, blob


@pytest.mark.skipif(sys.platform == "win32", reason="the Hub cache links files only where symlinks work")
def test_verified_weights_retire_the_file_they_replace_and_its_blob(two_models, capsys):
    link, blob = _retired_link(two_models, "org/encoder")

    assert install_models.main(["--json"]) == 0
    assert (link.exists(), link.is_symlink(), blob.exists()) == (False, False, False)
    assert '"retired": ["model.safetensors"]' in capsys.readouterr().out


def test_a_check_or_a_failed_fetch_retires_nothing(two_models):
    link = two_models._snapshot("org/reranker", "f" * 40) / "model.safetensors"
    link.parent.mkdir(parents=True, exist_ok=True)
    link.write_bytes(b"torch weights")
    two_models.contents["org/reranker"] = b"not the pinned bytes"

    install_models.main(["--check"])
    install_models.main([])

    assert link.exists()


@pytest.mark.skipif(sys.platform == "win32", reason="the Hub cache links files only where symlinks work")
def test_the_text_report_names_the_file_it_removed(two_models, capsys):
    _retired_link(two_models, "org/encoder")

    install_models.main([])

    assert "fetched org/encoder@ffffffffffff; removed model.safetensors" in capsys.readouterr().out


def _without_runtime(model: install_models.PinnedModel) -> install_models.PinnedModel:
    return install_models.PinnedModel(
        model.repo_id,
        model.revision,
        model.weights_file,
        model.weights_sha256,
        model.weights_bytes,
        model.allow_patterns,
        model.retired_files,
        ("a_module_nobody_installed",),
    )


def test_a_model_whose_runtime_is_absent_is_not_fetched(two_models, monkeypatch):
    encoder, reranker = _pinned_to({"org/encoder": b"encoder weights", "org/reranker": b"reranker weights"})
    monkeypatch.setattr(install_models, "pinned_models", lambda: (encoder, _without_runtime(reranker)))

    assert install_models.main([]) == 0
    assert [repo for repo, _, _ in two_models.downloads] == ["org/encoder"]


def test_with_no_runtime_installed_there_is_nothing_to_fetch_and_no_failure(monkeypatch, capsys):
    """A base install's nightly `models` step is not a failed night."""
    pins = tuple(_without_runtime(model) for model in install_models.pinned_models())
    monkeypatch.setattr(install_models, "pinned_models", lambda: pins)
    monkeypatch.setattr(install_models, "hub_library", lambda: None)

    assert install_models.main([]) == 0
    assert "nothing to fetch" in capsys.readouterr().out
