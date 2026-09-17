# The knowledge zone is denied by default, as the contract already says

Dated 2026-09-17. Finding I-A6 and section D of the third audit (medium: a privacy
exposure). The research before the fix.

## What was found

- The operating contract says `knowledge/` is "denied by default" and that `.gitignore`,
  not a directory boundary, keeps private knowledge out of a public repository.
- `.gitignore` denies the known subdirectories one by one. Anything new directly under
  `knowledge/` is allowed. `scripts/build_guardrails.py` writes `knowledge/guardrails.md`
  — rules learned from the owner's sessions — and `git check-ignore knowledge/guardrails.md`
  matches nothing, so `git add -A` would stage it. The same holds for a new subdirectory
  and for a non-Markdown file under `knowledge/daily/` (the rule there is `*.md`).
- `tests/test_structure.py` guards `knowledge/notes` and `knowledge/daily` only.
- Tracked benchmark result files carry the absolute paths of the machine they were measured
  on (about three thousand occurrences of the home directory in `benchmark/**/*.json`), and
  one code comment names another private project of the owner by its path.

## Practice on this date

- gitignore: "An optional prefix `!` which negates the pattern; any matching file excluded
  by a previous pattern will become included again. It is not possible to re-include a file
  if a parent directory of that file is excluded." and "If there is a separator at the
  beginning or middle (or both) of the pattern, then the pattern is relative to the
  directory level of the particular `.gitignore` file itself."
  (<https://git-scm.com/docs/gitignore>, fetched today.)
- So a deny-by-default zone is written as `knowledge/*` — which excludes the entries, not
  the directory `knowledge` itself — followed by one literal `!` line for each thing that is
  published. The subdirectories are re-included as directories and keep their own deny
  rules. This is the shape the file already uses for `knowledge/notes/*`.
- A recorded measurement stays evidence when a path prefix is replaced by a placeholder:
  the numbers, the answers and the relative paths are untouched.

## The decision

- `.gitignore`: `knowledge/*` first, then the three published files, then `!knowledge/*/`
  and `knowledge/*/**`: every directory stays open and everything inside it is denied
  until a literal line names it. The directories are not re-included one by one, because
  `tests/test_structure.py` and the index read any `!knowledge/notes/…` line as a published
  page and rightly refuse one that is not a Markdown file. `knowledge/daily/*.md` becomes
  `knowledge/daily/*`; the project template gains `!knowledge/projects/_template/**`.
  Nothing that is tracked today changes state; the contract text does not change.
- Benchmark result JSON: the checkout path becomes `<vault>`, the agent scratch directory
  `<tmp>`, any other home path `<home>`. Two code comments lose their absolute path.
- One test holds the line: a probe file anywhere new under `knowledge/` is ignored, every
  tracked path under `knowledge/` is on the published list, and no tracked benchmark result
  or script names a home directory.
- Left to the owner, because deleting or rewriting tracked documents is theirs to decide:
  dated documents under `docs/` that name the other project and home paths, the
  `docs/enforcement/` tooling, and test fixtures that use the other project's name as a
  sample value.

Files: `.gitignore`, `benchmark/` result JSON, `scripts/mcp_server.py`,
`scripts/answer_budget.py`, `tests/test_the_knowledge_zone_is_denied_by_default.py`,
`docs/research/2026-09-17-the-knowledge-zone-is-denied-by-default.md`.
