# A record too large to keep whole

Date: 2026-08-26.
Question: `knowledge/notes/bounded-capture-excerpt-decision.md` replaced a
refusal with an excerpt — over 900 KiB, the capture adapter keeps
`CAPTURE_EXCERPT_SIDE_BYTES` from each end and joins them with a marker naming
the dropped byte count. Of 487 transcripts on this machine 36 exceed the bound
and the largest is 105 MB. What does current practice do when a record is too
large to keep whole, what does each option lose, and does this vault's own
2026-08-25 measurement — which tested head-and-tail against tail-only on forty
real sessions — argue against the shape now shipped?

## Finding 1 — keeping both ends is the shipped default, and it is not a 50/50 split

OpenRouter's `middle-out` transform is the closest production analogue: when
input exceeds the context window it "compresses prompts and message chains to
the context size by removing or truncating messages in the middle of the
prompt", and its context-compression plugin "keeps half of the messages from the
start and half from the end of the conversation". It is on by default for
endpoints at or below 8k. The stated justification is the *lost in the middle*
result — models attend least to material away from the edges — so both ends are
kept and the middle is what goes. That is the same shape the decision chose, and
it is a default rather than an exotic option.

The one place where the split ratio was measured rather than assumed does not
choose halves. Sun et al., *How to Fine-Tune BERT for Text Classification?*
(arXiv:1905.05583), compare head-only (first 510 tokens), tail-only (last 510)
and head+tail, and the head+tail they report is "empirically select the first
128 and the last 382 tokens" — 25% head, 75% tail. That ratio was chosen by
measurement on IMDb and Sogou, and head+tail won on both.

This vault ships halves in two places: `CAPTURE_EXCERPT_SIDE_BYTES =
MAX_CAPTURE_EVIDENCE_BYTES // 2` in `scripts/integration_adapter.py`, and
`_within_share` in `scripts/episode_consolidation.py`, which takes `share // 2`
from each end. Neither ratio was measured; both are the obvious default.

## Finding 2 — the alternatives, and what each gives up

**Reservoir sampling** (Vitter) gives a uniform sample of an unbounded stream in
fixed memory, which is the property the head/tail window does not have: a
uniform sample can reach the middle. What it loses is contiguity and order —
entries are replaced at random throughout the stream, so what survives is a
scatter of lines rather than readable passages, and there is no guarantee the
last turn survives at all. For a record whose purpose is that a human can read
the conversation back, that is the wrong trade.

**Structured summarisation** (map-reduce over chunks) keeps semantic coverage of
the whole document rather than of its ends. It costs a model call per chunk —
for a 105 MB transcript, many — and it converts evidence into a derived
artifact. This repository already rejected exactly that substitution on
2026-08-23, on measurement: storing the conversation and searching it beat
storing a distillation by 16–22 points, and
`knowledge/notes/session-evidence-retention-decision.md` records the choice.
A summariser inside the capture hook would reintroduce it one layer earlier.

**Chunked retention** — keep the whole record split across parts — is what this
repository does one stage later, for oversized daily logs
(`knowledge/notes/oversized-daily-compile-decision.md`). It loses nothing, and
it is the option the decision did not take. The reason it did not is stated and
holds: a lifecycle hook cannot hold 105 MB in memory, and the durable record
downstream is capped at 512 KiB anyway. Chunked retention would need a streaming
writer and a multi-part record format, which is a different and larger change.

**Silent truncation** is the field's actual baseline and is worse than all of
the above. OpenTelemetry's attribute value length limits truncate to the limit
"with no error raised" — no marker, no count, nothing in the record saying it
happened. The recommended mitigation is to add a `log.truncated` attribute so
that truncation is at least countable. Measured against that baseline, the
decision's marker naming the dropped byte count is ahead of the OTel default,
not behind it.

## Finding 3 — the 2026-08-25 measurement is cited backwards

`bounded-capture-excerpt-decision.md` says:

> Head **and** tail, not either alone. That is the same choice already recorded
> for nightly consolidation, for the same measured reason: a long session states
> its decisions early and its outcome late, and a tail-only window was shown to
> miss decisions sitting 31,814 characters from the end.

`docs/research/2026-08-25-what-the-vault-decides-to-remember.md` records the
opposite outcome for that number:

