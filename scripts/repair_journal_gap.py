#!/usr/bin/env python3
"""Append a committed sequence the journal does not carry.

`ProjectJournalRebuildRequired` says in its own docstring that append-only
ordering needs an explicit verified rebuild, and until now there was none. A
sequence the coordinator calls committed while the journal ends before it stops
every later sequence for that project, permanently.

That state is not supposed to arise, and on this vault it arose because of a
change of mine that was reverted an hour later: `llm-wiki` 1429 was settled as
committed without its entry being written. The journal is the authority, so such
a row is a false record and its write is still owed.

The walk is deliberately narrow. Starting at the journal head, a sequence is
appended only when all three hold: the coordinator has a committed row at
exactly `head + 1`, the journal carries no record with that sequence, and the
project lease is held. It stops at the first sequence that fails any of them, so
it can never reorder a journal, never skip a sequence, and never append one the
journal already has. On a vault with no gap it does nothing.

See `docs/research/2026-08-30-a-sequence-nothing-can-settle.md`.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from memory_state import ROOT, STATE_ROOT  # noqa: E402
from project_journal import ProjectLeaseBusy, ProjectStore, _timestamp  # noqa: E402


def journal_sequences(store: ProjectStore, project: str) -> set[int]:
    content = store._read_journal_bytes(project)  # noqa: SLF001
    return {int(event["sequence"]) for event in store._journal_events(project, content)}  # noqa: SLF001


def _committed_sequences(store: ProjectStore, project: str) -> set[int]:
    with store.coordinator._connect() as database:  # noqa: SLF001
        rows = database.execute(
            "SELECT sequence FROM project_checkpoints WHERE project = ? "
            "AND state = 'committed'",
            (project,),
        ).fetchall()
    return {int(row["sequence"]) for row in rows}


def _projects(store: ProjectStore) -> list[str]:
    with store.coordinator._connect() as database:  # noqa: SLF001
        rows = database.execute(
            "SELECT DISTINCT project FROM project_checkpoints ORDER BY project"
        ).fetchall()
    return [str(row["project"]) for row in rows]


def missing_sequences(store: ProjectStore, project: str) -> list[int]:
    """The run of committed sequences the journal lacks, from its head forward."""
    written = journal_sequences(store, project)
    committed = _committed_sequences(store, project)
    head = max(written) if written else 0
    missing: list[int] = []
    while head + 1 in committed and head + 1 not in written:
        head += 1
        missing.append(head)
    return missing


def _append_under_lease(store: ProjectStore, project: str, sequence: int, lease) -> str:
    reservation = store.coordinator.retry_unsettled_sequence(
        project,
        sequence,
        {
            "project": project,
            "lease_token": lease.token,
            "fencing_epoch": lease.epoch,
            "expires_at": _timestamp(lease.expires_at),
        },
        include_committed=True,
    )
    if reservation is None:
        return "no row"
    receipt = store._replayed_under_lease(reservation, lease, None)  # noqa: SLF001
    return "appended" if receipt is not None else "not appended"


def append_one(store: ProjectStore, project: str, sequence: int) -> str:
    try:
        lease = store.acquire_lease(project, "journal-gap-repair")
    except ProjectLeaseBusy:
        return "project is leased by another invocation"
    try:
        return _append_under_lease(store, project, sequence, lease)
    finally:
        store._release(lease)  # noqa: SLF001


def _repair_project(store: ProjectStore, project: str) -> list[str]:
    lines: list[str] = []
    for sequence in missing_sequences(store, project):
        lines.append(f"{project} {sequence}: {append_one(store, project, sequence)}")
    return lines


def repair(store: ProjectStore, project: str | None = None) -> list[str]:
    projects = [project] if project else _projects(store)
    lines: list[str] = []
    for name in projects:
        lines.extend(_repair_project(store, name))
    return lines


def _survey(store: ProjectStore, project: str | None) -> dict[str, list[int]]:
    projects = [project] if project else _projects(store)
    found = {name: missing_sequences(store, name) for name in projects}
    return {name: gaps for name, gaps in found.items() if gaps}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--vault", default=str(ROOT))
    parser.add_argument("--state-root", default=str(STATE_ROOT))
    parser.add_argument("--project", default=None)
    parser.add_argument(
        "--list-only", action="store_true", help="name the gaps and change nothing"
    )
    args = parser.parse_args()
    store = ProjectStore(Path(args.vault), Path(args.state_root))
    if args.list_only:
        print(json.dumps(_survey(store, args.project), indent=1))
        return 0
    print("\n".join(repair(store, args.project)) or "no journal gap")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
