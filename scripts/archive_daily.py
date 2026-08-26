"""Publish eligible daily logs as verified immutable BagIt packages."""
from __future__ import annotations

import argparse
import errno
import json
import os
import re
import sqlite3
import stat
import sys
import time
import uuid
from collections.abc import Callable
from contextlib import closing
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from bounded_io import read_stable_bytes  # noqa: E402
from evidence_resolver import (  # noqa: E402
    MAX_DAILY_BYTES,
    EvidenceRef,
    EvidenceResolutionError,
    EvidenceResolver,
    _line_span,
    _regular_directory,
    bounded_directory_entries,
    compile_authority_attestation,
    daily_entries,
    validate_bag,
)
from markdown_transaction import (  # noqa: E402
    MarkdownChange,
    MarkdownCoordinator,
    _acl_output_text,
    _harden_owner_only,
    _run_acl_command,
    _windows_acl_identity,
)
from memory_queue import MemoryQueue, QueueOperationError, SourceFence  # noqa: E402
from memory_state import ROOT, STATE_ROOT  # noqa: E402
from reliable_memory import (  # noqa: E402
    DEFAULTS,
    _set_owner_only,
    canonical_json_bytes,
    fsync_directory,
    fsync_file,
    sha256_bytes,
)

DEFAULT_HOT_DAYS = 90
DEFAULT_TRANSACTION_RETENTION_DAYS = 30
MAX_POLICY_BYTES = 1024 * 1024
MAX_ARCHIVE_ENTRIES = 10_000
MAX_ARCHIVE_MONTHS = 1_200
ARCHIVE_WRITER_WAIT_SECONDS = 0.25
# Explicit access control entries survive `/inheritance:r`, which only drops
# inherited ones. Windows images place explicit SYSTEM, Administrators and
# OWNER RIGHTS entries on the temporary tree, so a sealed package needs the
# same removal passes the cache root and the LSP owner directory already use.
_BROAD_ACL_SIDS = (
    "*S-1-1-0",  # Everyone
    "*S-1-3-0",  # Creator Owner
    "*S-1-3-4",  # Owner Rights
    "*S-1-5-11",  # Authenticated Users
    "*S-1-5-18",  # Local System
    "*S-1-5-32-544",  # Administrators
    "*S-1-5-32-545",  # Users
    "*S-1-15-2-1",  # All application packages
    "*S-1-15-2-2",  # All restricted application packages
)
DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")


@dataclass(frozen=True)
class Eligibility:
    eligible: bool
    reasons: tuple[str, ...]
    source_sha256: str | None = None
    receipt: dict[str, object] | None = None
    blocking_task_ids: tuple[str, ...] = ()


BUILD_INTENT_FIELDS = frozenset(
    {
        "created_at",
        "final_bag_name",
        "logical_daily_id",
        "schema_version",
        "source_hash",
    }
)


@dataclass(frozen=True)
class _CompileAuthority:
    """Proven compile-receipt authority for one archive build."""

    receipt_bytes: bytes
    receipt_path: Path
    logical_path: str
    source_identity: str
    operation_id: str
    attestation: dict[str, object]


@dataclass(frozen=True)
class ArchiveReceipt:
    logical_daily_id: str
    source_sha256: str
    bag_path: Path
    state: str


class ArchiveConflict(RuntimeError):
    code = "archive_source_conflict"


class ArchiveFenceConflict(RuntimeError):
    code = "archive_source_fence_lost"


def _recovery_bounds(
    deadline: float, cancelled: Callable[[], bool] | None
) -> dict[str, object]:
    """Pass recovery bounds down only when the caller actually set any."""
    if deadline == float("inf") and cancelled is None:
        return {}
    return {"deadline": deadline, "cancelled": cancelled}


