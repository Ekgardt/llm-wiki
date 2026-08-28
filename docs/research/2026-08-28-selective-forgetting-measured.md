# Selective forgetting, measured rather than asserted (`MEM-12`)

Date: 2026-08-28. Roadmap item `MEM-12`, section 12 of
`docs/DEVELOPER-AUDIT-STATUS-2026-08-18.md`. Market context:
`docs/research/2026-08-27-number-one-memory-market-research.md`.

MemoryAgentBench scores four competencies — accurate retrieval, test-time
learning, long-range understanding, and **selective forgetting** — and reports
that no system masters all four, with selective forgetting the one most
systems fail conspicuously. This vault has shipped forgetting machinery for
months (`archive_stale.py`, `archive_sessions.py`, the access-driven reprieve)
and has never measured it. This note is that measurement.

Stand: `benchmark/run_selective_forgetting.py` +
`benchmark/selective_forgetting_vault.py`. Tests:
`tests/test_selective_forgetting_stand.py` (40, green).

---

## 1. What "forgotten" means here — established by measurement

The first question the roadmap asks is what forgetting *is* in this system:
exclusion from the corpus, exclusion from search, or both. Traced through the
graph and then measured, not inferred.

There are three independent exclusion gates, and they do not agree with each
other:

| reader | archive directory | retired status |
|---|---|---|
| `corpus_snapshot._Discovery._directory_excluded` (`ARCHIVE_DIRECTORIES`) | excluded | `_included` → `page_status.is_retired` |
| `search_memory._collect_pages` (legacy lexical index) | **not excluded** — `SKIP_DIRS` is `{"projects", "gaps", "raw-sources"}` | `_is_retired_page` |
| `search_memory._legacy_vector_excluded` (legacy dense) | — | `is_retired(_page_status(path))` |

So the corpus collector forgets by *two* signals and the legacy index by
*one*. That difference is not academic; §4 measures where it bites.

**Nothing is deleted.** `archive_stale` moves a page to
`knowledge/notes/archive/<year>/` and rewrites its status; `archive_sessions`
moves a record to `knowledge/raw/sessions/archive/<YYYY-MM>/<date>/`. Both go
through `mutate_knowledge`, and in the measured ageing run the 29 archived
pages left **62 062 bytes** sitting in the active working tree one directory
deeper, still greppable and still in git. Forgetting here is *exclusion from
retrieval*, full stop.

---

## 2. Method

Each phase runs in its own process (`LLM_WIKI_ROOT` is resolved at import
time, and a corpus generation is process-global) against a throwaway vault
seeded with **copies** of the live vault's 107 knowledge pages. The live vault
is read-only throughout; nothing here archives it.

The pipeline is the product's own: `repair_installed_vault` adopts the trial
vault onto the Reliability V3 pair, so every move is a real transaction;
one immutable corpus generation is built and activated through
`build_incremental_generation` with the helpers `doctor` uses; `archive_stale`
and `archive_sessions` do the forgetting; `retrieval.retrieve_via_search_memory`
answers the probes.

**Probes.** A probe is a verbatim phrase taken from the target page itself —
its longest plain body line, clipped to 12 words, chosen deterministically. A
page that is retrievable at all is retrievable by its own words, so a miss is
attributable to the forgetting policy rather than to a weak question. Probes
are asked with `semantic=False, rerank=False`: presence and absence are what
this stand measures, and the optional legs are deadline-bounded and vary
between runs (recorded 2026-08-26 in `knowledge/log.md`).

**Pairing.** Every rate is a paired before/after or treatment/control
comparison over the same named cohort, never a bare score:

* *supersession* — the 4 live pages carrying `status: superseded`, against a
  control arm in which the same pages are forced to `accepted` and the
  generation rebuilt. The control proves the probes work; the treatment
  measures the status.
* *ageing* — real pages whose mtime is pushed past their own type window (the
  ages are synthetic, the pages are real), split into a cohort nobody reads, a
  cohort read once through `access_tracking.record_access` before archiving,
  and the never-archive types. Asked before archiving and again after, with
  **the same query string both times**.
