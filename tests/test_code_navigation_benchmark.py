"""Behavioral tests for the deterministic Python navigation qualification fixture, its pinned identity and its arithmetic."""

from __future__ import annotations

import ast
import copy
import hashlib
import json
import sys
from dataclasses import replace
from pathlib import Path

import pytest

BENCHMARK_ROOT = Path(__file__).resolve().parent.parent / "benchmark"
SCRIPTS_ROOT = Path(__file__).resolve().parent.parent / "scripts"
for path in (BENCHMARK_ROOT, SCRIPTS_ROOT):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import navigation_benchmark_metrics as metrics  # noqa: E402
from generate_python_qualification import (  # noqa: E402
    QualificationRepository,
    generate_qualification_repository,
    gold_payload,
)
from navigation_benchmark_git import (  # noqa: E402
    FixtureIdentityError,
    initialize_deterministic_git,
    verify_repository_identity,
)
from navigation_benchmark_metrics import (  # noqa: E402
    _observed_expected_exception,
    nearest_rank_percentile,
    precision_recall_f1,
)
from navigation_benchmark_schema import (  # noqa: E402
    load_manifest,
    validate_gold,
    validate_manifest,
)

from tests.code_navigation_benchmark_helpers import PACKAGE_SHA256  # noqa: E402
from tests.modules.python_qualification_corpus.support import (  # noqa: E402
    _GOLD_CAPABILITIES,
)
from tests.modules.python_qualification_corpus.support import (  # noqa: E402
    generated_pair as generated_pair,
)


def _is_padding_definition(query) -> bool:
    if query.capability != "definition":
        return False
    return query.expected_locations[0].path.startswith("padding/module_")


def _padding_entry(repository, module_index: int) -> tuple:
    """The path of a padding module and the name of its last block."""
    module_path = repository.root / f"padding/module_{module_index:02d}.py"
    tree = ast.parse(module_path.read_bytes(), filename=module_path.as_posix())
    functions = [node for node in tree.body if isinstance(node, ast.FunctionDef)]
    return module_path, functions[-1].name


def _assert_padding_query(repository, module_index: int, query) -> None:
    """A gold query points from the user module at one padding entry block."""
    module_path, entry_name = _padding_entry(repository, module_index)
    expected = entry_name.encode("ascii")
    location = query.expected_locations[0]
    assert (
        query.path,
        query.symbol,
        query.direction,
        len(query.expected_locations),
        location.path,
    ) == (
        "qual/padding_users.py",
        entry_name,
        None,
        1,
        f"padding/module_{module_index:02d}.py",
    )
    assert module_path.read_bytes()[location.byte_start : location.byte_end] == expected
    query_content = (repository.root / query.path).read_bytes()
    assert query_content[query.byte_offset : query.byte_end] == expected


def _sampled_padding_ids(repository, expected_ids: tuple) -> tuple:
    sampled = metrics._performance_queries(repository.gold_queries)
    return tuple(
        query.query_id for query in sampled if query.query_id in expected_ids
    )


_PADDING_QUERY_IDS = tuple(f"definition-{index:03d}" for index in range(168, 200))


def _query_ids(queries) -> tuple:
    return tuple(query.query_id for query in queries)


def test_exactly_32_padding_entries_are_bound_to_definition_gold_queries(
    generated_pair: tuple[QualificationRepository, QualificationRepository],
) -> None:
    repository = generated_pair[0]
    padding_queries = [
        query for query in repository.gold_queries if _is_padding_definition(query)
    ]
    assert _query_ids(padding_queries) == _PADDING_QUERY_IDS
    assert len(padding_queries) == 32

    for module_index, query in enumerate(padding_queries):
        _assert_padding_query(repository, module_index, query)

    assert _sampled_padding_ids(repository, _PADDING_QUERY_IDS) == (
        "definition-177",
        "definition-199",
    )


def test_repository_identity_rejects_any_in_memory_workload_content_tamper(
    generated_pair: tuple[QualificationRepository, QualificationRepository],
) -> None:
    repository = generated_pair[0]
    manifest = load_manifest()
    workload = repository.workloads[23]
    content_fields = (
        "original_content",
        "edited_content",
        "created_content",
        "baseline_probe_content",
        "create_probe_content",
        "edit_probe_content",
        "rename_probe_content",
        "rename_old_probe_content",
        "delete_probe_content",
    )
    for field in content_fields:
        mutated = replace(workload, **{field: getattr(workload, field) + b"# tamper\n"})
        workloads = list(repository.workloads)
        workloads[23] = mutated
        with pytest.raises(FixtureIdentityError, match="workload catalog"):
            verify_repository_identity(
                replace(repository, workloads=tuple(workloads)),
                manifest,
            )


def _capability_tally(sample) -> dict:
    capabilities = [query.capability for query in sample]
    return {name: capabilities.count(name) for name in _GOLD_CAPABILITIES}


