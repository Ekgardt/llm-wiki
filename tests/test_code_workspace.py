from __future__ import annotations

import dataclasses
import hashlib
import inspect
import json
import os
import stat
import subprocess
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest
from reliable_memory import canonical_json_bytes


def _write(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)


def _capture(root: Path, **options):
    from code_workspace import RepositoryCodeLimits, collect_repository_code

    options.setdefault("roots", ("src",))
    options.setdefault("include_globs", ("**/*.py",))
    options.setdefault("ignore_globs", ("**/ignored.py",))
    options.setdefault("suffixes", (".py",))
    options.setdefault("limits", RepositoryCodeLimits())
    return collect_repository_code(
        root,
        **options,
    )


def test_collect_repository_code_has_exact_keyword_only_api() -> None:
    from code_workspace import collect_repository_code

    parameters = inspect.signature(collect_repository_code).parameters
    assert parameters["checkout_root"].kind is inspect.Parameter.POSITIONAL_OR_KEYWORD
    for name in ("roots", "include_globs", "ignore_globs", "suffixes", "limits"):
        assert parameters[name].kind is inspect.Parameter.KEYWORD_ONLY
        assert parameters[name].default is inspect.Parameter.empty
    annotations = {
        name: str(parameters[name].annotation).replace(" ", "")
        for name in ("roots", "include_globs", "ignore_globs", "suffixes")
    }
    assert annotations == {
        "roots": "tuple[str,...]",
        "include_globs": "tuple[str,...]",
        "ignore_globs": "tuple[str,...]",
        "suffixes": "tuple[str,...]",
    }
    assert str(parameters["limits"].annotation) == "RepositoryCodeLimits"


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


def test_collected_policy_normalizes_every_text_value_to_nfc(tmp_path: Path) -> None:
    from code_workspace import code_capture_as_dict

    root = tmp_path / "repository"
    composed = "caf\u00e9"
    decomposed = "cafe\u0301"
    _write(root / composed / f"app.{composed}", b"answer = 42\n")

    snapshot = _capture(
        root,
        roots=(decomposed,),
        include_globs=(f"{decomposed}/*.{decomposed}",),
        ignore_globs=(f"{decomposed}/ignored.{decomposed}",),
        suffixes=(f".{decomposed}",),
    )
    policy = code_capture_as_dict(snapshot.code_capture)["policy"]

    assert policy == {
        "roots": [composed],
        "include_globs": [f"{composed}/*.{composed}"],
        "ignore_globs": [f"{composed}/ignored.{composed}"],
        "suffixes": [f".{composed}"],
    }
    assert [source.record.relative_path for source in snapshot.sources] == [
        f"{composed}/app.{composed}"
    ]


@pytest.mark.parametrize(
    "field", ("roots", "include_globs", "ignore_globs", "suffixes")
)
def test_policy_rejects_distinct_values_colliding_after_nfc_normalization(
    tmp_path: Path, field: str
) -> None:
    root = tmp_path / "repository"
    _write(root / "src/app.py", b"answer = 42\n")
    composed = "caf\u00e9"
    decomposed = "cafe\u0301"
    options = {
        "roots": ("src",),
        "include_globs": ("**/*.py",),
        "ignore_globs": (),
        "suffixes": (".py",),
    }
    if field == "roots":
        options[field] = (composed, decomposed)
    elif field == "suffixes":
        options[field] = (f".{composed}", f".{decomposed}")
    else:
        options[field] = (f"{composed}/**", f"{decomposed}/**")

    with pytest.raises(ValueError, match="normalization.*collision"):
        _capture(root, **options)


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


