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
    python scripts/export_vault.py --verify             # explicit compatibility flag;
                                                        #   verification is always mandatory

Exit codes:
    0 — archive passed bounded path/content checks and was published.
    1 — git archive failed, or mandatory verification failed.
    2 — usage error (missing git, --strict with archived paths that differ
        from the working tree, etc.).

Why this exists: a colleague audit repeatedly flagged that the zip
they received contained `.venv/` (~300 MB), `settings.local.json`
(machine-local permissions), and other non-exportable artifacts.
Root cause: operator used `zip -r` on the folder, bypassing
`.gitignore`. Fix: make `git archive` the default path.
"""

from __future__ import annotations

import argparse
import io
import os
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import zipfile
from pathlib import Path
from typing import BinaryIO

# Force utf-8 on stdout — the description text contains a Unicode arrow.
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, io.UnsupportedOperation):
        pass

sys.path.insert(0, str(Path(__file__).resolve().parent))
from memory_state import ROOT  # noqa: E402
from model_dlp import (  # noqa: E402
    DLPPolicy,
    load_policy,
    require_safe_content,
)

MAX_ARCHIVE_BYTES = 512 * 1024 * 1024
MAX_ARCHIVE_MEMBERS = 10_000
MAX_MEMBER_BYTES = 16 * 1024 * 1024
MAX_TOTAL_UNCOMPRESSED_BYTES = 256 * 1024 * 1024
MAX_MEMBER_NAME_BYTES = 4096
MAX_REPORTED_STRICT_PATHS = 20
_DRIVE_PATH_RE = re.compile(r"^[A-Za-z]:")


class ArchiveVerificationError(ValueError):
    """The archive could not be completely and safely inspected."""


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
        "--output",
        "-o",
        type=Path,
        help=("Output archive path. Default: `<vault>/../llm-wiki-export-<shortsha>.<ext>`."),
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
        help=("Compatibility flag. Path and content security verification is always mandatory."),
    )
    p.add_argument(
        "--no-verify",
        dest="verify",
        action="store_false",
        help="Deprecated compatibility flag; security verification still runs.",
    )
    p.add_argument(
        "--strict",
        action="store_true",
        help=(
            "Fail, naming the paths, if any file the archive carries differs "
            "from the working tree. Untracked files never trip this: "
            "`git archive` cannot include them. Without this flag the "
            "difference is allowed — the archive simply holds the committed "
            "version (`git archive` uses the ref, not the working copy)."
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


def _paths_differing_from_ref(ref: str) -> tuple[str, ...]:
    """Tracked paths whose working copy differs from the ref being archived.

    `git status --porcelain` was the wrong question twice over. It reports
    untracked files, which `git archive` can never carry, and — since the vault
    and the checkout became one directory — it reports the tracked index and
    log that the runtime rewrites on every compile. So `--strict` refused every
    export in the installed vault and named nothing. What the flag means is
    narrower: which paths the archive carries would not match what is on disk.
    `-z` because this repository has already been bitten by the porcelain
    status column and by C-quoted paths.
    """
    out = _run("git", "diff", "--name-only", "-z", ref, "--", capture=True, check=False)
    return tuple(path for path in out.stdout.split("\0") if path)


def _report_strict_drift(ref: str, paths: tuple[str, ...]) -> None:
    print(
        f"export_vault: --strict: {len(paths)} tracked path(s) differ from {ref}; "
        "the archive would carry the committed version:",
        file=sys.stderr,
    )
    for path in paths[:MAX_REPORTED_STRICT_PATHS]:
        print(f"  {path}", file=sys.stderr)
    if len(paths) > MAX_REPORTED_STRICT_PATHS:
        remaining = len(paths) - MAX_REPORTED_STRICT_PATHS
        print(f"  ... and {remaining} more", file=sys.stderr)


FORMAT_EXTENSIONS = {"zip": "zip", "tar": "tar", "tar.gz": "tar.gz"}


def _default_output(ref: str, fmt: str) -> Path:
    sha = _short_sha(ref)
    ext = FORMAT_EXTENSIONS[_require_supported_format(fmt)]
    return (ROOT.parent / f"llm-wiki-export-{sha}.{ext}").resolve()


def _git_archive(ref: str, fmt: str, output: Path) -> None:
    # `git archive` format names differ slightly: accepts `zip`, `tar`,
    # and `tar.gz` (as of git 2.20+). `--output` takes the destination.
    output.parent.mkdir(parents=True, exist_ok=True)
    _run(
        "git",
        "archive",
        f"--format={fmt}",
        "--output",
        str(output),
        ref,
    )


def _require_supported_format(archive_format: str | None) -> str:
    if archive_format not in {"zip", "tar", "tar.gz"}:
        raise ArchiveVerificationError("unsupported archive format")
    return archive_format


def _archive_format(output: Path, requested: str | None) -> str:
    if requested is not None:
        return _require_supported_format(requested)
    suffix = "".join(output.suffixes)
    inferred = {".zip": "zip", ".tar": "tar", ".tar.gz": "tar.gz"}.get(suffix)
    return _require_supported_format(inferred)


def _member_text(name: str) -> str:
    if not name:
        raise ArchiveVerificationError("empty archive member name")
    if "\x00" in name or len(name.encode("utf-8")) > MAX_MEMBER_NAME_BYTES:
        raise ArchiveVerificationError("invalid archive member name")
    return name.replace("\\", "/").rstrip("/")


def _member_parts(name: str) -> tuple[str, ...]:
    if name.startswith("/") or _DRIVE_PATH_RE.match(name):
        raise ArchiveVerificationError("absolute archive member path")
    parts = tuple(name.split("/"))
    if any(part in {"", ".", ".."} for part in parts):
        raise ArchiveVerificationError("ambiguous archive member path")
    return parts


def _normalized_member_name(name: str) -> str:
    normalized = "/".join(_member_parts(_member_text(name)))
    forbidden = next(
        (
            pattern
            for pattern, anchor in FORBIDDEN_PATH_PATTERNS
            if _is_forbidden(normalized, pattern, anchor)
        ),
        None,
    )
    if forbidden is not None:
        raise ArchiveVerificationError("forbidden archive member path")
    return normalized


def _register_member(seen: set[str], name: str) -> str:
    normalized = _normalized_member_name(name)
    if normalized in seen:
        raise ArchiveVerificationError("duplicate archive member path")
    seen.add(normalized)
    return normalized


def _bounded_total(total: int, size: int) -> int:
    if size < 0 or size > MAX_MEMBER_BYTES:
        raise ArchiveVerificationError("archive member exceeds size limit")
    updated = total + size
    if updated > MAX_TOTAL_UNCOMPRESSED_BYTES:
        raise ArchiveVerificationError("archive exceeds uncompressed size limit")
    return updated


def _read_member(stream: BinaryIO) -> bytes:
    content = stream.read(MAX_MEMBER_BYTES + 1)
    if len(content) > MAX_MEMBER_BYTES:
        raise ArchiveVerificationError("archive member exceeds read limit")
    return content


def _scan_metadata(values: tuple[bytes, ...], policy: DLPPolicy) -> None:
    for value in values:
        _bounded_total(0, len(value))
        require_safe_content(value, policy)


def _zip_member_type(info: zipfile.ZipInfo) -> None:
    mode = info.external_attr >> 16
    member_type = stat.S_IFMT(mode)
    if member_type not in {0, stat.S_IFDIR, stat.S_IFREG}:
        raise ArchiveVerificationError("unsupported ZIP member type")


def _scan_zip_member(archive: zipfile.ZipFile, info: zipfile.ZipInfo, policy: DLPPolicy) -> int:
    _scan_metadata((info.filename.encode("utf-8"), info.comment, info.extra), policy)
    _zip_member_type(info)
    if info.is_dir():
        return 0
    if info.flag_bits & 0x1:
        raise ArchiveVerificationError("encrypted ZIP member")
    _bounded_total(0, info.file_size)
    with archive.open(info, "r") as stream:
        content = _read_member(stream)
    require_safe_content(content, policy)
    return len(content)


def _scan_zip(output: Path, policy: DLPPolicy) -> tuple[int, int]:
    seen: set[str] = set()
    total = 0
    with zipfile.ZipFile(output) as archive:
        _scan_metadata((archive.comment,), policy)
        members = archive.infolist()
        if len(members) > MAX_ARCHIVE_MEMBERS:
            raise ArchiveVerificationError("archive member count exceeds limit")
        for info in members:
            _register_member(seen, info.filename)
            total = _bounded_total(total, _scan_zip_member(archive, info, policy))
    return len(seen), total


def _tar_metadata(member: tarfile.TarInfo) -> tuple[bytes, ...]:
    pax = tuple(
        f"{key}={value}".encode("utf-8", errors="surrogatepass")
        for key, value in sorted(member.pax_headers.items())
    )
    return (
        member.name.encode("utf-8", errors="surrogatepass"),
        member.linkname.encode("utf-8", errors="surrogatepass"),
        str(member.uname).encode("utf-8", errors="surrogatepass"),
        str(member.gname).encode("utf-8", errors="surrogatepass"),
        *pax,
    )


def _scan_regular_tar_member(
    archive: tarfile.TarFile, member: tarfile.TarInfo, policy: DLPPolicy
) -> int:
    if not member.isfile():
        raise ArchiveVerificationError("unsupported TAR member type")
    _bounded_total(0, member.size)
    stream = archive.extractfile(member)
    if stream is None:
        raise ArchiveVerificationError("TAR member is unreadable")
    with stream:
        content = _read_member(stream)
    require_safe_content(content, policy)
    return len(content)


def _scan_tar_member(archive: tarfile.TarFile, member: tarfile.TarInfo, policy: DLPPolicy) -> int:
    _scan_metadata(_tar_metadata(member), policy)
    if member.isdir():
        return 0
    return _scan_regular_tar_member(archive, member, policy)


def _scan_tar(output: Path, policy: DLPPolicy) -> tuple[int, int]:
    seen: set[str] = set()
    total = 0
    count = 0
    with tarfile.open(output, "r:*") as archive:
        for member in archive:
            count += 1
            if count > MAX_ARCHIVE_MEMBERS:
                raise ArchiveVerificationError("archive member count exceeds limit")
            _register_member(seen, member.name)
            total = _bounded_total(total, _scan_tar_member(archive, member, policy))
    return count, total


def _scan_archive(output: Path, archive_format: str, policy: DLPPolicy) -> tuple[int, int]:
    scanners = {"zip": _scan_zip, "tar": _scan_tar, "tar.gz": _scan_tar}
    scanner = scanners.get(archive_format)
    if scanner is None:
        raise ArchiveVerificationError("unsupported archive format")
    return scanner(output, policy)


def _verify_archive(output: Path, requested_format: str | None = None) -> int:
    """Fail closed unless every bounded archive member passes path and DLP checks."""
    try:
        if output.stat().st_size > MAX_ARCHIVE_BYTES:
            raise ArchiveVerificationError("archive exceeds compressed size limit")
        archive_format = _archive_format(output, requested_format)
        files, total = _scan_archive(output, archive_format, load_policy())
    except Exception as exc:  # noqa: BLE001 - incomplete verification must fail closed
        print(
            f"export_vault: verification failed ({type(exc).__name__})",
            file=sys.stderr,
        )
        return 1
    size_mb = output.stat().st_size / (1024 * 1024)
    print(f"\nArchive: {output}")
    print(f"  Members: {files}")
    print(f"  Uncompressed: {total / (1024 * 1024):.2f} MB")
    print(f"  Size: {size_mb:.2f} MB")
    print("  Verify: OK (paths and content)")
    return 0


def _staging_path(output: Path) -> Path:
    output.parent.mkdir(parents=True, exist_ok=True)
    descriptor, raw_path = tempfile.mkstemp(
        prefix=f".{output.name}.", suffix=".tmp", dir=output.parent
    )
    os.close(descriptor)
    return Path(raw_path)


def _build_verified_archive(args: argparse.Namespace, output: Path) -> int:
    staging = _staging_path(output)
    try:
        _git_archive(args.ref, args.format, staging)
        if not args.verify:
            print(
                "export_vault: --no-verify is deprecated; security checks remain mandatory",
                file=sys.stderr,
            )
        if _verify_archive(staging, args.format) != 0:
            return 1
        os.replace(staging, output)
        return 0
    except (OSError, subprocess.SubprocessError):
        print("export_vault: archive build failed", file=sys.stderr)
        return 1
    finally:
        staging.unlink(missing_ok=True)


def main() -> int:
    args = parse_args()
    _require_git()

    drifted = _paths_differing_from_ref(args.ref) if args.strict else ()
    if drifted:
        _report_strict_drift(args.ref, drifted)
        return 2

    output = args.output or _default_output(args.ref, args.format)
    result = _build_verified_archive(args, output)
    if result != 0:
        return result

    print(f"\nexport_vault: done. → {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
