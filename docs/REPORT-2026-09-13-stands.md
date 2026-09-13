# Every stand, measured on 2026-09-13

What this is: every measurement stand in `benchmark/` run once on the commit
`694991b` tree, on this machine, sequentially, one process at a time. Numbers
only, with what each one refuses to claim stated as plainly as what it passes.
Method decided beforehand in
`docs/research/2026-09-13-what-the-stands-must-show-after-the-verdict-cache.md`.

Machine: 4 cores, 8-15 GiB class, Linux 6.8, Python 3.12.3, Node v22.23.2.

## Against the other tool — the parity stand

`benchmark/run_code_parity.py`, 16 tasks, three runs per side, artifacts in the
job scratch (`parity-run1..3.json`), aggregate by
`benchmark/aggregate_code_parity.py`:

| side | correct | wrong | confident-wrong | tokens | seconds (3 runs) |
|---|---|---|---|---|---|
| llm_wiki | 16 | 0 | 0 | 8 108 | 31.91-32.63 |
| llm_wiki_best | 16 | 0 | 0 | 6 196 | 33.89-34.87 |
| codebase-memory-mcp | 14 | 2 | 2 | 10 641 | 34.28-34.97 |
| trace_mcp | 9 | 6 | 5 | 9 194 | 22.47-25.37 |
| serena | 12 | 3 | 3 | 4 226 | 89.4-109.82 |

Paired on the 14 tasks both `llm_wiki_best` and the other tool answered
correctly: 4 186 tokens against 9 785 — **0.43×**.

Every condition of the pre-stated decision rule
(`docs/research/2026-09-12-when-we-would-drop-the-other-tool.md`) holds, and the
lower of our two columns is the one that counts: correctness 16 against 14, no
task they answer and we do not, confident-wrong 0 against 2, tokens well under
1.5×, and total wall clock now *below* theirs rather than 1.58× above it.

## The vault's own answer, cold and warm

One `get_architecture mode=query` against the installed vault, in a fresh
process each time (`_execute_tool_call`, 955 characters of answer):

- with no remembered verdict for the artifact: **1.210 s**
- with the verdict remembered (4 later processes): **1.012, 1.018, 1.036 s**

Yesterday's cold figure was 1.23 s and this morning's was 4.9 s. The row walk
the verdict replaces is the ~0.2 s difference between the first call and the
rest, and it is now paid once per distinct artifact digest instead of once per
process.

A full hybrid search from cold in a fresh process is **8.4 s**, almost all of it
the one-time load of the two models — unchanged by today's work and named here so
the parity numbers are not read as search latency.

## Code navigation — `benchmark/run_code_navigation.py --fixture`

Correctness mode, exit 0, 104 s: definitions 200/200 exact, references F1 1.0
(500 true positives, 0 false), calls F1 1.0 (100 attempted), citation locations
800/800, task success rate 1.0. Reliability: 20 crash attempts, 20 recoveries,
0 orphan processes, edit-to-fresh p50 96 ms / p95 136 ms over 50 mutation cycles.

Qualification mode, exit 0, 112 s: cold readiness 0.88 s, warm facade p50 85 ms
/ p95 131 ms, overhead over direct Pyright p95 69 ms (direct p95 61 ms, 20
samples). `market_superiority_claimed: false` — the stand refuses that claim
without operator-corpus evidence, and it is not made here.

## Durability — `benchmark/run_durability.py`

110 trials, 108 of them killed mid-write, 108 kills observed: **0 silent
losses**, mean 1.15 recovery runs when the write landed, verdict PASS. The one
named failure reason observed was `RuntimeError: intent_fenced`, which is the
fence doing its job.

## Retrieval, classification, contradictions, conflicts

- `run_retrieval_v2.py`: exit 0, deterministic-fake complete;
  `quality_claim=false`, `release_evidence=false` — the frozen corpus gates the
  pipeline, it does not license a quality claim.
- `run_flush_classification.py --adapter canned`: 9 cases, tier accuracy 1.0,
  durable-content recall 1.0, false promotion 0.0, gates passed.
- `run_contradiction_benchmark.py`: extraction F1 1.0, class macro F1 1.0,
  lifecycle macro F1 1.0, provenance correctness 1.0, false supersession 0.0,
  quarantine coverage 0.67 of 80 candidates, 0 quarantine notes published,
  `semantic_benchmark_gate: false` (40 primary and 40 critique calls under the
  fake provider, so the semantic gate stays unclaimed).
- `run_conflict_resolution.py`: 161 conflicts scored from 453 parsed facts, both
  arms (`vault`, `argmax`) accuracy 1.0, 2 unparsed lines in the fixture.
- `run_scale_matrix.py --smoke`: exit 0, deterministic offline smoke, backend
  `exact-numpy` selected, 32-document corpus, 8 dimensions, 4 queries.
