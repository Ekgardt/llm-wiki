"""CI quality guards — catch documentation drift, undefined installer vars,
and benchmark/report consistency before they ship.

These tests enforce invariants that are easy to break silently:
  - skills must not reference the non-existent ``qmd`` CLI
  - install scripts must not use undefined variables
  - CHANGELOG version + test-count must match pyproject + live suite
  - architecture docs must not cite metrics absent from the benchmark report
  - skills' allowed-tools must only reference scripts that actually exist
  - README benchmark tables must not invent competitor numbers
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


# ─── Helpers ────────────────────────────────────────────────────────

def _collect_test_count() -> int:
    """Return the live number of collected pytest tests."""
    r = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q"],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    text = (r.stdout or "") + (r.stderr or "")
    m = re.search(r"(\d+)\s+tests?\s+collected", text)
    if m:
        return int(m.group(1))
    m = re.search(r"(\d+)\s+selected", text)
    if m:
        return int(m.group(1))
    raise AssertionError(f"could not parse pytest collect count:\n{text[-500:]}")


# ─── 1. No qmd references in skills ─────────────────────────────────

def test_no_qmd_refs_in_skills():
    """The qmd CLI does not exist in this repo; skills must not reference it
    as a command. The conceptual tier name 'QMD' (matching lookup_mode.py)
    is allowed."""
    import re

    skills_dir = ROOT / "skills"
    hits: list[str] = []
    # Match qmd as a CLI command (e.g. `qmd status`, `qmd embed`) — not as
    # a standalone tier label like "| **QMD** |" or "## Tier: QMD".
    cli_re = re.compile(r"\bqmd\s+(?:status|embed|index|query|collections|build|sync)\b", re.IGNORECASE)
    for skill_md in sorted(skills_dir.glob("*/SKILL.md")):
        for i, line in enumerate(
            skill_md.read_text(encoding="utf-8").splitlines(), 1
        ):
            if cli_re.search(line):
                hits.append(f"{skill_md.relative_to(ROOT)}:{i}: {line.strip()}")
    assert not hits, "qmd CLI references found in skills (qmd CLI does not exist):\n" + "\n".join(hits)


def test_ci_uses_current_gitleaks_action():
    """Gitleaks must use the Node 24 action with an available scanner release."""
    workflow = (ROOT / ".github" / "workflows" / "tests.yml").read_text(encoding="utf-8")
    assert "gitleaks/gitleaks-action@e0c47f4f8be36e29cdc102c57e68cb5cbf0e8d1e" in workflow
    assert "GITLEAKS_VERSION: 8.30.1" in workflow


# ─── 2. install.ps1 — no undefined PowerShell variables ─────────────

def test_install_ps1_no_undefined_vars():
    """Every $var referenced in install.ps1 must be assigned or a known automatic."""
    content = (ROOT / "install.ps1").read_text(encoding="utf-8")

    skip = {
        "_", "args", "LASTEXITCODE", "PROFILE", "env", "PSScriptRoot",
        "ErrorActionPreference", "true", "false", "null", "input",
    }

    # Collect all $varName references.
    refs: set[str] = set()
    for m in re.finditer(r"\$([A-Za-z_]\w*)", content):
        var = m.group(1)
        if var in skip:
            continue
        refs.add(var)

    # Collect assignments: $var = ...
    assigned: set[str] = set()
    for m in re.finditer(r"\$([A-Za-z_]\w*)\s*=", content):
        assigned.add(m.group(1))

    # Collect function parameters: function Name($a, $b)
    for fm in re.finditer(r"function\s+\w+\s*\(([^)]*)\)", content):
        for pm in re.finditer(r"\$([A-Za-z_]\w*)", fm.group(1)):
            assigned.add(pm.group(1))

    undefined = sorted(refs - assigned - skip)
    assert not undefined, f"Undefined PowerShell vars in install.ps1: {undefined}"


# ─── 3. install.sh — no undefined bash variables ────────────────────

def test_install_sh_no_undefined_vars():
    """Every $VAR referenced in install.sh must be assigned or a known environment."""
    content = (ROOT / "install.sh").read_text(encoding="utf-8")

    skip = {
        "HOME", "PATH", "PROFILE", "LLM_WIKI_ROOT", "LLM_WIKI_STATE_ROOT",
        # Standard bash/environment builtins not assigned inside the script.
        "SHELL", "BASH_SOURCE", "ZSH_VERSION", "BASH_VERSION",
    }

    # Collect all $VAR and ${VAR} references (not $(...) command subs).
    refs: set[str] = set()
    for m in re.finditer(r"\$\{?([A-Za-z_]\w*)", content):
        var = m.group(1)
        if var in skip:
            continue
        refs.add(var)

    # Collect assignments: VAR= or export VAR=
    assigned: set[str] = set()
    for m in re.finditer(
        r"(?:^|\s|;)(?:export\s+)?([A-Za-z_]\w*)\s*=", content, re.MULTILINE
    ):
        assigned.add(m.group(1))

    undefined = sorted(refs - assigned - skip)
    assert not undefined, f"Undefined bash vars in install.sh: {undefined}"


# ─── 4. CHANGELOG latest version matches pyproject.toml ─────────────

def test_changelog_latest_version_matches_pyproject():
    """The first [X.Y.Z] header in CHANGELOG must equal pyproject's version."""
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    m_cl = re.search(r"^##\s*\[(\d+(?:\.\d+)*)\]", changelog, re.MULTILINE)
    assert m_cl, "could not find a version header in CHANGELOG.md"
    cl_ver = m_cl.group(1)

    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    m_pp = re.search(r'^version\s*=\s*"([^"]+)"', pyproject, re.MULTILINE)
    assert m_pp, "could not parse version from pyproject.toml"
    pp_ver = m_pp.group(1)

    assert cl_ver == pp_ver, (
        f"CHANGELOG latest version [{cl_ver}] != pyproject version [{pp_ver}]"
    )


