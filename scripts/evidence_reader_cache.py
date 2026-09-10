"""Process-local reuse of validated Evidence Graph readers.

Measured 2026-09-10 on a 1 022-file fixture with a 44.7 MB generation: a warm
`find_callers` spent 458 of 521 ms *opening* the generation — the catalog
validated it three times per open, each pass hashing all six artifacts, then
the reader hashed `evidence.sqlite3` once more — and 60 ms answering. The
cost was proportional to artifact bytes times opens per answer, which is why
the owner saw 1.1 s per `callers` answer on a 105 MB generation (#24).

A generation is immutable after registration, so a reader that was fully
validated once in this process stays valid as long as nothing it depends on
has changed identity. This module keeps the last few validated readers and
reuses one while three stat identities hold, the way Git's index trusts an
unchanged `lstat` before it reads content (racy-git):

* `catalog.sqlite3` — any registration or activation rewrites it, so an
  unchanged catalog means the selection for this scope is unchanged;
* the generation's `evidence.sqlite3` — immutable, proven rather than assumed;
* the checkout's Git state files (`HEAD`, `ORIG_HEAD`, `index`, `logs/HEAD`,
  `packed-refs`, the branch ref) — a commit or checkout touches at least one,
  and then the scope is re-resolved and kept only for the same repository.

Nothing here touches disk except `stat`; there is no state to delete. Callers
keep their `try/finally: graph.close()` shape: they receive a lease whose
`close()` releases the reader instead of closing it. A reader is closed when
it is evicted, forgotten, or superseded, and never while a lease still holds
it. Design: `docs/research/2026-09-10-warm-index-answers-inside-the-loop.md`.
"""

from __future__ import annotations

import os
import sqlite3
import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

# Eight directories is more than one session ever queries at once; beyond it
# the least recently used reader is closed.
MAX_ENTRIES = 8
# A reader nobody has asked for in ten minutes is closed on the next cache
# access. An open reader pins its generation's file, and on Windows a pinned
# file cannot be pruned; a session that keeps asking keeps the reader, a
# session that moved on releases it. A process that never asks again still
# holds one reader until it exits.
IDLE_SECONDS = 10 * 60

# What a commit, checkout, reset or merge touches in the checkout's own git
# directory, and in the common directory shared by linked worktrees.
_GIT_DIR_STATE = ("HEAD", "ORIG_HEAD", "index", "logs/HEAD")
_COMMON_DIR_STATE = ("packed-refs",)



def shared_readers_supported() -> bool:
    """Only a serialized sqlite3 build may hand one connection to many threads."""
    return sqlite3.threadsafety == 3


def _stat_identity(path: Path):
    try:
        info = os.stat(path)
    except OSError:
        return None
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)


def _identities(paths: tuple[Path, ...]) -> tuple:
    return tuple(_stat_identity(path) for path in paths)


def _linked_git_dir(marker: Path, root: Path) -> Path | None:
    """The `gitdir:` target of a linked worktree's `.git` file."""
    try:
        text = marker.read_text(encoding="utf-8", errors="strict")
    except OSError:
        return None
    if not text.startswith("gitdir:"):
        return None
    target = Path(text[len("gitdir:"):].strip())
    return target if target.is_absolute() else root / target


def _git_dir(scope) -> Path | None:
    if scope.git_common_dir is None:
        return None
    root = Path(scope.checkout_root)
    marker = root / ".git"
    if marker.is_dir():
        return marker
    return _linked_git_dir(marker, root)


def _head_ref_path(git_dir: Path, common: Path) -> Path | None:
    """The loose ref file HEAD points at, when HEAD is symbolic."""
    try:
        head = (git_dir / "HEAD").read_text(encoding="utf-8", errors="strict")
    except OSError:
        return None
    if not head.startswith("ref:"):
        return None
    return common / head[len("ref:"):].strip()


def git_state_paths(scope) -> tuple[Path, ...]:
    """The files whose stat identity changes when the checkout's HEAD moves."""
    git_dir = _git_dir(scope)
    if git_dir is None:
        return ()
    common = Path(scope.git_common_dir)
    paths = [git_dir / name for name in _GIT_DIR_STATE]
    paths.extend(common / name for name in _COMMON_DIR_STATE)
    ref = _head_ref_path(git_dir, common)
    if ref is not None:
        paths.append(ref)
    return tuple(paths)


@dataclass
class _Entry:
    key: tuple[str, bool]
    scope: object
    graph: object
    catalog_path: Path
    catalog_identity: object
    artifact_identity: object
    git_paths: tuple[Path, ...]
    git_identity: tuple
    leases: int = 0
    retired: bool = False
    last_used: float = field(default_factory=time.monotonic)
    memo: dict = field(default_factory=dict)
    # Held for the lifetime of every lease. Python's sqlite3 interleaves the
    # statements of one connection used from two threads — measured 2026-09-10:
    # eight concurrent `find_callers` gave three different answers — so a
    # shared reader is used by one thread at a time. Re-entrant, because one
    # answer may open the same directory twice on its own thread.
    lock: threading.RLock = field(default_factory=threading.RLock)

    def catalog_unchanged(self, catalog_path: Path) -> bool:
        if catalog_path != self.catalog_path:
            return False
        return _stat_identity(catalog_path) == self.catalog_identity

    def artifact_unchanged(self) -> bool:
        return _stat_identity(self.graph.database_path) == self.artifact_identity

    def git_unchanged(self) -> bool:
        return _identities(self.git_paths) == self.git_identity

    def adopt_scope(self, scope) -> None:
        self.scope = scope
        self.git_paths = git_state_paths(scope)
        self.git_identity = _identities(self.git_paths)


