"""The generation catalog: registration, activation and retirement."""

from __future__ import annotations

import os
import shutil
import sqlite3
import time
from collections.abc import Callable
from contextlib import closing, contextmanager
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

from llm_wiki import generation_descriptors, generation_limits, runtime_sync, sqlite_io
from llm_wiki.formats.canonical import canonical_json_bytes, sha256_bytes
from llm_wiki.generation_descriptors import open_read_descriptor, stable_descriptor_state
from llm_wiki.generation_fields import (
    check_cancelled,
    check_monotonic_deadline,
    checked_generation_id,
    raise_writer_timeout,
    require_absolute_deadline,
    require_catalog_bound,
    require_inside_state_root,
    utc_timestamp,
    valid_page_metric,
)
from llm_wiki.generation_limits import (
    BUSY_MS,
    CLEANUP_CATALOG_FENCE_SECONDS,
    COMMIT_BUSY_FLOOR_MS,
    MAX_CATALOG_BYTES,
    RETAINED_ANCESTOR_GENERATIONS,
    UNBOUNDED_BUSY_MS,
)
from llm_wiki.generation_lineage import (
    generation_in_use,
    pointer_is,
    pointer_matches,
    record_activation,
    require_activated,
    require_active_root,
    require_retained_ancestors,
    retained_generation_ids,
)
from llm_wiki.generation_registration import (
    candidate_manifest,
    close_descriptors,
    manifest_belongs_to,
    manifest_scope,
    require_fallback_registration,
    require_publication_scope,
    require_registration_match,
    require_registration_token,
    selected_registration_token,
    still_referenced,
    unlink_manifest,
    verified_manifest,
)
from llm_wiki.generation_seal import (
    CANDIDATE_MINT,
    EntrySeal,
    GenerationSealCapability,
    HeldFile,
    ValidatedCandidate,
    append_ancestors,
    append_unseen,
    bounded_scandir,
    entry_seal,
    expected_file_entries,
    metadata_only,
    same_regular_file,
    scan_generation,
    scope_from_manifest_bytes,
)
from llm_wiki.generation_validation import policy_code_roots, validate_generation
from reliable_memory import validate_runtime_file, validate_state_root
from repository_scope import RepositoryScope


