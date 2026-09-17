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


def _read(relative_path: str) -> str:
    return (ROOT / relative_path).read_text(encoding="utf-8")


def _missing(markers, text: str) -> list[str]:
    """Markers that must be in the text and are not; the list is the failure message."""
    return [marker for marker in markers if marker not in text]


def _present(markers, text: str) -> list[str]:
    """Markers that must not be in the text and are; the list is the failure message."""
    return [marker for marker in markers if marker in text]


def _unmatched(patterns, text: str) -> list[str]:
    """Patterns that must match the text and do not."""
    return [pattern for pattern in patterns if re.search(pattern, text) is None]


def _matched(patterns, text: str) -> list[str]:
    """Patterns that must not match the text and do."""
    return [pattern for pattern in patterns if re.search(pattern, text) is not None]


# ─── 1. No QMD references on active product surfaces ────────────────

def _assert_docs_clean(relative_paths, check) -> None:
    for relative_path in relative_paths:
        check(relative_path)


def _powershell_variables(content: str) -> set[str]:
    return set(re.findall(r"\$([A-Za-z_]\w*)", content))


def _powershell_parameter_names(content: str) -> set[str]:
    """Variables bound by a function signature or a param() block."""
    names: set[str] = set()
    for match in re.finditer(r"function\s+[\w-]+\s*\(([^)]*)\)", content):
        names |= _powershell_variables(match.group(1))
    for block in re.finditer(r"param\((.*?)\)\s*(?:\{|\r?\n)", content, re.DOTALL):
        names |= _powershell_variables(block.group(1))
    return names


def _powershell_assigned_variables(content: str) -> set[str]:
    assigned = set(re.findall(r"\$([A-Za-z_]\w*)\s*=", content))
    assigned |= _powershell_parameter_names(content)
    assigned |= set(re.findall(r"foreach\s*\(\s*\$([A-Za-z_]\w*)\s+in\b", content))
    return assigned


_STDLIB_AND_EXTERNAL = frozenset(
    {
        "os", "sys", "re", "json", "time", "datetime", "pathlib",
        "hashlib", "subprocess", "argparse", "contextlib",
        "io", "math", "secrets", "threading", "typing",
        "collections", "functools", "itertools", "enum",
        "dataclasses", "abc", "copy", "tempfile", "shutil",
        "importlib", "traceback", "textwrap", "string",
        "unittest", "pytest", "__future__", "warnings",
    }
)


def _is_tracked_script(py: Path, tracked: set[str]) -> bool:
    return f"scripts/{py.name}" in tracked or py.name in tracked


def _local_import_names(source: str) -> list[str]:
    """Imported names that resolve to a module file in scripts/."""
    names = []
    for match in re.finditer(r"^\s*(?:from|import)\s+(\w+)", source, re.MULTILINE):
        name = match.group(1)
        if name in _STDLIB_AND_EXTERNAL:
            continue
        if (ROOT / "scripts" / f"{name}.py").exists():
            names.append(name)
    return names


def _assert_local_imports_tracked(py: Path, tracked: set[str]) -> None:
    for name in _local_import_names(py.read_text(encoding="utf-8")):
        assert f"scripts/{name}.py" in tracked or f"{name}.py" in tracked, (
            f"scripts/{py.name}: imports '{name}' which exists as "
            f"scripts/{name}.py but is NOT tracked by Git. "
            f"Run: git add scripts/{name}.py"
        )


def _untracked_module_names(status_output: str) -> list[str]:
    names = []
    for line in status_output.strip().splitlines():
        if line.startswith("??") and line.endswith(".py"):
            names.append(line.split("/")[-1].strip().replace(".py", ""))
    return names


def _assert_no_untracked_import(py: Path, untracked: list[str]) -> None:
    source = py.read_text(encoding="utf-8")
    for module in untracked:
        assert re.search(rf"(?:from|import)\s+{module}\b", source) is None, (
            f"scripts/{py.name}: imports '{module}' which is UNTRACKED. "
            f"Run: git add scripts/{module}.py — clean clone will break."
        )


