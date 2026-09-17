# The guard-rail slots go to the strongest and newest rules

Dated 2026-09-17. The leftover of finding M-A12 of the third audit: the first round made the
truncation visible, and left "which rules deserve the slots" to be decided. The research
before the fix.

Files: `scripts/build_guardrails.py`,
`tests/test_the_guard_rails_say_how_many_rules_they_left_out.py`.

## What was found

- `build_guardrails` keeps the first 15 rules it collected and then the first 5 of each
  type. The order it collects in is the order of the source manifest — path order, so
  effectively alphabetical by slug, with the promoted feedback records last. A rule the
  operator stated yesterday loses its slot to an inferred page whose slug starts with `a`.
- Every field needed to do better is already in the pages and is already read elsewhere:
  `source_authority` and `confidence` are required of any page that makes a claim
  (`CLAUDE.md` §4 rule 13), and `timestamp` is written into every promoted feedback page by
  `feedback_capture`. `build_guardrails` compiles `TIMESTAMP_RE` and never uses it.
- Promoted feedback records carry `captured_at` and are, by construction, the operator's own
  corrections.

## Practice on this date

- The project's own contract already states the ranking and the reason: "Set `confidence`
  (high|medium|low) and `source_authority` (user|ai-derived|web|inferred) when a page makes
  a claim. Hierarchy: user-stated > web-sourced > ai-derived > inferred. The compile/search
  pipeline uses these fields to rank retrieval results." (`CLAUDE.md` §4 rule 13). The
  guard rails are retrieval into the session-start prompt, and were the one reader ignoring
  it.
- Recency as the tiebreak is what the feature is for: the block exists so that the last
  correction is not repeated (module docstring, "rules the agent has internalized from past
  corrections"). Between two rules of equal authority the newer one describes the habit the
  operator is currently correcting.

## The decision

- Rules are ordered before any ceiling is applied: by authority (user, then web, then
  ai-derived, then inferred; a page with no `source_authority` is treated as inferred, as
  rule 13 says), then by declared time (frontmatter `timestamp` for a page, `captured_at`
  for a feedback record), newest first, then by path so the order is stable on a vault
  where nothing else distinguishes two rules. A promoted feedback record ranks as `user`.
- The ceilings stay at 15 overall and 5 per type: they are the session-start token budget,
  not a judgement, and they already say what they dropped.
- Deduplication still keeps the first of two rules with the same opening; after this change
  "first" means the stronger and newer one rather than the alphabetically earlier one.
- No path, environment variable or contract changes.
