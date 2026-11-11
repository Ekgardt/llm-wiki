"""The nightly and the sync refresh the one shared generation builder."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from tests.generation_maintenance_helpers import _vault


def _ok_checks(names) -> list[dict]:
    return [
        {"id": name, "status": "ok", "message": "ok", "details": {}} for name in names
    ]


_SYNC_CHECK_NAMES = (
    "environment",
    "runtime",
    "filesystem",
    "integrations",
    "transactions",
    "queue",
)


def test_nightly_uses_shared_generation_builder_not_process_local_graphs(monkeypatch):
    import scheduled_nightly

    calls = []
    monkeypatch.setattr(
        scheduled_nightly,
        "run_generation_maintenance",
        lambda **kwargs: (
            calls.append(kwargs) or {"status": "built", "generation_id": "gen-1", "partial": False}
        ),
    )

    result = scheduled_nightly.refresh_generation(lambda message: calls.append(message))

    assert result == 0
    assert any(isinstance(item, dict) and item["max_sources"] > 0 for item in calls)
    source = Path(scheduled_nightly.__file__).read_text(encoding="utf-8")
    assert "rebuild_graph_cache" not in source
    assert "index_directory(ROOT" not in source


def test_edited_wikilink_is_visible_after_nightly_in_fresh_processes(
    tmp_path, monkeypatch
):
    import scheduled_nightly

    root, state = _vault(tmp_path)
    notes = root / "knowledge" / "notes"
    source = notes / "source.md"
    source.write_text(
        "---\ntype: concept\n---\n# Source\n[[target-a]]\n",
        encoding="utf-8",
    )
    for target in ("a", "b"):
        (notes / f"target-{target}.md").write_text(
            f"---\ntype: concept\n---\n# Target {target.upper()}\n",
            encoding="utf-8",
        )

    monkeypatch.setenv("LLM_WIKI_ROOT", str(root))
    monkeypatch.setenv("LLM_WIKI_STATE_ROOT", str(state))
    logs = []

    def fresh_neighbors() -> list[str]:
        environment = os.environ.copy()
        environment.update(
            {
                "LLM_WIKI_ROOT": str(root),
                "LLM_WIKI_STATE_ROOT": str(state),
                "PYTHONPATH": str(Path(__file__).resolve().parent.parent / "scripts"),
            }
        )
        completed = subprocess.run(
            [
                sys.executable,
                "-c",
                (
                    "import json, graph_neighbors; "
                    "graph_neighbors.build_link_graph = lambda *args, **kwargs: "
                    "(_ for _ in ()).throw(AssertionError('live source fallback')); "
                    "print(json.dumps(graph_neighbors.get_neighbors("
                    "'knowledge/notes/source.md')))"
                ),
            ],
            cwd=root,
            env=environment,
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
        )
        return json.loads(completed.stdout)

    assert scheduled_nightly.refresh_generation(logs.append) == 0
    assert fresh_neighbors() == ["knowledge/notes/target-a.md"]

    source.write_text(
        "---\ntype: concept\n---\n# Source\n[[target-b]]\n",
        encoding="utf-8",
    )

    assert scheduled_nightly.refresh_generation(logs.append) == 0
    assert fresh_neighbors() == ["knowledge/notes/target-b.md"]


def _recording_kwargs(calls, value):
    """A stub that remembers the call it was made with."""

    def stub(**kwargs):
        calls.append(kwargs)
        return value

    return stub


def _action_of(result: dict, action_id: str) -> dict:
    return next(item for item in result["actions"] if item["id"] == action_id)


def _stub_sync_report(monkeypatch, sync_memory, report, dependency) -> None:
    monkeypatch.setattr(sync_memory.doctor, "run_doctor", lambda **kwargs: report)
    monkeypatch.setattr(sync_memory, "dependency_action", lambda **kwargs: dependency)


def test_sync_check_reports_stale_generation_and_apply_uses_shared_builder(tmp_path, monkeypatch):
    import sync_memory

    calls = []
    generation = {
        "id": "generation",
        "status": "degraded",
        "message": "Generation is stale.",
        "details": {"freshness": "stale", "repairable": True},
    }
    index = {
        "id": "index",
        "status": "ok",
        "message": "Index is fresh.",
        "details": {"freshness": "fresh", "repairable": False},
    }
    report = {
        "overall_status": "degraded",
        "repaired": [],
        "checks": _ok_checks(_SYNC_CHECK_NAMES) + [generation, index],
    }
    dependency = {"id": "dependencies", "status": "ok", "message": "ok", "details": {}}
    refreshed = {
        "id": "indexes",
        "status": "changed",
        "message": "Generation refreshed.",
        "details": {"generation": "gen-2", "partial": False},
    }
    _stub_sync_report(monkeypatch, sync_memory, report, dependency)
    monkeypatch.setattr(
        sync_memory, "run_generation_builder", _recording_kwargs(calls, refreshed)
    )

    checked = sync_memory.run_sync(root=tmp_path, state_root=tmp_path, home=tmp_path, apply=False)
    applied = sync_memory.run_sync(root=tmp_path, state_root=tmp_path, home=tmp_path, apply=True)

    checked_index = _action_of(checked, "indexes")
    applied_index = _action_of(applied, "indexes")
    assert checked_index["status"] == "skipped"
    assert checked_index["details"]["checks"]["generation"] == "degraded"
    assert applied_index["status"] == "changed"
    assert calls, "apply must reach the shared generation builder"
    assert calls[0]["max_sources"] > 0
