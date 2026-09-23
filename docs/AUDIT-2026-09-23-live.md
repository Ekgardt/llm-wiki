# Audit of the live system — 2026-09-23

Scope: the owner's installed vault (state root = vault), as it ran on
2026-09-23 between 08:40 and 09:40 UTC, code `95b02427`. Every number below was read from
the live runtime, its logs, its databases (read-only) or the user journal; nothing is
taken from tests or from earlier reports. "Fixed" means a change is in PR 38; "Open"
means nothing has been changed yet; "Decision" means the fix changes structure and needs
the owner's sign-off first (CLAUDE.md §0).

## A. Outage and self-healing

### A1. Every Markdown writer was refused for six days — Fixed (PR 38 `95b02427`, repaired 09:20:42)
- `logs/capture-failures.jsonl`: 81 entries since 2026-09-17 13:37 with
  `reliability_v3_record_invalid <- candidate artifacts remain after adoption`; the compile of
  2026-09-17 13:59 failed the same way; no `knowledge/daily/2026-09-1[5-9].md` or later exists.
- Cause: `run/markdown-transactions-v3.candidate.sqlite3` (204 800 B, 2026-09-17 13:04:54),
  an empty v3 schema with two expired `maintenance_owners` rows named `actor-a`/`actor-b`,
  `project:a`/`project:b` — test fixtures. A pytest session with
  `LLM_WIKI_TEST_USE_EXTERNAL_STATE=1` and no `LLM_WIKI_STATE_ROOT` resolved its state root to
  the vault (`tests/conftest.py` defaulted to `VAULT_ROOT`).
- Fix in PR 38: doctor `adoption` check (error, with cause and path); `--repair` retires an
  empty, ownerless candidate to `run/coordinator-quarantine/`; the harness refuses a state
  root inside the vault. Repair run 2026-09-23 09:20:42: `retire_stray_candidate` →
  `run/coordinator-quarantine/20260923T092042Z-markdown-transactions-v3.candidate.sqlite3`;
  `adoption ok`; `compile_memory.py --dry-run` exit 0; project journals written again at 09:29.
- Research: `docs/research/2026-09-23-a-stray-candidate-stopped-the-memory-for-six-days.md`.

### A2. The nightly and weekly passes failed silently — Open
- `journalctl --user -u llm-wiki-nightly.service`: exit 1 every night 2026-09-18 → 09-23 at
  `take_scheduled_fence` with the A1 refusal; `llm-wiki-weekly.service` failed 2026-09-20 the
  same way. `run/state.json` still says `last_nightly_status = success` (2026-09-17).
- The session-start block said "Compile: the last run failed" and "48 captures lost" — never
  "the nightly has failed six nights" and never the cause. A failed maintenance pass has no
  path to the operator except `journalctl`.
