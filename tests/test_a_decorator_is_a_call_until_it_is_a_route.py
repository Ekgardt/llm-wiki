"""Audit 3, B25: decorators, route verbs and one expression too deep to render.

Research:
`docs/research/2026-09-17-graph-a-decorator-is-a-call-until-it-is-a-route.md`.
"""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from code_extractor import extract_code  # noqa: E402
from corpus_snapshot import CapturedSource, SourceMetadata, SourceRecord  # noqa: E402

DECORATED = (
    b"def factory(value):\n"
    b"    return lambda item: item\n"
    b"\n"
    b"@factory(1)\n"
    b"def worker():\n"
    b"    return 1\n"
    b"\n"
    b"@factory(2)\n"
    b"class Holder:\n"
    b"    pass\n"
)

FLASK = (
    b"from flask import Flask\n"
    b"\n"
    b"app = Flask(__name__)\n"
    b"\n"
    b"@app.route('/users')\n"
    b"def list_users():\n"
    b"    return 1\n"
    b"\n"
    b"@app.route('/items', methods=['post', 'put'])\n"
    b"def save_item():\n"
    b"    return 2\n"
    b"\n"
    b"@app.get('/health')\n"
    b"def health():\n"
    b"    return 3\n"
)

DEEP = b"def probe():\n    " + b"a" + b".b" * 500 + b"()\n"


def _source(path: str, content: bytes) -> CapturedSource:
    return CapturedSource(
        SourceRecord(
            logical_id=f"source:{path}",
            relative_path=path,
            sha256=hashlib.sha256(content).hexdigest(),
            size=len(content),
            media_type="text/x-python",
            language="python",
            git_oid=None,
        ),
        SourceMetadata(type="code", language="python"),
        content,
    )


def _extract(path: str, content: bytes):
    return extract_code((_source(path, content),), repository_id="repo")


def _call_target_names(result) -> list[str]:
    by_id = {node["node_id"]: node["metadata"].get("name") for node in result.nodes}
    return sorted(
        by_id[item["target_node_id"]]
        for item in result.assertions
        if item["edge_type"] == "CALLS"
    )


def _route_names(result) -> set[str]:
    return {
        node["metadata"]["name"] for node in result.nodes if node["kind"] == "route"
    }


def test_a_parameterised_decorator_keeps_its_call_edge_on_a_function_and_a_class():
    result = _extract("dec.py", DECORATED)

    assert _call_target_names(result) == ["factory", "factory"]


def test_flask_route_is_filed_under_the_verbs_it_declares():
    result = _extract("api.py", FLASK)

    assert _route_names(result) == {
        "GET /users", "POST /items", "PUT /items", "GET /health",
    }


def test_a_route_decorator_is_still_not_a_call():
    result = _extract("api.py", FLASK)
    call_texts = [
        item["target_text"]
        for item in result.observations
        if item["edge_type"] == "CALLS"
    ]

    assert _call_target_names(result) == []
    assert call_texts == ["Flask"]


def test_one_expression_too_deep_to_render_does_not_end_the_extraction():
    result = _extract("deep.py", DEEP)
    texts = [
        item["target_text"]
        for item in result.observations
        if item["edge_type"] == "CALLS"
    ]

    assert [node["metadata"]["name"] for node in result.nodes if node["kind"] == "function"] == ["probe"]
    assert texts == ["<expression nested deeper than 64>"]
