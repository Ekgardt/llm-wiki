"""Build, refresh and retire the derived evidence generation under the maintenance fence."""
from __future__ import annotations

import contextlib
import hashlib
import math
import os
import secrets
import shutil
import sqlite3
import stat
import sys
import threading
import time
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, NamedTuple

import process_liveness
import reliable_memory
from iso_time import utc_text
from llm_wiki import monotonic_deadline, runtime_roots
from llm_wiki.formats.canonical import canonical_json_bytes
from settings import setting_value

# doctor's whole-run budget; one run widens it with --time-budget. basis unknown — value predates measurement; review when checks report budget exhaustion on an idle machine.
DEFAULT_TIME_BUDGET_SECONDS = 5.0


# A generation build asked for without a budget (`doctor --rebuild-generation`,
# `search_memory` rebuild); the nightly and the compile pass their own. A build out of
# time stops and says so. Basis unknown: value predates measurement; review when a
# manual rebuild on the installed vault is seen running out of time.
DEFAULT_GENERATION_TIME_BUDGET_SECONDS = 60.0


# An unregistered, invalid generation directory touched this recently may be a build
# in flight under another fence; the longest builder bound is 15 minutes. See
# `docs/research/2026-09-14-a-build-in-flight-is-not-an-orphan.md`; the rule itself is
# `generation_lineage.untouched_for`, shared with the prune.
GENERATION_ORPHAN_GRACE_SECONDS = 24 * 60 * 60


# doctor's maintenance fence is a "repair" lease in the ownership registry, whose
# timing is fixed there (operational_ownership.timing: 120 s TTL, 40 s heartbeat);
# these must equal it, or the registry refuses the lease. A contract, not a tunable.
MAINTENANCE_LEASE_SECONDS = 120
MAINTENANCE_HEARTBEAT_SECONDS = 40.0


def as_utc(value: datetime | None) -> datetime:
    value = value or datetime.now(UTC)
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def result(check_id: str, status: str, message: str, details: dict) -> dict:
    return {"id": check_id, "status": status, "message": message, "details": details}


def within(path: Path, root: Path) -> bool:
    try:
        resolved_path = path.resolve()
        resolved_root = root.resolve()
        common = os.path.commonpath((resolved_path, resolved_root))
        return os.path.normcase(common) == os.path.normcase(resolved_root)
    except (OSError, ValueError):
        return False


def safe_kind(path: Path, root: Path) -> tuple[str, os.stat_result | None]:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return "missing", None
    except OSError:
        return "unsafe", None
    return _kind_of_stat(path, root, info), info


def _kind_of_stat(path: Path, root: Path, info: os.stat_result) -> str:
    if stat.S_ISLNK(info.st_mode):
        return "symlink"
    if not within(path, root):
        return "outside"
    return _regular_kind(info)


def _regular_kind(info: os.stat_result) -> str:
    if stat.S_ISDIR(info.st_mode):
        return "directory"
    if stat.S_ISREG(info.st_mode):
        return "regular"
    return "special"


def database_table_columns(
    database: sqlite3.Connection,
    table: str,
    deadline: float = float("inf"),
) -> set[str]:
    if monotonic_deadline.deadline_reached(deadline):
        raise TimeoutError("database schema deadline")
    rows = database.execute(f'PRAGMA table_info("{table}")').fetchmany(257)
    if len(rows) > 256:
        raise sqlite3.DatabaseError("database schema exceeds column limit")
    return {str(row[1]) for row in rows}


def parse_utc(value: object) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def row_value(row: sqlite3.Row, column: str) -> object:
    return row[column] if column in row.keys() else None


def live_owner(row: sqlite3.Row, now: datetime, *, pid_column: str) -> bool:
    identity = row_value(row, "process_start_identity")
    expiry = parse_utc(row_value(row, "expires_at"))
    return owner_pid_live(row_value(row, pid_column), identity) or _owner_unexpired(expiry, now)


def owner_pid_live(pid: object, identity: object = None) -> bool:
    """The recorded start identity, when a row has one, names the process, not the PID (C-12)."""
    if not isinstance(pid, int) or pid <= 0:
        return False
    if not isinstance(identity, str):
        return process_liveness.owner_alive(pid)
    return process_liveness.owner_alive(pid, identity)


def _owner_unexpired(expiry: datetime | None, now: datetime) -> bool:
    return expiry is not None and expiry > now


def filesystem_check(state_root: Path, deadline: float | None = None) -> dict:
    deadline = validated_doctor_deadline(deadline, DEFAULT_TIME_BUDGET_SECONDS)
    if monotonic_deadline.deadline_reached(deadline):
        return result(
            "filesystem",
            "error",
            "Filesystem check exceeded its deadline.",
            {
                "local": False,
                "locking": "unknown",
                "budget_exhausted": True,
                "read_error": True,
            },
        )
    unusable = _filesystem_unusable(state_root)
    if unusable is not None:
        return unusable
    return _locking_result(_probe_locking(state_root, deadline))


def _filesystem_unusable(state_root: Path) -> dict | None:
    try:
        network = reliable_memory.known_network_path(state_root)
    except (OSError, RuntimeError, ValueError):
        return result(
            "filesystem",
            "degraded",
            "Runtime filesystem type could not be determined.",
            {"local": False, "locking": "unknown", "read_error": True},
        )
    if network:
        return result(
            "filesystem",
            "error",
            "Runtime must use a local filesystem.",
            {"local": False, "locking": "unsupported"},
        )
    if not state_root.is_dir():
        return result(
            "filesystem",
            "degraded",
            "Runtime filesystem locking cannot be probed until the state root exists.",
            {"local": True, "locking": "unknown"},
        )
    return None


def _probe_locking(state_root: Path, probe_deadline: float) -> bool | None:
    try:
        return reliable_memory.sqlite_lock_probe(state_root, deadline=probe_deadline)
    except (OSError, RuntimeError, sqlite3.Error):
        return None


# Three states, one table each: a nested ternary hid which reading meant what.
_LOCKING_STATE = {True: "supported", False: "unsupported", None: "unknown"}


_LOCKING_STATUS = {True: "ok", False: "error", None: "degraded"}


_LOCKING_MESSAGE = {
    True: "Runtime filesystem supports local locking.",
    False: "Runtime filesystem locking is broken.",
    None: "Runtime filesystem locking probe is unavailable.",
}


def _locking_result(locking: bool | None) -> dict:
    return result(
        "filesystem",
        _LOCKING_STATUS[locking],
        _LOCKING_MESSAGE[locking],
        {"local": True, "locking": _LOCKING_STATE[locking]},
    )


def maintenance_extractor_identity() -> str:
    import code_languages
    from llm_wiki import extractor_versions

    inputs = {
        "classifier": code_languages.CLASSIFIER_IDENTITY,
        "code_extractor": extractor_versions.CODE_EXTRACTOR_VERSION,
        "corpus_extractor": extractor_versions.CORPUS_EXTRACTOR_VERSION,
    }
    digest = hashlib.sha256(canonical_json_bytes(inputs)).hexdigest()
    return f"maintenance-extractors/v3:{digest}"


def active_pointer(database: sqlite3.Connection) -> object:
    row = database.execute(
        "SELECT active_generation_id FROM catalog_state WHERE singleton=1"
    ).fetchone()
    if row is None:
        return None
    return row[0]


def generation_source_limit(max_sources: int | None, root: Path) -> int:
    """The caller's source bound, else the vault's `settings` corpus.max_files."""
    return setting_value("corpus.max_files", root) if max_sources is None else max_sources


def require_positive_source_limit(max_sources: object) -> None:
    if (
        isinstance(max_sources, bool)
        or not isinstance(max_sources, int)
        or max_sources < 1
    ):
        raise ValueError("max_sources must be a positive integer")


