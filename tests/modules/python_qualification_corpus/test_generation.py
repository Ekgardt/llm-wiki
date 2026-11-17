"""Original deterministic corpus assertions, independent of navigation execution."""
from __future__ import annotations

import ast
from dataclasses import fields, replace
from pathlib import Path

from .support import (
    _GOLD_CAPABILITIES,
    _line_start_bytes,
    qualification_generator,
)
from .support import (
    generated_pair as generated_pair,
)

QualificationRepository = qualification_generator.QualificationRepository
CALL_QUERIES = qualification_generator.CALL_QUERIES
DEFINITION_QUERIES = qualification_generator.DEFINITION_QUERIES
REFERENCE_QUERIES = qualification_generator.REFERENCE_QUERIES
FIXTURE_SEED = qualification_generator.FIXTURE_SEED
FIXTURE_LINES = qualification_generator.FIXTURE_LINES


def _source_files(repository: QualificationRepository) -> dict[str, bytes]:
    return {
        path.relative_to(repository.root).as_posix(): path.read_bytes()
        for path in sorted(repository.root.rglob("*.py"))
    }


def _padding_block_spans(tree: ast.Module) -> set[int]:
    """How many lines each top-level function of a padding module spans."""
    return {
        node.end_lineno - node.lineno + 1
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.end_lineno is not None
    }


def _padding_module_shape(module: Path) -> tuple:
    """A padding module compiles, holds only functions of one size, and has no comments."""
    content = module.read_bytes()
    compile(content, module.as_posix(), "exec")
    tree = ast.parse(content, filename=module.as_posix())
    return (
        bool(tree.body),
        {type(node) for node in tree.body},
        _padding_block_spans(tree),
        b"#" in content,
    )


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
    assert qualification_generator.PADDING_BLOCK_LINES == 64
    for module in padding_modules:
        assert _padding_module_shape(module) == (
            True,
            {ast.FunctionDef},
            {qualification_generator.PADDING_BLOCK_LINES},
            False,
        )
    assert not (first.root / ".git").exists()


def _name_id(node) -> str | None:
    """The identifier of an `ast.Name`, and nothing for any other node."""
    if not isinstance(node, ast.Name):
        return None
    return node.id


def _signature_shape(function: ast.FunctionDef) -> tuple:
    """A padding function's whole signature, as one comparable tuple."""
    return (
        [argument.arg for argument in function.args.args],
        _name_id(function.args.args[0].annotation),
        _name_id(function.returns),
        bool(function.decorator_list),
        bool(function.args.posonlyargs),
        bool(function.args.kwonlyargs),
        function.args.vararg,
        function.args.kwarg,
    )


_PADDING_SIGNATURE = (["accumulator"], "int", "int", False, False, False, None, None)


def _assert_padding_signature(
    function: ast.FunctionDef,
    module_index: int,
    block_index: int,
) -> None:
    assert function.name == f"padding_{module_index:02d}_{block_index:03d}"
    assert _signature_shape(function) == _PADDING_SIGNATURE
    assert function.end_lineno is not None
    assert function.end_lineno - function.lineno + 1 == 64


def _returns_accumulator(statement) -> bool:
    if not isinstance(statement, ast.Return):
        return False
    return _name_id(statement.value) == "accumulator"


def _call_shape(statement) -> tuple:
    """The shape of `accumulator = callee(accumulator)`, for any statement."""
    if not isinstance(statement, ast.Assign):
        return ()
    call = statement.value
    return (
        [_name_id(target) for target in statement.targets],
        _name_id(getattr(call, "func", None)),
        [_name_id(argument) for argument in getattr(call, "args", ())],
        bool(getattr(call, "keywords", ())),
    )


def _aug_shape(operation) -> tuple:
    """The shape of `accumulator += <int>`, for any statement."""
    if not isinstance(operation, ast.AugAssign):
        return ()
    return (
        _name_id(operation.target),
        type(operation.op),
        type(getattr(operation.value, "value", None)),
    )


def _aug_constants(operations) -> list[int]:
    """Every constant added to the accumulator, refusing any other statement."""
    values: list[int] = []
    for operation in operations:
        assert _aug_shape(operation) == ("accumulator", ast.Add, int)
        values.append(operation.value.value)
    return values


def _chained_previous(body: list, module_index: int, block_index: int) -> tuple:
    """Every block but the first opens by calling the block before it."""
    if not block_index:
        return ()
    previous_name = f"padding_{module_index:02d}_{block_index - 1:03d}"
    assert _call_shape(body.pop(0)) == (
        ["accumulator"],
        previous_name,
        ["accumulator"],
        False,
    )
    return (previous_name,)


