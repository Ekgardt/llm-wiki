# Windows filesystem handles

`scripts/windows_workspace.py` is the existing independent owner of Windows
handle-relative filesystem operations. It imports only the standard library.
No source move, compatibility alias, new abstraction or behavior change is needed.
The native ctypes declarations and API loader remain here; queue process identity
and ACL policy retain their separate owners. Consumers include reliable IO, LSP
ownership/security, doctor and installers. They retain their public import paths.

`capability` reports availability; `require_capability` refuses unsupported native
operations. Open/create methods return handles the caller must close. Directory
walking retains parent handles; reparse points and unsupported path components
are refused. Identity includes volume/file identity and expected kind. Local
absolute paths retain UTF-16 bounds and sharing modes. Reading/writing, file size,
readonly attributes, flushing, create-only publication, replacement and deletion
retain the existing error and durability contracts. This is a filesystem boundary,
not application admission, transaction recovery or an OS sandbox.

The complete source, original focused tests, import guards and common pytest
bootstrap belong in the context packet. Run `uv run pytest tests/test_windows_workspace.py
tests/modules/windows_workspace -q`. Windows native tests skipped on Linux remain
unverified on this host; an import guard is not a native platform qualification.
No dead implementation is introduced or retained by this in-place qualification.
