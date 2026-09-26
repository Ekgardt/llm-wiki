"""Shared pytest fixtures and environment bootstrap.

Makes the suite **hermetic** — runs green from a fresh clone without
any pre-set environment variables or pre-existing runtime state.

  1. Subprocess-invoked hooks read `LLM_WIKI_ROOT`; absent env → no-op.
  2. State must not pollute the developer's real runtime (production
     lives inside the vault under gitignored `cache/logs/run/`). Tests
     redirect `LLM_WIKI_STATE_ROOT` to a session-scoped pytest temp dir.

Override: set `LLM_WIKI_STATE_ROOT` before pytest AND
`LLM_WIKI_TEST_USE_EXTERNAL_STATE=1` if you need a custom location.
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

import pytest

pytest_plugins = ("tests.code_kernel_helpers",)
collect_ignore_glob = ["fixtures/code_kernel/python/tests/test_service.py"]

VAULT_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = VAULT_ROOT / "scripts"

if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

# The vault the host's own hooks write to, as this process received it.
_HOST_VAULT = os.environ.get("LLM_WIKI_ROOT")
# 1. Vault root — always pin to this checkout for hermetic subprocess hooks.
os.environ["LLM_WIKI_ROOT"] = str(VAULT_ROOT)
# A test that starts a server must not load models in the background by accident:
# a closing server now waits for inference, and an unwaited one aborted the test
# process at exit. Tests of the warm-up call it directly. See
# `docs/research/2026-09-14-no-model-running-at-exit.md`.
os.environ.setdefault("LLMWIKI_NO_ENCODER_WARMUP", "1")

_USE_EXTERNAL_STATE = os.environ.get(
    "LLM_WIKI_TEST_USE_EXTERNAL_STATE", ""
).lower() in {"1", "true", "yes"}


def _external_state_root_problem(state_root: Path, vault_root: Path) -> str | None:
    """Why an external state root may not be used: it is the vault, or inside it.

    This checkout is the owner's live vault, and a pytest session whose state
    root resolved here left a stray coordinator candidate in `run/` on
    2026-09-17 that refused every Markdown writer for six days. See
    `docs/research/2026-09-23-a-stray-candidate-stopped-the-memory-for-six-days.md`.
    """
    resolved = state_root.resolve()
    vault = vault_root.resolve()
    if resolved != vault and vault not in resolved.parents:
        return None
    return (
        f"LLM_WIKI_STATE_ROOT={resolved} is the vault {vault} or inside it; a test "
        "session there writes into the live run/. Point LLM_WIKI_STATE_ROOT outside "
        "the checkout."
    )


_EARLY_STATE_ROOT: Path | None = None
if not _USE_EXTERNAL_STATE:
    # Set this before pytest imports test modules; those imports may load
    # memory_state during collection and cache its module-level paths.
    _EARLY_STATE_ROOT = Path(tempfile.mkdtemp(prefix="llm-wiki-test-state-"))
    os.environ["LLM_WIKI_STATE_ROOT"] = str(_EARLY_STATE_ROOT)
else:
    _problem = _external_state_root_problem(
        Path(os.environ.get("LLM_WIKI_STATE_ROOT", str(VAULT_ROOT))), VAULT_ROOT
    )
    if _problem is not None:
        raise pytest.UsageError(_problem)

# 2. Isolated state root OUTSIDE the vault for hermetic tests (production
#    runtime lives inside the vault under gitignored cache/logs/run/, but
#    tests must not mutate those). Uses a session-scoped pytest temp dir
#    so state is fresh per session and cleaned up automatically.
#    Override: set LLM_WIKI_STATE_ROOT before pytest AND
#    LLM_WIKI_TEST_USE_EXTERNAL_STATE=1.
@pytest.fixture(scope="session", autouse=True)
def _isolate_test_state_root():
    """Provide a hermetic, session-scoped state root for every test."""
    state_root = Path(os.environ["LLM_WIKI_STATE_ROOT"])
    state_dir = state_root / "run"
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_root / "logs").mkdir(parents=True, exist_ok=True)
    (state_root / "cache").mkdir(parents=True, exist_ok=True)
    state_file = state_dir / "state.json"
    if not state_file.exists():
        state_file.write_text("{}\n", encoding="utf-8")
    yield
    if _EARLY_STATE_ROOT is not None:
        shutil.rmtree(_EARLY_STATE_ROOT, ignore_errors=True)


# This checkout *is* the owner's vault since the two directories were merged on
# 2026-08-21, so a test that writes knowledge through the pinned LLM_WIKI_ROOT
# writes into their memory. By 2026-08-24 that had left 384 project journals from
# past pytest sessions in `knowledge/projects`, and they were coming back as
# answers to real questions. The guard makes the next one impossible to miss.
# What a test may not do is leave knowledge behind in this checkout, which has
# been the owner's live vault since the two directories merged on 2026-08-21. By
# 2026-08-24 that had left 384 project journals from past pytest sessions, and
# they were coming back as answers to real questions.
#
# The watch is deliberately uneven, because the live runtime writes here too:
#   * `knowledge/projects` is compared by name only — a real session working in
#     `project-beta` or `project-alpha` appends to its own journal while the suite runs,
#     and that is the owner's work, not a leak. A leaking test creates a project
#     of its own, which shows up as a new name.
#   * `knowledge/notes` is compared file by file — only a nightly compile writes
#     there, so any change during a run is worth stopping for.
#   * `knowledge/raw/sessions` is compared file by file too, since 2026-08-26.
#     Leaving it unwatched cost three fixture session records — `session-1.md`
#     under 2026-08-24, -25 and -26 — written by `tests/test_flush_classification`
#     through the pinned root while only `flush_memory.STATE_ROOT` was patched.
#     A record is a whole file that is never rewritten, so file-by-file is the
#     same watch as notes. The accepted cost: a genuine capture finishing while
#     the suite runs trips the guard too. The message names the path, and a real
#     record names a real session id, so the two are told apart by looking.
#   * `knowledge/daily` is not watched in the live checkout: the capture appends
#     to today's log continuously, measured mid-run. Anywhere else — a worktree,
#     a clean checkout, CI — nothing but a test appends there, and it is watched
#     file by file: two stale tests had been writing prompt lines into a
#     worktree's daily log (docs/research/2026-09-25-a-test-that-writes-a-daily-log-is-caught.md).
_WATCHED_PROJECTS = "knowledge/projects"
_WATCHED_NOTES = "knowledge/notes"
_WATCHED_SESSIONS = "knowledge/raw/sessions"
_WATCHED_DAILY = "knowledge/daily"


def _file_identity(path: Path) -> tuple[int, int]:
    info = path.stat()
    return (info.st_size, info.st_mtime_ns)


def _files_under(directory: Path, root: Path = VAULT_ROOT) -> dict[str, tuple[int, int]]:
    if not directory.is_dir():
        return {}
    return {
        str(path.relative_to(root)): _file_identity(path)
        for path in directory.rglob("*")
        if path.is_file()
    }


def _names_under(directory: Path, root: Path = VAULT_ROOT) -> dict[str, tuple[int, int]]:
    if not directory.is_dir():
        return {}
    return {str(entry.relative_to(root)): (0, 0) for entry in directory.iterdir()}


def _knowledge_entries(root: Path = VAULT_ROOT) -> dict[str, tuple[int, int]]:
    seen = _names_under(root / _WATCHED_PROJECTS, root)
    seen.update(_files_under(root / _WATCHED_NOTES, root))
    seen.update(_files_under(root / _WATCHED_SESSIONS, root))
    seen.update(_daily_files(root))
    return seen


def _host_writes_here(root: Path) -> bool:
    if not _HOST_VAULT:
        return False
    return Path(_HOST_VAULT).resolve() == root.resolve()


def _daily_files(root: Path) -> dict[str, tuple[int, int]]:
    """The daily logs, unless the host's own capture is appending to them."""
    if _host_writes_here(root):
        return {}
    return _files_under(root / _WATCHED_DAILY, root)


