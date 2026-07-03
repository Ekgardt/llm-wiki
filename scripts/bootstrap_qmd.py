"""Bootstrap the QMD hybrid lex+vec index over the vault's content trees.

Registers collections for `wiki/`, `raw/`, and `inbox/`, runs an index
update, and generates embeddings. Idempotent — re-running is safe;
`qmd` de-duplicates collections and incrementally updates.

Replaces the earlier `bootstrap-qmd.ps1` which hardcoded
`Set-Location "$LLM_WIKI_ROOT"` and called a sibling `scripts/qmd.ps1`
helper that no longer exists. The current convention is to invoke
the `qmd` CLI directly from PATH; this script does that portably.

Usage:
    python scripts/bootstrap_qmd.py            # run all steps
    python scripts/bootstrap_qmd.py --only embed  # skip to embeddings

When to run:
- First-time setup on a new machine.
- After a major content reorganization (large batch of new wiki pages
  or raw sources added), if you want the QMD index to reflect it before
  the next scheduled refresh.

Prerequisites:
- `qmd` CLI on PATH. On Windows-only environments, prefer Git Bash so
  `qmd` (a POSIX-style shim) resolves correctly.
- `$LLM_WIKI_STATE_ROOT/qmd/` directory is writable (QMD stores
  `index.sqlite` there).

Safe to no-op: every step uses `qmd`'s own idempotent commands. If
`qmd` is not on PATH, the script prints a clear error and exits 1;
it does not try to heuristically locate the binary.
"""
from __future__ import annotations

import argparse
import io
import shutil
import subprocess
import sys
from pathlib import Path

# Force utf-8 on stdout — argparse's help output contains a `→` arrow
# that breaks on Windows cp1251 consoles otherwise.
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, io.UnsupportedOperation):
        pass

sys.path.insert(0, str(Path(__file__).resolve().parent))
from memory_state import ROOT  # noqa: E402

STEPS = ("collections", "update", "embed", "status")


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument(
        "--only",
        choices=STEPS,
        help=(
            "Run a single step and exit. Default: run all four steps in "
            "order (collections → update → embed → status)."
        ),
    )
    return p.parse_args()


def _run(*cmd: str) -> int:
    """Echo + run; return exit code. Stdout/stderr stream to console."""
    print(f"$ {' '.join(cmd)}")
    return subprocess.run(list(cmd)).returncode


def _run_capture(*cmd: str) -> tuple[int, str, str]:
    """Echo + run; capture stdout/stderr as strings. Both streams also
    echoed so the user sees the same output they would interactively.
    """
    print(f"$ {' '.join(cmd)}")
    proc = subprocess.run(list(cmd), capture_output=True, text=True)
    if proc.stdout:
        print(proc.stdout, end="")
    if proc.stderr:
        print(proc.stderr, end="", file=sys.stderr)
    return proc.returncode, proc.stdout or "", proc.stderr or ""


def _require_qmd() -> None:
    if shutil.which("qmd") is None:
        print(
            "bootstrap_qmd: `qmd` not found on PATH.\n"
            "  On Windows, try running this script from Git Bash where the\n"
            "  qmd shim resolves correctly, or install qmd globally.",
            file=sys.stderr,
        )
        sys.exit(1)


# Stdout/stderr patterns that confirm a non-zero exit was merely the
# collection already being registered — safe to continue. Anything else
# is treated as a real failure.
_ALREADY_EXISTS_MARKERS = (
    "already exists",
    "already registered",
    "duplicate collection",
)


def add_collections() -> int:
    """Register collections for wiki/, raw/, inbox/.

    Idempotent in the narrow sense: a pre-existing collection with the
    same name is treated as benign (stdout/stderr must explicitly say
    "already exists" / "already registered" / "duplicate collection").
    ANY OTHER non-zero exit — path not found, permission denied, DB
    locked, qmd binary crash — fails the whole bootstrap step. This
    avoids the earlier "every failure looks like success" footgun the
    audit flagged.
    """
    for name in ("wiki", "raw", "inbox"):
        target = ROOT / name
        if not target.is_dir():
            print(f"  skip `{name}` — directory missing at {target}")
            continue
        rc, stdout, stderr = _run_capture(
            "qmd", "collection", "add", str(target), "--name", name
        )
        if rc == 0:
            continue
        blob = (stdout + "\n" + stderr).lower()
        if any(marker in blob for marker in _ALREADY_EXISTS_MARKERS):
            print(f"  (non-fatal: `{name}` is already registered)")
            continue
        # Non-zero with no "already exists" hint → real failure.
        print(
            f"bootstrap_qmd: `qmd collection add {name}` failed (rc={rc}) "
            f"with no already-exists marker in output — aborting.",
            file=sys.stderr,
        )
        return rc
    return 0


def update_index() -> int:
    return _run("qmd", "update")


def generate_embeddings() -> int:
    return _run("qmd", "embed")


def show_status() -> int:
    return _run("qmd", "status")


def main() -> int:
    args = parse_args()
    _require_qmd()

    dispatch = {
        "collections": add_collections,
        "update": update_index,
        "embed": generate_embeddings,
        "status": show_status,
    }

    if args.only:
        return dispatch[args.only]()

    # Full run. Every step aborts the full bootstrap on non-zero —
    # including `collections`. `add_collections()` already normalizes
    # benign "already registered" outcomes to rc=0 internally; any
    # non-zero from it now represents a real failure (missing path,
    # DB lock, permission, qmd crash) that must stop the run so the
    # operator doesn't mistake partial progress for success. Earlier
    # versions special-cased `step != "collections"` here — left
    # over from when tolerance lived in this loop instead of inside
    # `add_collections()`. That split responsibility is gone now.
    for step in STEPS:
        print(f"\n── {step} ──")
        rc = dispatch[step]()
        if rc != 0:
            print(f"bootstrap_qmd: step `{step}` failed (rc={rc})", file=sys.stderr)
            return rc

    print("\nbootstrap_qmd: done.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
