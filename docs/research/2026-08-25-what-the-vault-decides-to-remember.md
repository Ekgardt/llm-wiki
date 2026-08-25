# What decides that a session is remembered (2026-08-25)

One-sentence summary: current practice separates *retaining* an episode from
*promoting* it into durable knowledge, and where a classifier reads a long
transcript from is a measured property of the truncation, not a matter of taste.

## Why this was researched

`OPEN-034` and `NEW-62` sat open because they were framed as one question —
"how good is the classifier" — that could not be answered without human labels.
The measurement that did not need labels was already recorded: on forty real
sessions the product kept **1** at the full 60 000-character budget and **4**
at a third of it. Seeing more made it keep less. A metric cannot be repaired by
better labels when the input to the thing being measured is aimed at the wrong
bytes.

## What the field says (August 2026)

**Retention and promotion are different decisions.** The 2026 surveys describe
memory as tiered: raw episodes are cheap and kept, and a separate consolidation
step decides what is promoted into durable form; without that step stores grow
without bound and retrieval degrades. The same sources are explicit that
*selective forgetting is unsolved* and that teams should write down an explicit
retention policy rather than assume the memory layer self-manages. That is an
argument for making retention unconditional and cheap, and for putting the
judgement into consolidation, where the evidence is available — not into a
hook that fires once at the end of a session.

**Truncation position is measured, not assumed.** The standard comparison in
text classification is head-only, tail-only and head+tail; head+tail is the
usual winner, and tail-only wins on tasks whose outcome sits in the final turns
(fraud/abuse detection on a conversation, for example). Separately, the
"lost in the middle" literature reports weak attention to material away from
the edges. A transcript of engineering work is not an outcome-at-the-end
document: it states the problem at the start, decides in the middle, and ends
in tool output. Tail-only is therefore the worst of the three choices for this
corpus, which is exactly the shape `NEW-62` measured.

## What this does not establish

- None of these results were reproduced here. They are cited for the shape of
  the choice; the number that decides anything in this repository is the
  before/after promotion count on its own forty sessions.
- Head+tail is not claimed optimal. It is claimed better than tail-only for
  this document type, and it matches what `episode_consolidation.py` already
  does with the same records, so the vault stops carrying two conventions.
- The parametric-consolidation line of work (writing tendencies into weights)
  is not applicable here and was not pursued: this product's authority is
  Markdown in git, and a parametric store cannot be reviewed, diffed, or
  superseded.

## Sources

- [Memory Depth, Not Memory Access: Selective Parametric Consolidation for Long-Running Language Agents](https://arxiv.org/abs/2606.26806)
- [State of AI agent memory 2026](https://mem0.ai/blog/state-of-ai-agent-memory-2026)
- [Long-term memory architectures for AI agents](https://redis.io/blog/long-term-memory-architectures-ai-agents/)
- [Investigating Text Shortening Strategy in BERT: Truncation vs Summarization](https://arxiv.org/html/2403.12799v1)
- [Never Lost in the Middle: Mastering Long-Context Question Answering](https://arxiv.org/html/2311.09198v2)
