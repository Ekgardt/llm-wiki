"""Where throwaway work lives: the platform's temporary directory and the host's job directory.

Two passes need the same answer. The residue retirer removes the transcripts the
memory's own calls left from these places, and the repository passes do not keep
code generations for a checkout that lives in one (a clean worktree an agent made
for one test run cost the nightly about three minutes and 600 MB a night). Both
spellings are compared, as given and resolved: macOS writes its temporary
directory as `/var/...` and resolves it to `/private/var/...`, and Windows writes a
user's directory short and long. See
`docs/research/2026-09-24-every-store-has-a-bound.md`.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path


def host_directory() -> Path:
    """The agent host's configuration directory (`CLAUDE_CONFIG_DIR` or `~/.claude`)."""
    configured = os.environ.get("CLAUDE_CONFIG_DIR")
    return Path(configured) if configured else Path.home() / ".claude"


def ephemeral_roots() -> tuple[Path, ...]:
    """The roots throwaway work is made under, as the platform spells them."""
    return (Path(tempfile.gettempdir()), host_directory() / "jobs")


def resolved(path: Path) -> Path:
    try:
        return Path(path).resolve()
    except OSError:
        return Path(path).absolute()


def spellings(roots: tuple[Path, ...]) -> tuple[Path, ...]:
    """Every root as given and resolved, without repeats."""
    return tuple(dict.fromkeys(spelling for root in roots for spelling in (Path(root), resolved(root))))


def is_under(path: Path, roots: tuple[Path, ...]) -> bool:
    """Whether `path`, resolved, lies at or below one of `roots`, resolved."""
    target = resolved(path)
    return any(target.is_relative_to(resolved(root)) for root in roots)


def is_throwaway_checkout(path: Path) -> bool:
    """A checkout made inside the host's job directory, for one job.

    Narrower than `ephemeral_roots`: a checkout in the platform's temporary
    directory goes when the system clears it, and then retention retires it as
    missing; the job directory survives restarts, and one clean worktree there
    cost the nightly three minutes and 600 MB a night.
    """
    return is_under(path, (host_directory() / "jobs",))