class DailyArchiver:
    """Check retention policy, publish sealed bags, and recover mixed states."""

    def __init__(
        self,
        vault: Path,
        state_root: Path,
        *,
        clock: Callable[[], datetime] | None = None,
        killpoint: Callable[[str], None] | None = None,
        queue: MemoryQueue | None = None,
        source_heartbeat_seconds: int = DEFAULTS.queue_heartbeat_seconds,
        source_lease_seconds: int = DEFAULTS.queue_lease_seconds,
    ) -> None:
        self.vault = Path(vault).resolve(strict=True)
        self.state_root = Path(state_root)
        self.daily_root = self.vault / "knowledge" / "daily"
        self.archive_root = self.daily_root / "archive"
        self.coordinator = MarkdownCoordinator(self.vault, self.state_root)
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.killpoint = killpoint or (lambda _point: None)
        self.queue = queue or MemoryQueue(self.state_root)
        self.source_heartbeat_seconds = source_heartbeat_seconds
        self.source_lease_seconds = source_lease_seconds

    def eligible(
        self,
        source: Path,
        *,
        hot_days: int = DEFAULT_HOT_DAYS,
        transaction_retention_days: int = DEFAULT_TRANSACTION_RETENTION_DAYS,
    ) -> Eligibility:
        with self.coordinator.writer_gate(wait_seconds=ARCHIVE_WRITER_WAIT_SECONDS):
            return self._eligible(
                source,
                hot_days=hot_days,
                transaction_retention_days=transaction_retention_days,
                ignore_current_writer=True,
            )

    @staticmethod
    def _validate_retention_days(hot_days: object, transaction_retention_days: object) -> None:
        for value in (hot_days, transaction_retention_days):
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise ValueError("retention days must be non-negative integers")

    def _validated_source(self, source: Path) -> Path:
        source = Path(source)
        if source.parent.resolve() != self.daily_root.resolve() or source.suffix != ".md":
            raise ValueError("archive source must be a flat daily Markdown file")
        return source

    def _retention_reasons(self, daily_date: date, hot_days: int) -> list[str]:
        """Today's log and anything inside the hot window stay where they are."""
        today = self.clock().astimezone(timezone.utc).date()
        reasons: list[str] = []
        if daily_date == today:
            reasons.append("today")
        if (today - daily_date).days <= hot_days:
            reasons.append("hot_retention")
        return reasons

    def _read_compile_receipt(self, logical_path: str, digest: str) -> dict[str, object] | None:
        try:
            from compile_memory import read_compile_receipt_v3

            return read_compile_receipt_v3(logical_path, digest, self.coordinator)
        except (OSError, RuntimeError, ValueError):
            return None

    @staticmethod
    def _receipt_binding_reason(receipt: dict[str, object], logical_path: str, digest: str) -> str | None:
        """A receipt must bind this exact logical path and content digest."""
        source = receipt.get("source")
        bound = (
            isinstance(source, dict)
            and source.get("logical_path") == logical_path
            and source.get("sha256") == digest
        )
        if not bound:
            return "compile_receipt_v3_missing"
        if receipt.get("schema_version") != "compile-receipt/v3":
            return "nonterminal_compile_operation"
        return None

    def _receipt_reasons(
        self, logical_path: str, digest: str
    ) -> tuple[dict[str, object] | None, list[str]]:
        reasons: list[str] = []
        operation_state = self._receipt_operation_state(logical_path, digest)
        if operation_state is not None and operation_state != "committed":
            reasons.append("nonterminal_compile_operation")
        receipt = self._read_compile_receipt(logical_path, digest)
        if receipt is None:
            reasons.append("compile_receipt_v3_missing")
            return None, reasons
        reasons.extend(
            self._binding_reasons(receipt, logical_path, digest)
        )
        return receipt, reasons

    def _binding_reasons(
        self, receipt: dict[str, object], logical_path: str, digest: str
    ) -> list[str]:
        binding = self._receipt_binding_reason(receipt, logical_path, digest)
        return [] if binding is None else [binding]

    def _evidence_reasons(self, source: Path, content: bytes, digest: str) -> list[str]:
        """Every evidence block in the daily must still resolve before it moves."""
        try:
            self._resolve_all_blocks(source, content, digest)
        except (OSError, ValueError, EvidenceResolutionError):
            return ["unresolved_evidence"]
        return []

    def _resolve_all_blocks(self, source: Path, content: bytes, digest: str) -> None:
        blocks = daily_entries(content)
        if not blocks:
            raise EvidenceResolutionError("daily has no evidence blocks")
        resolver = EvidenceResolver(self.vault, state_root=self.state_root)
        for block_id, start, end in blocks:
            resolver.resolve(EvidenceRef(source.stem, digest, block_id, start, end))

    def _writer_reason(self, ignore_current_writer: bool) -> list[str]:
        if ignore_current_writer or not self._writer_active():
            return []
        return ["active_writer"]

    def _queue_reasons(
        self,
        source: Path,
        digest: str,
        logical_path: str,
        *,
        skip_queue_database_checks: bool,
    ) -> tuple[tuple[str, ...], list[str]]:
        if skip_queue_database_checks:
            return (), self._legacy_queue_reasons(source, digest)
        blocking = tuple(self._queue_references(source.stem, digest))
        reasons = ["queue_reference"] if blocking else []
        if self.queue.source_failure(logical_path, digest) is not None:
            reasons.append("source_failure")
        return blocking, reasons + self._legacy_queue_reasons(source, digest)

    def _legacy_queue_reasons(self, source: Path, digest: str) -> list[str]:
        if self._legacy_queue_references(source.stem, digest):
            return ["legacy_queue_reference"]
        return []

    def _pin_reasons(self, source: Path, digest: str) -> list[str]:
        """Decision evidence and manual pins that hold the source in place."""
        reasons: list[str] = []
        if self._decision_references(source.stem, digest):
            reasons.append("decision_evidence")
        if self._policy_contains("archive-pins.json", source.stem, digest):
            reasons.append("manual_pin")
        return reasons

    def _receipt_retention_reason(
        self,
        receipt: dict[str, object] | None,
        digest: str,
        transaction_retention_days: int,
    ) -> list[str]:
        if receipt is None:
            return []
        if not self._receipt_transaction_retained(receipt, digest, transaction_retention_days):
            return []
        return ["transaction_retention"]

    def _retained_reasons(
        self,
        source: Path,
        digest: str,
        receipt: dict[str, object] | None,
        transaction_retention_days: int,
    ) -> list[str]:
        """Transactions, decisions, and manual pins that hold the source in place."""
        reasons: list[str] = []
        if self._transaction_references(
            source.name, transaction_retention_days=transaction_retention_days
        ):
            reasons.append("active_transaction")
        reasons.extend(
            self._receipt_retention_reason(receipt, digest, transaction_retention_days)
        )
        return reasons + self._pin_reasons(source, digest)

    def _eligible(
        self,
        source: Path,
        *,
        hot_days: int,
        transaction_retention_days: int,
        ignore_current_writer: bool,
        skip_queue_database_checks: bool = False,
    ) -> Eligibility:
        self._validate_retention_days(hot_days, transaction_retention_days)
        source = self._validated_source(source)
        if DATE_RE.fullmatch(source.stem) is None:
            return Eligibility(False, ("invalid_daily_id",))
        try:
            daily_date = date.fromisoformat(source.stem)
            content = read_stable_bytes(source, MAX_DAILY_BYTES, label="daily archive source")
        except (OSError, ValueError) as exc:
            return Eligibility(False, (f"source:{exc}",))

        digest = sha256_bytes(content)
        logical_path = f"knowledge/daily/{source.name}"
        receipt, receipt_reasons = self._receipt_reasons(logical_path, digest)
        blocking_tasks, queue_reasons = self._queue_reasons(
            source,
            digest,
            logical_path,
            skip_queue_database_checks=skip_queue_database_checks,
        )
        reasons = [
            *self._retention_reasons(daily_date, hot_days),
            *receipt_reasons,
            *self._evidence_reasons(source, content, digest),
            *queue_reasons,
            *self._retained_reasons(source, digest, receipt, transaction_retention_days),
            *self._writer_reason(ignore_current_writer),
        ]
        return Eligibility(
            not reasons,
            tuple(dict.fromkeys(reasons)),
            digest,
            receipt,
            blocking_tasks,
        )

    def _receipt_operation_state(
        self, logical_path: str, digest: str
    ) -> str | None:
        from compile_memory import compile_receipt_path, compile_source_identity

        path = compile_receipt_path(compile_source_identity(logical_path, digest))
        if not path.exists():
            return None
        try:
            text = read_stable_bytes(
                path, MAX_POLICY_BYTES, label="compile receipt"
            ).decode("utf-8", errors="strict")
            record = json.loads(text.split("```json\n", 1)[1].split("\n```", 1)[0])
            operation_id = record["operation_id"]
            if not isinstance(operation_id, str):
                return "invalid"
            with closing(sqlite3.connect(self.coordinator.database_path)) as connection:
                row = connection.execute(
                    'SELECT state FROM "transaction" WHERE operation_id=?',
                    (operation_id,),
                ).fetchone()
            return "missing" if row is None else str(row[0])
        except (
            IndexError,
            KeyError,
            OSError,
            TypeError,
            UnicodeDecodeError,
            ValueError,
            json.JSONDecodeError,
            sqlite3.Error,
        ):
            return "invalid"

    @staticmethod
    def _receipt_expected_paths(receipt: dict[str, object]) -> set[str]:
        """Markdown paths the receipt's own transaction must still carry."""
        identity = receipt.get("source_identity")
        paths = {
            str(item.get("path"))
            for item in receipt.get("operations", [])
            if isinstance(item, dict)
        }
        paths.add(str(identity and f"knowledge/daily/receipts/v3-{identity}.md"))
        return paths

    def _transaction_within_retention(
        self, transaction: object, transaction_retention_days: int
    ) -> bool:
        cutoff = self.clock().astimezone(timezone.utc) - timedelta(
            days=transaction_retention_days
        )
        try:
            updated = datetime.fromisoformat(
                transaction.updated_at.replace("Z", "+00:00")
            )
        except ValueError:
            return True
        return updated >= cutoff

    def _committed_receipt_transaction(self, receipt: dict[str, object]) -> object | None:
        """The committed transaction this receipt names, or None when unusable."""
        operation_id = receipt.get("operation_id")
        if not isinstance(operation_id, str):
            return None
        transaction = self.coordinator._record_for_operation_id(operation_id)
        if transaction is None or transaction.state != "committed":
            return None
        return transaction

    def _receipt_transaction_retained(
        self,
        receipt: dict[str, object],
        digest: str,
        transaction_retention_days: int,
    ) -> bool:
        transaction = self._committed_receipt_transaction(receipt)
        if transaction is None:
            return True
        expected = self._receipt_expected_paths(receipt)
        if not expected.issubset({item.path for item in transaction.operations}):
            return True
        return self._transaction_within_retention(transaction, transaction_retention_days)

    def archive(
        self,
        daily_id: str,
        *,
        hot_days: int = DEFAULT_HOT_DAYS,
        transaction_retention_days: int = DEFAULT_TRANSACTION_RETENTION_DAYS,
    ) -> ArchiveReceipt:
        if DATE_RE.fullmatch(daily_id) is None:
            raise ValueError("daily ID must be YYYY-MM-DD")
        with self.coordinator.writer_gate(wait_seconds=ARCHIVE_WRITER_WAIT_SECONDS):
            return self._archive_under_writer_gate(
                daily_id,
                hot_days=hot_days,
                transaction_retention_days=transaction_retention_days,
            )

    def _archive_under_writer_gate(
        self,
        daily_id: str,
        *,
        hot_days: int,
        transaction_retention_days: int,
    ) -> ArchiveReceipt:
        source = self.daily_root / f"{daily_id}.md"
        self.coordinator.recover()
        source_bytes = read_stable_bytes(
            source, MAX_DAILY_BYTES, label="daily archive source"
        )
        self._recover_hidden_builds()
        reused = self._recover_daily(
            daily_id,
            sha256_bytes(source_bytes),
            hot_days=hot_days,
            transaction_retention_days=transaction_retention_days,
        )
        if reused is not None:
            return reused
        eligibility, content = self._archive_preflight(
            source,
            hot_days=hot_days,
            transaction_retention_days=transaction_retention_days,
        )
        receipt = self._archive_under_fence(
            daily_id,
            content,
            eligibility,
            hot_days=hot_days,
            transaction_retention_days=transaction_retention_days,
        )
        self.rebuild_index()
        return receipt

    def _archive_preflight(
        self,
        source: Path,
        *,
        hot_days: int,
        transaction_retention_days: int,
    ) -> tuple[Eligibility, bytes]:
        """Prove the daily is eligible and still byte-identical before any write."""
        eligibility = self._eligible(
            source,
            hot_days=hot_days,
            transaction_retention_days=transaction_retention_days,
            ignore_current_writer=True,
        )
        if not eligibility.eligible or eligibility.source_sha256 is None:
            raise ValueError(
                "daily is not archive eligible: " + ", ".join(eligibility.reasons)
            )
        content = read_stable_bytes(source, MAX_DAILY_BYTES, label="daily archive source")
        if sha256_bytes(content) != eligibility.source_sha256:
            raise RuntimeError("daily source changed during archive preflight")
        return eligibility, content

    def _release_source_fence(self, queue: MemoryQueue, fence: SourceFence) -> None:
        had_error = sys.exc_info()[0] is not None
        try:
            queue.release_source_fence(fence.token)
        except QueueOperationError:
            if not had_error:
                raise ArchiveFenceConflict(
                    "archive source fence was lost before release"
                )

    def _archive_under_fence(
        self,
        daily_id: str,
        content: bytes,
        eligibility: Eligibility,
        *,
        hot_days: int,
        transaction_retention_days: int,
    ) -> ArchiveReceipt:
        queue = self.queue
        fence = queue.acquire_source_fence(
            daily_id,
            eligibility.source_sha256,
            lease_seconds=self.source_lease_seconds,
        )
        try:
            final_bag = self._archive_with_heartbeat(
                queue,
                fence,
                daily_id,
                content,
                eligibility,
                hot_days=hot_days,
                transaction_retention_days=transaction_retention_days,
            )
        finally:
            self._release_source_fence(queue, fence)
        return ArchiveReceipt(daily_id, eligibility.source_sha256, final_bag, "archived")

    def _archive_with_heartbeat(
        self,
        queue: MemoryQueue,
        fence: SourceFence,
        daily_id: str,
        content: bytes,
        eligibility: Eligibility,
        *,
        hot_days: int,
        transaction_retention_days: int,
    ) -> Path:
        try:
            with queue.source_fence_heartbeat(
                fence,
                heartbeat_seconds=self.source_heartbeat_seconds,
                lease_seconds=self.source_lease_seconds,
            ) as heartbeat:
                self._require_no_source_failure(
                    queue, daily_id, eligibility.source_sha256
                )
                return self._build_and_publish(
                    queue,
                    heartbeat,
                    daily_id,
                    content,
                    eligibility,
                    hot_days=hot_days,
                    transaction_retention_days=transaction_retention_days,
                )
        except QueueOperationError as exc:
            if exc.code == "source_fence_lost":
                raise ArchiveFenceConflict(
                    "archive source fence heartbeat was lost"
                ) from exc
            raise

    def _require_no_source_failure(
        self, queue: MemoryQueue, daily_id: str, digest: str
    ) -> None:
        if queue.source_failure(f"knowledge/daily/{daily_id}.md", digest) is not None:
            raise ValueError("daily is not archive eligible: source_failure")

    def _revalidate_published(self, final_bag: Path, digest: str) -> None:
        validated = validate_bag(
            final_bag, coordinator=self.coordinator, vault=self.vault
        )
        if validated.manifest["source_hash"] != digest:
            raise RuntimeError("published archive failed source revalidation")

    def _build_and_publish(
        self,
        queue: MemoryQueue,
        heartbeat: object,
        daily_id: str,
        content: bytes,
        eligibility: Eligibility,
        *,
        hot_days: int,
        transaction_retention_days: int,
    ) -> Path:
        final_bag, publish_build = self._build_bag(
            daily_id, content, eligibility, hot_days=hot_days
        )
        published = False
        try:
            self.killpoint("after_build")
            heartbeat.refresh()
            self.killpoint("before_publish_rename")
            self._prepare_build_for_publish(publish_build)
            heartbeat.refresh()
            self._publish_build(publish_build, final_bag)
            published = True
            fsync_directory(final_bag.parent)
            self.killpoint("after_publish_rename")
            self._revalidate_published(final_bag, eligibility.source_sha256)
            self.killpoint("after_revalidate")
            self._remove_flat_under_finalization(
                queue,
                heartbeat.refresh(),
                hot_days=hot_days,
                transaction_retention_days=transaction_retention_days,
            )
            self.killpoint("after_source_delete")
        except BaseException:
            if not published:
                self._remove_build(publish_build)
            raise
        return final_bag

    def _classify_published_bags(
        self, daily_id: str, digest: str
    ) -> tuple[list[Path], list[Path]]:
        """Split published bags for this daily into exact matches and conflicts."""
        exact: list[Path] = []
        conflicts: list[Path] = []
        for path in self._archive_paths(hidden=False):
            bag = validate_bag(path, coordinator=self.coordinator, vault=self.vault)
            if bag.manifest["logical_daily_id"] != daily_id:
                continue
            self._sort_published_bag(path, bag, daily_id, digest, exact, conflicts)
        return exact, conflicts

    def _sort_published_bag(
        self,
        path: Path,
        bag: object,
        daily_id: str,
        digest: str,
        exact: list[Path],
        conflicts: list[Path],
    ) -> None:
        if bag.manifest["source_hash"] == digest:
            exact.append(path)
            return
        self._quarantine(path, daily_id, str(bag.manifest["source_hash"]))
        conflicts.append(path)

    def _keep_one_exact_bag(self, exact: list[Path], daily_id: str, digest: str) -> Path:
        """One published bag survives; identical duplicates go to quarantine."""
        for duplicate in exact[1:]:
            self._quarantine(duplicate, daily_id, digest, reason="duplicate_exact_match")
        return exact[0]

    def _flat_still_matches(self, daily_id: str, digest: str) -> bool:
        flat = self.daily_root / f"{daily_id}.md"
        flat_bytes = read_stable_bytes(flat, MAX_DAILY_BYTES, label="daily duplicate")
        return sha256_bytes(flat_bytes) == digest

    def _recovered_keeper_still_matches(
        self, keeper: object, daily_id: str, digest: str
    ) -> bool:
        """A flat source that changed under a published bag is quarantined, not kept."""
        if self._flat_still_matches(daily_id, digest):
            return True
        self._quarantine(keeper, daily_id, digest)
        return False

    def _recover_daily(
        self,
        daily_id: str,
        digest: str,
        *,
        hot_days: int,
        transaction_retention_days: int,
    ) -> ArchiveReceipt | None:
        exact, conflicts = self._classify_published_bags(daily_id, digest)
        if conflicts:
            raise ArchiveConflict(
                f"published archive conflicts with flat source daily:{daily_id}"
            )
        if not exact:
            return None
        return self._recover_from_published_bag(
            exact,
            daily_id,
            digest,
            hot_days=hot_days,
            transaction_retention_days=transaction_retention_days,
        )

    def _recover_from_published_bag(
        self,
        exact: list,
        daily_id: str,
        digest: str,
        *,
        hot_days: int,
        transaction_retention_days: int,
    ) -> ArchiveReceipt | None:
        keeper = self._keep_one_exact_bag(exact, daily_id, digest)
        if not self._recovered_keeper_still_matches(keeper, daily_id, digest):
            return None
        self._remove_flat_after_eligibility_recheck(
            daily_id,
            digest,
            hot_days=hot_days,
            transaction_retention_days=transaction_retention_days,
        )
        self.rebuild_index()
        return ArchiveReceipt(daily_id, digest, keeper, "recovered")

    def _month_entry_names(self, month: Path) -> set[str]:
        if not month.exists():
            return set()
        return {
            entry.name
            for entry in bounded_directory_entries(
                month, MAX_ARCHIVE_ENTRIES, label="daily archive month"
            )
        }

    def _remove_new_builds(self, month: Path, before: set[str]) -> None:
        """Drop only the build directories this attempt created."""
        for name in self._month_entry_names(month) - before:
            if ".building-" in name:
                self._remove_build(month / name)

    def _build_bag(
        self,
        daily_id: str,
        content: bytes,
        eligibility: Eligibility,
        *,
        hot_days: int,
    ) -> tuple[Path, Path]:
        month = self.archive_root / daily_id[:7]
        before = self._month_entry_names(month)
        try:
            return self._build_bag_contents(
                daily_id, content, eligibility, hot_days=hot_days
            )
        except BaseException:
            self._remove_new_builds(month, before)
            raise

    def _ensure_month_directory(self, daily_id: str) -> Path:
        month = self.archive_root / daily_id[:7]
        self._ensure_archive_root()
        if not month.exists():
            month.mkdir()
            _regular_directory(month, label="daily archive month")
            fsync_directory(self.archive_root)
            _harden_owner_only(month, 0o700)
            return month
        _regular_directory(month, label="daily archive month")
        _harden_owner_only(month, 0o700)
        return month

    def _start_publish_build(
        self,
        month: Path,
        daily_id: str,
        nonce: str,
        final_bag: Path,
        now: datetime,
        content: bytes,
    ) -> Path:
        """Create the hidden build directory and its recoverable intent record."""
        publish_build = month / f".bag-{daily_id}.building-{nonce}"
        publish_build.mkdir()
        _harden_owner_only(publish_build, 0o700)
        fsync_directory(month)
        intent = {
            "created_at": now.isoformat().replace("+00:00", "Z"),
            "final_bag_name": final_bag.name,
            "logical_daily_id": daily_id,
            "schema_version": "archive-build-intent/v1",
            "source_hash": sha256_bytes(content),
        }
        (publish_build / "build-intent.json").write_bytes(canonical_json_bytes(intent))
        return publish_build

    def _write_payload_and_tags(
        self, publish_build: Path, daily_id: str, content: bytes, now: datetime
    ) -> str:
        """Write the BagIt payload and its tag files; return the payload digest."""
        data = publish_build / "data"
        data.mkdir()
        _set_owner_only(data, 0o700)
        fsync_directory(publish_build)
        payload_name = f"data/{daily_id}.md"
        (publish_build / payload_name).write_bytes(content)
        digest = sha256_bytes(content)
        bag_info = (
            f"Bagging-Date: {now.date().isoformat()}\n"
            f"Payload-Oxum: {len(content)}.1\n"
            f"External-Identifier: daily:{daily_id}\n"
        ).encode()
        (publish_build / "bagit.txt").write_bytes(
            b"BagIt-Version: 1.0\nTag-File-Character-Encoding: UTF-8\n"
        )
        (publish_build / "bag-info.txt").write_bytes(bag_info)
        (publish_build / "manifest-sha256.txt").write_bytes(
            f"{digest}  {payload_name}\n".encode()
        )
        return digest

    def _committed_compile_transaction(self, receipt: dict[str, object]) -> object:
        transaction = self.coordinator._record_for_operation_id(
            str(receipt["operation_id"])
        )
        if transaction is None or transaction.state != "committed":
            raise RuntimeError("compile transaction authority is not committed")
        return transaction

    def _commit_sequence(self, transaction: object) -> int:
        with self.coordinator._connect() as database:
            row = database.execute(
                'SELECT rowid AS commit_sequence FROM "transaction" WHERE id=?',
                (transaction.id,),
            ).fetchone()
        if row is None:
            raise RuntimeError("compile transaction authority disappeared")
        return int(row["commit_sequence"])

    def _compile_authority(
        self, eligibility: Eligibility, daily_id: str, digest: str
    ) -> _CompileAuthority:
        """Re-prove the compile receipt and its committed transaction at build time."""
        receipt = eligibility.receipt
        assert receipt is not None
        logical_path = f"knowledge/daily/{daily_id}.md"
        from compile_memory import (
            compile_receipt_path,
            compile_source_identity,
            read_compile_receipt_v3,
        )

        source_identity = compile_source_identity(logical_path, digest)
        receipt_path = compile_receipt_path(source_identity)
        receipt_bytes = read_stable_bytes(
            receipt_path, MAX_POLICY_BYTES, label="compile receipt"
        )
        authoritative_receipt = read_compile_receipt_v3(
            logical_path,
            digest,
            self.coordinator,
            path=receipt_path,
            vault=self.vault,
        )
        if authoritative_receipt != receipt:
            raise RuntimeError("compile receipt authority changed during archive")
        transaction = self._committed_compile_transaction(receipt)
        return _CompileAuthority(
            receipt_bytes=receipt_bytes,
            receipt_path=receipt_path,
            logical_path=logical_path,
            source_identity=source_identity,
            operation_id=str(receipt["operation_id"]),
            attestation=compile_authority_attestation(
                transaction, self._commit_sequence(transaction)
            ),
        )

    @staticmethod
    def _evidence_entries(content: bytes) -> list[dict[str, object]]:
        return [
            {
                "block_id": block_id,
                "byte_start": start,
                "byte_end": end,
                "line_start": _line_span(content, start, end)[0],
                "line_end": _line_span(content, start, end)[1],
                "sha256": sha256_bytes(content[start:end]),
            }
            for block_id, start, end in daily_entries(content)
        ]

    @staticmethod
    def _write_tag_manifest(publish_build: Path) -> None:
        tag_names = (
            "archive-manifest.json",
            "bag-info.txt",
            "bagit.txt",
            "compile-receipt.md",
            "manifest-sha256.txt",
        )
        (publish_build / "tagmanifest-sha256.txt").write_bytes(
            "".join(
                f"{sha256_bytes((publish_build / name).read_bytes())}  {name}\n"
                for name in tag_names
            ).encode()
        )

    def _write_manifest(
        self,
        publish_build: Path,
        daily_id: str,
        content: bytes,
        digest: str,
        authority: _CompileAuthority,
        eligibility: Eligibility,
        now: datetime,
        *,
        hot_days: int,
    ) -> None:
        (publish_build / "compile-receipt.md").write_bytes(authority.receipt_bytes)
        manifest = {
            "schema_version": "archive-manifest/v1",
            "logical_daily_id": daily_id,
            "original_path": f"knowledge/daily/{daily_id}.md",
            "source_hash": digest,
            "payload_hash": digest,
            "compile_receipt_ref": {
                "schema": "compile-receipt-ref/v1",
                "path": authority.receipt_path.relative_to(self.vault).as_posix(),
                "logical_path": authority.logical_path,
                "source_digest": digest,
                "source_identity": authority.source_identity,
                "receipt_file_hash": sha256_bytes(authority.receipt_bytes),
                "embedded_path": "compile-receipt.md",
            },
            "compile_authority": authority.attestation,
            "queue_preflight": {
                "checked_at": now.isoformat().replace("+00:00", "Z"),
                "passed": True,
                "blocking_task_ids": list(eligibility.blocking_task_ids),
            },
            "operations": [
                {"operation_id": authority.operation_id, "state": "succeeded"}
            ],
            "evidence": self._evidence_entries(content),
            "pins": [],
            "retention_days": hot_days,
        }
        (publish_build / "archive-manifest.json").write_bytes(
            canonical_json_bytes(manifest)
        )
        self._write_tag_manifest(publish_build)

    def _finalize_build(self, publish_build: Path) -> None:
        """Harden, flush, seal, and validate the finished build directory."""
        for path in sorted(
            item for item in self._bounded_tree(publish_build) if item.is_file()
        ):
            _set_owner_only(path, 0o600)
            fsync_file(path)
        fsync_directory(publish_build / "data")
        fsync_directory(publish_build)
        self._seal(publish_build)
        validate_bag(
            publish_build,
            coordinator=self.coordinator,
            vault=self.vault,
            allow_build_intent=True,
        )

    def _build_bag_contents(
        self,
        daily_id: str,
        content: bytes,
        eligibility: Eligibility,
        *,
        hot_days: int,
    ) -> tuple[Path, Path]:
        now = self.clock().astimezone(timezone.utc)
        month = self._ensure_month_directory(daily_id)
        nonce = uuid.uuid4().hex
        stamp = now.strftime("%Y%m%dT%H%M%S%fZ")
        final_bag = month / f"bag-{stamp}-{daily_id}-{nonce}"
        publish_build = self._start_publish_build(
            month, daily_id, nonce, final_bag, now, content
        )
        digest = self._write_payload_and_tags(publish_build, daily_id, content, now)
        authority = self._compile_authority(eligibility, daily_id, digest)
        self._write_manifest(
            publish_build,
            daily_id,
            content,
            digest,
            authority,
            eligibility,
            now,
            hot_days=hot_days,
        )
        self._finalize_build(publish_build)
        return final_bag, publish_build

    def _decode_build_intent(self, build: Path) -> dict[str, object]:
        raw = read_stable_bytes(
            build / "build-intent.json", MAX_POLICY_BYTES, label="archive build intent"
        )
        value = json.loads(raw.decode("utf-8", errors="strict"))
        if canonical_json_bytes(value) != raw or not isinstance(value, dict):
            raise ValueError("archive build intent is not canonical")
        return value

    @staticmethod
    def _require_build_intent_fields(value: dict[str, object]) -> None:
        if set(value) != BUILD_INTENT_FIELDS:
            raise ValueError("archive build intent fields are invalid")
        if value.get("schema_version") != "archive-build-intent/v1":
            raise ValueError("archive build intent fields are invalid")

    @staticmethod
    def _require_build_intent_identity(value: dict[str, object]) -> None:
        daily_id = str(value["logical_daily_id"])
        final_pattern = rf"bag-[A-Za-z0-9-]+-{daily_id}-[0-9a-f]{{32}}"
        named = (
            DATE_RE.fullmatch(daily_id) is not None
            and re.fullmatch(r"[0-9a-f]{64}", str(value["source_hash"])) is not None
            and re.fullmatch(final_pattern, str(value["final_bag_name"])) is not None
        )
        if not named:
            raise ValueError("archive build intent identity is invalid")

    def _read_build_intent(self, build: Path) -> dict[str, str]:
        value = self._decode_build_intent(build)
        self._require_build_intent_fields(value)
        self._require_build_intent_identity(value)
        return {key: str(value[key]) for key in BUILD_INTENT_FIELDS}

    def _prepare_build_for_publish(self, build: Path) -> dict[str, str]:
        intent = self._read_build_intent(build)
        validate_bag(
            build,
            coordinator=self.coordinator,
            vault=self.vault,
            allow_build_intent=True,
        )
        self._unseal_root(build)
        if os.name == "nt":
            _harden_owner_only(build / "build-intent.json", 0o600)
        else:
            (build / "build-intent.json").chmod(0o600)
        (build / "build-intent.json").unlink()
        fsync_directory(build)
        self._set_archive_read_only(build)
        validate_bag(build, coordinator=self.coordinator, vault=self.vault)
        return intent

    @staticmethod
    def _unseal_root(build: Path) -> None:
        """Make only the package root writable; every member stays read-only."""
        if os.name == "nt":
            _harden_owner_only(build, 0o700)
            return
        build.chmod(0o700)

    def _publish_build(self, build: Path, final: Path) -> None:
        """Rename a sealed build into place, then seal it under its final name.

        `rename(2)` returns EACCES when "oldpath is a directory and does not
        allow write permission (needed to update the `..` entry)". POSIX makes
        that a *may*, so it is implementation-defined: macOS enforces it, Linux
        does not when the parent is unchanged. The build root is sealed at
        0o500, so publication failed on macOS only - and in recovery that
        failure is caught as OSError, which deletes the interrupted build
        instead of finishing it. The archive is immutable evidence; losing one
        to a permission bit is worse than the test failures that exposed it.

        The root is writable for exactly one rename and is sealed again
        immediately, or restored if the rename fails so a later recovery pass
        still finds a valid package.
        """
        self._unseal_root(build)
        try:
            build.replace(final)
        except BaseException:
            self._set_archive_read_only(build)
            raise
        self._set_archive_read_only(final)

    @staticmethod
    def _recovery_stopped(
        deadline: float, cancelled: Callable[[], bool] | None
    ) -> bool:
        return time.monotonic() >= deadline or bool(cancelled and cancelled())

    def _require_recovery_active(
        self, deadline: float, cancelled: Callable[[], bool] | None
    ) -> None:
        if self._recovery_stopped(deadline, cancelled):
            raise TimeoutError("archive recovery cancelled or deadline reached")

    def _require_safe_hidden_build(
        self, build: Path, deadline: float, cancelled: Callable[[], bool] | None
    ) -> None:
        try:
            self._bounded_tree(build)
        except (OSError, PermissionError, ValueError) as exc:
            self._require_recovery_active(deadline, cancelled)
            self._quarantine_hidden_build(build, "unsafe_hidden_build")
            raise ArchiveConflict("unsafe hidden archive build") from exc

    def _publish_recovered_build(
        self, build: Path, deadline: float, cancelled: Callable[[], bool] | None
    ) -> None:
        intent = self._read_build_intent(build)
        validate_bag(
            build,
            coordinator=self.coordinator,
            vault=self.vault,
            allow_build_intent=True,
        )
        final = build.parent / intent["final_bag_name"]
        if final.exists():
            self._require_recovery_active(deadline, cancelled)
            self._remove_build(build)
            return
        self._require_recovery_active(deadline, cancelled)
        self._prepare_build_for_publish(build)
        self._require_recovery_active(deadline, cancelled)
        self._publish_build(build, final)
        fsync_directory(final.parent)

    def _recover_hidden_build(
        self, build: Path, deadline: float, cancelled: Callable[[], bool] | None
    ) -> None:
        try:
            self._publish_recovered_build(build, deadline, cancelled)
        except (OSError, TypeError, ValueError, EvidenceResolutionError):
            self._require_recovery_active(deadline, cancelled)
            self._remove_build(build)

    def _recover_hidden_builds(
        self,
        *,
        deadline: float = float("inf"),
        cancelled: Callable[[], bool] | None = None,
    ) -> None:
        for build in self._archive_paths(hidden=True):
            if self._recovery_stopped(deadline, cancelled):
                raise TimeoutError("archive recovery cancelled or deadline reached")
            self._require_safe_hidden_build(build, deadline, cancelled)
            self._recover_hidden_build(build, deadline, cancelled)

    def _quarantine_directory(self, name: str) -> tuple[Path, Path]:
        """The quarantine root and one of its child stores, created on demand."""
        root = self.state_root / "run" / "archive-quarantine"
        child = root / name
        if not root.exists():
            root.mkdir(parents=True)
            _harden_owner_only(root, 0o700)
            fsync_directory(root.parent)
        _regular_directory(root, label="archive quarantine root")
        if not child.exists():
            child.mkdir()
            _harden_owner_only(child, 0o700)
            fsync_directory(root)
        _regular_directory(child, label=f"archive quarantine {name}")
        return root, child

    @staticmethod
    def _write_quarantine_record(root: Path, prefix: str, record: dict[str, str]) -> None:
        path = root / f"{prefix}{sha256_bytes(canonical_json_bytes(record))}.json"
        path.write_bytes(canonical_json_bytes(record))
        _harden_owner_only(path, 0o600)
        fsync_file(path)
        fsync_directory(root)

    def _move_quarantined_build(self, build: Path, builds: Path) -> Path:
        """Move a build into quarantine, staying on this volume if it must."""
        destination = builds / f"{build.name}-{uuid.uuid4().hex}"
        try:
            self._publish_build(build, destination)
            return destination
        except OSError as exc:
            if exc.errno != errno.EXDEV:
                raise
        fallback = build.parent / f".quarantined-build-{uuid.uuid4().hex}"
        self._publish_build(build, fallback)
        return fallback

    def _quarantine_hidden_build(self, build: Path, reason: str) -> None:
        root, builds = self._quarantine_directory("builds")
        original = build.relative_to(self.vault).as_posix()
        parent = build.parent
        destination = self._move_quarantined_build(build, builds)
        fsync_directory(parent)
        fsync_directory(destination.parent)
        self._write_quarantine_record(
            root,
            "build-",
            {
                "original_path": original,
                "quarantine_path": str(destination),
                "reason": reason,
            },
        )

    def _remove_flat(
        self,
        daily_id: str,
        digest: str,
        *,
        deadline: float = float("inf"),
        cancelled: Callable[[], bool] | None = None,
    ) -> None:
        if self._recovery_stopped(deadline, cancelled):
            raise TimeoutError("archive recovery cancelled or deadline reached")
        relative = f"knowledge/daily/{daily_id}.md"
        bounds = _recovery_bounds(deadline, cancelled)
        transaction = self.coordinator.prepare(
            [MarkdownChange.delete(relative, max_before_bytes=MAX_DAILY_BYTES)],
            operation_id=f"archive-remove:{daily_id}:{digest}",
            preconditions={relative: digest},
            **bounds,
        )
        self.coordinator.apply(transaction.id, **bounds)

    def _remove_flat_with_heartbeat(
        self,
        queue: MemoryQueue,
        fence: SourceFence,
        *,
        hot_days: int,
        transaction_retention_days: int,
        deadline: float,
        cancelled: Callable[[], bool] | None,
    ) -> None:
        try:
            with queue.source_fence_heartbeat(
                fence,
                heartbeat_seconds=self.source_heartbeat_seconds,
                lease_seconds=self.source_lease_seconds,
            ) as heartbeat:
                self._remove_flat_under_finalization(
                    queue,
                    heartbeat.refresh(),
                    hot_days=hot_days,
                    transaction_retention_days=transaction_retention_days,
                    deadline=deadline,
                    cancelled=cancelled,
                )
        except QueueOperationError as exc:
            if exc.code == "source_fence_lost":
                raise ArchiveFenceConflict(
                    "archive source fence heartbeat was lost"
                ) from exc
            raise

    def _remove_flat_after_eligibility_recheck(
        self,
        daily_id: str,
        digest: str,
        *,
        hot_days: int,
        transaction_retention_days: int,
        deadline: float = float("inf"),
        cancelled: Callable[[], bool] | None = None,
    ) -> None:
        if self._recovery_stopped(deadline, cancelled):
            raise TimeoutError("archive recovery cancelled or deadline reached")
        queue = self.queue
        fence = queue.acquire_source_fence(
            daily_id, digest, lease_seconds=self.source_lease_seconds
        )
        try:
            self._remove_flat_with_heartbeat(
                queue,
                fence,
                hot_days=hot_days,
                transaction_retention_days=transaction_retention_days,
                deadline=deadline,
                cancelled=cancelled,
            )
        finally:
            self._release_source_fence(queue, fence)

    def _require_removable_source(
        self,
        fence: SourceFence,
        *,
        hot_days: int,
        transaction_retention_days: int,
    ) -> None:
        """The source must still be the fenced bytes and still be eligible."""
        eligibility = self._eligible(
            self.daily_root / f"{fence.daily_id}.md",
            hot_days=hot_days,
            transaction_retention_days=transaction_retention_days,
            ignore_current_writer=True,
            skip_queue_database_checks=True,
        )
        if eligibility.source_sha256 != fence.source_digest:
            raise RuntimeError("daily source changed before recovery removal")
        if not eligibility.eligible:
            raise ValueError(
                "daily is not archive eligible: " + ", ".join(eligibility.reasons)
            )

    def _remove_flat_under_finalization(
        self,
        queue: MemoryQueue,
        fence: SourceFence,
        *,
        hot_days: int,
        transaction_retention_days: int,
        deadline: float = float("inf"),
        cancelled: Callable[[], bool] | None = None,
    ) -> None:
        if self._recovery_stopped(deadline, cancelled):
            raise TimeoutError("archive recovery cancelled or deadline reached")
        try:
            with queue.source_finalization(fence):
                self._require_removable_source(
                    fence,
                    hot_days=hot_days,
                    transaction_retention_days=transaction_retention_days,
                )
                self._remove_flat(
                    fence.daily_id,
                    fence.source_digest,
                    deadline=deadline,
                    cancelled=cancelled,
                )
        except QueueOperationError as exc:
            if exc.code in {"source_failure", "source_referenced"}:
                raise ValueError(f"daily is not archive eligible: {exc.code}") from exc
            raise

    def _group_published_bags(
        self, deadline: float, cancelled: Callable[[], bool] | None
    ) -> dict[tuple[str, str], list[Path]]:
        """Published bags grouped by the (daily, digest) identity they claim."""
        grouped: dict[tuple[str, str], list[Path]] = {}
        for path in self._archive_paths(hidden=False):
            if self._recovery_stopped(deadline, cancelled):
                raise TimeoutError("archive recovery cancelled or deadline reached")
            bag = validate_bag(path, coordinator=self.coordinator, vault=self.vault)
            identity = (
                str(bag.manifest["logical_daily_id"]),
                str(bag.manifest["source_hash"]),
            )
            grouped.setdefault(identity, []).append(path)
        return grouped

    def _quarantine_all(
        self,
        paths: list[Path],
        daily_id: str,
        digest: str,
        deadline: float,
        cancelled: Callable[[], bool] | None,
    ) -> list[ArchiveReceipt]:
        receipts: list[ArchiveReceipt] = []
        for path in paths:
            self._require_recovery_active(deadline, cancelled)
            self._quarantine(path, daily_id, digest)
            receipts.append(ArchiveReceipt(daily_id, digest, path, "quarantined"))
        return receipts

    def _recover_group(
        self,
        daily_id: str,
        digest: str,
        paths: list[Path],
        *,
        hot_days: int,
        transaction_retention_days: int,
        deadline: float,
        cancelled: Callable[[], bool] | None,
    ) -> list[ArchiveReceipt]:
        """Reconcile one published identity against the flat daily still on disk."""
        flat = self.daily_root / f"{daily_id}.md"
        if not flat.exists():
            return []
        flat_bytes = read_stable_bytes(flat, MAX_DAILY_BYTES, label="daily duplicate")
        if sha256_bytes(flat_bytes) != digest:
            return self._quarantine_all(paths, daily_id, digest, deadline, cancelled)
        keeper = paths[0]
        for duplicate in paths[1:]:
            self._require_recovery_active(deadline, cancelled)
            self._quarantine(duplicate, daily_id, digest, reason="duplicate_exact_match")
        self._remove_flat_after_eligibility_recheck(
            daily_id,
            digest,
            hot_days=hot_days,
            transaction_retention_days=transaction_retention_days,
            deadline=deadline,
            cancelled=cancelled,
        )
        return [ArchiveReceipt(daily_id, digest, keeper, "recovered")]

    def _recover_groups(
        self,
        grouped: dict[tuple[str, str], list[Path]],
        *,
        hot_days: int,
        transaction_retention_days: int,
        deadline: float,
        cancelled: Callable[[], bool] | None,
    ) -> list[ArchiveReceipt]:
        recovered: list[ArchiveReceipt] = []
        for (daily_id, digest), paths in sorted(grouped.items()):
            if self._recovery_stopped(deadline, cancelled):
                raise TimeoutError("archive recovery cancelled or deadline reached")
            recovered.extend(
                self._recover_group(
                    daily_id,
                    digest,
                    paths,
                    hot_days=hot_days,
                    transaction_retention_days=transaction_retention_days,
                    deadline=deadline,
                    cancelled=cancelled,
                )
            )
        return recovered

    def recover(
        self,
        *,
        hot_days: int = DEFAULT_HOT_DAYS,
        transaction_retention_days: int = DEFAULT_TRANSACTION_RETENTION_DAYS,
        deadline: float = float("inf"),
        cancelled: Callable[[], bool] | None = None,
    ) -> list[ArchiveReceipt]:
        if not self.archive_root.exists():
            return []
        _regular_directory(self.archive_root, label="daily archive root")
        with self.coordinator.writer_gate(wait_seconds=ARCHIVE_WRITER_WAIT_SECONDS):
            self._recover_hidden_builds(deadline=deadline, cancelled=cancelled)
            recovered = self._recover_groups(
                self._group_published_bags(deadline, cancelled),
                hot_days=hot_days,
                transaction_retention_days=transaction_retention_days,
                deadline=deadline,
                cancelled=cancelled,
            )
            self.rebuild_index(deadline=deadline, cancelled=cancelled)
        return recovered

    def rebuild_index(
        self,
        *,
        deadline: float = float("inf"),
        cancelled: Callable[[], bool] | None = None,
    ) -> Path:
        self._ensure_archive_root()
        bags = []
        for path in self._archive_paths(hidden=False):
            if self._recovery_stopped(deadline, cancelled):
                raise TimeoutError("archive index rebuild cancelled or deadline reached")
            bag = validate_bag(path, coordinator=self.coordinator, vault=self.vault)
            bags.append(
                {
                    "bag_path": path.relative_to(self.vault).as_posix(),
                    "logical_daily_id": bag.manifest["logical_daily_id"],
                    "source_hash": bag.manifest["source_hash"],
                }
            )
        index = self.archive_root / "archive-index.json"
        temporary = self.archive_root / f".archive-index-{uuid.uuid4().hex}.tmp"
        temporary.write_bytes(
            canonical_json_bytes({"schema_version": "archive-index/v1", "bags": bags})
        )
        _harden_owner_only(temporary, 0o600)
        fsync_file(temporary)
        self._require_recovery_active(deadline, cancelled)
        temporary.replace(index)
        fsync_directory(self.archive_root)
        return index

    def _ensure_archive_root(self) -> None:
        _regular_directory(self.daily_root, label="daily root")
        if self.archive_root.exists():
            _regular_directory(self.archive_root, label="daily archive root")
        else:
            self.archive_root.mkdir()
            _regular_directory(self.archive_root, label="daily archive root")
            fsync_directory(self.daily_root)
        _harden_owner_only(self.archive_root, 0o700)

    @staticmethod
    def _is_archive_entry(name: str, *, hidden: bool) -> bool:
        if hidden:
            return ".building-" in name
        return name.startswith("bag-")

    def _month_archive_paths(self, month: Path, *, hidden: bool) -> list[Path]:
        _regular_directory(month, label="daily archive month")
        entries = bounded_directory_entries(
            month, MAX_ARCHIVE_ENTRIES, label="daily archive month"
        )
        found: list[Path] = []
        for entry in sorted(entries):
            if not self._is_archive_entry(entry.name, hidden=hidden):
                continue
            _regular_directory(entry, label="daily archive bag")
            found.append(entry)
        return found

    def _archive_months(self) -> list[Path]:
        months = bounded_directory_entries(
            self.archive_root, MAX_ARCHIVE_MONTHS, label="daily archive root"
        )
        return [
            month
            for month in sorted(months)
            if re.fullmatch(r"\d{4}-\d{2}", month.name) is not None
        ]

    def _archive_paths(self, *, hidden: bool) -> list[Path]:
        self._ensure_archive_root()
        paths: list[Path] = []
        for month in self._archive_months():
            paths.extend(self._month_archive_paths(month, hidden=hidden))
        return paths

    def _queue_references(self, daily_id: str, digest: str) -> list[str]:
        try:
            return list(self.queue.referencing_source_tasks(daily_id, digest))
        except (OSError, QueueOperationError, sqlite3.Error):
            return ["queue-unreadable"]

    def _legacy_queue_entries(self, legacy: Path) -> list[Path] | None:
        """Legacy task files, or None when the directory cannot be trusted."""
        try:
            entries = bounded_directory_entries(
                legacy, MAX_ARCHIVE_ENTRIES, label="legacy queue directory"
            )
        except (OSError, ValueError):
            return None
        return sorted(
            entry for entry in entries if entry.suffix in {".json", ".processing"}
        )

    @staticmethod
    def _legacy_task_mentions(path: Path, markers: tuple[bytes, ...]) -> bool:
        """An unreadable legacy task counts as a reference, never as an absence."""
        try:
            raw = read_stable_bytes(path, MAX_POLICY_BYTES, label="legacy queue task")
        except (OSError, ValueError):
            return True
        return any(marker in raw for marker in markers)

    def _legacy_queue_references(self, daily_id: str, digest: str) -> bool:
        legacy = self.state_root / "run" / "queue"
        if not legacy.exists():
            return False
        entries = self._legacy_queue_entries(legacy)
        if entries is None:
            return True
        markers = (daily_id.encode(), digest.encode())
        return any(self._legacy_task_mentions(path, markers) for path in entries)

    def _transaction_rows(self, source_name: str) -> list[tuple[object, object]] | None:
        """Transaction states touching this daily, or None when unreadable."""
        relative = f"knowledge/daily/{source_name}"
        try:
            with closing(sqlite3.connect(self.coordinator.database_path)) as connection:
                return connection.execute(
                    'SELECT t.state, t.updated_at FROM "transaction" t '
                    'JOIN "operation" o ON o.transaction_id=t.id WHERE o.path=?',
                    (relative,),
                ).fetchall()
        except sqlite3.Error:
            return None

    @staticmethod
    def _transaction_row_holds(state_name: object, updated_at: object, cutoff: datetime) -> bool:
        if state_name not in {"committed", "discarded"}:
            return True
        if state_name != "committed":
            return False
        updated = datetime.fromisoformat(str(updated_at).replace("Z", "+00:00"))
        return updated >= cutoff

    def _transaction_references(
        self, source_name: str, *, transaction_retention_days: int
    ) -> bool:
        rows = self._transaction_rows(source_name)
        if rows is None:
            return True
        cutoff = self.clock().astimezone(timezone.utc) - timedelta(
            days=transaction_retention_days
        )
        return any(
            self._transaction_row_holds(state_name, updated_at, cutoff)
            for state_name, updated_at in rows
        )

    def _writer_active(self) -> bool:
        try:
            with self.coordinator._connect() as connection:
                row = connection.execute(
                    "SELECT * FROM writer_owners WHERE gate_name='global'"
                ).fetchone()
            return row is not None and not self.coordinator._writer_owner_reclaimable(row)
        except sqlite3.Error:
            return True

    def _note_paths(self, notes: Path) -> list[Path] | None:
        try:
            return sorted(
                path
                for path in self._bounded_tree(notes)
                if path.suffix == ".md" and path.is_file()
            )
        except (OSError, ValueError):
            return None

    @staticmethod
    def _decision_page_cites(path: Path, markers: tuple[str, ...]) -> bool:
        """An unreadable page counts as a citation, never as an absence."""
        try:
            raw = read_stable_bytes(path, MAX_POLICY_BYTES, label="decision page")
            text = raw.decode("utf-8", errors="strict")
        except (OSError, UnicodeDecodeError, ValueError):
            return True
        frontmatter = text.split("---", 2)[1] if text.startswith("---") else ""
        if re.search(r"(?m)^type:\s*decision\s*$", frontmatter) is None:
            return False
        return any(marker in text for marker in markers)

    def _decision_references(self, daily_id: str, digest: str) -> bool:
        notes = self.vault / "knowledge" / "notes"
        if not notes.exists():
            return False
        note_paths = self._note_paths(notes)
        if note_paths is None:
            return True
        markers = (
            f"daily:{daily_id} sha256:{digest}",
            f"knowledge/daily/{daily_id}.md",
        )
        return any(self._decision_page_cites(path, markers) for path in note_paths)

    def _policy_contains(self, filename: str, daily_id: str, digest: str) -> bool:
        path = self.state_root / "run" / filename
        if not path.exists():
            return False
        try:
            value = json.loads(
                read_stable_bytes(path, MAX_POLICY_BYTES, label=filename).decode("utf-8")
            )
        except (OSError, UnicodeDecodeError, ValueError, json.JSONDecodeError):
            return True
        if not isinstance(value, dict):
            return True
        return daily_id in value.get("daily_ids", []) or digest in value.get(
            "source_hashes", []
        )

    @staticmethod
    def _quarantine_destination(bags: Path, bag: Path) -> Path:
        destination = bags / bag.name
        if destination.exists():
            return bags / f"{bag.name}-{uuid.uuid4().hex}"
        return destination

    def _move_quarantined_bag(self, bag: Path, destination: Path) -> None:
        """Rename when the filesystem allows it, copy when it does not."""
        try:
            self._publish_build(bag, destination)
        except OSError as exc:
            if exc.errno not in {errno.EACCES, errno.EPERM, errno.EXDEV}:
                raise
            self._copy_bag_cross_volume(bag, destination)

    def _quarantine(
        self,
        bag: Path,
        daily_id: str,
        digest: str,
        *,
        reason: str = "duplicate_hash_mismatch",
    ) -> None:
        target, bags = self._quarantine_directory("bags")
        destination = self._quarantine_destination(bags, bag)
        original = bag.relative_to(self.vault).as_posix()
        parent = bag.parent
        self._move_quarantined_bag(bag, destination)
        fsync_directory(parent)
        fsync_directory(bags)
        self._write_quarantine_record(
            target,
            "",
            {
                "bag_path": original,
                "quarantine_path": destination.relative_to(self.state_root).as_posix(),
                "logical_daily_id": daily_id,
                "reason": reason,
                "source_hash": digest,
            },
        )

    @staticmethod
    def _tree_directories(entries: list[Path]) -> list[Path]:
        return sorted(
            (item for item in entries if item.is_dir()),
            key=lambda item: len(item.parts),
        )

    @staticmethod
    def _tree_files(entries: list[Path]) -> list[Path]:
        return sorted(item for item in entries if item.is_file())

    @staticmethod
    def _mirror_directories(source: Path, staging: Path, directories: list[Path]) -> None:
        for directory in directories:
            target = staging / directory.relative_to(source)
            target.mkdir()
            _harden_owner_only(target, 0o700)
            fsync_directory(target.parent)

    @staticmethod
    def _copy_files(source: Path, staging: Path, files: list[Path]) -> None:
        for file in files:
            data = read_stable_bytes(
                file, MAX_DAILY_BYTES, label="archive quarantine copy"
            )
            target = staging / file.relative_to(source)
            target.write_bytes(data)
            _harden_owner_only(target, 0o600)
            fsync_file(target)

    def _copy_tree_into_staging(self, source: Path, staging: Path) -> None:
        entries = self._bounded_tree(source)
        directories = self._tree_directories(entries)
        self._mirror_directories(source, staging, directories)
        self._copy_files(source, staging, self._tree_files(entries))
        for directory in reversed(directories):
            fsync_directory(staging / directory.relative_to(source))
        fsync_directory(staging)

    def _copy_bag_cross_volume(self, source: Path, destination: Path) -> None:
        staging = destination.parent / f".{destination.name}.staging-{uuid.uuid4().hex}"
        staging.mkdir()
        _harden_owner_only(staging, 0o700)
        fsync_directory(staging.parent)
        try:
            self._copy_tree_into_staging(source, staging)
            self._seal(staging)
            validate_bag(staging, coordinator=self.coordinator, vault=self.vault)
            # Same sealed-directory rename that `_publish_build` exists for:
            # macOS returns EACCES when renaming a directory that denies write
            # permission, and the staged copy is sealed at 0o500 above.
            self._publish_build(staging, destination)
            fsync_directory(destination.parent)
            self._remove_build(source)
        except BaseException:
            if staging.exists():
                self._remove_build(staging)
            raise

    @staticmethod
    def _seal(root: Path) -> None:
        paths = [*sorted(DailyArchiver._bounded_tree(root), reverse=True), root]
        try:
            for path in paths:
                DailyArchiver._set_archive_read_only(path)
        except (OSError, PermissionError) as exc:
            raise PermissionError(f"archive seal failed: {exc}") from exc

    @staticmethod
    def _bounded_tree(root: Path) -> list[Path]:
        found: list[Path] = []
        pending = [Path(root)]
        while pending:
            DailyArchiver._scan_directory(pending.pop(), found, pending)
        return found

    @staticmethod
    def _scan_directory(parent: Path, found: list[Path], pending: list[Path]) -> None:
        for entry in bounded_directory_entries(
            parent, MAX_ARCHIVE_ENTRIES, label="archive package directory"
        ):
            info = entry.lstat()
            DailyArchiver._require_plain_entry(entry, info)
            found.append(entry)
            if len(found) > MAX_ARCHIVE_ENTRIES:
                raise ValueError("archive package exceeds the entry scan limit")
            if stat.S_ISDIR(info.st_mode):
                _regular_directory(entry, label="archive package directory")
                pending.append(entry)

    @staticmethod
    def _require_plain_entry(entry: Path, info: os.stat_result) -> None:
        """Only regular files and directories may live inside an archive package."""
        if entry.is_symlink() or getattr(info, "st_file_attributes", 0) & 0x400:
            raise PermissionError("archive package contains a symlink or reparse point")
        if stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode):
            return
        raise PermissionError("archive package contains a special file")

    @staticmethod
    def _read_only_acl_commands(path: Path, identity: str) -> tuple[list[str], ...]:
        access = "(OI)(CI)(RX)" if path.is_dir() else "(R)"
        target = str(path)
        return (
            ["icacls", target, "/inheritance:r", "/grant:r", f"{identity}:{access}"],
            ["icacls", target, "/remove:g", *_BROAD_ACL_SIDS],
            ["icacls", target, "/remove:d", *_BROAD_ACL_SIDS],
        )

    @staticmethod
    def _windows_read_only_acl(path: Path) -> None:
        identity = _windows_acl_identity()
        applied = [
            _run_acl_command(command)
            for command in DailyArchiver._read_only_acl_commands(path, identity)
        ]
        verified = _run_acl_command(["icacls", str(path)])
        DailyArchiver._require_read_only_acl(identity, applied, verified)

    @staticmethod
    def _acl_lines(acl: str) -> list[str]:
        return [line.strip() for line in acl.splitlines() if ":(" in line]

    @staticmethod
    def _acl_grants_only_identity(identity: str, acl_lines: list[str]) -> bool:
        return all(identity.casefold() in line.casefold() for line in acl_lines)

    @staticmethod
    def _acl_has_write_marker(acl: str) -> bool:
        return any(marker in acl for marker in ("(F)", "(M)", "(W)"))

    @staticmethod
    def _acl_failure(reason: str, detail: str) -> PermissionError:
        """Name what the ACL check saw; the seal fails either way."""
        return PermissionError(
            f"archive read-only ACL verification failed ({reason}): {detail[:400]}"
        )

    @staticmethod
    def _require_acl_commands_succeeded(applied: list, verified: object) -> None:
        codes = [command.returncode for command in applied]
        if any(code != 0 for code in codes) or verified.returncode != 0:
            first = applied[0]
            raise DailyArchiver._acl_failure(
                "icacls exit",
                f"apply={codes} read={verified.returncode} "
                f"{_acl_output_text(getattr(first, 'stdout', b''))}",
            )

    @staticmethod
    def _require_read_only_acl(identity: str, applied: list, verified: object) -> None:
        DailyArchiver._require_acl_commands_succeeded(applied, verified)
        acl = _acl_output_text(verified.stdout)
        acl_lines = DailyArchiver._acl_lines(acl)
        if not acl_lines:
            raise DailyArchiver._acl_failure("no ACL entries", acl)
        DailyArchiver._require_acl_grants_read_only(identity, acl, acl_lines)

    @staticmethod
    def _require_acl_grants_read_only(
        identity: str, acl: str, acl_lines: list
    ) -> None:
        if not DailyArchiver._acl_grants_only_identity(identity, acl_lines):
            raise DailyArchiver._acl_failure(
                f"principal other than {identity}", " | ".join(acl_lines)
            )
        if DailyArchiver._acl_has_write_marker(acl):
            raise DailyArchiver._acl_failure(
                "write access granted", " | ".join(acl_lines)
            )

    @staticmethod
    def _posix_read_only_mode(path: Path) -> None:
        mode = 0o500 if path.is_dir() else 0o400
        path.chmod(mode)
        if stat.S_IMODE(path.stat().st_mode) != mode:
            raise PermissionError("archive read-only mode verification failed")

    @staticmethod
    def _set_archive_read_only(path: Path) -> None:
        if os.name == "nt":
            DailyArchiver._windows_read_only_acl(path)
            return
        DailyArchiver._posix_read_only_mode(path)

    @staticmethod
    def _harden_windows_tree(path: Path, entries: list[Path]) -> None:
        _harden_owner_only(path, 0o700)
        for item in sorted(entries, key=lambda value: len(value.parts)):
            _harden_owner_only(item, 0o700 if item.is_dir() else 0o600)

    @staticmethod
    def _remove_entry(item: Path) -> None:
        if item.is_dir():
            item.rmdir()
            return
        item.unlink()

    @staticmethod
    def _remove_build(path: Path) -> None:
        if not path.exists():
            return
        entries = DailyArchiver._bounded_tree(path)
        if os.name == "posix":
            DailyArchiver._remove_tree_descriptor_relative(path)
            return
        DailyArchiver._remove_tree_by_path(path, entries)

    @staticmethod
    def _remove_tree_by_path(path: Path, entries: list) -> None:
        """The path-based removal every platform without descriptor removal takes."""
        if os.name == "nt":
            DailyArchiver._harden_windows_tree(path, entries)
        for item in sorted(entries, key=lambda value: len(value.parts), reverse=True):
            DailyArchiver._remove_entry(item)
        path.rmdir()

    @staticmethod
    def _remove_directory_entry(directory_fd: int, name: str) -> None:
        """Remove one directory member by descriptor, refusing to follow links."""
        child_fd = os.open(
            name,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=directory_fd,
        )
        try:
            os.fchmod(child_fd, 0o700)
            names = os.listdir(child_fd)
            if len(names) > MAX_ARCHIVE_ENTRIES:
                raise ValueError("archive package exceeds the entry scan limit")
            for child in names:
                DailyArchiver._remove_named_entry(child_fd, child)
        finally:
            os.close(child_fd)
        os.rmdir(name, dir_fd=directory_fd)

    @staticmethod
    def _remove_file_entry(directory_fd: int, name: str) -> None:
        os.chmod(name, 0o600, dir_fd=directory_fd, follow_symlinks=False)
        os.unlink(name, dir_fd=directory_fd)

    @staticmethod
    def _remove_named_entry(directory_fd: int, name: str) -> None:
        info = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        if stat.S_ISLNK(info.st_mode):
            raise PermissionError("refusing to remove linked archive build member")
        if stat.S_ISDIR(info.st_mode):
            DailyArchiver._remove_directory_entry(directory_fd, name)
            return
        DailyArchiver._remove_regular_entry(directory_fd, name, info)

    @staticmethod
    def _remove_regular_entry(directory_fd: int, name: str, info: object) -> None:
        if not stat.S_ISREG(info.st_mode):
            raise PermissionError("refusing to remove special archive build member")
        DailyArchiver._remove_file_entry(directory_fd, name)

    @staticmethod
    def _remove_tree_descriptor_relative(path: Path) -> None:
        parent_fd = os.open(
            path.parent,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
        )
        try:
            DailyArchiver._remove_named_entry(parent_fd, path.name)
        finally:
            os.close(parent_fd)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--commit", action="store_true", help="Publish eligible bags")
    parser.add_argument("--hot-days", type=int, default=DEFAULT_HOT_DAYS)
    parser.add_argument(
        "--transaction-retention-days",
        type=int,
        default=DEFAULT_TRANSACTION_RETENTION_DAYS,
    )
    return parser.parse_args(argv)


def _flat_daily_sources(archiver: DailyArchiver) -> list[Path]:
    entries = bounded_directory_entries(
        archiver.daily_root, MAX_ARCHIVE_ENTRIES, label="flat daily directory"
    )
    return sorted(
        entry for entry in entries if entry.suffix == ".md" and entry.is_file()
    )


def _archive_one(
    archiver: DailyArchiver, source: Path, args: argparse.Namespace
) -> ArchiveReceipt | None:
    """Archive one eligible daily, or report what a commit run would do."""
    status = archiver.eligible(
        source,
        hot_days=args.hot_days,
        transaction_retention_days=args.transaction_retention_days,
    )
    if not status.eligible:
        return None
    if not args.commit:
        print(f"Would archive: {source.name}")
        return None
    return archiver.archive(
        source.stem,
        hot_days=args.hot_days,
        transaction_retention_days=args.transaction_retention_days,
    )


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    archiver = DailyArchiver(ROOT, STATE_ROOT)
    results = [
        receipt
        for receipt in (
            _archive_one(archiver, source, args)
            for source in _flat_daily_sources(archiver)
        )
        if receipt is not None
    ]
    if args.commit:
        print(f"Archived {len(results)} log(s).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