def _ensure_maintenance_schema(database: sqlite3.Connection) -> None:
    database.execute(
        """CREATE TABLE IF NOT EXISTS maintenance_owners (
               owner_name TEXT PRIMARY KEY,
               owner_token TEXT NOT NULL,
               process_id INTEGER NOT NULL,
               acquired_at TEXT NOT NULL,
               heartbeat_at TEXT,
               expires_at TEXT,
               fencing_epoch INTEGER NOT NULL DEFAULT 1
           )"""
    )
    columns = database_table_columns(database, "maintenance_owners")
    for name, declaration in (
        ("heartbeat_at", "TEXT"),
        ("expires_at", "TEXT"),
        ("fencing_epoch", "INTEGER NOT NULL DEFAULT 1"),
    ):
        if name not in columns:
            database.execute(f"ALTER TABLE maintenance_owners ADD COLUMN {name} {declaration}")


class MaintenanceFenceLost(RuntimeError):
    """The maintenance fence is no longer ours, with what the row held.

    Three separate checks raise this, and until 2026-08-24 all three raised the
    same bare string, so a deferred nightly rebuild said only that the fence was
    lost — never which check saw it, nor who held the row instead. `where` names
    the check and `observed` carries the row, so the next occurrence identifies
    the other owner instead of starting the investigation from zero.
    """

    def __init__(self, where: str, observed: dict[str, object]) -> None:
        super().__init__("maintenance_owner_fence_lost")
        self.where = where
        self.observed = observed


_OWNER_ROW_FIELDS = (
    "process_id",
    "fencing_epoch",
    "acquired_at",
    "heartbeat_at",
    "expires_at",
    "role",
    "actor_id",
)


def _observed_owner_row(row: sqlite3.Row | None) -> dict[str, object]:
    if row is None:
        return {"present": False}
    columns = set(row.keys())
    observed: dict[str, object] = {"present": True}
    for field in _OWNER_ROW_FIELDS:
        observed[field] = row[field] if field in columns else None
    return observed


def _read_owner_row(database: Any) -> sqlite3.Row | None:
    return database.execute(
        "SELECT * FROM maintenance_owners WHERE owner_name='doctor'"
    ).fetchone()


def _fence_lost(where: str, database: Any, lease: dict[str, object]) -> MaintenanceFenceLost:
    observed = _observed_owner_row(_read_owner_row(database))
    observed["held_epoch"] = lease.get("epoch")
    observed["held_by_process"] = os.getpid()
    return MaintenanceFenceLost(where, observed)


# The adopted runtime has one ownership registry, and doctor's maintenance
# fence is one row in it. "repair" is the registry role whose lease timing
# (120 s TTL, 40 s heartbeat) matches MAINTENANCE_LEASE_SECONDS and
# MAINTENANCE_HEARTBEAT_SECONDS, and it carries no marker obligation; the
# scope keeps every doctor pass — generation refresh and --repair alike —
# mutually exclusive, exactly as the legacy 'doctor' owner row did.
V3_MAINTENANCE_ROLE = "repair"


V3_MAINTENANCE_SCOPE = "doctor:maintenance"


def maintenance_ownership_registry(coordinator: Any) -> Any | None:
    """The adopted coordinator's ownership registry, or None on the legacy path.

    An adopted vault holds maintenance owners in the coordinator-v3 schema
    (role/scope/actor_id, no `owner_name`), and the product's own path to that
    schema is `operational_ownership.OwnershipRegistry` — the same registry the
    capture worker and the project store use. Speaking raw legacy SQL at that
    database is what NEW-109 was: `no such column: owner_name` on every
    nightly generation refresh of an adopted vault.
    """
    if getattr(coordinator, "_database_contract", None) is None:
        return None
    from operational_ownership import OwnershipRegistry

    return OwnershipRegistry.from_adopted_database(
        Path(coordinator.state_root), coordinator.database_path
    )


def _v3_maintenance_actor() -> str:
    """The fence's own actor identity, distinct from the plain user identity.

    `maintenance_owners.actor_id` is UNIQUE across the whole v3 table — one
    live lease per actor. The guarded refresh itself still has to enter the
    Markdown writer gate when it publishes a generation, and that gate
    acquires role `markdown-writer` under the plain `current_actor_identity()`.
    Measured on an adopted vault on 2026-08-27: holding the fence under the
    plain identity made the pass's own publication fail with
    `owner_identity_conflict`. The maintenance pass is a distinct agent of the
    same user, and naming it so is what lets its own guarded work write.
    """
    from operational_ownership import current_actor_identity

    return f"{current_actor_identity()}#doctor-maintenance"


def _acquired_v3_maintenance(
    coordinator: Any, registry: Any
) -> tuple[Any, dict[str, object]] | None:
    from operational_ownership import OperationalOwnershipError

    try:
        owner = registry.acquire(
            V3_MAINTENANCE_ROLE,
            scope=V3_MAINTENANCE_SCOPE,
            actor_id=_v3_maintenance_actor(),
        )
    except OperationalOwnershipError:
        # owner_busy, a live runtime-deletion check, or fail-closed liveness
        # doubt: every refusal means "not ours now", which is the deferred
        # `maintenance_owner_busy` outcome, never a traceback.
        return None
    lease: dict[str, object] = {
        "token": owner.token,
        "epoch": owner.epoch,
        "registry": registry,
        "owner": owner,
    }
    return coordinator, lease


def _read_v3_owner_row(registry: Any) -> sqlite3.Row | None:
    with closing(registry.connect()) as database:
        return database.execute(
            "SELECT * FROM maintenance_owners WHERE role=? AND scope=?",
            (V3_MAINTENANCE_ROLE, V3_MAINTENANCE_SCOPE),
        ).fetchone()


def _v3_fence_lost(where: str, lease: dict[str, object]) -> MaintenanceFenceLost:
    observed = _observed_owner_row(_read_v3_owner_row(lease["registry"]))
    observed["held_epoch"] = lease.get("epoch")
    observed["held_by_process"] = os.getpid()
    return MaintenanceFenceLost(where, observed)


def _heartbeat_v3_maintenance(lease: dict[str, object]) -> None:
    from operational_ownership import OperationalOwnershipError

    registry = lease["registry"]
    try:
        lease["owner"] = registry.heartbeat(lease["owner"])
    except OperationalOwnershipError as exc:
        raise _v3_fence_lost("heartbeat", lease) from exc


def _require_v3_maintenance(lease: dict[str, object]) -> None:
    from operational_ownership import OperationalOwnershipError

    registry = lease["registry"]
    with closing(registry.connect()) as database:
        try:
            registry.require(database, lease["owner"])
        except OperationalOwnershipError as exc:
            raise _v3_fence_lost("require", lease) from exc


def _release_v3_maintenance(lease: dict[str, object]) -> None:
    from operational_ownership import OperationalOwnershipError

    try:
        lease["registry"].release(lease["owner"])
    except OperationalOwnershipError as exc:
        raise _v3_fence_lost("release", lease) from exc


def _maintenance_holder(root: Path, state_root: Path) -> dict[str, object]:
    """Who holds the maintenance fence, so a deferred caller knows whether to wait (#29.6)."""
    from markdown_transaction import active_or_legacy_coordinator

    try:
        coordinator = active_or_legacy_coordinator(root, state_root)
        registry = maintenance_ownership_registry(coordinator)
        if registry is not None:
            return _observed_owner_row(_read_v3_owner_row(registry))
        with coordinator._connect() as database:  # noqa: SLF001
            return _observed_owner_row(_read_owner_row(database))
    except (OSError, sqlite3.Error, ValueError, AttributeError):
        return {"present": None}