def _leaked_entries(
    before: dict[str, tuple[int, int]], after: dict[str, tuple[int, int]]
) -> list[str]:
    return sorted(name for name, identity in after.items() if before.get(name) != identity)


@pytest.fixture(scope="session", autouse=True)
def _no_writes_into_the_live_vault():
    """Fail the session when a test leaves knowledge behind in this checkout."""
    before = _knowledge_entries()
    yield
    leaked = _leaked_entries(before, _knowledge_entries())
    assert not leaked, "tests wrote into the live vault: " + ", ".join(leaked[:20])


SHIPPED_APPEND_BUDGETS = "shipped_append_budgets"
_APPEND_BUDGETS = (
    "daily_log_append.BREADCRUMB_APPEND_BUDGET_SECONDS",
    "daily_log_append.LIFECYCLE_APPEND_BUDGET_SECONDS",
)


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        f"{SHIPPED_APPEND_BUDGETS}: the test measures the hooks' append budgets and sees the shipped values",
    )


@pytest.fixture(autouse=True)
def _unhurried_hook_appends(request, monkeypatch):
    """A test of what a hook writes does not race the hook's append budget.

    The budget fits the host's timeout, not a loaded CI runner: the first append into a
    fresh vault took 1.4-5.6 s on the Windows shards, the breadcrumb budget is 3 s, and a
    writer past it gives up by design. Patched by name, so a reloaded module is patched too.
    Research: docs/research/2026-09-17-a-content-test-does-not-race-the-hook-budget.md.
    """
    if request.node.get_closest_marker(SHIPPED_APPEND_BUDGETS):
        return
    from tests.slow_machine import LONG_TIMEOUT

    for budget in _APPEND_BUDGETS:
        monkeypatch.setattr(budget, LONG_TIMEOUT)


