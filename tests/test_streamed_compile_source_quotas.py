"""Streaming has no implicit total-volume cutoff; explicit quotas still fail closed."""
from pathlib import Path

import compile_memory as compile
import doctor
import pytest
import settings

from tests.test_compile_ceiling_counts_only_compile_inputs import _bind_compiler


def _bind(root, monkeypatch):
    _bind_compiler(monkeypatch, root)
    monkeypatch.delenv("LLM_WIKI_COMPILE_MAX_SOURCES", raising=False)
    monkeypatch.delenv("LLM_WIKI_COMPILE_MAX_TOTAL_SOURCE_BYTES", raising=False)


@pytest.mark.parametrize("count,body_bytes", [(2001, 32), (33, 1024 * 1024)])
def test_streamed_capture_passes_the_old_aggregate_cutoffs(tmp_path, monkeypatch, count, body_bytes):
    _bind(tmp_path, monkeypatch)
    notes = tmp_path / "knowledge/notes"
    notes.mkdir(parents=True)
    body = b"x" * body_bytes
    for index in range(count):
        (notes / f"{index}.md").write_bytes(body)
    captured = 0
    for group, record in compile.iter_compile_records([]):
        assert record.sha256 == compile.sha256_bytes(body)
        captured += int(group == "sources")
    assert captured == count


@pytest.mark.parametrize("key,value", [("max_sources", 1), ("max_total_source_bytes", 1)])
def test_explicit_quota_is_still_enforced_at_the_compilation_root(
    tmp_path, monkeypatch, key, value,
):
    _bind(tmp_path, monkeypatch)
    (tmp_path / "llm-wiki.toml").write_text(f"[compile]\n{key} = {value}\n")
    budget = compile._SourceBudget()
    source = compile.SourceSnapshot("knowledge/notes/a.md", b"x", compile.sha256_bytes(b"x"))
    budget.add(source)
    with pytest.raises(ValueError, match=f"compile.{key}"):
        budget.add(source)


def test_zero_byte_quota_does_not_turn_daily_reads_into_zero_byte_reads(tmp_path, monkeypatch):
    _bind(tmp_path, monkeypatch)
    (tmp_path / "llm-wiki.toml").write_text("[compile]\nmax_total_source_bytes = 0\n")
    daily = tmp_path / "knowledge/daily/2026-10-20.md"
    daily.parent.mkdir(parents=True)
    content = b"## [10:00:00] capture\nDurable source.\n"
    daily.write_bytes(content)
    sources = [(record.logical_path, bytes(record.content))
               for group, record in compile.iter_compile_records([daily]) if group == "sources"]
    assert sources == [("knowledge/daily/2026-10-20.md", content)]
    assert compile._daily_read_budget() == compile.MAX_DAILY_BYTES


def test_disabled_quotas_do_not_create_health_warnings_or_divide_by_zero(tmp_path, monkeypatch):
    _bind(tmp_path, monkeypatch)
    (tmp_path / "llm-wiki.toml").write_text("[compile]\nmax_sources = 0\nmax_total_source_bytes = 0\n")
    details = doctor._settings_check(tmp_path)["details"]
    assert "compile.max_sources" not in details["near_ceiling"]
    assert "compile.max_total_source_bytes" not in details["near_ceiling"]
    assert details["overridden"]["compile.max_sources"] == {"value": 0, "source": "llm-wiki.toml"}


def test_environment_keeps_precedence_and_can_disable_a_file_quota(tmp_path):
    (tmp_path / "llm-wiki.toml").write_text("[compile]\nmax_sources = 1\n")
    values = settings.effective(tmp_path, environ={"LLM_WIKI_COMPILE_MAX_SOURCES": "0"})
    assert values["compile.max_sources"].value == 0
    assert values["compile.max_sources"].source == "LLM_WIKI_COMPILE_MAX_SOURCES"


@pytest.mark.parametrize("key", ["max_sources", "max_total_source_bytes"])
def test_negative_quota_remains_invalid(tmp_path: Path, key):
    (tmp_path / "llm-wiki.toml").write_text(f"[compile]\n{key} = -1\n")
    with pytest.raises(settings.SettingsError, match=key):
        settings.effective(tmp_path, environ={})
