# The verdict cache forgets the oldest, not the first in the alphabet

Dated 2026-09-17. Third audit, a low finding of the generations report. Research before the fix.

Files: `scripts/verified_artifacts.py`,
`tests/test_the_verdict_cache_forgets_the_oldest.py`.

## What was found

- `VerifiedArtifacts._merged` keeps the last `MAX_ENTRIES = 512` keys of
  `{**entries, **fresh}`, which reads as "the most recent". `_write` serialises with
  `sort_keys=True`, so after one save the file's order — and the order the next process loads
  — is alphabetical. From then on "the last 512" means the 512 greatest keys, and a key that
  is remembered again keeps its old position instead of moving to the end.
- Harmless while generation ids sort by time and `verdict|...` keys are few; wrong as soon
  as more than 512 keys exist: a verdict about the newest bytes can be the one evicted, and
  the cost is the slow open the cache exists to avoid.
- Reproduced through the product's own save path: 512 old verdicts are saved, one of them is
  verified again, then a new one is saved. The re-verified verdict — the second newest — is
  the one forgotten, because it still sits first in the file.

## Practice on this date

- Python, `json.dumps`: "If sort_keys is true (default: False), then the output of
  dictionaries will be sorted by key." (https://docs.python.org/3/library/json.html). And
  the mapping type: "Changed in version 3.7: Dictionary order is guaranteed to be insertion
  order." (https://docs.python.org/3/library/stdtypes.html#dict). Insertion order survives a
  JSON round trip only when the writer does not sort.
- Nothing reads the file's bytes for identity: it is a disposable cache under `cache/`, so
  byte-for-byte determinism of the file buys nothing.

## The decision

- The file is written in insertion order, and `_merged` puts what this scan verified after
  what was loaded, moving a re-verified key to the end. "The last 512" is then the most
  recently verified 512. The schema and the keys do not change; an existing alphabetical
  file is read as it is and corrects itself as entries are re-verified.