def test_capture_globs_match_complete_posix_path_segments(tmp_path: Path) -> None:
    root = tmp_path / "repository"
    _write(root / "src/app.py", b"app = True\n")
    _write(root / "src/pkg/generated.py", b"direct = True\n")
    _write(root / "src/pkg/deep/generated.py", b"deep = True\n")

    snapshot = _capture(
        root,
        include_globs=("src/*.py", "src/**/*.py"),
        ignore_globs=("src/*/generated.py",),
    )

    assert [source.record.relative_path for source in snapshot.sources] == [
        "src/app.py",
        "src/pkg/deep/generated.py",
    ]


@pytest.mark.parametrize(
    ("pattern", "accepted"),
    (
        ("**", True),
        ("src/**name.py", True),
        ("src/", True),
        ("/src/*.py", False),
        ("C:/src/*.py", False),
        ("../src/*.py", False),
        ("src/../*.py", False),
        (r"src\*.py", False),
        ("./src/*.py", False),
        ("src/./*.py", False),
        ("src//*.py", False),
    ),
)
@pytest.mark.parametrize("field", ("include_globs", "ignore_globs"))
def test_repository_policy_glob_runtime_and_manifest_schema_agree(
    pattern: str, accepted: bool, field: str
) -> None:
    import jsonschema
    from code_workspace import RepositoryCodePolicy

    schema = json.loads(
        (
            Path(__file__).resolve().parents[1]
            / "scripts/schemas/evidence-graph-manifest-v1.json"
        ).read_text(encoding="utf-8")
    )["properties"]["code_capture"]["properties"]["policy"]
    policy = {
        "roots": ["src"],
        "include_globs": [pattern] if field == "include_globs" else ["**"],
        "ignore_globs": [pattern] if field == "ignore_globs" else [],
        "suffixes": [".py"],
    }
    schema_accepted = not list(jsonschema.Draft202012Validator(schema).iter_errors(policy))
    try:
        RepositoryCodePolicy(
            ("src",),
            (pattern,) if field == "include_globs" else ("**",),
            (pattern,) if field == "ignore_globs" else (),
            (".py",),
        )
    except ValueError:
        runtime_accepted = False
    else:
        runtime_accepted = True

    assert schema_accepted is accepted
    assert runtime_accepted is schema_accepted


def test_repository_policy_accepts_bounded_glob_with_more_than_256_segments() -> None:
    import jsonschema
    from code_workspace import RepositoryCodePolicy

    pattern = "/".join("x" for _ in range(257))
    schema = json.loads(
        (
            Path(__file__).resolve().parents[1]
            / "scripts/schemas/evidence-graph-manifest-v1.json"
        ).read_text(encoding="utf-8")
    )["properties"]["code_capture"]["properties"]["policy"]
    policy = {
        "roots": ["src"],
        "include_globs": [pattern],
        "ignore_globs": [],
        "suffixes": [".py"],
    }

    assert not list(jsonschema.Draft202012Validator(schema).iter_errors(policy))
    assert RepositoryCodePolicy(("src",), (pattern,), (), (".py",))


@pytest.mark.parametrize(
    ("suffix", "accepted"),
    (
        ("", False),
        (".", False),
        ("py", False),
        (".py", True),
        (".PY", True),
        (".caf\u00e9", True),
        (".a/b", False),
        (r".a\b", False),
        ("." + "a" * 127, True),
        ("." + "a" * 128, False),
    ),
)
def test_repository_suffix_runtime_and_manifest_schema_agree(
    suffix: str, accepted: bool
) -> None:
    import code_workspace
    import jsonschema

    schema = json.loads(
        (
            Path(__file__).resolve().parents[1]
            / "scripts/schemas/evidence-graph-manifest-v1.json"
        ).read_text(encoding="utf-8")
    )["properties"]["code_capture"]["properties"]["policy"]["properties"][
        "suffixes"
    ]["items"]
    schema_accepted = not list(jsonschema.Draft202012Validator(schema).iter_errors(suffix))
    try:
        code_workspace._policy(("src",), ("**",), (), (suffix,))
    except ValueError:
        runtime_accepted = False
    else:
        runtime_accepted = True

    assert schema_accepted is accepted
    assert runtime_accepted is schema_accepted


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
            limits=__import__("code_workspace").RepositoryCodeLimits(),
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


