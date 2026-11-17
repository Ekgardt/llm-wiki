"""The compile's daily selection, receipt discovery, run state and lock."""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

import compile_lock
import process_liveness  # noqa: E402
from bounded_io import (  # noqa: E402
    immutable_source_digest,
    read_stable_bytes,
)
from compile_receipts import (
    RECEIPT_DISCOVERY_PARSING,
    daily_is_compiled,
    iter_daily_parts,
    open_daily_source,
    receipt_predicate,
    require_compile_active,
    validated_discovery_source,
)
from evidence_resolver import (  # noqa: E402
    MAX_DAILY_PART_BYTES,  # noqa: F401 - re-exported: callers read the writer's bound here
)
from llm_client import (  # noqa: E402
    call_ceiling,
)
from llm_wiki.compile_receipt_authority import (
    MAX_RECEIPT_BYTES,
)
from llm_wiki.formats.canonical import canonical_json_bytes, sha256_bytes  # noqa: E402
from llm_wiki.formats.receipts import (  # noqa: E402  # noqa: E402  # noqa: E402  # noqa: E402  # noqa: E402  # noqa: E402  # noqa: E402  # noqa: E402  # noqa: E402  # noqa: E402  # noqa: E402  # noqa: E402  # noqa: E402  # noqa: E402  # noqa: E402
    COMPILE_RECEIPT_V4_SCHEMA,
    UnsupportedCompileReceiptVersion,
    compile_context_source_identity,
    compile_source_identity,
    parse_compile_receipt_version,
    receipt_source_fields,
    receipt_version_reader,
    require_v4_descriptor,
)
from markdown_transaction import (  # noqa: E402
    MarkdownCoordinator,
)
from memory_state import closed_daily_logs, daily_logs, load_state, update_state
from settings import setting_value  # noqa: E402

if TYPE_CHECKING:
    from operational_ownership import OwnerLease
from llm_wiki import runtime_roots
from llm_wiki.compile_pipeline_apply import existing_receipt_selection
from llm_wiki.compile_pipeline_batches import CompilePlanUnavailable
from llm_wiki.compile_pipeline_claims import (
    DROPPED_CLAIMS,
    QUARANTINE_OPERATION_PREFIX,
    BatchOutcome,
    compile_outcome,
)
from llm_wiki.compile_pipeline_receipts import context_receipt_path, v4_source_descriptor
from llm_wiki.compile_pipeline_sources import MAX_FAILURE_DETAIL_CHARS, open_optional_daily_source


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser()
    # Deprecated, and hidden from --help so no new command line learns it. It is
    # still accepted, and says so, because old command lines and notes name it;
    # it cannot be made real, because a committed day is never compiled again.
    p.add_argument("--all", action="store_true", help=argparse.SUPPRESS)
    p.add_argument("--file", type=str, default=None)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument(
        "--discard-unusable-receipts",
        action="store_true",
        help=(
            "Remove compile receipts that no longer parse and exit. A corrupt "
            "receipt is an error by contract; this is the deliberate way out."
        ),
    )
    p.add_argument(
        "--trigger",
        choices=["auto", "manual"],
        default="manual",
        help="Source of invocation. 'auto' is set by flush_memory when a hook "
        "fires the compile; any direct CLI run defaults to 'manual'.",
    )
    p.add_argument(
        "--lock-token",
        default=None,
        help="The compile lock written for this run by the process that spawned "
        "it. Passed by maybe_compile; a direct CLI run has none.",
    )
    p.add_argument(
        "--closed-days-only",
        action="store_true",
        help="Leave out the newest daily log, the one still being appended to. "
        "Passed by the session start; the nightly and a manual run take every day.",
    )
    return p.parse_args()


def canonical_dailies() -> list[Path]:
    """Every daily log in the vault, and nothing else that lives beside them."""
    return daily_logs(runtime_roots.daily_dir())


@contextmanager
def receipt_discovery_scope():
    """One compile owns pure discovery parsing; live authority is never cached."""
    token = RECEIPT_DISCOVERY_PARSING.set(_ReceiptDiscoveryParsing())
    try:
        yield
    finally:
        RECEIPT_DISCOVERY_PARSING.reset(token)


