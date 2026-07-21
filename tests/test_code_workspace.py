from __future__ import annotations

import dataclasses
import os
from pathlib import Path

import pytest


def _write(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)


def _capture(root: Path, **options):
    from code_workspace import collect_repository_code

    options.setdefault("roots", ("src",))
    options.setdefault("include_globs", ("**/*.py",))
    options.setdefault("ignore_globs", ("**/ignored.py",))
    options.setdefault("suffixes", (".py",))
    return collect_repository_code(
        root,
        **options,
    )


def test_repository_contracts_are_frozen_slotted_normalized_and_deterministic(
    tmp_path: Path,
) -> None:
    from code_workspace import RepositoryCodeLimits

    root = tmp_path / "repository"
    _write(root / "src/z.py", b"z = 1\n")
    _write(root / "src/a.py", "name = 'caf\u00e9'\n".encode())

    first = _capture(root)
    second = collect = _capture(
        root,
        roots=("src", "src"),
        include_globs=("**/*.py", "**/*.py"),
        ignore_globs=("**/ignored.py", "**/ignored.py"),
        suffixes=(".PY", ".py"),
    )

    assert first.source_hashes == second.source_hashes
    assert [source.record.relative_path for source in first.sources] == [
        "src/a.py",
        "src/z.py",
    ]
    assert first.code_capture == collect.code_capture
    assert dataclasses.is_dataclass(first.code_capture)
    assert first.code_capture.policy.roots == ("src",)
    assert first.code_capture.policy.suffixes == (".py",)
    assert first.code_capture.limits == RepositoryCodeLimits()
    assert first.code_capture.membership_sha256 == second.code_capture.membership_sha256
    with pytest.raises((AttributeError, dataclasses.FrozenInstanceError)):
        first.code_capture.membership_sha256 = "0" * 64


def test_capture_filters_suffix_include_ignore_and_always_ignored_directories(
    tmp_path: Path,
) -> None:
    root = tmp_path / "repository"
    _write(root / "src/keep.py", b"keep = True\n")
    _write(root / "src/ignored.py", b"ignored = True\n")
    _write(root / "src/keep.txt", b"not code\n")
    _write(root / "src/__pycache__/cached.py", b"cached = True\n")
    _write(root / "src/.venv/lib.py", b"venv = True\n")
    _write(root / "cache/generated.py", b"cache = True\n")

    snapshot = _capture(root)

    assert [source.record.relative_path for source in snapshot.sources] == ["src/keep.py"]
    root_membership = {item.relative_path: item for item in snapshot.code_capture.directories}
    assert root_membership["src"].entry_count == 5


@pytest.mark.parametrize(
    "roots",
    [
        (),
        ("",),
        (".",),
        ("..",),
        ("src/../outside",),
        ("/src",),
        (r"src\pkg",),
        ("cache",),
        ("src/.venv",),
    ],
)
def test_capture_rejects_empty_or_unsafe_roots_before_traversal(
    tmp_path: Path, roots: tuple[str, ...], monkeypatch: pytest.MonkeyPatch
) -> None:
    from code_workspace import collect_repository_code

    root = tmp_path / "repository"
    root.mkdir()
    monkeypatch.setattr(os, "scandir", lambda *_args: pytest.fail("traversed unsafe root"))
    with pytest.raises(ValueError, match="root"):
        collect_repository_code(
            root,
            roots=roots,
            include_globs=("**/*.py",),
            ignore_globs=(),
            suffixes=(".py",),
        )


def test_capture_rejects_invalid_utf8_and_casefold_collisions(tmp_path: Path) -> None:
    root = tmp_path / "repository"
    _write(root / "src/bad.py", b"value = \xff\n")
    with pytest.raises(UnicodeDecodeError):
        _capture(root)


@pytest.mark.skipif(os.name == "nt", reason="distinct NFC-equivalent names require POSIX")
def test_capture_rejects_nfc_collisions(tmp_path: Path) -> None:
    root = tmp_path / "repository"
    _write(root / "src/caf\u00e9.py", b"one = 1\n")
    _write(root / "src/cafe\u0301.py", b"two = 2\n")
    with pytest.raises(ValueError, match="collision"):
        _capture(root)

    (root / "src/bad.py").unlink()
    _write(root / "src/Name.py", b"upper = 1\n")
    _write(root / "src/name.py", b"lower = 1\n")
    if len(list((root / "src").glob("*.py"))) < 2:
        pytest.skip("case-sensitive names are unavailable")
    with pytest.raises(ValueError, match="collision"):
        _capture(root)


def test_capture_rejects_symlinks_even_when_ignored(tmp_path: Path) -> None:
    root = tmp_path / "repository"
    outside = tmp_path / "outside.py"
    _write(outside, b"secret = True\n")
    (root / "src").mkdir(parents=True)
    link = root / "src/ignored.py"
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip("symlink creation is unavailable")

    with pytest.raises(PermissionError, match="link|unsafe|reparse"):
        _capture(root)


