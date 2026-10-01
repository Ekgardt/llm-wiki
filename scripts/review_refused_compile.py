"""Explicitly reject an unapplied compile draft while retaining its evidence.

A review is not a successful retry, a compile receipt, or permission to delete
transaction artifacts. Only an operator invokes this command after reviewing
all intended output. Automatic recovery never writes these decisions.
"""
from __future__ import annotations

import argparse
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path

from reliable_memory import (
    canonical_json_bytes,
    publish_runtime_file,
    read_runtime_bytes,
    sha256_bytes,
)


def _review_path(state_root, identifier):
    if re.fullmatch(r"[0-9a-f]{32}", identifier) is None:
        raise ValueError("invalid transaction identifier")
    return Path(state_root) / "run" / "transactions" / identifier / "operator-review.json"


def _binding(database, identifier):
    row = database.execute(
        'SELECT operation_id,request_hash,plan_hash,error_code FROM "transaction" '
        "WHERE id=? AND state='quarantined'", (identifier,),
    ).fetchone()
    if row is None or not row[0].startswith("compile:"):
        raise ValueError("review requires a quarantined compile")
    return dict(zip(("operation_id", "request_hash", "plan_hash", "error_code"), row))


def _require_unapplied(database, identifier):
    rows = database.execute(
        'SELECT applied FROM operation WHERE transaction_id=?', (identifier,),
    ).fetchall()
    if not rows or any(row[0] != 0 for row in rows):
        raise ValueError("review cannot settle applied or missing operations")


def _required_text(value):
    if not isinstance(value, str) or not value.strip():
        raise ValueError("review requires nonempty actor and rationale")
    return value


def _read_record(path, state_root):
    raw = read_runtime_bytes(path, state_root, max_bytes=path.stat().st_size, owner_only=True)
    return json.loads(raw)


def _require_review(record, binding, identifier):
    expected = {"schema_version", "transaction_id", "disposition", "binding", "actor", "reason", "reviewed_at"}
    if set(record) != expected:
        raise ValueError("invalid review fields")
    identity = (record["schema_version"], record["transaction_id"], record["disposition"], record["binding"])
    if identity != ("compile-refusal-review/v1", identifier, "rejected", binding):
        raise ValueError("review does not bind this refused draft")
    _required_text(record["actor"])
    _required_text(record["reason"])
    _require_review_time(record["reviewed_at"])


def _require_review_time(value):
    stamp = datetime.fromisoformat(value)
    if stamp.tzinfo is None:
        raise ValueError("review timestamp requires timezone")


def reviewed_refusal(database, identifier, state_root):
    """Fail closed on missing, edited, incomplete, or inapplicable reviews."""
    try:
        path = _review_path(state_root, identifier)
        record = _read_record(path, state_root)
        binding = _binding(database, identifier)
        _require_unapplied(database, identifier)
        _require_review(record, binding, identifier)
        plan = _read_plan_bytes(path.parent / "plan.json", state_root)
        return sha256_bytes(plan) == binding["plan_hash"]
    except (OSError, ValueError, KeyError, TypeError):
        return False


def review_allows_replay(database, identifier, state_root):
    if not os.path.lexists(_review_path(state_root, identifier)):
        return True
    if not reviewed_refusal(database, identifier, state_root):
        raise ValueError("invalid operator review requires inspection before replay")
    return False


def _read_plan_bytes(path, state_root):
    return read_runtime_bytes(path, state_root, max_bytes=path.stat().st_size, owner_only=True)


def reject_draft(coordinator, identifier, *, actor, reason):
    """Publish one immutable operator decision; never change the refused plan."""
    actor, reason = _required_text(actor), _required_text(reason)
    path = _review_path(coordinator.state_root, identifier)
    with coordinator.writer_gate():
        coordinator._load_verified_plan(coordinator._record(identifier))
        with coordinator._connect() as database:
            binding = _binding(database, identifier)
            _require_unapplied(database, identifier)
        return _publish_review(path, coordinator.state_root, identifier, binding, actor, reason)


def _publish_review(path, state_root, identifier, binding, actor, reason):
    if path.exists():
        prior = _read_record(path, state_root)
        _require_review(prior, binding, identifier)
        if (prior["actor"], prior["reason"]) != (actor, reason):
            raise ValueError("immutable review already has a different decision")
        return prior
    record = {
        "schema_version": "compile-refusal-review/v1", "transaction_id": identifier,
        "disposition": "rejected", "binding": binding, "actor": actor, "reason": reason,
        "reviewed_at": datetime.now(timezone.utc).isoformat(),
    }
    publish_runtime_file(path, canonical_json_bytes(record), state_root=state_root, create_only=True)
    return record


def main():
    from markdown_transaction import active_markdown_coordinator
    from memory_state import ROOT, STATE_ROOT

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("transaction_id")
    parser.add_argument("--actor", required=True)
    parser.add_argument("--reason", required=True)
    args = parser.parse_args()
    coordinator = active_markdown_coordinator(Path(ROOT), Path(STATE_ROOT))
    record = reject_draft(coordinator, args.transaction_id, actor=args.actor, reason=args.reason)
    print(json.dumps({"transaction_id": record["transaction_id"], "disposition": "rejected"}))


if __name__ == "__main__":
    main()
