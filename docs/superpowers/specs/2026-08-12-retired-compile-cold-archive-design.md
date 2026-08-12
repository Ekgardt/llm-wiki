# Retired Compile Evidence Cold Archive Design

## Goal

Release bounded `run/retired-manifests/` and `run/retired-journals/` capacity
without deleting evidence or moving anything that remains part of live compile
recovery.

## Approved Shape

The compiler owns `run/compile-archive/transactions/<transaction-id>/`. This is
a new durable child of the existing `run/` recovery zone, not a new runtime root
or environment contract.

Operators use four explicit phases:

1. `audit` inventories hot retired stores and computes the live closure.
2. `backup-only` copies byte-exact candidates into a sealed, unapproved
   transaction without changing either hot store.
3. `apply` requires the reviewed manifest with only `approved: true` changed,
   rechecks closure and identities under the compile lock, and removes hot names
   through resumable transaction staging.
4. `verify` revalidates the archive payload, committed transaction journal,
   absence from hot stores, and empty staging.

## Live Closure

Roots are every canonical 64-hex identifier present in strict state and queue
records plus every active compile manifest and journal. Edges come from manifest
generation and batch IDs and journal batch, accepted generation, and accepted
batch IDs. A retired artifact is eligible only when its entire connected
component is unreachable. Unknown or malformed input fails closed.

## Transaction Layout

```
run/compile-archive/transactions/<transaction-id>/
  manifest.json
  manifest.seal.json
  transaction.json
  payload/
    retired-manifests/<original-name>.json
    retired-journals/<original-name>.json
  source-retired/
    retired-manifests/<original-name>.json
    retired-journals/<original-name>.json
  staging/
    retired-manifests/
    retired-journals/
```

The manifest seals roots, artifact IDs, original hot names, archive-relative
paths, sizes, hashes, and exact source snapshots. The seal excludes only the
top-level approval boolean. The mutable transaction journal records per-artifact
`prepared`, `removing`, `staged`, and `removed` progress. Archive payload is
never changed during apply. The `payload/` tree is the reviewed copy, while
`source-retired/` receives the exact original hot file identity so apply does
not unlink through a pathname that an ABA race could replace.

## Crash And Race Rules

- Persist `removing` before hot-to-staging rename.
- Persist `staged` before moving the original into `source-retired/`.
- A `removing` item may resume from hot or exact staging bytes, but never from
  both absent.
- A `staged` item may resume from exact staging bytes or an exact
  `source-retired/` original.
- Replacements, ABA races, hard links, symlinks, reparse points, ambiguous IDs,
  and changed closure abort without deleting unowned bytes.
- Re-running the same approved apply after commit is idempotent.

## Replay Boundary

Cold archive payload is deliberately outside `_load_manifest()` and
`_load_journal()`. It preserves evidence but does not silently widen automatic
replay. A future restore operation must be separately designed and approved.

## Current Primary Guidance

- Python 3.10 documents `os.fsync()` as forcing file data to disk and requires
  flushing buffered file objects first:
  https://docs.python.org/3.10/library/os.html#os.fsync
- Microsoft documents volume serial plus file index as a stable same-target
  comparison and `MOVEFILE_WRITE_THROUGH` as waiting for a move to reach disk:
  https://learn.microsoft.com/windows/win32/api/fileapi/nf-fileapi-getfileinformationbyhandle
  and https://learn.microsoft.com/windows/win32/api/winbase/nf-winbase-movefileexa
- Python 3.10.20 is a source-only security release; uv can request and pin an
  exact patch in `.python-version` and install a managed distribution:
  https://www.python.org/downloads/release/python-31020/ and
  https://docs.astral.sh/uv/concepts/python-versions/#python-version-files
