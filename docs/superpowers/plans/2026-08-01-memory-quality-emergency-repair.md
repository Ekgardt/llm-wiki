# Memory Quality Emergency Repair Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stop low-value or unscoped session material from entering durable memory, restore project-safe retrieval, and remove only explicitly reviewed, machine-verifiable garbage from the installed active corpus while preserving ambiguous history.

**Architecture:** Keep durable queue and compile state intact. Add deterministic admission and provenance checks at capture, compile, project-context, and retrieval boundaries; model output remains advisory. Repair installed content through a reviewed SHA-256 manifest that can physically remove only narrowly proven garbage, uses private source staging for transaction recovery, and purges those source bytes after commit.

**Tech Stack:** Python 3.10/3.13, Node.js 24, OpenCode SDK 1.18.10, pytest, Ruff, native file locks and atomic writes.

**Current evidence:** 2,358 daily blocks include 1,398 noise blocks, 1,371 shell telemetry blocks, and 1,626 unscoped records. The installed corpus has 75 physical notes but 43 logical pages, 32 duplicate typed copies, one usable handoff out of ten projects, and seven false feedback candidates. The 142-task production queue is out of scope for drain until this repair passes.

---

### Task 1: Add Executable Quality Use Cases

**Files:**
- Create: `tests/fixtures/memory_quality_cases.json`
- Create: `tests/test_memory_quality_contract.py`
- Modify: `tests/README.md`

- [ ] **Step 1: Add labeled fixtures**

Create deterministic cases for `FLUSH_OK`, `FLUSH_MINOR`, and `FLUSH_MAJOR`. Include status/progress, audit verdict, changed-file summary, shell telemetry, service prompt, actionable gotcha, durable decision with rationale, missing project provenance, duplicate idle transcript, template handoff, duplicate typed note, and grounded/ungrounded Q&A.

- [ ] **Step 2: Add behavioral contract tests**

Tests must call production parsers and validators, not search source strings. Each case asserts whether daily append, compile admission, project injection, and active retrieval are allowed.

- [ ] **Step 3: Verify RED**

Run:

```powershell
uv run pytest tests/test_memory_quality_contract.py -q
```

Expected: failures for classifier parity, unscoped compile admission, template injection, duplicate active notes, and ungrounded file-back.

---

### Task 2: Stop Noisy Session Capture

**Files:**
- Modify: `scripts/flush_memory.py`
- Modify: `scripts/llm-wiki-memory-opencode.js`
- Modify: `tests/test_flush_classification.py`
- Modify: `tests/test_integration_injection.py`

- [ ] **Step 1: Write failing Python classifier tests**

Require status/progress, audit verdicts, file/path/code summaries, navigation, service prompts, and shell telemetry to classify as `FLUSH_OK`. Require unrecognized output to fail classification instead of becoming arbitrary minor prose.

- [ ] **Step 2: Verify RED**

```powershell
uv run pytest tests/test_flush_classification.py -q
```

- [ ] **Step 3: Align the Python prompt and fail closed**

Use the OpenCode exclusion vocabulary in `flush_memory.py`. `_classify_response()` must accept only exact `FLUSH_OK` or a recognized MAJOR/MINOR token with a non-empty allowed-section body; malformed output raises `ValueError` and remains queued rather than being appended.

- [ ] **Step 4: Write failing idle-idempotency tests**

Create the same OpenCode session transcript twice and assert only one model classification and one daily append. Change the transcript and assert one new classification.

- [ ] **Step 5: Verify RED**

```powershell
uv run pytest tests/test_integration_injection.py -q
```

- [ ] **Step 6: Add transcript-digest idempotency**

Hash the bounded redacted transcript with Node `crypto`; retain the last digest per session in the plugin instance. Skip only exact repeats. Record the digest before provider work only after a successful durable append or exact `FLUSH_OK`, so failures remain retryable.

- [ ] **Step 7: Verify GREEN**

```powershell
uv run pytest tests/test_flush_classification.py tests/test_integration_injection.py -q
```

---

### Isolated Task 2A: Add Stable Exactly-Once Capture Identity

**Files:**
- Modify: `scripts/flush_memory.py`
- Modify: `scripts/memory_queue.py`
- Modify: `scripts/daily_log_append.py`
- Modify: `tests/test_flush_classification.py`
- Modify: `tests/test_memory_queue.py`

**Architecture:** Every newly built flush payload carries a canonical lowercase
64-hex `capture_id`. It is SHA-256 over compact, key-sorted UTF-8 JSON containing
`event`, `session_id`, `trigger`, `project_slug`, `project_root`, and the SHA-256
of the bounded normalized conversational transcript; `occurred_at` is excluded.
The existing queue-order lock reuses pending or processing flush tasks with the
same validated identity without allocating a sequence. Direct and deferred
writes share `<!-- llm-wiki-capture: <capture_id> -->`; legacy tasks without an
identity retain their queue-task marker and behavior.

- [x] **Write RED identity, concurrency, retry-window, and transcript tests**
- [x] **Verify RED before production edits**

```text
18 failed, 3 passed, 171 deselected
```

- [x] **Implement the minimal identity, queue reuse, marker, and transcript fix**
- [x] **Run focused pytest, Ruff on changed Python, and `git diff --check`**

```text
GREEN: 188 passed, 4 skipped
Ruff: All checks passed
Diff check: passed
Full-suite diagnostic: 1903 passed, 35 skipped, with only the two explicitly
out-of-scope test-count documentation guards failing (1921 documented vs 1940 live).
```

This isolated task does not update test-count references or any documentation
other than this emergency plan. It does not commit, install, deploy, start a
server, or touch the production queue.

#### Spec-review follow-up

OpenCode now derives the same canonical identity from its bounded redacted
transcript and sends both `captureId` and the matching marker to the locked
Python writer. Capture-marker deduplication scans the bounded daily-file
inventory globally while holding the shared append lock; legacy queue/direct
markers remain target-local. The stdin contract rejects a missing, malformed,
duplicated, or mismatched capture marker, and context injection hides capture
markers as idempotency metadata.

- [x] **Write RED tests for cross-runtime parity, fallback retry, cross-day dedupe, unsafe scan candidates, strict stdin validation, and context filtering**
- [x] **Verify RED before the spec-review production edits**

```text
8 failed, 5 passed, 1 skipped, 382 deselected
```

- [x] **Implement OpenCode parity, strict marker propagation, bounded global dedupe, and context filtering**
- [x] **Run review-specific and broader focused verification**

```text
Review-specific GREEN: 13 passed, 1 skipped, 382 deselected
Broader focused GREEN: 468 passed, 5 skipped
Final focused acceptance: 438 passed, 5 skipped
Full-suite diagnostic: 1912 passed, 36 skipped, with only the two explicitly
out-of-scope test-count documentation guards failing (1921 documented vs 1950 live).
Scoped full suite: 1912 passed, 36 skipped, 2 test-count guards deselected
Clean-clone import guards: 2 passed
Ruff: All checks passed
Node syntax check: passed
Diff check: passed
```

#### Canonical-root parity follow-up

**Files:**
- Modify: `scripts/llm-wiki-memory-opencode.js`
- Modify: `tests/test_integration_injection.py`

- [x] **Write a failing acknowledgement-loss integration test**

Pass OpenCode a native absolute worktree containing a lexical `..`, return the
resolved root in the bounded `codex_memory.py state-path --json` response, and
assert the direct payload, daily metadata, heartbeat, and fallback arguments
all use that canonical root. Compute the expected identity with Python and
apply the fallback queue task to prove one global capture remains.

- [x] **Verify RED**

```powershell
uv run pytest tests/test_integration_injection.py::test_opencode_ack_loss_fallback_queue_apply_is_one_capture -q
```

Expected: direct and fallback capture identities differ because the plugin
still hashes and persists the raw worktree.

```text
RED: 1 failed; direct projectRoot was the lexical `..` worktree instead of
the canonical root returned by state-path.
Precompact RED: 1 failed; project_root was `D:/project` instead of the
confirmed canonical root.
```

- [x] **Add a bounded project-identity helper**

Parse the `state-path --json` stdout only when its UTF-8 representation is
within the existing child-stdout limit. Require one JSON object with non-empty
`slug` and `cwd` strings, reject control characters and non-native-absolute
roots, and return `{ slug, projectRoot }`. Keep `computeSlug()` as a wrapper for
callers that only need the slug.

- [x] **Use canonical identity throughout idle capture**

Resolve identity once per idle event. Use `projectRoot` for `buildCaptureId`,
daily root metadata, `appendDaily`, every idle heartbeat, and
`flush_memory.py --project-root`; never fall back to the raw worktree for
capture provenance.

- [x] **Verify GREEN and static checks**

```powershell
uv run pytest tests/test_integration_injection.py -q
uv run ruff check scripts/ tests/
node --check scripts/llm-wiki-memory-opencode.js
git diff --check
```

```text
Canonical-root acknowledgement-loss GREEN: 1 passed
Precompact canonical-root GREEN: 1 passed
OpenCode integration GREEN: 68 passed
Broader focused GREEN: 438 passed, 5 skipped
Ruff: All checks passed
Node syntax check: passed
Diff check: passed with the existing LF-to-CRLF warning
```

#### Code-quality closure

**Files:**
- Modify: `scripts/flush_memory.py`
- Modify: `scripts/llm-wiki-memory-opencode.js`
- Modify: `scripts/daily_log_append.py`
- Modify: `tests/test_flush_classification.py`
- Modify: `tests/test_integration_injection.py`
- Modify: `tests/test_memory_queue.py`
- Modify: all currently changed test-count documentation

- [x] **TDD 1: Make Python the only capture-ID implementation**

Add RED CLI and OpenCode integration tests covering Unicode whitespace,
embedded controls, CR/LF normalization, bounded stdin, canonical-only stdout,
and direct-to-fallback parity. Add `flush_memory.py --capture-id` using the
existing provenance arguments and an 8 MiB transcript-stdin cap; then remove
the JS normalizer/hash implementation and require a canonical helper result.

- [x] **TDD 2: Separate source bytes from normalized conversation chars**

Add RED tests for an escaped 50,000-character JSONL message, an escaped
40,000-character whole JSON document, and input beyond 8 MiB. Scan/read at
most `8 * 1024 * 1024` source bytes, then apply `MAX_TRANSCRIPT_CHARS` to the
parsed normalized conversation.

