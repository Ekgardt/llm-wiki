"""NEW-124 and NEW-125: a zero that means "unresolved", and communities that name symbols.

NEW-124. `find_callers` was built only from resolved CALLS assertions, so a
method reached through a variable — `queue.recover_expired_leases()` — answered
**0 callers** while two real ones existed. That is worse than the row-ceiling
refusal it replaced: a refusal says "I cannot", this said "nobody does". The
extractor already records such a call as an *observation* carrying the call
text, the reason and its span; the answer simply never read them.

NEW-125. Communities were lists of bare `code:node:<md5>` strings, naming no
symbol, file or line, so `answer_budget` correctly classified the whole field
as an opaque collection and dropped it — `mode=community` answered nothing.

Every test below fails on the code before the fix: the caller tests with
`KeyError` on fields that did not exist, the community tests because a member
was a hash string rather than a named row.

Assertions are folded into whole-value comparisons rather than one `assert` per
field, because the managed complexity gate counts every `assert` as a branch.
"""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

# Line 2 holds `queue.recover_expired_leases()`; line 3 holds `helper()`. The
# spans below point at those two call sites, so the answer's line numbers are
# read out of real bytes rather than asserted into existence.
SOURCE = (
    b"def run_worker(queue):\n"
    b"    queue.recover_expired_leases()\n"
    b"    helper()\n"
    b"def helper(): pass\n"
)
_DYNAMIC_START = SOURCE.index(b"queue.recover_expired_leases")
_DYNAMIC_END = _DYNAMIC_START + len(b"queue.recover_expired_leases")
_RESOLVED_START = SOURCE.index(b"helper()\n")
_RESOLVED_END = _RESOLVED_START + len(b"helper")

_NODES = (
    ("run_worker", "function", "app", "app.py"),
    ("helper", "function", "app", "app.py"),
    ("recover_expired_leases", "method", "app.Queue", "app.py"),
    ("claim_capture", "method", "app.Queue", "app.py"),
)

# `claim_capture` is deliberately absent: a node with no occurrence is the case
# where a true zero has to stay a true zero.
_DEFINITION_SPANS = {
    "run_worker": (SOURCE.index(b"run_worker"), len(b"run_worker")),
    "helper": (SOURCE.rindex(b"helper"), len(b"helper")),
    "recover_expired_leases": (
        SOURCE.index(b"recover_expired_leases"),
        len(b"recover_expired_leases"),
    ),
}


def _line_of(offset: int) -> int:
    return SOURCE.count(b"\n", 0, offset) + 1


def _node(name: str, kind: str, owner: str, path: str) -> dict:
    return {
        "node_id": name,
        "kind": kind,
        "identity_scheme": "python/v1",
        "identity_key": f"{owner}:{name}",
        "metadata": {"name": name, "owner": owner, "path": path},
    }


def _occurrence(name: str) -> dict:
    start, length = _DEFINITION_SPANS[name]
    return {
        "occurrence_id": f"occurrence-{name}",
        "node_id": name,
        "source_id": "source",
        "role": "definition",
        "byte_start": start,
        "byte_end": start + length,
        "line_start": _line_of(start),
        "line_end": _line_of(start + length),
    }


def _span(evidence_id: str, binding: dict, start: int, end: int) -> dict:
    return {
        "evidence_id": evidence_id,
        "source_id": "source",
        "byte_start": start,
        "byte_end": end,
        "span_sha256": hashlib.sha256(SOURCE[start:end]).hexdigest(),
        **binding,
    }


def _graph_records() -> dict:
    assertion = {
        "assertion_id": "resolved-helper",
        "source_node_id": "run_worker",
        "edge_type": "CALLS",
        "target_node_id": "helper",
        "literal": None,
        "confidence": "high",
        "authority": "ai-derived",
        "resolution": "resolved",
        "extractor": "python/v1",
    }
    observation = {
        "observation_id": "dynamic-recover",
        "source_node_id": "run_worker",
        "edge_type": "CALLS",
        "target_text": "queue.recover_expired_leases",
        "reason": "dynamic_dispatch",
        "extractor": "python/v1",
    }
    return {
        "sources": [
            {
                "source_id": "source",
                "relative_path": "app.py",
                "sha256": hashlib.sha256(SOURCE).hexdigest(),
                "size": len(SOURCE),
                "media_type": "text/x-python",
                "language": "python",
                "git_oid": None,
            }
        ],
        "source_bytes": {"source": SOURCE},
        "nodes": [_node(*record) for record in _NODES],
        "occurrences": [_occurrence(name) for name in sorted(_DEFINITION_SPANS)],
        "assertions": [assertion],
        "evidence": [
            _span(
                "evidence-resolved",
                {"assertion_id": "resolved-helper", "observation_id": None},
                _RESOLVED_START,
                _RESOLVED_END,
            ),
            _span(
                "evidence-dynamic",
                {"assertion_id": None, "observation_id": "dynamic-recover"},
                _DYNAMIC_START,
                _DYNAMIC_END,
            ),
        ],
        "observations": [observation],
        "dependencies": [],
    }


@pytest.fixture()
def repository(tmp_path, monkeypatch):
    """An active generation wired into `code_graph`, with the live scan forbidden."""
    import code_graph
    from generation_catalog import GenerationCatalog
    from repository_scope import resolve_repository_scope

    from tests.test_evidence_graph_recovery import _publish

    catalog = GenerationCatalog(tmp_path / "state")
    _publish(
        catalog,
        "active",
        graph_records=_graph_records(),
        repository_scope=resolve_repository_scope(tmp_path).as_dict(),
    )
    catalog.register("active")
    catalog.activate("active", expected_active=None)
    monkeypatch.setattr(code_graph, "_generation_catalog", lambda directory: catalog)
    monkeypatch.setattr(
        code_graph,
        "_workspace_call_graph",
        lambda directory: (_ for _ in ()).throw(AssertionError("live scan used")),
    )
    return tmp_path


