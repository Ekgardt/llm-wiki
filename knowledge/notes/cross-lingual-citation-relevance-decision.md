---
type: decision
title: "Cross-Lingual Citation Relevance"
description: "Word overlap decides a citation only within one script; across scripts only tokens that survive translation count, and where there are none the gate abstains."
date: 2026-08-21
confidence: high
source_authority: user
status: active
---
# Cross-Lingual Citation Relevance

One-sentence summary: Word overlap decides a citation only within one script; across scripts only tokens that survive translation count, and where there are none the gate abstains.

## Decision

Date: 2026-08-21. Narrows [[knowledge/notes/citation-relevance-gate-decision|Citation Relevance Gate]] (2026-08-19), which stands unchanged for pairs written in the same script.

`_require_citation_touches_claim` no longer fails an answer purely because a
claim and its cited span share no content token. It now asks first whether the
two are written in the same script.

**Same script.** Unchanged. No shared content token fails the answer with
`cited evidence shares no content with the claim it supports`.

**Different scripts.** Word overlap carries no signal, so it decides nothing.
What decides is the set of tokens that survive translation: those carrying a
digit or an underscore, and those whose script differs from the claim's own —
which is how an identifier the writer kept verbatim appears inside a translated
sentence. If the claim carries such tokens and the span carries none of them,
the answer fails with `cited evidence shares no content that survives
translation with the claim it supports`.

**Different scripts, no such token.** The gate abstains. This is a real hole and
it is named rather than hidden, on the same footing as the older decision's
statement that entailment is not verified.

## Why

The vault's notes are English by project rule and the operator's questions
arrive in Russian. A correct English span cited under a Russian claim shares
nothing, so the 2026-08-19 gate failed every correct answer in exactly the
configuration this vault runs. Reproduced before the change: claim tokens
`{выполнено, обязательство, отказ, повторяет, пока, сторож}` against span tokens
`{gate, its, obligation, refusal, repeats, stands}`, intersection empty.

A second operator's agent reported adopting the same word-overlap approximation
and reverting it within the hour for the same reason, with the same conclusion:
a gate that always fires gets switched off, which costs more than the gap.

## What was considered and rejected

**An entailment or cross-lingual NLI model.** The correct instrument, and what
the 2026 literature uses. Rejected here: it adds a model dependency and a
runtime cost to a project whose stated preference is deterministic checks, and
this gate has never claimed to verify entailment.

**Transliteration matching between Cyrillic and Latin.** Rejected because the
same literature that makes it attractive also finds that named entities are
transliterated rather than translated, so their surface form changes. A matcher
would produce both misses and false hits while looking authoritative.

**Treating named entities as anchors.** Rejected for the same reason. Only
figures, versions, counts and code identifiers are relied on.

## Evidence

- `scripts/query_memory.py` — `_require_citation_touches_claim`,
  `_require_surviving_overlap`, `_anchor_tokens`, `_dominant_script`.
- `tests/test_grounded_qa.py` — four cases: a correct cross-script citation is
  kept, a kept identifier must still appear, the within-language rule is
  unchanged, and unspaced scripts still match on bigrams.
- `docs/research/2026-08-21-cross-lingual-citation-relevance.md` — the sources
  and what each rules in or out.

## Related

- [[knowledge/notes/citation-relevance-gate-decision|Citation Relevance Gate]] — the decision this narrows.
- [[knowledge/notes/one-trust-weight-across-retrieval-paths-decision|One Trust Weight Across Retrieval Paths]] — the other gate that decides what retrieval returns.
- [[knowledge/notes/a-failing-claim-does-not-destroy-the-answer-decision]] — links to this page.