- [x] **TDD 3: Read daily candidates through validated descriptors**

Add RED tests for unsafe daily directories, symlink/reparse candidates,
pre-open path swaps, special files, fd identity drift, and bounded fd reads.
Use `bind_atomic_writes_to_directory()` only as a scoped read-only directory
identity hold; open candidates with `O_NOFOLLOW`, `O_NONBLOCK`, and `O_BINARY`
where available, validate `lstat`/`fstat` identity and attributes before and
after reading, and never use path-based file reads.

- [x] **TDD 4: Bound and short-circuit the global scan**

Add RED tests proving no candidate after a validated match is opened and that
scanning fails closed after 64 MiB. Limit directory inventory to 4,096 entries,
individual files to 4 MiB, and aggregate bytes actually read to 64 MiB.

- [x] **TDD 5: Reject every malformed capture-prefix line**

Add RED stdin-contract tests for malformed-only, malformed-plus-canonical,
duplicate, missing, and mismatched capture lines. Require exactly one matching
canonical line when `captureId` is supplied and reject every capture-prefix
line when it is absent.

- [x] **Final count and verification**

Run focused RED/GREEN suites, collect the final platform-stable count, update
all changed count documents and local Windows pass/skip evidence, keep
`AGENTS.md` and `CLAUDE.md` byte-identical and the three READMEs count-synced,
then run the full suite, Ruff, Node syntax, clean-clone guards, and diff check.

No step commits, installs, deploys, starts a server, or accesses production.

```text
Capture helper RED: 2 failed
Capture helper GREEN: 2 passed; exact 8 MiB boundary follow-up passed
Transcript source-bound RED: 4 failed
Transcript source-bound GREEN: 6 passed
Descriptor/scan RED: 5 failed, 2 skipped
Descriptor/scan GREEN: 5 passed, 2 skipped
Exact aggregate-byte follow-up RED/GREEN: 1 failed (16 read vs 15 cap), then 1 passed
Strict-prefix RED: 2 failed, 2 passed
Strict-prefix GREEN: 4 passed; hidden-prefix follow-up RED/GREEN: 1 failed, then 1 passed
Platform-stable collection: 1983
Local Windows full suite: 1943 passed, 40 skipped
Ruff: All checks passed
Node syntax check: passed
```

#### Atomic capture publication follow-up

- [x] **Write RED hardlink, symlink/path-swap, final-bound, and exact-size tests**
- [x] **Retain validated target bytes and publish through bound `atomic_write()`**
- [x] **Stop descriptor reads at declared size and accept exactly 4 MiB**
- [x] **Run final full-suite and documentation verification**

```text
Focused RED: 3 failed, 1 passed, 1 skipped; deterministic replacement RED: 1 failed
Focused GREEN: 5 passed, 1 skipped
Relevant suites: 368 passed, 8 skipped
Ruff: All checks passed
Diff check: passed with the existing LF-to-CRLF warning
Platform-stable collection: 1983
Local Windows full suite: 1943 passed, 40 skipped
```

#### Final collision, marker, and digest follow-up

- [x] **Reject casefold-equivalent noncanonical target names before scanning**
- [x] **Match capture metadata only as a canonical standalone line**
- [x] **Escape reserved capture prefixes in prompts and model bodies**
- [x] **Own capture-ID failure fallback through `processIdleDigest()`**
- [x] **Run final relevant, full-suite, and documentation verification**

```text
Focused RED: 6 failed, 1 passed, 1 skipped
Focused GREEN: 7 passed, 1 skipped
Relevant suites: 629 passed, 10 skipped
Platform-stable collection: 1983
Local Windows full suite: 1943 passed, 40 skipped
Ruff: All checks passed
Node syntax check: passed
Diff check: passed with the existing LF-to-CRLF warning
```

#### Classified append failure follow-up

- [x] **Write RED tests for stdin fallback, ambiguous staged append, and queue failure**
- [x] **Route classified append exceptions through the existing atomic queue transaction**
- [x] **Preserve the bounded transcript, resolved provenance, and canonical capture ID**
- [x] **Verify queued replay no-ops after an ambiguous successful append**
- [x] **Run focused, relevant, full-suite, static, and count synchronization checks**

```text
Focused RED: 3 failed
Focused GREEN: 3 passed
Relevant suites: 221 passed, 9 skipped
Platform-stable collection: 1983
Local Windows full suite: 1943 passed, 40 skipped
Ruff: All checks passed
Node syntax check: passed
Documentation and clean-clone guards: 5 passed
```

---

### Task 3: Enforce Compile Admission and Project Provenance

**Files:**
- Modify: `scripts/compile_memory.py`
- Modify: `scripts/session_start_context.py`
- Modify: `tests/test_compile_bounded_batches.py`
- Modify: `tests/test_memory_quality_contract.py`

- [x] **Step 1: Write failing admission tests**

Reject operational FLUSH records lacking all of: completion marker, valid tier, source session, non-unknown slug, native absolute root, and at least one allowed durable section. Reject `FLUSH_OK`, status prose, file lists, and source quotes outside durable sections for both create and update operations. Keep explicit synthetic public fixtures as test-only legacy evidence.

- [x] **Step 2: Verify RED**

```powershell
uv run pytest tests/test_compile_bounded_batches.py tests/test_memory_quality_contract.py -q
```

- [x] **Step 3: Add deterministic admission**

Normalize daily blocks through the shared parser, return structured provenance with each admitted source, and filter before prompt assembly. Model counters cannot override rejection.

- [x] **Step 4: Carry project identity through operations**

Add `project_slug` and `project_root` to prepared source records and compile operations. Created notes receive both fields. Updates require matching project identity; missing, global, mixed, changed-root, and cross-project provenance is rejected.

- [x] **Step 5: Verify GREEN**

```powershell
uv run pytest tests/test_compile_bounded_batches.py tests/test_memory_quality_contract.py -q
```

```text
Admission RED: 9 failed, 552 passed, 18 skipped
Admission GREEN: 561 passed, 18 skipped
Project provenance RED: 7 failed
Project provenance GREEN: 7 passed
Journal/receipt RED: 2 failed; GREEN: 2 passed
Focused compile/context verification: 750 passed, 18 skipped
Platform-stable collection: 2053
Documentation guards: 68 passed, 3 skipped
Local Windows full suite: 2013 passed, 40 skipped
Ruff: All checks passed
Diff check: passed with the existing JavaScript LF-to-CRLF warning
```

#### Versioned admission hardening follow-up

The approved design uses immutable schema migration rather than in-place
rewrites. New manifests are version 3 and bind any reconciled legacy effects;
new journals are version 2. Version 2 manifests and version 1 journals have
separate hash-checking readers and are accepted only by migration code.

- [x] **TDD 1: Persisted artifact compatibility**

Add active, retired, and recovery-required fixtures that persist a valid v2
manifest with v1 journals. Assert applied effects are reconciled and bound into
the replacement v3 manifest, pending operations are never executed, legacy
artifacts are retired, active/progress state is invalidated, and preparation
returns a fresh v3 request. Corrupted hashes and version-specific field drift
must fail closed.

- [x] **TDD 2: Semantic and Markdown-context admission**

Add section-specific rejection/acceptance matrices for decisions, lessons,
commands, gotchas, and open questions. Reject status/progress, test counts,
audit/review verdicts, and changed-file/code/path summaries. Add fenced-code,
HTML-comment, and raw-block fixtures proving hidden headings and bullets never
render as compile evidence.

- [x] **TDD 3: Project path and frontmatter safety**

Reject C0/C1 controls, CR/LF, U+2028, U+2029, and invalid Unicode scalar values
in project identity. Assert both frontmatter fields are JSON-quoted YAML strings
and round-trip as strings for `true`, `null`, and `123`. Assert host-native path
comparison accepts equivalent Windows separator/case forms and rejects a
different canonical root.

- [x] **TDD 4: Linear admitted-source indexing**

Instrument record rendering, source joining, and evidence fact counting. Assert
one index is built per normalize/apply pass and calls are bounded by records plus
unique evidence, including repeated evidence across several operations. Pass the
same index into journal/source matching so project identity is not reparsed.

- [x] **TDD 5: Full verification and count synchronization**

Run every focused RED/GREEN command, collect the platform-stable total, update
all live count references, then run documentation guards, the complete suite,
`uv run --no-sync ruff check scripts/ tests/`, and `git diff --check`. Do not
commit, install, deploy, start a server, or access production.

```text
Compatibility RED: 6 failed; GREEN: 6 passed
Semantic/context RED: 11 failed, 16 passed; GREEN: 27 passed
Path/frontmatter RED: 13 failed, 10 passed; GREEN: 25 passed
Linear-index RED: 2 failed; GREEN: 2 passed
Focused compile/project contract: 713 passed, 19 skipped
Platform-stable collection: 2053
Documentation guards: 68 passed, 3 skipped
Local Windows full suite: 2013 passed, 40 skipped
Ruff: All checks passed
Diff check: passed with the existing JavaScript LF-to-CRLF warning
```

Research basis: CommonMark 0.31.2 fenced/HTML block rules,
https://spec.commonmark.org/0.31.2/; YAML 1.2.2 scalar resolution,
https://yaml.org/spec/1.2.2/; Python 3.14 JSON string serialization,
https://docs.python.org/3/library/json.html; Python host-native path
normalization, https://docs.python.org/3/library/os.path.html#os.path.normcase;
and OWASP syntactic plus semantic input validation,
https://cheatsheetseries.owasp.org/cheatsheets/Input_Validation_Cheat_Sheet.html.

#### Re-review persistence and admission closure

The approved closure keeps current schemas unshipped at manifest v3, journal
v2, and receipt v2 while making their evidence contract complete. Legacy
validation is frozen to the bytes produced by commit `93be6b8`: migration-only
v2 manifests and v1 journals are checked against the shipped parser, renderer,
packing, and operation shapes rather than reconstructed with current admission.
Verified v1 effects remain unscoped and byte-exact; current code must not invent
project fields that the shipped journal never contained.

Evidence identity is the SHA-256 of compact, key-sorted UTF-8 JSON containing
`daily_date`, `timestamp`, and the exact normalized durable bullet. Consumed
tokens are bounded and persist through legacy reconciliation, fresh v3
manifests, v2 journals, v2 receipts, replay boundaries, retired-journal replay,
and `state.json`. Admission reserves tokens from earlier accepted journals and
the current plan, and rejects reuse before any new journal or note mutation.
Successful publication merges tokens atomically into state; invalidating a
receipt never erases the durable barrier, and capacity exhaustion fails closed.

