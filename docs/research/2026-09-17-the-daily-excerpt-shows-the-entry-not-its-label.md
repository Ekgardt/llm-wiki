# The daily excerpt shows the entry, not its label

Dated 2026-09-17. Finding C-F13 of the third audit (low, reproduced). The research before the
fix.

## What was found

- Session start shows six lines of the latest daily entry. The filter that drops bookkeeping
  lines (`NOISE_PATTERNS` in `session_start_context.py`) was written for the old entry format:
  it knows `- Trigger:`, `- Transcript:` and `- Project root:`.
- Entries are now written by the capture worker, whose header block is `- Trigger:`,
  `- Agent:`, `- Capture intent: <64 hex characters>` and `- Tier:`. Three of those pass as
  content. Reproduced with the audit's script: the excerpt is the header, three label lines and
  two lines of substance.
- The session id is cut from a `session-end` header but not from a `pre-compact` one, which
  the same worker writes in the same shape.
- `clip()` returns its argument unchanged and ignores its `limit`; `INDEX_BULLET_MAX`,
  `LOG_ENTRY_MAX` and `DAILY_LINE_MAX` are read by nothing else. The module docstring still
  says lines are clipped. Whole lines were the decision
  (`docs/research/2026-09-14-less-noise-at-session-start.md`); the leftovers only mislead.

## Practice on this date

- "good context engineering means finding the smallest possible set of high-signal tokens that
  maximize the likelihood of some desired outcome"
  ([Anthropic, Effective context engineering for AI agents](https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents),
  fetched 2026-09-17). A 64-character digest and the words `claude` and `major` are not that;
  in a six-line budget they displace half of what the entry says.
- The owner's rule 4 asks the same of a system that spends tokens on every session.

## The decision

- `- Agent:`, `- Capture intent:` and `- Tier:` join the dropped label lines.
- The session id is cut from the header of any lifecycle entry the worker writes
  (`session-end`, `pre-compact`).
- `clip()` and the three unused limits are removed and the docstring says what happens:
  lines are kept whole, and the section budgets decide what fits.

Files: `scripts/session_start_context.py`,
`tests/test_the_daily_excerpt_shows_the_entry_not_its_label.py`
