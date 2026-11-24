"""The weights arrive with the install: fetched once, verified, never twice.

See `docs/research/2026-09-10-the-weights-arrive-with-the-install.md`.
"""
from __future__ import annotations

import hashlib
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import install_models  # noqa: E402
import model_files  # noqa: E402
import openvino_runtime  # noqa: E402

WEIGHTS = "onnx/model.onnx"
COMPANION = "tokenizer.json"
REVISION = "f" * 40
RETIRED = ("org/old-encoder", "e" * 40)


class _CacheNotFound(Exception):
    pass


class _FakeCache:
    """`scan_cache_dir()`'s answer: the revisions held, and the deletions asked for."""

    def __init__(self, held: list[tuple[str, str]]) -> None:
        self.held = held
        self.deleted: list[str] = []

    @property
    def repos(self):
        return [
            SimpleNamespace(repo_id=repo, repo_type="model",
                            revisions=[SimpleNamespace(commit_hash=revision)])
            for repo, revision in self.held
        ]

    def delete_revisions(self, revision):
        def execute():
            self.deleted.append(revision)
            self.held = [entry for entry in self.held if entry[1] != revision]

        return SimpleNamespace(execute=execute)


class _FakeHub(ModuleType):
    """A Hub whose downloads write the pinned files into `local_dir`."""

    CacheNotFound = _CacheNotFound

    def __init__(self, contents: dict[str, bytes]) -> None:
        super().__init__("huggingface_hub")
        self.contents = contents
        self.downloads: list[tuple[str, str, list[str]]] = []
        self.cache = _FakeCache([])

    def snapshot_download(self, repo_id, *, revision, allow_patterns, local_dir):
        self.downloads.append((repo_id, revision, list(allow_patterns)))
        for name in allow_patterns:
            target = Path(local_dir) / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(self.contents[repo_id] if name == WEIGHTS else b"companion")
        return local_dir

    def scan_cache_dir(self):
        return self.cache


def _pinned_to(contents: dict[str, bytes]) -> tuple[install_models.PinnedModel, ...]:
    return tuple(
        install_models.PinnedModel(
            repo_id,
            REVISION,
            WEIGHTS,
            hashlib.sha256(blob).hexdigest(),
            len(blob),
            (WEIGHTS, COMPANION),
            RETIRED if repo_id == "org/encoder" else None,
        )
        for repo_id, blob in contents.items()
    )


@pytest.fixture
def two_models(tmp_path, monkeypatch):
    contents = {"org/encoder": b"encoder weights", "org/reranker": b"reranker weights"}
    hub = _FakeHub(contents)
    pins = _pinned_to(contents)
    monkeypatch.setattr(model_files, "models_root", lambda: tmp_path / "models")
    monkeypatch.setattr(install_models, "_verification_record", lambda: tmp_path / "verified.json")
    monkeypatch.setattr(install_models, "hub_library", lambda: hub)
    monkeypatch.setattr(install_models, "pinned_models", lambda: pins)
    monkeypatch.setattr(install_models, "_runtime_installed", lambda _model: True)
    monkeypatch.setattr(
        openvino_runtime, "telemetry_consent_file", lambda: tmp_path / "openvino_telemetry"
    )
    return hub


def _installed_path(repo_id: str, name: str) -> Path:
    return model_files.model_directory(repo_id, REVISION) / name


def test_the_install_declines_openvino_telemetry_before_any_load(two_models, tmp_path):
    install_models.main(["--check"])

    assert (tmp_path / "openvino_telemetry").read_text(encoding="utf-8").strip() == "0"


def test_missing_weights_are_fetched_at_the_pinned_commit_and_verified(two_models, capsys):
    assert install_models.main([]) == 0

    assert [(repo, rev) for repo, rev, _ in two_models.downloads] == [
        ("org/encoder", REVISION),
        ("org/reranker", REVISION),
    ]
    assert all(patterns == [WEIGHTS, COMPANION] for _, _, patterns in two_models.downloads)
    assert _installed_path("org/encoder", WEIGHTS).read_bytes() == b"encoder weights"
    assert capsys.readouterr().out.count("fetched") == 2


def test_the_files_are_ordinary_files_under_the_pinned_revision(two_models, tmp_path):
    install_models.main([])

    weights = tmp_path / "models" / "org--reranker" / REVISION / WEIGHTS
    assert (weights.is_file(), weights.is_symlink()) == (True, False)


def test_present_verified_weights_are_not_fetched_again(two_models, capsys):
    install_models.main([])
    two_models.downloads.clear()

    assert install_models.main([]) == 0
    assert two_models.downloads == []
    assert capsys.readouterr().out.count("present") == 2


