# Corrections are learned by compile, not by candidates

Date: 2026-09-25. Q8 (docs/AUDIT-2026-09-25-full.md, legacy sweep).

## Fact
- `feedback_capture.py` matches a prompt against phrases ("no, ", "actually",
  "I prefer", …) and writes a JSON candidate under `knowledge/feedback/`. A
  candidate reaches the guardrails (`build_guardrails` source 2) only after
  `feedback_capture.py promote <id>`, a manual command. Its own docstring:
  "nothing is auto-promoted".
- On 2026-09-25 the live vault held three candidates, all written by a timing run
  of mine by mistake (removed); in the whole history of the vault none was
  promoted.
- Every prompt is already written verbatim to the daily log by
  `user_prompt_capture.py`, and the nightly compile turns the daily log into
  typed notes with an LLM; guardrails source 1 reads those notes. The rules the
  session start prints today (for example "do not leave a human annotation step")
  are corrections the owner gave, learned this way.
- The owner does no manual steps ("я не буду делать никаких ручных разметок,
  система должна работать автоматически").
- `build_context` globbed every candidate file on each context build.

## Source (fetched 2026-09-25)
Mem0 documentation, "Add memory", https://docs.mem0.ai/core-concepts/memory-operations/add:
"Mem0 sends the messages through an LLM that pulls out key facts, decisions, or
preferences to remember." and "You trigger this pipeline with a single `add`
call: no manual orchestration needed." Current practice is automatic LLM
extraction; a keyword match waiting for a human confirmation is neither.

## Decision (logged with `log_decision`, 2026-09-25)
Retire the candidate path: `feedback_capture.py`, its delegate entries in the
integration adapter, guardrails source 2, the `build_context` feedback counts,
the tracked README and the layout lines in CLAUDE.md, AGENTS.md,
docs/STRUCTURE.md, docs/USER-GUIDE.md, docs/ARCHITECTURE.md and the three READMEs.
Kept on purpose: the `.gitignore` denial (an old candidate is never committed),
the transaction allowlist's `knowledge/feedback/*.json` target (transactions
written today still name it, and their recovery must validate), and the guardrail
source manifest root (a saved manifest listing a feedback file must still
validate). Those files are inert: nothing turns them into rules any more.

## Uncertainty
Nothing measures how many real corrections compile turns into rules; the claim
rests on the rules visible at session start, not on a count.

## Files
- scripts/feedback_capture.py (removed)
- scripts/integration_adapter.py
- scripts/build_guardrails.py
- scripts/build_context.py
- knowledge/feedback/README.md (removed)
- CLAUDE.md, AGENTS.md, docs/STRUCTURE.md, docs/USER-GUIDE.md, docs/ARCHITECTURE.md
- README.md, README.ru.md, README.zh-CN.md, CONTRIBUTING.md
- tests (the feedback tests removed or narrowed)
