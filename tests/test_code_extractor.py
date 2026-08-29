"""Pure code-to-Evidence-Graph extraction contracts."""

from __future__ import annotations

import hashlib
import sys
import time
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from corpus_snapshot import CapturedSource, SourceMetadata, SourceRecord  # noqa: E402


def _source(path: str, content: bytes, *, language: str = "python") -> CapturedSource:
    return CapturedSource(
        SourceRecord(
            logical_id=f"source:{path}",
            relative_path=path,
            sha256=hashlib.sha256(content).hexdigest(),
            size=len(content),
            media_type="text/x-python",
            language=language,
            git_oid=None,
        ),
        SourceMetadata(type="code", language=language),
        content,
    )


def _node(result, kind: str, name: str):
    return next(
        node
        for node in result.nodes
        if node["kind"] == kind and node["metadata"].get("name") == name
    )


def test_expired_deadline_aborts_before_code_source_iteration():
    from code_extractor import extract_code

    touched = False

    def sources():
        nonlocal touched
        touched = True
        yield _source("app.py", b"def app():\n    return 1\n")

    with pytest.raises(TimeoutError, match="deadline"):
        extract_code(sources(), repository_id="repo", deadline=time.monotonic() - 1)

    assert touched is False


def test_extracts_required_python_nodes_and_honest_relationships():
    from code_extractor import extract_code

    content = (
        b"from dep import helper\n"
        b"from fastapi import FastAPI\n"
        b"app = FastAPI()\n"
        b"class Base:\n    pass\n\n"
        b"class User(Base):\n"
        b"    __tablename__ = 'users'\n"
        b"    def save(self):\n        helper()\n\n"
        b"@app.get('/users')\n"
        b"def list_users():\n    return User()\n\n"
        b"def main():\n    list_users()\n\n"
        b"if __name__ == '__main__':\n    main()\n"
    )

    dependency = _source("dep.py", b"def helper():\n    pass\n")
    result = extract_code(
        (_source("src/app.py", content), dependency), repository_id="example/repo"
    )

    kinds = {node["kind"] for node in result.nodes}
    assert {
        "repository", "directory", "file", "module", "class", "method",
        "function", "route", "table", "entry-point",
    } <= kinds
    edges = {assertion["edge_type"] for assertion in result.assertions}
    assert {"CONTAINS", "DEFINES", "IMPORTS", "CALLS", "INHERITS", "EXPOSES"} <= edges
    assert all(assertion["resolution"] == "resolved" for assertion in result.assertions)


def test_preserves_exact_utf8_byte_and_line_spans_for_declarations_and_edges():
    from code_extractor import extract_code

    content = "# cafe\u0301 \U0001f680\ndef target(): pass\ndef caller():\n    target()\n".encode()
    source = _source("app.py", content)
    result = extract_code((source,), repository_id="repo")
    caller = _node(result, "function", "caller")
    occurrence = next(item for item in result.occurrences if item["node_id"] == caller["node_id"])
    call = next(item for item in result.assertions if item["edge_type"] == "CALLS")
    evidence = next(item for item in result.evidence if item["assertion_id"] == call["assertion_id"])

    assert content[occurrence["byte_start"]:occurrence["byte_end"]].startswith(b"def caller")
    assert (occurrence["line_start"], occurrence["line_end"]) == (3, 4)
    assert content[evidence["byte_start"]:evidence["byte_end"]] == b"target()"
    assert evidence["span_sha256"] == hashlib.sha256(b"target()").hexdigest()


def test_uses_stable_non_line_identity_and_scip_symbol_when_supplied():
    from code_extractor import SCIP_DEFINITION_ROLE, ScipSymbol, extract_code

    original = b"def run(value: int):\n    return value\n"
    shifted = b"\n\n" + original
    first = extract_code((_source("app.py", original),), repository_id="repo")
    second = extract_code((_source("app.py", shifted),), repository_id="repo")
    first_run = _node(first, "function", "run")
    second_run = _node(second, "function", "run")
    name_start = shifted.index(b"run")
    symbols = (
        ScipSymbol("source:app.py", shifted.index(b"def run"), len(shifted), "overlap", SCIP_DEFINITION_ROLE),
        ScipSymbol("source:app.py", name_start, name_start + 3, "reference", 0),
        ScipSymbol(
            "source:app.py", name_start, name_start + 3,
            "scip-python . repo 1 app/run().", SCIP_DEFINITION_ROLE,
        ),
    )
    scip_result = extract_code(
        (_source("app.py", shifted),), repository_id="repo", scip_symbols=symbols
    )

    assert first_run["node_id"] == second_run["node_id"]
    assert "@L" not in first_run["identity_key"]
    assert _node(scip_result, "function", "run")["identity_scheme"] == "scip/v1"

    without_exact_definition = extract_code(
        (_source("app.py", shifted),), repository_id="repo", scip_symbols=symbols[:2]
    )
    assert _node(without_exact_definition, "function", "run")["identity_scheme"] == "code-symbol/v1"


