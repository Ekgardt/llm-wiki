"""Retention of foreign repository generations (#24, D1).

Per checkout, by identity: a gone or marked checkout loses every generation
and its hint table; a live one keeps its newest two. The vault's own
generations are never considered, and every discard runs under the
per-repository fence.
"""

from __future__ import annotations

import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from tests import test_repository_refresh as _refresh_tests  # noqa: E402
from tests.test_repository_index import ALPHA, _git, _repository  # noqa: E402

adopted_vault = _refresh_tests.adopted_vault
_isolated_reader_cache = _refresh_tests._isolated_reader_cache


def _generation_ids(state: Path) -> set[str]:
    import repository_index

    catalog = repository_index._open_catalog(state, read_only=True)
    return {identifier for identifier, _at, _manifest in catalog.registered_manifests()}


def _edited(repository: Path, name: str) -> None:
    (repository / "pkg" / f"{name}.py").write_text(f"def {name}():\n    return 1\n", encoding="utf-8")


def _three_generations(state: Path, repository: Path) -> list[str]:
    import repository_index

    first = repository_index.index_repository(repository, state_root=state)["generation_id"]
    _edited(repository, "beta")
    second = repository_index.refresh_repository(repository, state_root=state)["generation_id"]
    _edited(repository, "gamma")
    third = repository_index.refresh_repository(repository, state_root=state)["generation_id"]
    return [first, second, third]


def test_a_live_checkout_keeps_its_newest_two_and_a_dry_run_removes_nothing(adopted_vault, tmp_path):
    import repository_retention

    _root, state = adopted_vault
    repository = _repository(tmp_path / "repo", {"pkg/alpha.py": ALPHA})
    first, second, third = _three_generations(state, repository)

    planned = repository_retention.retire_repositories(state_root=state, apply=False)
    after_plan = _generation_ids(state)
    applied = repository_retention.retire_repositories(state_root=state)

    assert [(entry["verdict"], entry["kept"], entry["retire"]) for entry in planned["checkouts"]] == [
        ("kept", [third, second], [first])
    ]
    assert (after_plan, _generation_ids(state)) == ({first, second, third}, {second, third})
    assert applied["checkouts"][0]["retired"] == [{"generation_id": first, "status": "retired"}]


def test_a_removed_worktree_loses_its_generations_and_its_hint_table(adopted_vault, tmp_path):
    import code_hints
    import repository_index
    import repository_retention
    from repository_scope import resolve_repository_scope

    _root, state = adopted_vault
    repository = _repository(tmp_path / "repo", {"pkg/alpha.py": ALPHA})
    kept = repository_index.index_repository(repository, state_root=state)["generation_id"]
    topic = repository.parent / "repo-topic"
    _git(repository, "worktree", "add", "-q", "-b", "topic", str(topic))
    gone = repository_index.index_repository(topic, state_root=state)["generation_id"]
    checkout_id = resolve_repository_scope(topic).checkout_id
    _git(repository, "worktree", "remove", "--force", str(topic))

    answer = repository_retention.retire_repositories(state_root=state)

    verdicts = {entry["checkout_root"]: entry["verdict"] for entry in answer["checkouts"]}
    assert (verdicts, _generation_ids(state)) == ({str(topic): "checkout_missing"}, {kept})
    assert (code_hints.hints_path(state, checkout_id).exists(), gone in _generation_ids(state)) == (False, False)


def test_a_checkout_marked_not_indexed_is_retired_whole(adopted_vault, tmp_path):
    import repository_index
    import repository_retention

    _root, state = adopted_vault
    repository = _repository(tmp_path / "repo", {"pkg/alpha.py": ALPHA})
    repository_index.index_repository(repository, state_root=state)
    _git(repository, "config", "llmwiki.index", "false")

    answer = repository_retention.retire_repositories(state_root=state)
    assert ([entry["verdict"] for entry in answer["checkouts"]], _generation_ids(state)) == (
        ["marked_not_indexed"],
        set(),
    )
    assert answer["checkouts"][0]["hints_removed"] is True


def test_an_orphan_hint_table_is_removed(adopted_vault):
    import code_hints
    import repository_retention

    from tests.test_code_hints import _meta

    _root, state = adopted_vault
    orphan = "checkout:" + "3" * 64
    code_hints.write_hints(state, _meta(orphan), [])
    (state / "cache" / "evidence-graph").mkdir(parents=True, exist_ok=True)

    answer = repository_retention._orphan_hints(state, [])
    assert (answer, code_hints.hints_path(state, orphan).exists()) == ([orphan], False)


def test_the_vault_and_every_activated_generation_are_never_grouped(adopted_vault, tmp_path):
    import repository_retention

    root, _state = adopted_vault
    foreign = {"repository_scope": {"checkout_id": "checkout:f", "checkout_root": str(tmp_path / "x")}}
    vault = {"repository_scope": {"checkout_id": "checkout:v", "checkout_root": str(root)}}
    manifests = [("g-foreign", "t", foreign), ("g-vault", "t", vault), ("g-active", "t", foreign), ("g-none", "t", {})]

    groups = repository_retention._foreign_groups(manifests, frozenset({"g-active"}))
    assert {key: value["generations"] for key, value in groups.items()} == {"checkout:f": ["g-foreign"]}


def test_the_nightly_retires_after_refreshing_and_before_pruning():
    import scheduled_nightly

    labels = [step.label for step in scheduled_nightly._post_compile_steps()]
    order = [labels.index(label) for label in ("repositories", "repository_retention", "prune_generations")]
    assert order == sorted(order)
