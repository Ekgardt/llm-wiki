# Claim identity includes its immutable observation

Research checked 2026-10-01 against three independent primary sources:
- W3C PROV-DM: https://www.w3.org/TR/prov-dm/ — provenance distinguishes entities and the activities/evidence from which they derive.
- Git Objects: https://git-scm.com/book/en/v2/Git-Internals-Git-Objects — content-derived identity supports deterministic reuse without confusing different content.
- RFC 9110 idempotence: https://www.rfc-editor.org/rfc/rfc9110.html#name-idempotent-methods — repeated identical requests should have the same intended effect.

The compiler used only date plus semantic fingerprint for a claim ID, although a record also owns immutable evidence, observation time and literal text. Different observations of the same semantic fact on one day therefore collided. Separately, merging an exact saved record rejected a safe replay. Both scenarios were reproduced from retained local cached plans and synthetic regressions.

New records hash the complete semantic fingerprint, immutable evidence reference (source digest, date, timestamp, byte range) and literal SHA-256. The semantic fingerprint remains unchanged for contradiction comparison. The full SHA-256 is used without truncation. Identical ledger records merge once; the same ID with different content still refuses, including different lifecycle or authority. Old IDs, ledger contents, receipts, transaction identities and pending plans are not rewritten. The extractor version and cache normalization version distinguish newly generated plans; old receipts remain authoritative and old saved plans retain their identities and safeguards.

Rejected alternatives: overwrite on ID collision (loses provenance); discard all semantic duplicates (loses independent evidence); random IDs (breaks deterministic replay); rename historical records (breaks links/receipts); weaken same-ID conflict checks. No schema, dependency, new runtime path, new limit or model change. New reference-bound IDs may differ when an immutable source snapshot changes; this intentionally preserves source-version provenance while semantic fingerprints remain available for semantic grouping.

Regression: old code fails distinct-observation identity and exact replay; new code accepts both and still rejects changed content. Real retained exact replay stays byte-equivalent; the other retained observation merges after regenerating its new identity while all prior records and both evidence bodies remain.