def _basename(row: dict) -> dict:
    """The fixture lives under a temporary root; only the file name is stable."""
    return {**row, "file": Path(str(row["file"])).name}


def _plain_rows(rows: list) -> list:
    return [_basename(row) for row in rows]


def _plain_community(community: dict) -> dict:
    return {**community, "members": _plain_rows(community["members"])}


def test_a_method_reached_through_a_variable_is_named_not_answered_zero(repository):
    import code_graph

    answer = code_graph.find_callers(
        "recover_expired_leases", repository, with_report=True
    )
    counts = (
        answer["callers"],
        answer["unresolved_caller_count"],
        answer["unresolved_callers_truncated"],
    )

    assert counts == ([], 1, False)
    assert _plain_rows(answer["unresolved_callers"]) == [
        {
            "file": "app.py",
            "line": 2,
            "function": "recover_expired_leases",
            "qualified_name": "app.run_worker",
            "call_text": "queue.recover_expired_leases",
            "reason": "dynamic_dispatch",
        }
    ]


def test_a_method_nobody_names_at_all_still_answers_a_true_zero(repository):
    """The new field must not turn every empty answer into a maybe."""
    import code_graph

    answer = code_graph.find_callers("claim_capture", repository, with_report=True)

    assert (answer["callers"], answer["unresolved_callers"]) == ([], [])
    assert answer["unresolved_caller_count"] == 0


def test_a_resolved_caller_is_not_double_counted_as_unresolved(repository):
    import code_graph

    answer = code_graph.find_callers("helper", repository, with_report=True)

    assert [row["qualified_name"] for row in answer["callers"]] == ["app.run_worker"]
    assert answer["unresolved_caller_count"] == 0


def test_the_attribute_match_does_not_read_underscores_as_wildcards(repository):
    """`LIKE '%.recover_expired_leases'` would also match `recover-expired-leases`."""
    import code_graph

    graph = code_graph._active_evidence_graph(repository)
    try:
        counts = tuple(
            graph.unresolved_calls_naming(name)["count"]
            for name in ("recover_expired_leases", "recover.expired.leases", "expired_leases")
        )
    finally:
        graph.close()

    assert counts == (1, 0, 0)


def test_communities_name_symbols_with_a_file_and_a_line(repository):
    import code_graph

    answer = code_graph.detect_communities(repository, with_report=True)
    counts = (
        answer["community_count"],
        answer["community_member_count"],
        answer["communities_truncated"],
    )

    assert counts == (1, 2, False)
    assert _plain_community(answer["communities"][0]) == {
        "size": 2,
        "members_truncated": False,
        "members_omitted": 0,
        "members": [
            {"qualified_name": "app.helper", "file": "app.py", "line": 4},
            {"qualified_name": "app.run_worker", "file": "app.py", "line": 1},
        ],
    }


def test_a_symbol_anchors_the_community_answer(repository):
    """"Which module does X belong to" cannot be served by a bounded listing.

    Measured on this repository: 4 078 communities over 17 194 members, 899 071
    estimated tokens to name them all, and `fuse_rrf`'s community 729th by size.
    The anchor is what makes the question answerable at all.
    """
    import code_graph

    named = code_graph.detect_communities(
        repository, symbol="run_worker", with_report=True
    )
    absent = code_graph.detect_communities(
        repository, symbol="claim_capture", with_report=True
    )

    assert (named["community_count"], len(named["communities"])) == (1, 1)
    assert (absent["community_count"], absent["communities"]) == (0, [])


def test_named_communities_survive_the_budget_that_dropped_the_hashes():
    """The NEW-125 symptom itself: an opaque collection is dropped, a named one is not."""
    from answer_budget import shape_code_answer

    hashed = {
        "architecture": {
            "communities": [["code:node:" + "a" * 32, "code:node:" + "b" * 32]]
        }
    }
    member = {
        "qualified_name": "scripts.retrieval.fuse_rrf",
        "file": "scripts/retrieval.py",
        "line": 1378,
    }
    named = {
        "architecture": {
            "communities": [
                {
                    "size": 2,
                    "members": [member],
                    "members_truncated": True,
                    "members_omitted": 1,
                }
            ]
        }
    }

    assert "communities" not in shape_code_answer(hashed)["architecture"]
    assert shape_code_answer(named)["architecture"]["communities"][0]["members"] == [
        member
    ]


def test_the_community_bound_is_stated_rather_than_implied(repository, monkeypatch):
    """A cut listing has to say how many communities and members it left out."""
    import code_graph

    monkeypatch.setattr(code_graph, "COMMUNITY_LIMIT", 1)
    monkeypatch.setattr(code_graph, "COMMUNITY_MEMBER_LIMIT", 1)
    monkeypatch.setattr(
        code_graph,
        "_stored_communities",
        lambda graph, edges: [["run_worker", "helper"], ["recover_expired_leases"]],
    )

    answer = code_graph.detect_communities(repository, with_report=True)
    stated = (
        answer["community_count"],
        answer["communities_truncated"],
        answer["community_limit"],
        len(answer["communities"]),
    )

    assert stated == (2, True, 1, 1)
    assert _plain_community(answer["communities"][0]) == {
        "size": 2,
        "members_truncated": True,
        "members_omitted": 1,
        "members": [{"qualified_name": "app.run_worker", "file": "app.py", "line": 1}],
    }
