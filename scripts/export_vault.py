"""Build a clean export archive of the vault via `git archive`.

Makes the right thing the easy thing: one command produces a zip/tar
containing ONLY git-tracked files at the current HEAD. No `.venv/`,
no `.git/`, no `.pytest_cache/`, no `.obsidian/workspace.json`, no
`.claude/settings.local.json`, no `gitleaks-report.json` — all of
which are gitignored and cannot leak through `git archive` by
construction.

This is the scripted equivalent of the recipe documented in
`docs/EXPORTING.md`. The intent: make it harder to accidentally ship
a raw-folder zip that carries machine-local baggage.

Usage:
    python scripts/export_vault.py                      # produces llm-wiki-export-<shortsha>.zip
    python scripts/export_vault.py --output ../my.zip   # custom output path
    python scripts/export_vault.py --ref v1.2.0         # archive a tag or older commit
    python scripts/export_vault.py --format tar.gz      # tarball instead of zip
    python scripts/export_vault.py --verify             # post-build, list the archive and
                                                        #   fail if any forbidden path slipped in

Exit codes:
    0 — archive written and (if --verify) passed the forbidden-path check.
    1 — git archive failed, or verification found a forbidden path.
    2 — usage error (missing git, dirty working tree with --strict, etc.).

Why this exists: a colleague audit repeatedly flagged that the zip
they received contained `.venv/` (~300 MB), `settings.local.json`
(machine-local permissions), and other non-exportable artifacts.
Root cause: operator used `zip -r` on the folder, bypassing
`.gitignore`. Fix: make `git archive` the default path.
"""
from __future__ import annotations

import argparse
import io
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

# Force utf-8 on stdout — the description text contains a Unicode arrow.
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, io.UnsupportedOperation):
        pass

sys.path.insert(0, str(Path(__file__).resolve().parent))
from memory_state import ROOT  # noqa: E402

# Paths that MUST NOT appear in an export archive. All gitignored, so
# `git archive` cannot include them — this list is the verification
# post-check, catching archives built via other tools (e.g. `zip -r`).
#
# Each entry is (pattern, anchor):
#   "root"     — matches only when the archive entry STARTS WITH pattern
#                (e.g. "cache/" matches "cache/foo.md" but NOT
#                "knowledge/raw/cache-effects.md").
#   "anywhere" — substring match anywhere in the path (for specific files
#                like ".claude/settings.local.json").
#
# [L-001] Previous versions used bare `pattern in name` which caused
# false positives on legitimate nested paths (e.g. a knowledge page
# named "cache-effects.md" was blocked by the "cache/" pattern).
FORBIDDEN_PATH_PATTERNS: tuple[tuple[str, str], ...] = (
    # Top-level dirs (root-anchored — nested occurrences are NOT blocked).
    (".venv/", "root"),
    (".git/", "root"),
    (".pytest_cache/", "root"),
    ("__pycache__/", "root"),
    (".obsidian/", "root"),
    ("cache/", "root"),
    ("logs/", "root"),
    ("run/", "root"),
    ("state/", "root"),
    ("wiki/", "root"),
    ("memory/", "root"),
    ("outputs/", "root"),
    (".ci-lint-state/", "root"),
        ("LLM-wiki-state/", "root"),
    # Specific files (anywhere match — these are unique enough to be safe).
    (".obsidian/workspace.json", "anywhere"),
    (".claude/settings.local.json", "anywhere"),
    ("gitleaks-report.json", "anywhere"),
    ("gitleaks-report.sarif", "anywhere"),
)


def _is_forbidden(name: str, pattern: str, anchor: str) -> bool:
    """Check whether *name* matches a forbidden *pattern*.

    For ``"root"`` anchored patterns the entry must start with the pattern
    (so ``cache/`` matches ``cache/foo.md`` but not
    ``knowledge/raw/cache-effects.md``).  For ``"anywhere"`` patterns a
    plain substring check is used.
    """
    if anchor == "root":
        stripped = pattern.rstrip("/")
        return name == stripped or name.startswith(pattern)
    return pattern in name


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument(
        "--output", "-o",
        type=Path,
        help=(
            "Output archive path. Default: "
            "`<vault>/../llm-wiki-export-<shortsha>.<ext>`."
        ),
    )
    p.add_argument(
        "--ref",
        default="HEAD",
        help="Git ref to archive (branch / tag / SHA). Default: HEAD.",
    )
    p.add_argument(
        "--format",
        choices=["zip", "tar", "tar.gz"],
        default="zip",
        help="Archive format. Default: zip.",
    )
    p.add_argument(
        "--verify",
        action="store_true",
        default=True,
        help=(
            "After building, list archive contents and fail if any "
            "forbidden path is present. On by default — pass "
            "`--no-verify` to skip."
        ),
    )
    p.add_argument("--no-verify", dest="verify", action="store_false")
    p.add_argument(
        "--strict",
        action="store_true",
        help=(
            "Fail if the working tree has uncommitted changes. Without "
            "this flag, uncommitted changes are allowed — they simply "
            "won't be in the archive (`git archive` uses the ref, not "
            "the working copy)."
        ),
    )
    return p.parse_args()


