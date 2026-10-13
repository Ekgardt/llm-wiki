"""A slow first checkout leaves time for the remaining registered work."""
import repository_index
import repository_worktrees


def _selected_rows(monkeypatch, rows):
    monkeypatch.setattr(repository_index, "_adopted_vault", lambda *args: {"status": "current"})
    monkeypatch.setattr(repository_index, "_registered_rows", lambda *args: rows)
    monkeypatch.setattr(repository_worktrees, "follow_worktrees", lambda *args, **kwargs: {})


def test_a_slow_first_checkout_cannot_spend_the_second_checkouts_time(monkeypatch):
    now = [0.0]
    bounds = []

    def refresh(row, state, deadline):
        bounds.append(deadline)
        now[0] = deadline
        return {"checkout": row}

    monkeypatch.setattr(repository_index.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(repository_index, "_refresh_row", refresh)
    _selected_rows(monkeypatch, ["busy", "quiet"])
    result = repository_index.refresh_all_repositories(budget_seconds=120.0)
    assert bounds == [60.0, 120.0]
    assert result["repositories"] == [{"checkout": "busy"}, {"checkout": "quiet"}]


def test_time_saved_by_a_fast_checkout_remains_available(monkeypatch):
    now = [0.0]
    bounds = []

    def refresh(row, state, deadline):
        bounds.append(deadline)
        now[0] += 1.0
        return {"checkout": row}

    monkeypatch.setattr(repository_index.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(repository_index, "_refresh_row", refresh)
    _selected_rows(monkeypatch, ["first", "second", "third"])
    repository_index.refresh_all_repositories(budget_seconds=120.0)
    assert bounds == [40.0, 60.5, 120.0]


def test_an_expired_shared_deadline_is_never_extended(monkeypatch):
    monkeypatch.setattr(repository_index.time, "monotonic", lambda: 121.0)
    assert repository_index._fair_refresh_deadline(120.0, 2) == 120.0
