# Windows permissions: remove the dependency of basic IO on the queue and transactions

Research date: 2026-11-02. Status: both sequential steps were explicitly approved by the user.
Original version: dd3ad044. This is the next boundary of the overall decomposition plan.

## The confirmed cause of the coupling

reliable_memory._harden_runtime_owner_only imports queue._harden_owner_only,
which on Windows imports transaction._harden_windows_acl.
reliable_memory._require_windows_owner_only imports queue._is_owner_only,
and the Windows check in the queue imports transaction helpers again.
One more strict Windows verifier in reliable_memory imports transaction directly.
So the basic runtime IO depends on executors that themselves use it.
This is a confirmed dependency structure, not a claim of an inevitable import crash.

The ACL code uses _windows_kernel32 for the console code page. The same loader is needed
by operations on file handles, and its prototypes refer to ctypes structures.
A plain move of the ACL code with a reverse import of transaction would not remove the original coupling.

## Two sequential moves

1. scripts/llm_wiki/windows_native.py receives from markdown_transaction
   _WindowsFileInformation, _WindowsDispositionInformation,
   _WINDOWS_KERNEL32_PROTOTYPES and _windows_kernel32 together with its current lru_cache.
   Dependencies are only ctypes/wintypes and functools. The import does not load kernel32:
   WinDLL is called lazily, as now. The old names in transaction are identity aliases.
   Operations that open, check, delete and close handles stay with transaction.
2. scripts/llm_wiki/windows_acl.py receives 18 transaction ACL functions from
   _windows_acl_identity to _acl_lines_naming and four queue functions:
   _windows_acl_lines, _acl_owner_lines, _acl_is_owner_only, _is_owner_only_windows.
   The new module uses only stdlib and windows_native. The reverse local imports of
   transaction in the moved functions are removed; they call their owner.
   reliable_memory calls the new owner directly in all three Windows paths;
   queue and transaction keep their different POSIX/Windows dispatch semantics.

The exact list of definitions: logs/windows-permissions-inventory.json.
All confirmed production imports of these helper names are switched to the owner
while keeping the old lazy/eager scope; compatibility aliases stay where
there are active consumers. Test substitutions of internal calls move to
the new owner; the test patch of the external dispatch in the queue stays valid.
Do not add a forwarding registry, dynamic substitution of globals or duplicate loaders.
Delete the old definitions after the AST/behaviour comparison.

Each module has its own tests/modules/windows_native or windows_acl,
a guide and a context manifest in docs/modules/windows-native* or windows-acl*.
The full package of each is ≤100000 o200k_base tokens; the numbers are not measured yet,
since the new modules and their own tests have not been created yet.

## What is kept and why

The five kernel32 prototypes, handle sizes, use_last_error=True, the ctypes structures
and the single cache stay the same. Kept: the user name/domain,
the order of codec fallbacks and handling of non-ASCII names, the icacls arguments, the 5 s timeout,
all error texts and the fail-closed results. No new limits are introduced.

The strict verifier requires a single explicit ACE without an inherited marker. The boolean
queue checker has a different existing contract. Do not merge these
predicates and do not silently strengthen/weaken them. POSIX chmod, the unsupported-filesystem
warning, path/symlink/handle checks, transaction fences, runtime roots and the schema
stay the same. Do not change the ACLs of live files to demonstrate the move.

Alternatives: keeping the reverse imports keeps the problem; copying the loader gives
two owners of the ABI/cache; a new framework/pywin32 is an unnecessary dependency and a risk
of behaviour change. Merging all transactional Windows IO with the ACL code would mean
moving publication operations for the sake of a permission check. The choice is two existing
layers: a shared lazy binding, and the ACL check/application on top of it.

## Primary sources checked 2026-11-02

