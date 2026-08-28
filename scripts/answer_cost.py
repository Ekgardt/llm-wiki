"""OPS-02: every tool answer states what it cost.

Roadmap `docs/DEVELOPER-AUDIT-STATUS-2026-08-18.md` §12. Rule 4 requires the
product to spend tokens frugally in operation; until now that was asserted and
never observed at runtime, because every cost number the vault has
(`CODE-06`, `CODE-07`, `MEM-10`) came from a benchmark harness after the fact.

Four properties are load-bearing.

1. **One estimator.** `answer_budget.estimate_tokens` is imported, never
   re-implemented. Two token counters that disagreed would be worse than none,
   and reusing it makes a runtime number directly comparable to the harness
   numbers. Its known bias is measured in the research note: it serialises
   compactly while the wire form is `indent=2`, so it understates by ~4 % on a
   large answer and ~9-13 % on a tiny one.

2. **A missing measurement reads as missing.** `0` is a legal value for both
   tokens and milliseconds, so it can never double as "unknown". An estimate
   that cannot be produced is `null`; a block that cannot be built is absent.
   This is the OpenTelemetry GenAI rule verbatim - "If instrumentation cannot
   efficiently obtain number of input and/or output tokens […] it MUST NOT
   report usage metric" - not a local invention.

3. **Stages are read, not re-derived.** The envelope's `components` map already
   carries `fresh`/`stale`/`missing`/`unknown` per retrieval leg, folded there
   by `mcp_server._recall_components` from the trace fields `signals_used`,
   `fallback_reason` and `reranker_fallback_reason`. A second derivation could
   disagree with the first.

4. **The telemetry obeys the law it measures.** The block costs a measured
   handful of tokens. Against a retrieval answer that is a rounding error;
   against a cheap answer it is a quarter of the payload. So by default it is
   attached only when it costs at most `MAX_SHARE_OF_ANSWER` of the answer it
   describes, and `LLM_WIKI_ANSWER_COST=always` forces it on for an operator
   auditing cheap tools.

Research: `docs/research/2026-08-28-answer-cost-telemetry.md`.
"""

from __future__ import annotations

import os

from answer_budget import estimate_tokens

# The key the block occupies inside the envelope's `data`. Not a top-level
# envelope field: `mcp_contract.envelope_schema()` declares
# `additionalProperties: False` over its twelve keys and is handed to the SDK
# as every tool's outputSchema, so a thirteenth key would make every answer
# fail its own declared schema.
COST_KEY = "answer_cost"

# Named in-band beside the number, because the count is an offline estimate
# rather than a provider-reported one and the caller cannot tell from a bare
# integer which it is holding.
ESTIMATE_METHOD = "chars/4"

MODE_ENV = "LLM_WIKI_ANSWER_COST"
_AUTO = "auto"
_ALWAYS = "always"
_NEVER = "never"
_MODES = frozenset({_AUTO, _ALWAYS, _NEVER})

# The block is attached when it costs at most this share of the answer it
# describes. Measured on the live vault 2026-08-28, after the block was itself
# trimmed against rule 4 - see MEASURED_BLOCK_TOKENS.
MAX_SHARE_OF_ANSWER = 0.01

# What the block costs, measured 2026-08-28, so a reader need not run it to
# find out: 23 tokens on an answer with no optional stages, 52 on a retrieval
# answer carrying the stage line and a refusal reason. At 1 % that admits
# answers from ~2 300 tokens (~5 200 with stages) upward; a `recall` answer
# measured 12 525. `tests/test_answer_cost.py` holds both to the promise.
MEASURED_BLOCK_TOKENS = {"without_stages": 23, "with_stages": 52}

# Digit widths move the block by a token or two; the ceiling is what a caller
# may rely on, the numbers above are what a canonical block actually costs.
BLOCK_TOKEN_CEILING = 60

# A stage that produced evidence, even stale evidence, ran; a stage recorded as
# missing was refused, timed out or was never asked for, and `not_run_reason`
# says which. Anything else is unknown and is reported as unknown - never
# folded into "ran", which would read as a stage that was paid for.
_RAN = "ran"
_NOT_RUN = "not_run"
_UNKNOWN = "unknown"
_STATE_BY_FRESHNESS = {"fresh": _RAN, "stale": _RAN, "missing": _NOT_RUN}
_STATES = (_RAN, _NOT_RUN, _UNKNOWN)

# One packed field rather than three keys: measured 2026-08-28, three keys cost
# 23 tokens against this form's 14, and at `indent=2` every extra key is a
# whole line. The block reports rule 4; it does not get to be exempt from it.
_STAGE_KEY = "stages"

# Read in order; the first one set names why a leg did not run.
_REASON_KEYS = ("fallback_reason", "reranker_fallback_reason")


