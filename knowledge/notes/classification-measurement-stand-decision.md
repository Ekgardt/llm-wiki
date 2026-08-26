---
type: decision
title: "Classification Measurement Stand"
description: "Session classification is measured by a labelled corpus and three gates; the shipped corpus is public and small, and the real number needs real sessions."
date: 2026-08-19
confidence: high
source_authority: user
status: active
---
# Classification Measurement Stand

One-sentence summary: Session classification is measured by a labelled corpus and three gates; the shipped corpus is public and small, and the real number needs real sessions.

## Decision

Date: 2026-08-19.

`benchmark/run_flush_classification.py` scores the classification step against a
labelled corpus and reports three numbers:

- `tier_accuracy` — how often the tier matches the label;
- `durable_content_recall` — of the sessions that hold something durable, the
  share whose distilled block still contains every marker the label requires;
- `false_promotion_rate` — of the sessions that hold nothing, the share promoted
  into durable memory anyway.

Release thresholds live in the corpus, not the runner: 0.85, 0.9, and 0.1. The
runner exits non-zero when a gate fails.

The stand scores the prompt the product sends. `build_classification_prompt` and
`CLASSIFICATION_SYSTEM_PROMPT` were extracted from `flush_memory.py` for exactly
that reason, and a regression asserts the stand uses them rather than a copy.

Two adapters: `canned` replays a labelled response per case, which measures the
parser and the retention rule offline and deterministically; `provider` calls
the configured model through `llm_client`, which measures the model too.

## Why

The audit's open item was that nobody knew the share of sessions whose useful
content never reached durable memory. Only response parsing was tested, and a
parser test cannot answer that question.

Thresholds are stated as provisional. They were chosen to be meaningful rather
than measured — no real corpus exists to calibrate them against, and pretending
otherwise would be the same mistake the item was raised about.

## Consequences

- The shipped corpus is nine synthetic public cases in English and Russian,
  covering decisions, lessons, commands, gotchas, open questions, and pure
  status chatter. It proves the stand works. It does not answer the question.
- The real number requires an installed vault's own sessions, labelled by their
  operator, passed with `--corpus`. That corpus must never enter this public
  repository.
- `--adapter provider` costs one model call per case. The default costs none.
- When the thresholds are calibrated against a real corpus, the numbers change
  in the corpus file and this page is superseded rather than edited.

## Source / Evidence

- Explicit operator approval, 2026-08-19; audit item OPEN-034.
- Runner: `benchmark/run_flush_classification.py`.
- Corpus and schema: `benchmark/flush-classification-v1.json`,
  `benchmark/flush-classification-v1.schema.json`.
- Regressions: `tests/test_flush_classification_benchmark.py` — a dropped
  decision fails the recall gate, promoted chatter fails the promotion gate,
  and the stand is proved to use the product's own prompt.

## Related
- [[knowledge/notes/session-promotion-policy-decision]] — хранение безусловно; повышение до страницы решает консолидация по всей записи.

- [[citation-relevance-gate-decision]]
- [[one-trust-weight-across-retrieval-paths-decision]]
