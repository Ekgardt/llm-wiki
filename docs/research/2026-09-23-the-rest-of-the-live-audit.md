# The rest of the live audit

Dated 2026-09-23. Findings A2, A3, A4, B3, C1, C2, C4 and C6 (C3 examined, left open) of
`docs/AUDIT-2026-09-23-live.md`, fixed as one batch. A1, B1 and B2 have their own notes of
the same day.

Files: `scripts/scheduled_nightly.py`, `scripts/scheduled_weekly.py`,
`scripts/session_start_context.py`, `scripts/doctor.py`,
`scripts/integration_adapter.py`, `scripts/compile_memory.py`, `scripts/build_guardrails.py`,
`scripts/retire_lsp_evidence.py` (new),
`tests/test_the_rest_of_the_live_audit.py`, `CLAUDE.md`, `AGENTS.md`, `docs/STRUCTURE.md`,
`docs/research/2026-09-23-the-rest-of-the-live-audit.md`.

## What was found, and what each fix is

- **A2 — a pass that fails before it starts records nothing.** `scheduled_nightly.main` takes
  the fence first; when `take_scheduled_fence` raised the adoption refusal, the process
  exited 1 and `run/state.json` kept `last_nightly_status = success` from six nights before.
  `_record_nightly_result` already knows how to record a failure; it was only reachable from
  inside the pass. Fix: a failure raised while taking the fence is recorded the same way
  (nightly and weekly); session start already prints its `Nightly` line whenever the last
  status is `failed`, so the recording was the whole gap. Google's monitoring chapter names the rule:
  alert on symptoms the user sees, and keep the cause one hop away
  ([SRE book, Monitoring Distributed Systems](https://sre.google/sre-book/monitoring-distributed-systems/));
  here the symptom is the missing memory and the cause was six `journalctl` screens away.
- **A4 — the session-start health block "never measures".** It reads the nightly's
  `logs/doctor-report.json` first and measures for itself only when that is older than 36 h;
  six failed nights left no report, so every session printed "not measured" at the 0.1 s
  budget. With A2 the failed night is named; the budget stays as the suite pins it.
- **A3 — doctor hid its own findings.** (a) `_transaction_result` said "healthy" while the
  scan was truncated and 117 rows sat in quarantine: a truncated scan is now `degraded`
  with a message that its counts are lower bounds. (b) `MAX_STATE_BYTES` was 256 KiB while
  the product's own writer allows 40 pending checkpoint items per project
  (`MAX_PENDING_CHECKPOINT_ITEMS`); 91 projects made 354 KiB and silenced the `scheduler`
  and `capture` checks. The bound is now 4 MiB, above what the writer can produce on any
  vault the pending bound admits. (c) The "still happening"/"none recently" flip is the
  documented one-hour live window (`HOOK_ERROR_LIVE_SECONDS`), not a defect.
- **B3 — the legacy index could never be repaired.** `doctor._rebuild_index` collected pages
  with its own `rglob` (175) while the freshness check used `search_memory._collect_pages`
  (170), so a rebuilt index was stale by membership every night. One collector now serves
  both.
- **C3 — a dead task keeps no reason.** Not changed. The child already records a raised
  exception to the trail (`memory_queue._record_processor_failure`, 2026-08-28), and the
  live trail holds no such record for the 225 failed attempts: they were not exceptions.
  `_unsuccessful_worker_failure` names `processor_failed` whenever the processor *returns*
  `False`, and a `False` return carries no reason by construction. The fix belongs to the
  flush processor's outcome type, which is a separate change with its own note; the audit
  entry stays open and now names the exact cause.
- **C4 — `run/state.json` as an unbounded ledger.** Pending checkpoint events drain only when
  their project commits; a project that never commits again (every junk directory the B2
  rule now refuses) keeps its events forever. Pending events older than 30 days are dropped
  when the queue is next touched, and the drop is written to the same trail (kind
  `checkpoint_expired`), so nothing disappears in silence.
- **C6 — the session-start block misinforms.** "346 daily logs" counted `receipts/`;
  daily logs are now the top-level `YYYY-MM-DD.md` files. The guard-rails block printed a
  one-sentence summary cut at its first line; CommonMark joins a paragraph's continuation
  lines ([CommonMark 0.31.2, paragraphs](https://spec.commonmark.org/0.31.2/#paragraphs)),
  and the extractor now does the same. `compile_memory.py --dry-run` moved
  `last_compile_started_at`/`finished_at`; a dry run now leaves the clock alone.
- **C1 — 12 generations "never activated".** They are code generations of agent worktrees,
  registered-only by design (`docs/research/2026-09-15-a-code-generation-is-not-abandoned.md`)
  and retained while their checkout exists. Four such worktrees under `.claude/worktrees/`
  were left behind by finished subagents, clean and committed; they are removed, and the
  nightly's Step 3c' retires their generations. No code change.
- **C2 — 80 LSP failure roots retained for the operator.** The contract said roots holding
  `failure.json` are "left for the operator", who has no command for them, so they only
  grow. The nightly now retires failure roots older than 14 days whose owner is not live,
  keeping the newest 20 as evidence; the sentence in CLAUDE.md/AGENTS.md says so. The
  pyright manifest is re-qualified with the documented `install_pyright.py`.

## Cost, by rule 4

Every change is a bounded read or one extra line in an existing trail; nothing runs on
the answer path. The doctor's larger state bound costs at most 4 MiB of memory once per
pass.

## Sources

- [Monitoring Distributed Systems — Site Reliability Engineering](https://sre.google/sre-book/monitoring-distributed-systems/) — fetched 2026-09-23.
- [CommonMark Spec 0.31.2 — Paragraphs](https://spec.commonmark.org/0.31.2/#paragraphs) — fetched 2026-09-23.
- `journalctl --user -u llm-wiki-nightly.service`, `run/state.json`, `logs/doctor-report.json` on the live vault, read 2026-09-23.