> The head+tail literature did not transfer. Forty real sessions through the
> product's own prompt and provider, eighty calls: tail-only promoted 24, head+
> tail promoted 24, two sessions changed tier in opposite directions. One
> regressed because its decisions sat 31 814 characters from the end — inside a
> 60 000 tail, outside a 30 000 half. The change was reverted.

31,814 < 60,000 and 31,814 > 30,000. The 60,000-character **tail-only** window
caught those decisions; the 30,000+30,000 **head+tail** window missed them. The
figure is evidence that a symmetric head+tail split lost a session, and the
decision page offers it as evidence that tail-only did. `knowledge/log.md` for
2026-08-25 records the same result in the same direction.

Half of the sentence is still true: the head+tail shape *is* already recorded
for nightly consolidation — `episode_consolidation._within_share` has done
head+tail since MEM-03 on 2026-08-23, and its docstring gives the reason as "a
session's work is rarely at its start", which is an argument against *head-only*,
not against tail-only. What is false is "for the same measured reason", and the
inverted reading of 31,814.

## Does the decision hold?

The decision holds. The correction is to its stated reason, not to its shape,
and mostly because the two are answering different questions:

- **Excerpting instead of refusing** is not in question at all. Nothing found
  here defends refusing a record for being large, and the vault's own downstream
  cap at `session_evidence.MAX_EVIDENCE_BYTES` means the refusal was protecting
  a record that was going to be truncated regardless.
- **Head+tail for retention** is supported independently by OpenRouter's default
  and by the lost-in-the-middle rationale. The 2026-08-25 measurement was about
  what a *classifier reads* in order to assign a tier — a decision the vault
  then removed from the classifier entirely
  (`knowledge/notes/session-promotion-policy-decision.md`). Retention is a
  different question and the 2026-08-25 result does not settle it either way.
- **The 50/50 ratio is the weak part**, and it is weak in both places. The one
  measured head+tail ratio in the literature is 25/75. The one measurement this
  vault ran on its own sessions is a data point against halves specifically: at
  60,000 characters, halves lost material that a full tail kept. Nothing here
  says 50/50 is wrong for a 900 KiB evidence budget — the failure was at a
  budget 15× smaller — but nothing says it is right, and the decision presents
  it as though it had been measured.

## What this research does not claim

- It does not re-run the 2026-08-25 experiment, and it does not claim the result
  transfers from a 60,000-character classifier budget to a 900 KiB retention
  budget. The two differ by more than a constant.
- It does not claim a 25/75 split would be better here. Sun et al. measured
  document classification with a 510-token budget on IMDb and Sogou; a session
  transcript is neither, and the ratio is described in that paper as empirically
  selected, not derived.
- It does not measure what the shipped excerpt actually keeps on this machine's
  36 oversized transcripts. The decision's one measured excerpt (a 41 MB
  transcript encoding to 944,343 bytes) is a size check, not a content check —
  nobody has checked whether the decisions in those 36 sessions fall inside the
  kept windows.
- It does not evaluate the JSON-escaping headroom risk the decision names. That
  risk is unchanged by the decision and was correctly declared unchanged.

## Sources

- OpenRouter, message transforms: `middle-out` compression, "keeps half of the
  messages from the start and half from the end", default at ≤8k context, and
  the lost-in-the-middle justification.
  https://openrouter.ai/docs/guides/features/message-transforms
- Chaner Sun, Xipeng Qiu, Yige Xu, Xuanjing Huang, *How to Fine-Tune BERT for
  Text Classification?* (arXiv:1905.05583) — head-only / tail-only / head+tail,
  with head+tail "empirically select the first 128 and the last 382 tokens", and
  head+tail winning on IMDb and Sogou.
  https://arxiv.org/pdf/1905.05583
- OpenTelemetry, attribute value length limits: truncation "with no error
  raised", and `log.truncated` as the recommended way to make it observable.
  https://opentelemetry.io/docs/specs/otel/common/ ·
  https://oneuptime.com/blog/post/2026-02-06-attribute-value-length-limits-opentelemetry/view
- Reservoir sampling and what a uniform stream sample gives up in temporal and
  contiguity terms.
  https://samwho.dev/reservoir-sampling
- This repository: `docs/research/2026-08-25-what-the-vault-decides-to-remember.md`
  (the forty-session measurement and the 31,814-character regression),
  `knowledge/log.md` 2026-08-25, `scripts/episode_consolidation.py`
  (`_within_share`), `scripts/integration_adapter.py`
  (`CAPTURE_EXCERPT_SIDE_BYTES`).
