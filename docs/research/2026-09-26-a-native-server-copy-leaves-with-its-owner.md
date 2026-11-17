# A native server copy leaves with its owner

Date: 2026-09-26. Audit 2026-09-26 items A-6 and C-7.

## Fact
- A native server (gopls, rust-analyzer) is launched from a verified copy the
  session writes into its owner root: `tempfile.mkstemp(prefix=".launch-",
  dir=owner_root)` (`pyright_session.py`, `_open_snapshot`), and the launcher
  deliberately stops owning it (`_native_path_launch`) because the server reads
  its own executable while it runs.
- Nothing removes it: the success cleanup unlinks `owner.json`, `failure.json`
  and `cancellation`, then `rmdir`s the owner root (`lsp_process.py`,
  `_unlink_posix_scratch`, `_remove_posix_owner_root`). The `rmdir` fails, every
  close of that session raises, the session is marked stranded, and at four
  sessions every query fails until the MCP process restarts (audit reproduction).
- A failed start keeps the whole root as evidence, 43–45 MB of binary included;
  retirement bounds evidence by count and age only.

## Source (fetched 2026-09-26)
rmdir(2), https://man7.org/linux/man-pages/man2/rmdir.2.html: "ENOTEMPTY path
contains entries other than . and ..". An owner root that still holds the copy
cannot be removed.

## Decision
- The success cleanup unlinks every regular `.launch-*` file in its own owner root
  (through the owner directory descriptor) before the `rmdir`.
- The sweep that already takes down dead owner roots also removes the `.launch-*`
  copies from roots it keeps for their `failure.json`, once every process the
  records name is proven dead. The evidence records stay; the binary is not
  evidence.

## Files
- scripts/lsp_process.py
- tests/test_a_native_server_copy_leaves_with_its_owner.py

## Qualification correction, 2026-09-30

The isolated namespace-aware candidate exposed an incomplete test record:
`test_a_dead_failure_root_keeps_its_records_and_drops_the_copy` stored only
`owner_pid`. The host stopped at this failure after 643 passing tests and two
skips (user-provided output, 106.10 seconds). A local run reproduced the same
failure. For the same reaped child, the current reader returns `unknown` without
its recorded identity and `dead` with the identity captured while it was alive.
The former must not authorize deletion in Linux: the PID may name an invisible
owner in another namespace.

The test-only correction reuses the existing synchronized `finished_process`
helper and records `owner_start_identity`. It still requires removal of the
launch copy and now checks both retained records byte for byte. Additional cases
require all files to survive for a live owner, a PID-only Linux record, and an
unqualified old Linux identity. Runtime cleanup and ownership checks are unchanged.
Related sweep and ownership scenarios passed before the correction (58 tests).

Before editing, the isolated native index was refreshed to
`generation-18d9f6fbc16390c9-c59b69bc`; change detection reported no stale sources.
Graph caller/callee results were supplemented with source inspection because
the graph reports incomplete coverage. This corrects a fixture for the existing
ownership contract, without a new architecture, dependency, runtime path or limit.
The candidate is not installed; full host qualification and safe cutover remain
open. Evidence is retained under `logs/audit-2026-09-30-native-copy-*`.