def _run(*cmd: str, check: bool = True, capture: bool = False) -> subprocess.CompletedProcess:
    """Echo + run. By default fail-fast on non-zero."""
    print(f"$ {' '.join(cmd)}")
    return subprocess.run(
        list(cmd),
        cwd=str(ROOT),
        check=check,
        text=True,
        capture_output=capture,
    )


def _require_git() -> None:
    if shutil.which("git") is None:
        print("export_vault: `git` not found on PATH.", file=sys.stderr)
        sys.exit(2)


def _short_sha(ref: str) -> str:
    out = _run("git", "rev-parse", "--short", ref, capture=True)
    return out.stdout.strip()


def _working_tree_dirty() -> bool:
    out = _run("git", "status", "--porcelain", capture=True, check=False)
    return bool(out.stdout.strip())


def _default_output(ref: str, fmt: str) -> Path:
    sha = _short_sha(ref)
    ext = "zip" if fmt == "zip" else ("tar.gz" if fmt == "tar.gz" else "tar")
    return (ROOT.parent / f"llm-wiki-export-{sha}.{ext}").resolve()


def _git_archive(ref: str, fmt: str, output: Path) -> None:
    # `git archive` format names differ slightly: accepts `zip`, `tar`,
    # and `tar.gz` (as of git 2.20+). `--output` takes the destination.
    output.parent.mkdir(parents=True, exist_ok=True)
    _run(
        "git", "archive",
        f"--format={fmt}",
        "--output", str(output),
        ref,
    )


def _verify_archive(output: Path) -> int:
    """List archive contents, report size, and fail if any FORBIDDEN
    path slipped in.

    Supports zip and tar (including .tar.gz). For anything else, prints
    a warning and skips verification.
    """
    suffix = "".join(output.suffixes)
    names: list[str] = []
    if suffix == ".zip":
        with zipfile.ZipFile(output) as zf:
            names = zf.namelist()
    elif suffix in (".tar", ".tar.gz"):
        import tarfile
        with tarfile.open(output) as tf:
            names = tf.getnames()
    else:
        print(f"export_vault: unknown archive suffix `{suffix}`, skipping verify", file=sys.stderr)
        return 0

    size_mb = output.stat().st_size / (1024 * 1024)
    print()
    print(f"Archive: {output}")
    print(f"  Files: {len(names)}")
    print(f"  Size:  {size_mb:.2f} MB")

    # Find any forbidden matches
    hits: list[tuple[str, str]] = []  # (pattern, matched_file)
    for pattern, anchor in FORBIDDEN_PATH_PATTERNS:
        for name in names:
            if _is_forbidden(name, pattern, anchor):
                hits.append((pattern, name))
                break  # one example per pattern is enough

    if hits:
        print("")
        print("export_vault: FORBIDDEN PATHS IN ARCHIVE:", file=sys.stderr)
        for pattern, example in hits:
            print(f"  - pattern `{pattern}` matched: {example}", file=sys.stderr)
        print(
            "\nThis usually means the archive was built with `zip -r` "
            "instead of `git archive`. Re-run export_vault.py or follow "
            "docs/EXPORTING.md.",
            file=sys.stderr,
        )
        return 1

    print("  Verify: OK (no forbidden paths)")
    return 0


def main() -> int:
    args = parse_args()
    _require_git()

    if args.strict and _working_tree_dirty():
        print(
            "export_vault: working tree has uncommitted changes "
            "(use --no-strict to allow).",
            file=sys.stderr,
        )
        return 2

    output = args.output or _default_output(args.ref, args.format)

    _git_archive(args.ref, args.format, output)

    if args.verify:
        rc = _verify_archive(output)
        if rc != 0:
            return rc

    print(f"\nexport_vault: done. → {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
