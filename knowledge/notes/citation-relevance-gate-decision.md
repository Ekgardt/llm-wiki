---
type: decision
title: "Citation Relevance Gate"
description: "A citation that shares no content with the claim it is offered for is rejected; entailment itself is still not verified and is not claimed."
date: 2026-08-19
confidence: high
source_authority: user
status: active
---
# Citation Relevance Gate

One-sentence summary: A citation that shares no content with the claim it is offered for is rejected; entailment itself is still not verified and is not claimed.

## Decision

Date: 2026-08-19.

`verify_grounded_answer` gains one more hard gate. For every (claim, cited span)
pair, the claim's content tokens must intersect the span's. Content tokens are
words of three characters or more that are not function words, plus character
bigrams for unspaced scripts so Chinese and Japanese are covered. A pair with no
intersection fails the answer with `cited evidence shares no content with the
claim it supports`.

This is stated as a necessary condition, not as entailment. A span from the
right page that repeats one term still passes. What it closes is the case the
audit named: a citation that is truthful about a different subject.

Entailment verification — asking whether the span actually supports the claim —
remains unimplemented and unclaimed. Documentation must not describe the gate as
more than it is.

## Why

The Q&A path already verified path, source hash, span bounds, duplicate
citations, and exact citation use, then handed the answer over. None of that
looks at what the span says. The gap was reachable: a model that cites a real
span about an unrelated subject passed every gate.

An entailment check needs a model call per claim, a prompt and schema, a token
budget, and a labelled set to know its error rate. None of that exists here, and
adding an unmeasured judge to a fail-closed path would trade a known gap for an
unknown one. The deterministic condition costs nothing, runs offline, and is
provably free of false abstentions in the only direction that matters: a claim
sharing not one content word with its evidence.

## Consequences

- A paraphrase that shares at least one content word with its span still passes.
  A full-synonym paraphrase sharing nothing would now abstain; that is the
  intended trade and the reason the threshold is one token, not several.
- The answer path stays offline and adds no model call.
- The remainder of OPEN-017 stays open: relevance is bounded, support is not
  proved. Closing it needs an evaluated entailment judge.

## Source / Evidence

- Explicit operator approval, 2026-08-19; audit item OPEN-017.
- Gate: `scripts/query_memory.py::_require_citation_touches_claim`.
- Regressions: `tests/test_grounded_qa.py` — an unrelated citation is rejected,
  a real paraphrase is kept.

## Related

- [[one-trust-weight-across-retrieval-paths-decision]]
- [[reliable-memory-stage-2]]
- [[classification-measurement-stand-decision]]
