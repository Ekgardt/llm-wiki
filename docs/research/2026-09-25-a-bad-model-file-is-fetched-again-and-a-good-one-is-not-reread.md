# A bad model file is fetched again, and a good one is not re-read

Date: 2026-09-25. Audit item C-28 (`docs/AUDIT-2026-09-25-full.md`).

## Facts (checked in code and by test)

- `install_models._verified_download` removed only the snapshot link of a mismatching weights
  file. The Hugging Face cache keeps files as blobs behind links, and a file already present in
  a snapshot is not downloaded again, so the bad bytes stayed and the run failed every night.
  With a fake hub that, like the Hub, does not re-download a present file, the old code fails
  with `FileNotFoundError` on a corrupted cache; the new code fetches the pinned bytes.
- Every nightly run re-hashed each verified weights file (470 MB + 2.27 GB) to report `present`.

## Source

- git-update-index, https://git-scm.com/docs/git-update-index (fetched 2026-09-25): "Many
  operations in Git depend on your filesystem to have an efficient `lstat`(`2`) implementation,
  so that `st_mtime` information for working tree files can be cheaply checked to see if the file
  contents have changed from the version recorded in the index file." The same quick check is
  used here.

## Decision

- A mismatching cached weights file is removed with its blob (the existing `_retire_cached`)
  before the fetch and after a failed verification, so the next fetch brings the pinned bytes.
- A file that verified is recorded in `cache/model-verification.json` (disposable, one entry per
  pinned model) with its resolved path, size, modification time in ns, inode and the moment of
  verification; an unchanged file is not hashed again. As in git's "racy" case, a file whose
  modification time is within 2 s of its verification is re-read: the first test run caught a
  same-size rewrite in the same clock tick passing the quick check. A failed write of the record
  only costs a re-hash.

## Uncertainty

- A file rewritten in place with the same size and a restored modification time would pass the
  quick check until its stat changes; the fetch-time verification is unchanged.

## Files

- `scripts/install_models.py`
- `tests/test_a_bad_model_file_is_fetched_again.py`
- `CHANGELOG.md`
