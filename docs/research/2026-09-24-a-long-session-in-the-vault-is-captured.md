# A long session in the vault is captured

Dated 2026-09-24. Audit items B-8, B-12, B-13, C-7 and C-14 (`docs/AUDIT-2026-09-24-live.md`).

Files: `scripts/user_prompt_capture.py`, `scripts/session_evidence.py`,
`scripts/episode_consolidation.py`, `scripts/flush_memory.py`,
`tests/test_a_long_session_in_the_vault_is_captured.py` (new), `CHANGELOG.md`,
`docs/research/2026-09-24-a-long-session-in-the-vault-is-captured.md`.

## What was found

- **B-12.** `user_prompt_capture._should_skip` drops every prompt whose working directory
  is inside the vault: "Sessions run inside the vault are maintenance loops, not user
  work." The owner develops the memory system in the vault, so the owner's longest
  sessions had no prompt counter and the every-20th-prompt capture (landed 2026-09-17)
  never fired live; such a session is captured only when its context is compacted. The
  rule predates the reentry marker: since 2026-09-17 the adapter drops every event raised
  by the memory's own processes (`CLAUDE_INVOKED_BY`, `integration_adapter._is_memory_automation`),
  which is what "maintenance loops" meant. The tool-breadcrumb and project-tag hooks keep
  their own in-vault rules; they write per-tool lines and project handoffs, which a
  session in the vault already gets from its checkpoints.
- **B-8.** A session record is `knowledge/raw/sessions/<day>/<session>.md` and a second
  capture of the same session on the same day replaces it. A capture keeps a bounded head
  and tail of the transcript, so the earlier tail is lost; and because consolidation
  marks a day done by the *names* of its records (`record_set_digest`), a replaced record
  does not reopen the day, so the new tail is never consolidated either. On 2026-09-23 the
  record of this job's session was 4 983 bytes and said 241 MB were not captured.
- **B-13.** 25 capture tasks are dead (8 attempts each, 2026-08-27..09-08; the causes —
  the stray candidate, the writer race — are fixed). 23 were redriven on 2026-09-06 and
  cancelled. Redriving them now would write summaries of month-old sessions into today's
  daily log under today's date. The weekly purge (see
  `2026-09-24-every-store-has-a-bound.md`) exports them, intents included, to the private
  queue archive, so their evidence is kept without inventing a date.
- **C-7.** A rule is written "**Rule** — When {trigger}: …" and the model's trigger already
  begins with "When": the live daily log has "When When planning…".
- **C-14.** `_require_canonical_body` says output is refused when "a reply … declares no
  tier anywhere"; `_declared_tier` reads only the first non-blank line, as its own
  docstring says, for a security reason (a tier quoted from a transcript must not decide).
- The prompt hook, when `memory_state` cannot be imported, replaces `spawn_detached` with a
  function that returns None: the 20th-prompt capture would then never run and say nothing.

## Practice on this date

- Filter automation by its identity, not by where it runs: the reentry marker names the
  memory's own processes exactly; a directory is shared by the owner and the automation.
- An append-only evidence store names each piece so a later piece never replaces an earlier
  one; identical content keeps one name (content addressing), so a replay stays idempotent.

## The decisions

1. The prompt hook no longer skips the vault; the adapter's reentry check stays the one
   filter for the memory's own traffic.
2. A second, different record of a session on the same day is written beside the first as
   `<session>@<8 hex of its sha256>.md`; identical content keeps the first name. The day's
   record set changes, so consolidation reads the new window.
3. The 25 dead tasks are not redriven; the weekly purge archives them.
4. A rule's trigger loses a leading "When" before the template adds one.
5. The docstring says what `_declared_tier` does.
6. When `memory_state` cannot be imported, the prompt hook records the failure once and
   does nothing else, instead of running with no-op stand-ins.

## Sources

- `integration_adapter._is_memory_automation` and
  `docs/research/2026-09-17-the-six-capture-corrections-the-first-round-left.md` (the reentry
  marker); the live vault's `knowledge/raw/sessions/2026-09-23/`, `run/queue-v3.sqlite3` and
  `knowledge/daily/2026-09-24.md`, read 2026-09-24.
- Content-addressed storage as practised by Git objects — https://git-scm.com/book/en/v2/Git-Internals-Git-Objects — fetched 2026-09-24.
