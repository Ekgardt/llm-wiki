# Windows native declarations

`llm_wiki.windows_native` owns the existing two ctypes structures, five kernel32
prototypes and one cached lazy loader. It imports only stdlib; importing it does
not load kernel32. The original use_last_error=True, restype/argtypes and complete
handle widths remain intact. The module adds no API or behavior.

`markdown_transaction` retains handle open/identity/delete/close operations and
reexports the same objects. Those aliases preserve the current call surface and
cache identity; there is no second loader. ACL can depend on this binding without
importing the transaction executor. Runtime state and permissions are unchanged.

Own tests verify declared signatures, exact structure fields, pointer-width handle,
lazy single-cache loading, last-error request and isolated import without a DLL.
Integration in test_windows_handles_keep_their_width_and_their_error checks real
consumer error propagation through a mocked OS boundary. Linux mocks are not native
Windows qualification. Own tests: `pytest tests/modules/windows_native -q`.
The context manifest includes source, all own tests and required docs/instructions;
its ceiling is 100000 o200k_base tokens.

Evidence: ../research/2026-11-02-windows-permissions-boundaries.md.

## Windows native binding and ACL ownership (approved 2026-11-02)

`scripts/llm_wiki/windows_native.py` owns the existing two ctypes structures,
five kernel32 prototypes and single lazy cached loader. Handle operations remain
in markdown_transaction and use identical imported objects. Importing the owner
does not load a Windows DLL. `scripts/llm_wiki/windows_acl.py` owns the existing
ACL commands, identity/codec parsing and strict/boolean checks from transaction
and queue, depending only on stdlib and windows_native. Runtime filesystem owners reach
these lower modules without importing queue or transaction for Windows IO.
POSIX behavior, explicit-versus-inherited verification differences, commands,
timeouts, native signatures and error policy are preserved. Both modules have
own tests and complete ≤100,000-token context manifests under tests/modules and
docs/modules. No new runtime path, WinAPI surface or security policy is added.
Approval: `knowledge/notes/windows-permissions-boundaries-decision-20261102.md`.

`loaded_module_filename(handle)` resolves a module already loaded by the current
process through GetModuleFileNameW. The handle is borrowed: this helper neither
loads nor unloads that module. Zero has the native API meaning of current
executable; the compile-runtime caller separately refuses a missing/zero Python
DLL handle. A Unicode buffer grows on truncation; native errors propagate. The
initial MAX_PATH-sized allocation is not a limit on accepted paths.
Source: [Windows Python library identity](../research/2026-11-03-windows-python-library-identity.md).
