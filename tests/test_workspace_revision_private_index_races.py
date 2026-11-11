"""The private-index fast path of a revision check survives races, cancellation and failures, and never returns a stale True."""


from __future__ import annotations

import os
import shutil
import sys
import time
from pathlib import Path, PurePosixPath

import pytest
import workspace_revision
from repository_scope import resolve_repository_scope
from workspace_revision import (
    compute_workspace_revision,
)

from tests.workspace_revision_helpers import (
    _isolated_global_git_configuration,  # noqa: F401
    _linux_memfd_available,
    _run_git,
    _verification_git_call_counts,
)
from tests.workspace_revision_race_helpers import (
    _qualify_private_git_wrapper,
)


def _seal_number(name: str, number: int) -> int:
    """This seal's number, from `fcntl` where it publishes one, else the Linux ABI.

    The CPython builds `uv` installs are compiled against headers that declare none
    of the seal constants, so a test that reads them off the module dies on the
    interpreter this project's own command selects. The numbers are a Linux ABI;
    preferring `fcntl`'s value where there is one keeps this an independent check of
    the numbers the product uses rather than a copy of them.
    """
    import fcntl

    value = getattr(fcntl, name, number)
    assert value == number
    return value


def _opens_target_file(path, target_name, flags, kwargs) -> bool:
    """A file open of the raced entry, not the directory handle above it."""
    if path != target_name or kwargs.get("dir_fd") is None:
        return False
    return not flags & getattr(os, "O_DIRECTORY", 0)


def _block_unless_nonblocking(flags: int) -> None:
    if flags & os.O_NONBLOCK:
        return
    time.sleep(1.0)


class _PrivateIndexInjection:
    """Mutable state shared by one failure injection and its assertions."""

    def __init__(self, failure: str) -> None:
        self.failure = failure
        self.fired = False
        self.private_descriptor: int | None = None
        self.index_descriptor: int | None = None


_MEMFD_DESCRIPTOR_FAILURES = frozenset(
    {"memfd-fchmod", "memfd-write", "memfd-fsync", "memfd-lseek", "memfd-utime"}
)


def _install_memfd_recorder(monkeypatch, state: _PrivateIndexInjection) -> None:
    real_memfd_create = workspace_revision.os.memfd_create

    def recording_memfd_create(*args, **kwargs):
        if state.failure == "memfd-create":
            state.fired = True
            raise OSError("injected memfd creation failure")
        state.private_descriptor = real_memfd_create(*args, **kwargs)
        return state.private_descriptor

    monkeypatch.setattr(workspace_revision.os, "memfd_create", recording_memfd_create)


def _inject_memfd_descriptor_failure(monkeypatch, state: _PrivateIndexInjection) -> None:
    operation_name = state.failure.removeprefix("memfd-")
    real_operation = getattr(workspace_revision.os, operation_name)

    def fail_descriptor_operation(descriptor, *args, **kwargs):
        if descriptor == state.private_descriptor and not state.fired:
            state.fired = True
            raise OSError(f"injected {operation_name} failure")
        return real_operation(descriptor, *args, **kwargs)

    monkeypatch.setattr(
        workspace_revision.os, operation_name, fail_descriptor_operation
    )


def _inject_memfd_fcntl_failure(monkeypatch, state: _PrivateIndexInjection) -> None:
    assert workspace_revision.fcntl is not None
    real_fcntl = workspace_revision.fcntl.fcntl

    def fail_fcntl(descriptor, *args, **kwargs):
        if descriptor == state.private_descriptor and not state.fired:
            state.fired = True
            raise OSError("injected fcntl failure")
        return real_fcntl(descriptor, *args, **kwargs)

    monkeypatch.setattr(workspace_revision.fcntl, "fcntl", fail_fcntl)


def _is_index_metadata(path, kwargs) -> bool:
    return path == "index" and kwargs.get("dir_fd") is not None


