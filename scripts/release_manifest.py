"""Print the manifest a release pins: the commit and what the installer runs.

`install.sh` and `install.ps1` accept a remote bootstrap only for an exact
40-hex commit OID — never a branch or a tag name — so a published release has
to state that OID somewhere a reader can check. This prints it together with the
SHA-256 of every file the bootstrap executes or depends on, so an operator can
verify the checkout they are about to run against the release notes.

    uv run python scripts/release_manifest.py v4.0.0
    uv run python scripts/release_manifest.py v4.0.0 --markdown
"""
from __future__ import annotations

import argparse
import hashlib
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# What the remote bootstrap runs or verifies. Not the whole tree: a manifest
# nobody can check by hand is a manifest nobody checks.
PINNED_FILES = (
    "install.sh",
    "install.ps1",
    "pyproject.toml",
    "uv.lock",
    "scripts/install_smoke.py",
    "scripts/installer_config.py",
)


def commit_of(ref: str) -> str:
    """The 40-hex OID a ref names, which is what the bootstrap accepts."""
    result = subprocess.run(
        ["git", "rev-parse", f"{ref}^{{commit}}"],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise SystemExit(f"unknown ref: {ref}")
    return result.stdout.strip()


def _blob(ref: str, path: str) -> bytes:
    result = subprocess.run(
        ["git", "show", f"{ref}:{path}"],
        cwd=str(ROOT),
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        raise SystemExit(f"missing from {ref}: {path}")
    return result.stdout


def manifest(ref: str) -> dict[str, str]:
    """SHA-256 of every pinned file as that ref has it."""
    return {
        path: hashlib.sha256(_blob(ref, path)).hexdigest() for path in PINNED_FILES
    }


def _markdown(ref: str, oid: str, hashes: dict[str, str]) -> str:
    rows = "\n".join(f"| `{path}` | `{digest}` |" for path, digest in hashes.items())
    return (
        f"# Release {ref}\n\n"
        f"Commit: `{oid}`\n\n"
        "The remote bootstrap accepts only this exact OID:\n\n"
        "```bash\n"
        f"LLM_WIKI_COMMIT={oid} bash ./install.sh\n"
        "```\n\n"
        "| File | SHA-256 |\n|---|---|\n"
        f"{rows}\n"
    )


def _plain(ref: str, oid: str, hashes: dict[str, str]) -> str:
    lines = [f"{ref} {oid}"]
    lines.extend(f"{digest}  {path}" for path, digest in hashes.items())
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("ref", help="The tag or commit the release publishes")
    parser.add_argument("--markdown", action="store_true", help="Release-note form")
    arguments = parser.parse_args(argv)
    oid = commit_of(arguments.ref)
    hashes = manifest(arguments.ref)
    render = _markdown if arguments.markdown else _plain
    print(render(arguments.ref, oid, hashes))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