Current source admission builds one occurrence index while parsing admitted
records. Each complete normalized bullet maps directly to its durable quality
and project identity. Provider citations use exact dictionary lookup: substrings
never match, duplicate occurrences remain countable, and normalization, journal
validation, replay, execution, and receipt construction reuse the same bounded
index. Work is bounded by source records and indexed bullets plus at most
`MAX_PROVIDER_OPERATIONS * MAX_PROVIDER_EVIDENCE` lookups.

- [x] **TDD 1: Authentic shipped v2/v1 compatibility**

  Generate fixed artifact fixtures from the exact `93be6b8` source, including
  operations without project fields and byte-exact unscoped effects. First
  prove the current compatibility helper rejects or mis-derives them, then add
  a migration-only shipped reader. Pending work is retired, verified effects
  are preserved without re-execution, corrupt or field-drifted bytes fail
  closed, and fresh preparation returns current v3.

- [x] **TDD 2: Durable consumed-evidence admission barrier**

  Add RED tests for same-plan reuse, later batches, restart, retired journals,
  legacy migration, receipt invalidation/replay, successful publication, and
  bounded-capacity failure. Persist and validate canonical evidence tokens at
  every boundary; reject a consumed token before journal creation.

- [x] **TDD 3: Semantic paraphrases and CommonMark comments**

  Add RED section matrices for paraphrased completion, progress, audit/review,
  test-count, and changed-file reports. Add full-line, multiline, inline, and
  escaped-comment cases proving actual comments are hidden while visible text
  and `\\<!--` remain admissible when semantically durable.

- [x] **TDD 4: Unicode project-identity noncharacters**

  Add RED slug/root/frontmatter tests for `U+FDD0..U+FDEF` and every plane-end
  `U+nFFFE`/`U+nFFFF`, alongside existing controls, separators, and surrogates.
  Reject them before parsing, path comparison, JSON/YAML serialization, journal
  persistence, or note mutation.

- [x] **TDD 5: One-pass exact normalized evidence index**

  Add RED whole-bullet, substring-decoy, duplicate-quality, repeated-operation,
  and instrumentation tests. Replace raw block ranges and quote rescans with one
  exact occurrence map, remove substring-based evidence admission, and prove
  bounded work across normalization, replay, execution, and receipt validation.

- [x] **Final re-review verification and count synchronization**

  Run each focused RED/GREEN command, the compile/project acceptance suites,
  platform-stable collection, documentation guards, the complete suite, Ruff,
  clean-clone/import checks, and `git diff --check`. Update every live test-count
  reference and this evidence block. Do not commit, install, deploy, start a
  server, or access production.

```text
Exact-index RED: 6 failed, 9 passed, 590 deselected
Exact-index GREEN: 15 passed, 590 deselected
Prompt-contract RED: 1 failed
Prompt-contract GREEN: 1 passed
Full compile suite: 588 passed, 18 skipped
Quality contract: 35 passed
Project identity suite: 309 passed, 1 skipped
Session context suite: 176 passed
Compile audit suite: 68 passed
Platform-stable collection: 2272
Documentation and clean-clone guards: 4 passed
Local Windows full suite diagnostic: 2229 passed, 40 skipped, with only the
two stale test-count guards failing
Local Windows final full suite: 2232 passed, 40 skipped
Ruff: All checks passed
Node syntax check: passed
Diff check: passed with the existing JavaScript LF-to-CRLF warning
No commit, install, deploy, server start, or production access was performed.
```

Research checked 2026-08-02: CommonMark 0.31.2 block/inline HTML and backslash
escape rules, https://spec.commonmark.org/0.31.2/; YAML 1.2.2 character and
scalar rules, https://yaml.org/spec/1.2.2/; Unicode 17.0 controls, surrogates,
and noncharacters, https://www.unicode.org/versions/Unicode17.0.0/core-spec/chapter-23/;
Python 3.14.6 JSON and Unicode behavior,
https://docs.python.org/3/library/json.html and
https://docs.python.org/3/library/unicodedata.html; durable idempotency-token
guidance, https://martinfowler.com/articles/patterns-of-distributed-systems/idempotent-receiver.html
and https://docs.aws.amazon.com/wellarchitected/latest/reliability-pillar/rel_prevent_interaction_failure_idempotent.html.

#### Final review closure

The final review found five gaps in the preceding closure. Compatibility must
validate persisted v2 structure and immutable hashes without regenerating a
batch prompt with current code. Legacy substring citations that already caused
a durable effect must resolve against the frozen source block to one complete
bullet, or conservatively consume the entire source date/timestamp when no
unique bullet exists. Markdown comment handling must remain CommonMark-aware
and linear while preserving unmatched openers as visible text.

- [x] **TDD 1: Real v2 compatibility without current prompt comparison**

  Use `tests/fixtures/compile-v2-93be6b8.json`, generated by executing commit
  `93be6b8`, as the golden v2 manifest/v1 journal. Prove current validation
  rejects it only because the reconstructed batch prompt changed. Replace
  regeneration with bounded structural and relational checks over exact root,
  daily, layout, batch, generation, source, and hash fields. Treat persisted
  prompts as retired data: validate their type and bound, but never execute or
  compare them with a prompt generated by current code. Rehashed field drift
  must still fail closed.

- [x] **TDD 2: Safe legacy substring migration**

  Add unique, zero-match, ambiguous-match, restart, receipt-replay, retired
  journal, and capacity RED tests. Resolve each applied legacy citation against
  the frozen old source block. A unique admitted complete-bullet match consumes
  the current full-bullet token. Zero or multiple matches consume one bounded
  source/date/timestamp wildcard token, and current admission rejects every
  bullet covered by that wildcard. Never retain the old substring hash as the
  durable barrier.

- [x] **TDD 3: Remaining operational paraphrases**

  Add RED matrices for standalone finished/completed/migrated/changed/updated/
  added/removed summaries, test-suite-green wording, and file-path-plus-because
  reports. Reject those operational reports without weakening accepted
  section-specific decisions, lessons, commands, debugging notes, or questions.

- [x] **TDD 4: One-pass code-aware comment state machine**

  Add RED cases for comment-looking text inside single- and multi-backtick code
  spans, closed comments outside code spans, delimiters split around code spans,
  escaped openers, and unclosed openers. Scan each line once with explicit code
  span/comment state: closed real comments are hidden, code-span text is
  literal, and an opener that never closes remains visible rather than hiding
  the rest of the record.

- [x] **TDD 5: Character-visit scaling and final verification**

  Instrument the comment scanner with two probes: total input characters and
  scanner character visits. Assert visits remain a constant-factor bound for a
  long line containing many decoy openers/backtick runs and for a long
  multiline comment. Run focused RED/GREEN tests after every slice, then the
  compile/project acceptance suites, platform-stable collection, documentation
  guards, complete suite, Ruff, clean-clone/import checks, Node syntax, and
  `git diff --check`. Synchronize every live test count. Do not commit, install,
  deploy, start a server, or access production.

Closure evidence, 2026-08-02: the bounded ambiguous-match regression was
observed RED as `2 failed, 612 deselected`, then GREEN as `2 passed, 612
deselected`. The combined compatibility and memory-quality suites report `655
passed, 18 skipped`. Final collection reports `2336 tests collected`; the full
Windows suite reports `2296 passed, 40 skipped`. README/CHANGELOG count and
clean-clone/import guards report `8 passed`. Ruff reports `All checks passed!`,
Node syntax exits zero, and `git diff --check` exits zero with only Git's
existing JavaScript LF-to-CRLF working-copy notice.

Research rechecked 2026-08-02: CommonMark 0.31.2 specifies that backslash
escapes do not apply inside code spans and separates block parsing from inline
parsing, https://spec.commonmark.org/0.31.2/; Python 3.14.6 warns consumers to
bound untrusted JSON input and documents deterministic key-sorted serialization
for regression checks, https://docs.python.org/3/library/json.html.

#### Parser review closure

The remaining parser review requires unmatched comments to preserve source text
without granting it a path around fenced-code or raw-HTML admission. Operational
status detection must use bounded string/token work rather than whole-bullet
regex backtracking, and must distinguish reports from general durable rules.

- [x] **TDD 1: Replay unmatched comments through block admission**

  Add an exact unmatched-opener record followed by fenced and script-hidden
  durable bullets. Observe admission fail RED, then separate closed-comment
  stripping from the ordinary fence/raw-HTML state machine so unmatched text is
  preserved but hidden evidence remains unavailable.

- [x] **TDD 2: Context-aware operational status classification**

  Add first-person `We finished...` and `We changed scripts/...` RED cases and
  preserve completion, test-result, and file-path report rejection. Remove the
  unconditional leading-past-verb rule and admit `Changed schemas require
  versioned readers because persisted hashes must remain verifiable` as a
  durable general rule.

- [x] **TDD 3: Linear operational detection and final verification**

  Replace operational regex searches with fixed prefix checks and bounded token
  passes. Instrument input characters, character visits, token visits, prefix
  calls, and substring calls; assert explicit bounds over 20,000 repeated
  `audit` tokens. Run focused and complete suites, Ruff, clean-clone/import and
  documentation guards, platform-stable collection, Node syntax, and
  `git diff --check`. Synchronize all live counts without committing or touching
  production.

Closure evidence, 2026-08-02: unmatched-comment admission was observed RED as
`1 failed`, first-person/general-rule context as `3 failed, 20 passed, 36
deselected`, and the bounded-work interface as `1 failed`. Their focused GREEN
runs report `18 passed`, `23 passed, 36 deselected`, and `1 passed`; the complete
quality contract reports `59 passed`. Compile plus parser compatibility reports
`655 passed, 18 skipped`, and the wider compile/context acceptance run reports
`899 passed, 18 skipped`. Final collection reports `2336 tests collected`; the
complete Windows suite reports `2296 passed, 40 skipped`. README/CHANGELOG and
clean-clone/import guards report `8 passed`; Ruff reports `All checks passed!`,
Node syntax exits zero, and `git diff --check` exits zero with only Git's existing
JavaScript LF-to-CRLF working-copy notice.

