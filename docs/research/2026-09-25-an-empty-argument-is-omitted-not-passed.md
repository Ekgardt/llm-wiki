# An empty argument is omitted, not passed

Date: 2026-09-25. Audit item B-29 (`docs/AUDIT-2026-09-25-full.md`).

## Facts (checked in code)

- `install.ps1` called `installer_config.py sync-args ... --environment
  [string]$env:UV_PROJECT_ENVIRONMENT`; with the variable unset that is `--environment ""`.
- `Invoke-NativeCommand` runs `& $FilePath @ArgumentList`, the ordinary native invocation.
- `installer_config.py` declares `--environment` as an option that takes one value, so a bare
  `--environment` makes argparse exit with "expected one argument".
- Not reproduced here: no PowerShell is installed on this machine, and CI runs `pwsh` 7, which
  keeps the empty string. The audit marked this a suspicion.

## Source

- Microsoft Learn, `about_Parsing` (7.6), https://learn.microsoft.com/en-us/powershell/module/microsoft.powershell.core/about/about_parsing
  (fetched 2026-09-25), on PowerShell 7.3's native argument passing: "The new behavior is a
  **breaking change** from the Windows PowerShell 5.1 behavior." and among the new behaviours:
  "Empty strings as arguments are now preserved".

## Decision

- The flag is added only when the variable has a value; the Python side already defaults it.
- `Invoke-NativeCommand` refuses any empty argument on every PowerShell, so the class is caught
  by the CI Windows install jobs even though they run 7.x.
- A test fails if the installer casts an environment variable into an argument again.

## Files

- `install.ps1`
- `tests/test_an_empty_argument_is_omitted_not_passed.py`
- `CHANGELOG.md`
