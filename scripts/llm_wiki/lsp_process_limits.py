"""The bounds and timings every part of the LSP child process lifecycle shares."""
from __future__ import annotations

# The server stderr kept in memory, oldest bytes dropped first: a project constraint of the
# 2026-07-22 Pyright plan (docs/superpowers/plans/2026-07-22-python-pyright-navigation.md, "4 MiB
# stderr"); only the redacted last kilobyte is ever written out. Not measured.
MAX_STDERR_BYTES = 4 * 1024 * 1024
LSP_ENV_ALLOWLIST = frozenset(
    {
        "COMSPEC",
        "HOME",
        "LANG",
        "LC_ALL",
        "PATH",
        "PATHEXT",
        "SYSTEMROOT",
        "TEMP",
        "TMP",
        "TMPDIR",
        "USERPROFILE",
        "WINDIR",
    }
)

# One read of the stderr pipe: 64 KiB + 1, one byte over the default Linux pipe capacity of
# 65 536. The commit that set it (7d1f1a5d) gives no reason; review if stderr reads show up.
STDERR_CHUNK_BYTES = 65_537
# Spawning the server and publishing its first lease took longer than two
# seconds on a loaded four-core Windows machine, which turned a healthy
# start into `LSP startup deadline expired`. Readiness keeps its own much
# larger budget; this one only covers process start and lease publication.
STARTUP_WAIT_SECONDS = 10.0
FILE_ATTRIBUTE_REPARSE_POINT = 0x400
STARTUP_FAILED = "startup_failed"
PROCESS_EXITED = "process_exited"
# owner.json, lease.json and failure.json: the largest on the live vault is 219 bytes
# (2026-09-27) and failure.json's stderr tail is bounded at 1 KiB, so 4 KiB refuses only a record
# that is not ours.
MAX_EVIDENCE_BYTES = 4096
# The redacted last words of a failed server, as JSON encodes them. The rest of
# the record is under 300 bytes, so the whole file stays well inside its bound.
STDERR_TAIL_BYTES = 1024
# Redacted before the tail is cut, so no line reaches the redactor without its key.
STDERR_REDACTION_WINDOW_BYTES = 64 * 1024
# Output of the Windows ACL command run for one owner root (stdout and stderr together); 16 KiB
# refuses runaway output. Value predates measurement; review if a healthy ACL run is refused.
MAX_ACL_OUTPUT_BYTES = 16 * 1024
# The live lease is refreshed every 10 s and expires 30 s after its last refresh: the contract
# in CLAUDE.md and knowledge/notes/lsp-live-lease-decision.md (three missed beats = dead).
HEARTBEAT_SECONDS = 10.0
LEASE_EXPIRY_SECONDS = 30.0
# A server's graceful exit before it is killed; 2 s held on a loaded hosted Windows runner except once in
# 200 (docs/research/2026-09-25-the-clean-run-after-the-audit-fixes.md).
GRACEFUL_CLEANUP_SECONDS = 2.0
# The first recovery retry beat; it doubles up to the ceiling below. No contract depends on the value.
RECOVERY_RETRY_SECONDS = 0.05
# Where the recovery beat stops doubling. A cleanup that is stuck is stuck;
# past this the retries cost more than they can win back.
RECOVERY_RETRY_CEILING_SECONDS = 2.0
# Windows handles and temporary names held for cleanup at once, and incomplete startups in the
# module registry: one per start is normal; the bounds refuse a leak. Values predate measurement.
MAX_PENDING_CHILD_HANDLES = 8
MAX_PENDING_TEMP_NAMES = 1
MAX_STARTUP_CLEANUP_OWNERS = 8
# A server launch command: every pinned profile builds at most four arguments (node, server,
# one flag, one owner path; lsp_server_profile.launch_command). 64 arguments and 64 KiB refuse a malformed command before exec; both stay far
# under ARG_MAX. Values predate measurement; review when a profile needs more.
MAX_GENERATION_LAUNCH_ARGUMENTS = 64
MAX_GENERATION_LAUNCH_BYTES = 64 * 1024
WINDOWS_LEASE_RETRY_ERRORS = frozenset({5, 32, 33})
# Pause between retries of a Windows lease write refused by a sharing error (5, 32, 33), bounded by the lease expiry.
WINDOWS_LEASE_RETRY_SECONDS = 0.01
CLEANUP_STEPS = (
    "tree_termination",
    "tree_release",
    "protocol_stop",
    "generation_joins",
    "heartbeat_join",
    "recovery_join",
    "lease_removal",
    "evidence",
    "scratch_removal",
    "owner_handles",
)
MAX_RETAINED_CLEANUP_ERRORS = len(CLEANUP_STEPS)
