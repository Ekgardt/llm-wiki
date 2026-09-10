# An allowlist that reads as read-only must be read-only

Date: 2026-09-10. Trigger: audit finding OPS-12. `integrations/claude-code/settings.json`
ships an `allow` list that looks like a read-only toolkit and is not one:
`Bash(sed *)` matches `sed -i` (rewrites a file), `Bash(xargs *)` matches
`xargs -n1 rm`, `Bash(sort *)` matches `sort -o out in`, and
`Bash(uv run --directory *)` runs any Python file in any directory — all
without a permission prompt.

## Sources (Claude Code documentation, read 2026-09-10)

1. https://code.claude.com/docs/en/permissions, "Read-only commands": a
   built-in set runs without a prompt in every mode — `ls`, `cat`, `echo`,
   `pwd`, `head`, `tail`, `grep`, `find`, `wc`, `which`, `diff`, `stat`,
   `du`, … — so allow entries for them are redundant. Commands "with
   write-capable or exec-capable flags, such as `find`, `sort`, `sed`, and
   `git`, prompt when an unquoted glob is present".
2. Same page, "Process wrappers": "`find` with `-exec` or `-delete`: a
   `Bash(find *)` rule doesn't cover these forms." Bare `xargs` is stripped,
   but "`xargs -n1 grep pattern` is matched as an `xargs` command" — so
   `Bash(xargs *)` approves `xargs -n1 <anything>`.
3. Same page, "Wildcard patterns": "The `*` stands in for whatever text is
   in its place" — a mid-pattern wildcard is supported (`Bash(git * main)`),
   and an allow rule matches what it says: `Bash(rm *)` matches `rm -rf`.
   An allow rule is not filtered by the read-only analysis.
4. Same page: "Bash permission patterns that try to constrain command
   arguments are fragile" (variables, quoting) — so the narrow forms below
   are a smaller grant, not a guarantee.
5. `scripts/integration_hook_config.py` and `scripts/merge_claude_settings.py`:
   permissions are unioned into the user's list at install and "never taken
   back", because our copy of an entry cannot be told from theirs.

## Findings

- The audit's `find -delete` case is covered by the product (source 2);
  the `sed`, `xargs`, `sort` and `uv run --directory *` cases are real.
- The hooks do not need allow entries: hook commands run outside the
  permission check. `Bash(uv run --directory *)` served no shipped command
  of ours except the two search entries, which have their own rules.
- Installed vaults keep the old broad entries forever under the "never
  taken back" rule. The four strings are ours verbatim; retiring exactly
  those four at the next merge removes a grant we wrote, and a user who
  typed the same string by hand gets the prompt they would have had before
  our install. Everything else in the user's list stays untouched.

## Decision

1. `allow` keeps `git status`, `git diff *`, the four search/lookup
   entries and `sed -n *` (the read-only form); it drops `find`, `ls`,
   `cat`, `grep`, `wc`, `head`, `basename`, `echo` (built-in read-only),
   and `sort`, `xargs`, `uv run --directory *` (write or execute).
2. Both merge paths retire exactly the four over-broad strings we shipped
   (`RETIRED_ALLOW` in `merge_claude_settings.py`).
3. A test holds the shipped list to that set.

Files: `integrations/claude-code/settings.json`, `scripts/merge_claude_settings.py`,
`scripts/integration_hook_config.py`, `tests/test_merge_claude_settings.py`,
`tests/test_integration_injection.py`, `CHANGELOG.md`,
`docs/AUDIT-2026-09-10-operations-and-reliability.md`.
