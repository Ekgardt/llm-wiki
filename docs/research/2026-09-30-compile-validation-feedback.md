# Compile retries carry validator feedback

Date: 2026-09-30. Installed Codex 0.159.2, configured gpt-6-luna/max.

The recovery run repeated the same immutable-evidence validation failure three
 times. `_CompileAttempt._record` printed the reason, but `_drafted` sent the
identical source prompt on each retry. A regression with a genuinely nonexistent
quotation reproduces this information loss: the second prompt lacks the actual
validator failure on the old implementation.

Primary sources reviewed today:
- [Pydantic AI retries](https://pydantic.dev/docs/ai/core-concepts/retries/):
  validation failures are returned to the model as correction feedback.
- [OpenAI structured outputs](https://developers.openai.com/api/docs/guides/structured-outputs):
  schema compliance does not guarantee correct values; semantic validation remains necessary.
- [Anthropic troubleshooting](https://support.anthropic.com/en/articles/7996857-my-prompt-isn-t-giving-me-a-helpful-answer):
  follow-up feedback clarifies what a response needs to correct.

Decision: retain the existing retry policy and full immutable inputs, adding the
latest validation detail as explicitly labelled diagnostic data. The provider's
existing transport redaction still applies. Budget admission checks the enlarged
prompt before a call. The draft program version changes so old cached plans do
not stand in for the new contract. No dependency or runtime layout changes.

Alternatives: blind retries spend calls without useful feedback; weakening quote
matching would admit unsupported knowledge; replaying the entire invalid answer
would add unnecessary tokens and invalid claims. We use the existing validator
message, preserve all source bytes and never mark rejected work compiled.

The test proves feedback delivery and a subsequent valid plan, not that every
model mistake is now cured. A second test proves feedback participates in budget
admission. Existing provider failure, retry exhaustion, cache, claims and
transaction tests continue to apply. Live recovery must still complete before
claiming the backlog repaired.

## Feedback must fit alongside required inputs (2026-09-30 follow-up)

The local recovery journal at 18:36:54 recorded `critique:validation_error`
followed by `draft:input_budget`. Packing had filled the draft window with
optional pages, leaving no room for the validator's diagnostic. A regression
reproduced this exact failure lineage before the fix, then committed a validated
plan after it. This is a packing defect, not evidence that the provider's own
context window was exhausted. The installed Codex metadata lists 272000 tokens
for GPT-6 Luna, whereas compilation currently uses an application budget of32768.
This fix does not silently replace that budget or remove admission checks.

On a validation retry that no longer fits, rerun the existing relevance-based
selection of whole optional pages with the full feedback included in token
accounting. Retain every required daily part, its exact bytes and manifest,
all target snapshots and the original vault-file inventory. Return the effective
batch with the resolved plan, so publication and packing provenance refer to the
same inputs. Recompute action-source descriptors for cache identity. If required
sources plus feedback still cannot fit, refuse before another provider call.
No source files are deleted and no source or diagnostic is truncated.

Alternatives: omitting feedback repeats the original mistake; truncation loses
evidence; enlarging the budget without a provider-wide contract masks packing
and cost assumptions. Reselecting already optional context uses the established
pipeline, keeps the model and reserves, and spends no extra provider attempt
before the request fits. The trade-off is less supplementary context in that
retry, prioritized by the existing relevance ordering.

Independent primary references checked on 2026-09-30:
- [Python dataclass replacement](https://docs.python.org/3/library/dataclasses.html#dataclasses.replace)
  for explicit immutable value replacement.
- [Bazel remote caching](https://bazel.build/remote/caching) for cache identities
  derived from action inputs, not just an output label.
- [Nix input identity](https://nixos.org/guides/how-nix-works/) for the relation
  between declared inputs and reusable results.

Contracts and checks: required-source preservation, optional-page exclusion,
cache descriptors, measured retry input size, real transaction publication,
and refusal without dispatch when mandatory data still exceeds the budget.
The initial related suite passed150tests; the size-boundary variant additionally
covers a17KBdaily payload and40KBof optional material. No persistent schema,
dependency, runtime layout or host configuration changes. The former duplicate
retry-prompt formatter is replaced by one shared measured/rendered formatter.
