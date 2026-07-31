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


# ─── 1. No QMD references on active product surfaces ────────────────

def test_no_qmd_refs_in_skills():
    skill = ROOT / "skills" / "knowledge-lookup" / "SKILL.md"
    assert not re.search(r"\bqmd\b", skill.read_text(encoding="utf-8"), re.IGNORECASE)
    assert not (ROOT / "scripts" / "bootstrap_qmd.py").exists()
    lookup_mode = (ROOT / "scripts" / "lookup_mode.py").read_text(encoding="utf-8")
    assert not re.search(r"\bqmd\b", lookup_mode, re.IGNORECASE)
    active_docs = (
        "docs/ARCHITECTURE.md",
        "docs/STRUCTURE.md",
        "docs/USER-GUIDE.md",
        "docs/EXPORTING.md",
        "docs/SETUP-COGNEE.md",
        "integrations/README.md",
        "tests/README.md",
        "AGENTS.md",
        "CLAUDE.md",
        "knowledge/notes/Retrieval Workflow.md",
        "knowledge/notes/Ingestion Workflow.md",
    )
    for relative_path in active_docs:
        text = (ROOT / relative_path).read_text(encoding="utf-8")
        assert not re.search(r"\bqmd\b", text, re.IGNORECASE), (
            f"{relative_path}: stale QMD claim"
        )

    integration_docs = (
        "docs/ARCHITECTURE.md",
        "docs/STRUCTURE.md",
        "docs/USER-GUIDE.md",
        "integrations/README.md",
        "knowledge/notes/Ingestion Workflow.md",
    )
    for relative_path in integration_docs:
        text = (ROOT / relative_path).read_text(encoding="utf-8")
        assert "web clipper" not in text.casefold(), (
            f"{relative_path}: stale Web Clipper claim"
        )
    obsidian_integration = ROOT / "integrations" / "obsidian"
    bundled = [path for path in obsidian_integration.rglob("*") if path.is_file()]
    assert not bundled, f"bundled Obsidian integration files found: {bundled}"
    obsidian_note = (ROOT / "knowledge" / "notes" / "Obsidian.md").read_text(
        encoding="utf-8"
    )
    assert "canonical viewer" not in obsidian_note.casefold()
    assert "frontend here" not in obsidian_note.casefold()

    karpathy = (ROOT / "knowledge" / "notes" / "Andrej Karpathy.md").read_text(
        encoding="utf-8"
    )
    assert "historical" in karpathy.casefold()
    assert "current retrieval" in karpathy.casefold()

    from mcp_server import TOOL_INPUT_SCHEMAS

    assert len(TOOL_INPUT_SCHEMAS) == 12
    active_public_docs = (
        "README.md",
        "README.ru.md",
        "README.zh-CN.md",
        "AGENTS.md",
        "CLAUDE.md",
        "docs/ARCHITECTURE.md",
        "docs/STRUCTURE.md",
        "docs/USER-GUIDE.md",
        "integrations/README.md",
        "tests/README.md",
    )
    for relative_path in active_public_docs:
        text = (ROOT / relative_path).read_text(encoding="utf-8")
        counts = re.findall(
            r"\b(\d+)\s+(?:\S+\s+)?task-shaped\s+(?:MCP\s+)?(?:tools|инструмент\w*|工具)",
            text,
            re.IGNORECASE,
        )
        assert counts, f"{relative_path}: missing numeric task-shaped MCP tool count"
        assert set(counts) == {str(len(TOOL_INPUT_SCHEMAS))}, (
            f"{relative_path}: stale task-shaped MCP tool counts: {counts}"
        )


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
    for fm in re.finditer(r"function\s+[\w-]+\s*\(([^)]*)\)", content):
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
        "SHELL", "BASH_SOURCE", "ZSH_VERSION", "BASH_VERSION", "TMPDIR",
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


