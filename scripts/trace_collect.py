#!/usr/bin/env python3
"""Turn one `cProfile` profile into a bounded `execution-trace/v1` edge list.

`CODE-09`. This is the *collector* half of trace ingestion, and the split from
`trace_ingest.py` is the security design, not tidiness.

`cProfile` serialises with `marshal`, which is documented as not secure against
maliciously constructed data. A trace file is untrusted input by assumption, so
the ingester must never unmarshal one. This module does the unmarshalling, and
it is **trusted-input only**: run it on a profile you produced yourself,
seconds earlier, in your own process. It writes a flat JSON-lines file, which
is what the ingester reads.

Why `cProfile` at all: `pstats.Stats.stats` already holds a caller -> callee
edge set with exact call counts, keyed by `(filename, first line number,
function name)`. That key binds exactly to this vault's graph, whose function
and method definitions are stored as occurrences carrying the `def` line.
Names alone would not: this repository's live generation holds 296 methods
called `__init__`. See
`docs/research/2026-08-28-what-an-execution-trace-proves.md`.

Collect and convert:

    uv run python -m cProfile -o /tmp/suite.prof -m pytest tests/test_claims.py -q
    uv run python scripts/trace_collect.py /tmp/suite.prof /tmp/suite.trace.jsonl

The output names only repository-relative paths; frames outside the repository
(the standard library, site-packages, `<built-in>`) are dropped here rather
than refused later.
"""

from __future__ import annotations

import argparse
import json
import pstats
import sys
from pathlib import Path

TRACE_SCHEMA = "execution-trace/v1"

# Frame filenames `cProfile` uses for things that are not a file on disk.
_SYNTHETIC_FILENAMES = frozenset({"~", "", "<string>", "<stdin>", "<frozen importlib._bootstrap>"})


def _resolved_root(root: Path) -> Path:
    return Path(root).resolve(strict=True)


def _candidate_path(filename: str, root: Path) -> Path | None:
    if filename in _SYNTHETIC_FILENAMES:
        return None
    if filename.startswith("<"):
        return None
    candidate = Path(filename)
    return candidate if candidate.is_absolute() else root / candidate


def _relative_path(filename: str, root: Path) -> str | None:
    """Repository-relative POSIX path for a frame, or None when it is outside."""
    candidate = _candidate_path(filename, root)
    if candidate is None:
        return None
    try:
        resolved = candidate.resolve(strict=False)
    except OSError:
        return None
    try:
        return resolved.relative_to(root).as_posix()
    except ValueError:
        return None


def _frame(key: object, root: Path) -> dict[str, object] | None:
    """One `(filename, line, name)` pstats key as a validated trace frame."""
    if not isinstance(key, tuple) or len(key) != 3:
        return None
    relative = _relative_path(str(key[0]), root)
    if relative is None:
        return None
    return {"path": relative, "line": int(key[1]), "name": str(key[2])}


def _call_count(value: object) -> int:
    """pstats caller values are `(cc, nc, tt, ct)`; `profile` gives a bare int."""
    if isinstance(value, tuple):
        return int(value[0])
    return int(value)


def _edge(caller_key: object, callee: dict, value: object, root: Path) -> dict | None:
    caller = _frame(caller_key, root)
    if caller is None:
        return None
    count = _call_count(value)
    if count < 1:
        return None
    return {"caller": caller, "callee": callee, "count": count}


def _edges_into(callee_key: object, entry: object, root: Path) -> list[dict]:
    callee = _frame(callee_key, root)
    if callee is None:
        return []
    callers = entry[4]
    rows = [_edge(key, callee, value, root) for key, value in callers.items()]
    return [row for row in rows if row is not None]


def edges_from_profile(profile_path: Path, root: Path) -> list[dict]:
    """Every in-repository caller -> callee edge the profile recorded.

    Unmarshals `profile_path`. Trusted input only -- see the module docstring.
    """
    checkout = _resolved_root(root)
    stats = pstats.Stats(str(profile_path)).stats
    rows: list[dict] = []
    for callee_key, entry in stats.items():
        rows.extend(_edges_into(callee_key, entry, checkout))
    return rows


def _header(profile_path: Path, root: Path, count: int) -> dict[str, object]:
    return {
        "schema": TRACE_SCHEMA,
        "collector": "trace_collect/cProfile",
        "profile_name": Path(profile_path).name,
        "repository_root": str(root),
        "edge_count": count,
    }


def write_trace(rows: list[dict], destination: Path, profile_path: Path, root: Path) -> int:
    """Write the header line and one JSON object per edge. Returns the count."""
    lines = [json.dumps(_header(profile_path, root, len(rows)), sort_keys=True)]
    lines.extend(json.dumps(row, sort_keys=True) for row in rows)
    Path(destination).write_text("\n".join(lines) + "\n", encoding="utf-8")
    return len(rows)


def _parse_arguments(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("profile", type=Path, help="a cProfile .prof file you produced")
    parser.add_argument("destination", type=Path, help="the execution-trace/v1 file to write")
    parser.add_argument(
        "--root",
        type=Path,
        default=Path.cwd(),
        help="repository root that frame paths are made relative to",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    arguments = _parse_arguments(argv)
    root = _resolved_root(arguments.root)
    rows = edges_from_profile(arguments.profile, root)
    written = write_trace(rows, arguments.destination, arguments.profile, root)
    print(f"wrote {written} edge(s) to {arguments.destination}")
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    sys.exit(main())
