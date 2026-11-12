# Windows ACL ownership

`llm_wiki.windows_acl` owns the 18 existing transaction ACL helpers and four
queue Windows ACL helpers. It depends only on stdlib and windows_native. Import
and pure parsing do not load a DLL, launch icacls or import runtime executors.
Explicit operations retain the existing command arguments and five-second timeout.

Strict verification requires one explicit full-control entry for the exact owner;
the queue's boolean check preserves its previous handling of inherited entries.
They intentionally remain distinct contracts. Names, codec fallbacks, errors and
fail-closed results are unchanged. This module does not replace path containment,
atomic creation, handle identity or transaction fencing, and is not an OS sandbox.

runtime_environment and runtime_publication directly call the Windows ACL owner from their Windows
permission paths. POSIX chmod/unsupported warnings stay unchanged. Queue and
transaction retain their platform dispatch and expose identical compatibility
functions. Archives, evidence resolver, compile cache and LSP import the relevant
functions from their owner. There is no reverse executor import or copied policy.

Own tests cover strict/boolean behavior, command/error/timeout contracts, console
codec fallback and isolated import. The existing non-ASCII owner test is included
in the packet and now calls this owner. Cross-boundary tests verify publication
and runtime refusal through the original APIs. Internal monkeypatches follow the
new owner; outer dispatch patches remain at their consumer. Linux simulations do
not qualify native Windows behavior. No production permissions are changed here.

Run `pytest tests/modules/windows_acl tests/test_a_windows_owner_with_a_non_ascii_name_is_verified.py -q`.
The full context manifest includes all own tests, source dependencies, instructions
and documentation and must stay within 100000 o200k_base tokens.
Evidence: ../research/2026-11-02-windows-permissions-boundaries.md.

## Windows native binding and ACL ownership (approved 2026-11-02)

Shared canonical contract: [windows-native](windows-native.md).
The complete guide is included in this owner's maintenance packet.
