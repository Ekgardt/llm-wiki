# Runtime values and errors

`llm_wiki.runtime_contracts` owns the unchanged frozen five-field RuntimeFileIdentity,
UnsafeStateRoot (ValueError) and MetadataDurabilityUnavailable (OSError, stable
code). It owns no resource and imports only dataclasses. Public class module names
remain reliable_memory, whose exports are the identical objects; this preserves
existing serialized values and exceptions. Remove aliases only after supported
consumers and serialized records no longer require those paths.

The fixture records protocol-4 bytes captured before extraction. The accompanying
cross-owner compatibility test checks old facade exports and all moved public
callables as well as record/exception instances; it intentionally runs in the full
repository. Its facade contract is reexport identity, not a second implementation.
Isolated import tests separately prove this owner does not import the facade.
Run `uv run pytest tests/modules/runtime_contracts tests/test_runtime_io_compatibility.py -q`.

## Canonical ownership reference

Runtime filesystem decomposition keeps the public `reliable_memory` entry points
as compatibility aliases; SQLite lifecycle belongs to `llm_wiki.sqlite_io`. `llm_wiki.runtime_contracts` owns values/errors;
`runtime_environment` owns locality and permissions; `runtime_records` owns
metadata, reads and file identity; `runtime_sync` owns checked flushes;
`runtime_publication` owns sibling-file staging/publication and replacement proof.
Dependencies flow from publication to the lower owners without reverse imports.
SQLite metadata validation still avoids opening/closing a live database inode.
See `research/2026-11-03-runtime-io-boundaries.md`.
`llm_wiki.runtime_fingerprints` separately owns offline two-pass fingerprints and
their streaming/deadline policy. `runtime_records` supplies metadata/read/identity
only; publication does not depend on fingerprinting. This removes an unnecessary
source dependency while retaining all required context in each measured packet.
