# Windows fingerprint metadata and replacement fixtures

Date: 2026-11-03. Two distinct failures, with separate evidence.

## Executable dependency fingerprint

Native CI a6c8c2a0, run 41640869427, Windows end-to-end job 125933752227
retained artifact 13232472703. Its redacted diagnostic says PermissionError:
`runtime file changed during fingerprint` during compile. The compile dependency
inventory includes sys.executable. CPython 3.14.7's update_st_mode_from_path adds
0111 for .exe/.bat/.cmd/.com paths; os_fstat_impl does not perform that path-based
adjustment. Our opened-file comparison requires identical mode bits, although
these synthetic bits describe the name rather than an ACL or an actual mutation.
A local reproduction using matching stat fields except this documented mode
difference produces the same rejection. This identifies a real guard defect;
native end-to-end success still requires a new run after the correction.

Correct only the cross-API comparison: on Windows disregard the name-derived
execute bits while retaining type, read/write bits, identity, size, mtime and the
existing birth-time comparison. POSIX mode comparison remains exact. Same-API
before/after comparisons remain exact on all systems, including executable bits.
Two streaming hashes, containment, symlink/ACL checks and deadlines remain.
Qualification includes a failing portable representation case before the fix,
changed metadata counterexamples and real files of all executable extensions.

## Replacement during a fingerprint

Earlier native job 125910257152 on e2d9d318 showed WinError 5 from os.replace,
before the application's expected changed-file error. The reader deliberately
shares deletion, but MoveFileEx replacement of an existing open destination does
not thereby acquire POSIX replacement semantics. Microsoft documents separate
FILE_RENAME_POSIX_SEMANTICS for replacing an open destination. Renaming the open
original away is already exercised successfully by the stable-range fixture.

The test can create the same observed substitution by renaming the original to
an unused sibling and publishing the prepared replacement at its vacated path,
inside the same read callback. It must assert both real files' bytes afterward
and retain the original PermissionError('changed') expectation. This tests actual
path replacement with the old descriptor still open; it does not claim atomicity
of the adversary's two operations. No production sharing/rename change or skipped
application check is justified. The ordinary Windows nonsharing-descriptor refusal
test remains separate evidence of the OS boundary.

Primary sources checked today:
- CPython v3.14.7 source, update_st_mode_from_path and os_fstat_impl:
  https://github.com/python/cpython/blob/v3.14.7/Modules/posixmodule.c
- Microsoft FILE_RENAME_INFORMATION, open-destination and POSIX flags:
  https://learn.microsoft.com/en-us/windows-hardware/drivers/ddi/ntifs/ns-ntifs-_file_rename_information
- LLVM Windows filesystem source, rename_handle and rename_internal, an independent
  implementation distinguishing open-destination handling from ordinary replacement:
  https://llvm.org/doxygen/Windows_2Path_8inc_source.html

Do not treat the upstream .NET intermittent antivirus report as proof of antivirus
interference here. No sleeps/retries or blanket metadata omission are introduced.
Evidence retained privately: logs/native-windows-capture-failure.jsonl, native job
logs, local failing representation and the following qualified regression reports.
