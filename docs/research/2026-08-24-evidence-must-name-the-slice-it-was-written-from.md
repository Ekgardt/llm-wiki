# Evidence must name the slice it was written from (2026-08-24)

## The question

A compiled page cites a daily log: `daily:<day> sha256:<digest> block:<time>
bytes:<start>-<end>`. On this vault 31 of 99 pages failed that citation with
`flat daily source hash mismatch`. What should a reader accept?

## What was measured first

Every one of the 31 digests was the digest of a **compile part**, not of the
whole day. The compiler splits a day longer than 16 KiB at entry boundaries and
works one part at a time; the evidence it writes names that part and offsets
inside it. `EvidenceResolver.resolve` only ever compared the whole file, so it
rejected every page compiled from a split day — and this vault's days are all
split. Both the writer and the reader were tested, and each was right on its own
terms; nothing tested them against each other.

## What the practice says

The general problem is a reader verifying a historical state of an append-only
source. Certificate Transparency answers it with a *consistency proof*: the old
state must be provably a prefix of the new one, and the log proves it rather
than being trusted ([RFC 6962][ct], §2.1.2; [Trillian, Transparent
Logging][trillian]). Tamper-evident audit-log guidance from 2026 says the same
in plain terms: append-only storage plus hashes over entries, so post-hoc
modification is detectable by recomputing over the same units the writer used
([Merkle-tree pipelines for edge logs][merkle]; [Compliance by design][compliance]).

The lesson carried over here: the unit of verification must be the unit the
writer committed to. A whole-file digest is the wrong unit for a file that keeps
growing — it fails on every legitimate append, and failing on legitimate change
is how a check gets ignored.

## What this vault does

The reader now looks for an **entry-aligned slice that starts where a compile
part starts and whose bytes hash to what the page recorded**. That covers both
real cases: the part that was compiled, and a part that was the tail of the day
then and is the head of a longer part now. Nothing weaker is accepted — the
historical bytes must still be present verbatim and in place, so an edit inside
the cited region still fails the citation.

The splitter moved next to the reader, because a writer and a reader that cut
the same file in different places is exactly the defect that was found.

Cost: one extra pass over the day, only when the whole-file digest already
missed. Measured on this vault's 231 KB day: under 20 ms.

[ct]: https://www.rfc-editor.org/rfc/rfc6962.html
[trillian]: https://google.github.io/trillian/docs/TransparentLogging.html
[merkle]: https://arxiv.org/html/2605.00065v1
[compliance]: https://mattermost.com/blog/compliance-by-design-18-tips-to-implement-tamper-proof-audit-logs/
