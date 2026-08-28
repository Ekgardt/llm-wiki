# Who computes a claim's provenance

**Date:** 2026-08-28
**Occasion:** `NEW-120` — the claims subsystem has existed for months and has
never produced a single record. Fixing the reader (commit `6abff12`) was
necessary and not sufficient; this note is the current-practice research
required by rule 2 before changing what the compile draft asks a model for.

---

## What was measured here first

The producer end was never exercised. A claim reaches the vault only as
`operation["claims"]` inside a compile plan, and the draft prompt the product
sends never mentioned claims: measured on this vault's own
`knowledge/daily/2026-08-20.md`, the word "claim" appeared **zero** times in the
3 973-character draft prompt. The only place the field existed was the 6 153
characters of JSON schema pasted into the system prompt, where `claims` was one
optional key among many.

That alone would explain an empty ledger. The real behaviour is worse. Given
that exact prompt and schema, the real `claude` provider (223.5 s, from a
neutral working directory) volunteered claims **unasked** on both operations it
drafted — and fabricated every field that is a fact about bytes:

```json
"fingerprint": "a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2",
"evidence": {
  "reference": "daily:2026-08-20 sha256:c337b771…dd block:c337b771 bytes:0-180",
  "sha256": "c337b771…dd",
  …
}
```

The fingerprint is a placeholder pattern (the second claim's was the same
pattern reversed, `f6e5d4c3b2a1…`). `block:` must be `HH:MM:SS`; it got a hex
prefix. `evidence.sha256` must be the digest of `evidence.text`; it got a copy
of an operation marker found in the file. None of it is the model being
careless — a model cannot compute SHA-256 or byte offsets, and it was asked for
three of each.

Fed back through the product's own validation, the consequence was measured:

```
STEP 1  schema validation of the raw draft: PASSED, 2 operations
STEP 2  plan normalization: REFUSED — ValueError: claim semantic fields are not canonical
STEP 3  same draft with `claims` removed: PASSED — 2 operations
```

So the claims subsystem was not merely idle. It was a live hazard on the compile
path: an optional field nobody asked for, which the provider volunteers, and
which destroys the whole plan when it does — with a message that names a
canonicalization check and neither the operation nor the field.

## What current practice says

**Split the fields by who can guarantee them.** Microsoft's ISE engineering blog
describes a "field classification registry": a YAML document that marks every
output field of an extraction system as either `deterministic` (copy from
source, no LLM permitted) or `llm_required`. Deterministic there means exactly
what it means here — timestamps pulled from event logs, log entry IDs that serve
as audit anchors, raw descriptions written during the shift. The stated
principle is "Deterministic facts should be guaranteed by software.
Probabilistic judgments should be delegated to the model, explicitly and
narrowly." Reported effect: schema compliance 0% → 100%, deterministic fields
matching source data at 100%, and the result held when the foundation model was
swapped. That last point matters here: the failure this vault hit is not a
weakness of one provider, and no better model fixes it.

**Provenance is anchored by the system, selected by the model.** The
anchor-constrained framework in *Grounded Knowledge Graph Extraction via LLMs*
(MDPI *Computers* 15(3):178) builds an inventory of spans **and their positions**
before extraction, then constrains the model to that closed set. The reasons
given are the three that apply here: constraining extraction to text-grounded
elements, enabling provenance tracking for verification, and supporting coverage
monitoring. The model never writes a position.

**Constrained decoding does not rescue an impossible field.** The 2026 structured
-output surveys agree that schema-constrained generation guarantees syntactic
and structural conformance. It cannot make `sha256(x)` correct, and this vault's
`claude` backend runs in `prompt` mode anyway (`_base_capabilities`: native only
for `openai` and `ollama`), so the schema is advice, not a constraint.

## What this vault already had, unused

The compiler already computes, for the page's own `## Evidence` section, exactly
what a claim needs. `_evidence_binding` binds a quoted line to a byte span of an
immutable snapshot and returns `reference` (`daily:DATE sha256:… block:HH:MM:SS
bytes:S-E`) and `quote_sha256`. `claims.ClaimPipeline.normalize` already derives
`fingerprint` from the canonical semantic payload. Nothing new had to be
invented; the two halves had simply never been joined, and `assess_claim_
contradictions` — the "sole lifecycle policy boundary" — has **zero callers**
(confirmed by the code graph, generation `2026-08-28T14:43:28Z`, and by grep).

## The split adopted

| Field | Who supplies it | Why |
|---|---|---|
| `subject`, `relation`, `value`, `qualifiers` | model | the sentence's meaning; nothing else can read it |
| `evidence_index` | model | which of the operation's own evidence lines states it |
| `text`, `evidence.text` | derived | the quoted line, verbatim |
| `evidence.reference`, `evidence.sha256` | derived | byte span and digest of an immutable snapshot |
| `fingerprint`, `id` | derived | digests of the canonical semantics |
| `observed_at`, `validity.from` | derived | the entry's own timestamp |
| `lifecycle` | derived (`active`) | a drafted claim is not a lifecycle decision |
| `confidence` (`medium`), `authority` (`ai-derived`) | derived | see below |

`confidence` and `authority` are deliberately taken away from the model. The
page this ledger is written onto is rendered `confidence: medium` /
`source_authority: ai-derived` unconditionally, so a claim lifted from the same
line by the same pass is no more authoritative than the page that carries it.
And the measured draft shows why it matters: the model awarded itself
`"authority": "user"` on both claims. Typed provenance multiplies the score that
decides retrieval order (`one-trust-weight-across-retrieval-paths-decision`), so
a self-assigned authority tier is a self-assigned rank.

