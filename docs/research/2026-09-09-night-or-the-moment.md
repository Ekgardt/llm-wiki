# Night, idle time, or the moment: when a memory should do its expensive work — 2026-09-09

The owner's questions: is a nightly pass effective for the system as a
whole; does ours use caching to be cheaper; would it be better to do the
work at once. Sources first, then our own numbers, then the decision.

## 1. What the science says about offline consolidation

- **Complementary learning systems** (McClelland, McNaughton, O'Reilly
  1995; PNAS 2022 model of hippocampus–neocortex interaction during sleep,
  https://www.pnas.org/doi/10.1073/pnas.2123432119): a fast store keeps
  episodes as they happen; a slow store integrates them offline, by
  interleaved replay, because integrating fast causes catastrophic
  interference. The two-speed design is not a convenience; it is what
  makes a memory both immediate and stable. Our raw daily entries are the
  fast store; compiled pages and keys are the slow one.
- **Sleep-time compute** (Letta, arXiv:2504.13171): thinking about a context
  before the question arrives cuts test-time compute about **5×** at equal
  accuracy, and scaling it raises accuracy **+13% and +18%** on the two
  stateful benchmarks; amortised over ten questions on one context the
  cost per question falls **2.5×**. The gain "widens as the questions
  become more predictable from the context" — and a personal memory's
  questions are predictable: what did I decide, what do I own, when.
- **Retain or consolidate** (arXiv:2607.17545): consolidation wins only
  when the reading budget is tight (32–64 tokens: 52% vs 4%); at a loose
  budget raw retention beats every consolidation operator by 8–11 points.
  Consolidate to *find*, keep raw to *read* — the same conclusion as the
  verbatim-beats-extracted study (arXiv:2601.00821).
- **Cost of write versus read** (arXiv:2603.04814): a fact store costs about
  $0.04 to write a 100k-token context once and $0.0013 per read; it is
  cheaper than re-reading the context after about **ten** questions. The
  same study measures a 33–35 point accuracy loss when facts replace the
  raw context for reasoning, which is why the write must add keys, not
  replace text.
- **Batch versus online construction** (segment trees, arXiv:2606.04555):
  batch LLM construction gives the best structure; online cosine
  construction uses about 10× fewer tokens and 5× less latency. Quality
  when there is time, cheapness when there is not.
- **Latency budgets** (https://supermemory.ai/blog/latency-budgets-memory-retrieval):
  retrieval has about 200 ms in a chat; extraction and consolidation take
  20–40 s. Consolidation can never sit on the request path; the only
  question is *which* off-path moment.

## 2. What the competitors do

Mem0, Zep, Supermemory and OMEGA extract at ingest — the moment of the
message — but asynchronously, on a queue, off the request path; every
message pays a model call. Mastra's observer runs on a threshold during
the conversation. Letta runs sleep-time agents in idle time. None runs a
fixed nightly job; all of them do the work off-path, most of them as soon
as the message lands.

## 3. Our own numbers

- Capture is free: zero model calls at the moment (our axiom). The nightly
  pass of 2026-09-08 took six minutes: adoption, reclaim, queue, an
  episode step of 8 items in one batch (47 s), then compile.
- **The compile failed** — `claim tree page exceeds 4194304 bytes`
  (`claim_tree_manifest.MAX_CLAIM_TREE_FILE_BYTES`) — and because compile
  had not finished in five minutes the pass skipped lint, index and graph.
  It has failed since 2026-09-07. The daily files of this week are 110 KB
  to 870 KB a day, almost all of it this session's own captures, and the
  claim tree page they feed has outgrown its cap. So the honest answer to
  "how effective is our nightly" today is: the expensive half of it has
  produced nothing for two nights, and the health line at session start
  has been saying so.
- **Caching:** the provider is the Claude CLI on the subscription. Prompt
  caching is applied by the CLI to its own stable prefix; our per-call
  content differs, so cache reads cover the shared prefix only, and the
  telemetry fields for cache reads exist but the CLI path reports none.
  The Batch API's 50% and explicit `cache_control` 90% discounts
  (https://pecollective.com/tools/claude-pricing-guide/) are API features
  and do not apply to a subscription. Night is not cheaper for us per
  token. What batching does buy is fewer calls: the fact-key step sends
  twenty-five turns in one call, so the instruction is paid once per
  twenty-five turns instead of once per turn — the same saving a prompt
  cache gives, obtained structurally.

## 4. The arithmetic

Let c be the cost of extracting from one session at the moment, q the
number of questions that will ever touch that session, and r the extra
cost a question pays when the extraction was not done. At-the-moment
extraction costs c per session regardless of q; deferred extraction costs
c only for sessions that are still there to be keyed when the batch runs,
and a question asked before that pays r. With q averaging well under one
for most sessions in a personal vault — most captured sessions are never
asked about — eager extraction is paid mostly for nothing, which is
Letta's amortisation argument read backwards. The deferral is right as
long as r is small, and r is small here: a question before the batch still
finds the raw entry, it only lacks the key.

## 5. Decision

1. **Keep the two speeds.** No model call at capture; expensive work
   off-path. This is CLS, sleep-time compute and the cost paper agreeing.
2. **Idle time, not a fixed hour.** The consolidation and key steps should
   run when the machine is idle and enough new turns have accumulated
   (a debounce on new entries), with the nightly job as the floor, not the
   only trigger. That closes the same-day gap the fixed hour leaves.
3. **Batch the calls, and keep the prompt stable.** Twenty-five turns a
   call; the instruction first, so any cache the provider keeps hits it.
   If the provider is ever an API, the Batch API and `cache_control` stack
   and the batch shape is exactly what they reward.
4. **Fix the compile first.** A nightly that fails is worse than none
   because it looks like maintenance. The 4 MB claim-tree page cap needs
   either a page split or a bound on what a day of captures feeds it; that
   is the next task, before any run.
5. **Measure the compiled layer on the stand** (plan part 2, lever 8). The
   only number that says whether compile earns its keep is an arm that
   uses it, and there is none yet.