@pytest.mark.parametrize("value", ["x" * 512, "界" * 512])
def test_code_capture_file_source_id_accepts_512_unicode_characters(value: str) -> None:
    from code_workspace import CodeCaptureFile
    from corpus_snapshot import FileStatMetadata

    metadata = FileStatMetadata(1, 1, 1, stat.S_IFREG, 1, 1)
    assert CodeCaptureFile(value, "src/a.py", "a" * 64, metadata).source_id == value


@pytest.mark.parametrize("value", ["x" * 513, "界" * 513])
def test_code_capture_file_source_id_rejects_513_unicode_characters(value: str) -> None:
    from code_workspace import CodeCaptureFile
    from corpus_snapshot import FileStatMetadata

    metadata = FileStatMetadata(1, 1, 1, stat.S_IFREG, 1, 1)
    with pytest.raises(ValueError, match="source_id"):
        CodeCaptureFile(value, "src/a.py", "a" * 64, metadata)


def test_same_file_fails_closed_without_device_or_inode() -> None:
    from code_workspace import _same_file

    metadata = SimpleNamespace(
        st_dev=0,
        st_ino=0,
        st_mode=stat.S_IFREG,
        st_size=1,
        st_mtime_ns=1,
        st_ctime_ns=1,
    )
    assert _same_file(metadata, metadata) is False


def test_manifest_capture_source_id_enforces_512_byte_boundary(tmp_path: Path) -> None:
    from code_workspace import code_capture_as_dict, validate_code_capture

    root = tmp_path / "repository"
    _write(root / "src/app.py", b"answer = 42\n")
    capture = code_capture_as_dict(_capture(root).code_capture)
    capture["files"][0]["source_id"] = "x" * 512
    capture["membership_sha256"] = hashlib.sha256(
        canonical_json_bytes(
            {"files": capture["files"], "directories": capture["directories"]}
        )
    ).hexdigest()
    assert validate_code_capture(capture)["files"][0]["source_id"] == "x" * 512

    capture["files"][0]["source_id"] += "x"
    capture["membership_sha256"] = hashlib.sha256(
        canonical_json_bytes(
            {"files": capture["files"], "directories": capture["directories"]}
        )
    ).hexdigest()
    with pytest.raises(ValueError, match="source_id"):
        validate_code_capture(capture)


def test_collector_rejects_generated_source_id_over_512_characters(tmp_path: Path) -> None:
    root = tmp_path / "repository"
    deep = root / "src" / ("a" * 250) / ("b" * 250)
    _write(deep / "long.py", b"answer = 42\n")
    with pytest.raises(ValueError, match="source_id.*512"):
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