Research rechecked 2026-08-02: CommonMark 0.31.2 gives block structure
precedence over inline structure and describes two-phase parsing,
https://spec.commonmark.org/0.31.2/#precedence; Python 3.14.6 documents greedy
regex repetition and backtracking and recommends string methods for simpler
text processing, https://docs.python.org/3.14/library/re.html and
https://docs.python.org/3.14/howto/regex.html#use-string-methods.

#### Generation-lineage and fail-closed re-review closure

The approved closure keeps manifest v3, journal v2, and receipt v2 unshipped.
Every v3 legacy reconciliation carries a bounded, unique, exact
`generation_lineage` list. Migration records the validated v2 generation ID
even when no legacy effect survived. Because the reconciliation is part of the
v3 manifest descriptor, the v3 generation ID cryptographically binds that
predecessor. A v2 receipt carries the same exact list. Replay may cross a
generation boundary only when the current validated manifest and candidate
receipt agree on the lineage and the old boundary generation is a member.

V1 receipts remain readable only when every referenced journal reconstructs
their consumed evidence exactly. Missing, unreadable, inconsistent, malformed,
or over-capacity evidence raises before a replay boundary is persisted, a new
manifest is created, a note operation executes, or the index rebuilds. No
empty-evidence fallback remains.

Operational-policy normalization removes bounded CommonMark-style opening,
closing, and self-closing HTML tag lexemes regardless of tag name or attributes.
The scanner runs only in `_visible_policy_text`, after block/fence/comment
filtering, and shields complete code spans. Malformed tags, escaped openers,
comparison operators, and code examples remain literal.

- [x] **TDD 1: Bind real v2/v1 migration lineage into v3 completion**

  Add RED coverage for the authentic `93be6b8` fixture entering through
  `compile_index_pending`, migrating, and completing with the old v2 generation
  in both the replacement manifest and final receipt. Assert the v3 generation
  changes if lineage changes, and reject duplicate, malformed, oversized, or
  unlinked lineage.

- [x] **TDD 2: Permit only cryptographically linked cross-generation replay**

  Add RED tests proving an old receipt boundary is satisfied by a new v3
  receipt only when the validated manifest and receipt contain the same exact
  predecessor. Reject a receipt-only claim, a manifest-only claim, a different
  predecessor, and a current generation mismatch.

- [x] **TDD 3: Fail closed when v1 evidence cannot be reconstructed**

  Replace the unavailable-journal empty fallback test. Add missing,
  inconsistent, and unreadable v1-journal restart probes with note and index
  sentinels. Assert preparation raises before state, manifest, journal, note, or
  index mutation.

- [x] **TDD 4: Remove arbitrary bounded HTML tag lexemes only in policy text**

  Add exact `<a ...>Status:</a>` and `<span .../>Status:` RED probes. Add
  accepted comparison, escaped-tag, inline-code, and fenced-code counterexamples.
  Implement a bounded scanner without whole-input backtracking.

- [x] **Final verification and count synchronization**

  Run each RED/GREEN slice, the compile and policy acceptance suites,
  platform-stable collection, documentation guards, the complete suite, Ruff,
  clean-clone/import checks, Node syntax, and `git diff --check`. Synchronize all
  live counts. Do not commit, install, deploy, start a server, or access
  production.

Research rechecked 2026-08-02: W3C PROV identifies object identity, versioning,
and derivation as core provenance requirements,
https://www.w3.org/TR/prov-overview/; Python 3.14.6 guarantees SHA-256 and
lowercase hexadecimal digests, https://docs.python.org/3/library/hashlib.html;
the Idempotent Receiver pattern requires exact persisted request identity,
https://martinfowler.com/articles/patterns-of-distributed-systems/idempotent-receiver.html;
OWASP recommends explicit handling of unexpected failure modes,
https://cheatsheetseries.owasp.org/cheatsheets/Error_Handling_Cheat_Sheet.html;
and CommonMark 0.31.2 separates block parsing from inline raw HTML and treats
code spans as literal, https://spec.commonmark.org/0.31.2/#raw-html.

Closure evidence, 2026-08-02: lineage and linked-replay coverage was observed
RED as `7 failed, 4 passed`, then GREEN as `11 passed`; an unhashable lineage
probe was separately RED as `1 failed, 4 passed`, then GREEN as `5 passed`.
Missing, inconsistent, and unreadable v1 evidence was RED as `4 failed`, then
GREEN as `4 passed`; the wider legacy/receipt/replay slice reports `70 passed`.
Arbitrary HTML policy probes were RED as `6 failed, 19 passed`, then GREEN as
`25 passed`; the skipped-code-span regression was RED as `1 failed`, then GREEN
as `1 passed`, and the complete quality contract reports `87 passed`. Final
compile plus policy acceptance reports `733 passed, 18 skipped`.
Context, audit, guardrail, and pending-work acceptance reports `416 passed`.
Final collection reports `2387 tests collected`; the complete Windows suite
reports `2347 passed, 40 skipped`. README/CHANGELOG count and clean-clone/import
guards report `9 passed`. Ruff reports `All checks passed!`, Node syntax exits
zero, and `git diff --check` exits zero with only Git's existing JavaScript
LF-to-CRLF working-copy notice. No commit, install, deploy, server start, or
production access was performed.

#### Second re-review: exact journal provenance and uncapped linear tag scanning

**Files:**
- Modify: `scripts/compile_memory.py`
- Modify: `scripts/session_start_context.py`
- Modify: `tests/test_compile_bounded_batches.py`
- Modify: `tests/test_memory_quality_contract.py`
- Modify: live test-count documentation after collection changes

**Architecture:** Keep effect-producing legacy `journal_ids` separate from a
bounded `predecessor_journal_ids` lineage. Migration records the ordered union
of every structurally valid old manifest batch ID and matching validated v1
receipt boundary journal ID. V3 identity binds that exact structure; the final
v2 receipt contains the ordered union of predecessor and current journals,
while effects continue to name only journals that actually produced effects.
Cross-generation replay requires the old boundary journals to be represented
in predecessor lineage and requires the candidate receipt to equal the
manifest-derived union.

Replace the policy tag helper's 32-attribute and 1024-character limits with one
forward quote-aware state machine over the already bounded policy line. A tag
candidate is removed only after its complete syntactically valid closing `>` or
`/>` is seen. On malformed syntax, resume at the first unconsumed character so
the input is preserved without rescanning a prefix; on an unclosed quoted or
otherwise valid prefix, preserve the remainder. Keep complete code spans and
escaped `<` characters literal. Test-only scan counters prove character visits
grow linearly.

- [x] **TDD 1: Preserve mixed effectful/effectless v1 journal provenance**

  Build a genuine two-batch v2 manifest, one applied v1 journal with an effect,
  one complete v1 journal with an empty operation list, and a v1 receipt naming
  both. First prove `is_compile_receipt_valid()` accepts it. Invalidate only its
  index evidence, migrate and complete the replacement v3 generation, then
  assert `predecessor_journal_ids` retains both IDs, effectful `journal_ids` and
  effects retain only the first, and the final valid receipt contains both old
  IDs plus every current batch ID. Add malformed, duplicate, over-capacity, and
  replay-union rejection probes for the new exact field.

- [x] **TDD 2: Remove arbitrary policy-tag caps with linear scanning**

  Add RED probes for a valid 33-attribute opening tag and a valid quoted tag
  exceeding 1024 characters. Add malformed, unclosed, comparison, escaped, and
  code counterexamples around long tags. Add a doubling probe using scan
  counters and require total character visits to remain a fixed linear multiple
  of the post-entity-decoding input length.

- [x] **Verification and count synchronization**

  Run each new test alone for RED and GREEN, the full compile/policy acceptance
  files, platform-stable collection, the complete suite, Ruff, documentation
  guards, Node syntax, and `git diff --check` with `uv run --no-sync`. Synchronize
  all live counts. Do not commit, install, deploy, start a server, or access
  production.

Research rechecked 2026-08-02: W3C PROV calls derivation, versioning, and the
provenance of provenance core requirements, https://www.w3.org/TR/prov-overview/;
CommonMark 0.31.2 specifies raw HTML tags and literal escaped/code syntax,
https://spec.commonmark.org/0.31.2/#raw-html; OWASP recommends syntactic
validation and warns about denial-of-service from pathological text matching,
https://cheatsheetseries.owasp.org/cheatsheets/Input_Validation_Cheat_Sheet.html;
and Python 3.14.6 documents list accumulation plus `str.join()` as the linear
alternative to repeated immutable-string concatenation,
https://docs.python.org/3/library/stdtypes.html#common-sequence-operations.

Closure evidence, 2026-08-02: the real mixed effectful/effectless v1 receipt
reached the intended RED failure as `1 failed` with final publication reporting
`compile effect receipt does not satisfy replay boundary`; the new exact schema
and linked replay acceptance also failed before implementation. The lineage
slice then passed, and the complete compile file reported `657 passed, 18
skipped`. The 33-attribute, over-1024-character, unclosed-quote, and scaling
probes were RED as `4 failed`, then GREEN as `4 passed`; the complete quality
contract reports `91 passed`. Whole-diff review added a same-generation
boundary-union counterexample, observed RED as `1 failed, 3 passed`, then GREEN
as `5 passed` with linked acceptance. Final compile plus policy acceptance is
`749 passed, 18 skipped`; collection is `2403`; the complete Windows suite is
`2363 passed, 40 skipped`. Live counts are synchronized. No commit, install,
deploy, server start, or production access was performed.

---

### Task 4: Reject Placeholder Project Context

**Files:**
- Modify: `scripts/bootstrap_project.py`
- Modify: `scripts/build_context.py`
- Modify: `scripts/session_start_context.py`
- Modify: `scripts/session_start_project_state.py`
- Modify: `tests/test_bootstrap_project.py`
- Modify: `tests/test_context_noise.py`
- Modify: `tests/test_integration_injection.py`
- Modify: `tests/test_project_state.py`
- Modify: `tests/test_slug.py`
- Modify: live test-count documentation after collection changes

- [x] **Step 1: Write failing tests**

Assert that literal template placeholders, invalid ownership tuples, dead-process-only handoffs, and stale bootstrap provenance are not injected as trusted project context. A valid current handoff remains first priority.

- [x] **Step 2: Verify RED**

```powershell
uv run pytest tests/test_context_noise.py tests/test_project_state.py -q
```

- [x] **Step 3: Add shared validity checks**

Treat template markers as absent content. Require a canonical project root JSON and runtime slug match before trusted injection. Label stale bootstrap data unavailable rather than presenting it as current.