@pytest.fixture(autouse=True)
def _no_real_user_config(monkeypatch):
    """No test resolves a config path from the machine's `XDG_CONFIG_HOME`.

    GitHub's Ubuntu runners set it, so installer tests that pass a temporary
    `home` wrote the OpenCode plugin into the runner's real `~/.config`, and a
    later test found that file and refused it as someone else's
    (`install_resource_ownership_ambiguous`, CI run 36182427739). A test that
    wants the variable sets it itself. Research:
    docs/research/2026-09-25-a-failed-reinstall-puts-the-old-one-back.md.
    """
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)


# Default fake provider for any accidental live LLM calls in unit tests.
os.environ.setdefault("MEMORY_LLM_PROVIDER", "fake")


# Which test was running when a process died, written to a file instead of the
# console. `-v` on the Windows shards cost two runs: 40 and then 60 minutes of cap
# on jobs that normally take 22-26, while a silent death still needs the name of
# the test it happened in. One short append per test costs nothing and survives a
# kill. Research:
# docs/research/2026-09-13-a-shorter-answer-and-a-fresher-line.md.
def _arm_progress_file(path: str | None) -> str | None:
    """The progress path with its directory in place, or None when there is none.

    The workflow names a file under `pytest-timings/`, a directory nothing
    creates before pytest writes the junit file at session end; every append
    before that raised `FileNotFoundError` into the `except OSError` below, so
    the Windows shard that died on 2026-09-23 left no line. One `mkdir` here.
    """
    if not path:
        return None
    try:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        return None
    return path


_PROGRESS_FILE = _arm_progress_file(os.environ.get("LLM_WIKI_TEST_PROGRESS_FILE"))


def pytest_runtest_logstart(nodeid, location):  # noqa: ARG001
    if not _PROGRESS_FILE:
        return
    try:
        with open(_PROGRESS_FILE, "a", encoding="utf-8") as progress:
            progress.write(f"{nodeid}\n")
    except OSError:
        return