def _assert_referenced_scripts_exist(skill_md: Path, bash_call: str) -> None:
    """Allowed-tools may name runtime commands, but never a missing script."""
    if bash_call.strip().startswith("uv run"):
        return
    for script_rel in re.findall(r"(scripts/\S+\.py)", bash_call):
        assert (ROOT / script_rel).is_file(), (
            f"{skill_md.relative_to(ROOT)}: allowed-tools references "
            f"{script_rel} which does not exist"
        )


def _assert_matching_version(installer: str, src: str, match, current_version: str) -> None:
    tag_version = match.group(1)
    line = src[: match.start()].count("\n") + 1
    assert tag_version == current_version, (
        f"{installer}:{line}: references v{tag_version} but "
        f"pyproject.toml is {current_version}. Update installer comment."
    )


def _assert_no_qmd_claim(relative_path: str) -> None:
    text = (ROOT / relative_path).read_text(encoding="utf-8")
    assert not re.search(r"\bqmd\b", text, re.IGNORECASE), (
        f"{relative_path}: stale QMD claim"
    )


def _assert_no_web_clipper_claim(relative_path: str) -> None:
    text = (ROOT / relative_path).read_text(encoding="utf-8")
    assert "web clipper" not in text.casefold(), (
        f"{relative_path}: stale Web Clipper claim"
    )


def _assert_tool_count_documented(relative_path: str, expected: int) -> None:
    """Every public document states the same MCP tool count as the server."""
    text = (ROOT / relative_path).read_text(encoding="utf-8")
    counts = re.findall(
        r"\b(\d+)\s+(?:\S+\s+)?task-shaped\s+(?:MCP\s+)?(?:tools|инструмент\w*|工具)",
        text,
        re.IGNORECASE,
    )
    assert counts, f"{relative_path}: missing numeric task-shaped MCP tool count"
    assert set(counts) == {str(expected)}, (
        f"{relative_path}: stale task-shaped MCP tool counts: {counts}"
    )


_QMD_FREE_DOCS = (
    "docs/ARCHITECTURE.md",
    "docs/STRUCTURE.md",
    "docs/USER-GUIDE.md",
    "docs/EXPORTING.md",
    "integrations/README.md",
    "tests/README.md",
    "AGENTS.md",
    "CLAUDE.md",
)
_WEB_CLIPPER_FREE_DOCS = (
    "docs/ARCHITECTURE.md",
    "docs/STRUCTURE.md",
    "docs/USER-GUIDE.md",
    "integrations/README.md",
)
_TOOL_COUNT_DOCS = (
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


def _assert_qmd_code_is_gone() -> None:
    skill = ROOT / "skills" / "knowledge-lookup" / "SKILL.md"
    assert not re.search(r"\bqmd\b", skill.read_text(encoding="utf-8"), re.IGNORECASE)
    assert not (ROOT / "scripts" / "bootstrap_qmd.py").exists()
    lookup_mode = (ROOT / "scripts" / "lookup_mode.py").read_text(encoding="utf-8")
    assert not re.search(r"\bqmd\b", lookup_mode, re.IGNORECASE)


def _assert_no_bundled_obsidian_files() -> None:
    obsidian_integration = ROOT / "integrations" / "obsidian"
    bundled = [path for path in obsidian_integration.rglob("*") if path.is_file()]
    assert not bundled, f"bundled Obsidian integration files found: {bundled}"


def _assert_tool_count_everywhere() -> None:
    from mcp_server import TOOL_INPUT_SCHEMAS

    assert len(TOOL_INPUT_SCHEMAS) == 12
    for relative_path in _TOOL_COUNT_DOCS:
        _assert_tool_count_documented(relative_path, len(TOOL_INPUT_SCHEMAS))


def test_no_qmd_refs_in_skills():
    _assert_qmd_code_is_gone()
    _assert_docs_clean(_QMD_FREE_DOCS, _assert_no_qmd_claim)
    _assert_docs_clean(_WEB_CLIPPER_FREE_DOCS, _assert_no_web_clipper_claim)
    _assert_no_bundled_obsidian_files()
    _assert_tool_count_everywhere()


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
        "PSHOME", "PSVersionTable",
    }

    # Collect all $varName references.
    refs = _powershell_variables(content) - skip
    assigned = _powershell_assigned_variables(content)

    undefined = sorted(refs - assigned - skip)
    assert not undefined, f"Undefined PowerShell vars in install.ps1: {undefined}"