def test_installers_do_not_infer_pytest_exit_status_from_output():
    powershell_source = (ROOT / "install.ps1").read_text(encoding="utf-8")
    assert powershell_source.isascii(), (
        "install.ps1 must remain ASCII-safe for Windows PowerShell 5.1 without a BOM"
    )
    powershell = powershell_source.split("4. Run tests", 1)[1].split(
        "5. Set environment variables", 1
    )[0]
    shell = (ROOT / "install.sh").read_text(encoding="utf-8").split(
        "4. Run tests", 1
    )[1].split("5. Set environment variables", 1)[0]

    assert "Start-Process" in powershell
    assert "-PassThru" in powershell
    assert "$testProcess.Handle" in powershell
    assert ".WaitForExit()" in powershell
    assert ".ExitCode" in powershell
    assert "finally" in powershell
    assert ".HasExited" in powershell
    assert ".Kill()" in powershell
    assert powershell.count("WaitForExit") >= 3
    assert "taskkill.exe" in powershell
    assert '"/PID"' in powershell
    assert '"/T"' in powershell
    assert '"/F"' in powershell
    assert "WaitForExit(10000)" in powershell
    assert "[int]$testProcess.Id" in powershell
    assert "GetTempFileName" not in powershell
    assert "RedirectStandard" not in powershell
    assert "Get-Content" not in powershell
    assert "Remove-Item" not in powershell
    assert "$testOutput = uv" not in powershell
    assert "-match" not in powershell.casefold()
    assert "mktemp" not in shell
    assert "testOutput" not in shell
    assert "tail -n 1" not in shell
    assert "cut -c" not in shell
    assert re.search(r"trap .*EXIT", shell)
    assert re.search(r"uv run pytest -q\s*&", shell)
    assert "testPid=$!" in shell
    assert "testPgid=$!" in shell
    assert 'wait "$testPid"' in shell
    assert "if wait_test_child" in shell
    assert 'kill -s TERM -- "-$testPgid"' in shell
    assert 'kill -s CONT -- "-$testPgid"' in shell
    assert 'kill -s KILL -- "-$testPgid"' in shell
    assert "set -m" in shell
    assert "set +m" in shell
    assert 'trap \'stop_test_child; restore_test_monitor_mode\' EXIT' in shell
    assert "testMonitorMode=off; set -m" in shell
    assert "restore_test_monitor_mode" in shell
    assert "setsid" not in shell
    assert "wait -f" not in shell
    assert not re.search(r'=\s*"\$\(uv run pytest', shell)
    assert "grep" not in shell
    assert "|| true" not in shell
    assert 'ok "Test suite passed"' in shell
    assert 'Ok "Test suite passed"' in powershell


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
    """The latest CHANGELOG section's 'N tests' claim must match the live count.

    If an [Unreleased] section exists with a test count, that takes priority
    (development in progress). Otherwise the latest version section is checked.
    """
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")

    # Check for an [Unreleased] section first — dev work in progress.
    unreleased_match = re.search(
        r"^##\s*\[Unreleased[^\]]*\]", changelog, re.MULTILINE
    )
    if unreleased_match:
        # Find the next section header to bound the Unreleased block.
        after = changelog[unreleased_match.end():]
        next_hdr = re.search(r"^##\s*\[", after, re.MULTILINE)
        un_section = changelog[unreleased_match.start():
            unreleased_match.end() + (next_hdr.start() if next_hdr else len(after))
        ]
        count_match = re.search(r"(\d+)\s+tests?\b", un_section)
        if count_match:
            claimed = int(count_match.group(1))
            live = _collect_test_count()
            assert claimed == live, (
                f"CHANGELOG [Unreleased] claims {claimed} tests but live "
                f"suite collects {live}; update CHANGELOG"
            )
            return

    # Fall through to version-numbered sections.
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
    assert "NATIVE LIFECYCLE EVENTS" in arch
    assert "MCP READS + ACTIONS" in arch
    assert "LLM BACKEND (CLASSIFY + COMPILE ONLY)" in arch
    assert "5 backends including Ollama" in arch
    assert "unique: no other system" not in arch.casefold()

    base = arch.split("### Base retrieval tier", 1)[1].split("### ", 1)[0]
    assert "Vector" not in base
    assert "### Optional semantic tier" in arch
    assert "### Hybrid tier" in arch
    assert "base, zero dependencies" not in arch.casefold()
    assert "base install remains zero-dep" not in arch.casefold()
    assert "installer baseline" in arch.casefold()
    assert "manual dependency selection" in arch.casefold()

    guide = (ROOT / "docs" / "USER-GUIDE.md").read_text(encoding="utf-8")
    assert "BAAI/bge-small-en-v1.5" in guide
    assert "cache/vectors.npy" in guide
    assert "vectors_meta.json" in guide
    assert "MiniLM" not in guide
    assert "vectors.json" not in guide

    structure = (ROOT / "docs" / "STRUCTURE.md").read_text(encoding="utf-8")
    assert "cache/vectors.json" not in structure
    assert "cache/evidence-graph/generations/<generation-id>/" in structure
    assert "vectors.json" in structure
    search_source = (ROOT / "scripts" / "search_memory.py").read_text(encoding="utf-8")
    assert "legacy vectors.json" not in search_source

    contributing = (ROOT / "CONTRIBUTING.md").read_text(encoding="utf-8")
    assert "5087 tests collected" in contributing

    integrations = (ROOT / "integrations" / "README.md").read_text(encoding="utf-8")
    assert "installer baseline" in integrations.casefold()
    assert "manual dependency selection" in integrations.casefold()


def test_stage_two_reliability_contract_is_documented():
    docs = {
        relative: (ROOT / relative).read_text(encoding="utf-8")
        for relative in (
            "docs/ARCHITECTURE.md",
            "docs/USER-GUIDE.md",
            "docs/operating-model.md",
            "AGENTS.md",
            "CLAUDE.md",
        )
    }
    combined = "\n".join(docs.values()).casefold()
    required = (
        "markdown remains authoritative",
        "rollback-journal",
        "synchronous=full",
        "no wal",
        "local filesystem",
        "best-effort",
        "mixed tree",
        "cooperating",
        "cas",
        "30-day undo",
        "source failure",
        "live project lease",
        "automatic git",
        "persistent daemon",
        "cloud service",
        "remote queue",
        "exactly-once",
        "gzip",
        "eager backfill",
        "semantic supersession",
        "quarantine",
    )
    for marker in required:
        assert marker in combined, f"Stage 2 docs missing contract marker {marker!r}"

    architecture = docs["docs/ARCHITECTURE.md"].casefold()
    assert "sqlite knowledge source" in architecture
    assert "at least once" in architecture

    guide = docs["docs/USER-GUIDE.md"]
    for command in (
        "markdown_transaction.py recover",
        "markdown_transaction.py undo <transaction-id>",
        "markdown_transaction.py prune --retention-days 30",
        "memory_queue.py migrate",
        "memory_queue.py redrive <task-id>",
        "memory_queue.py purge --terminal-before <ISO-8601> --export <path>",
        "archive_daily.py --commit --hot-days 90",
    ):
        assert command in guide


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