`qualifiers` are kept even though they are 1 358 of the candidate schema's 2 973
characters, because `contradiction_pipeline` compares qualifiers to decide
whether two claims are about the same thing; dropping them would make two claims
that differ only by a qualifier collide on one derived id, and one would be
silently discarded as a duplicate.

## A malformed claim costs the claim, not the page

A claim is an optional enrichment; the page is correct without one. Refusing the
whole plan for it was disproportionate and is what this vault already decided in
kind — "a false refusal is more expensive for memory than a weak citation"
(2026-08-25, `OPEN-017`). So a candidate that fails the candidate schema, or
whose `evidence_index` points nowhere, is dropped and **named** on stderr
(`compile_memory: claim dropped on <slug>: <reason>`), and the operation keeps
its page. Silent dropping is the other sin this vault keeps recording, so the
drop is never silent.

## What a claim costs, in tokens (rule 4)

Measured on this vault's `2026-08-20` daily, with the product's own schema
serialization; token figures are the ÷4 English-JSON estimate, and the
characters are exact.

| | before | after | delta |
|---|---|---|---|
| whole draft schema | 6 153 ch (~1 538 tok) | 4 705 ch (~1 176 tok) | **−1 448 ch** |
| — its `claims` part | 4 421 ch (~1 105 tok) | 2 973 ch (~743 tok) | −1 448 ch |
| draft prompt preamble | 387 ch (~97 tok) | 851 ch (~213 tok) | +464 ch |
| **net, per draft call** | | | **−984 ch (≈ −246 tok)** |
| one claim the model emits | 1 200 ch (~300 tok) | 127 ch (~32 tok) | −1 073 ch |
| one derived record, on disk | — | 822 ch | (0 tok — never sent) |

So asking for claims properly is *cheaper* than the schema that never asked, on
every draft call, plus about 270 tokens saved per claim actually emitted.

One added cost was found and removed rather than paid: the critique prompt
serializes each normalized operation, which would have carried the full 822-
character derived records. The reviewer judges whether an operation is specific,
durable and exactly evidenced; a derived claim has nothing in it for a reviewer
to improve, and on a long day 8 records × 16 operations would shrink the review
batches and buy extra provider calls to re-read what cannot change. `claims` is
stripped from the critique payload only; the plan still carries it.

`MAX_CLAIMS_PER_OPERATION` is 8, down from the schema's old 100. Eight facts per
compiled page is generous, and 100 × 127 characters of output per operation is
not a bound worth advertising to a model.

## A second defect found on the way

`_daily_for_evidence` returned the **sole** snapshot of a date, and a long day is
carried as one snapshot per 16 KiB part under a single logical path. Every real
daily of this vault is far past 16 KiB, so a claim on any real day resolved
against no snapshot at all and failed with "compile claim evidence source is
absent from the snapshot". This is the same defect fixed for quoted evidence on
2026-08-24; the claim path read a different helper and kept it. The reference
carries the part digest, so the fix selects by digest and there is nothing
ambiguous to resolve.

## What it produced

End to end in a throwaway vault, real `claude` provider, this vault's own
`knowledge/daily/2026-08-20.md` (3 671 bytes, 193.6 s): 3 operations, 5 claim
candidates, **5 derived records**, 0 dropped, written into 3 page ledgers and
read back by `ClaimIndex.active_records()` with empty `diagnostics()`. One of
them:

```
claim-2026-08-20-80f038ed7ba88880b7d6769cf63b44a6
  pull request scope policy --equals--> {"type":"string","value":"one plan task per PR"}
  daily:2026-08-20 sha256:c69195057163e71b…41 block:23:51:23 bytes:2360-2510
```

The split-day fix on the real 70 063-byte `2026-08-21.md` (5 parts): a claim on
part 4 binds to part 4's digest and the plan normalizes; with
`_daily_for_evidence` restored to the pre-fix rule the identical claim fails
with "compile claim evidence source is absent from the snapshot".

The honest other side, same day, same code: on `knowledge/daily/2026-04-19.md`
the provider drafted 4 operations and **zero** claims. That daily is about
editing conventions rather than settled subject/relation/value facts, and the
prompt says to omit claims when the lines settle none — so it is the contract
working, not a failure. But it is the measured shape of the thing: claims arrive
when a day contains them, not once per compile.

## What is not claimed

That the provider will emit *good* claims. It will emit *valid* ones now — the
fields it cannot get right are no longer asked of it — but whether a drafted
triple is a fact worth keeping is a judgement, and this note measures no such
quality. Nor is entailment verified: the claim's `text` is the cited line
verbatim, which proves the line was said, not that subject/relation/value follow
from it. That limit is the same one already recorded for citations.

## Sources

- [Separating Deterministic Extraction from AI Inference in Industrial Summarization — Microsoft ISE Developer Blog](https://devblogs.microsoft.com/ise/separating-deterministic-extraction-from-ai-inference/)
- [Grounded Knowledge Graph Extraction via LLMs: An Anchor-Constrained Framework with Provenance Tracking — MDPI *Computers* 15(3):178](https://www.mdpi.com/2073-431X/15/3/178)
- [Getting Structured Output From LLMs in 2026 — JSON, Tool Use, and Validation](https://projectsupply.in/blog/structured-output-llm-2026)
- [Structured data extraction from unstructured content using LLM schemas — Simon Willison](https://simonwillison.net/2025/Feb/28/llm-schemas/)
