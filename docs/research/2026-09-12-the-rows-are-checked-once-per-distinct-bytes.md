# The rows are checked once per distinct bytes, not once per process

Dated 2026-09-12. CI run 34725227244 (head `e71e4d0`) failed shard 3 on every
platform. Five of its failures are one defect of mine, and this note is the
research for the fix.

## The facts

- Failing: `tests/test_search_ranking.py::test_malformed_generation_fts_falls_back_legacy`
  for `non-list-heading-json`, `invalid-chunk-order`, `invalid-source-hash`,
  `invalid-chunk-id` and `blank-content`
  (`AssertionError: assert ['252ef524bcd...'] == ['legacy']` — the search
  answered from a damaged generation instead of falling back).
- Each of those parameters rewrites one chunk row inside `search.sqlite3` and
  then calls `_refresh_artifact_descriptor`, so the manifest's declared digest
  is recomputed over the damaged bytes. **The digest therefore cannot catch this
  damage**; the row walk was the only check that did.
- I removed that walk from the read path earlier today
  (`docs/research/2026-09-12-a-reader-checks-the-digest-a-writer-derives.md`):
  `_stored_chunks_match` returns True immediately when no re-derivation was
  asked for. That note's boundary — "a reader checks the digest, a writer
  derives" — is right about the *re-derivation* (comparing every row against a
  freshly chunked source) and wrong about the *row invariants* (chunk ids that
  are sha256, contiguous `chunk_order`, a heading ancestry that is a JSON list,
  non-blank content, unique ids). Those invariants are not a determinism check
  of our chunker; they are what the reader needs to be true about the rows it is
  about to serve.
- The other three failures of that shard are a stale test lambda
  (`tests/test_code_extractor.py`, already fixed) and two fake catalogs in
  `tests/test_code_graph.py` that do not answer `code_generation_for_repository`
  — the question `_opened_code_or_active` now asks first.

## Research, current practice on this date

