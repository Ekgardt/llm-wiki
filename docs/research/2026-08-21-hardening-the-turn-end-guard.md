# Hardening the guard — 2026-08-21

A second, deeper pass after the first note was judged too thin. The first pass
established *where* control can sit. This one reads the guard's own source and
current practice together, and names defects with line numbers instead of
recommendations with adjectives.

## What was asked

What do current best practices say a runtime guard for an autonomous coding
agent must do, and which of those does this machine's guard fail to do?

## What the sources say

**There is a theory of what a runtime monitor can enforce, and it is not
folklore.** Schneider's truncation automata terminate the run on violation and
can enforce *safety* properties only — "nothing bad happens". Ligatti and
Bauer's edit automata add suppression and *insertion* of actions, and thereby
enforce a strictly larger class, including infinite renewal properties, which
subsume the computable safety properties. This matters concretely: "the graph
must be consulted before code changes" and "something that could fail must run
before the work is declared done" are not safety properties. They are
obligations that must *eventually* be discharged. A refuse-only gate cannot
enforce an obligation; it can only refuse the step that would leave it
undischarged, and it must keep refusing until the obligation is met. A gate
that refuses once and then yields enforces nothing at all.

**Reasoning-level compliance is the failure mode, not the fix.** "Reason Less,
Verify More" measures agents that violate a policy while appearing to comply,
because the model rationalises the violation during reasoning and nothing
errors. Deterministic gates recover exactly those violations. So the guard must
not be improved by asking the agent to self-report; it must be improved by
making the artifact check stricter.

**Rules are trigger, predicate, enforcement.** AgentSpec (ICSE 2026) evaluates a
proposed action before execution: a trigger fires on an event, a predicate
decides whether the rule applies here, and an enforcement acts. Its enforcement
vocabulary is wider than refusal: terminate, user inspection, corrective
invocation, self-examination. The overhead is milliseconds. This machine's
policy already has the trigger/predicate shape; it has only one enforcement.

**A control with an auto-approve path is not a control.** The 2026
human-in-the-loop literature is blunt that approval fatigue, auto-approve
habits and "YOLO mode" bypasses are what make oversight fail, and that the fix
is more precise triggers rather than more approvals — escalate on risk signals,
not on action categories, or the queue turns into a rubber stamp.

**Blocked actions are the evidence that matters.** Guidance on tamper-evident
logging for agent actions puts it directly: that the agent tried something and
was stopped is the record proving the control worked, and the audited component
should not have unrestricted control over the evidence describing it. This
guard already records the enforcement digest with every decision, which is the
right instinct; what it does not record is the case where it gave up.

**Least agency.** The OWASP Top 10 for Agentic Applications 2026 (ASI01-ASI10,
published 2025-12-09) names planning, tool use, memory and human-agent trust
among its categories, and states the principle of least agency: the minimum
autonomy and tool scope needed, and human approval for critical actions
regardless of agent confidence.

## What the guard actually does

Read from source on this machine, not inferred.

**`gate_stop.py:236` yields on the first retry.** The handler begins
`if payload.get("stop_hook_active"): _record_turn_end(session); return 0`.
`stop_hook_active` is set when the turn is continuing *because* the stop hook
blocked. So the gate refuses once and then permits unconditionally. The status
line reports the limit as "overridden after 8 consecutive blocks", which
describes the runtime's own ceiling and not this code: the code yields at one.

**The turn window is reset even when refusing.** `main` calls
`_record_turn_end` on both paths, and `_turn_start` reads that marker as the
beginning of the next window. So after a refusal the audit records of the turn
fall outside the window and `evaluate` finds no code change to complain about.
The yield is therefore doubly built in: once by the early return, once by the
window reset. Either alone would be enough to make the obligation
unenforceable.

**`checkers.py::_is_new_module` requires `tool_name == "Write"`.** Rules 1 and 2
were widened to `Bash` earlier today, but this trigger was not: a module created
by a shell redirect or a heredoc is not a new module as far as rule 2 is
concerned. The widening left its own hole.

**`checkers.py::_research_today` accepts any file.** It returns true when any
file under the research directory has today's date in its name or its first
2000 characters. A file about an unrelated subject, or an empty one with a
dated heading, satisfies rule 2 for every architectural change made that day.

## What this means for the change

Four changes, all deterministic, none asking the model anything.

1. **Make the turn-end obligation an obligation.** Stop resetting the window on
   refusal, and stop returning early on `stop_hook_active`. The refusal then
   repeats until something that could have failed actually runs — the renewal
   property enforced the only way a monitor can enforce one.
2. **Bound it, and record the yield.** An unbounded repeat would deadlock a
   session whose obligation cannot be discharged, and the runtime would cut it
   off anyway. After a small number of consecutive refusals the gate permits,
   but writes a distinct `yield` decision naming the undischarged obligation,
   and says so on stderr. The bypass becomes evidence instead of silence, which
   is what the logging guidance asks for. A marker that cannot be written also
   yields, so a broken log cannot wedge the session.
3. **Close the new-module hole** so the trigger follows the tool coverage it was
   widened to.
4. **Make rule 2's predicate about research.** Require the dated artifact to
   carry at least one source URL and to be more than a stub. Deliberately not
   subject-matching: demanding that the note name the exact file being changed
   would refuse honest research and is how a gate gets switched off.

Not adopted: an operator-approval enforcement for ordinary refusals. The
fatigue literature is clear that adding approvals to categories rather than to
risk signals produces a rubber stamp, and this guard's refusals are frequent by
design. The escalation added here is the recorded yield, which costs the
operator nothing until they look.

Not adopted: a hash chain over the audit log. `common.py` already argues the
case against it — an unkeyed chain in a store its own writer can rewrite
protects against nothing — and the per-record enforcement digest gives the
property that matters, which is that a weakened rule is visible at the decision
where it was first used.

## Sources

- http://users.ece.cmu.edu/~lbauer/papers/2005/ijis2005-editauto.pdf — Ligatti, Bauer, Walker, *Edit automata: enforcement mechanisms for run-time security policies*
- https://arxiv.org/pdf/1804.08917 — Developing theoretical foundations for runtime enforcement
- https://arxiv.org/pdf/2607.07405 — Reason Less, Verify More: deterministic gates recover a silent policy-violation failure mode
- https://arxiv.org/abs/2503.18666 — AgentSpec: customizable runtime enforcement for safe and reliable LLM agents (ICSE 2026)
- https://genai.owasp.org/resource/owasp-top-10-for-agentic-applications-for-2026/ — OWASP Top 10 for Agentic Applications 2026
- https://workos.com/blog/approval-fatigue-agent-governance — Approval fatigue as an agent-governance attack surface
- https://www.c-sharpcorner.com/article/building-tamper-evident-audit-logs-for-ai-agent-actions/ — Building tamper-evident audit logs for AI agent actions
- https://arxiv.org/html/2601.09923v1 — CaMeLs can use computers too: system-level security for computer-use agents