class GraphLease:
    """One borrower's handle on a cached reader; `close()` returns it."""

    def __init__(self, entry: _Entry) -> None:
        self._entry = entry
        self._graph = entry.graph
        self._held = True

    def __getattr__(self, name: str):
        return getattr(self._graph, name)

    @property
    def cached_scope(self):
        """The checkout scope as last resolved, including its current commit."""
        return self._entry.scope

    def memoized(self, key: str, compute: Callable[[], object]) -> object:
        """A per-generation value computed once; the generation cannot change."""
        memo = self._entry.memo
        if key not in memo:
            memo[key] = compute()
        return memo[key]

    def close(self) -> None:
        if not self._held:
            return
        self._held = False
        _release(self._entry)

    def __enter__(self) -> GraphLease:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def __del__(self) -> None:
        # A lease dropped without `close()` must not pin the reader for the
        # other threads; at interpreter exit the registry may already be gone.
        try:
            self.close()
        except Exception:  # noqa: BLE001 - finalizer, nothing to report to
            pass


_LOCK = threading.Lock()
_ENTRIES: OrderedDict[tuple[str, bool], _Entry] = OrderedDict()


def _close_quietly(graph) -> None:
    try:
        graph.close()
    except (sqlite3.Error, OSError, AttributeError):
        pass


def _retire_entry(entry: _Entry) -> None:
    """Under the lock: close now, or at the last release when still borrowed."""
    entry.retired = True
    if entry.leases == 0:
        _close_quietly(entry.graph)


def _release(entry: _Entry) -> None:
    entry.lock.release()
    with _LOCK:
        entry.leases -= 1
        if entry.retired and entry.leases == 0:
            _close_quietly(entry.graph)


def _borrow(entry: _Entry) -> bool:
    """Under the registry lock: count the lease unless the entry was retired."""
    with _LOCK:
        if entry.retired:
            return False
        entry.leases += 1
        entry.last_used = time.monotonic()
        _ENTRIES.move_to_end(entry.key)
        return True


def _expire_idle(now: float) -> None:
    """Close readers nobody asked for within IDLE_SECONDS; a held lease waits."""
    with _LOCK:
        idle = [entry for entry in _ENTRIES.values() if now - entry.last_used > IDLE_SECONDS]
        for entry in idle:
            _ENTRIES.pop(entry.key, None)
            _retire_entry(entry)


def _lease(entry: _Entry) -> GraphLease | None:
    # The entry lock is taken outside the registry lock: a holder releasing
    # needs the registry lock, so taking both in the other order would deadlock.
    entry.lock.acquire()
    if _borrow(entry):
        return GraphLease(entry)
    entry.lock.release()
    return None


def _store(entry: _Entry) -> _Entry:
    with _LOCK:
        previous = _ENTRIES.pop(entry.key, None)
        if previous is not None:
            _retire_entry(previous)
        _ENTRIES[entry.key] = entry
        while len(_ENTRIES) > MAX_ENTRIES:
            _retire_entry(_ENTRIES.popitem(last=False)[1])
    return entry


def forget(key: tuple[str, bool]) -> None:
    """Drop one directory's reader, closing it once nobody holds it."""
    with _LOCK:
        entry = _ENTRIES.pop(key, None)
        if entry is not None:
            _retire_entry(entry)


def clear() -> None:
    """Drop every reader; tests call this between isolated vaults."""
    with _LOCK:
        entries = list(_ENTRIES.values())
        _ENTRIES.clear()
        for entry in entries:
            _retire_entry(entry)


def _with_current_scope(entry: _Entry, resolve_scope: Callable[[], object]):
    if entry.git_unchanged():
        return entry
    scope = resolve_scope()
    if not scope.same_repository(entry.scope):
        return None
    entry.adopt_scope(scope)
    return entry


def _reusable(entry: _Entry | None, catalog_path: Path, resolve_scope):
    if entry is None or not entry.catalog_unchanged(catalog_path):
        return None
    if not entry.artifact_unchanged():
        return None
    return _with_current_scope(entry, resolve_scope)


def _fill(key, catalog_path: Path, resolve_scope, open_graph) -> GraphLease | None:
    # Identities are captured before the open they guard: a catalog or a
    # checkout that changes during the open leaves a mismatch behind, so the
    # next call re-opens rather than trusting a reader that may be behind.
    catalog_identity = _stat_identity(catalog_path)
    scope = resolve_scope()
    git_paths = git_state_paths(scope)
    git_identity = _identities(git_paths)
    graph = open_graph(scope)
    if graph is None:
        forget(key)
        return None
    entry = _Entry(
        key=key,
        scope=scope,
        graph=graph,
        catalog_path=catalog_path,
        catalog_identity=catalog_identity,
        artifact_identity=_stat_identity(graph.database_path),
        git_paths=git_paths,
        git_identity=git_identity,
    )
    return _lease(_store(entry))


def leased_graph(
    key: tuple[str, bool],
    *,
    catalog_path: Path,
    resolve_scope: Callable[[], object],
    open_graph: Callable[[object], object],
) -> GraphLease | None:
    """A validated reader for `key`, reused while its identities hold.

    `resolve_scope` and `open_graph` are the caller's own bounded calls; they
    run only on a miss or after the checkout's Git state moved. `None` means
    the catalog holds no generation for this repository right now.
    """
    _expire_idle(time.monotonic())
    with _LOCK:
        entry = _ENTRIES.get(key)
    reusable = _reusable(entry, catalog_path, resolve_scope)
    lease = None if reusable is None else _lease(reusable)
    if lease is not None:
        return lease
    return _fill(key, catalog_path, resolve_scope, open_graph)


def cached_entries() -> int:
    with _LOCK:
        return len(_ENTRIES)
