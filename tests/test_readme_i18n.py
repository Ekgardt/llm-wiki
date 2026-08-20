"""Guard: public READMEs stay in sync on critical facts.

Prevents shipping EN updates while RU/ZH lag (release regression).
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
README_FILES = [
    ROOT / "README.md",
    ROOT / "README.ru.md",
    ROOT / "README.zh-CN.md",
]
SHARED_COMMANDS = (
    "uv sync --locked --no-default-groups",
    "uv sync --locked --no-default-groups --inexact --extra hybrid",
    "uv run --locked --no-sync python scripts/install_smoke.py --deadline-seconds 120",
    "uv run --locked --no-sync python scripts/repair_installed_memory.py --check --json",
)


def _readmes() -> list[tuple[Path, str]]:
    return [(path, path.read_text(encoding="utf-8")) for path in README_FILES]


def _collect_test_count() -> int:
    r = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q"],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    # last line like "171 tests collected in 0.12s" or "171 selected"
    text = (r.stdout or "") + (r.stderr or "")
    m = re.search(r"(\d+)\s+tests?\s+collected", text)
    if m:
        return int(m.group(1))
    m = re.search(r"(\d+)\s+selected", text)
    if m:
        return int(m.group(1))
    raise AssertionError(f"could not parse pytest collect count:\n{text[-500:]}")


def test_all_readmes_exist():
    for p in README_FILES:
        assert p.is_file(), f"missing {p.name}"


def test_all_readmes_use_dynamic_ci_badge_and_local_installers():
    """Every README links status to CI and documents local installer execution."""
    for p, text in _readmes():
        assert "actions/workflows/tests.yml/badge.svg" in text, (
            f"{p.name}: test badge must report the live workflow status"
        )
        for command in (
            'LLM_WIKI_ROOT="$(pwd)" bash ./install.sh',
            "$env:LLM_WIKI_ROOT = (Get-Location).Path",
            ".\\install.ps1",
        ):
            assert command in text, f"{p.name}: missing local installer {command!r}"


def test_all_readmes_use_correct_github_repo():
    for p in README_FILES:
        text = p.read_text(encoding="utf-8")
        assert "Ekgardt/llm-wiki" in text, f"{p.name}: missing Ekgardt/llm-wiki"
        assert "llm-knowledge/notes" not in text, f"{p.name}: stale llm-knowledge URL"


def test_readmes_advertise_only_immutable_remote_bootstrap():
    for path, text in _readmes():
        assert "raw.githubusercontent.com/Ekgardt/llm-wiki/main/install." not in text, (
            f"{path.name}: mutable main bootstrap must not be advertised"
        )
        assert "/v4.0.0/install." not in text, f"{path.name}: tag bootstrap must not be advertised"
        assert "brightgreen.svg" not in text, f"{path.name}: static green badge is unverified"
        assert "CI green" not in text, f"{path.name}: CI status claim is unverified"
        assert "LLM_WIKI_COMMIT" in text, (
            f"{path.name}: approved full-OID bootstrap target must be explicit"
        )
        assert "40" in text, f"{path.name}: full commit OID length must be explicit"


def test_readmes_share_locked_install_and_read_only_repair_commands():
    for path, text in _readmes():
        for command in SHARED_COMMANDS:
            assert command in text, f"{path.name}: missing shared command {command!r}"
        assert "reliability_v3_runtime_activation_incomplete" in text, (
            f"{path.name}: must state that mutating v3 adoption is not activated"
        )


def test_readmes_mark_precommit_as_opt_in() -> None:
    command = (
        "uv run --locked --no-sync pre-commit install --hook-type pre-commit --hook-type pre-push"
    )
    markers = {
        "README.md": "Opt-in; the installer does not activate these hooks",
        "README.ru.md": "Опционально; установщик не активирует эти хуки",
        "README.zh-CN.md": "可选；安装程序不会启用这些钩子",
    }
    for path, text in _readmes():
        assert markers[path.name] in text, f"{path.name}: pre-commit activation is ambiguous"
        assert command in text, f"{path.name}: missing opt-in pre-commit command"


def test_readmes_describe_managed_local_ide_hooks_and_viewer_integration() -> None:
    markers = {
        "README.md": (
            "Automatic local hooks when detected",
            "Cursor cloud agents do not load user-level hooks",
            "Viewer only",
        ),
        "README.ru.md": (
            "Автоматические локальные хуки после обнаружения",
            "Cursor cloud agents не загружают user-level хуки",
            "Только viewer",
        ),
        "README.zh-CN.md": (
            "检测到后自动启用本地钩子",
            "Cursor 云端智能体不会加载用户级钩子",
            "仅 viewer",
        ),
    }
    for path, text in _readmes():
        for marker in markers[path.name]:
            assert marker in text, f"{path.name}: missing integration status {marker!r}"


def test_installers_report_agent_activation_and_scheduler_limits_truthfully() -> None:
    shell = (ROOT / "install.sh").read_text(encoding="utf-8")
    powershell = (ROOT / "install.ps1").read_text(encoding="utf-8")
    guide = (ROOT / "docs/USER-GUIDE.md").read_text(encoding="utf-8")

    for installer in (shell, powershell):
        assert "OpenCode: active automatic" in installer
        assert "Cursor: active automatic local hooks" in installer
        assert "Antigravity: active automatic local hooks" in installer
        assert "Agent integrations:" in installer
    assert "captures automatically" not in shell
    assert "capture is automatic" not in powershell
    assert "even while you sleep" not in guide
    assert "Windows tasks run only while the current user is logged on" in guide


def test_windows_scheduler_status_validates_registered_contract() -> None:
    source = (ROOT / "scripts/install-scheduled-tasks.ps1").read_text(encoding="utf-8")

    assert "function Test-LLMWikiScheduledTasks" in source
    assert ".Principal.LogonType" in source
    assert ".Actions.Count" in source
    assert ".Triggers.Count" in source
    assert "if ($verified)" in source
    assert "exit 1" in source


def test_all_readmes_mention_knowledge_layout():
    for p in README_FILES:
        text = p.read_text(encoding="utf-8")
        assert "knowledge/" in text, f"{p.name}: must document knowledge/ layout"


def test_all_readmes_mention_current_version():
    """Every README must mention the version declared in pyproject.toml.
    The version is read live so bumping pyproject + READMEs in the same
    change keeps this test green without editing the test itself.
    """
    import re as _re

    pyproject = Path(__file__).resolve().parent.parent / "pyproject.toml"
    m = _re.search(
        r'^version\s*=\s*"([^"]+)"', pyproject.read_text(encoding="utf-8"), _re.MULTILINE
    )
    assert m, "could not parse version from pyproject.toml"
    current = m.group(1)
    required = ("MCP", "12", "doctor", "envelope", "resource", "integration_adapter.py")
    critical_markers = {
        "README.md": (
            "12 task-shaped",
            "full regression suite",
            "Historical current 112",
            "optional Obsidian viewer",
        ),
        "README.ru.md": (
            "12 task-shaped",
            "полный регрессионный набор",
            "Исторические текущие 112",
            "Obsidian как опциональный viewer",
        ),
        "README.zh-CN.md": (
            "12 个 task-shaped",
            "完整回归套件",
            "历史当前 112",
            "Obsidian 为可选 viewer",
        ),
    }
    for p, text in _readmes():
        assert current in text, (
            f"{p.name}: must mention version {current} (current release per pyproject.toml)"
        )
        for claim in required:
            assert claim.casefold() in text.casefold(), f"{p.name}: missing {claim!r} claim"
        assert "degraded" in text.casefold(), f"{p.name}: missing degraded-only health claim"
        assert "obsidian" in text.casefold(), f"{p.name}: missing optional Obsidian viewer"
        assert "web clipper" not in text.casefold(), f"{p.name}: stale Web Clipper claim"
        assert not re.search(r"\bqmd\b", text, re.IGNORECASE), f"{p.name}: stale QMD claim"
        for marker in critical_markers[p.name]:
            assert marker in text, f"{p.name}: missing critical parity marker {marker!r}"
        assert "zero runtime dependencies" not in text.casefold()
        assert "stdlib-only" not in text.casefold()


def test_all_readmes_share_reliable_memory_operator_commands():
    commands = (
        "uv run python scripts/doctor.py",
        "uv run python scripts/doctor.py --repair",
        "uv run python scripts/markdown_transaction.py recover",
        "uv run python scripts/markdown_transaction.py undo <transaction-id>",
        "uv run python scripts/markdown_transaction.py prune --retention-days 30",
        "uv run python scripts/memory_queue.py migrate",
        "uv run python scripts/memory_queue.py work --max-tasks 20 --max-seconds 600 "
        "--idle-seconds 2 --lease-seconds 120 --heartbeat-seconds 40 "
        "--max-attempts 8 --retry-base-seconds 30 --retry-cap-seconds 3600",
        "uv run python scripts/memory_queue.py redrive <task-id>",
        "uv run python scripts/memory_queue.py purge --terminal-before <ISO-8601> --export <path>",
        "uv run python scripts/archive_daily.py --commit --hot-days 90",
        "uv run python benchmark/run_contradiction_benchmark.py --corpus "
        "benchmark/contradiction-v1.json",
    )
    for path, text in _readmes():
        for command in commands:
            assert command in text, f"{path.name}: missing operator command {command!r}"


def test_all_readmes_share_locked_dependency_profiles_and_smoke_contract() -> None:
    commands = (
        "uv sync --locked --no-default-groups",
        "uv run --locked --no-sync python scripts/install_smoke.py --deadline-seconds 120",
        "uv sync --locked --no-default-groups --inexact --extra hybrid",
        "uv sync --locked --no-default-groups --inexact --extra code-graph",
        "uv sync --locked",
        "uv run --locked --no-sync pytest -q",
    )
    for path, text in _readmes():
        for command in commands:
            assert command in text, f"{path.name}: missing dependency command {command!r}"
        assert "mcp-server" in text, f"{path.name}: missing MCP compatibility alias"
        assert "compatibility alias" in text, f"{path.name}: alias semantics must be explicit"
        assert "production smoke" in text, f"{path.name}: missing bounded smoke claim"
        assert "Node 22" in text, f"{path.name}: missing optional navigation prerequisite"


def test_ci_qualifies_real_pyright_on_all_supported_os_families():
    import yaml

    workflow = yaml.safe_load(
        (ROOT / ".github" / "workflows" / "tests.yml").read_text(encoding="utf-8")
    )
    job = workflow["jobs"]["pyright-navigation"]
    entries = job["strategy"]["matrix"]["include"]
    assert [
        (entry["os"], entry["platform"], entry["python"], entry["node"])
        for entry in entries
    ] == [
        ("ubuntu-24.04", "linux", "3.10", "22.23.1"),
        ("windows-2025", "windows", "3.10", "22.23.1"),
        ("macos-15", "macos", "3.10", "22.23.1"),
    ]
    # The budget is per platform, because the same suite takes about three
    # times longer on the hosted Windows image. Every family declares one.
    assert all(entry["timeout"] > 0 for entry in entries)
    assert job["timeout-minutes"] == "${{ matrix.timeout }}"
    assert job["env"]["LLM_WIKI_STATE_ROOT"] == ("${{ github.workspace }}/../llm-wiki-state")
    assert job["env"]["LLM_WIKI_TEST_USE_EXTERNAL_STATE"] == "1"
    install_step = next(
        step for step in job["steps"] if step.get("name") == "Explicit Pyright install"
    )
    assert '"${{ env.LLM_WIKI_STATE_ROOT }}"' in install_step["run"]
    assert "shell" not in install_step


def test_docs_state_security_install_and_market_truth():
    text = (ROOT / "docs" / "CODE-NAVIGATION.md").read_text(encoding="utf-8")
    for value in (
        "trusted local repositories",
        "not an OS sandbox",
        "Pyright 1.1.411",
        "cache/code-tools/pyright/1.1.411/",
        "run/lsp/<owner-nonce>/",
        "never downloads during a query",
        "Market superiority remains unclaimed",
    ):
        assert value in text, f"CODE-NAVIGATION.md missing {value!r}"


def test_user_guide_describes_search_signals_conditionally() -> None:
    text = (ROOT / "docs" / "USER-GUIDE.md").read_text(encoding="utf-8")
    normalized = " ".join(text.split())

    assert "Plain `search_memory.py` always runs BM25" in normalized
    assert "`--semantic` enables vectors when the optional model is available" in normalized
    assert "graph-neighbor fusion applies only when graph evidence is available" in normalized
    assert "`search_memory.py` runs hybrid BM25 + Vector + Graph fusion." not in normalized


def test_all_readmes_share_python_navigation_operator_contract():
    translated_trust_markers = {
        "README.md": ("trusted local repositories", "not an OS sandbox"),
        "README.ru.md": ("доверенных локальных репозиториях", "не является OS sandbox"),
        "README.zh-CN.md": ("受信任的本地仓库", "不是 OS sandbox"),
    }
    shared = (
        "Pyright 1.1.411",
        'uv run python scripts/install_pyright.py --state-root "$LLM_WIKI_STATE_ROOT"',
        "docs/CODE-NAVIGATION.md",
        "`definition`",
        "`references`",
        "`diagnostics`",
    )
    for path, text in _readmes():
        for marker in (*shared, *translated_trust_markers[path.name]):
            assert marker in text, f"{path.name}: missing navigation marker {marker!r}"


def test_navigation_is_current_in_operator_and_architecture_docs():
    required = {
        "USER-GUIDE.md": (
            "## Read-only Python code navigation",
            "Pyright 1.1.411",
            "trusted local repositories",
            "not an OS sandbox",
            "mode=definition",
        ),
        "ARCHITECTURE.md": (
            "## Read-only Python navigation",
            "query-time LSP observations are not written",
            "no semantic result cache",
        ),
        "operating-model.md": (
            "## Read-only code-navigation boundary",
            "explicit operator installation",
            "Market superiority remains unclaimed",
        ),
        "CONTRIBUTING.md": (
            "real-Pyright CI",
            "scripts/install_pyright.py",
            "benchmark/run_code_navigation.py",
        ),
    }
    for name, markers in required.items():
        parent = ROOT if name == "CONTRIBUTING.md" else ROOT / "docs"
        text = (parent / name).read_text(encoding="utf-8")
        for marker in markers:
            assert marker in text, f"{name}: missing navigation marker {marker!r}"