class _ReceiptDiscoveryParsing:
    """Keep only the latest content digest and encoded descriptor per receipt path."""

    def __init__(self):
        self.schema = COMPILE_RECEIPT_V4_SCHEMA.read_bytes()
        self.entries = {}

    def _require_schema(self):
        if COMPILE_RECEIPT_V4_SCHEMA.read_bytes() != self.schema:
            raise ValueError("compile receipt schema changed during discovery")

    def source(self, path, raw, fields):
        self._require_schema()
        digest = sha256_bytes(raw)
        previous = self.entries.get(path)
        if previous is None or previous[0] != digest:
            source = validated_discovery_source(raw, fields)
            self._require_schema()
            self.entries[path] = (digest, canonical_json_bytes(source))
        return json.loads(self.entries[path][1])


def _receipt_version_failure_reason(raw: bytes) -> str:
    try:
        receipt_version_reader(raw)
    except UnsupportedCompileReceiptVersion:
        raise
    except (IndexError, KeyError, TypeError, ValueError, UnicodeDecodeError, json.JSONDecodeError) as error:
        return str(error)[:MAX_FAILURE_DETAIL_CHARS]
    return ""


def _unusable_receipt_reason(path: Path) -> str:
    """Why this receipt cannot be read, or "" when it reads fine."""
    try:
        raw = path.read_bytes()
    except OSError as error:
        return str(error)[:MAX_FAILURE_DETAIL_CHARS]
    version_failure = _receipt_version_failure_reason(raw)
    if version_failure:
        return version_failure
    fields = receipt_source_fields(raw)
    if fields is None:
        return "receipt does not declare the source it belongs to"
    return _parse_failure_reason(raw, fields)


def _parse_failure_reason(raw: bytes, fields: tuple[str, str]) -> str:
    try:
        parse_compile_receipt_version(raw, logical_path=fields[0], source_sha256=fields[1])
    except UnsupportedCompileReceiptVersion:
        raise
    except ValueError as error:
        return str(error)[:MAX_FAILURE_DETAIL_CHARS]
    return ""


def discard_unusable_receipts() -> list[str]:
    """Remove receipts that no longer parse, naming each one. Operator-only.

    A receipt is evidence that a source was compiled, and the contract is that
    an unreadable one is an error rather than a quiet "not compiled" — a
    corruption must not be papered over by recompiling. But a receipt written by
    a defective writer then blocks every later compile of the whole vault, so
    there has to be a way out that a person takes deliberately: this is it. What
    is lost is the record of a compile, not the pages, which the next pass
    rebuilds from the immutable daily.
    """
    directory = runtime_roots.daily_dir() / "receipts"
    if not directory.is_dir():
        return []
    discarded: list[str] = []
    checked = [(path, _unusable_receipt_reason(path)) for path in sorted(directory.glob("*.md"))]
    owners = receipt_owners()
    for path, reason in checked:
        if not reason:
            continue
        print(f"compile_memory: discarding {path.name}: {reason}", file=sys.stderr)
        path.unlink()
        discarded.append(path.name)
    _forget_discarded_days(discarded, owners=owners)
    return discarded


def _forget_discarded_days(discarded: Sequence[str], *, owners: dict[str, str] | None = None) -> None:
    """Take the days whose receipts were discarded out of the mirror.

    The mirror is only a cheap diagnostic copy of what the receipts say, but a
    day left in it is never offered to a compile again — so discarding a receipt
    without clearing the mirror left the day "compiled" with no evidence, which
    is the one thing the receipt contract forbids. The day is found from the
    receipt's file name, which is the identity of its source, so an unreadable
    receipt names its day as well as a readable one. Days recorded before
    receipts existed carry no discarded receipt and are left alone.
    """
    owners = receipt_owners() if owners is None else owners
    forgotten = sorted({owners[name] for name in discarded if name in owners})
    if not forgotten:
        return
    print(
        f"compile_memory: {len(forgotten)} day(s) are pending again: "
        + ", ".join(forgotten),
        file=sys.stderr,
    )
    update_state(lambda state: _drop_mirror_days(state, forgotten))