def test_a_file_that_does_not_match_its_pin_is_removed_and_the_run_fails(two_models, capsys):
    two_models.contents["org/reranker"] = b"not the pinned bytes"

    assert install_models.main([]) == install_models.EXIT_INCOMPLETE
    assert not _installed_path("org/reranker", WEIGHTS).exists()
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
    assert (encoder.repo_id, encoder.revision, encoder.read_files) == (
        embedding_model.EMBEDDING_MODEL,
        embedding_model.EMBEDDING_MODEL_REVISION,
        embedding_model.EMBEDDING_READ_FILES,
    )
    assert (cross_encoder.repo_id, cross_encoder.revision, cross_encoder.read_files) == (
        reranker.DEFAULT_RERANKER_MODEL,
        reranker.DEFAULT_RERANKER_REVISION,
        reranker.RERANKER_READ_FILES,
    )
    assert all(
        len(model.weights_sha256) == 64 and model.weights_bytes > 0
        for model in (encoder, cross_encoder)
    )


def test_doctor_names_the_missing_weights_and_the_command(monkeypatch):
    import doctor

    monkeypatch.setattr(install_models, "wanted_models", install_models.pinned_models)
    monkeypatch.setattr(
        install_models, "missing_models", lambda: [install_models.pinned_models()[1]]
    )

    finding = doctor._models_check()

    assert finding["status"] == "degraded"
    assert "Horizon-Labs/multilingual-reranker-base@7330f6d0b3f5" in finding["message"]
    assert "install_models.py" in finding["message"]
    assert finding["details"]["command"] == "uv run python scripts/install_models.py"


def test_doctor_expects_no_weights_without_a_model_runtime(monkeypatch):
    import doctor

    monkeypatch.setattr(install_models, "wanted_models", lambda: [])

    assert doctor._models_check()["status"] == "ok"


def test_the_nightly_fetches_missing_weights_after_its_index_work():
    import scheduled_nightly

    steps = {step.label: step for step in scheduled_nightly._post_compile_steps()}
    labels = list(steps)

    assert labels.index("models") > labels.index("lint")
    assert steps["models"].command[-1].endswith("install_models.py")


def test_weights_without_their_companion_file_are_fetched_again(two_models):
    install_models.main([])
    _installed_path("org/encoder", COMPANION).unlink()
    two_models.downloads.clear()

    assert install_models.main([]) == 0
    assert [repo for repo, _, _ in two_models.downloads] == ["org/encoder"]


def test_verified_weights_delete_the_revision_they_replace_from_the_hub_cache(two_models, capsys):
    two_models.cache.held = [RETIRED, ("org/unrelated", "d" * 40)]

    assert install_models.main(["--json"]) == 0
    assert two_models.cache.deleted == [RETIRED[1]]
    assert '"retired": ["org/old-encoder@eeeeeeeeeeee"]' in capsys.readouterr().out


def test_a_check_or_a_failed_fetch_retires_nothing(two_models):
    two_models.cache.held = [RETIRED]
    two_models.contents["org/encoder"] = b"not the pinned bytes"

    install_models.main(["--check"])
    install_models.main([])

    assert two_models.cache.deleted == []


def test_no_hub_cache_at_all_retires_nothing_and_does_not_fail(two_models, monkeypatch):
    def no_cache():
        raise _CacheNotFound("no cache directory")

    monkeypatch.setattr(two_models, "scan_cache_dir", no_cache)

    assert install_models.main([]) == 0


def test_the_text_report_names_the_revision_it_removed(two_models, capsys):
    two_models.cache.held = [RETIRED]

    install_models.main([])

    assert "fetched org/encoder@ffffffffffff; removed org/old-encoder@eeeeeeeeeeee" in (
        capsys.readouterr().out
    )


def test_a_model_whose_runtime_is_absent_is_not_fetched(two_models, monkeypatch):
    monkeypatch.setattr(
        install_models, "_runtime_installed", lambda model: model.repo_id == "org/encoder"
    )

    assert install_models.main([]) == 0
    assert [repo for repo, _, _ in two_models.downloads] == ["org/encoder"]


def test_with_no_runtime_installed_there_is_nothing_to_fetch_and_no_failure(monkeypatch, capsys):
    """A base install's nightly `models` step is not a failed night."""
    monkeypatch.setattr(install_models, "_runtime_installed", lambda _model: False)
    monkeypatch.setattr(install_models, "hub_library", lambda: None)

    assert install_models.main([]) == 0
    assert "nothing to fetch" in capsys.readouterr().out
