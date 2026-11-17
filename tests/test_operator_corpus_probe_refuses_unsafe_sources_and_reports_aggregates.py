from __future__ import annotations

import importlib
import importlib.util
import os
import sys
import time
import tokenize
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from repository_scope import resolve_repository_scope

helpers = importlib.import_module("tests.retrieval_v2_helpers")
operator_corpus = importlib.import_module("navigation_benchmark_operator_corpus")
deadlines = importlib.import_module("navigation_benchmark_deadlines")
SCRIPTS = helpers.BENCHMARK.parent / "scripts"


def _deadline() -> float:
    return time.monotonic() + 30


def _source(root: Path, relative: str, text: str) -> Path:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def test_a_source_in_a_nested_package_is_read_after_every_parent_is_checked(
    tmp_path: Path,
) -> None:
    source = _source(tmp_path, "a/b/c/module.py", "class Nested:\n    pass\n")

    definition = operator_corpus._operator_definition(
        source, tmp_path, operator_root=tmp_path, deadline=_deadline()
    )

    assert definition == ("a/b/c/module.py", 1, len("class "))


def test_a_source_under_a_symlinked_parent_is_refused(tmp_path: Path) -> None:
    _source(tmp_path / "outside", "module.py", "def private():\n    pass\n")
    root = tmp_path / "root"
    root.mkdir()
    (root / "linked").symlink_to(tmp_path / "outside", target_is_directory=True)

    with pytest.raises(PermissionError, match="parent must be a regular directory"):
        operator_corpus._read_operator_source(
            root / "linked" / "module.py", root, deadline=_deadline()
        )


def test_a_source_that_grows_past_the_limit_while_read_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _source(tmp_path, "module.py", "x = 1\n" * 4)
    monkeypatch.setattr(operator_corpus, "OPERATOR_MAX_SOURCE_BYTES", 8)
    descriptor = os.open(source, os.O_RDONLY)
    try:
        with pytest.raises(ValueError, match="operator source exceeds the byte limit"):
            operator_corpus._read_operator_chunks(descriptor, _deadline())
    finally:
        os.close(descriptor)


def test_a_source_rewritten_before_it_is_opened_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _source(tmp_path, "module.py", "def first():\n    pass\n")
    real_open = operator_corpus._open_operator_source

    def rewrite_then_open(path: Path, flags: int) -> Any:
        path.write_text("def a_longer_replacement():\n    pass\n", encoding="utf-8")
        return real_open(path, flags)

    monkeypatch.setattr(operator_corpus, "_open_operator_source", rewrite_then_open)

    with pytest.raises(PermissionError, match="operator source changed before open"):
        operator_corpus._read_operator_source(source, tmp_path, deadline=_deadline())


def test_a_source_replaced_after_its_read_is_refused(tmp_path: Path) -> None:
    source = _source(tmp_path, "module.py", "def first():\n    pass\n")
    stale_state = operator_corpus._expected_operator_state(source)
    source.unlink()
    _source(tmp_path, "module.py", "def replacement():\n    pass\n")

    with pytest.raises(PermissionError, match="operator source was replaced during read"):
        operator_corpus._require_source_not_replaced(source, stale_state)


def test_a_source_without_any_definition_offers_no_query(tmp_path: Path) -> None:
    source = _source(tmp_path, "constants.py", "VALUE = 1\nOTHER = VALUE + 1\n")

    assert (
        operator_corpus._operator_definition(
            source, tmp_path, operator_root=tmp_path, deadline=_deadline()
        )
        is None
    )


def _token(kind: int, text: str) -> SimpleNamespace:
    return SimpleNamespace(type=kind, string=text)


@pytest.mark.parametrize(
    ("token", "expect_name", "action"),
    [
        (_token(tokenize.NAME, "def"), False, "start"),
        (_token(tokenize.NAME, "value"), False, None),
        (_token(tokenize.NAME, "value"), True, "name"),
        (_token(tokenize.OP, "("), True, "reset"),
        (_token(tokenize.NL, "\n"), True, None),
        (_token(tokenize.INDENT, "    "), True, None),
    ],
)
def test_a_definition_name_is_awaited_only_after_def_or_class(
    token: SimpleNamespace, expect_name: bool, action: str | None
) -> None:
    assert operator_corpus._definition_token_action(token, expect_name) == action


def test_a_missing_operator_root_is_unavailable(tmp_path: Path) -> None:
    with pytest.raises(operator_corpus._OperatorTraversalError, match="root is unavailable"):
        operator_corpus._require_operator_root(tmp_path / "missing")


