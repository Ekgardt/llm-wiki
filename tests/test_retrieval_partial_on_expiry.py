"""What an expired retrieval owes the caller.

Before this contract existed, every stop check in `retrieve()` threw away the
legs that had already finished. Measured on the live vault at load 17-19, that
made the MCP path return no rows at all for every one of seven stand cases --
`applied@5` 0.0 -- while the same code on the same generation through the
direct path scored 0.8571. The lexical answer was in hand and was binned.

The contract these tests pin:

  * no new work starts after the stop -- the deadline still binds every backend
    call and the reranker, so the caller's deadline is not extended;
  * work already paid for is returned, labelled `partial` with the stop as
    `fallback_reason` and with `signals_used` / `effective_mode` naming only
    the legs that finished;
  * when nothing finished, the stop propagates unchanged -- a row-less result
    with no signals would be a refusal wearing the clothes of an answer;
  * a `TimeoutError` raised by a *backend* is not salvageable. That says the
    work failed, not that we stopped it.

See `docs/research/2026-08-29-what-an-expired-retrieval-still-owes.md`.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))


def _hit(cid: str, path: str, score: float) -> dict:
    return {
        "candidate_id": cid,
        "chunk_id": cid,
        "path": path,
        "relative_path": path,
        "parent_id": path,
        "score": score,
        "title": Path(path).stem,
        "summary": f"summary {cid}",
        "content": f"body of {cid} " * 3,
        "source_sha256": "a" * 64,
        "project": "demo",
        "byte_start": 0,
        "byte_end": 10,
        "heading_path": [],
    }


def _expire_after_lexical(hits):
    """A lexical backend that finishes and leaves no budget behind it.

    It waits on the clock rather than on `sleep`. `time.sleep(0.06)` against a
    50 ms deadline assumes the sleep never returns early, and on Windows under
    Python 3.10 the timer granularity is coarse enough that it does: measured
    2026-08-31, `py3.10-s3` was the last failing shard, with the dense leg
    running because the budget had not actually expired. Every other version
    and platform passed the same test.
    """
    deadline = time.monotonic() + 0.05

    def lexical(**_kwargs):
        for _ in range(10_000):
            if time.monotonic() > deadline:
                break
            time.sleep(0.001)
        return hits

    return deadline, lexical


def test_the_finished_lexical_leg_survives_the_deadline():
    import retrieval

    deadline, lexical = _expire_after_lexical([_hit("a", "a.md", 1.0)])
    dense_calls = []

    result = retrieval.retrieve(
        "needle",
        requested_profile="HYBRID",
        lexical_backend=lexical,
        dense_backend=lambda **_k: dense_calls.append(True) or [],
        rerank_enabled=False,
        deadline_monotonic=deadline,
    )

    assert dense_calls == []
    assert [item.relative_path for item in result.candidates] == ["a.md"]


def test_a_salvaged_answer_says_it_is_partial_and_why():
    import retrieval

    deadline, lexical = _expire_after_lexical([_hit("a", "a.md", 1.0)])

    result = retrieval.retrieve(
        "needle",
        requested_profile="HYBRID",
        lexical_backend=lexical,
        dense_backend=lambda **_k: [_hit("b", "b.md", 1.0)],
        rerank_enabled=False,
        deadline_monotonic=deadline,
    )

    # HYBRID was asked for and one leg answered, so the mode must say BASE and
    # the signals must name only that leg. Read as one tuple: every field of
    # the label is part of the same claim, and a caller reads them together.
    label = (
        result.trace.partial,
        result.trace.fallback_reason,
        result.trace.signals_used,
        result.trace.effective_mode,
        result.trace.reranker_applied,
    )
    assert label == (True, "deadline_expired_partial_result", ("lexical",), "BASE", False)


def test_a_cancelled_run_names_the_cancel_not_the_clock():
    import retrieval

    stop = {"now": False}

    def lexical(**_kwargs):
        stop["now"] = True
        return [_hit("a", "a.md", 1.0)]

    result = retrieval.retrieve(
        "needle",
        requested_profile="BASE",
        lexical_backend=lexical,
        rerank_enabled=False,
        cancelled=lambda: stop["now"],
    )

    assert result.trace.fallback_reason == "cancelled_partial_result"
    assert result.trace.partial is True


def test_an_expiry_before_any_leg_finished_still_raises():
    import retrieval

    calls = []

    with pytest.raises(TimeoutError, match="deadline exceeded"):
        retrieval.retrieve(
            "needle",
            requested_profile="BASE",
            lexical_backend=lambda **_k: calls.append(True) or [],
            rerank_enabled=False,
            deadline_monotonic=time.monotonic() - 1,
        )

    assert calls == []


def test_a_backend_timeout_is_not_salvaged():
    """The backend failed; we did not stop it. That difference is the boundary."""
    import retrieval

    def lexical(**_kwargs):
        raise TimeoutError("legacy lexical deadline")

    with pytest.raises(TimeoutError, match="legacy lexical deadline"):
        retrieval.retrieve(
            "needle",
            requested_profile="BASE",
            lexical_backend=lexical,
            rerank_enabled=False,
            deadline_monotonic=time.monotonic() + 30,
        )


def test_a_backend_timeout_after_a_finished_leg_is_still_raised():
    import retrieval

    def dense(**_kwargs):
        raise TimeoutError("legacy dense deadline")

    with pytest.raises(TimeoutError, match="legacy dense deadline"):
        retrieval.retrieve(
            "needle",
            requested_profile="HYBRID",
            lexical_backend=lambda **_k: [_hit("a", "a.md", 1.0)],
            dense_backend=dense,
            rerank_enabled=False,
            deadline_monotonic=time.monotonic() + 30,
        )


def test_salvage_starts_no_new_work_and_runs_no_reranker(monkeypatch):
    import retrieval

    deadline, lexical = _expire_after_lexical([_hit("a", "a.md", 1.0)])
    reranked = []

    monkeypatch.setattr(
        retrieval,
        "_reranked_candidates",
        lambda *_a, **_k: reranked.append(True) or (),
    )

    answer = retrieval.retrieve(
        "needle",
        requested_profile="BASE",
        lexical_backend=lexical,
        rerank_enabled=True,
        deadline_monotonic=deadline,
    )

    assert reranked == []
    assert answer.trace.partial is True


def test_salvage_is_bounded_in_memory_work():
    """The unwinding must not cost anything like a leg. Measured, not assumed."""
    import retrieval

    deadline, lexical = _expire_after_lexical(
        [_hit(f"c{index}", f"p{index % 20}.md", 1.0 / (index + 1)) for index in range(400)]
    )

    started = time.monotonic()
    result = retrieval.retrieve(
        "needle",
        requested_profile="BASE",
        lexical_backend=lexical,
        rerank_enabled=False,
        limit=5,
        deadline_monotonic=deadline,
    )
    spent = time.monotonic() - started

    assert result.trace.partial is True
    assert len(result.candidates) == 5
    # 0.06 s of that is the backend's own sleep. The salvage itself is the rest,
    # and a whole second of headroom is far above anything measured for it.
    assert spent < 1.06
