# A snapshot says when it last looked

Date: 2026-09-25. Audit item B-32 (`docs/AUDIT-2026-09-25-full.md`).

## Facts (checked in code)

- `snapshot_knowledge._committed` reported every failed `git commit` as "no change", and the
  nightly reclaim step (`reclaim_runtime_state.snapshot_memory`) passed that on as success.
- Doctor's `backup` check dated the copy by the snapshot repository's newest commit. An unchanged
  memory makes no commit, so a vault that did not change for longer than the freshness window
  was reported with a stale backup although the nightly snapshot ran and succeeded.
- The contract said "The single automatic Git operation is the nightly fast-forward update of the
  checkout", while the snapshot commits every night — in its own remote-less repository outside
  the vault, which that sentence did not mention.

## Source

- git-diff, https://git-scm.com/docs/git-diff (fetched 2026-09-25): `--exit-code` "exits with 1
  if there were differences and 0 means no differences"; `--quiet` "Disable all output of the
  program. Implies `--exit-code`."

## Decision

- "no change" is decided by `git diff --cached --quiet` after `git add -A`; a failing `git add` or
  `git commit` raises `SnapshotFailed` with git's last line, which the nightly step reports as
  `failed: SnapshotFailed`.
- Every successful snapshot, committed or not, writes its UTC time to
  `.git/llm-wiki-last-snapshot` (inside `.git`, never part of the copy); doctor takes the later of
  that and the newest commit.
- `CLAUDE.md`/`AGENTS.md` (byte-identical) now say the fast-forward is the single automatic Git
  operation on the checkout, and name the snapshot's own repository.

## Files

- `scripts/snapshot_knowledge.py`
- `scripts/doctor.py`
- `tests/test_a_snapshot_says_when_it_last_looked.py`
- `CLAUDE.md`
- `AGENTS.md`
- `CHANGELOG.md`