def acquire_maintenance_owner(
    root: Path, state_root: Path, now: datetime
) -> tuple[Any, dict[str, object]] | None:
    from markdown_transaction import active_or_legacy_coordinator

    coordinator = active_or_legacy_coordinator(root, state_root)
    registry = maintenance_ownership_registry(coordinator)
    if registry is not None:
        return _acquired_v3_maintenance(coordinator, registry)
    return _acquired_legacy_maintenance(coordinator, now)


def _next_doctor_fencing_epoch(row: Any, now: datetime) -> int | None:
    if row is None:
        return 1
    epoch = int(row["fencing_epoch"] or 0) + 1
    if live_owner(row, now, pid_column="process_id"):
        return None
    return epoch


def _acquired_legacy_maintenance(
    coordinator: Any, now: datetime
) -> tuple[Any, dict[str, object]] | None:
    token = secrets.token_hex(16)
    expires = now + timedelta(seconds=MAINTENANCE_LEASE_SECONDS)
    with coordinator._connect() as database:
        database.execute("BEGIN IMMEDIATE")
        _ensure_maintenance_schema(database)
        row = database.execute(
            "SELECT * FROM maintenance_owners WHERE owner_name='doctor'"
        ).fetchone()
        epoch = _next_doctor_fencing_epoch(row, now)
        if epoch is None:
            database.rollback()
            return None
        database.execute(
            """INSERT INTO maintenance_owners(
                   owner_name,owner_token,process_id,acquired_at,heartbeat_at,
                   expires_at,fencing_epoch
               ) VALUES('doctor',?,?,?,?,?,?)
               ON CONFLICT(owner_name) DO UPDATE SET
                   owner_token=excluded.owner_token,
                   process_id=excluded.process_id,
                   acquired_at=excluded.acquired_at,
                   heartbeat_at=excluded.heartbeat_at,
                   expires_at=excluded.expires_at,
                   fencing_epoch=excluded.fencing_epoch""",
            (
                token,
                os.getpid(),
                utc_text(now),
                utc_text(now),
                utc_text(expires),
                epoch,
            ),
        )
        database.commit()
    return coordinator, {"token": token, "epoch": epoch}


def heartbeat_maintenance_owner(
    coordinator: Any, lease: dict[str, object], now: datetime | None = None
) -> None:
    if "owner" in lease:
        _heartbeat_v3_maintenance(lease)
        return
    heartbeat = as_utc(now)
    expires = heartbeat + timedelta(seconds=MAINTENANCE_LEASE_SECONDS)
    with coordinator._connect() as database:
        database.execute("BEGIN IMMEDIATE")
        changed = database.execute(
            """UPDATE maintenance_owners SET heartbeat_at=?,expires_at=?
               WHERE owner_name='doctor' AND owner_token=? AND fencing_epoch=?""",
            (
                utc_text(heartbeat),
                utc_text(expires),
                lease["token"],
                lease["epoch"],
            ),
        ).rowcount
        if changed != 1:
            lost = _fence_lost("heartbeat", database, lease)
            database.rollback()
            raise lost
        database.commit()


def _owner_row_is_ours(row: sqlite3.Row | None, lease: dict[str, object]) -> bool:
    if row is None:
        return False
    if row["owner_token"] != lease["token"] or row["fencing_epoch"] != lease["epoch"]:
        return False
    return row["process_id"] == os.getpid()


def require_maintenance_owner(coordinator: Any, lease: dict[str, object]) -> None:
    if "owner" in lease:
        _require_v3_maintenance(lease)
        return
    with coordinator._connect() as database:
        row = _read_owner_row(database)
        if _owner_row_is_ours(row, lease):
            return
        raise _fence_lost("require", database, lease)


def release_maintenance_owner(coordinator: Any, lease: dict[str, object]) -> None:
    if "owner" in lease:
        _release_v3_maintenance(lease)
        return
    with coordinator._connect() as database:
        database.execute("BEGIN IMMEDIATE")
        released_at = datetime.min.replace(tzinfo=UTC).isoformat()
        changed = database.execute(
            """UPDATE maintenance_owners
               SET owner_token='',process_id=0,heartbeat_at=?,expires_at=?
               WHERE owner_name='doctor' AND owner_token=? AND fencing_epoch=?""",
            (
                released_at,
                released_at,
                lease["token"],
                lease["epoch"],
            ),
        ).rowcount
        if changed != 1:
            lost = _fence_lost("release", database, lease)
            database.rollback()
            raise lost
        database.commit()


def _heartbeat_interval(lease: dict[str, object]) -> float:
    """The renewal pace the held lease was granted, not a constant of another schema.

    A registry lease carries its own timing: `repair` is 120 s renewed every 40,
    `doctor` is 30 s renewed every 10. Renewing a `doctor` lease every 40 s found
    it expired at the first beat and cancelled every fenced build at 44 s, each
    night from 2026-09-13. Research:
    `docs/research/2026-09-14-a-heartbeat-beats-at-the-pace-of-its-own-lease.md`.
    """
    owner = lease.get("owner")
    if owner is None:
        return MAINTENANCE_HEARTBEAT_SECONDS
    return float(owner.heartbeat_seconds)


def lease_seconds(lease: dict[str, object]) -> float:
    """How long the held lease lasts from a renewal: its own TTL, or the legacy row's."""
    owner = lease.get("owner")
    if owner is None:
        return float(MAINTENANCE_LEASE_SECONDS)
    return float(owner.ttl_seconds)


def transient_beat_failure(error: BaseException) -> bool:
    """Retry confirmed SQLite contention, preserving other failure causes."""
    from llm_wiki.sqlite_deadlines import busy_database

    return busy_database(error)


class MaintenanceHeartbeat:
    """Keep one fenced maintenance owner live through cancellable repair work."""

    def __init__(
        self,
        coordinator: Any,
        lease: dict[str, object],
        *,
        deadline: float,
    ) -> None:
        self.coordinator = coordinator
        self.lease = lease
        self.deadline = deadline
        self.interval = _heartbeat_interval(lease)
        # The owner row is already written; its expiry is counted from here.
        self._held_since = time.monotonic()
        self._stop = threading.Event()
        self._lost = threading.Event()
        self._thread: threading.Thread | None = None

    def __enter__(self) -> MaintenanceHeartbeat:
        self.check()
        self._thread = threading.Thread(
            target=self._heartbeat_loop,
            name="llm-wiki-doctor-heartbeat",
            daemon=True,
        )
        self._thread.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=max(1.0, self.interval * 2))
        release_maintenance_owner(self.coordinator, self.lease)

    def _heartbeat_loop(self) -> None:
        """Renew on the lease's pace; a busy database is retried until the lease expires.

        Counting two misses let a 30-second lease expire behind one slow failure.
        See `docs/research/2026-09-14-a-busy-database-is-not-a-lost-lease.md`.
        """
        from lease_renewal import renew_until_stopped
        from llm_wiki.reliability_defaults import DEFAULTS

        ended = renew_until_stopped(
            self._renew,
            interval=self.interval,
            lease_seconds=lease_seconds(self.lease),
            attempt_seconds=DEFAULTS.markdown_busy_ms / 1_000,
            held_since=self._held_since,
            stop=self._stop,
            transient=transient_beat_failure,
        )
        if ended is not None:
            self._lost.set()

    def _renew(self) -> None:
        heartbeat_maintenance_owner(self.coordinator, self.lease)

    def cancelled(self) -> bool:
        return self._lost.is_set() or monotonic_deadline.deadline_reached(self.deadline)

    def check(self) -> None:
        if self.cancelled():
            raise TimeoutError("maintenance deadline or heartbeat fence reached")
        require_maintenance_owner(self.coordinator, self.lease)

    def run(self, operation, /, *args, **kwargs):
        self.check()
        result = operation(*args, **kwargs)
        self.check()
        return result

    def cleanup(self, operation, /, *args, **kwargs):
        """Release first, report the fence after.

        Cleanup is the releasing half of a repair — today only the index lock,
        whose release is token-checked and so can never touch another owner's
        lock. Requiring the fence first meant a fence lost mid-rebuild left our
        own lock file on disk until its staleness rule expired it.
        """
        result = operation(*args, **kwargs)
        require_maintenance_owner(self.coordinator, self.lease)
        return result


