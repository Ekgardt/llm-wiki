# Judging a citation across two languages — 2026-08-21

Why this was needed: the citation relevance gate added on 2026-08-19 refuses a
(claim, span) pair that shares no content token. In this vault notes are English
by project rule and questions arrive in Russian, so a correct English span under
a Russian claim shares nothing and the answer fails. Reproduced on this machine:
claim tokens `{выполнено, обязательство, отказ, повторяет, пока, сторож}`, span
tokens `{gate, its, obligation, refusal, repeats, stands}`, intersection empty,
`GroundedQAError`. A second agent reported hitting and reverting the same design
within an hour for the same reason.

## What was asked

When a claim and the span cited for it are written in different languages, what
does current practice use to decide whether the span supports the claim, and
what deterministic signal — if any — is left?

## What the sources say

**Lexical overlap is not the method anywhere.** Cross-lingual claim
verification is done with models: fine-tuned small language models transfer
from English to unseen target languages, and cross-lingual fact-checking work
is entity-aware rather than surface-form aware. The 2026 Citation Needed
Detection work spans 18 languages and multiple scripts precisely because
surface matching does not carry across them. Nothing in the current literature
proposes token overlap as a cross-lingual support signal, and the reason is
arithmetic rather than subtle: across scripts the intersection is empty for
related and unrelated pairs alike, so the test has no discriminating power at
all.

**Anchor points are the exception, and they are narrow.** Work on cross-lingual
generalisation describes anchor points as lexical items that overlap between
languages, and finds that even a small number of them helps alignment
measurably. The items that actually survive are figures, versions, counts and
code identifiers. Named entities are the trap: the standard finding is that
named entities are *transliterated rather than translated*, so a person or
place name is not a reliable anchor between Latin and Cyrillic — it changes
surface form. A Latin-script identifier kept verbatim inside a Cyrillic sentence
is a reliable anchor, because keeping it verbatim is what the writer did.

**A check with no signal must not refuse.** This is the same conclusion the
runtime-enforcement reading reached from the other side: a gate that always
fires gets switched off, which costs more than the gap it was meant to close.
An entailment model is the correct instrument here — the 2026 approaches run
one — and none runs in this project.

## What this means for the gate

Keep the audited within-language behaviour exactly as it is, and stop applying
it where it has no signal.

1. When the claim and the span are written in the same script, the existing
   rule stands unchanged: no shared content token fails the answer.
2. When they are not, fall back to anchors — tokens carrying a digit or an
   underscore, and tokens whose script differs from the claim's own, which is
   how a kept identifier appears in a translated sentence. If the claim carries
   anchors and none of them appear in the span, the answer fails.
3. If the claim carries no anchors and the scripts differ, the gate abstains.
   That is a real hole and it is named rather than hidden: cross-lingual support
   is not verified, exactly as entailment is not verified.

Not adopted: an entailment or cross-lingual NLI model. It is the right
instrument and it is what the literature uses, but it adds a model dependency
and a runtime cost to a project whose stated preference is deterministic checks,
and the gate has never claimed to verify entailment.

Not adopted: transliteration matching between Cyrillic and Latin. The same
literature that makes transliteration attractive also shows the surface form
changes, so a matcher would produce both misses and false hits while looking
authoritative.

## Sources

- https://arxiv.org/abs/2605.31136 — Multilingual and cross-lingual citation-needed detection on Wikipedia for lower-resource languages
- https://arxiv.org/pdf/2607.04043 — Claim2Source at CheckThat! 2026: multilingual scientific claim-source retrieval
- https://arxiv.org/pdf/2503.15220 — Entity-aware cross-lingual claim detection for automated fact-checking
- https://arxiv.org/pdf/2404.07982 — The role of language imbalance in cross-lingual generalisation (anchor points)
- https://arxiv.org/pdf/2103.11811 — MasakhaNER (named entities are transliterated rather than translated)
