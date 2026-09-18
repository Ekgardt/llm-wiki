# The tools nothing ran

Dated 2026-09-18. The orphan tools of the third audit's dead-code list that live in the
install area: `scripts/check_knowledge_writers.py`, `scripts/ci_timing_report.py` with its
schema, and the three one-shot `repair_*` scripts. The owner delegated the decision; the rule
is that what is unneeded and useless is deleted, and nothing is broken.

## What was found

- **`check_knowledge_writers.py` (1,108 lines) is not unrun.** It is called by
  `tests/test_automatic_writer_integration.py`, which imports `discover_repository_writers`
  and `discover_repository_entrypoints` and asserts that the whole repository holds no
  unapproved writer to `knowledge/`. That test runs in the CI shards. Run here today over
  the tree, the scanner reports twelve approved writers and exits 0 in 58 s. What it guards
  is the "reliable mutation boundary" `CLAUDE.md` states: automatic Markdown writes go
  through recoverable transactions. A linter with a test that fails the build is wired in;
  what it lacked was a place where a person is told it exists.
- **`ci_timing_report.py` has a live input.** The workflow names every job
  `timing::<class>::<name>` — the exact shape the script parses — and the `pytest-full` jobs
  upload the JUnit timings it reads. Its `TIMEOUT_CEILINGS` are what each job's
  `timeout-minutes` should match: focused 900 s, clean and installer 1200 s, the full suites
  2700–3600 s. Checked here: the two jobs added today were 30 and 20 minutes against
  ceilings of 20 and 15; they are now 20 and 15.
- **The three `repair_*` scripts** each reclaim bytes that a defect fixed in September 2026
  left unwritten: a block a lost append race dropped, the pages a refused compile never
  wrote, and queue tasks that spent every attempt while still looking ready. Each has a test,
  a research note, prints what it found, and changes nothing without `--apply`. The defects
  are fixed, so a vault installed since then never meets those states; an older vault can
  still hold them, and nothing else reclaims them.

## The decision

Nothing here is deleted, because nothing here is useless — what was missing is the place a
person looks:

- `check_knowledge_writers.py` stays; its guarantee already runs in CI through its test. No
  second wiring is added: running the same 58-second scan twice per pull request would buy
  nothing.
- `ci_timing_report.py` and its schema stay, and `CONTRIBUTING.md` now says where CI job
  timeouts come from, which class ceiling each job must match, and the exact command that
  compiles the evidence.
- The three repairs stay, and `docs/USER-GUIDE.md` lists them in the recovery section as
  one-shot repairs for a vault that predates the fixes, check-first and `--apply` to act.

A capability that recovers the owner's bytes is not deleted to shorten a list; a tool a
person cannot find is documented, not removed.

## The files nothing names (F2–F4 of the same list)

- **68 benchmark result files** (12 named nowhere, 56 cited only by family prefix in dated
  documents, 7.1 MB in all) and **about 25 dated documents** — audits, comparisons, options,
  superpowers plans and specs — that no test pins and no other document names.
- They are kept. Each is the evidence behind a number a research note or the changelog
  quotes; a result file that nothing names by its exact file name is still the proof of the
  measurement it recorded, and a dated audit is the record of what was true that day.
  Deleting the owner's measurement and audit record to save 7 MB in a repository whose
  history is already 81 MB is a bad trade, and it is also the owner's own material rather
  than product code.
- One module is left undecided on purpose: `scripts/freshness_watch.py` (587 lines, 368 lines
  of tests) implements the "optional bounded watching" that `CLAUDE.md` names in the approved
  superset contract, and nothing calls it. It is neither dead by the brief's rule (a contract
  promises it) nor mine to wire: its only sensible callers are a session hook or the query
  path, both owned by other areas, and choosing one is a decision about when the vault spends
  CPU while its owner is working. It is named for the owner, not deleted.

Files: `.github/workflows/tests.yml`, `CONTRIBUTING.md`, `docs/USER-GUIDE.md`,
`docs/research/2026-09-18-the-tools-nothing-ran.md`.
