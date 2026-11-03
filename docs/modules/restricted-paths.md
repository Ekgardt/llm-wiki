# Restricted logical POSIX paths

llm_wiki.restricted_paths validates an existing caller-supplied path string and
allowed-root tuple. It rejects empty/non-text values, backslashes, repeated slashes,
drive-qualified paths, absolute or non-normalized forms, traversal and paths outside
the allowed roots. Root membership uses path components, not a string prefix;
matching is POSIX and case-sensitive. The exact allowed root is permitted.

The result is a PurePosixPath, not filesystem evidence. Symlink containment, inode
identity, file type, permissions and transaction admission belong to IO callers.
No path is opened, resolved against disk, created or deleted here. This policy is
separate from maintenance-bundle metadata names, which are in-memory map keys.

All six existing functions and errors retain their bodies. reliable_memory exposes
identity aliases; the public function retains its historical pickle name. Existing
pure tests move intact; archive/queue/transaction/generation consumers remain covered
by their integration suites. No new dependency, normalization or limit is introduced.

Run `uv run pytest tests/modules/restricted_paths -q -s`.
See ../research/2026-11-03-path-and-pragma-boundaries.md.