def test_a_directory_that_vanished_before_its_visit_is_unavailable(tmp_path: Path) -> None:
    with pytest.raises(
        operator_corpus._OperatorTraversalError, match="operator directory is unavailable"
    ):
        operator_corpus._directory_identity(tmp_path / "vanished")


def test_a_directory_that_cannot_be_listed_fails_the_traversal(tmp_path: Path) -> None:
    scan = operator_corpus._OperatorScan(tmp_path, _deadline())
    plain_file = _source(tmp_path, "file.txt", "text")

    with pytest.raises(
        operator_corpus._OperatorTraversalError, match="operator directory cannot be scanned"
    ):
        operator_corpus._scan_entries(scan, plain_file)


def test_an_entry_deleted_after_listing_cannot_be_inspected(tmp_path: Path) -> None:
    source = _source(tmp_path, "module.py", "x = 1\n")
    with os.scandir(tmp_path) as iterator:
        entries = list(iterator)
    source.unlink()

    with pytest.raises(
        operator_corpus._OperatorTraversalError, match="operator entry cannot be inspected"
    ):
        operator_corpus._entry_metadata(entries[0])


def test_a_python_file_that_resolves_outside_the_root_escaped_it(tmp_path: Path) -> None:
    outside = _source(tmp_path, "outside/module.py", "x = 1\n")
    root = tmp_path / "root"
    root.mkdir()

    with pytest.raises(operator_corpus._OperatorTraversalError, match="escaped its root"):
        operator_corpus._contained_python_file(outside, root.resolve())


class TimingOutNavigation:
    def query(self, request: object, *, deadline: float) -> object:
        raise deadlines.BenchmarkTimeoutError("benchmark run deadline exceeded")


def test_a_benchmark_timeout_during_a_query_is_never_counted_as_a_probe_error(
    tmp_path: Path,
) -> None:
    source = _source(tmp_path, "module.py", "def target():\n    pass\n")
    scope = resolve_repository_scope(tmp_path)
    tally = operator_corpus._ProbeTally()

    with pytest.raises(deadlines.BenchmarkTimeoutError):
        operator_corpus._probe_files(
            TimingOutNavigation(), scope, tmp_path, [source], tally, _deadline()
        )
    assert (tally.attempts, tally.errors, tally.available) == (1, 0, True)


class NeverClosingNavigation:
    def __init__(self) -> None:
        self.attempts = 0

    def close(self, *, deadline: float) -> None:
        self.attempts += 1
        raise RuntimeError("server will not stop")


def test_a_navigation_that_refuses_both_closes_counts_two_errors() -> None:
    navigation = NeverClosingNavigation()
    tally = operator_corpus._ProbeTally()

    operator_corpus._close_operator_navigation(navigation, tally)

    assert navigation.attempts == 2
    assert (tally.errors, tally.available) == (2, False)


AGGREGATE = {
    "available": True,
    "python_files": 2,
    "queries_attempted": 2,
    "queries_succeeded": 1,
    "errors": 0,
}


@pytest.mark.parametrize(
    ("value", "message"),
    [
        (["not", "a", "dict"], "non-aggregate result"),
        ({**AGGREGATE, "paths": ["private.py"]}, "non-aggregate result"),
        ({**AGGREGATE, "available": "yes"}, "aggregate metrics are invalid"),
        ({**AGGREGATE, "errors": -1}, "aggregate metrics are invalid"),
    ],
)
def test_only_well_formed_aggregate_metrics_are_accepted(value: object, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        operator_corpus._operator_metrics(value)


def test_well_formed_aggregate_metrics_are_copied() -> None:
    accepted = operator_corpus._operator_metrics(AGGREGATE)

    assert accepted == AGGREGATE
    assert accepted is not AGGREGATE


def test_loading_the_module_puts_scripts_and_benchmark_on_the_import_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    roots = {str(SCRIPTS), str(helpers.BENCHMARK)}
    monkeypatch.setattr(sys, "path", [entry for entry in sys.path if entry not in roots])
    spec = importlib.util.spec_from_file_location(
        "navigation_benchmark_operator_corpus_copy",
        helpers.BENCHMARK / "navigation_benchmark_operator_corpus.py",
    )
    assert spec is not None
    assert spec.loader is not None

    spec.loader.exec_module(importlib.util.module_from_spec(spec))

    assert roots <= set(sys.path)