def _generation_cache_absent(state_root: Path, graph_root: Path) -> bool:
    if safe_kind(graph_root / "catalog.sqlite3", state_root)[0] != "missing":
        return False
    return safe_kind(graph_root / "generations", state_root)[0] == "missing"


def _active_pointer_value(catalog: object) -> str | None:
    with closing(
        catalog._readonly()  # noqa: SLF001 - repair needs pointer comparison
    ) as database:
        return active_pointer(database)


def _record_generation_recovery(
    catalog: object, deadline: float, repaired: list[dict], active_before: str | None
) -> None:
    recovered = catalog.recover_orphans(deadline=deadline)
    if recovered:
        repaired.append(
            {"action": "recover_generation_orphans", "count": len(recovered)}
        )
    active_manifest = catalog.get_active(deadline=deadline)
    active_after = _active_generation_id(active_manifest)
    if active_after != active_before:
        repaired.append(
            {
                "action": "fallback_generation",
                "from": active_before,
                "to": active_after,
            }
        )


def _cleanup_stop_reached(deadline: float, cancelled) -> bool:
    return bool(cancelled and cancelled()) or monotonic_deadline.deadline_reached(deadline)


def _skip_generation_child(entry: os.DirEntry, registered: set[str]) -> bool:
    if entry.name in registered or not entry.is_dir(follow_symlinks=False):
        return True
    return _written_within_grace(Path(entry.path))


def _written_within_grace(path: Path) -> bool:
    """Whether a build could still be writing here; an unreadable directory is kept."""
    from llm_wiki.generation_lineage import untouched_for

    return not untouched_for(path, GENERATION_ORPHAN_GRACE_SECONDS)


def _removable_generation_orphan(
    path: Path,
    entry_name: str,
    state_root: Path,
    catalog: object,
    deadline: float,
    cancelled,
) -> bool:
    """True only for an unregistered child that fails validation in place."""
    from llm_wiki import generation_descriptors, generation_fields, generation_validation

    try:
        generation_fields.checked_generation_id(entry_name)
        if generation_descriptors.is_link_or_reparse(path):
            return False
        generation_validation.validate_generation(
            path,
            state_root,
            deadline=deadline,
            cancelled=cancelled,
        )
    except TimeoutError:
        raise
    except (FileNotFoundError, OSError, PermissionError, TypeError, ValueError):
        parent = path.parent.resolve(strict=True)
        return parent == catalog.generations_path.resolve(strict=True)
    return False


def _remove_generation_orphan(
    entry: os.DirEntry,
    registered: set[str],
    state_root: Path,
    catalog: object,
    deadline: float,
    cancelled,
) -> int:
    """Remove one unregistered generation child; return how many were removed."""
    if _skip_generation_child(entry, registered):
        return 0
    path = Path(entry.path)
    if not _removable_generation_orphan(
        path,
        entry.name,
        state_root,
        catalog,
        deadline,
        cancelled,
    ):
        return 0
    shutil.rmtree(path)
    return 1


def _cleanup_generation_orphans(
    catalog: object,
    state_root: Path,
    registered: set[str],
    deadline: float,
    cancelled,
) -> int:
    from llm_wiki import generation_limits, generation_seal

    children = generation_seal.bounded_scandir(
        catalog.generations_path,
        generation_limits.MAX_GENERATION_CHILDREN,
        "generation child count exceeds cleanup bound",
        deadline=deadline,
        cancelled=cancelled,
    )
    removed = 0
    for entry in children:
        if _cleanup_stop_reached(deadline, cancelled):
            raise TimeoutError("generation cleanup deadline reached")
        removed += _remove_generation_orphan(
            entry,
            registered,
            state_root,
            catalog,
            deadline,
            cancelled,
        )
    return removed


def repair_generation_catalog(
    root: Path,
    state_root: Path,
    *,
    deadline: float,
    cancelled,
    repaired: list[dict],
) -> None:
    """Recover valid generations and remove only invalid unregistered partials."""
    del root
    from llm_wiki import generation_catalog_store

    graph_root = state_root / "cache" / "evidence-graph"
    if _generation_cache_absent(state_root, graph_root):
        return
    catalog = generation_catalog_store.GenerationCatalog(state_root)
    active_before = _active_pointer_value(catalog)
    _record_generation_recovery(catalog, deadline, repaired, active_before)
    registered = set(catalog.registered_generation_ids(
        deadline=deadline, cancelled=cancelled
    ))
    removed = _cleanup_generation_orphans(
        catalog, state_root, registered, deadline, cancelled
    )
    if removed:
        repaired.append({"action": "cleanup_generation_orphans", "count": removed})


class _PartitionState(NamedTuple):
    grouped: dict
    occurrence_sources: dict
    record_sources: dict
    node_references: dict
    dependencies: dict
    workspace_sensitive: set


def _new_partition_state(source_ids: tuple[str, ...]) -> _PartitionState:
    grouped = {
        source_id: {
            "nodes": [],
            "occurrences": [],
            "assertions": [],
            "evidence": [],
            "observations": [],
            "dependencies": [],
        }
        for source_id in source_ids
    }
    return _PartitionState(
        grouped=grouped,
        occurrence_sources={},
        record_sources={},
        node_references={},
        dependencies={source_id: set() for source_id in source_ids},
        workspace_sensitive=set(),
    )


def _record_owner(state: _PartitionState, record_id: object, source_id: str) -> None:
    if record_id is None:
        return
    state.record_sources[str(record_id)] = source_id


def _reference_node(state: _PartitionState, node_id: object, owner: str) -> None:
    if node_id is None:
        return
    state.node_references.setdefault(str(node_id), set()).add(owner)


def _partition_occurrences(result, state: _PartitionState, check_stop) -> None:
    for occurrence in result.occurrences:
        check_stop()
        source_id = str(occurrence["source_id"])
        node_id = str(occurrence["node_id"])
        state.occurrence_sources.setdefault(node_id, set()).add(source_id)
        state.node_references.setdefault(node_id, set()).add(source_id)
        state.grouped[source_id]["occurrences"].append(occurrence)


def _partition_evidence(result, state: _PartitionState, check_stop) -> None:
    for evidence in result.evidence:
        check_stop()
        source_id = str(evidence["source_id"])
        state.grouped[source_id]["evidence"].append(evidence)
        _record_owner(state, evidence.get("assertion_id"), source_id)
        _record_owner(state, evidence.get("observation_id"), source_id)


def _partition_assertions(result, state: _PartitionState, check_stop) -> None:
    for assertion in result.assertions:
        check_stop()
        owner = state.record_sources[str(assertion["assertion_id"])]
        state.grouped[owner]["assertions"].append(assertion)
        _reference_node(state, assertion["source_node_id"], owner)
        target = assertion.get("target_node_id")
        if target is None:
            continue
        _reference_node(state, target, owner)
        state.dependencies[owner].update(
            state.occurrence_sources.get(str(target), ())
        )


