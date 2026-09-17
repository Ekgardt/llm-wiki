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


def _missing(text: str, markers) -> list:
    """The markers this text should carry and does not."""
    return [marker for marker in markers if marker not in text]


def _present(text: str, markers) -> list:
    """The markers this text carries and should not."""
    return [marker for marker in markers if marker in text]


# A mutable bootstrap target, a static badge or a bare CI claim is unverified;
# the approved target is a full 40-character commit OID.
_FORBIDDEN_BOOTSTRAP = (
    "raw.githubusercontent.com/Ekgardt/llm-wiki/main/install.",
    "/v4.0.0/install.",
    "brightgreen.svg",
    "CI green",
)
_REQUIRED_BOOTSTRAP = ("LLM_WIKI_COMMIT", "40")

# Claims every README must carry (compared case-folded), claims none may carry,
# and the per-language parity markers.
_REQUIRED_CLAIMS = (
    "mcp",
    "12",
    "doctor",
    "envelope",
    "resource",
    "integration_adapter.py",
    "degraded",
    "obsidian",
)
_STALE_CLAIMS = ("web clipper", "zero runtime dependencies", "stdlib-only")
_PARITY_MARKERS = {
    "README.md": (
        "12 task-shaped",
        "full regression suite",
        "retrieval-v2.json",
        "optional Obsidian viewer",
    ),
    "README.ru.md": (
        "12 task-shaped",
        "полный регрессионный набор",
        "retrieval-v2.json",
        "Obsidian как опциональный viewer",
    ),
    "README.zh-CN.md": (
        "12 个 task-shaped",
        "完整回归套件",
        "retrieval-v2.json",
        "Obsidian 为可选 viewer",
    ),
}


def _matrix_rows(entries) -> list:
    """(os, platform, python, node) for each qualification matrix entry."""
    return [
        (entry["os"], entry["platform"], entry["python"], entry["node"])
        for entry in entries
    ]


def _matrix_budgets(entries) -> list:
    return [entry["timeout"] for entry in entries]


def _named_step(job: dict, name: str) -> dict:
    return next(step for step in job["steps"] if step.get("name") == name)


def _declared_version() -> str:
    """The version `pyproject.toml` declares, read live."""
    pyproject = ROOT / "pyproject.toml"
    match = re.search(
        r'^version\s*=\s*"([^"]+)"', pyproject.read_text(encoding="utf-8"), re.MULTILINE
    )
    assert match, "could not parse version from pyproject.toml"
    return match.group(1)


def _version_offences(name: str, text: str, current: str) -> tuple:
    """(version missing, claims missing, stale claims present, stale QMD claim)."""
    folded = text.casefold()
    return (
        _missing(text, (current, *_PARITY_MARKERS[name])),
        _missing(folded, _REQUIRED_CLAIMS),
        _present(folded, _STALE_CLAIMS),
        bool(re.search(r"\bqmd\b", text, re.IGNORECASE)),
    )


def test_readmes_advertise_only_immutable_remote_bootstrap():
    offences = {
        path.name: (
            _present(text, _FORBIDDEN_BOOTSTRAP),
            _missing(text, _REQUIRED_BOOTSTRAP),
        )
        for path, text in _readmes()
    }

    assert offences == {name: ([], []) for name in offences}


