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
    """The qmd CLI does not exist in this repo; skills must not reference it."""
    skills_dir = ROOT / "skills"
    hits: list[str] = []
    for skill_md in sorted(skills_dir.glob("*/SKILL.md")):
        for i, line in enumerate(
            skill_md.read_text(encoding="utf-8").splitlines(), 1
        ):
            if "qmd" in line.lower():
                hits.append(f"{skill_md.relative_to(ROOT)}:{i}: {line.strip()}")
    assert not hits, "qmd references found in skills (qmd CLI does not exist):\n" + "\n".join(hits)


def test_ci_uses_current_gitleaks_action():
    """Gitleaks must use the Node 24 action with an available scanner release."""
    workflow = (ROOT / ".github" / "workflows" / "tests.yml").read_text(encoding="utf-8")
    assert "gitleaks/gitleaks-action@e0c47f4f8be36e29cdc102c57e68cb5cbf0e8d1e" in workflow


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