def test_unresolved_semantics_are_controlled_observations_with_evidence():
    from code_extractor import extract_code

    content = (
        b"from missing import absent\n"
        b"def run(client):\n"
        b"    client.save()\n"
        b"    absent()\n"
    )
    result = extract_code((_source("app.py", content),), repository_id="repo")

    reasons = {item["reason"] for item in result.observations}
    assert {"dynamic_dispatch", "missing_dependency"} <= reasons
    assert all(item["reason"] in {
        "ambiguous_target", "dynamic_dispatch", "missing_dependency", "parse_error",
        "unresolved_reference", "unsupported_semantics",
    } for item in result.observations)
    observed = {item["observation_id"] for item in result.observations}
    assert observed <= {item["observation_id"] for item in result.evidence}


def test_cross_file_resolution_keeps_ambiguous_and_missing_targets_as_observations():
    from code_extractor import extract_code

    sources = (
        _source(
            "app.py",
            b"from dep import helper\nfrom absent import missing\nhelper()\nmissing()\n",
        ),
        _source("dep.py", b"def helper(): pass\ndef helper(): pass\n"),
    )

    result = extract_code(sources, repository_id="repo")

    assert not [
        item for item in result.assertions
        if item["edge_type"] == "CALLS"
    ]
    observed = {
        (item["edge_type"], item["target_text"], item["reason"])
        for item in result.observations
    }
    assert ("CALLS", "helper", "ambiguous_target") in observed
    assert ("IMPORTS", "absent", "missing_dependency") in observed
    assert ("CALLS", "missing", "missing_dependency") in observed


def test_package_init_relative_import_resolves_sibling_module():
    from code_extractor import extract_code

    result = extract_code(
        (
            _source(
                "scripts/pkg/__init__.py",
                b"from .dep import helper\nhelper()\n",
            ),
            _source("scripts/pkg/dep.py", b"def helper(): pass\n"),
        ),
        repository_id="repo",
    )

    resolved = [
        item for item in result.assertions
        if item["edge_type"] in {"IMPORTS", "CALLS"}
    ]
    assert {item["edge_type"] for item in resolved} == {"IMPORTS", "CALLS"}
    assert not [
        item for item in result.observations
        if item["edge_type"] in {"IMPORTS", "CALLS"}
    ]


def test_package_init_from_dot_import_targets_submodule_when_alias_is_unused():
    from code_extractor import extract_code

    sources = (
        _source("scripts/pkg/__init__.py", b"from . import dep\n"),
        _source("scripts/pkg/dep.py", b"VALUE = 1\n"),
    )
    result = extract_code(sources, repository_id="repo")
    modules = {
        item["node_id"]: item["metadata"]["path"]
        for item in result.nodes
        if item["kind"] == "module"
    }
    imported = next(item for item in result.assertions if item["edge_type"] == "IMPORTS")

    assert modules[imported["target_node_id"]] == "scripts/pkg/dep.py"


def test_ordinary_module_from_dot_import_targets_sibling_submodule():
    from code_extractor import extract_code

    sources = (
        _source("scripts/pkg/__init__.py", b""),
        _source("scripts/pkg/service.py", b"from . import dep\n"),
        _source("scripts/pkg/dep.py", b"VALUE = 1\n"),
    )
    result = extract_code(sources, repository_id="repo")
    modules = {
        item["node_id"]: item["metadata"]["path"]
        for item in result.nodes
        if item["kind"] == "module"
    }
    service_module = next(
        item["node_id"]
        for item in result.nodes
        if item["kind"] == "module"
        and item["metadata"]["path"] == "scripts/pkg/service.py"
    )
    imported = next(
        item for item in result.assertions
        if item["edge_type"] == "IMPORTS" and item["source_node_id"] == service_module
    )

    assert modules[imported["target_node_id"]] == "scripts/pkg/dep.py"


