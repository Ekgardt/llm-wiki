# Proposal: a compile receipt preserves its source context

Date: 2026-10-03. Status: proposed, not approved or installed.

## Decision requested

Approve a new `compile-receipt/v4` under the existing private
`knowledge/daily/receipts/` directory. A receipt will preserve the original daily
SHA-256 and original byte count, together with the absolute bounds of its work
part. Existing v3 receipts, their schema, paths, and historical authority remain
unchanged. No environment variable, runtime root, database, daemon, MCP tool,
source-text rewrite, or larger quote budget is proposed.

Plain-language decision wording: “Yes, preserve the original diary checksum,
its size, and the processed part's boundaries in a new version of compile
receipts. Keep old receipts and make additions to a diary remain compatible.”

This changes persisted structure. AGENTS.md requires explicit sign-off before
code, then a private decision page and an update to `docs/STRUCTURE.md`.
No v4 schema or implementation has been written.

## Reproduced problem and relevant evidence

A long entry can continue into a work part without its original clock heading.
The installed binder refuses a real quote as `bound in 0 of 1 part(s)` and the
model lacks that entry clock. Two isolated regressions fail against unchanged
installed code. On the immutable October 2 daily, ten of 990 actual work parts
lack local headings; all ten have a verifiable original entry identifier.

The in-memory candidate retains one shared original byte representation and
one shared entry map. It binds physical citations while retaining part-specific
receipts. Seven qualification cases pass, but an eighth correctly remains red:
after a real committed v3 receipt, changing an earlier original heading leaves
the continuation digest unchanged and early selection silently skips it.

v3 receipts discard the citation reference from receipt evidence. Their strict
schema stores no physical context. Cache identity is checked too late to fix
early selection. Current page contents are not the original committed image;
undo images may be pruned. Neither provides a durable substitute.

Fresh built-in navigation and source reads cover compilation, early selection,
archive authority, the shared evidence resolver, and doctor's staged receipt
checks. Partial graph answers remain explicitly partial; source verification
supplements them. Public research includes no private transcript, project name,
or live-process identifier. Detailed synthetic/read-only evidence stays in
private logs.

## Current primary research and alternatives

Primary sources checked on the research date:

1. [W3C Web Annotation Data Model, selectors and state](https://www.w3.org/TR/annotation-model/):
   byte-position selection must identify its source representation; a changed
   representation can invalidate a positional citation. Keep the existing
   strict SHA-256, quote, declared-block, and byte-span proof. This is a relevant
   design comparison, not a claim that this product implements JSON-LD.
2. [JSON Schema, closed objects](https://json-schema.org/understanding-json-schema/reference/object):
   `additionalProperties: false` rejects unknown fields. Extending the strict
   v3 record in place would break old readers. Use an explicit new version and
   retain exact v3 validation.
3. [IETF RFC 6709, versioning](https://www.rfc-editor.org/rfc/rfc6709.html#section-4.1):
   version changes need defined compatibility or clean failure, plus actual
   interoperability tests. It is an Informational protocol-design document,
   not a mandate for this local file format.
4. [SQLite isolation](https://www.sqlite.org/isolation.html): publication and
   committed visibility remain transactional. Source context must be part of
   the authoritative receipt's identity and transaction, not a disposable cache.

Considered alternatives:

- Synthetic headings change physical bytes and would falsify provenance.
- Accepting headerless evidence or skipping strict resolution hides the defect.
- Larger work parts change archive partition compatibility and packing; the
  measured part-line sizes do not justify changing the existing ledger budget.
- Cache-only context, current pages, or retained undo images cannot survive all
  valid cleanup and later updates as authoritative evidence.
- A new global writer restriction or inferred transaction-hash chain adds scope
  and cannot safely characterize every external append or authorized repair.
- An explicit v4 receipt binds durable context without changing authoritative
  source text. This is the selected proposal, subject to sign-off and tests.

## Exact proposed persisted contract

The v4 source descriptor retains logical path, selected part SHA-256, part byte
size, and existing occurrence bounds. It additionally preserves:

```json
{
  "original_sha256": "<SHA-256 of the immutable daily snapshot>",
  "original_byte_size": 21694,
  "byte_start": 16384,
  "byte_end": 21694
}
```

The numbers are illustrative, not limits. Bounds are the actual existing
DailySnapshot bounds. They must satisfy start < end <= original size, match
the selected part byte size, and hash to its recorded part SHA-256. Existing
read/resource budgets continue to apply; no arbitrary new cap is introduced.

v4 source identity binds logical path, part digest, original digest and size,
and absolute bounds through restricted canonical JSON and SHA-256. Its filename
is `v4-<v4-source-identity>.md` in the existing receipt directory. The v3 identity
algorithm and `v3-<source-identity>.md` filenames do not change. Different source
contexts can therefore receive distinct create-only v4 receipts without editing
the old receipt. The complete v4 batch manifest and operation identity bind
those descriptors, including direct application with a supplied action key.

Citation syntax remains the existing EvidenceRef: physical original digest,
declared block, and absolute quote span. Binding and later claim validation
must verify the whole physical line and containment in the selected work part.
No artificial header is inserted and no citation may reach an adjacent part.

## Selection, append compatibility, and legacy handling

All early skip paths must use context-aware authoritative selection: the
pre-snapshot `daily_is_compiled` callers, `_daily_parts`' compiled callback,
source selection, post-commit bookkeeping, and archive eligibility. Keeping
`read_compile_receipt_v3` as a historical parser is permitted; treating its
boolean presence as sufficient new-context completion is not.

A v4 receipt permits skipping an unchanged part only after its committed
transaction authority and receipt bytes validate and the current daily has at
least the saved original byte count. Hash exactly that saved-length current
prefix and compare it with the saved original SHA-256; verify the saved part
bounds and part digest inside that same prefix. A benign append preserves that
proof and does not force reprocessing unchanged parts. New or changed part
bytes still require work, as they do today. A shortened or changed prefix does
not prove current completion and requires new processing under a distinct
context-bound identity; old evidence remains historical, never rewritten.

Legacy v3 lacks the original-context proof. The initial conservative rule is
explicitly `unverified_context`, not `compiled` for the new context-aware path.
Do not manufacture missing hashes from current bytes or automatically convert
an old receipt into a newly authoritative one. Qualification must measure the
legacy reprocessing workload and token cost. Any narrower reuse rule must be
proved from retained authoritative evidence and included in the reviewed plan;
it cannot be improvised during installation. Unknown evidence remains retained
and prevents source deletion. This proposal does not declare historical losses
or source failures repaired.

Discovery of multiple immutable v4 contexts must use one bounded-by-deadline
directory pass with an in-memory grouping by logical part identity, not a new
persistent index and not repeated whole-directory scans per part. Invalid or
incomplete records remain visible and fail closed. The lookup grouping is
derived and cannot replace full receipt/transaction validation.

## Compatibility and installation

Old v3 readers remain unchanged and old v3 records remain readable. New readers
dispatch explicitly by record version and validate the appropriate identity,
schema, and committed authority. Unsupported versions produce a visible refusal.
All archive and doctor consumers must be upgraded together; the strict old
parser must never silently interpret v4 as v3.

An already-running compiler owns a snapshot and loaded old code. Do not stop it,
alter its private sources, replace its receipts, or assume a source update has
changed its loaded reader. Before v4 publication, prove its natural completion
and inventory other live/unknown old consumers using existing ownership and
installation evidence. Reader-first qualification and byte-identical installed
guards precede enabling the new writer through the normal reviewed installation;
no hidden feature flag or bypass is proposed. If old reader readiness cannot be
proved, defer writer activation and report that fact.

Archive validation must dispatch embedded receipt versions and verify v4 context
against the immutable payload prefix and part bounds before retiring any daily.
Existing archive manifests are retained. Their current reference fields are
version-neutral strings/digests, so prefer preserving v1/v2 manifest formats;
the actual compatibility guards must prove this before choosing not to version
the manifest. Existing bag authority, transaction checks, and retention fences
remain unchanged. Old live readers that cannot read new embedded receipts must
be accounted for before publication/deletion, not discovered after source loss.

Canonical fenced installation retains exact preimages and afterimage hashes.
Runtime source failures, unresolved intents, queued work, undo evidence, and live
owners are preserved. Rollback after any v4 publication requires v4-capable
readers; reverting to v3-only source and claiming full compatibility is unsafe.

## Planned files and required qualification

Code/schema scope, only after approval:

- `scripts/compile_memory.py`: shared original context, context-aware early
  selection, v4 identities/receipt writer/read dispatch, cache identity, all
  completion predicates and bookkeeping.
- `scripts/schemas/compile-receipt-v4.json`: separate strict versioned record.
- `scripts/archive_daily.py`: authoritative version dispatch and context-aware
  archive eligibility.
- `scripts/evidence_resolver.py`: embedded v4 receipt authority and source-prefix
  checks while preserving physical citation and old bag support.
- `scripts/doctor.py`: staged/committed version dispatch and honest unknown
  legacy-context diagnostics.
- Related existing schema/compiler/archive/doctor tests plus
  `tests/test_a_continuation_keeps_its_original_entry.py` and focused version
  interoperability guards.
- `docs/STRUCTURE.md`, byte-identical `AGENTS.md` and `CLAUDE.md`, `CHANGELOG.md`,
  this research record, and a private accepted decision page after sign-off.

Qualification must include genuine before/after continuation binding, physical
line rejection, immutable receipt authority, normal append reuse, changed-prefix
refusal, same-part different-context create-only receipts, old v3 readers and
archives, mixed-version diagnostics, missing legacy proof, adopted installed
transactions, snapshot/model/critique/publication/reader cycles, interruption
and rollback evidence, actual callable Lizard/AST checks, and fresh built-in
navigation after installation. No tests run in the live vault checkout.

The existing controlled full-cycle evidence, which does not fix early skip,
uses identical synthetic source and provider responses. Old code makes three
draft attempts and publishes nothing; the candidate makes one draft and one
critique, publishes a real page and v3 receipt, and resolves its citation.
The product's conservative estimates total 33,312 input plus 1,527 output tokens
before, and 12,924 input plus 612 output after. These are estimated counts, not
provider billing or live-model quality; there are zero real model calls. Failed
old execution is shorter than successful publication, so no speed improvement
is claimed. v4 adoption and legacy reprocessing cost remain unmeasured and must
be evaluated separately before claiming efficiency or completion.

## Cleanup conditions

Remove replaced in-memory helpers and stale v3-only active writer/selection paths
after v4 qualification. Keep explicit v3 historical readers/schema for retained
receipts, bags, rollback evidence, and proven old consumers. Their removal
condition is a verified inventory proving no such consumer or retained reference
requires them, with preserved source data and a reviewed migration; this proposal
does not authorize deleting immutable historical evidence to reach that condition.
Only obsolete derived code generations and verified owned qualification scratch
are eligible for ordinary cleanup. All private/runtime deletion guards remain.