- [Microsoft icacls](https://learn.microsoft.com/en-us/windows-server/administration/windows-commands/icacls):
  grant:r replaces explicit permissions, inheritance removal has separate
  semantics; /verify does not by itself prove owner-only. So the commands and
  the subsequent own check are kept, and inherited/explicit are not mixed.
- [Python ctypes](https://docs.python.org/3.10/library/ctypes.html): the argument types,
  result types and Windows error handling are essential for native calls. Kept:
  restype/argtypes/use_last_error and lazy loading, without platform-dependent imports
  of executable runtime modules. Python 3.10 is unsupported upstream; the project floor does not change.
- [OWASP insecure temporary file](https://community.owasp.org/vulnerabilities/Insecure_Temporary_File):
  temporary names and permissions do not remove the creation race; secure create/open
  is needed separately. So the ACL extraction does not replace the existing filesystem
  containment/atomic-create checks and is not declared a new OS sandbox.

In addition, [Python os.chmod](https://docs.python.org/3.10/library/os.html#os.chmod):
platform limitations differ; POSIX mode bits are not considered a replacement for Windows ACLs.
The sources justify keeping the contracts; they do not prove the absence of all
existing ACL defects. Native Windows validation is separate from Linux mocks.

## Research and qualification

The built-in index dd3ad044 is current, changes:stale=false. The graph revealed the loader callers:
the console codec and five groups of handle operations; the hardener callers: both dispatches and
existing tests; the boolean Windows reader: the queue dispatch and Cyrillic-owner tests.
The graph is incomplete; AST and source search supplement the local imports/monkeypatches.
Evidence: logs/windows-permissions-graph.json.

Baseline: transaction, reliable memory, non-ASCII owner and native handle-width tests
on Linux: 154 passed, 8 skipped, 3.15 s. Evidence: logs/windows-permissions-baseline.txt.
The skips are not declared a native Windows qualification.

After explicit approval: private decision and STRUCTURE before the code; each step separately
with AST comparisons (imports are normalized), a red/green boundary guard, an isolated import,
identity/cache tests, faithful mocked subprocess outcomes and the existing native tests.
Check a Unicode owner, an extra ACE, a malformed/failed command, inherited-vs-explicit,
an unsupported codec, full-width handles and last-error preservation. Consumer migrations
are checked together with the queue, transaction/recovery, reliable_memory and compile_cache.
Each stage: Python 3.10, global CCN/shape, Ruff, CI discovery/weights, the full token
budget; commit/push and refreshing the index. Do not claim native Windows without running it.
A live rollout, rebuilding operational state, changing the security policy and a new
WinAPI surface are not part of the moves being approved.

Additional readers baseline: 5 passed, 0.32s;
logs/windows-permissions-reader-baseline.txt.


## First step: native binding

The private decision and STRUCTURE were recorded before the code. The whole block of the two structures,
the prototypes and the lazy loader was moved byte for byte from dd3ad044; the rest of the transaction AST
matched after excluding imports. The boundary guard first failed on the old owner.
The aliases keep the identity of the types, the prototypes and the single cache.
Own+handle integration:12passed; Python3.10 own+handles+Unicode/readers:19passed.
Initial package:69845 o200k_base tokens/23 files. Native Windows was not run.
Evidence: logs/windows-native-preservation.json; logs/windows-native-boundary-red.txt;
logs/windows-native-own.txt; logs/windows-native-python310.txt;
logs/windows-native-related.txt/.xml; logs/windows-native-final-budget.txt.
The second step, ACL, was not yet implemented at the time of this entry.

Native binding expanded:325passed,8skipped,58.85s; it includes transaction/recovery,
reliable memory, Unicode readers, global CCN/shape, structure and CI policy.
Ruff and diff checks are clean. Skips are not proof of the native platform.

## Second step: ACL owner

22 functions were moved from transaction/queue; their AST matches after removing
the reverse imports. The other bodies of transaction, queue, archive, compile_cache,
evidence_resolver and LSP matched without imports. In reliable_memory the comparison
additionally normalizes exactly two approved redirections of Windows-only
calls: hardening and boolean verification; the strict verifier changes only its import.

The boundary guard first caught the old definitions and the reverse imports of basic IO.
After the move the owner depends only on stdlib/windows_native, and runtime IO does not
import queue/transaction. The old function aliases were checked for identity;
substitutions of internal ACL calls moved to the owner, and the external dispatches did not change.
The standalone non-ASCII test stays at its old path (its fixture is used by
integration tests), imports the owner and is part of the full ACL package.

First selected set:48passed/1skipped. Expanded:637passed/11skipped,69.75s,
including transaction/recovery, queue/migration, reliable memory, archives, cache,
evidence resolver, global CCN/shape, structure and CI. Python3.10 selected:
119passed/4skipped,2.28s. The added direct runtime bridge tests separately:
19passed/1skipped; their CCN/shape were checked separately after they were added.
LSP ACL/owner selection:1passed/2skipped/254deselected; this is not the whole LSP suite.
Windows native validation was not carried out. A skip is not a platform success.

Evidence: logs/windows-acl-preservation.json; logs/windows-acl-boundary-red.txt;
logs/windows-acl-own.txt; logs/windows-acl-related.txt/.xml;
logs/windows-acl-python310.txt; logs/windows-acl-runtime-bridge.txt/.xml;
logs/windows-acl-lsp.txt; logs/windows-acl-final-budget.txt.
Neither live ACLs, nor the runtime layout/schema, nor the POSIX policy were changed.
The full regression suite, public CI and a live rollout are not claimed.
