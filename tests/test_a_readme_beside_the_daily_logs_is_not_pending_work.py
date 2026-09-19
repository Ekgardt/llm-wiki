"""`knowledge/daily/README.md` is not a daily log for any reader of that directory.

Research: `docs/research/2026-09-17-a-daily-log-is-named-by-its-date-everywhere.md`.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))


@pytest.fixture
def shipped_vault(tmp_path, monkeypatch):
    """A vault as the repository ships it: a README beside no daily log, empty state."""
    root = tmp_path / "vault"
    state_root = tmp_path / "state"
    daily = root / "knowledge" / "daily"
    daily.mkdir(parents=True)
    (state_root / "run").mkdir(parents=True)
    (daily / "README.md").write_text("# Daily logs\n\n[[some-page]] and [[other-page]]\n", encoding="utf-8")
    monkeypatch.setenv("LLM_WIKI_ROOT", str(root))
    monkeypatch.setenv("LLM_WIKI_STATE_ROOT", str(state_root))
    for name in ("maybe_compile", "memory_state", "mcp_server", "co_activation"):
        monkeypatch.delitem(sys.modules, name, raising=False)
    return root


def test_a_vault_with_only_the_readme_has_no_pending_work(shipped_vault):
    import maybe_compile

    assert maybe_compile._has_pending_work() is False


def test_a_daily_log_beside_the_readme_is_still_pending_work(shipped_vault):
    import maybe_compile

    (shipped_vault / "knowledge" / "daily" / "2026-09-01.md").write_text("## [10:00:00] s\n", encoding="utf-8")

    assert maybe_compile._has_pending_work() is True


def test_the_vault_status_backlog_does_not_count_the_readme(shipped_vault):
    import mcp_server

    assert mcp_server._vault_status()["compile_backlog"] == 0


def test_the_fact_keys_pass_is_handed_daily_logs_only(shipped_vault):
    import fact_keys

    (shipped_vault / "knowledge" / "daily" / "2026-09-01.md").write_text("## [10:00:00] s\n", encoding="utf-8")

    assert fact_keys._daily_paths(shipped_vault) == ["knowledge/daily/2026-09-01.md"]


def test_the_readme_links_never_enter_the_co_activation_table(shipped_vault):
    import co_activation

    assert co_activation.build(shipped_vault) == {}


def test_daily_logs_of_an_absent_directory_is_empty(tmp_path):
    import memory_state

    assert memory_state.daily_logs(tmp_path / "nowhere") == []