def _drop_mirror_days(state: dict, names: Sequence[str]) -> None:
    mirror = require_state_mapping(state, "compiled_daily_hashes")
    for name in names:
        mirror.pop(name, None)


def receipt_owners() -> dict[str, str]:
    """Which daily each receipt file name belongs to, by the name alone."""
    owners: dict[str, str] = {}
    dailies = frozenset(canonical_dailies())
    for path in dailies:
        record_receipt_owners(owners, path)
    for path in sorted((runtime_roots.daily_dir() / "receipts").glob("v4-*.md")):
        _record_declared_context_owner(owners, path, dailies)
    return owners


def _record_declared_context_owner(owners: dict[str, str], path: Path, dailies: frozenset[Path]) -> None:
    try:
        source = _declared_context_owner(path, dailies)
    except (OSError, IndexError, KeyError, TypeError, ValueError):
        return
    owners[path.name] = Path(source["logical_path"]).name


def _declared_context_owner(path: Path, dailies: frozenset[Path]) -> Mapping[str, object]:
    raw = read_stable_bytes(path, MAX_RECEIPT_BYTES, label="compile receipt owner")
    record = json.loads(raw.split(b"```json\n", 1)[1].split(b"\n```", 1)[0])
    source = record["source"]
    require_v4_descriptor(source)
    if record["schema_version"] != "compile-receipt/v4" or path.name != f"v4-{compile_context_source_identity(source)}.md":
        raise ValueError("declared context owner identity disagrees")
    if runtime_roots.vault_root() / source["logical_path"] not in dailies:
        raise ValueError("declared context owner is not a current daily")
    return source


def record_receipt_owners(owners: dict[str, str], path: Path) -> None:
    with open_optional_daily_source(path) as content:
        if content is not None:
            _record_daily_receipt_owners(owners, path, content)


def _record_daily_receipt_owners(owners, path, content):
    logical = path.relative_to(runtime_roots.vault_root()).as_posix()
    owners[f"{immutable_source_digest(content)}.md"] = path.name
    for part in iter_daily_parts(logical, content):
        owners[f"v3-{compile_source_identity(logical, part.sha256)}.md"] = path.name
        owners[context_receipt_path(v4_source_descriptor(part)).name] = path.name


def repair_compile_mirror(coordinator: MarkdownCoordinator, *, deadline: float = math.inf, selection=None) -> None:
    """Make the diagnostic mirror agree with the receipts, every pass.

    A vault that already carries the wrong digest would keep reporting a phantom
    backlog for ever, because the day is compiled and no compile will ever
    revisit it. Nothing here decides anything: the receipts already did, and
    this only writes down what they say.
    """
    require_compile_active(deadline, None)
    compiled = existing_receipt_selection(selection, coordinator, deadline)
    corrected = {}
    unreceipted = []
    for path in canonical_dailies():
        require_compile_active(deadline, None)
        whole = whole_daily_digest(path.relative_to(runtime_roots.vault_root()).as_posix(), compiled)
        if whole is None:
            unreceipted.append(path.name)
            continue
        corrected[path.name] = whole
    quarantined = _quarantine_only_days(unreceipted, coordinator, deadline=deadline)
    if corrected or quarantined:
        update_state_under_clock(lambda state: _apply_mirror_repair(state, corrected, quarantined), deadline)


def _quarantine_only_days(names: Sequence[str], coordinator: MarkdownCoordinator, *, deadline: float = math.inf) -> list[str]:
    """Days the mirror holds only because a quarantined batch once wrote them there.

    A quarantine commit writes candidates and no receipt, so such a day was
    skipped for ever. A mirror-only day from before receipts has no quarantine
    commit behind it and is left alone. See
    `docs/research/2026-09-25-a-quarantined-day-stays-pending.md`.
    """
    commits = load_state().get("compiled_daily_commits", {})
    if not isinstance(commits, dict):
        return []
    return [name for name in names if _committed_by_quarantine(commits.get(name), coordinator, deadline=deadline)]