def test_from_dot_import_preserves_ambiguous_submodule_candidates():
    from code_extractor import extract_code

    sources = (
        _source("scripts/pkg/__init__.py", b"from . import dep\n"),
        _source("scripts/pkg/dep.py", b"VALUE = 1\n"),
        _source("scripts/pkg/dep/__init__.py", b"VALUE = 2\n"),
    )
    result = extract_code(sources, repository_id="repo")
    observation = next(
        item for item in result.observations
        if item["edge_type"] == "IMPORTS"
    )

    assert observation["target_text"] == "scripts.pkg.dep"
    assert observation["reason"] == "ambiguous_target"
    assert result.observation_source_dependencies[observation["observation_id"]] == (
        "source:scripts/pkg/dep.py",
        "source:scripts/pkg/dep/__init__.py",
    )


def test_from_dot_import_keeps_package_attribute_as_imported_name():
    from code_extractor import extract_code

    source = _source(
        "scripts/pkg/__init__.py",
        b"def exported(): pass\nfrom . import exported\nexported()\n",
    )
    result = extract_code((source,), repository_id="repo")
    call = next(item for item in result.assertions if item["edge_type"] == "CALLS")
    target = next(item for item in result.nodes if item["node_id"] == call["target_node_id"])

    assert target["kind"] == "function"
    assert target["metadata"]["name"] == "exported"


def test_ordinary_module_relative_import_keeps_module_parent_context():
    from code_extractor import extract_code

    result = extract_code(
        (
            _source(
                "scripts/pkg/service.py",
                b"from .dep import helper\nhelper()\n",
            ),
            _source("scripts/pkg/dep.py", b"def helper(): pass\n"),
        ),
        repository_id="repo",
    )

    assert {item["edge_type"] for item in result.assertions} >= {"IMPORTS", "CALLS"}


def test_nested_package_init_relative_import_ascends_from_package_context():
    from code_extractor import extract_code

    result = extract_code(
        (
            _source(
                "scripts/pkg/sub/__init__.py",
                b"from ..dep import helper\nhelper()\n",
            ),
            _source("scripts/pkg/dep.py", b"def helper(): pass\n"),
        ),
        repository_id="repo",
    )

    assert {item["edge_type"] for item in result.assertions} >= {"IMPORTS", "CALLS"}
    assert not [
        item for item in result.observations
        if item["edge_type"] in {"IMPORTS", "CALLS"}
    ]


def test_relative_import_with_too_many_dots_stays_unresolved():
    from code_extractor import extract_code

    result = extract_code(
        (
            _source(
                "scripts/pkg/__init__.py",
                b"from ....dep import helper\nhelper()\n",
            ),
            _source("scripts/pkg/dep.py", b"def helper(): pass\n"),
        ),
        repository_id="repo",
    )

    assert not [
        item for item in result.assertions
        if item["edge_type"] in {"IMPORTS", "CALLS"}
    ]
    assert {
        (item["edge_type"], item["reason"])
        for item in result.observations
        if item["edge_type"] in {"IMPORTS", "CALLS"}
    } == {
        ("IMPORTS", "missing_dependency"),
        ("CALLS", "missing_dependency"),
    }


def test_ambiguous_candidate_dependency_metadata_is_bounded():
    from code_extractor import ExtractionLimits, extract_code

    sources = (
        _source("app.py", b"from dep import helper\nhelper()\n"),
        _source("one/dep.py", b"def helper(): pass\n"),
        _source("two/dep.py", b"def helper(): pass\n"),
    )

    with pytest.raises(ValueError, match="candidate dependency ceiling"):
        extract_code(
            sources,
            repository_id="repo",
            limits=ExtractionLimits(max_candidate_dependencies=1),
        )


