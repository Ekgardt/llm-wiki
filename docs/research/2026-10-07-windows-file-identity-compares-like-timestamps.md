# Windows file identity compares like timestamps

Researched 2026-10-07. This change affects transient Codex executable and configuration qualification, not model choice, settings, storage, or persisted formats.

## Cause and primary evidence

[CPython issue 157671](https://github.com/python/cpython/issues/157671), opened 2026-09-17 and still open when checked, reports official Windows Python 3.14.7 returning creation time from pathname `stat().st_ctime_ns` while handle `fstat().st_ctime_ns` returns change time. Python documentation treats the Windows creation-time meaning as deprecated; the issue also reports 3.10.20 matching and 3.14.6 differing. This is a version/platform observation, not a guarantee for every Windows filesystem.

[Microsoft FILE_BASIC_INFO](https://learn.microsoft.com/en-us/windows/win32/api/winbase/ns-winbase-file_basic_info) distinguishes CreationTime and ChangeTime. [MITRE CWE-367](https://cwe.mitre.org/data/definitions/367.html) explains why a pathname check followed by use is vulnerable to changes between those operations. These are three independent primary organizations. The issue and Python documentation count as one organization.

The shared client compared a complete identity tuple from `fstat` with one from pathname `stat`. Consequently a stable selected executable or loaded configuration could be refused before its actual drift/protocol checks ran. Changing the diagnostic text would not correct that mismatch.

## Bound handle comparison

On Windows, the original read handle stays open. The client resolves the original pathname, reopens that pathname, and compares both handles using `fstat`, including device, inode, size, modification time, change time, and mode. It hashes the reopened bytes against the first digest, rechecks both open handles, verifies pathname resolution again, and rechecks both handles before closing them. The existing attempt-bound executable and configuration requalification still runs. A read, resolve, hash, or identity error remains a failure.

POSIX keeps the existing complete pathname-stat identity comparison, including ctime. Configuration checks retain their original candidate pathname; executable checks retain their resolved pathname. No Windows birthtime substitution or global removal of the change guard is introduced.

This remains cooperative validation rather than an atomic guarantee against a hostile filesystem changing the path after the last check or before execution. No metadata or successful authority verdict is cached.

## Alternatives and cost

Replacing change time with birth time would lose change evidence. Removing ctime everywhere would weaken POSIX and handle-change checks. A separate Windows native-API wrapper could retrieve common fields but adds another implementation and platform dependency. Reopening and comparing only complete handle metadata is cheaper and fixes the timestamp mismatch; it does not directly compare reopened bytes with the first digest. The additional hash is a deliberate stronger byte-binding check, not a mathematical necessity for correcting the timestamp naming discrepancy alone.

A read-only alternating measurement on this Linux host exercised the Windows branch with the same actual selected executable bytes. Its 289,101,384 bytes were hashed once in the POSIX branch (0.291–0.297 seconds) and twice in the simulated Windows branch (0.583–0.586 seconds). A 1,396-byte configuration took about 45–80 versus 106–125 microseconds. Input hashes were unchanged. This is not a Windows latency measurement or a whole-cycle performance claim. Each Windows qualification/recheck adds one full read/hash; cumulative extra bytes equal the number of actual bindings times the relevant file sizes. No actual full-cycle binding count was measured here.

## Qualification scope

The original simulated stable-file scenario fails for both consumers despite identical handle identity and bytes. Original run: five failed and ten passed; three additional positive setup tests also met the same original rejection. Controls preserve failures for changes after the first read, replacement, disappearance, a mismatched reopened digest, change during the reopened read, change-time-only drift, symlink retargeting, and POSIX ctime mismatch. Both handles must remain open during the second digest and close on success or failure.

The simulation modifies only the pathname timestamp view and chooses the Windows branch; it is not an actual Windows kernel execution. Real Windows CI is still needed to confirm the reported platform failure. Existing PowerShell wrapper tests may remain skipped when PowerShell is unavailable. Python 3.10 runtime qualification is separate from Windows runtime qualification.

Related platform fixture corrections normalize a manually supplied UTC anchor to the existing producer's `+00:00` representation and give the Markdown transaction API POSIX relative paths. They are maintained separately; production timestamp and restricted-path guards are not relaxed.