def _committed_by_quarantine(record: object, coordinator: MarkdownCoordinator, *, deadline: float = math.inf) -> bool:
    if not isinstance(record, dict) or not isinstance(record.get("sequence"), int):
        return False
    operation_id = coordinator.operation_id_at(record["sequence"], deadline=deadline) or ""
    return operation_id.startswith(QUARANTINE_OPERATION_PREFIX)


def _apply_mirror_repair(state: dict, corrected: dict, quarantined: Sequence[str]) -> None:
    mirror = require_state_mapping(state, "compiled_daily_hashes")
    commits = require_state_mapping(state, "compiled_daily_commits")
    for name, digest in corrected.items():
        mirror[name] = digest
    for name in quarantined:
        mirror.pop(name, None)
        commits.pop(name, None)


def select_dailies(
    args: argparse.Namespace,
    state: dict,
    *,
    coordinator: MarkdownCoordinator,
    deadline: float = math.inf,
    selection=None,
) -> list[Path]:
    selection = existing_receipt_selection(selection, coordinator, deadline)
    if args.file:
        return explicit_daily(Path(args.file).resolve(), coordinator, selection=selection)
    return [
        path
        for path in offered_dailies(args)
        if not daily_already_compiled(path, selection)
    ]


def offered_dailies(args: argparse.Namespace) -> list[Path]:
    """Every day, or every closed day when the session start asked (`closed_daily_logs`)."""
    if getattr(args, "closed_days_only", False):
        return closed_daily_logs(runtime_roots.daily_dir())
    return canonical_dailies()


def explicit_daily(path: Path, coordinator: MarkdownCoordinator, *, selection=None) -> list[Path]:
    _require_inside_daily_dir(path)
    if not path.is_file() or path.suffix.lower() != ".md":
        raise SystemExit(
            f"compile_memory: --file must be an existing .md daily log: {path}"
        )
    if daily_already_compiled(path, selection or receipt_predicate(coordinator)):
        return []
    return [path]


def _require_inside_daily_dir(path: Path) -> None:
    daily_root = runtime_roots.daily_dir().resolve()
    try:
        path.relative_to(daily_root)
    except ValueError as exc:
        raise SystemExit(
            f"compile_memory: --file must be under {daily_root}, got {path}"
        ) from exc


def daily_already_compiled(path: Path, selection) -> bool:
    logical_path = path.relative_to(runtime_roots.vault_root()).as_posix()
    with open_daily_source(path, deadline=getattr(selection, 'deadline', None)) as content:
        return daily_is_compiled(logical_path, content, selection)


def mark_started_unless_dry(args: argparse.Namespace) -> None:
    """A dry run writes nothing, so it moves no clock either.

    On 2026-09-23 a `--dry-run` rewrote `last_compile_started_at`/`finished_at`
    and outcome `nothing` over the last real compile's record.
    """
    if getattr(args, "dry_run", False):
        return
    _mark_started(args.trigger)


def mark_error_unless_dry(args: argparse.Namespace, error: BaseException) -> None:
    if getattr(args, "dry_run", False):
        return
    mark_finished(args.trigger, "error", f"{type(error).__name__}: {error}")


def mark_ok_unless_dry(
    args: argparse.Namespace, *, outcomes: Sequence[BatchOutcome] = ()
) -> None:
    if getattr(args, "dry_run", False):
        return
    mark_finished(args.trigger, "ok", outcomes=outcomes)


def _mark_started(trigger: str) -> None:
    started_iso = datetime.now().isoformat(timespec="seconds")

    def _mutate(s: dict) -> None:
        s["last_compile_started_at"] = started_iso
        s["last_compile_started_trigger"] = trigger
        s["last_compile_status"] = "running"
        s.pop("last_compile_error", None)

    update_state(_mutate)


def finished_outcome(status: str, outcomes: Sequence[BatchOutcome]) -> str:
    if status == "error":
        return "failed"
    return compile_outcome(outcomes)


def _mark_refused(trigger: str, reason: str) -> None:
    """A run that did not get the lock records its refusal, not the holder's status.

    See `docs/research/2026-09-14-the-small-integrity-gaps.md`.
    """
    refused_iso = datetime.now().isoformat(timespec="seconds")

    def _mutate(s: dict) -> None:
        s["last_compile_refused_at"] = refused_iso
        s["last_compile_refused_trigger"] = trigger
        s["last_compile_refused_reason"] = reason[:500]

    update_state(_mutate)