def test_module_alias_lookup_uses_index_without_scanning_10k_modules():
    import code_extractor

    class CountingModules(dict):
        def __init__(self, values):
            super().__init__(values)
            self.iterated = 0

        def __iter__(self):
            for key in super().__iter__():
                self.iterated += 1
                yield key

    class CountingIndex(dict):
        def __init__(self, values):
            super().__init__(values)
            self.lookups = 0

        def get(self, key, default=None):
            self.lookups += 1
            return super().get(key, default)

    collector = code_extractor._Collector(
        (), "repo", (), code_extractor.ExtractionLimits(), None, None
    )
    collector.modules = CountingModules(
        {
            f"package{index}.module{index}": [f"node:{index}"]
            for index in range(10_000)
        }
    )
    target = "package9999.module9999"
    collector.module_name_index = CountingIndex(
        {"module9999": (target,)}
    )
    collector.modules.iterated = 0

    matches = collector._matching_modules("module9999")

    assert matches == (target,)
    assert collector.module_name_index.lookups == 1
    assert collector.modules.iterated == 0


def test_syntax_traversal_checks_cancellation_before_walking_large_fake_tree():
    import code_extractor

    state = {"visited": 0, "checks": 0}

    class FakeNode:
        def __init__(self, children=()):
            self._children = children

        @property
        def named_children(self):
            state["visited"] += 1
            return self._children

    root = FakeNode(tuple(FakeNode() for _ in range(10_000)))

    def cancelled():
        state["checks"] += 1
        return state["checks"] >= 2

    collector = code_extractor._Collector(
        (), "repo", (), code_extractor.ExtractionLimits(), None, cancelled
    )

    with pytest.raises(TimeoutError, match="cancelled"):
        collector._syntax_nodes(root, 20_000)

    assert state["visited"] <= 256


@pytest.mark.parametrize(
    ("argument", "limit_name", "message"),
    [
        ("scip_symbols", "max_scip_symbols", "SCIP symbol ceiling"),
        ("co_changes", "max_co_changes", "co-change ceiling"),
    ],
)
def test_optional_iterable_overflow_stops_after_limit_plus_one(argument, limit_name, message):
    from code_extractor import ExtractionLimits, extract_code

    consumed = 0

    def values():
        nonlocal consumed
        for _index in range(10):
            consumed += 1
            yield object()

    with pytest.raises(ValueError, match=message):
        extract_code(
            (_source("app.py", b"def app(): pass\n"),),
            repository_id="repo",
            limits=ExtractionLimits(**{limit_name: 2}),
            **{argument: values()},
        )

    assert consumed == 3


@pytest.mark.parametrize("argument", ["scip_symbols", "co_changes"])
def test_optional_iterable_consumption_checks_cancellation(argument):
    from code_extractor import ExtractionLimits, extract_code

    state = {"consumed": 0, "cancelled": False}

    def values():
        for _index in range(5):
            state["consumed"] += 1
            if state["consumed"] == 2:
                state["cancelled"] = True
            yield object()

    with pytest.raises(TimeoutError, match="cancelled"):
        extract_code(
            (_source("app.py", b"def app(): pass\n"),),
            repository_id="repo",
            limits=ExtractionLimits(max_scip_symbols=10, max_co_changes=10),
            cancelled=lambda: state["cancelled"],
            **{argument: values()},
        )

    assert state["consumed"] == 2


def test_parse_errors_and_unsupported_languages_degrade_without_fake_edges():
    from code_extractor import extract_code

    sources = (
        _source("broken.py", b"def broken(:\n", language="python"),
        _source("query.xyz", b"opaque syntax\n", language="unknown"),
    )
    result = extract_code(sources, repository_id="repo")

    assert {item["reason"] for item in result.observations} == {
        "parse_error", "unsupported_semantics"
    }
    assert not {item["edge_type"] for item in result.assertions} & {
        "CALLS", "IMPORTS", "INHERITS", "IMPLEMENTS", "READS", "WRITES"
    }


def test_tree_sitter_languages_extract_when_available_and_degrade_when_absent(monkeypatch):
    import code_extractor

    source = _source(
        "app.ts",
        b"class Service {}\nfunction target() {}\nfunction caller() { target(); }\n",
        language="typescript",
    )
    available = code_extractor.extract_code((source,), repository_id="repo")

    assert {node["kind"] for node in available.nodes} >= {"class", "function"}
    assert "CALLS" in {item["edge_type"] for item in available.assertions}

    monkeypatch.setattr(code_extractor, "_optional_parser", lambda language: None)
    degraded = code_extractor.extract_code((source,), repository_id="repo")
    assert {item["reason"] for item in degraded.observations} == {"unsupported_semantics"}


