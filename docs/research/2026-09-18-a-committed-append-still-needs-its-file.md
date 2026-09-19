# A committed append still needs its file

Dated 2026-09-18. Finding Q-L4 of the third audit (Markdown transactions).

Files: scripts/markdown_transaction.py,
tests/test_an_append_whose_file_is_gone_is_written_again.py

## What was found

- `_classify_comparable_append` answers a repeated `append_knowledge` with the committed record
  as soon as the stored request matches, without looking at the target at all. Reproduced by the
  audit and again here: append, delete the file, append the same block under the same operation
  id → the call returns `committed` and the file does not exist. The caller — the daily log
  header, the vault log header, the capture append — is told its line is on disk when nothing is.
- The mutate path does not have this hole: `apply` ends in `_verify_committed_targets`, which
  compares every target's current hash with its after-image and raises `TransactionDriftError`.
- That exact check is the wrong one for an append, and this is the reason the hole exists. A
  daily log is appended to by every later writer, so its current hash stops matching any single
  append's after-image within seconds. Requiring the after-image would make the second call for
  a perfectly healthy file raise drift. The audit's `M10` fix has the same shape: when the
  evidence for "already done" is gone, the request is new rather than an error.
- Permanent operation ids make this reachable rather than theoretical: `daily_log_append.py`
  uses `daily-header` plus the file name and `query_memory.py` uses
  `knowledge-log-header:log`, so the same id is asked again every time the file is absent —
  which is exactly the state after a rotation, an archive run, or an operator deleting a log.

## Practice on this date

- Idempotency keys are answers about a *request*, not a promise about the world afterwards.
  Stripe's rule for a key whose stored evidence is gone is to treat the call as new: "We generate
  a new request if a key is reused after the original is pruned"
  ([Stripe API, Idempotent requests](https://docs.stripe.com/api/idempotent_requests)). The same
  reading applies when the effect, rather than the record, is what disappeared.
- The narrow check is the honest one: existence answers "did this write survive at all", which
  is what the caller is asking, while byte equality answers "has anyone written since", which is
  none of this operation's business.

## The decision

- A committed duplicate append is still answered with the committed record while its target
  exists. When `_current_hash` answers `ABSENT`, the block went with the file, so the attempt
  advances and the next candidate id writes the line again.
- Changed bytes are deliberately not treated as loss: other writers append to the same file, and
  raising on that would break the ordinary case to catch a rare one.
