# Session Memory Operating Model

One-sentence summary: Session memory captures what Claude Code and the human learned while working, then compiles it into durable project memory.

## Raw layer
- `knowledge/daily/YYYY-MM-DD.md` stores captured session-end and (optionally) pre-compact summaries.
- Baseline path is the `SessionEnd` hook: just work and close Claude — `scripts/session_end_capture.py` spawns `flush_memory.py` and a daily-log entry lands automatically. No `/compact` required.
- The `PreCompact` hook is a safety net for long sessions that auto-compact; `/compact` is an **optional manual tool**, not part of the regular capture regimen.

## Compiled layer
- `knowledge/notes/` stores durable pages for decisions, patterns, debugging notes, concepts, and Q&A.

## Reliable write and evidence model
- Markdown remains authoritative. Runtime SQLite is coordination and derived state,
  never the knowledge source.
- Every automatic Markdown mutation uses recoverable before/after-hash transactions.
  Internal readers coordinate through the writer gate. External editors may briefly
  see a mixed tree; CAS guarantees apply only to cooperating transaction writers.
- Project `journal.md` is append-only and `state.md` is its deterministic projection.
- Compile planning uses an immutable source snapshot. A source append during an LLM
  call stays pending rather than being falsely covered by the receipt.
- Evidence references use logical daily ID, content hash, block, and byte span. The
  same fail-closed resolver reads either a flat daily source or a verified archive.
- Daily archive keeps a 90-day hot set, moves eligible sources into immutable
  uncompressed BagIt bags, and never archives source failures, uncompiled content,
  decision evidence, or manual pins.
- Atomic claim candidates that fail literal evidence or semantic agreement enter
  quarantine. The contradiction benchmark gates semantic supersession; there is no
  eager backfill or automatic semantic lifecycle mutation.
- Markdown, Git, and append-only project journals are authoritative. Evidence Graph,
  FTS, vectors, tiers, contextual artifacts, telemetry, and model caches are derived.
- Derived artifacts consumed together belong to one validated immutable generation.
  Registration validates source membership, hashes, schemas, integrity, and evidence;
  CAS activation is the publication point. Interrupted candidates do not replace the
  prior active generation.
- Readers recheck generation seals. Corruption falls back to a revalidated prior
  generation, then to labelled legacy retrieval or bounded live extraction.
- Grounded QA sends captured authoritative spans as untrusted evidence and verifies
  citation IDs, paths, source/span hashes, revisions, and ranges before accepting an
  answer or filing it back.

## Context and model evidence policy

- Complete context items are packed by safety/health/handoff/blocker/decision/evidence/
  history priority and relevance per token. Mandatory overflow fails closed.
- Token count source is always one of `reported`, `tokenizer`, `estimated`, `mixed`,
  or `unknown`. UTF-8 byte estimates are planning values, not exact model counts.
- New embedding/reranker defaults require pinned revisions and complete EN/RU/ZH
  quality, resource, license, regression, material-improvement, and Pareto evidence.
  The current model selection and real Graphify comparison are **evidence pending**.
- Deterministic comparative smoke results prove orchestration only. They do not prove
  Graphify parity, model superiority, quality improvement, or token savings.

## Operational boundaries
- Operational SQLite uses rollback-journal, `synchronous=FULL`, and no WAL on the
  current runtime. It requires a local filesystem; cloud-folder detection is
  best-effort and cloud/network runtime roots are unsupported.
- Queue delivery is at least once. Leases fence stale workers and operation IDs make
  supported side effects idempotent; exactly-once external effects are not promised.
- Default queue lease/heartbeat is 120/40 seconds, 8 attempts, retry base/cap
  30/3600 seconds, and short-lived worker limits 20 tasks/600 seconds/2 idle seconds.
  Transaction undo retention is 30 days. Runtime CLI flags provide explicit overrides.
- `run/` deletion is blocked by nonterminal/conflicted/quarantined transactions,
  source failure, the 30-day undo window, retained queue tasks/results, and any live
  project lease, writer, queue worker, or maintenance owner.
