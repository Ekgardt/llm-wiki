# The README says what the project is today

Date: 2026-09-26. The owner: the GitHub description is hopelessly out of date;
rewrite it fully to match the project's current state.

## What was wrong (checked against the code at cd956c0a)

- It promised features that no longer exist: a loop detector and an agent timeline
  (removed 2026-09-17, `docs/research/2026-09-17-three-modules-nothing-runs.md`) and
  feedback capture (removed 2026-09-25, commit b8a6ca70).
- "Claude Code: five hooks" — `integrations/claude-code/settings.json` has seven
  lifecycle events plus two code-graph hint hooks.
- The title and filename boost as described changed on 2026-09-26 (audit C-10);
  "direct page reads at small scale" is gone (DIRECT only without a generation,
  `scripts/lookup_mode.py`); "triple fusion" is the GLOBAL mode only.
- "Node 22 only for Pyright" — typescript-language-server also runs on Node.
- The comparison table made claims about other projects this repository cannot
  verify, and marked removed features "Yes".
- Missing: session records, the nightly self-update and snapshot, the encrypted
  Restic backup, project scoping, the language servers' status, `unsupported`.
- The GitHub About text named Cursor, and the topics named cursor and antigravity,
  both retired on 2026-08-26.

Checked and deliberately not claimed: session records are kept but not indexed
(`scripts/corpus_snapshot.py`, `_walk_knowledge`: measured to take the corpus over),
so the README says they are kept, not searched.

## Decision

One structure in all three languages: what it is and a day of use, quick start,
agents and the MCP interface, what you get, where things live, keeping memory safe,
code navigation, search index, benchmark, contributing. Operator detail stays in
`docs/USER-GUIDE.md`; the README links to it. The comparison table is removed, and
the README says no comparison is claimed. Every sentence the README tests pin is
kept.

## Sources

- GitHub Docs, "About READMEs", fetched 2026-09-26,
  https://docs.github.com/en/repositories/managing-your-repositorys-settings-and-features/customizing-your-repository/about-readmes —
  a README typically covers "What the project does", "Why the project is useful",
  "How users can get started with the project", "Where users can get help with your
  project", "Who maintains and contributes to the project".
- Make a README, fetched 2026-09-26, https://www.makeareadme.com/ — "If you think
  your README is too long, consider utilizing another form of documentation rather
  than cutting out information."
- Diátaxis, fetched 2026-09-26, https://diataxis.fr/ — "Diátaxis identifies four
  distinct needs, and four corresponding forms of documentation - tutorials,
  how-to guides, technical reference and explanation."

Conclusion (mine): the README answers what, why and how to start, and hands
reference and how-to detail to the guides that already hold it, instead of carrying
all of it and going stale in four places.

## Files

- `README.md`, `README.ru.md`, `README.zh-CN.md`