- [x] **Step 4: Verify GREEN**

```powershell
uv run pytest tests/test_context_noise.py tests/test_project_state.py -q
```

Research checked 2026-08-02: W3C PROV identifies entity identity, derivation,
and revision as provenance requirements, https://www.w3.org/TR/prov-overview/;
Python 3.14.6 documents bounded subprocess timeouts and canonical path
resolution, https://docs.python.org/3/library/subprocess.html#subprocess.run and
https://docs.python.org/3/library/pathlib.html#pathlib.Path.resolve; and Git
documents exact repository-root and commit verification through `rev-parse`,
https://git-scm.com/docs/git-rev-parse.

Spec-review research checked 2026-08-02: Git documents repository-routing
environment variables at https://git-scm.com/docs/git#_environment_variables;
Python documents descriptor-relative/no-follow opens and file metadata at
https://docs.python.org/3/library/os.html#os.open and
https://docs.python.org/3/library/stat.html; Windows documents reparse-point and
sharing semantics for `CreateFileW` at
https://learn.microsoft.com/en-us/windows/win32/api/fileapi/nf-fileapi-createfilew;
and CommonMark 0.31.2 defines fenced code as literal block content at
https://spec.commonmark.org/0.31.2/#fenced-code-blocks.

Closure evidence, 2026-08-02: the initial ownership, placeholder, process-only,
and stale-bootstrap slice was RED as `16 failed, 184 passed, 1 skipped`, then
GREEN as `202 passed, 1 skipped`. Whole-diff review added legitimate-PID and
parent-repository freshness counterexamples, observed RED as `2 failed`, then
GREEN as `2 passed`.

Spec-review follow-up, 2026-08-02: eight additional findings were handled with
RED-before-GREEN tests for ambient Git repository routing, exact root-scoped
pages and heartbeats, visible canonical ownership metadata, strict bootstrap
frontmatter, structural headings, handoff-first clipping, live-PID-aware noise
filtering, and identity-bound no-follow state reads. The first focused integration
run exposed six legacy test seams; after preserving the inventory seam and moving
bounded-read observation to `os.fdopen`, the project-context slice reported `733
passed, 8 skipped`. Final whole-diff review then added fenced/commented template
publication counterexamples, observed RED as `2 failed`, and reached GREEN as `9
passed`; the final project-context slice reports `735 passed, 8 skipped`.

Collection reports `2468 tests`; documentation and structure guards report `32
passed`; clean-clone/import guards report `2 passed`; and the complete native
Python 3.13 Windows suite reports `2426 passed, 42 skipped`. Ruff reports `All
checks passed!`, Node syntax exits zero, `AGENTS.md` is byte-identical to
`CLAUDE.md`, and `git diff --check` exits zero with only the existing JavaScript
LF/CRLF warning. The existing Python 3.10 interpreter has no pytest; its matrix
was not run because installing or synchronizing dependencies was explicitly out
of scope. No commit, install, deploy, server start, or production access was
performed.

Re-review closure, 2026-08-02: five follow-up findings were handled with
RED-before-GREEN coverage for paired slug/root guardrail and advisory scope,
canonical ownership tuples that ignore legacy root-looking history, structural
pending-description suppression when a project root contains a backtick,
partial first-line handoff clipping, and section-aware standalone dead-PID
filtering. The focused RED/GREEN runs were `3 failed` to `4 passed`, one
parameterized ownership failure to `9 passed`, `1 failed` to `3 passed`, `1
failed` to `4 passed`, and `1 failed, 1 passed` to `6 passed`, respectively.
The complete project-state/context/guardrail slice reports `398 passed, 2
skipped`.

The follow-up research also checked Python 3.14.6 process-probe semantics at
https://docs.python.org/3/library/os.html#os.kill and CommonMark 0.31.2 code-span
delimiter behavior at https://spec.commonmark.org/0.31.2/#code-spans. Final
collection reports `2477 tests`; the complete native Python 3.13 Windows suite
reports `2435 passed, 42 skipped`. Full Ruff reports `All checks passed!`, Node
syntax exits zero, `AGENTS.md` remains byte-identical to `CLAUDE.md`, and `git
diff --check` exits zero with only the existing JavaScript LF/CRLF warning. The
Python 3.10 matrix remains unavailable without installing pytest, which is out
of scope. No commit, install, deploy, server start, or production access was
performed.

Final ownership follow-up, 2026-08-03: exact preservation/trust coverage for
`Project root JSON migration...`, `Runtime slug JSON migration...`, `Project
root migration...`, and the corresponding invalid-value prose forms was RED as
`6 failed`, then GREEN as `6 passed`. Ownership recognition and suppression now
require an exact colon-delimited key and a syntactically valid JSON string or
legacy backtick value. Separate exact-key declaration guards retain fail-closed
behavior when malformed canonical metadata is the only available claim, without
allowing prose to invalidate a valid canonical tuple. Template publication also
preserves non-metadata key-prefix prose and invalid legacy value text.

Research checked Python 3.14.6 whole-string matching and JSON decoding at
https://docs.python.org/3/library/re.html#re.Pattern.fullmatch and
https://docs.python.org/3/library/json.html#json.JSONDecoder.raw_decode, plus RFC
8259 colon and string grammar at https://www.rfc-editor.org/rfc/rfc8259. The
focused ownership/context slice reports `561 passed, 3 skipped`; collection
reports `2483 tests`; and the complete native Python 3.13 Windows suite reports
`2441 passed, 42 skipped`. Full Ruff reports `All checks passed!`, Node syntax
exits zero, and operational count guards report `2 passed`. No commit, install,
deploy, server start, or production access was performed.

---

### Task 5: Canonicalize Retrieval and File-Back

**Files:**
- Modify: `scripts/vault_editorial.py`
- Modify: `scripts/rebuild_memory_index.py`
- Modify: `scripts/search_memory.py`
- Modify: `scripts/query_memory.py`
- Modify: `tests/test_search_ranking.py`
- Modify: `tests/test_memory_quality_contract.py`

- [x] **Step 1: Write failing active-page tests**

Given flat and typed pages with the same normalized title/slug, select one deterministic canonical page, preferring the active flat page. Exclude archived and superseded pages everywhere. Apply authority order `user > web > ai-derived > inferred`, then confidence.

- [x] **Step 2: Write failing file-back tests**

Require at least one resolved source-page citation and exact evidence before writing Q&A. Refuse overwrite of an existing slug unless the existing page is explicitly superseded through the normal lifecycle.

- [x] **Step 3: Verify RED**

```powershell
uv run pytest tests/test_search_ranking.py tests/test_memory_quality_contract.py -q
```

- [x] **Step 4: Add one shared active-note selector**

Use it from index rebuild and search collection. Return duplicate diagnostics, but never index both canonical and shadow copies.

- [x] **Step 5: Ground file-back in source pages**

Load selected source pages, cite them in the generated page, validate the citations, and use atomic create-only publication.

- [x] **Step 6: Verify GREEN**

```powershell
uv run pytest tests/test_search_ranking.py tests/test_memory_quality_contract.py -q
```

Task 5 closure evidence, 2026-08-03: the unchanged focused baseline was `103
passed`. Canonical selection, retrieval filtering, grounded answer, exact
citation/evidence, create-only publication, and side-effect ordering coverage
was first observed RED as `32 failed, 103 passed, 1 skipped`, then GREEN as
`135 passed, 1 skipped`. Whole-diff review added bounded-source exposure and
editorial parity probes (`2 failed` then `2 passed`), partial-index and filtered
RRF rank plus malformed-trust probes (`3 failed` then `3 passed`), and an active
typed-slug collision probe (`1 failed` then `1 passed`). The final required
focused command reports `140 passed, 1 skipped`.

The shared selector now uses capped no-follow inventory and descriptor-validated
UTF-8 reads, rejects malformed sensitive metadata, excludes editorial/archive/
inactive pages, and chooses one flat-first, authority-then-confidence canonical
page with bounded deterministic diagnostics. Search and Markdown index use that
same set; stale FTS manifests detect winner changes, and FTS/vector/graph fusion
cannot restore shadows. File-back validates exact ROOT-relative POSIX paths and
verbatim quotes only against the canonical bounded source snapshots exposed to
the provider, rejects active slug collisions and every existing target, and
publishes only through a bound absent-target atomic create before rebuild/log.

Final collection is `2576`; the native Python 3.13 Windows suite reports `2533
passed, 43 skipped`. Ruff reports `All checks passed!`; Node syntax exits zero;
documentation, tracked-import, untracked-import, and AGENTS/CLAUDE equality
guards report `5 passed`; and `git diff --check` exits zero with only the
pre-existing JavaScript LF/CRLF working-copy notice. No commit, staging,
installation, deployment, server start, production-data access, or production
queue access was performed.

Research rechecked 2026-08-03: W3C PROV identifies identity, attribution,
derivation, validation, and versioning as provenance foundations,
https://www.w3.org/TR/prov-overview/; Python 3.14.6 documents portable
descriptor operations, platform-specific open flags, and `OSError` failure
semantics, https://docs.python.org/3/library/os.html#os.open; and Python 3.14.6
guarantees SHA-256 while `hexdigest()` emits hexadecimal text,
https://docs.python.org/3/library/hashlib.html.

Spec-review follow-up, 2026-08-03: fourteen behavioral cases were added after
the prior closure. Synthetic truncation evidence (`1 failed`), outside-vault
source masquerading plus a canonical winner becoming a shadow (`2 failed`),
same-byte file replacement (`1 failed`), fabricated provider exposure (`1
failed`), empty logical identities (`1 failed`), frontmatter/fence/comment/raw
HTML H1 decoys (`4 failed`), and independently repeated citation paths or
quotes (`2 failed`) were each observed RED before their production change.
Two production `search()` tests for stale FTS, vector, and graph paths passed
immediately, confirming the implementation was sound and the gap was behavioral
coverage. The final focused command reports `154 passed, 1 skipped`.

Prompt rendering metadata is now separate from real exposed source text.
Publication performs one fresh canonical selection and matches every cited
provider snapshot by exact root-relative path, filesystem identity, SHA-256,
content, and fresh quote membership before mutation. Empty normalized title and
slug identities fail closed with capped diagnostics; title extraction reuses the
existing Markdown visibility scanner; and citation paths and quotes are each
unique. Final collection is `2590`; the native Python 3.13 Windows suite reports
`2547 passed, 43 skipped`. Full Ruff and Node syntax checks pass. Research
rechecked CommonMark 0.31.2 block/ATX rules at
https://spec.commonmark.org/0.31.2/ and Python 3.14.6 path identity/resolution
semantics at https://docs.python.org/3/library/pathlib.html. No commit, staging,
installation, deployment, server start, production-data access, or production
queue access was performed.

