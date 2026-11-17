"""Sweeping owner roots whose recorded processes are proven dead."""
from __future__ import annotations

import contextlib
import os
import re
import shutil
import stat
from collections.abc import Iterator, Sequence
from pathlib import Path

from llm_wiki.lsp_process_limits import (
    MAX_EVIDENCE_BYTES,
)
from llm_wiki.lsp_process_owner import (
    canonical_evidence_record,
)
from llm_wiki.lsp_process_state import (
    is_server_pid,
)
from process_liveness import MAX_START_IDENTITY_CHARS as _MAX_START_IDENTITY_CHARS
from process_liveness import owner_alive, process_start_identity


def validated_owner_root(owner_root: Path) -> str:
    owner_nonce = owner_root.name
    if _OWNER_NONCE_PATTERN.fullmatch(owner_nonce) is None:
        raise ValueError(
            "owner_root basename must be 32 lowercase hexadecimal characters"
        )
    _require_fresh_owner_root(owner_root)
    sweep_dead_owner_roots(owner_root)
    return owner_nonce


_OWNER_NONCE_PATTERN = re.compile(r"[0-9a-f]{32}")
# The sweep examines no more owner roots than doctor reports on; a root that
# kept failure evidence is passed over with one stat and costs nothing of it.
_MAX_SWEPT_OWNER_ROOTS = 128
# Entries of run/lsp/ the sweep looks at before stopping; a vault holds tens. Value predates
# measurement.
_MAX_SCANNED_OWNER_ENTRIES = 4096


def add_start_identity(record: dict, field: str, pid: int) -> None:
    """Name the start of the process a pid points at, when the platform can tell.

    A pid alone kept a dead root forever once the OS handed it to another process
    (audit C-39, docs/research/2026-09-25-the-lsp-sweep-reaches-every-dead-root.md).
    """
    try:
        identity = process_start_identity(pid)
    except (OSError, ValueError):
        return
    if isinstance(identity, str) and 0 < len(identity) <= _MAX_START_IDENTITY_CHARS:
        record[field] = identity


def sweep_dead_owner_roots(owner_root: Path) -> None:
    """Remove sibling owner roots whose owner is dead and which kept no evidence.

    A controlled close removes its own root; a failure or an abrupt death
    leaves one behind, with its records and any sealed `launch-` tree, and
    nothing else in the product ever removes it. There is no daemon, so the
    sweep runs at the one moment the product is already working in this
    directory: starting a new server. It never raises — a sweep that cannot
    finish is no reason to fail a start. Research:
    `docs/research/2026-09-17-lsp-the-owner-record-names-the-first-generation-only.md`.
    """
    with contextlib.suppress(OSError):
        _sweep_owner_parent(owner_root.parent, owner_root.name)


def _sweep_owner_parent(parent: Path, mine: str) -> None:
    """Examine at most `_MAX_SWEPT_OWNER_ROOTS` candidates among the first entries.

    Counting every entry let roots that kept failure evidence fill the window, so
    dead roots past them were never examined (audit C-39).
    """
    with os.scandir(parent) as entries:
        _sweep_scanned_owner_roots(entries, mine)


def _sweep_scanned_owner_roots(entries: Iterator[os.DirEntry], mine: str) -> None:
    examined = 0
    for scanned, entry in enumerate(entries, 1):
        if scanned > _MAX_SCANNED_OWNER_ENTRIES or examined >= _MAX_SWEPT_OWNER_ROOTS:
            return
        examined += _sweep_one_owner_root(Path(entry.path), entry.name, mine)


def _strip_dead_evidence_root(root: Path) -> None:
    """A failure root keeps its records; its dead server's binary copy is not evidence (C-7)."""
    copies = _plain_launch_copies(root)
    if copies and _owner_root_is_dead(root):
        _unlink_all(copies)


def _plain_launch_copies(root: Path) -> list[Path]:
    return [path for path in root.glob(".launch-*") if _is_plain_file(path)]


def _unlink_all(paths: list[Path]) -> None:
    for path in paths:
        with contextlib.suppress(OSError):
            path.unlink()


def _is_plain_file(path: Path) -> bool:
    return path.is_file() and not path.is_symlink()


def remove_dead_owner_root(root: Path) -> None:
    if _owner_root_is_dead(root):
        with contextlib.suppress(OSError):
            _remove_owner_root_tree(root)