- There is no automatic Git operation, persistent daemon, cloud service, remote
  queue/cache, SQLite knowledge source, gzip archive tier, or automatic purge.

## Rules
- Not every chat detail deserves permanence.
- Save durable decisions, lessons, repeatable commands, architectural constraints, and gotchas.
- Keep project memory distinct from external-source research in `knowledge/notes/`.

## Compile Procedure

### When to run `/session-memory-compile`
- Optional — not required for the baseline "just work and close Claude" flow. The `SessionEnd` hook already captures the raw daily log; compilation into `knowledge/notes/` is a separate, deliberate step.
- Run it when a working session produced non-trivial decisions or lessons worth lifting.
- Run it before closing a multi-day initiative, to consolidate scattered daily notes.
- If `/compact` was used and a pre-compact summary landed in `knowledge/daily/`, that's a reasonable cue to compile — but `/compact` itself is never required.
- Skip if the day's daily log is only status chatter — nothing to lift.

### What from `knowledge/daily/` is worth lifting
Lift an item into `knowledge/notes/` only if it is:
- reusable beyond the session it came from,
- not already captured in code, config, or `knowledge/notes/`,
- specific enough to act on next time (not "we should be careful with X").

### Knowledge categories
All pages live flat under `knowledge/notes/<slug>.md` with a `type:` frontmatter field. The categories below describe that field, not subdirectories.
- **concepts** — project-specific mental models and vocabulary (e.g. "raw → inbox → wiki pipeline"). Noun-shaped.
- **decisions** — a choice made, with the alternatives rejected and the reason. Dated. Immutable once written; supersede rather than edit.
- **patterns** — a recurring approach that worked more than once ("when X, do Y because Z"). Verb-shaped.
- **debugging** — a concrete failure mode and its fix/diagnostic. Symptom → cause → resolution.
- **qa** — a question the human asked and its settled answer, when the answer is non-obvious and likely to be asked again.

If an item could fit two categories, prefer the more actionable one (patterns > concepts; debugging > qa).

### Do not lift into durable memory
- Status updates, task progress, "what I did today."
- Restatements of code, file paths, or structure discoverable by reading the repo.
- One-off chat preferences already covered by `CLAUDE.md` or auto-memory.
- Summaries of `knowledge/raw/` or `knowledge/inbox/` material — those belong in `knowledge/notes/`.
- Speculation not yet validated by use.

### Updating the indexes
For every new knowledge page:
1. Add a one-line bullet under the correct section in `knowledge/index.md` (format: `- [[knowledge/notes/<slug>]] — one-line hook`).
2. Append a dated entry to `knowledge/log.md` describing what was compiled and from which daily source(s).
3. Keep `knowledge/index.md` section bullets alphabetized within their section.

### `knowledge/daily/` vs `knowledge/notes/` boundary

**Two-question checklist** — answer these in order, first clear *yes* wins:

| # | Question | If yes → |
|---|---|---|
| 1 | Would this be useful to someone who has **never seen this repo's session history**? | `knowledge/notes/` |
| 2 | Does it cite `knowledge/raw/`, a public reference, or a named external author? | `knowledge/notes/` |

If both are **no** → keep it in episodic form (`knowledge/daily/`) until compile lifts a durable slice. That's it — don't overthink it.

Examples:
- "When `Edit` fails with multiple matches, expand `old_string` with preceding unique context." → both **no** as raw session chatter, but after compile becomes a notes page (`type: debugging`).
- "Karpathy's April 2026 pattern for LLM-maintained wikis works at ~100 articles without RAG." → both **yes** → `knowledge/notes/`.
- "Preliminary flagging as a vault convention" → may start as daily capture, then compile into a notes decision/pattern page.

**Expanded criteria** (use only when the two-question check is ambiguous):
- `knowledge/notes/` if the insight generalizes beyond a single session.
- `knowledge/daily/` if it is still episodic (session-bound, not yet distilled).

Rule of thumb: daily is first-person episodic capture; notes are third-person compiled knowledge.