* *restore* — every archived page brought back through `restore_page`,
  compared byte for byte against the page as it was before archiving.
* *legacy* — the same archive read by `_collect_pages`, the index used when no
  corpus generation is usable.
* *sessions* — the second forgetting mechanism, on raw session records.

One methodological correction is worth recording because the first run got it
wrong: the after-round originally re-read the probe out of the target page,
which by then had moved, and reported *0 of 37 probes usable*. That is file
absence, not retrieval. Queries are now taken once, before archiving, and
replayed.

---

## 3. Numbers

Run of 2026-08-28 on this machine, one full pass, all five phases, 442.22 s
wall (load average ~9, other agents running). Report:
`benchmark/run_selective_forgetting.py --report <path>`.
`(live)` = the vault's own pages and statuses; `(synthetic ages)` = real pages
with mtimes moved; `(synthetic case)` = a page shape constructed for the stand.

Verdict of the run: **FAIL**, on one gate — `legacy.no_archive_leak`. Eight of
nine gates pass. The failing one is §4's `NEW-126`, and it is the reason the
stand was worth building.

### Supersession — live data, n = 4 forget, n = 4 retain

| | control arm (forced active) | treatment arm (as written) |
|---|---|---|
| corpus sources | 107 | 103 |
| generation chunks (`vector_state: complete` both arms) | 620 | 592 |
| superseded pages in corpus | 4 / 4 | **0 / 4** |
| superseded pages surfaced | 4 / 4, all at rank 1 | **0 / 4** |
| their `superseded_by` successors surfaced | 4 / 4 | **4 / 4**, all at rank 1 |

**forget_rate 1.00 (4/4), retain_rate 1.00 (4/4).** The control arm is what
makes this a measurement: the same four pages, the same four probes, one
frontmatter word apart, answer at rank 1 when active and are absent when
superseded.

n = 4 is the entire live population of superseded pages, not a sample.

### Ageing — real pages, synthetic ages

Seeded 107 pages: **37 archivable** by type, **66 never-archive-and-in-force**,
4 superseded (excluded from both cohorts). Of the 37, the first 8 were read
once through `record_access` to test the reprieve; 29 were left untouched.
Every page in every cohort is probed — no sampling.

| cohort | n | surfaced before | surfaced after | in corpus after |
|---|---|---|---|---|
| aged, unread (must be forgotten) | 29 | 29 | **0** | 0 |
| aged, read once (must be reprieved) | 8 | 8 | **8** | 8 |
| never-archive types (must survive) | 66 | 66 | **66** | 66 |

Corpus sources 103 → 74; generation chunks 592 → 466; `archive_stale --apply`
reported `ARCHIVED: 29`, zero errors; 62 062 bytes left in the archive.
Phase time 209.37 s, of which two full generation builds.

**forget_rate 1.00 (29/29), reprieve_rate 1.00 (8/8), retain_rate 1.00
(66/66).** All 103 probes were usable in both rounds.

The reprieve number is the one worth pausing on: a single `record_access` call
moves `decay_score` to 0.75 against a 0.3 floor, and the archiver leaves the
page alone even though its mtime is 30 days past its window. That is exactly
FiFA's Priority Decay usage term, and it works — 8 of 8, with the untouched 29
archived in the same pass, so the reprieve is discriminating rather than a
blanket refusal to archive.

### Restore — real pages, n = 37

All 37 archivable pages archived (no reads in this phase, so no reprieve) and
restored: **37 / 37 byte-identical** to the pre-archive original. Fidelity
1.00, phase time 12.93 s.

This holds for a reason the stand also measured: all 37 archivable live pages
carry *no* `status:` line, so archiving inserts one and restore removes the
one it inserted. The round trip is lossless because there was nothing to lose.
The two synthetic cases in §4 show what happens when there is.

### Legacy index — 39 archived files

38 of 39 archived files are refused by `_collect_pages`; **1 leaks** and
answers its own probe at **rank 1, from inside the archive directory**. See
`NEW-126`.

