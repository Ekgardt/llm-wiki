# What a project checkpoint should record, and who states it

Dated 2026-08-29. Written because this vault's project journal held 1,000
events and every one of them was empty.

## What is true here today, measured

- `knowledge/projects/llm-wiki/journal.md` held 1,000 events. Every one carries
  `{"id": "checkpoint-none", "action": "close", "value": ""}` on all three
  scalar fields and empty lists on the other six. Events carrying a single list
  operation: **zero**.
- `integration_adapter._checkpoint_delta` reads `payload["project_delta"]` and
  falls back to `_empty_delta()`. Across `scripts/`, `integrations/`, `skills/`
  and `rules/` that key is only ever read. It is written only in two tests.
- So `state.md` reports `Goal: None, Phase: None`, and always has.
- The 4,672 events waiting in the queue break down as `post_tool_use` 3,838,
  `file_changed` 587, `significant_failure` 86, `stop` 79, `user_prompt` 69,
  and a handful of lifecycle events. The only observation flags ever set true
  are `dirty`, `changed` and `significant` — 587 each, all on `file_changed`.
- `decision`, `correction`, `blocker_opened`, `blocker_closed`,
  `task_completed`, `task_cancelled`, `public_contract_changed` and
  `test_result_changed` are accepted by the payload allowlist and **never set
  by anything**.
- A `post_tool_use` observation carries `{"type", "event_id"}` and nothing
  else. Not the tool. Not the target. 3,838 records of "a tool was used".

The subsystem is not broken in its mechanism — leases, fences, transactions,
journal and projection all work. It is recording that something happened and
never what.

## What the field records

The converged template across 2026 practice is small and consistent: goal,
status, progress, blocker, next step, one file per session, the same shape
every time so twenty sessions can be scanned in a minute. That is
field-for-field what `project-checkpoint-v1.json` already declares — `goal`,
`phase`, `current_task`, `next_actions`, `decisions`, `blockers`,
`changed_files`, `commands`, `verification`. **The schema needs nothing.** Only
a producer is missing.

There is a measured benefit to carrying it: an organizational context layer
with an explicit ontology improved final-answer accuracy by **+20%** and cut
average tool calls by **~39%**.

## Who states it — the part the science actually settles

This is where the evidence is sharper than intuition, and it points away from
the obvious answer.

**The agent's own narration carries little signal.** The cold-start ablation in
*The Cold-Start Safety Gap in LLM Agents* separates the contribution of the
agentic tasks in history from the agent's own response content, and finds the
tasks are the primary driver while **the response content has little effect**.
An agent's summary of what it was doing is the part that does not help.

**Self-report is also a poisoning vector.** The 2026 work on agent memory
reliability treats stored belief as something whose quality depends on source
trust, write-path legitimacy and conflict status, not on recall — and names
cross-turn memory corruption as a distinct agent hallucination mode. Filtering
agent-generated inference apart from stated fact is described as part of
reliability, not of debugging.

**The dominant failure is staleness, not absence.** *Momento* reports that
agents fail primarily by misestimating state — treating prior session history
as a reliable proxy for current context rather than as stale information
requiring re-validation. A checkpoint that says "the goal is X" without saying
when that was established, and on what evidence, produces exactly this failure.

Taken together the answer is not "the agent states it" and not "derive it all",
but: **derive from what was done, admit what was said, keep the two apart, and
date every item.**

## The proposal

1. **Derive the delta from observations the capture path already carries.** A
   `file_changed` event names changed files; `significant_failure` opens a
   blocker; a passing verification closes one; `task_completed` closes the
   current task. These flags exist in the allowlist and are simply never set —
   the wiring is missing, not the design.

2. **Carry the tool and its target in a `post_tool_use` observation.** This is
   the largest source by far, 3,838 of 4,672, and it is recorded as content-free.
   The vault already writes exactly this elsewhere: the session-evidence
   decision requires each tool call as one line naming the tool and its target
   in `knowledge/raw/sessions/`. The checkpoint path should see the same thing.

3. **Let an agent state goal and phase explicitly, and mark it as stated.**
   Those two are genuinely not derivable from actions — they are intent. This
   vault already has the vocabulary for the distinction: `source_authority`
   with `user > web > ai-derived > inferred`. The project journal is the one
   place that does not use it.

4. **Date every item.** Each surviving projection item should carry when it was
   last established, so a resuming agent can see that a blocker is four days
   old rather than assume it is current. This is the direct answer to Momento's
   finding and costs one field.

## The cost of being wrong, both ways

**Recording too little** is what happens now: a thousand records of nothing, a
handoff that says `None`, and the cost is invisible because an empty state file
looks exactly like a project with nothing to say. Every session pays the
cold-start again — the +20%/−39% left on the table, every time.

**Recording too much, or trusting self-report** is worse in kind rather than in
degree. A confidently stated goal that is three days stale steers a fresh
session wrong, and it steers it *with confidence*, because the state file is
the first thing read. That is the failure Momento measures, and unlike the
first it does not look like an absence — it looks like an answer.

The asymmetry decides the default: derive first, mark what was merely stated,
date everything, and prefer an item's absence to an item that cannot say when
it was true.

## What this note does not settle

Whether the derived items are good enough to be worth reading is an empirical
question, and this vault can answer it: replay the 4,672 queued observations
through a derivation and read the resulting `state.md`. That measurement is not
done here.

## Sources

- [The Cold-Start Safety Gap in LLM Agents (arXiv 2606.07867)](https://arxiv.org/html/2606.07867)
- [Momento: Evaluating Persistent Memory and Reasoning with Multi-Session Agentic Conversations (arXiv 2606.00832)](https://arxiv.org/abs/2606.00832)
- [When Does Belief-Based Agent Memory Help? Reliability-Conditional Updating and Provenance-Capped Poisoning Defense (arXiv 2606.22030)](https://arxiv.org/html/2606.22030)
- [From Agent Traces to Trust: A Survey of Evidence Tracing and Execution Provenance in LLM Agents (arXiv 2606.04990)](https://arxiv.org/pdf/2606.04990)
- [Memory for Autonomous LLM Agents: Mechanisms, Evaluation, and Emerging Frontiers (arXiv 2603.07670)](https://arxiv.org/html/2603.07670v1)
- [Always-On Agents: A Survey of Persistent Memory, State, and Governance in LLM Agents (arXiv 2606.30306)](https://arxiv.org/pdf/2606.30306)
- [Evaluating LLM Agent Handoffs 2026 — Future AGI](https://futureagi.com/blog/evaluating-llm-agent-handoffs-2026/)
- [State of AI Agent Memory 2026 — mem0](https://mem0.ai/blog/state-of-ai-agent-memory-2026)
- [ICLR 2026 Workshop on Memory for LLM-Based Agentic Systems](https://iclr.cc/virtual/2026/workshop/10000792)