def test_capture_rejects_root_substitution_before_canonicalization(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import code_workspace

    root = tmp_path / "repository"
    parked = tmp_path / "parked"
    target = tmp_path / "target"
    _write(root / "src/app.py", b"safe = True\n")
    _write(target / "src/app.py", b"secret = True\n")
    substituted = False

    def substitute(_root: Path) -> None:
        nonlocal substituted
        try:
            root.rename(parked)
        except OSError as exc:
            raise PermissionError("held root rejected substitution") from exc
        if os.name == "nt":
            result = subprocess.run(
                ["cmd", "/c", "mklink", "/J", str(root), str(target)],
                capture_output=True,
                check=False,
            )
            if result.returncode != 0:
                pytest.skip("Windows junction creation is unavailable")
        else:
            root.symlink_to(target, target_is_directory=True)
        substituted = True

    real_read = code_workspace._read_candidate

    def reject_target(path, *args, **kwargs):
        if target in path.parents:
            pytest.fail("capture read from substituted checkout target")
        return real_read(path, *args, **kwargs)

    monkeypatch.setattr(code_workspace, "_capture_root_barrier", substitute, raising=False)
    monkeypatch.setattr(code_workspace, "_read_candidate", reject_target)
    with pytest.raises((PermissionError, RuntimeError), match="root|substitution|changed"):
        _capture(root)
    assert substituted or os.name == "nt"


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


@pytest.mark.parametrize("_attempt", range(10))
def test_capture_rejects_replacement_between_stat_and_open_at_barrier(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _attempt: int
) -> None:
    import code_workspace

    root = tmp_path / "repository"
    target = root / "src/app.py"
    replacement = tmp_path / "replacement.py"
    _write(target, b"answer = 42\n")
    _write(replacement, b"answer = 43\n")
    replaced = threading.Event()

    def replace_before_open(path: Path) -> None:
        if not replaced.is_set():
            assert path.name == target.name
            replaced.set()
            try:
                os.replace(replacement, target)
            except OSError as exc:
                raise PermissionError("file changed before no-follow open") from exc

    monkeypatch.setattr(code_workspace, "_capture_open_barrier", replace_before_open, raising=False)
    with pytest.raises(PermissionError, match="changed before"):
        _capture(root)
    assert replaced.is_set()


@pytest.mark.skipif(os.name != "nt", reason="Windows enumeration identity race")
@pytest.mark.parametrize("_attempt", range(20))
def test_windows_capture_rejects_replacement_between_enumeration_stat_and_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _attempt: int
) -> None:
    import code_workspace

    root = tmp_path / "repository"
    target = root / "src/app.py"
    attacker = tmp_path / "attacker.py"
    _write(target, b"safe = True\n")
    _write(attacker, b"secret = True\n")
    attacker_descriptor = code_workspace._open_read(attacker)
    try:
        attacker_identity = code_workspace._descriptor_identity(attacker_descriptor)
    finally:
        os.close(attacker_descriptor)

    real_read = code_workspace.os.read
    replaced = False
    attacker_reads = 0

    def replace_after_enumeration_stat(path: Path) -> None:
        nonlocal replaced
        if path == target and not replaced:
            os.replace(attacker, target)
            replaced = True

    def track_attacker_read(descriptor: int, size: int) -> bytes:
        nonlocal attacker_reads
        metadata = os.fstat(descriptor)
        if (
            stat.S_ISREG(metadata.st_mode)
            and code_workspace._descriptor_identity(descriptor) == attacker_identity
        ):
            attacker_reads += 1
        return real_read(descriptor, size)

    monkeypatch.setattr(
        code_workspace,
        "_capture_entry_identity_barrier",
        replace_after_enumeration_stat,
        raising=False,
    )
    monkeypatch.setattr(code_workspace.os, "read", track_attacker_read)
    with pytest.raises(PermissionError, match="changed during enumeration"):
        _capture(root)

    assert replaced
    assert attacker_reads == 0


def test_capture_rejects_unavailable_descriptor_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import code_workspace

    root = tmp_path / "repository"
    _write(root / "src/app.py", b"answer = 42\n")

    def unavailable(_descriptor: int):
        raise RuntimeError("identity unavailable")

    monkeypatch.setattr(code_workspace, "_descriptor_identity", unavailable, raising=False)
    with pytest.raises(RuntimeError, match="identity"):
        _capture(root)


@pytest.mark.skipif(os.name != "nt", reason="Windows handle file IDs only")
def test_windows_descriptor_identity_is_stable(tmp_path: Path) -> None:
    import code_workspace

    target = tmp_path / "source.py"
    _write(target, b"answer = 42\n")
    descriptor = code_workspace._open_read(target)
    try:
        first = code_workspace._descriptor_identity(descriptor)
        second = code_workspace._descriptor_identity(descriptor)
    finally:
        os.close(descriptor)
    assert first == second
    assert first[0] == "windows"