def attach_answer_cost(envelope, *, elapsed_seconds, budget_seconds) -> bool:
    """Attach one cost block to an answer body; report whether it was attached.

    Returns False rather than raising whenever the answer cannot carry the
    block, so a telemetry problem can never cost the caller its answer.
    """
    data = _answer_body(envelope)
    if data is None:
        return False
    block = build_cost_block(
        envelope, elapsed_seconds=elapsed_seconds, budget_seconds=budget_seconds
    )
    if not _worth_its_own_cost(block):
        return False
    data[COST_KEY] = block
    return True


def build_cost_block(envelope, *, elapsed_seconds, budget_seconds) -> dict:
    """The cost of one answer: tokens, wall time, and which stages ran.

    `tokens_estimated` measures the answer *without* this block. Measuring it
    with the block would be circular - the number changes the size it reports -
    and `answer_budget` resolves the same circularity the same way, by holding
    a fixed allowance back rather than measuring after the fact.
    """
    block = {
        "tokens_estimated": _estimated_tokens(envelope),
        "estimate_method": ESTIMATE_METHOD,
        "duration_ms": _milliseconds(elapsed_seconds),
        "budget_ms": _milliseconds(budget_seconds),
    }
    block.update(_stage_fields(envelope))
    return block


def _answer_body(envelope):
    """The dict the block can be attached to, or None when there is not one."""
    if not isinstance(envelope, dict):
        return None
    data = envelope.get("data")
    if isinstance(data, dict):
        return data
    return None


def _estimated_tokens(envelope):
    """The estimate, or None when it cannot be produced. Never 0 as a stand-in."""
    try:
        return estimate_tokens(envelope)
    except (TypeError, ValueError, RecursionError):
        return None


def _milliseconds(seconds):
    """Whole milliseconds, or None when the clock reading is unusable."""
    if isinstance(seconds, bool) or not isinstance(seconds, (int, float)):
        return None
    if seconds < 0:
        return None
    return round(seconds * 1000)


def _stage_fields(envelope) -> dict:
    """Which optional stages ran, from the components map the envelope already has."""
    components = envelope.get("components")
    if not isinstance(components, dict) or not components:
        return {}
    fields = _named_stage_buckets(components)
    return _with_refusal_reason(fields, envelope)


def _named_stage_buckets(components: dict) -> dict:
    buckets = {state: [] for state in _STATES}
    for name, detail in sorted(components.items()):
        buckets[_stage_state(detail)].append(str(name))
    return {_STAGE_KEY: "; ".join(_stated_buckets(buckets))}


def _stated_buckets(buckets: dict) -> list:
    named = [(state, ", ".join(names)) for state, names in buckets.items() if names]
    return [f"{state}: {names}" for state, names in named]


def _stage_state(detail) -> str:
    return _STATE_BY_FRESHNESS.get(_freshness_of(detail), _UNKNOWN)


def _freshness_of(detail) -> str:
    if not isinstance(detail, dict):
        return _UNKNOWN
    return str(detail.get("freshness", _UNKNOWN))


def _with_refusal_reason(fields: dict, envelope) -> dict:
    reason = _refusal_reason(envelope)
    if reason is None:
        return fields
    return {**fields, "not_run_reason": reason}


def _refusal_reason(envelope):
    """The planner's own word for why a leg did not run, reused not re-derived."""
    trace = _retrieval_trace(envelope)
    stated = [value for value in map(trace.get, _REASON_KEYS) if _is_stated(value)]
    if not stated:
        return None
    return stated[0]


def _is_stated(value) -> bool:
    return isinstance(value, str) and bool(value)


def _retrieval_trace(envelope) -> dict:
    data = _answer_body(envelope)
    if data is None:
        return {}
    trace = data.get("retrieval_trace")
    if isinstance(trace, dict):
        return trace
    return {}


def _worth_its_own_cost(block: dict) -> bool:
    """Telemetry that breaks rule 4 to report rule 4 is not worth attaching."""
    mode = _mode()
    if mode != _AUTO:
        return mode == _ALWAYS
    return _is_rounding_error(block)


def _mode() -> str:
    value = os.environ.get(MODE_ENV, _AUTO).strip().lower()
    if value in _MODES:
        return value
    return _AUTO


def _is_rounding_error(block: dict) -> bool:
    answer = block.get("tokens_estimated")
    if not isinstance(answer, int) or answer <= 0:
        # An answer whose size is unknown cannot be shown to afford the block,
        # and the fail-closed side of an unknown is to spend nothing.
        return False
    return block_tokens(block) <= answer * MAX_SHARE_OF_ANSWER


def block_tokens(block: dict) -> int:
    """What the block itself costs, by the same estimator it reports with."""
    return estimate_tokens(block)