@pytest.mark.parametrize(
    ("limit_name", "limit", "message"),
    [
        ("max_entries", 1, "entry"),
        ("max_files", 1, "file"),
        ("max_total_bytes", 5, "byte"),
        ("max_directories", 1, "director"),
        ("max_depth", 0, "depth"),
    ],
)
def test_capture_enforces_each_repository_ceiling(
    tmp_path: Path, limit_name: str, limit: int, message: str
) -> None:
    from code_workspace import RepositoryCodeLimits

    root = tmp_path / "repository"
    _write(root / "src/a.py", b"aaaa\n")
    _write(root / "src/deep/b.py", b"bbbb\n")
    options = dataclasses.asdict(RepositoryCodeLimits())
    options[limit_name] = limit
    with pytest.raises(ValueError, match=message):
        _capture(root, limits=RepositoryCodeLimits(**options))


def test_repository_limits_reject_booleans_and_schema_maximum_overruns() -> None:
    from code_workspace import FileStatMetadata, RepositoryCodeLimits, RepositoryCodePolicy

    with pytest.raises(ValueError, match="max_files"):
        RepositoryCodeLimits(max_files=True)
    with pytest.raises(ValueError, match="max_file_bytes"):
        RepositoryCodeLimits(max_file_bytes=8 * 1024 * 1024 + 1)
    with pytest.raises(ValueError, match="chunk_bytes"):
        RepositoryCodeLimits(chunk_bytes=64 * 1024 + 1)
    with pytest.raises(ValueError, match="sorted|unique|roots"):
        RepositoryCodePolicy(("z", "a"), (), (), (".py",))
    with pytest.raises(ValueError, match="size"):
        FileStatMetadata(True, 0, 0, 0, 0, 0)


def test_capture_rejects_file_growth_during_chunked_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import code_workspace

    root = tmp_path / "repository"
    target = root / "src/app.py"
    _write(target, b"a" * 100)
    real_read = code_workspace.os.read
    changed = False

    def growing_read(descriptor: int, size: int) -> bytes:
        nonlocal changed
        chunk = real_read(descriptor, size)
        if chunk and not changed:
            with target.open("ab") as handle:
                handle.write(b"growth")
            changed = True
        return chunk

    monkeypatch.setattr(code_workspace.os, "read", growing_read)
    with pytest.raises((PermissionError, RuntimeError), match="changed"):
        _capture(root, limits=code_workspace.RepositoryCodeLimits(chunk_bytes=8))
    assert changed


def test_capture_rechecks_directory_membership_before_returning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import code_workspace

    root = tmp_path / "repository"
    _write(root / "src/app.py", b"answer = 42\n")
    real_read = code_workspace._read_candidate
    changed = False

    def mutating_read(*args, **kwargs):
        nonlocal changed
        content = real_read(*args, **kwargs)
        if not changed:
            _write(root / "src/late.txt", b"late membership\n")
            changed = True
        return content

    monkeypatch.setattr(code_workspace, "_read_candidate", mutating_read)
    with pytest.raises(RuntimeError, match="membership.*changed"):
        _capture(root)


def test_sealed_workspace_verifies_and_detects_file_membership_changes(tmp_path: Path) -> None:
    from code_workspace import WorkspaceChanged, seal_workspace, verify_workspace_seal

    repository = tmp_path / "repository"
    _write(repository / "src/app.py", b"answer = 42\n")
    snapshot = _capture(repository)

    for mutation in ("changed", "missing", "extra"):
        workspace = seal_workspace(snapshot, tmp_path / f"sealed-{mutation}")
        verify_workspace_seal(workspace, snapshot)
        target = workspace.root / "src/app.py"
        if mutation == "changed":
            target.chmod(0o600)
            target.write_bytes(b"answer = 43\n")
        elif mutation == "missing":
            target.chmod(0o600)
            target.unlink()
        else:
            target.parent.chmod(0o700)
            (target.parent / "extra.py").write_bytes(b"extra = True\n")
        with pytest.raises(WorkspaceChanged):
            verify_workspace_seal(workspace, snapshot)


def test_sealed_workspace_rejects_symlink_substitution(tmp_path: Path) -> None:
    from code_workspace import WorkspaceChanged, seal_workspace, verify_workspace_seal

    repository = tmp_path / "repository"
    _write(repository / "src/app.py", b"answer = 42\n")
    snapshot = _capture(repository)
    workspace = seal_workspace(snapshot, tmp_path / "sealed")
    target = workspace.root / "src/app.py"
    target.chmod(0o600)
    target.unlink()
    try:
        target.symlink_to(repository / "src/app.py")
    except OSError:
        pytest.skip("symlink creation is unavailable")

    with pytest.raises(WorkspaceChanged, match="link|reparse|changed"):
        verify_workspace_seal(workspace, snapshot)