def _inject_metadata_stat_failure(monkeypatch, state: _PrivateIndexInjection) -> None:
    real_stat = workspace_revision.os.stat

    def fail_metadata_stat(path, *args, **kwargs):
        if _is_index_metadata(path, kwargs) and not state.fired:
            state.fired = True
            raise OSError("injected metadata stat failure")
        return real_stat(path, *args, **kwargs)

    monkeypatch.setattr(workspace_revision.os, "stat", fail_metadata_stat)


def _inject_metadata_open_failure(monkeypatch, state: _PrivateIndexInjection) -> None:
    real_open = workspace_revision.os.open

    def fail_metadata_open(path, *args, **kwargs):
        if _is_index_metadata(path, kwargs) and not state.fired:
            state.fired = True
            raise OSError("injected metadata open failure")
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(workspace_revision.os, "open", fail_metadata_open)


def _inject_metadata_read_failure(monkeypatch, state: _PrivateIndexInjection) -> None:
    real_open = workspace_revision.os.open
    real_read = workspace_revision.os.read

    def record_metadata_open(path, *args, **kwargs):
        descriptor = real_open(path, *args, **kwargs)
        if _is_index_metadata(path, kwargs):
            state.index_descriptor = descriptor
        return descriptor

    def fail_metadata_read(descriptor, *args, **kwargs):
        if descriptor == state.index_descriptor and not state.fired:
            state.fired = True
            raise OSError("injected metadata read failure")
        return real_read(descriptor, *args, **kwargs)

    monkeypatch.setattr(workspace_revision.os, "open", record_metadata_open)
    monkeypatch.setattr(workspace_revision.os, "read", fail_metadata_read)


def _inject_subprocess_failure(monkeypatch, state: _PrivateIndexInjection) -> None:
    real_popen = workspace_revision.subprocess.Popen

    class FailingReader:
        def __init__(self, wrapped) -> None:
            self.wrapped = wrapped

        def read(self, *args, **kwargs):
            state.fired = True
            raise OSError("injected subprocess pipe failure")

        def close(self) -> None:
            self.wrapped.close()

    def fail_private_process(*args, **kwargs):
        if not kwargs.get("pass_fds"):
            return real_popen(*args, **kwargs)
        if state.failure == "subprocess-launch":
            state.fired = True
            raise OSError("injected subprocess launch failure")
        process = real_popen(*args, **kwargs)
        process.stdout = FailingReader(process.stdout)
        return process

    monkeypatch.setattr(workspace_revision.subprocess, "Popen", fail_private_process)


def _inject_post_proof_failure(monkeypatch, state: _PrivateIndexInjection) -> None:
    def fail_post_proof(*args, **kwargs):
        state.fired = True
        raise OSError("injected post-proof stat failure")

    monkeypatch.setattr(
        workspace_revision, "validate_private_git_proof", fail_post_proof
    )


_PRIVATE_INDEX_INJECTIONS = {
    "memfd-fcntl": _inject_memfd_fcntl_failure,
    "metadata-stat": _inject_metadata_stat_failure,
    "metadata-open": _inject_metadata_open_failure,
    "metadata-read": _inject_metadata_read_failure,
    "subprocess-launch": _inject_subprocess_failure,
    "subprocess-read": _inject_subprocess_failure,
    "post-proof-stat": _inject_post_proof_failure,
}


def _install_failure_injection(monkeypatch, state: _PrivateIndexInjection) -> None:
    """Break one operational step of the private-index optimization."""
    if state.failure in _MEMFD_DESCRIPTOR_FAILURES:
        _inject_memfd_descriptor_failure(monkeypatch, state)
        return
    injector = _PRIVATE_INDEX_INJECTIONS.get(state.failure)
    if injector is not None:
        injector(monkeypatch, state)


def _race_index(repository: Path, **_context) -> None:
    """Replace the indexed blob without touching the working tree."""
    oid = _run_git(
        repository,
        "hash-object",
        "-w",
        "--stdin",
        input_bytes=b"index-only replacement\n",
    ).strip()
    _run_git(
        repository,
        "update-index",
        "--cacheinfo",
        "100644",
        oid.decode("ascii"),
        "pkg/api.py",
    )