class GenerationCatalog:
    """Catalog generic immutable generations and atomically select one active ID."""

    def __init__(
        self,
        state_root: Path,
        *,
        catalog_path: Path | None = None,
        clock: Callable[[], datetime | str] | None = None,
        monotonic: Callable[[], float] | None = None,
        max_catalog_bytes: int = MAX_CATALOG_BYTES,
    ) -> None:
        require_catalog_bound(max_catalog_bytes)
        self.state_root = Path(state_root)
        self.max_catalog_bytes = max_catalog_bytes
        validate_state_root(self.state_root)
        default = self.state_root / "cache" / "evidence-graph" / "catalog.sqlite3"
        self.catalog_path = Path(catalog_path) if catalog_path is not None else default
        require_inside_state_root(
            self.catalog_path.resolve(strict=False), self.state_root.resolve(strict=True)
        )
        self.generations_path = self.catalog_path.parent / "generations"
        self.catalog_path.parent.mkdir(parents=True, exist_ok=True)
        self.generations_path.mkdir(parents=True, exist_ok=True)
        runtime_sync.fsync_directory(self.catalog_path.parent)
        runtime_sync.fsync_directory(self.catalog_path.parent.parent)
        self._clock = clock or (lambda: datetime.now(UTC))
        self._monotonic = monotonic or time.monotonic
        self._candidate_issuer = object()
        # One validation per generation per process, re-checked by its cheap
        # entry seal. See `_validate` below for what that trades away.
        self._validated: dict[str, tuple[tuple[EntrySeal, ...], tuple]] = {}
        self._held_code: dict[str, bool] = {}
        self._read_only = False
        with closing(self._connect()) as database, database:
            self._ensure_schema(database)

    @classmethod
    def open_existing_read_only(
        cls,
        state_root: Path,
        *,
        catalog_path: Path | None = None,
        clock: Callable[[], datetime | str] | None = None,
        monotonic: Callable[[], float] | None = None,
        max_catalog_bytes: int = MAX_CATALOG_BYTES,
        deadline: float | None = None,
        cancelled: Callable[[], bool] | None = None,
    ) -> GenerationCatalog:
        """Open an existing catalog without creating or repairing runtime state."""
        require_catalog_bound(max_catalog_bytes)
        catalog = cls.__new__(cls)
        catalog._monotonic = monotonic or time.monotonic

        def check_stop() -> None:
            check_cancelled(cancelled)
            catalog._check_deadline(deadline)

        check_stop()
        catalog.state_root = Path(state_root)
        check_stop()
        resolved_state_root = catalog.state_root.resolve(strict=True)
        check_stop()
        catalog.max_catalog_bytes = max_catalog_bytes
        default = catalog.state_root / "cache" / "evidence-graph" / "catalog.sqlite3"
        catalog.catalog_path = Path(catalog_path) if catalog_path is not None else default
        check_stop()
        require_inside_state_root(
            catalog.catalog_path.resolve(strict=True), resolved_state_root
        )
        check_stop()
        catalog.generations_path = catalog.catalog_path.parent / "generations"
        catalog._clock = clock or (lambda: datetime.now(UTC))
        catalog._candidate_issuer = object()
        catalog._validated = {}
        catalog._held_code = {}
        catalog._read_only = True
        check_stop()
        with closing(catalog._readonly()):
            check_stop()
        check_stop()
        return catalog

    def _connect(self, *, deadline: float | None = None) -> sqlite3.Connection:
        self._check_deadline(deadline)
        if self.catalog_path.exists() or self.catalog_path.is_symlink():
            validate_runtime_file(
                self.catalog_path,
                self.state_root,
                max_bytes=self.max_catalog_bytes,
            )
        return sqlite_io.open_operational_db(self.catalog_path, busy_ms=self._busy_ms(deadline))

    def _busy_ms(self, deadline: float | None) -> int:
        """Readers wait for a lock exactly as writers do; busy is not an error."""
        if deadline is None:
            return UNBOUNDED_BUSY_MS
        return self._remaining_busy_ms(deadline)

    def _check_deadline(self, deadline: float | None) -> None:
        require_absolute_deadline(deadline)
        check_monotonic_deadline(deadline, self._monotonic)

    def _remaining_busy_ms(self, deadline: float) -> int:
        remaining = deadline - self._monotonic()
        if remaining <= 0:
            raise TimeoutError("generation catalog deadline reached")
        return min(BUSY_MS, max(0, int(remaining * 1000)))

    @contextmanager
    def _write_transaction(self, deadline: float | None):
        self._check_deadline(deadline)
        if self._read_only:
            raise PermissionError("read-only generation catalog cannot write")
        with closing(self._connect(deadline=deadline)) as database:
            try:
                self._apply_busy_timeout(database, deadline)
                database.execute("BEGIN IMMEDIATE")
                self._check_deadline(deadline)
                yield database
                self._apply_commit_busy_timeout(database, deadline)
                database.commit()
            except sqlite3.OperationalError as exc:
                database.rollback()
                raise_writer_timeout(exc, deadline)
                raise
            except BaseException:
                database.rollback()
                raise

    def _apply_busy_timeout(self, database: sqlite3.Connection, deadline: float | None) -> None:
        if deadline is None:
            return
        database.execute(f"PRAGMA busy_timeout={self._remaining_busy_ms(deadline):d}")

    def _apply_commit_busy_timeout(
        self, database: sqlite3.Connection, deadline: float | None
    ) -> None:
        """Bound the commit's lock wait without abandoning finished work.

        The body of a write transaction may already have done what cannot be
        undone -- `discard_superseded` removes the generation's tree inside it --
        so a deadline that passed while the body ran must not cancel the commit,
        as `_remaining_busy_ms` would by raising. It only makes the commit's own
        wait short. See
        `docs/research/2026-09-17-small-corrections-in-the-generations-area.md`.
        """
        if deadline is None:
            return
        remaining = int(max(0.0, deadline - self._monotonic()) * 1000)
        database.execute(
            f"PRAGMA busy_timeout={max(COMMIT_BUSY_FLOOR_MS, min(BUSY_MS, remaining)):d}"
        )

    def _readonly(self, *, deadline: float | None = None) -> sqlite3.Connection:
        return sqlite_io.open_readonly_operational_db(
            self.catalog_path,
            self.state_root,
            max_bytes=self.max_catalog_bytes,
            busy_ms=self._busy_ms(deadline),
        )

    @staticmethod
    def _ensure_schema(database: sqlite3.Connection) -> None:
        database.executescript(
            """
            CREATE TABLE IF NOT EXISTS generations (
                generation_id TEXT PRIMARY KEY,
                parent_generation_id TEXT,
                manifest_json BLOB NOT NULL,
                manifest_sha256 TEXT NOT NULL,
                registered_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS catalog_state (
                singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                active_generation_id TEXT
            );
            INSERT OR IGNORE INTO catalog_state(singleton, active_generation_id)
            VALUES (1, NULL);
            CREATE TABLE IF NOT EXISTS activation_history (
                sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                generation_id TEXT NOT NULL,
                activated_at TEXT NOT NULL
            );
            """
        )

    def _validated_generation(
        self,
        identifier: str,
        *,
        deep: bool,
        deadline: float | None,
        cancelled: Callable[[], bool] | None,
    ) -> tuple[dict[str, object], bytes, tuple[EntrySeal, ...]]:
        manifest, seal = validate_generation(
            self.generations_path / identifier,
            state_root=self.state_root,
            deep=deep,
            deadline=deadline,
            monotonic=self._monotonic,
            cancelled=cancelled,
        )
        return manifest, canonical_json_bytes(manifest), seal

    def _validate(
        self,
        generation_id: str,
        *,
        deep: bool = False,
        deadline: float | None = None,
        cancelled: Callable[[], bool] | None = None,
    ) -> tuple[dict[str, object], bytes, tuple[EntrySeal, ...]]:
        """Validate one generation, at most once per process while it holds still.

        One answer opened the same generation three times and hashed its 295 MB
        of artifacts three times for it — 3.3 s of a 4.2 s cold call, measured
        2026-09-12. A generation is immutable after activation, so the repeat
        asks a question whose answer cannot have changed unless the directory
        moved; the cheap entry scan — device, inode, mode, size, mtime — says
        whether it moved, and only then is the hash paid again.

        What this gives up, stated plainly: a swap that leaves every entry's
        size, mtime and inode identical is no longer caught by *this* check
        inside one process. The manifest equality against the catalog row, the
        seal re-check at the end of an open, and the database validation still
        stand. Approved by the owner on 2026-09-12 with that trade named; the
        same memoisation already existed one level down, in
        `_validate_databases_once`. Research:
        `docs/research/2026-09-12-three-changes-to-pass-them.md`.
        """
        identifier = checked_generation_id(generation_id)
        seal, _files = scan_generation(
            self.generations_path / identifier,
            deadline=deadline,
            monotonic=self._monotonic,
            cancelled=cancelled,
        )
        remembered = self._remembered_validation((identifier, deep), seal)
        if remembered is not None:
            return remembered
        validated = self._validated_generation(
            identifier, deep=deep, deadline=deadline, cancelled=cancelled
        )
        self._validated[(identifier, deep)] = (seal, validated)
        return validated

    def _remembered_validation(
        self, identity: tuple[str, bool], seal: tuple[EntrySeal, ...]
    ) -> tuple | None:
        """The stored result, when the directory has not moved since it was taken.

        A deep verdict answers a shallow question: it checked everything the
        shallow one would and more. The reverse is never true, which is why the
        depth is in the key at all.
        """
        identifier, deep = identity
        stored = self._validated.get((identifier, True))
        if stored is None or deep:
            stored = self._validated.get(identity)
        if stored is None or stored[0] != seal:
            return None
        return stored[1]

    def _catalog_identity(self) -> EntrySeal:
        validate_runtime_file(
            self.catalog_path,
            self.state_root,
            max_bytes=self.max_catalog_bytes,
        )
        return entry_seal(self.catalog_path, "catalog.sqlite3")

    @staticmethod
    def _same_catalog_identity(left: EntrySeal, right: EntrySeal) -> bool:
        return (
            left.path,
            left.kind,
            left.device,
            left.inode,
            left.mode,
        ) == (
            right.path,
            right.kind,
            right.device,
            right.inode,
            right.mode,
        )

    def _validate_candidate(
        self,
        generation_id: str,
        *,
        expected_repository_scope: RepositoryScope | None = None,
        deadline: float | None = None,
        cancelled: Callable[[], bool] | None = None,
    ) -> ValidatedCandidate:
        """Fully validate and mint one process-local publication capability."""
        if expected_repository_scope is not None and not isinstance(
            expected_repository_scope, RepositoryScope
        ):
            raise TypeError("expected_repository_scope must be a RepositoryScope or None")
        identifier = checked_generation_id(generation_id)
        manifest, encoded, seal = self._validate(
            identifier, deep=True, deadline=deadline, cancelled=cancelled
        )
        scope = manifest_scope(manifest)
        require_publication_scope(scope, expected_repository_scope)
        scope_bytes = None if scope is None else canonical_json_bytes(scope.as_dict())
        return ValidatedCandidate(
            issuer=self._candidate_issuer,
            generation_id=identifier,
            generation_path=(self.generations_path / identifier).resolve(strict=True),
            state_root=self.state_root.resolve(strict=True),
            catalog_path=self.catalog_path.resolve(strict=True),
            catalog_identity=self._catalog_identity(),
            manifest_bytes=encoded,
            manifest_sha256=sha256_bytes(encoded),
            repository_scope_bytes=scope_bytes,
            seal=seal,
            mint=CANDIDATE_MINT,
        )

    def _candidate_payload(
        self,
        candidate: object,
        *,
        deadline: float | None,
        cancelled: Callable[[], bool] | None,
    ) -> tuple[dict[str, object], bytes, GenerationSealCapability]:
        self._check_deadline(deadline)
        check_cancelled(cancelled)
        if not isinstance(candidate, ValidatedCandidate) or (
            candidate.issuer is not self._candidate_issuer
        ):
            raise TypeError("validated candidate was not issued by this catalog")
        generation_path = self.generations_path / candidate.generation_id
        self._require_candidate_identity(candidate, generation_path)
        manifest = candidate_manifest(candidate)
        capability = self._acquire_seal_capability(
            generation_path,
            candidate.seal,
            deadline=deadline,
            cancelled=cancelled,
        )
        return manifest, candidate.manifest_bytes, capability

    def _require_candidate_identity(self, candidate, generation_path: Path) -> None:
        same_paths = (
            generation_path.resolve(strict=True) == candidate.generation_path
            and self.state_root.resolve(strict=True) == candidate.state_root
            and self.catalog_path.resolve(strict=True) == candidate.catalog_path
        )
        same_catalog = self._same_catalog_identity(
            candidate.catalog_identity, self._catalog_identity()
        )
        if not same_paths or not same_catalog:
            raise PermissionError("validated candidate catalog or path identity changed")

    def _write_registration(
        self,
        database: sqlite3.Connection,
        capability,
        *,
        generation_id: str,
        parent_generation_id: object,
        encoded: bytes,
        digest: str,
        timestamp: str,
        deadline: float | None,
    ) -> None:
        """One registration row, written once and immutable afterwards."""
        row = database.execute(
            "SELECT manifest_json, manifest_sha256 FROM generations WHERE generation_id = ?",
            (generation_id,),
        ).fetchone()
        require_registration_match(row, encoded, digest)
        if not capability.revalidate():
            raise ValueError("generation changed before registration")
        self._insert_registration(
            database,
            row,
            generation_id=generation_id,
            parent_generation_id=parent_generation_id,
            encoded=encoded,
            digest=digest,
            timestamp=timestamp,
        )
        self._check_deadline(deadline)

    def _insert_registration(
        self,
        database: sqlite3.Connection,
        row,
        *,
        generation_id: str,
        parent_generation_id: object,
        encoded: bytes,
        digest: str,
        timestamp: str,
    ) -> None:
        """An existing row is the idempotent retry; it is never rewritten."""
        if row is not None:
            return
        database.execute(
            "INSERT INTO generations "
            "(generation_id, parent_generation_id, manifest_json, "
            "manifest_sha256, registered_at) VALUES (?, ?, ?, ?, ?)",
            (generation_id, parent_generation_id, encoded, digest, timestamp),
        )
        self._require_catalog_bytes(database)

    def register_validated(
        self,
        candidate: object,
        *,
        deadline: float | None = None,
        cancelled: Callable[[], bool] | None = None,
    ) -> dict[str, object]:
        """Register a same-process candidate without repeating semantic validation."""
        manifest, encoded, capability = self._candidate_payload(
            candidate, deadline=deadline, cancelled=cancelled
        )
        assert isinstance(candidate, ValidatedCandidate)
        timestamp = utc_timestamp(self._clock)
        with capability:
            with self._write_transaction(deadline) as database:
                check_cancelled(cancelled)
                self._write_registration(
                    database,
                    capability,
                    generation_id=candidate.generation_id,
                    parent_generation_id=manifest.get("parent_generation_id"),
                    encoded=encoded,
                    digest=candidate.manifest_sha256,
                    timestamp=timestamp,
                    deadline=deadline,
                )
        return manifest

    def register(
        self,
        generation_id: str,
        *,
        deadline: float | None = None,
        cancelled: Callable[[], bool] | None = None,
    ) -> dict[str, object]:
        """Register a complete generation; identical retries are idempotent."""
        self._check_deadline(deadline)
        check_cancelled(cancelled)
        manifest, encoded, seal = self._validate(
            generation_id, deep=True, deadline=deadline, cancelled=cancelled
        )
        timestamp = utc_timestamp(self._clock)
        capability = self._acquire_seal_capability(
            self.generations_path / generation_id,
            seal,
            deadline=deadline,
            cancelled=cancelled,
        )
        with capability:
            with self._write_transaction(deadline) as database:
                check_cancelled(cancelled)
                self._write_registration(
                    database,
                    capability,
                    generation_id=generation_id,
                    parent_generation_id=manifest.get("parent_generation_id"),
                    encoded=encoded,
                    digest=sha256_bytes(encoded),
                    timestamp=timestamp,
                    deadline=deadline,
                )
        return manifest

    def _registered_generation(
        self,
        generation_id: str,
        *,
        deadline: float | None = None,
        cancelled: Callable[[], bool] | None = None,
    ) -> tuple[dict[str, object], tuple[EntrySeal, ...]]:
        check_cancelled(cancelled)
        manifest, encoded, seal = self._validate(
            generation_id, deadline=deadline, cancelled=cancelled
        )
        self._check_deadline(deadline)
        with closing(self._readonly(deadline=deadline)) as database:
            row = database.execute(
                "SELECT manifest_json, manifest_sha256 FROM generations WHERE generation_id = ?",
                (generation_id,),
            ).fetchone()
        self._check_deadline(deadline)
        if row is None:
            raise ValueError("generation is not registered")
        if bytes(row["manifest_json"]) != encoded or row["manifest_sha256"] != sha256_bytes(
            encoded
        ):
            raise ValueError("registered generation no longer matches its manifest")
        return manifest, seal

    def _registered_manifest(
        self, generation_id: str, *, deadline: float | None = None
    ) -> dict[str, object]:
        return self._registered_generation(generation_id, deadline=deadline)[0]

    def _deadline_seal_unchanged(
        self,
        generation_path: Path,
        expected: tuple[EntrySeal, ...],
        deadline: float | None,
        *,
        cancelled: Callable[[], bool] | None = None,
    ) -> bool:
        options: dict[str, object] = {}
        if deadline is not None:
            options["deadline"] = deadline
        if cancelled is not None:
            options["cancelled"] = cancelled
        return self._seal_unchanged(generation_path, expected, **options)

    def _require_expected_tree(
        self,
        generation_path: Path,
        expected: tuple[EntrySeal, ...],
        *,
        deadline: float | None,
        cancelled: Callable[[], bool] | None,
    ) -> dict[str, EntrySeal]:
        """The files the seal names, once the tree on disk still matches it."""
        current, files = scan_generation(
            generation_path,
            deadline=deadline,
            monotonic=self._monotonic,
            cancelled=cancelled,
        )
        expected_files = expected_file_entries(expected)
        if current != metadata_only(expected) or files != set(expected_files):
            raise PermissionError("generation changed before publication sealing")
        return expected_files

    def _hold_expected_files(
        self,
        generation_path: Path,
        expected_files: dict[str, EntrySeal],
        held_files: list,
        *,
        deadline: float | None,
        cancelled: Callable[[], bool] | None,
    ) -> None:
        """Open every named file, appending as we go so the caller can close them."""
        for relative, entry in expected_files.items():
            check_cancelled(cancelled)
            self._check_deadline(deadline)
            held_files.append(self._held_file_for(generation_path, relative, entry))

    def _held_file_for(self, generation_path: Path, relative: str, entry) -> HeldFile:
        path = generation_path.joinpath(*relative.split("/"))
        if entry_seal(path, relative) != replace(entry, sha256=None):
            raise PermissionError("generation member changed before opening")
        descriptor = open_read_descriptor(path)
        try:
            opened = os.fstat(descriptor)
            if not same_regular_file(opened, path):
                raise PermissionError("generation member identity changed while opening")
            return HeldFile(
                path=path,
                relative=relative,
                descriptor=descriptor,
                opened_state=stable_descriptor_state(descriptor),
            )
        except BaseException:
            os.close(descriptor)
            raise

    def _require_sealed_content(self, capability, expected_files: dict) -> None:
        for held in capability.held_files:
            entry = expected_files[held.relative]
            size, digest = generation_descriptors.hash_descriptor(
                held.descriptor,
                entry.size,
                deadline=capability.deadline,
                monotonic=self._monotonic,
                cancelled=capability.cancelled,
            )
            if size != entry.size or digest != entry.sha256:
                raise PermissionError("generation content changed while sealing")

    def _acquire_seal_capability(
        self,
        generation_path: Path,
        expected: tuple[EntrySeal, ...],
        *,
        deadline: float | None = None,
        cancelled: Callable[[], bool] | None = None,
    ) -> GenerationSealCapability:
        """Hash a bounded generation through held descriptors outside the writer lock."""
        self._check_deadline(deadline)
        check_cancelled(cancelled)
        generation_path = Path(generation_path)
        expected_files = self._require_expected_tree(
            generation_path, expected, deadline=deadline, cancelled=cancelled
        )
        held_files: list[HeldFile] = []
        try:
            self._hold_expected_files(
                generation_path,
                expected_files,
                held_files,
                deadline=deadline,
                cancelled=cancelled,
            )
            capability = GenerationSealCapability(
                generation_path,
                expected,
                tuple(held_files),
                deadline=deadline,
                monotonic=self._monotonic,
                cancelled=cancelled,
            )
            self._require_sealed_content(capability, expected_files)
            if not capability.revalidate():
                raise PermissionError("generation changed during publication sealing")
            return capability
        except BaseException:
            close_descriptors(held_files)
            raise

    def _seal_unchanged(
        self,
        generation_path: Path,
        expected: tuple[EntrySeal, ...],
        *,
        deadline: float | None = None,
        cancelled: Callable[[], bool] | None = None,
    ) -> bool:
        """Recheck bounded metadata and content without a catalog writer lock."""
        try:
            capability = self._acquire_seal_capability(
                generation_path,
                expected,
                deadline=deadline,
                cancelled=cancelled,
            )
        except (FileNotFoundError, PermissionError, ValueError):
            return False
        with capability:
            return capability.revalidate()

    def _catalog_rows(
        self,
        database: sqlite3.Connection,
        query: str,
        deadline: float | None,
        cancelled: Callable[[], bool] | None = None,
    ) -> list[sqlite3.Row]:
        stopped: list[TimeoutError] = []
        progress = self._catalog_query_progress(deadline, cancelled, stopped)
        # One is SQLite's minimum callback interval. This is scheduling, not a
        # record quota: interrupt sorting as well as iteration at the caller's stop.
        database.set_progress_handler(progress, 1)
        try:
            return self._collect_catalog_rows(database.execute(query), deadline, cancelled)
        except sqlite3.OperationalError:
            if stopped:
                raise stopped[0] from None
            raise
        finally:
            database.set_progress_handler(None, 0)

    def _catalog_query_progress(self, deadline, cancelled, stopped):
        if deadline is None and cancelled is None:
            return None

        def progress() -> int:
            try:
                self._check_deadline(deadline)
                check_cancelled(cancelled)
            except TimeoutError as exc:
                stopped.append(exc)
                return 1
            return 0

        return progress

    def _collect_catalog_rows(self, cursor, deadline, cancelled) -> list[sqlite3.Row]:
        rows: list[sqlite3.Row] = []
        for row in cursor:
            self._check_deadline(deadline)
            check_cancelled(cancelled)
            rows.append(row)
        return rows

    def _require_catalog_bytes(self, database: sqlite3.Connection) -> None:
        page_count = database.execute("PRAGMA main.page_count").fetchone()[0]
        page_size = database.execute("PRAGMA main.page_size").fetchone()[0]
        if not valid_page_metric(page_count, 0) or not valid_page_metric(page_size, 1):
            raise ValueError("catalog page metadata is invalid")
        # Reserve one page because commit may finalize a pending b-tree allocation.
        if page_count * page_size + page_size > self.max_catalog_bytes:
            raise ValueError("catalog byte ceiling would be exceeded")

    def _require_activation_ready(
        self,
        database: sqlite3.Connection,
        capability,
        *,
        identifier: str,
        expected_active: str | None,
        registration_token: tuple[bytes, str],
    ) -> None:
        registered = database.execute(
            "SELECT manifest_json, manifest_sha256 FROM generations WHERE generation_id = ?",
            (identifier,),
        ).fetchone()
        require_registration_token(registered, registration_token)
        if not capability.revalidate():
            raise ValueError("generation changed before activation")

    def _cas_activate(
        self,
        database: sqlite3.Connection,
        capability,
        *,
        identifier: str,
        expected_active: str | None,
        registration_token: tuple[bytes, str],
        timestamp: str,
        deadline: float | None,
    ) -> bool | None:
        """None when the pointer moved under us; otherwise whether it now points here."""
        if not pointer_is(database, expected_active):
            self._check_deadline(deadline)
            return None
        self._require_activation_ready(
            database,
            capability,
            identifier=identifier,
            expected_active=expected_active,
            registration_token=registration_token,
        )
        self._check_deadline(deadline)
        if identifier != expected_active:
            self._move_active_pointer(database, identifier, timestamp)
        self._check_deadline(deadline)
        return True

    def _move_active_pointer(
        self, database: sqlite3.Connection, identifier: str, timestamp: str
    ) -> None:
        database.execute(
            "UPDATE catalog_state SET active_generation_id = ? WHERE singleton = 1",
            (identifier,),
        )
        database.execute(
            "INSERT INTO activation_history(generation_id, activated_at) VALUES (?, ?)",
            (identifier, timestamp),
        )
        self._require_catalog_bytes(database)

    def _activate_validated(
        self,
        candidate: object,
        *,
        expected_active: str | None,
        deadline: float | None = None,
        cancelled: Callable[[], bool] | None = None,
    ) -> bool:
        """CAS-activate a same-process candidate without semantic revalidation."""
        manifest, encoded, capability = self._candidate_payload(
            candidate, deadline=deadline, cancelled=cancelled
        )
        assert isinstance(candidate, ValidatedCandidate)
        if expected_active is not None:
            expected_active = checked_generation_id(expected_active)
        timestamp = utc_timestamp(self._clock)
        with capability:
            with self._write_transaction(deadline) as database:
                check_cancelled(cancelled)
                outcome = self._cas_activate(
                    database,
                    capability,
                    identifier=candidate.generation_id,
                    expected_active=expected_active,
                    registration_token=(encoded, candidate.manifest_sha256),
                    timestamp=timestamp,
                    deadline=deadline,
                )
        return bool(outcome)

    def activate(
        self,
        generation_id: str,
        *,
        expected_active: str | None,
        deadline: float | None = None,
        cancelled: Callable[[], bool] | None = None,
    ) -> bool:
        """Atomically activate a validated generation if the pointer matches."""
        self._check_deadline(deadline)
        check_cancelled(cancelled)
        identifier = checked_generation_id(generation_id)
        if expected_active is not None:
            expected_active = checked_generation_id(expected_active)
        manifest, seal = self._registered_generation(
            identifier, deadline=deadline, cancelled=cancelled
        )
        encoded = canonical_json_bytes(manifest)
        capability = self._acquire_seal_capability(
            self.generations_path / identifier,
            seal,
            deadline=deadline,
            cancelled=cancelled,
        )
        timestamp = utc_timestamp(self._clock)
        with capability:
            with self._write_transaction(deadline) as database:
                check_cancelled(cancelled)
                outcome = self._cas_activate(
                    database,
                    capability,
                    identifier=identifier,
                    expected_active=expected_active,
                    registration_token=(encoded, sha256_bytes(encoded)),
                    timestamp=timestamp,
                    deadline=deadline,
                )
        return bool(outcome)

    def _drop_registration(self, identifier: str) -> bool | None:
        """Remove the row; None means the generation is in use and must be kept."""
        cleanup_deadline = self._monotonic() + CLEANUP_CATALOG_FENCE_SECONDS
        with self._write_transaction(cleanup_deadline) as database:
            if generation_in_use(database, identifier):
                return None
            return self._delete_registration(database, identifier)

    def _unseal_unregistered(self, identifier: str) -> None:
        """Make the unregistered tree unrecoverable before its removal can be stopped.

        The tree removal honours the caller's deadline; this does not. A complete
        tree with no row used to be registered again by `recover_orphans`, as the
        newest generation of its checkout. See
        `docs/research/2026-09-17-a-retired-generation-does-not-come-back.md`.
        """
        cleanup_deadline = self._monotonic() + CLEANUP_CATALOG_FENCE_SECONDS
        with self._write_transaction(cleanup_deadline) as database:
            if still_referenced(database, identifier):
                return
            unlink_manifest(self.generations_path / identifier)

    @staticmethod
    def _delete_registration(database: sqlite3.Connection, identifier: str) -> bool:
        registered = database.execute(
            "SELECT 1 FROM generations WHERE generation_id = ?", (identifier,)
        ).fetchone()
        if registered is None:
            return False
        database.execute("DELETE FROM generations WHERE generation_id = ?", (identifier,))
        return True

    def _remove_generation_tree(
        self,
        identifier: str,
        *,
        deadline: float | None,
        cancelled: Callable[[], bool] | None,
    ) -> bool:
        generation_path = self.generations_path / identifier
        if not generation_path.exists() and not generation_path.is_symlink():
            return False
        scan_generation(
            generation_path,
            deadline=deadline,
            monotonic=self._monotonic,
            cancelled=cancelled,
        )
        check_cancelled(cancelled)
        self._check_deadline(deadline)
        shutil.rmtree(generation_path)
        if generation_path.exists():
            raise OSError("generation cleanup did not remove candidate")
        runtime_sync.fsync_directory(self.generations_path)
        return True

    def discard_unactivated(
        self,
        generation_id: str,
        *,
        deadline: float | None = None,
        cancelled: Callable[[], bool] | None = None,
    ) -> bool:
        """Remove a never-activated registration after fenced publication aborts."""
        identifier = checked_generation_id(generation_id)
        require_absolute_deadline(deadline)
        removed_registration = self._drop_registration(identifier)
        if removed_registration is None:
            return False
        self._unseal_unregistered(identifier)

        # The committed delete cannot be rolled back by filesystem failures. A
        # second writer fence keeps activation and registration from racing the
        # recursive cleanup.
        check_cancelled(cancelled)
        self._check_deadline(deadline)
        with self._write_transaction(deadline) as database:
            check_cancelled(cancelled)
            if still_referenced(database, identifier):
                return False
            removed_directory = self._remove_generation_tree(
                identifier, deadline=deadline, cancelled=cancelled
            )
            check_cancelled(cancelled)
            self._check_deadline(deadline)
        return removed_registration or removed_directory

    def retained_generations(
        self,
        *,
        retained_ancestors: int = RETAINED_ANCESTOR_GENERATIONS,
        deadline: float | None = None,
        cancelled: Callable[[], bool] | None = None,
    ) -> tuple[str, ...]:
        """The generations retention protects: the active one and its ancestors."""
        require_retained_ancestors(retained_ancestors)
        self._check_deadline(deadline)
        check_cancelled(cancelled)
        with closing(self._readonly(deadline=deadline)) as database:
            return retained_generation_ids(database, retained_ancestors)

    def registered_generation_ids(
        self,
        *,
        deadline: float | None = None,
        cancelled: Callable[[], bool] | None = None,
    ) -> tuple[str, ...]:
        """Every registered generation id, without validating a single tree."""
        self._check_deadline(deadline)
        check_cancelled(cancelled)
        with closing(self._readonly(deadline=deadline)) as database:
            rows = self._catalog_rows(
                database,
                "SELECT generation_id FROM generations",
                deadline,
                cancelled,
            )
        return tuple(row["generation_id"] for row in rows)

    def activated_generation_ids(
        self,
        *,
        deadline: float | None = None,
        cancelled: Callable[[], bool] | None = None,
    ) -> frozenset[str]:
        """Every generation that has ever been active — the superseded set."""
        self._check_deadline(deadline)
        check_cancelled(cancelled)
        with closing(self._readonly(deadline=deadline)) as database:
            rows = self._catalog_rows(
                database,
                "SELECT DISTINCT generation_id FROM activation_history",
                deadline,
                cancelled,
            )
        return frozenset(row["generation_id"] for row in rows)

    @staticmethod
    def _require_registered(database: sqlite3.Connection, identifier: str) -> None:
        """A tree with no row is not this catalog's to remove."""
        registered = database.execute(
            "SELECT 1 FROM generations WHERE generation_id = ?", (identifier,)
        ).fetchone()
        if registered is None:
            raise ValueError("generation is not registered")

    def _require_prunable(
        self, database: sqlite3.Connection, identifier: str, retained: tuple[str, ...]
    ) -> None:
        """Registered, once active, and no longer retained. Its tree may already be
        gone: that is a discard whose commit was lost, and discarding completes it.
        See `docs/research/2026-09-14-a-removal-past-its-point-of-no-return-finishes.md`."""
        if identifier in retained:
            raise ValueError("retained generation cannot be discarded")
        self._require_registered(database, identifier)
        require_activated(database, identifier)

    @staticmethod
    def _delete_generation_rows(database: sqlite3.Connection, identifier: str) -> None:
        """Registration and activation history leave together: a surviving
        history row keeps `_generation_in_use` refusing a tree that is gone."""
        database.execute("DELETE FROM generations WHERE generation_id = ?", (identifier,))
        database.execute(
            "DELETE FROM activation_history WHERE generation_id = ?", (identifier,)
        )

    def discard_superseded(
        self,
        generation_id: str,
        *,
        retained_ancestors: int = RETAINED_ANCESTOR_GENERATIONS,
        deadline: float | None = None,
        cancelled: Callable[[], bool] | None = None,
    ) -> bool:
        """Drop one generation retention no longer keeps: row, history and tree.

        `discard_unactivated` refuses anything that was ever activated, which is
        every generation but one on a vault that has been building nightly. This
        removes a superseded one, and it removes all three parts inside a single
        write transaction: a filesystem failure rolls the registration back
        rather than stranding a row whose tree is gone. The live set is
        recomputed from that same transaction, so an activation racing this call
        cannot make it delete what has just become live.
        """
        identifier = checked_generation_id(generation_id)
        require_absolute_deadline(deadline)
        require_retained_ancestors(retained_ancestors)
        with self._write_transaction(deadline) as database:
            check_cancelled(cancelled)
            retained = retained_generation_ids(database, retained_ancestors)
            require_active_root(retained)
            self._require_prunable(database, identifier, retained)
            self._delete_generation_rows(database, identifier)
            # Past this removal nothing may abort the commit: the tree cannot come
            # back, and a rolled-back row would name a tree that no longer exists.
            self._remove_generation_tree(
                identifier, deadline=deadline, cancelled=cancelled
            )
        return True

    def _snapshot_catalog(
        self,
        *,
        deadline: float | None = None,
        cancelled: Callable[[], bool] | None = None,
    ) -> tuple[str | None, list[str], dict[str, str | None]]:
        check_cancelled(cancelled)
        self._check_deadline(deadline)
        with closing(self._readonly(deadline=deadline)) as database:
            state = database.execute(
                "SELECT active_generation_id FROM catalog_state WHERE singleton = 1"
            ).fetchone()
            if state is None:
                raise ValueError("catalog active pointer is missing")
            history_rows = self._catalog_rows(
                database,
                "SELECT generation_id FROM activation_history ORDER BY sequence DESC",
                deadline,
                cancelled,
            )
            history = [row["generation_id"] for row in history_rows]
            generation_rows = self._catalog_rows(
                database,
                "SELECT generation_id, parent_generation_id FROM generations",
                deadline,
                cancelled,
            )
            parents = {row["generation_id"]: row["parent_generation_id"] for row in generation_rows}
        check_cancelled(cancelled)
        self._check_deadline(deadline)
        return state["active_generation_id"], history, parents

    @staticmethod
    def _fallback_order(
        active: str | None,
        history: list[str],
        parents: dict[str, str | None],
        *,
        deadline: float | None = None,
        cancelled: Callable[[], bool] | None = None,
    ) -> list[str]:
        ordered: list[str] = []
        seen: set[str] = set()
        prior = [identifier for identifier in [active, *history] if identifier is not None]
        for identifier in prior:
            check_cancelled(cancelled)
            check_monotonic_deadline(deadline, time.monotonic)
            append_unseen(ordered, seen, identifier)
        for identifier in prior:
            check_cancelled(cancelled)
            check_monotonic_deadline(deadline, time.monotonic)
            append_ancestors(ordered, seen, parents, identifier, deadline, cancelled)
        return ordered

    def code_generation_for_repository(
        self,
        repository_scope: RepositoryScope,
        *,
        deadline: float | None = None,
        cancelled: Callable[[], bool] | None = None,
    ) -> tuple[str, dict[str, object]] | tuple[None, None]:
        """The newest registered generation of this repository that holds code.

        The vault carries two generations of one checkout — memory, which the
        active pointer names, and code, which is only ever registered — so a code
        question cannot use the pointer. A generation that holds code says so with
        `code_roots`; this returns the newest such registration for the scope, or
        (None, None) when the repository has none yet. Decision:
        `docs/research/2026-09-12-the-vault-is-a-repository-too.md`.
        """
        held = self.code_generations_for_repository(
            repository_scope, deadline=deadline, cancelled=cancelled
        )
        if not held:
            return None, None
        return held[0]

    def code_generations_for_repository(
        self,
        repository_scope: RepositoryScope,
        *,
        deadline: float | None = None,
        cancelled: Callable[[], bool] | None = None,
    ) -> list[tuple[str, dict[str, object]]]:
        """Every registered code generation of this repository, newest first.

        Retention keeps a second one for a reader behind a refresh; a reader
        that cannot open the newest walks on to it.
        """
        expected = RepositoryScope.from_dict(repository_scope.as_dict())
        return [
            (identifier, manifest)
            for identifier, _registered_at, manifest in self.registered_manifests(
                deadline=deadline, cancelled=cancelled
            )
            if manifest_belongs_to(manifest, expected)
            and self.holds_code(identifier, manifest)
        ]

    def holds_code(self, identifier: str, manifest: dict[str, object]) -> bool:
        """True when this generation was built from code roots, not from the memory walk.

        The manifest says so since 2026-09-12; an older one is asked through the
        snapshot policy it binds. Remembered per id: a generation is immutable.
        See `docs/research/2026-09-17-a-question-is-answered-by-its-own-kind-of-generation.md`.
        """
        if manifest.get("code_roots"):
            return True
        if identifier not in self._held_code:
            self._held_code[identifier] = bool(
                policy_code_roots(
                    self.generations_path / identifier, manifest, self.state_root
                )
            )
        return self._held_code[identifier]

    def registered_manifests(
        self,
        *,
        deadline: float | None = None,
        cancelled: Callable[[], bool] | None = None,
    ) -> list[tuple[str, str, dict[str, object]]]:
        """Every registered generation as (id, registered_at, manifest), newest first.

        Only manifests whose stored bytes still hash to the recorded digest and
        still round-trip through canonical JSON are returned; a row that fails
        either is skipped rather than raised, because one damaged registration
        must not hide every healthy one from a listing.
        """
        rows = self._registered_rows(deadline)
        manifests: list[tuple[str, str, dict[str, object]]] = []
        for row in rows:
            check_cancelled(cancelled)
            self._check_deadline(deadline)
            manifest = verified_manifest(row)
            if manifest is not None:
                manifests.append((row["generation_id"], row["registered_at"], manifest))
        return manifests

    def _registered_rows(self, deadline: float | None) -> list[sqlite3.Row]:
        self._check_deadline(deadline)
        with closing(self._readonly(deadline=deadline)) as database:
            return self._catalog_rows(
                database,
                "SELECT generation_id, registered_at, manifest_json, manifest_sha256 "
                "FROM generations ORDER BY registered_at DESC, generation_id DESC",
                deadline,
            )

    def _scoped_generation(
        self,
        repository_scope: RepositoryScope | None,
        *,
        deadline: float | None,
        cancelled: Callable[[], bool] | None,
    ) -> dict[str, object] | None:
        """The newest registered generation of a repository the pointer does not name.

        This is the whole of CODE-03's read side, and it is deliberately
        asymmetric to `_select_fallback`: it never moves the active pointer,
        never writes activation history and never repairs anything. The single
        pointer belongs to the vault. Were a foreign repository allowed to
        activate, indexing one would make the vault's own scope unresolvable and
        every knowledge query would lose its generation and read Markdown
        directly (`no_active_generation`) -- NEW-65, recreated deliberately.

        The pointer path answers memory questions, so a code generation is
        never its answer: a code reader asks `code_generations_for_repository`.
        See `docs/research/2026-09-17-a-question-is-answered-by-its-own-kind-of-generation.md`.
        """
        if repository_scope is None:
            return None
        manifests = self.registered_manifests(deadline=deadline, cancelled=cancelled)
        candidates = (
            self._usable_memory_generation(
                identifier, manifest, repository_scope, deadline, cancelled
            )
            for identifier, _registered_at, manifest in manifests
        )
        return next((selected for selected in candidates if selected is not None), None)

    def _usable_memory_generation(
        self,
        identifier: str,
        manifest: dict[str, object],
        scope: RepositoryScope,
        deadline: float | None,
        cancelled: Callable[[], bool] | None,
    ) -> dict[str, object] | None:
        """This registered generation when it is the scope's memory and still valid."""
        if not self._memory_of(identifier, manifest, scope):
            return None
        return self._validated_scoped_generation(identifier, deadline, cancelled)

    def _memory_of(
        self, identifier: str, manifest: dict[str, object], scope: RepositoryScope
    ) -> bool:
        if not manifest_belongs_to(manifest, scope):
            return False
        return not self.holds_code(identifier, manifest)

    def _validated_scoped_generation(
        self,
        identifier: str,
        deadline: float | None,
        cancelled: Callable[[], bool] | None,
    ) -> dict[str, object] | None:
        """Re-validate artifacts before handing a generation out; None when unusable."""
        try:
            selected, _seal = self._registered_generation_shaped(
                identifier, deadline, cancelled
            )
        except (FileNotFoundError, PermissionError, TypeError, ValueError):
            return None
        return selected

    def _registered_repository_scope(
        self,
        generation_id: str,
        *,
        deadline: float | None = None,
        cancelled: Callable[[], bool] | None = None,
    ) -> RepositoryScope | None:
        """Read repository eligibility from the catalog without validating artifacts."""
        check_cancelled(cancelled)
        self._check_deadline(deadline)
        with closing(self._readonly(deadline=deadline)) as database:
            row = database.execute(
                "SELECT manifest_json, manifest_sha256 FROM generations "
                "WHERE generation_id = ?",
                (generation_id,),
            ).fetchone()
        check_cancelled(cancelled)
        self._check_deadline(deadline)
        if row is None:
            return None
        encoded = bytes(row["manifest_json"])
        if sha256_bytes(encoded) != row["manifest_sha256"]:
            return None
        return scope_from_manifest_bytes(encoded)

    def _scope_admits(
        self,
        identifier: str | None,
        repository_scope: RepositoryScope | None,
        *,
        deadline: float | None,
        cancelled: Callable[[], bool] | None,
    ) -> bool:
        """Whether a registered generation belongs to this repository checkout.

        Eligibility is the repository identity, not the commit the generation
        happened to be built at. Comparing whole scopes made every generation
        ineligible after the next commit, so retrieval fell back to the legacy
        index almost always on a vault that commits its own runtime (NEW-65). The
        commit stays in the manifest as provenance, and publication still binds it
        exactly.
        """
        if identifier is None or repository_scope is None:
            return True
        registered = self._registered_repository_scope(
            identifier, deadline=deadline, cancelled=cancelled
        )
        if registered is None:
            return False
        return registered.same_repository(repository_scope)

    def _registered_generation_shaped(
        self,
        identifier: str,
        deadline: float | None,
        cancelled: Callable[[], bool] | None,
    ) -> tuple[dict[str, object], tuple[EntrySeal, ...]]:
        """Keep the bare call shape when nothing bounds the read.

        A caller may replace this method with a positional stub; passing unused
        keyword arguments would turn its failure into a TypeError that fallback
        selection swallows.
        """
        if deadline is None and cancelled is None:
            return self._registered_generation(identifier)
        return self._registered_generation(
            identifier, deadline=deadline, cancelled=cancelled
        )

    def _select_fallback(
        self,
        active: str | None,
        history: list[str],
        parents: dict[str, str | None],
        repository_scope: RepositoryScope | None,
        *,
        deadline: float | None,
        cancelled: Callable[[], bool] | None,
    ) -> tuple[dict[str, object] | None, str | None, tuple[EntrySeal, ...] | None]:
        for identifier in self._fallback_order(
            active, history, parents, deadline=deadline, cancelled=cancelled
        ):
            check_cancelled(cancelled)
            self._check_deadline(deadline)
            if not self._scope_admits(
                identifier, repository_scope, deadline=deadline, cancelled=cancelled
            ):
                continue
            try:
                selected, seal = self._registered_generation_shaped(
                    identifier, deadline, cancelled
                )
            except (FileNotFoundError, PermissionError, TypeError, ValueError):
                continue
            return selected, identifier, seal
        return None, None, None

    def _require_fallback_seal(
        self,
        selected_id: str | None,
        selected_seal: tuple[EntrySeal, ...] | None,
        deadline: float | None,
        cancelled: Callable[[], bool] | None,
    ) -> None:
        if selected_id is None:
            return
        unchanged = selected_seal is not None and self._deadline_seal_unchanged(
            self.generations_path / selected_id, selected_seal, deadline, cancelled=cancelled
        )
        if not unchanged:
            raise ValueError("fallback generation changed after validation seal")

    def _repair_pointer(
        self,
        database: sqlite3.Connection,
        *,
        active: str | None,
        selected_id: str | None,
        selected_token: tuple[bytes, str] | None,
        timestamp: str,
    ) -> bool:
        """False when the pointer moved under us and the attempt must restart."""
        if not pointer_matches(database, active):
            return False
        self._require_repair_target(database, selected_id, selected_token)
        database.execute(
            "UPDATE catalog_state SET active_generation_id = ? WHERE singleton = 1",
            (selected_id,),
        )
        record_activation(database, selected_id, timestamp)
        self._require_catalog_bytes(database)
        return True

    def _require_repair_target(
        self,
        database: sqlite3.Connection,
        selected_id: str | None,
        selected_token: tuple[bytes, str] | None,
    ) -> None:
        """Clearing the pointer has no target to check; repointing it does."""
        if selected_id is None:
            return
        registered = database.execute(
            "SELECT manifest_json, manifest_sha256 FROM generations WHERE generation_id = ?",
            (selected_id,),
        ).fetchone()
        require_fallback_registration(registered, selected_token)

    def _repair_active_pointer(
        self,
        *,
        active: str | None,
        selected: dict[str, object] | None,
        selected_id: str | None,
        deadline: float | None,
        cancelled: Callable[[], bool] | None,
    ) -> bool:
        """False when the pointer moved under us and the caller must retry."""
        selected_token = selected_registration_token(selected, selected_id)
        timestamp = utc_timestamp(self._clock)
        with self._write_transaction(deadline) as database:
            check_cancelled(cancelled)
            repaired = self._repair_pointer(
                database,
                active=active,
                selected_id=selected_id,
                selected_token=selected_token,
                timestamp=timestamp,
            )
            check_cancelled(cancelled)
            self._check_deadline(deadline)
        return repaired

    def _active_attempt(
        self,
        *,
        repository_scope: RepositoryScope | None,
        deadline: float | None,
        cancelled: Callable[[], bool] | None,
    ) -> tuple[bool, dict[str, object] | None]:
        """One attempt; the first element says whether the answer is settled."""
        active, history, parents = self._snapshot_catalog(
            deadline=deadline, cancelled=cancelled
        )
        if not self._scope_admits(
            active, repository_scope, deadline=deadline, cancelled=cancelled
        ):
            # CODE-03: the pointer names another repository. Answer from this
            # repository's own registered generations, without moving it.
            return True, self._scoped_generation(
                repository_scope, deadline=deadline, cancelled=cancelled
            )
        selected, selected_id, selected_seal = self._select_fallback(
            active, history, parents, repository_scope,
            deadline=deadline, cancelled=cancelled,
        )
        return self._settled_selection(
            active,
            selected,
            selected_id,
            selected_seal,
            repository_scope,
            deadline,
            cancelled,
        )

    def _or_scoped(
        self,
        selected: dict[str, object] | None,
        repository_scope: RepositoryScope | None,
        deadline: float | None,
        cancelled: Callable[[], bool] | None,
    ) -> dict[str, object] | None:
        """Fall through to this repository's own generations when nothing was found.

        `_select_fallback` walks the active pointer, the activation history and
        their parents, so a generation that was registered and never activated
        is invisible to it. That is every CODE-03 generation, by design, and it
        is also every generation of a young vault whose pointer is still empty.
        Reached only when the walk found nothing; the vault's normal case
        (pointer set, its own generation selected) never gets here.
        """
        if selected is not None:
            return selected
        return self._scoped_generation(
            repository_scope, deadline=deadline, cancelled=cancelled
        )

    def _settled_selection(
        self,
        active: str | None,
        selected: dict[str, object] | None,
        selected_id: str | None,
        selected_seal: tuple[EntrySeal, ...] | None,
        repository_scope: RepositoryScope | None,
        deadline: float | None,
        cancelled: Callable[[], bool] | None,
    ) -> tuple[bool, dict[str, object] | None]:
        """Settle a fallback selection, repairing the pointer when it may."""
        if selected_id == active:
            check_cancelled(cancelled)
            self._check_deadline(deadline)
            return True, self._or_scoped(
                selected, repository_scope, deadline, cancelled
            )
        self._require_fallback_seal(selected_id, selected_seal, deadline, cancelled)
        if self._read_only:
            return True, selected
        repaired = self._repair_active_pointer(
            active=active,
            selected=selected,
            selected_id=selected_id,
            deadline=deadline,
            cancelled=cancelled,
        )
        return repaired, selected

    def _get_active(
        self,
        *,
        repository_scope: RepositoryScope | None = None,
        deadline: float | None = None,
        cancelled: Callable[[], bool] | None = None,
    ) -> dict[str, object] | None:
        """Return a valid active generation, repairing a corrupt pointer safely."""
        for _attempt in range(3):
            check_cancelled(cancelled)
            self._check_deadline(deadline)
            settled, manifest = self._active_attempt(
                repository_scope=repository_scope,
                deadline=deadline,
                cancelled=cancelled,
            )
            if settled:
                return manifest
        raise sqlite3.OperationalError("active generation changed during fallback repair")

    def get_active(
        self,
        *,
        deadline: float | None = None,
        cancelled: Callable[[], bool] | None = None,
    ) -> dict[str, object] | None:
        """Return a valid active generation, repairing a corrupt pointer safely."""
        return self._get_active(deadline=deadline, cancelled=cancelled)

    def get_active_for_repository(
        self,
        repository_scope: RepositoryScope,
        *,
        deadline: float | None = None,
        cancelled: Callable[[], bool] | None = None,
    ) -> dict[str, object] | None:
        """Return or repair an active generation only within one repository scope."""
        if not isinstance(repository_scope, RepositoryScope):
            raise TypeError("repository_scope must be a RepositoryScope")
        expected_scope = RepositoryScope.from_dict(repository_scope.as_dict())
        return self._get_active(
            repository_scope=expected_scope,
            deadline=deadline,
            cancelled=cancelled,
        )

    def _registered_identifiers(self, deadline: float | None) -> set[str]:
        with closing(self._readonly(deadline=deadline)) as database:
            rows = self._catalog_rows(
                database,
                "SELECT generation_id FROM generations",
                deadline,
            )
            return {row["generation_id"] for row in rows}

    def _register_shaped(self, generation_id: str, deadline: float | None) -> None:
        """Keep the bare call shape when nothing bounds the registration."""
        if deadline is None:
            self.register(generation_id)
            return
        self.register(generation_id, deadline=deadline)

    def _recovered_child(self, entry, registered: set[str], deadline: float | None) -> bool:
        """True when this child was registered by this pass."""
        if entry.name in registered or not entry.is_dir(follow_symlinks=False):
            return False
        try:
            checked_generation_id(entry.name)
            if generation_descriptors.is_link_or_reparse(Path(entry.path)):
                return False
            self._register_shaped(entry.name, deadline)
        except (FileNotFoundError, PermissionError, TypeError, ValueError):
            return False
        return True

    def recover_orphans(self, *, deadline: float | None = None) -> list[str]:
        """Register complete immediate-child generations without activating them."""
        self._check_deadline(deadline)
        registered = self._registered_identifiers(deadline)
        children = bounded_scandir(
            self.generations_path,
            generation_limits.MAX_GENERATION_CHILDREN,
            "generation child count exceeds the recovery bound",
            deadline=deadline,
            monotonic=self._monotonic,
        )
        recovered: list[str] = []
        for entry in children:
            if not self._recover_child_before_deadline(entry, registered, deadline, recovered):
                return recovered
        return recovered

    def _recover_child_before_deadline(
        self, entry, registered: set[str], deadline: float | None, recovered: list[str]
    ) -> bool:
        """False when the deadline stops a pass that already recovered something."""
        try:
            self._check_deadline(deadline)
            if self._recovered_child(entry, registered, deadline):
                recovered.append(entry.name)
        except TimeoutError:
            if recovered:
                return False
            raise
        return True