# ─── 5. CHANGELOG test count matches live suite ─────────────────────

def test_changelog_test_count_matches_live():
    """The latest CHANGELOG section's 'N tests' claim must match the live count."""
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    headers = list(
        re.finditer(r"^##\s*\[\d+(?:\.\d+)*\]", changelog, re.MULTILINE)
    )
    assert headers, "no version headers in CHANGELOG.md"
    start = headers[0].start()
    end = headers[1].start() if len(headers) > 1 else len(changelog)
    section = changelog[start:end]

    count_match = re.search(r"(\d+)\s+tests?\b", section)
    assert count_match, "no 'N tests' claim in latest CHANGELOG section"
    claimed = int(count_match.group(1))

    live = _collect_test_count()
    assert claimed == live, (
        f"CHANGELOG claims {claimed} tests but live suite collects {live}; "
        f"update CHANGELOG before release"
    )


# ─── 6. ARCHITECTURE.md must not cite Recall@2 ──────────────────────

def test_architecture_no_recall_at_2():
    """Recall@2 is not in benchmark/report.md; docs must not cite it."""
    arch = (ROOT / "docs" / "ARCHITECTURE.md").read_text(encoding="utf-8")
    assert "Recall@2" not in arch, (
        "docs/ARCHITECTURE.md cites Recall@2, which is absent from "
        "benchmark/report.md — remove or replace with a reported metric"
    )


# ─── 7. Skills' allowed-tools reference existing scripts ────────────

def test_skills_allowed_tools_reference_existing_scripts():
    """Direct Bash(script ...) references in skills must point to real files."""
    skills_dir = ROOT / "skills"
    for skill_md in sorted(skills_dir.glob("*/SKILL.md")):
        text = skill_md.read_text(encoding="utf-8")
        for bash_call in re.findall(r"Bash\(([^)]*)\)", text):
            # Ignore runtime commands ("uv run ...").
            if bash_call.strip().startswith("uv run"):
                continue
            for script_rel in re.findall(r"(scripts/\S+\.py)", bash_call):
                assert (ROOT / script_rel).is_file(), (
                    f"{skill_md.relative_to(ROOT)}: allowed-tools references "
                    f"{script_rel} which does not exist"
                )


# ─── 8. README must not invent agentmemory Recall@10 ────────────────

