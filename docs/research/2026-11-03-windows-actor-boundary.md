# Windows actor SID lookup outside the ownership registry

Date: 2026-11-03. Internal boundary under the approved decomposition mandate.

## Existing responsibility

`operational_ownership.py` contains a self-contained Windows token probe:
_WindowsTokenApi plus _windows_token_api, _windows_open_token,
_windows_token_information, _windows_token_sid_text, _validated_windows_actor,
and _windows_actor_identity. These seven definitions use only ctypes, dataclasses
and regular expressions. The registry's current_actor_identity dispatches to the
probe; role/scope actor derivation and process identity remain in the registry.

Move those definitions exactly to `scripts/llm_wiki/windows_actor.py`, with
identity-preserving aliases at the old location. Preserve class/function module
metadata where compatibility requires it. Leave platform dispatch, public actor
names, errors, ownership leases, SQL and process probes unchanged. Retain the
existing monkeypatch seam on operational_ownership._windows_actor_identity.
No new generalized Windows service or second token probe is needed.

## Evidence and tests

The three before-transfer integration files pass56tests. Exact source/ASTs for
all51top-level definitions are retained in logs/windows-actor-before-ast.json.
Direct source search found only internal calls to the six helpers and an existing
platform-dispatch test replacing the old _windows_actor_identity alias. Check the
fresh builtin index before implementation and preserve the unchanged consumers.

Before moving production code, exercise the old definitions through the same
focused tests that will qualify the new module: ABI widths/types, variable token
buffer, error propagation, SID text allocation/free and token close on both
successful and failed probes. Record actual validation outcomes for malformed and
bounded SID inputs. Include a real Windows probe, explicitly skipped on Linux.
After moving, preserve every original top-level definition AST, compare aliases,
run the original ownership suite and all packet/complexity checks.

## Chosen boundary and alternatives

Keep the code in the registry: valid behavior, but native identity cannot be
maintained without importing the large state-owner closure. Put SID lookup in
windows_native: mixes the common kernel32 binding cache with this uncached
advapi32 token lifecycle and is unnecessary for an exact behavior-preserving move.
Replace with username, command output or an environment value: changes identity
semantics and introduces locale/spoofing risks. Rework all platform/process actor
APIs now: exceeds this proven seam and complicates existing patch contracts.

The new packet owns the whole probe and its focused tests; the full original
ownership integration remains an explicit separate qualification obligation.
Aliases have one implementation and remain until actual consumers and serialized
identities can migrate under a compatible release contract. No installation,
state location, dependency, release or new limit. The existing256-byte actor bound
is preserved from ownership semantics, not introduced as a convenience.

## Primary sources checked2026-11-03

- Microsoft OpenProcessToken:
  https://learn.microsoft.com/en-us/windows/win32/api/processthreadsapi/nf-processthreadsapi-openprocesstoken
  — use the process token, propagate native failure and close the returned handle.
- Python3.14 ctypes: https://docs.python.org/3.14/library/ctypes.html — explicit
  native signatures, calling convention and saved last-error state are material;
  an integer alone is not evidence that a pointer-sized handle was preserved.
- Import Linter contract types:
  https://import-linter.readthedocs.io/en/stable/contract_types/ — make forbidden
  dependency directions testable. No new dependency or ignored import is added;
  use the project's checked-source and isolated-import guards.

These sources justify the boundary and ABI obligations, not a claim that Linux
fakes prove native Windows behavior. Native verification remains explicitly tracked.

Fresh builtin evidence before implementation: eb2f6bfb, generation
`generation-18e48de3b9edcd8b-619a9d5a`; one caller each for
_windows_actor_identity and _windows_token_api, eight for current_actor_identity,
six for ownership_actor_identity. Report: logs/windows-actor-graph.json.