def _race_head(repository: Path, **_context) -> None:
    _run_git(repository, "commit", "--allow-empty", "-m", "move head")


def _race_source(target: Path, **_context) -> None:
    target.write_text("changed_after_status = True\n", encoding="utf-8")


def _race_directory(repository: Path, **_context) -> None:
    (repository / "inventory-race.txt").write_text("changed\n", encoding="utf-8")


def _race_directory_restored_mtime(repository: Path, root_info, **_context) -> None:
    _race_directory(repository)
    os.utime(repository, ns=(root_info.st_atime_ns, root_info.st_mtime_ns))


def _race_tracked_irrelevant_restored_mtime(irrelevant: Path, **_context) -> None:
    before = irrelevant.stat()
    irrelevant.write_text("after!\n", encoding="utf-8")
    os.utime(irrelevant, ns=(before.st_atime_ns, before.st_mtime_ns))


def _race_deleted(deleted: Path, deleted_content: bytes, **_context) -> None:
    deleted.write_bytes(deleted_content)


def _race_symlink(target: Path, outside: Path, **_context) -> None:
    target.unlink()
    target.symlink_to(outside)


_WORKSPACE_RACES = {
    "index": _race_index,
    "head": _race_head,
    "source": _race_source,
    "directory": _race_directory,
    "directory-restored-mtime": _race_directory_restored_mtime,
    "tracked-irrelevant-restored-mtime": _race_tracked_irrelevant_restored_mtime,
    "deleted": _race_deleted,
}


def _apply_workspace_race(race: str, **context) -> None:
    """One mutation that lands after the private status was taken."""
    _WORKSPACE_RACES.get(race, _race_symlink)(**context)


@pytest.mark.skipif(not _linux_memfd_available(), reason="Linux memfd optimization")
@pytest.mark.parametrize(
    "race",
    [
        "index",
        "head",
        "source",
        "directory",
        "directory-restored-mtime",
        "tracked-irrelevant-restored-mtime",
        "deleted",
        "symlink",
    ],
)
def test_private_index_races_never_return_stale_true(
    repository: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    race: str,
) -> None:
    target = repository / "pkg/api.py"
    deleted = repository / "pkg/base.py"
    deleted_content = deleted.read_bytes()
    if race == "deleted":
        deleted.unlink()
    irrelevant = repository / "README.txt"
    if race == "tracked-irrelevant-restored-mtime":
        irrelevant.write_text("before\n", encoding="utf-8")
        _run_git(repository, "add", "README.txt")
        _run_git(repository, "commit", "-m", "track irrelevant file")
    outside = tmp_path / "outside.py"
    outside.write_bytes(target.read_bytes())
    root_info = repository.stat()
    scope = resolve_repository_scope(repository)
    expected = compute_workspace_revision(scope)
    real_private = workspace_revision.git_state_with_private_index
    fired = False

    def mutate_after_private_status(*args, **kwargs):
        nonlocal fired
        result = real_private(*args, **kwargs)
        _apply_workspace_race(
            race,
            repository=repository,
            target=target,
            irrelevant=irrelevant,
            deleted=deleted,
            deleted_content=deleted_content,
            outside=outside,
            root_info=root_info,
        )
        fired = True
        return result

    monkeypatch.setattr(
        workspace_revision,
        "git_state_with_private_index",
        mutate_after_private_status,
    )

    try:
        unchanged = workspace_revision.verify_workspace_revision_unchanged(scope, expected)
    except (PermissionError, RuntimeError):
        unchanged = False

    assert (fired, unchanged) == (True, False)


def _mutate_after(monkeypatch: pytest.MonkeyPatch, name: str, mutate) -> None:
    """Call `mutate` right after the product's `name` helper returns, keeping its result."""
    real = getattr(workspace_revision, name)

    def wrapper(*args, **kwargs):
        result = real(*args, **kwargs)
        mutate()
        return result

    monkeypatch.setattr(workspace_revision, name, wrapper)