def _workspace_sensitive_observation(
    observation: dict, observation_dependencies: dict
) -> bool:
    if observation["reason"] not in {"missing_dependency", "unresolved_reference"}:
        return False
    return str(observation["observation_id"]) not in observation_dependencies


def _partition_observations(result, state: _PartitionState, check_stop) -> None:
    observation_dependencies = getattr(result, "observation_source_dependencies", {})
    for observation in result.observations:
        check_stop()
        owner = state.record_sources[str(observation["observation_id"])]
        state.grouped[owner]["observations"].append(observation)
        _reference_node(state, observation.get("source_node_id"), owner)
        if _workspace_sensitive_observation(observation, observation_dependencies):
            state.workspace_sensitive.add(owner)


def _partition_source_dependencies(result, state: _PartitionState, check_stop) -> None:
    declared = getattr(result, "observation_source_dependencies", {})
    for observation_id, candidate_sources in declared.items():
        check_stop()
        owner = state.record_sources[str(observation_id)]
        state.dependencies[owner].update(candidate_sources)


def _dependency_owners(dependency: dict, state: _PartitionState) -> tuple[str, ...]:
    owner = dependency.get("source_id")
    if owner is not None:
        return (str(owner),)
    node_id = str(dependency["dependent_node_id"])
    return tuple(sorted(state.occurrence_sources.get(node_id, ())))


def _partition_dependencies(result, state: _PartitionState, check_stop) -> None:
    for dependency in getattr(result, "dependencies", ()):
        check_stop()
        for source_id in _dependency_owners(dependency, state):
            state.grouped[source_id]["dependencies"].append(dependency)


def _partition_nodes(
    result, state: _PartitionState, check_stop, fallback_owner: str
) -> None:
    for node in result.nodes:
        check_stop()
        node_id = str(node["node_id"])
        owners = state.occurrence_sources.get(node_id)
        if not owners:
            owners = state.node_references.get(node_id, {fallback_owner})
        for source_id in sorted(owners):
            state.grouped[source_id]["nodes"].append(node)


def _drop_self_dependencies(
    source_ids: tuple[str, ...], state: _PartitionState, check_stop
) -> None:
    for source_id in source_ids:
        check_stop()
        state.dependencies[source_id].discard(source_id)


def _source_partitions(
    source_ids: tuple[str, ...],
    state: _PartitionState,
    check_stop,
    source_extraction,
) -> dict:
    partitions = {}
    for source_id in source_ids:
        check_stop()
        records = state.grouped[source_id]
        partitions[source_id] = source_extraction(
            nodes=tuple(records["nodes"]),
            occurrences=tuple(records["occurrences"]),
            assertions=tuple(records["assertions"]),
            evidence=tuple(records["evidence"]),
            observations=tuple(records["observations"]),
            dependencies=tuple(records["dependencies"]),
            source_dependencies=tuple(sorted(state.dependencies[source_id])),
            workspace_sensitive=source_id in state.workspace_sensitive,
        )
    return partitions


def partition_code_extraction(
    result,
    code_sources,
    *,
    deadline: float | None = None,
    cancelled=None,
):
    """Partition one multi-source extraction by the source proving each record."""
    from evidence_graph_builder import SourceExtraction

    source_ids = tuple(source.record.logical_id for source in code_sources)
    state = _new_partition_state(source_ids)

    def check_stop() -> None:
        if cancelled is not None and cancelled():
            raise TimeoutError("workspace extraction partition cancelled")
        if deadline is not None and time.monotonic() >= deadline:
            raise TimeoutError("workspace extraction partition deadline reached")

    check_stop()
    _partition_occurrences(result, state, check_stop)
    _partition_evidence(result, state, check_stop)
    _partition_assertions(result, state, check_stop)
    _partition_observations(result, state, check_stop)
    _partition_source_dependencies(result, state, check_stop)
    _partition_dependencies(result, state, check_stop)
    _partition_nodes(result, state, check_stop, min(source_ids))
    _drop_self_dependencies(source_ids, state, check_stop)
    return _source_partitions(source_ids, state, check_stop, SourceExtraction)


def is_memory_source(snapshot, source) -> bool:
    """A source the memory walk collected; a code root named `knowledge` holds code.

    See `docs/research/2026-09-17-a-question-is-answered-by-its-own-kind-of-generation.md`.
    """
    from corpus_snapshot import is_memory_path

    return is_memory_path(source.record.relative_path, snapshot.policy.code_roots)


def code_extraction_sources(snapshot):
    return tuple(
        sorted(
            (
                source
                for source in snapshot.sources
                if not is_memory_source(snapshot, source)
            ),
            key=lambda source: (
                source.record.logical_id,
                source.record.relative_path,
                source.record.language or "",
            ),
        )
    )


def knowledge_extraction_sources(snapshot):
    return tuple(
        source
        for source in snapshot.sources
        if is_memory_source(snapshot, source)
        and not source.record.relative_path.startswith("knowledge/projects/")
    )


class _SourceExtractionAdapter:
    """The incremental builder adapter for one immutable snapshot.

    A class rather than a closure so each branch of the decision is its own
    named step and the two memoized partitions are ordinary attributes.

    `_code_result` batches `extract_code` over every code source in the
    snapshot rather than over the rebuild set, and that is deliberate rather
    than an oversight. `Collector.extract` is two passes: the definitions pass
    writes every shared resolution index, so it must see the whole universe,
    and the edges pass only reads them, so it is safely per-source. Splitting
    them was built and measured. It is correct -- a whole incremental
    generation built that way is identical to a full build across all seven
    tables of `evidence.sqlite3`, an added source included -- and it does not
    pay: the definitions pass is 43% of the extractor and cannot be narrowed,
    so the split is worth 3.0 CPU s of a ~150 s pass when the rebuild set is
    today's 427 of 678, and 9.8 s only when it is 1. End to end that measured
    +0.98 s and -4.30 s respectively, and the sibling note measured narrow
    rebuild sets arriving on 6.2% of code-touching commits, so the expectation
    is a net loss of about 0.65 s a pass.

    One construction makes a partial batch answer differently, and it is the
    reason any revival needs the note rather than this paragraph:
    `_partition_nodes` sends a node with no occurrence and no reference to
    `min(source_ids)`, and "referenced by nobody" is global knowledge a partial
    run does not have. A zero-byte source contributes exactly such a node.

    The knowledge half has no such defect to fix: a knowledge-page edit puts no
    code source in the rebuild set, so `extract_code` is never called at all,
    and `extract_knowledge` costs 0.07 CPU s for 107 sources.

    `tests/test_extraction_universe.py` pins the structural facts this rests
    on. See
    `docs/research/2026-08-29-what-a-partial-extraction-can-get-wrong.md`.
    """

    def __init__(self, snapshot, repository_id: str) -> None:
        self.by_id = {source.record.logical_id: source for source in snapshot.sources}
        self.memory_ids = frozenset(
            source.record.logical_id
            for source in snapshot.sources
            if is_memory_source(snapshot, source)
        )
        self.code_sources = code_extraction_sources(snapshot)
        self.knowledge_sources = knowledge_extraction_sources(snapshot)
        self.repository_id = repository_id
        self.code_partitions = None
        self.knowledge_partitions = None

    def __call__(self, source, content, *, sources, source_bytes, deadline, cancelled):
        from evidence_graph_builder import SourceExtraction

        del sources
        captured = self.by_id[str(source["source_id"])]
        if captured.content != content:
            raise ValueError("incremental extraction bytes differ from snapshot")
        result = self._result_for(captured, source_bytes, deadline, cancelled)
        return source_extraction(SourceExtraction, result, content)

    def _result_for(self, captured, source_bytes, deadline, cancelled):
        if captured.record.logical_id not in self.memory_ids:
            return self._code_result(captured, source_bytes, deadline, cancelled)
        if captured.record.relative_path.startswith("knowledge/projects/"):
            return _project_extraction(captured, deadline, cancelled)
        return self._knowledge_result(captured, source_bytes, deadline, cancelled)

    def _knowledge_result(self, captured, source_bytes, deadline, cancelled):
        self.knowledge_partitions = _memoized(
            self.knowledge_partitions,
            lambda: _knowledge_partitions(
                self.knowledge_sources, source_bytes, deadline, cancelled
            ),
        )
        return self.knowledge_partitions[captured.record.logical_id]

    def _code_result(self, captured, source_bytes, deadline, cancelled):
        self.code_partitions = _memoized(
            self.code_partitions,
            lambda: _code_partitions(
                self.code_sources, self.repository_id, source_bytes, deadline, cancelled
            ),
        )
        return self.code_partitions[captured.record.logical_id]


