# A slug the journal refuses is not a slug

Dated 2026-09-18. Finding Q-L30 of the third audit (operational core, project handoff).

Files: scripts/project_journal.py, scripts/session_start_project_state.py,
tests/test_a_project_named_con_still_gets_a_handoff.py

## What was found

Two independent problems in the same hook, both stated in the row.

**The slug.** `session_start_project_state._sanitize` lowercases, replaces
`[\s_/\\:*?"<>|]+` with hyphens and strips hyphens. `project_journal._require_slug` then checks
the same value against rules `_sanitize` never applies: `_require_unreserved_slug` rejects the
reserved Windows device names (`con`, `prn`, `aux`, `nul`, `com1`…), `_require_portable_slug`
rejects a trailing space or dot, and `_portable_slug_characters` rejects any character in the
Unicode `C` categories. A project directory called `aux`, or `notes.` — or one whose name
contains a stray control byte — therefore produces a slug the journal refuses on every
checkpoint. The refusal is not shown to anybody: the hook writes one line to
`logs/hook-errors.log` and returns, so that project simply never has a handoff, for ever, with no
symptom an owner would connect to its name.

The producer and the consumer of the slug disagree, and the consumer is the authority: it is the
one that has to put the name in a path on Windows.

**The bootstrap.** `_bootstrap_new_project` runs `bootstrap_project.py` with
`subprocess.run(..., timeout=30)` inside the SessionStart hook, under the comment "never block
session start on bootstrap failure". The comment is about the `except`; the 30-second wait above
it is exactly a block. Measured here on 2026-09-18: `bootstrap_project.py` takes **0.09 s** on a
fresh one-commit repository and **0.12 s** on this repository, which has thousands of commits and
a remote. The budget is two hundred and fifty times the work, and this product's own statement of
how long session start may wait for anything is `SESSION_START_RECOVERY_SECONDS = 0.25`.

## Practice on this date

The robustness principle is usually quoted for its first half; the half that applies here is the
second. RFC 1122 §1.2.2 states it as "Be liberal in what you accept, and conservative in what you
send" ([RFC 1122](https://www.rfc-editor.org/rfc/rfc1122)). A component that hands a name to a
validator whose rules it can read is obliged to hand it a name that passes. The alternative —
which is what happens today — is a silent, permanent, per-project failure.

For the timeout, the rule is that a bound is chosen from a measurement, not from a round number.
0.09 s and 0.12 s measured, against a 30 s bound, is not a safety margin; it is a bound nobody
sized.

## The decision

- `project_journal` grows one exported function, `portable_slug`, which turns a candidate into
  the nearest name `_require_slug` accepts: it drops the characters the journal refuses, trims
  trailing spaces and dots, prefixes `project-` when the result reads as a reserved Windows
  device name, and answers `""` when nothing usable is left. `_require_unreserved_slug` is
  refactored to share the one reserved-name test, so the producer cannot drift from the
  validator again.
- `_sanitize` ends by returning `portable_slug(...)`. The slug a hook mints is one the journal
  accepts, by construction.
- The bootstrap's budget becomes a named constant of 5 seconds — forty times the measured cost,
  one sixth of what it was — and the comment says what actually happens: a bootstrap that does
  not finish in time leaves no `bootstrap.md`, so the next session start of that project runs it
  again. Nothing is lost by cutting the wait.
