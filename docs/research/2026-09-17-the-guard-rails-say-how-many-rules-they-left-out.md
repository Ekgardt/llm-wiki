# The guard rails say how many rules they left out, and their file stays private

Dated 2026-09-17. Finding M-A12 of the third audit (low/medium, confirmed by reading, by
`git check-ignore`, and by a test). The research before the fix.

Files: `scripts/build_guardrails.py`, `.gitignore`,
`tests/test_the_guard_rails_say_how_many_rules_they_left_out.py`.

## What was found

- `build_guardrails` keeps the first 15 rules and then `_type_block` prints the first five
  of each type under a header that carries the whole count: `**PATTERN** (12):` followed
  by five lines. The reader — an agent at session start — is told there are twelve rules
  and shown five, with no sign that seven are missing, nor that anything past the
  fifteenth was dropped before grouping.
- `_RULE_LABELS["pattern_rule"]` is never produced by any collector (dead key).
- `build_guardrails.py --apply` writes `knowledge/guardrails.md`: one-sentence summaries of
  private pages. `.gitignore` does not deny that path (`git check-ignore
  knowledge/guardrails.md` prints nothing), so the file shows up as untracked and one
  `git add knowledge/` away from a public repository. No automatic caller uses `--apply`.

## Practice on this date

- "A gitignore file specifies intentionally untracked files that Git should ignore. Files
  already tracked by Git are not affected" ([gitignore, Git documentation](https://git-scm.com/docs/gitignore)).
  The file is not tracked, so a deny line is enough and affects nothing that ships. The
  repository's own rule is that what keeps private knowledge out is `.gitignore`, not a
  directory boundary (`CLAUDE.md` §2).
- A truncated list that reports the untruncated count is the same shape as the partial
  contradiction audit: what was left out is said, not implied absent.

## The decision

- A type block whose rules were trimmed says `(5 of 12 shown)`; an untrimmed one keeps
  `(N)`. When rules fell past the overall ceiling, one last line says how many.
  The ceilings themselves (15 overall, 5 per type) are the session-start token budget and
  stay as they are; which rules deserve the slots (recency, priority) is the owner's
  choice and is named in the report.
- The dead `pattern_rule` label is removed.
- `.gitignore` denies `knowledge/guardrails.md`. No path, environment variable or contract
  changes.