def generation_source_extractor(snapshot, repository_id: str):
    """Return the incremental builder adapter for one immutable snapshot."""
    return _SourceExtractionAdapter(snapshot, repository_id)


def _memoized(value, build):
    """Compute once. An empty mapping is a real answer, so `or` will not do."""
    return build() if value is None else value


def _require_snapshot_bytes(items, source_bytes: dict, label: str) -> None:
    if any(item.content != source_bytes[item.record.logical_id] for item in items):
        raise ValueError(f"{label} extraction bytes differ from snapshot")


def _project_extraction(captured, deadline, cancelled):
    from project_extractor import extract_projects

    return extract_projects((captured,), deadline=deadline, cancelled=cancelled)


def _knowledge_partitions(knowledge_sources, source_bytes, deadline, cancelled):
    from knowledge_extractor import extract_knowledge

    _require_snapshot_bytes(knowledge_sources, source_bytes, "knowledge")
    workspace_result = extract_knowledge(
        knowledge_sources, deadline=deadline, cancelled=cancelled
    )
    return partition_code_extraction(
        workspace_result, knowledge_sources, deadline=deadline, cancelled=cancelled
    )


def _code_partitions(code_sources, repository_id, source_bytes, deadline, cancelled):
    from code_extractor import extract_code

    _require_snapshot_bytes(code_sources, source_bytes, "workspace")
    workspace_result = extract_code(
        code_sources,
        repository_id=repository_id,
        deadline=deadline,
        cancelled=cancelled,
    )
    return partition_code_extraction(
        workspace_result, code_sources, deadline=deadline, cancelled=cancelled
    )


def source_extraction(source_extraction_class, result, content: bytes):
    """One source's records, plus the metadata `evidence_graph_builder_semantic_changes` reads.

    All five fingerprints are one value under five names -- the content digest --
    so every byte-level change counts as a semantic one. That is deliberate,
    not an accident of five suggestive key names: it over-invalidates and can
    never under-invalidate, which is the safe direction.

    Only `exports` could carry a real definition. A source is legible to another
    source solely through the definitions it contributes to `code_extractor`'s
    shared `definitions`/`python_scopes`/`modules` indexes; `imports` and
    `aliases` are a per-source local table that enters no shared index,
    `signatures` already sit inside the export identity key, and a project
    journal is extracted alone. Building it was measured and does not pay: the
    rebuild set for a code edit falls from 423 sources to 1 and the pass gets no
    faster, because `_SourceExtractionAdapter._code_result` batches
    `extract_code` over every code source as soon as one is rebuilt -- and the
    edited source is always one. See
    `docs/research/2026-08-29-what-an-invalidation-fingerprint-can-mean.md`
    and `tests/test_invalidation_fingerprints.py`.
    """
    digest = hashlib.sha256(content).hexdigest()
    fingerprints = {
        key: hashlib.sha256(f"{key}:{digest}".encode("ascii")).hexdigest()
        for key in (
            "exports",
            "imports",
            "signatures",
            "aliases",
            "project_metadata",
        )
    }
    return source_extraction_class(
        nodes=tuple(result.nodes),
        occurrences=tuple(result.occurrences),
        assertions=tuple(result.assertions),
        evidence=tuple(result.evidence),
        observations=tuple(result.observations),
        dependencies=tuple(getattr(result, "dependencies", ())),
        source_dependencies=tuple(getattr(result, "source_dependencies", ())),
        workspace_sensitive=bool(getattr(result, "workspace_sensitive", False)),
        invalidation_fingerprints=fingerprints,
    )


def corpus_policy(snapshot: object) -> dict:
    return {
        "daily_paths": list(snapshot.policy.daily_paths),
        "code_roots": list(snapshot.policy.code_roots),
        "include_historical": snapshot.policy.include_historical,
        "as_of": snapshot.policy.as_of,
    }


def workspace_manifest_sha256(snapshot: object) -> str:
    """Identity of the non-knowledge sources, which decides graph reuse."""
    from llm_wiki.formats.canonical import canonical_json_bytes

    membership = sorted(
        [
            source.record.logical_id,
            source.record.relative_path,
            source.record.language,
        ]
        for source in snapshot.sources
        if not is_memory_source(snapshot, source)
    )
    return hashlib.sha256(canonical_json_bytes(membership)).hexdigest()


def _parent_workspace_manifest(
    catalog: object, parent_id: str | None, deadline: float, cancelled
) -> str | None:
    if parent_id is None:
        return None
    from evidence_graph_builder import load_incremental_manifest

    parent_incremental, _parent_generation = load_incremental_manifest(
        catalog,
        parent_id,
        deadline=deadline,
        cancelled=cancelled,
    )
    if parent_incremental is None:
        return None
    return parent_incremental["reuse_config"].get("workspace_manifest_sha256")


def _parent_matches_versions(
    parent: dict, snapshot: object, extractor_version: str
) -> bool:
    """Every version the active generation records still matches the live one."""
    return (
        parent.get("collector_version") == snapshot.collector_version
        and parent.get("extractor_version") == snapshot.extractor_version
        and parent.get("graph_extractor_version") == extractor_version
    )


def parent_matches_identity(
    parent: dict, repository_scope: object, snapshot: object, extractor_version: str
) -> bool:
    """Whether the active generation belongs to this checkout and this toolchain.

    Identity, not equality -- the same distinction `scope_state` already draws
    above. A scope record carries `git_commit`, so comparing the whole record
    made this gate false after every commit on a vault that commits its own
    runtime, and the idle maintenance pass rebuilt the whole generation instead
    of returning `current`. Whether the generation is *stale* is a different
    question, asked by the source manifest and workspace digests in
    `_parent_describes_snapshot`. See NEW-138.
    """
    from repository_scope import same_repository_record

    return (
        parent.get("schema_version") == "corpus-generation/v2"
        and same_repository_record(
            parent.get("repository_scope"), repository_scope.as_dict()
        )
        and _parent_matches_versions(parent, snapshot, extractor_version)
    )


def _parent_describes_snapshot(
    parent: dict,
    repository_scope: object,
    snapshot: object,
    extractor_version: str,
    parent_workspace_sha256: str | None,
    workspace_sha256: str,
) -> bool:
    """The identity, the source manifest and the workspace all still agree."""
    return (
        parent_matches_identity(
            parent, repository_scope, snapshot, extractor_version
        )
        and parent.get("source_manifest_sha256") == snapshot.corpus_sha256
        and parent_workspace_sha256 == workspace_sha256
    )


