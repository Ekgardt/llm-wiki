# Instrumented repeat: one maintenance task, two verification phases

Date: 2026-11-03. This is a separate repeat of the
[first pair](2026-11-03-dependency-contract-paired-maintenance-result.md), whose
missing usage counters remain missing. No retrospective counters are invented.
The owner authorized two independent participants. Both fresh detached fd9918c4
checkouts used Python 3.14.7, the same task, graph and allowed context, Codex CLI
0.166.0, and the existing selected gpt-6-astra / medium setting. Neither received
the first pair's solution or the other arm's work. FULL read the complete
answer_budget.py; CONTRACT read its qualified interface and semantics. Tests
executed the real provider in both arms. Command-log inspection found no omitted
provider implementation/test-source reads by CONTRACT.

Both independently fixed repeated measurement by copying the envelope and body,
removing only reserved data.answer_cost, and calling the unchanged estimator.
Their first runs reproduced the defect. Both also encountered a preparation
failure: sandbox DNS prevented downloading the real o200k_base tokenizer into
fresh temporary caches. Phase one ended with two context-budget failures on each
arm. Exit zero of the agent process did not count as acceptance.

The coordinator supplied identical upstream tokenizer bytes, checked by the
installed tokenizer and SHA256
446a9538cb6c348e3516120d7c08b09f57c36495e2acfffe59a5bf8b0cfb1a2d.
A fresh verification phase for each arm read only its own previous evidence and
original instructions. This was reconstructed context, not a hidden resumed
conversation. All phase-one failures and phase-two rereads are counted below.
No source changes were needed in phase two: FULL passed 66 tests and CONTRACT
64, with zero skips, plus Ruff, actual CCN/shape and whitespace checks. The
coordinator then ran each implementation against the other arm's tests: 64 and
66 passed respectively. Normal bootstrap and guards remained active throughout.

## Measured participant usage

Counters come from every turn.completed event, including corrections, reports
and verification. Cached input is a subset of input, not an additional charge.
Reasoning output is reported separately without adding it again to output.

| Raw counter | FULL, both phases | CONTRACT, both phases |
| --- | ---: | ---: |
| Input tokens | 1518732 | 1346641 |
| Cached input tokens | 1404288 | 1245440 |
| Uncached input tokens | 114444 | 101201 |
| Output tokens | 7588 | 6711 |
| Reported reasoning output tokens | 413 | 701 |
| Cache-write input tokens | 0 | 0 |
| Input plus output tokens | 1526320 | 1353352 |
| Sum of participant process durations, seconds | 219.375 | 204.148 |

FULL phase-one input/output: 958022/5374; phase two: 560710/2214.
CONTRACT phase-one input/output: 998695/5047; phase two: 347946/1664.
The observed combined participant token difference is 11.33%; uncached input
11.57%. In phase one alone CONTRACT used more total tokens and time. Different
verification narration and rereads contribute to the combined result. This is
one task, not a causal estimate of a general improvement or a billing estimate.

The duration is the sum of active participant processes, including their tools;
it excludes the interval while the coordinator repaired the environment. Root
research, preparation, review, cache repair and cross-check model costs were not
separately metered per arm. The earlier uninstrumented pair also remains part of
the wider project's costs. Consequently **total project/full-cycle savings remain
unproven**. This evidence does not adopt canonical dependency-source omission or
qualify other boundaries. Full-source packets and the 100000-token ceiling stay
in force. A controlled broader trial needs complete coordination accounting and
representative tasks before a general efficiency claim.

Evidence retained privately in cli-pair/: original JSONL/stderr, completion
records, tokenizer-cache-repair.json, participant-usage-summary.json,
root-cross-check.json and cross-test logs. Each participant retains its original
report, phase-two report, patch and verification JUnit. These are derived trial
artifacts, not installed runtime state. The production fix was already integrated
from the first pair independently of the source-omission decision.

Instrumentation reference: [official non-interactive CLI documentation](https://learn.chatgpt.com/docs/non-interactive-mode).
