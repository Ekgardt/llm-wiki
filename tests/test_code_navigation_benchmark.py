"""Behavioral tests for the deterministic Python navigation qualification."""

from __future__ import annotations

import ast
import builtins
import copy
import ctypes
import hashlib
import json
import math
import os
import subprocess
import sys
import threading
import time
from dataclasses import fields, replace
from pathlib import Path
from types import SimpleNamespace

import pytest

BENCHMARK_ROOT = Path(__file__).resolve().parent.parent / "benchmark"
SCRIPTS_ROOT = Path(__file__).resolve().parent.parent / "scripts"
for path in (BENCHMARK_ROOT, SCRIPTS_ROOT):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import generate_python_qualification as qualification_generator  # noqa: E402
import run_code_navigation as benchmark_runner  # noqa: E402
from code_intelligence import Capability, PositionEncoding, PositionRange  # noqa: E402
from code_navigation import (  # noqa: E402
    NavigationLocation,
    NavigationResult,
    NavigationStatus,
    Provenance,
    ResolutionLabel,
)
from code_navigation_renderer import estimate_tokens, render_navigation  # noqa: E402
from generate_python_qualification import (  # noqa: E402
    CALL_QUERIES,
    DEFINITION_QUERIES,
    FIXTURE_LINES,
    FIXTURE_SEED,
    PYRIGHT_VERSION,
    REFERENCE_QUERIES,
    GoldLocation,
    GoldQuery,
    QualificationRepository,
    generate_qualification_repository,
    gold_payload,
)
from lsp_positions import LspPosition, LspRange, path_to_file_uri  # noqa: E402
from pyright_session import LspLocation  # noqa: E402
from repository_scope import resolve_repository_scope  # noqa: E402
from run_code_navigation import (  # noqa: E402
    GATE_THRESHOLDS,
    BenchmarkDependencies,
    FixtureIdentityError,
    QualifiedIdentityError,
    _current_citation,
    _mutate_and_measure,
    _navigation_request,
    _observed_expected_exception,
    _probe_operator_corpus,
    _RealNavigationRuntime,
    _token_record,
    evaluate_gates,
    initialize_deterministic_git,
    load_manifest,
    nearest_rank_percentile,
    parse_args,
    precision_recall_f1,
    resolve_state_root,
    run_fixture_benchmark,
    validate_gold,
    validate_manifest,
    validate_report,
    verify_repository_identity,
)

PACKAGE_SHA256 = "bd5c488fc20fa237a944279bf32cae2f986cf10d5d5d9e8705819859daeb2f4a"


@pytest.fixture(scope="module")
def generated_pair(
    tmp_path_factory: pytest.TempPathFactory,
) -> tuple[QualificationRepository, QualificationRepository]:
    root = tmp_path_factory.mktemp("code-navigation-qualification")
    return (
        generate_qualification_repository(root / "first"),
        generate_qualification_repository(root / "second"),
    )


def _source_files(repository: QualificationRepository) -> dict[str, bytes]:
    return {
        path.relative_to(repository.root).as_posix(): path.read_bytes()
        for path in sorted(repository.root.rglob("*.py"))
    }


def test_generator_is_exactly_100_000_lines_and_byte_identical_across_roots(
    generated_pair: tuple[QualificationRepository, QualificationRepository],
) -> None:
    first, second = generated_pair
    assert first.line_count == second.line_count == FIXTURE_LINES == 100_000
    assert first.source_manifest_sha256 == second.source_manifest_sha256
    assert first.gold_sha256 == second.gold_sha256
    assert first.source_manifest == second.source_manifest
    assert _source_files(first) == _source_files(second)
    assert sum(data.count(b"\n") for data in _source_files(first).values()) == 100_000
    source_bytes = sum(map(len, _source_files(first).values()))
    assert qualification_generator.FIXTURE_MIN_PYTHON_BYTES == 2 * 1024 * 1024
    assert qualification_generator.FIXTURE_MAX_PYTHON_BYTES == 4 * 1024 * 1024
    assert qualification_generator.FIXTURE_MIN_PYTHON_BYTES <= source_bytes
    assert source_bytes <= qualification_generator.FIXTURE_MAX_PYTHON_BYTES
    padding_modules = sorted((first.root / "padding").glob("module_*.py"))
    assert len(padding_modules) == qualification_generator.PADDING_MODULES == 32
    assert len(_source_files(first)) == 48
    assert first.root.joinpath("qual", "padding_users.py").is_file()
    for module in padding_modules:
        content = module.read_bytes()
        compile(content, module.as_posix(), "exec")
        tree = ast.parse(content, filename=module.as_posix())
        assert tree.body
        assert all(isinstance(node, ast.FunctionDef) for node in tree.body)
        assert qualification_generator.PADDING_BLOCK_LINES == 64
        assert all(
            node.end_lineno is not None
            and node.end_lineno - node.lineno + 1 == qualification_generator.PADDING_BLOCK_LINES
            for node in tree.body
            if isinstance(node, ast.FunctionDef)
        )
        assert b"#" not in content
    assert not (first.root / ".git").exists()


