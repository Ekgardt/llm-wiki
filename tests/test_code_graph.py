"""Tests for code_graph.py — tree-sitter code intelligence."""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from code_graph import (  # noqa: E402
    LANGUAGE_MAP,
    _get_parser,
    _louvain_communities,
    analyze_co_changes,
    detect_code_tools,
    detect_communities,
    detect_language,
    enrich_python_semantics,
    find_callers,
    find_dead_code,
    get_architecture,
    index_directory,
    parse_file,
    refine_call_edges_with_co_changes,
)
from code_languages import CODE_LANGUAGE_BY_SUFFIX  # noqa: E402
from import_resolver import (  # noqa: E402
    build_python_symbol_registry,
    resolve_python_imports_and_calls,
)


def _activate_graph(tmp_path, repository=None):
    from generation_catalog import GenerationCatalog
    from repository_scope import resolve_repository_scope

    from tests.test_evidence_graph_recovery import _publish, _rich_graph_records

    catalog = GenerationCatalog(tmp_path / "state")
    scope = resolve_repository_scope(repository or tmp_path)
    _publish(
        catalog,
        "active",
        graph_records=_rich_graph_records(),
        repository_scope=scope.as_dict(),
    )
    catalog.register("active")
    catalog.activate("active", expected_active=None)
    return catalog


def _activate_nested_repository_graph(tmp_path, monkeypatch):
    repository = tmp_path / "repository"
    nested = repository / "src" / "feature"
    nested.mkdir(parents=True)
    subprocess.run(
        ["git", "init", str(repository)],
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )
    source = repository / "app.py"
    source.write_text("def caller():\n    callee()\n", encoding="utf-8")
    catalog = _activate_graph(tmp_path, repository)
    monkeypatch.setenv("LLM_WIKI_STATE_ROOT", str(catalog.state_root))
    return repository, nested, source