Second spec-review follow-up, 2026-08-03: twenty-two behavioral cases were
added. The first stale-row fixture passed and was tightened until its SQL top
three were provably stale, then failed as intended (`1 failed`). Caller-expanded
120-character source exposure (`1 failed`), whitespace-only newline/space/tab
evidence (`3 failed` after correcting the fixture), BOM-prefixed frontmatter H1
decoy handling (`1 failed`), and unsafe selector paths (`4 failed`) were each
observed RED before production changes. Citation syntax reported `6 failed, 5
passed`: C1, U+2028/U+2029, surrogates, and noncharacters were the missing
categories, while existing C0, CR/LF, root, and traversal rejection stayed
green. A BOM raw-byte aggregate-bound review case then failed before the
snapshot accounting fix (`1 failed`). The focused command now reports `176
passed, 1 skipped`.

FTS now joins a bounded temporary table populated from the validated canonical
inventory before ranking and limiting. Prompt generation and publication share
one bounded source-preparation function; publication derives exposure from fresh
canonical bytes and rejects any caller mismatch. Evidence requires non-whitespace
content. The bounded reader strips one leading UTF-8 BOM while hashing the exact
raw bytes. Selector and citation paths share one exact ROOT-relative POSIX
validator, and unsafe selector diagnostics are ASCII-escaped and capped. Final
collection is `2612`; native Python 3.13 Windows verification reports `2569
passed, 43 skipped`. Full Ruff and Node syntax checks pass. Research rechecked
SQLite SELECT ordering/filtering and temporary-table semantics at
https://www.sqlite.org/lang_select.html and
https://www.sqlite.org/lang_createtable.html, Python 3.14.6 parameterized bulk
execution at https://docs.python.org/3/library/sqlite3.html, and UTF-8 signature
decoding at https://docs.python.org/3/library/codecs.html. No commit, staging,
installation, deployment, server start, production-data access, or production
queue access was performed.

Final spec-review follow-up, 2026-08-03: twelve behavioral cases were added.
Trailing source newlines that were evidence-validatable but absent from the
rendered body failed RED (`1 failed`). Backtick-bearing selector and citation
paths each failed RED (`1 failed` each), while an accepted bracket/space path
already serialized with exactly two backtick delimiters (`1 passed`). The eight
requested invalid-grounding cases all passed immediately as characterization
coverage: exact nonexistent, archived, and superseded sources; empty,
non-string, oversized, and control-bearing evidence; and duplicate JSON keys.
Those tests instrument publication, rebuild, and log functions and prove zero
side effects; the duplicate-key case also proves the `object_pairs_hook`
diagnostic. The focused command reports `188 passed, 1 skipped`.

Source preparation now computes one post-truncation, post-`rstrip()` body and
uses that exact string for provider rendering, aggregate budget accounting, and
evidence validation. The shared path validator rejects the single backtick that
would terminate the chosen source-line code span; existing control/newline and
backslash rejection covers the other syntax-breaking characters. Final
collection is `2624`; native Python 3.13 Windows verification reports `2581
passed, 43 skipped`. Full Ruff and Node syntax checks pass. Research rechecked
CommonMark 0.31.2 code-span delimiter behavior at
https://spec.commonmark.org/0.31.2/#code-spans and Python 3.14.6 ordered JSON
pair hooks at https://docs.python.org/3/library/json.html. No commit, staging,
installation, deployment, server start, production-data access, or production
queue access was performed.

Final two-finding follow-up, 2026-08-03: ten behavioral cases were added. The
exact inline-comment H1 suite reported `2 failed, 2 passed`: comment-prefix and
multiline-comment-close text synthesized false headings, while a real H1 with a
trailing comment and ordinary prefixed prose already behaved correctly. The
integrated `topic#fragment.md` selector/index test failed RED (`1 failed`), and
the five `#`, `|`, `^`, `[`, and `]` citation cases failed RED (`5 failed`). An
initial focused run then exposed an obsolete positive fixture that used brackets;
after changing that accepted fixture to parentheses, the focused command reports
`198 passed, 1 skipped`.

H1 title extraction now requires an ATX opener on the original aligned line
before reading comment-filtered visible text, preventing inline comment removal
from creating block syntax while preserving frontmatter, fence, raw HTML,
multiline comment, BOM, and linear scanning behavior. The shared safe-path
predicate now rejects every delimiter that changes the generated wikilink target
or Evidence code span: `` ` ``, `#`, `|`, `^`, `[`, and `]`. Unsafe pages are
excluded with diagnostics before index generation; accepted links remain exact.
Final collection is `2634`; native Python 3.13 Windows verification reports
`2591 passed, 43 skipped`. Full Ruff and Node syntax checks pass. Research
rechecked Obsidian internal-link target syntax at https://help.obsidian.md/links
and CommonMark 0.31.2 ATX heading structure at
https://spec.commonmark.org/0.31.2/#atx-headings. No commit, staging,
installation, deployment, server start, production-data access, or production
queue access was performed.

Nine-finding reliability follow-up, 2026-08-03: twenty-six behavioral tests
were added with sequential RED-to-GREEN evidence. Provider paths and bodies are
secret-safe while raw/sanitized quote intersection remains fileable; prospective
Q&A pages cannot displace canonical winners and are sized by exact UTF-8 bytes;
descriptor snapshots reject hardlinks and retain full file identity, raw bytes,
and versioned inventory/canonical generations. Compile and Q&A writers share one
reentrant cross-process publication lock in compile-before-publication order,
and filed evidence records the validated source SHA-256.

Markdown, FTS, vector, graph, temporal filtering, and benchmark generation now
consume one immutable active-note selection. Derived artifacts persist versioned
canonical-generation metadata, preserved-mtime content changes rebuild caches,
and post-validation SQLite corruption closes every connection before one
rebuild/retry. Strict JSON performs lexical depth/token/number preflight before
`json.loads`; empty ranked sources no longer suppress later evidence; and
`as_of`/`since` require real canonical `YYYY-MM-DD` values while `as_of` applies
snapshot authority weights and fails closed on malformed temporal metadata.

Focused verification reports `326 passed, 2 skipped`; impacted compile,
state/security, and retrieval groups report `660 passed, 18 skipped`, `289
passed, 9 skipped`, and `474 passed, 2 skipped`. Full collection is `2660`; the
local Windows suite reports `2617 passed, 43 skipped`. Full Ruff, Python
compileall, Node syntax, five documentation/import/count guards,
AGENTS/CLAUDE equality, and `git diff --check` pass. The canonical BM25 benchmark
reports 66 queries over 33 active winners, Recall@1 92.4%, Recall@3/5/10 100%,
MRR 0.9596, p50 36.4 ms, p95 43.7 ms, and average 37.5 ms from one local
Windows/Python 3.14 run; latency is machine-specific.

Research rechecked OWASP LLM02 sensitive-information disclosure at
https://genai.owasp.org/llmrisk/llm022025-sensitive-information-disclosure/,
Python JSON decoding at https://docs.python.org/3/library/json.html, portable
file identity at https://docs.python.org/3/library/os.path.html#os.path.samestat,
SQLite integrity/corruption guidance at
https://www.sqlite.org/pragma.html#pragma_integrity_check and
https://www.sqlite.org/howtocorrupt.html, Windows file sharing at
https://learn.microsoft.com/en-us/windows/win32/api/fileapi/nf-fileapi-createfilew,
and W3C provenance at https://www.w3.org/TR/prov-dm/. Implementation and
verification used the worktree root plus explicit temporary state; no commit,
staging, installation, deployment, server start, or production-vault access was
performed during the fix phase.

Six-spec-regression follow-up, 2026-08-03: twenty-eight behavioral cases were
added and one obsolete authority-weight helper test was removed, for a net gain
of twenty-seven. Rendered Q&A candidates and canonical selection now call the
same visible-H1/logical-identity parser, including a pre-existing typed winner
and a winner introduced as the publication lock is acquired. Provider grounding
retains separately bounded raw and sanitized spans derived from the same source
prefix, requires evidence in both, and rejects marker-only redaction evidence.

FTS validates exact required columns and canonical-generation metadata on the
same read-only SQLite connection immediately before querying; a valid prior
generation swapped after `_needs_rebuild` causes exactly one rebuild/retry.
Unrounded BM25 and vector relevance now ranks first, with authority, confidence,
and stable path used only for exact-score ties; this supersedes the preceding
nine-finding statement that `as_of` applied authority multipliers. Citation JSON
uses the shared strict decoder with explicit byte, character, depth, and member
bounds, and parser resource failures become controlled publication failures.

The vector cache now has an exact schema/version plus generation, source hashes,
model/version, dimensions, and page count. Reads are bounded and no-follow;
metadata, paths, hashes, parallel arrays, finite numeric vector dimensions,
duplicates, extras, and aggregate limits are validated. Invalid matching-
generation caches rebuild once, while invalid rebuilt or conversion-exhausted
payloads fail closed without escaping.

Sequential RED evidence was `2 failed`, `1 failed`, `1 failed`, `2 failed, 1
passed`, `5 failed`, then `15 failed` plus a final conversion `1 failed` for the
six slices. Their focused GREEN runs were `4 passed`, `6 passed`, `4 passed`, `3
passed`, `8 passed`, and `15 passed` plus `1 passed`. Task 5 reports `166 passed,
1 skipped`; retrieval/cache reports `78 passed`; compile reports `728 passed, 18
skipped`; shared JSON consumers report `676 passed, 10 skipped`; documentation
guards report `94 passed, 3 skipped`. Final collection is `2687`, and the local
Windows full suite reports `2644 passed, 43 skipped`.

Research rechecked CommonMark 0.31.2 ATX heading behavior at
https://spec.commonmark.org/0.31.2/#atx-headings, SQLite connection/schema
guidance at https://www.sqlite.org/pragma.html and
https://docs.python.org/3/library/sqlite3.html, Python JSON resource guidance at
https://docs.python.org/3/library/json.html, and finite-number validation at
https://docs.python.org/3/library/math.html#math.isfinite. No commit, staging,
installation, deployment, server start, production-vault access, or production
queue access was performed.