def test_javascript_observation_targets_are_canonical_and_evidence_stays_exact():
    import code_extractor

    with pytest.raises(ValueError, match="must not be empty"):
        code_extractor._canonical_observation_target(" \r\n\t")

    oversized_function = 'client["' + ("e\u0301" * 3000) + '"]'
    content = (
        "function collectTranscript(messages) {\r\n"
        "  return messages\r\n"
        "    .flatMap((message) => Array.isArray(message?.parts) ? message.parts : [])\r\n"
        '    .map((part) => typeof part?.text === "string" ? part.text : "")\r\n'
        "    .filter(Boolean)\r\n"
        '    .join("\\n\\n")\r\n'
        "    .slice(-MAX_TRANSCRIPT_CHARS);\r\n"
        "}\r\n"
        f"function oversized() {{ return {oversized_function}(); }}\r\n"
    ).encode()
    source = _source("llm-wiki-memory-opencode.js", content, language="javascript")

    result = code_extractor.extract_code((source,), repository_id="repo")
    call_observations = [
        item for item in result.observations if item["edge_type"] == "CALLS"
    ]
    targets = [item["target_text"] for item in call_observations]

    assert targets
    assert all(target and "\r" not in target and "\n" not in target for target in targets)
    assert all(len(target) <= 4096 and len(target.encode()) <= 4096 for target in targets)
    assert "messages .flatMap" in targets
    oversized = next(target for target in targets if target.startswith('client["'))
    digest = hashlib.sha256(oversized_function.encode()).hexdigest()
    assert oversized.endswith(f"... [sha256:{digest}]")

    chained = next(item for item in call_observations if item["target_text"].endswith(".slice"))
    evidence = next(
        item for item in result.evidence
        if item["observation_id"] == chained["observation_id"]
    )
    exact_span = content[evidence["byte_start"]:evidence["byte_end"]]
    assert b"\r\n" in exact_span
    assert exact_span.endswith(b".slice(-MAX_TRANSCRIPT_CHARS)")
    assert evidence["span_sha256"] == hashlib.sha256(exact_span).hexdigest()


def test_syntax_only_calls_do_not_cross_module_or_language_boundaries():
    from code_extractor import extract_code

    sources = (
        _source("target.ts", b"function target() {}\n", language="typescript"),
        _source("caller.ts", b"function caller() { target(); }\n", language="typescript"),
    )

    result = extract_code(sources, repository_id="repo")

    assert not [item for item in result.assertions if item["edge_type"] == "CALLS"]
    assert any(
        item["edge_type"] == "CALLS" and item["reason"] in {"ambiguous_target", "unresolved_reference"}
        for item in result.observations
    )


def test_syntax_bare_call_shadowed_by_parameter_is_unresolved():
    from code_extractor import extract_code

    source = _source(
        "app.ts",
        b"function target() {}\nfunction caller(target: () => void) { target(); }\n",
        language="typescript",
    )

    result = extract_code((source,), repository_id="repo")

    assert not [item for item in result.assertions if item["edge_type"] == "CALLS"]
    assert any(
        item["edge_type"] == "CALLS" and item["reason"] == "unresolved_reference"
        for item in result.observations
    )


def test_python_nested_scope_resolution_never_leaks_to_sibling_scope():
    from code_extractor import extract_code

    source = _source(
        "service.py",
        b"def first():\n"
        b"    def helper(): pass\n"
        b"    helper()\n\n"
        b"def second():\n"
        b"    helper()\n",
    )

    result = extract_code((source,), repository_id="repo")

    assert len([item for item in result.assertions if item["edge_type"] == "CALLS"]) == 1
    assert len([
        item for item in result.observations
        if item["edge_type"] == "CALLS" and item["reason"] == "unresolved_reference"
    ]) == 1


def test_missing_python_base_is_an_observation():
    from code_extractor import extract_code

    result = extract_code(
        (_source("child.py", b"class Child(MissingBase):\n    pass\n"),),
        repository_id="repo",
    )

    assert any(
        item["edge_type"] == "INHERITS"
        and item["target_text"] == "MissingBase"
        and item["reason"] == "unresolved_reference"
        for item in result.observations
    )


