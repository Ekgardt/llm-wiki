# Compiled prose uses the reader's evidence contract

Reviewed 2026-09-29, on v5.0.0 at 9a32316c.

The live structural lint found six freshly compiled pages whose model-written
body contained abbreviated `daily:<date> <time>` citations. The renderer had
also appended valid digest- and byte-bound citations. Validating the structured
evidence array and claim ledger therefore did not validate the page readers
would receive. Eight regression cases reproduced acceptance of malformed prose
or a canonical reference to a source absent from the compile snapshot.

The shared semantic validator now renders the proposed page, parses references
with the existing `extract_evidence_references`, and resolves them against the
immutable input snapshot. Claim validation reuses that resolution helper and
still checks the literal text and digest separately. Draft, critique, plan
validation, materialization and apply already pass this boundary. Both create
and update operations are covered; saved plans are revalidated before any page
or receipt is published. A valid inline reference survives unchanged.

The draft instruction explains that evidence quotations belong in the array
and the renderer supplies the Evidence and Related sections. Its hash already
participates in the existing compile call identity. This is guidance, not a
substitute for validation. No dependency, schema version, runtime directory,
budget or alternate parser is added. The previous claim-resolution code is
factored into the shared helper, not retained as another implementation.

## Sources and alternatives

- [OWASP input validation](https://cheatsheetseries.owasp.org/cheatsheets/Input_Validation_Cheat_Sheet.html)
  distinguishes syntax from contextual validity and recommends checking external
  inputs before persistence. Here both citation parsing and snapshot resolution
  are necessary.
- [JSON Schema string validation](https://json-schema.org/understanding-json-schema/reference/string)
  provides length, pattern and format constraints. The existing string schema
  cannot establish that an embedded citation names bytes in this snapshot.
- [W3C Web Annotation Data Model](https://www.w3.org/TR/annotation-model/)
  distinguishes quote and position selectors. This supports retaining the
  project's existing source identity, byte span and literal checks rather than
  treating a timestamp as equivalent evidence.

These are independent primary sources, checked on the review date. This change
repairs an existing validation boundary; it does not replace the evidence
format with W3C annotations or upgrade any library.

Prompt-only prevention leaves malformed saved plans valid. Teaching lint to
ignore malformed candidates hides the producer defect. Silently stripping or
rewriting model prose can change the author's claim or select ambiguous source
bytes. A second citation regex can drift from the reader. Reusing the reader
and snapshot resolver avoids those alternatives. The cost is another bounded
render/parse during validation; no LLM call or live-source scan is needed.

Previously published pages require a separate, evidence-backed repair. This
guard neither rewrites their history nor declares their findings resolved.
