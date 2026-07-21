from __future__ import annotations

import dataclasses
import hashlib
import inspect
import os
import stat
import subprocess
from pathlib import Path

import pytest
from reliable_memory import canonical_json_bytes


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


def test_code_capture_files_and_membership_have_exact_canonical_shape(tmp_path: Path) -> None:
    from code_workspace import code_capture_as_dict

    root = tmp_path / "repository"
    _write(root / "src/app.py", b"answer = 42\n")
    capture = code_capture_as_dict(_capture(root).code_capture)

    assert set(capture["files"][0]) == {
        "source_id",
        "relative_path",
        "sha256",
        "stat",
    }
    assert set(capture["files"][0]["stat"]) == {
        "size",
        "mtime_ns",
        "ctime_ns",
        "mode",
        "device",
        "inode",
    }
    assert capture["membership_sha256"] == hashlib.sha256(
        canonical_json_bytes(
            {"files": capture["files"], "directories": capture["directories"]}
        )
    ).hexdigest()


def test_membership_hash_changes_for_every_file_contract_field(tmp_path: Path) -> None:
    from code_workspace import validate_code_capture

    root = tmp_path / "repository"
    _write(root / "src/app.py", b"answer = 42\n")
    original = __import__("code_workspace").code_capture_as_dict(_capture(root).code_capture)
    mutations = {
        "source_id": "source:src/other.py",
        "relative_path": "src/other.py",
        "sha256": "f" * 64,
        "size": original["files"][0]["stat"]["size"] + 1,
        "mtime_ns": original["files"][0]["stat"]["mtime_ns"] + 1,
        "ctime_ns": original["files"][0]["stat"]["ctime_ns"] + 1,
        "mode": original["files"][0]["stat"]["mode"] + 1,
        "device": original["files"][0]["stat"]["device"] + 1,
        "inode": original["files"][0]["stat"]["inode"] + 1,
    }
    for field, value in mutations.items():
        damaged = __import__("copy").deepcopy(original)
        target = damaged["files"][0]
        if field in target:
            target[field] = value
        else:
            target["stat"][field] = value
        with pytest.raises(ValueError):
            validate_code_capture(damaged)


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


def test_capture_rejects_invalid_utf8(tmp_path: Path) -> None:
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


def test_contract_rejects_casefold_collision_on_every_platform() -> None:
    from code_workspace import CodeCaptureContract, CodeCaptureFile
    from corpus_snapshot import FileStatMetadata, RepositoryCodeLimits, RepositoryCodePolicy

    metadata = FileStatMetadata(1, 1, 1, stat.S_IFREG, 1, 1)
    files = (
        CodeCaptureFile("source:src/A.py", "src/A.py", "a" * 64, metadata),
        CodeCaptureFile("source:src/a.py", "src/a.py", "b" * 64, metadata),
    )
    with pytest.raises(ValueError, match="collision"):
        CodeCaptureContract(
            RepositoryCodePolicy(("src",), ("**/*.py",), (), (".py",)),
            RepositoryCodeLimits(),
            files,
            (),
            "0" * 64,
        )

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
    assert RepositoryCodeLimits(
        max_files=1_000_000,
        max_file_bytes=1024**3,
        max_total_bytes=16 * 1024**3,
        max_entries=5_000_000,
        max_directories=1_000_000,
        max_depth=256,
        chunk_bytes=8 * 1024 * 1024,
    )
    with pytest.raises(ValueError, match="max_file_bytes"):
        RepositoryCodeLimits(max_file_bytes=1024**3 + 1)
    with pytest.raises(ValueError, match="chunk_bytes"):
        RepositoryCodeLimits(chunk_bytes=4095)
    with pytest.raises(ValueError, match="sorted|unique|roots"):
        RepositoryCodePolicy(("z", "a"), (), (), (".py",))
    with pytest.raises(ValueError, match="size"):
        FileStatMetadata(True, 0, 0, 0, 0, 0)


def test_policy_cardinalities_and_capture_signature_are_exact() -> None:
    from code_workspace import RepositoryCodePolicy, collect_repository_code

    signature = inspect.signature(collect_repository_code)
    assert signature.parameters["checkout_root"].kind is inspect.Parameter.POSITIONAL_OR_KEYWORD
    assert all(
        parameter.kind is inspect.Parameter.KEYWORD_ONLY
        for parameter in tuple(signature.parameters.values())[1:]
    )
    assert RepositoryCodePolicy(("src",), ("**/*.py",), (), (".py",))
    with pytest.raises(ValueError, match="roots"):
        RepositoryCodePolicy(tuple(f"r{i}" for i in range(129)), ("**",), (), (".py",))
    with pytest.raises(ValueError, match="include"):
        RepositoryCodePolicy(("src",), (), (), (".py",))
    with pytest.raises(ValueError, match="suffix"):
        RepositoryCodePolicy(("src",), ("**",), (), ())


def test_capture_rejects_linked_checkout_root_before_resolution(tmp_path: Path) -> None:
    target = tmp_path / "target"
    _write(target / "src/app.py", b"answer = 42\n")
    linked = tmp_path / "linked"
    try:
        linked.symlink_to(target, target_is_directory=True)
    except OSError:
        pytest.skip("directory symlink creation is unavailable")
    with pytest.raises(PermissionError, match="checkout root|link|reparse"):
        _capture(linked)


@pytest.mark.skipif(os.name != "nt", reason="Windows junction check")
def test_capture_rejects_windows_reparse_checkout_root(tmp_path: Path) -> None:
    target = tmp_path / "target"
    _write(target / "src/app.py", b"answer = 42\n")
    linked = tmp_path / "junction"
    result = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(linked), str(target)],
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        pytest.skip("Windows junction creation is unavailable")
    with pytest.raises(PermissionError, match="checkout root|link|reparse"):
        _capture(linked)


