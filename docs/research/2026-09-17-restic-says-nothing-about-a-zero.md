# Restic says nothing about a zero, and says a lot about progress

Dated 2026-09-17. Findings Q-H3 (high) and Q-M17 (medium) of the third audit. Both were
"suspected" because no Restic was at hand; both were checked here against the pinned
release. The research before the fix.

Files: scripts/private_vault_backup.py, tests/test_a_real_restic_summary_is_accepted.py

## What was found

- `_validate_restore_counter` requires every one of `total_files`, `files_restored`,
  `files_skipped`, `files_deleted`, `total_bytes`, `bytes_restored`, `bytes_skipped` to be
  present as an `int` in the restore summary.
- Restic 0.19.1 declares every one of those fields `omitempty`
  (`internal/ui/restore/json.go`, tag `v0.19.1`, struct `summaryOutput`:
  `FilesSkipped uint64 \`json:"files_skipped,omitempty"\`` and the same for the other six,
  https://github.com/restic/restic/blob/v0.19.1/internal/ui/restore/json.go). Go omits a
  zero `uint64` under `omitempty`.
- Observed with the real binary (`restic 0.19.1 compiled with go1.26.4 on linux/amd64`,
  SHA256SUMS checked), a clean restore of two files printed exactly:
  `{"message_type":"summary","total_files":3,"files_restored":3,"total_bytes":8,"bytes_restored":8}`.
  No `files_skipped`, `files_deleted` or `bytes_skipped`. So every clean restore was refused
  with `restic_restore_output_invalid`, and the `except BaseException` in
  `restore_private_vault` then cleared the freshly restored target. H3 is real.
- M17: the audit expected 60 status lines a second. The source says otherwise:
  `CalculateProgressInterval` starts from `time.Second / 10`, and only
  `RESTIC_PROGRESS_FPS` raises it, capped at 60
  (https://github.com/restic/restic/blob/v0.19.1/internal/ui/progress.go). Ten lines a
  second of a few hundred bytes still fill the 1 MiB bound of `_run_bounded` within minutes,
  and the run is then killed with `restic_output_limit`. The defect is real, the rate is
  lower than stated.
- The same function returns an interval of 0 when `show` is false, and both commands pass
  `!gopts.Quiet` as `show`. The summary is printed by `terminal.Print` regardless of
  verbosity. Observed: `restic backup --json --quiet .` and
  `restic restore latest --json --quiet --target <dir>` each print the summary line and
  nothing else.

## The decision

- A counter that is absent from the restore summary reads as zero, which is what Restic
  means by leaving it out. A counter that is present must still be a non-negative `int`, and
  a non-zero skipped or deleted count still refuses the restore. The restored image is then
  validated against the manifest digest exactly as before, so a summary that says too little
  cannot pass a bad restore.
- `backup` and `restore` are run with `--quiet` next to `--json`: no status stream, the
  summary only. The output bound stays at 1 MiB; raising it would only move the cliff.
- Tests: the verbatim summary of the real binary goes through `restore_private_vault`; and
  one end-to-end backup and restore runs against a real `restic 0.19.1` when one is on
  `PATH`, and is skipped otherwise (no CI job installs Restic today — named in the report).
