"""Structural invariants for the llm-wiki repository.

These tests enforce the canonical three-zone layout and agent-contract
identity so that architectural drift is caught automatically (CI +
pre-commit) rather than discovered mid-task.

The canonical reference is `docs/STRUCTURE.md` + this file. If a test here
fails, the structure was changed without updating the canonical reference —
fix the reference first, then the code.
"""
from __future__ import annotations

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# Agent contract identity — AGENTS.md and CLAUDE.md MUST be byte-identical.
# ---------------------------------------------------------------------------


def test_agents_md_and_claude_md_are_identical():
    """AGENTS.md and CLAUDE.md serve the same purpose (agent operating
    contract) and must be byte-identical so every agent reads the same rules
    regardless of which file it loads. If this fails, sync one to the other.
    """
    agents = (ROOT / "AGENTS.md").read_bytes()
    claude = (ROOT / "CLAUDE.md").read_bytes()
    assert agents == claude, (
        "AGENTS.md and CLAUDE.md have diverged. Run: "
        "Copy-Item AGENTS.md CLAUDE.md -Force  (or vice versa)"
    )


def test_agent_contract_mentions_three_zone_process_rule():
    """The contract must document the 'architecture changes require sign-off'
    rule so future agents don't improvise structural changes mid-task.
    """
    text = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
    assert "sign-off" in text.lower() or "explicit" in text.lower(), (
        "AGENTS.md must mention the architecture-change sign-off process"
    )


# ---------------------------------------------------------------------------
# Three-zone layout — directory invariants.
# ---------------------------------------------------------------------------

CODE_DIRS = {
    "scripts", "tests", "docs", "skills", "rules", "integrations", "benchmark",
}
KNOWLEDGE_DIRS = {
    "daily", "notes", "projects", "raw", "inbox", "feedback",
}
RUNTIME_DIRS = {
    "cache", "logs", "run",
}
FORBIDDEN_ROOT_DIRS = {
    "wiki", "memory", "outputs", "state", "LLM-wiki-state",
}


@pytest.mark.parametrize("name", sorted(CODE_DIRS))
def test_code_zone_dirs_exist(name: str):
    d = ROOT / name
    assert d.is_dir(), f"CODE zone dir missing: {name}/"


@pytest.mark.parametrize("name", sorted(KNOWLEDGE_DIRS))
def test_knowledge_zone_dirs_exist(name: str):
    d = ROOT / "knowledge" / name
    assert d.is_dir(), f"KNOWLEDGE zone dir missing: knowledge/{name}/"


@pytest.mark.parametrize("name", sorted(RUNTIME_DIRS))
def test_runtime_dirs_are_gitignored(name: str):
    """Runtime dirs may or may not exist (created on demand), but if they
    exist they MUST be gitignored — never tracked.
    """
    d = ROOT / name
    if not d.exists():
        return
    import subprocess

    result = subprocess.run(
        ["git", "check-ignore", name],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        f"runtime dir {name}/ exists at vault root but is NOT gitignored"
    )


@pytest.mark.parametrize("name", sorted(FORBIDDEN_ROOT_DIRS))
def test_forbidden_root_dirs_absent(name: str):
    d = ROOT / name
    assert not d.exists(), (
        f"forbidden dir {name}/ exists at vault root — three-zone violation"
    )


def test_no_tracked_files_in_runtime_dirs():
    """No file under cache/, logs/, run/ should be tracked by git."""
    import subprocess

    result = subprocess.run(
        ["git", "ls-files", "cache/", "logs/", "run/", "cognee/"],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
    )
    tracked = [line for line in result.stdout.strip().splitlines() if line]
    assert not tracked, (
        f"runtime dirs have tracked files (should be gitignored): {tracked}"
    )


# ---------------------------------------------------------------------------
# memory_state.py default — STATE_ROOT must default to the vault root.
# ---------------------------------------------------------------------------


def test_memory_state_default_is_vault_root():
    """The canonical layout puts runtime INSIDE the vault (gitignored). The
    default STATE_ROOT in memory_state.py must be ROOT (the vault), not a
    sibling LLM-wiki-state/ dir. This catches accidental regression to the
    old sibling layout.
    """
    src = (ROOT / "scripts" / "memory_state.py").read_text(encoding="utf-8")
    # The default expression must resolve to ROOT, not ROOT.parent / ...
    assert 'os.environ.get("LLM_WIKI_STATE_ROOT", str(ROOT))' in src, (
        "memory_state.py STATE_ROOT default must be str(ROOT), not "
        "ROOT.parent / 'LLM-wiki-state'. See docs/STRUCTURE.md."
    )
    assert "ROOT.parent" not in src.split("STATE_ROOT")[1].split("\n")[0], (
        "STATE_ROOT line references ROOT.parent — sibling layout regression"
    )


# ---------------------------------------------------------------------------
# README i18n structural parity (section count + claims).
# ---------------------------------------------------------------------------


def test_readmes_have_same_h2_section_count():
    """All three READMEs must have the same number of top-level sections.
    Drift here is the #1 cause of 'RU/ZH is a compressed digest, not a
    translation'.
    """
    import re

    counts = {}
    for name in ("README.md", "README.ru.md", "README.zh-CN.md"):
        text = (ROOT / name).read_text(encoding="utf-8")
        h2s = re.findall(r"^## ", text, re.MULTILINE)
        counts[name] = len(h2s)
    vals = list(counts.values())
    assert len(set(vals)) == 1, (
        f"README H2 section count drift: {counts}. "
        "All three must have the same number of top-level sections."
    )