def test_code_graph_importable_as_package():
    result = subprocess.run(
        [sys.executable, "-c", "import scripts.code_graph"],
        cwd=Path(__file__).resolve().parent.parent,
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert result.returncode == 0, result.stderr


def test_code_extractor_importable_as_package():
    result = subprocess.run(
        [sys.executable, "-c", "import scripts.code_extractor"],
        cwd=Path(__file__).resolve().parent.parent,
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert result.returncode == 0, result.stderr


def test_public_facades_query_active_evidence_graph_before_live_scan(tmp_path, monkeypatch):
    import code_graph

    catalog = _activate_graph(tmp_path)
    monkeypatch.setattr(code_graph, "_generation_catalog", lambda directory: catalog)
    monkeypatch.setattr(
        code_graph,
        "_workspace_call_graph",
        lambda directory: (_ for _ in ()).throw(AssertionError("live scan used")),
    )

    callers = code_graph.find_callers("callee", tmp_path)
    architecture = code_graph.get_architecture(tmp_path)
    located = [(Path(item["file"]).name, item["line"]) for item in callers]

    assert (located, code_graph.find_callers("missing", tmp_path)) == (
        [("app.py", 2)],
        [],
    )
    assert (
        code_graph.find_callees("caller", tmp_path)[0]["callee"],
        bool(code_graph.find_dead_code(tmp_path)),
        isinstance(code_graph.detect_communities(tmp_path), list),
        architecture["graph_complete"],
        architecture["unresolved_count"],
    ) == ("callee", True, True, False, 1)


def test_store_first_reports_generation_completeness_and_unresolved_count(
    tmp_path, monkeypatch
):
    import code_graph

    catalog = _activate_graph(tmp_path)
    monkeypatch.setattr(code_graph, "_generation_catalog", lambda directory: catalog)

    callers = code_graph.find_callers("callee", tmp_path, with_report=True)
    architecture = code_graph.get_architecture(tmp_path, with_report=True)

    assert (
        callers["callers"][0]["qualified_name"],
        callers["source_generation"],
        callers["graph_complete"],
        callers["unresolved_count"],
        callers["fallback"],
    ) == ("caller", "active", False, 1, False)
    assert (architecture["source_generation"], architecture["unresolved_count"]) == (
        "active",
        1,
    )


def test_dependency_and_path_facades_prefer_active_generation(tmp_path, monkeypatch):
    import code_graph

    catalog = _activate_graph(tmp_path)
    monkeypatch.setattr(code_graph, "_generation_catalog", lambda directory: catalog)
    monkeypatch.setattr(
        code_graph,
        "_workspace_call_graph",
        lambda directory: (_ for _ in ()).throw(AssertionError("live scan used")),
    )

    dependencies = code_graph.find_dependencies("caller", tmp_path, with_report=True)
    paths = code_graph.find_paths("caller", "callee", tmp_path, with_report=True)

    named = [item["node_id"] for item in dependencies["dependencies"]]

    assert (named, paths["paths"]) == (
        ["callee"],
        [{"node_ids": ["caller", "callee"], "assertion_ids": ["assertion"], "depth": 1}],
    )
    assert (
        dependencies["source_generation"],
        paths["source_generation"],
        dependencies["fallback"],
        paths["fallback"],
    ) == ("active", "active", False, False)


def test_live_fallback_report_is_explicit_when_generation_is_missing(tmp_path):
    import code_graph

    (tmp_path / "live.py").write_text(
        "def target(): pass\ndef caller(): target()\n", encoding="utf-8"
    )

    result = code_graph.find_callers("target", tmp_path, with_report=True)

    assert (
        bool(result["callers"]),
        result["source_generation"],
        result["graph_complete"],
        isinstance(result["unresolved_count"], int),
        result["fallback"],
    ) == (True, None, False, True, True)


def test_repository_binding_rejects_same_basename_repository_in_shared_state(
    tmp_path, monkeypatch
):
    import code_graph
    from generation_catalog import GenerationCatalog
    from repository_scope import resolve_repository_scope

    from tests.test_evidence_graph_recovery import _publish, _rich_graph_records

    repository_a = tmp_path / "owner-a" / "project"
    repository_b = tmp_path / "owner-b" / "project"
    repository_a.mkdir(parents=True)
    repository_b.mkdir(parents=True)
    (repository_b / "b.py").write_text("def b_only_symbol():\n    pass\n", encoding="utf-8")
    records = _rich_graph_records()
    for node in records["nodes"]:
        node["metadata"]["name"] = "a_only_symbol"
    state = tmp_path / "shared-state"
    catalog = GenerationCatalog(state)
    _publish(
        catalog,
        "repo-a",
        graph_records=records,
        repository_scope=resolve_repository_scope(repository_a).as_dict(),
    )
    catalog.register("repo-a")
    assert catalog.activate("repo-a", expected_active=None)
    monkeypatch.setenv("LLM_WIKI_STATE_ROOT", str(state))

    dead = code_graph.find_dead_code(repository_b, with_report=True)
    architecture = code_graph.get_architecture(repository_b, with_report=True)

    leaked = [
        (
            report["source_generation"],
            report["fallback"],
            "a_only_symbol" in json.dumps(report),
            str(repository_a) in json.dumps(report),
        )
        for report in (dead, architecture)
    ]

    assert leaked == [(None, True, False, False)] * 2


def test_repository_binding_exact_scope_still_uses_store(tmp_path, monkeypatch):
    import code_graph

    repository = tmp_path / "repository"
    repository.mkdir()
    catalog = _activate_graph(tmp_path, repository)
    monkeypatch.setenv("LLM_WIKI_STATE_ROOT", str(catalog.state_root))
    monkeypatch.setattr(
        code_graph,
        "_workspace_call_graph",
        lambda directory: (_ for _ in ()).throw(AssertionError("live scan used")),
    )

    report = code_graph.find_dead_code(repository, with_report=True)

    assert report["source_generation"] == "active"
    assert report["fallback"] is False


def test_external_repository_uses_canonical_state_root_without_env(tmp_path, monkeypatch):
    import code_graph
    import memory_state

    repository = tmp_path / "external-repository"
    repository.mkdir()
    catalog = _activate_graph(tmp_path, repository)
    monkeypatch.delenv("LLM_WIKI_STATE_ROOT", raising=False)
    monkeypatch.setattr(memory_state, "STATE_ROOT", catalog.state_root)
    monkeypatch.setattr(
        code_graph,
        "_workspace_call_graph",
        lambda directory: (_ for _ in ()).throw(AssertionError("live scan used")),
    )

    report = code_graph.find_dead_code(repository, with_report=True)

    assert report["source_generation"] == "active"
    assert report["fallback"] is False


def test_nested_directory_store_renders_occurrence_from_checkout_root(
    tmp_path, monkeypatch
):
    import code_graph

    repository, nested, source = _activate_nested_repository_graph(tmp_path, monkeypatch)

    report = code_graph.find_dead_code(nested, with_report=True)

    caller = next(item for item in report["candidates"] if item["name"] == "caller")

    assert (
        Path(caller["file"]),
        Path(caller["file"]) == nested / "app.py",
        report["source_scope"],
        Path(report["source_root"]),
    ) == (source, False, "checkout", repository)


def test_nested_directory_store_renders_edge_evidence_from_checkout_root(
    tmp_path, monkeypatch
):
    import code_graph

    repository, nested, source = _activate_nested_repository_graph(tmp_path, monkeypatch)

    report = code_graph.find_callers("callee", nested, with_report=True)

    assert Path(report["callers"][0]["file"]) == source
    assert Path(report["callers"][0]["file"]) != nested / "app.py"
    assert report["source_scope"] == "checkout"
    assert Path(report["source_root"]) == repository


def test_dependency_and_path_facades_use_bounded_live_fallback_without_generation(
    tmp_path,
):
    import code_graph

    (tmp_path / "live.py").write_text(
        "def target(): pass\ndef caller(): target()\n", encoding="utf-8"
    )

    dependencies = code_graph.find_dependencies("caller", tmp_path, with_report=True)
    paths = code_graph.find_paths("caller", "target", tmp_path, with_report=True)

    assert [item["metadata"]["name"] for item in dependencies["dependencies"]] == [
        "target"
    ]
    assert paths["paths"][0]["depth"] == 1
    assert dependencies["fallback"] is paths["fallback"] is True


def test_community_cache_is_bounded_and_scoped_to_pinned_generation(
    tmp_path, monkeypatch
):
    import code_graph

    catalog = _activate_graph(tmp_path)
    monkeypatch.setattr(code_graph, "_generation_catalog", lambda directory: catalog)
    graph = code_graph._active_evidence_graph(tmp_path)
    calls = graph.edges(edge_types=("CALLS",), max_rows=10_000)
    computed = []
    monkeypatch.setattr(
        code_graph,
        "_louvain_communities",
        lambda value: computed.append(value) or [["caller", "callee"]],
    )

    try:
        repeated = [code_graph._stored_communities(graph, calls) for _ in range(2)]
        cached = len(graph._derived_code_graph_cache)
    finally:
        graph.close()

    assert repeated == [[["caller", "callee"]]] * 2
    assert (len(computed), cached <= code_graph.MAX_DERIVED_COMMUNITY_CACHE) == (1, True)


def test_generation_catalog_can_open_existing_catalog_read_only(tmp_path, monkeypatch):
    import code_graph
    import generation_catalog

    catalog = _activate_graph(tmp_path)
    monkeypatch.setenv("LLM_WIKI_STATE_ROOT", str(catalog.state_root))
    captured = {}
    deadline = time.monotonic() + 5

    def cancelled():
        return False

    original_open = generation_catalog.GenerationCatalog.open_existing_read_only

    def capture_open(_cls, state_root, **options):
        captured.update(options)
        return original_open(state_root, **options)

    monkeypatch.setattr(
        generation_catalog.GenerationCatalog,
        "open_existing_read_only",
        classmethod(capture_open),
    )

    reader = code_graph._generation_catalog(
        tmp_path,
        read_only=True,
        deadline=deadline,
        cancelled=cancelled,
    )

    assert reader is not None
    assert reader._read_only is True
    assert reader.get_active()["generation_id"] == "active"
    assert captured == {
        "catalog_path": catalog.catalog_path,
        "deadline": deadline,
        "cancelled": cancelled,
    }


def test_active_evidence_graph_forwards_read_only_deadline_and_cancellation(
    tmp_path, monkeypatch
):
    import code_graph
    import evidence_graph
    import repository_scope

    captured = {}
    catalog = object()
    graph = object()

    def cancelled():
        return False

    deadline = time.monotonic() + 5
    original_resolve = repository_scope.resolve_repository_scope

    def open_catalog(directory, **options):
        captured["catalog"] = (directory, options)
        return catalog

    def open_graph(received_catalog, scope, **options):
        captured["graph"] = (received_catalog, scope, options)
        return graph

    def resolve_scope(directory, **options):
        captured["scope"] = (directory, options)
        return original_resolve(directory, **options)

    monkeypatch.setattr(code_graph, "_generation_catalog", open_catalog)
    monkeypatch.setattr(
        evidence_graph.EvidenceGraph,
        "open_active_for_repository",
        open_graph,
    )
    monkeypatch.setattr(repository_scope, "resolve_repository_scope", resolve_scope)

    opened = code_graph._active_evidence_graph(
        tmp_path,
        read_only=True,
        deadline=deadline,
        cancelled=cancelled,
    )
    forwarded = {"deadline": deadline, "cancelled": cancelled}

    assert (opened is graph, captured["graph"][0] is catalog) == (True, True)
    assert (captured["catalog"], captured["scope"], captured["graph"][2]) == (
        (tmp_path, {"read_only": True, **forwarded}),
        (tmp_path, forwarded),
        forwarded,
    )


def test_active_evidence_graph_preserves_legacy_no_keyword_path(tmp_path, monkeypatch):
    import code_graph
    import evidence_graph
    import repository_scope

    catalog = object()
    graph = object()
    calls = []

    def open_catalog(directory):
        calls.append(("catalog", directory))
        return catalog

    def resolve_scope(directory):
        calls.append(("scope", directory))
        return object()

    def open_graph(received_catalog, _scope):
        calls.append(("graph", received_catalog))
        return graph

    monkeypatch.setattr(code_graph, "_generation_catalog", open_catalog)
    monkeypatch.setattr(repository_scope, "resolve_repository_scope", resolve_scope)
    monkeypatch.setattr(
        evidence_graph.EvidenceGraph,
        "open_active_for_repository",
        open_graph,
    )

    assert code_graph._active_evidence_graph(tmp_path) is graph
    assert calls == [
        ("catalog", tmp_path),
        ("scope", tmp_path),
        ("graph", catalog),
    ]


def test_active_evidence_graph_propagates_delayed_scope_deadline(
    tmp_path, monkeypatch
):
    import code_graph
    import repository_scope

    captured = {}
    deadline = time.monotonic() + 5

    def cancelled():
        return False

    monkeypatch.setattr(
        code_graph,
        "_generation_catalog",
        lambda _directory, **_options: object(),
    )

    def delayed_scope(directory, **options):
        captured.update(directory=directory, options=options)
        raise TimeoutError("repository scope deadline reached")

    monkeypatch.setattr(repository_scope, "resolve_repository_scope", delayed_scope)

    with pytest.raises(TimeoutError, match="scope deadline"):
        code_graph._active_evidence_graph(
            tmp_path,
            read_only=True,
            deadline=deadline,
            cancelled=cancelled,
        )

    assert captured == {
        "directory": tmp_path,
        "options": {"deadline": deadline, "cancelled": cancelled},
    }


def test_active_evidence_graph_propagates_timeout(tmp_path, monkeypatch):
    import code_graph

    def timed_out(_directory, **_options):
        raise TimeoutError("catalog deadline reached")

    monkeypatch.setattr(code_graph, "_generation_catalog", timed_out)

    with pytest.raises(TimeoutError, match="deadline"):
        code_graph._active_evidence_graph(
            tmp_path,
            read_only=True,
            deadline=time.monotonic() + 5,
            cancelled=lambda: False,
        )


def test_store_facades_switch_only_after_generation_activation(tmp_path, monkeypatch):
    import code_graph
    from evidence_graph import EvidenceGraph
    from generation_catalog import GenerationCatalog
    from repository_scope import resolve_repository_scope

    from tests.test_evidence_graph_recovery import _publish, _rich_graph_records

    catalog = GenerationCatalog(tmp_path / "state")
    scope = resolve_repository_scope(tmp_path).as_dict()
    _publish(
        catalog,
        "prior",
        graph_records=_rich_graph_records(),
        repository_scope=scope,
    )
    catalog.register("prior")
    catalog.activate("prior", expected_active=None)
    monkeypatch.setattr(code_graph, "_generation_catalog", lambda directory: catalog)

    _publish(
        catalog,
        "next",
        parent="prior",
        graph_records=_rich_graph_records(),
        repository_scope=scope,
    )
    catalog.register("next")
    entered = threading.Event()
    release = threading.Event()
    real_edges = EvidenceGraph.edges

    def paused_edges(graph, **options):
        if graph.generation_id == "prior":
            entered.set()
            assert release.wait(5)
        return real_edges(graph, **options)

    monkeypatch.setattr(EvidenceGraph, "edges", paused_edges)
    with ThreadPoolExecutor(max_workers=1) as pool:
        reader = pool.submit(
            code_graph.find_callers, "callee", tmp_path, with_report=True
        )
        assert entered.wait(5)
        catalog.activate("next", expected_active="prior")
        release.set()
        during = reader.result(timeout=5)

    assert during["source_generation"] == "prior"
    after = code_graph.find_callers("callee", tmp_path, with_report=True)
    assert after["source_generation"] == "next"


def test_store_hotspots_count_distinct_callers_not_call_sites(tmp_path, monkeypatch):
    import code_graph
    from generation_catalog import GenerationCatalog
    from repository_scope import resolve_repository_scope

    from tests.test_evidence_graph_recovery import _publish, _rich_graph_records

    records = _rich_graph_records()
    records["assertions"].append(
        {**records["assertions"][0], "assertion_id": "assertion-2"}
    )
    records["evidence"].append(
        {
            **records["evidence"][0],
            "evidence_id": "evidence-2",
            "assertion_id": "assertion-2",
        }
    )
    catalog = GenerationCatalog(tmp_path / "state")
    _publish(
        catalog,
        "active",
        graph_records=records,
        repository_scope=resolve_repository_scope(tmp_path).as_dict(),
    )
    catalog.register("active")
    catalog.activate("active", expected_active=None)
    monkeypatch.setattr(code_graph, "_generation_catalog", lambda directory: catalog)

    hotspots = code_graph.get_architecture(tmp_path)["hotspots"]

    assert hotspots[0]["incoming_callers"] == 1


def test_explicit_live_request_bypasses_active_store(tmp_path, monkeypatch):
    import code_graph

    catalog = _activate_graph(tmp_path)
    monkeypatch.setattr(code_graph, "_generation_catalog", lambda directory: catalog)
    (tmp_path / "live.py").write_text(
        "def target(): pass\ndef caller(): target()\n", encoding="utf-8"
    )
    for name in (
        "_store_find_callers", "_store_find_callees", "_store_find_dead_code",
        "_store_get_architecture", "_store_detect_communities",
    ):
        monkeypatch.setattr(
            code_graph,
            name,
            lambda *_args: (_ for _ in ()).throw(AssertionError("store used")),
        )

    assert (
        bool(code_graph.find_callers("target", tmp_path, live=True)),
        bool(code_graph.find_callees("caller", tmp_path, live=True)),
        bool(code_graph.find_dead_code(tmp_path, live=True)),
        code_graph.get_architecture(tmp_path, live=True)["graph_complete"],
        isinstance(code_graph.detect_communities(tmp_path, live=True), list),
    ) == (True, True, True, False, True)


def test_invalid_active_store_uses_live_fallback(tmp_path, monkeypatch):
    import sqlite3

    import code_graph

    monkeypatch.setattr(
        code_graph,
        "_generation_catalog",
        lambda directory: (_ for _ in ()).throw(sqlite3.DatabaseError("invalid")),
    )
    (tmp_path / "live.py").write_text("def target(): pass\ntarget()\n", encoding="utf-8")

    assert code_graph.find_callers("target", tmp_path)


class TestDetectLanguage:
    """Test language detection from file extension."""

    def test_python(self):
        assert detect_language(Path("test.py")) == "python"

    def test_javascript(self):
        assert detect_language(Path("app.js")) == "javascript"

    def test_typescript(self):
        assert detect_language(Path("app.ts")) == "typescript"

    def test_tsx(self):
        assert detect_language(Path("component.tsx")) == "typescript"

    def test_unknown(self):
        assert detect_language(Path("readme.md")) is None

    @pytest.mark.parametrize(
        ("name", "language"),
        [("APP.PY", "python"), ("component.generated.D.TS", "typescript")],
    )
    def test_final_suffix_is_case_insensitive(self, name, language):
        assert detect_language(Path(name)) == language

    @pytest.mark.parametrize(
        ("suffix", "language"), sorted(CODE_LANGUAGE_BY_SUFFIX.items())
    )
    def test_all_supported_suffixes(self, suffix, language):
        assert detect_language(Path(f"example{suffix}")) == language

    def test_language_map_has_independent_core_contract(self):
        expected = {
            ".py": "python",
            ".ts": "typescript",
            ".cpp": "cpp",
            ".cs": "c_sharp",
            ".sh": "bash",
        }

        assert {
            suffix: CODE_LANGUAGE_BY_SUFFIX.get(suffix) for suffix in expected
        } == expected


LANGUAGE_CASES = [
    ("go", "example.go", 'package demo\nimport "fmt"\ntype Greeter struct{}\nfunc greet() { fmt.Println("hi") }\n', "greet", "Greeter", "Println"),
    ("rust", "example.rs", "use std::fmt;\nstruct Greeter;\nfn greet() { println!(\"hi\"); }\n", "greet", "Greeter", "println"),
    ("java", "Example.java", "import java.util.List;\nclass Greeter { void greet() { System.out.println(\"hi\"); } }\n", "greet", "Greeter", "println"),
    ("c", "example.c", "#include <stdio.h>\nstruct Greeter { int value; };\nvoid greet(void) { puts(\"hi\"); }\n", "greet", "Greeter", "puts"),
    ("cpp", "example.cpp", "#include <iostream>\nclass Greeter {};\nvoid greet() { std::puts(\"hi\"); }\n", "greet", "Greeter", "puts"),
    ("ruby", "example.rb", "require 'json'\nclass Greeter\n  def greet\n    puts 'hi'\n  end\nend\n", "greet", "Greeter", "puts"),
    ("php", "example.php", "<?php\nrequire 'vendor.php';\nclass Greeter {}\nfunction greet() { print_message(); }\n", "greet", "Greeter", "print_message"),
    ("c_sharp", "Example.cs", "using System;\nclass Greeter { void Greet() { Console.WriteLine(\"hi\"); } }\n", "Greet", "Greeter", "WriteLine"),
    ("bash", "example.sh", "#!/usr/bin/env bash\nsource ./common.sh\ngreet() { printf 'hi\\n'; }\ngreet\n", "greet", None, "printf"),
]

FALLBACK_CASES = [
    ("fallback.go", 'package demo\nimport "fmt"\ntype Greeter struct{}\nfunc greet() { fmt.Println("hi") }\n', "greet", "Greeter", "Println", "fmt"),
    ("fallback.rs", 'use std::fmt;\nstruct Greeter;\nfn greet() { println!("hi"); }\n', "greet", "Greeter", "println", "std::fmt"),
    ("Fallback.java", 'import java.util.List;\nclass Greeter { void greet() { System.out.println("hi"); } }\n', "greet", "Greeter", "println", "java.util.List"),
    ("fallback.c", '#include <stdio.h>\nstruct Greeter { int value; };\nvoid greet(void) { puts("hi"); }\n', "greet", "Greeter", "puts", "stdio.h"),
    ("fallback.cpp", '#include <cstdio>\nclass Greeter {};\nvoid greet() { std::puts("hi"); }\n', "greet", "Greeter", "puts", "cstdio"),
    ("fallback.rb", "require 'json'\nclass Greeter\n  def greet\n    puts 'hi'\n  end\nend\n", "greet", "Greeter", "puts", "json"),
    ("fallback.php", "<?php\nuse App\\Service;\nclass Greeter {}\nfunction greet() { print_message(); }\n", "greet", "Greeter", "print_message", "App\\Service"),
    ("Fallback.cs", 'using System;\nclass Greeter { void Greet() { Console.WriteLine("hi"); } }\n', "Greet", "Greeter", "WriteLine", "System"),
    ("fallback.sh", "source ./common.sh\ngreet() { printf 'hi\\n'; }\ngreet\n", "greet", None, "printf", "./common.sh"),
]


def _parsed_language_case(tmp_path, filename: str, source: str) -> dict:
    """Write one case-table row to disk and parse it."""
    path = tmp_path / filename
    path.write_text(source, encoding="utf-8")
    return parse_file(path)


def _assert_extracted_names(result, function, class_name, call, filename) -> None:
    """The names one case expects; an empty `class_name` means it declares none.

    Pulled out of the two case-table loops so each loop stays one branch: the
    complexity gate counts every `assert` and every `if`, and a per-case body
    inlined in a loop puts the whole table's assertions on one function.
    """
    expected_classes = {class_name} if class_name else set()
    assert function in {item["name"] for item in result["functions"]}, filename
    assert expected_classes <= {item["name"] for item in result["classes"]}, filename
    assert call in {item["name"] for item in result["calls"]}, filename


class TestParseFile:
    """Test source file parsing."""

    def test_parse_python_file(self, tmp_path):
        """Parse a simple Python file and extract functions."""
        f = tmp_path / "example.py"
        f.write_text(
            "def hello():\n"
            "    print('world')\n\n"
            "def goodbye():\n"
            "    hello()\n",
            encoding="utf-8",
        )
        result = parse_file(f)
        names = [f["name"] for f in result["functions"]]

        assert (result["language"], len(result["functions"]) >= 2) == ("python", True)
        assert {"hello", "goodbye"} <= set(names)

    def test_parse_javascript_file(self, tmp_path):
        f = tmp_path / "app.js"
        f.write_text(
            "function greet() { return 'hi'; }\n"
            "const farewell = () => { return 'bye'; }\n",
            encoding="utf-8",
        )
        result = parse_file(f)
        assert result["language"] == "javascript"
        assert len(result["functions"]) >= 1

    def test_parse_typescript_file(self, tmp_path):
        f = tmp_path / "svc.ts"
        f.write_text(
            "class Service {\n"
            "  fetch(): void {}\n"
            "}\n",
            encoding="utf-8",
        )
        result = parse_file(f)
        assert result["language"] == "typescript"

    def test_parse_unknown_file(self, tmp_path):
        f = tmp_path / "readme.md"
        f.write_text("# Hello\n", encoding="utf-8")
        result = parse_file(f)
        assert result["language"] is None
        assert result["functions"] == []

    def test_parse_extracts_line_numbers(self, tmp_path):
        f = tmp_path / "lined.py"
        f.write_text("\n\ndef func():\n    pass\n", encoding="utf-8")
        result = parse_file(f)
        assert len(result["functions"]) >= 1
        assert result["functions"][0]["line"] >= 3

    @staticmethod
    def test_new_languages_use_real_snippets_and_extract_with_installed_grammars(tmp_path):
        for language, filename, source, function, class_name, call in LANGUAGE_CASES:
            result = _parsed_language_case(tmp_path, filename, source)

            assert result["language"] == language
            if importlib.util.find_spec("tree_sitter_" + language) is not None:
                _assert_extracted_names(result, function, class_name, call, filename)

    def test_missing_grammar_uses_fallback_without_import_failure(self, tmp_path, monkeypatch):
        import code_graph

        path = tmp_path / "example.go"
        path.write_text("package demo\nfunc greet() {}\n", encoding="utf-8")
        code_graph._ts.pop("go", None)
        monkeypatch.setattr("code_graph.importlib.import_module", lambda name: (_ for _ in ()).throw(ImportError(name)))

        result = parse_file(path)

        assert result["language"] == "go"
        assert {item["name"] for item in result["functions"]} == {"greet"}

    def test_grammar_loaders_are_lazy_and_cached(self, monkeypatch):
        import code_graph

        code_graph._ts.clear()
        imported = []
        monkeypatch.setattr(code_graph.importlib, "import_module", lambda name: imported.append(name) or (_ for _ in ()).throw(ImportError(name)))

        assert _get_parser("go") is None
        assert imported == ["tree_sitter_go"]

    def test_each_language_has_a_materialized_query_file(self):
        query_dir = Path(__file__).resolve().parent.parent / "scripts" / "queries"

        languages = set(LANGUAGE_MAP.values())
        bodies = {
            language: (query_dir / f"{language}.scm").read_text(encoding="utf-8")
            for language in languages
        }
        shaped = {
            language: (
                15 <= len(body.splitlines()) <= 35,
                "@function.name" in body,
                "@call.name" in body,
            )
            for language, body in bodies.items()
        }

        assert {path.stem for path in query_dir.glob("*.scm")} == languages
        assert shaped == {language: (True, True, True) for language in languages}

    def test_ruby_imports_only_require_or_load_and_capture_path(self, tmp_path):
        path = tmp_path / "imports.rb"
        path.write_text(
            "require 'json'\nload 'boot.rb'\nrequire_relative 'local'\nputs 'not-import'\n",
            encoding="utf-8",
        )

        imports = {item["name"] for item in parse_file(path)["imports"]}

        assert imports == {"json", "boot.rb"}
        assert "require" not in imports
        assert "not-import" not in imports

    def test_bash_imports_only_source_or_dot_and_capture_path(self, tmp_path):
        path = tmp_path / "imports.sh"
        path.write_text(
            "source ./common.sh\n. ./env.sh\nprintf './not-import.sh'\n",
            encoding="utf-8",
        )

        imports = {item["name"] for item in parse_file(path)["imports"]}

        assert imports == {"./common.sh", "./env.sh"}
        assert "source" not in imports
        assert "./not-import.sh" not in imports

    def test_php_imports_include_require_paths_and_use_declarations(self, tmp_path):
        path = tmp_path / "imports.php"
        path.write_text(
            "<?php\ninclude 'a.php';\nrequire_once 'b.php';\nuse App\\Service;\n"
            "namespace Demo;\nprint_message();\n",
            encoding="utf-8",
        )

        imports = {item["name"] for item in parse_file(path)["imports"]}

        assert imports == {"a.php", "b.php", "App\\Service"}
        assert "Demo" not in imports
        assert "print_message" not in imports

    def test_java_imports_never_include_package_declaration(self, tmp_path):
        path = tmp_path / "Imports.java"
        path.write_text(
            "package demo.app;\nimport java.util.List;\nclass Imports {}\n",
            encoding="utf-8",
        )

        imports = {item["name"] for item in parse_file(path)["imports"]}

        assert imports == {"java.util.List"}
        assert "demo.app" not in imports

    def test_all_new_languages_have_useful_regex_fallback(self, tmp_path, monkeypatch):
        monkeypatch.setattr("code_graph._get_parser", lambda language: None)

        for filename, source, function, class_name, call, imported in FALLBACK_CASES:
            result = _parsed_language_case(tmp_path, filename, source)

            _assert_extracted_names(result, function, class_name, call, filename)
            assert imported in {item["name"] for item in result["imports"]}, filename


class TestFindCallers:
    """Test caller search."""

    def test_find_callers_finds_function(self, tmp_path):
        """find_callers should locate calls to a function."""
        f1 = tmp_path / "a.py"
        f1.write_text("def target():\n    pass\n", encoding="utf-8")
        f2 = tmp_path / "b.py"
        f2.write_text("from a import target\n\ntarget()\n", encoding="utf-8")

        callers = find_callers("target", tmp_path)
        # Should find at least one call in b.py
        assert any("b.py" in c["file"] for c in callers)

    def test_find_callers_ignores_unbound_bare_call(self, tmp_path):
        (tmp_path / "a.py").write_text("def target():\n    pass\n", encoding="utf-8")
        (tmp_path / "b.py").write_text("target()\n", encoding="utf-8")

        assert find_callers("target", tmp_path) == []

    def test_find_callers_resolves_from_import_alias(self, tmp_path):
        (tmp_path / "auth.py").write_text("def verify():\n    pass\n", encoding="utf-8")
        (tmp_path / "service.py").write_text(
            "from auth import verify as check\n\ncheck()\n",
            encoding="utf-8",
        )

        callers = find_callers("verify", tmp_path)

        assert len(callers) == 1
        assert callers[0]["qualified_name"] == "auth.verify"
        assert callers[0]["confidence"] == "confirmed"

    def test_find_callers_resolves_module_import(self, tmp_path):
        (tmp_path / "auth.py").write_text("def verify():\n    pass\n", encoding="utf-8")
        clients = tmp_path / "clients"
        clients.mkdir()
        (clients / "service.py").write_text(
            "import auth as security\n\nsecurity.verify()\n",
            encoding="utf-8",
        )

        callers = find_callers("verify", tmp_path)

        assert len(callers) == 1
        assert callers[0]["qualified_name"] == "auth.verify"
        assert callers[0]["confidence"] == "confirmed"

    def test_parse_python_marks_static_and_dynamic_method_confidence(self, tmp_path):
        source = tmp_path / "service.py"
        source.write_text(
            "class Service:\n"
            "    def verify(self):\n"
            "        pass\n\n"
            "    def run(self, client):\n"
            "        self.verify()\n"
            "        Service.verify(self)\n"
            "        client.verify()\n",
            encoding="utf-8",
        )

        calls = parse_file(source)["calls"]

        by_name = {(call["name"], call["line"]): call for call in calls}
        assert by_name[("verify", 6)]["confidence"] == "confirmed"
        assert by_name[("verify", 7)]["confidence"] == "confirmed"
        assert by_name[("verify", 8)]["confidence"] == "unknown"

    def test_find_callers_does_not_guess_dynamic_method(self, tmp_path):
        (tmp_path / "service.py").write_text(
            "def run(client):\n    client.verify()\n",
            encoding="utf-8",
        )

        assert find_callers("verify", tmp_path) == []

    def test_nested_function_resolves_only_in_lexical_scope(self, tmp_path):
        source = tmp_path / "service.py"
        source.write_text(
            "def first():\n"
            "    def helper():\n"
            "        pass\n"
            "    helper()\n\n"
            "def second():\n"
            "    helper()\n",
            encoding="utf-8",
        )

        calls = {call["line"]: call for call in parse_file(source)["calls"]}

        assert calls[4]["confidence"] == "confirmed"
        assert calls[7]["confidence"] == "unknown"

    def test_function_import_resolves_only_in_lexical_scope(self, tmp_path):
        (tmp_path / "auth.py").write_text("def verify():\n    pass\n", encoding="utf-8")
        (tmp_path / "service.py").write_text(
            "def first():\n"
            "    from auth import verify\n"
            "    verify()\n\n"
            "def second():\n"
            "    verify()\n",
            encoding="utf-8",
        )

        callers = find_callers("verify", tmp_path)

        assert [call["line"] for call in callers] == [3]

    def test_missing_workspace_targets_are_not_confirmed(self, tmp_path):
        (tmp_path / "auth.py").write_text("def verify():\n    pass\n", encoding="utf-8")
        (tmp_path / "service.py").write_text(
            "import auth\n"
            "from auth import missing as absent\n\n"
            "auth.missing()\n"
            "absent()\n",
            encoding="utf-8",
        )

        assert find_callers("missing", tmp_path) == []

    def test_relative_import_resolves_from_current_package(self, tmp_path):
        package = tmp_path / "pkg"
        package.mkdir()
        (package / "__init__.py").write_text("", encoding="utf-8")
        (package / "auth.py").write_text("def verify():\n    pass\n", encoding="utf-8")
        (package / "service.py").write_text(
            "from .auth import verify\n\nverify()\n",
            encoding="utf-8",
        )

        callers = find_callers("verify", tmp_path)

        assert len(callers) == 1
        assert callers[0]["qualified_name"] == "pkg.auth.verify"

    def test_from_imported_module_resolves_attribute(self, tmp_path):
        package = tmp_path / "pkg"
        package.mkdir()
        (package / "__init__.py").write_text("", encoding="utf-8")
        (package / "auth.py").write_text("def verify():\n    pass\n", encoding="utf-8")
        (tmp_path / "service.py").write_text(
            "from pkg import auth\n\nauth.verify()\n",
            encoding="utf-8",
        )

        callers = find_callers("verify", tmp_path)

        assert len(callers) == 1
        assert callers[0]["qualified_name"] == "pkg.auth.verify"

    def test_call_before_import_is_not_confirmed(self, tmp_path):
        (tmp_path / "auth.py").write_text("def verify():\n    pass\n", encoding="utf-8")
        (tmp_path / "service.py").write_text(
            "verify()\nfrom auth import verify\nverify()\n",
            encoding="utf-8",
        )

        callers = find_callers("verify", tmp_path)

        assert [call["line"] for call in callers] == [3]

    def test_assignment_shadows_import_after_binding(self, tmp_path):
        (tmp_path / "auth.py").write_text("def verify():\n    pass\n", encoding="utf-8")
        (tmp_path / "service.py").write_text(
            "from auth import verify\n"
            "verify()\n"
            "verify = lambda: None\n"
            "verify()\n",
            encoding="utf-8",
        )

        callers = find_callers("verify", tmp_path)

        assert [call["line"] for call in callers] == [2]

    def test_parameter_and_local_definition_shadow_import(self, tmp_path):
        (tmp_path / "auth.py").write_text("def verify():\n    pass\n", encoding="utf-8")
        (tmp_path / "service.py").write_text(
            "from auth import verify\n\n"
            "def parameter_shadow(verify):\n"
            "    verify()\n\n"
            "verify()\n"
            "def verify():\n"
            "    pass\n"
            "verify()\n",
            encoding="utf-8",
        )

        callers = find_callers("verify", tmp_path)

        assert [call["line"] for call in callers] == [6, 9]
        assert callers[1]["qualified_name"] == "service.verify"

    def test_find_callers_empty_result(self, tmp_path):
        f = tmp_path / "x.py"
        f.write_text("def other():\n    pass\n", encoding="utf-8")
        callers = find_callers("nonexistent_func", tmp_path)
        assert callers == []

    def test_find_callers_keeps_javascript_name_heuristic(self, tmp_path):
        (tmp_path / "caller.js").write_text(
            "function run() { target(); }\n",
            encoding="utf-8",
        )

        callers = find_callers("target", tmp_path)

        assert len(callers) == 1
        assert callers[0]["confidence"] == "heuristic"

    def test_init_resolves_relative_symbol_import(self, tmp_path):
        package = tmp_path / "pkg"
        package.mkdir()
        (package / "auth.py").write_text("def verify():\n    pass\n", encoding="utf-8")
        (package / "__init__.py").write_text(
            "from .auth import verify\n\nverify()\n",
            encoding="utf-8",
        )

        callers = find_callers("verify", tmp_path)

        assert len(callers) == 1
        assert callers[0]["qualified_name"] == "pkg.auth.verify"

    def test_init_resolves_relative_module_import(self, tmp_path):
        package = tmp_path / "pkg"
        package.mkdir()
        (package / "auth.py").write_text("def verify():\n    pass\n", encoding="utf-8")
        (package / "__init__.py").write_text(
            "from . import auth\n\nauth.verify()\n",
            encoding="utf-8",
        )

        callers = find_callers("verify", tmp_path)

        assert len(callers) == 1
        assert callers[0]["qualified_name"] == "pkg.auth.verify"

    def test_parameter_shadows_class_name(self, tmp_path):
        source = tmp_path / "service.py"
        source.write_text(
            "class Service:\n"
            "    def verify(self):\n"
            "        pass\n\n"
            "def run(Service):\n"
            "    Service.verify()\n",
            encoding="utf-8",
        )

        call = next(call for call in parse_file(source)["calls"] if call["line"] == 6)

        assert call["confidence"] == "unknown"

    def test_class_scope_is_not_method_lexical_parent(self, tmp_path):
        source = tmp_path / "service.py"
        source.write_text(
            "class Service:\n"
            "    def helper(self):\n"
            "        pass\n\n"
            "    def run(self):\n"
            "        helper()\n",
            encoding="utf-8",
        )

        call = next(call for call in parse_file(source)["calls"] if call["line"] == 6)

        assert call["confidence"] == "unknown"

    def test_dotted_module_import_resolves_long_call_chain(self, tmp_path):
        package = tmp_path / "pkg"
        package.mkdir()
        (package / "__init__.py").write_text("", encoding="utf-8")
        (package / "auth.py").write_text("def verify():\n    pass\n", encoding="utf-8")
        (tmp_path / "service.py").write_text(
            "import pkg.auth\n\npkg.auth.verify()\n",
            encoding="utf-8",
        )

        callers = find_callers("verify", tmp_path)

        assert len(callers) == 1
        assert callers[0]["qualified_name"] == "pkg.auth.verify"

    def test_compound_bindings_are_branch_local_and_merge_as_unknown(self, tmp_path):
        (tmp_path / "auth.py").write_text("def verify():\n    pass\n", encoding="utf-8")
        cases = {
            "if_case.py": "if flag:\n    from auth import verify\n    verify()\nverify()\n",
            "try_case.py": "try:\n    from auth import verify\n    verify()\nexcept ImportError:\n    pass\nverify()\n",
            "for_case.py": "for item in items:\n    from auth import verify\n    verify()\nverify()\n",
            "with_case.py": "with context():\n    from auth import verify\n    verify()\nverify()\n",
        }
        for name, content in cases.items():
            (tmp_path / name).write_text(content, encoding="utf-8")

        callers = find_callers("verify", tmp_path)

        assert sorted(Path(call["file"]).name for call in callers) == sorted(cases)
        assert all(call["line"] == 3 for call in callers)

    def test_calls_in_decorators_defaults_and_class_bases_are_visited(self, tmp_path):
        (tmp_path / "deps.py").write_text(
            "def decorate(): pass\ndef factory(): pass\ndef base(): pass\n",
            encoding="utf-8",
        )
        source = tmp_path / "service.py"
        source.write_text(
            "from deps import decorate, factory, base\n\n"
            "@decorate()\n"
            "def run(value=factory()):\n"
            "    pass\n\n"
            "class Child(base()):\n"
            "    pass\n",
            encoding="utf-8",
        )

        calls = parse_file(source)["calls"]

        assert {(call["name"], call["confidence"]) for call in calls} == {
            ("decorate", "confirmed"),
            ("factory", "confirmed"),
            ("base", "confirmed"),
        }

    def test_self_is_confirmed_only_until_shadowed(self, tmp_path):
        source = tmp_path / "service.py"
        source.write_text(
            "class Service:\n"
            "    def verify(self): pass\n\n"
            "    def normal(self):\n"
            "        self.verify()\n\n"
            "    def reassigned(self, other):\n"
            "        self = other\n"
            "        self.verify()\n\n"
            "    def outer(self):\n"
            "        def inner():\n"
            "            nonlocal self\n"
            "            self.verify()\n",
            encoding="utf-8",
        )

        calls = {call["line"]: call for call in parse_file(source)["calls"]}

        assert (
            calls[5]["confidence"],
            calls[9]["confidence"],
            calls[14]["confidence"],
            calls[9]["semantic_eligible"],
            calls[14]["semantic_eligible"],
        ) == ("confirmed", "unknown", "unknown", False, False)

    def test_function_late_assignment_shadows_outer_import_at_compile_time(self, tmp_path):
        (tmp_path / "auth.py").write_text("def verify(): pass\n", encoding="utf-8")
        (tmp_path / "service.py").write_text(
            "from auth import verify\n\n"
            "def run():\n"
            "    verify()\n"
            "    verify = lambda: None\n",
            encoding="utf-8",
        )

        assert find_callers("verify", tmp_path) == []

        call = parse_file(tmp_path / "service.py")["calls"][0]
        assert call["semantic_eligible"] is False
        assert call["unresolved_reason"] == "shadowed_binding"

    def test_package_reexport_resolves_imported_symbol(self, tmp_path):
        package = tmp_path / "pkg"
        package.mkdir()
        (package / "auth.py").write_text("def verify(): pass\n", encoding="utf-8")
        (package / "__init__.py").write_text(
            "from .auth import verify\n",
            encoding="utf-8",
        )
        (tmp_path / "service.py").write_text(
            "from pkg import verify\n\nverify()\n",
            encoding="utf-8",
        )

        callers = find_callers("verify", tmp_path)

        assert len(callers) == 1
        assert callers[0]["qualified_name"] == "pkg.verify"

    def test_nonlocal_import_uses_enclosing_binding(self, tmp_path):
        (tmp_path / "auth.py").write_text("def verify(): pass\n", encoding="utf-8")
        source = tmp_path / "service.py"
        source.write_text(
            "def outer():\n"
            "    from auth import verify\n"
            "    def inner():\n"
            "        nonlocal verify\n"
            "        verify()\n",
            encoding="utf-8",
        )

        call = next(call for call in parse_file(source)["calls"] if call["line"] == 5)

        assert call["confidence"] == "confirmed"
        assert call["qualified_name"] == "auth.verify"

    def test_javascript_method_declaration_is_not_a_call(self, tmp_path):
        (tmp_path / "service.js").write_text(
            "class Service {\n"
            "  verify() {}\n"
            "  run() {\n"
            "    verify();\n"
            "  }\n"
            "}\n",
            encoding="utf-8",
        )

        callers = find_callers("verify", tmp_path)

        assert [call["line"] for call in callers] == [4]

    def test_local_class_method_does_not_inherit_class_scope(self, tmp_path):
        source = tmp_path / "service.py"
        source.write_text(
            "def outer():\n"
            "    class Local:\n"
            "        def helper(self):\n"
            "            pass\n\n"
            "        def run(self):\n"
            "            helper()\n",
            encoding="utf-8",
        )

        call = next(call for call in parse_file(source)["calls"] if call["line"] == 7)

        assert call["confidence"] == "unknown"
        assert call["qualified_name"] is None

    def test_while_binding_is_branch_local_and_unknown_after_loop(self, tmp_path):
        (tmp_path / "auth.py").write_text("def verify(): pass\n", encoding="utf-8")
        (tmp_path / "service.py").write_text(
            "while condition:\n"
            "    from auth import verify\n"
            "    verify()\n"
            "verify()\n",
            encoding="utf-8",
        )

        _, calls = resolve_python_imports_and_calls(
            tmp_path / "service.py",
            build_python_symbol_registry(tmp_path),
            tmp_path,
        )

        assert [(call["line"], call["confidence"]) for call in calls] == [
            (3, "confirmed"),
            (4, "unknown"),
        ]

    def test_match_binding_is_case_local_and_unknown_after_match(self, tmp_path):
        (tmp_path / "auth.py").write_text("def verify(): pass\n", encoding="utf-8")
        (tmp_path / "service.py").write_text(
            "match value:\n"
            "    case 1:\n"
            "        from auth import verify\n"
            "        verify()\n"
            "verify()\n",
            encoding="utf-8",
        )

        _, calls = resolve_python_imports_and_calls(
            tmp_path / "service.py",
            build_python_symbol_registry(tmp_path),
            tmp_path,
        )

        assert [(call["line"], call["confidence"]) for call in calls] == [
            (4, "confirmed"),
            (5, "unknown"),
        ]


class TestFindDeadCode:
    @pytest.mark.parametrize(
        ("filename", "source", "expected_signatures"),
        [
            (
                "Service.java",
                "class Service { void run(int value) {} void run(String value) {} }\n",
                {"run(int value)", "run(String value)"},
            ),
            (
                "service.cpp",
                "void run(int value) {}\nvoid run(const char* value) {}\n",
                {"run(int value)", "run(const char* value)"},
            ),
        ],
    )
    def test_overloads_have_distinct_signature_ids(
        self, tmp_path, filename, source, expected_signatures
    ):
        (tmp_path / filename).write_text(source, encoding="utf-8")

        candidates = [item for item in find_dead_code(tmp_path) if item["name"] == "run"]

        # Two distinct signatures prove two distinct symbol ids: they are cut
        # from the identifier itself.
        signatures = {item["symbol_id"].rsplit("::", 1)[1] for item in candidates}

        assert (len(candidates), signatures) == (2, expected_signatures)

    def test_reports_only_zero_confirmed_incoming_calls_as_honest_candidates(
        self, tmp_path
    ):
        (tmp_path / "service.py").write_text(
            "def used(): pass\n"
            "def unused(): pass\n"
            "def dynamic_target(): pass\n"
            "def run(client):\n"
            "    used()\n"
            "    client.dynamic_target()\n",
            encoding="utf-8",
        )

        candidates = find_dead_code(tmp_path)

        assert [item["name"] for item in candidates] == ["dynamic_target", "run", "unused"]
        assert all(item["status"] == "candidate" for item in candidates)
        assert all(item["reason"] == "zero_confirmed_incoming_calls" for item in candidates)
        assert all(item["graph_complete"] is False for item in candidates)

    def test_excludes_entry_points_tests_exports_and_framework_routes(self, tmp_path):
        (tmp_path / "api.py").write_text(
            "__all__ = ['public_api']\n"
            "def main(): pass\n"
            "def __init__(): pass\n"
            "def test_helper(): pass\n"
            "def public_api(): pass\n"
            "@app.route('/health')\n"
            "def health(): pass\n"
            "def actual_candidate(): pass\n",
            encoding="utf-8",
        )
        (tmp_path / "web.js").write_text(
            "export function exposed() {}\nfunction js_candidate() {}\n",
            encoding="utf-8",
        )
        (tmp_path / "Controller.java").write_text(
            "class Controller {\n"
            "  @GetMapping(\"/health\")\n"
            "  public void health() {}\n"
            "  public void javaCandidate() {}\n"
            "}\n",
            encoding="utf-8",
        )

        names = {item["name"] for item in find_dead_code(tmp_path)}

        assert names == {"actual_candidate", "js_candidate", "javaCandidate"}

    @pytest.mark.parametrize(
        ("filename", "source", "target"),
        [
            ("sample.py", "def target(): pass\ndef caller(): target()\n", "target"),
            ("sample.js", "function target() {}\nfunction caller() { target(); }\n", "target"),
            ("sample.ts", "function target(): void {}\nfunction caller(): void { target(); }\n", "target"),
            ("sample.go", "package p\nfunc target() {}\nfunc caller() { target() }\n", "target"),
            ("sample.rs", "fn target() {}\nfn caller() { target(); }\n", "target"),
            ("Sample.java", "class Sample { static void target() {} static void caller() { target(); } }\n", "target"),
            ("sample.c", "void target() {}\nvoid caller() { target(); }\n", "target"),
            ("sample.cpp", "void target() {}\nvoid caller() { target(); }\n", "target"),
            ("sample.rb", "def target; end\ndef caller\n target()\nend\n", "target"),
            ("sample.php", "<?php function target() {} function caller() { target(); }\n", "target"),
            ("Sample.cs", "class Sample { static void target() {} static void caller() { target(); } }\n", "target"),
            ("sample.sh", "target() { :; }\ncaller() { target; }\n", "target"),
        ],
    )
    def test_incoming_calls_are_counted_for_all_supported_languages(
        self, tmp_path, filename, source, target
    ):
        (tmp_path / filename).write_text(source, encoding="utf-8")

        candidates = find_dead_code(tmp_path)

        assert target not in {item["name"] for item in candidates}
        assert all(item["symbol_id"].count("::") == 2 for item in candidates)

    def test_same_name_definitions_do_not_share_incoming_calls(self, tmp_path):
        (tmp_path / "first.py").write_text("def shared(): pass\n", encoding="utf-8")
        (tmp_path / "second.py").write_text("def shared(): pass\n", encoding="utf-8")
        (tmp_path / "caller.py").write_text(
            "from first import shared\ndef run(): shared()\n", encoding="utf-8"
        )

        shared = [item for item in find_dead_code(tmp_path) if item["name"] == "shared"]

        assert len(shared) == 1
        assert shared[0]["symbol_id"] == "second.py::<module>::shared()"


def _architecture_summary(architecture: dict) -> tuple:
    """Entry points, routes and community sizes as one comparable value."""
    return (
        {(item["kind"], item["name"]) for item in architecture["entry_points"]},
        {(item["method"], item["path"]) for item in architecture["routes"]},
        [item["size"] for item in architecture["communities"]],
    )


class TestGetArchitecture:
    def test_summarizes_entry_points_routes_hotspots_and_communities(
        self, tmp_path, monkeypatch
    ):
        (tmp_path / "service.py").write_text(
            "def shared(): pass\n"
            "def first(): shared()\n"
            "def second(): shared()\n"
            "@app.route('/health')\n"
            "def health(): pass\n"
            "def main(): first()\n",
            encoding="utf-8",
        )
        (tmp_path / "server.js").write_text(
            "app.get('/users', listUsers);\napp.listen(3000);\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(
            "code_graph._communities_from_edges", lambda edges: [["first", "shared"]]
        )

        architecture = get_architecture(tmp_path)

        # NEW-125: the community field carries named rows and states its bound.
        assert _architecture_summary(architecture) == (
            {("main", "main"), ("listen", "app.listen")},
            {("ROUTE", "/health"), ("GET", "/users")},
            [2],
        )
        assert (
            architecture["hotspots"][0]["name"],
            architecture["hotspots"][0]["incoming_callers"],
            architecture["community_count"],
            architecture["communities_truncated"],
            architecture["graph_complete"],
        ) == ("shared", 2, 1, False, False)

    def test_hotspots_keep_same_name_symbols_separate(self, tmp_path):
        (tmp_path / "first.py").write_text("def shared(): pass\n", encoding="utf-8")
        (tmp_path / "second.py").write_text("def shared(): pass\n", encoding="utf-8")
        (tmp_path / "caller.py").write_text(
            "from first import shared\ndef run(): shared()\n", encoding="utf-8"
        )

        hotspots = get_architecture(tmp_path)["hotspots"]

        assert [(item["symbol_id"], item["incoming_callers"]) for item in hotspots] == [
            ("first.py::<module>::shared()", 1)
        ]


class TestLouvainCommunities:
    def test_weighted_undirected_graph_finds_two_dense_modules_deterministically(self):
        edges = {
            "a": {"b": 4.0, "c": 4.0},
            "b": {"a": 4.0, "c": 4.0},
            "c": {"a": 4.0, "b": 4.0, "x": 0.1},
            "x": {"c": 0.1, "y": 4.0, "z": 4.0},
            "y": {"x": 4.0, "z": 4.0},
            "z": {"x": 4.0, "y": 4.0},
        }
        reversed_edges = {
            node: dict(reversed(list(neighbors.items())))
            for node, neighbors in reversed(list(edges.items()))
        }

        expected = [["a", "b", "c"], ["x", "y", "z"]]
        assert _louvain_communities(edges) == expected
        assert _louvain_communities(reversed_edges) == expected

    def test_detect_communities_uses_real_caller_to_callee_edges(self, tmp_path):
        (tmp_path / "modules.py").write_text(
            "def alpha():\n    beta()\n    beta()\n"
            "def beta():\n    alpha()\n"
            "def gamma():\n    delta()\n    delta()\n"
            "def delta():\n    gamma()\n",
            encoding="utf-8",
        )

        # NEW-125: a member is a named row - qualified name, file and line.
        assert [
            [member["qualified_name"] for member in group["members"]]
            for group in detect_communities(tmp_path)
        ] == [["alpha", "beta"], ["delta", "gamma"]]

    def test_aggregation_counts_existing_self_loops_once_and_internal_edges_twice(self):
        import code_graph

        graph = {
            "a": {"a": 6.0, "b": 2.0, "c": 1.0},
            "b": {"a": 2.0, "b": 4.0, "c": 3.0},
            "c": {"a": 1.0, "b": 3.0},
        }

        assert code_graph._aggregate_louvain_graph(graph, {"a": 0, "b": 0, "c": 1}) == {
            0: {0: 14.0, 1: 4.0},
            1: {0: 4.0},
        }

    def test_communities_return_canonical_ids_for_duplicate_names(self, tmp_path):
        (tmp_path / "first.py").write_text(
            "def shared(): helper()\ndef helper(): shared()\n", encoding="utf-8"
        )
        (tmp_path / "second.py").write_text(
            "def shared(): helper()\ndef helper(): shared()\n", encoding="utf-8"
        )

        communities = detect_communities(tmp_path)

        # NEW-125: the name alone would collide; the file keeps them distinct.
        assert [
            [
                (member["qualified_name"], Path(member["file"]).name)
                for member in group["members"]
            ]
            for group in communities
        ] == [
            [("helper", "first.py"), ("shared", "first.py")],
            [("helper", "second.py"), ("shared", "second.py")],
        ]


class TestQueryCaptureNormalization:
    @pytest.mark.parametrize(
        ("filename", "source", "expected"),
        [
            ("app.js", 'import value from "./dep.js";\n', "./dep.js"),
            ("app.ts", "import value from './dep.js';\n", "./dep.js"),
            ("app.c", '#include "dep.h"\n', "dep.h"),
        ],
    )
    def test_import_literals_are_unquoted(self, tmp_path, filename, source, expected):
        path = tmp_path / filename
        path.write_text(source, encoding="utf-8")

        assert parse_file(path)["imports"][0]["name"] == expected


class TestIndexDirectory:
    """Test directory indexing."""

    def test_index_empty_dir(self, tmp_path):
        stats = index_directory(tmp_path, verbose=False)
        assert stats["files"] == 0

    def test_index_counts_correctly(self, tmp_path):
        (tmp_path / "a.py").write_text("def f(): pass\n", encoding="utf-8")
        (tmp_path / "b.js").write_text("function g() {}\n", encoding="utf-8")
        (tmp_path / "c.md").write_text("# Not code\n", encoding="utf-8")

        stats = index_directory(tmp_path, verbose=False)
        assert stats["files"] == 2  # Only .py and .js
        assert stats["functions"] >= 2

    def test_index_skips_git_and_venv(self, tmp_path):
        (tmp_path / ".git").mkdir()
        (tmp_path / ".git" / "hook.py").write_text("def git_func(): pass\n", encoding="utf-8")
        (tmp_path / "real.py").write_text("def real_func(): pass\n", encoding="utf-8")

        stats = index_directory(tmp_path, verbose=False)
        assert stats["files"] == 1  # Only real.py, not .git/hook.py

    def test_index_returns_all_stats(self, tmp_path):
        (tmp_path / "test.py").write_text("def f(): pass\nclass C: pass\n", encoding="utf-8")
        stats = index_directory(tmp_path, verbose=False)

        assert {"files", "functions", "classes", "calls", "imports"} <= set(stats)

    def test_index_detects_tools_fresh_on_every_call(self, tmp_path, monkeypatch):
        calls = []
        monkeypatch.setattr(
            "code_graph.detect_code_tools",
            lambda directory, cache_path=None: calls.append(directory) or {"tools": {}},
        )

        index_directory(tmp_path, verbose=False)
        index_directory(tmp_path, verbose=False)

        assert calls == [tmp_path, tmp_path]

    def test_dynamic_receiver_is_semantically_eligible(self, tmp_path):
        source = tmp_path / "service.py"
        source.write_text("def run(client):\n    client.verify()\n", encoding="utf-8")

        call = parse_file(source)["calls"][0]

        assert call["confidence"] == "unknown"
        assert call["semantic_eligible"] is True
        assert call["unresolved_reason"] == "dynamic_receiver"


class TestCodeToolDetection:
    def test_writes_manifest_with_workspace_typescript_preferred(self, tmp_path, monkeypatch):
        workspace_tsc = tmp_path / "node_modules" / ".bin" / "tsc.cmd"
        workspace_tsc.parent.mkdir(parents=True)
        workspace_tsc.write_text("", encoding="utf-8")
        cache_path = tmp_path / "state" / "cache" / "code_tools.json"
        monkeypatch.setattr("code_graph.metadata.version", lambda name: "0.19.2")
        monkeypatch.setattr(
            "code_graph.shutil.which",
            lambda name: {"rust-analyzer": "/bin/rust-analyzer", "gopls": None}.get(name),
        )
        monkeypatch.setattr(
            "code_graph._probe_version",
            lambda args, timeout=2: ("1.2.3", None),
        )

        manifest = detect_code_tools(tmp_path, cache_path=cache_path)

        tools = manifest["tools"]

        assert (
            manifest["schema_version"],
            bool(manifest["generated_at"]),
            tools["python"]["provider"],
            tools["python"]["capabilities"]["semantic"],
            tools["typescript"]["path"],
            tools["typescript"]["capabilities"]["semantic"],
            tools["rust"]["available"],
            tools["go"]["available"],
        ) == (1, True, "jedi", True, str(workspace_tsc.resolve()), False, True, False)
        assert json.loads(cache_path.read_text(encoding="utf-8")) == manifest

    def test_windows_prefers_workspace_tsc_cmd(self, tmp_path, monkeypatch):
        bin_dir = tmp_path / "node_modules" / ".bin"
        bin_dir.mkdir(parents=True)
        (bin_dir / "tsc").write_text("shim", encoding="utf-8")
        cmd = bin_dir / "tsc.cmd"
        cmd.write_text("cmd", encoding="utf-8")
        monkeypatch.setattr(sys, "platform", "win32")
        monkeypatch.setattr("code_graph.metadata.version", lambda name: "0.20")
        monkeypatch.setattr("code_graph.importlib.import_module", lambda name: object())
        monkeypatch.setattr("code_graph.shutil.which", lambda name: None)
        monkeypatch.setattr(
            "code_graph._probe_version", lambda args, timeout=2: ("Version 1", None)
        )

        manifest = detect_code_tools(tmp_path, cache_path=tmp_path / "tools.json")

        assert manifest["tools"]["typescript"]["path"] == str(cmd.resolve())

    def test_jedi_metadata_without_import_is_unavailable(self, tmp_path, monkeypatch):
        monkeypatch.setattr("code_graph.metadata.version", lambda name: "0.20")
        monkeypatch.setattr(
            "code_graph.importlib.import_module",
            lambda name: (_ for _ in ()).throw(ImportError("broken jedi")),
        )
        monkeypatch.setattr("code_graph.shutil.which", lambda name: None)

        manifest = detect_code_tools(tmp_path, cache_path=tmp_path / "tools.json")

        assert manifest["tools"]["python"]["available"] is False
        assert "broken jedi" in manifest["tools"]["python"]["failure"]

    def test_external_version_probes_run_concurrently_with_short_timeouts(
        self, tmp_path, monkeypatch
    ):
        seen = []
        monkeypatch.setattr("code_graph.metadata.version", lambda name: "0.20")
        monkeypatch.setattr("code_graph.importlib.import_module", lambda name: object())
        monkeypatch.setattr("code_graph.shutil.which", lambda name: f"/bin/{name}")

        def slow_probe(args, timeout=5):
            seen.append(timeout)
            time.sleep(0.15)
            return "1.0", None

        monkeypatch.setattr("code_graph._probe_version", slow_probe)
        started = time.monotonic()
        detect_code_tools(tmp_path, cache_path=tmp_path / "tools.json")
        elapsed = time.monotonic() - started

        assert elapsed < 0.35
        assert seen == [2, 2, 2]

    def test_concurrent_manifest_writers_leave_valid_file_and_no_temps(
        self, tmp_path, monkeypatch
    ):
        cache_path = tmp_path / "cache" / "code_tools.json"
        monkeypatch.setattr("code_graph.metadata.version", lambda name: "0.20")
        monkeypatch.setattr("code_graph.importlib.import_module", lambda name: object())
        monkeypatch.setattr("code_graph.shutil.which", lambda name: None)
        real_replace = __import__("os").replace
        guard = threading.Lock()
        active = 0
        max_active = 0

        def observed_replace(source, destination):
            nonlocal active, max_active
            with guard:
                active += 1
                max_active = max(max_active, active)
            try:
                time.sleep(0.001)
                real_replace(source, destination)
            finally:
                with guard:
                    active -= 1

        monkeypatch.setattr("code_graph.os.replace", observed_replace)

        with ThreadPoolExecutor(max_workers=32) as pool:
            manifests = list(pool.map(lambda _: detect_code_tools(tmp_path, cache_path), range(256)))

        written = json.loads(cache_path.read_text(encoding="utf-8"))
        assert written in manifests
        assert max_active == 1
        assert list(cache_path.parent.glob("code_tools.json.*.tmp")) == []

    def test_manifest_contention_is_best_effort_and_preserves_last_valid_file(
        self, tmp_path, monkeypatch
    ):
        cache_path = tmp_path / "cache" / "code_tools.json"
        cache_path.parent.mkdir()
        previous = {"schema_version": 1, "generated_at": "old", "tools": {}}
        cache_path.write_text(json.dumps(previous), encoding="utf-8")
        monkeypatch.setattr("code_graph.metadata.version", lambda name: "0.20")
        monkeypatch.setattr("code_graph.importlib.import_module", lambda name: object())
        monkeypatch.setattr("code_graph.shutil.which", lambda name: None)
        monkeypatch.setattr(
            "code_graph.os.replace",
            lambda source, destination: (_ for _ in ()).throw(PermissionError("busy")),
        )

        detected = detect_code_tools(tmp_path, cache_path)

        assert detected["schema_version"] == 1
        assert json.loads(cache_path.read_text(encoding="utf-8")) == previous
        assert list(cache_path.parent.glob("code_tools.json.*.tmp")) == []

    def test_missing_and_failing_tools_do_not_crash_and_replace_corrupt_manifest(
        self, tmp_path, monkeypatch
    ):
        cache_path = tmp_path / "cache" / "code_tools.json"
        cache_path.parent.mkdir()
        cache_path.write_text("{broken", encoding="utf-8")

        def missing_jedi(name):
            raise ModuleNotFoundError(name)

        monkeypatch.setattr("code_graph.metadata.version", missing_jedi)
        monkeypatch.setattr("code_graph.shutil.which", lambda name: f"/bin/{name}")
        monkeypatch.setattr(
            "code_graph._probe_version", lambda args, timeout=2: (None, "probe failed")
        )

        manifest = detect_code_tools(tmp_path, cache_path=cache_path)

        assert all(not tool["available"] for tool in manifest["tools"].values())
        assert manifest["tools"]["python"]["failure"]
        assert manifest["tools"]["typescript"]["failure"] == "probe failed"
        assert json.loads(cache_path.read_text(encoding="utf-8")) == manifest


class TestPythonSemanticEnrichment:
    def test_jedi_resolves_single_workspace_target_without_downgrading_confirmed(
        self, tmp_path, monkeypatch
    ):
        source = tmp_path / "service.py"
        target = tmp_path / "models.py"
        source.write_text("client.save()\n", encoding="utf-8")
        target.write_text("def save(): pass\n", encoding="utf-8")
        calls = [
            {
                "name": "save", "line": 1, "column": 7, "confidence": "unknown",
                "qualified_name": None, "semantic_eligible": True,
            },
            {"name": "fixed", "line": 2, "column": 0, "confidence": "confirmed", "qualified_name": "local.fixed"},
        ]

        class Definition:
            module_path = target
            full_name = "models.save"

        class Script:
            def __init__(self, **kwargs):
                pass

            def infer(self, line, column):
                return [Definition()]

        class Project:
            def __init__(self, path):
                pass

        monkeypatch.setitem(sys.modules, "jedi", SimpleNamespace(Project=Project, Script=Script))

        enriched = enrich_python_semantics(source, calls, tmp_path)

        assert enriched[0]["qualified_name"] == "models.save"
        assert enriched[0]["confidence"] == "confirmed"
        assert enriched[0]["evidence"] == "jedi"
        assert enriched[1] == calls[1]

    def test_jedi_ambiguous_or_outside_workspace_stays_unknown(self, tmp_path, monkeypatch):
        source = tmp_path / "service.py"
        source.write_text("target()\n", encoding="utf-8")
        outside = tmp_path.parent / "outside.py"

        class Definition:
            def __init__(self, path, name):
                self.module_path = path
                self.full_name = name

        answers = [
            [Definition(tmp_path / "a.py", "a.target"), Definition(tmp_path / "b.py", "b.target")],
            [Definition(outside, "outside.target")],
        ]

        class Script:
            def __init__(self, **kwargs):
                pass

            def infer(self, line, column):
                return answers.pop(0)

        class Project:
            def __init__(self, path):
                pass

        monkeypatch.setitem(sys.modules, "jedi", SimpleNamespace(Project=Project, Script=Script))
        calls = [
            {"name": "target", "line": 1, "column": 0, "confidence": "unknown", "qualified_name": None},
            {"name": "target", "line": 1, "column": 0, "confidence": "unknown", "qualified_name": None},
        ]

        assert enrich_python_semantics(source, calls, tmp_path) == calls

    def test_same_full_name_from_different_modules_is_ambiguous(self, tmp_path, monkeypatch):
        source = tmp_path / "service.py"
        source.write_text("target()\n", encoding="utf-8")

        class Definition:
            full_name = "package.target"

            def __init__(self, path):
                self.module_path = path

        class Script:
            def __init__(self, **kwargs):
                pass

            def infer(self, line, column):
                return [Definition(tmp_path / "a.py"), Definition(tmp_path / "b.py")]

        class Project:
            def __init__(self, path):
                pass

        monkeypatch.setitem(sys.modules, "jedi", SimpleNamespace(Project=Project, Script=Script))
        call = {
            "name": "target", "line": 1, "column": 0, "confidence": "unknown",
            "qualified_name": None, "semantic_eligible": True,
        }

        assert enrich_python_semantics(source, [call], tmp_path) == [call]

    def test_missing_jedi_falls_back_without_crashing(self, tmp_path, monkeypatch):
        source = tmp_path / "service.py"
        source.write_text("target()\n", encoding="utf-8")
        calls = [{"name": "target", "line": 1, "column": 0, "confidence": "unknown"}]
        monkeypatch.delitem(sys.modules, "jedi", raising=False)
        monkeypatch.setattr("code_graph.importlib.import_module", lambda name: (_ for _ in ()).throw(ImportError()))

        assert enrich_python_semantics(source, calls, tmp_path) == calls


class TestCoChanges:
    @staticmethod
    def _git_log(*commits):
        fields = []
        for commit, changes in commits:
            fields.extend(["COMMIT", commit])
            for change in changes:
                fields.extend(change)
        return ("\0".join(fields) + "\0").encode()

    def test_analyzes_file_level_co_changes_and_tracks_rename_identity(
        self, tmp_path, monkeypatch
    ):
        output = self._git_log(
            ("oldest", [("M", "api.py"), ("M", "legacy.py")]),
            ("middle", [("M", "api.py"), ("M", "legacy.py")]),
            ("rename", [("R100", "legacy.py", "current.py")]),
            ("newest", [("M", "api.py"), ("M", "current.py")]),
            ("other-1", [("M", "unrelated-1.py")]),
            ("other-2", [("M", "unrelated-2.py")]),
        )
        seen = {}

        def fake_run(args, **kwargs):
            seen["args"] = args
            seen["kwargs"] = kwargs
            return subprocess.CompletedProcess(args, 0, stdout=output, stderr=b"")

        monkeypatch.setattr("code_graph.subprocess.run", fake_run)

        edges = analyze_co_changes(tmp_path)

        edge = edges[0]

        assert (
            len(edges),
            {edge["source"], edge["target"]},
            edge["type"],
            edge["shared_commits"],
            edge["weight"],
            {"npmi", "lift", "support"} <= edge.keys(),
            seen["kwargs"]["timeout"],
        ) == (1, {"api.py", "current.py"}, "CO_CHANGED_WITH", 3, 0.866025, True, 10)
        assert seen["args"] == [
            "git", "log", "--reverse", "--max-count=2000", "--no-merges",
            "--name-status", "-z",
            "--find-renames=50%", "--find-copies=50%", "--format=COMMIT%x00%H",
            "--", ".",
        ]

    def test_subdirectory_uses_repo_root_paths_and_limited_pathspec(
        self, tmp_path, monkeypatch
    ):
        subdirectory = tmp_path / "pkg"
        subdirectory.mkdir()
        output = self._git_log(
            ("1", [("M", "pkg/a.py"), ("M", "pkg/b.py")]),
            ("2", [("M", "pkg/a.py"), ("M", "pkg/b.py")]),
            ("3", [("M", "pkg/a.py"), ("M", "pkg/b.py")]),
            ("4", [("M", "other.py")]),
        )
        calls = []

        def fake_run(args, **kwargs):
            calls.append((args, kwargs))
            if args[:3] == ["git", "rev-parse", "--show-toplevel"]:
                return subprocess.CompletedProcess(
                    args, 0, stdout=str(tmp_path).encode() + b"\n"
                )
            return subprocess.CompletedProcess(args, 0, stdout=output)

        monkeypatch.setattr("code_graph.subprocess.run", fake_run)

        edge = analyze_co_changes(subdirectory)[0]

        assert {edge["source"], edge["target"]} == {"pkg/a.py", "pkg/b.py"}
        log_args, log_kwargs = calls[1]
        assert log_args[-2:] == ["--", "pkg"]
        assert log_kwargs["cwd"] == str(tmp_path)

    def test_real_nul_records_strip_status_separator_but_preserve_paths(
        self, tmp_path, monkeypatch
    ):
        output = (
            b"COMMIT\x001\x00\nM\x00 leading.py\x00M\x00peer.py\x00"
            b"COMMIT\x002\x00\nM\x00 leading.py\x00M\x00peer.py\x00"
            b"COMMIT\x003\x00\nM\x00 leading.py\x00M\x00peer.py\x00"
            b"COMMIT\x004\x00\nM\x00other.py\x00"
        )
        monkeypatch.setattr(
            "code_graph.subprocess.run",
            lambda args, **kwargs: subprocess.CompletedProcess(args, 0, stdout=output),
        )

        edge = analyze_co_changes(tmp_path)[0]

        assert {edge["source"], edge["target"]} == {" leading.py", "peer.py"}

    def test_copy_keeps_source_and_target_as_distinct_identities(
        self, tmp_path, monkeypatch
    ):
        output = self._git_log(
            ("1", [("M", "source.py"), ("M", "source-peer.py")]),
            ("2", [("M", "source.py"), ("M", "source-peer.py")]),
            ("3", [("M", "source.py"), ("M", "source-peer.py")]),
            ("copy", [("C100", "source.py", "copy.py")]),
            ("4", [("M", "copy.py"), ("M", "copy-peer.py")]),
            ("5", [("M", "copy.py"), ("M", "copy-peer.py")]),
            ("6", [("M", "copy.py"), ("M", "copy-peer.py")]),
            ("other", [("M", "other.py")]),
        )
        monkeypatch.setattr(
            "code_graph.subprocess.run",
            lambda args, **kwargs: subprocess.CompletedProcess(args, 0, stdout=output),
        )

        pairs = [
            {edge["source"], edge["target"]} for edge in analyze_co_changes(tmp_path)
        ]

        assert {"source.py", "source-peer.py"} in pairs
        assert {"copy.py", "copy-peer.py"} in pairs

    def test_reused_rename_source_gets_a_new_identity(self, tmp_path, monkeypatch):
        output = self._git_log(
            ("1", [("M", "old.py"), ("M", "first-peer.py")]),
            ("2", [("M", "old.py"), ("M", "first-peer.py")]),
            ("3", [("M", "old.py"), ("M", "first-peer.py")]),
            ("rename", [("R100", "old.py", "current.py")]),
            ("4", [("A", "old.py"), ("M", "second-peer.py")]),
            ("5", [("M", "old.py"), ("M", "second-peer.py")]),
            ("6", [("M", "old.py"), ("M", "second-peer.py")]),
            ("other", [("M", "other.py")]),
        )
        monkeypatch.setattr(
            "code_graph.subprocess.run",
            lambda args, **kwargs: subprocess.CompletedProcess(args, 0, stdout=output),
        )

        pairs = [
            {edge["source"], edge["target"]} for edge in analyze_co_changes(tmp_path)
        ]

        assert {"current.py", "first-peer.py"} in pairs
        assert {"old.py", "second-peer.py"} in pairs

    def test_rare_coincidence_is_not_reported(self, tmp_path, monkeypatch):
        commits = []
        for index in range(3):
            commits.append((f"shared-{index}", [("M", "a.py"), ("M", "b.py")]))
        for index in range(7):
            commits.append((f"a-{index}", [("M", "a.py")]))
            commits.append((f"b-{index}", [("M", "b.py")]))
        monkeypatch.setattr(
            "code_graph.subprocess.run",
            lambda args, **kwargs: subprocess.CompletedProcess(
                args, 0, stdout=self._git_log(*commits), stderr=b""
            ),
        )

        assert analyze_co_changes(tmp_path) == []

    def test_ignores_giant_commits_and_corrects_popular_files(
        self, tmp_path, monkeypatch
    ):
        giant = [("M", f"bulk-{index}.py") for index in range(51)]
        commits = [("giant", giant)]
        for index in range(3):
            commits.append(
                (f"shared-{index}", [("M", "hub.py"), ("M", "feature.py")])
            )
        for index in range(7):
            commits.append((f"hub-{index}", [("M", "hub.py")]))
        monkeypatch.setattr(
            "code_graph.subprocess.run",
            lambda args, **kwargs: subprocess.CompletedProcess(
                args, 0, stdout=self._git_log(*commits), stderr=b""
            ),
        )

        assert analyze_co_changes(tmp_path) == []

    def test_refinement_only_adds_evidence_to_confirmed_call_edges(self):
        calls = [
            {"source": "a.py", "target": "b.py", "type": "CALLS", "confidence": "confirmed"},
            {"source": "a.py", "target": "c.py", "type": "CALLS", "confidence": "unknown"},
            {"source": "a.py", "target": "b.py", "type": "IMPORTS", "confidence": "confirmed"},
        ]
        co_changes = [{
            "source": "a.py", "target": "b.py", "type": "CO_CHANGED_WITH", "weight": 0.8
        }]

        refined = refine_call_edges_with_co_changes(calls, co_changes)

        assert [edge["type"] for edge in refined] == ["CALLS", "CALLS", "IMPORTS"]
        assert (
            refined[0]["evidence"]["co_change_weight"],
            "evidence" in refined[1],
            "evidence" in refined[2],
            {edge["type"] for edge in co_changes},
        ) == (0.8, False, False, {"CO_CHANGED_WITH"})

    def test_refinement_normalizes_absolute_and_windows_paths(self, tmp_path):
        calls = [{
            "source": str(tmp_path / "pkg" / "a.py"),
            "target": "pkg\\b.py",
            "type": "CALLS",
            "confidence": "confirmed",
        }]
        co_changes = [{
            "source": "pkg/a.py",
            "target": "pkg/b.py",
            "type": "CO_CHANGED_WITH",
            "weight": 0.9,
        }]

        refined = refine_call_edges_with_co_changes(calls, co_changes, tmp_path)

        assert refined[0]["evidence"]["co_change_weight"] == 0.9

    def test_returns_empty_outside_git_or_on_timeout(self, tmp_path, monkeypatch):
        def fail(*args, **kwargs):
            raise subprocess.TimeoutExpired(args[0], kwargs["timeout"])

        monkeypatch.setattr("code_graph.subprocess.run", fail)

        assert analyze_co_changes(tmp_path) == []
