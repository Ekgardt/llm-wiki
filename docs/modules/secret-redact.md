# Shared secret masking

`secret_redact.py` remains the single stdlib-only owner of best-effort redaction
for strings and structured values, error descriptions and scoped caches.
No production source, marker, regular expression, token entropy rule, cache scope
or public import changes in this qualification. DLP authorization stays with the
model boundary; this redactor does not claim exhaustive secret detection.

Credential-key matching distinguishes actual values from code, references and
noncredential command arguments. JSON, quoted and command-line shapes preserve
the established markers and valid surrounding syntax where the existing contract
requires it. Assignment candidate indexing must produce exactly the original
pattern substitutions. The scoped cache is an optimization only: different input
or prerequisite changes must not reuse an invalid redaction.

The packet contains the complete production module and focused redaction, command/line-cache test files and relocated pure tests,
including seeded equivalence and long-line behavior tests. The unrelated provider
timeout-environment check remains active in the provider-timeout suite. Mixed
DLP, compile, event-envelope and capture tests remain integration obligations;
module qualification does not claim those subsystems have been decomposed.

Run the manifest's focused test files and `tests/modules/secret_redact` with pytest.
Import guards refuse dependencies on other product modules. See
`../research/2026-11-03-redaction-and-repository-scope-boundaries.md`.