### Sessions — n = 2 records

* records written to `knowledge/raw/sessions/`: 2
* **records in the corpus: 0 of 2** — session evidence is deliberately not
  collected (`corpus_snapshot._walk_knowledge`, MEM-01 reverted 2026-08-24)
* aged record moved: yes; recent record kept: yes; archived bytes identical to
  the original: yes
* a restore path for session records: **none exists**

The honest consequence: archiving a session record changes **nothing about
retrieval**, because the record was never retrievable. It is a disk-layout
operation. It is still worth having — consolidation reads yesterday and a
person greps months back — but it is not selective forgetting in
MemoryAgentBench's sense, and it should not be counted as such.

---

## 4. What the stand found — three defects, none patched

Per the task's rules of engagement, `scripts/` was not edited. Each is
reproduced deterministically by the stand. Registry ids are proposed, not
assigned: `docs/DEVELOPER-AUDIT-STATUS-2026-08-18.md` was outside the files
this pass may touch.

### `NEW-125` — restore erases a status it did not add (synthetic case)

`_with_archived_status` **overwrites** an existing status value with
`archived`; `_without_archived_status` **deletes** the `status: archived`
line. A page archived as `status: preliminary` therefore comes back with no
status at all: 98 bytes in, 78 bytes out, the word `preliminary` gone. The
page is still findable (absent status reads as `active`), so nothing breaks
loudly — a declared editorial state is simply lost across a round trip that
the module's own docstring calls dormancy rather than deletion.

Not reachable on today's live data, and the stand says why: of the 37
archivable-type pages, **0 carry a `status:` line**; the two pages that carry
`status: preliminary` are both `type: synthesis`, which never archives. This
is a latent defect, and it will stop being latent the first time a
`debugging` or `pattern` page is written with a status word.

### `NEW-126` — an archived page with no inserted status stays in the legacy index

`_inserted_archived_frontmatter` returns the content **unchanged** when the
page has no frontmatter but the literal `status:` appears anywhere in its
body. Such a page is archived — moved into `knowledge/notes/archive/<year>/` —
while declaring no retired status at all. The corpus collector still refuses
it, by directory. The legacy lexical walker does not: `SKIP_DIRS` does not
contain `archive`, so `_collect_pages` admits it, and the measured probe
returns it at **rank 1**.

Measured: 39 archived files, 38 refused, 1 admitted; the vault's own
`_declares_retired` reads `False` on the archived copy. The blast radius is
bounded — the legacy index is only consulted when no corpus generation is
usable — but that state is not hypothetical: this vault hit exactly it on
2026-08-24 (`NEW-81`, empty active pointer, search returning zero rows).

Also not reachable on today's live data: 0 of 107 notes lack frontmatter.

### `NEW-127` — the recency signal is mtime, and the vault's own writers reset it

`_stale_by_mtime` reads `md.stat().st_mtime`. On this vault that is not the
page's age. Measured on the 76 git-tracked notes, with a clean working tree
(`git diff --name-only HEAD -- knowledge/notes` → empty):

* mtime age: min 0.1 d, median 2.1 d, max 11.2 d
* last-commit age: min 0.1 d, median 9.6 d, max 49.2 d
* **53 of 76 pages have an mtime more than a day newer than their last content
  change while being byte-identical to HEAD**; the largest gaps are 36–38 days

Checkouts, index rebuilds and the nightly backlink writer (`NEW-84`,
2026-08-24) all touch pages without changing them, and each touch resets the
forgetting clock. A page can be up to 38 days "younger" than it is.

The consequence, measured directly: **0 of 107 live pages are archivable
today** — by mtime (oldest 11.2 d against a 60 d shortest window) *and* by the
page's own declared `timestamp`/`date` (oldest 56 d, 36 pages parseable, 0 over
window). The age-based forgetting path has never fired on this vault and, on
mtime, cannot fire for at least another 49 days. Every ageing number in §3 is
therefore from synthetic ages on real pages, and is labelled so.

