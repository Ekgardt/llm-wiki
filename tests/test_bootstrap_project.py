from __future__ import annotations

import json
import os
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import bootstrap_project
import pytest
import session_start_project_state


def _configure_owned_project(monkeypatch, tmp_path: Path) -> tuple[Path, Path, Path]:
    vault = tmp_path / "vault"
    projects = vault / "knowledge" / "projects"
    project = tmp_path / "active"
    project.mkdir()
    state_path = projects / "active" / "state.md"
    state_path.parent.mkdir(parents=True)
    state_path.write_text(
        f"- Project root JSON: {json.dumps(str(project.resolve()))}\n"
        '- Runtime slug JSON: "active"\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(bootstrap_project, "ROOT", vault)
    monkeypatch.setattr(bootstrap_project, "STATE_DIR", tmp_path / "run")
    monkeypatch.setattr(bootstrap_project, "PROJECTS_DIR", projects)
    monkeypatch.setenv("LLM_WIKI_STATE_ROOT", str(tmp_path / "runtime"))
    return project, projects, state_path


def test_bootstrap_has_no_slug_fallback_when_identity_is_unavailable(
    monkeypatch, tmp_path: Path
):
    vault = tmp_path / "vault"
    projects = vault / "knowledge" / "projects"
    project = tmp_path / "Alpha`[Beta]#(Gamma)_Delta!"
    project.mkdir()
    projects.mkdir(parents=True)
    monkeypatch.setattr(bootstrap_project, "ROOT", vault)
    monkeypatch.setattr(bootstrap_project, "STATE_DIR", tmp_path / "run")
    monkeypatch.setattr(bootstrap_project, "PROJECTS_DIR", projects)

    result = bootstrap_project.bootstrap(str(project), apply=True)

    assert "identity unavailable" in result.lower()
    assert list(projects.rglob("bootstrap.md")) == []


def test_bootstrap_dry_run_unresolvable_path_returns_safe_status(
    monkeypatch,
    tmp_path,
):
    project = tmp_path / "unresolvable"
    real_resolve = Path.resolve

    def failing_resolve(path, *args, **kwargs):
        if path == project:
            raise OSError("path unavailable")
        return real_resolve(path, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", failing_resolve)

    result = bootstrap_project.bootstrap(str(project), apply=False)

    assert "project path unavailable" in result.lower()
    assert not (tmp_path / "run").exists()
    assert not (tmp_path / "knowledge").exists()


def test_bootstrap_dry_run_is_byte_for_byte_vault_read_only(
    monkeypatch,
    tmp_path: Path,
):
    vault = tmp_path / "vault"
    projects = vault / "knowledge" / "projects"
    template = projects / "_template" / "state.md"
    template.parent.mkdir(parents=True)
    template.write_text(
        "# <Project Name>\n- Project root JSON: <absolute path JSON>\n",
        encoding="utf-8",
    )
    project = tmp_path / "new-project"
    (project / ".git").mkdir(parents=True)
    (project / "README.md").write_text(
        "# Preview\n\nDry-run project description.\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(bootstrap_project, "ROOT", vault)
    monkeypatch.setattr(bootstrap_project, "PROJECTS_DIR", projects)
    monkeypatch.setattr(bootstrap_project, "STATE_DIR", tmp_path / "runtime" / "run")
    monkeypatch.setenv("LLM_WIKI_STATE_ROOT", str(tmp_path / "runtime"))

    def snapshot() -> list[tuple[str, str, bytes | None]]:
        return [
            (
                path.relative_to(vault).as_posix(),
                "dir" if path.is_dir() else "file",
                None if path.is_dir() else path.read_bytes(),
            )
            for path in sorted(vault.rglob("*"))
        ]

    before = snapshot()
    result = bootstrap_project.bootstrap(str(project), apply=False)
    after = snapshot()

    assert "Dry-run project description." in result
    assert after == before
    assert not (projects / "new-project").exists()
    assert not (tmp_path / "runtime").exists()


def test_bootstrap_readme_read_requests_an_explicit_byte_bound(monkeypatch, tmp_path):
    readme = tmp_path / "README.md"
    readme.write_text("# Project\n\nBOUNDED_README_SUMMARY\n", encoding="utf-8")
    real_open = Path.open
    real_read_text = Path.read_text
    read_sizes: list[int] = []

    class TrackingFile:
        def __init__(self, handle):
            self.handle = handle

        def __enter__(self):
            return self

        def __exit__(self, *exc_info):
            return self.handle.__exit__(*exc_info)

        def __getattr__(self, name):
            return getattr(self.handle, name)

        def read(self, size=-1):
            read_sizes.append(size)
            return self.handle.read(size)

    def reject_read_text(path, *args, **kwargs):
        if path == readme:
            raise AssertionError("bootstrap README read must be byte-bounded")
        return real_read_text(path, *args, **kwargs)

    def tracking_open(path, *args, **kwargs):
        handle = real_open(path, *args, **kwargs)
        mode = args[0] if args else kwargs.get("mode", "r")
        if path == readme and "r" in mode:
            assert "b" in mode
            return TrackingFile(handle)
        return handle

    monkeypatch.setattr(Path, "read_text", reject_read_text)
    monkeypatch.setattr(Path, "open", tracking_open)

    assert bootstrap_project._extract_readme_summary(str(tmp_path)) == "BOUNDED_README_SUMMARY"
    assert read_sizes and all(size > 0 for size in read_sizes)


def test_bootstrap_oversized_package_json_is_skipped(monkeypatch, tmp_path):
    package = tmp_path / "package.json"
    package.write_text(
        json.dumps({"dependencies": {"react": "1"}, "padding": "x" * 500}),
        encoding="utf-8",
    )
    monkeypatch.setattr(bootstrap_project, "MAX_PACKAGE_JSON_BYTES", 128, raising=False)

    stack = bootstrap_project._extract_tech_stack(str(tmp_path))

    assert "- Node.js / JavaScript (`package.json`)" in stack
    assert "- React" not in stack


@pytest.mark.skipif(os.name != "posix", reason="POSIX symlink semantics")
@pytest.mark.parametrize("target_exists", (True, False))
def test_bootstrap_readme_rejects_external_and_broken_symlinks(
    tmp_path,
    target_exists,
):
    project = tmp_path / "project"
    project.mkdir()
    outside = tmp_path / "outside-readme.md"
    if target_exists:
        outside.write_text("# Outside\n\nEXTERNAL_README_MUST_NOT_BE_READ\n", encoding="utf-8")
    (project / "README.md").symlink_to(outside)

    summary = bootstrap_project._extract_readme_summary(str(project))

    assert summary == "(no README found)"
    assert "EXTERNAL_README_MUST_NOT_BE_READ" not in summary


@pytest.mark.skipif(os.name != "posix", reason="POSIX symlink semantics")
def test_bootstrap_rejects_readme_swapped_to_external_symlink_before_open(
    monkeypatch,
    tmp_path,
):
    project = tmp_path / "project"
    project.mkdir()
    readme = project / "README.md"
    readme.write_text("# Original\n\nORIGINAL_MUST_NOT_BE_READ_AFTER_SWAP\n", encoding="utf-8")
    outside = tmp_path / "outside-readme.md"
    outside.write_text("# Outside\n\nEXTERNAL_MUST_NOT_BE_READ_AFTER_SWAP\n", encoding="utf-8")
    real_lstat = Path.lstat
    swapped = False

    def swapping_lstat(path):
        nonlocal swapped
        metadata = real_lstat(path)
        if path == readme and not swapped:
            swapped = True
            readme.unlink()
            readme.symlink_to(outside)
        return metadata

    monkeypatch.setattr(Path, "lstat", swapping_lstat)

    summary = bootstrap_project._extract_readme_summary(str(project))

    assert swapped is True
    assert summary == "(no README found)"


def test_bootstrap_readme_rejects_mocked_windows_reparse_point(
    monkeypatch,
    tmp_path,
):
    readme = tmp_path / "README.md"
    readme.write_text("# Reparse\n\nREPARSE_CONTENT_MUST_NOT_BE_READ\n", encoding="utf-8")
    real_lstat = Path.lstat

    class ReparseMetadata:
        def __init__(self, metadata):
            self._metadata = metadata
            self.st_mode = metadata.st_mode
            self.st_size = metadata.st_size
            self.st_file_attributes = 0x400

        def __getattr__(self, name):
            return getattr(self._metadata, name)

    monkeypatch.setattr(
        Path,
        "lstat",
        lambda path: ReparseMetadata(real_lstat(path))
        if path == readme
        else real_lstat(path),
    )

    assert bootstrap_project._extract_readme_summary(str(tmp_path)) == "(no README found)"


@pytest.mark.skipif(os.name != "posix", reason="POSIX symlink semantics")
def test_bootstrap_package_json_rejects_external_symlink(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    outside = tmp_path / "outside-package.json"
    outside.write_text(
        json.dumps({"dependencies": {"react": "1"}}),
        encoding="utf-8",
    )
    (project / "package.json").symlink_to(outside)

    stack = bootstrap_project._extract_tech_stack(str(project))

    assert "- Node.js / JavaScript (`package.json`)" not in stack
    assert "- React" not in stack


@pytest.mark.parametrize(
    "dependencies",
    (None, [], "react", 1, True),
)
def test_bootstrap_package_json_guards_dependency_mapping_types(
    tmp_path,
    dependencies,
):
    (tmp_path / "package.json").write_text(
        json.dumps(
            {
                "dependencies": dependencies,
                "devDependencies": {"typescript": "1"},
            }
        ),
        encoding="utf-8",
    )

    stack = bootstrap_project._extract_tech_stack(str(tmp_path))

    assert "- Node.js / JavaScript (`package.json`)" in stack
    assert "- React" not in stack
    assert "- TypeScript" in stack


def test_bootstrap_project_json_reads_require_strict_utf8(tmp_path):
    (tmp_path / "README.md").write_bytes(b"# Project\n\ninvalid: \xff\n")
    (tmp_path / "package.json").write_bytes(
        b'{"dependencies":{"react":"1"},"invalid":"\xff"}'
    )

    assert bootstrap_project._extract_readme_summary(str(tmp_path)) == "(no README found)"
    stack = bootstrap_project._extract_tech_stack(str(tmp_path))
    assert "- Node.js / JavaScript (`package.json`)" in stack
    assert "- React" not in stack


def test_bootstrap_docs_scan_stops_at_output_limit(monkeypatch, tmp_path):
    docs = tmp_path / "docs"
    docs.mkdir()
    for index in range(20):
        (docs / f"doc-{index:02}.md").write_text("doc\n", encoding="utf-8")
    monkeypatch.setattr(bootstrap_project, "MAX_DOC_FILES", 15, raising=False)

    result = bootstrap_project._extract_docs_structure(str(tmp_path))

    assert len(result) == 15
    assert result == [f"- `docs/doc-{index:02}.md`" for index in range(15)]


@pytest.mark.skipif(os.name != "posix", reason="POSIX symlink semantics")
def test_bootstrap_docs_inventory_does_not_follow_symlinked_root(tmp_path):
    external_docs = tmp_path / "external-docs"
    external_docs.mkdir()
    (external_docs / "private.md").write_text("PRIVATE_DOC\n", encoding="utf-8")
    project = tmp_path / "project"
    project.mkdir()
    (project / "docs").symlink_to(external_docs, target_is_directory=True)

    assert bootstrap_project._extract_docs_structure(str(project)) is None


@pytest.mark.skipif(os.name != "nt", reason="Windows junction semantics")
def test_bootstrap_nested_junction_makes_inventory_incomplete_without_scanning_target(
    monkeypatch,
    tmp_path,
):
    import memory_state

    project = tmp_path / "project"
    nested = project / "docs" / "nested"
    nested.mkdir(parents=True)
    external = tmp_path / "external-docs"
    external.mkdir()
    (external / "private.md").write_text("PRIVATE_DOC\n", encoding="utf-8")
    junction = nested / "external-junction"
    command = [
        os.environ.get("COMSPEC", "cmd.exe"),
        "/d",
        "/c",
        "mklink",
        "/J",
        str(junction),
        str(external),
    ]
    created = subprocess.run(command, capture_output=True, check=False)
    if created.returncode != 0:
        detail = created.stderr.decode(errors="replace")
        pytest.skip(f"directory junctions are unavailable: {detail}")

    scanned: list[Path] = []
    real_scandir = memory_state.os.scandir

    def guarded_scandir(path):
        candidate = Path(path)
        scanned.append(candidate)
        if candidate == junction:
            raise AssertionError("external junction target was scanned")
        return real_scandir(path)

    monkeypatch.setattr(memory_state.os, "scandir", guarded_scandir)

    assert bootstrap_project._extract_docs_structure(str(project)) is None
    assert junction not in scanned


@pytest.mark.skipif(os.name != "nt", reason="Windows directory sharing semantics")
def test_bounded_inventory_holds_empty_directory_against_junction_aba(
    monkeypatch,
    tmp_path,
):
    import memory_state

    root = tmp_path / "docs"
    nested = root / "nested"
    nested.mkdir(parents=True)
    external = tmp_path / "external-docs"
    external.mkdir()
    external_private = external / "private.md"
    external_private.write_text("PRIVATE_DOC\n", encoding="utf-8")
    probe = tmp_path / "junction-probe"
    command = [
        os.environ.get("COMSPEC", "cmd.exe"),
        "/d",
        "/c",
        "mklink",
        "/J",
        str(probe),
        str(external),
    ]
    created = subprocess.run(command, capture_output=True, check=False)
    if created.returncode != 0:
        detail = created.stderr.decode(errors="replace")
        pytest.skip(f"directory junctions are unavailable: {detail}")
    probe.rmdir()

    real_scandir = memory_state.os.scandir
    attempted = False
    denied: OSError | None = None
    scanned: list[Path] = []
    consumed: list[str] = []

    class TrackingScandir:
        def __init__(self, iterator):
            self.iterator = iterator

        def __enter__(self):
            self.iterator.__enter__()
            return self

        def __exit__(self, *exc_info):
            return self.iterator.__exit__(*exc_info)

        def __iter__(self):
            return self

        def __next__(self):
            entry = next(self.iterator)
            consumed.append(entry.name)
            return entry

        def close(self):
            self.iterator.close()

    def attacking_scandir(path):
        nonlocal attempted, denied
        candidate = Path(path)
        scanned.append(candidate)
        if candidate != nested:
            return real_scandir(path)
        attempted = True
        held = root / "nested-original"
        try:
            nested.rename(held)
        except OSError as exc:
            denied = exc
            return TrackingScandir(real_scandir(nested))

        created = subprocess.run(
            command[:-2] + [str(nested), str(external)],
            capture_output=True,
            check=False,
        )
        if created.returncode != 0:
            held.rename(nested)
            detail = created.stderr.decode(errors="replace")
            pytest.skip(f"directory junctions are unavailable: {detail}")
        iterator = real_scandir(nested)
        nested.rmdir()
        held.rename(nested)
        return TrackingScandir(iterator)

    monkeypatch.setattr(memory_state.os, "scandir", attacking_scandir)

    inventory = memory_state.bounded_path_inventory(
        root,
        "*.md",
        10,
        recursive=True,
        kind="file",
        required_root=True,
    )

    assert attempted is True
    assert denied is not None
    assert getattr(denied, "winerror", None) in {5, 32}
    assert inventory.paths == ()
    assert inventory.error is False
    assert consumed == []
    assert scanned == [root, nested]
    assert nested.is_dir()
    assert external_private.read_text(encoding="utf-8") == "PRIVATE_DOC\n"


def test_windows_directory_handle_uses_list_access_when_delete_is_unavailable(
    monkeypatch,
    tmp_path,
):
    import ctypes

    import memory_state

    root = tmp_path / "docs"
    root.mkdir()
    create_calls = []
    close_calls = []
    invalid_handle = ctypes.c_void_p(-1).value

    class FakeFunction:
        def __init__(self, callback):
            self.callback = callback

        def __call__(self, *args):
            return self.callback(*args)

    class FakeKernel32:
        pass

    def create_file(*args):
        create_calls.append(args)
        if args[1] == 0x00010000:
            return invalid_handle
        assert args[1] == 0x0001
        return 73

    kernel32 = FakeKernel32()
    kernel32.CreateFileW = FakeFunction(create_file)
    kernel32.CloseHandle = FakeFunction(
        lambda handle: close_calls.append(handle) or True
    )
    monkeypatch.setattr(
        memory_state,
        "_load_windows_kernel32",
        lambda: kernel32,
        raising=False,
    )

    handle_state = memory_state._open_windows_directory_handle(root)
    try:
        assert handle_state == (kernel32, 73)
        assert len(create_calls) == 1
        assert create_calls[0][1] == 0x0001
        assert create_calls[0][2] == 0x00000001 | 0x00000002
        assert create_calls[0][4] == 3
        assert create_calls[0][5] == 0x02000000 | 0x00200000
    finally:
        assert memory_state._close_windows_directory_handle(handle_state) is True
    assert close_calls == [73]


@pytest.mark.skipif(os.name != "nt", reason="Windows directory access semantics")
def test_windows_directory_handle_lists_system_directory_without_delete_access():
    import ctypes
    from ctypes import wintypes

    import memory_state

    windows = Path(os.environ.get("SystemRoot", r"C:\Windows"))
    if not windows.is_dir():
        pytest.skip("Windows system directory is unavailable")

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_file = kernel32.CreateFileW
    create_file.argtypes = (
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    )
    create_file.restype = wintypes.HANDLE
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = (wintypes.HANDLE,)
    close_handle.restype = wintypes.BOOL
    invalid_handle = ctypes.c_void_p(-1).value
    delete_handle = create_file(
        str(windows),
        0x00010000,
        0x00000001 | 0x00000002,
        None,
        3,
        0x02000000 | 0x00200000,
        None,
    )
    delete_error = ctypes.get_last_error()
    if delete_handle != invalid_handle:
        close_handle(delete_handle)
        pytest.skip("DELETE access is available on the Windows system directory")
    if delete_error != 5:
        pytest.skip(f"DELETE access failed with Windows error {delete_error}, not 5")

    expected = windows.lstat()
    handle_state = memory_state._open_windows_directory_handle(windows)
    try:
        device, inode, attributes = memory_state._windows_directory_handle_metadata(
            handle_state
        )
        assert (device, inode) == (expected.st_dev, expected.st_ino)
        assert attributes & 0x10
        assert not attributes & 0x400
        with os.scandir(windows) as entries:
            assert next(entries, None) is not None
    finally:
        assert memory_state._close_windows_directory_handle(handle_state) is True


def test_bounded_inventory_windows_handle_open_failure_stops_before_scan(
    monkeypatch,
    tmp_path,
):
    import memory_state

    root = tmp_path / "docs"
    root.mkdir()
    scanned: list[Path] = []
    real_scandir = memory_state.os.scandir

    def failing_open(path):
        assert path == root
        raise OSError("CreateFileW failed")

    def tracking_scandir(path):
        scanned.append(Path(path))
        return real_scandir(path)

    monkeypatch.setattr(
        memory_state,
        "_USE_WINDOWS_DIRECTORY_HANDLES",
        True,
        raising=False,
    )
    monkeypatch.setattr(
        memory_state,
        "_open_windows_directory_handle",
        failing_open,
        raising=False,
    )
    monkeypatch.setattr(memory_state.os, "scandir", tracking_scandir)

    inventory = memory_state.bounded_path_inventory(
        root,
        "*",
        10,
        recursive=False,
        kind="file",
        required_root=True,
    )

    assert inventory.paths == ()
    assert inventory.error is True
    assert scanned == []


def test_bounded_inventory_windows_handle_validation_failure_closes_handle(
    monkeypatch,
    tmp_path,
):
    import memory_state

    root = tmp_path / "docs"
    root.mkdir()
    handle = object()
    closed: list[object] = []
    scanned: list[Path] = []
    real_scandir = memory_state.os.scandir

    def tracking_scandir(path):
        scanned.append(Path(path))
        return real_scandir(path)

    monkeypatch.setattr(
        memory_state,
        "_USE_WINDOWS_DIRECTORY_HANDLES",
        True,
        raising=False,
    )
    monkeypatch.setattr(
        memory_state,
        "_open_windows_directory_handle",
        lambda _path: handle,
        raising=False,
    )
    monkeypatch.setattr(
        memory_state,
        "_windows_directory_handle_metadata",
        lambda _handle: (root.lstat().st_dev, root.lstat().st_ino, 0x410),
        raising=False,
    )
    monkeypatch.setattr(
        memory_state,
        "_close_windows_directory_handle",
        lambda value: closed.append(value) or True,
        raising=False,
    )
    monkeypatch.setattr(memory_state.os, "scandir", tracking_scandir)

    inventory = memory_state.bounded_path_inventory(
        root,
        "*",
        10,
        recursive=False,
        kind="file",
        required_root=True,
    )

    assert inventory.paths == ()
    assert inventory.error is True
    assert scanned == []
    assert closed == [handle]


def test_bounded_inventory_windows_close_failure_is_error_after_scanner_cleanup(
    monkeypatch,
    tmp_path,
):
    import memory_state

    root = tmp_path / "docs"
    root.mkdir()
    (root / "visible.md").write_text("DOC\n", encoding="utf-8")
    metadata = root.lstat()
    handle = object()
    scanner_closed = False
    close_calls: list[object] = []
    real_scandir = memory_state.os.scandir

    class TrackingScandir:
        def __init__(self, iterator):
            self.iterator = iterator

        def __enter__(self):
            self.iterator.__enter__()
            return self

        def __exit__(self, *exc_info):
            nonlocal scanner_closed
            scanner_closed = True
            return self.iterator.__exit__(*exc_info)

        def __iter__(self):
            return self

        def __next__(self):
            return next(self.iterator)

        def close(self):
            nonlocal scanner_closed
            scanner_closed = True
            self.iterator.close()

    def failing_close(value):
        assert scanner_closed is True
        close_calls.append(value)
        return False

    monkeypatch.setattr(
        memory_state,
        "_USE_WINDOWS_DIRECTORY_HANDLES",
        True,
        raising=False,
    )
    monkeypatch.setattr(
        memory_state,
        "_open_windows_directory_handle",
        lambda _path: handle,
        raising=False,
    )
    monkeypatch.setattr(
        memory_state,
        "_windows_directory_handle_metadata",
        lambda _handle: (metadata.st_dev, metadata.st_ino, 0x10),
        raising=False,
    )
    monkeypatch.setattr(
        memory_state,
        "_close_windows_directory_handle",
        failing_close,
        raising=False,
    )
    monkeypatch.setattr(
        memory_state.os,
        "scandir",
        lambda path: TrackingScandir(real_scandir(path)),
    )

    inventory = memory_state.bounded_path_inventory(
        root,
        "*.md",
        10,
        recursive=False,
        kind="file",
        required_root=True,
    )

    assert inventory.paths == ()
    assert inventory.error is True
    assert scanner_closed is True
    assert close_calls == [handle]


def test_bounded_inventory_rechecks_swapped_directory_before_consuming_entries(
    monkeypatch,
    tmp_path,
):
    import memory_state

    monkeypatch.setattr(
        memory_state,
        "_USE_WINDOWS_DIRECTORY_HANDLES",
        False,
        raising=False,
    )

    root = tmp_path / "docs"
    nested = root / "nested"
    nested.mkdir(parents=True)
    external = tmp_path / "external-docs"
    external.mkdir()
    (external / "private.md").write_text("PRIVATE_DOC\n", encoding="utf-8")
    real_lstat = Path.lstat
    real_scandir = memory_state.os.scandir
    swapped = False
    nested_lstat_calls = 0
    consumed: list[str] = []
    close_calls = 0

    class TrackingScandir:
        def __init__(self, iterator):
            self.iterator = iterator

        def __enter__(self):
            self.iterator.__enter__()
            return self

        def __exit__(self, *exc_info):
            nonlocal close_calls
            close_calls += 1
            return self.iterator.__exit__(*exc_info)

        def __iter__(self):
            return self

        def __next__(self):
            entry = next(self.iterator)
            consumed.append(entry.name)
            return entry

        def close(self):
            nonlocal close_calls
            close_calls += 1
            self.iterator.close()

    def swapping_lstat(path):
        nonlocal nested_lstat_calls, swapped
        metadata = real_lstat(path)
        if path != nested or swapped:
            return metadata
        nested_lstat_calls += 1
        if nested_lstat_calls == 1:
            return metadata
        nested.rmdir()
        if os.name == "nt":
            command = [
                os.environ.get("COMSPEC", "cmd.exe"),
                "/d",
                "/c",
                "mklink",
                "/J",
                str(nested),
                str(external),
            ]
            created = subprocess.run(command, capture_output=True, check=False)
            if created.returncode != 0:
                detail = created.stderr.decode(errors="replace")
                pytest.skip(f"directory junctions are unavailable: {detail}")
        else:
            try:
                nested.symlink_to(external, target_is_directory=True)
            except OSError as exc:
                pytest.skip(f"directory symlinks are unavailable: {exc}")
        swapped = True
        return metadata

    def tracking_scandir(path):
        iterator = real_scandir(path)
        return TrackingScandir(iterator) if Path(path) == nested else iterator

    monkeypatch.setattr(Path, "lstat", swapping_lstat)
    monkeypatch.setattr(memory_state.os, "scandir", tracking_scandir)

    inventory = memory_state.bounded_path_inventory(
        root,
        "*.md",
        10,
        recursive=True,
        kind="file",
        required_root=True,
    )

    assert swapped is True
    assert inventory.paths == ()
    assert inventory.error is True
    assert consumed == []
    assert close_calls == 1


def test_bounded_inventory_rejects_directory_aba_before_matching_external_entry(
    monkeypatch,
    tmp_path,
):
    import memory_state

    monkeypatch.setattr(
        memory_state,
        "_USE_WINDOWS_DIRECTORY_HANDLES",
        False,
        raising=False,
    )

    root = tmp_path / "docs"
    nested = root / "nested"
    nested.mkdir(parents=True)
    external = tmp_path / "external-docs"
    external.mkdir()
    external_private = external / "private.md"
    external_private.write_text("PRIVATE_DOC\n", encoding="utf-8")
    real_scandir = memory_state.os.scandir
    real_fnmatch = memory_state.fnmatch
    swapped = False
    scanned: list[Path] = []
    matched: list[str] = []

    def aba_scandir(path):
        nonlocal swapped
        candidate = Path(path)
        scanned.append(candidate)
        if candidate != nested:
            return real_scandir(path)
        held = root / "nested-original"
        nested.rename(held)
        if os.name == "nt":
            command = [
                os.environ.get("COMSPEC", "cmd.exe"),
                "/d",
                "/c",
                "mklink",
                "/J",
                str(nested),
                str(external),
            ]
            created = subprocess.run(command, capture_output=True, check=False)
            if created.returncode != 0:
                detail = created.stderr.decode(errors="replace")
                pytest.skip(f"directory junctions are unavailable: {detail}")
        else:
            try:
                nested.symlink_to(external, target_is_directory=True)
            except OSError as exc:
                pytest.skip(f"directory symlinks are unavailable: {exc}")
        iterator = real_scandir(nested)
        if os.name == "nt":
            nested.rmdir()
        else:
            nested.unlink()
        held.rename(nested)
        swapped = True
        return iterator

    def tracking_fnmatch(name, pattern):
        matched.append(name)
        return real_fnmatch(name, pattern)

    monkeypatch.setattr(memory_state.os, "scandir", aba_scandir)
    monkeypatch.setattr(memory_state, "fnmatch", tracking_fnmatch)

    inventory = memory_state.bounded_path_inventory(
        root,
        "*.md",
        10,
        recursive=True,
        kind="file",
        required_root=True,
    )

    assert swapped is True
    assert inventory.paths == ()
    assert inventory.error is True
    assert "private.md" not in matched
    assert scanned == [root, nested]
    assert external_private.read_text(encoding="utf-8") == "PRIVATE_DOC\n"


def test_bounded_inventory_rejects_same_name_different_entry_identity(
    monkeypatch,
    tmp_path,
):
    import memory_state

    root = tmp_path / "docs"
    root.mkdir()
    lexical = root / "private.md"
    lexical.write_text("LOCAL_DOC\n", encoding="utf-8")
    bound = tmp_path / "private.md"
    bound.write_text("EXTERNAL_DOC\n", encoding="utf-8")
    bound_metadata = bound.lstat()
    real_scandir = memory_state.os.scandir
    real_fnmatch = memory_state.fnmatch
    matched: list[str] = []

    class BoundEntry:
        def __init__(self, entry):
            self.entry = entry

        def stat(self, *, follow_symlinks=True):
            assert follow_symlinks is False
            return bound_metadata

        def __getattr__(self, name):
            return getattr(self.entry, name)

    class BoundScanner:
        def __init__(self, iterator):
            self.iterator = iterator

        def __enter__(self):
            self.iterator.__enter__()
            return self

        def __exit__(self, *exc_info):
            return self.iterator.__exit__(*exc_info)

        def __iter__(self):
            return self

        def __next__(self):
            return BoundEntry(next(self.iterator))

        def close(self):
            self.iterator.close()

    def bound_scandir(path):
        iterator = real_scandir(path)
        return BoundScanner(iterator) if Path(path) == root else iterator

    def tracking_fnmatch(name, pattern):
        matched.append(name)
        return real_fnmatch(name, pattern)

    monkeypatch.setattr(memory_state.os, "scandir", bound_scandir)
    monkeypatch.setattr(memory_state, "fnmatch", tracking_fnmatch)

    inventory = memory_state.bounded_path_inventory(
        root,
        "*.md",
        10,
        recursive=False,
        kind="file",
        required_root=True,
    )

    assert inventory.paths == ()
    assert inventory.error is True
    assert matched == []
    assert lexical.read_text(encoding="utf-8") == "LOCAL_DOC\n"
    assert bound.read_text(encoding="utf-8") == "EXTERNAL_DOC\n"


def test_bootstrap_docs_cap_counts_nonmatching_entries_and_reports_unavailable(
    monkeypatch,
    tmp_path,
):
    docs = tmp_path / "docs"
    docs.mkdir()
    for index in range(3):
        (docs / f"ignored-{index}.txt").write_text("not docs\n", encoding="utf-8")
    (docs / "visible.md").write_text("docs\n", encoding="utf-8")
    monkeypatch.setattr(bootstrap_project, "MAX_DOC_ENTRIES_SCANNED", 2)
    monkeypatch.setattr(bootstrap_project, "_extract_git_timeline", lambda _cwd: [])
    monkeypatch.setattr(bootstrap_project, "_extract_readme_summary", lambda _cwd: "Summary")
    monkeypatch.setattr(bootstrap_project, "_extract_tech_stack", lambda _cwd: [])
    monkeypatch.setattr(bootstrap_project, "_run_git", lambda _cwd, *_args: "")

    assert bootstrap_project._extract_docs_structure(str(tmp_path)) is None
    result = bootstrap_project.bootstrap(str(tmp_path), apply=False)
    assert "documentation inventory unavailable" in result.lower()


def test_concurrent_bootstrap_collects_project_once(monkeypatch, tmp_path: Path):
    project, projects, _state_path = _configure_owned_project(
        monkeypatch, tmp_path
    )
    monkeypatch.setattr(bootstrap_project, "_extract_git_timeline", lambda _cwd: [])
    monkeypatch.setattr(bootstrap_project, "_extract_tech_stack", lambda _cwd: [])
    monkeypatch.setattr(bootstrap_project, "_extract_docs_structure", lambda _cwd: [])
    monkeypatch.setattr(bootstrap_project, "_run_git", lambda _cwd, *_args: "")

    contenders = threading.Barrier(2)
    counter_lock = threading.Lock()
    active_collectors = 0
    max_active_collectors = 0
    collector_calls = 0

    def slow_summary(_cwd: str) -> str:
        nonlocal active_collectors, max_active_collectors, collector_calls
        with counter_lock:
            collector_calls += 1
            active_collectors += 1
            max_active_collectors = max(max_active_collectors, active_collectors)
        try:
            contenders.wait(timeout=0.2)
        except threading.BrokenBarrierError:
            pass
        time.sleep(0.02)
        with counter_lock:
            active_collectors -= 1
        return "Project summary"

    monkeypatch.setattr(bootstrap_project, "_extract_readme_summary", slow_summary)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _index: bootstrap_project.bootstrap(str(project), True), range(2)))

    assert collector_calls == 1
    assert max_active_collectors == 1
    bootstrap_path = projects / "active" / "bootstrap.md"
    assert bootstrap_path.is_file()
    assert "Project summary" in bootstrap_path.read_text(encoding="utf-8")
    assert all("bootstrap.md" in result for result in results)


def test_bootstrap_is_complete_when_it_becomes_visible(monkeypatch, tmp_path: Path):
    project, projects, _state_path = _configure_owned_project(
        monkeypatch, tmp_path
    )
    bootstrap_path = projects / "active" / "bootstrap.md"
    monkeypatch.setattr(bootstrap_project, "_extract_git_timeline", lambda _cwd: [])
    monkeypatch.setattr(
        bootstrap_project,
        "_extract_readme_summary",
        lambda _cwd: "Complete project summary",
    )
    monkeypatch.setattr(bootstrap_project, "_extract_tech_stack", lambda _cwd: [])
    monkeypatch.setattr(bootstrap_project, "_extract_docs_structure", lambda _cwd: [])
    monkeypatch.setattr(bootstrap_project, "_run_git", lambda _cwd, *_args: "")

    real_open = Path.open
    partial_visible = threading.Event()
    reader_finished = threading.Event()

    class SlowFinalHandle:
        def __init__(self, handle):
            self.handle = handle

        def __enter__(self):
            self.handle.__enter__()
            return self

        def __exit__(self, *exc_info):
            return self.handle.__exit__(*exc_info)

        def write(self, content: str) -> int:
            midpoint = len(content) // 2
            written = self.handle.write(content[:midpoint])
            self.handle.flush()
            partial_visible.set()
            reader_finished.wait(timeout=5)
            return written + self.handle.write(content[midpoint:])

        def __getattr__(self, name):
            return getattr(self.handle, name)

    def slow_final_open(path, *args, **kwargs):
        handle = real_open(path, *args, **kwargs)
        mode = args[0] if args else kwargs.get("mode", "r")
        if path == bootstrap_path and "w" in mode:
            return SlowFinalHandle(handle)
        return handle

    monkeypatch.setattr(Path, "open", slow_final_open)

    def observe_first_visible_content() -> str:
        partial_visible.wait(timeout=0.2)
        deadline = time.monotonic() + 5
        while not bootstrap_path.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        observed = bootstrap_path.read_text(encoding="utf-8")
        reader_finished.set()
        return observed

    with ThreadPoolExecutor(max_workers=2) as pool:
        reader = pool.submit(observe_first_visible_content)
        writer = pool.submit(bootstrap_project.bootstrap, str(project), True)
        writer.result(timeout=5)
        first_visible = reader.result(timeout=5)

    final_content = bootstrap_path.read_text(encoding="utf-8")
    assert first_visible == final_content
    assert final_content.endswith("Complete project summary\n\n")


def test_bootstrap_uses_exact_owned_state_path_and_records_provenance(
    monkeypatch, tmp_path: Path
):
    vault = tmp_path / "vault"
    projects = vault / "knowledge" / "projects"
    project = tmp_path / "workspace" / "active"
    project.mkdir(parents=True)
    state_path = projects / "legacy folder" / "state.md"
    state_path.parent.mkdir(parents=True)
    state_path.write_text(
        f"- Project root JSON: {json.dumps(str(project.resolve()))}\n"
        '- Runtime slug JSON: "active-safe"\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(bootstrap_project, "ROOT", vault)
    monkeypatch.setattr(bootstrap_project, "STATE_DIR", tmp_path / "run")
    monkeypatch.setattr(bootstrap_project, "PROJECTS_DIR", projects)
    monkeypatch.setenv("LLM_WIKI_STATE_ROOT", str(tmp_path / "runtime"))
    monkeypatch.setattr(bootstrap_project, "_extract_git_timeline", lambda _cwd: [])
    monkeypatch.setattr(bootstrap_project, "_extract_readme_summary", lambda _cwd: "Summary")
    monkeypatch.setattr(bootstrap_project, "_extract_tech_stack", lambda _cwd: [])
    monkeypatch.setattr(bootstrap_project, "_extract_docs_structure", lambda _cwd: [])
    monkeypatch.setattr(bootstrap_project, "_run_git", lambda _cwd, *_args: "")

    bootstrap_project.bootstrap(str(project), apply=True)

    bootstrap_path = state_path.with_name("bootstrap.md")
    body = bootstrap_path.read_text(encoding="utf-8")
    assert not (projects / "active-safe" / "bootstrap.md").exists()
    assert f"project_root_json: {json.dumps(str(project.resolve()))}" in body
    assert f"project_state_path_json: {json.dumps(str(state_path.resolve()))}" in body
    assert 'project_slug_json: "active-safe"' in body


def test_bootstrap_revalidates_identity_before_publishing(monkeypatch, tmp_path: Path):
    project, _projects, state_path = _configure_owned_project(monkeypatch, tmp_path)
    confirmations = iter(
        [
            ("active", state_path.resolve(), False),
            None,
        ]
    )
    monkeypatch.setattr(
        session_start_project_state,
        "confirm_project_identity",
        lambda *_args: next(confirmations),
    )
    monkeypatch.setattr(bootstrap_project, "_extract_git_timeline", lambda _cwd: [])
    monkeypatch.setattr(bootstrap_project, "_extract_readme_summary", lambda _cwd: "Summary")
    monkeypatch.setattr(bootstrap_project, "_extract_tech_stack", lambda _cwd: [])
    monkeypatch.setattr(bootstrap_project, "_extract_docs_structure", lambda _cwd: [])
    monkeypatch.setattr(bootstrap_project, "_run_git", lambda _cwd, *_args: "")

    result = bootstrap_project.bootstrap(str(project), apply=True)

    assert "identity changed" in result.lower()
    assert not state_path.with_name("bootstrap.md").exists()


def test_bootstrap_publication_fits_consumer_with_complete_line_truncation(
    monkeypatch,
    tmp_path: Path,
):
    project, _projects, state_path = _configure_owned_project(monkeypatch, tmp_path)
    summary = "\n".join(
        f"README_LINE_{index:03d} " + ("x" * 160) + f" END_{index:03d}"
        for index in range(100)
    )
    monkeypatch.setattr(bootstrap_project, "_extract_git_timeline", lambda _cwd: [])
    monkeypatch.setattr(
        bootstrap_project,
        "_extract_readme_summary",
        lambda _cwd: summary,
    )
    monkeypatch.setattr(bootstrap_project, "_extract_tech_stack", lambda _cwd: [])
    monkeypatch.setattr(bootstrap_project, "_extract_docs_structure", lambda _cwd: [])
    monkeypatch.setattr(bootstrap_project, "_run_git", lambda _cwd, *_args: "")

    result = bootstrap_project.bootstrap(str(project), apply=True)

    bootstrap_path = state_path.with_name("bootstrap.md")
    serialized = bootstrap_path.read_text(encoding="utf-8")
    marker = "... (bootstrap truncated)"
    assert "Written:" in result
    assert len(serialized) <= session_start_project_state.MAX_BOOTSTRAP_READ_CHARS
    assert serialized.splitlines()[-1] == marker
    assert 'project_slug_json: "active"' in serialized
    assert f"project_root_json: {json.dumps(str(project.resolve()))}" in serialized
    assert f"project_state_path_json: {json.dumps(str(state_path.resolve()))}" in serialized
    for line in serialized.splitlines():
        if line.startswith("README_LINE_"):
            index = line.removeprefix("README_LINE_").split()[0]
            assert line.endswith(f"END_{index}")
    assert marker in session_start_project_state._read_bootstrap_context(state_path)