def test_performance_sample_is_exact_deterministic_and_stratified(
    generated_pair: tuple[QualificationRepository, QualificationRepository],
) -> None:
    first, second = generated_pair
    first_sample = metrics._performance_queries(first.gold_queries)
    second_sample = metrics._performance_queries(second.gold_queries)

    assert first_sample == second_sample
    assert len(first_sample) == metrics.PERFORMANCE_SAMPLES == 20
    assert len({query.query_id for query in first_sample}) == 20
    assert _capability_tally(first_sample) == {
        "definition": 10,
        "references": 5,
        "calls": 5,
    }
    assert tuple(query.query_id for query in first_sample) == (
        "definition-000",
        "references-000",
        "calls-000",
        "definition-022",
        "definition-044",
        "references-025",
        "calls-025",
        "definition-066",
        "definition-088",
        "references-050",
        "calls-050",
        "definition-111",
        "definition-133",
        "references-074",
        "calls-074",
        "definition-155",
        "definition-177",
        "references-099",
        "calls-099",
        "definition-199",
    )


def test_gold_payload_is_canonical_closed_and_reproducibly_hashed(
    generated_pair: tuple[QualificationRepository, QualificationRepository],
) -> None:
    first, second = generated_pair
    first_payload = gold_payload(first)
    second_payload = gold_payload(second)
    validate_gold(first_payload)
    assert first_payload == second_payload
    canonical = json.dumps(
        first_payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    assert hashlib.sha256(canonical).hexdigest() == first.gold_sha256
    with pytest.raises(ValueError, match="schema"):
        validate_gold({**first_payload, "unknown": True})
    nested_unknown = copy.deepcopy(first_payload)
    nested_unknown["queries"][0]["unknown"] = True  # type: ignore[index]
    with pytest.raises(ValueError, match="schema"):
        validate_gold(nested_unknown)


def test_manifest_is_fully_pinned_and_bound_to_generated_source_and_gold(
    generated_pair: tuple[QualificationRepository, QualificationRepository],
) -> None:
    repository = generated_pair[0]
    manifest = load_manifest()
    validate_manifest(manifest)
    assert manifest == {
        "schema_version": "code-navigation-python/v1",
        "fixture_seed": 411,
        "fixture_lines": 100_000,
        "python_min": "3.14",
        "pyright_version": "1.1.411",
        "pyright_package_sha256": PACKAGE_SHA256,
        "node_major": 22,
        "definition_queries": 200,
        "reference_queries": 100,
        "call_queries": 100,
        "edit_rename_delete_cycles": 50,
        "crash_cycles": 20,
        "default_limit": 10,
        "max_estimated_tokens": 1200,
        "expected_source_manifest_sha256": repository.source_manifest_sha256,
        "expected_gold_sha256": repository.gold_sha256,
        "market_superiority_claimed": False,
    }
    for key in manifest:
        wrong = dict(manifest)
        wrong[key] = "wrong" if not isinstance(manifest[key], bool) else True
        with pytest.raises(ValueError, match="schema"):
            validate_manifest(wrong)
    with pytest.raises(ValueError, match="schema"):
        validate_manifest({**manifest, "unknown": 1})


def test_repository_hash_mismatch_aborts_before_runtime(
    generated_pair: tuple[QualificationRepository, QualificationRepository],
) -> None:
    repository = generated_pair[0]
    manifest = load_manifest()
    verify_repository_identity(repository, manifest)
    with pytest.raises(FixtureIdentityError, match="source manifest"):
        verify_repository_identity(
            repository,
            {**manifest, "expected_source_manifest_sha256": "0" * 64},
        )
    with pytest.raises(FixtureIdentityError, match="gold"):
        verify_repository_identity(
            repository,
            {**manifest, "expected_gold_sha256": "0" * 64},
        )


def test_repository_verification_rehashes_current_source_and_gold(tmp_path: Path) -> None:
    manifest = load_manifest()
    source_tampered = generate_qualification_repository(tmp_path / "source-tampered")
    source = source_tampered.root / "qual/definitions.py"
    source.write_bytes(source.read_bytes() + b"# post-generation tamper\n")
    with pytest.raises(FixtureIdentityError, match="source manifest"):
        verify_repository_identity(source_tampered, manifest)

    gold_tampered = generate_qualification_repository(tmp_path / "gold-tampered")
    altered = replace(
        gold_tampered,
        gold_queries=gold_tampered.gold_queries[:-1],
    )
    with pytest.raises(FixtureIdentityError, match="gold"):
        verify_repository_identity(altered, manifest)


def test_repository_verification_binds_pyright_configuration(tmp_path: Path) -> None:
    repository = generate_qualification_repository(tmp_path / "config-tampered")
    manifest = load_manifest()
    verify_repository_identity(repository, manifest)
    config = repository.root / "pyrightconfig.json"
    config.write_bytes(config.read_bytes() + b" ")

    with pytest.raises(FixtureIdentityError, match="source manifest"):
        verify_repository_identity(repository, manifest)


def test_deterministic_git_commit_is_root_independent(tmp_path: Path) -> None:
    first = generate_qualification_repository(tmp_path / "git-first")
    second = generate_qualification_repository(tmp_path / "git-second")
    first_commit = initialize_deterministic_git(first.root)
    second_commit = initialize_deterministic_git(second.root)
    assert first_commit == second_commit
    assert first_commit == "9f360189a30b37dbf1007010c77f0176dd2ab1d9"
    assert len(first_commit) == 40
    assert first_commit == first_commit.lower()


def test_deterministic_git_ignores_hostile_ambient_configuration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    baseline = generate_qualification_repository(tmp_path / "git-baseline")
    hostile = generate_qualification_repository(tmp_path / "git-hostile")
    expected_commit = initialize_deterministic_git(baseline.root)

    hostile_home = tmp_path / "hostile-home"
    hostile_home.mkdir()
    template = hostile_home / "template"
    hooks = template / "hooks"
    hooks.mkdir(parents=True)
    hook_sentinel = tmp_path / "hook-ran"
    filter_sentinel = tmp_path / "filter-ran"
    hook = hooks / "pre-commit"
    hook.write_text(
        '#!/bin/sh\nprintf hook > "$QUALIFICATION_HOOK_SENTINEL"\nexit 1\n',
        encoding="utf-8",
    )
    hook.chmod(0o755)
    filter_program = hostile_home / "hostile-filter.sh"
    filter_program.write_text(
        '#!/bin/sh\nprintf filter > "$QUALIFICATION_FILTER_SENTINEL"\ncat\n',
        encoding="utf-8",
    )
    filter_program.chmod(0o755)
    attributes = hostile_home / "attributes"
    attributes.write_text("*.py filter=hostile text\n", encoding="utf-8")
    (hostile_home / ".gitconfig").write_text(
        "\n".join(
            (
                "[init]",
                "\tdefaultObjectFormat = sha256",
                f"\ttemplateDir = {template.as_posix()}",
                "[core]",
                "\tautocrlf = true",
                "\tfilemode = true",
                f"\tattributesFile = {attributes.as_posix()}",
                f"\thooksPath = {hooks.as_posix()}",
                '[filter "hostile"]',
                f"\tclean = {filter_program.as_posix()}",
                "\trequired = true",
                "[commit]",
                "\tgpgSign = true",
                "",
            )
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("HOME", str(hostile_home))
    monkeypatch.setenv("USERPROFILE", str(hostile_home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(hostile_home))
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(hostile_home / ".gitconfig"))
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", str(hostile_home / ".gitconfig"))
    monkeypatch.setenv("GIT_DEFAULT_HASH", "sha256")
    monkeypatch.setenv("GIT_TEMPLATE_DIR", str(template))
    monkeypatch.setenv("QUALIFICATION_HOOK_SENTINEL", str(hook_sentinel))
    monkeypatch.setenv("QUALIFICATION_FILTER_SENTINEL", str(filter_sentinel))

    actual_commit = initialize_deterministic_git(hostile.root)

    assert actual_commit == expected_commit
    assert len(actual_commit) == 40
    assert not hook_sentinel.exists()
    assert not filter_sentinel.exists()


@pytest.mark.parametrize(
    ("expected", "actual", "counts", "scores"),
    [
        (set(), set(), (0, 0, 0), (1.0, 1.0, 1.0)),
        ({"a"}, set(), (0, 0, 1), (1.0, 0.0, 0.0)),
        (set(), {"a"}, (0, 1, 0), (0.0, 1.0, 0.0)),
        ({"a", "b"}, {"b", "c"}, (1, 1, 1), (0.5, 0.5, 0.5)),
    ],
)
def test_precision_recall_f1_arithmetic(
    expected: set[str],
    actual: set[str],
    counts: tuple[int, int, int],
    scores: tuple[float, float, float],
) -> None:
    metric = precision_recall_f1(expected, actual)
    assert (metric["true_positive"], metric["false_positive"], metric["false_negative"]) == counts
    assert (metric["precision"], metric["recall"], metric["f1"]) == scores


def test_nearest_rank_percentiles_are_deterministic() -> None:
    assert nearest_rank_percentile([], 0.5) is None
    assert nearest_rank_percentile([7.0], 0.95) == 7.0
    assert nearest_rank_percentile([5, 1, 4, 2, 3], 0.5) == 3.0
    assert nearest_rank_percentile(range(1, 101), 0.95) == 95.0
    with pytest.raises(ValueError):
        nearest_rank_percentile([1.0], 0.0)
    with pytest.raises(ValueError):
        nearest_rank_percentile([float("nan")], 0.95)


def test_interruption_evidence_requires_the_expected_exception() -> None:
    def timeout() -> None:
        raise TimeoutError("measured timeout")

    def wrong_failure() -> None:
        raise RuntimeError("not the requested scenario")

    assert _observed_expected_exception(timeout, TimeoutError)
    assert not _observed_expected_exception(lambda: None, TimeoutError)
    assert not _observed_expected_exception(wrong_failure, TimeoutError)