def test_emits_only_explicit_implements_reads_and_writes_relationships():
    from code_extractor import extract_code

    python = _source(
        "models.py",
        b"class User:\n"
        b"    __tablename__ = 'users'\n\n"
        b"import sqlite3\n\n"
        b"def load(connection: sqlite3.Connection):\n"
        b"    connection.execute('SELECT * FROM users')\n\n"
        b"def save(connection: sqlite3.Connection):\n"
        b"    connection.execute('UPDATE users SET name = 1')\n",
    )
    typescript = _source(
        "service.ts",
        b"interface Service {}\n"
        b"class Implementation implements Service {}\n"
        b"function main() {}\n",
        language="typescript",
    )

    result = extract_code((python, typescript), repository_id="repo")

    edges = {item["edge_type"] for item in result.assertions}
    assert {"IMPLEMENTS", "READS", "WRITES"} <= edges
    assert "entry-point" not in {item["kind"] for item in result.nodes}

    sql_evidence = [
        evidence
        for evidence in result.evidence
        if evidence["assertion_id"] in {
            item["assertion_id"] for item in result.assertions
            if item["edge_type"] in {"READS", "WRITES"}
        }
    ]
    assert {python.content[item["byte_start"]:item["byte_end"]] for item in sql_evidence} == {b"users"}


def test_unsupported_route_entry_and_sql_semantics_are_observations():
    from code_extractor import extract_code

    source = _source(
        "pretender.py",
        b"class Pretender:\n"
        b"    __tablename__ = 'users'\n\n"
        b"@fake.get('/users')\n"
        b"def handler(logger):\n"
        b"    logger.execute('SELECT * FROM users')\n\n"
        b"def main(): pass\n",
    )

    result = extract_code((source,), repository_id="repo")

    assert not {"EXPOSES", "READS", "WRITES"} & {
        item["edge_type"] for item in result.assertions
    }
    assert {"EXPOSES", "READS"} <= {item["edge_type"] for item in result.observations}


def test_framework_and_database_names_without_real_imports_are_not_proof():
    from code_extractor import extract_code

    source = _source(
        "lookalikes.py",
        b"class FastAPI:\n"
        b"    def get(self, path): return lambda function: function\n\n"
        b"class User:\n"
        b"    __tablename__ = 'users'\n\n"
        b"app = FastAPI()\n"
        b"@app.get('/users')\n"
        b"def handler(connection: sqlite3.Connection):\n"
        b"    connection.execute('SELECT * FROM users')\n",
    )

    result = extract_code((source,), repository_id="repo")

    assert not {"EXPOSES", "READS"} & {item["edge_type"] for item in result.assertions}
    assert {"EXPOSES", "READS"} <= {item["edge_type"] for item in result.observations}


def test_static_self_method_resolves_but_dynamic_receiver_is_observed():
    from code_extractor import extract_code

    source = _source(
        "service.py",
        b"class Service:\n"
        b"    def save(self): pass\n"
        b"    def run(self, client):\n"
        b"        self.save()\n"
        b"        client.save()\n",
    )
    result = extract_code((source,), repository_id="repo")

    calls = [item for item in result.assertions if item["edge_type"] == "CALLS"]
    assert len(calls) == 1
    assert [item["reason"] for item in result.observations] == ["dynamic_dispatch"]


def test_bare_method_name_is_not_fabricated_from_class_scope():
    from code_extractor import extract_code

    source = _source(
        "service.py",
        b"class Service:\n"
        b"    def helper(self): pass\n"
        b"    def run(self):\n"
        b"        helper()\n",
    )

    result = extract_code((source,), repository_id="repo")

    assert not [item for item in result.assertions if item["edge_type"] == "CALLS"]
    assert [item["reason"] for item in result.observations] == ["unresolved_reference"]


