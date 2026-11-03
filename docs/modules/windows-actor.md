# Windows actor SID probe

`scripts/llm_wiki/windows_actor.py` owns the existing process-token lookup:
its frozen native-function record, exact ctypes prototypes, token query buffer,
SID conversion, actor validation and handle cleanup. It depends only on the
standard library and loads Windows DLLs only when the probe is called.

The probe requests TOKEN_QUERY, reads TokenUser using the reported buffer size,
converts the SID with the Unicode API, copies its text and frees that allocation.
The opened token closes even if reading or validating the SID raises. Native
errors propagate; malformed or over-bound SID text retains its prior refusal.
The existing256-byte actor bound belongs to the ownership contract. A successful
probe produces `windows-sid:<SID>`, not a username or authorization decision.

`operational_ownership.py` retains aliases and historical serialized identities.
Its platform dispatch still calls its `_windows_actor_identity` name, preserving
the existing test/integration patch seam. Process identity, role/scope digest,
leases, SQL, admission and registry state remain there. These compatibility names
have one implementation; remove them only after callers and serialized identities
have a compatible migration, not as part of this internal move.

Whole packet: `windows-actor-context.json`. Focused tests first passed against the
original definitions, then against their moved implementation. The saved validation
fixture contains actual pre-transfer outcomes, including the exact256-byte boundary.
Tests cover ABI widths, native errors, buffer sizing, SID allocation/free, handle
closing, stdlib isolation and a real Windows probe. Linux skips the native probe;
its fakes do not establish Windows qualification.

`tests/test_operational_ownership.py`, `test_ownership_actor_is_the_agent.py` and
`test_windows_handles_keep_their_width_and_their_error.py` remain separate full
integration obligations; they are run without weakening their assertions.
No registry implementation is hidden in this packet or declared complete by it.

See [research](../research/2026-11-03-windows-actor-boundary.md).

## Structural reference retained from the overview

[Windows actor identity](windows-actor.md) isolates the existing process
access-token/SID probe from `operational_ownership.py`. The registry keeps the old
aliases and platform dispatch; SQL, leases, process identity and role/scope actor
derivation retain their owners. Exact ctypes signatures, token closing, SID buffer
release, native errors and existing identity bounds are preserved.