def _mutate_before(monkeypatch: pytest.MonkeyPatch, name: str, mutate) -> None:
    """Call `mutate` just before the product's `name` helper is entered."""
    real = getattr(workspace_revision, name)

    def wrapper(*args, **kwargs):
        mutate()
        return real(*args, **kwargs)

    monkeypatch.setattr(workspace_revision, name, wrapper)


def _mutate_during_root_discovery(
    monkeypatch: pytest.MonkeyPatch, repository: Path, mutate
) -> None:
    """Call `mutate` when the root directory's own snapshot has just been validated."""
    real = workspace_revision.validate_directory_snapshot

    def wrapper(*args, **kwargs):
        result = real(*args, **kwargs)
        snapshot = args[1]
        if snapshot.path == repository:
            mutate()
        return result

    monkeypatch.setattr(workspace_revision, "validate_directory_snapshot", wrapper)


# One setup per parametrize id: each creates the ignored-but-relevant file at a
# different point of the verification, and every point must end in a False answer.
_IGNORED_INVENTORY_RACE_PHASES = {
    "after-content-hash": lambda monkeypatch, repository, mutate: (
        _mutate_after(monkeypatch, "hash_file_for_verification", mutate)
    ),
    "after-private-status": lambda monkeypatch, repository, mutate: (
        _mutate_after(monkeypatch, "git_state_with_private_index", mutate)
    ),
    "before-final-proof": lambda monkeypatch, repository, mutate: (
        _mutate_before(monkeypatch, "validate_private_git_proof", mutate)
    ),
    "before-private-proof": lambda monkeypatch, repository, mutate: (
        _mutate_after(monkeypatch, "_private_raw_semantics_safe", mutate)
    ),
    "during-discovery": _mutate_during_root_discovery,
}


@pytest.mark.skipif(not _linux_memfd_available(), reason="Linux memfd optimization")
@pytest.mark.parametrize("phase", sorted(_IGNORED_INVENTORY_RACE_PHASES))
def test_private_index_ignored_relevant_inventory_race_uses_fresh_exact_scan(
    repository: Path,
    monkeypatch: pytest.MonkeyPatch,
    phase: str,
) -> None:
    (repository / ".gitignore").write_text("late.py\n", encoding="ascii")
    _run_git(repository, "add", ".gitignore")
    _run_git(repository, "commit", "-m", "ignore late source")
    scope = resolve_repository_scope(repository)
    expected = compute_workspace_revision(scope)
    root_info = repository.stat()
    mutated = False

    def create_ignored_source() -> None:
        nonlocal mutated
        if mutated:
            return
        (repository / "late.py").write_text("late = True\n", encoding="ascii")
        os.utime(repository, ns=(root_info.st_atime_ns, root_info.st_mtime_ns))
        mutated = True

    _IGNORED_INVENTORY_RACE_PHASES[phase](monkeypatch, repository, create_ignored_source)

    assert (
        workspace_revision.verify_workspace_revision_unchanged(scope, expected),
        mutated,
    ) == (False, True)