Final four-finding and timing closure, 2026-08-03: candidate-inclusive Q&A
admission now reserves one shared selector entry and subtracts the exact encoded
candidate from the shared aggregate-byte limit under the publication lock. The
Markdown index selects, renders, revalidates, and publishes under that same lock;
compile invokes the reentrant rebuild in-process rather than waiting on a child.
Grounded answers scan one explicit 20-candidate result window until five usable
sources are exposed. Strict JSON accepts an explicit lexical-token limit, while
vector node and lexical ceilings are derived from the exact schema, page count,
dimensions, and byte cap; writer and reader use the same roundtrip decoder.
The compile acknowledgement integration test now waits on its condition for at
most 20 seconds below the existing 30-second process timeout and reports full
state on timeout.

Sequential RED evidence was `2 failed, 2 passed` for candidate-inclusive entry
and aggregate boundaries, `2 failed` for stale-index/compile-lock ordering, `1
failed` for five-empty/sixth-valid source backfill, `3 failed, 1 passed` for
explicit/vector JSON limits, and `1 failed` under deterministic 0.6-second Python
startup delay. Focused GREEN runs were `4 passed`, `2 passed`, `1 passed`, `4
passed`, and `1 passed`; the combined slice reports `12 passed`. Task 5 reports
`170 passed, 1 skipped`; retrieval/cache reports `82 passed`; compile reports
`661 passed, 18 skipped`; capture hooks report `86 passed, 1 skipped`; and the
OpenCode integration file reports `69 passed`. Documentation, count, parity, and
import guards report `94 passed, 3 skipped`. Collection is `2697`; the local
Windows full suite reports `2654 passed, 43 skipped`. Ruff reports `All checks
passed!`; Python compileall, Node syntax, AGENTS/CLAUDE parity, clean-clone import
guards, and `git diff --check` pass.

Research rechecked Python 3.14.6 JSON resource guidance and compact deterministic
serialization at https://docs.python.org/3.14/library/json.html, reentrant context
manager semantics at https://docs.python.org/3.14/library/contextlib.html, and
condition/deadline semantics at https://docs.python.org/3.14/library/threading.html.
No commit, staging, installation, deployment, server start, production-vault
access, or production queue access was performed.

---

### Task 6: Remove Verified Installed Corpus Garbage

**Files:**
- Modify: `scripts/repair_installed_memory.py`
- Modify: `tests/test_repair_installed_memory.py`
- Modify: `docs/USER-GUIDE.md`

- [x] **Step 1: Write failing reviewed-manifest tests**

Audit must classify byte-exact duplicate shadows, explicitly named stale active
pages, generated false feedback, whole daily files containing only empty/generated
records, and visible placeholder project handoffs without mutating. Non-identical
shadows remain report-only because title/slug similarity does not prove garbage.
The prepared manifest records every source SHA-256 and intended action with
`approved: false`; mutating apply requires the operator to change that exact
field to `true` and rejects any other manifest or source drift.

- [x] **Step 2: Verify RED**

```powershell
uv run pytest tests/test_repair_installed_memory.py -q
```

- [x] **Step 3: Implement reviewed physical removal**

Delete only byte-exact duplicate shadow notes, explicitly reviewed stale notes,
demonstrably generated false-feedback files, and whole daily files whose complete
line coverage proves they contain no durable or unrecognized content. Replace an
exact visible project-state handoff placeholder with the existing unavailable
marker. Never delete a non-identical shadow, ordinary or mixed daily content,
queue tasks, terminal tasks, compile journals, receipts, manifests, or project
state ownership.

The transaction may hold source bytes only in private temporary staging while it
is in progress so a crash can roll back safely. After all actions pass their
postconditions, purge those staged bytes before reporting success. The retained
manifest contains hashes and action results, not recoverable source content; the
completed cleanup therefore has no backup and no post-completion rollback, as
explicitly selected by the user on 2026-08-03.

- [x] **Step 4: Verify GREEN**

```powershell
uv run pytest tests/test_repair_installed_memory.py -q
```

- [x] **Step 4a: Close the seven post-review specification findings**

Use one surgical schema-v4 state extension rather than a schema bump or a
second staging tree. Schema v3 remains frozen to recovery and verification:
validation accepts only its historical `clean_daily`, `quarantine`, `preserve`,
`review`, and `propose_safe_api_delete` actions, verification handles each
explicitly, and mutating apply rejects every non-v4 manifest.

Preflight `--output` before inventory, lock acquisition, recovery, staging, or
mutation. Require an existing non-link parent, an output outside both vault and
state roots, no link/reparse/hard-link target, and no lexical or physical alias
of `--audit-report`, `--manifest`, or `--sessions-file`. Recheck the same
contract immediately before the final atomic report write.

Require every accepted manifest to be exactly
`<state>/run/backups/<safe-transaction-id>/manifest.json`. During backup-only
preparation, create and fsync a schema-v4 `preparing` transaction journal that
owns the complete expected staging inventory before writing any source bytes;
record each staged artifact durably, seal the final manifest, then transition
that same journal to `prepared`. Approved apply reuses this journal.

For commit and rollback cleanup, persist a terminal-source state
(`committed_pending_purge` or `rollback_complete_purge_pending`) and record each
path ID as purge-authorized before unlinking its staging bytes. Recovery may
accept a missing staging file only when that exact path ID was already recorded;
unknown files, unrecorded omissions, and content drift fail closed. A
`preparing` crash purges only journal-owned exact staging artifacts and preserves
every unknown path.

Classify producer-authentic unpromoted feedback by the fields actually emitted
by `feedback_capture.py`: `status: candidate` plus `trigger: opencode-idle`,
without an invented `source_role`. Add a canonical daily fixture from
`render_flush_block()` under the writer-created `# Daily Session Memory - DATE`
shape (including its em-dash form in actual output), require its one exact
completion marker to be covered metadata, and preserve malformed, duplicate,
misplaced, or mixed-marker files.

Execute strict RED-to-GREEN cycles in this order:

- [x] Reject schema-v3 apply and unknown/unhandled frozen-v3 actions.
- [x] Reject unsafe output before any observable operation side effect.
- [x] Reject nested or aliased manifest locations.
- [x] Recover rollback purge only from exact journaled progress.
- [x] Recover preparation crashes from the pre-staging ownership journal.
- [x] Recognize producer-shaped generated-idle feedback without `source_role`.
- [x] Recognize exactly framed canonical completed empty daily records only.

Run each new behavioral test alone before and after its implementation, then run
the complete repair file, affected producer/parser files, broad Task 6 slice,
full collection and suite, Ruff, compileall, Node syntax, documentation/count/
parity/clean-clone guards, and `git diff --check`. Record exact evidence below.
Do not commit, stage, install, deploy, start a server, access production, or run
the installed-vault workflow.

Post-review closure evidence, 2026-08-04: schema separation was RED as `3
failed`, then GREEN as `3 passed`, with the retained v3 recovery slice at `4
passed`. Output preflight was RED as `5 failed, 1 passed`, then GREEN as `6
passed`; valid external output brought that slice to `7 passed`. Direct-child
manifest placement was `1 failed` then `2 passed` with the valid layout control.
Rollback purge state/progress was `2 failed, 1 passed` then `3 passed`, and the
broader commit/rollback crash slice reported `9 passed`. Pre-staging ownership
and preparation recovery were `4 failed` then `4 passed`. Producer-authentic
feedback was `1 failed` then `2 passed`; exact writer daily framing was `1
failed, 4 passed` then `5 passed`. Review follow-ups for direct-user precedence
and type-confused journal progress were `2 failed` then `2 passed`.

The final repair file reports `74 passed, 2 skipped`; repair plus feedback,
flush, and daily parser coverage reports `361 passed, 2 skipped`. Collection is
`2734`; the complete native Python 3.13.13 Windows suite reports `2689 passed,
45 skipped`. Ruff, compileall, Node syntax, documentation/count/parity/
clean-clone guards, and `git diff --check` pass. Python 3.10 is unavailable and
was not installed. No commit, staging, installation, deployment, server start,
installed-vault workflow, production-vault access, production queue access, or
production cleanup was performed. Production access: no.

Final specification closure, 2026-08-04: the preparation journal now binds the
complete approval-normalized manifest SHA-256 before the first source-staging
write. Apply, preparation recovery, transaction recovery, and verify reject a
resealed identity or action rewrite even when source bytes are unchanged; only
the explicit `approved` boolean is excluded from that binding. The focused
binding tests were RED as `4 failed` and GREEN as `4 passed`.

The schema-v4 journal validator now enforces status-specific staging, action,
result, restore, error, and purge progress. Attempted/mutated/result IDs follow
the manifest path order, staging and purge IDs follow staging-path order,
rollback progress follows reverse attempted order, and terminal commit/rollback
states require their exact complete sets. The focused status tests were RED as
`4 failed` and GREEN as `4 passed`; the final repair file reports `83 passed, 2
skipped`, and repair plus feedback, flush, and daily parser coverage reports
`370 passed, 2 skipped`. Documentation/import guards report `97 passed, 4
skipped`. Collection is `2743`; the complete native Python 3.13.13 Windows suite
reports `2698 passed, 45 skipped`. Ruff, compileall, Node syntax, documentation
count/parity/clean-clone guards, and `git diff --check` pass. Python 3.10 remains
unavailable and was not installed. No commit, staging, installation, deployment,
server start, installed-vault workflow, production-vault access, production
queue access, or production cleanup was performed. Production access: no.

Final recovery-accounting closure, 2026-08-04: the reconstructed repair suite
preserved three intended RED cases and initially reported `3 failed, 83 passed,
2 skipped`. A valid `critical_manual_recovery` retry was rejected before it
could resolve a persisted issue; a crash between a source mutation and its
journal update could record a restore outside `mutated_path_ids`; and a failed
rollback postcondition did not persist `critical_rollback_failed` before
retaining source staging. Each case passed alone after the minimal change. Final
self-review added the recovery-side postcondition counterpart, observed RED as
`1 failed` and GREEN as `1 passed`.

Apply and recovery now seed one exact restored/unresolved partition before
rollback, clear stale issues through the shared accounting helpers, promote only
a proven or conservatively unresolved crash-window mutation, and validate every
persisted rollback transition. `rollback_complete_purge_pending` is validated
before any source staging is purged. A rollback-postcondition failure persists
`critical_rollback_failed` and retains exact source staging for retry. The
existing exact-partition validator and preparation recovery's rejection of an
unrecorded missing staged artifact remained green.

