# A refusal is cheaper than a crash

Dated 2026-09-18. Findings Q-L24 and Q-L26 of the third audit: three inputs the queue accepts
and then fails on, each in a different way.

Files: scripts/memory_queue.py,
tests/test_bad_input_is_refused_before_it_reaches_the_database.py

## What was found

- **Q-L24.** `_valid_source_failure_fields` accepts an `error_code` of 1 to 200 *characters*.
  The v3 schema stores it under `CHECK (length(CAST(error_code AS BLOB)) BETWEEN 1 AND 64)` —
  64 *bytes*. A code between those two bounds passes validation and then breaks the insert, so
  the caller gets `sqlite3.IntegrityError` from inside a transaction instead of the `ValueError`
  the validator exists to raise. Two numbers for one field, and the wrong one is checked. A
  multi-byte code makes the gap wider still: 64 characters of Cyrillic are 128 bytes.
- **Q-L26, first half.** `_is_secret_key(key)` calls `key.casefold()`. It is reached for every
  key of every payload through `_redacted_mapping` ← `_redact_payload` ← `enqueue`. A payload
  with a non-string key raises `AttributeError` out of `enqueue`.
  The first draft of this note said such a payload would otherwise store fine, because
  `json.dumps` coerces `int`, `float`, `bool` and `None` keys. Writing the test disproved it:
  this vault does not use `json.dumps` defaults. `reliable_memory._canonical_key` refuses the
  same payload one step later with `TypeError("canonical JSON object keys must be strings")`,
  and refusing it is right — a key whose encoded form is invented is a key nobody wrote.
  So the defect is not that the payload is rejected; it is *where* and *how*. The redaction
  walk breaks first, with an `AttributeError` that names nothing, instead of letting the
  encoder refuse the payload by its stated rule.
- **Q-L26, second half.** `_parents_before_children` recurses once per redrive generation while
  ordering a legacy v2 queue for import. The chain length comes from whatever history the
  pre-adoption queue accumulated, and an over-deep chain raises `RecursionError` in the middle
  of a migration, where every other failure is a named `_migration_error`.
- The audit also notes that `_is_secret_key` redacts any key ending in `_token`. That is
  deliberate and safe: over-redacting a key that is not a secret costs nothing, while the
  reverse writes a credential into the queue.

## Practice on this date

- Python's `json` coerces basic non-string keys by default — "If *skipkeys* is true, then dict
  keys that are not of a basic type (`str`, `int`, `float`, `bool`, `None`) will be skipped
  instead of raising a `TypeError`" ([Python, json](https://docs.python.org/3/library/json.html)).
  This vault deliberately does not: `_canonical_key` refuses a non-string key outright, because
  a canonical encoding that invents a key's text cannot be compared byte for byte afterwards,
  and byte comparison is what every digest in this system rests on. The lesson for the fix is
  that the refusal already exists and has a name; nothing above it should crash first.
- On recursion, the language's own limit is a guard rather than a contract: `sys.setrecursionlimit`
  exists because "the highest possible limit is platform-dependent"
  ([Python, sys](https://docs.python.org/3/library/sys.html)). Code that walks data of
  caller-controlled depth uses its own stack; the CPython C stack is not a bound anyone chose.

## The decision

- The source-failure validator checks the bound the schema actually enforces: 1 to 64 bytes of
  UTF-8, measured after encoding. One number, stated where the caller can act on it.
- `_is_secret_key` answers False for a key that is not a string, so the redaction walk finishes
  and the canonical encoder refuses the payload with the reason it already has
  (`canonical JSON object keys must be strings`) instead of an `AttributeError` from the
  redactor. A non-string key cannot spell a secret name, so nothing stops being redacted.
- `_parents_before_children` orders with an explicit stack instead of Python's. The order it
  produces is unchanged (parents before children, ties by sorted id) and a cycle still raises
  the same named `queue_v2_lineage_ambiguous`.
