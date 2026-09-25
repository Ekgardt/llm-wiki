#!/usr/bin/env python3
"""Retire the transcripts the memory's own provider calls left under `~/.claude/projects`.

Until 2026-09-14 every memory call through `claude -p` was saved by the CLI as a
session outside the vault (entry point `sdk-cli`). The flag `--no-session-persistence`
stopped new ones; the residue stayed until an operator deleted it by hand on
2026-09-23 (1 082 transcripts, 101 MB, and 113 empty project directories). A
transcript is the memory's own when its records name `sdk-cli` and a working
directory the memory calls from: the vault, the platform's temporary directory, or
the host's job directory. A session someone held (`cli`) is never touched, nor is a
programmatic call made from anywhere else. Run by the nightly pass.
Research: `docs/research/2026-09-23-the-memory-retires-its-own-residue.md`.
"""

from __future__ import annotations

import json
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from ephemeral_paths import (  # noqa: E402
    ephemeral_roots,
    host_directory,
    is_under,
    resolved,
    spellings,
)
from retire_lsp_evidence import SCAN_BUDGET_SECONDS  # noqa: E402

OWN_CALL_ENTRYPOINT = "sdk-cli"
HEAD_LINES = 40  # the entry point is in the first records of a transcript
MAX_TRANSCRIPTS_PER_PASS = 2000


def projects_directory() -> Path:
    return host_directory() / "projects"


def _raw_roots(vault_root: Path) -> tuple[Path, ...]:
    return (Path(vault_root), *ephemeral_roots())


def own_call_roots(vault_root: Path) -> tuple[Path, ...]:
    """Where the memory runs its calls from; a transcript from elsewhere is not ours."""
    return tuple(resolved(root) for root in _raw_roots(vault_root))


def root_spellings(vault_root: Path) -> tuple[Path, ...]:
    """Every way a root is written: as given and resolved (macOS `/var` is `/private/var`)."""
    return spellings(_raw_roots(vault_root))


def _decoded(line: str) -> dict:
    try:
        value = json.loads(line)
    except (ValueError, TypeError):
        return {}
    return value if isinstance(value, dict) else {}


def _first_entry(path: Path) -> dict:
    """The first record that names an entry point, or nothing."""
    with path.open(encoding="utf-8", errors="replace") as handle:
        for _ in range(HEAD_LINES):
            line = handle.readline()
            if not line:
                return {}
            entry = _decoded(line)
            if entry.get("entrypoint"):
                return entry
    return {}


def is_own_call(path: Path, roots: tuple[Path, ...]) -> bool:
    """`sdk-cli` from one of the memory's working directories; anything else is kept."""
    try:
        entry = _first_entry(path)
    except OSError:
        return False
    if entry.get("entrypoint") != OWN_CALL_ENTRYPOINT:
        return False
    cwd = entry.get("cwd")
    return isinstance(cwd, str) and is_under(Path(cwd), roots)


def _encoded(root: Path) -> str:
    """How the CLI names a project directory: separators, drive colons and dots become `-`."""
    return re.sub(r"[\\/:.]", "-", str(root))


def _own_empty_directory(directory: Path, spellings: tuple[Path, ...]) -> bool:
    if not directory.is_dir() or any(directory.iterdir()):
        return False
    return any(directory.name.startswith(_encoded(root)) for root in spellings)


def _bounded_transcripts(projects: Path, deadline: float) -> list[Path]:
    found: list[Path] = []
    for path in projects.glob("*/*.jsonl"):
        if len(found) >= MAX_TRANSCRIPTS_PER_PASS or time.monotonic() >= deadline:
            break
        found.append(path)
    return found


def _remove_own_transcripts(projects: Path, roots: tuple[Path, ...]) -> int:
    deadline = time.monotonic() + SCAN_BUDGET_SECONDS
    own = [p for p in _bounded_transcripts(projects, deadline) if is_own_call(p, roots)]
    for path in own:
        path.unlink(missing_ok=True)
    return len(own)


def _remove_emptied_directories(projects: Path, spellings: tuple[Path, ...]) -> int:
    emptied = [d for d in sorted(projects.iterdir()) if _own_empty_directory(d, spellings)]
    for directory in emptied:
        directory.rmdir()
    return len(emptied)


def retire(projects: Path, vault_root: Path) -> tuple[int, int]:
    """Remove the memory's own transcripts and its emptied project directories."""
    if not projects.is_dir():
        return 0, 0
    removed = _remove_own_transcripts(projects, own_call_roots(vault_root))
    return removed, _remove_emptied_directories(projects, root_spellings(vault_root))


def main() -> int:
    from memory_state import ROOT

    removed, emptied = retire(projects_directory(), ROOT)
    print(
        f"retire_own_call_transcripts: removed {removed} transcript(s) of the memory's "
        f"own calls and {emptied} emptied project director(ies)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
