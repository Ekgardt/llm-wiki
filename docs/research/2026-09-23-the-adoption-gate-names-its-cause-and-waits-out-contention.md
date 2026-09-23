# The adoption gate names its cause and waits out contention

Dated 2026-09-23. Files: `scripts/installed_memory_repair.py`, `scripts/memory_queue.py`,
`tests/test_the_adoption_gate_names_its_cause.py`,
`docs/research/2026-09-23-the-adoption-gate-names-its-cause-and-waits-out-contention.md`.

## What was found

- `doctor --rebuild-generation` on the live vault at 18:27 UTC reported the queue repair
  failed with `ReliabilityV3ValidationError: reliability_v3_record_invalid`; the same
  command two minutes later, and a queue-only repair, passed, and the standalone
  validation passed. The nightly of 03:00 UTC died with the same code (there, the stray
  candidate of the morning's audit). The code names neither the file nor the cause:
  `require_reliability_v3_adopted` wraps every exception of the validation into that
  one code and the wrapper's message is the code alone, so a session-hook writer
  holding the database for a moment and a broken record read the same.
- The coordinator already treats this: `markdown_transaction._require_adopted_once`
  retries the validation for 30 s on transient SQLite or Windows sharing contention
  and caches a passed validation per record digest. The adopted queue's
  `active_memory_queue` calls the validator bare, once, on every open, so a
  contended moment fails the open outright.

## Practice on this date

- An error that fences every write must carry what it saw (this vault's own rule
  since 2026-08-29, `docs/research/2026-08-29-*`; the audit note in
  `installed_memory_repair.py` says the code "names neither the file nor the cause").
- One validation, one retry policy, one cache: the queue reuses the coordinator's.

## The decisions

1. `ReliabilityV3ValidationError` carries a `detail`; the wrapper sets it to the cause's
   type and message, and `str()` reads `code: detail`, which is what doctor's
   `describe_error` prints into `repair_errors`.
2. `active_memory_queue` validates through `markdown_transaction._require_adopted_once`:
   retried on contention, cached once passed.

## Cost, by rule 4

None on the hot path: a passed validation is cached per process; a contended one
waits at most 30 s instead of failing.

## Sources

- `logs/maintenance/`, `journalctl --user -u llm-wiki-nightly.service` (03:00 UTC), and the doctor reports of 18:27–18:31 UTC on the live vault, 2026-09-23.
- `scripts/markdown_transaction.py` `_validate_adoption_with_retry`, read 2026-09-23.