# ─── 3. install.sh — no undefined bash variables ────────────────────

def test_install_sh_no_undefined_vars():
    """Every $VAR referenced in install.sh must be assigned or a known environment."""
    content = (ROOT / "install.sh").read_text(encoding="utf-8")

    skip = {
        "HOME", "PATH", "PROFILE", "LLM_WIKI_ROOT", "LLM_WIKI_STATE_ROOT",
        "LLM_WIKI_COMMIT", "LLM_WIKI_INSTALL_SMOKE_TIMEOUT_SECONDS", "SECONDS",
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
    assigned.update(re.findall(r"\blocal\s+([A-Za-z_]\w*)", content))
    assigned.update(re.findall(r"\bfor\s+([A-Za-z_]\w*)\s+in\b", content))

    undefined = sorted(refs - assigned - skip)
    assert not undefined, f"Undefined bash vars in install.sh: {undefined}"


_POWERSHELL_SMOKE_REQUIRED = (
    "Start-Process",
    "-PassThru",
    "$testProcess.Handle",
    ".WaitForExit($testTimeoutMilliseconds)",
    ".ExitCode",
    "finally",
    ".HasExited",
    ".Kill()",
    "taskkill.exe",
    '"/PID"',
    '"/T"',
    '"/F"',
    "WaitForExit(10000)",
    "[int]$testProcess.Id",
    'Ok "Production smoke passed"',
    "LLM_WIKI_INSTALL_SMOKE_TIMEOUT_SECONDS",
    '"Production smoke failed; installation aborted"',
    "Fail $testFailure",
)
_POWERSHELL_SMOKE_FORBIDDEN = (
    "GetTempFileName",
    "RedirectStandard",
    "Get-Content",
    "Remove-Item",
    "$testOutput = uv",
)
# Checked against the casefolded section: `-Match` is as forbidden as `-match`.
_POWERSHELL_SMOKE_FORBIDDEN_CASEFOLDED = ("-match",)
_SHELL_SMOKE_REQUIRED = (
    "testPid=$!",
    "testPgid=$!",
    'wait "$testPid"',
    "if wait_test_child",
    'kill -s TERM -- "-$testPgid"',
    'kill -s CONT -- "-$testPgid"',
    'kill -s KILL -- "-$testPgid"',
    "set -m",
    "set +m",
    'trap \'stop_test_timer; stop_test_child; restore_test_monitor_mode\' EXIT',
    "testMonitorMode=off; set -m",
    "restore_test_monitor_mode",
    'ok "Production smoke passed"',
    "LLM_WIKI_INSTALL_SMOKE_TIMEOUT_SECONDS",
    'fail "Production smoke failed; installation aborted"',
)
_SHELL_SMOKE_FORBIDDEN = (
    "mktemp",
    "testOutput",
    "tail -n 1",
    "cut -c",
    "setsid",
    "wait -f",
    "grep",
    "|| true",
)
_SHELL_SMOKE_REQUIRED_PATTERNS = (
    r"trap .*EXIT",
    r"uv run --locked --no-sync python scripts/install_smoke.py --deadline-seconds 120\s*&",
)
_SHELL_SMOKE_FORBIDDEN_PATTERNS = (r'=\s*"\$\(uv run .*install_smoke',)


def _smoke_section(installer_source: str) -> str:
    """The installer text between the smoke step's heading and the next step's."""
    after_heading = installer_source.split("4. Run production smoke", 1)[1]
    return after_heading.split("5. Set environment variables", 1)[0]


def _assert_powershell_smoke_waits_on_the_process(powershell: str) -> None:
    assert _missing(_POWERSHELL_SMOKE_REQUIRED, powershell) == []
    assert powershell.count("WaitForExit") >= 3
    assert _present(_POWERSHELL_SMOKE_FORBIDDEN, powershell) == []
    assert _present(_POWERSHELL_SMOKE_FORBIDDEN_CASEFOLDED, powershell.casefold()) == []


def _assert_shell_smoke_waits_on_the_process(shell: str) -> None:
    assert _missing(_SHELL_SMOKE_REQUIRED, shell) == []
    assert _present(_SHELL_SMOKE_FORBIDDEN, shell) == []
    assert _unmatched(_SHELL_SMOKE_REQUIRED_PATTERNS, shell) == []
    assert _matched(_SHELL_SMOKE_FORBIDDEN_PATTERNS, shell) == []


def test_installers_do_not_infer_smoke_exit_status_from_output():
    powershell_source = _read("install.ps1")
    assert powershell_source.isascii(), (
        "install.ps1 must remain ASCII-safe for Windows PowerShell 5.1 without a BOM"
    )
    _assert_powershell_smoke_waits_on_the_process(_smoke_section(powershell_source))
    _assert_shell_smoke_waits_on_the_process(_smoke_section(_read("install.sh")))


def test_installers_verify_before_external_configuration_mutation():
    shell = (ROOT / "install.sh").read_text(encoding="utf-8")
    powershell = (ROOT / "install.ps1").read_text(encoding="utf-8")

    assert shell.index('ok "Production smoke passed"') < shell.index(
        "protect_push_urls_if_authorized", shell.index('ok "Production smoke passed"')
    )
    assert powershell.index('Ok "Production smoke passed"') < powershell.index(
        "Protect-PushUrlsIfAuthorized", powershell.index('Ok "Production smoke passed"')
    )


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

def _unreleased_section(changelog: str) -> str | None:
    """The [Unreleased] block up to the next section header, or None without one."""
    unreleased_match = re.search(
        r"^##\s*\[Unreleased[^\]]*\]", changelog, re.MULTILINE
    )
    if not unreleased_match:
        return None
    # Find the next section header to bound the Unreleased block.
    after = changelog[unreleased_match.end():]
    next_hdr = re.search(r"^##\s*\[", after, re.MULTILINE)
    length = next_hdr.start() if next_hdr else len(after)
    return changelog[unreleased_match.start(): unreleased_match.end() + length]


def _assert_unreleased_count_is_live(un_section: str) -> None:
    count_match = re.search(r"(\d+)\s+tests?\b", un_section)
    if not count_match:
        return
    claimed = int(count_match.group(1))
    live = _collect_test_count()
    assert claimed == live, (
        f"CHANGELOG [Unreleased] claims {claimed} tests but live "
        f"suite collects {live}; update CHANGELOG"
    )


def _latest_version_section(changelog: str) -> str:
    headers = list(
        re.finditer(r"^##\s*\[\d+(?:\.\d+)*\]", changelog, re.MULTILINE)
    )
    assert headers, "no version headers in CHANGELOG.md"
    start = headers[0].start()
    end = headers[1].start() if len(headers) > 1 else len(changelog)
    return changelog[start:end]


def _assert_release_count_is_live(section: str) -> None:
    count_match = re.search(r"(\d+)\s+tests?\b", section)
    assert count_match, "no 'N tests' claim in latest CHANGELOG section"
    claimed = int(count_match.group(1))

    live = _collect_test_count()
    assert claimed == live, (
        f"CHANGELOG claims {claimed} tests but live suite collects {live}; "
        f"update CHANGELOG before release"
    )


def test_changelog_test_count_matches_live():
    """The latest CHANGELOG section's 'N tests' claim must match the live count.

    If an [Unreleased] section exists, validate its count when present and leave
    immutable release-history counts alone. Without [Unreleased], check the latest
    version section.
    """
    changelog = _read("CHANGELOG.md")

    # Check for an [Unreleased] section first — dev work in progress.
    un_section = _unreleased_section(changelog)
    if un_section is not None:
        _assert_unreleased_count_is_live(un_section)
        return

    # Fall through to version-numbered sections.
    _assert_release_count_is_live(_latest_version_section(changelog))


# ─── 6. ARCHITECTURE.md must not cite Recall@2 ──────────────────────

_ARCHITECTURE_REQUIRED = (
    "NATIVE LIFECYCLE EVENTS",
    "MCP READS + ACTIONS",
    "LLM BACKEND (CLASSIFY + COMPILE ONLY)",
    "5 backends including Ollama",
    "### Optional semantic tier",
    "### Hybrid tier",
)
# The casefolded tuples are compared with the casefolded document.
_ARCHITECTURE_FORBIDDEN_CASEFOLDED = (
    "unique: no other system",
    "base, zero dependencies",
    "base install remains zero-dep",
)
_INSTALLER_BASELINE_CASEFOLDED = (
    "installer baseline",
    "manual dependency selection",
)
# Issue #29: the legacy `cache/vectors.npy` pair does not exist on a 4.0
# vault; vectors live in the active evidence generation and are built by
# a generation refresh, and the guide must say so.
_GUIDE_VECTOR_REQUIRED = (
    "intfloat/multilingual-e5-small",
    "cache/evidence-graph/generations/",
)
_GUIDE_VECTOR_FORBIDDEN = ("MiniLM", "vectors.json")
_STRUCTURE_VECTOR_REQUIRED = (
    "cache/evidence-graph/generations/<generation-id>/",
    "vectors.json",
)
_STRUCTURE_VECTOR_FORBIDDEN = ("cache/vectors.json",)


def _assert_architecture_names_the_real_tiers(arch: str) -> None:
    assert "Recall@2" not in arch, (
        "docs/ARCHITECTURE.md cites Recall@2, which is absent from "
        "benchmark/report.md — remove or replace with a reported metric"
    )
    assert _missing(_ARCHITECTURE_REQUIRED, arch) == []
    base = arch.split("### Base retrieval tier", 1)[1].split("### ", 1)[0]
    assert "Vector" not in base


def _assert_architecture_makes_no_stale_claim(arch: str) -> None:
    assert _present(_ARCHITECTURE_FORBIDDEN_CASEFOLDED, arch.casefold()) == []
    assert _missing(_INSTALLER_BASELINE_CASEFOLDED, arch.casefold()) == []


def _assert_vector_storage_is_documented() -> None:
    # A line here used to read `assert <legacy cache path> not in <the guide's
    # semantic section> or True`. `x or True` is always true, so it asserted
    # nothing and was dropped rather than carried along as a check it never was.
    guide = _read("docs/USER-GUIDE.md")
    assert _missing(_GUIDE_VECTOR_REQUIRED, guide) == []
    assert _present(_GUIDE_VECTOR_FORBIDDEN, guide) == []

    structure = _read("docs/STRUCTURE.md")
    assert _present(_STRUCTURE_VECTOR_FORBIDDEN, structure) == []
    assert _missing(_STRUCTURE_VECTOR_REQUIRED, structure) == []


def _assert_release_gate_is_documented() -> None:
    search_source = _read("scripts/search_memory.py")
    assert "legacy vectors.json" not in search_source

    contributing = _read("CONTRIBUTING.md")
    assert "full regression suite is the release gate" in contributing.casefold()
    assert re.search(r"\b\d+\s+tests collected\b", contributing) is None

    integrations = _read("integrations/README.md")
    assert _missing(_INSTALLER_BASELINE_CASEFOLDED, integrations.casefold()) == []


def test_architecture_no_recall_at_2():
    """Recall@2 is not in benchmark/report.md; docs must not cite it."""
    arch = _read("docs/ARCHITECTURE.md")
    _assert_architecture_names_the_real_tiers(arch)
    _assert_architecture_makes_no_stale_claim(arch)
    _assert_vector_storage_is_documented()
    _assert_release_gate_is_documented()


_STAGE_TWO_DOCS = (
    "docs/ARCHITECTURE.md",
    "docs/USER-GUIDE.md",
    "docs/operating-model.md",
    "AGENTS.md",
    "CLAUDE.md",
)
# Compared with the casefolded text of all five documents joined together.
_STAGE_TWO_MARKERS_CASEFOLDED = (
    "markdown remains authoritative",
    "rollback-journal",
    "synchronous=full",
    "no wal",
    "local filesystem",
    "best-effort",
    "mixed tree",
    "cooperating",
    "cas",
    "2-day undo",
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
_STAGE_TWO_ARCHITECTURE_CASEFOLDED = (
    "sqlite knowledge source",
    "at least once",
)
_STAGE_TWO_GUIDE_COMMANDS = (
    "markdown_transaction.py recover",
    "markdown_transaction.py undo <transaction-id>",
    "markdown_transaction.py prune --retention-days 30",
    "memory_queue.py migrate",
    "memory_queue.py redrive <task-id>",
    "memory_queue.py purge --terminal-before <ISO-8601> --export <path>",
    "archive_daily.py --commit --hot-days 90",
)


def test_stage_two_reliability_contract_is_documented():
    combined = "\n".join(map(_read, _STAGE_TWO_DOCS)).casefold()
    missing = _missing(_STAGE_TWO_MARKERS_CASEFOLDED, combined)
    assert missing == [], f"Stage 2 docs missing contract marker {missing!r}"

    architecture = _read("docs/ARCHITECTURE.md").casefold()
    assert _missing(_STAGE_TWO_ARCHITECTURE_CASEFOLDED, architecture) == []

    assert _missing(_STAGE_TWO_GUIDE_COMMANDS, _read("docs/USER-GUIDE.md")) == []


# ─── 7. Skills' allowed-tools reference existing scripts ────────────

def test_skills_allowed_tools_reference_existing_scripts():
    """Direct Bash(script ...) references in skills must point to real files."""
    skills_dir = ROOT / "skills"
    for skill_md in sorted(skills_dir.glob("*/SKILL.md")):
        text = skill_md.read_text(encoding="utf-8")
        for bash_call in re.findall(r"Bash\(([^)]*)\)", text):
            _assert_referenced_scripts_exist(skill_md, bash_call)


# ─── 8. README must not invent agentmemory Recall@10 ────────────────

def _assert_no_competitor_percentage(competitor_cells) -> None:
    for cell in competitor_cells:
        assert not re.search(r"\d+\.?\d*%", cell), (
            f"README Recall@10 competitor cell '{cell}' has a percentage "
            f"not backed by benchmark/report.md — use 'n/a'"
        )


def test_readme_recall_at_10_agentmemory():
    """README must not claim a competitor Recall@10 % unless report.md has it."""
    readme = _read("README.md")
    report = _read("benchmark/report.md")

    report_has_recall10 = "Recall@10" in report

    row = re.search(
        r"\|\s*Recall@10\s*\|([^|]+)\|([^|]+)\|([^|]+)\|([^|]+)\|", readme
    )
    if not row:
        return  # no Recall@10 row — nothing to guard

    cells = [c.strip() for c in row.groups()]
    # cells[0] = LLM Wiki (allowed to have a %); rest are competitors.
    if not report_has_recall10:
        _assert_no_competitor_percentage(cells[1:])


# ─── 9. Lint check count in docs must match code ────────────────────

def test_lint_check_count_matches_code():
    """The lint check count in README/docs must match lint_memory.py source."""
    import lint_memory

    # The registry is the count; counting `def check_` lines drifted whenever a
    # helper was renamed and said nothing about which categories exist.
    actual = len(lint_memory.CHECK_NAMES)
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


# ─── 10. Retired Cognee bridge stays retired ────────────────────────

_COGNEE_FREE_DOCS = (
    "README.md",
    "README.ru.md",
    "README.zh-CN.md",
    "docs/ARCHITECTURE.md",
    "docs/USER-GUIDE.md",
    "CONTRIBUTING.md",
    "knowledge/README.md",
)
_COGNEE_ADVERTISING = ("--extra cognee", "scripts/cognee_sync.py", "Optional: Cognee")
_COGNEE_LEGACY_CACHE_REQUIRED = (
    "`cache/cognee/` — retired disposable legacy cache",
    "never removed automatically",
)


def _assert_cognee_code_is_gone() -> None:
    assert not (ROOT / "scripts" / "cognee_sync.py").exists()
    assert not (ROOT / "docs" / "SETUP-COGNEE.md").exists()

    pyproject = _read("pyproject.toml")
    lockfile = _read("uv.lock")
    assert 'cognee = [' not in pyproject
    assert 'name = "cognee"' not in lockfile


def _assert_installer_leaves_cognee_cache_alone(installer: str) -> None:
    source = _read(installer)
    assert "cache/cognee" not in source.replace("\\", "/")


def _assert_doc_does_not_advertise_cognee(doc_name: str) -> None:
    advertised = _present(_COGNEE_ADVERTISING, _read(doc_name))
    assert advertised == [], f"{doc_name} still advertises retired Cognee: {advertised}"


def test_cognee_bridge_is_retired_without_deleting_legacy_cache():
    """Cognee has no supported entry point, but its old cache is preserved."""
    _assert_cognee_code_is_gone()
    _assert_docs_clean(("install.sh", "install.ps1"), _assert_installer_leaves_cognee_cache_alone)
    _assert_docs_clean(_COGNEE_FREE_DOCS, _assert_doc_does_not_advertise_cognee)
    assert _missing(_COGNEE_LEGACY_CACHE_REQUIRED, _read("docs/STRUCTURE.md")) == []


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
            _assert_matching_version(installer, src, m, current_version)


_MUTABLE_BOOTSTRAP = (
    "raw.githubusercontent.com/Ekgardt/llm-wiki/main/install.",
    "/v4.0.0/install.",
    "git clone --branch v4.0.0",
    "astral.sh/uv/install.",
)
_INSTALLER_PIN_REQUIRED = ("LLM_WIKI_COMMIT", "full 40-hex commit OID")
_INSTALL_SH_ROOT_REQUIRED = (
    "uv is required",
    'VAULT_ROOT="${LLM_WIKI_ROOT:-$SCRIPT_DIR}"',
    '${BASH_SOURCE[0]:-}',
)
_INSTALL_PS1_ROOT_REQUIRED = (
    "uv is required",
    "if ($env:LLM_WIKI_ROOT)",
    "IsNullOrWhiteSpace($PSScriptRoot)",
)
_INSTALLER_FALSE_COMFORT = ("core features will still work",)


def _assert_no_mutable_bootstrap(name: str) -> None:
    advertised = _present(_MUTABLE_BOOTSTRAP, _read(name))
    assert advertised == [], f"{name}: advertises mutable bootstrap {advertised!r}"


def _assert_installer_pins_its_commit(installer: str) -> None:
    source = _read(installer)
    assert _missing(_INSTALLER_PIN_REQUIRED, source) == []
    assert "scripts/installer_config.py" in source.replace("\\", "/")
    assert "protect_push" in source.casefold().replace("-", "_")


def _assert_installers_fail_closed() -> None:
    shell = _read("install.sh")
    powershell = _read("install.ps1")
    assert _missing(_INSTALL_SH_ROOT_REQUIRED, shell) == []
    assert _missing(_INSTALL_PS1_ROOT_REQUIRED, powershell) == []
    assert _present(_INSTALLER_FALSE_COMFORT, shell) == []
    assert _present(_INSTALLER_FALSE_COMFORT, powershell) == []


def test_remote_bootstrap_is_immutable_and_fail_closed():
    _assert_docs_clean(
        ("install.sh", "install.ps1", "docs/USER-GUIDE.md"), _assert_no_mutable_bootstrap
    )
    _assert_docs_clean(("install.sh", "install.ps1"), _assert_installer_pins_its_commit)
    _assert_installers_fail_closed()


def test_unix_installer_is_executable_in_git():
    result = subprocess.run(
        ["git", "ls-files", "--stage", "install.sh"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout.split(maxsplit=1)[0] == "100755"


# ─── 12. All daily-log writers use shared lock ──────────────────────

# Writes that reach a daily log: an open() on a daily path, a write through
# DAILY_DIR, or a call into the shared appender. `append.*daily` used to be one
# of these patterns and matched any line where an unrelated `.append(` happened
# to sit beside a `daily_id` argument.
_DAILY_WRITE_PATTERN = re.compile(
    r"daily[\w.]*\.open\s*\(|DAILY_DIR.*\.write|append_daily\s*\("
)
_DAILY_LOCK_MARKERS = ("append_daily", "locked_append")
_DAILY_INFRASTRUCTURE = ("daily_log_append.py", "memory_state.py")


def _writes_daily_log(source: str) -> bool:
    return _DAILY_WRITE_PATTERN.search(source) is not None


def _uses_daily_lock(source: str) -> bool:
    return any(marker in source for marker in _DAILY_LOCK_MARKERS)


def test_all_daily_writers_use_lock():
    """Scripts that write to daily logs must go through append_daily or locked_append."""
    for py in sorted((ROOT / "scripts").glob("*.py")):
        if py.name in _DAILY_INFRASTRUCTURE:
            continue  # These define the lock/append infrastructure.
        source = py.read_text(encoding="utf-8")
        if not _writes_daily_log(source):
            continue
        assert _uses_daily_lock(source), (
            f"{py.name}: writes to daily log without append_daily() or "
            f"locked_append(). All daily-log writes must go through the transaction."
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
        if not _is_tracked_script(py, tracked):
            continue  # untracked script — skip (will be caught by git status)
        _assert_local_imports_tracked(py, tracked)


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
    untracked = _untracked_module_names(r.stdout)
    if not untracked:
        return  # No untracked .py files — clean

    # Check if any tracked script imports these untracked modules
    for py in sorted((ROOT / "scripts").glob("*.py")):
        _assert_no_untracked_import(py, untracked)


@pytest.mark.parametrize("entry_point", ["install_smoke", "sync_memory", "doctor", "mcp_server"])
def test_production_entry_points_import_without_pyyaml(entry_point):
    """The entry points start without importing PyYAML, even though a base install now has it.

    A module-level import of `corpus_snapshot` from `doctor` pulled `yaml` into
    `install_smoke` and the clean production job failed in nine seconds
    (PR #16, 2026-09-10). Importing each entry point with `yaml` blocked is the
    check that job runs, minus the runner.
    """
    code = (
        "import sys; sys.modules['yaml'] = None; "
        f"sys.path.insert(0, {str(ROOT / 'scripts')!r}); import {entry_point}"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, cwd=ROOT, check=False
    )
    assert result.returncode == 0, result.stderr[-800:]
