"""Retention of foreign repository generations (#24, D1).

Per checkout, by identity: a gone or marked checkout loses every generation
and its hint table; a live one keeps its newest two. The vault's memory
generations are never considered, and every discard runs under the
per-repository fence.
"""

from __future__ import annotations

import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from tests import repository_refresh_helpers as _refresh_tests_helpers  # noqa: E402
from tests.repository_index_helpers import ALPHA, _git, _repository  # noqa: E402
from tests.repository_retention_helpers import _generation_ids  # noqa: E402

adopted_vault = _refresh_tests_helpers.adopted_vault
_isolated_reader_cache = _refresh_tests_helpers._isolated_reader_cache


def _edited(repository: Path, name: str) -> None:
    (repository / "pkg" / f"{name}.py").write_text(f"def {name}():\n    return 1\n", encoding="utf-8")


def _three_generations(state: Path, repository: Path) -> list[str]:
    import repository_indexing

    first = repository_indexing.index_repository(repository, state_root=state)["generation_id"]
    _edited(repository, "beta")
    second = repository_indexing.refresh_repository(repository, state_root=state)["generation_id"]
    _edited(repository, "gamma")
    third = repository_indexing.refresh_repository(repository, state_root=state)["generation_id"]
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
    import repository_indexing
    import repository_retention
    from repository_scope import resolve_repository_scope

    _root, state = adopted_vault
    repository = _repository(tmp_path / "repo", {"pkg/alpha.py": ALPHA})
    kept = repository_indexing.index_repository(repository, state_root=state)["generation_id"]
    topic = repository.parent / "repo-topic"
    _git(repository, "worktree", "add", "-q", "-b", "topic", str(topic))
    gone = repository_indexing.index_repository(topic, state_root=state)["generation_id"]
    checkout_id = resolve_repository_scope(topic).checkout_id
    _git(repository, "worktree", "remove", "--force", str(topic))

    answer = repository_retention.retire_repositories(state_root=state)

    verdicts = {Path(entry["checkout_root"]): entry["verdict"] for entry in answer["checkouts"]}
    assert (verdicts, _generation_ids(state)) == ({topic.resolve(): "checkout_missing"}, {kept})
    assert (code_hints.hints_path(state, checkout_id).exists(), gone in _generation_ids(state)) == (False, False)


def test_a_checkout_marked_not_indexed_is_retired_whole(adopted_vault, tmp_path):
    import repository_indexing
    import repository_retention

    _root, state = adopted_vault
    repository = _repository(tmp_path / "repo", {"pkg/alpha.py": ALPHA})
    repository_indexing.index_repository(repository, state_root=state)
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

    from tests.code_hints_helpers import _meta

    _root, state = adopted_vault
    orphan = "checkout:" + "3" * 64
    code_hints.write_hints(state, _meta(orphan), [])
    (state / "cache" / "evidence-graph").mkdir(parents=True, exist_ok=True)

    answer = repository_retention.orphan_hints(state, [])
    assert (answer, code_hints.hints_path(state, orphan).exists()) == ([orphan], False)


def test_the_vault_and_every_activated_generation_are_never_grouped(adopted_vault, tmp_path):
    import repository_retention

    root, _state = adopted_vault
    foreign = {"repository_scope": {"checkout_id": "checkout:f", "checkout_root": str(tmp_path / "x")}}
    vault = {"repository_scope": {"checkout_id": "checkout:v", "checkout_root": str(root)}}
    manifests = [("g-foreign", "t", foreign), ("g-vault", "t", vault), ("g-active", "t", foreign), ("g-none", "t", {})]

    groups = repository_retention.foreign_groups(manifests, frozenset({"g-active"}), _code_roots_only)
    assert {key: value["generations"] for key, value in groups.items()} == {"checkout:f": ["g-foreign"]}


def _code_roots_only(_identifier, manifest) -> bool:
    return bool(manifest.get("code_roots"))


def test_a_vault_code_generation_without_code_roots_is_retentions_to_collect(adopted_vault):
    """Audit C-42: the catalog's `holds_code` decides, not the manifest field alone."""
    import repository_retention

    root, _state = adopted_vault
    vault = {"repository_scope": {"checkout_id": "checkout:v", "checkout_root": str(root)}}
    manifests = [("g-legacy-code", "t", vault), ("g-memory", "t", vault)]

    groups = repository_retention.foreign_groups(
        manifests, frozenset(), lambda identifier, _manifest: identifier == "g-legacy-code"
    )
    assert {key: value["generations"] for key, value in groups.items()} == {
        "checkout:v": ["g-legacy-code"]
    }


def test_the_nightly_retires_after_refreshing_and_before_pruning():
    import nightly_budget

    labels = [step.label for step in nightly_budget.post_compile_steps()]
    order = [labels.index(label) for label in ("repositories", "repository_retention", "prune_generations")]
    assert order == sorted(order)
