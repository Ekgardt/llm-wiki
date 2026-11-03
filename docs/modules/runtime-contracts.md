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
