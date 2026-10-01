# A quarantined claim does not hold its day

Date: 2026-09-25. Audit item A-13 (`docs/AUDIT-2026-09-25-full.md`).

## Facts (checked in code and on a copy of the live vault)

- `ContradictionPipeline._reduce_outcomes` quarantined every claim that had no
  candidate in the verified claim ledger but had any hit in the vault search
  ("retrieval-only context has no verified claim ledger"). A search over 209
  pages finds something for almost any text, so a new claim about a new subject
  was quarantined because it was new.
- `_CompileApplication` then took the whole batch to `_commit_quarantine`:
  candidates only, no page, no receipt. `docs/USER-GUIDE.md` says so and says
  a person must review the candidate: "There is no accept command". Nobody does,
  and the owner has said the system must not need manual review.
- The code already records quarantine per claim: `_rendered_claims` marks a
  quarantined claim `lifecycle: quarantined` on the page that carries it
  ("Quarantine is recorded on the claim, not on the page carrying it"), and
  `plan_changes` writes its candidate inside the publishing transaction. A
  non-active claim is `compatible` with every later claim
  (`_DETERMINISTIC_RULES`, first rule), so it cannot supersede anything.
- Live vault, `knowledge/inbox/claims/`: 102 candidates, all with the reason
  "contradiction assessment requires manual review", written 2026-08-29 to
  2026-09-11. Re-assessed today on a copy with the current ledger and no
  retrieval rule: 86 `unrelated`, 3 `equivalent`, 1 `compatible`,
  1 `no-candidate` (keep-both), 3 `supersede`, 8 need a semantic judgement.
  92 of their source pages were never created; their days were compiled later
  by other plans, except `2026-09-01` (audit A-12).

## Source

- Zep: A Temporal Knowledge Graph Architecture for Agent Memory,
  https://arxiv.org/html/2501.13956 (fetched 2026-09-25): the system "employs an
  LLM to compare new edges against semantically related existing edges to
  identify potential contradictions". Contradiction is judged against existing
  facts, not against any text a search returns; a fact with no related fact is
  simply added.

## Decision

- Retrieval-only context no longer quarantines. With no ledger candidate the
  claim is `keep-both`, and the retrieved pages stay in its evidence.
- A batch is not held back by a quarantined claim. It publishes its pages and
  receipts; the quarantined claim is carried on its page as
  `lifecycle: quarantined` and its candidate is written in the same
  transaction. The day is compiled. Only a stale lifecycle target (a
  concurrent change) still ends in a candidates-only commit.
- The contract stays: automatic semantic supersession remains disabled; a
  quarantined claim never supersedes or is superseded.
- The 102 existing candidates stay as the audit trail. Nothing counts them as
  work, and their days are compiled.
- A decision page is recorded in the private vault and the user guide says the
  new behaviour.

## Files

- `scripts/contradiction_pipeline.py`
- `scripts/compile_memory.py`
- `tests/test_contradiction_pipeline.py`
- `tests/test_compile_transactions.py`
- `tests/test_a_quarantined_claim_does_not_hold_its_day.py`
- `docs/USER-GUIDE.md`
- `CHANGELOG.md`


## 2026-09-30: do not retrieve context the compiler does not consume

Code and graph tracing show that the no-ledger fallback searches the normal
retrieval path, including its cross-encoder, before the compiler can commit.
The no-candidate decision is keep-both with or without those search hits. The
compiler consumes the recommendation, lifecycle mutations and normalized ledger
claims; retrieval-only evidence of a keep-both assessment is not published.
The query therefore adds inference work without affecting this consumer's result.
A real installed query at two CPU threads spent 5.775 seconds in warm reranking
alone; this is a query measurement, not a paired compile benchmark. Four retained
refusals in one publication lineage had project state changes between successive
claim-tree snapshots. Unnecessary search lengthens that vulnerable interval; it
is not proven to be the only cause of all historical refusals.

Sources checked on 2026-09-30:

- SQLite, https://sqlite.org/isolation.html: WAL isolates read snapshots but
  still serializes writers; it does not make separate filesystem snapshots
  immune to concurrent changes. Installed SQLite remains 3.53.1.
- David Rice, Optimistic Offline Lock,
  https://martinfowler.com/eaaCatalog/optimisticOfflineLock.html: validate changes
  against concurrent edits before committing. Keep the existing manifest check.
- Google SRE, https://sre.google/sre-book/handling-overload/: distinguish useful
  work and optional work when controlling load. This supports examining the
  consumer's needs, not weakening any correctness check.

Chosen: an explicit per-assessment option to omit retrieval-only evidence. The
compiler selects it because it does not consume that evidence. Normal readers,
MCP contradiction checks and benchmark callers keep their existing default.
Verified ledger selection, deterministic classification, semantic gates, policy
mutations, quarantine, manifest checks and receipts remain the same. Remove the
compiler's redundant search callback/import; no second implementation, dependency,
runtime format, path, environment contract or arbitrary limit is introduced.

Alternatives rejected: raising retries repeats the unnecessary work; holding the
writer gate across inference blocks capture; removing the snapshot check admits
stale assessments; globally disabling search would remove useful context from
readers. The tradeoff is deliberate: compile assessments have no auxiliary search
hits, while published knowledge and admission are unchanged.

The full publication regression fails on the prior code because the search is
called; after the change it publishes the same claim without a search. Paired
assessment assertions preserve classification, recommendation and mutations while
checking that default readers still receive retrieval-only evidence. Existing
concurrency, stale-input, lifecycle and receipt tests remain required.
