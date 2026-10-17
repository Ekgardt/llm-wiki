# The critic sees selected context and claim meaning

Research date: 2026-10-07. The installed compiler captured existing pages, but
its review prompt supplied only proposed operations and their literal citations.
It also removed claim records entirely. Verified citation bytes do not prove that
a model's subject, relation, value or scope follows from those bytes.

The isolated change supplies the complete already-selected non-daily immutable
source view to the critic, as untrusted context, and compact claim semantics with
the corresponding validated operation evidence index. It omits identity hashes,
ledger bookkeeping and repeated literal text. Existing binding, claim validation,
DLP, budget checks and review verdict schema remain authoritative. A model's
project field is still discarded and recomputed by the existing source-block
project mechanism. The critic program advances to v5, invalidating old review
request identities. Empty-operation handling is unchanged.

This corrects missing review inputs. It does not prove semantic entailment,
necessary-context selection, or a faster useful model cycle. Context omitted by
packing remains omitted. Additional review input can increase tokens or cause the
existing fit check to refuse; no budget is raised. Two distinct supported claims
may share evidence and must not be dropped solely because their spans coincide.

Primary research checked on the research date:

- [W3C PROV-DM](https://www.w3.org/TR/prov-dm/) distinguishes entities and their
  derivations. Exact source provenance does not itself establish claim meaning.
- [Lost in the Middle, TACL 2024](https://aclanthology.org/2024.tacl-1.9/) measures
  limits of using long contexts. It supports testing useful context, not assuming
  that a larger window improves this installed model.
- [OWASP LLM01](https://genai.owasp.org/llmrisk/llm01-prompt-injection/) treats
  external content as untrusted; review inputs cannot become source authority or
  override downstream validation.

Rejected alternatives: full redundant claim records; evidence-hash-only duplicate
suppression; project-specific rules; merely enlarging the optional context window.
A separate proposed mandatory-context association based on authenticated physical
citation overlap requires its own safety, capacity and useful-cycle evidence.

The original isolated regression failed twice: selected decision bytes and compact
semantic claims were absent. The repaired focused checks passed without changing
existing tests. Further connected checks and exact source hashes are recorded in
private qualification artifacts; this document makes no installation or full-audit
completion claim. No model call was made for this change.