def test_extraction_is_deterministic_immutable_and_bounded():
    from code_extractor import ExtractionLimits, extract_code

    source = _source("app.py", b"def one(): pass\ndef two(): pass\n")
    first = extract_code((source,), repository_id="repo")
    second = extract_code((source,), repository_id="repo")

    assert first == second
    with pytest.raises(FrozenInstanceError):
        first.nodes = ()
    with pytest.raises(TypeError):
        first.nodes[0]["metadata"]["name"] = "changed"
    with pytest.raises(ValueError, match="node ceiling"):
        extract_code((source,), repository_id="repo", limits=ExtractionLimits(max_nodes=2))


def test_deep_freeze_recursively_freezes_metadata_containers():
    from code_extractor import _deep_freeze

    frozen = _deep_freeze({"nested": {"items": ["one"]}})

    with pytest.raises(TypeError):
        frozen["nested"]["items"][0] = "changed"


def test_output_is_accepted_by_task18_schema(tmp_path):
    from code_extractor import extract_code
    from evidence_graph import create_generation_database

    source = _source("app.py", b"def target(): pass\ndef caller(): target()\n")
    result = extract_code((source,), repository_id="repo")
    create_generation_database(
        tmp_path / "evidence.sqlite3",
        sources=[{
            "source_id": source.record.logical_id,
            "relative_path": source.record.relative_path,
            "sha256": source.record.sha256,
            "size": source.record.size,
            "media_type": source.record.media_type,
            "language": source.record.language,
            "git_oid": source.record.git_oid,
        }],
        source_bytes={source.record.logical_id: source.content},
        nodes=result.nodes,
        occurrences=result.occurrences,
        assertions=result.assertions,
        evidence=result.evidence,
        observations=result.observations,
        dependencies=(),
    )

    assert (tmp_path / "evidence.sqlite3").is_file()


def test_co_change_requires_explicit_captured_history_evidence():
    from code_extractor import CoChange, extract_code

    first = _source("a.py", b"def a(): pass\n")
    second = _source("b.py", b"def b(): pass\n")
    history = _source("history.log", b"commit abc: a.py b.py\n", language="unknown")
    proven = CoChange(
        "a.py", "b.py", 0.8, history.record.logical_id, 0, len(history.content)
    )
    unproven = CoChange("b.py", "a.py", 0.8)

    result = extract_code(
        (first, second, history), repository_id="repo", co_changes=(unproven, proven)
    )

    edges = [item for item in result.assertions if item["edge_type"] == "CO_CHANGED_WITH"]
    assert len(edges) == 1
    evidence = next(item for item in result.evidence if item["assertion_id"] == edges[0]["assertion_id"])
    assert evidence["source_id"] == history.record.logical_id


def test_facade_prefers_store_and_falls_back_to_live_extraction(tmp_path, monkeypatch):
    import code_graph

    (tmp_path / "a.py").write_text("def target(): pass\n", encoding="utf-8")
    (tmp_path / "b.py").write_text(
        "from a import target\ndef caller(): target()\n", encoding="utf-8"
    )
    stored = [{"file": "stored.py", "line": 7, "function": "target"}]
    monkeypatch.setattr(code_graph, "_store_find_callers", lambda name, root: stored)

    assert code_graph.find_callers("target", tmp_path) is stored
    monkeypatch.setattr(code_graph, "_store_find_callers", lambda name, root: None)
    assert any(Path(item["file"]).name == "b.py" for item in code_graph.find_callers("target", tmp_path))


def test_remaining_graph_queries_are_store_first_facades(tmp_path, monkeypatch):
    import code_graph

    expected = {
        "callees": [{"callee": "stored"}],
        "dead": [{"name": "stored"}],
        "architecture": {"entry_points": ["stored"]},
        "communities": [["stored"]],
    }
    monkeypatch.setattr(code_graph, "_store_find_callees", lambda name, root: expected["callees"])
    monkeypatch.setattr(code_graph, "_store_find_dead_code", lambda root: expected["dead"])
    monkeypatch.setattr(
        code_graph,
        "_store_get_architecture",
        lambda root, limit: expected["architecture"],
    )
    monkeypatch.setattr(code_graph, "_store_detect_communities", lambda root: expected["communities"])

    assert code_graph.find_callees("target", tmp_path) == expected["callees"]
    assert code_graph.find_dead_code(tmp_path) == expected["dead"]
    assert code_graph.get_architecture(tmp_path) == expected["architecture"]
    assert code_graph.detect_communities(tmp_path) == expected["communities"]
