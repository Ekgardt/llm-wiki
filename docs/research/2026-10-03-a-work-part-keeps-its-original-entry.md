# A work part keeps its original entry

Research date: 2026-10-03. This change concerns in-memory compilation inputs,
not a new evidence format, runtime path, environment variable, or queue.

The installed compiler splits a long daily record into lossless work parts.
A continuation can contain complete observations without its original entry
heading. The binder searches headings only in the part and refuses a real quote
as `bound in 0 of 1 part(s)`. The prompt likewise lacks the original clock.
Two regressions reproduce those failures against the unchanged implementation.

Primary sources checked on the research date:

- [W3C Web Annotation Data Model, selectors and state](https://www.w3.org/TR/annotation-model/):
  byte-position selectors address the original representation; exact quotes and
  representation identity matter when a resource changes. This product retains
  its existing SHA-256 and strict physical block/span resolver, rather than
  adopting JSON-LD or claiming W3C conformance.
- [RFC 8259, character encoding](https://www.rfc-editor.org/rfc/rfc8259):
  JSON interchange uses UTF-8. The implementation counts physical byte offsets,
  not decoded character positions; source text remains exact UTF-8 bytes.
- [SQLite isolation](https://www.sqlite.org/isolation.html): committed visibility
  and writer serialization are separate from application-level evidence identity.
  Existing transaction and receipt boundaries remain authoritative; successful
  parsing alone cannot prove publication or receipt scope.

Alternatives considered:

1. Prepending a synthetic heading to every part changes the source bytes and
   breaks physical provenance. Rejected.
2. Accepting a headerless part or skipping strict resolution hides the missing
   context and permits fabricated clocks. Rejected.
3. Enlarging parts until every entry fits changes archive partition compatibility
   and defeats bounded packing. The actual 990-part source measurement shows
   no part line exceeds the existing ledger text budget. Rejected.
4. Retaining the immutable physical daily representation alongside work parts
   preserves the original heading and exact absolute spans without extending
   persisted reference syntax. Selected for qualification.

Each part shares the same immutable `bytes` object for its original daily; no
per-part copy is introduced. The model sees only selected part content plus
derived entry identifiers and part bounds. Those identifiers are metadata, not
invented source lines. Packing measures the complete resulting prompt.

Receipts continue to identify the selected part digest. A citation identifies
the immutable physical daily digest and absolute span through the existing
reference format. Both binding and later claim validation require the span to
lie wholly inside the selected work part. Physical line validation prevents a
split line from being misrepresented as complete. Ambiguous timestamps and
quotes remain fail-closed. Cache identity includes physical context digests and
part bounds; the changed prompt program invalidates old cached drafts.

Tradeoffs: hashing and entry discovery now inspect the already-retained daily
  representation. Full-line evidence crossing a part boundary remains refused;
this change does not authorize incomplete-line claims or remove the ledger
budget. Historical source recovery, archive resolution, actual transactional
publication, token cost, and installation still need explicit qualification.

Qualification so far: two original regressions fail against installed code;
the candidate passes 110 related checks and 172 archive/cache/structure checks.
The actual October 2 source has 990 work parts, ten without local entry headings;
all ten have a verified original entry identifier. The shared source and entry
map were measured directly, with no model call or operational database write.

A controlled complete cycle uses the same physical source and provider responses.
The original makes three draft attempts and publishes nothing. The candidate
uses one draft and one critique, publishes a real page and authoritative v3
part receipt, and resolves its physical citation. Those are isolated controlled
responses, not a live model quality or latency claim.

Installation is blocked by a newly reproduced context-disposition defect, not
considered qualified: after a real committed v3 part receipt, changing only an
earlier original heading leaves the continuation digest unchanged. Early receipt
selection skips that part before the new cache identity can be checked. The
strict v3 receipt retains neither physical-context digest nor its reference.
Transaction after-images can be pruned, so depending on their continued presence
cannot repair this guarantee. The append-only daily contract needs an enforced
and complete proof, or a separately chosen compatible durable-context contract.
No receipt schema extension or reinterpretation of existing fields is installed.