def test_capture_persists_stat_from_hashed_descriptor(tmp_path: Path) -> None:
    import code_workspace

    root = tmp_path / "repository"
    target = root / "src/app.py"
    _write(target, b"answer = 42\n")
    descriptor = code_workspace._open_read(target)
    try:
        expected = code_workspace._stat_metadata(os.fstat(descriptor))
    finally:
        os.close(descriptor)
    captured = _capture(root).code_capture.files[0]
    assert captured.stat == expected


def test_capture_rejects_replacement_between_stat_and_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import code_workspace

    root = tmp_path / "repository"
    target = root / "src/app.py"
    replacement = tmp_path / "replacement.py"
    _write(target, b"answer = 42\n")
    _write(replacement, b"answer = 43\n")
    real_open = code_workspace._open_read
    replaced = False

    def replacing_open(path: Path) -> int:
        nonlocal replaced
        if path == target and not replaced:
            replaced = True
            os.replace(replacement, target)
        return real_open(path)

    monkeypatch.setattr(code_workspace, "_open_read", replacing_open)
    with pytest.raises(PermissionError, match="changed before"):
        _capture(root)
    assert replaced


def test_capture_rejects_changed_then_restored_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import code_workspace

    root = tmp_path / "repository"
    target = root / "src/app.py"
    original = b"answer = 42\n"
    _write(target, original)
    called = False

    def barrier(_descriptor: int) -> None:
        nonlocal called
        if called:
            return
        called = True
        before = target.stat()
        target.write_bytes(b"answer = 43\n")
        target.write_bytes(original)
        os.utime(target, ns=(before.st_atime_ns, before.st_mtime_ns))

    monkeypatch.setattr(code_workspace, "_capture_read_barrier", barrier, raising=False)
    with pytest.raises(RuntimeError, match="changed"):
        _capture(root)
    assert called


def test_capture_rejects_file_growth_during_chunked_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import code_workspace

    root = tmp_path / "repository"
    target = root / "src/app.py"
    _write(target, b"a" * 5000)
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
        _capture(root, limits=code_workspace.RepositoryCodeLimits(chunk_bytes=4096))
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
    from code_workspace import (
        WorkspaceChanged,
        seal_workspace,
        verify_workspace_seal,
        workspace_sealing_supported,
    )

    repository = tmp_path / "repository"
    _write(repository / "src/app.py", b"answer = 42\n")
    snapshot = _capture(repository)
    if not workspace_sealing_supported():
        with pytest.raises(RuntimeError, match="root-relative no-follow"):
            seal_workspace(snapshot, tmp_path / "sealed-unsupported")
        return

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
    from code_workspace import (
        WorkspaceChanged,
        seal_workspace,
        verify_workspace_seal,
        workspace_sealing_supported,
    )

    repository = tmp_path / "repository"
    _write(repository / "src/app.py", b"answer = 42\n")
    snapshot = _capture(repository)
    if not workspace_sealing_supported():
        pytest.skip("root-relative no-follow workspace primitive is unavailable")
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


def test_sealing_holds_parent_against_substitution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import code_workspace

    repository = tmp_path / "repository"
    _write(repository / "src/app.py", b"answer = 42\n")
    snapshot = _capture(repository)
    destination = tmp_path / "sealed"
    if not code_workspace.workspace_sealing_supported():
        with pytest.raises(RuntimeError, match="root-relative no-follow"):
            code_workspace.seal_workspace(snapshot, destination)
        return
    displaced = tmp_path / "displaced"
    replacement = tmp_path / "replacement"
    replacement.mkdir()
    attacked = blocked = False

    def barrier(component: str) -> None:
        nonlocal attacked, blocked
        if component != "src" or attacked or blocked:
            return
        try:
            destination.rename(displaced)
            replacement.rename(destination)
            attacked = True
        except OSError:
            blocked = True

    monkeypatch.setattr(code_workspace, "_seal_component_barrier", barrier, raising=False)
    if os.name == "posix":
        with pytest.raises(
            (PermissionError, code_workspace.WorkspaceChanged, RuntimeError)
        ):
            code_workspace.seal_workspace(snapshot, destination)
        assert attacked
    else:
        workspace = code_workspace.seal_workspace(snapshot, destination)
        assert blocked
        code_workspace.verify_workspace_seal(workspace, snapshot)


def test_verification_holds_parent_against_substitution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import code_workspace

    repository = tmp_path / "repository"
    _write(repository / "src/app.py", b"answer = 42\n")
    snapshot = _capture(repository)
    if not code_workspace.workspace_sealing_supported():
        pytest.skip("root-relative no-follow workspace primitive is unavailable")
    destination = tmp_path / "sealed"
    workspace = code_workspace.seal_workspace(snapshot, destination)
    displaced = tmp_path / "displaced"
    replacement = tmp_path / "replacement"
    replacement.mkdir()
    attacked = blocked = False

    def barrier(component: str) -> None:
        nonlocal attacked, blocked
        if component != "src" or attacked or blocked:
            return
        try:
            destination.rename(displaced)
            replacement.rename(destination)
            attacked = True
        except OSError:
            blocked = True

    monkeypatch.setattr(code_workspace, "_verify_component_barrier", barrier, raising=False)
    if os.name == "posix":
        with pytest.raises(code_workspace.WorkspaceChanged):
            code_workspace.verify_workspace_seal(workspace, snapshot)
        assert attacked
    else:
        code_workspace.verify_workspace_seal(workspace, snapshot)
        assert blocked
