# Runtime filesystem responsibilities

2026-11-03, approved completion mandate; baseline f19217d3. Split the existing
reliable_memory filesystem responsibilities into five one-way owners under
scripts/llm_wiki, preserving exact function/class bodies and current algorithms:

- runtime_contracts: UnsafeStateRoot, MetadataDurabilityUnavailable and
  RuntimeFileIdentity. Preserve public identity and pickle path reliable_memory.
- runtime_environment: network filesystem/mount detection, Windows reparse
  detection, local-root refusal, cloud-name warning and owner permission checks
  and hardening. Retain existing lazy Windows ACL calls and platform probes.
- runtime_records: containment/metadata checks, bounded reads, stable fingerprints
  and identities. SQLite-file validation remains metadata-only: never open and
  close a descriptor on an inode whose process may hold SQLite advisory locks.
- runtime_sync: file/directory sync, platform branches and metadata-durability
  error conversion. Failed directory durability remains an error.
- runtime_publication: sibling-file publication, adoption/duplicate recognition,
  replacement evidence, staging, permission verification and read-back. Its
  dependencies point to records/environment/sync/contracts, never the executor.

reliable_memory retains SQLite connection lifecycle, locking probes, journal
replay and state-root preparation/orchestration. It reexports old entry points
as identical aliases. Public callable/class pickle identities remain compatible.
The old implementation bodies are removed, not wrapped or copied. Consumers can
continue importing the same names; alias removal requires evidence of migration.

No runtime location, permissions, path syntax, hashes, byte limits, deadlines,
SQL schema, transaction boundary, native API flag or directory layout changes.
Cloud detection is still best-effort; same-volume publication is not a promise
of a transaction spanning multiple files. Existing values and their rationale
comments travel together. The existing positive-integer validator remains shared
with sqlite_contracts; this introduces no SQLite IO into the publisher.

Tests that replace an entire former module binding must patch its actual new
owner. Changing an attribute on a shared stdlib object is different from replacing
the binding itself. Inspect every monkeypatch/patch site and keep fault-injection
assertions and scenario coverage. No proxy forwarding, silent no-op injection,
weakened assertion or production test-only hook is allowed. Verify original ASTs,
public pickle bytes, current consumers, root compatibility and real failure paths.

Research checked today, three independent primary publishers:
- Python 3.10 os: descriptor lifetime, replace/rename, permissions and fsync are
  distinct operations. Retain caller sequencing; do not infer durability from
  rename alone. https://docs.python.org/3.10/library/os.html
- SQLite atomic commit: rollback-journal behavior depends on locking and durable
  flushes. Do not introduce a metadata probe that closes a live database inode.
  https://www.sqlite.org/atomiccommit.html
- Microsoft MoveFileExW: move flags and cross-volume copy have different failure
  semantics. Keep the existing native wrapper and same-parent restrictions.
  https://learn.microsoft.com/en-us/windows/win32/api/winbase/nf-winbase-movefileexw
The existing Python >=3.10 contract remains despite upstream EOL. No new package,
platform promise or filesystem technology is selected. A new storage service or
rewritten transaction API would alter resource ownership unnecessarily; a generic
IO catch-all would preserve the present coupling. The chosen split follows real
resource and policy responsibilities.

Baseline focused reliability/schema/state/fingerprint/adoption-lock tests:
126 passed, 3 platform skips. Fresh indexed call evidence and the full runtime
global-symbol map precede implementation. Evidence: logs/runtime-io-*.

The import guard gains an explicit dependency list; its default still forbids
all other product modules. Two negative/positive tests first failed, then passed;
allowing one declared dependency must not allow its undeclared product imports.
Each runtime layer has a separate lower-layer allowlist; Windows ACL verification
remains an explicit lazy publisher dependency. All 15 initial owner guards failed
before extraction. All 87 moved class/function ASTs and 18 protocol-4 public
pickle forms match the baseline after extraction. Compatibility fixtures belong
under tests/modules/runtime_contracts/fixtures/ and their cross-owner verification
under tests/test_runtime_io_compatibility.py. Isolated owner tests use real files,
metadata, permission and sync failures; the compatibility test deliberately checks
both the old facade and the new owners.

## Measured refinement before the second extraction

The first full publication packet is 101390 tokens, above the user's limit.
It is not qualified. The source dependency audit shows that publication needs
bounded reads and metadata identity, but imports the whole streaming fingerprint
implementation through runtime_records. Refine that real responsibility:
llm_wiki.runtime_fingerprints owns _fingerprint_metadata,
_require_fingerprint_metadata, _digest_runtime_descriptor,
_verified_runtime_digest, fingerprint_runtime_file and fingerprint_regular_file.
It depends on runtime_records metadata/identity, bounded_io and the existing
deadline check. runtime_records no longer imports hashing, chunk policy or
SQLite-deadline helpers; publication therefore no longer needs those source
contracts in its packet. No required context is omitted from any actual dependency.
All six bodies and legacy callable pickle paths stay identical. This separates
normal read validation from explicitly offline, two-pass fingerprinting and adds
no resource, bound, facade or behavior. It follows the same source research and
approved dependency-oriented refinement mandate.

## Qualification

The unchanged 87 bodies/classes and remaining facade ASTs are verified against
the preimage; all 18 captured public pickle byte strings match. The first related
run exposed seven stale injection bindings plus one missing declaration of the
existing lazy ACL dependency. Tests now patch actual lookup owners; the dynamic
write/file-sync/directory-sync fault fixture patches publication. Doctor's direct
facade probes and capture's facade read injection deliberately stay there. The
full patch-site inventory and original tests are retained in logs/runtime-io-*.
No original assertion was removed. The new fingerprint unit test initially used
the wrong tuple element; the established (digest, identity) API was checked and
both returned values are now asserted. No production algorithm changed.

Related runs: 98 passed/4 skipped for publication, permission and storage failures;
295 passed/3 skipped for doctor, locking, schema and recovery; 258 passed/5 skipped
for transactions, queue, adoption and breadcrumbs. Weighted qualification passed
329/7 skipped. Python 3.10 passed 98/3 skipped. All module suites, import/measurement
guards, complexity, structure, CI, limits and privacy passed 925/2 skipped. Ruff
and local CCN/shape passed. All 41 emitted artifacts round-trip to identical paths
and bytes under the unchanged token ceiling after the fingerprint refinement.

Pinned e91402e0 whole regression completed successfully: 15483 passed, 244 skipped,
5 warnings, 1788.49 seconds. It predates this runtime split and the two intervening
test-ownership changes. A new pinned full run is required for the resulting commit.
Windows skips are not native platform qualification. Nothing installed live.