@pytest.mark.skipif(not _linux_memfd_available(), reason="Linux filename semantics")
def test_private_index_unknown_irrelevant_backslash_name_uses_exact_fallback(
    repository: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (repository / ".gitignore").write_text("*.txt\n", encoding="ascii")
    _run_git(repository, "add", ".gitignore")
    _run_git(repository, "commit", "-m", "ignore text assets")
    (repository / "ignored\\asset.txt").write_text("ignored\n", encoding="ascii")
    scope = resolve_repository_scope(repository)
    expected = compute_workspace_revision(scope)

    result, exact_calls, private_calls = _verification_git_call_counts(
        scope, expected, monkeypatch
    )

    assert result is True
    assert exact_calls == 1
    assert private_calls == 0


@pytest.mark.skipif(not _linux_memfd_available(), reason="Linux memfd optimization")
def test_private_index_rejects_restored_mtime_mutation_after_content_hash(
    repository: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scope = resolve_repository_scope(repository)
    expected = compute_workspace_revision(scope)
    target = repository / "pkg/api.py"
    real_hash = workspace_revision.hash_file_for_verification
    fired = False

    def mutate_after_hash(path: Path, **kwargs):
        nonlocal fired
        result = real_hash(path, **kwargs)
        if path == target and not fired:
            info = target.stat()
            content = target.read_bytes()
            target.write_bytes(bytes([content[0] ^ 1]) + content[1:])
            os.utime(target, ns=(info.st_atime_ns, info.st_mtime_ns))
            fired = True
        return result

    monkeypatch.setattr(workspace_revision, "hash_file_for_verification", mutate_after_hash)

    try:
        unchanged = workspace_revision.verify_workspace_revision_unchanged(scope, expected)
    except PermissionError:
        unchanged = False

    assert fired
    assert unchanged is False


def test_private_index_parser_honors_cancellation(repository: Path) -> None:
    raw = (repository / ".git/index").read_bytes()
    calls = 0

    def cancelled() -> bool:
        nonlocal calls
        calls += 1
        return calls >= 3

    with pytest.raises(TimeoutError, match="cancel"):
        workspace_revision.parse_git_index(
            raw,
            hash_name="sha1",
            deadline=time.monotonic() + 5,
            cancelled=cancelled,
        )

    assert calls == 3


@pytest.mark.skipif(not _linux_memfd_available(), reason="Linux memfd optimization")
def test_private_index_memfd_closes_on_success_and_operational_timeout(
    repository: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scope = resolve_repository_scope(repository)
    expected = compute_workspace_revision(scope)
    real_create = os.memfd_create
    real_exact = workspace_revision.git_state
    descriptors: list[int] = []
    exact_calls = 0

    def recording_create(*args, **kwargs):
        descriptor = real_create(*args, **kwargs)
        descriptors.append(descriptor)
        return descriptor

    monkeypatch.setattr(workspace_revision.os, "memfd_create", recording_create)
    assert workspace_revision.verify_workspace_revision_unchanged(scope, expected) is True

    # A plain closure rather than a throwing generator expression: the generator
    # would cost the complexity gate a branch inside this test, the closure costs none.
    def timing_out_private_status(*args, **kwargs):
        raise TimeoutError("private status timeout")

    monkeypatch.setattr(
        workspace_revision,
        "git_state_with_private_index",
        timing_out_private_status,
    )

    def recording_exact(*args, **kwargs):
        nonlocal exact_calls
        exact_calls += 1
        return real_exact(*args, **kwargs)

    monkeypatch.setattr(workspace_revision, "git_state", recording_exact)
    assert workspace_revision.verify_workspace_revision_unchanged(scope, expected) is True

    assert (len(descriptors), exact_calls, (repository / ".git/index.lock").exists()) == (
        2,
        1,
        False,
    )
    for descriptor in descriptors:
        with pytest.raises(OSError):
            os.fstat(descriptor)


@pytest.mark.skipif(not _linux_memfd_available(), reason="Linux memfd optimization")
def test_private_index_is_fully_sealed_before_git_can_read_it(
    repository: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import fcntl

    get_seals = _seal_number("F_GET_SEALS", 1034)
    required = (
        _seal_number("F_SEAL_WRITE", 0x0008)
        | _seal_number("F_SEAL_GROW", 0x0004)
        | _seal_number("F_SEAL_SHRINK", 0x0002)
        | _seal_number("F_SEAL_SEAL", 0x0001)
    )
    real_private = workspace_revision.git_state_with_private_index
    real_create = os.memfd_create
    flags_seen: list[int] = []

    def recording_create(name: str, flags: int) -> int:
        flags_seen.append(flags)
        return real_create(name, flags)

    def assert_sealed(root: Path, descriptor: int, **kwargs):
        assert fcntl.fcntl(descriptor, get_seals) & required == required
        with pytest.raises(OSError):
            os.pwrite(descriptor, b"X", 0)
        return real_private(root, descriptor, **kwargs)

    monkeypatch.setattr(workspace_revision.os, "memfd_create", recording_create)
    monkeypatch.setattr(workspace_revision, "git_state_with_private_index", assert_sealed)
    scope = resolve_repository_scope(repository)
    expected = compute_workspace_revision(scope)

    assert workspace_revision.verify_workspace_revision_unchanged(scope, expected) is True
    assert flags_seen == [os.MFD_CLOEXEC | os.MFD_ALLOW_SEALING]


@pytest.mark.skipif(not _linux_memfd_available(), reason="Linux memfd optimization")
def test_private_index_inherited_by_git_descendant_remains_read_only(
    repository: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_git = shutil.which("git")
    if real_git is None:
        pytest.skip("Git executable is unavailable")
    scope = resolve_repository_scope(repository)
    expected = compute_workspace_revision(scope)
    wrapper_root = tmp_path / "sealed-git-wrapper"
    wrapper_root.mkdir()
    result = wrapper_root / "result"
    probe = wrapper_root / "probe.py"
    probe.write_text(
        "import os\n"
        "from pathlib import Path\n"
        "descriptor = os.open(os.environ['GIT_INDEX_FILE'], os.O_RDWR)\n"
        "try:\n"
        "    os.pwrite(descriptor, b'D', 0)\n"
        "except OSError:\n"
        "    outcome = 'sealed'\n"
        "else:\n"
        "    outcome = 'writable'\n"
        "finally:\n"
        "    os.close(descriptor)\n"
        "Path(os.environ['REVISION_SEAL_RESULT']).write_text(outcome, encoding='ascii')\n",
        encoding="ascii",
    )
    wrapper = wrapper_root / "git"
    wrapper.write_text(
        "#!/bin/sh\n"
        '"$REVISION_PYTHON" "$REVISION_SEAL_PROBE"\n'
        'exec "$REVISION_REAL_GIT" "$@"\n',
        encoding="ascii",
    )
    wrapper.chmod(0o755)
    _qualify_private_git_wrapper(monkeypatch, wrapper)
    monkeypatch.setenv("PATH", f"{wrapper_root}{os.pathsep}{os.environ.get('PATH', '')}")
    monkeypatch.setenv("REVISION_PYTHON", sys.executable)
    monkeypatch.setenv("REVISION_REAL_GIT", real_git)
    monkeypatch.setenv("REVISION_SEAL_PROBE", str(probe))
    monkeypatch.setenv("REVISION_SEAL_RESULT", str(result))

    assert workspace_revision.verify_workspace_revision_unchanged(scope, expected) is True
    assert result.read_text(encoding="ascii") == "sealed"


@pytest.mark.skipif(not _linux_memfd_available(), reason="Linux memfd optimization")
@pytest.mark.parametrize(
    "failure",
    [
        "memfd-create",
        "memfd-fchmod",
        "memfd-write",
        "memfd-fsync",
        "memfd-lseek",
        "memfd-utime",
        "memfd-fcntl",
        "metadata-stat",
        "metadata-open",
        "metadata-read",
        "subprocess-launch",
        "subprocess-read",
        "post-proof-stat",
    ],
)
def test_private_index_operational_uncertainty_uses_exact_fallback(
    repository: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    scope = resolve_repository_scope(repository)
    expected = compute_workspace_revision(scope)
    state = _PrivateIndexInjection(failure)
    _install_memfd_recorder(monkeypatch, state)
    _install_failure_injection(monkeypatch, state)

    result, exact_calls, _private_calls = _verification_git_call_counts(
        scope, expected, monkeypatch
    )

    assert state.fired
    assert result is True
    assert exact_calls == 1


@pytest.mark.skipif(not _linux_memfd_available(), reason="Linux memfd optimization")
@pytest.mark.parametrize("metadata", ["config", "HEAD", "index"])
def test_private_index_malformed_metadata_uses_exact_fallback(
    repository: Path,
    monkeypatch: pytest.MonkeyPatch,
    metadata: str,
) -> None:
    scope = resolve_repository_scope(repository)
    expected = compute_workspace_revision(scope)
    real_read = workspace_revision.read_owned_file
    fired = False
    malformed = {
        "config": b"[include]\npath = /outside\n",
        "HEAD": b"not-an-object-id\n",
        "index": b"not-an-index",
    }

    def return_malformed(root: Path, path: Path, *args, **kwargs):
        nonlocal fired
        result = real_read(root, path, *args, **kwargs)
        if path.name == metadata and result is not None and not fired:
            fired = True
            return type(result)(malformed[metadata], result.fence)
        return result

    monkeypatch.setattr(workspace_revision, "read_owned_file", return_malformed)

    result, exact_calls, private_calls = _verification_git_call_counts(
        scope, expected, monkeypatch
    )

    assert fired
    assert result is True
    assert exact_calls == 1
    assert private_calls == 0


@pytest.mark.skipif(not _linux_memfd_available(), reason="Linux metadata descriptors")
@pytest.mark.parametrize("metadata", ["config", "HEAD", "index", "ref"])
def test_private_metadata_fifo_is_rejected_without_opening(
    repository: Path,
    metadata: str,
) -> None:
    if metadata == "ref":
        reference = _run_git(repository, "symbolic-ref", "HEAD").decode("ascii").strip()
        target = repository / ".git" / reference
    else:
        target = repository / ".git" / metadata
    original = target.read_bytes()
    target.unlink()
    try:
        os.mkfifo(target)
    except OSError:
        target.write_bytes(original)
        pytest.skip("FIFO creation is unavailable")
    try:
        started = time.monotonic()
        result = workspace_revision.read_owned_file(
            repository,
            target,
            64 * 1024 * 1024,
            deadline=started + 0.25,
            cancelled=None,
        )
        elapsed = time.monotonic() - started
    finally:
        target.unlink()
        target.write_bytes(original)

    assert result is None
    assert elapsed < 0.1


@pytest.mark.skipif(not _linux_memfd_available(), reason="Linux metadata descriptors")
@pytest.mark.parametrize("metadata", ["config", "HEAD", "index", "ref"])
def test_private_metadata_open_race_cannot_block_past_caller_deadline(
    repository: Path,
    monkeypatch: pytest.MonkeyPatch,
    metadata: str,
) -> None:
    scope = resolve_repository_scope(repository)
    expected = compute_workspace_revision(scope)
    if metadata == "ref":
        reference = _run_git(repository, "symbolic-ref", "HEAD").decode("ascii").strip()
        target_name = PurePosixPath(reference).name
    else:
        target_name = metadata
    real_open = workspace_revision.os.open
    fired = False

    def simulate_fifo_race(path, flags, *args, **kwargs):
        nonlocal fired
        if not fired and _opens_target_file(path, target_name, flags, kwargs):
            fired = True
            _block_unless_nonblocking(flags)
            raise BlockingIOError("injected FIFO replacement")
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(workspace_revision.os, "open", simulate_fifo_race)
    started = time.monotonic()
    result = workspace_revision.verify_workspace_revision_unchanged(
        scope,
        expected,
        deadline=started + 0.5,
    )
    elapsed = time.monotonic() - started

    assert fired
    assert result is True
    assert elapsed < 0.5


@pytest.mark.skipif(not _linux_memfd_available(), reason="Linux memfd optimization")
def test_private_index_post_proof_cancellation_propagates(
    repository: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scope = resolve_repository_scope(repository)
    expected = compute_workspace_revision(scope)
    real_validate = workspace_revision.validate_private_git_proof
    validation_started = False

    def cancelled() -> bool:
        return validation_started

    def cancel_during_validation(*args, **kwargs):
        nonlocal validation_started
        validation_started = True
        return real_validate(*args, **kwargs)

    monkeypatch.setattr(
        workspace_revision,
        "validate_private_git_proof",
        cancel_during_validation,
    )

    with pytest.raises(TimeoutError, match="cancelled"):
        workspace_revision.verify_workspace_revision_unchanged(
            scope,
            expected,
            cancelled=cancelled,
        )
    assert validation_started