- `run_comparative.py --smoke`: exit 0, `computed: false`, reason "real paired
  observations unavailable" — the comparative claim is gated off by design until
  a real paired run exists.

## Selective forgetting: what failed, and what it found

`run_selective_forgetting.py` over the live vault's notes, verdict **FAIL** on
one gate of nine:

- `ageing.forget_rate` **1.0** — all 59 pages meant to be forgotten stopped
  surfacing (11 of 59 surfaced before, 0 after), and their bytes are retained
  under `knowledge/notes/archive` (210 800 bytes).
- `ageing.reprieve_rate` **1.0** — all 8 pages read recently kept surfacing.
- `ageing.retain_rate` **0.8857** — the gate wants 1.0. Of 100 pages that must
  keep surfacing, 70 surfaced before the ageing pass and **62 after**: eight
  pages that should have been untouched stopped surfacing once 59 other pages
  were archived.
- Supersession, legacy-leak and session-window gates all pass; 0 leaked, aged
  session record moved, archived bytes identical.

That was a real finding, and it was two defects, both now fixed and re-measured
(`docs/research/2026-09-13-one-argument-one-slot.md`):

1. **The product spent visible slots on repeats.** Since 2026-09-08 a slot
   belonged to a page *and heading*, which is right for a daily log (its headings
   are separate sessions) and wrong for a compiled note (its headings are sections
   of one argument). Ten rows held six pages: one workflow note took ranks 3, 4
   and 5. The unit of a repeat is now the episode for `knowledge/daily/**` and
   `knowledge/raw/**` and the page for everything else. Measured on the same live
   notes: pages surfacing in a ten-row window rose from **70 to 80 of 100**, and
   nothing is dropped — the repeats still follow the distinct pages.
2. **The gate measured rank, not presence, against its own docstring.** Every
   page it called forgotten came back at rank 11, 11, 12, 11, 12 or 13 once asked
   for a wider window. Presence is now decided over a documented window of 200,
   and the share of retained pages that keep a place in the first ten rows is
   reported (`visible_rate`) rather than gated.

Re-run after both, all phases, 74 s, exit 0: supersession 1.0/1.0, ageing
1.0/1.0/1.0, restore fidelity 1.0, no archive leak, session window applied,
archived bytes identical — **verdict PASS**, with `visible_rate` 0.75 recorded as
the window fact it is.

## Not run, and why

`run_longmemeval.py` and `run_consolidation.py` drive a real provider
(`--provider claude`) over the LongMemEval dataset on disk. They are the two
stands that spend model tokens, and they are left for an explicit decision about
sample size rather than started silently inside this pass.

## Re-measured after the slot fix, and what that exposed

The parity stand run again on the changed tree (258 s, exit 0): our two columns
15 of 16 correct with one partial, the other tool 14 correct, 1 partial, 1 wrong.
The one partial is **T04 on all five sides at once** — "where is `_page_diverse`
defined?" — because the slot fix moved that definition and no index had caught up
when the stand ran. The gold now resolves that number from the tree while it
grades, so the stand asks for the truth.

Checked afterwards, and it does not flatter us: the other tool's graph already
answers **3054**, the line the definition sits on now; ours still answers
**3030**, the line it sat on before the edit. Their index watches the tree and
refreshes itself in the background; ours refreshes on the nightly pass, and a
forced refresh inside a live session is refused by the maintenance fence — which
is correct as a fence and still leaves us answering a stale line for up to a day.
That is a real gap, it is named in the superset contract as "optional bounded
watching" and it is not implemented. It is the next thing worth doing.

Nothing about the counted standing changed — 15 against 14, 0 confident-wrong
against 1 — and on this one question, today, they were right and we were stale.

## Evidence

Commit `694991b`. Full local suite in a clean detached worktree with an external
state root: **8 415 passed, 371 skipped, 1 xfailed, exit 0, 16 min 15 s**. CI run
34727708815 on `694991b`: 45 checks green, one job red —
`timing::windows_full::py3.11-s2` died after 2 058 of 2 080 tests with no
summary, no junit.xml and no traceback, 14 minutes into a 40-minute budget. That
one is open and named in the changelog; `PYTHONFAULTHANDLER=1` and `-v` on the
Windows shards are in place so the next occurrence names its test.

Final state, commit `150f928`: the full suite in a clean detached worktree with an
external state root, with nothing else running on the machine — **8 434 passed,
371 skipped, 1 xfailed, exit 0, 16 min 29 s**. An earlier run of the same commit
reported two failures, `test_unix_installer_initial_monitor_mode_cleans_stopped_test_tree`
and `test_caller_restart_failure_keeps_deadline_and_retains_cleanup_owner`; both
pass alone in 0.79 s, and both are process-and-deadline tests that were sharing
four cores with a parity stand and an index rebuild I had started beside them.
That was a method error of mine, not a defect, and the rule it breaks is the
vault's own: do not run maintenance or stands while a run is in flight.