---

## 5. Against the field

**MemoryAgentBench** ([2507.05257](https://arxiv.org/abs/2507.05257)) frames
selective forgetting as: after an update supersedes an old fact, does the
system answer from the new one. That is precisely the supersession phase, and
this vault answers 4/4 with the successor at rank 1 — with the strong caveat
that n = 4 and the supersession is *declared in frontmatter by a writer*, not
inferred by the system. MemoryAgentBench's FactConsolidation asks a system to
notice the conflict itself. This vault does not; it is told. That is a design
choice this project has already made and defended (deterministic supersession,
no LLM in conflict resolution) but it means our 4/4 and their scores are not
the same measurement.

**FiFA / MaRS** ([2512.12856](https://arxiv.org/abs/2512.12856)) is the closer
comparison, and the contrast is sharp in both directions.

*Where we match.* FiFA's six policies include **Priority Decay** — "type
prior, recency, and usage frequency". `archive_stale` is literally those three
terms: `TYPE_AGE_DAYS` (type prior), mtime (recency), `decay_score`'s
reinforcement from `access_tracking` (usage). We did not derive it from FiFA,
but it is the same policy, and §3 shows the usage term working at 8/8.

*Where we are ahead.* FiFA's eviction is destructive except for `derivesFrom`
provenance on summaries. Ours is reversible by design and measured lossless at
37/37. Reactivation is a contract, not a rescue (`MEM-06`, 2026-08-25).

*Where the claim does not transfer, and this is the part that matters.* FiFA
measures **privacy leakage** as sensitive content persisting in the store, and
its forgetting is **actual deletion of the node**. Ours is exclusion from
retrieval with every byte left in the working tree, in `git`, and in `grep`.
On FiFA's privacy metric this vault scores **zero mitigation** for any
sensitive item: archiving it does not reduce what persists, only what is
recalled. The 81 392 bytes measured in §1 are the number. Any future claim
that this vault's archive "preserves privacy" would be false; the defensible
claim is that it preserves *coherence* and *answer quality* by keeping stale
material out of answers.

*Where we have nothing.* FiFA's budget is `∑wᵢ ≤ B` over token weight. This
vault has **no size budget at all** — no ceiling on pages, chunks, or index
bytes, and no policy that evicts because the corpus got too big. Forgetting is
triggered only by the calendar and only per-page. The measured generation
went 592 → 424 chunks, but nothing asked it to; that was a side effect of a
time window, not a budget. If `MEM-12` is meant to claim the FiFA form, a
budget term is the missing half.

---

## 6. Limits, stated plainly

* Probes are lexical and deterministic. Nothing here measures whether the
  semantic leg or the cross-encoder would resurface an archived page; they
  read the same generation, so they should not, but that is an argument, not a
  measurement.
* Every ageing number uses synthetic mtimes on real pages. There is no live
  cohort, because §4 shows there cannot be one for another 49 days.
* n = 4 for supersession. It is the whole population, which makes it complete
  and small at the same time.
* The two defect cases in §4 are synthetic page shapes. Both are currently
  unreachable on live data, and the stand reports which measurement says so.
* Nothing here measures MemoryAgentBench's other three competencies, and
  nothing here is a MemoryAgentBench score — the datasets were not run. This
  is our machinery on our data in their shape.

## Sources

- [Evaluating Memory in LLM Agents via Incremental Multi-Turn Interactions (MemoryAgentBench, arXiv 2507.05257)](https://arxiv.org/abs/2507.05257)
- [Forgetful but Faithful: A Cognitive Memory Architecture and Benchmark for Privacy-Aware Generative Agents (FiFA/MaRS, arXiv 2512.12856)](https://arxiv.org/abs/2512.12856)
- `docs/research/2026-08-27-number-one-memory-market-research.md`
- `knowledge/notes/session-evidence-retention-decision.md`, `knowledge/log.md` 2026-08-25 (`MEM-06`), 2026-08-24 (`MEM-01`, `NEW-81`, `NEW-84`)