def _sweep_one_owner_root(root: Path, name: str, mine: str) -> int:
    """1 when this root was a candidate the sweep examined, else 0."""
    if name == mine or _OWNER_NONCE_PATTERN.fullmatch(name) is None:
        return 0
    if (root / "failure.json").exists():
        _strip_dead_evidence_root(root)
        return 0
    remove_dead_owner_root(root)
    return 1


def _owner_root_is_dead(root: Path) -> bool:
    """Every process the records name is proven gone (the caller kept evidence roots)."""
    processes = _owner_root_pids(root)
    if not processes:
        return False
    return all(process_is_gone(pid, identity) for pid, identity in processes)


def process_is_gone(pid: int, identity: str | None) -> bool:
    """Proven dead: gone, or its pid now names a process that started later."""
    return not owner_alive(pid, identity)


def _owner_root_pids(root: Path) -> tuple[tuple[int, str | None], ...]:
    """Every process this root's records name, or () when they cannot be read.

    The owner record must be there: without it the root belongs to a start
    that has not published yet, which is a live one. The lease may be absent —
    a controlled cleanup removes it — but if it is there it names the
    generation running now, which after a restart is not the one the owner
    record names.
    """
    owner = _record_pids(
        root / "owner.json", (("owner_pid", "owner_start_identity"),), required=True
    )
    lease = _record_pids(
        root / "lease.json",
        (
            ("manager_pid", "manager_start_identity"),
            ("server_pid", "server_start_identity"),
        ),
        required=False,
    )
    if owner is None or lease is None:
        return ()
    return owner + lease


def _record_pids(
    path: Path, names: tuple[tuple[str, str], ...], *, required: bool
) -> tuple[tuple[int, str | None], ...] | None:
    """The pids the record names; () when absent and allowed to be; None otherwise."""
    try:
        payload = _bounded_record_bytes(path)
    except FileNotFoundError:
        return None if required else ()
    except OSError:
        return None
    return _payload_pids(payload, names)


def _bounded_record_bytes(path: Path) -> bytes:
    with open(path, "rb") as handle:
        payload = handle.read(MAX_EVIDENCE_BYTES + 1)
    if len(payload) > MAX_EVIDENCE_BYTES:
        raise OSError("LSP evidence record exceeds its byte bound")
    return payload


def _payload_pids(
    payload: bytes, names: tuple[tuple[str, str], ...]
) -> tuple[tuple[int, str | None], ...] | None:
    """Each (pid, start identity) pair, or None when any pid is not one."""
    try:
        record = canonical_evidence_record(payload)
    except ValueError:
        return None
    return _pid_pairs(record, names)


def _pid_pairs(
    record: dict, names: tuple[tuple[str, str], ...]
) -> tuple[tuple[int, str | None], ...] | None:
    pairs = tuple(
        (record.get(pid_name), _recorded_identity(record.get(identity_name)))
        for pid_name, identity_name in names
    )
    if not all(is_server_pid(pid) for pid, _ in pairs):
        return None
    return pairs


def _recorded_identity(value: object) -> str | None:
    if isinstance(value, str) and 0 < len(value) <= _MAX_START_IDENTITY_CHARS:
        return value
    return None


def _remove_owner_root_tree(root: Path) -> None:
    """Take one dead owner root down, unsealing the launch trees it may hold."""
    for directory, _subdirectories, files in os.walk(root):
        _unseal_for_removal(Path(directory), files)
    shutil.rmtree(root)


def _unseal_for_removal(directory: Path, files: Sequence[str]) -> None:
    """A sealed launch tree is 0o500 and 0o400; nothing can be unlinked inside it."""
    os.chmod(directory, 0o700)
    for name in files:
        path = directory / name
        # chmod follows a link, and Linux cannot chmod the link itself; a link
        # needs no mode to be unlinked (audit C-39).
        if not stat.S_ISLNK(os.lstat(path).st_mode):
            os.chmod(path, 0o600)


def _require_fresh_owner_root(owner_root: Path) -> None:
    if os.path.lexists(owner_root):
        raise FileExistsError(owner_root)
    if not owner_root.parent.exists() or not owner_root.parent.is_dir():
        raise FileNotFoundError(owner_root.parent)
