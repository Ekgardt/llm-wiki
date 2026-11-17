# Repair refresh retains the context already reviewed

Date: 2026-10-09. Status: local qualification; useful complete installed compilation remains open.

A real installed pass of 15f9722b exposed a failure after a source group was divided for correction. Ordinary refresh reread the vault, then greedily selected context pages again. A newly preferred page could displace a page already present when the critic reviewed the draft. The existing preservation guard correctly refused the child. Repeated division did not solve this selection defect.

A causal regression creates a reviewed page, gives the batch its measured input capacity, then introduces a higher-ranked page that fits individually but displaces the old page. Ordinary refresh demonstrably selects the newcomer; repair failed instead of reaching its existing resolution boundary. This test failed before correction without changing an original assertion.

Repair refresh now retains all previously selected context paths and adds newly required citation context. It reads their current bytes and current targets from the normal fresh vault snapshot; it does not reuse stale page contents. The snapshot construction is shared with ordinary refresh. Existing manifest, model, capacity, citation, transaction and receipt validation remain unchanged. A genuinely missing page remains an explicit refusal. Ordinary initial selection still uses the existing ranking.

This preserves the already approved complete-context contract. It adds no runtime location, persistent schema, provider, retry count, numerical limit or architecture change. Disabling the preservation guard, dropping feedback, retaining stale bytes and retrying ordinary ranking until it happens to succeed were rejected.

## Evidence and remaining work

The causal test failed before correction. The compiler-related set passed: 588 tests; two subsequently added negative/freshness cases passed separately. Python 3.10 checks passed: 46 tests. Actual measurement of ten changed/new/nested callables found maximum CCN 3 and no if or nesting violations. Ruff passed. Actual model publication and complete platform CI still require verification.

The interrupted installed attempt lasted 1008.661 seconds, including 892.794 CPU seconds. Its two completed model calls reported 104405 input and 3762 output tokens. It published no pages. The measured whole-source packing took 300.68 seconds. The process was interrupted through its verified process handle after confirming it had no subprocess, so no live provider call was silently omitted. These costs are retained and this attempt is not described as a completed cycle.

## Current research and alternatives

Checked on 2026-10-09 against three independent primary sources. [Self-Refine](https://arxiv.org/abs/2303.17651) describes iterative use of feedback to revise a draft; it does not establish that replacing supporting context preserves a correction's meaning. [Anthropic's context engineering guidance](https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents) treats context management across iterations as part of achieving the desired behavior; dropping critical context is a different decision from removing redundant material. [OpenAI evaluation guidance](https://developers.openai.com/api/docs/guides/evaluation-best-practices) emphasizes task-specific, representative and regression evaluation. These sources support the evaluation approach, while the repository's explicit complete-context contract and the causal test determine the concrete invariant. They do not prove this installed model's quality or latency.

Source: `tests/test_reviewed_context_cannot_be_reselected_during_repair.py`; unchanged source-repartition, citation, feedback and context tests; private installed attempt `step7-installed-complete-repair-publication-full-cycle-sol-medium-20261009`; verified interruption report `step7-complete-repair-retained-context-confirmed-interruption-20261009.json`.