def _parent_is_current(
    parent: dict | None,
    force_rebuild: bool,
    repository_scope: object,
    snapshot: object,
    extractor_version: str,
    parent_workspace_sha256: str | None,
    workspace_sha256: str,
) -> bool:
    """True when the active generation already describes the live sources."""
    if parent is None or force_rebuild:
        return False
    return _parent_describes_snapshot(
        parent,
        repository_scope,
        snapshot,
        extractor_version,
        parent_workspace_sha256,
        workspace_sha256,
    )


def generation_source_rows(snapshot: object) -> list[dict]:
    return [
        {
            "source_id": source.record.logical_id,
            "relative_path": source.record.relative_path,
            "sha256": source.record.sha256,
            "size": source.record.size,
            "media_type": source.record.media_type,
            "language": source.record.language,
            "git_oid": source.record.git_oid,
        }
        for source in snapshot.sources
    ]


def generation_source_bytes(snapshot: object) -> dict:
    return {source.record.logical_id: source.content for source in snapshot.sources}


def _active_generation_id(parent: dict | None) -> str | None:
    if parent is None:
        return None
    return str(parent["generation_id"])


def _reuse_parent_id(force_rebuild: bool, parent_id: str | None) -> str | None:
    if force_rebuild:
        return None
    return parent_id


def fresh_generation_id(catalog: object) -> str:
    while True:
        generation_id = f"generation-{time.time_ns():x}-{secrets.token_hex(4)}"
        if not (catalog.generations_path / generation_id).exists():
            return generation_id


def _generation_build_result(built: object, snapshot: object) -> dict:
    if not built.activated:
        return {
            "status": "deferred",
            "generation_id": built.generation_id,
            "sources": len(snapshot.sources),
            "partial": True,
            "reason": "activation_race",
        }
    return {
        "status": "built",
        "generation_id": built.generation_id,
        "sources": len(snapshot.sources),
        "rebuilt_sources": len(built.rebuilt_sources),
        "reused_sources": len(built.reused_sources),
        "partial": False,
    }


def build_or_refresh_generation(
    root: Path,
    state_root: Path,
    *,
    deadline: float,
    cancelled,
    max_sources: int,
    force_rebuild: bool,
    coordinator: object | None = None,
    code_roots: tuple[str, ...] | None = None,
    phases: dict[str, float] | None = None,
) -> dict:
    from corpus_snapshot import VAULT_CODE_ROOTS, collect_corpus
    from evidence_graph_builder import (
        GRAPH_SCHEMA_VERSION,
        IncrementalReuseConfig,
        build_incremental_generation,
    )
    from llm_wiki.generation_catalog_store import GenerationCatalog
    from repository_scope import resolve_repository_scope

    with _timed_phase(phases, "scope"):
        repository_scope = resolve_repository_scope(
            root, deadline=deadline, cancelled=cancelled
        )
    extractor_version = maintenance_extractor_identity()
    with _timed_phase(phases, "snapshot"):
        snapshot = collect_corpus(
            root,
            code_roots=VAULT_CODE_ROOTS if code_roots is None else code_roots,
            max_files=max_sources,
            deadline=deadline,
            coordinator=coordinator,
            cancelled=cancelled,
        )
    if len(snapshot.sources) > max_sources:
        raise ValueError("corpus source limit exceeded")

    catalog = GenerationCatalog(state_root)
    with _timed_phase(phases, "parent"):
        parent = catalog.get_active(deadline=deadline)
        parent_id = _active_generation_id(parent)
        workspace_sha256 = workspace_manifest_sha256(snapshot)
        parent_workspace_sha256 = _parent_workspace_manifest(
            catalog, parent_id, deadline, cancelled
        )
    if _parent_is_current(
        parent,
        force_rebuild,
        repository_scope,
        snapshot,
        extractor_version,
        parent_workspace_sha256,
        workspace_sha256,
    ):
        return {
            "status": "current",
            "generation_id": parent_id,
            "sources": len(snapshot.sources),
            "partial": False,
        }

    config = IncrementalReuseConfig(
        extractor_version=extractor_version,
        grammar_version="builtin-grammars/v1",
        compiler_version=f"python-{sys.version_info.major}.{sys.version_info.minor}",
        resolver_config_sha256=hashlib.sha256(
            b"llm-wiki-maintenance-resolver/v1"
        ).hexdigest(),
        schema_version=GRAPH_SCHEMA_VERSION,
        workspace_manifest_sha256=workspace_sha256,
    )
    with _timed_phase(phases, "build"):
        built = build_incremental_generation(
            catalog,
            sources=generation_source_rows(snapshot),
            source_bytes=generation_source_bytes(snapshot),
            extractor=generation_source_extractor(
                snapshot, repository_scope.repository_id
            ),
            reuse_config=config,
            generation_id=fresh_generation_id(catalog),
            parent_generation_id=_reuse_parent_id(force_rebuild, parent_id),
            policy=corpus_policy(snapshot),
            expected_active=parent_id,
            deadline=deadline,
            cancelled=cancelled,
            repository_scope=repository_scope,
            snapshot=snapshot,
            publication_root=root,
            coordinator=coordinator,
        )
    return _generation_build_result(built, snapshot)


@contextlib.contextmanager
def _timed_phase(phases: dict[str, float] | None, name: str):
    """Record what this phase cost, so a deferred build can say where its time went.

    A refresh that stopped at its budget used to report `time_limit` and
    nothing else; on a hosted Windows runner a two-file build passed 60 s and
    no one could say in which phase. See
    `docs/research/2026-09-10-a-timeout-is-a-hang-bound-not-a-stopwatch.md`.
    """
    if phases is None:
        yield
        return
    started = time.monotonic()
    try:
        yield
    finally:
        phases[name] = round(time.monotonic() - started, 3)


def _maintenance_outcome(
    status: str,
    reason: str,
    *,
    partial: bool,
    repairs: list[dict] | None = None,
    details: dict[str, object] | None = None,
) -> dict:
    outcome = {
        "status": status,
        "generation_id": None,
        "sources": 0,
        "partial": partial,
        "reason": reason,
    }
    if repairs is not None:
        outcome["repairs"] = repairs
    if details is not None:
        outcome["details"] = details
    return outcome


def fence_loss_details(exc: BaseException) -> dict[str, object] | None:
    """What the lost fence saw, when the raiser bothered to say."""
    if not isinstance(exc, MaintenanceFenceLost):
        return None
    return {"where": exc.where, "observed": exc.observed}