def mark_finished(
    trigger: str,
    status: str,
    error: str | None = None,
    *,
    outcomes: Sequence[BatchOutcome] = (),
) -> None:
    finished_iso = datetime.now().isoformat(timespec="seconds")

    def _mutate(s: dict) -> None:
        s["last_compile_finished_at"] = finished_iso
        s["last_compile_finished_trigger"] = trigger
        s["last_compile_status"] = status
        s["last_compile_outcome"] = finished_outcome(status, outcomes)
        s["last_compile_dropped_claims"] = len(DROPPED_CLAIMS)
        if error is not None:
            s["last_compile_error"] = error[:500]
        else:
            s.pop("last_compile_error", None)

    update_state(_mutate)
    # Clear the maybe_compile lock so the next trigger knows we're done; a
    # lock never expires by age, only with its process.
    _clear_compile_lock()


def _clear_compile_lock() -> None:
    """Clear the maybe_compile PID lock — only if we own it.

    Refuses to delete a lock owned by another live process: that lock may
    belong to a newer compile spawned after a stale-lock steal. A PID-0
    placeholder is cleared only when its owner token proves we wrote it;
    otherwise it is left for the PID-0 TTL to handle.
    """
    try:
        lock_file = runtime_roots.state_root() / "run" / "compile.pid"
        lines = lock_lines(lock_file)
        if lines is None:
            return
        if _lock_is_ours(lines):
            _unlink_quietly(lock_file)
    except OSError:
        pass


def lock_lines(lock_file: Path) -> list[str] | None:
    """The lock's lines, or None when there is nothing left to decide."""
    if not lock_file.exists():
        return None
    text = lock_file.read_text(encoding="utf-8").strip()
    if not text:
        _remove_abandoned_empty_lock(lock_file)
        return None
    return text.splitlines()


def _remove_abandoned_empty_lock(lock_file: Path) -> None:
    """An empty lock inside the spawn window belongs to a writer still writing it.

    Research: docs/research/2026-09-17-a-lock-names-the-process-not-only-its-number.md
    """
    if compile_lock.file_age(lock_file) <= compile_lock.PID0_TTL_SECONDS:
        return
    _unlink_quietly(lock_file)


def _lock_is_ours(lines: list[str]) -> bool:
    """Unreadable, our own PID, or a dead owner; a placeholder is a spawner's."""
    pid = _lock_pid(lines)
    if pid is None or pid == os.getpid():
        return True
    if pid == 0:
        return False
    return not process_liveness.owner_alive(pid, _lock_identity(lines))


def _lock_identity(lines: list[str]) -> str:
    """The owner's process start identity, absent in a lock of three lines."""
    if len(lines) <= 3:
        return ""
    return lines[3].strip()


def _lock_pid(lines: list[str]) -> int | None:
    """None means the lock is unreadable, which makes it ours to remove."""
    try:
        return int(lines[0].strip())
    except (IndexError, ValueError):
        return None


def _unlink_quietly(path: Path) -> None:
    try:
        path.unlink()
    except OSError:
        pass


def report_deprecated_flags(args: argparse.Namespace) -> None:
    """Say plainly that a flag does nothing rather than letting it look busy."""
    if not args.all:
        return
    print(
        "compile_memory: --all is deprecated and does nothing: every pending "
        "daily log is compiled anyway, and a day with a committed receipt is "
        "never compiled again. The flag will be removed.",
        file=sys.stderr,
    )


