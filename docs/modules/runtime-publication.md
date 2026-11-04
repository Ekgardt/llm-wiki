# Runtime file publication

`llm_wiki.runtime_publication` owns existing sibling publication/adoption/duplicate
recognition, staging, replacement evidence, permission verification and read-back.
Dependencies are runtime contracts/environment/records/sync, canonical hashing,
the existing positive-integer validator and lazy Windows native/ACL boundaries.
There is no import of reliable_memory, queue or Markdown transaction executors.

Creation remains exclusive. Replacement requires current identity plus digest.
The private staged file is hardened before publication; published permission drift
is refused rather than repaired. Existing surviving-stage/adoption behavior and
failure cleanup are preserved. A failed permission-hardening step cannot publish
its staged bytes. Windows move flags and POSIX file/directory sync order do not
change; every durability refusal remains visible. This is single-file publication,
not a transaction over an arbitrary set of files, and it supplies no admission.

Old public exports and callable pickle paths remain identical. Fault injection
patches the binding actually used here, including imported sync/hardening helpers;
patching the former facade cannot instrument an internal call anymore. Existing
storage-failure recovery, ACL and transaction integration tests remain active.
Run `uv run pytest tests/modules/runtime_publication -q`.

## Canonical ownership reference

Shared canonical contract: [runtime-contracts](runtime-contracts.md).
The complete guide is included in this owner's maintenance packet.

## State consumer — 2026-11-04

`memory_state` imports the identical `durable_publish_file` object directly from
this owner and `fsync_directory` from runtime_sync. Local names remain available
for existing callers and fault injection. Function bodies, root resolution and
reload semantics stay unchanged. This removes the reliable_memory import without
adding a forwarding layer. Research: [Python import bindings](https://docs.python.org/3.14/reference/import.html),
[pytest monkeypatch](https://docs.pytest.org/en/stable/how-to/monkeypatch.html),
[Google import guidance](https://google.github.io/styleguide/pyguide.html#22-imports).
Preserving the existing local bindings takes precedence over switching to module-qualified
call sites. Regression verifies fresh-process import, object identity and a real
atomic write; full consumer tests retain lock, error and permission checks.

## Complete lock writes — 2026-11-04

`write_all` is the identical existing `_write_all` function, now a public entry
point for blocking descriptor consumers. It retries partial writes and refuses
zero progress. State, compile fallback, nightly marker, claims token/preparation
and install lock preparation share this implementation. State creation closes
its descriptor on write failure; other owners retain their existing finally
blocks. Partial failed files remain subject to existing recovery, with no unsafe
unlink of a replacement file. This guarantees complete accepted bytes on return,
not fsync or atomic publication of the lock. Existing deadlines, flags and formats
are unchanged. Other deadline-aware transport loops keep their own responsibilities.

Evidence and alternatives: [Python raw writes](https://docs.python.org/3.14/library/os.html#os.write),
[Linux write semantics](https://man7.org/linux/man-pages/man2/write.2.html),
[Microsoft WriteFile](https://learn.microsoft.com/en-us/windows/win32/api/fileapi/nf-fileapi-writefile).
Buffered IO could also retry, but would add another mechanism where a checked
descriptor writer already exists. Generic integration tests inject short, zero
and failing writes, verify descriptor closure and native file-size-limit refusal.
The historical helper and its implementation remain unchanged.
