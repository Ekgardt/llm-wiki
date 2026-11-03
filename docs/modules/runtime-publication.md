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