1. **Validate at publish, and re-read once after publishing — not on every
   query.** Iceberg's Write-Audit-Publish writes to an isolated branch, audits
   it there, promotes it by a metadata fast-forward, and then "re-read[s] the
   main branch immediately after publishing to validate the post-merge state"
   ([Write-Audit-Publish in Apache Iceberg](https://www.telm.ai/blog/what-is-write-audit-publish-in-apache-iceberg-and-why-it-matters-for-data-quality/),
   [Streamlining data quality with WAP and branching](https://www.dremio.com/blog/streamlining-data-quality-in-apache-iceberg-with-write-audit-publish-branching/)).
   The audit is bound to the bytes being published, not to each later reader.
2. **A full structural check per open is measured overhead, and belongs on a
   cadence or a cold start.** SQLite's own forum: `PRAGMA integrity_check` "has
   to read every piece of data in the table and its indexes, and do a lot of
   cross-referencing"
   ([SQLite forum](https://sqlite.org/forum/forumpost/9d9e63a8d4)); a reported
   case had 20 agent databases stalling startup by 10–100 s on checks that were
   always healthy, with the recommendation to run them "on a maintenance cadence
   (e.g. once per N hours or on cold start), not on every open"
   ([slow SQLite](https://github.com/openclaw/openclaw/issues/145909)).
3. **A verified artifact is imported, not re-verified.** A content-addressed
   cache verifies "every cache-key binding, meta.json, artifact name/size and
   blob hash before publication, and import[s] verified entries in batch without
   re-hashing bytes already verified"
   ([batched verification and import](https://github.com/kunobi-ninja/kache/issues/812)).
4. **Validity is recorded, not recomputed.** `nix store verify` exists as a
   *command*: a store path's validity and trust are recorded in the store's
   database when it is added, and ordinary use does not re-hash the closure
   ([nix store verify](https://nixos.org/manual/nix/stable/command-ref/new-cli/nix3-store-verify)).

## The decision

**Restore the row-invariant walk on the read path, and remember its verdict
keyed by the artifact's content digest**, exactly as the artifact digests
themselves are now remembered.

- `verified_artifacts` gains a second kind of entry: a named verdict for a
  digest (`verdict|fts-chunks|<sha256>`). The identity is the content hash, so
  Git's racily-clean rule does not apply to it — there is no stat to be raced.
- `validate_generation_fts_artifact` asks the cache before the walk and records
  a successful walk after it. `deep` always walks, and still re-derives.
- The first cold read of a *new* generation pays the walk once (0.38 s on the
  installed vault, 3 405 rows); every later process pays nothing, which is the
  same shape as the digest cache and keeps this morning's 1.23 s cold answer.
- A cache that cannot be read, an artifact with no declared digest, or a digest
  that changed all fall back to walking. The failure mode stays a slow read.

Why not the alternatives:

- **Leave the walk off the read path.** It is what CI is refusing, and rightly:
  five kinds of row damage would be served instead of falling back to lexical
  search.
- **Re-express the invariants as one SQL aggregate.** Faster than a Python loop,
  but it would state the same rules a second time in another language, and the
  cost it saves is already saved by remembering the verdict.
- **Move the invariants into `doctor` only.** A reader would then serve rows it
  never checked, and the fallback contract these five tests encode would be gone.

## The fourth failure of that run, and its class

`timing::focused::pyright-windows` failed
`tests/test_lsp_process.py::test_autonomous_bootstrap_uses_configured_budget_and_retains_cleanup_owner`
with `TimeoutError: LSP startup deadline expired after initial lease
publication`. One number was doing two jobs: `deadline=time.monotonic() + 0.8`
was both the budget for starting a real server process and handshaking with it,
and the autonomous replacement budget the test measures. The replacement budget
has to stay small because the replacement waits it out; the start budget has to
be sized for the slowest supported machine. They are now two numbers — the start
uses `_STARTUP_BUDGET_SECONDS`, the replacement stays 0.8 s and is passed
explicitly as `bootstrap_timeout_seconds`, which is what the neighbouring
`test_configured_start_preserves_explicit_autonomous_bootstrap_budget` already
does. This is the same defect class as commit `9c88bbf`: "each bound now measures
the hang it was written for, not the runner's speed".

## Open, and honest

The verdict is trusted for bytes whose digest matches. An attacker able to
rewrite the artifact *and* the manifest descriptor *and* the catalog seal is
outside what this cache changes — but I state plainly that within one state
root, a damaged artifact that has already been walked once cannot be damaged
again into a previously-seen digest, which is what the digest key buys.

Files: `scripts/search_memory.py`, `scripts/verified_artifacts.py`,
`tests/test_a_verified_digest_is_remembered.py`, `tests/test_code_graph.py`,
`tests/test_lsp_process.py`,
`benchmark/code-parity-v2.json`,
`docs/research/2026-09-12-the-rows-are-checked-once-per-distinct-bytes.md`.

## 2026-10-05: the sealed query consumes the same verified row verdict

The current ordinary MCP request reproduced a remaining cold refusal on the
actual captured corpus: 59,873 sources, 126,298 chunks and a genuinely extracted
six-table graph. Its foreground ownership already preempted whole-path warmup.
An already-running native reranker load still overlapped the mandatory work.
Ordered tracing measured catalog selection at 8.361 seconds, its light FTS
check at 1.539 seconds, and a subsequent strict query FTS check at 3.773 seconds
including a 2.158-second stored-row walk. The query ignored the existing
`VerifiedArtifacts` `fts-chunks` verdict that the catalog had consumed for the
same bytes. This does not prove that native loading alone caused the refusal;
6.821 seconds of the catalog's earlier work remained unsegmented.

The compatible correction passes the existing query seal into the connection
reader. It consumes a successful persistent row verdict only after comparing
an actual current FTS file seal with the captured named artifact seal and the
canonical manifest digest. Schema, metadata, row count and version checks still
run. The actual file and canonical manifest are checked again before returning
the open connection. A failed or cancelled check closes it. This scoped reuse
does not seed the process-wide verdict for unsealed callers. Raw callers,
missing verdicts and unknown bytes still run the strict row checks. No database,
schema, runtime path, setting, deadline or resource limit is added.

Fresh primary research on this date: [Git's racy-file proof](https://git-scm.com/docs/racy-git)
explains why matching timestamps alone cannot establish unchanged bytes;
[SQLite URI contracts](https://www.sqlite.org/uri.html) distinguish read-only
access from an assertion of immutability; [Python 3.10.22 cache semantics](https://docs.python.org/3.10/library/functools.html)
do not guarantee that concurrent first calls compute once. The existing
content-digest and racy-file checks remain the authority. Repeated unconditional
walks were reliable but expensive; a new persisted attestation would broaden the
contract; disabling warmup or increasing the request clock would mask the issue.

The original regression observed a full walk despite a successful persisted
verdict and an actual current seal. Fifteen focused guards now pass, including
unsealed and absent-verdict strict reads, malformed seals, corrupted new bytes,
unknown metadata version, original-seal changes, atomic file replacement and
content changes with forged mtime during validation, changed manifests, caller
expiry and late cancellation. Mutation and cancellation close the connection.
The related 158-test run passed; its existing availability test actually loaded
and quantized the local Torch reranker. Local token and monetary cost are unknown.

One subsequent unprofiled ordinary MCP first/repeated cycle, with real local
models, normal warmup and the unchanged fourteen-second clock, returned in
11.794 and 6.003 seconds. The first answer used lexical evidence; the second also
used the real reranker (1.668 seconds). Both remained `BASE`, with dense work not
admitted. This proves the two measured answers met their clocks on the genuine
full fixture, not full HYBRID readiness or an isolated speedup. Earlier failed
cycles and their costs are retained. ROOT is not installed by this qualification;
whole-current-product regression and installation remain separate checks.
