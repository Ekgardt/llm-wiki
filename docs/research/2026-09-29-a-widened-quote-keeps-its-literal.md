# A widened quote keeps its literal

Date: 2026-09-29. Runtime: Python 3.14.6, LLM Wiki 5.0.0.

A real recovery pass rejected its plan with `claim evidence literal hash does
not match` after reporting that a quoted fragment was widened to its source
line. A representative fixture reproduces the same failure: the binding names
the whole line's byte range and hash, while `_derived_claim` copies the original
fragment into the claim and evidence text. The critic receives the same
inconsistent text/hash pair even when the operation carries no claims.

The immutable snapshot is the authority. Its binding now carries the exact
literal alongside the existing hash and reference. Claim derivation and critic
input use that literal. Receipt serialization selects its existing declared
fields explicitly; the internal literal does not change either receipt schema.
Existing normalized plans containing partial quotes remain valid, and repeated
validation is stable. The unused binding-only wrapper is removed.

Alternatives: rejecting all partial quotes would undo the supported whole-line
expansion; changing only the hash would disagree with the authoritative byte
range; rewriting persisted plan content would reject already normalized plans.
Passing the verified literal fixes both consumers without a migration, another
source read, a model request, or weaker acceptance checks.

Primary references checked on the date above:

- [Python hashlib](https://docs.python.org/3/library/hashlib.html): the digest
  depends on the bytes supplied to the hash operation.
- [W3C Text Quote Selector](https://www.w3.org/TR/annotation-model/#text-quote-selector):
  exact quoted text identifies the selected source text. This project retains
  its own byte-based reference format rather than adopting character offsets.
- [RFC 8785](https://www.rfc-editor.org/rfc/rfc8785): hashing requires a stable
  representation; string data must survive serialization. No canonicalization
  algorithm or library is changed here.

Regression coverage: two partial-quote claims fail on the prior implementation
at the real literal validator; a claim-free critic input also exposes the old
hash/text mismatch. Publication tests cover both complete and partial quotes
through the page ledger and claim index. Existing partial-quote normalized-plan
acceptance and receipt tests remain in force.