def test_readme_recall_at_10_agentmemory():
    """README must not claim a competitor Recall@10 % unless report.md has it."""
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    report = (ROOT / "benchmark" / "report.md").read_text(encoding="utf-8")

    report_has_recall10 = "Recall@10" in report

    row = re.search(
        r"\|\s*Recall@10\s*\|([^|]+)\|([^|]+)\|([^|]+)\|([^|]+)\|", readme
    )
    if not row:
        return  # no Recall@10 row — nothing to guard

    cells = [c.strip() for c in row.groups()]
    # cells[0] = LLM Wiki (allowed to have a %); rest are competitors.
    if not report_has_recall10:
        for cell in cells[1:]:
            assert not re.search(r"\d+\.?\d*%", cell), (
                f"README Recall@10 competitor cell '{cell}' has a percentage "
                f"not backed by benchmark/report.md — use 'n/a'"
            )


# ─── 9. Lint check count in docs must match code ────────────────────

def test_lint_check_count_matches_code():
    """The lint check count in README/docs must match lint_memory.py source."""
    lint_src = (ROOT / "scripts" / "lint_memory.py").read_text(encoding="utf-8")
    # Count registered check categories in run_checks()
    checks = re.findall(r'checks\.append\(', lint_src)
    if not checks:
        # Alternative: count check_ function definitions
        checks = re.findall(r'^def check_', lint_src, re.MULTILINE)
    actual = len(checks)
    assert actual > 0, "Could not count lint checks in lint_memory.py"

    for doc_name in ("README.md", "README.ru.md", "README.zh-CN.md",
                      "docs/ARCHITECTURE.md"):
        doc = (ROOT / doc_name).read_text(encoding="utf-8")
        # Find "N lint checks" or "N checks" patterns
        for m in re.finditer(r"(\d+)\s*(?:lint[- ]?checks?|structural\s+(?:lint\s+)?checks?)", doc, re.IGNORECASE):
            claimed = int(m.group(1))
            # The doc may say "13 structural" (correct if total is 14 with contradiction)
            # or "14" total. Accept either if it matches actual or actual-1.
            assert claimed in (actual, actual - 1), (
                f"{doc_name}: claims {claimed} lint checks but code has {actual}. "
                f"Update docs to match."
            )


# ─── 10. No standalone root cognee/ in docs ─────────────────────────

def test_no_standalone_cognee_in_docs():
    """Docs must use cache/cognee/ not standalone root cognee/."""
    structure = (ROOT / "docs" / "STRUCTURE.md").read_text(encoding="utf-8")
    # Extract canonical runtime dirs from STRUCTURE.md
    assert "cache/cognee" in structure, "STRUCTURE.md must document cache/cognee/"

    for doc_name in ("README.md", "README.ru.md", "README.zh-CN.md",
                      "docs/USER-GUIDE.md", "CONTRIBUTING.md"):
        doc = (ROOT / doc_name).read_text(encoding="utf-8")
        # Find standalone cognee/ not preceded by cache/
        for m in re.finditer(r"(?<!cache/)(?<!cache\\)\bcognee/", doc):
            line = doc[:m.start()].count("\n") + 1
            pytest.fail(
                f"{doc_name}:{line}: standalone 'cognee/' found — "
                f"should be 'cache/cognee/' per STRUCTURE.md"
            )


# ─── 11. Installer version comments match pyproject.toml ────────────

def test_installer_version_matches_pyproject():
    """Installer version-tag comments must match pyproject.toml version."""
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    version_match = re.search(r'version\s*=\s*"(\d+\.\d+\.\d+)"', pyproject)
    assert version_match, "No version in pyproject.toml"
    current_version = version_match.group(1)

    for installer in ("install.sh", "install.ps1"):
        src = (ROOT / installer).read_text(encoding="utf-8")
        # Find version-tag references like v3.3.3
        for m in re.finditer(r"v(\d+\.\d+\.\d+)", src):
            tag_version = m.group(1)
            if tag_version != current_version:
                line = src[:m.start()].count("\n") + 1
                pytest.fail(
                    f"{installer}:{line}: references v{tag_version} but "
                    f"pyproject.toml is {current_version}. Update installer comment."
                )


# ─── 12. All daily-log writers use shared lock ──────────────────────