Recovery verification also restored the exact scoped v2 fixture from its
database-preserved original read (`SHA-256
03e164bf4e7f3257222469dbc84eb4ad73b69c527219f1bbbee34eb6dbf0d0c4`) and
repaired four mechanical patch-replay artifacts without changing their intended
behavior. The repair file reports `87 passed, 2 skipped`; the exact Task 7
focused command reports `1410 passed, 23 skipped`; collection is `2747`; and the
complete local Windows/Python 3.14.6 suite reports `2702 passed, 45 skipped`.
Ruff, compileall, Node syntax, documentation/count/parity/clean-clone guards,
and `git diff --check` pass. No commit, staging, product installation,
deployment, server start, installed-vault workflow, production-vault access,
production queue access, or production cleanup was performed. Production
access: no.

Research rechecked 2026-08-04: AWS recommends persisting exact pending,
completed, and failed operation state under atomic concurrency control and
testing successful, failed, and duplicate retries,
https://docs.aws.amazon.com/wellarchitected/latest/reliability-pillar/rel_prevent_interaction_failure_idempotent.html;
the Amazon Builders' Library requires the state record and related mutations to
form one atomic all-or-nothing operation,
https://aws.amazon.com/builders-library/making-retries-safe-with-idempotent-APIs/;
Python 3.14.6 documents cross-platform `os.fsync()` durability and `OSError`
failure behavior, https://docs.python.org/3.14/library/os.html#os.fsync; and W3C
PROV identifies processing steps, validation constraints, reproducibility, and
provenance-of-provenance as trust requirements,
https://www.w3.org/TR/prov-overview/.

- [ ] **Step 5: Run the reviewed installed workflow**

Run `audit` with any explicit stale-page paths, then `apply --backup-only` to
prepare private temporary staging and an unapproved manifest. Inspect every
candidate and action, change only `approved` to `true`, run `apply --manifest`,
then `verify --manifest`. Stop before the first mutation on any hash, identity,
classification, or manifest mismatch. Confirm temporary staged source bytes are
absent after successful apply and verify.

Task 6 source closure, 2026-08-03: the pre-change repair suite reported `39
passed`. Schema v4 now separates actionable candidates from report-only
diagnostics; binds exact paths, SHA-256 digests, file identities, stale-page
requests, postconditions, and every non-approval manifest field; and supports
all five reviewed actions under repair, publication, daily, feedback, and
project-state locks. Ordinary failures restore prior mutations byte-exactly,
recreated or divergent paths stop in `critical_manual_recovery`, and successful
commit purges private source staging. Interrupted schema-v3 transactions remain
recoverable without deleting their legacy artifacts.

Whole-action, tamper, drift, rollback, crash, verify, concurrency, inventory,
and legacy-recovery tests report `49 passed, 2 skipped`. A final crash-window
review found that interruption midway through source-staging purge was not
recoverable; the focused regression was RED as `1 failed`, then GREEN as `1
passed` after recovery accepted and revalidated only the remaining expected
staging subset. Broad Task 6 acceptance reports `1372 passed, 23 skipped`.
Collection is `2709`; documentation, count, parity, structure, and clean-clone
guards report `94 passed, 3 skipped`; and the complete native Python 3.13.13
Windows suite reports `2664 passed, 45 skipped`. One preceding full run hit the
existing detached-bootstrap 10-second integration timeout; that test then
passed alone in 2.00 seconds, its complete file reported `69 passed`, and the
unchanged full rerun produced the clean result above. Ruff reports `All checks
passed!`; Python compileall, Node syntax, AGENTS/CLAUDE parity, and `git diff
--check` pass.

Python 3.10 is not installed in this environment, so that runtime matrix was
not run and no interpreter or dependency was installed; Ruff continues to
enforce Python 3.10 syntax. Step 5 remains intentionally pending for a separate
operator-approved installed-vault run. No commit, staging, installation,
deployment, server start, production-vault access, production queue access, or
production cleanup was performed. Production access: no.

Research rechecked 2026-08-03: Python 3.14.6 recommends bounding untrusted JSON
and documents repeated-name handling plus deterministic key sorting,
https://docs.python.org/3.14/library/json.html; `os.path.samestat()` compares
`lstat`/`fstat` identities across supported platforms,
https://docs.python.org/3.14/library/os.path.html#os.path.samestat; Microsoft
documents that hard-link paths share one underlying file,
https://learn.microsoft.com/en-us/windows/win32/fileio/hard-links-and-junctions;
and NIST SP 800-88 Rev. 2 defines sanitization as making target data access
infeasible for the selected effort level,
https://csrc.nist.gov/pubs/sp/800/88/r2/final.

---

### Task 7: Rebuild, Verify, Commit, and Install

**Files:**
- Modify test-count references in `AGENTS.md`, `CLAUDE.md`, `CONTRIBUTING.md`, `README.md`, `README.ru.md`, `README.zh-CN.md`, `docs/STRUCTURE.md`, `docs/USER-GUIDE.md`, and `tests/README.md` if collection changes.
- Modify: `CHANGELOG.md`

- [ ] **Step 1: Dependency and platform checks**

Search every changed helper name across `scripts/`, `tests/`, and `docs/`. Verify Windows path handling and POSIX-native absolute paths. Do not add a dependency.

- [ ] **Step 2: Focused acceptance**

```powershell
uv run pytest tests/test_memory_quality_contract.py tests/test_flush_classification.py tests/test_integration_injection.py tests/test_compile_bounded_batches.py tests/test_context_noise.py tests/test_project_state.py tests/test_search_ranking.py tests/test_repair_installed_memory.py -q
```

- [ ] **Step 3: Full seven gates**

```powershell
git status --short
uv run ruff check scripts/ tests/
uv run pytest --collect-only -q
uv run pytest -q
node --check scripts/llm-wiki-memory-opencode.js
git diff --check
```

Run Python 3.10 and 3.13 matrices and clean-clone guards. Run structural lint after installed repair and require no active duplicate notes.

- [ ] **Step 4: Independent review**

Run spec-compliance review, then code-quality review, then one final whole-diff review. Fix every finding and repeat verification.

- [ ] **Step 5: Commit and install**

Commit source only after all gates pass. Cherry-pick the verified commit into `D:\tools-agent\llm-wiki`, preserving private diffs. Deploy plugin to both configured OpenCode targets and verify SHA-256 parity. Do not drain the production queue in this task.

---

## Research Basis

- OpenAI eval guidance: define representative inputs and ground-truth criteria before prompt iteration: https://platform.openai.com/docs/guides/evals. The hosted Evals platform is deprecated in 2026, so this project keeps deterministic local fixtures and pytest graders.
- OpenAI Structured Outputs: strict schema adherence, explicit refusal/incomplete handling, and deterministic application validation: https://platform.openai.com/docs/guides/structured-outputs. OpenCode SDK service calls retain the existing token protocol where response schema is unavailable.
- W3C PROV: record entity identity, producing activity, attribution, derivation, and versioning to assess reliability: https://www.w3.org/TR/prov-overview/.
- Idempotent Receiver: identify work uniquely and ignore exact duplicate requests: https://martinfowler.com/articles/patterns-of-distributed-systems/idempotent-receiver.html.
- Python 3.14 `hashlib`: SHA-256 and lowercase hexadecimal digests are available on all supported builds: https://docs.python.org/3/library/hashlib.html.
- Python 3.14 `json`: `sort_keys=True` and compact separators provide deterministic object serialization for these string-only identity fields: https://docs.python.org/3/library/json.html.
- Python 3.14 documents `str.casefold()` for caseless matching and `str.splitlines()` line-boundary semantics: https://docs.python.org/3/library/stdtypes.html#str.casefold.
- Python 3.14 documents operational I/O failures under `OSError`, while process-exiting signals inherit directly from `BaseException`; classified append fallback therefore catches `Exception` rather than suppressing shutdown: https://docs.python.org/3/library/exceptions.html.
- Node.js 24.18.1 is the current Node 24 LTS release as of 2026-07-28: https://nodejs.org/en/about/previous-releases.
- Node.js 24 `Buffer.byteLength()` measures UTF-8 bytes for the injected-runtime output bound: https://nodejs.org/docs/latest-v24.x/api/buffer.html#static-method-bufferbytelengthstring-encoding.
- Node.js 24 documents OS-specific path behavior and that `path.isAbsolute()` checks literal absoluteness rather than resolving traversal: https://nodejs.org/docs/latest-v24.x/api/path.html#pathisabsolutepath.
- Python 3.13 warns that untrusted JSON can consume substantial CPU and memory and recommends limiting input before parsing: https://docs.python.org/3.13/library/json.html.
- Python 3.13 documents descriptor-based `os.open()`/`os.fstat()` and platform-dependent open flags: https://docs.python.org/3.13/library/os.html#os.open.
- Python 3.13 documents `os.path.samestat()` as the cross-platform comparison for `lstat`/`fstat` identities: https://docs.python.org/3.13/library/os.path.html#os.path.samestat.
- Microsoft documents `CreateFileW` directory handles, `FILE_FLAG_OPEN_REPARSE_POINT`, and share modes that control rename/delete while a handle is held: https://learn.microsoft.com/windows/win32/api/fileapi/nf-fileapi-createfilew.
- Microsoft documents that all hard-link names address one file, so in-place writes through any link affect every link: https://learn.microsoft.com/windows/win32/fileio/hard-links-and-junctions.
- Linux man-pages 6.18 documents that `renameat()` atomically replaces the destination directory entry while other hard links remain unaffected: https://man7.org/linux/man-pages/man2/rename.2.html.

## Self-Review Checklist

- [ ] Every persistence boundary has a deterministic behavioral test.
- [ ] No model response can bypass provenance or semantic admission.
- [ ] Repeated idle events are idempotent without suppressing changed transcripts.
- [ ] Project-specific knowledge cannot become global or cross-project.
- [ ] Index and search use the same active canonical page set.
- [ ] Only reviewed, machine-verifiable garbage is physically removed; ambiguous and ordinary history is preserved.
- [ ] Production queue and five terminal tasks remain untouched.
- [ ] The reviewed manifest hashes every mutation target and no completed transaction retains source backup bytes.
- [ ] Test counts and all three README languages are synchronized.
- [ ] All seven fix-discipline gates pass before commit and installation.