def test_readmes_share_locked_install_and_read_only_repair_commands():
    for path, text in _readmes():
        for command in SHARED_COMMANDS:
            assert command in text, f"{path.name}: missing shared command {command!r}"
        assert "--adopt-ownership-v3" in text, (
            f"{path.name}: must name the v3 adoption command the installer runs (issue #17)"
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


def test_readmes_describe_the_retired_hosts_and_viewer_integration() -> None:
    markers = {
        "README.md": (
            "Cursor and Antigravity were retired on 2026-08-26",
            "`uninstall` still takes back hooks an earlier install wrote",
            "Viewer only",
        ),
        "README.ru.md": (
            "Cursor и Antigravity сняты с поддержки 2026-08-26",
            "`uninstall` по-прежнему забирает хуки",
            "Только viewer",
        ),
        "README.zh-CN.md": (
            "Cursor 与 Antigravity 已于 2026-08-26 退出支持",
            "`uninstall` 仍会收回旧版安装写入的钩子",
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

    required = ("OpenCode: active automatic", "Agent integrations:")
    unnamed_agents = ("cursor", "antigravity")

    assert (
        _missing(shell, required),
        _missing(powershell, required),
        _present(shell.casefold(), unnamed_agents),
        _present(powershell.casefold(), unnamed_agents),
        _present(shell, ("captures automatically",)),
        _present(powershell, ("capture is automatic",)),
        _present(guide, ("even while you sleep",)),
        _missing(
            guide,
            ("Windows tasks run only while the current user is logged on",),
        ),
    ) == ([], [], [], [], [], [], [], [])


def test_windows_scheduler_status_validates_registered_contract() -> None:
    source = (ROOT / "scripts/install-scheduled-tasks.ps1").read_text(encoding="utf-8")

    assert _missing(
        source,
        (
            "function Test-LLMWikiScheduledTasks",
            ".Principal.LogonType",
            ".Actions.Count",
            ".Triggers.Count",
            "if ($verified)",
            "exit 1",
        ),
    ) == []


def test_all_readmes_mention_knowledge_layout():
    for p in README_FILES:
        text = p.read_text(encoding="utf-8")
        assert "knowledge/" in text, f"{p.name}: must document knowledge/ layout"


def test_all_readmes_mention_current_version():
    """Every README must mention the version declared in pyproject.toml.
    The version is read live so bumping pyproject + READMEs in the same
    change keeps this test green without editing the test itself.
    """
    current = _declared_version()
    offences = {
        path.name: _version_offences(path.name, text, current) for path, text in _readmes()
    }

    assert offences == {name: ([], [], [], False) for name in offences}


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
    claims = (
        "mcp-server",
        "compatibility alias",
        "production smoke",
        "Node 22",
    )
    offences = {
        path.name: _missing(text, (*commands, *claims)) for path, text in _readmes()
    }

    assert offences == {name: [] for name in offences}


def test_ci_qualifies_real_pyright_on_all_supported_os_families():
    import yaml

    workflow = yaml.safe_load(
        (ROOT / ".github" / "workflows" / "tests.yml").read_text(encoding="utf-8")
    )
    job = workflow["jobs"]["pyright-navigation"]
    entries = job["strategy"]["matrix"]["include"]
    assert _matrix_rows(entries) == [
        ("ubuntu-24.04", "linux", "3.10", "22.23.1"),
        ("windows-2025", "windows", "3.10", "22.23.1"),
        ("macos-15", "macos", "3.10", "22.23.1"),
    ]
    # The budget is per platform, because the same suite takes about three
    # times longer on the hosted Windows image. Every family declares one.
    budgets = _matrix_budgets(entries)
    install_step = _named_step(job, "Explicit Pyright install")
    assert (
        min(budgets) > 0,
        job["timeout-minutes"],
        job["env"]["LLM_WIKI_STATE_ROOT"],
        job["env"]["LLM_WIKI_TEST_USE_EXTERNAL_STATE"],
        '"${{ env.LLM_WIKI_STATE_ROOT }}"' in install_step["run"],
        "shell" in install_step,
    ) == (
        True,
        "${{ matrix.timeout }}",
        "${{ github.workspace }}/../llm-wiki-state",
        "1",
        True,
        False,
    )


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

    required = (
        "reads the active evidence generation first",
        "Vectors are on by default when the optional model is available; "
        "`--no-semantic` turns them off",
        "Graph-neighbor fusion applies only when graph evidence is available",
    )
    stale = (
        "`--semantic` enables vectors",
        "`search_memory.py` runs hybrid BM25 + Vector + Graph fusion.",
    )

    assert (_missing(normalized, required), _present(normalized, stale)) == ([], [])


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
            "rust-analyzer 1.98.1",
            "trusted local repositories",
            "not an OS sandbox",
            "mode=definition",
        ),
        "ARCHITECTURE.md": (
            "## Read-only precise navigation",
            "rust-analyzer 1.98.1",
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
