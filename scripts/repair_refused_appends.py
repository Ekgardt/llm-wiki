"""Replay the delta of an append that a race refused, as a new attempt.

A daily append is written as a whole-file replace with a precondition on the
bytes it read. When another session lands first the precondition fails, the
attempt is quarantined, and the block it carried — a tool breadcrumb, a
checkpoint line — is written nowhere. Measured on this vault on 2026-09-07:
nine such attempts stood open, six of them tool breadcrumbs for
`knowledge/daily/2026-09-05.md`, each one a marker line and a block of about
250 bytes that nothing wrote since.

The block is recoverable, because a refused transaction keeps both images and
an append-shaped replace is one whose before-image is a prefix of its after-
image: the delta *is* the block. Replaying the delta is the retry the
quarantine decision already names — a new operation with the next ordinal
that names the refused transaction as its parent, so `doctor` closes the
refusal by lineage (`idempotent-retry-after-quarantine-decision`). Every
block opens with its `<!-- llm-wiki-operation:… -->` marker, so a block
already present is recognised and never appended twice — the deduplication by
event id that event-store replays rely on.

A replace whose before-image is not a prefix of its after-image is not an
append and is left alone; so is anything refused by the DLP boundary, which
is a decision and not an accident. Report by default; `--apply` writes.
See `docs/research/2026-09-07-a-refused-append-is-owed-its-delta.md`.
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from markdown_transaction import (  # noqa: E402
    _image_bytes,
    active_or_legacy_coordinator,
    append_knowledge,
)
from memory_state import ROOT, STATE_ROOT  # noqa: E402
from reliable_memory import sha256_bytes  # noqa: E402

# Daily entries only. A project journal is also append-shaped, but its
# events carry sequence numbers and a retry rewrites them, so a refused
# journal append whose retry committed looks owed by its bytes and is not:
# four of them did on this vault. The journal has its own gap repair.
KNOWLEDGE = "knowledge/daily/"
REPLAYABLE = "precondition_failed"
_MARKER = re.compile(r"<!-- llm-wiki-operation:[0-9a-f]{64} -->")


@dataclass(frozen=True)
class OwedAppend:
    transaction_id: str
    operation_id: str
    path: str
    delta: bytes


def _quarantined(database: sqlite3.Connection) -> list[tuple[str, str]]:
    """Refused races that no retry in their lineage ever closed."""
    from doctor import _resolved_by_lineage

    retried = _resolved_by_lineage(database)
    rows = database.execute(
        'SELECT id, operation_id FROM "transaction" WHERE state=\'quarantined\' '
        "AND error_code = ? ORDER BY created_at",
        (REPLAYABLE,),
    )
    return [(str(row[0]), str(row[1])) for row in rows if str(row[0]) not in retried]


def _plan_of(directory: Path) -> dict | None:
    plan = directory / "plan.json"
    if not plan.is_file():
        return None
    try:
        return json.loads(plan.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _artifact_of(directory: Path, descriptor: object) -> Path | None:
    if not isinstance(descriptor, dict):
        return None
    artifact = directory / str(descriptor.get("artifact", ""))
    if not artifact.is_file():
        return None
    return artifact


def _image(directory: Path, descriptor: object) -> bytes | None:
    """One planned image, only if it still hashes to what the plan recorded."""
    artifact = _artifact_of(directory, descriptor)
    if artifact is None:
        return None
    content = _image_bytes(artifact)
    if sha256_bytes(content) != descriptor.get("sha256"):
        return None
    return content


def _is_knowledge_replace(operation: dict) -> bool:
    if operation.get("kind") != "replace":
        return False
    return str(operation.get("path", "")).startswith(KNOWLEDGE)


def _tail_added(before: bytes | None, after: bytes | None) -> bytes | None:
    """The bytes a replace added, when it added and changed nothing else."""
    if before is None or after is None:
        return None
    if not after.startswith(before):
        return None
    return after[len(before):]


def _delta_of(directory: Path, operation: dict) -> bytes | None:
    if not _is_knowledge_replace(operation):
        return None
    before = _image(directory, operation.get("before"))
    after = _image(directory, operation.get("after"))
    return _tail_added(before, after)


def _already_present(delta: bytes, live: Path) -> bool:
    """The block is there when its marker is, or its bytes are, in the file."""
    try:
        content = live.read_bytes()
    except OSError:
        return False
    marker = _MARKER.search(delta.decode("utf-8", errors="replace"))
    needle = marker.group(0).encode("utf-8") if marker else delta
    return needle in content


def _owed_in(directory: Path, identity: tuple[str, str], vault: Path) -> list[OwedAppend]:
    plan = _plan_of(directory)
    operations = plan.get("operations", []) if isinstance(plan, dict) else []
    owed = []
    for operation in operations:
        delta = _delta_of(directory, operation)
        if not delta or _already_present(delta, vault / str(operation["path"])):
            continue
        owed.append(OwedAppend(identity[0], identity[1], str(operation["path"]), delta))
    return owed


def _collect(coordinator, vault: Path) -> list[OwedAppend]:
    with coordinator._connect() as database:  # noqa: SLF001
        identities = _quarantined(database)
    found: list[OwedAppend] = []
    for identity in identities:
        found.extend(_owed_in(coordinator.transaction_root / identity[0], identity, vault))
    return found


def _replay(vault: Path, owed: list[OwedAppend]) -> None:
    """The same operation id: the coordinator hands the next ordinal and the parent."""
    for item in owed:
        record = append_knowledge(item.operation_id, vault / item.path, item.delta)
        print(f"appended {len(item.delta)} bytes to {item.path} ({record.state})")


def _report(owed: list[OwedAppend]) -> None:
    if not owed:
        print("nothing owed: no refused append is missing its block")
        return
    print(f"{len(owed)} block(s) a refused append carried and nothing wrote since:")
    for item in owed:
        print(f"  {item.path}  ({len(item.delta)} bytes, from {item.transaction_id})")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply", action="store_true", help="append the blocks; otherwise only report"
    )
    args = parser.parse_args(argv)
    vault = Path(ROOT)
    coordinator = active_or_legacy_coordinator(vault, Path(STATE_ROOT))
    owed = _collect(coordinator, vault)
    _report(owed)
    if args.apply:
        _replay(vault, owed)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
