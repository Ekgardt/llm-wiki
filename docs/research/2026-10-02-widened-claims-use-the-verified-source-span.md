# Widened claims use the verified source span

Date: 2026-10-02.

The live compile published 339 of 342 pending parts of the October 1 daily source, but ended in error. Three tests reproduced its repeated widened-quote literal-hash mismatch before repair; fabricated evidence was already rejected.

The shared binder expands partial evidence to its complete source line. The claim producer previously kept the partial model text beside the complete-line digest. It now reads the exact bound bytes from the immutable snapshot for claim and evidence text. Strict validation, schemas, source hashes and transaction boundaries remain unchanged. No additional model call, limit, setting or runtime path is introduced.

Alternatives: weakening hash validation admits inconsistent evidence; disabling widening abandons the established complete-line contract. Reusing the verified span preserves that contract and prevents deterministic producer errors from causing model retries. Actual full-cycle token savings have not been measured.

Independent primary research: [W3C exact text selectors](https://www.w3.org/TR/annotation-model/#text-quote-selector), [Python byte digests](https://docs.python.org/3/library/hashlib.html), [Git byte hashing](https://git-scm.com/docs/git-hash-object). These support exact identity, not a claim that the remaining live backlog is recovered.

Three regressions failed before repair. After repair, 117 related checks passed, including transactional ledger publication and claim-index rebuild. English, bullet and indented Russian partial evidence retains its verified complete literal; fabricated evidence remains rejected. Installed verification and real compilation require separate evidence. Historical capture loss and retrieval quality remain open.