def test_all_daily_writers_use_lock():
    """Scripts that write to daily logs must use _daily_lock or append_daily."""
    daily_writers = []
    for py in (ROOT / "scripts").glob("*.py"):
        src = py.read_text(encoding="utf-8")
        # Check if the script writes to a daily log file
        if re.search(r'(daily.*\.open\s*\(|append.*daily|DAILY_DIR.*\.write)', src):
            if py.name in ("daily_log_append.py", "memory_state.py"):
                continue  # These define the lock/append infrastructure
            daily_writers.append(py)

    for py in daily_writers:
        src = py.read_text(encoding="utf-8")
        has_lock = "_daily_lock" in src or "append_daily" in src or "locked_append" in src
        if not has_lock:
            pytest.fail(
                f"{py.name}: writes to daily log without using _daily_lock() "
                f"or append_daily(). All daily-log writes must be lock-protected."
            )


# ─── 13. Clean-clone: all imports in tracked scripts resolve to tracked files ─

def test_all_script_imports_resolve_in_git():
    """Every local import in scripts/*.py must resolve to a file tracked by Git.

    This catches the #1 recurring issue across audit rounds: new .py files
    created during fixes but never `git add`ed. On a clean clone, these
    cause ModuleNotFoundError before any test can run.
    """
    import subprocess

    # Get list of tracked files
    r = subprocess.run(
        ["git", "ls-files", "scripts/", "tests/"],
        cwd=ROOT, capture_output=True, text=True,
    )
    tracked = set()
    for line in r.stdout.strip().splitlines():
        tracked.add(line.split("/")[-1])  # filename only
        tracked.add(line)  # full path

    # Scan all tracked scripts for local imports
    for py in sorted((ROOT / "scripts").glob("*.py")):
        rel = f"scripts/{py.name}"
        if rel not in tracked and py.name not in tracked:
            continue  # untracked script — skip (will be caught by git status)
        src = py.read_text(encoding="utf-8")
        # Find local imports (not stdlib, not pip packages)
        for m in re.finditer(r"^\s*(?:from|import)\s+(\w+)", src, re.MULTILINE):
            mod_name = m.group(1)
            # Skip stdlib and known external packages
            if mod_name in ("os", "sys", "re", "json", "time", "datetime", "pathlib",
                            "hashlib", "subprocess", "argparse", "contextlib",
                            "io", "math", "secrets", "threading", "typing",
                            "collections", "functools", "itertools", "enum",
                            "dataclasses", "abc", "copy", "tempfile", "shutil",
                            "importlib", "traceback", "textwrap", "string",
                            "unittest", "pytest", "__future__",
                            "datetime", "warnings"):
                continue
            # Check if this is a local module (a .py file in scripts/)
            potential_file = ROOT / "scripts" / f"{mod_name}.py"
            if potential_file.exists():
                # It's a local import — must be tracked
                if f"scripts/{mod_name}.py" not in tracked and mod_name + ".py" not in tracked:
                    pytest.fail(
                        f"scripts/{py.name}: imports '{mod_name}' which exists as "
                        f"scripts/{mod_name}.py but is NOT tracked by Git. "
                        f"Run: git add scripts/{mod_name}.py"
                    )


# ─── 14. No untracked .py files that are imported by tracked code ──────────

def test_no_untracked_imported_modules():
    """No untracked .py file in scripts/ should be importable by tracked code.

    This is the clean-clone test: if a new helper module is created during
    a fix but not committed, the next clean clone breaks. This test catches
    that before it ships.
    """
    import subprocess

    # Get untracked .py files
    r = subprocess.run(
        ["git", "status", "--short", "--porcelain", "scripts/"],
        cwd=ROOT, capture_output=True, text=True,
    )
    untracked = []
    for line in r.stdout.strip().splitlines():
        if line.startswith("??") and line.endswith(".py"):
            name = line.split("/")[-1].strip()
            untracked.append(name.replace(".py", ""))

    if not untracked:
        return  # No untracked .py files — clean

    # Check if any tracked script imports these untracked modules
    for py in sorted((ROOT / "scripts").glob("*.py")):
        src = py.read_text(encoding="utf-8")
        for mod in untracked:
            if re.search(rf"(?:from|import)\s+{mod}\b", src):
                pytest.fail(
                    f"scripts/{py.name}: imports '{mod}' which is UNTRACKED. "
                    f"Run: git add scripts/{mod}.py — clean clone will break."
                )