def compile_under_lock(
    args: argparse.Namespace,
    *,
    deadline: float = float("inf"),
    cancelled: Callable[[], bool] | None = None,
    owner: OwnerLease | None = None,
    execute,
) -> int:
    """Guard both compile executors: lock, start stamp, call ceiling, release.

    The command line and the in-process entry (the MCP `compile` tool) both come
    through here; the leased worker supplies its durable executor. The in-process
    entry used to call `_run` bare: it ran beside a
    spawned compile, drafted under the 90s default, and its finish stamp
    overwrote the status of the compile still running.
    Research: docs/research/2026-09-17-every-compile-takes-the-compile-lock.md
    """
    lock_token, refusal = acquire_compile_lock(getattr(args, "lock_token", None))
    if lock_token is None:
        print(f"compile_memory: not running: {refusal}", file=sys.stderr)
        _mark_refused(args.trigger, refusal)
        return 1
    mark_started_unless_dry(args)
    try:
        with receipt_discovery_scope(), call_ceiling(setting_value("provider.draft_ceiling_seconds")):
            return execute(args, deadline=deadline, cancelled=cancelled, owner=owner)
    except BaseException as e:  # noqa: BLE001
        mark_error_unless_dry(args, e)
        raise
    finally:
        release_compile_lock(lock_token)


def leased_compile_args(lease):
    args = argparse.Namespace(file=None, all=False, dry_run=False, trigger='auto',
                              closed_days_only=False, compile_request=None)
    if 'compile_request' not in lease.payload:
        return args  # Historical queue tasks meant all pending daily logs.
    if set(lease.payload) != {'compile_request'}:
        raise ValueError('compile request payload has unknown fields')
    request = _validated_compile_request(lease.payload['compile_request'])
    args.file = _compile_request_live_path(request['file'])
    args.closed_days_only = request['closed_days_only']
    args.trigger = request['trigger']
    args.compile_request = request
    return args


def _validated_compile_request(document):
    fields = {'schema', 'file', 'closed_days_only', 'trigger'}
    if not isinstance(document, dict) or set(document) != fields:
        raise ValueError('compile request has invalid fields')
    if document['schema'] != 'compile-request/v1':
        raise ValueError('compile request schema is unsupported')
    _require_compile_request_options(document)
    return dict(document)


def _require_compile_request_options(document):
    if not isinstance(document['trigger'], str) or document['trigger'] not in {'auto', 'manual'}:
        raise ValueError('compile request trigger must be auto or manual')
    if type(document['closed_days_only']) is not bool:
        raise ValueError('compile request closed_days_only must be boolean')
    _compile_request_file(document['file'])


def _compile_request_file(value):
    from llm_wiki.queue_sources import require_canonical_daily_id

    if value is None:
        return None
    if not isinstance(value, str) or re.fullmatch(r'knowledge/daily/\d{4}-\d{2}-\d{2}\.md', value) is None:
        raise ValueError('compile request file must name a canonical daily source')
    try:
        require_canonical_daily_id(Path(value).stem)
    except ValueError as error:
        raise ValueError('compile request file has an invalid daily date') from error
    return value


def _compile_request_live_path(logical_path):
    if logical_path is None:
        return None
    return str(runtime_roots.vault_root() / logical_path)


def _compile_cli_file(value):
    if value is None:
        return None
    path = Path(value).resolve()
    _require_inside_daily_dir(path)
    return _compile_request_file(path.relative_to(runtime_roots.vault_root().resolve()).as_posix())


def compile_task_payload(args):
    """Encode source intent only; lock ownership and dry-run never enter this task."""
    if getattr(args, 'dry_run', False):
        raise ValueError('compile dry-run cannot become a mutating queue task')
    request = {'schema': 'compile-request/v1', 'file': _compile_cli_file(args.file),
               'closed_days_only': getattr(args, 'closed_days_only', False), 'trigger': args.trigger}
    return {'compile_request': _validated_compile_request(request)}


def require_leased_compile_queue(queue, lease):
    from compile_progress import QUEUE_SCHEMA_VERSION

    version = getattr(getattr(queue, 'expected_contract', None), 'user_version', None)
    if (lease.kind, lease.handler_version, version) != ('compile', 1, QUEUE_SCHEMA_VERSION):
        raise ValueError('leased compile requires compile handler version 1 in the adopted compile journal')
    queue.compile_origin(lease)


def leased_compile_request(args, planning):
    selection = getattr(args, 'compile_request', None)
    if selection is not None:
        return {'schema': 'leased-compile-request/v2', 'planning': planning.request(),
                'trigger': args.trigger, 'selection': dict(selection)}
    return {'schema': 'leased-compile-request/v1', 'planning': planning.request(),
            'trigger': args.trigger, 'selection': 'all_pending_daily_logs'}


