# Language-server install manifest values

Under the approved decomposition mandate, the deterministic
install-manifest builder and validator move from lsp_identity to
llm_wiki.lsp_install_manifest. The existing constants, class and function bodies
move unchanged. lsp_identity keeps direct aliases, including private helpers;
ManifestError keeps its public module identity. Installers, discovery and session
callers keep their current imports and bindings. No installer behavior, pin,
manifest schema, error code or runtime path changes.

The owner constructs and validates values against a supplied LanguageServerProfile.
It does not read files, probe Node, discover a repository, verify actual artifact
bytes or qualify an installation. In particular, acceptance of a manifest value
is not proof of file integrity; the caller's digest comparison remains required.
The complete generic profile and canonical serialization implementations stay in
its packet. Registry/discovery/installer integration remains in existing tests;
new isolated checks compare all pinned-profile outputs with a recorded pre-move
baseline and exercise malformed shapes and field mismatch codes. The baseline is
test evidence, never a new source of production pins.

## Research — 2026-11-04

Primary sources checked: [RFC8259](https://www.rfc-editor.org/info/rfc8259/),
[Python JSON](https://docs.python.org/3.14/library/json.html), and
[pytest integration](https://docs.pytest.org/en/stable/explanation/goodpractices.html).
Keep the existing canonical encoding, closed shape and error ordering; substituting
a JSON library/schema engine changes acceptance behavior without helping the
boundary. Lazy importing discovery delays coupling, while duplicating the builder
in the installer creates competing authority. One value owner with direct aliases
preserves callers and allows isolated qualification. No technology upgrade is
part of this transfer.

All original wiring assertions remain. Required gates are pre/post baseline and
AST equality, facade identity, isolated dependency imports, related installer and
session regression, actual complexity/shape, full packet <=100000 o200k_base tokens
and byte-exact reconstruction. A successful value test is not native execution
qualification or whole-project completion. Qualification status is recorded separately from this boundary contract.

## Canonical structural contract

[Language-server install manifest values](lsp-install-manifest.md) owns
the pure builder/validator boundary. Discovery retains filesystem and
executable qualification; old imports remain direct aliases after the transfer.