def _padding_block_facts(
    function: ast.FunctionDef,
    module_index: int,
    block_index: int,
) -> tuple[tuple, list[int]]:
    """Check one block, and report what it calls and what it adds."""
    _assert_padding_signature(function, module_index, block_index)
    body = list(function.body)
    assert _returns_accumulator(body.pop())
    calls = _chained_previous(body, module_index, block_index)
    assert body
    return calls, _aug_constants(body)


def _padding_module_facts(
    module_index: int,
    module_path: Path,
    edges: dict,
    constants: list[int],
) -> str:
    """Check one padding module, and report the name of its entry block."""
    content = module_path.read_bytes()
    assert b"#" not in content
    tree = ast.parse(content, filename=module_path.as_posix())
    functions = [node for node in tree.body if isinstance(node, ast.FunctionDef)]
    assert len(functions) == (48 if module_index < 24 else 47)
    assert len(functions) == len(tree.body)
    for block_index, function in enumerate(functions):
        calls, values = _padding_block_facts(function, module_index, block_index)
        edges[function.name] = calls
        constants.extend(values)
    return functions[-1].name


def _is_padding_module(module: str | None) -> bool:
    if module is None:
        return False
    return module.startswith("padding.module_")


def _single_plain_name(node: ast.ImportFrom) -> bool:
    if len(node.names) != 1:
        return False
    return node.names[0].asname is None


def _padding_import_nodes(users_tree: ast.Module) -> list:
    return [
        node
        for node in users_tree.body
        if isinstance(node, ast.ImportFrom) and _is_padding_module(node.module)
    ]


def _padding_module_names() -> list[str]:
    return [f"padding.module_{index:02d}" for index in range(32)]


def _assert_padding_imports(users_tree: ast.Module, entry_names: list[str]) -> None:
    """The user module imports one entry block from each padding module."""
    imports = _padding_import_nodes(users_tree)
    assert [node.module for node in imports] == _padding_module_names()
    assert [node.names[0].name for node in imports] == entry_names
    assert [_single_plain_name(node) for node in imports] == [True] * len(imports)


def _user_function(users_tree: ast.Module) -> ast.FunctionDef:
    functions = [node for node in users_tree.body if isinstance(node, ast.FunctionDef)]
    assert len(functions) == 1
    assert functions[0].name == "exercise_padding"
    return functions[0]


def _called_entries(user_calls: list) -> list[str]:
    """One `accumulator = entry(accumulator)` per padding module, in order."""
    names: list[str] = []
    for statement in user_calls:
        shape = _call_shape(statement)
        assert (shape[0], shape[2], shape[3]) == (
            ["accumulator"],
            ["accumulator"],
            False,
        )
        names.append(shape[1])
    return names


