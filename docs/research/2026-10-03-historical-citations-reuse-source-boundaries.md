# Historical citations reuse source boundaries

Research and reproduction date: 2026-10-03; Python 3.10 support unchanged.
No new runtime paths, environment contracts, providers or budgets.

A genuine installed compile, after the immutable-packing repair, began Codex
within roughly 18 seconds. Two draft replies failed their required JSON shape;
the third draft and critique returned. Four actual calls consumed 88164 input
and 9225 output tokens, with 51968 cached tokens included in input. The run was
cooperatively interrupted after 887.046s while rebuilding the claim index.
Its traceback identified historical slice lookup repeatedly rediscovering all
entry offsets inside every candidate-start loop. Nine unresolved compile
precondition refusals now remain, including two added during that run. Loading
and quantizing the local retrieval model also appeared in its log; this stack
sample does not attribute the entire remaining time to one operation.

A citation still requires exact original bytes and SHA-256. Current canonical
compile parts are checked directly first. Historical lookup discovers all
eligible ends once, then uses a binary search to begin incremental hashing after
each candidate start. No mutable file identity or timestamp substitutes for
content hashing. The resolver's existing operation-local cache still rechecks
the current source digest and invalidates offsets after content changes.

The unsupported 4096-candidate truncation is removed: it hid an authentic older
slice whose end lies beyond the retained prefix. Existing source byte bounds
remain, and no new bound replaces that candidate cutoff. Legacy whole-tail and
historical separator variants still use full exact hash verification. This is
an algorithm change, not a shorter evidence check or a synthesized receipt.

Two new guards genuinely failed before the repair: one measured three repeated
entry scans where one suffices; the other could not resolve a 4200-entry prefix
in a 5000-entry source. A third checks exact current parts including Unicode and
inner cuts without historical scanning. Related mutation, block and evidence
checks retain their assertions. An actual claim rebuild against 1224 vault
pages, using temporary derived SQLite state and unchanged source manifests,
completed in 7.267s, with 699 collected claims, 680 evidence resolutions and zero
diagnostics. No Markdown or model output was substituted.

Three actual active-ledger digests from the largest cited daily (5383252 bytes)
were resolved against identical captured bytes by old installed and candidate
readers. Both returned identical bytes with the original digest. Times were
0.237/0.00355, 4.755/0.00702 and 5.093/0.00738s; the complete paired experiment
cost 10.110s. These are lookup measurements, not completed compile, full wire
answer quality or full-cycle model token efficiency.

Alternatives: increasing the cutoff leaves a different hidden evidence gap;
restricting historical slices to today's part length rejects older whole-file
citations; accepting quotes without the historical source hash weakens provenance;
persisting another slice index adds invalidation and storage ownership work.
Call-local precomputation and an exact current-part fast path retain the existing
reader's authority and fallback without those changes.

Primary sources checked on the research date:

- [Python 3.10 bisect documentation](https://docs.python.org/3.10/library/bisect.html)
  specifies bisect_right and recommends precomputed keys for repeated searches.
- [NIST FIPS 180-4](https://csrc.nist.gov/pubs/fips/180-4/upd1/final) specifies the
  existing SHA-256 integrity primitive. Its announced revision is not an excuse
  to weaken the currently supported source hash contract.
- [RFC 9162](https://datatracker.ietf.org/doc/html/rfc9162), Experimental,
  supersedes RFC 6962 and describes append-only verification with consistency
  proofs. This reader verifies content digests; it does not implement or claim
  Certificate Transparency's Merkle consistency protocol. The RFC Editor fetch
  returned HTTP429; the authoritative IETF publication was checked instead.

Private audit artifacts preserve real failures, source navigation with incomplete
graph flags, full cost, profiles and paired evidence. Runtime completion remains
separate from this repair's qualification.
