"""Import past agent transcripts as session records — an explicit operator action.

Session records started on 2026-08-23, so the vault remembers nothing that
happened before it. The transcripts are still on disk; this walks them once and
writes the same record the live capture writes, through the same transaction and
the same DLP boundary. Nothing is deleted and nothing is moved: the transcript
stays where it is.

It is a command, not a nightly step, because the cost is the operator's to
accept: the dry run prints how many sessions, how many bytes, and what is already
there, and writes nothing until `--apply`.

    uv run python scripts/backfill_sessions.py                # plan only
    uv run python scripts/backfill_sessions.py --apply        # write records

See knowledge/notes/session-evidence-retention-decision.md (MEM-08).
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from host_transcripts import host_transcript_roots  # noqa: E402
from memory_state import ROOT  # noqa: E402
from session_evidence import (  # noqa: E402
    evidence_relative_path,
    render_transcript,
    write_session_evidence,
)

# Where the hosts keep their sessions, a moved `CLAUDE_CONFIG_DIR` or `CODEX_HOME` included.
DEFAULT_SOURCE_ROOTS = host_transcript_roots()
# One transcript can be tens of megabytes; the record itself is bounded to 512 KB
# by the writer, so reading more than this only costs time.
MAX_TRANSCRIPT_BYTES = 8 * 1024 * 1024
MAX_TRANSCRIPTS = 10_000
TRANSCRIPT_SUFFIXES = (".jsonl", ".json")
# A memory call runs from a temporary directory with this prefix
# (`llm_client.provider_cwd`); its saved session is the vault's own prompt, not a
# conversation. See `docs/research/2026-09-14-a-memory-call-leaves-no-session.md`.
PROVIDER_CWD_MARKER = "llm-wiki-provider-"
# How many leading records may be read to find the session's working directory.
MAX_HEAD_RECORDS = 8


@dataclass
class Outcome:
    """What the pass did, in the terms the operator asked about."""

    scanned: int = 0
    written: int = 0
    present: int = 0
    empty: int = 0
    refused: int = 0
    bytes_written: int = 0
    unscanned: int = 0
    refused_sessions: list[str] = field(default_factory=list)

    def as_lines(self, applied: bool) -> list[str]:
        verb = "written" if applied else "to write"
        return [
            f"transcripts scanned: {self.scanned}",
            f"records {verb}: {self.written} ({self.bytes_written / 1024:.0f} KiB)",
            f"already present: {self.present}",
            f"nothing to keep: {self.empty}",
            f"refused by the writer: {self.refused}",
            *self._cap_lines(),
        ]

    def _cap_lines(self) -> list[str]:
        """The cap is named when it cut the scan, so a short pass is never silent."""
        if not self.unscanned:
            return []
        return [
            f"left unscanned by the {MAX_TRANSCRIPTS}-transcript cap: {self.unscanned}"
            " (run again after these are recorded)"
        ]


def _found_transcripts(roots: tuple[Path, ...]) -> list[Path]:
    found: list[Path] = []
    for root in roots:
        found.extend(_transcripts_under(root))
    return sorted(found)


def _transcripts(roots: tuple[Path, ...]) -> list[Path]:
    return _found_transcripts(roots)[:MAX_TRANSCRIPTS]


def _transcripts_under(root: Path) -> list[Path]:
    if not root.is_dir():
        return []
    return [path for path in root.rglob("*") if _conversation_file(path)]


def _conversation_file(path: Path) -> bool:
    if not path.is_file() or path.suffix.casefold() not in TRANSCRIPT_SUFFIXES:
        return False
    return not _memory_call(path)


def _memory_call(path: Path) -> bool:
    """A session saved by a memory call: named for, or started in, a provider directory."""
    if any(PROVIDER_CWD_MARKER in part for part in path.parts):
        return True
    return PROVIDER_CWD_MARKER in Path(_first_cwd(path)).name


def _head_lines(path: Path) -> list[str]:
    """The first records of a transcript, as lines; empty when it cannot be read."""
    try:
        with path.open("r", encoding="utf-8", errors="ignore") as handle:
            return [handle.readline(MAX_TRANSCRIPT_BYTES) for _ in range(MAX_HEAD_RECORDS)]
    except OSError:
        return []


def _first_cwd(path: Path) -> str:
    """The working directory the first records name (Claude `cwd`, Codex `payload.cwd`)."""
    return next((cwd for cwd in map(_record_cwd, _head_lines(path)) if cwd), "")


def _record_cwd(line: str) -> str:
    try:
        record = json.loads(line)
    except ValueError:
        return ""
    return _text(_field(record, "cwd")) or _text(_field(_field(record, "payload"), "cwd"))


def _field(record: object, key: str) -> object:
    return record.get(key) if isinstance(record, dict) else None


def _text(value: object) -> str:
    return value if isinstance(value, str) else ""


def _session_day(path: Path) -> str:
    """The local day the session ended, by the transcript's own timestamp.

    Local, not UTC: live capture stamps `captured_at` from `datetime.now().astimezone()`,
    and the record's directory is that day. A UTC day filed the same session under a
    different directory than live capture would, and "existing records are left alone"
    then wrote it a second time. See
    `docs/research/2026-09-17-the-six-capture-corrections-the-first-round-left.md`.
    """
    try:
        modified = path.stat().st_mtime
    except OSError:
        return "undated"
    return datetime.fromtimestamp(modified).date().isoformat()


def _record_session_id(path: Path) -> str:
    """The id the session calls itself by, or the file's stem when it names none.

    Live capture names a record by the host's session id. A Codex rollout file is
    named `rollout-<date>-<uuid>`, so naming the record by the file stem gave the same
    session two records, one per path.
    """
    return _first_session_id(path) or path.stem


def _first_session_id(path: Path) -> str:
    """The session id the first records carry (Claude `sessionId`, Codex `payload.id`)."""
    return next((found for found in map(_record_session_id_field, _head_lines(path)) if found), "")


def _record_session_id_field(line: str) -> str:
    try:
        record = json.loads(line)
    except ValueError:
        return ""
    return _text(_field(record, "sessionId")) or _text(_field(_field(record, "payload"), "id"))


def _read_transcript(path: Path) -> str:
    try:
        with path.open("r", encoding="utf-8", errors="ignore") as handle:
            return handle.read(MAX_TRANSCRIPT_BYTES)
    except OSError:
        return ""


def _fields(path: Path, day: str) -> dict[str, object]:
    return {
        "session": _record_session_id(path),
        "host": "claude" if ".claude" in path.parts else "codex",
        "event": "backfill",
        # The local day, as live capture records it; no offset is claimed for a
        # timestamp the transcript itself did not carry.
        "captured_at": f"{day}T00:00:00",
        "source_event_id": None,
    }


def _record_exists(vault: Path, day: str, session: str) -> bool:
    return (vault / evidence_relative_path(day, session)).exists()


def _plan_one(vault: Path, path: Path, outcome: Outcome) -> tuple[str, str] | None:
    """(day, transcript) when this session is worth a record, else None."""
    outcome.scanned += 1
    day = _session_day(path)
    if _record_exists(vault, day, _record_session_id(path)):
        outcome.present += 1
        return None
    transcript = _read_transcript(path)
    if not render_transcript(transcript).strip():
        outcome.empty += 1
        return None
    return day, transcript


def _write_one(vault: Path, path: Path, day: str, transcript: str, outcome: Outcome) -> None:
    written = write_session_evidence(vault, _fields(path, day), transcript)
    if written is None:
        outcome.refused += 1
        outcome.refused_sessions.append(path.stem)
        return
    outcome.written += 1
    outcome.bytes_written += written.stat().st_size


def _count_one(transcript: str, outcome: Outcome) -> None:
    """The dry run counts the record it would write, without writing it."""
    outcome.written += 1
    outcome.bytes_written += len(render_transcript(transcript).encode("utf-8"))


def backfill(vault: Path, roots: tuple[Path, ...], *, apply: bool) -> Outcome:
    """Write one record per past transcript; existing records are left alone."""
    outcome = Outcome()
    found = _found_transcripts(roots)
    outcome.unscanned = max(0, len(found) - MAX_TRANSCRIPTS)
    for path in found[:MAX_TRANSCRIPTS]:
        planned = _plan_one(vault, path, outcome)
        if planned is None:
            continue
        day, transcript = planned
        if not apply:
            _count_one(transcript, outcome)
            continue
        _write_one(vault, path, day, transcript, outcome)
    return outcome


def _parsed_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--apply", action="store_true", help="Write the records (default: plan only)"
    )
    parser.add_argument(
        "--source",
        action="append",
        default=None,
        help="Transcript directory; repeatable. Defaults to the agent session dirs.",
    )
    return parser.parse_args()


def _source_roots(values: list[str] | None) -> tuple[Path, ...]:
    if not values:
        return DEFAULT_SOURCE_ROOTS
    return tuple(Path(value).expanduser() for value in values)


def main() -> int:
    args = _parsed_arguments()
    started = time.perf_counter()
    outcome = backfill(ROOT, _source_roots(args.source), apply=args.apply)
    for line in outcome.as_lines(args.apply):
        print(line)
    print(f"seconds: {time.perf_counter() - started:.1f}")
    if not args.apply:
        print("\nPlan only. Re-run with --apply to write these records.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
