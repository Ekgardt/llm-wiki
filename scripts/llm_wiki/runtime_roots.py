"""Where the vault and its runtime state live, read from the environment on every call.

`LLM_WIKI_ROOT` names the vault. Unset or empty, the vault is the checkout these
scripts belong to; from a Git worktree that is the main checkout, so every writer
in a worktree reaches the one vault. `LLM_WIKI_STATE_ROOT` names the runtime root
holding `cache/`, `logs/` and `run/`; unset or empty, it is the vault itself.

Reading on every call is the contract: a test or a host that sets the variables
reaches every module at once, and no module keeps a copy that another one lacks.
Only the Git probe of the checkout is remembered, because the checkout a process
runs from does not move while it runs.
"""
from __future__ import annotations

import functools
import os
import subprocess
from pathlib import Path

# Every hook resolves the vault, and a hook's whole budget is seconds;
# `rev-parse` answers in milliseconds.
ROOT_PROBE_TIMEOUT_SECONDS = 2.0
CODE_CHECKOUT = Path(__file__).resolve().parents[2]


def configured_vault_root() -> Path | None:
    """The vault the environment names, or None when it names none."""
    return _configured("LLM_WIKI_ROOT")


def configured_state_root() -> Path | None:
    """The runtime root the environment names, or None when it names none."""
    return _configured("LLM_WIKI_STATE_ROOT")


def _configured(variable: str) -> Path | None:
    value = os.environ.get(variable)
    if not value:
        return None
    return Path(value).expanduser().resolve()


def vault_root() -> Path:
    return configured_vault_root() or checkout_vault(CODE_CHECKOUT)


def state_root() -> Path:
    return state_root_for(vault_root())


def state_root_for(vault: Path | str) -> Path:
    """The configured runtime root, else `vault` itself -- for a caller handed a vault."""
    return configured_state_root() or Path(vault).resolve()


def knowledge_dir() -> Path:
    return vault_root() / "knowledge"


def notes_dir() -> Path:
    return knowledge_dir() / "notes"


def index_file() -> Path:
    return knowledge_dir() / "index.md"


def daily_dir() -> Path:
    return knowledge_dir() / "daily"


def projects_dir() -> Path:
    return knowledge_dir() / "projects"


def run_dir() -> Path:
    return state_root() / "run"


def reports_dir() -> Path:
    return state_root() / "logs"


@functools.cache
def checkout_vault(checkout: Path) -> Path:
    """The main checkout of `checkout`'s repository, or `checkout` when Git cannot say."""
    common = _git_common_dir(checkout)
    if common is not None and common.name == ".git":
        return common.parent
    return checkout


def _git_common_dir(checkout: Path) -> Path | None:
    try:
        out = subprocess.check_output(
            ["git", "rev-parse", "--git-common-dir"],
            cwd=str(checkout),
            text=True,
            stderr=subprocess.DEVNULL,
            timeout=ROOT_PROBE_TIMEOUT_SECONDS,
        ).strip()
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError):
        return None
    return (checkout / out).resolve()
