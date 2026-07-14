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
import uuid
from collections.abc import Callable
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
    _blocks,
    _line_span,
    _regular_directory,
    bounded_directory_entries,
    compile_authority_attestation,
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
DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")


@dataclass(frozen=True)
class Eligibility:
    eligible: bool
    reasons: tuple[str, ...]
    source_sha256: str | None = None
    receipt: dict[str, object] | None = None
    blocking_task_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class ArchiveReceipt:
    logical_daily_id: str
    source_sha256: str
    bag_path: Path
    state: str


class ArchiveConflict(RuntimeError):
    code = "archive_source_conflict"


class DailyArchiver:
    """Check retention policy, publish sealed bags, and recover mixed states."""

    def __init__(
        self,
        vault: Path,
        state_root: Path,
        *,
        clock: Callable[[], datetime] | None = None,
        killpoint: Callable[[str], None] | None = None,
    ) -> None:
        self.vault = Path(vault).resolve(strict=True)
        self.state_root = Path(state_root)
        self.daily_root = self.vault / "knowledge" / "daily"
        self.archive_root = self.daily_root / "archive"
        self.coordinator = MarkdownCoordinator(self.vault, self.state_root)
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.killpoint = killpoint or (lambda _point: None)

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

    def _eligible(
        self,
        source: Path,
        *,
        hot_days: int,
        transaction_retention_days: int,
        ignore_current_writer: bool,
        skip_queue_database_checks: bool = False,
    ) -> Eligibility:
        if (
            not isinstance(hot_days, int)
            or isinstance(hot_days, bool)
            or hot_days < 0
            or not isinstance(transaction_retention_days, int)
            or isinstance(transaction_retention_days, bool)
            or transaction_retention_days < 0
        ):
            raise ValueError("retention days must be non-negative integers")
        source = Path(source)
        if source.parent.resolve() != self.daily_root.resolve() or source.suffix != ".md":
            raise ValueError("archive source must be a flat daily Markdown file")
        if DATE_RE.fullmatch(source.stem) is None:
            return Eligibility(False, ("invalid_daily_id",))
        try:
            daily_date = date.fromisoformat(source.stem)
            content = read_stable_bytes(source, MAX_DAILY_BYTES, label="daily archive source")
        except (OSError, ValueError) as exc:
            return Eligibility(False, (f"source:{exc}",))
        digest = sha256_bytes(content)
        reasons: list[str] = []
        today = self.clock().astimezone(timezone.utc).date()
        if daily_date == today:
            reasons.append("today")
        if (today - daily_date).days <= hot_days:
            reasons.append("hot_retention")

        receipt: dict[str, object] | None = None
        receipt_operation_state = self._receipt_operation_state(digest)
        if receipt_operation_state is not None and receipt_operation_state != "committed":
            reasons.append("nonterminal_compile_operation")
        try:
            from compile_memory import read_compile_receipt

            receipt = read_compile_receipt(digest, self.coordinator)
        except (OSError, RuntimeError, ValueError):
            receipt = None
        if receipt is None or receipt.get("source_digest") != digest:
            reasons.append("compile_receipt")
        elif receipt.get("state") != "completed":
            reasons.append("nonterminal_compile_operation")

        try:
            blocks = _blocks(content)
            if not blocks:
                raise EvidenceResolutionError("daily has no evidence blocks")
            for block_id, start, end in blocks:
                EvidenceResolver(self.vault, state_root=self.state_root).resolve(
                    EvidenceRef(source.stem, digest, block_id, start, end)
                )
        except (OSError, ValueError, EvidenceResolutionError):
            reasons.append("unresolved_evidence")

        blocking_tasks = (
            () if skip_queue_database_checks else self._queue_references(source.stem, digest)
        )
        if blocking_tasks:
            reasons.append("queue_reference")
        if self._legacy_queue_references(source.stem, digest):
            reasons.append("legacy_queue_reference")
        if self._transaction_references(
            source.name, transaction_retention_days=transaction_retention_days
        ):
            reasons.append("active_transaction")
        if receipt is not None and self._receipt_transaction_retained(
            receipt, digest, transaction_retention_days
        ):
            reasons.append("transaction_retention")
        logical_path = f"knowledge/daily/{source.name}"
        if (
            not skip_queue_database_checks
            and MemoryQueue(self.state_root).source_failure(logical_path, digest) is not None
        ):
            reasons.append("source_failure")
        if not ignore_current_writer and self._writer_active():
            reasons.append("active_writer")
        if self._decision_references(source.stem, digest):
            reasons.append("decision_evidence")
        if self._policy_contains("archive-pins.json", source.stem, digest):
            reasons.append("manual_pin")
        return Eligibility(
            not reasons,
            tuple(dict.fromkeys(reasons)),
            digest,
            receipt,
            tuple(blocking_tasks),
        )

    def _receipt_operation_state(self, digest: str) -> str | None:
        path = self.daily_root / "receipts" / f"{digest}.md"
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
            with sqlite3.connect(self.coordinator.database_path) as connection:
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

    def _receipt_transaction_retained(
        self,
        receipt: dict[str, object],
        digest: str,
        transaction_retention_days: int,
    ) -> bool:
        operation_id = receipt.get("operation_id")
        if not isinstance(operation_id, str):
            return True
        transaction = self.coordinator._record_for_operation_id(operation_id)
        if transaction is None or transaction.state != "committed":
            return True
        expected_paths = {
            f"knowledge/daily/receipts/{digest}.md",
            *(
                str(item.get("path"))
                for item in receipt.get("operations", [])
                if isinstance(item, dict)
            ),
        }
        if not expected_paths.issubset({item.path for item in transaction.operations}):
            return True
        cutoff = self.clock().astimezone(timezone.utc) - timedelta(
            days=transaction_retention_days
        )
        try:
            updated = datetime.fromisoformat(transaction.updated_at.replace("Z", "+00:00"))
        except ValueError:
            return True
        return updated >= cutoff

    def archive(
        self,
        daily_id: str,
        *,
        hot_days: int = DEFAULT_HOT_DAYS,
        transaction_retention_days: int = DEFAULT_TRANSACTION_RETENTION_DAYS,
    ) -> ArchiveReceipt:
        if DATE_RE.fullmatch(daily_id) is None:
            raise ValueError("daily ID must be YYYY-MM-DD")
        source = self.daily_root / f"{daily_id}.md"
        with self.coordinator.writer_gate(wait_seconds=ARCHIVE_WRITER_WAIT_SECONDS):
            self.coordinator.recover()
            source_bytes = read_stable_bytes(
                source, MAX_DAILY_BYTES, label="daily archive source"
            )
            source_digest = sha256_bytes(source_bytes)
            self._recover_hidden_builds()
            reused = self._recover_daily(
                daily_id,
                source_digest,
                hot_days=hot_days,
                transaction_retention_days=transaction_retention_days,
            )
            if reused is not None:
                return reused
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
            queue = MemoryQueue(self.state_root)
            fence = queue.acquire_source_fence(daily_id, eligibility.source_sha256)
            try:
                if queue.source_failure(
                    f"knowledge/daily/{daily_id}.md", eligibility.source_sha256
                ) is not None:
                    raise ValueError("daily is not archive eligible: source_failure")
                final_bag, publish_build = self._build_bag(
                    daily_id, content, eligibility, hot_days=hot_days
                )
                published = False
                try:
                    self.killpoint("after_build")
                    self.killpoint("before_publish_rename")
                    self._prepare_build_for_publish(publish_build)
                    publish_build.replace(final_bag)
                    published = True
                    fsync_directory(final_bag.parent)
                    self.killpoint("after_publish_rename")
                    validated = validate_bag(
                        final_bag, coordinator=self.coordinator, vault=self.vault
                    )
                    if validated.manifest["source_hash"] != eligibility.source_sha256:
                        raise RuntimeError("published archive failed source revalidation")
                    self.killpoint("after_revalidate")
                    self._remove_flat_under_finalization(
                        queue,
                        fence,
                        hot_days=hot_days,
                        transaction_retention_days=transaction_retention_days,
                    )
                    self.killpoint("after_source_delete")
                except BaseException:
                    if not published:
                        self._remove_build(publish_build)
                    raise
            finally:
                queue.release_source_fence(fence.token)
            self.rebuild_index()
            return ArchiveReceipt(daily_id, eligibility.source_sha256, final_bag, "archived")

    def _recover_daily(
        self,
        daily_id: str,
        digest: str,
        *,
        hot_days: int,
        transaction_retention_days: int,
    ) -> ArchiveReceipt | None:
        exact: list[Path] = []
        conflicts: list[Path] = []
        for path in self._archive_paths(hidden=False):
            bag = validate_bag(path, coordinator=self.coordinator, vault=self.vault)
            if bag.manifest["logical_daily_id"] != daily_id:
                continue
            if bag.manifest["source_hash"] == digest:
                exact.append(path)
            else:
                self._quarantine(path, daily_id, str(bag.manifest["source_hash"]))
                conflicts.append(path)
        if conflicts:
            raise ArchiveConflict(
                f"published archive conflicts with flat source daily:{daily_id}"
            )
        if not exact:
            return None
        keeper = exact[0]
        for duplicate in exact[1:]:
            self._quarantine(duplicate, daily_id, digest, reason="duplicate_exact_match")
        flat = self.daily_root / f"{daily_id}.md"
        flat_bytes = read_stable_bytes(flat, MAX_DAILY_BYTES, label="daily duplicate")
        if sha256_bytes(flat_bytes) != digest:
            self._quarantine(keeper, daily_id, digest)
            return None
        self._remove_flat_after_eligibility_recheck(
            daily_id,
            digest,
            hot_days=hot_days,
            transaction_retention_days=transaction_retention_days,
        )
        self.rebuild_index()
        return ArchiveReceipt(daily_id, digest, keeper, "recovered")

    def _build_bag(
        self,
        daily_id: str,
        content: bytes,
        eligibility: Eligibility,
        *,
        hot_days: int,
    ) -> tuple[Path, Path]:
        month = self.archive_root / daily_id[:7]
        before = (
            {entry.name for entry in bounded_directory_entries(
                month, MAX_ARCHIVE_ENTRIES, label="daily archive month"
            )}
            if month.exists()
            else set()
        )
        try:
            return self._build_bag_contents(
                daily_id, content, eligibility, hot_days=hot_days
            )
        except BaseException:
            if month.exists():
                for entry in bounded_directory_entries(
                    month, MAX_ARCHIVE_ENTRIES, label="daily archive month"
                ):
                    if entry.name not in before and ".building-" in entry.name:
                        self._remove_build(entry)
            raise

    def _build_bag_contents(
        self,
        daily_id: str,
        content: bytes,
        eligibility: Eligibility,
        *,
        hot_days: int,
    ) -> tuple[Path, Path]:
        now = self.clock().astimezone(timezone.utc)
        month = self.archive_root / daily_id[:7]
        self._ensure_archive_root()
        if month.exists():
            _regular_directory(month, label="daily archive month")
        else:
            month.mkdir()
            _regular_directory(month, label="daily archive month")
            fsync_directory(self.archive_root)
        _harden_owner_only(month, 0o700)
        nonce = uuid.uuid4().hex
        stamp = now.strftime("%Y%m%dT%H%M%S%fZ")
        final_bag = month / f"bag-{stamp}-{daily_id}-{nonce}"
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
        (publish_build / "build-intent.json").write_bytes(
            canonical_json_bytes(intent)
        )
        data = publish_build / "data"
        data.mkdir()
        _set_owner_only(data, 0o700)
        fsync_directory(publish_build)
        payload_name = f"data/{daily_id}.md"
        payload = publish_build / payload_name
        payload.write_bytes(content)
        digest = sha256_bytes(content)
        bagit = b"BagIt-Version: 1.0\nTag-File-Character-Encoding: UTF-8\n"
        bag_info = (
            f"Bagging-Date: {now.date().isoformat()}\n"
            f"Payload-Oxum: {len(content)}.1\n"
            f"External-Identifier: daily:{daily_id}\n"
        ).encode()
        (publish_build / "bagit.txt").write_bytes(bagit)
        (publish_build / "bag-info.txt").write_bytes(bag_info)
        (publish_build / "manifest-sha256.txt").write_bytes(
            f"{digest}  {payload_name}\n".encode()
        )
        receipt = eligibility.receipt
        assert receipt is not None
        receipt_path = self.daily_root / "receipts" / f"{digest}.md"
        receipt_bytes = read_stable_bytes(
            receipt_path, MAX_POLICY_BYTES, label="compile receipt"
        )
        from compile_memory import read_compile_receipt

        authoritative_receipt = read_compile_receipt(
            digest,
            self.coordinator,
            path=receipt_path,
            vault=self.vault,
        )
        if authoritative_receipt != receipt:
            raise RuntimeError("compile receipt authority changed during archive")
        transaction = self.coordinator._record_for_operation_id(
            str(receipt["operation_id"])
        )
        if transaction is None or transaction.state != "committed":
            raise RuntimeError("compile transaction authority is not committed")
        with self.coordinator._connect() as database:
            sequence_row = database.execute(
                'SELECT rowid AS commit_sequence FROM "transaction" WHERE id=?',
                (transaction.id,),
            ).fetchone()
        if sequence_row is None:
            raise RuntimeError("compile transaction authority disappeared")
        compile_authority = compile_authority_attestation(
            transaction, int(sequence_row["commit_sequence"])
        )
        (publish_build / "compile-receipt.md").write_bytes(receipt_bytes)
        evidence = [
            {
                "block_id": block_id,
                "byte_start": start,
                "byte_end": end,
                "line_start": _line_span(content, start, end)[0],
                "line_end": _line_span(content, start, end)[1],
                "sha256": sha256_bytes(content[start:end]),
            }
            for block_id, start, end in _blocks(content)
        ]
        manifest = {
            "schema_version": "archive-manifest/v1",
            "logical_daily_id": daily_id,
            "original_path": f"knowledge/daily/{daily_id}.md",
            "source_hash": digest,
            "payload_hash": digest,
            "compile_receipt_ref": {
                "schema": "compile-receipt-ref/v1",
                "path": receipt_path.relative_to(self.vault).as_posix(),
                "source_digest": digest,
                "receipt_file_hash": sha256_bytes(receipt_bytes),
                "embedded_path": "compile-receipt.md",
            },
            "compile_authority": compile_authority,
            "queue_preflight": {
                "checked_at": now.isoformat().replace("+00:00", "Z"),
                "passed": True,
                "blocking_task_ids": list(eligibility.blocking_task_ids),
            },
            "operations": [
                {"operation_id": str(receipt["operation_id"]), "state": "succeeded"}
            ],
            "evidence": evidence,
            "pins": [],
            "retention_days": hot_days,
        }
        (publish_build / "archive-manifest.json").write_bytes(
            canonical_json_bytes(manifest)
        )
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
        for path in sorted(
            item for item in self._bounded_tree(publish_build) if item.is_file()
        ):
            _set_owner_only(path, 0o600)
            fsync_file(path)
        fsync_directory(data)
        fsync_directory(publish_build)
        self._seal(publish_build)
        validate_bag(
            publish_build,
            coordinator=self.coordinator,
            vault=self.vault,
            allow_build_intent=True,
        )
        return final_bag, publish_build

    def _read_build_intent(self, build: Path) -> dict[str, str]:
        raw = read_stable_bytes(
            build / "build-intent.json", MAX_POLICY_BYTES, label="archive build intent"
        )
        value = json.loads(raw.decode("utf-8", errors="strict"))
        if canonical_json_bytes(value) != raw or not isinstance(value, dict):
            raise ValueError("archive build intent is not canonical")
        required = {
            "created_at",
            "final_bag_name",
            "logical_daily_id",
            "schema_version",
            "source_hash",
        }
        if set(value) != required or value.get("schema_version") != "archive-build-intent/v1":
            raise ValueError("archive build intent fields are invalid")
        daily_id = str(value["logical_daily_id"])
        digest = str(value["source_hash"])
        final_name = str(value["final_bag_name"])
        if (
            DATE_RE.fullmatch(daily_id) is None
            or re.fullmatch(r"[0-9a-f]{64}", digest) is None
            or re.fullmatch(rf"bag-[A-Za-z0-9-]+-{daily_id}-[0-9a-f]{{32}}", final_name)
            is None
        ):
            raise ValueError("archive build intent identity is invalid")
        return {key: str(value[key]) for key in required}

    def _prepare_build_for_publish(self, build: Path) -> dict[str, str]:
        intent = self._read_build_intent(build)
        validate_bag(
            build,
            coordinator=self.coordinator,
            vault=self.vault,
            allow_build_intent=True,
        )
        if os.name == "nt":
            _harden_owner_only(build, 0o700)
            _harden_owner_only(build / "build-intent.json", 0o600)
        else:
            build.chmod(0o700)
            (build / "build-intent.json").chmod(0o600)
        (build / "build-intent.json").unlink()
        fsync_directory(build)
        self._set_archive_read_only(build)
        validate_bag(build, coordinator=self.coordinator, vault=self.vault)
        return intent

    def _recover_hidden_builds(self) -> None:
        for build in self._archive_paths(hidden=True):
            try:
                self._bounded_tree(build)
            except (OSError, PermissionError, ValueError) as exc:
                self._quarantine_hidden_build(build, "unsafe_hidden_build")
                raise ArchiveConflict("unsafe hidden archive build") from exc
            try:
                intent = self._read_build_intent(build)
                validate_bag(
                    build,
                    coordinator=self.coordinator,
                    vault=self.vault,
                    allow_build_intent=True,
                )
                final = build.parent / intent["final_bag_name"]
                if final.exists():
                    self._remove_build(build)
                    continue
                self._prepare_build_for_publish(build)
                build.replace(final)
                fsync_directory(final.parent)
            except (OSError, TypeError, ValueError, EvidenceResolutionError):
                self._remove_build(build)

    def _quarantine_hidden_build(self, build: Path, reason: str) -> None:
        root = self.state_root / "run/archive-quarantine"
        builds = root / "builds"
        if not root.exists():
            root.mkdir(parents=True)
            _harden_owner_only(root, 0o700)
            fsync_directory(root.parent)
        if not builds.exists():
            builds.mkdir()
            _harden_owner_only(builds, 0o700)
            fsync_directory(root)
        destination = builds / f"{build.name}-{uuid.uuid4().hex}"
        try:
            build.replace(destination)
        except OSError as exc:
            if exc.errno != errno.EXDEV:
                raise
            destination = build.parent / f".quarantined-build-{uuid.uuid4().hex}"
            build.replace(destination)
        fsync_directory(build.parent)
        if destination.parent == builds:
            fsync_directory(builds)
        record = {
            "original_path": build.relative_to(self.vault).as_posix(),
            "quarantine_path": str(destination),
            "reason": reason,
        }
        record_path = root / f"build-{sha256_bytes(canonical_json_bytes(record))}.json"
        record_path.write_bytes(canonical_json_bytes(record))
        _harden_owner_only(record_path, 0o600)
        fsync_file(record_path)
        fsync_directory(root)

    def _remove_flat(self, daily_id: str, digest: str) -> None:
        relative = f"knowledge/daily/{daily_id}.md"
        transaction = self.coordinator.prepare(
            [MarkdownChange.delete(relative, max_before_bytes=MAX_DAILY_BYTES)],
            operation_id=f"archive-remove:{daily_id}:{digest}",
            preconditions={relative: digest},
        )
        self.coordinator.apply(transaction.id)

    def _remove_flat_after_eligibility_recheck(
        self,
        daily_id: str,
        digest: str,
        *,
        hot_days: int,
        transaction_retention_days: int,
    ) -> None:
        queue = MemoryQueue(self.state_root)
        fence = queue.acquire_source_fence(daily_id, digest)
        try:
            self._remove_flat_under_finalization(
                queue,
                fence,
                hot_days=hot_days,
                transaction_retention_days=transaction_retention_days,
            )
        finally:
            queue.release_source_fence(fence.token)

    def _remove_flat_under_finalization(
        self,
        queue: MemoryQueue,
        fence: SourceFence,
        *,
        hot_days: int,
        transaction_retention_days: int,
    ) -> None:
        try:
            with queue.source_finalization(fence):
                source = self.daily_root / f"{fence.daily_id}.md"
                eligibility = self._eligible(
                    source,
                    hot_days=hot_days,
                    transaction_retention_days=transaction_retention_days,
                    ignore_current_writer=True,
                    skip_queue_database_checks=True,
                )
                if eligibility.source_sha256 != fence.source_digest:
                    raise RuntimeError("daily source changed before recovery removal")
                if not eligibility.eligible:
                    raise ValueError(
                        "daily is not archive eligible: "
                        + ", ".join(eligibility.reasons)
                    )
                self._remove_flat(fence.daily_id, fence.source_digest)
        except QueueOperationError as exc:
            if exc.code in {"source_failure", "source_referenced"}:
                raise ValueError(f"daily is not archive eligible: {exc.code}") from exc
            raise

    def recover(
        self,
        *,
        hot_days: int = DEFAULT_HOT_DAYS,
        transaction_retention_days: int = DEFAULT_TRANSACTION_RETENTION_DAYS,
    ) -> list[ArchiveReceipt]:
        recovered: list[ArchiveReceipt] = []
        if not self.archive_root.exists():
            return recovered
        _regular_directory(self.archive_root, label="daily archive root")
        with self.coordinator.writer_gate(wait_seconds=ARCHIVE_WRITER_WAIT_SECONDS):
            self._recover_hidden_builds()
            grouped: dict[tuple[str, str], list[Path]] = {}
            for path in self._archive_paths(hidden=False):
                bag = validate_bag(
                    path, coordinator=self.coordinator, vault=self.vault
                )
                daily_id = str(bag.manifest["logical_daily_id"])
                digest = str(bag.manifest["source_hash"])
                grouped.setdefault((daily_id, digest), []).append(path)
            for (daily_id, digest), paths in sorted(grouped.items()):
                flat = self.daily_root / f"{daily_id}.md"
                if not flat.exists():
                    continue
                flat_bytes = read_stable_bytes(flat, MAX_DAILY_BYTES, label="daily duplicate")
                if sha256_bytes(flat_bytes) == digest:
                    keeper = paths[0]
                    for duplicate in paths[1:]:
                        self._quarantine(
                            duplicate,
                            daily_id,
                            digest,
                            reason="duplicate_exact_match",
                        )
                    self._remove_flat_after_eligibility_recheck(
                        daily_id,
                        digest,
                        hot_days=hot_days,
                        transaction_retention_days=transaction_retention_days,
                    )
                    recovered.extend((ArchiveReceipt(daily_id, digest, keeper, "recovered"),))
                else:
                    for path in paths:
                        self._quarantine(path, daily_id, digest)
                        recovered.extend(
                            (ArchiveReceipt(daily_id, digest, path, "quarantined"),)
                        )
            self.rebuild_index()
        return recovered

    def rebuild_index(self) -> Path:
        self._ensure_archive_root()
        bags = []
        for path in self._archive_paths(hidden=False):
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

    def _archive_paths(self, *, hidden: bool) -> list[Path]:
        self._ensure_archive_root()
        paths: list[Path] = []
        months = bounded_directory_entries(
            self.archive_root, MAX_ARCHIVE_MONTHS, label="daily archive root"
        )
        for month in sorted(months):
            if re.fullmatch(r"\d{4}-\d{2}", month.name) is None:
                continue
            _regular_directory(month, label="daily archive month")
            entries = bounded_directory_entries(
                month, MAX_ARCHIVE_ENTRIES, label="daily archive month"
            )
            for entry in sorted(entries):
                matches = ".building-" in entry.name if hidden else entry.name.startswith("bag-")
                if not matches:
                    continue
                _regular_directory(entry, label="daily archive bag")
                paths.append(entry)
        return paths

    def _queue_references(self, daily_id: str, digest: str) -> list[str]:
        try:
            return list(MemoryQueue(self.state_root).referencing_source_tasks(daily_id, digest))
        except (OSError, QueueOperationError, sqlite3.Error):
            return ["queue-unreadable"]

    def _legacy_queue_references(self, daily_id: str, digest: str) -> bool:
        legacy = self.state_root / "run" / "queue"
        if not legacy.exists():
            return False
        markers = (daily_id.encode(), digest.encode())
        try:
            entries = bounded_directory_entries(
                legacy, MAX_ARCHIVE_ENTRIES, label="legacy queue directory"
            )
        except (OSError, ValueError):
            return True
        for path in sorted(
            entry for entry in entries if entry.suffix in {".json", ".processing"}
        ):
            try:
                raw = read_stable_bytes(path, MAX_POLICY_BYTES, label="legacy queue task")
            except (OSError, ValueError):
                return True
            if any(marker in raw for marker in markers):
                return True
        return False

    def _transaction_references(
        self, source_name: str, *, transaction_retention_days: int
    ) -> bool:
        database = self.coordinator.database_path
        cutoff = self.clock().astimezone(timezone.utc) - timedelta(
            days=transaction_retention_days
        )
        relative = f"knowledge/daily/{source_name}"
        try:
            with sqlite3.connect(database) as connection:
                rows = connection.execute(
                    'SELECT t.state, t.updated_at FROM "transaction" t '
                    'JOIN "operation" o ON o.transaction_id=t.id WHERE o.path=?',
                    (relative,),
                ).fetchall()
        except sqlite3.Error:
            return True
        for state_name, updated_at in rows:
            if state_name not in {"committed", "discarded"}:
                return True
            if state_name == "committed":
                updated = datetime.fromisoformat(str(updated_at).replace("Z", "+00:00"))
                if updated >= cutoff:
                    return True
        return False

    def _writer_active(self) -> bool:
        try:
            with self.coordinator._connect() as connection:
                row = connection.execute(
                    "SELECT * FROM writer_owners WHERE gate_name='global'"
                ).fetchone()
            return row is not None and not self.coordinator._writer_owner_reclaimable(row)
        except sqlite3.Error:
            return True

    def _decision_references(self, daily_id: str, digest: str) -> bool:
        notes = self.vault / "knowledge" / "notes"
        if not notes.exists():
            return False
        markers = (
            f"daily:{daily_id} sha256:{digest}",
            f"knowledge/daily/{daily_id}.md",
        )
        try:
            note_paths = [
                path
                for path in self._bounded_tree(notes)
                if path.suffix == ".md" and path.is_file()
            ]
        except (OSError, ValueError):
            return True
        for path in sorted(note_paths):
            try:
                raw = read_stable_bytes(path, MAX_POLICY_BYTES, label="decision page")
                text = raw.decode("utf-8", errors="strict")
            except (OSError, UnicodeDecodeError, ValueError):
                return True
            frontmatter = text.split("---", 2)[1] if text.startswith("---") else ""
            if re.search(r"(?m)^type:\s*decision\s*$", frontmatter) and any(
                marker in text for marker in markers
            ):
                return True
        return False

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

    def _quarantine(
        self,
        bag: Path,
        daily_id: str,
        digest: str,
        *,
        reason: str = "duplicate_hash_mismatch",
    ) -> None:
        target = self.state_root / "run" / "archive-quarantine"
        bags = target / "bags"
        if not target.exists():
            target.mkdir(parents=True)
            _harden_owner_only(target, 0o700)
            fsync_directory(target.parent)
        _regular_directory(target, label="archive quarantine root")
        if not bags.exists():
            bags.mkdir()
            _harden_owner_only(bags, 0o700)
            fsync_directory(target)
        _regular_directory(bags, label="archive quarantine bags")
        destination = bags / bag.name
        if destination.exists():
            destination = bags / f"{bag.name}-{uuid.uuid4().hex}"
        original = bag.relative_to(self.vault).as_posix()
        try:
            bag.replace(destination)
        except OSError as exc:
            if exc.errno != errno.EXDEV:
                raise
            self._copy_bag_cross_volume(bag, destination)
        fsync_directory(bag.parent)
        fsync_directory(bags)
        record = {
            "bag_path": original,
            "quarantine_path": destination.relative_to(self.state_root).as_posix(),
            "logical_daily_id": daily_id,
            "reason": reason,
            "source_hash": digest,
        }
        path = target / f"{sha256_bytes(canonical_json_bytes(record))}.json"
        path.write_bytes(canonical_json_bytes(record))
        _harden_owner_only(path, 0o600)
        fsync_file(path)
        fsync_directory(target)

    def _copy_bag_cross_volume(self, source: Path, destination: Path) -> None:
        staging = destination.parent / f".{destination.name}.staging-{uuid.uuid4().hex}"
        staging.mkdir()
        _harden_owner_only(staging, 0o700)
        fsync_directory(staging.parent)
        try:
            entries = self._bounded_tree(source)
            directories = sorted(
                (item for item in entries if item.is_dir()),
                key=lambda item: len(item.parts),
            )
            for directory in directories:
                target = staging / directory.relative_to(source)
                target.mkdir()
                _harden_owner_only(target, 0o700)
                fsync_directory(target.parent)
            for file in sorted(item for item in entries if item.is_file()):
                relative = file.relative_to(source)
                data = read_stable_bytes(file, MAX_DAILY_BYTES, label="archive quarantine copy")
                target = staging / relative
                target.write_bytes(data)
                _harden_owner_only(target, 0o600)
                fsync_file(target)
            for directory in reversed(directories):
                fsync_directory(staging / directory.relative_to(source))
            fsync_directory(staging)
            self._seal(staging)
            validate_bag(staging, coordinator=self.coordinator, vault=self.vault)
            staging.replace(destination)
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
            parent = pending.pop()
            for entry in bounded_directory_entries(
                parent, MAX_ARCHIVE_ENTRIES, label="archive package directory"
            ):
                info = entry.lstat()
                if (
                    entry.is_symlink()
                    or getattr(info, "st_file_attributes", 0) & 0x400
                ):
                    raise PermissionError("archive package contains a symlink or reparse point")
                found.append(entry)
                if len(found) > MAX_ARCHIVE_ENTRIES:
                    raise ValueError("archive package exceeds the entry scan limit")
                if stat.S_ISDIR(info.st_mode):
                    _regular_directory(entry, label="archive package directory")
                    pending.append(entry)
                elif not stat.S_ISREG(info.st_mode):
                    raise PermissionError("archive package contains a special file")
        return found

    @staticmethod
    def _set_archive_read_only(path: Path) -> None:
        if os.name == "nt":
            identity = _windows_acl_identity()
            access = "(OI)(CI)(RX)" if path.is_dir() else "(R)"
            changed = _run_acl_command(
                [
                    "icacls",
                    str(path),
                    "/inheritance:r",
                    "/grant:r",
                    f"{identity}:{access}",
                ]
            )
            verified = _run_acl_command(["icacls", str(path)])
            acl = _acl_output_text(verified.stdout)
            acl_lines = [line.strip() for line in acl.splitlines() if ":(" in line]
            if (
                changed.returncode != 0
                or verified.returncode != 0
                or not acl_lines
                or any(identity.casefold() not in line.casefold() for line in acl_lines)
                or any(marker in acl for marker in ("(F)", "(M)", "(W)"))
            ):
                raise PermissionError("archive read-only ACL verification failed")
        else:
            mode = 0o500 if path.is_dir() else 0o400
            path.chmod(mode)
            if stat.S_IMODE(path.stat().st_mode) != mode:
                raise PermissionError("archive read-only mode verification failed")

    @staticmethod
    def _remove_build(path: Path) -> None:
        if not path.exists():
            return
        entries = DailyArchiver._bounded_tree(path)
        if os.name == "posix":
            DailyArchiver._remove_tree_descriptor_relative(path)
            return
        if os.name == "nt":
            _harden_owner_only(path, 0o700)
            for item in sorted(entries, key=lambda value: len(value.parts)):
                _harden_owner_only(item, 0o700 if item.is_dir() else 0o600)
        for item in sorted(entries, key=lambda value: len(value.parts), reverse=True):
            if item.is_dir():
                item.rmdir()
            else:
                item.unlink()
        path.rmdir()

    @staticmethod
    def _remove_tree_descriptor_relative(path: Path) -> None:
        parent_fd = os.open(
            path.parent,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
        )

        def remove_entry(directory_fd: int, name: str) -> None:
            info = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            if stat.S_ISLNK(info.st_mode):
                raise PermissionError("refusing to remove linked archive build member")
            if stat.S_ISDIR(info.st_mode):
                child_fd = os.open(
                    name,
                    os.O_RDONLY
                    | getattr(os, "O_DIRECTORY", 0)
                    | getattr(os, "O_NOFOLLOW", 0),
                    dir_fd=directory_fd,
                )
                try:
                    os.fchmod(child_fd, 0o700)
                    names = os.listdir(child_fd)
                    if len(names) > MAX_ARCHIVE_ENTRIES:
                        raise ValueError("archive package exceeds the entry scan limit")
                    for child in names:
                        remove_entry(child_fd, child)
                finally:
                    os.close(child_fd)
                os.rmdir(name, dir_fd=directory_fd)
            elif stat.S_ISREG(info.st_mode):
                os.chmod(
                    name,
                    0o600,
                    dir_fd=directory_fd,
                    follow_symlinks=False,
                )
                os.unlink(name, dir_fd=directory_fd)
            else:
                raise PermissionError("refusing to remove special archive build member")

        try:
            remove_entry(parent_fd, path.name)
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


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    archiver = DailyArchiver(ROOT, STATE_ROOT)
    results: list[ArchiveReceipt] = []
    daily_entries = bounded_directory_entries(
        archiver.daily_root, MAX_ARCHIVE_ENTRIES, label="flat daily directory"
    )
    for source in sorted(
        entry for entry in daily_entries if entry.suffix == ".md" and entry.is_file()
    ):
        status = archiver.eligible(
            source,
            hot_days=args.hot_days,
            transaction_retention_days=args.transaction_retention_days,
        )
        if not status.eligible:
            continue
        if args.commit:
            results.append(
                archiver.archive(
                    source.stem,
                    hot_days=args.hot_days,
                    transaction_retention_days=args.transaction_retention_days,
                )
            )
        else:
            print(f"Would archive: {source.name}")
    if args.commit:
        print(f"Archived {len(results)} log(s).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
