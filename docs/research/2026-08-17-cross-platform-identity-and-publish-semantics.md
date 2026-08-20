# Cross-platform file identity and publish semantics — 2026-08-17

Research behind four bug fixes on `fix/linux-installer-and-transient-cleanup`.
Every failure below was observed in CI run `32059892877`; none of them is a
design change, and each fix restores an invariant the code already intended.

## 1. Windows file identity widened in Python 3.12

**Observed.** On `windows_full::py3.12`, `py3.13` and `py3.14`, roughly seventy
`tests/test_code_workspace.py` cases fail with
`PermissionError: repository code directory changed during enumeration`, plus
one direct failure of `_windows_stat_matches_identity` showing
`st_dev=9230925183971590040` — a value far wider than a 32-bit volume serial.
`py3.10` and `py3.11` were cancelled by fail-fast, so they carry no verdict.

**Cause.** [What's New in Python 3.12](https://docs.python.org/3/whatsnew/3.12.html)
records, under gh-99726 (Steve Dower), that `os.stat()` and `os.lstat()` are
"more accurate" on Windows: `st_dev` may now be **up to 64 bits** and `st_ino`
**up to 128 bits**, read from `FILE_ID_INFO`. `GetFileInformationByHandle`
still reports the 32-bit `dwVolumeSerialNumber` of
`BY_HANDLE_FILE_INFORMATION`.

`code_workspace._entry_identity` compared one against the other, so from Python
3.12 onward the volume serials could never be equal and every directory looked
as if it had been swapped mid-enumeration. The TOCTOU guard was rejecting the
file it had just opened.

**Fix.** Ask the handle for both identities it can report — the
`BY_HANDLE_FILE_INFORMATION` pair and the `FILE_ID_INFO` pair — and accept a
match against either. Both come from the same open handle, so this is still an
exact identity match; it only stops assuming which of the two representations
the running interpreter's `os.stat` used.

**Rejected alternatives.** Pinning to `FILE_ID_INFO` alone would break Python
3.10 and 3.11, which the project still supports. Comparing only the file index
and ignoring the volume would weaken the guard: two volumes can repeat an
index.

## 2. POSIX renames a sealed directory only where the platform allows it

**Observed.** On `macos_full::py3.10` and `py3.14`, eleven
`tests/test_archive_daily_bagit.py` cases fail with `PermissionError: [Errno 13]`
renaming `.bag-<id>.building-<nonce>` to its final `bag-…` name.

**Cause.** [`rename(2)`](https://man7.org/linux/man-pages/man2/rename.2.html)
gives EACCES when "oldpath is a directory and does not allow write permission
(needed to update the `..` entry)". POSIX states the same as a *may*, so it is
implementation-defined: macOS enforces it, Linux does not when the parent is
unchanged. `DailyArchiver._seal` sets the build root to `0o500` before
`_prepare_build_for_publish`, and the publish is a directory rename — so the
rename is denied on macOS and permitted on Linux.

**Second-order effect, worse than the test failures.** In
`_recover_hidden_builds` the same rename sits inside `except OSError: …
_remove_build(build)`. On macOS an interrupted archive build is therefore
**deleted rather than resumed** — silent loss of an immutable evidence package
that recovery exists to save.

**Fix.** Keep every member read-only throughout; make the root writable for the
duration of the single `rename()` call and seal it again immediately after,
under its final name. On failure the root is re-sealed so the build stays valid
for a later recovery pass. The window where the root is writable contains one
syscall and no member ever becomes writable.

## 3. A frozen benchmark baseline bound to the whole lock file

**Observed.** Twelve `tests/test_retrieval_v2_benchmark.py` cases fail on every
platform, including locally (`12 failed, 6401 passed, 247 skipped`). Exactly
two clauses of `_verify_baseline_package_contract` fail:
`environment["uv_lock_sha256"]` and `verified["uv_lock_sha256"]`. Every frozen
package version is still present in `uv.lock`, and `package_map_sha256` still
matches.

**Cause.** Commit `350eec8` ("feat: close security and reliability audit gaps"),
already on `main`, retired the Cognee bridge and regenerated `uv.lock`. The
frozen baseline `benchmark/baseline-2026-07-16-retrieval.json` records the
digest of the whole lock file, which covers several hundred packages the
benchmark never loads.

**Not fixed here.** Relaxing the binding changes what evidence the benchmark
accepts, and re-attesting the baseline requires a real model run that this
environment cannot perform offline. Recorded for the machine owner's decision
rather than decided unilaterally.

## 4. An installer error that names the failure and hides the cause

**Observed.** `pyright-linux`, `pyright-macos` and `pyright-windows` all fail
with the single line `pyright_download_failed`.

**Verified locally.** The pinned URL
`https://registry.npmjs.org/pyright/-/pyright-1.1.411.tgz` returns HTTP 200
with no redirects, and its sha256 equals `PYRIGHT_PACKAGE_SHA256` exactly;
`install_pyright.py` completes with exit 0 against a scratch state root. The
pin is correct and the artifact is reachable — the CI failure is environmental,
and nothing in the log says which environment fact caused it.

**Cause.** `main()` prints `str(exc)`, which is the stable machine-readable
code, and discards `__cause__` — the `HTTPError`, `URLError` or `OSError` that
actually explains it. Three CI jobs are therefore undiagnosable by design.

**Fix.** Keep the code as the first line, so anything parsing it is unaffected,
and print the cause chain after it.

## Sources

- [What's New In Python 3.12](https://docs.python.org/3/whatsnew/3.12.html) — gh-99726, Windows `st_dev`/`st_ino` widening.
- [`rename(2)` manual page](https://man7.org/linux/man-pages/man2/rename.2.html) — EACCES on renaming a directory without write permission.
- [`rename(3)`](https://linux.die.net/man/3/rename) — the POSIX "may be required" wording that makes this platform-dependent.