def require_resolved_run_planning(planning):
    if not any(item.resolution_failure is None for item in planning.candidates):
        raise CompilePlanUnavailable('no provider has usable compile planning evidence')


def finish_leased_compile(args, completed):
    if completed is None:
        error = str(load_state().get('last_compile_error', 'compile work has unresolved sources'))
        mark_finished(args.trigger, 'error', error)
        return 1
    count = completed.details['work_completion']['batch_count']
    outcomes = (BatchOutcome(0, 'published'),) if count else ()
    mark_finished(args.trigger, 'ok', outcomes=outcomes)
    print(f'compile_memory: verified completion of {count} source group(s).')
    return 0


SPAWNED_LOCK = "spawned"


def acquire_compile_lock(spawn_token: str | None = None) -> tuple[str | None, str]:
    """Claim the compile lock for a direct run: (lock handle, reason).

    The handle is the owner token when this run claimed the lock,
    `SPAWNED_LOCK` when the spawner wrote it for us and keeps its lifecycle,
    None when the run is refused — another compile holds the lock, or the
    lock could not be taken or read. Doubt refuses: two compiles writing one
    daily log is worse than one late compile. The token travels in the
    return value, not in a module global (audit OPS-22).
    Research: docs/research/2026-09-10-a-lock-lives-as-long-as-its-process-not-thirty-minutes.md
    """
    try:
        if compile_lock.claim_lock():
            return (_claim_direct_lock(), "claimed")
        if _spawned_lock_is_ours(spawn_token):
            return (SPAWNED_LOCK, "spawned")
        return (None, f"lock held by another compile ({compile_lock.lock_state()[1]})")
    except Exception as exc:  # noqa: BLE001 - any lock failure refuses the run
        return (None, f"compile lock unavailable ({type(exc).__name__}: {exc})")


def _claim_direct_lock() -> str:
    """Replace the PID-0 placeholder with our PID; the new token is the handle."""
    compile_lock.write_lock(os.getpid())
    return compile_lock.lock_owner_token() or ""


def _spawned_lock_is_ours(spawn_token: str | None = None) -> bool:
    """The lock the spawner wrote for us: our PID, or the token it handed us.

    A child that reaches the lock before its spawner replaced the PID-0
    placeholder used to refuse itself, and the night lost that compile.
    Research: docs/research/2026-09-17-a-lock-names-the-process-not-only-its-number.md
    """
    lock = compile_lock.read_lock()
    if not lock:
        return False
    return lock.get("pid") == os.getpid() or _token_matches(lock, spawn_token)


def _token_matches(lock: dict, spawn_token: str | None) -> bool:
    return bool(spawn_token) and lock.get("owner") == spawn_token


def release_compile_lock(lock_token: str | None) -> None:
    """maybe_compile owns the lifecycle of a lock it wrote for a spawned run."""
    if not lock_token or lock_token == SPAWNED_LOCK:
        return
    try:
        compile_lock.clear_lock(lock_token)
    except Exception as exc:  # noqa: BLE001 - reported, never hidden
        print(f"compile_memory: compile lock not released ({exc})", file=sys.stderr)


def whole_daily_digest(logical_path: str, compiled) -> str | None:
    """The digest of the file itself, once every part of it has a receipt."""
    with open_optional_daily_source(runtime_roots.vault_root() / logical_path, deadline=getattr(compiled, 'deadline', None)) as content:
        if content is None:
            return None
        if not daily_is_compiled(logical_path, content, compiled):
            return None
        return immutable_source_digest(content)


def update_state_under_clock(mutator: Callable[[dict], None], deadline: float) -> None:
    require_compile_active(deadline, None)
    if math.isinf(deadline):
        update_state(mutator)
        return
    update_state(mutator, lock_timeout=max(0.0, deadline - time.monotonic()))
    require_compile_active(deadline, None)


def require_state_mapping(state: dict[str, object], key: str) -> dict:
    value = state.setdefault(key, {})
    if not isinstance(value, dict):
        raise ValueError(f"{key} must be a mapping")
    return value
