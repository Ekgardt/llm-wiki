"""Where the hosts keep their session transcripts.

Both hosts let the user move their directory — `CLAUDE_CONFIG_DIR` for Claude
Code, `CODEX_HOME` for Codex — and a hook inherits the host's environment, so the
variable names the directory this very host writes to. The defaults stay on the
list because the scheduler's environment is not the host's. See
`docs/research/2026-09-17-a-moved-host-directory-is-still-the-host.md`.
"""
from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

# (environment variable, default directory under home, where transcripts live inside it)
_HOSTS = (
    ("CLAUDE_CONFIG_DIR", ".claude", "projects"),
    ("CODEX_HOME", ".codex", "sessions"),
)


def _configured(environ: Mapping[str, str], variable: str) -> list[Path]:
    value = environ.get(variable, "").strip()
    return [Path(value).expanduser()] if value else []


def host_transcript_roots(
    environ: Mapping[str, str] | None = None, home: Path | None = None
) -> tuple[Path, ...]:
    """The default transcript directories, then the configured ones, without repeats."""
    environ = os.environ if environ is None else environ
    home = Path.home() if home is None else home
    roots: list[Path] = []
    for variable, directory, inner in _HOSTS:
        bases = [home / directory, *_configured(environ, variable)]
        roots.extend(base / inner for base in bases)
    return tuple(dict.fromkeys(roots))