- Class: a failed scheduled pass must record its failure in `run/state.json`
  (`last_nightly_status = failed`, the exception's head) and the session-start block must name
  it. Doctor's `scheduler` check reads that state, so it names it too.

### A3. Doctor hid the diagnosis — partly Fixed (A1 check), rest Open
- `run/state.json` is 354 509 B against doctor's 262 144-B bound, so the `scheduler` and
  `capture` checks report "could not be fully checked" instead of their findings. The file is
  large because `project_checkpoint_pending` holds 168 events for 91 projects (232 569 B) —
  events the refused checkpoints could not commit. The failure grew the file that would have
  named it. The product's own bound is 40 events × every project, which can exceed doctor's.
- `transactions`: "Transaction state is healthy", `quarantined: 27`; the database holds 117
  quarantined rows (114 `precondition_failed` of 2026-09-06/07, 3 `dlp_content_blocked`) and the
  scan reports `transaction_scan_truncated`. A truncated scan must not present its counts as
  the whole truth (fixed: the message says they are lower bounds; the status stays `ok` by the
  bounded-scan decision).
- `hooks`: 08:57 "220 hook failure(s) … still happening"; 09:20 "220 … none recently" — the
  recency verdict flips inside an hour on the same trail.
- `run_deletion`: always status `ok`; the adoption failure code sat in its `blockers`.

### A4. The session-start health block never measures — Open
- `logs/session-start-last.txt`: "Health was not measured: 10 of 19 checks did not run inside
  the 0.1s budget" — every session. A budget no check set can meet reports nothing; the block
  costs tokens and carries no information. Either raise the budget to what the cheap checks
  need (the `adoption`, `runtime`, `environment` checks take milliseconds) or drop the block.

## B. Corpus and retrieval

### B1. 94 % of the search index is project-journal JSON — Decided and changed (`d4ccd7b4`)
- Active generation `generation-18d5fd161ebe694b-f901256a`, `search.sqlite3`: 4 846 chunks —
  2 906 from `knowledge/projects/*/journal.md` (9 381 780 B), 984 from other project pages,
  956 from `knowledge/notes` (592 744 B). 2 021 chunks begin with `{`: canonical checkpoint
  JSON, one event per chunk.
- `corpus_snapshot._walk_knowledge` excludes session records because a measurement showed
  they "take the corpus over" (hit@5 0.7 → 0.0); journals were admitted with no measurement,
  and the 2026-09-10 decision `claim-readers-do-not-scan-the-journal` says the journal is an
  event log no claim reader consults. The retrieval corpus consults it 2 906 times.
- Cost: a warm search takes 2.38 s on an idle machine and 5.7–6.3 s under a load of 4 (the
  first figure was measured while a walk measurement was running); the reranker takes
  2.2 s of the 2.38 s over ten candidates. Without journals: 1.87 s and 1.8 s (−21 %,
  −18 %), 1 970 chunks instead of 4 846 — see the research note of the same day.
- Proposal: the memory generation carries `state.md`/`context.md` (the claim pages) and not
  `journal.md`; the journal stays authoritative on disk, greppable and consolidated nightly,
  exactly like session records. Measured before/after on the owner's 27 real questions.

### B2. Any directory a tool runs in becomes a project — Decided and changed (`d4ccd7b4`)
- `knowledge/projects/` holds 85 directories. 49 are `agent-<hash>` (all 2026-08-26, before
  `owning_checkout` unwrapped agent worktrees). Since then, projects were minted for: a
  benchmark run directory under `cache/benchmarks/`, a transaction directory under
  `run/transactions/`, a pytest temp directory under `/tmp`, a benchmark provider temp
  directory, the owner's home directory, and the vault itself — 2 937 events, last written
  today via `debounce_flush`, although `_require_not_the_vault` (2026-09-10, issue #20)
  exists.
- Mechanism: `integration_adapter._project_context` takes the hook payload's `cwd` and
  `session_start_project_state._compute_slug` names the project after that folder. Nothing
  resolves to the git top level, and nothing refuses a temp directory, a directory inside the
  vault, or the vault. These journals are then indexed (B1): the four largest projects
  hold 1 237, 779, 289 and 173 chunks.
- Proposal: project = git top level of `cwd` (an agent worktree still unwrapped); a `cwd`
  under the vault, under `/tmp`, or outside any repository maps to no project; existing junk
  directories are listed for the owner to retire (they are the owner's files).

### B3. The legacy FTS index can never be repaired — Open
- `doctor --repair` 09:20: "Index repair failed: rebuilt index did not validate as fresh";
  `run/state.json` says `last_index_rebuild_ok = True`. The rebuild collects 175 pages
  (`doctor._rebuildable_pages`, `rglob`), the freshness check collects 170
  (`search_memory._collect_pages("all")`, which drops `README.md` and four pages); membership
  differs, so the rebuilt index is stale by definition, every night, and the nightly's Step 3b
  rebuilds it again. The legacy index is only the fallback when no generation is active.
- Fix class: one collector for build and check.

### B4. Daily logs are not searchable until compiled — by design, noted
- The generation's policy is `daily_paths: []`; the nightly indexes compiled pages only
  (`docs/research/2026-08-28-longmemeval-first-number.md`). 25 daily logs exist (the
  session-start "346 daily logs" counts 320 receipts, see C6); 24 have receipts; the last
  committed compile is 2026-09-14. A question about the last nine days is answered from
  nothing until tonight's pass.

### B5. The graph walk has something to walk here — measurement, no change
- The live graph: 16 608 assertions, of which 16 103 join nodes without text (checkpoints,
  projects); 505 `LINKS_TO` edges join 115 notes. Daily logs are not in the graph.
- `tmp/walk/walk_live.py` on the owner's 27 real questions from the session records: the
  product's 12 candidates hold 10.6 notes on average; two hops of wikilinks reach 44 more
  notes (291 chunks); in 26 of 27 questions the best neighbour scores above the median of the
  12 under the same reranker. No gold exists on the live vault, so this is the precondition,
  not a gain. Proposal: a 20-question owner-labelled set before any walk is built.

## C. Runtime hygiene

### C1. 4.0 GB of generations, 12 never activated — Open
- `cache/evidence-graph/generations`: 15 directories, 99 MB–434 MB each, 4.0 GB. The nightly's
  `prune_generations` reports `12 pending activation` and removes only superseded ones; the
  pending ones are code generations of agent worktrees registered on 2026-09-14/15 and never
  activated. Nothing ever removes a registered-never-activated generation.

### C2. 80 LSP failure roots retained, pyright degraded — Open
- `run/lsp/`: 80 owner roots with `failure.json` `process_exited` (40 on 2026-09-13, 20 on
  09-14, 20 on 09-15). Doctor: "LSP runtime owners are bounded" (ok) and
  `pyright_manifest_predates_tree_digest` (degraded, `install_pyright.py` recommended).
  Failure roots are "left for the operator", who has no command to retire them.

### C3. Dead queue tasks keep no reason — Open (cause named 2026-09-23: the processor returns `False`, which carries no reason; exceptions already reach the trail)
- `run/queue-v3.sqlite3`: 25 `flush` tasks `dead` after 8 attempts each (`processor_failed`),
  `result_reference = None`; `attempt_history` holds 225 failed attempts against 45 succeeded
  (last failure 2026-09-12). `source_failures`: 43 rows, 34 `RuntimeError` from `compile` on
  August logs. Neither table stores the exception text; the operator cannot learn why five
  attempts fail for every one that succeeds.

### C4. `run/state.json` as an unbounded ledger — Open
- 354 509 B, 36 keys; `project_checkpoint_pending` 232 569 B, `codex_heartbeats` 4 506 B.
  Pending events drain only on a successful checkpoint of the same project, so junk projects
  (B2) keep theirs forever, and the file crosses doctor's bound (A3).

### C5. Self-update and install record — noted
- The nightly's Step 5 reports `update: current (none)`: the checkout is on branch `work` and
  updates only by the operator's fast-forward. `run/install/manifest.json` records release
  `78a729ea` (2026-08-22) while `95b02427` runs; the install record does not describe the
  running code.

### C6. The session-start block misinforms — Open
- "346 daily logs": `_count_md(DAILY_DIR)` recurses into `receipts/` (320 files). "82 active
  projects": counts the junk of B2. The guard-rails block prints a truncated rule ("a daily log
  larger than the compile input budget should be").
- `compile_memory.py --dry-run` wrote `last_compile_finished_at`/`started_at` into
  `run/state.json` (09:21); a dry run should not move the clock.

### C7. Windows CI shard died without a trace — Open, observing
- PR 38 run 35837737925, `windows_full::py3.14-s2`: exit 1 after its 101st test, no traceback,
  no progress file (its directory did not exist — fixed in PR 38). The 101st test is
  `test_a_lost_lease_stops_its_child`, which kills a child tree on Windows by a
  parent-pid walk (`memory_queue._descendants_of` over a Toolhelp snapshot) and `taskkill /T`;
  a parent-pid walk can name a reused pid. This is a hypothesis; the progress file will name
  the test next time.

### C8. Privacy and permissions — no finding
- `git ls-files knowledge` lists only `index.md`, `log.md`, `projects/_template/state.md`;
  `git check-ignore` denies `knowledge/projects/*` and `knowledge/daily/*`. `run/` and `cache/`
  are 0700, databases 0600. No secret pattern in `logs/hook-errors.log` or
  `logs/capture-failures.jsonl`. The privacy guard passes on the live tree (23/23).
- Hook cost: `session_start` 2.0 s wall, `post_tool_use` 0.24 s wall.

### C9. An in-process handler crashes at interpreter exit — Low
- Calling `mcp_server._tool_recall` in a plain Python process: 21.2 s cold (model load), then
  "terminate called without an active exception" and a core dump at exit. `search_memory.py`
  and a bare reranker load exit 0. The server's own shutdown path is the one to check.

### D1. The catch-up nightly of 09:29 failed on consolidation — Fixed (research note of the same day)
- `logs/nightly-2026-09-23.md`: Step 1b `claude backend exceeded 90s and was stopped`;
  `failures=1`. Consolidation used the provider client's 90 s default while the compile
  gives the same provider 300 s. Now 300 s for consolidation too.

### D2. `install_pyright.py` refuses to repair the install it is recommended for — Fixed
- `pyright_existing_install_invalid <- pyright_manifest_predates_tree_digest`, exit 1. A
  pre-era receipt now retires the directory and the pinned release installs fresh.

## What the owner decides
1. B1 — take `journal.md` out of the memory generation (structure change).
2. B2 — project identity by git top level; refuse temp, vault-internal and vault directories;
   retire the listed junk directories.
3. The order of the rest: A2, A3, B3, C1–C4, C6 are code fixes under the five rules and need
   no sign-off.
