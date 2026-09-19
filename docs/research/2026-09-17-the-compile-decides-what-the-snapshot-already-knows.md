# The compile decides what the snapshot already knows

Dated 2026-09-17. Findings M-A4, M-A5, M-A6 and M-A7 of the third audit, all four left in
round one as the owner's decision and delegated in round two. The research before the fixes.

Files: `scripts/compile_memory.py`, `skills/session-memory-compile/SKILL.md`,
`docs/USER-GUIDE.md`, `CHANGELOG.md`, `tests/test_compile_transactions.py`,
`tests/test_compile_hardening.py`,
`tests/test_the_compile_decides_what_the_snapshot_already_knows.py`.

## What was found (each verified by reading and by a test that fails before the fix)

- **M-A6.** `_require_target_state` refuses a `create` whose slug already exists and an
  `update` whose slug does not, and the refusal rejects the whole plan as a
  `validation_error`. The draft prompt shows only the context pages that fit, taken in
  alphabetical path order, so on a vault of hundreds of pages the model has never seen most
  slugs. It cannot get the action right except by luck; the retry (a full draft call, about
  27k input tokens) asks the same blind question again.
- **M-A7.** `_dropped_from_reviews` reads only `drop` verdicts. An operation the critic left
  out, or named with a mistyped slug, is written as if it had passed, while `_review`'s
  docstring promises "nothing goes unreviewed". The critique prompt itself asks only for
  drops, so an empty list is today a legal "everything passes".
- **M-A5.** `--discard-unusable-receipts` promises that "the next pass rebuilds from the
  immutable daily", but `_daily_already_compiled` falls back to the state mirror, which still
  holds the day's whole-file digest, and `maybe_compile._has_pending_work` reads the same
  mirror. The day stays "compiled" with no receipt.
- **M-A4.** `--all` is parsed and read nowhere. Making it real would mean recompiling
  committed days, which the receipt contract forbids (a committed day is never compiled
  again) and which would duplicate every page's update sections and pay for every day again.

## Practice on this date

- "UPSERT is a clause added to INSERT that causes the INSERT to behave as an UPDATE or a
  no-op if the INSERT would violate a uniqueness constraint."
  ([SQLite, UPSERT](https://www.sqlite.org/lang_upsert.html)). Whether a row exists is a fact
  the store knows and the writer does not; the store resolves it instead of refusing.
- "Fail-safe defaults: Base access decisions on permission rather than exclusion." (Saltzer
  and Schroeder, *The Protection of Information in Computer Systems*, 1975, design principle
  b). A gate that passes whatever it was not told to stop is exclusion; a gate that writes
  only what received a `pass` is permission.
- "During a project's lifetime, some arguments may need to be removed from the command line.
  Before removing them, you should inform your users that the arguments are deprecated and
  will be removed." ([argparse, `deprecated`](https://docs.python.org/3/library/argparse.html)).
  The parameter itself needs Python 3.13; the product supports 3.10, so the notice is printed
  by hand.

## Token cost of the alternatives for M-A6

- Listing every existing slug in the draft prompt: about 8 tokens a slug, on every draft call
  and every retry, growing with the vault for ever — 4,000 tokens a call at 500 pages. Law 4
  (spend tokens sparingly) speaks against it, and it still leaves the refusal in place for
  the day the model ignores the list.
- Dropping the one refused operation and keeping the rest: zero tokens, but the day then
  gets its receipt and the dropped knowledge is never compiled again.
- Letting the snapshot decide the action: zero tokens, nothing lost. An `update` is an
  appended dated section and never reads the old page, and both actions carry the same
  required fields, so the rewrite is mechanical in both directions.

## The decisions

- **M-A6.** The action follows the immutable snapshot: a drafted `create` for a slug that
  exists becomes an `update`, a drafted `update` for a slug that does not becomes a `create`,
  and stderr names each rewrite. This happens once, before the critique, so reviewer, cache
  and plan validator all see the same operation. `_require_target_state` stays as the
  validator's check (a cached plan whose target changed since is still refused). Any other
  invalid operation — evidence that does not bind, a wrong shape — still refuses the whole
  plan and is retried: the retry is the remedy for a stochastic generation, and a partial
  plan would seal the day with knowledge missing. Existing slugs are not listed in the
  prompt, and the alphabetical choice of context pages is left as it is: once the action no
  longer depends on what the model was shown, the context pages only help wording.
- **M-A7.** The critique asks for exactly one review of every operation
  (`compile-critique/v3`; about 30 output tokens an operation). Operations a reply did not
  name are asked about once more, alone — a small prompt, not a new draft. What is still
  unnamed after that is a `validation_error` of the critique stage, which the existing
  bounded retry handles; nothing is written without a `pass`.
- **M-A5.** Discarding a receipt also removes the mirror entry of the day it belonged to.
  The day is found from the receipt's file name, which is the identity of (logical path,
  part digest) — computable for every daily part even when the receipt's own text is
  unreadable. Mirror-only days of old vaults (compiled before receipts existed) are not
  touched. The receipts stay the authority and the mirror stops contradicting them.
- **M-A4.** `--all` stays accepted for one more minor release, is hidden from `--help`,
  prints one line to stderr saying that it does nothing and will be removed, and is recorded
  under *Deprecated* in `CHANGELOG.md`. The skill and the user guide stop offering it. It is
  not made real, for the reason above. The owner's rule that a CLI spelling named in the user
  guide is not deleted outright is kept.
