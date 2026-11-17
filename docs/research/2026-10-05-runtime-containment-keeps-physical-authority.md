# Runtime containment keeps physical authority

Research date: 2026-10-05. Candidate qualification; installation is recorded separately.

The current queue inspection spends substantial time validating retained result
files and durable capture records. Its 60-second profiled observation includes
71,822 shared runtime metadata validations and 345,025 physical path resolutions.
The shared metadata reader constructs a relative `Path` solely to check containment.
That allocation contributes measurable cost without supplying later data.

The change retains fresh strict resolution of both the configured root and the
file's parent on every call, then compares their platform-normalized components
with `os.path.commonpath`. It still takes the actual leaf metadata with `lstat`.
The existing regular-file, symlink/reparse, size, ownership, descriptor identity
and stable-read checks remain. No file authority or schema result is cached.

Primary sources checked on 2026-10-05:

- [Python 3.10 os.path](https://docs.python.org/3.10/library/os.path.html):
  `commonpath` compares components, accepts path-like objects and rejects different
  drives. `commonprefix` cannot establish path containment. `normcase` follows
  platform casing rules.
- [Microsoft file and namespace rules](https://learn.microsoft.com/en-us/windows/win32/fileio/naming-a-file):
  drive, namespace and case handling are platform concerns; physical resolution
  remains delegated to the existing `Path.resolve` implementation.
- [Linux path_resolution](https://man7.org/linux/man-pages/man7/path_resolution.7.html):
  directory permissions, symlinks and parent components affect physical authority.
  Both strict resolutions must remain fresh.

Rejected alternatives: lexical string prefixes admit sibling directories; caching
root or parent resolution can retain stale symlink authority; non-strict resolution
changes missing-parent behavior; raising the inspection deadline does not remove
redundant work. This is an allocation change within the existing shared boundary,
not a new runtime location, data format or security contract.

A matched read-only comparison over the same 67,222 current queue-result paths
returned identical ordered acceptance, device, inode and mode records. The old
function took 4.609 seconds; the candidate took 3.058 seconds. These sequential
measurements include shared-machine contention. They do not establish whole-health
latency, full nightly completion, model cost or audit closure.

The new sibling-prefix, parent-retarget and root-retarget guards pass on the old
implementation as well: the original defect is redundant allocation measured by
profiling, not an existing containment escape. They protect the optimization.
The original baseline runtime file suite passed 57 cases with three unavailable
platform cases. Candidate checks and actual callable complexity are recorded in
private qualification logs. Linux execution and Python 3.10 grammar are not Windows
or Python 3.10 runtime qualification.

Evidence: the current profiled queue diagnostic, exact fresh indexed snippets of
`reliable_memory._contained_runtime_metadata` and related capture/queue consumers,
and the matched current-path comparison remain private. No runtime specimens or
private knowledge are published with this document.