def _positive_finite_number(value: object) -> bool:
    """A real, finite number greater than zero; a bool is not a number here."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    return math.isfinite(value) and value > 0


def _require_positive_time_budget(time_budget_seconds: object) -> None:
    if not _positive_finite_number(time_budget_seconds):
        raise ValueError("time_budget_seconds must be positive and finite")


def unusable_filesystem_outcome(state_path: Path, deadline: float) -> dict | None:
    filesystem = filesystem_check(state_path, deadline)
    if filesystem["status"] != "error":
        return None
    if filesystem["details"].get("budget_exhausted"):
        return _maintenance_outcome("deferred", "time_limit", partial=True)
    return _maintenance_outcome("error", "unsupported_filesystem", partial=False)


# Which bound a bounded refusal actually hit. Collapsing all of them into
# "source_limit" told an operator to shrink a corpus that was not the problem.
_BOUNDED_REASONS = (
    ("incremental manifest", "manifest_byte_ceiling"),
    ("total byte limit", "corpus_byte_limit"),
    ("entry limit", "corpus_entry_limit"),
    ("directory limit", "corpus_directory_limit"),
    ("depth limit", "corpus_depth_limit"),
)


def _bounded_reason(message: str) -> str:
    """The name of the bound this refusal names, or the generic source limit."""
    lowered = message.casefold()
    for fragment, reason in _BOUNDED_REASONS:
        if fragment in lowered:
            return reason
    return "source_limit"


def _value_error_outcome(exc: ValueError, repaired: list[dict]) -> dict:
    message = str(exc)
    lowered = message.casefold()
    if "limit" in lowered or "ceiling" in lowered:
        return _maintenance_outcome(
            "deferred", _bounded_reason(message), partial=True, repairs=repaired
        )
    # `ValueError` alone tells the operator nothing: a refresh that ends this way
    # names only the exception class, and the nightly log then carries a word
    # with no cause behind it. Measured 2026-08-24 on this vault, where a refresh
    # ended `error: ValueError` after 275 seconds and nothing said why.
    return _maintenance_outcome(
        "error",
        type(exc).__name__,
        partial=False,
        repairs=repaired,
        details={"message": message[:400]},
    )


def refreshed_generation(
    root_path: Path,
    state_path: Path,
    coordinator: Any,
    lease: dict[str, object],
    deadline: float,
    max_sources: int,
    force_rebuild: bool,
    repaired: list[dict],
    code_roots: tuple[str, ...] | None = None,
    phases: dict[str, float] | None = None,
) -> dict:
    with MaintenanceHeartbeat(coordinator, lease, deadline=deadline) as guard:
        guard.run(
            repair_generation_catalog,
            root_path,
            state_path,
            deadline=deadline,
            cancelled=guard.cancelled,
            repaired=repaired,
        )
        result = _built_or_recaptured(
            guard,
            root_path,
            state_path,
            deadline=deadline,
            max_sources=max_sources,
            force_rebuild=force_rebuild,
            coordinator=coordinator,
            code_roots=code_roots,
            phases=phases,
        )
        result["repairs"] = repaired
        result["details"] = {"phase_seconds": dict(phases or {})}
        return result


def _built_generation(guard, root_path, state_path, *, force_rebuild, **stop) -> dict:
    return guard.run(
        build_or_refresh_generation,
        root_path,
        state_path,
        cancelled=guard.cancelled,
        force_rebuild=force_rebuild,
        **stop,
    )


def _built_or_recaptured(
    guard, root_path, state_path, *, force_rebuild, **stop
) -> dict:
    """Build once; a vault written to mid-capture is captured again, same fence.

    The recapture used to happen one level up, after the first attempt's guard
    had already exited — and exiting releases the maintenance owner. The retry
    then presented the same, now-released lease and was told the fence was lost.
    On a vault whose capture appends while the corpus is being read, that is the
    ordinary case, not an edge: it defeated three of five rebuilds and the
    nightly pass of 2026-08-24, each time after four minutes of work.
    """
    try:
        return _built_generation(
            guard, root_path, state_path, force_rebuild=force_rebuild, **stop
        )
    except _corpus_changed_error():
        return _built_generation(
            guard, root_path, state_path, force_rebuild=True, **stop
        )


def _corpus_changed_error() -> type[BaseException]:
    """The error a live vault raises when it was written to mid-capture."""
    from corpus_snapshot import CorpusChanged

    return CorpusChanged


def run_generation_maintenance(
    root: Path | str | None = None,
    state_root: Path | str | None = None,
    *,
    time_budget_seconds: float = DEFAULT_GENERATION_TIME_BUDGET_SECONDS,
    max_sources: int | None = None,
    force_rebuild: bool = False,
    code_roots: tuple[str, ...] | None = None,
) -> dict:
    """Run one bounded fenced generation refresh; never mutate knowledge.

    `code_roots` is the generation's policy: None means the vault's own
    generation, which holds memory only (`corpus_snapshot.VAULT_CODE_ROOTS`,
    empty). Naming roots here is a test seam and nothing else: no production
    caller passes them, and the production path for a code generation is
    `repository_index`, which registers it without ever activating it. A
    generation built here with roots *is* activated, so it must not be used to
    build one for a running vault (audit 3, G-L4). `corpus_snapshot` is
    imported where it is used: it needs PyYAML, which the production install
    does not carry.
    """
    _require_positive_time_budget(time_budget_seconds)
    root_path = Path(root or runtime_roots.vault_root()).resolve()
    max_sources = generation_source_limit(max_sources, root_path)
    require_positive_source_limit(max_sources)
    state_path = Path(os.path.abspath(state_root or runtime_roots.state_root_for(root_path)))
    deadline = time.monotonic() + float(time_budget_seconds)
    unusable = unusable_filesystem_outcome(state_path, deadline)
    if unusable is not None:
        return unusable
    acquired = acquire_maintenance_owner(
        root_path, state_path, datetime.now(UTC)
    )
    if acquired is None:
        return _maintenance_outcome(
            "deferred",
            "maintenance_owner_busy",
            partial=True,
            details={"holder": _maintenance_holder(root_path, state_path)},
        )
    coordinator, lease = acquired
    return _guarded_generation_refresh(
        root_path,
        state_path,
        coordinator,
        lease,
        deadline,
        max_sources,
        force_rebuild,
        code_roots=code_roots,
    )


def _fence_lost_outcome(exc: RuntimeError, repaired: list[dict]) -> dict:
    # Another maintenance owner took the fence while this pass was working.
    # Losing that race is an ordinary outcome — the nightly timer and a manual
    # run collide — and belongs in the report, not in a traceback.
    if str(exc) != "maintenance_owner_fence_lost":
        raise exc
    return _maintenance_outcome(
        "deferred",
        "maintenance_owner_lost",
        partial=True,
        repairs=repaired,
        details=fence_loss_details(exc),
    )


def _attempted_generation_refresh(
    root_path,
    state_path,
    coordinator,
    lease,
    deadline,
    max_sources,
    force_rebuild,
    repaired,
    code_roots=None,
) -> dict:
    phases: dict[str, float] = {}
    try:
        return refreshed_generation(
            root_path,
            state_path,
            coordinator,
            lease,
            deadline,
            max_sources,
            force_rebuild,
            repaired,
            code_roots=code_roots,
            phases=phases,
        )
    except TimeoutError:
        return _maintenance_outcome(
            "deferred",
            "time_limit",
            partial=True,
            repairs=repaired,
            details={"phase_seconds": phases},
        )
    except _corpus_changed_error():
        # The vault was written to while its snapshot was being validated. That
        # is the normal state of a vault in use, so it is captured again rather
        # than deferred: deferring would leave an active vault never refreshed.
        # The recapture already happened under this fence; a second change
        # means the vault is being written to faster than it can be read, so
        # the next pass takes it.
        return _maintenance_outcome(
            "deferred", "corpus_changed", partial=True, repairs=repaired
        )
    except RuntimeError as exc:
        return _fence_lost_outcome(exc, repaired)


def _guarded_generation_refresh(
    root_path,
    state_path,
    coordinator,
    lease,
    deadline,
    max_sources,
    force_rebuild,
    code_roots=None,
) -> dict:
    repaired: list[dict] = []
    try:
        return _attempted_generation_refresh(
            root_path,
            state_path,
            coordinator,
            lease,
            deadline,
            max_sources,
            force_rebuild,
            repaired,
            code_roots=code_roots,
        )
    except ValueError as exc:
        return _value_error_outcome(exc, repaired)
    except (OSError, PermissionError, sqlite3.Error) as exc:
        return _maintenance_outcome(
            "error", type(exc).__name__, partial=False, repairs=repaired
        )


def validated_doctor_deadline(
    deadline: float | None, time_budget_seconds: float
) -> float:
    if deadline is None:
        return time.monotonic() + max(0.0, time_budget_seconds)
    if (
        isinstance(deadline, bool)
        or not isinstance(deadline, (int, float))
        or not math.isfinite(deadline)
    ):
        raise ValueError("deadline must be a finite monotonic timestamp")
    return float(deadline)
