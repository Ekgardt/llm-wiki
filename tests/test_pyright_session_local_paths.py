from __future__ import annotations

import stat
import time
from pathlib import Path

import pytest
from llm_wiki.pyright_session_paths import (
    ensure_lsp_parent,
    path_identity,
    require_startup_deadline,
    validated_local_file,
)


def _deadline() -> float:
    return time.monotonic() + 5.0


def test_an_expired_startup_deadline_is_refused() -> None:
    with pytest.raises(TimeoutError, match="Pyright startup deadline expired"):
        require_startup_deadline(time.monotonic() - 1.0)


def test_a_symbolic_link_is_not_an_acceptable_path(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.write_bytes(b"x")
    link = tmp_path / "link"
    link.symlink_to(target)

    with pytest.raises(PermissionError, match="must not traverse a link"):
        path_identity(link)


def test_a_local_regular_file_is_accepted(tmp_path: Path) -> None:
    server = tmp_path / "server.js"
    server.write_bytes(b"server")

    assert validated_local_file(server, "server", deadline=_deadline()) == server


def test_a_value_that_is_not_a_path_is_refused() -> None:
    with pytest.raises(TypeError, match="server must be a Path"):
        validated_local_file("/bin/server", "server", deadline=_deadline())


@pytest.mark.parametrize("value", [Path("relative/server.js"), Path("/opt/../server.js")])
def test_a_relative_or_traversing_path_is_refused(value: Path) -> None:
    with pytest.raises(ValueError, match="server must be an absolute local path"):
        validated_local_file(value, "server", deadline=_deadline())


def test_a_file_below_a_non_directory_is_refused(tmp_path: Path) -> None:
    blocker = tmp_path / "blocker"
    blocker.write_bytes(b"not a directory")

    with pytest.raises(ValueError, match="server parent must be a directory"):
        validated_local_file(blocker / "server.js", "server", deadline=_deadline())


def test_a_directory_is_not_a_local_regular_file(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="server must be a local regular file"):
        validated_local_file(tmp_path, "server", deadline=_deadline())


def test_the_lsp_parent_is_created_owner_only_under_the_state_root(tmp_path: Path) -> None:
    parent = ensure_lsp_parent(tmp_path, deadline=_deadline())

    assert parent == tmp_path / "run" / "lsp"
    assert stat.S_IMODE(parent.stat().st_mode) == 0o700


def test_an_existing_lsp_parent_is_reused(tmp_path: Path) -> None:
    first = ensure_lsp_parent(tmp_path, deadline=_deadline())
    (first / "owner").mkdir()

    second = ensure_lsp_parent(tmp_path, deadline=_deadline())

    assert (second, sorted(path.name for path in second.iterdir())) == (first, ["owner"])


def test_a_relative_state_root_is_refused() -> None:
    with pytest.raises(ValueError, match="state_root must be an absolute local path"):
        ensure_lsp_parent(Path("relative-state"), deadline=_deadline())


def test_a_state_root_that_is_a_file_is_refused(tmp_path: Path) -> None:
    state_root = tmp_path / "state"
    state_root.write_bytes(b"file")

    with pytest.raises(NotADirectoryError):
        ensure_lsp_parent(state_root, deadline=_deadline())


def test_a_run_entry_that_is_a_file_is_refused(tmp_path: Path) -> None:
    (tmp_path / "run").write_bytes(b"file")

    with pytest.raises(PermissionError, match="LSP run parent must be a directory"):
        ensure_lsp_parent(tmp_path, deadline=_deadline())
