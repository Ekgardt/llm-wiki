---
type: decision
status: active
confidence: high
source_authority: user
date: 2026-08-23
---

# Sessions Are Kept Verbatim; Only Promotion Stays a Judgement

One-sentence summary: every session leaves a redacted, searchable copy of itself
in the vault, and the classifier stops deciding whether a session is worth
keeping — it decides only whether the session deserves a curated page.

## The problem, measured

On 40 real sessions from this machine the classifier answered "nothing worth
keeping" 39 times. It never kept anything it should not have — no false
promotions in either run — so the bar is not misaimed, it is far too high for a
system whose failure mode is silence. Given a third of the excerpt it promoted 4
of 40 rather than 1, which says the reading window is deciding as much as the
judgement: the classifier reads the tail of a transcript, and a long session's
tail is tool traffic while the decisions sit earlier.

The user-visible consequence is simple. Ask in a month "why did we choose the
systemd timer over cron?" and the vault has nothing, because the session that
decided it was classified as chatter.

## The decision

**1. The session is kept.** Every captured session writes a redacted copy of
itself to `knowledge/raw/sessions/<date>/<session-id>.md`: the human and
assistant turns verbatim, each tool call as a single line naming the tool and its
target. It is written before any classification and regardless of the tier, so
retention never depends on a judgement made before the question exists. That
directory is private by default — `knowledge/raw/**` is denied in `.gitignore`.

**2. The classifier keeps its job, narrowed.** It decides promotion — whether
this session deserves a compiled page — and no longer decides retention. Its
no-false-promotion behaviour is exactly the property promotion wants.

**3. Session evidence is a distinct source class.** It enters the corpus with its
own kind (`session`), lower trust weight than curated notes, so a compiled
decision page still outranks the conversation it came from. Retrieval already
weighs typed provenance from one table and already diversifies by page in fusion.

**4. It ages out; pages do not.** Session evidence lives in the existing 90-day
hot window and is then archived by the existing immutable path. Compiled pages
are unaffected. This is the answer to store growth: current guidance measures
top-5 recall falling from 94% to 71% as a store grows from 100 to 10,000 items,
and retention by class is the recommended mitigation.

**5. Backfill is a separate, explicit command.** This machine holds 227 existing
transcripts (~130 MB of JSONL). Importing them makes the memory useful
immediately, but it is an operator action with a visible cost, not something a
nightly pass does by itself.

## Why this shape

A 2026 controlled ablation swapped only the stored representation inside one
pipeline: verbatim conversation chunks beat LLM-extracted artifacts by 15.9
points on LoCoMo and 22.0 on LongMemEval-S, and the extracted-artifact pipeline
never beat naive RAG. The mechanism they name is *lossy distillation* —
"extraction commits to relevance at write-time before questions exist, while
verbatim storage defers relevance decisions to query-time." A union store, both
representations indexed together, matched chunks alone; substituting artifacts
for text is what costs.

This vault's write path is that commitment in its strongest form: not distilling
the session but discarding it. Everything else in the design — compiled pages,
provenance weighting, archives — already matches what the literature recommends
keeping alongside the source.

Research: `docs/research/2026-08-23-what-a-session-should-leave-behind.md`.

## What this is *not* claimed to be

It is not "the best memory architecture available". Nobody can support that claim
on this date: an audit of the published leaderboard found corrupted ground truth
(6.4% of questions), a judge that accepted 62.8% of wrong-but-topical answers, a
test corpus that fits inside one context window, and reproductions that fell from
92.32% to 38.38% and from 84% to 58.44%. Independent multi-dataset work
(MemoryBench) reports that the well-known memory systems cannot consistently beat
a naive RAG baseline over the same material.

What this proposal is: that baseline, which the elaborate systems fail to beat
consistently, plus the curated layer this vault already has. Anything more
elaborate — a temporal knowledge graph of the sessions, hierarchical
consolidation, self-editing memory blocks — is adopted only if it beats this by a
clear margin **on this vault's own measurement**, which is the gate the same
critics recommend and which this repository already has a stand for.

## What this is not

- Not a change to what gets compiled into notes; the compile pipeline is
  untouched.
- Not a second store: session evidence is Markdown in the vault, indexed by the
  existing generation, with no new database, daemon, or MCP tool.
- Not unbounded: per-session and per-corpus size caps hold, redaction runs before
  the write, and the 90-day window bounds the hot set.

## What comes after, and in what order

A second research pass over the whole landscape (survey of agent memory
mechanisms, MemoryBench, the benchmark audit, sleep-time compute) put two further
upgrades behind this one, both cheap here because the machinery already exists:

1. **Consolidation in the idle window** over the new raw layer. The survey's
   first-listed open frontier is principled consolidation, and it describes
   exactly this shape: raw episodes in a hot probation buffer, promoted to durable
   storage only after validation. Letta reports 18% higher accuracy and 2.5x lower
   cost per query from moving that work off the query path. This vault already
   runs nightly and weekly passes with reflection, tiering and archiving in them.
2. **Post-mortem lessons into the procedural layer.** The survey's context table
   is explicit that coding agents need procedural memory — verified patterns — and
   the largest documented gain there is reflective memory (91% vs 80% pass@1 on
   HumanEval). Skills, playbook crystallisation and a reflection pass already
   exist to attach it to; what is missing is the post-mortem itself.

Neither is started before the raw layer exists, because both consolidate material
that is currently thrown away.

Research: `docs/research/2026-08-23-memory-architectures-second-pass.md`.

## Open questions

- Whether tool output should be kept beyond one line per call. Kept out for now:
  the studied setting is conversation, and tool traffic is what drowns the signal
  in the first place.
- Whether the promotion bar should also be lowered once retention no longer
  depends on it. Measure first: the stand from `OPEN-034` scores promotion, and
  retrieval benchmarks score whether the answer can now be found at all.

## Related

- [[knowledge/notes/nightly-builds-generation-vectors-decision]] — the retrieval
  path this evidence enters.
- [[knowledge/notes/one-trust-weight-across-retrieval-paths-decision]] — why a
  curated page still outranks the session it came from.
- [[knowledge/notes/observable-capture-and-bounded-maintenance-decision]] — the
  capture path this writes from.
