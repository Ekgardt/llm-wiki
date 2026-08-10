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


def test_all_readmes_use_dynamic_ci_badge_and_full_suite_wording():
    """Every README links status to CI and avoids a brittle test count."""
    for p, text in _readmes():
        assert "actions/workflows/tests.yml/badge.svg" in text, (
            f"{p.name}: test badge must report the live workflow status"
        )
        assert "uv sync --locked --extra mcp-server" in text, (
            f"{p.name}: manual install must include the MCP baseline"
        )


def test_all_readmes_use_correct_github_repo():
    for p in README_FILES:
        text = p.read_text(encoding="utf-8")
        assert "Ekgardt/llm-wiki" in text, f"{p.name}: missing Ekgardt/llm-wiki"
        assert "llm-knowledge/notes" not in text, f"{p.name}: stale llm-knowledge URL"


def test_readmes_do_not_advertise_unimplemented_remote_bootstrap():
    for path, text in _readmes():
        assert "raw.githubusercontent.com/Ekgardt/llm-wiki/main/install." not in text, (
            f"{path.name}: mutable main bootstrap must not be advertised"
        )
        assert "/v4.0.0/install." not in text, (
            f"{path.name}: unpublished release bootstrap must not be advertised"
        )
        assert "brightgreen.svg" not in text, f"{path.name}: static green badge is unverified"
        assert "CI green" not in text, f"{path.name}: CI status claim is unverified"
        assert "LLM_WIKI_COMMIT" in text, (
            f"{path.name}: approved full-OID bootstrap target must be explicit"
        )
        assert "40" in text, f"{path.name}: full commit OID length must be explicit"


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
    m = _re.search(r'^version\s*=\s*"([^"]+)"', pyproject.read_text(encoding="utf-8"), _re.MULTILINE)
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
        "uv run python scripts/memory_queue.py purge --terminal-before <ISO-8601> "
        "--export <path>",
        "uv run python scripts/archive_daily.py --commit --hot-days 90",
        "uv run python benchmark/run_contradiction_benchmark.py --corpus "
        "benchmark/contradiction-v1.json",
    )
    for path, text in _readmes():
        for command in commands:
            assert command in text, f"{path.name}: missing operator command {command!r}"


def test_ci_qualifies_real_pyright_on_all_supported_os_families():
    import yaml

    workflow = yaml.safe_load(
        (ROOT / ".github" / "workflows" / "tests.yml").read_text(encoding="utf-8")
    )
    job = workflow["jobs"]["pyright-navigation"]
    assert job["strategy"]["matrix"]["os"] == [
        "ubuntu-latest",
        "windows-latest",
        "macos-latest",
    ]
    assert job["strategy"]["matrix"]["python"] == ["3.10"]
    assert job["strategy"]["matrix"]["node"] == ["22.23.1"]


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
