from __future__ import annotations

import importlib
import json
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

helpers = importlib.import_module("tests.retrieval_v2_helpers")
outputs = importlib.import_module("retrieval_v2_outputs")
corpus_module = importlib.import_module("retrieval_v2_corpus")


@pytest.fixture
def portable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(outputs, "_use_posix_dir_fd", lambda: False)


def test_a_path_that_cannot_be_inspected_counts_as_a_reparse_point(tmp_path: Path) -> None:
    assert outputs.is_reparse_point(tmp_path / "missing") is True
    assert outputs.is_reparse_point(tmp_path) is False


def test_a_symlinked_model_cache_root_is_refused(tmp_path: Path) -> None:
    (tmp_path / "real").mkdir()
    (tmp_path / "linked").symlink_to(tmp_path / "real", target_is_directory=True)

    with pytest.raises(ValueError, match="model cache root must not be a symlink"):
        outputs.prepared_model_cache_root(tmp_path / "linked")


def test_a_model_cache_root_that_turns_into_a_reparse_point_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(outputs, "is_reparse_point", lambda path: True)

    with pytest.raises(ValueError, match="model cache root must not be a symlink"):
        outputs.prepared_model_cache_root(tmp_path / "fresh")
    assert (tmp_path / "fresh").is_dir()


def test_a_model_cache_subdirectory_linked_outside_the_root_is_refused(tmp_path: Path) -> None:
    (tmp_path / "elsewhere").mkdir()
    root = tmp_path / "cache"
    root.mkdir()
    (root / "torch").symlink_to(tmp_path / "elsewhere", target_is_directory=True)

    with (
        pytest.raises(ValueError, match="model cache path escaped isolated cache root"),
        outputs.model_cache_environment(root, allow_download=False),
    ):
        raise AssertionError("the environment must not be entered")
    assert "TORCH_HOME" not in os.environ or os.environ["TORCH_HOME"] != str(root / "torch")


def test_a_missing_path_never_has_the_expected_identity(tmp_path: Path) -> None:
    identity = tmp_path.stat()

    assert outputs.same_path_identity(tmp_path / "missing", identity) is False
    assert outputs.same_path_identity(tmp_path, identity) is True


@pytest.mark.skipif(os.name == "nt", reason="Windows cannot open a directory as a descriptor")
def test_a_descriptor_of_another_directory_is_refused(tmp_path: Path) -> None:
    (tmp_path / "other").mkdir()
    descriptor = os.open(tmp_path / "other", os.O_RDONLY)
    try:
        with pytest.raises(PermissionError, match="cache root changed"):
            outputs._require_descriptor_identity(descriptor, tmp_path.stat(), "cache root changed")
    finally:
        os.close(descriptor)


def _default_corpus() -> Any:
    return corpus_module.load_corpus(helpers.CORPUS, helpers.SCHEMA)


def test_the_portable_fake_index_is_published_whole_without_temporary_files(
    tmp_path: Path, portable: None
) -> None:
    corpus = _default_corpus()

    written = outputs.write_fake_index(corpus, tmp_path / "cache")

    published = tmp_path / "cache" / "fake-index.json"
    assert published.stat().st_size == written
    assert published.read_bytes() == outputs._fake_index_bytes(corpus)
    assert [path.name for path in (tmp_path / "cache").iterdir()] == ["fake-index.json"]


def test_a_portable_output_is_created_once_and_never_overwritten(
    tmp_path: Path, portable: None
) -> None:
    output = tmp_path / "report.json"

    outputs.write_new_output(output, '{"a":1}\n')

    assert json.loads(output.read_text(encoding="utf-8")) == {"a": 1}
    with pytest.raises(FileExistsError):
        outputs.write_new_output(output, "replacement")
    assert output.read_text(encoding="utf-8") == '{"a":1}\n'


@pytest.mark.parametrize("posix", [True, False])
def test_a_publication_that_fails_after_opening_removes_its_partial_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, posix: bool
) -> None:
    monkeypatch.setattr(outputs, "_use_posix_dir_fd", lambda: posix)
    output = tmp_path / "report.json"
    not_bytes: Any = "text where bytes belong"

    with pytest.raises(TypeError):
        outputs.write_new_bytes(output, not_bytes)
    assert output.exists() is False


def _fsync_hook(monkeypatch: pytest.MonkeyPatch, interfere: Callable[[], None]) -> None:
    real_fsync = os.fsync
    calls: list[int] = []

    def fsync(descriptor: int) -> None:
        calls.append(descriptor)
        real_fsync(descriptor)
        interfere()

    monkeypatch.setattr(outputs.os, "fsync", fsync)


def test_an_output_replaced_during_publication_is_refused_and_the_stranger_kept(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output = tmp_path / "out" / "report.json"
    output.parent.mkdir()

    def replace_output() -> None:
        output.unlink()
        output.write_text("stranger", encoding="utf-8")

    _fsync_hook(monkeypatch, replace_output)

    with pytest.raises(PermissionError, match="output changed during publication"):
        outputs.write_new_output(output, "report")
    assert output.read_text(encoding="utf-8") == "stranger"


def test_an_output_whose_parent_moved_during_publication_is_refused_and_removed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    parent = tmp_path / "out"
    parent.mkdir()
    moved = tmp_path / "moved"

    def move_parent() -> None:
        parent.rename(moved)
        parent.mkdir()

    _fsync_hook(monkeypatch, move_parent)

    with pytest.raises(PermissionError, match="output parent changed during publication"):
        outputs.write_new_output(parent / "report.json", "report")
    assert list(moved.iterdir()) == []
    assert list(parent.iterdir()) == []


def test_an_output_inside_the_source_tree_is_refused() -> None:
    with pytest.raises(ValueError, match="--output must be outside source and vault roots"):
        outputs.require_outside_source_and_vault(helpers.ROOT.resolve() / "report.json")
