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
