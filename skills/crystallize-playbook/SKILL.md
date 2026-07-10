---
type: skill
name: crystallize-playbook
argument-hint: "[optional: file or topic to crystallize from]"
description: Extract a reusable workflow from a recent successful task and save it as a draft playbook under knowledge/notes/workflows/. The crystallization is conservative — it only extracts steps that would apply to future tasks of the same shape, never one-off project specifics.
disable-model-invocation: true
allowed-tools: Read Glob Grep LS Edit Write
title: "SKILL"
timestamp: 2026-07-03T05:41:37
---

Crystallize a reusable workflow (a "playbook") from a recent successful task.

This is the procedural-memory promotion mechanism: successful executions become first-class workflow pages that future sessions can reference, mirroring VEP's "Training Arena" pattern where high-scoring strategies crystallize into playbooks.

## When to invoke

After a task that:
- Took multiple steps to complete.
- Has a shape that will recur (debugging a class of bug, deploying a service, reviewing a PR, fixing a flaky test, etc.).
- Produced an outcome the user confirmed as correct.

Do NOT invoke after:
- One-shot questions or lookups.
- Tasks that are inherently project-specific (one-off migrations, unique incidents).
- Tasks where the user explicitly said the approach was wrong.

## Procedure

1. **Identify the source material**. If `$ARGUMENTS` is a file path, read it. Otherwise, inspect the most recent daily log under `knowledge/daily/` for today's date and find the most recent meaningful session block (one with `Tier: major` or substantial content).

2. **Extract the workflow shape, not the specifics**. Ask:
   - What *class* of problem does this solve? (e.g. "debug intermittent test failure", "add a new API endpoint with authz", "migrate a Django model")
   - What are the 3-7 steps that would apply to ANY instance of this class?
   - What gotchas generalrise from this specific instance?

3. **Check for duplicates**. Read `knowledge/notes/workflows/` (and `knowledge/notes/patterns/`) — if a similar workflow already exists, the result should be an UPDATE to that page, not a new sibling. The DEDUP-BEFORE-CREATE rule from compile_memory.py applies here.

4. **Draft the playbook** under `knowledge/notes/workflows/<descriptive-slug>.md` using this format:

   ```markdown
   ---
   type: workflow
   title: "<action verb phrase>"
   description: "<when to use this playbook>"
   timestamp: <ISO 8601>
   source_authority: ai-derived
   confidence: medium
   ---

   # <Verb phrase>

   One-sentence summary: <when to apply this playbook, in one sentence>.

   ## When to use
   - Trigger conditions.

   ## Steps
   1. <step>
   2. <step>
   ...

   ## Gotchas
   - <thing that went wrong in the source instance, generalized>

   ## Evidence
   - Crystallized from `knowledge/daily/<date>.md` block at `[HH:MM:SS]` (verified to support the steps above).

   ## Related
   - [[knowledge/notes/patterns/<related-pattern>]]
   - [[knowledge/notes/decisions/<related-decision>]]
   ```

5. **Verify evidence**. Before writing, open the cited daily log block and confirm it actually contains the workflow you're describing. Same VERIFY-BEFORE-WRITE rule as compile_memory.py — fabrication is the highest-severity defect.

6. **Update the index**. After creating the playbook, add a line to `knowledge/index.md` under a new or existing `## Workflows` section, with the page stem and the one-sentence summary.

7. **Reciprocal backlinks**. Edit each `## Related` target to add a backlink to the new playbook (unless it already links back).

8. **Do NOT auto-promote to a skill**. `skills/` is for human-curated, stable workflows. Playbooks under `knowledge/notes/workflows/` are agent-authored drafts; they earn promotion to a SKILL.md only after explicit user review.

## Output

End your turn with:

```
CRYSTALLIZE_DONE: <n> page(s) touched: <paths>
CRYSTALLIZE_AUDIT: verified <a> evidence citations; <b> duplicates checked; <c> backlinks added
```

If the source task did not yield a reusable workflow (too specific, too one-off), emit:

```
CRYSTALLIZE_DONE: 0 page(s) touched: (not reusable)
```

Either way, summarize in 1-2 sentences what you decided and why.