def _assert_live_constants(constants: list[int]) -> None:
    """Every added constant is distinct, and every one carries the fixture seed."""
    assert len(constants) == len(set(constants))
    assert {value // 10_000_000 for value in constants} == {FIXTURE_SEED}


def _reachable_from(entries: list[str], edges: dict) -> set[str]:
    reachable: set[str] = set()
    pending = list(entries)
    while pending:
        name = pending.pop()
        if name in reachable:
            continue
        reachable.add(name)
        pending.extend(edges[name])
    return reachable


def test_padding_ast_is_live_and_reachable_from_every_static_user_call(
    generated_pair: tuple[QualificationRepository, QualificationRepository],
) -> None:
    repository = generated_pair[0]
    function_edges: dict[str, tuple[str, ...]] = {}
    live_constants: list[int] = []
    module_paths = sorted((repository.root / "padding").glob("module_*.py"))
    entry_names = [
        _padding_module_facts(index, path, function_edges, live_constants)
        for index, path in enumerate(module_paths)
    ]
    _assert_live_constants(live_constants)

    users_path = repository.root / "qual" / "padding_users.py"
    users_content = users_path.read_bytes()
    assert b"#" not in users_content
    users_tree = ast.parse(users_content, filename=users_path.as_posix())
    _assert_padding_imports(users_tree, entry_names)
    user_function = _user_function(users_tree)
    called_entries = _called_entries(user_function.body[:32])
    assert called_entries == entry_names
    live_constants.extend(_aug_constants(user_function.body[32:-1]))
    assert _returns_accumulator(user_function.body[-1])
    _assert_live_constants(live_constants)
    assert _reachable_from(called_entries, function_edges) == set(function_edges)


_SHARED_WORKLOAD_PATHS = (
    "original_path",
    "renamed_path",
    "created_path",
    "probe_path",
)


_PER_WORKLOAD_SYMBOLS = ("original_symbol", "edited_symbol", "created_symbol")


_PER_WORKLOAD_CONTENTS = (
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


def _distinct_count(workloads, attribute: str) -> int:
    return len({getattr(workload, attribute) for workload in workloads})


def _distinct_counts(workloads, attributes: tuple) -> set[int]:
    return {_distinct_count(workloads, attribute) for attribute in attributes}


def _assert_workload_probes(workload) -> None:
    """A workload's probe file is its own, and every probe names its symbol."""
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


def _manifest_paths(repository) -> set:
    return {path for path, _digest in repository.source_manifest}


def _marked_paths(paths: set, marker: str) -> set:
    return {path for path in paths if marker in path}


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
    workloads = repository.workloads
    assert _distinct_counts(workloads, _SHARED_WORKLOAD_PATHS) == {1}
    first = repository.workloads[0]
    assert len(
        {first.original_path, first.renamed_path, first.created_path, first.probe_path}
    ) == 4
    assert _distinct_counts(workloads, _PER_WORKLOAD_SYMBOLS) == {50}
    assert _distinct_counts(workloads, _PER_WORKLOAD_CONTENTS) == {50}
    for workload in workloads:
        _assert_workload_probes(workload)
    assert (repository.root / first.original_path).read_bytes() == first.original_content
    assert (repository.root / first.probe_path).read_bytes() == first.baseline_probe_content
    manifest_paths = _manifest_paths(repository)
    assert _marked_paths(manifest_paths, "cycle_") == set()
    assert _marked_paths(manifest_paths, "probe_") == set()
    assert _marked_paths(manifest_paths, "broken") != set()


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


def _capability_counts(queries) -> dict:
    return {
        capability: sum(query.capability == capability for query in queries)
        for capability in _GOLD_CAPABILITIES
    }


def _location_shape(location) -> tuple:
    """A gold location spans bytes, carries a digest, and counts lines from one."""
    return (
        location.byte_end > location.byte_start,
        len(location.source_sha256),
        location.line >= 1,
        location.character >= 0,
    )


def _gold_locations(queries) -> list:
    return [location for query in queries for location in query.expected_locations]


def _queries_of(queries, capability: str) -> list:
    return [query for query in queries if query.capability == capability]


def _expected_location_counts(queries) -> set:
    return {len(query.expected_locations) for query in queries}


def _cross_file(queries) -> set:
    return {query.path != query.expected_locations[0].path for query in queries}


def _directions(queries) -> set:
    return {query.direction for query in queries}


def test_gold_has_exact_counts_complete_ranges_and_cross_file_answers(
    generated_pair: tuple[QualificationRepository, QualificationRepository],
) -> None:
    queries = generated_pair[0].gold_queries
    assert (DEFINITION_QUERIES, REFERENCE_QUERIES, CALL_QUERIES) == (200, 100, 100)
    assert _capability_counts(queries) == {
        "definition": 200,
        "references": 100,
        "calls": 100,
    }
    assert len(queries) == 400
    assert min(_expected_location_counts(queries)) >= 1
    assert {_location_shape(location) for location in _gold_locations(queries)} == {
        (True, 64, True, True)
    }
    assert _cross_file(_queries_of(queries, "definition")) == {True}
    assert min(_expected_location_counts(_queries_of(queries, "references"))) >= 5
    calls = _queries_of(queries, "calls")
    assert _directions(calls) == {"outgoing"}
    assert _cross_file(calls) == {True}


def _character_offset_bytes(repository, query) -> int:
    """How many bytes of the query's line precede its character."""
    text = (repository.root / query.path).read_text(encoding="utf-8")
    prefix = text.splitlines()[query.line - 1][: query.codepoint_character]
    return len(prefix.encode("utf-8"))


def _is_unicode_query(repository, query) -> bool:
    """A query is unicode-bearing by its symbol, or by a byte offset that differs."""
    if any(ord(character) > 127 for character in query.symbol):
        return True
    return query.character != _character_offset_bytes(repository, query)


def test_unicode_query_characters_are_utf8_byte_offsets(
    generated_pair: tuple[QualificationRepository, QualificationRepository],
) -> None:
    repository = generated_pair[0]
    unicode_queries = [
        query
        for query in repository.gold_queries
        if _is_unicode_query(repository, query)
    ]
    assert unicode_queries
    for query in unicode_queries:
        content = (repository.root / query.path).read_bytes()
        line_start = _line_start_bytes(content, query.line)
        assert query.byte_offset == line_start + query.character
        assert content[query.byte_offset : query.byte_end] == query.symbol.encode("utf-8")
