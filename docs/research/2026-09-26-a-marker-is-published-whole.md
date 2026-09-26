# A marker is published whole

Date: 2026-09-26. Audit 2026-09-26 B-21.

## Facts

- `operational_ownership._publish_marker` created `run/maintenance.lock` with
  `O_CREAT|O_EXCL` and wrote the PID afterwards. A crash between the two left a
  file that `_marker_pid` could not parse; `_remove_orphan_marker` then raised
  `marker_identity_invalid` on every later nightly and weekly pass, for good.
- POSIX `link()` (https://pubs.opengroup.org/onlinepubs/9799919799/functions/link.html,
  fetched 2026-09-26): it "shall atomically create a new hard link for the
  existing file", and fails with EEXIST when "The path2 argument resolves to an
  existing directory entry". Python's `os.link` raises `FileExistsError` there.
- `run/compile.pid` is also written in place by the pre-registry compile lock
  (`maybe_compile`), so a torn `compile.pid` can still belong to a live writer.

## Decision

- `_publish_marker` writes the payload whole to a staging file beside the marker,
  fsyncs it, and links it in with `os.link`; `FileExistsError` keeps the old
  exclusivity. The staging name is removed either way.
- For `run/maintenance.lock` only, an ownerless marker that can be read but names
  no PID is torn by construction — no live writer exposes a partial file now — and
  is removed like a dead owner's. A marker that cannot be read at all still refuses
  by name; `compile.pid` keeps refusing. No age is read.

## Files

- `scripts/operational_ownership.py`
- `tests/test_a_marker_is_published_whole.py`
- `tests/test_scheduled_fence.py`
- `CHANGELOG.md`
