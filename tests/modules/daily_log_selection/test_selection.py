"""Select actual directory entries without runtime state or clock dependence."""
import pytest
from llm_wiki.daily_log_selection import DAILY_LOG_NAME, closed_daily_logs, daily_logs


def test_missing_directory_and_regular_file_have_no_daily_entries(tmp_path):
    ordinary = tmp_path / 'file'
    ordinary.write_text('content', encoding='utf-8')
    assert daily_logs(tmp_path / 'missing') == []
    assert daily_logs(ordinary) == []
    assert closed_daily_logs(tmp_path) == []


def test_selection_sorts_immediate_dates_and_ignores_readme_and_nested_days(tmp_path):
    names = ['2026-11-03.md', 'README.md', '2026-11-01.md', '2026-1-01.md']
    for name in names:
        (tmp_path / name).write_bytes(b'\xff')
    nested = tmp_path / 'nested'
    nested.mkdir()
    (nested / '2026-10-01.md').write_text('nested', encoding='utf-8')
    assert daily_logs(tmp_path) == [tmp_path / '2026-11-01.md', tmp_path / '2026-11-03.md']
    assert closed_daily_logs(tmp_path) == [tmp_path / '2026-11-01.md']


def test_one_day_is_open_even_when_its_date_is_old(tmp_path):
    entry = tmp_path / '2000-01-01.md'
    entry.touch()
    assert daily_logs(tmp_path) == [entry]
    assert closed_daily_logs(tmp_path) == []


@pytest.mark.parametrize('name', ['2026-99-99.md', '２０２６-１１-０３.md'])
def test_existing_pattern_is_shape_selection_not_calendar_validation(tmp_path, name):
    entry = tmp_path / name
    entry.touch()
    assert DAILY_LOG_NAME.fullmatch(name) is not None
    assert daily_logs(tmp_path) == [entry]


def test_existing_glob_semantics_include_a_date_shaped_directory(tmp_path):
    entry = tmp_path / '2026-11-03.md'
    entry.mkdir()
    assert daily_logs(tmp_path) == [entry]
