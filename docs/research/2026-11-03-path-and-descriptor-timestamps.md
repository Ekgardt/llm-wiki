# Compare timestamps within their observation domain

Date: 2026-11-03. Native Windows jobs 125828626107 and 125828626202 show stable
archive reads rejected and widespread adoption failure. The archive traceback
ends at bounded_io._require_live_source comparing lstat ctime against fstat ctime.
Runtime fingerprints make the same cross-API comparison during adoption receipts.

CPython issue 157671 demonstrates that on Windows 3.13/3.14 path stat retains
creation-time compatibility while fstat can return metadata change time. Those
fields can differ on a single unchanged file. Do not discard all ctime checks or
substitute birthtime for ongoing descriptor-change detection.

Keep separate initial path and descriptor snapshots. At admission compare existing
identity/size/mtime/mode checks and comparable timestamps: on Windows compare
birthtime (falling back to ctime on older Python), otherwise ctime. Then compare
all subsequent descriptor observations, including ctime, with the descriptor
baseline. Fingerprint final path validation stays against the original path
snapshot. Existing two-pass hashes, final source digest, containment, no-follow,
permissions, deadlines and writer-exclusion contracts remain unchanged.

The small cross-API timestamp comparison belongs beside stable reads in bounded_io;
runtime_fingerprints already depends on that module and reuses it. No new owner,
module, native API, store or runtime path. _require_opened_identity returns the
validated descriptor observation instead of throwing it away; existing callers
that ignore its return remain valid. Preserve POSIX pre-open ctime refusal.

Primary sources checked today:
- https://github.com/python/cpython/issues/157671 and linked CPython 3.14.7 source:
  reported native mismatch and exact implementation paths; issue remains open.
- https://learn.microsoft.com/en-us/windows/win32/api/winbase/ns-winbase-file_basic_info
  distinguishes CreationTime, LastWriteTime and metadata ChangeTime.
- https://man7.org/linux/man-pages/man2/stat.2.html describes same-file observations
  through path and descriptor on POSIX; retain the stricter matching semantics.

Alternatives rejected: ignore ctime on Windows throughout (loses mutation checks),
compare only birthtime throughout (creation is not change), retry unchanged reads
(the mismatch is deterministic), or weaken adoption acceptance. Reproduce both
public readers with synthetic native stat observations; test changed descriptor
ctime, changed creation/identity and POSIX mismatches as negative controls. Native
rerun remains required; local simulation alone does not establish Windows success.

Local reproduction failed exactly the two unchanged-file acceptance cases; the
five original refusal controls passed. After correction, 76 related reader,
fingerprint, archive, adoption and smoke cases passed on Python 3.12. Own bounded
reader and added controls: 57 passed, one existing platform skip. The expanded
Python 3.10 set passes 184 tests with one existing skip. Both descriptor ctime
mutation controls remain enforced; birthtime mismatch and POSIX ctime mismatch
still refuse. Test observations copy each snapshot instead of mutating the held
baseline. The mixed public-reader regression is owned by runtime-fingerprints,
which already depends on bounded_io; pure comparison cases stay with bounded IO.
The adoption diagnosis follows the confirmed same fingerprint comparison in its
receipt path; native success remains unconfirmed until rerun.
