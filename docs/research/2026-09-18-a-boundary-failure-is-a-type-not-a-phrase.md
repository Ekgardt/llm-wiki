# A boundary failure is a type, not a phrase

Dated 2026-09-18. Finding Q-L14 of the third audit (operational core, Markdown transactions).

Files: scripts/markdown_transaction.py,
tests/test_a_missing_file_is_not_a_moved_parent.py

## What was found

Eleven decisions in `markdown_transaction.py` are taken by searching the *text* of an exception:

- `_is_target_boundary_error` matches eight phrases (`"parent identity"`, `"outside the vault"`,
  `"stable directory"`, …) and, on top of that, answers True for **every** `FileNotFoundError`.
  Four callers act on that answer: apply quarantines the transaction as
  `parent_identity_changed`, recovery settles it as quarantined, and the two rollback helpers
  raise `_TargetBoundaryChanged`, which stops an undo half way.
- `_conflicted_failure` and `_conflicted_from_message` pick `before_hash_mismatch` versus
  `unknown_target_bytes` from `"before state mismatch"` / `"after state mismatch"`.
- `_translate_apply_error` and `_recovered_from_message` recognise a damaged image by
  `"after-image is corrupt" or "plan hash mismatch"`.
- `_prepare_or_recover` (`"operation_id is already bound"`), `_append_value_failure`
  (same phrase, plus `"precondition changed"`), `_apply_or_reread_terminal` and
  `_append_runtime_failure` (`"transaction cannot be applied from state"`).
- `_cli_message_code` maps five more phrases to the CLI's error codes.

Two of these are wrong today, not merely fragile:

1. **Any missing file is read as a moved parent.** The phrase list is at least aimed at the
   boundary; `isinstance(error, FileNotFoundError)` is not. Everything under apply that can
   fail with `ENOENT` for its own reasons — an unlinked staging file, a target the plan expects
   to replace — is reported to the caller as `parent_identity_changed` and the transaction is
   quarantined, which is the one disposition an operator cannot undo automatically.
2. **A phrase is not owned by the site that raises it.** `"transaction before-image is corrupt
   for …"` (`_before_state`) and `"abort before-image is corrupt"` (`_abort_before_state`) are
   raised in unrelated places and both fall into the CLI's `before_image_corrupt` bucket by
   accident of wording. Rewording any of these eleven messages — for clarity, for a translation,
   for a path added to the text — silently changes behaviour, with no test that would notice.

`"busy"`/`"locked"` in `_is_transient_writer_contention` is deliberately **not** in this list:
`sqlite3.Error.sqlite_errorcode` arrived in Python 3.11 and this product's floor is
`requires-python = ">=3.10"`, so on the supported floor the driver offers nothing else.

## Practice on this date

Python fixed exactly this shape in its own standard library. PEP 3151, *Reworking the OS and IO
exception hierarchy*, was written because callers had to discriminate errors by a secondary
value rather than by type: "This is a lot more to type, and also forces the user to remember the
various cryptic mnemonics from the `errno` module. It imposes an additional cognitive burden and
gets tiresome rather quickly." Its observation about what people do instead is the shape this
module is in — "many programmers will instead write the following code, which silences
exceptions too broadly: `try: os.remove(filename) except OSError: pass`" — and its answer is the
one taken here: "What the programmer would like to write instead is something such as:
`try: os.remove(filename) except FileNotFoundError: pass`"
([PEP 3151](https://peps.python.org/pep-3151/)).

The same rule is already the product's own: `TransactionFailure` carries `code` and `state`,
`ProjectPendingPriorError` carries its sequences, and `OperationBoundElsewhereError` was
introduced in the second round for one of these phrases. This finding finishes that work rather
than starting a new convention.

## The decision

- Each of the eleven conditions is raised as a named type that carries what the dispatcher needs:
  `TargetBoundaryFailure` (already present) for a parent that is not the prepared one,
  `TargetPathBoundaryError(ValueError)` for a path that cannot be contained,
  `TargetStateMismatch` (with `state_name`, `path`), `TransactionImageError`,
  `TransactionStateError` (with `state`), `PreconditionChangedError`, and for the CLI
  `RefusedOperation` / `RefusedArgument`, each carrying its own `code`.
- Every new type keeps the base class its callers already catch (`ValueError` stays a
  `ValueError`, `RuntimeError` stays a `RuntimeError`) and the message text is unchanged, so no
  caller outside this module and no existing test has to move.
- `_is_target_boundary_error` becomes one `isinstance` check. `FileNotFoundError` leaves it.
  So that the genuine case does not leave with it, every place that opens or stats a *parent*
  directory — the POSIX `O_DIRECTORY|O_NOFOLLOW` opens, `_parent_identity`, the Windows
  directory handles — converts `ENOENT`, `ENOTDIR` and `ELOOP` into `TargetBoundaryFailure`
  at the point where the code still knows it was looking at the parent. Any other `OSError`
  keeps its own identity and travels up untranslated, which is what it did before this module
  ever tried to name it.
