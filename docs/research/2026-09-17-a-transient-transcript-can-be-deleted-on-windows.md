# A transient transcript can be deleted on Windows

Dated 2026-09-17. Finding C-F14 of the third audit (low-medium, suspected: there is no Windows
machine in this audit, and none was used for this fix). The research before the fix.

## What was found

- A host that sends its transcript inline gets it written to
  `cache/transient-transcripts/`, read once, and deleted (`_cleanup_runtime_transient`).
- On Windows the directory and the file are hardened with
  `icacls <path> /inheritance:r /grant:r <user>:(R,W)`: inheritance is cut and the user is
  left with read and write, and nothing else.
- Deleting a file on Windows needs the delete right on the file, or delete-child on its
  directory. `(R,W)` grants neither, so `unlink` should fail with `PermissionError`, which the
  cleanup swallows. Redacted session text would then pile up in that directory.

## Practice on this date

- Microsoft lists the simple rights separately: "N - No access F - Full access M - Modify
  access RX - Read and execute access R - Read-only access W - Write-only access D - Delete
  access" ([icacls, Microsoft Learn](https://learn.microsoft.com/en-us/windows-server/administration/windows-commands/icacls),
  fetched 2026-09-17). Read and write do not include delete; it has to be granted.
- Least privilege still holds: the file is data, so it needs read, write and delete, and not
  execute. `M` would add execute; `(R,W,D)` does not.

## The decision

- The grant becomes `<user>:(R,W,D)`. Nothing else about the hardening changes.
- What is tested is the command that is run. That the delete then succeeds on Windows is
  reasoned from the documentation above and has not been observed; the Windows CI job is
  where it will first be seen.

Files: `scripts/integration_adapter.py`,
`tests/test_plugin_helpers.py`