@pytest.mark.skipif(os.name != "nt", reason="Windows lexical no-follow open only")
def test_windows_no_follow_open_does_not_resolve_path_first(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import code_workspace

    target = tmp_path / "source.py"
    _write(target, b"answer = 42\n")

    def reject_resolution(*_args, **_kwargs):
        pytest.fail("no-follow open resolved the path before CreateFileW")

    monkeypatch.setattr(Path, "resolve", reject_resolution)
    descriptor = code_workspace._open_read(target)
    try:
        assert os.read(descriptor, 64) == b"answer = 42\n"
    finally:
        os.close(descriptor)


@pytest.mark.skipif(os.name != "nt", reason="Windows pre-read identity race")
def test_windows_capture_rejects_pre_read_replacement_without_reading_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import code_workspace

    root = tmp_path / "repository"
    target = root / "src/app.py"
    replacement = tmp_path / "replacement.py"
    _write(target, b"safe = True\n")
    _write(replacement, b"secret = True\n")
    descriptor = code_workspace._open_read(replacement)
    try:
        replacement_identity = code_workspace._descriptor_identity(descriptor)
    finally:
        os.close(descriptor)

    real_candidate_read = code_workspace._read_candidate
    real_read = code_workspace.os.read
    replaced = False
    replacement_reads = 0

    def replace_before_candidate_read(*args, **kwargs):
        nonlocal replaced
        if not replaced:
            os.replace(replacement, target)
            replaced = True
        return real_candidate_read(*args, **kwargs)

    def reject_replacement_read(descriptor: int, size: int) -> bytes:
        nonlocal replacement_reads
        if code_workspace._descriptor_identity(descriptor) == replacement_identity:
            replacement_reads += 1
        return real_read(descriptor, size)

    monkeypatch.setattr(code_workspace, "_read_candidate", replace_before_candidate_read)
    monkeypatch.setattr(code_workspace.os, "read", reject_replacement_read)
    with pytest.raises(PermissionError, match="changed before"):
        _capture(root)
    assert replaced
    assert replacement_reads == 0


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
    target_descriptor = code_workspace._open_read(target)
    try:
        target_identity = code_workspace._descriptor_identity(target_descriptor)
    finally:
        os.close(target_descriptor)
    real_read = code_workspace.os.read
    changed = False

    def growing_read(descriptor: int, size: int) -> bytes:
        nonlocal changed
        chunk = real_read(descriptor, size)
        if (
            chunk
            and not changed
            and stat.S_ISREG(os.fstat(descriptor).st_mode)
            and code_workspace._descriptor_identity(descriptor) == target_identity
        ):
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


@pytest.mark.parametrize("operation", ("seal", "verify"))
@pytest.mark.parametrize("mutation", ("content", "size", "hash", "manifest"))
def test_workspace_boundaries_reject_forged_snapshot_before_filesystem_access(
    tmp_path: Path, operation: str, mutation: str
) -> None:
    from code_workspace import SealedWorkspace, seal_workspace, verify_workspace_seal

    repository = tmp_path / "repository"
    _write(repository / "src/app.py", b"answer = 42\n")
    snapshot = _capture(repository)
    source = snapshot.sources[0]
    record = source.record
    if mutation == "content":
        damaged_source = dataclasses.replace(source, content=b"answer = 43\n")
        damaged = dataclasses.replace(snapshot, sources=(damaged_source,))
    elif mutation == "size":
        damaged_record = dataclasses.replace(record, size=record.size + 1)
        damaged = dataclasses.replace(
            snapshot, sources=(dataclasses.replace(source, record=damaged_record),)
        )
    elif mutation == "hash":
        damaged_record = dataclasses.replace(record, sha256="f" * 64)
        damaged = dataclasses.replace(
            snapshot, sources=(dataclasses.replace(source, record=damaged_record),)
        )
    else:
        damaged = dataclasses.replace(snapshot, corpus_sha256="f" * 64)

    destination = tmp_path / f"sealed-{operation}-{mutation}"
    entries = tuple(
        (item.record.relative_path, item.record.size, item.record.sha256)
        for item in damaged.sources
    )
    workspace = SealedWorkspace(
        destination,
        damaged.corpus_sha256,
        entries,
        owner_only=True,
        read_only_requested=True,
    )
    with pytest.raises(ValueError, match="snapshot"):
        if operation == "seal":
            seal_workspace(damaged, destination)
        else:
            verify_workspace_seal(workspace, damaged)
    assert not destination.exists()


def test_workspace_boundary_rejects_boolean_source_size(tmp_path: Path) -> None:
    from code_workspace import seal_workspace

    repository = tmp_path / "repository"
    _write(repository / "src/app.py", b"x")
    snapshot = _capture(repository)
    source = snapshot.sources[0]
    damaged_record = dataclasses.replace(source.record, size=True)
    damaged = dataclasses.replace(
        snapshot,
        sources=(dataclasses.replace(source, record=damaged_record),),
    )

    with pytest.raises(ValueError, match="snapshot"):
        seal_workspace(damaged, tmp_path / "sealed")


@pytest.mark.skipif(os.name != "posix", reason="POSIX descriptor-relative sealing only")
def test_seal_and_verify_descriptor_peak_is_bounded_by_depth(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import code_workspace

    repository = tmp_path / "repository"
    for index in range(100):
        _write(repository / f"src/pkg-{index:03d}/app.py", b"answer = 42\n")
    limits = code_workspace.RepositoryCodeLimits(max_depth=8)
    snapshot = _capture(repository, limits=limits)

    real_open = code_workspace.os.open
    real_close = code_workspace.os.close
    active: set[int] = set()
    peak = 0

    def tracked_open(*args, **kwargs) -> int:
        nonlocal peak
        descriptor = real_open(*args, **kwargs)
        active.add(descriptor)
        peak = max(peak, len(active))
        return descriptor

    def tracked_close(descriptor: int) -> None:
        try:
            real_close(descriptor)
        finally:
            active.discard(descriptor)

    monkeypatch.setattr(code_workspace.os, "open", tracked_open)
    monkeypatch.setattr(code_workspace.os, "close", tracked_close)
    monkeypatch.setattr(
        code_workspace.os,
        "supports_dir_fd",
        {*code_workspace.os.supports_dir_fd, tracked_open},
    )
    workspace = code_workspace.seal_workspace(snapshot, tmp_path / "sealed")
    code_workspace.verify_workspace_seal(workspace, snapshot)

    assert not active
    assert peak <= limits.max_depth + 12


@pytest.mark.skipif(os.name != "posix", reason="POSIX descriptor-relative sealing only")
def test_verification_directory_limit_excludes_synthetic_workspace_root(
    tmp_path: Path,
) -> None:
    import code_workspace

    repository = tmp_path / "repository"
    _write(repository / "src/app.py", b"answer = 42\n")
    snapshot = _capture(
        repository,
        limits=code_workspace.RepositoryCodeLimits(max_directories=1),
    )

    workspace = code_workspace.seal_workspace(snapshot, tmp_path / "sealed")
    code_workspace.verify_workspace_seal(workspace, snapshot)


@pytest.mark.skipif(os.name != "posix", reason="POSIX descriptor-relative sealing only")
def test_seal_rejects_internal_directory_injected_before_exclusive_mkdir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import code_workspace

    repository = tmp_path / "repository"
    _write(repository / "src/app.py", b"answer = 42\n")
    snapshot = _capture(repository)
    destination = tmp_path / "sealed"
    injected = destination / "src"

    def inject(component: str) -> None:
        if component == "src" and not injected.exists():
            injected.mkdir()

    monkeypatch.setattr(code_workspace, "_seal_component_barrier", inject)
    with pytest.raises(FileExistsError):
        code_workspace.seal_workspace(snapshot, destination)

    assert injected.is_dir()
    assert not (injected / "app.py").exists()


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
