# Small keys, large values, pruned reading, and a loop that stops — 2026-09-08

The owner, twice today: the fixes read as crutches and break rules 2 and 4;
has nobody in the world done better? They have. This note names the three
mechanisms the field settled on for exactly our three problems — finding
an instance the question does not name, reading less than the whole round,
and counting without a fixed window — with the sources, and replaces the
two numbers chosen under pressure ("three whole entries", "three on the
second pass") with them.

## 1. Finding: the key is smaller than the value

- Dense X Retrieval (arXiv:2312.06648, EMNLP 2024): indexing a corpus by
  fine-grained units — propositions — "significantly outperforms
  passage-level units in retrieval"; for a fixed reading budget the finer
  index gives better answers.
- Parent-document / small-to-big retrieval (https://zeroentropy.dev/concepts/parent-document-retrieval/):
  index the small chunk, retrieve by it, hand the model its larger parent.
  The retrieval scores the precise span; the generation sees the context.
- LongMemEval (arXiv:2410.10813, CP2): keys finer than values, +9.4%
  recall@k, +5.4% accuracy; their keys were extracted user facts.

What the user said is the fact; what the assistant replied is the noise
around it, and on captured sessions the reply is 80% of the bytes. So the
key is the **user turn** and the value is the **round** (that turn and the
reply it got). No model call: the turn boundary is a line marker.

**Design.** The corpus splits a rendered conversation at every turn, user
and assistant, each turn a chunk (still bounded at 4 KB, short ones folded
into the previous). Retrieval therefore ranks turns; a user turn is a
300-byte key. Delivery pairs a retrieved turn with its partner — the reply
after a user turn, the question before an assistant turn — which is the
round. Whole entries are no longer a default at all: `WHOLE_ENTRIES_DEFAULT`
becomes 0 and the variable stays for the sweep.

## 2. Reading: extractive pruning of the value

- RECOMP (arXiv:2310.04408): an extractive compressor that selects the
  useful sentences of retrieved documents gives up to 10× compression with
  minimal accuracy loss, and "extractive compression often outperforms all
  other approaches … token pruning methods often lag behind".
- Provence (arXiv:2501.16214, ICLR 2025; XProvence arXiv:2601.18886 for
  multilingual): a sentence-level pruner that "dynamically detects the
  amount of relevant information in the context — from zero to all
  sentences", little-to-no drop across seven domains.
- Contextual compression survey (arXiv:2409.13385).

We add no model: the dual encoder we already run for retrieval
(`multilingual-e5-small`) scores each sentence of an assistant turn against
the question, and the turn is delivered as its top sentences with their
exact byte ranges; a user turn is delivered whole, because it is short and
it is the fact. A pruned span is still a span of the file: the citation
gates hash and overlap exactly as before. Provence's own model is the
upgrade path if the dual encoder proves too weak, and would be a dependency
decision.

## 3. Counting: a loop that stops when nothing new appears

- PAR2-RAG (arXiv:2603.29085): breadth-first anchoring builds a high-recall
  evidence frontier, then depth-first refinement under an evidence
  sufficiency control, iterating.
- Stop-RAG (arXiv:2510.14337): stopping as a decision, not a fixed count of
  steps; S2G-RAG (arXiv:2604.23783): structured sufficiency and gap judging
  between rounds.
- Iterative RAG diagnostic (arXiv:2601.19827): every published loop bounds
  its steps; five is the common ceiling.

**Design.** For a declared count or sum: a step is fan-out → search →
merge → answer under the counting rule. After each step the set of
instances (the claim's `inputs`, after entity clustering) is compared with
the step before; the loop stops when a step adds no instance, when the
searches return nothing new, or at three steps. Width comes from finding
more keys, not from bringing entries in whole; the second pass reads the
cited spans plus what is new, as it already does. `SECOND_PASS_WHOLE_ENTRIES`
is removed.

## What this replaces

| crutch | replaced by |
|---|---|
| three whole entries by default | user-turn keys, round values, no whole entries |
| three whole entries on a second pass | the loop that stops on no new instance |
| reading every byte of a delivered round | sentence pruning of assistant turns by the dual encoder |

## Expected cost

Twelve keys × (300 B key + pruned reply ≈ 600 B) ≈ 11 KB ≈ 3k tokens for
a single-pass question; a count adds one to three steps of the same size.
Against run 1's 13.4k and today's 19k–58k.

## Rule of decision

One run of 200, seed 101, judged on both protocols, against the run-1
baseline and the tasks 1–6 arm: kept if accuracy is within the spread of
the better of the two and prompt tokens per question fall below 8k; the
owner's metric, verified-correct per thousand tokens, is reported first.

## Addendum, first live questions

Single-hop questions came in at 5.9k and 8.3k prompt tokens, right, against
run 1's 13.4k. The two counts fell: instruments 3 of 4, dinner parties 1 of
3. The cause is coverage, not the key: twelve turn candidates reach fewer
entries than twelve 4 KB pieces did, and the retrieval's visible-slot rule
took one slot per *page*, so three sessions of one day competed for a slot.
Two changes, both from the same design: the slot rule keys on the entry
(page and heading), and the candidate count is twenty-four — for a fixed
reading budget, finer units and more of them, which is Dense X Retrieval's
result restated.
