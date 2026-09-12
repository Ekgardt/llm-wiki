"""What a call passes, and which route a client call reaches (#24, B).

The extractor records argument bindings, not data flow: for one resolved
call it names which caller-visible name lands on which parameter of the
callee. A client call with a literal path names the route node of the same
method and path. Research:
`docs/research/2026-09-11-argument-bindings-and-route-calls.md`.
"""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from corpus_snapshot import CapturedSource, SourceMetadata, SourceRecord  # noqa: E402

SERVICE = b"""\
from fastapi import APIRouter

router = APIRouter()


@router.get("/items")
def list_items(limit, offset=0):
    return [limit, offset]
"""

CLIENT = b"""\
import requests


def fetch(limit, cursor):
    return requests.get("https://service.example/items", params=limit)


def call_local(limit, offset):
    return requests.post("/unknown", json=offset)
"""

CALLER = b"""\
def receive(first, second=None):
    return (first, second)


def send(alpha, beta):
    return receive(alpha, second=beta)


def send_literal(alpha):
    return receive("text")
"""


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


def _extract(**files: bytes):
    from code_extractor import extract_code

    return extract_code(
        [_source(name.replace("_", "/") + ".py", content) for name, content in files.items()],
        repository_id="repo",
    )


def _edges(result, edge_type: str) -> list[dict]:
    return [item for item in result.assertions if item["edge_type"] == edge_type]


def _span(result, assertion_id: str) -> tuple[str, int, int]:
    item = next(row for row in result.evidence if row["assertion_id"] == assertion_id)
    return (str(item["source_id"]), int(item["byte_start"]), int(item["byte_end"]))


def _callee_of(result, binding: dict) -> str:
    """The callee a binding belongs to: the CALLS edge over the same call span."""
    span = _span(result, str(binding["assertion_id"]))
    call = next(
        row
        for row in _edges(result, "CALLS")
        if _span(result, str(row["assertion_id"])) == span
    )
    return _node_name(result, str(call["target_node_id"]))


def _node_name(result, node_id: str) -> str:
    return next(
        str(node["metadata"].get("name"))
        for node in result.nodes
        if node["node_id"] == node_id
    )


def test_a_resolved_call_records_which_argument_binds_which_parameter():
    """The binding carries no target node; the CALLS edge of the same span names it."""
    result = _extract(caller=CALLER)
    flows = _edges(result, "BINDS_ARGUMENTS")
    literals = sorted(str(item["literal"]) for item in flows)

    assert (literals, [_callee_of(result, item) for item in flows]) == (
        ["alpha->first,beta->second"],
        ["receive"],
    )
    assert [item["target_node_id"] for item in flows] == [None]


def test_a_literal_argument_and_an_unresolved_call_bind_nothing():
    """`receive("text")` passes no caller-visible name; an unknown callee proves nothing."""
    unknown = b"def send(alpha):\n    return missing(alpha)\n"
    result = _extract(caller=CALLER, unknown=unknown)
    bound = {item["literal"] for item in _edges(result, "BINDS_ARGUMENTS")}

    assert bound == {"alpha->first,beta->second"}


def _unresolved_routes(result) -> list[str]:
    return [
        str(item["target_text"])
        for item in result.observations
        if item["edge_type"] == "HTTP_CALLS"
    ]


def test_a_client_call_reaches_the_route_of_the_same_method_and_path():
    result = _extract(service=SERVICE, client=CLIENT)
    calls = _edges(result, "HTTP_CALLS")
    reached = [(_node_name(result, item["target_node_id"]), item["confidence"]) for item in calls]

    assert (reached, _unresolved_routes(result)) == (
        [("GET /items", "medium")],
        ["POST /unknown"],
    )


def test_bindings_are_bounded_in_count_and_bytes():
    from code_extractor import MAX_BINDINGS, _bounded_bindings

    pairs = [f"argument{index}->parameter{index}" for index in range(20)]
    bounded = _bounded_bindings(pairs)
    long_pairs = [f"{'a' * 120}->{'b' * 120}", "second->second"]

    assert (bounded.endswith(f"+{20 - MAX_BINDINGS} more"), len(bounded.encode()) <= 256) == (
        True,
        True,
    )
    assert _bounded_bindings(long_pairs).endswith("+1 more")
