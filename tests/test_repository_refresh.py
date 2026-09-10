"""The background refresh of a foreign repository (#24, section A).

A refresh looks first and rebuilds only when sources changed, under the
ownership registry's `doctor` role scoped to the one repository; the MCP
server starts it detached once per (repository, commit) and answers now from
the generation it has, saying which commit that generation was built from.
The full build is exercised once here, because it loads the embedding model.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from tests.test_code_graph import _activate_graph  # noqa: E402
from tests.test_repository_index import ALPHA, _git, _repository  # noqa: E402


@pytest.fixture(autouse=True)
def _isolated_reader_cache():
    import evidence_reader_cache

    evidence_reader_cache.clear()
    yield
    evidence_reader_cache.clear()


@pytest.fixture
def adopted_vault(tmp_path, monkeypatch):
    """A vault whose coordinator went through real V3 adoption, so a fence exists."""
    from installed_memory_repair import repair_installed_vault

    root = tmp_path / "vault"
    state = tmp_path / "state"
    (root / "scripts").mkdir(parents=True)
    (root / "scripts/integration_adapter.py").write_bytes(
        (SCRIPTS / "integration_adapter.py").read_bytes()
    )
    (root / "knowledge/notes").mkdir(parents=True)
    (root / "knowledge/projects").mkdir(parents=True)
    report = repair_installed_vault(
        root=root, state_root=state, adopt_ownership_v3=True, confirm_all_agents_stopped=True
    )
    assert report["overall_status"] == "ok"
    monkeypatch.setenv("LLM_WIKI_STATE_ROOT", str(state))
    monkeypatch.setenv("LLM_WIKI_ROOT", str(root))
    import memory_state

    monkeypatch.setattr(memory_state, "ROOT", root, raising=False)
    monkeypatch.setattr(memory_state, "STATE_ROOT", state, raising=False)
    return root, state


def test_refresh_of_an_unindexed_repository_says_not_indexed(adopted_vault, tmp_path):
    import repository_index

    _root, state = adopted_vault
    repository = _repository(tmp_path / "repo", {"scripts/alpha.py": ALPHA})

    answer = repository_index.refresh_repository(repository, state_root=state)

    assert (answer["status"], answer["stale"]) == ("not_indexed", True)


def test_refresh_rebuilds_only_after_an_edit_and_under_the_repository_fence(
    adopted_vault, tmp_path
):
    import repository_index

    _root, state = adopted_vault
    repository = _repository(tmp_path / "repo", {"scripts/alpha.py": ALPHA})
    first = repository_index.index_repository(repository, state_root=state)

    untouched = repository_index.refresh_repository(repository, state_root=state)
    (repository / "scripts/beta.py").write_text("def beta():\n    return 2\n", encoding="utf-8")
    rebuilt = repository_index.refresh_repository(repository, state_root=state)

    assert (untouched["status"], untouched["generation_id"]) == ("fresh", first["generation_id"])
    assert (rebuilt["status"], rebuilt["previous_generation_id"], rebuilt["rebuilt_sources"] >= 1) == (
        "refreshed",
        first["generation_id"],
        True,
    )
    _assert_second_holder_is_refused(repository_index, repository, state, rebuilt)
    _assert_a_missing_checkout_is_named_not_deleted(repository_index, repository, state)


def _assert_second_holder_is_refused(repository_index, repository, state, rebuilt) -> None:
    _coordinator, registry = repository_index._refresh_fence(state)
    lease = registry.acquire(
        repository_index.REFRESH_ROLE,
        scope=repository_index.refresh_scope(rebuilt["repository_id"]),
    )
    (repository / "scripts/gamma.py").write_text("def gamma():\n    return 3\n", encoding="utf-8")
    try:
        answer = repository_index.refresh_repository(repository, state_root=state)
    finally:
        registry.release(lease)
    assert (answer["status"], answer["stale"], answer["generation_id"]) == (
        "refresh_owned_elsewhere",
        True,
        rebuilt["generation_id"],
    )


def _assert_a_missing_checkout_is_named_not_deleted(repository_index, repository, state) -> None:
    import shutil

    shutil.rmtree(repository)
    answer = repository_index.refresh_all_repositories(state_root=state, budget_seconds=60)
    statuses = {row["status"] for row in answer["repositories"]}
    listed = repository_index.list_repositories(state_root=state)["repositories"]
    assert (statuses, len(listed)) == ({"checkout_missing"}, 1)


def test_refresh_without_an_adopted_coordinator_is_reported_not_run(tmp_path, monkeypatch):
    """An unadopted vault has no scoped fence; the refresh says so and does not build."""
    import repository_index

    monkeypatch.setattr(repository_index, "_refresh_fence", lambda _root: (object(), None))
    monkeypatch.setattr(
        repository_index,
        "_staleness",
        lambda *_args: {"stale": True, "reason": "sources_changed"},
    )
    repository = _repository(tmp_path / "repo", {"scripts/alpha.py": ALPHA})
    admission = repository_index.admit_repository(repository, state_root=tmp_path / "state")

    answer = repository_index._fenced_rebuild(
        admission, None, "generation-x", {}, tmp_path / "state", {"stale": True, "reason": "sources_changed"}, None, None
    )

    assert (answer["status"], answer["reason"]) == ("refresh_unavailable", "coordinator_v3_required")


def test_the_cli_detect_verb_answers_json_and_exit_two_when_not_indexed(tmp_path):
    repository = _repository(tmp_path / "repo", {"scripts/alpha.py": ALPHA})
    state = tmp_path / "state"
    state.mkdir()
    environment = dict(os.environ)
    environment["LLM_WIKI_STATE_ROOT"] = str(state)
    environment["LLM_WIKI_ROOT"] = str(tmp_path / "vault")

    completed = subprocess.run(
        [sys.executable, str(SCRIPTS / "repository_index.py"), "detect", str(repository)],
        capture_output=True,
        text=True,
        timeout=120,
        env=environment,
    )

    answer = json.loads(completed.stdout)
    assert (completed.returncode, answer["status"]) == (2, "not_indexed")


def test_a_structural_answer_names_its_commit_and_starts_one_refresh_per_commit(
    tmp_path, monkeypatch
):
    import code_graph
    import mcp_server
    import memory_state

    repository = _repository(tmp_path / "repository", {"app.py": "def caller():\n    callee()\n"})
    catalog = _activate_graph(tmp_path, repository)
    monkeypatch.setattr(code_graph, "_generation_catalog", lambda _directory: catalog)
    monkeypatch.setattr(mcp_server, "_REFRESH_REQUESTED", {})
    spawned: list[list[str]] = []
    monkeypatch.setattr(
        memory_state, "spawn_detached", lambda args, **_options: spawned.append(args) or 4242
    )

    def freshness():
        answer = mcp_server._get_architecture_mode(
            str(repository), mode="callers", symbol="callee", deadline=time.monotonic() + 30
        )
        return answer["freshness"]

    before = freshness()
    _git(repository, "commit", "-q", "--allow-empty", "-m", "moved")
    first_after = freshness()
    second_after = freshness()

    assert (before["stale_by_commit"], before["refresh"]) == (False, "not_needed")
    assert (first_after["stale_by_commit"], first_after["refresh"], second_after["refresh"]) == (
        True,
        "started",
        "already_requested",
    )
    assert (len(spawned), spawned[0][1].endswith("repository_index.py"), spawned[0][2:]) == (
        1,
        True,
        ["refresh", str(repository)],
    )


def test_the_nightly_pass_refreshes_repositories_before_it_prunes():
    import scheduled_nightly

    labels = [step.label for step in scheduled_nightly._post_compile_steps()]
    step = next(step for step in scheduled_nightly._post_compile_steps() if step.label == "repositories")

    assert (labels.index("repositories") < labels.index("prune_generations"), step.command[-1]) == (
        True,
        "refresh-all",
    )
