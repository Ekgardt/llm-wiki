# Pinned Pyright configuration data

`llm_wiki.pyright_configuration` owns ten existing package/configuration/schema
constants and five freeze/thaw functions. It uses only stdlib and canonical JSON
identity. It neither probes an executable nor reads settings, installs packages,
starts a process or acquires runtime ownership. Values and bodies are unchanged.
The configured Pyright release remains 1.1.411; this is not an upgrade claim.

Nested configuration mappings/lists remain immutable and thawing creates fresh
mutable values. The existing restricted thaw domain is preserved, including
rejection of floats and unsupported values. Canonical configuration and initialize
hashes remain byte-identical. General language-profile freezing has a distinct
accepted domain and is not substituted for this implementation.

lsp_profiles imports data here directly, removing its dependency on discovery.
pyright_profile retains the public aliases for installers, discovery, fixtures
and external callers. Its runtime functions still resolve its own bindings, so
existing digest/integrity fault injection keeps testing actual installation checks.
The thaw function preserves its pyright_profile serialization identity. Aliases
can be removed only when all supported consumers and serialized names migrate.

Two original immutability tests moved intact; manifest and dataclass equivalence
checks remain integrated. Focused guards verify isolated import, fixed hashes,
independent mutable copies and the restricted value domain. Run
`pytest tests/modules/pyright_configuration tests/test_pyright_profile.py
 tests/test_language_registry_has_no_discovery_dependency.py -q`. Installer and
session suites remain required integration checks. The complete owner packet
includes canonical implementation/guide, instructions, bootstrap and measurement.