def test_padding_ast_is_live_and_reachable_from_every_static_user_call(
    generated_pair: tuple[QualificationRepository, QualificationRepository],
) -> None:
    repository = generated_pair[0]
    function_edges: dict[str, tuple[str, ...]] = {}
    entry_names: list[str] = []
    live_constants: list[int] = []
    module_paths = sorted((repository.root / "padding").glob("module_*.py"))

    for module_index, module_path in enumerate(module_paths):
        content = module_path.read_bytes()
        assert b"#" not in content
        tree = ast.parse(content, filename=module_path.as_posix())
        functions = [node for node in tree.body if isinstance(node, ast.FunctionDef)]
        assert len(functions) == (48 if module_index < 24 else 47)
        assert len(functions) == len(tree.body)

        for block_index, function in enumerate(functions):
            expected_name = f"padding_{module_index:02d}_{block_index:03d}"
            assert function.name == expected_name
            assert not function.decorator_list
            assert not function.args.posonlyargs
            assert not function.args.kwonlyargs
            assert function.args.vararg is None
            assert function.args.kwarg is None
            assert [argument.arg for argument in function.args.args] == ["accumulator"]
            annotation = function.args.args[0].annotation
            assert isinstance(annotation, ast.Name) and annotation.id == "int"
            assert isinstance(function.returns, ast.Name) and function.returns.id == "int"
            assert function.end_lineno is not None
            assert function.end_lineno - function.lineno + 1 == 64

            body = list(function.body)
            returned = body.pop()
            assert isinstance(returned, ast.Return)
            assert isinstance(returned.value, ast.Name)
            assert returned.value.id == "accumulator"

            if block_index:
                previous_name = f"padding_{module_index:02d}_{block_index - 1:03d}"
                chain = body.pop(0)
                assert isinstance(chain, ast.Assign)
                assert len(chain.targets) == 1
                assert isinstance(chain.targets[0], ast.Name)
                assert chain.targets[0].id == "accumulator"
                assert isinstance(chain.value, ast.Call)
                assert isinstance(chain.value.func, ast.Name)
                assert chain.value.func.id == previous_name
                assert len(chain.value.args) == 1
                assert isinstance(chain.value.args[0], ast.Name)
                assert chain.value.args[0].id == "accumulator"
                assert not chain.value.keywords
                function_edges[function.name] = (previous_name,)
            else:
                function_edges[function.name] = ()

            assert body
            for operation in body:
                assert isinstance(operation, ast.AugAssign)
                assert isinstance(operation.target, ast.Name)
                assert operation.target.id == "accumulator"
                assert isinstance(operation.op, ast.Add)
                assert isinstance(operation.value, ast.Constant)
                assert isinstance(operation.value.value, int)
                live_constants.append(operation.value.value)

        entry_names.append(functions[-1].name)

    assert len(live_constants) == len(set(live_constants))
    assert all(value // 10_000_000 == FIXTURE_SEED for value in live_constants)

    users_path = repository.root / "qual" / "padding_users.py"
    users_content = users_path.read_bytes()
    assert b"#" not in users_content
    users_tree = ast.parse(users_content, filename=users_path.as_posix())
    padding_imports = [
        node
        for node in users_tree.body
        if isinstance(node, ast.ImportFrom)
        and node.module is not None
        and node.module.startswith("padding.module_")
    ]
    assert [node.module for node in padding_imports] == [
        f"padding.module_{index:02d}" for index in range(32)
    ]
    assert [node.names[0].name for node in padding_imports] == entry_names
    assert all(len(node.names) == 1 and node.names[0].asname is None for node in padding_imports)

    user_functions = [node for node in users_tree.body if isinstance(node, ast.FunctionDef)]
    assert len(user_functions) == 1
    user_function = user_functions[0]
    assert user_function.name == "exercise_padding"
    user_calls = user_function.body[:32]
    called_entries: list[str] = []
    for statement in user_calls:
        assert isinstance(statement, ast.Assign)
        assert len(statement.targets) == 1
        assert isinstance(statement.targets[0], ast.Name)
        assert statement.targets[0].id == "accumulator"
        assert isinstance(statement.value, ast.Call)
        assert isinstance(statement.value.func, ast.Name)
        called_entries.append(statement.value.func.id)
        assert len(statement.value.args) == 1
        assert isinstance(statement.value.args[0], ast.Name)
        assert statement.value.args[0].id == "accumulator"
        assert not statement.value.keywords
    assert called_entries == entry_names

    for operation in user_function.body[32:-1]:
        assert isinstance(operation, ast.AugAssign)
        assert isinstance(operation.target, ast.Name)
        assert operation.target.id == "accumulator"
        assert isinstance(operation.op, ast.Add)
        assert isinstance(operation.value, ast.Constant)
        assert isinstance(operation.value.value, int)
        live_constants.append(operation.value.value)
    returned = user_function.body[-1]
    assert isinstance(returned, ast.Return)
    assert isinstance(returned.value, ast.Name)
    assert returned.value.id == "accumulator"
    assert len(live_constants) == len(set(live_constants))
    assert all(value // 10_000_000 == FIXTURE_SEED for value in live_constants)

    reachable: set[str] = set()
    pending = list(called_entries)
    while pending:
        function_name = pending.pop()
        if function_name in reachable:
            continue
        reachable.add(function_name)
        pending.extend(function_edges[function_name])
    assert reachable == set(function_edges)


def test_exactly_32_padding_entries_are_bound_to_definition_gold_queries(
    generated_pair: tuple[QualificationRepository, QualificationRepository],
) -> None:
    repository = generated_pair[0]
    padding_queries = [
        query
        for query in repository.gold_queries
        if query.capability == "definition"
        and query.expected_locations[0].path.startswith("padding/module_")
    ]
    expected_ids = tuple(f"definition-{index:03d}" for index in range(168, 200))
    assert tuple(query.query_id for query in padding_queries) == expected_ids
    assert len(padding_queries) == 32

    for module_index, query in enumerate(padding_queries):
        module_path = repository.root / f"padding/module_{module_index:02d}.py"
        module_tree = ast.parse(module_path.read_bytes(), filename=module_path.as_posix())
        functions = [node for node in module_tree.body if isinstance(node, ast.FunctionDef)]
        entry_name = functions[-1].name
        assert query.path == "qual/padding_users.py"
        assert query.symbol == entry_name
        assert query.direction is None
        assert len(query.expected_locations) == 1
        location = query.expected_locations[0]
        assert location.path == f"padding/module_{module_index:02d}.py"
        assert module_path.read_bytes()[location.byte_start : location.byte_end] == (
            entry_name.encode("ascii")
        )
        query_content = (repository.root / query.path).read_bytes()
        assert query_content[query.byte_offset : query.byte_end] == entry_name.encode("ascii")

    sampled_padding_ids = tuple(
        query.query_id
        for query in benchmark_runner._performance_queries(repository.gold_queries)
        if query.query_id in expected_ids
    )
    assert sampled_padding_ids == ("definition-177", "definition-199")


def test_generator_contains_promised_semantics_and_dedicated_workloads(
    generated_pair: tuple[QualificationRepository, QualificationRepository],
) -> None:
    repository = generated_pair[0]
    assert set(repository.feature_inventory) == {
        "ambiguous_names",
        "broken_files",
        "cross_file_calls",
        "cross_file_definitions",
        "cross_file_imports",
        "cross_file_references",
        "inheritance",
        "protocols",
        "unicode_identifiers",
    }
    assert repository.ambiguous_symbols == ("execute",)
    assert len(repository.workloads) == 50
    assert len({workload.workload_id for workload in repository.workloads}) == 50
    for attribute in ("original_path", "renamed_path", "created_path", "probe_path"):
        assert len({getattr(workload, attribute) for workload in repository.workloads}) == 1
    first = repository.workloads[0]
    assert len({first.original_path, first.renamed_path, first.created_path, first.probe_path}) == 4
    for attribute in ("original_symbol", "edited_symbol", "created_symbol"):
        assert len({getattr(workload, attribute) for workload in repository.workloads}) == 50
    for attribute in (
        "original_content",
        "edited_content",
        "created_content",
        "baseline_probe_content",
        "create_probe_content",
        "edit_probe_content",
        "rename_probe_content",
        "rename_old_probe_content",
        "delete_probe_content",
    ):
        assert len({getattr(workload, attribute) for workload in repository.workloads}) == 50
    for workload in repository.workloads:
        assert workload.probe_path not in {
            workload.original_path,
            workload.renamed_path,
            workload.created_path,
        }
        assert workload.original_symbol != workload.edited_symbol
        original_start = workload.original_content.index(workload.original_symbol.encode())
        edited_start = workload.edited_content.index(workload.edited_symbol.encode())
        assert (original_start, workload.original_symbol) != (
            edited_start,
            workload.edited_symbol,
        )
        assert workload.created_symbol.encode() in workload.create_probe_content
        assert workload.edited_symbol.encode() in workload.edit_probe_content
        assert workload.renamed_path.removesuffix(".py").split("/")[-1].encode() in (
            workload.rename_probe_content
        )
        assert workload.edited_symbol.encode() in workload.delete_probe_content
    assert (repository.root / first.original_path).read_bytes() == first.original_content
    assert (repository.root / first.probe_path).read_bytes() == first.baseline_probe_content
    assert not any(
        "cycle_" in path or "probe_" in path
        for path, _digest in repository.source_manifest
    )
    assert any("broken" in path for path, _digest in repository.source_manifest)


def test_workload_catalog_canonically_binds_every_variant_and_field(
    generated_pair: tuple[QualificationRepository, QualificationRepository],
) -> None:
    repository = generated_pair[0]
    catalog_path = repository.root / qualification_generator.WORKLOAD_CATALOG_PATH
    expected = qualification_generator.workload_catalog_bytes(repository.workloads)

    assert catalog_path.read_bytes() == expected
    compile(expected, qualification_generator.WORKLOAD_CATALOG_PATH, "exec")
    catalog_text = expected.decode("ascii")
    assert catalog_text.count("    ('mutation-") == 50
    for workload in repository.workloads:
        assert qualification_generator.workload_digest(workload) in catalog_text

    baseline = repository.workloads[17]
    baseline_digest = qualification_generator.workload_digest(baseline)
    for field in fields(baseline):
        value = getattr(baseline, field.name)
        changed = value + b"#changed\n" if isinstance(value, bytes) else value + "-changed"
        assert qualification_generator.workload_digest(
            replace(baseline, **{field.name: changed})
        ) != baseline_digest


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


def test_gold_has_exact_counts_complete_ranges_and_cross_file_answers(
    generated_pair: tuple[QualificationRepository, QualificationRepository],
) -> None:
    repository = generated_pair[0]
    counts = {
        capability: sum(query.capability == capability for query in repository.gold_queries)
        for capability in ("definition", "references", "calls")
    }
    assert (DEFINITION_QUERIES, REFERENCE_QUERIES, CALL_QUERIES) == (200, 100, 100)
    assert counts == {"definition": 200, "references": 100, "calls": 100}
    assert len(repository.gold_queries) == 400
    assert all(query.expected_locations for query in repository.gold_queries)
    assert all(
        location.byte_end > location.byte_start
        and len(location.source_sha256) == 64
        and location.line >= 1
        and location.character >= 0
        for query in repository.gold_queries
        for location in query.expected_locations
    )
    definitions = [query for query in repository.gold_queries if query.capability == "definition"]
    assert all(query.path != query.expected_locations[0].path for query in definitions)
    references = [query for query in repository.gold_queries if query.capability == "references"]
    assert all(len(query.expected_locations) >= 5 for query in references)
    calls = [query for query in repository.gold_queries if query.capability == "calls"]
    assert all(query.direction == "outgoing" for query in calls)
    assert all(query.path != query.expected_locations[0].path for query in calls)


def test_performance_sample_is_exact_deterministic_and_stratified(
    generated_pair: tuple[QualificationRepository, QualificationRepository],
) -> None:
    first, second = generated_pair
    first_sample = benchmark_runner._performance_queries(first.gold_queries)
    second_sample = benchmark_runner._performance_queries(second.gold_queries)

    assert first_sample == second_sample
    assert len(first_sample) == benchmark_runner.PERFORMANCE_SAMPLES == 20
    assert len({query.query_id for query in first_sample}) == 20
    assert [query.capability for query in first_sample].count("definition") == 10
    assert [query.capability for query in first_sample].count("references") == 5
    assert [query.capability for query in first_sample].count("calls") == 5
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


def test_unicode_query_characters_are_utf8_byte_offsets(
    generated_pair: tuple[QualificationRepository, QualificationRepository],
) -> None:
    repository = generated_pair[0]
    unicode_queries = [
        query
        for query in repository.gold_queries
        if any(ord(character) > 127 for character in query.symbol)
        or query.character
        != len(
            (repository.root / query.path)
            .read_text(encoding="utf-8")
            .splitlines()[query.line - 1][: query.codepoint_character]
            .encode("utf-8")
        )
    ]
    assert unicode_queries
    for query in unicode_queries:
        content = (repository.root / query.path).read_bytes()
        lines = content.splitlines(keepends=True)
        line_start = sum(len(line) for line in lines[: query.line - 1])
        assert query.byte_offset == line_start + query.character
        assert content[query.byte_offset : query.byte_end] == query.symbol.encode("utf-8")


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
        "python_min": "3.10",
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


def _metric(tp: int, fp: int, fn: int) -> dict[str, int | float]:
    return precision_recall_f1(
        {("expected", index) for index in range(tp + fn)},
        {
            *(("expected", index) for index in range(tp)),
            *(("actual", index) for index in range(fp)),
        },
    )


def _gold_query_ids() -> list[str]:
    return [
        *(f"definition-{index:03d}" for index in range(200)),
        *(f"references-{index:03d}" for index in range(100)),
        *(f"calls-{index:03d}" for index in range(100)),
    ]


def _measured_report(mode: str = "qualification") -> dict[str, object]:
    qualification = mode == "qualification"
    return {
        "schema_version": "code-navigation-python-report/v1",
        "mode": mode,
        "identity": {
            "source_manifest_sha256": "30f0b042afc971426f9e0c8e6fa8f1c9446976de82a427acda3d1ec9eb0e31e8",
            "gold_sha256": "da2ddb5b6ef245a388b7fa4142770aa25e0580319df16af6ac7475a3c0455a5f",
            "git_commit": "c" * 40,
            "python_version": "3.10.14",
            "pyright_version": "1.1.411",
            "pyright_package_sha256": PACKAGE_SHA256,
            "node_version": "v22.15.0",
            "node_major": 22,
        },
        "environment": {
            "os": "Linux",
            "os_version": "1",
            "architecture": "test-arch",
            "cpu_model": "test-cpu",
            "cpu_core_count": 8,
            "ram_class": "16-31 GiB",
        },
        "workload": {
            "fixture_seed": 411,
            "fixture_lines": 100_000,
            "definition_queries": 200,
            "reference_queries": 100,
            "call_queries": 100,
            "edit_rename_delete_cycles": 50,
            "crash_cycles": 20,
            "default_limit": 10,
            "max_estimated_tokens": 1200,
            "freshness_checks": 250,
            "ownership_checks": 4,
            "ownership_scenarios": [
                "normal_shutdown",
                "crash",
                "timeout",
                "cancellation",
            ],
        },
        "evidence": {
            "measured": True,
            "runner": "code-navigation-real/v1",
            "source_hash_verified": True,
            "gold_hash_verified": True,
            "git_commit_verified": True,
            "identity_verified": True,
            "query_attempts": 400,
            "mutation_cycles": 50,
            "crash_attempts": 20,
            "ownership_checks": 4,
        },
        "correctness": {
            "definitions": {"attempted": 200, "exact": 198, "accuracy": 0.99},
            "references": {"attempted": 100, **_metric(475, 25, 25)},
            "calls": {"attempted": 100, **_metric(100, 0, 0)},
            "task_success_rate": 0.99,
            "citation_locations_attempted": 800,
            "citation_locations_correct": 800,
            "citation_correctness_rate": 1.0,
        },
        "tokens": {
            "cache_read_label": "not_applicable_no_result_cache",
            "tasks": [
                {
                    "query_id": query_id,
                    "uncached_input_tokens": 10,
                    "cache_read_tokens": 0,
                    "raw_tool_tokens": 20,
                    "output_tokens": 15,
                }
                for query_id in _gold_query_ids()[:396]
            ],
            "default_items": 10,
            "max_default_estimated_tokens": 1200,
        },
        "reliability": {
            "stale_answer_count": 0,
            "freshness_checks_attempted": 250,
            "freshness_checks_measured": 250,
            "stale_result_rate": 0.0,
            "mutation_cycles_measured": 50,
            "edit_to_fresh_p50_ms": 2.0,
            "edit_to_fresh_p95_ms": 4.0,
            "crash_recoveries": 20,
            "crash_attempts": 20,
            "recovery_rate": 1.0,
            "orphan_process_count": 0,
            "orphan_checks_attempted": 4,
            "orphan_checks_measured": 4,
            "orphan_process_rate": 0.0,
            "ownership": {
                scenario: {"available": True, "orphan_count": 0}
                for scenario in (
                    "normal_shutdown",
                    "crash",
                    "timeout",
                    "cancellation",
                )
            },
        },
        "performance": {
            "available": qualification,
            "cold_readiness_seconds": 30.0 if qualification else None,
            "warm_facade_p50_ms": 5.0 if qualification else None,
            "warm_facade_p95_ms": 8.0 if qualification else None,
            "direct_pyright_p95_ms": 3.0 if qualification else None,
            "warm_overhead_p95_ms": 5.0 if qualification else None,
            "sample_count": 20 if qualification else 0,
        },
        "resources": {
            "available": qualification,
            "client_peak_rss_mib": 60.0 if qualification else None,
            "method": "measured-test" if qualification else "not_measured_in_correctness_only",
        },
        "operator_corpus": None,
        "errors": [],
        "market_superiority_claimed": False,
    }


def _set_gate(report: dict[str, object], field: str, value: object) -> None:
    paths = {
        "definition_accuracy": ("correctness", "definitions", "accuracy"),
        "reference_f1": ("correctness", "references", "f1"),
        "stale_answer_count": ("reliability", "stale_answer_count"),
        "stale_result_rate": ("reliability", "stale_result_rate"),
        "orphan_process_count": ("reliability", "orphan_process_count"),
        "orphan_process_rate": ("reliability", "orphan_process_rate"),
        "recovery_rate": ("reliability", "recovery_rate"),
        "default_items": ("tokens", "default_items"),
        "default_estimated_tokens": ("tokens", "max_default_estimated_tokens"),
        "warm_overhead_p95_ms": ("performance", "warm_overhead_p95_ms"),
        "cold_readiness_seconds": ("performance", "cold_readiness_seconds"),
        "client_rss_mib": ("resources", "client_peak_rss_mib"),
    }
    current: dict[str, object] = report
    for key in paths[field][:-1]:
        current = current[key]  # type: ignore[assignment]
    current[paths[field][-1]] = value


def test_gates_require_schema_valid_measured_complete_evidence() -> None:
    report = _measured_report()
    validate_report(report)
    assert evaluate_gates(report)["passed"] is True

    hand_built = {field: threshold for field, threshold in GATE_THRESHOLDS.items()}
    assert evaluate_gates(hand_built)["passed"] is False
    assert evaluate_gates(hand_built)["schema_valid"] is False

    incomplete = copy.deepcopy(report)
    incomplete["evidence"]["query_attempts"] = 399  # type: ignore[index]
    assert evaluate_gates(incomplete)["passed"] is False
    assert evaluate_gates(incomplete)["evidence_complete"] is False

    fake = copy.deepcopy(report)
    fake["evidence"]["measured"] = False  # type: ignore[index]
    assert evaluate_gates(fake)["passed"] is False

    inconsistent_recovery = copy.deepcopy(report)
    inconsistent_recovery["reliability"]["crash_recoveries"] = 19  # type: ignore[index]
    assert evaluate_gates(inconsistent_recovery)["passed"] is False
    assert evaluate_gates(inconsistent_recovery)["evidence_complete"] is False

    inconsistent_metric = copy.deepcopy(report)
    inconsistent_metric["correctness"]["references"]["true_positive"] = 94  # type: ignore[index]
    assert evaluate_gates(inconsistent_metric)["passed"] is False
    assert evaluate_gates(inconsistent_metric)["evidence_complete"] is False

    incomplete_pairing = copy.deepcopy(report)
    incomplete_pairing["performance"]["direct_pyright_p95_ms"] = None  # type: ignore[index]
    assert evaluate_gates(incomplete_pairing)["passed"] is False
    assert evaluate_gates(incomplete_pairing)["evidence_complete"] is False


@pytest.mark.parametrize(
    ("os_name", "python_version"),
    [
        ("Windows", "3.10.14"),
        ("Darwin", "3.10.14"),
        ("Linux", "3.9.19"),
        ("Linux", "3.11.9"),
        ("Linux", "3.10"),
        ("Linux", "3.10.14 (qualification build)"),
    ],
)
def test_qualification_gates_require_linux_and_machine_readable_python_3_10(
    os_name: str,
    python_version: str,
) -> None:
    report = _measured_report()
    report["environment"]["os"] = os_name  # type: ignore[index]
    report["identity"]["python_version"] = python_version  # type: ignore[index]
    validate_report(report)

    evaluation = evaluate_gates(report)

    assert evaluation["schema_valid"] is True
    assert evaluation["evidence_complete"] is False
    assert evaluation["passed"] is False


def test_report_records_machine_readable_platform_python_version(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(benchmark_runner.platform, "python_version", lambda: "3.10.42")

    report = run_fixture_benchmark(
        tmp_path / "python-version-identity",
        state_root=tmp_path / "state",
        mode="correctness-only",
        dependencies=BenchmarkDependencies(
            discover_identity=lambda scope, state_root, deadline: _qualified_identity(),
            runtime_factory=lambda repository, scope, identity, state_root: (
                BehavioralNavigationRuntime(repository, scope)
            ),
        ),
    )

    assert report["identity"]["python_version"] == "3.10.42"


def test_schema_valid_metric_edge_cases_fail_closed_without_raising() -> None:
    zero_reference_domain = _measured_report()
    zero_reference_domain["correctness"]["references"] = {  # type: ignore[index]
        "attempted": 100,
        **_metric(0, 0, 0),
    }
    validate_report(zero_reference_domain)
    evaluation = evaluate_gates(zero_reference_domain)
    assert evaluation["schema_valid"] is True
    assert evaluation["evidence_complete"] is False
    assert evaluation["passed"] is False

    zero_definition_denominator = _measured_report()
    zero_definition_denominator["correctness"]["definitions"] = {  # type: ignore[index]
        "attempted": 0,
        "exact": 0,
        "accuracy": 0.0,
    }
    evaluation = evaluate_gates(zero_definition_denominator)
    assert evaluation["passed"] is False

    wrong_reference_domain = _measured_report()
    wrong_reference_domain["correctness"]["references"] = {  # type: ignore[index]
        "attempted": 100,
        **_metric(474, 26, 25),
    }
    validate_report(wrong_reference_domain)
    evaluation = evaluate_gates(wrong_reference_domain)
    assert evaluation["schema_valid"] is True
    assert evaluation["evidence_complete"] is False

    wrong_call_domain = _measured_report()
    wrong_call_domain["correctness"]["calls"] = {  # type: ignore[index]
        "attempted": 100,
        **_metric(99, 1, 0),
    }
    assert evaluate_gates(wrong_call_domain)["evidence_complete"] is False


@pytest.mark.parametrize(
    ("section", "field", "value"),
    [
        ("correctness", "citation_correctness_rate", float("nan")),
        ("correctness", "task_success_rate", float("nan")),
        ("reliability", "edit_to_fresh_p50_ms", float("nan")),
        ("reliability", "stale_result_rate", float("nan")),
        ("reliability", "edit_to_fresh_p95_ms", float("inf")),
        ("performance", "warm_overhead_p95_ms", float("inf")),
    ],
)
def test_report_rejects_nonfinite_numbers_before_schema_acceptance(
    section: str,
    field: str,
    value: float,
) -> None:
    report = _measured_report()
    report[section][field] = value  # type: ignore[index]
    with pytest.raises(ValueError, match="finite"):
        validate_report(report)
    evaluation = evaluate_gates(report)
    assert evaluation["schema_valid"] is False
    assert evaluation["passed"] is False


def test_token_ids_are_unique_known_gold_queries_and_rates_are_consistent() -> None:
    duplicate = _measured_report()
    duplicate["tokens"]["tasks"][1]["query_id"] = (  # type: ignore[index]
        duplicate["tokens"]["tasks"][0]["query_id"]  # type: ignore[index]
    )
    evaluation = evaluate_gates(duplicate)
    assert evaluation["passed"] is False
    assert evaluation["evidence_complete"] is False

    unknown = _measured_report()
    unknown["tokens"]["tasks"][0]["query_id"] = "unknown-999"  # type: ignore[index]
    evaluation = evaluate_gates(unknown)
    assert evaluation["passed"] is False
    assert evaluation["evidence_complete"] is False

    wrong_stale_rate = _measured_report()
    wrong_stale_rate["reliability"]["stale_result_rate"] = 0.1  # type: ignore[index]
    assert evaluate_gates(wrong_stale_rate)["evidence_complete"] is False

    wrong_orphan_rate = _measured_report()
    wrong_orphan_rate["reliability"]["orphan_process_rate"] = 0.25  # type: ignore[index]
    assert evaluate_gates(wrong_orphan_rate)["evidence_complete"] is False

    wrong_citation_rate = _measured_report()
    wrong_citation_rate["correctness"]["citation_locations_correct"] = 799  # type: ignore[index]
    validate_report(wrong_citation_rate)
    assert evaluate_gates(wrong_citation_rate)["evidence_complete"] is False


@pytest.mark.parametrize(
    ("attempted", "correct"),
    [(795, 795), (800, 795)],
)
def test_solved_tasks_require_their_gold_location_citations(
    attempted: int,
    correct: int,
) -> None:
    report = _measured_report()
    report["correctness"]["citation_locations_attempted"] = attempted  # type: ignore[index]
    report["correctness"]["citation_locations_correct"] = correct  # type: ignore[index]
    report["correctness"]["citation_correctness_rate"] = correct / attempted  # type: ignore[index]
    validate_report(report)

    evaluation = evaluate_gates(report)
    assert evaluation["schema_valid"] is True
    assert evaluation["evidence_complete"] is False
    assert evaluation["passed"] is False


@pytest.mark.parametrize(
    ("mode", "section", "p50_field", "p95_field"),
    [
        (
            "correctness-only",
            "reliability",
            "edit_to_fresh_p50_ms",
            "edit_to_fresh_p95_ms",
        ),
        (
            "qualification",
            "performance",
            "warm_facade_p50_ms",
            "warm_facade_p95_ms",
        ),
    ],
)
def test_percentile_ordering_fails_closed(
    mode: str,
    section: str,
    p50_field: str,
    p95_field: str,
) -> None:
    report = _measured_report(mode)
    report[section][p50_field] = 9.0  # type: ignore[index]
    report[section][p95_field] = 8.0  # type: ignore[index]
    validate_report(report)

    evaluation = evaluate_gates(report)
    assert evaluation["schema_valid"] is True
    assert evaluation["evidence_complete"] is False
    assert evaluation["passed"] is False


def test_present_operator_corpus_must_be_available_and_error_free() -> None:
    report = _measured_report("correctness-only")
    report["operator_corpus"] = {
        "available": False,
        "python_files": 1,
        "queries_attempted": 1,
        "queries_succeeded": 1,
        "errors": 1,
    }
    validate_report(report)

    evaluation = evaluate_gates(report)
    assert evaluation["schema_valid"] is True
    assert evaluation["evidence_complete"] is False
    assert evaluation["passed"] is False


@pytest.mark.parametrize(
    ("field", "failure"),
    [
        ("definition_accuracy", 0.989999),
        ("reference_f1", 0.949999),
        ("stale_answer_count", 1),
        ("stale_result_rate", 0.000001),
        ("orphan_process_count", 1),
        ("orphan_process_rate", 0.000001),
        ("recovery_rate", 0.999999),
        ("default_items", 11),
        ("default_estimated_tokens", 1201),
        ("warm_overhead_p95_ms", 30.000001),
        ("cold_readiness_seconds", 60.000001),
        ("client_rss_mib", 100.0),
    ],
)
def test_every_production_threshold_fails_closed(field: str, failure: object) -> None:
    report = _measured_report()
    _set_gate(report, field, failure)
    evaluation = evaluate_gates(report)
    assert evaluation["gates"][field]["passed"] is False
    assert evaluation["passed"] is False


def test_correctness_mode_has_schema_locked_reduced_gate_scope() -> None:
    report = _measured_report("correctness-only")
    report["environment"]["os"] = "Windows"  # type: ignore[index]
    report["identity"]["python_version"] = "3.12.10 (diagnostic build)"  # type: ignore[index]
    validate_report(report)
    evaluation = evaluate_gates(report)
    assert evaluation["scope"] == "correctness_reliability"
    assert evaluation["passed"] is True
    assert "warm_overhead_p95_ms" not in evaluation["gates"]

    masquerade = copy.deepcopy(report)
    masquerade["performance"]["available"] = True  # type: ignore[index]
    with pytest.raises(ValueError, match="schema"):
        validate_report(masquerade)


def test_report_schema_rejects_unknown_fields_and_unmeasured_qualification() -> None:
    report = _measured_report()
    with pytest.raises(ValueError, match="schema"):
        validate_report({**report, "unknown": True})
    nested_unknown = copy.deepcopy(report)
    nested_unknown["identity"]["unknown"] = True  # type: ignore[index]
    with pytest.raises(ValueError, match="schema"):
        validate_report(nested_unknown)

    unmeasured = copy.deepcopy(report)
    unmeasured["performance"]["available"] = False  # type: ignore[index]
    for field in (
        "cold_readiness_seconds",
        "warm_facade_p50_ms",
        "warm_facade_p95_ms",
        "direct_pyright_p95_ms",
        "warm_overhead_p95_ms",
    ):
        unmeasured["performance"][field] = None  # type: ignore[index]
    unmeasured["performance"]["sample_count"] = 0  # type: ignore[index]
    unmeasured["resources"] = {
        "available": False,
        "client_peak_rss_mib": None,
        "method": "unavailable",
    }
    validate_report(unmeasured)
    evaluation = evaluate_gates(unmeasured)
    assert evaluation["schema_valid"] is True
    assert evaluation["evidence_complete"] is False
    assert evaluation["passed"] is False

    negative_overhead = copy.deepcopy(report)
    negative_overhead["performance"]["warm_overhead_p95_ms"] = -0.25  # type: ignore[index]
    validate_report(negative_overhead)


def test_runtime_schema_validation_has_no_jsonschema_import(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_import = builtins.__import__

    def blocked_import(name: str, *args, **kwargs):
        if name == "jsonschema" or name.startswith("jsonschema."):
            raise ImportError("jsonschema intentionally unavailable")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", blocked_import)
    validate_manifest(load_manifest())
    validate_report(_measured_report())
    repository = generate_qualification_repository(tmp_path / "schema-gold")
    validate_gold(gold_payload(repository))


@pytest.mark.parametrize(
    ("document", "mutate"),
    [
        ("manifest", lambda value: value.__setitem__("fixture_seed", True)),
        ("gold", lambda value: value["queries"][0].__setitem__("line", True)),
        (
            "gold",
            lambda value: value["queries"][0].__setitem__("query_id", "definition-x"),
        ),
        (
            "report",
            lambda value: value["environment"].__setitem__("cpu_core_count", True),
        ),
        (
            "report",
            lambda value: value["reliability"].__setitem__("orphan_process_rate", 1.01),
        ),
        (
            "report",
            lambda value: value["tokens"]["tasks"].append(value["tokens"]["tasks"][0]),
        ),
    ],
)
def test_stdlib_schema_validator_rejects_closed_contract_violations(
    tmp_path: Path,
    document: str,
    mutate,
) -> None:
    repository = generate_qualification_repository(tmp_path / f"strict-{document}")
    values = {
        "manifest": load_manifest(),
        "gold": gold_payload(repository),
        "report": _measured_report(),
    }
    validators = {
        "manifest": validate_manifest,
        "gold": validate_gold,
        "report": validate_report,
    }
    value = copy.deepcopy(values[document])
    mutate(value)
    with pytest.raises(ValueError, match="schema"):
        validators[document](value)


def test_public_schemas_are_valid_draft7_when_optional_validator_is_available() -> None:
    jsonschema = pytest.importorskip("jsonschema")
    for path in (
        benchmark_runner.MANIFEST_SCHEMA,
        benchmark_runner.GOLD_SCHEMA,
        benchmark_runner.REPORT_SCHEMA,
    ):
        jsonschema.Draft7Validator.check_schema(json.loads(path.read_text(encoding="utf-8")))


def test_citation_validation_requires_the_current_source_hash(tmp_path: Path) -> None:
    content = b"def target() -> int:\n    return 1\n"
    source = tmp_path / "module.py"
    source.write_bytes(content)
    start = content.index(b"target")
    location = NavigationLocation(
        "module.py",
        PositionRange(start, start + len(b"target")),
        1,
        len(b"def "),
        None,
        None,
        ResolutionLabel.LSP_CONFIRMED,
        (Provenance("lsp", "pyright", PYRIGHT_VERSION, "provider_reported"),),
    )
    digest = hashlib.sha256(content).hexdigest()
    assert _current_citation(tmp_path, location, expected_sha256=digest)
    assert not _current_citation(tmp_path, location, expected_sha256="0" * 64)


class BehavioralNavigationRuntime:
    """Small semantic fake that returns locations, never metric values."""

    def __init__(
        self,
        repository: QualificationRepository,
        scope: object,
        *,
        mismatch_query_id: str | None = None,
    ) -> None:
        self.repository = repository
        self.scope = scope
        self.mismatch_query_id = mismatch_query_id
        self.calls: list[object] = []
        self._queries = {
            (
                query.capability,
                query.path,
                query.line,
                query.character,
                query.direction,
            ): query
            for query in repository.gold_queries
        }

    @staticmethod
    def _capability_name(capability: Capability) -> str:
        return {
            Capability.DEFINITIONS: "definition",
            Capability.REFERENCES: "references",
            Capability.CALLS: "calls",
        }[capability]

    def _dynamic_query(self, request: object) -> GoldQuery | None:
        path = self.repository.root / request.path
        if not path.is_file():
            return None
        content = path.read_bytes()
        lines = content.splitlines(keepends=True)
        line = lines[request.line - 1]
        tail = line[request.character :]
        symbol = tail.split(b"(", 1)[0].strip().decode("utf-8")
        import_suffix = f" import {symbol}"
        module = next(
            (
                source_line[5 : -len(import_suffix)]
                for source_line in content.decode("utf-8").splitlines()
                if source_line.startswith("from ") and source_line.endswith(import_suffix)
            ),
            None,
        )
        if not module:
            return None
        target_path = f"{module.replace('.', '/')}.py"
        target = self.repository.root / target_path
        expected: tuple[GoldLocation, ...] = ()
        if target.is_file():
            target_content = target.read_bytes()
            marker = f"def {symbol}(".encode()
            byte_start = target_content.find(marker)
            if byte_start < 0:
                return None
            byte_start += len(b"def ")
            prefix = target_content[:byte_start]
            expected = (
                GoldLocation(
                    target_path,
                    prefix.count(b"\n") + 1,
                    byte_start - (prefix.rfind(b"\n") + 1),
                    byte_start,
                    byte_start + len(symbol.encode()),
                    hashlib.sha256(target_content).hexdigest(),
                ),
            )
        digest = hashlib.sha256(content).hexdigest()
        query_start = sum(len(item) for item in lines[: request.line - 1]) + request.character
        return GoldQuery(
            "dynamic",
            "definition",
            request.path,
            request.line,
            request.character,
            request.character,
            query_start,
            query_start + len(symbol.encode()),
            digest,
            symbol,
            None,
            expected,
        )

    def _query_for_request(self, request: object) -> GoldQuery | None:
        key = (
            self._capability_name(request.capability),
            request.path,
            request.line,
            request.character,
            request.direction,
        )
        return self._queries.get(key) or self._dynamic_query(request)

    def _direct_result_for_query(self, query: GoldQuery) -> object:
        locations = tuple(
            LspLocation(
                path_to_file_uri(self.repository.root / location.path),
                LspRange(
                    LspPosition(location.line - 1, location.character),
                    LspPosition(
                        location.line - 1,
                        location.character + location.byte_end - location.byte_start,
                    ),
                ),
            )
            for location in query.expected_locations
        )
        return SimpleNamespace(
            coverage="provider_reported",
            partial=False,
            locations=locations,
        )

    @property
    def position_encoding(self) -> PositionEncoding:
        return PositionEncoding.UTF8

    def query(self, request: object, *, deadline: float) -> NavigationResult:
        assert deadline > time.monotonic()
        self.calls.append(request)
        query = self._query_for_request(request)
        expected = () if query is None else query.expected_locations
        if query is not None and query.query_id == self.mismatch_query_id:
            expected = ()
        provenance = (Provenance("lsp", "pyright", PYRIGHT_VERSION, "provider_reported"),)
        locations = tuple(
            NavigationLocation(
                location.path,
                PositionRange(location.byte_start, location.byte_end),
                location.line,
                location.character,
                None,
                None,
                ResolutionLabel.LSP_CONFIRMED,
                provenance,
            )
            for location in expected
        )
        capability = request.capability
        status = NavigationStatus.OK if query is not None else NavigationStatus.ERROR
        return NavigationResult(
            status,
            capability,
            capability if query is not None else None,
            "pyright",
            PYRIGHT_VERSION,
            self.scope.repository_id,
            self.scope.checkout_id,
            "a" * 64,
            "a" * 64,
            1,
            PositionEncoding.UTF8,
            "query_ready",
            query.symbol if query is not None else None,
            len(locations),
            request.offset,
            request.limit,
            locations,
            (),
            None,
            ResolutionLabel.LSP_CONFIRMED if locations else ResolutionLabel.UNRESOLVED,
            provenance,
            (),
        )

    def direct_query(self, request: object, *, deadline: float) -> object:
        assert deadline > time.monotonic()
        query = self._query_for_request(request)
        assert query is not None
        return self._direct_result_for_query(query)

    def synchronize(self, *, deadline: float) -> None:
        assert deadline > time.monotonic()

    def crash_and_recover(self, request: object, *, deadline: float) -> NavigationResult:
        return self.query(request, deadline=deadline)

    def ownership_checks(self, *, deadline: float) -> dict[str, dict[str, int | bool]]:
        assert deadline > time.monotonic()
        return {
            scenario: {"available": True, "orphan_count": 0}
            for scenario in (
                "normal_shutdown",
                "crash",
                "timeout",
                "cancellation",
            )
        }

    def close(self, *, deadline: float) -> int:
        assert deadline > time.monotonic()
        return 0


@pytest.mark.parametrize("scenario", ["timeout", "cancellation"])
def test_ownership_interruption_rejects_terminal_without_sent_proof(
    monkeypatch: pytest.MonkeyPatch,
    scenario: str,
) -> None:
    terminal = threading.Event()

    class Protocol:
        @staticmethod
        def _sent_request_evidence() -> tuple[int, str | None]:
            return 0, None

    class Process:
        protocol = Protocol()

        @staticmethod
        def request(*args, **kwargs):
            terminal.set()
            if scenario == "timeout":
                raise TimeoutError("terminal without dispatch")
            raise benchmark_runner.RequestCancelled("terminal without dispatch")

    class Runtime(_RealNavigationRuntime):
        def __init__(self) -> None:
            self._cleanup_failed = False
            self.reset_calls = 0

        def _prepare_process(self, deadline: float):
            return object(), Process()

        def _reset(self, deadline: float) -> int:
            assert terminal.is_set()
            self.reset_calls += 1
            return 0

    monkeypatch.setattr(benchmark_runner, "_OWNERSHIP_SCENARIOS", (scenario,))
    runtime = Runtime()

    outcomes = runtime.ownership_checks(deadline=time.monotonic() + 2.0)

    assert outcomes == {scenario: {"available": False, "orphan_count": None}}
    assert runtime.reset_calls == 1


@pytest.mark.parametrize("scenario", ["timeout", "cancellation"])
def test_ownership_interruption_dispatches_before_terminal_and_then_cleans_up(
    monkeypatch: pytest.MonkeyPatch,
    scenario: str,
) -> None:
    sent = threading.Event()
    terminal = threading.Event()
    events: list[str] = []

    class Protocol:
        @staticmethod
        def _sent_request_evidence() -> tuple[int, str | None]:
            return (1, "workspace/symbol") if sent.is_set() else (0, None)

    class Process:
        protocol = Protocol()

        @staticmethod
        def request(
            method: str,
            params: object,
            *,
            deadline: float,
            cancellation=None,
        ):
            assert method == "workspace/symbol"
            if params == {"query": ""}:
                sent.set()
                events.append("empty-completed")
                return []
            assert params == {"query": "__llm_wiki_ownership_probe_no_match__"}
            if deadline <= time.monotonic() or (
                cancellation is not None and cancellation.is_cancelled()
            ):
                terminal.set()
                if scenario == "timeout":
                    raise TimeoutError("rejected before dispatch")
                raise benchmark_runner.RequestCancelled("rejected before dispatch")
            sent.set()
            events.append("sent")
            if scenario == "cancellation":
                assert cancellation is not None
                assert cancellation.wait(max(0.0, deadline - time.monotonic()))
                terminal.set()
                events.append("terminal")
                raise benchmark_runner.RequestCancelled("cancelled after dispatch")
            while time.monotonic() < deadline:
                time.sleep(0.001)
            terminal.set()
            events.append("terminal")
            raise TimeoutError("timed out after dispatch")

    class Runtime(_RealNavigationRuntime):
        def __init__(self) -> None:
            self._cleanup_failed = False

        def _prepare_process(self, deadline: float):
            return object(), Process()

        def _reset(self, deadline: float) -> int:
            assert terminal.is_set()
            events.append("cleanup")
            return 0

    monkeypatch.setattr(benchmark_runner, "_OWNERSHIP_SCENARIOS", (scenario,))

    outcomes = Runtime().ownership_checks(deadline=time.monotonic() + 2.0)

    assert outcomes == {scenario: {"available": True, "orphan_count": 0}}
    assert sent.is_set()
    assert events == ["sent", "terminal", "cleanup"]


def test_ownership_waits_for_post_write_evidence_after_fast_completion() -> None:
    evidence_visible = threading.Event()
    events: list[str] = []
    timers: list[threading.Timer] = []

    class Protocol:
        @staticmethod
        def _sent_request_evidence() -> tuple[int, str | None]:
            return (
                (1, "workspace/symbol")
                if evidence_visible.is_set()
                else (0, None)
            )

    class Process:
        protocol = Protocol()
        cancellation = None

        @classmethod
        def request(cls, *args, cancellation=None, **kwargs):
            cls.cancellation = cancellation
            events.append("completed")

            def publish_evidence() -> None:
                assert cancellation is not None
                assert cancellation.is_cancelled() is False
                events.append("evidence")
                evidence_visible.set()

            timer = threading.Timer(0.02, publish_evidence)
            timers.append(timer)
            timer.start()
            return []

    runtime = object.__new__(_RealNavigationRuntime)
    runtime._cleanup_failed = False
    try:
        with pytest.raises(RuntimeError, match="expected terminal"):
            runtime._observe_inflight_interruption(
                Process(),
                "cancellation",
                time.monotonic() + 1,
            )
    finally:
        for timer in timers:
            timer.join()

    assert evidence_visible.is_set()
    assert Process.cancellation is not None
    assert Process.cancellation.is_cancelled() is True
    assert events == ["completed", "evidence"]


def test_real_runtime_measures_four_active_ownership_scenarios() -> None:
    class ActivePopen:
        def __init__(self, runtime: ActiveOwnershipRuntime) -> None:
            self.runtime = runtime

        def kill(self) -> None:
            assert self.runtime.active
            self.runtime.active = False

        def wait(self, *, timeout: float) -> int:
            raise AssertionError("the lifecycle owner must reap the process")

    class ActiveProtocol:
        def __init__(self, runtime: ActiveOwnershipRuntime) -> None:
            self.runtime = runtime

        def _sent_request_evidence(self) -> tuple[int, str | None]:
            return (
                (1, "workspace/symbol")
                if self.runtime.probe_sent
                else (0, None)
            )

    class ActiveProcess:
        def __init__(self, runtime: ActiveOwnershipRuntime) -> None:
            self.runtime = runtime
            self.process = ActivePopen(runtime)
            self.protocol = ActiveProtocol(runtime)

        def request(self, *args, deadline: float, cancellation=None, **kwargs):
            self.runtime.probe_sent = True
            if self.runtime.prepare_count == 4:
                assert cancellation is not None
                assert cancellation.wait(max(0.0, deadline - time.monotonic()))
                self.runtime.probe_terminal = True
                raise benchmark_runner.RequestCancelled("measured cancellation")
            while time.monotonic() < deadline:
                time.sleep(0.001)
            self.runtime.probe_terminal = True
            raise TimeoutError("measured timeout")

    class ActiveOwnershipRuntime(_RealNavigationRuntime):
        def __init__(self) -> None:
            self.active = False
            self.prepare_count = 0
            self.active_close_count = 0
            self.lazy_open_count = 0
            self.probe_sent = False
            self.probe_terminal = False
            self.process = ActiveProcess(self)

        def _prepare_process(self, deadline: float):
            assert deadline > time.monotonic()
            assert not self.active
            self.active = True
            self.prepare_count += 1
            self.probe_sent = False
            self.probe_terminal = False
            return object(), self.process

        def query(self, request: object, *, deadline: float) -> None:
            assert deadline > time.monotonic()
            self.active = True

        def _close_and_count(self, deadline: float) -> int:
            assert deadline > time.monotonic()
            assert self.active
            if self.prepare_count in {3, 4}:
                assert self.probe_terminal
            self.active = False
            self.active_close_count += 1
            return 0

        def _open(self) -> None:
            assert not self.active
            self.lazy_open_count += 1

    runtime = ActiveOwnershipRuntime()
    outcomes = runtime.ownership_checks(deadline=time.monotonic() + 10.0)

    assert tuple(outcomes) == (
        "normal_shutdown",
        "crash",
        "timeout",
        "cancellation",
    )
    assert all(outcome == {"available": True, "orphan_count": 0} for outcome in outcomes.values())
    assert runtime.prepare_count == 4
    assert runtime.active_close_count == 4
    assert runtime.lazy_open_count == 4
    assert runtime.active is False


@pytest.mark.parametrize(
    "first_close",
    [RuntimeError("first close failed"), 1],
    ids=["raises", "orphan-signal"],
)
def test_reset_retries_cleanup_once_and_never_reopens_after_failed_proof(
    first_close: object,
) -> None:
    class CleanupRuntime(_RealNavigationRuntime):
        def __init__(self) -> None:
            self.outcomes = [first_close, 0]
            self.close_deadlines: list[float] = []
            self.open_count = 0
            self._cleanup_failed = False

        def _close_and_count(self, deadline: float) -> int:
            self.close_deadlines.append(deadline)
            outcome = self.outcomes.pop(0)
            if isinstance(outcome, BaseException):
                raise outcome
            return outcome

        def _open(self) -> None:
            self.open_count += 1

    runtime = CleanupRuntime()
    before = time.monotonic()
    with pytest.raises(RuntimeError, match="cleanup"):
        runtime._reset(before - 1.0)

    assert len(runtime.close_deadlines) == 2
    assert runtime.close_deadlines[1] > before
    assert runtime.open_count == 0
    assert runtime.cleanup_failed is True


def test_reset_retry_failure_retains_terminal_unknown_cleanup() -> None:
    class CleanupRuntime(_RealNavigationRuntime):
        def __init__(self) -> None:
            self.close_count = 0
            self.open_count = 0
            self._cleanup_failed = False

        def _close_and_count(self, deadline: float) -> int:
            self.close_count += 1
            raise RuntimeError("cleanup remains unproven")

        def _open(self) -> None:
            self.open_count += 1

    runtime = CleanupRuntime()
    with pytest.raises(RuntimeError, match="cleanup"):
        runtime._reset(time.monotonic() - 1.0)

    assert runtime.close_count == 2
    assert runtime.open_count == 0
    assert runtime.cleanup_failed is True


def test_ownership_cleanup_failure_stops_all_subsequent_scenarios() -> None:
    class Process:
        process = SimpleNamespace(kill=lambda: None, wait=lambda timeout: 0)

        @staticmethod
        def request(*args, **kwargs):
            raise TimeoutError("expected")

    class CleanupRuntime(_RealNavigationRuntime):
        def __init__(self) -> None:
            self.prepare_count = 0
            self.open_count = 0
            self.outcomes = [1, 0]
            self._cleanup_failed = False

        def _prepare_process(self, deadline: float):
            self.prepare_count += 1
            return object(), Process()

        def _close_and_count(self, deadline: float) -> int:
            return self.outcomes.pop(0)

        def _open(self) -> None:
            self.open_count += 1

    runtime = CleanupRuntime()
    outcomes = runtime.ownership_checks(deadline=time.monotonic() + 10.0)

    assert runtime.prepare_count == 1
    assert runtime.open_count == 0
    assert runtime.cleanup_failed is True
    assert all(
        outcome == {"available": False, "orphan_count": None}
        for outcome in outcomes.values()
    )


def test_close_counts_one_binary_incident_for_one_leaked_session(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner_root = tmp_path / "owner"
    owner_root.mkdir()
    (owner_root / "lease.json").write_text("{}", encoding="utf-8")
    popen = SimpleNamespace(poll=lambda: None)
    process = SimpleNamespace(process=popen, _coordinator=object(), owner_root=owner_root)
    runtime = object.__new__(_RealNavigationRuntime)
    runtime._session = SimpleNamespace(_process=process)
    runtime._navigation = SimpleNamespace(close=lambda deadline: None)
    monkeypatch.setattr(benchmark_runner, "_coordinator_has_ownership", lambda value: True)

    assert runtime._close_and_count(time.monotonic() + 10.0) == 1


def test_crash_without_a_process_returns_none() -> None:
    class NoProcessRuntime(_RealNavigationRuntime):
        def __init__(self) -> None:
            self._session = SimpleNamespace(_process=None)

        def query(self, request: object, *, deadline: float) -> None:
            return None

    assert (
        NoProcessRuntime().crash_and_recover(object(), deadline=time.monotonic() + 10.0)
        is None
    )


def test_post_crash_transient_not_ready_retries_once_under_the_same_deadline() -> None:
    events: list[str] = []
    deadlines: list[float] = []
    request = SimpleNamespace(
        capability=Capability.DEFINITIONS,
        repository=SimpleNamespace(repository_id="repository", checkout_id="checkout"),
        offset=0,
        limit=10,
    )
    recovered = replace(
        _empty_navigation_result(request),
        status=NavigationStatus.PARTIAL,
        readiness="query_ready",
    )
    transient = replace(
        _empty_navigation_result(request),
        status=NavigationStatus.NOT_READY,
        readiness="not_ready",
    )

    class Popen:
        def kill(self) -> None:
            events.append("kill")

        def wait(self, *, timeout: float) -> int:
            raise AssertionError("the lifecycle owner must reap the process")

    class CrashRuntime(_RealNavigationRuntime):
        def __init__(self) -> None:
            self._cleanup_failed = False
            self._session = SimpleNamespace(_process=SimpleNamespace(process=Popen()))
            self.query_results = [recovered, transient, recovered]

        def query(self, query_request: object, *, deadline: float) -> NavigationResult:
            assert query_request is request
            events.append("query")
            deadlines.append(deadline)
            return self.query_results.pop(0)

        def _reset(self, deadline: float) -> int:
            events.append("reset")
            return 0

    deadline = time.monotonic() + 10.0
    result = CrashRuntime().crash_and_recover(request, deadline=deadline)

    assert result is recovered
    assert events == ["query", "kill", "query", "query", "reset"]
    assert deadlines == [deadline, deadline, deadline]


def test_post_crash_transient_oserror_retries_once_under_the_same_deadline() -> None:
    events: list[str] = []
    deadlines: list[float] = []
    recovered = object()

    class Popen:
        def kill(self) -> None:
            events.append("kill")

        def wait(self, *, timeout: float) -> int:
            raise AssertionError("the lifecycle owner must reap the process")

    class CrashRuntime(_RealNavigationRuntime):
        def __init__(self) -> None:
            self._cleanup_failed = False
            self._session = SimpleNamespace(_process=SimpleNamespace(process=Popen()))
            self.query_calls = 0

        def query(self, request: object, *, deadline: float):
            self.query_calls += 1
            events.append(f"query-{self.query_calls}")
            deadlines.append(deadline)
            if self.query_calls == 2:
                raise OSError("lifecycle reaped the crashed process")
            return recovered

        def _reset(self, deadline: float) -> int:
            events.append("reset")
            return 0

    deadline = time.monotonic() + 10.0
    result = CrashRuntime().crash_and_recover(object(), deadline=deadline)

    assert result is recovered
    assert events == ["query-1", "kill", "query-2", "query-3", "reset"]
    assert deadlines == [deadline, deadline, deadline]


def test_post_crash_second_oserror_fails_closed_after_one_retry() -> None:
    events: list[str] = []

    class Popen:
        def kill(self) -> None:
            events.append("kill")

        def wait(self, *, timeout: float) -> int:
            raise AssertionError("the lifecycle owner must reap the process")

    class CrashRuntime(_RealNavigationRuntime):
        def __init__(self) -> None:
            self._cleanup_failed = False
            self._session = SimpleNamespace(_process=SimpleNamespace(process=Popen()))
            self.query_calls = 0

        def query(self, request: object, *, deadline: float):
            self.query_calls += 1
            events.append(f"query-{self.query_calls}")
            if self.query_calls > 1:
                raise OSError(f"recovery failure {self.query_calls - 1}")
            return object()

        def _reset(self, deadline: float) -> int:
            events.append("reset")
            return 0

    runtime = CrashRuntime()
    with pytest.raises(OSError, match="recovery failure 2"):
        runtime.crash_and_recover(object(), deadline=time.monotonic() + 10.0)

    assert runtime.query_calls == 3
    assert events == ["query-1", "kill", "query-2", "query-3", "reset"]


def test_post_crash_not_ready_retries_until_provider_recovers() -> None:
    request = SimpleNamespace(
        capability=Capability.DEFINITIONS,
        repository=SimpleNamespace(repository_id="repository", checkout_id="checkout"),
        offset=0,
        limit=10,
    )
    baseline = _empty_navigation_result(request)
    transient = replace(
        baseline,
        status=NavigationStatus.NOT_READY,
        readiness="not_ready",
    )
    initializing = replace(
        baseline,
        status=NavigationStatus.NOT_READY,
        readiness="protocol_initialized",
    )
    recovered = replace(
        baseline,
        status=NavigationStatus.PARTIAL,
        readiness="query_ready",
    )

    class Popen:
        def kill(self) -> None:
            return None

        def wait(self, *, timeout: float) -> int:
            raise AssertionError("the lifecycle owner must reap the process")

    class CrashRuntime(_RealNavigationRuntime):
        def __init__(self) -> None:
            self._cleanup_failed = False
            self._session = SimpleNamespace(_process=SimpleNamespace(process=Popen()))
            self.query_results = [
                recovered,
                transient,
                transient,
                initializing,
                transient,
                recovered,
            ]
            self.deadlines: list[float] = []
            self.reset_calls = 0

        def query(self, query_request: object, *, deadline: float) -> NavigationResult:
            self.deadlines.append(deadline)
            return self.query_results.pop(0)

        def _reset(self, deadline: float) -> int:
            self.reset_calls += 1
            return 0

    runtime = CrashRuntime()
    deadline = time.monotonic() + 10.0
    result = runtime.crash_and_recover(request, deadline=deadline)

    assert result is recovered
    assert runtime.query_results == []
    assert runtime.deadlines == [deadline] * 6
    assert runtime.reset_calls == 1


def test_post_crash_oserror_after_not_ready_uses_one_retry_budget() -> None:
    request = SimpleNamespace(
        capability=Capability.DEFINITIONS,
        repository=SimpleNamespace(repository_id="repository", checkout_id="checkout"),
        offset=0,
        limit=10,
    )
    baseline = _empty_navigation_result(request)
    transient = replace(
        baseline,
        status=NavigationStatus.NOT_READY,
        readiness="not_ready",
    )
    recovered = replace(
        baseline,
        status=NavigationStatus.PARTIAL,
        readiness="query_ready",
    )

    class Popen:
        def kill(self) -> None:
            return None

    class CrashRuntime(_RealNavigationRuntime):
        def __init__(self) -> None:
            self._cleanup_failed = False
            self._session = SimpleNamespace(_process=SimpleNamespace(process=Popen()))
            self.query_outcomes = [recovered, transient, OSError("reaped"), recovered]
            self.deadlines: list[float] = []
            self.reset_calls = 0

        def query(self, query_request: object, *, deadline: float) -> NavigationResult:
            self.deadlines.append(deadline)
            outcome = self.query_outcomes.pop(0)
            if isinstance(outcome, BaseException):
                raise outcome
            return outcome

        def _reset(self, deadline: float) -> int:
            self.reset_calls += 1
            return 0

    runtime = CrashRuntime()
    deadline = time.monotonic() + 10.0

    result = runtime.crash_and_recover(request, deadline=deadline)

    assert result is recovered
    assert runtime.query_outcomes == []
    assert runtime.deadlines == [deadline] * 4
    assert runtime.reset_calls == 1


def test_post_crash_not_ready_stops_at_deadline_and_resets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = SimpleNamespace(
        capability=Capability.DEFINITIONS,
        repository=SimpleNamespace(repository_id="repository", checkout_id="checkout"),
        offset=0,
        limit=10,
    )
    baseline = _empty_navigation_result(request)
    transient = replace(
        baseline,
        status=NavigationStatus.NOT_READY,
        readiness="not_ready",
    )
    recovered = replace(
        baseline,
        status=NavigationStatus.PARTIAL,
        readiness="query_ready",
    )

    class Popen:
        def kill(self) -> None:
            return None

    class CrashRuntime(_RealNavigationRuntime):
        def __init__(self) -> None:
            self._cleanup_failed = False
            self._session = SimpleNamespace(_process=SimpleNamespace(process=Popen()))
            self.query_results = [baseline, transient, recovered]
            self.reset_deadlines: list[float] = []

        def query(self, query_request: object, *, deadline: float) -> NavigationResult:
            return self.query_results.pop(0)

        def _reset(self, deadline: float) -> int:
            self.reset_deadlines.append(deadline)
            return 0

    deadline = 10.0
    monkeypatch.setattr(benchmark_runner.time, "monotonic", lambda: deadline)
    runtime = CrashRuntime()

    result = runtime.crash_and_recover(request, deadline=deadline)

    assert result is transient
    assert runtime.query_results == [recovered]
    assert runtime.reset_deadlines == [deadline]


def test_post_crash_recovery_error_always_resets_and_later_cycles_continue() -> None:
    events: list[str] = []
    recovered = object()

    class Popen:
        def kill(self) -> None:
            events.append("kill")

        def wait(self, *, timeout: float) -> int:
            raise AssertionError("the lifecycle owner must reap the process")

    class CrashRuntime(_RealNavigationRuntime):
        def __init__(self) -> None:
            self._cleanup_failed = False
            self._session = SimpleNamespace(_process=SimpleNamespace(process=Popen()))
            self.query_calls = 0
            self.reset_calls = 0

        def query(self, request: object, *, deadline: float):
            self.query_calls += 1
            events.append(f"query-{self.query_calls}")
            if self.query_calls == 2:
                raise RuntimeError("recovery query failed")
            return recovered

        def _reset(self, deadline: float) -> int:
            self.reset_calls += 1
            events.append(f"reset-{self.reset_calls}")
            return 0

    runtime = CrashRuntime()
    deadline = time.monotonic() + 10.0
    with pytest.raises(RuntimeError, match="recovery query failed"):
        runtime.crash_and_recover(object(), deadline=deadline)

    assert runtime.reset_calls == 1
    assert events[:4] == ["query-1", "kill", "query-2", "reset-1"]
    assert runtime.cleanup_failed is False
    assert runtime.crash_and_recover(object(), deadline=deadline) is recovered
    assert runtime.reset_calls == 2


def test_post_crash_kill_error_cannot_bypass_reset() -> None:
    class Popen:
        def kill(self) -> None:
            raise OSError("intentional kill failed")

        def wait(self, *, timeout: float) -> int:
            raise AssertionError("the lifecycle owner must reap the process")

    class CrashRuntime(_RealNavigationRuntime):
        def __init__(self) -> None:
            self._cleanup_failed = False
            self._session = SimpleNamespace(_process=SimpleNamespace(process=Popen()))
            self.reset_calls = 0

        def query(self, request: object, *, deadline: float):
            return object()

        def _reset(self, deadline: float) -> int:
            self.reset_calls += 1
            return 0

    runtime = CrashRuntime()
    with pytest.raises(OSError, match="intentional kill failed"):
        runtime.crash_and_recover(object(), deadline=time.monotonic() + 10.0)
    assert runtime.reset_calls == 1


def test_post_crash_recovery_and_reset_failure_retries_then_becomes_terminal() -> None:
    class Popen:
        def kill(self) -> None:
            return None

        def wait(self, *, timeout: float) -> int:
            raise AssertionError("the lifecycle owner must reap the process")

    class CrashRuntime(_RealNavigationRuntime):
        def __init__(self) -> None:
            self._cleanup_failed = False
            self._session = SimpleNamespace(_process=SimpleNamespace(process=Popen()))
            self.query_calls = 0
            self.close_outcomes = [RuntimeError("first cleanup failed"), 0]
            self.close_deadlines: list[float] = []
            self.open_count = 0

        def query(self, request: object, *, deadline: float):
            self.query_calls += 1
            if self.query_calls == 2:
                raise RuntimeError("recovery query failed")
            return object()

        def _close_and_count(self, deadline: float) -> int:
            self.close_deadlines.append(deadline)
            outcome = self.close_outcomes.pop(0)
            if isinstance(outcome, BaseException):
                raise outcome
            return outcome

        def _open(self) -> None:
            self.open_count += 1

    runtime = CrashRuntime()
    operation_deadline = time.monotonic() + 1.0
    with pytest.raises(RuntimeError, match="cleanup"):
        runtime.crash_and_recover(object(), deadline=operation_deadline)

    assert len(runtime.close_deadlines) == 2
    assert runtime.close_deadlines[1] > operation_deadline
    assert runtime.open_count == 0
    assert runtime.cleanup_failed is True
    calls = runtime.query_calls
    assert runtime.crash_and_recover(object(), deadline=time.monotonic() + 10.0) is None
    assert runtime.query_calls == calls


def _qualified_identity() -> SimpleNamespace:
    return SimpleNamespace(
        status="qualified",
        qualified=True,
        version=PYRIGHT_VERSION,
        package_sha256=PACKAGE_SHA256,
        node_version="v22.15.0",
        node_major=22,
        degradation_codes=(),
    )


def _empty_navigation_result(request: object) -> NavigationResult:
    return NavigationResult(
        NavigationStatus.ERROR,
        request.capability,
        None,
        "pyright",
        PYRIGHT_VERSION,
        request.repository.repository_id,
        request.repository.checkout_id,
        "a" * 64,
        "a" * 64,
        None,
        PositionEncoding.UTF8,
        "query_ready",
        None,
        0,
        request.offset,
        request.limit,
        (),
        (),
        None,
        ResolutionLabel.UNRESOLVED,
        (),
        (),
    )


def _result_with_locations(
    request: object,
    status: NavigationStatus,
    locations: tuple[NavigationLocation, ...],
) -> NavigationResult:
    provenance = (Provenance("lsp", "pyright", PYRIGHT_VERSION, "provider_reported"),)
    return replace(
        _empty_navigation_result(request),
        status=status,
        effective_capability=request.capability,
        symbol="measured",
        total=len(locations),
        locations=locations,
        resolution=(ResolutionLabel.LSP_CONFIRMED if locations else ResolutionLabel.UNRESOLVED),
        provenance=provenance,
    )


@pytest.mark.parametrize("partial_absence_call", [4, 5], ids=["rename-old", "delete"])
def test_partial_empty_mutation_absence_is_stale_but_positive_partial_is_valid(
    tmp_path: Path,
    partial_absence_call: int,
) -> None:
    repository = generate_qualification_repository(tmp_path / f"partial-{partial_absence_call}")
    repository = replace(repository, workloads=repository.workloads[:1])
    initialize_deterministic_git(repository.root)
    scope = resolve_repository_scope(repository.root)

    class PartialRuntime:
        def __init__(self) -> None:
            self.calls = 0

        def synchronize(self, *, deadline: float) -> None:
            assert deadline > time.monotonic()

        def query(self, request: object, *, deadline: float) -> NavigationResult:
            assert deadline > time.monotonic()
            self.calls += 1
            probe = repository.root / request.path
            import_line = probe.read_text(encoding="utf-8").splitlines()[0].split()
            module = import_line[1]
            symbol = import_line[3]
            target = repository.root / f"{module.replace('.', '/')}.py"
            locations: tuple[NavigationLocation, ...] = ()
            if target.is_file():
                content = target.read_bytes()
                byte_start = content.index(f"def {symbol}(".encode()) + len(b"def ")
                prefix = content[:byte_start]
                locations = (
                    NavigationLocation(
                        target.relative_to(repository.root).as_posix(),
                        PositionRange(byte_start, byte_start + len(symbol.encode())),
                        prefix.count(b"\n") + 1,
                        byte_start - (prefix.rfind(b"\n") + 1),
                        None,
                        None,
                        ResolutionLabel.LSP_CONFIRMED,
                        (Provenance("lsp", "pyright", PYRIGHT_VERSION, "provider_reported"),),
                    ),
                )
            status = (
                NavigationStatus.PARTIAL
                if locations or self.calls == partial_absence_call
                else NavigationStatus.OK
            )
            return _result_with_locations(request, status, locations)

    errors: list[dict[str, str]] = []
    stale, cycles, measured, latencies = _mutate_and_measure(
        PartialRuntime(),
        repository,
        scope,
        errors,
    )

    assert stale == 1
    assert cycles == 1
    assert measured == 5
    assert len(latencies) == 4
    assert errors == []


def test_mutation_checks_use_stable_probes_and_error_empty_never_passes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = generate_qualification_repository(tmp_path / "mutation-repository")
    repository = replace(repository, workloads=repository.workloads[:1])
    initialize_deterministic_git(repository.root)
    scope = resolve_repository_scope(repository.root)

    class ErrorRuntime:
        def __init__(self) -> None:
            self.calls: list[object] = []

        def synchronize(self, *, deadline: float) -> None:
            assert deadline > time.monotonic()

        def query(self, request: object, *, deadline: float) -> NavigationResult:
            assert deadline > time.monotonic()
            self.calls.append(request)
            return _empty_navigation_result(request)

    runtime = ErrorRuntime()
    errors: list[dict[str, str]] = []
    mutation_queries: list[GoldQuery] = []
    real_navigation_request = _navigation_request

    def capture_query(query: GoldQuery, query_scope: object) -> object:
        mutation_queries.append(query)
        return real_navigation_request(query, query_scope)

    monkeypatch.setattr(benchmark_runner, "_navigation_request", capture_query)
    stale, cycles, checks_measured, latencies = _mutate_and_measure(
        runtime,
        repository,
        scope,
        errors,
    )

    workload = repository.workloads[0]
    assert len(runtime.calls) == 5
    assert {request.path for request in runtime.calls} == {workload.probe_path}
    assert [query.query_id for query in mutation_queries] == [
        "mutation-000-create",
        "mutation-000-edit",
        "mutation-000-rename-new",
        "mutation-000-rename-old",
        "mutation-000-delete",
    ]
    assert [len(query.expected_locations) for query in mutation_queries] == [
        1,
        1,
        1,
        0,
        0,
    ]
    assert [
        query.expected_locations[0].path for query in mutation_queries if query.expected_locations
    ] == [
        workload.created_path,
        workload.original_path,
        workload.renamed_path,
    ]
    assert stale == 5
    assert cycles == 1
    assert checks_measured == 5
    assert latencies == []
    assert errors == []


def test_mutation_measurement_uses_the_facade_as_its_only_synchronization_path(
    tmp_path: Path,
) -> None:
    repository = generate_qualification_repository(tmp_path / "facade-sync-repository")
    repository = replace(repository, workloads=repository.workloads[:1])
    initialize_deterministic_git(repository.root)
    scope = resolve_repository_scope(repository.root)
    delegate = BehavioralNavigationRuntime(repository, scope)

    class QueryOnlyRuntime:
        def query(self, request: object, *, deadline: float) -> NavigationResult:
            return delegate.query(request, deadline=deadline)

    errors: list[dict[str, str]] = []
    stale, cycles, checks_measured, latencies = _mutate_and_measure(
        QueryOnlyRuntime(),
        repository,
        scope,
        errors,
    )

    assert stale == 0
    assert cycles == 1
    assert checks_measured == 5
    assert len(latencies) == 5
    assert errors == []


def test_all_mutation_cycles_reset_shared_paths_before_all_250_checks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = generate_qualification_repository(tmp_path / "shared-mutation-repository")
    initialize_deterministic_git(repository.root)
    scope = resolve_repository_scope(repository.root)
    first = repository.workloads[0]
    original = repository.root / first.original_path
    renamed = repository.root / first.renamed_path
    created = repository.root / first.created_path
    probe = repository.root / first.probe_path
    original.write_bytes(b"# stale original\n")
    renamed.write_bytes(b"# stale renamed\n")
    created.write_bytes(b"# stale created\n")
    probe.write_bytes(b"# stale probe\n")

    originals = {workload.original_content: workload.workload_id for workload in repository.workloads}
    probes = {
        workload.baseline_probe_content: workload.workload_id
        for workload in repository.workloads
    }
    original_resets: list[str] = []
    probe_resets: list[str] = []
    real_write_bytes = Path.write_bytes

    def record_reset(path: Path, data: bytes) -> int:
        if path == original and data in originals:
            original_resets.append(originals[data])
        if path == probe and data in probes:
            probe_resets.append(probes[data])
        return real_write_bytes(path, data)

    monkeypatch.setattr(Path, "write_bytes", record_reset)
    runtime = BehavioralNavigationRuntime(repository, scope)
    errors: list[dict[str, str]] = []
    stale, cycles, checks_measured, latencies = _mutate_and_measure(
        runtime,
        repository,
        scope,
        errors,
    )

    expected_order = [workload.workload_id for workload in repository.workloads]
    assert original_resets == expected_order
    assert probe_resets == expected_order
    assert stale == 0
    assert cycles == 50
    assert checks_measured == 250
    assert len(runtime.calls) == 250
    assert len(latencies) == 250
    assert errors == []


def test_mutation_reset_failure_is_recorded_and_skips_the_cycle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = generate_qualification_repository(tmp_path / "failed-reset-repository")
    repository = replace(repository, workloads=repository.workloads[:1])
    initialize_deterministic_git(repository.root)
    scope = resolve_repository_scope(repository.root)
    workload = repository.workloads[0]
    probe = repository.root / workload.probe_path
    real_write_bytes = Path.write_bytes

    def fail_probe_reset(path: Path, data: bytes) -> int:
        if path == probe and data == workload.baseline_probe_content:
            raise OSError("injected reset failure")
        return real_write_bytes(path, data)

    monkeypatch.setattr(Path, "write_bytes", fail_probe_reset)
    runtime = BehavioralNavigationRuntime(repository, scope)
    errors: list[dict[str, str]] = []
    _stale, cycles, checks_measured, latencies = _mutate_and_measure(
        runtime,
        repository,
        scope,
        errors,
    )

    assert cycles == 0
    assert checks_measured == 0
    assert runtime.calls == []
    assert latencies == []
    assert errors == [{"phase": "mutation_reset", "code": "OSError"}]


def test_raw_tool_tokens_cover_the_complete_normalized_result(tmp_path: Path) -> None:
    repository = generate_qualification_repository(tmp_path / "token-repository")
    initialize_deterministic_git(repository.root)
    scope = resolve_repository_scope(repository.root)
    query = repository.gold_queries[0]
    request = _navigation_request(query, scope)
    runtime = BehavioralNavigationRuntime(repository, scope)
    result = runtime.query(request, deadline=time.monotonic() + 10.0)
    provenance = (Provenance("lsp", "pyright", PYRIGHT_VERSION, "provider_reported"),)
    location = replace(
        result.locations[0],
        containing_symbol="QualifiedContainer",
        signature="def target(value: int) -> int",
        provenance=provenance,
    )
    result = replace(
        result,
        readiness="query_ready_with_complete_normalized_metadata",
        symbol=query.symbol,
        locations=(location,),
        hover="fully normalized hover metadata",
        provenance=provenance,
        warnings=("complete-normalized-warning",),
    )
    rendered = render_navigation(result)
    expected_raw = {
        "status": result.status.value,
        "requested_capability": result.requested_capability.value,
        "effective_capability": result.effective_capability.value,
        "provider": result.provider,
        "provider_version": result.provider_version,
        "repository_id": result.repository_id,
        "checkout_id": result.checkout_id,
        "workspace_revision_before": result.workspace_revision_before,
        "workspace_revision_after": result.workspace_revision_after,
        "document_version": result.document_version,
        "position_encoding": result.position_encoding.value,
        "readiness": result.readiness,
        "symbol": result.symbol,
        "total": result.total,
        "offset": result.offset,
        "limit": result.limit,
        "locations": [
            {
                "path": location.path,
                "range": {
                    "byte_start": location.range.byte_start,
                    "byte_end": location.range.byte_end,
                },
                "line": location.line,
                "character": location.character,
                "containing_symbol": location.containing_symbol,
                "signature": location.signature,
                "resolution": location.resolution.value,
                "provenance": [
                    {
                        "source": item.source,
                        "provider": item.provider,
                        "version": item.version,
                        "observation": item.observation,
                    }
                    for item in location.provenance
                ],
            }
        ],
        "diagnostics": [],
        "hover": result.hover,
        "resolution": result.resolution.value,
        "provenance": [
            {
                "source": item.source,
                "provider": item.provider,
                "version": item.version,
                "observation": item.observation,
            }
            for item in result.provenance
        ],
        "warnings": list(result.warnings),
    }
    record = _token_record(query, request, result, rendered)
    assert record["raw_tool_tokens"] == estimate_tokens(
        benchmark_runner._canonical_json(expected_raw)
    )


def test_runner_queries_every_gold_item_and_metrics_follow_actual_results(tmp_path: Path) -> None:
    runtimes: list[BehavioralNavigationRuntime] = []

    def runtime_factory(repository, scope, identity, state_root):
        assert identity.qualified
        assert state_root == tmp_path / "state"
        runtime = BehavioralNavigationRuntime(repository, scope, mismatch_query_id="definition-000")
        runtimes.append(runtime)
        return runtime

    report = run_fixture_benchmark(
        tmp_path / "work",
        state_root=tmp_path / "state",
        mode="correctness-only",
        dependencies=BenchmarkDependencies(
            discover_identity=lambda scope, state_root, deadline: _qualified_identity(),
            runtime_factory=runtime_factory,
        ),
    )

    assert report["correctness"]["definitions"]["accuracy"] == 199 / 200
    assert report["correctness"]["references"]["f1"] == 1.0
    assert report["correctness"]["calls"]["f1"] == 1.0
    assert report["evidence"]["query_attempts"] == 400
    assert report["reliability"]["recovery_rate"] == 1.0
    baseline_calls = runtimes[0].calls[:400]
    assert len(baseline_calls) == 400
    assert sum(call.capability is Capability.DEFINITIONS for call in baseline_calls) == 200
    assert sum(call.capability is Capability.REFERENCES for call in baseline_calls) == 100
    assert sum(call.capability is Capability.CALLS for call in baseline_calls) == 100
    assert report["tokens"]["tasks"]
    assert all(task["cache_read_tokens"] == 0 for task in report["tokens"]["tasks"])
    assert report["tokens"]["cache_read_label"] == "not_applicable_no_result_cache"
    validate_report(report)


def test_complete_semantic_fake_run_passes_correctness_gates(tmp_path: Path) -> None:
    class AcceptedPartialRuntime(BehavioralNavigationRuntime):
        def query(self, request: object, *, deadline: float) -> NavigationResult:
            result = super().query(request, deadline=deadline)
            return replace(result, status=NavigationStatus.PARTIAL) if result.locations else result

    report = run_fixture_benchmark(
        tmp_path / "complete-fake",
        state_root=tmp_path / "state",
        mode="correctness-only",
        dependencies=BenchmarkDependencies(
            discover_identity=lambda scope, state_root, deadline: _qualified_identity(),
            runtime_factory=lambda repository, scope, identity, state_root: (
                AcceptedPartialRuntime(repository, scope)
            ),
        ),
    )

    validate_report(report)
    assert report["errors"] == []
    assert evaluate_gates(report)["passed"] is True


def test_run_deadline_never_renews_and_cleanup_gets_a_fresh_budget(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeClock:
        def __init__(self) -> None:
            self.value = 100.0

        def __call__(self) -> float:
            return self.value

        def advance(self, seconds: float) -> None:
            self.value += seconds

    clock = FakeClock()
    discovery_deadlines: list[float] = []
    query_deadlines: list[float] = []
    cleanup_deadlines: list[float] = []

    class DeadlineRuntime:
        cleanup_failed = False

        def query(self, request: object, *, deadline: float) -> NavigationResult:
            query_deadlines.append(deadline)
            clock.advance(3.0)
            return _empty_navigation_result(request)

        def close(self, *, deadline: float) -> int:
            cleanup_deadlines.append(deadline)
            return 0

    def discover(scope: object, state_root: Path, deadline: float) -> SimpleNamespace:
        discovery_deadlines.append(deadline)
        return _qualified_identity()

    monkeypatch.setattr(benchmark_runner, "RUN_TIMEOUT_SECONDS", 5.0)
    monkeypatch.setattr(benchmark_runner, "CLEANUP_TIMEOUT_SECONDS", 2.0)
    with pytest.raises(benchmark_runner.BenchmarkTimeoutError):
        run_fixture_benchmark(
            tmp_path / "deadline",
            state_root=tmp_path / "state",
            mode="correctness-only",
            dependencies=BenchmarkDependencies(
                discover_identity=discover,
                runtime_factory=lambda *args: DeadlineRuntime(),
                monotonic=clock,
            ),
        )

    assert discovery_deadlines == [105.0]
    assert 1 <= len(query_deadlines) <= 2
    assert max(query_deadlines) <= 105.0
    assert cleanup_deadlines
    assert min(cleanup_deadlines) > 105.0


def test_every_benchmark_operation_receives_a_run_capped_deadline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started = time.monotonic()
    deadlines: list[tuple[str, float]] = []

    class RecordingRuntime(BehavioralNavigationRuntime):
        def query(self, request: object, *, deadline: float) -> NavigationResult:
            deadlines.append(("query", deadline))
            if request.path == "workloads/probe.py":
                deadlines.append(("mutation", deadline))
            return super().query(request, deadline=deadline)

        def direct_query(self, request: object, *, deadline: float) -> object:
            deadlines.append(("performance", deadline))
            return super().direct_query(request, deadline=deadline)

        def crash_and_recover(self, request: object, *, deadline: float) -> NavigationResult:
            deadlines.append(("crash", deadline))
            return self.query(request, deadline=deadline)

        def ownership_checks(self, *, deadline: float):
            deadlines.append(("ownership", deadline))
            return super().ownership_checks(deadline=deadline)

        def close(self, *, deadline: float) -> int:
            deadlines.append(("cleanup", deadline))
            return 0

    def discover(scope: object, state_root: Path, deadline: float) -> SimpleNamespace:
        deadlines.append(("discovery", deadline))
        return _qualified_identity()

    def operator(path: Path, state_root: Path, deadline: float) -> dict[str, object]:
        deadlines.append(("operator", deadline))
        return {
            "available": True,
            "python_files": 0,
            "queries_attempted": 0,
            "queries_succeeded": 0,
            "errors": 0,
        }

    monkeypatch.setattr(benchmark_runner, "RUN_TIMEOUT_SECONDS", 300.0)
    operator_root = tmp_path / "deadline-operator"
    operator_root.mkdir()
    run_fixture_benchmark(
        tmp_path / "all-deadlines",
        state_root=tmp_path / "state",
        mode="qualification",
        operator_corpus=operator_root,
        dependencies=BenchmarkDependencies(
            discover_identity=discover,
            runtime_factory=lambda repository, scope, identity, state_root: RecordingRuntime(
                repository, scope
            ),
            operator_probe=operator,
            monotonic=lambda: started,
        ),
    )

    measured = [(phase, deadline) for phase, deadline in deadlines if phase != "cleanup"]
    assert {phase for phase, _deadline in measured} >= {
        "discovery",
        "query",
        "performance",
        "mutation",
        "crash",
        "ownership",
        "operator",
    }
    assert all(deadline <= started + 300.0 for _phase, deadline in measured)


def test_cleanup_terminal_stops_crash_cycles_and_skips_ownership(
    tmp_path: Path,
) -> None:
    runtimes: list[BehavioralNavigationRuntime] = []

    class TerminalRuntime(BehavioralNavigationRuntime):
        cleanup_failed = False

        def __init__(self, repository: QualificationRepository, scope: object) -> None:
            super().__init__(repository, scope)
            self.crash_calls = 0
            self.ownership_calls = 0

        def crash_and_recover(self, request: object, *, deadline: float) -> None:
            self.crash_calls += 1
            self.cleanup_failed = True
            return None

        def ownership_checks(self, *, deadline: float):
            self.ownership_calls += 1
            return super().ownership_checks(deadline=deadline)

    def runtime_factory(repository, scope, identity, state_root):
        runtime = TerminalRuntime(repository, scope)
        runtimes.append(runtime)
        return runtime

    report = run_fixture_benchmark(
        tmp_path / "terminal-crash",
        state_root=tmp_path / "state",
        mode="correctness-only",
        dependencies=BenchmarkDependencies(
            discover_identity=lambda scope, state_root, deadline: _qualified_identity(),
            runtime_factory=runtime_factory,
        ),
    )

    assert runtimes[0].crash_calls == 1
    assert runtimes[0].ownership_calls == 0
    assert report["evidence"]["crash_attempts"] == 1
    assert report["evidence"]["ownership_checks"] == 0
    assert evaluate_gates(report)["passed"] is False


def test_cli_reports_sanitized_benchmark_timeout(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def timed_out(*args, **kwargs):
        raise benchmark_runner.BenchmarkTimeoutError("PRIVATE_TIMEOUT_DETAIL")

    monkeypatch.setattr(benchmark_runner, "run_fixture_benchmark", timed_out)
    result = benchmark_runner.main(["--fixture", "--correctness-only"])
    captured = capsys.readouterr()

    assert result != 0
    assert "BenchmarkTimeout" in captured.err
    assert "PRIVATE_TIMEOUT_DETAIL" not in captured.err


@pytest.mark.parametrize(
    "failure_mode",
    [
        "direct",
        "direct_empty",
        "direct_wrong",
        "direct_second_wrong",
        "status",
        "exact",
        "citation",
    ],
)
def test_performance_samples_require_successful_direct_and_exact_facade_results(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_mode: str,
) -> None:
    real_current_citation = benchmark_runner._current_citation
    runtimes: list[BehavioralNavigationRuntime] = []
    citation_failed = False

    def current_citation(*args, **kwargs) -> bool:
        nonlocal citation_failed
        valid = real_current_citation(*args, **kwargs)
        performance_call = runtimes and 400 < len(runtimes[0].calls) <= 440
        if failure_mode == "citation" and performance_call and not citation_failed:
            citation_failed = True
            return False
        return valid

    monkeypatch.setattr(benchmark_runner, "_current_citation", current_citation)

    class PerformanceRuntime(BehavioralNavigationRuntime):
        def __init__(self, repository: QualificationRepository, scope: object) -> None:
            super().__init__(repository, scope)
            self.direct_calls = 0

        def direct_query(self, request: object, *, deadline: float) -> object:
            assert deadline > time.monotonic()
            self.direct_calls += 1
            query = self._query_for_request(request)
            assert query is not None
            result = self._direct_result_for_query(query)
            if failure_mode == "direct":
                return SimpleNamespace(
                    coverage="not_ready",
                    partial=True,
                    locations=result.locations,
                )
            if failure_mode == "direct_empty":
                return SimpleNamespace(
                    coverage="provider_reported",
                    partial=False,
                    locations=(),
                )
            if failure_mode in {"direct_wrong", "direct_second_wrong"} and (
                failure_mode == "direct_wrong" or self.direct_calls % 2 == 0
            ):
                expected = {
                    (
                        location.path,
                        location.line,
                        location.character,
                        location.byte_start,
                        location.byte_end,
                    )
                    for location in query.expected_locations
                }
                wrong = next(
                    candidate
                    for candidate in self.repository.gold_queries
                    if {
                        (
                            location.path,
                            location.line,
                            location.character,
                            location.byte_start,
                            location.byte_end,
                        )
                        for location in candidate.expected_locations
                    }
                    != expected
                )
                return self._direct_result_for_query(wrong)
            return result

        def query(self, request: object, *, deadline: float) -> NavigationResult:
            result = super().query(request, deadline=deadline)
            if failure_mode == "status" and 400 < len(self.calls) <= 440:
                return replace(
                    result,
                    status=NavigationStatus.ERROR,
                    effective_capability=None,
                )
            if failure_mode == "exact" and 400 < len(self.calls) <= 440:
                return replace(
                    result,
                    total=0,
                    locations=(),
                    resolution=ResolutionLabel.UNRESOLVED,
                )
            return result

    def runtime_factory(repository, scope, identity, state_root):
        runtime = PerformanceRuntime(repository, scope)
        runtimes.append(runtime)
        return runtime

    report = run_fixture_benchmark(
        tmp_path / f"performance-{failure_mode}",
        state_root=tmp_path / "state",
        mode="qualification",
        dependencies=BenchmarkDependencies(
            discover_identity=lambda scope, state_root, deadline: _qualified_identity(),
            runtime_factory=runtime_factory,
        ),
    )

    expected_samples = 19 if failure_mode == "citation" else 0
    assert report["performance"]["available"] is False
    assert report["performance"]["sample_count"] == expected_samples
    if expected_samples == 0:
        assert report["performance"]["warm_facade_p50_ms"] is None
        assert report["performance"]["warm_facade_p95_ms"] is None
        assert report["performance"]["direct_pyright_p95_ms"] is None
        assert report["performance"]["warm_overhead_p95_ms"] is None


def test_warm_performance_pair_uses_two_counterbalanced_repetitions() -> None:
    events: list[tuple[str, float]] = []
    request = object()
    direct_results = [object(), object()]
    facade_results = [object(), object()]
    direct_index = 0
    facade_index = 0

    class Runtime:
        def direct_query(self, value: object, *, deadline: float) -> object:
            nonlocal direct_index
            assert value is request
            events.append(("direct", deadline))
            result = direct_results[direct_index]
            direct_index += 1
            return result

        def query(self, value: object, *, deadline: float) -> object:
            nonlocal facade_index
            assert value is request
            events.append(("facade", deadline))
            result = facade_results[facade_index]
            facade_index += 1
            return result

    ticks = iter((0.000, 0.010, 0.020, 0.040, 0.050, 0.074, 0.080, 0.094))
    deadlines = iter((101.0, 102.0, 103.0, 104.0))

    measured = benchmark_runner._measure_warm_performance_pair(
        Runtime(),
        request,
        next_deadline=lambda: next(deadlines),
        perf_counter=lambda: next(ticks),
    )

    assert events == [
        ("direct", 101.0),
        ("facade", 102.0),
        ("facade", 103.0),
        ("direct", 104.0),
    ]
    assert measured[0] == tuple(direct_results)
    assert measured[1] == tuple(facade_results)
    assert measured[2] == pytest.approx(12.0)
    assert measured[3] == pytest.approx(22.0)


def test_cold_readiness_requires_a_successful_exact_first_result(tmp_path: Path) -> None:
    class ColdFailureRuntime(BehavioralNavigationRuntime):
        def direct_query(self, request: object, *, deadline: float) -> object:
            return super().direct_query(request, deadline=deadline)

        def query(self, request: object, *, deadline: float) -> NavigationResult:
            result = super().query(request, deadline=deadline)
            if len(self.calls) == 1:
                return replace(
                    result,
                    status=NavigationStatus.ERROR,
                    effective_capability=None,
                )
            return result

    report = run_fixture_benchmark(
        tmp_path / "cold-failure",
        state_root=tmp_path / "state",
        mode="qualification",
        dependencies=BenchmarkDependencies(
            discover_identity=lambda scope, state_root, deadline: _qualified_identity(),
            runtime_factory=lambda repository, scope, identity, state_root: ColdFailureRuntime(
                repository,
                scope,
            ),
        ),
    )

    assert report["performance"]["cold_readiness_seconds"] is None
    assert report["performance"]["sample_count"] == 20
    assert report["performance"]["available"] is False


@pytest.mark.parametrize(
    "first_outcome",
    [RuntimeError("first close failed"), 1],
    ids=["raises", "leak-signal"],
)
def test_final_runtime_close_retries_once_but_keeps_scenario_unavailable(
    tmp_path: Path,
    first_outcome: object,
) -> None:
    runtimes: list[BehavioralNavigationRuntime] = []

    class RetryingCloseRuntime(BehavioralNavigationRuntime):
        def __init__(self, repository: QualificationRepository, scope: object) -> None:
            super().__init__(repository, scope)
            self.close_outcomes = [first_outcome, 0]
            self.close_deadlines: list[float] = []

        def close(self, *, deadline: float) -> int:
            self.close_deadlines.append(deadline)
            outcome = self.close_outcomes.pop(0)
            if isinstance(outcome, BaseException):
                raise outcome
            return outcome

    def runtime_factory(repository, scope, identity, state_root):
        runtime = RetryingCloseRuntime(repository, scope)
        runtimes.append(runtime)
        return runtime

    report = run_fixture_benchmark(
        tmp_path / f"retrying-final-close-{type(first_outcome).__name__}",
        state_root=tmp_path / "state",
        mode="correctness-only",
        dependencies=BenchmarkDependencies(
            discover_identity=lambda scope, state_root, deadline: _qualified_identity(),
            runtime_factory=runtime_factory,
        ),
    )

    assert len(runtimes[0].close_deadlines) == 2
    assert report["reliability"]["ownership"]["normal_shutdown"] == {
        "available": False,
        "orphan_count": None,
    }
    assert report["reliability"]["orphan_process_count"] is None
    assert report["reliability"]["orphan_process_rate"] is None
    assert any(error["phase"] == "final_close" for error in report["errors"])
    assert evaluate_gates(report)["passed"] is False


def test_final_runtime_close_retry_failure_retains_unknown_evidence(tmp_path: Path) -> None:
    runtimes: list[BehavioralNavigationRuntime] = []

    class FailingCloseRuntime(BehavioralNavigationRuntime):
        def __init__(self, repository: QualificationRepository, scope: object) -> None:
            super().__init__(repository, scope)
            self.close_calls = 0

        def close(self, *, deadline: float) -> int:
            self.close_calls += 1
            raise RuntimeError("closed final close failure")

    def runtime_factory(repository, scope, identity, state_root):
        runtime = FailingCloseRuntime(repository, scope)
        runtimes.append(runtime)
        return runtime

    report = run_fixture_benchmark(
        tmp_path / "failing-final-close",
        state_root=tmp_path / "state",
        mode="correctness-only",
        dependencies=BenchmarkDependencies(
            discover_identity=lambda scope, state_root, deadline: _qualified_identity(),
            runtime_factory=runtime_factory,
        ),
    )

    assert runtimes[0].close_calls == 2
    assert report["reliability"]["ownership"]["normal_shutdown"] == {
        "available": False,
        "orphan_count": None,
    }
    assert report["reliability"]["orphan_process_count"] is None
    assert report["evidence"]["ownership_checks"] == 3
    assert {error["phase"] for error in report["errors"]} >= {
        "final_close",
        "final_close_retry",
    }
    assert evaluate_gates(report)["passed"] is False


@pytest.mark.parametrize(
    "identity",
    [
        SimpleNamespace(
            status="missing",
            qualified=False,
            version=None,
            package_sha256=None,
            node_version=None,
            node_major=None,
            degradation_codes=("pyright_missing",),
        ),
        SimpleNamespace(
            status="qualified",
            qualified=True,
            version="1.1.410",
            package_sha256=PACKAGE_SHA256,
            node_version="v22.15.0",
            node_major=22,
            degradation_codes=(),
        ),
        SimpleNamespace(
            status="qualified",
            qualified=True,
            version=PYRIGHT_VERSION,
            package_sha256=None,
            node_version="v22.15.0",
            node_major=22,
            degradation_codes=(),
        ),
    ],
)
def test_runner_rejects_missing_or_wrong_qualified_identity(
    tmp_path: Path,
    identity: SimpleNamespace,
) -> None:
    with pytest.raises(QualifiedIdentityError):
        run_fixture_benchmark(
            tmp_path / identity.status / str(identity.version),
            state_root=tmp_path / "state",
            mode="correctness-only",
            dependencies=BenchmarkDependencies(
                discover_identity=lambda scope, state_root, deadline: identity,
                runtime_factory=lambda *args: pytest.fail("runtime must not be constructed"),
            ),
        )


def test_operator_corpus_is_aggregate_only_and_never_leaks_private_data(tmp_path: Path) -> None:
    private = (tmp_path / "private-corpus").resolve()
    private.mkdir()
    secret_text = "PRIVATE_SENTINEL_SOURCE_TEXT"
    (private / "secret_project_name.py").write_text(secret_text, encoding="utf-8")

    dependencies = BenchmarkDependencies(
        discover_identity=lambda scope, state_root, deadline: _qualified_identity(),
        runtime_factory=lambda repository, scope, identity, state_root: BehavioralNavigationRuntime(
            repository, scope
        ),
        operator_probe=lambda path, state_root, deadline: {
            "available": True,
            "python_files": 1,
            "queries_attempted": 1,
            "queries_succeeded": 1,
            "errors": 0,
        },
    )
    report = run_fixture_benchmark(
        tmp_path / "operator-work",
        state_root=tmp_path / "state",
        mode="correctness-only",
        dependencies=dependencies,
        operator_corpus=private,
    )
    encoded = json.dumps(report, sort_keys=True)
    assert str(private) not in encoded
    assert private.name not in encoded
    assert "secret_project_name" not in encoded
    assert secret_text not in encoded
    assert hashlib.sha256(str(private).encode()).hexdigest() not in encoded
    assert report["operator_corpus"] == {
        "available": True,
        "python_files": 1,
        "queries_attempted": 1,
        "queries_succeeded": 1,
        "errors": 0,
    }


def _install_operator_probe_fakes(
    monkeypatch: pytest.MonkeyPatch,
    root: Path,
) -> None:
    scope = SimpleNamespace(
        checkout_root=str(root),
        repository_id="repository:test",
        checkout_id="checkout:test",
    )

    class OperatorNavigation:
        def __init__(self, scope: object, session: object, identity: object) -> None:
            pass

        def query(self, request: object, *, deadline: float) -> NavigationResult:
            return _result_with_locations(request, NavigationStatus.OK, ())

        def close(self, *, deadline: float) -> None:
            return None

    monkeypatch.setattr(benchmark_runner, "resolve_repository_scope", lambda value: scope)
    monkeypatch.setattr(
        benchmark_runner,
        "discover_pyright",
        lambda scope, state_root, deadline: _qualified_identity(),
    )
    monkeypatch.setattr(
        benchmark_runner,
        "PyrightSession",
        lambda scope, identity, state_root: object(),
    )
    monkeypatch.setattr(benchmark_runner, "CodeNavigation", OperatorNavigation)


def test_parse_args_preserves_absolute_operator_path_for_no_follow_traversal(
    tmp_path: Path,
) -> None:
    root = (tmp_path / "operator-lexical").absolute()
    root.mkdir()
    (root / "inside.py").write_text("def inside():\n    return 1\n", encoding="utf-8")

    args = parse_args(
        ["--fixture", "--correctness-only", "--operator-corpus", os.fspath(root)]
    )
    expected = Path(os.path.abspath(os.path.expanduser(os.fspath(root))))

    assert args.operator_corpus == expected
    traversed_root, files = benchmark_runner._operator_python_files(
        args.operator_corpus,
        deadline=time.monotonic() + 10.0,
    )
    assert traversed_root == root.resolve()
    assert [path.name for path in files] == ["inside.py"]


def test_parse_and_traversal_reject_operator_root_symlink_without_leaking_target(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    target = tmp_path / "PRIVATE_ROOT_TARGET"
    target.mkdir()
    (target / "secret.py").write_text("PRIVATE_ROOT_TEXT", encoding="utf-8")
    link = tmp_path / "operator-root-link"
    try:
        os.symlink(target, link, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"directory symlink creation is unavailable: {type(exc).__name__}")

    with pytest.raises(SystemExit):
        parse_args(
            ["--fixture", "--correctness-only", "--operator-corpus", os.fspath(link)]
        )
    captured = capsys.readouterr()
    with pytest.raises(RuntimeError, match="regular directory"):
        benchmark_runner._operator_python_files(
            link,
            deadline=time.monotonic() + 10.0,
        )

    assert target.name not in captured.err
    assert "PRIVATE_ROOT_TEXT" not in captured.err


@pytest.mark.skipif(os.name != "nt", reason="Windows root-junction coverage")
def test_parse_and_traversal_reject_operator_root_windows_junction(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    target = tmp_path / "PRIVATE_JUNCTION_TARGET"
    target.mkdir()
    junction = tmp_path / "operator-root-junction"
    created = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(junction), str(target)],
        check=False,
        capture_output=True,
        shell=False,
        timeout=10,
    )
    if created.returncode != 0:
        pytest.skip("junction creation is unavailable")

    with pytest.raises(SystemExit):
        parse_args(
            ["--fixture", "--correctness-only", "--operator-corpus", os.fspath(junction)]
        )
    captured = capsys.readouterr()
    with pytest.raises(RuntimeError, match="regular directory"):
        benchmark_runner._operator_python_files(
            junction,
            deadline=time.monotonic() + 10.0,
        )

    assert target.name not in captured.err


def test_operator_probe_never_reads_or_reports_outside_symlink(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "operator-symlink"
    outside = tmp_path / "outside-sentinel.py"
    root.mkdir()
    (root / "inside.py").write_text("def inside() -> int:\n    return 1\n", encoding="utf-8")
    outside.write_text("OUTSIDE_PRIVATE_SENTINEL", encoding="utf-8")
    try:
        os.symlink(outside, root / "outside-link.py")
    except OSError as exc:
        pytest.skip(f"symlink creation is unavailable: {type(exc).__name__}")
    _install_operator_probe_fakes(monkeypatch, root)
    visited: list[Path] = []

    def definition(
        path: Path,
        checkout: Path,
        *,
        operator_root: Path,
        deadline: float,
    ):
        assert path.resolve() != outside.resolve()
        visited.append(path)
        return None

    monkeypatch.setattr(benchmark_runner, "_operator_definition", definition)
    metrics = _probe_operator_corpus(
        root,
        tmp_path / "state",
        deadline=time.monotonic() + 30.0,
    )

    assert [path.name for path in visited] == ["inside.py"]
    assert metrics == {
        "available": True,
        "python_files": 1,
        "queries_attempted": 0,
        "queries_succeeded": 0,
        "errors": 0,
    }
    encoded = json.dumps(metrics, sort_keys=True)
    assert outside.name not in encoded
    assert "OUTSIDE_PRIVATE_SENTINEL" not in encoded


def test_operator_probe_fails_closed_without_leaking_oversized_source(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "operator-oversized"
    root.mkdir()
    private_text = b"PRIVATE_OVERSIZED_SOURCE_TEXT"
    source = root / "private_project_name.py"
    source.write_bytes(
        b"def oversized() -> int:\n    return 1\n# "
        + private_text
        + b"x" * benchmark_runner.OPERATOR_MAX_SOURCE_BYTES
        + b"\n"
    )
    _install_operator_probe_fakes(monkeypatch, root)

    metrics = _probe_operator_corpus(
        root,
        tmp_path / "state",
        deadline=time.monotonic() + 30.0,
    )
    encoded = json.dumps(metrics, sort_keys=True)

    assert metrics == {
        "available": False,
        "python_files": 1,
        "queries_attempted": 0,
        "queries_succeeded": 0,
        "errors": 1,
    }
    assert source.name not in encoded
    assert private_text.decode() not in encoded


def test_operator_source_uses_bounded_chunk_reads_not_path_read_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "operator-bounded-read"
    root.mkdir()
    source = root / "bounded.py"
    source.write_bytes(
        b"def bounded() -> int:\n    return 1\n# "
        + b"x" * (benchmark_runner.OPERATOR_READ_CHUNK_BYTES * 2)
        + b"\n"
    )
    real_read_bytes = Path.read_bytes
    real_read = os.read
    requested_sizes: list[int] = []

    def forbid_path_read_bytes(path: Path) -> bytes:
        if path == source:
            pytest.fail("operator source must not use Path.read_bytes")
        return real_read_bytes(path)

    def tracked_read(descriptor: int, size: int) -> bytes:
        requested_sizes.append(size)
        return real_read(descriptor, size)

    monkeypatch.setattr(Path, "read_bytes", forbid_path_read_bytes)
    monkeypatch.setattr(benchmark_runner.os, "read", tracked_read)
    definition = benchmark_runner._operator_definition(
        source,
        root,
        operator_root=root,
        deadline=time.monotonic() + 10.0,
    )

    assert definition == ("bounded.py", 1, len("def "))
    assert len(requested_sizes) >= 3
    assert max(requested_sizes) <= benchmark_runner.OPERATOR_READ_CHUNK_BYTES


def test_operator_source_deadline_stops_chunk_reads_immediately(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "operator-read-deadline"
    root.mkdir()
    source = root / "deadline.py"
    source.write_bytes(
        b"def deadline() -> int:\n    return 1\n# "
        + b"x" * (benchmark_runner.OPERATOR_READ_CHUNK_BYTES * 3)
        + b"\n"
    )
    real_read = os.read
    expired = False
    reads = 0

    def tracked_read(descriptor: int, size: int) -> bytes:
        nonlocal expired, reads
        reads += 1
        chunk = real_read(descriptor, size)
        expired = True
        return chunk

    def check_deadline(deadline: float, **kwargs) -> float:
        if expired:
            raise benchmark_runner.BenchmarkTimeoutError("bounded source read expired")
        return 0.0

    monkeypatch.setattr(benchmark_runner.os, "read", tracked_read)
    monkeypatch.setattr(benchmark_runner, "_check_run_deadline", check_deadline)
    with pytest.raises(benchmark_runner.BenchmarkTimeoutError):
        benchmark_runner._operator_definition(
            source,
            root,
            operator_root=root,
            deadline=time.monotonic() + 10.0,
        )
    assert reads == 1


def test_operator_source_revalidates_stable_fstat_after_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "operator-fstat"
    root.mkdir()
    source = root / "stable.py"
    source.write_text("def stable() -> int:\n    return 1\n", encoding="utf-8")
    real_fstat = os.fstat
    calls = 0

    def changing_fstat(descriptor: int):
        nonlocal calls
        calls += 1
        info = real_fstat(descriptor)
        if calls == 2:
            return SimpleNamespace(
                st_dev=info.st_dev,
                st_ino=info.st_ino,
                st_mode=info.st_mode,
                st_size=info.st_size,
                st_mtime_ns=info.st_mtime_ns + 1,
                st_ctime_ns=info.st_ctime_ns,
            )
        return info

    monkeypatch.setattr(benchmark_runner.os, "fstat", changing_fstat)
    with pytest.raises(PermissionError, match="changed during read"):
        benchmark_runner._operator_definition(
            source,
            root,
            operator_root=root,
            deadline=time.monotonic() + 10.0,
        )
    assert calls == 2


def test_operator_source_rejects_outside_file_before_reading_private_text(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "operator-contained"
    root.mkdir()
    outside = tmp_path / "PRIVATE_OUTSIDE.py"
    outside.write_text("PRIVATE_OUTSIDE_SENTINEL", encoding="utf-8")
    real_read_bytes = Path.read_bytes

    def forbid_outside_read(path: Path) -> bytes:
        if path == outside:
            pytest.fail("outside source was read")
        return real_read_bytes(path)

    monkeypatch.setattr(Path, "read_bytes", forbid_outside_read)
    with pytest.raises(PermissionError, match="outside operator root") as captured:
        benchmark_runner._operator_definition(
            outside,
            root,
            operator_root=root,
            deadline=time.monotonic() + 10.0,
        )
    assert outside.name not in str(captured.value)
    assert "PRIVATE_OUTSIDE_SENTINEL" not in str(captured.value)


def test_operator_source_rejects_leaf_symlink_without_reading_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "operator-leaf-link"
    root.mkdir()
    outside = tmp_path / "PRIVATE_LEAF_TARGET.py"
    outside.write_text("PRIVATE_LEAF_SENTINEL", encoding="utf-8")
    link = root / "linked.py"
    try:
        os.symlink(outside, link)
    except OSError as exc:
        pytest.skip(f"file symlink creation is unavailable: {type(exc).__name__}")
    real_read_bytes = Path.read_bytes

    def forbid_target_read(path: Path) -> bytes:
        if path in {link, outside}:
            pytest.fail("linked outside source was read")
        return real_read_bytes(path)

    monkeypatch.setattr(Path, "read_bytes", forbid_target_read)
    with pytest.raises(PermissionError, match="regular file") as captured:
        benchmark_runner._operator_definition(
            link,
            root,
            operator_root=root,
            deadline=time.monotonic() + 10.0,
        )
    assert outside.name not in str(captured.value)
    assert "PRIVATE_LEAF_SENTINEL" not in str(captured.value)


def test_operator_source_rejects_reparse_leaf_metadata_before_open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "operator-reparse-leaf"
    root.mkdir()
    source = root / "reparse.py"
    source.write_text("def reparse() -> int:\n    return 1\n", encoding="utf-8")
    real_stat = Path.stat

    def reparse_stat(path: Path, *, follow_symlinks: bool = True):
        info = real_stat(path, follow_symlinks=follow_symlinks)
        if path == source and not follow_symlinks:
            return SimpleNamespace(
                st_dev=info.st_dev,
                st_ino=info.st_ino,
                st_mode=info.st_mode,
                st_size=info.st_size,
                st_mtime_ns=info.st_mtime_ns,
                st_ctime_ns=info.st_ctime_ns,
                st_file_attributes=0x400,
            )
        return info

    monkeypatch.setattr(Path, "stat", reparse_stat)
    with pytest.raises(PermissionError, match="regular file"):
        benchmark_runner._operator_definition(
            source,
            root,
            operator_root=root,
            deadline=time.monotonic() + 10.0,
        )


@pytest.mark.skipif(os.name != "nt", reason="Windows safe-handle coverage")
def test_operator_source_uses_windows_no_follow_safe_handle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "operator-windows-handle"
    root.mkdir()
    source = root / "safe.py"
    source.write_text("def safe() -> int:\n    return 1\n", encoding="utf-8")
    opened: list[Path] = []
    real_open = benchmark_runner._windows_workspace.open_exclusive_readonly_source_file

    def tracked_open(path: Path) -> int:
        opened.append(path)
        return real_open(path)

    monkeypatch.setattr(
        benchmark_runner._windows_workspace,
        "open_exclusive_readonly_source_file",
        tracked_open,
    )
    definition = benchmark_runner._operator_definition(
        source,
        root,
        operator_root=root,
        deadline=time.monotonic() + 10.0,
    )

    assert definition == ("safe.py", 1, len("def "))
    assert opened == [source]


@pytest.mark.skipif(os.name != "nt", reason="Windows junction coverage")
def test_operator_probe_never_descends_into_windows_junction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "operator-junction"
    outside = tmp_path / "outside-junction"
    root.mkdir()
    outside.mkdir()
    (root / "inside.py").write_text("def inside() -> int:\n    return 1\n", encoding="utf-8")
    sentinel = outside / "outside.py"
    sentinel.write_text("OUTSIDE_JUNCTION_SENTINEL", encoding="utf-8")
    junction = root / "junction"
    created = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(junction), str(outside)],
        check=False,
        capture_output=True,
        shell=False,
        timeout=10,
    )
    if created.returncode != 0:
        pytest.skip("junction creation is unavailable")
    _install_operator_probe_fakes(monkeypatch, root)
    visited: list[Path] = []

    def definition(
        path: Path,
        checkout: Path,
        *,
        operator_root: Path,
        deadline: float,
    ):
        assert outside.resolve() not in path.resolve().parents
        visited.append(path)
        return None

    monkeypatch.setattr(benchmark_runner, "_operator_definition", definition)
    metrics = _probe_operator_corpus(
        root,
        tmp_path / "state",
        deadline=time.monotonic() + 30.0,
    )

    assert [path.name for path in visited] == ["inside.py"]
    assert metrics["python_files"] == 1
    assert metrics["available"] is True


def test_operator_traversal_entry_cap_fails_closed_before_provider_start(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "operator-entry-cap"
    root.mkdir()
    for index in range(3):
        (root / f"module_{index}.py").write_text("def value():\n    return 1\n", encoding="utf-8")
    monkeypatch.setattr(benchmark_runner, "OPERATOR_MAX_SCANNED_ENTRIES", 1)
    monkeypatch.setattr(
        benchmark_runner,
        "discover_pyright",
        lambda *args, **kwargs: pytest.fail("provider must not start after traversal cap"),
    )

    metrics = _probe_operator_corpus(
        root,
        tmp_path / "state",
        deadline=time.monotonic() + 30.0,
    )

    assert metrics["available"] is False
    assert metrics["errors"] == 1
    assert metrics["queries_attempted"] == 0


def test_operator_traversal_depth_cap_fails_closed_before_provider_start(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "operator-depth-cap"
    nested = root / "nested"
    nested.mkdir(parents=True)
    (nested / "module.py").write_text("def value():\n    return 1\n", encoding="utf-8")
    monkeypatch.setattr(benchmark_runner, "OPERATOR_MAX_DEPTH", 0)
    monkeypatch.setattr(
        benchmark_runner,
        "discover_pyright",
        lambda *args, **kwargs: pytest.fail("provider must not start after traversal cap"),
    )

    metrics = _probe_operator_corpus(
        root,
        tmp_path / "state",
        deadline=time.monotonic() + 30.0,
    )

    assert metrics["available"] is False
    assert metrics["errors"] == 1
    assert metrics["queries_attempted"] == 0


def test_operator_traversal_deadline_propagates_benchmark_timeout(
    tmp_path: Path,
) -> None:
    root = tmp_path / "operator-deadline"
    root.mkdir()
    (root / "module.py").write_text("def value():\n    return 1\n", encoding="utf-8")

    with pytest.raises(benchmark_runner.BenchmarkTimeoutError):
        _probe_operator_corpus(
            root,
            tmp_path / "state",
            deadline=time.monotonic() - 1.0,
        )


def test_operator_traversal_selects_a_deterministic_bounded_file_set(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "operator-file-cap"
    root.mkdir()
    for name in ("z.py", "a.py", "m.py", "b.py"):
        (root / name).write_text("def value():\n    return 1\n", encoding="utf-8")
    monkeypatch.setattr(benchmark_runner, "OPERATOR_MAX_PYTHON_FILES", 2)
    _install_operator_probe_fakes(monkeypatch, root)
    visited: list[str] = []

    def definition(
        path: Path,
        checkout: Path,
        *,
        operator_root: Path,
        deadline: float,
    ):
        visited.append(path.name)
        return None

    monkeypatch.setattr(benchmark_runner, "_operator_definition", definition)
    metrics = _probe_operator_corpus(
        root,
        tmp_path / "state",
        deadline=time.monotonic() + 30.0,
    )

    assert visited == ["a.py", "b.py"]
    assert metrics["python_files"] == 2
    assert metrics["available"] is True


def test_operator_probe_close_failure_marks_metrics_unavailable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    operator_root = tmp_path / "operator-close"
    operator_root.mkdir()
    (operator_root / "sample.py").write_text(
        "def sample() -> int:\n    return 1\n",
        encoding="utf-8",
    )
    initialize_deterministic_git(operator_root)

    close_deadlines: list[float] = []

    class FailingOperatorNavigation:
        def __init__(self, scope: object, session: object, identity: object) -> None:
            self.scope = scope

        def query(self, request: object, *, deadline: float) -> NavigationResult:
            return replace(
                _empty_navigation_result(request),
                status=NavigationStatus.OK,
                effective_capability=Capability.DEFINITIONS,
            )

        def close(self, *, deadline: float) -> None:
            close_deadlines.append(deadline)
            if len(close_deadlines) == 1:
                raise RuntimeError("closed operator close failure")

    monkeypatch.setattr(
        benchmark_runner,
        "discover_pyright",
        lambda scope, state_root, deadline: _qualified_identity(),
    )
    monkeypatch.setattr(
        benchmark_runner,
        "PyrightSession",
        lambda scope, identity, state_root: object(),
    )
    monkeypatch.setattr(
        benchmark_runner,
        "CodeNavigation",
        FailingOperatorNavigation,
    )

    metrics = _probe_operator_corpus(
        operator_root,
        tmp_path / "state",
        deadline=time.monotonic() + 30.0,
    )
    assert len(close_deadlines) == 2
    assert close_deadlines[1] >= close_deadlines[0]
    assert metrics == {
        "available": False,
        "python_files": 1,
        "queries_attempted": 1,
        "queries_succeeded": 1,
        "errors": 1,
    }


class _FakeWindowsFunction:
    def __init__(self, callback) -> None:
        self._callback = callback
        self.argtypes = None
        self.restype = None

    def __call__(self, *args):
        return self._callback(*args)


def _install_fake_windows_rss_api(
    monkeypatch: pytest.MonkeyPatch,
    memory_callback,
) -> tuple[object, _FakeWindowsFunction, _FakeWindowsFunction, list[tuple[str, bool]]]:
    handle = object()
    get_current_process = _FakeWindowsFunction(lambda: handle)
    get_process_memory_info = _FakeWindowsFunction(memory_callback)
    libraries = {
        "kernel32": SimpleNamespace(GetCurrentProcess=get_current_process),
        "psapi": SimpleNamespace(GetProcessMemoryInfo=get_process_memory_info),
    }
    loads: list[tuple[str, bool]] = []

    def load(name: str, *, use_last_error: bool = False):
        loads.append((name, use_last_error))
        return libraries[name]

    monkeypatch.setattr(benchmark_runner.os, "name", "nt")
    monkeypatch.setattr(benchmark_runner.ctypes, "WinDLL", load, raising=False)
    return handle, get_current_process, get_process_memory_info, loads


def test_windows_peak_rss_uses_pointer_sized_win32_signatures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: dict[str, object] = {}

    def memory_info(handle: object, counters: object, size: int) -> int:
        observed.update(handle=handle, counters=counters, size=size)
        counters._obj.PeakWorkingSetSize = 96 * 1024 * 1024
        return 1

    handle, get_current_process, get_process_memory_info, loads = (
        _install_fake_windows_rss_api(monkeypatch, memory_info)
    )
    value, method = benchmark_runner._peak_rss()

    from ctypes import wintypes

    assert loads == [("kernel32", True), ("psapi", True)]
    assert get_current_process.argtypes == []
    assert get_current_process.restype is wintypes.HANDLE
    assert get_process_memory_info.argtypes[0] is wintypes.HANDLE
    assert get_process_memory_info.argtypes[1]._type_ is type(observed["counters"]._obj)
    assert get_process_memory_info.argtypes[2] is wintypes.DWORD
    assert get_process_memory_info.restype is wintypes.BOOL
    assert observed["handle"] is handle
    assert observed["size"] == ctypes.sizeof(observed["counters"]._obj)
    assert value == 96.0
    assert method == "measured-windows-peak-working-set"


def test_windows_peak_rss_api_failure_uses_last_error_and_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    last_error_calls: list[bool] = []
    _handle, _current, _memory, loads = _install_fake_windows_rss_api(
        monkeypatch,
        lambda handle, counters, size: 0,
    )
    monkeypatch.setattr(
        benchmark_runner.ctypes,
        "get_last_error",
        lambda: last_error_calls.append(True) or 5,
        raising=False,
    )

    assert benchmark_runner._peak_rss() == (None, "unavailable")
    assert loads == [("kernel32", True), ("psapi", True)]
    assert last_error_calls == [True]


@pytest.mark.skipif(os.name != "nt", reason="requires the real Win32 process APIs")
def test_windows_peak_rss_integration_is_finite_positive_and_measured() -> None:
    value, method = benchmark_runner._peak_rss()

    assert value is not None
    assert math.isfinite(value)
    assert value > 0.0
    assert method == "measured-windows-peak-working-set"


def test_peak_client_rss_is_measured_after_optional_operator_probe(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []

    class QualificationRuntime(BehavioralNavigationRuntime):
        def direct_query(self, request: object, *, deadline: float) -> object:
            return super().direct_query(request, deadline=deadline)

    def operator_probe(path: Path, state_root: Path, deadline: float) -> dict[str, object]:
        events.append("operator")
        return {
            "available": True,
            "python_files": 1,
            "queries_attempted": 1,
            "queries_succeeded": 1,
            "errors": 0,
        }

    def peak_rss() -> tuple[float, str]:
        events.append("rss")
        return 42.0, "measured-test"

    monkeypatch.setattr(benchmark_runner, "_peak_rss", peak_rss)
    operator_root = tmp_path / "operator-order"
    operator_root.mkdir()
    report = run_fixture_benchmark(
        tmp_path / "operator-order-work",
        state_root=tmp_path / "state",
        mode="qualification",
        operator_corpus=operator_root,
        dependencies=BenchmarkDependencies(
            discover_identity=lambda scope, state_root, deadline: _qualified_identity(),
            runtime_factory=lambda repository, scope, identity, state_root: QualificationRuntime(
                repository, scope
            ),
            operator_probe=operator_probe,
        ),
    )

    assert events == ["operator", "rss"]
    assert report["resources"]["client_peak_rss_mib"] == 42.0


def test_cli_sanitizes_unexpected_private_corpus_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    private = (tmp_path / "private-corpus").resolve()
    private.mkdir()

    def fail_run(*args, **kwargs):
        raise OSError(f"private path failed: {private}")

    monkeypatch.setattr(benchmark_runner, "run_fixture_benchmark", fail_run)
    result = benchmark_runner.main(
        [
            "--fixture",
            "--correctness-only",
            "--operator-corpus",
            str(private),
        ]
    )
    captured = capsys.readouterr()
    assert result != 0
    assert str(private) not in captured.out
    assert str(private) not in captured.err


def test_cli_modes_fixture_and_operator_path_are_enforced(tmp_path: Path) -> None:
    with pytest.raises(SystemExit):
        parse_args([])
    with pytest.raises(SystemExit):
        parse_args(["--fixture", "--correctness-only", "--qualification"])
    with pytest.raises(SystemExit):
        parse_args(["--correctness-only"])
    with pytest.raises(SystemExit):
        parse_args(
            [
                "--fixture",
                "--correctness-only",
                "--operator-corpus",
                "relative/path",
            ]
        )
    missing = (tmp_path / "missing").resolve()
    with pytest.raises(SystemExit):
        parse_args(
            [
                "--fixture",
                "--qualification",
                "--operator-corpus",
                str(missing),
            ]
        )
    assert parse_args(["--fixture", "--correctness-only"]).correctness_only


def test_require_gates_returns_nonzero_for_an_unmet_measured_gate(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    report = _measured_report("correctness-only")
    report["reliability"]["stale_answer_count"] = 1  # type: ignore[index]
    monkeypatch.setattr(
        benchmark_runner,
        "run_fixture_benchmark",
        lambda *args, **kwargs: report,
    )

    assert benchmark_runner.main(["--fixture", "--correctness-only"]) == 0
    assert benchmark_runner.main(["--fixture", "--correctness-only", "--require-gates"]) == 1
    capsys.readouterr()


def test_default_state_root_honors_environment_not_fixture_temp(tmp_path: Path) -> None:
    installed_state = (tmp_path / "installed-state").resolve()
    assert (
        resolve_state_root(None, environ={"LLM_WIKI_STATE_ROOT": str(installed_state)})
        == installed_state
    )
    explicit = (tmp_path / "explicit-state").resolve()
    assert (
        resolve_state_root(explicit, environ={"LLM_WIKI_STATE_ROOT": str(installed_state)})
        == explicit
    )
    fallback = resolve_state_root(None, environ={})
    assert fallback == BENCHMARK_ROOT.parent.resolve()
    assert "code-nav-" not in str(fallback)


def test_pinned_constants_match_approved_contract() -> None:
    assert FIXTURE_SEED == 411
    assert FIXTURE_LINES == 100_000
    assert PYRIGHT_VERSION == "1.1.411"
    assert GATE_THRESHOLDS == {
        "definition_accuracy": 0.99,
        "reference_f1": 0.95,
        "stale_answer_count": 0,
        "stale_result_rate": 0.0,
        "orphan_process_count": 0,
        "orphan_process_rate": 0.0,
        "recovery_rate": 1.0,
        "default_items": 10,
        "default_estimated_tokens": 1200,
        "warm_overhead_p95_ms": 30,
        "cold_readiness_seconds": 60,
        "client_rss_mib": 100,
    }
    assert os.path.isabs(str(BENCHMARK_ROOT))
