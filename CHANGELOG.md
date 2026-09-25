# Changelog

All notable changes to this project are documented here.
Format based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Versioning follows [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Fixed
- Navigation cleanup (audit C-44): the dead `code_intelligence` text helpers and three unreachable `Capability` members are removed (a test now holds that every capability has a route); `code_graph.py <dir>` no longer writes the unread `cache/code_tools.json` or runs the analysed repository's own `tsc`; live `find_dead_code` answers only for the named symbol; a Git run in `workspace_revision` gets the caller's whole deadline instead of a 5 s cap; `WorkspaceDelta.configuration_changed` (read by nothing) is removed; comments no longer describe the retired legacy index as a fallback. CLAUDE.md and AGENTS.md name `repository_index.py index <dir>` as the indexing command.
- A worktree its owner marked not to index is no longer a follow candidate, so eight such worktrees can no longer take every nightly follow slot from the rest (audit C-43).
- Vault code generations built before their manifest named their code roots are now collected: repository retention asks the catalog's own `holds_code`, the predicate prune uses, so no generation falls between the two collectors (audit C-42).
- The live code graph parses only regular files of its own tree up to 8 MiB (a symbolic link to a file elsewhere is no longer read), and no longer starts one unused `git log` per file (about 836 processes per live answer on this repository); the unread `git_commit`/`valid_from`/`author` parse fields are removed (audit C-41).
- An impact Git command checks its deadline before the child starts, so an expired deadline no longer leaves an unreaped Git process with an open pipe (audit C-40).
- The sweep of dead `run/lsp` owner roots reaches every dead root: roots that kept failure evidence no longer use up its window, `owner.json` and `lease.json` record each process's start identity so a reused pid reads as dead, and removal never chmods through a symbolic link (audit C-39).
- One oversized language-server reply fails only its own request: a result over a client bound or a frame over 8 MiB (consumed, up to 256 MiB) refuses that request with `ResponseRefused`, and an oversized diagnostics notification is dropped with a warning; the server keeps running instead of being killed and replayed into a second fatal failure (audit C-38).
- A failed language server's stderr tail is redacted before it is cut: no value loses its key to the cut, and a PEM private key block is removed whole, even when the cut took its BEGIN line (audit C-37).
- Lookups by a common name are no longer refused: community mode with a symbol, the provenance join, qualified snippets and trace callers ask up to the reader's ceiling and cut on their side, and the provenance answer says how many locations it left out (audit C-36).
- An impact answer keeps its graph result when the note scan fails: a note changed mid-scan or a scan ceiling becomes a warning instead of losing the answer (audit C-35).
- rust-analyzer and gopls answer queries offline (`CARGO_NET_OFFLINE=true`, `GOPROXY=off`): they could fetch crates or modules into the managed cache while a query waited.
- The workspace revision behind code navigation holds only relevant files and sees nested server configuration: an edited `README.md` no longer enters it while dirty (its commit made the server receive a `deleted` event for a file that exists), and a nested `go.mod` or `Cargo.toml` is now watched.
- A language-server process that failed for good is replaced by the next query, within the startup retry budget: the session kept the dead process and never started another, so a key in steady use stayed degraded.
- A Git command the workspace revision runs that exits non-zero is a `ValueError` (`GitCommandFailed`), so code navigation degrades with a named reason instead of passing a generic `CalledProcessError` to the caller.
- A language server whose install is missing or inconsistent leaves its session degraded with `<profile>_install_invalid` instead of raising and launching again on every query.
- A structural answer's refresh runs on its checkout's root (a question about a subfolder started a refresh the indexer refused, while the answer said `started`), and each worktree of a repository is asked for on its own.
- A slow `git` in the worktree helpers is that checkout's named refusal (`repository_git_probe_timed_out`), not the end of the nightly `refresh-all` or `retire`: the helpers now use the index's own bounded Git runner.
- Impact analysis says `approximate`, not `exact`, when the generation indexed other bytes of a changed file than the diff's old side: its offsets then pointed at other lines.
- The `get_architecture` summary and the `provenance`, `snippet`, `coverage`, `search`, `query`, `data_flow` and `cross_service` modes carry the generation's freshness block and start its refresh when the checkout moved, as the graph modes did.
- A structural answer's `graph` component says `stale` when its generation was built from an older commit of the checkout and `unknown` when no commit could be compared (it always said `fresh`), and the envelope's `source_commit` is read again every 5 s instead of once per server.
- Every `get_architecture` mode runs on the bounded code-graph workers under the call's deadline, as the summary did: `callers`, `callees`, `dependencies`, `path`, `community` and the symbol view parsed on the tool's own thread with no deadline, and a few hung calls held every MCP slot.
- The workspace revision behind code navigation no longer walks top-level folders git ignores whole (`node_modules`, `dist`, …): on a TypeScript checkout with dependencies installed it walked for 14.8 s and then refused at its 100 000-entry ceiling.
- A deleted or renamed top-level folder no longer stops a repository's code index: the refresh uses the recorded roots that still exist (and finds roots again when none does), and `changes` names tracked top-level entries the index does not cover.
- One Python file too long or too deeply nested to parse is that file's parse error, not the end of the repository's code index: every reader of repository Python now catches the same failures (`python_parse.PARSE_FAILURES`, including `RecursionError`).
- The Windows installer says when the `llm-wiki` MCP entry in `~/.claude.json` points at another vault, as the POSIX one does; a macOS uninstall no longer fails on a LaunchAgent launchd has already unloaded; the guide says the Linux timers run while you are logged in and how to enable lingering.
- The user guide lists the nightly and weekly steps the code runs and the 4 h/6 h scheduler limits it sets; every install hint keeps the other extras (`uv sync --locked --inexact --extra ...`); CONTRIBUTING and ARCHITECTURE no longer describe a check-only repair or a retired vector path. A test ties the guide to the code.
- The weekly pass counts a failed reflection or tier step (it logged the exception and reported success), and a weekly skipped because maintenance was already running records the skip, which doctor names when the weekly goes stale.
- Step artifacts under `logs/maintenance/` are kept as long as the reports that point at them (30 days): held to the report count, they lasted about two nights and a report's "full output" link led nowhere.
- The nightly writes every instant in its state in UTC with an offset (`failed_at` and `skipped_at` were local), and doctor compares a skip with the last run as instants, not as strings that differed by the time zone.
- One generation the weekly prune cannot remove is its own `ERROR:` line and the pass goes on (an oversized abandoned tree stopped the whole step), and `install_models` gives each model its own boundary: offline, the first fetch no longer keeps the second model from being tried.
- `install_models` replaces a cached weights file that does not match its pin in the same run (it removed only the link, and the cache re-linked the same bad bytes), and no longer re-hashes 2.3 GB of verified, unchanged weights every night; a file changed within 2 s of its verification is still re-read.
- `install_models` replaces a cached weights file that does not match its pin in the same run (it removed only the link, and the cache re-linked the same bad bytes), and no longer re-hashes 2.3 GB of verified, unchanged weights every night.
- Generation retention keeps the generation the fallback would try first — the previous activation — not only the parent: after a full rebuild, which records no parent, the prune removed the only spare.
- The knowledge snapshot reports a failed `git commit` as a failure (it said "no change"), and records each successful run inside its `.git`, so doctor no longer calls an unchanged memory's backup stale. The contract names the snapshot's own repository beside the one automatic Git operation on the checkout.
- A rerun of the installer that replaces an outgrown install puts the previous one back when the new install fails, instead of leaving no scheduler and no hooks; the install smoke names the doctor checks that failed (it printed only "RuntimeError"), and the last two places that resolved the uv link keep it.
- Scheduled runs call uv by the path the shell found, links kept: resolving it pinned Homebrew's versioned Cellar target, which `brew upgrade` deletes, and every scheduled run then failed to start. Doctor now says when an installed unit or LaunchAgent calls a uv that is gone.
- The Windows installer no longer passes `--environment ""` when `UV_PROJECT_ENVIRONMENT` is unset (Windows PowerShell 5.1 drops the empty string and the setup step stopped), and its native-command runner refuses any empty argument.
- CI runs the real encoder: the `clean-hybrid` job fetches the pinned ONNX encoder (cached), builds a generation with vectors and requires a search to answer with the dense signal. Before, no CI job reached the model session.
- The user guide names `doctor --rebuild-generation` where it tells how to rebuild the evidence generation; it said `--repair`, which only repairs the generation catalog. A test keeps the docs from saying it again.
- Failed MCP tool calls and telemetry writes are no longer counted as lost captures: they share the diagnostics trail but have their own count and a doctor `tools` check, so the capture check and the session-start line stop reporting tool errors as capture loss.
- Doctor says a check did not finish when its budget ran out mid-read, instead of `error` with advice to run `--repair`: under the default 5 s budget `queue` and `claims` were reported broken on a healthy vault. The check keeps its deletion codes.
- Three stale docstrings corrected: `answer_cost` (the wire is compact JSON, not `indent=2`), the lazy query encoder (a 2.7 s cold load, a tracked straggler) and `lookup_mode` (relation questions run GRAPH).
- `get_decisions` returns up to `limit` decision pages, one row each, in the agent row shape `recall` uses: it asked the search for exactly `limit` rows and filtered afterwards, so other pages and repeated chunks took the places.
- The health resource and `vault_status` count only past days as compile backlog: today's log is still being written and the nightly compiles it, so it made every answer partial from the first capture of the day.
- A telemetry write that fails leaves a record in the capture diagnostics (kind `telemetry_event`: deferred when the database was busy, lost otherwise); the best-effort writer returned False and no caller read it.
- `recall` and the CLI search ask the evidence graph when the question is about relations ("depends on", "callers", "related to", …), keeping dense search beside it; every semantic search used to run HYBRID, so the graph never answered them. A trace nothing reported names the mode the search would run.
- `get_context` answers name when their corpus was read: the envelope's `index_timestamp` was always empty because the tool reports a content hash, not a generation id; it now carries `collected_at`, the moment the Markdown snapshot was taken.
- `get_context` no longer promises a `content_preview` for `include: ["frontmatter"]`: the preview went with the context compiler in July; `include` is accepted for compatibility and ignored, and the page content is in `text`.
- Every process that starts model inference waits for it at exit (up to 30 s), not only the MCP server: a one-shot search or query whose deadline abandoned a rerank could otherwise exit mid-inference and abort.
- The reranker learns its cost once and stops wasting CPU under load: when its cost is unknown the one background run gets the stage ceiling so it can finish and record it, and when its cost is known not to fit the caller's window it is not started at all. Before, the background run was cut at the caller's deadline on every call, never recorded a cost and was never admitted.
- A failure while the MCP server builds or renders an answer's envelope is answered with the tool's safe error envelope, where it used to escape to the SDK and reach the client as raw text, possibly with a local path.
- An MCP answer is marked stale when any source its index holds moved after the index was built: a removed or renamed note and a changed project `state.md` or `context.md` now count, where only note contents did.
- The MCP search keeps one second of its deadline for the lexical fallback: the hybrid pass stops that much early, so when it runs out of time the lexical pass still answers, where before it was handed an expired deadline and failed too.
- With no active generation a natural question still finds pages: the Markdown fallback drops stop words and ranks pages by how many of the question's words they share, where it required every word, so "почему systemd таймер, а не cron" found nothing. Common Russian function words join the shared stop-word list.
- The MCP supervisor waits 35 s for a worker whose input closed before it signals it, so the worker's own 30-second wait for model inference is not cut short; and a server source removed while the supervisor fingerprints the code (the nightly update) changes the fingerprint instead of crashing the supervisor.
- A grounded `recall` through MCP has the grounded budget (120 s) instead of the 10 s every tool got, so it can answer at all; its provider call runs under a ceiling equal to the time left and is ended at the deadline instead of outliving it. The per-call ceiling is kept per context, so a compile's ceiling no longer applies to calls on other server threads.
- The ownership helpers refuse with `adopted_registry_required` instead of opening the migration-candidate database when called on an adopted vault without its registry.
- A prune that died after marking its row no longer has its images put back by the next recovery; they are removed, as the row says.
- A checkpoint reservation whose checkpoint another attempt committed is dropped by the next history prune instead of standing for 90 days (41 on this vault). The checkpoint log itself keeps growing by design: it is what a project journal is rebuilt from.
- A transaction and its images agree: the prune removes a directory no row names once it is an hour old (9 on this vault; a failed prepare leaves it for the prune, because whether its row committed is not known at the failure), and a settled row whose images are already gone is marked pruned so the history prune can take it (13 on this vault).
- The nightly reclaim settles a quarantined transaction whose every operation was rolled back to `discarded`, so the existing prunes take its images: all 117 quarantines on this vault (69 MB) had left nothing in the vault and held `run/` out of deletion for good. One with an operation still applied keeps its quarantine. Empty capture-intent shards under `ready/` are removed like those under `pending/`.
- A compile failure recorded for bytes a daily log no longer has is retired on the next compile pass: 44 of the 45 rows on this vault named content that was gone and held its day out of the archive and `run/` out of deletion for ever.
- The compile offers the planner the pages its day is about: the optional context of a batch is ranked by BM25 relevance to the batch's days instead of by path, so a day about a known subject updates the existing page instead of creating a near-duplicate beside it and leaving the old figures untouched. It costs about 40 ms on this vault and asks no model.
- The OpenCode plugin and the adapter read the events OpenCode sends: the session id of `session.created` (`info.id`), so the session-start context reaches the system prompt, and a tool's arguments (`args`), so tool lines name their target. The plugin no longer drops sessions inside the vault, and an idle with nothing new is not captured twice. Still open: one classification per OpenCode turn, which needs a measurement on a live OpenCode.
- A transcript copy the adapter makes for one event is removed when the event ends even if its capture intent could not be published; nothing read a kept copy any more, so it was private text left in the cache.
- The two plugin helper commands nothing has called since 2026-07-13 are retired: `tool_breadcrumb_append.py` and the command line of `daily_log_append.py` (whose appenders stay). `integrations/README.md` now says the OpenCode plugin ships in `scripts/` and forwards every event to the adapter.
- The `flush_memory.py` command line is retired with the 37 definitions only it reached (451 lines): nothing in the product ran it, and its "queue the call when no provider answers" branch was the only thing behind CLAUDE.md §6's claim, which now says what happens.
- Provider calls fail closed. A `claude --help` that failed once is no longer remembered as "no flags" for the life of the process, and while the isolation flags are unknown the Claude backend is not called; a `MEMORY_LLM_PROVIDER` that names no provider (a typo) calls no provider instead of the automatic chain with its cloud providers, and `doctor` names the value.
- `doctor` names installed Claude hooks that still pass a delegate retired on 2026-09-17 and says to rerun the installer, instead of reporting them as current.
- The weekly archive names each day older than the hot window that stays flat and why, goes on past a day that fails, and exits 1 when any did, so the weekly is reported degraded instead of stopping silently.
- Two stale capture tests no longer write prompt lines into the checkout's daily log, and the test session now fails when any test leaves a daily log behind outside the live vault.
- A Claude prompt reaches feedback capture like every other host's (a hook that names the event's own capture script now takes the full path), and the delegates behind the 5-second prompt and tool hooks stop at 2.5, 1 and 3.5 seconds, so a hang is recorded before the host kills the hook; a test ties those bounds to the installed hook timeouts.
- Episode consolidation: an empty answer for one batch fails that batch instead of stopping every later day, and a day closed with batches it could not read records them with the code revision, so it is opened again once the code changes and only the lost batches are asked again.
- Tool calls made inside the vault are captured, as prompts have been since 2026-09-24, and neither capture hook runs on a stand-in: a module that cannot import fails the hook visibly instead of a no-op recorder or a different project slug. A test now refuses any module that replaces a failed import with a function.
- A Codex session is recorded and classified: its rollout lines (`response_item` messages and tool calls) are read as the conversation they record, where before every line rendered to nothing, so a Codex session left no session record and gave the classifier no text.
- The contradiction check compares what can change its answer: a new claim is compared with every active claim about the same subject (relation-only matches, which were always unrelated, filled the list, and it was cut at 50 without a word); the compile no longer asks a model for a verdict that cannot change the decision, which spent tokens and sent the claim text out; and instants are compared as times, not as text.
- A quarantined claim no longer holds back its day. A page that a search happened to find no longer quarantines a new claim — it is evidence, not a contradiction — and a claim that is quarantined stays on its published page as `quarantined` with its candidate beside it, instead of keeping the whole batch unpublished until a manual review nobody performs.
- A day whose compile batch was quarantined stays pending, as the message says: the quarantine wrote the day into the compiled-days mirror without a receipt, and the day was never offered again. The mirror now records only receipted days, and each pass takes back out a day that an earlier quarantine hid.
- A capture whose provider does not answer waits an hour between attempts, as intended: the real client's "no answer" was read as an ordinary failure, so the eight attempts were spent in about an hour and the capture was lost.
- A long day split into parts is compiled whole: two parts of one day never share a compile batch, and a batch that would hold them is refused instead of silently dropping the second part while writing its receipt.

- **A secret written as JSON is still a secret.** The redactor — the one scrub
  before a provider, the daily log and the session record — passed `{"api_key":
  "…"}`, `{"password": "…"}`, a JSON `Authorization: Bearer` header, a database URL
  password, `AWS_SECRET_ACCESS_KEY=…` and GitLab tokens. A key is now any name that
  contains a credential word, as gitleaks' `generic-api-key` reads it, with a quote
  allowed before the separator; a JSON value keeps its quotes and brackets; a quoted
  value is code only when it interpolates; URL userinfo and `glpat-` are caught. See
  `docs/research/2026-09-25-a-secret-in-json-is-still-a-secret.md`.
- **`read_page` returns every note again.** 52 of 209 live notes answered "Evidence
  resolution failed": the `## Claims` block writes its references inside JSON strings,
  and the extractor ended a reference only at a backtick. A reference opened by a
  backtick or a quotation mark now ends at the matching one; strictness is unchanged.
  Lint skipped that block and so never saw it; it now reads the whole page, as
  `read_page` does. See `docs/research/2026-09-25-a-quoted-reference-ends-at-its-quote.md`.
- **The scheduler check says what the night could not do.** One table of limits
  (`SCHEDULER_LIMIT_HOURS`, 4 h and 6 h) now renders both the systemd units and the
  Windows tasks; the systemd nightly limit was 3 h, below the pass's 3.19 h worst
  case in auto provider mode, which the test missed because it measured the `fake`
  provider. The nightly records its code update outcome, and doctor's `scheduler`
  check names a failed fetch, stale dependencies, a needed installer run, a branch
  that is not the default, and installed units older than this release's time
  limit. See `docs/research/2026-09-25-the-scheduler-says-what-the-night-could-not-do.md`.
- **An update brings the extras the operator chose; `hybrid` has its reranker
  again; a base install has no failed nights.** `hybrid` is `semantic` plus
  `reranker` again, as documented (the ONNX change had dropped `torch` from it and
  turned the reranker off silently). The nightly update syncs every extra the
  operator chose — one with a package only it brings installed, names compared as
  the packaging specification normalizes them — so a package added to an extra
  arrives with the code; before, `huggingface_hub` never matched `huggingface-hub`
  and no extra was synced at all. `install_models.py` fetches only models whose
  runtime is installed and exits 0 with nothing to fetch, so a base install's
  nightly no longer records a failure every night. See
  `docs/research/2026-09-25-an-update-brings-the-extras-the-operator-chose.md`.
- **The history prune keeps every row something reads back.** The 90-day prune
  of 2026-09-24 would have deleted compile transactions that compile receipts,
  daily archives and evidence resolution read back, from about 2026-11-18. It now
  removes only the hook breadcrumb families `post-tool` and `user-prompt` (16 414
  of 23 457 rows on the live vault). See
  `docs/research/2026-09-25-history-prune-keeps-every-authority-row.md`.
- **A dead capture gets its second chance after a fix, and the weekly purge no
  longer aborts on one.** The nightly redrives every dead capture task that died
  before the checkout's HEAD commit, once, before the queue worker runs
  (`memory_queue.py redrive-dead-captures`). A redrive counts as spent only when
  its child could reach the capture worker; the 23 children of 2026-09-06 carried
  no capture link, so their parents still get theirs. The ordinary purge leaves a
  capture without a terminal record in place and names it as retained instead of
  failing the whole plan. See
  `docs/research/2026-09-25-a-dead-capture-gets-its-second-chance-after-a-fix.md`.

## [5.0.0] — 2026-09-24

Everything since 4.0.0, found and fixed by the audits of 2026-09-17 and
2026-09-24. A major release: Cursor and Antigravity are no longer supported
platforms, an upgrade from v3.3.0–v3.4.0 no longer imports the JSON queue, and
the legacy index files are no longer read (see Removed).

### Added

- **A ledger of things and events, posted once and counted by code.** The
  nightly fact-keys call now also returns, per user turn, the things the person
  names and the dated events about them; `scripts/ledger.py` posts each record
  once under a digest of its fields and source pointer, merges two records of one
  thing within 30 days into one event unless a stated quantity contradicts (the
  CDC's case de-duplication rule, decided field by field as Fellegi–Sunter), and
  `count` answers "how many" over every record of a kind with a tier —
  "confirmed" when every record is the user's own dated words, "probable"
  otherwise — and the pointers; `reconcile` flags a reader's number the ledger
  does not hold. The rows ride into the generation as a `ledger` table of
  `search.sqlite3`, disposable like the rest of it; a generation built before the
  table carries none and the reader is told so. A thing seen on two or more days
  opens the recurrence gate and its entity page is extended with dated pointer
  lines by code, never rewritten by a model. Zero provider calls at question
  time. Approved 2026-09-22; see `docs/STRUCTURE.md` and
  `docs/research/2026-09-22-a-ledger-of-things-and-events-posted-once.md`.
- **Every stand, measured in one pass.** `docs/REPORT-2026-09-13-stands.md`
  records all of them on `694991b`: the parity stand at 16 of 16 against the
  other tool's 14, with 0 confident-wrong against 2 and 0.43× the tokens on the
  14 both answered; code navigation 200/200 definitions and F1 1.0 on references
  and calls with 0 orphan processes; durability 0 silent losses over 108 killed
  trials; and the one failing gate — selective forgetting's `ageing.retain_rate`
  at 0.8857, where archiving 59 pages stopped eight others from surfacing. That
  last one is open and unexplained.
- **A killed test names itself, without paying for it.** `PYTHONFAULTHANDLER=1`
  on every shard, and the name of the running test appended to
  `LLM_WIKI_TEST_PROGRESS_FILE` — one line per test, uploaded with the timings.
  `-v` was tried first and cost two runs: a 40-minute and then a 65-minute cap on
  Windows shards that normally take 22-26 minutes.

- **The memory retires its own residue.** The nightly removes the transcripts the
  memory's own provider calls left under `~/.claude/projects` before
  `--no-session-persistence` (2026-09-14): `sdk-cli` records from the vault, the
  temporary directory or the job directory, and the project directories they
  emptied. A held session is never touched. The owner deleted 1 082 of them by
  hand on 2026-09-23; that was the last time. See
  `docs/research/2026-09-23-the-memory-retires-its-own-residue.md`.
- **The classification corpus labels itself.** `benchmark/build_flush_corpus.py`
  labels each real session with two automatic readings that never see the
  product's tier names: the rubric of 2026-08-23 and a new extractive reading that
  must quote, verbatim, the one passage a reader would still need later — a quote
  the transcript does not carry voids the reading. A case the readings agree on is
  confirmed; one they disagree on is contested, stays in the corpus, and counts in
  no metric, the rule the product applies to a claim its evaluators disagree on.
  The stand reports confirmed and contested counts; nothing is "provisional until
  a person reviews it". The corpus now carries the rendered, bounded conversation
  the product classifies, not the raw host JSONL it stopped reading on 2026-09-06.
  Schema v3 replaces v2. See `docs/research/2026-09-23-the-corpus-labels-itself.md`.

- **A verified digest is remembered across processes, and the warm-up it would
  have hidden is gone.** Hashing every artifact of a generation against its
  manifest is what a cold open paid, in every new process, to learn what the
  previous process already knew about an immutable file. It is now remembered by
  stat identity — generation, path, device, inode, size, mtime — in the disposable
  `cache/evidence-graph/verified-artifacts.json`, with Git's racily-clean rule:
  an artifact whose mtime is not strictly older than the cache's own is hashed
  anyway, and an unreadable cache is an empty cache. Cold open on the installed
  vault 2.22 s → 1.71 s, and 4.9 s → 1.71 s across the evening. The graph warm-up
  added earlier the same day is deleted: it moved the cost rather than removing
  it, which is what the owner called it. What remains of the 1.71 s is measured
  and named in
  `docs/research/2026-09-12-a-verdict-worth-remembering-across-processes.md`, and
  two of its three parts were the same waste elsewhere: the format receipt hashed
  241 MB to key a verdict it already had, and the index check walked all 3 405
  rows on a read that already has the digest. Both fixed, cold open **1.23 s** —
  4.9 s this morning. What remains is the seal's own read after the open, which is
  the fence itself and stays.
- **A reader checks the digest, a writer derives.** A cold code answer on the
  installed vault re-derived all 3 405 chunks of the search index before
  answering — 1.68 s of it spent inferring the language of each chunk — to prove
  that our own chunker is deterministic, when the artifact digest, the manifest's
  versions and the entry seal already pin every input. That re-derivation now runs
  where the rows are created and in `doctor`; a read trusts the digest. Depth is
  part of the memo keys, and a deep verdict answers a shallow question while the
  reverse never does. Cold 4.9 s → 2.22 s, warm 0.31 s.
- **The vault is a repository too, and answers about its own code.** The
  decision left open this morning, taken on the owner's instruction to decide by
  rules 2 and 4: the vault's checkout gets a code generation beside its memory
  one, because current practice keeps one index per data lifecycle and code apart
  from documents, and because mixing them re-derives 240 MB of code index for a
  knowledge edit. `admit_repository` no longer refuses the vault; a vault's code
  roots exclude `knowledge/` and say so; a generation that holds code names its
  roots in its manifest, so one checkout can carry both and a reader can tell
  them apart; the graph opener asks for the code generation first; and
  `refresh-all` adopts the vault once so the nightly keeps it fresh with no
  operator action. Measured after indexing the installed vault:
  `get_architecture mode=query` for `fuse_rrf` answers in 4.4 s where it
  returned `"nodes": []` this morning. Reasons, sources and costs:
  `docs/research/2026-09-12-the-vault-is-a-repository-too.md`.
- **The decision rule is satisfied.** Three runs, every condition met on the
  surface an agent actually reaches: 16 correct answers of 16 against
  codebase-memory-mcp's 15, zero confident-wrong answers against their one, no
  non-answers on either side, tokens 1.45× against a 1.5× ceiling and p95 per
  task 1.58× against a 2× ceiling. The rule was written before the first number
  was read and has not been touched since; by it, llm-wiki can now replace the
  other tool, and removing it is the owner's call. What moved the numbers is in
  `docs/research/2026-09-12-the-rule-is-satisfied.md`: six changes, of which one
  added a capability and five removed waste — two wrong answers and four repeats
  of work already done. Measured on the worktree checkout, because the installed
  vault still holds no code generation of its own.
- **Sixteen of sixteen, against fifteen.** After the two defect fixes and the
  three changes the owner approved on 2026-09-12, three runs of the parity set
  give our two surfaces 16 correct answers of 16 against codebase-memory-mcp's
  15, with zero confident-wrong answers against their one and no non-answers on
  either side; every grade repeated exactly across the three runs. The
  cross-service route question is ours too: correct in 0.6 s against their
  partial in 2.0 s. Cost is the one condition of the decision rule still unmet
  on the default surface — tokens 1.57× and p95 2.74× against ceilings of 1.5×
  and 2× — while the `query` surface passes all four at 1.18× and 1.36×. The
  other tool stays installed until the default surface passes too, and what the
  remaining gap is made of is measured in
  `docs/research/2026-09-12-sixteen-of-sixteen.md`: two `find_dead_code` calls at
  11 s carry the whole p95, and a quarter of the largest answer is one absolute
  path prefix repeated 110 times.

- **The parity numbers exist, and they say keep the other tool.** Three runs of
  the sixteen-task set and three of the cross-service pair, llm-wiki against
  codebase-memory-mcp, both sides indexing the same checkout, graded by a rule
  written before the numbers were read. Correct answers: 13 and 14 of 16 for our
  two columns against 15; tokens 9 869 / 7 685 against 5 565; p95 per task
  12.7 s / 10.2 s against 4.2 s; one confident-wrong answer each and no
  non-answers on either side. Every grade repeated exactly in all three runs. We
  win "which tests exercise this function" outright and the cross-service route
  question in 0.6 s against 2.0 s; we lose "where is this constant defined" in
  every run, because the generation holds no module-level constant node. Two of
  the rule's four conditions fail, so codebase-memory-mcp stays installed and
  the four things that would close the gap are named in
  `docs/research/2026-09-12-the-first-honest-parity-numbers.md`. Runs are in
  `benchmark/code-parity-v2-2026-09-12-run{1,2,3}.json` and
  `benchmark/code-parity-cross-service-2026-09-12-run{1,2,3}.json`.

- **Precise navigation for Rust.** `rust-analyzer` 1.98.1 answers
  `definition`, `references`, `implementations`, `type`, `callers`/`callees`
  and `hover` for `.rs`. The binary is published, but it needs a toolchain
  behind it — the project is read by `cargo metadata`, the sysroot by
  `rustc --print sysroot`, and the standard library from its *sources* — so
  the install unpacks five archives of one release (`rust-analyzer`, `rustc`,
  `rust-std`, `cargo`, `rust-src`), each pinned by the SHA-256 the release
  manifest publishes, into one toolchain under
  `cache/code-tools/rust-analyzer/1.98.1/`. No compilation. Measured here:
  49 s to install, 137.5 MB downloaded, 701 MB on disk, `definition` 0.10 s
  warm. A profile may now declare further **components** with their own
  platform tables and their own place in the managed root, archives may be
  `.tar.xz`, and the per-member size bound is a profile's own — a language
  server binary can be 90 MB. The profile also names the library path its
  verified copy needs: `rust-analyzer` is linked against `librustc_driver` and
  finds it relative to itself, so the copy in the owner root would otherwise
  die before the handshake. Installation stays one explicit operator action
  (`uv run python scripts/install_language_server.py --profile rust-analyzer
  --state-root <state-root>`).
- **Precise navigation for Go (#24, B).** `gopls v0.23.0` joins Pyright and
  `typescript-language-server` as a managed profile, so `definition`,
  `references`, `implementations`, `type`, `callers`/`callees` and `hover`
  answer for `.go` files from a type checker instead of a name match. It is
  the first managed server that is **built** rather than unpacked, because the
  Go team publishes gopls only as a module: the install unpacks a pinned Go
  toolchain (1.27.1, per-platform archive pinned by sha256) and compiles one
  pinned module version with it, inside `cache/code-tools/gopls/v0.23.0/`,
  with `GOPATH`, `GOCACHE`, `GOMODCACHE` and `GOBIN` under that root and
  `GOTOOLCHAIN=local` so the pin cannot be swapped. Measured here: 44 s to
  install, 324 MB on disk, `definition` 0.48 s cold and 0.10 s warm. It is
  also the first **native** server: a profile now declares `node_major` or
  `native`, identity stops probing Node for it, and the verified copy is
  launched by path from the owner root instead of through an unlinked
  descriptor — gopls hashes its own executable and re-executes itself, so a
  file with no name makes it exit. The launch invariant is stated instead of
  assumed: a generation may name the inherited descriptor or a file inside its
  own owner root, never anything else. Installation stays one explicit
  operator action (`uv run python scripts/install_language_server.py --profile
  gopls --state-root <state-root>`); until it runs, Go answers from structural
  evidence exactly as before. New module `scripts/go_source_build.py`.
- **Argument bindings, the HTTP boundary and routes across repositories
  (#24, B3/D2).** `get_architecture mode=data_flow` answers, hop by hop,
  which caller-visible name binds which parameter of the callee, and says in
  its own answer that this is argument binding and not data-flow analysis.
  `mode=cross_service` follows calls plus literal-path HTTP client calls:
  a call reaches the `route` node of the same method and path and the walk
  turns around into the handler that exposes it; a route this repository
  does not serve is matched against the exported routes of every other
  indexed checkout and named with the repository it lives in, without
  opening a second generation. The extractor (`code-extractor/v12`) records
  one `BINDS_ARGUMENTS` literal assertion per resolved call (at most 8
  `argument->parameter` pairs, 256 bytes) and one `HTTP_CALLS` assertion per
  literal-path client call; the callee of a binding is named by the `CALLS`
  assertion of the same call span, because the graph contract allows an
  assertion to carry a target node or a literal, never both. Hint files gain
  a `route` table (`code-hints/v2`) and new readers
  `EvidenceGraph.argument_bindings` and `unresolved_edges` answer both
  modes. Measured on this repository: 30 617 bindings, 24.0 s to index.
  Derived generations rebuild themselves on the next nightly pass; until
  then both modes answer nothing.
- **The parity set asks the new questions (#24, E).**
  `benchmark/code-parity-v2.json` carries the thirteen v1 tasks unchanged and
  adds two argument-binding tasks and one "which tests exercise this
  function" task, with gold read by hand from the working tree on
  2026-09-11; the stand now defaults to it.
  `benchmark/code-parity-cross-service-v1.json` asks the cross-service
  questions against the two-repository fixture
  `benchmark/build_cross_service_fixture.py` builds, because this repository
  serves no HTTP route. No run is included: runs need the owner's word.
- **The graph meets the agent where it searches (#24, C).** A Claude Code
  `Grep`/`Glob`, or a Codex `rg`/`grep`, whose pattern names a symbol of an
  indexed repository gets a hint of at most three definitions (qualified name,
  path:line, resolved in/out degree) and the `mcp__llm-wiki__get_architecture`
  call that answers authoritatively; `SubagentStart` (Claude Code, Codex) and
  every session start get one line naming the code tools; the OpenCode plugin
  appends the hint to its `grep`/`glob` output (best effort: OpenCode does not
  document that this reaches the model). One adapter, `scripts/graph_hint.py`,
  reads `cache/code-hints/<checkout-hash>.sqlite3`, a per-checkout table each
  index build exports from its generation through the new
  `EvidenceGraph.symbol_page` (the validated reader costs ~2 s cold; a hook
  cannot). Measured, one fresh process per call, 558-source fixture: 56 ms p50
  / 58 ms p95 for a hit or a miss, 33 ms for a literal, plus ~9 ms for
  `uv run`. Silent on anything else and on every error; never blocks a tool
  call; the text is labelled as repository data. Codex ownership recognises the
  new handlers through one rule (`scripts/codex_hook_identity.py`) shared by the
  installer merge and the doctor's runtime-hook check, which would otherwise
  have called every installed Codex `runtime_hooks_mismatch`; the OpenCode plugin was rewritten under the complexity gate
  with unchanged lifecycle behaviour. Tool names stay `mcp__llm-wiki__*` (C3).
- **Repository indexes follow worktrees, and are retired (#24, D1).** The
  nightly `refresh-all` indexes up to eight new worktrees of every registered
  repository with their sibling's code roots, and the first structural answer
  in a new worktree starts the fenced `repository_index.py follow` detached. A
  new nightly step, `repository_index.py retire`, removes every generation of
  a checkout whose root is gone or that is marked not indexed, and all but the
  newest two of a live one — foreign generations were never activated, so the
  pruner had kept every one — each repository under its refresh fence, the
  vault's own generations never considered; hint tables without a generation
  go with them. `git config branch.<name>.llmwikiIndex false` or
  `llmwiki.index false` marks a checkout; `index`, `refresh` and `follow`
  refuse it by name. Cross-repository routes (D2) are not done: the graph has
  no route nodes. Research:
  `docs/research/2026-09-11-the-graph-meets-the-agent-where-it-searches.md`.
- CI installs the production profile into a clean environment on Windows and macOS too, and runs the install smoke there (audit OPS-14).
- One end-to-end nightly test runs the pass with its real step runner against real child processes, one of which fails, and checks the report line, the artifact and the recorded state (audit OPS-17).
- **The query surface answers the whole graph (#24, B).** `get_architecture`
  gains `mode=search` — ranked qualified names with in/out degree, exact
  `total` and `has_more`, globs, a path prefix — and `depth` (1–8) on
  `callers`/`callees`, a breadth-first CALLS closure reporting
  `depth_applied` and `depth_frontier_open`. `mode=snippet` accepts
  `owner.name` and cuts the block out of the generation's stored bytes at the
  exact definition span (`precision: "exact"`, `freshness` against the
  working tree). `mode=coverage` now answers `indexed` and `freshness` from
  the generation's own source row — a foreign repository's indexed file
  answered `indexed=false` beside a real node count — and adds a `parse`
  block naming the `ERROR`/`MISSING` ranges (tree-sitter) or `SyntaxError`
  line (Python) the extractor could not read. `mode=impact` adds
  `affected_symbols`: the code symbols a dirty diff reaches within eight
  hops, beside the unchanged `affected` groups. No new tool, no generation
  format change. Measured warm on a 1 020-file fixture: search ~99 ms,
  snippet 13 ms, coverage 16 ms, callers depth 3 from 20 seeds 144 ms.
  New modules
  `scripts/symbol_search.py`, `scripts/impact_symbols.py`; new readers
  `EvidenceGraph.source_by_path`, `source_observations`, `search_nodes`.
- **The weights arrive with the install.** `scripts/install_models.py`
  fetches the encoder and the default reranker at their pinned commits,
  only the files the loaders read, verifies `model.safetensors` against the
  size and SHA-256 recorded beside each revision, removes a file that does
  not match, and never fetches a present file again. The installer runs it
  when the semantic extra is present, the nightly pass runs it every night
  (a no-op once the weights are there), and `doctor` reports `models:
  degraded` with the command while they are missing. Before this nothing in
  the product downloaded a model: a fresh install answered by words alone
  and only the trace said so.
- **Background incremental refresh of repository indexes (#24, A).**
  `repository_index.py refresh <dir>` hashes a registered repository's sources
  against its newest generation and rebuilds only when something changed,
  reusing every unchanged record (one edited file in a 1 022-file fixture:
  100 rebuilt, 922 reused, 16 s against 60 s for the full build), fenced under
  the ownership registry's `doctor` role scoped to that one repository. The
  MCP server spawns it detached once per repository and commit when a
  structural answer finds the checkout's commit ahead of the generation's; the
  nightly pass runs `refresh-all`. Structural answers carry a `freshness`
  block naming both commits and what was done. `repository_index.py` gains a
  command line (`index`, `list`, `detect`, `refresh`, `refresh-all`).

### Changed

- **The encoder runs through ONNX Runtime, without torch.** The search loaded
  `intfloat/multilingual-e5-small` through `sentence-transformers` on `torch`:
  6.5 s before the first query, 8.2 s for a cold command-line search. It now
  reads the model's own `onnx/model.onnx` at the same pinned revision with
  `onnxruntime` and `tokenizers` (`scripts/onnx_encoder.py`): a cold search takes
  2.3 s, a batch of 402 vault texts encodes in 4.8 s instead of 5.9 s, and the
  vectors equal the old ones to 1.4e-07, so no index is rebuilt. The `semantic`
  and `hybrid` extras no longer pull `torch`; only the reranker does.
  `install_models.py` fetches and verifies the ONNX weights and then removes the
  encoder's retired PyTorch weights (470 MB) from the cache. See
  `docs/research/2026-09-25-the-encoder-runs-without-torch.md`.

- **One classification prompt.** The capture path sent three lines that named
  `FLUSH_OK`, `FLUSH_MAJOR` and `FLUSH_MINOR` and never said what a tier meant,
  while the measurement stand scored `build_classification_prompt`, which no
  installed hook reached. The capture worker now sends that prompt with its
  system prompt, so the stand measures what the product sends; the wire grammar
  and its parser are unchanged. Measured on the same 21 confirmed real cases:
  the short prompt named the tier right 0.857 of the time and kept 0.667 of the
  marker terms; the rich prompt 0.905 and 0.476, because "be terse" dropped
  terms; with one added sentence — keep names exactly as the transcript spells
  them — 0.952 and 0.714 (2026-09-23).
- **A fresh install builds its first generation.** `doctor` reports a missing
  generation as degraded and repairable instead of "legacy retrieval remains
  available", so the installer's `sync_memory.py --apply` builds it (measured:
  48.9 s for the live vault's 189 pages with vectors, into a scratch state
  root), `doctor --rebuild-generation` builds it on demand, and a
  generation reason (`generation_corrupt`, …) is reported ahead of the
  Markdown read's `no_active_generation` when both apply.

- **One limit, one place.** Fifteen numeric bounds that were copied into a
  second module now have one owner each (the 64 KiB I/O chunk, the archive
  installers' bounds, the extractors' bounds, the capture-intent bound, the
  hook lock timeout, the index bound, the hook-configuration bound, the
  generation-manifest bound, the ninety hot days); doctor reads the hook
  configuration and a generation's manifest under the writers' own bounds
  instead of smaller ones of its own. Every remaining name reused for a
  different bound says what it bounds, and `tests/test_one_limit_one_place.py`
  refuses a new copy. `docs/LIMITS-2026-09-23.md` lists every limit with no
  recorded reason. `docs/research/2026-09-23-one-limit-one-place.md`.
- **The memory generation carries a project's claim pages, not its journal.**
  On the live vault of 2026-09-23 `journal.md` was 94 % of the search index's
  bytes — one checkpoint event per chunk — and most of the reranker's time;
  the accepted 2026-09-10 decision already names `state.md`/`context.md` as the
  claim pages and the journal as the event log. The journal stays on disk,
  consolidated nightly and greppable, as session records do.
- **A directory is a project only if the owner could be working in it.** The
  slug rule refuses a directory inside the vault, a direct child of the
  platform's temporary directory (what `mkdtemp` and the provider CLI make) and
  the home directory, for every caller at once. The audit found 85 project
  directories, minted for a benchmark run under `cache/`, a transaction under
  `run/`, `/tmp`, the provider's temp directory, `$HOME` and the vault itself.
  `docs/research/2026-09-23-the-corpus-is-the-claim-pages-and-a-project-is-a-project.md`.

- **The navigation gate measures our share, not the machine.**
  `warm_overhead_p95_ms` failed CI at 34.28 ms against 30 while
  `direct_pyright_p95_ms` — Pyright itself, which no change of ours can reach —
  rose 27.6% in the same three hours; our share of the work moved 2.4%. The
  bound is now `max(30 ms, 0.90 x direct_pyright_p95_ms)`: the approved 30 ms
  floor, or, on a slower machine, 90% of what the language server took on the
  same queries in the same run. A uniformly slower runner can no longer fail the
  gate; our layer growing its share still does, and an absent or non-positive
  control makes the evidence incomplete. The August runs, where our share was
  0.48-0.50 against today's 0.72-0.74, show what the single absolute number was
  blind to. See `docs/CODE-NAVIGATION.md` and
  `docs/research/2026-09-18-the-gate-measures-our-share-not-the-machine.md`.
- **The test tree comes under the complexity law.** `lizard -C 5` reported 88
  functions over CCN 5 in `tests/`; it now reports none. The scenario ladders
  in the navigation tests become tables of runners and builders, the
  121-line AST walk over the benchmark fixture becomes one function per module
  and per block, the fake LSP server's two 300-line loops become
  `_SemanticServer` and `_LifecycleServer` with one method per protocol
  request, and the runtime-deletion guard resolves paths through a dispatch on
  node type. No test lost an assertion: each file was run before and after.

- **The reader is named by configuration, and this machine names Sonnet.** With
  `MEMORY_CLAUDE_MODEL` unset the claude CLI call carries no `--model` flag, so the
  pipeline read with whatever the operator's own session was set to — on this
  machine Opus, which refused the grounded-QA prompt and failed 18 of 19
  LongMemEval questions. The code still invents no model; the docstring says why,
  and the machine is configured instead (agent sessions and both maintenance
  units). Verified on the installed vault: answered in 59.1 s where it refused
  before. See `docs/research/2026-09-13-the-pipeline-asks-sonnet-by-default.md`.

- Every file touched in this round also passes the second complexity analysis, which counts what the first did not: more than two `if` at one level, an exit followed by `else`, and radon's count of asserts and comprehensions. 157 findings across 14 files went to 0, the largest being impact analysis (`analyze_impact` from CCN 34), the LSP path and log guards, and the retrieval stand; messages, check order and outputs unchanged.
- The older code passes both complexity analyses too: every product module under `scripts/` and `benchmark/` is at zero findings (the last five, in `codex_memory` and `merge_claude_settings`, were fixed on the #24 branch). The largest were the code-graph extractor (`extract_code` CCN 63), the Pyright navigation facade (one method at CCN 90), the analysis contracts and the installer. The extractor's output over the whole repository is record-for-record identical, so `EXTRACTOR_VERSION` stays `code-extractor/v11` and no graph is rebuilt for it; the navigation branches the tests never reached are now pinned by `tests/test_code_navigation_fault_paths.py`, which passes on the code before and after the change.
- **Codex sessions leave the same breadcrumbs as Claude's.** Codex supports `UserPromptSubmit` and `PostToolUse` for `apply_patch` and `Bash`, and the template registered neither for capture, so a Codex session recorded no mid-session prompt or edit. Both are registered now through `integration_adapter.py --source codex`, a patch is recorded as an edit of the first file it touches, and the ownership rule and the doctor's runtime-hook check know the two new handlers. The capture path prints nothing, which is what Codex requires of a `UserPromptSubmit` hook. Research: `docs/research/2026-09-11-codex-leaves-breadcrumbs-too.md`.
- Contextual retrieval keeps only what runs: the LLM branches that every entry point refused before reaching them, and their option validator, are gone; the deterministic context, the cache identities for both modes, every public signature and every message stay; the rest is named steps under the complexity gates.

- The retrieval stand obeys the complexity gate: one run is an object with a method per stage (build, selection, embedding and its lexical fallback, materialized retrieval, reranking, evaluation, report), report verification and selection aggregation are named checks, the CLI is a table of modes; report bytes, messages, error order and clock reads unchanged (audit H3).
- The navigation stand obeys the complexity gate: schema validation is one function per keyword, the fixture run is one object with a phase per measurement, the evidence check is a list of named predicates, the gates are one entry per field; reports, error order and gate verdicts unchanged (audit L13).
- The comparative stand obeys the complexity gate: one check per manifest section, one finding collector per preflight probe, the paired statistics as named steps; messages, codes and RNG consumption unchanged (audit L13).
- The scale stand obeys the complexity gate: the three optional adapter cells share one cell shape, the crash matrix is one outcome per point with named steps; reports and adoption reasons unchanged (audit L13).
- The Python qualification generator and the contradiction benchmark obey the complexity gate; their output is byte-identical (audit L13, first two files).
- `pyright_profile` obeys the complexity gate: the Node probe is one run with named phases, JSONC normalisation is two small scanners, and each system-candidate shape is one function; degradation codes and precedence unchanged (audit OPS-15, the last file in scope).
- `lsp_protocol` obeys the complexity gate: the frame reader, the JSON validators, start-up, the writer loop and the fatal transition are small steps under the one state lock; messages, outcomes and lock discipline unchanged (audit OPS-15).
- `lsp_security` obeys the complexity gate: the no-follow walk, the provider URI check and the path redaction scanners are pipelines of named steps; every containment message and refusal order is unchanged (audit OPS-15).
- `sync_memory`, `install_smoke` and `lsp_positions` obey the complexity gate: the sync run is a table of actions over one `_SyncRun`, the three `uv` steps are one step record, the file-URI parser is a pipeline of named checks; behaviour and messages unchanged (audit OPS-15).
- Three tests no longer sleep to assert that a worker is still blocked; the outcome after the release is the proof (audit M9).
- The public search path is a pipeline over one `_SearchRun` object instead of a 290-line function with eleven closures; behaviour and the trace are unchanged (audit L8).
- The 16 and 64 GiB constants of the evidence graph and the generation catalog are declared as absurdity ceilings that name where the real read bounds live (audit M8).
- Test helpers obey the complexity gate: the 24-arm damage ladder in the evidence-graph tests is a table, and six other test functions are split into named helpers (audit M12).
- **A `recall` row carries its page, not the trace.** Through MCP each row
  repeated the twelve trace fields and thirteen per-signal scores the
  envelope already reports once; rows now carry the page, its score and
  one per-signal score. Measured on one five-row call: a row 1 112 → 541
  bytes, the envelope 8 849 → 4 996 (audit M7, rule 4). Research:
  `docs/research/2026-09-11-a-row-carries-its-page-not-the-trace.md`.
- **The installer job runs on macOS too.** The LaunchAgent path had no CI
  evidence (audit OPS-14); `macos-15` joins the installer matrix. Research:
  `docs/research/2026-09-11-the-installer-runs-on-every-platform-it-claims.md`.
- **The shipped Claude Code allowlist grants only read-only forms.** `Bash(sed *)`,
  `Bash(xargs *)`, `Bash(sort *)` and `Bash(uv run --directory *)` let an
  agent rewrite files or run any Python without a prompt under a
  read-only-looking name; they are gone, `sed -n *` stays, and the built-in
  read-only commands (`ls`, `cat`, `grep`, `find`, …) need no entry. The
  settings merge retires exactly those four strings from an installed
  `~/.claude/settings.json` on the next install or sync (audit OPS-12).
  Research: `docs/research/2026-09-10-an-allowlist-that-reads-as-read-only-must-be-read-only.md`.
- **The nightly and weekly passes take the canonical fence.** On an adopted
  vault they hold the registry's `nightly`/`weekly` lease with a heartbeat,
  so the doctor and the `run/` deletion contract see a running pass; a lost
  fence stops the pass before its next step and is recorded as
  `owner_fence_lost` instead of success; a marker a dead owner left behind
  is reclaimed only with the registry's proof (expired lease and a provably
  dead process, or an ownerless marker naming a PID that no longer exists),
  never by age. A vault without a V3 coordinator keeps the legacy marker. The
  `inspect.signature` ownership plumbing that no step ever received is gone
  (audit OPS-02, OPS-03; owner's yes 2026-09-10). Decision:
  `knowledge/notes/nightly-takes-the-canonical-fence-decision.md`; research:
  `docs/research/2026-09-10-the-nightly-and-the-fence-it-never-takes.md`.
- **The cross-encoder reranker is on by default.** `BAAI/bge-reranker-v2-m3`
  at its matrix-pinned revision is the product default when the environment
  names no reranker (`LLMWIKI_RERANKER_MODEL=off` switches it off), it
  reranks every question in a rerank profile instead of waiting for a
  trigger a Russian question over English pages never matched, its depth is
  10 over the fused pool of 20, and the MCP server loads it at start-up so no
  question pays the load. Measured on the 45-query cross-lingual corpus:
  cross-language MRR 0.60 → 0.98 on the shipped encoder, where swapping the
  encoder gained at most 0.04 (issue #29.3). A load that fails is recorded
  once and not retried per question.
- **Structural code answers are warm (#24, A).** The validated Evidence Graph
  reader is kept per MCP process and reused while the catalog, the artifact
  and the checkout's Git state keep their stat identity, instead of the
  catalog re-validating the generation three times per open and hashing every
  artifact each time. Same 44.7 MB generation, warm p50: `callers` 42 ms (was
  511), `callees` 23 ms (249), `symbol` 86 ms (1 007), snippet 21 ms (335),
  coverage 21 ms (258).
- **`code_graph.py --callers` no longer re-parses a repository without a
  generation** (300 s on 1 026 files in #24): it names `mode=index` and exits
  2; `--live` opts into the scan.

### Deprecated

- **`compile_memory.py --all`.** It has never changed anything: every daily log
  without a committed receipt is compiled anyway, and a day with one is never
  compiled again. The flag is still accepted, is hidden from `--help`, and now
  prints one line saying it does nothing and will be removed.

### Removed

- **`optimum` and the encoder's `sentence-transformers`.** `optimum` was declared
  in the `reranker` extra and imported nowhere; the reranker's comment said "via
  ONNX", which it has not done since it moved to `transformers`. The search
  extras no longer name `sentence-transformers`; only the retrieval benchmark,
  which measures the native library on purpose, keeps it.

- **Code no product path reaches.** Seven functions nothing called; twelve that
  only their own tests called (`MemoryQueue.export_task`,
  `payload_for_execution`, `retrieval.trace_to_dict`,
  `project_journal.build_handoff`, `retrieval_telemetry.record_event` and
  `count_events_after`, and six smaller ones); the Windows directory-listing layer
  of `generation_catalog.py` they left unreached; and the module-level queue owner
  of `memory_queue.py`, which no product module called since the queue worker and
  doctor own the queue through `MemoryQueue.queue_owner`. Tests that used a removed
  function as a tool now use the product path. Test seams and the Reliability v3
  repair and discard operations stay. `compile_memory.py --all` stays deprecated
  until 6.0.0. See
  `docs/research/2026-09-24-code-no-product-path-reaches-is-removed.md`.
- **The manual label review.** `benchmark/review_flush_labels.py`, its tests and
  its verdict sidecar are gone: the owner does no manual labelling, and the two
  automatic readings above replace the step it existed for. Corpus schema v2
  (`label_reviewed`, `human_tier`) is refused; the only v2 corpus was the private
  live one, rebuilt under v3 (2026-09-23).
- **Legacy that nothing reads.** The pre-telemetry `cache/access_log.jsonl`
  reader (no writer since 2026-08-20, no file on the live vault), the
  positional `git_range` of `analyze_impact` (every caller names its
  endpoints), and the readers for incremental-manifest versions v1–v4 (every
  generation on the live vault is v5; a fresh install builds v5). A parent
  whose manifest cannot be read is now rebuilt in full instead of failing the
  build. The legacy FTS index, the v2 queue and coordinator readers and the JSON
  queue migration stay, with the evidence and the plan for each in
  `docs/research/2026-09-23-legacy-that-nothing-reads.md`.
- **The legacy FTS5 index and vector cache.** `cache/index.sqlite`,
  `cache/.paths-manifest`, `cache/vectors.npy` and `cache/vectors_meta.json`
  are read and written by nothing: their builders, readers, swap lock and
  freshness manifest, `doctor`'s `index` check and repair, `sync_memory`'s
  index builder, the nightly's Step 3b, `lookup_mode`'s index probe and about
  a hundred functions with them. On the live vault the legacy index answered
  537 of 15 284 retrievals ever, the last on 2026-09-05; a fresh install read
  it by design until now. The evidence generation is the only index: without
  one a search reads Markdown directly, bounded by its deadline, and every
  such hit says `no_active_generation`; `search_memory.py --rebuild` rebuilds
  the generation and `--status` names it. See
  `docs/research/2026-09-23-the-generation-is-the-only-index.md`.
- **The JSON queue import.** Releases v3.3.0–v3.4.0 (July 2026) kept one file
  per task under `run/queue/`; the importer, its marker, quarantine, lease
  repair and `memory_queue.py migrate` are gone, as is the `run/queue/` the
  installers still created. Measured before removal: the installer's adoption
  already refused such a vault and never ran the import. A `run/queue/` holding
  records is now refused by the queue (`legacy_json_queue_unsupported`) and
  named by `doctor`, which keeps `run/` from deletion while they exist. See
  `docs/research/2026-09-23-the-json-queue-import-goes.md`.

- `scripts/session_feedback.py`, a decision-staleness loop that was never wired: nothing recorded an injection, so its check always found nothing and its verdict was read by nobody.
- The daily-log file lock (`daily_log_append._daily_lock`) and the four tests that exercised it: no writer has taken it since every daily-log write moved onto the transaction's `append_knowledge`, whose cross-process serialization the writer-integration and append-race tests already prove.
- **The second, unreachable retrieval pipeline.** `search_memory._search_backends`
  had no caller; 40 functions reachable only from it (legacy triple RRF, its
  own reranker call, its own generation search) and the tests that existed
  only for them are gone — 897 lines. The live path is unchanged:
  `search()` → `retrieval.retrieve_via_search_memory` → `retrieval.fuse_rrf`.
  Audit H1; research `docs/research/2026-09-10-one-retrieval-pipeline-not-two.md`.
- **The repository ships no memory.** The 89 published pages under
  `knowledge/notes/` (the owner's architecture decisions and the
  demonstration pages), the two synthetic daily logs and the vault's log
  entries leave the repository; a fresh install starts with an empty memory
  instead of another person's guard rails and "89 curated pages" (issue #19).
  The owner's pages stay where they are, private. `knowledge/index.md` and
  `knowledge/log.md` ship as empty skeletons the runtime fills.
- **The memory index no longer holds the product's own code.** The vault
  generation collects `knowledge/` only; `scripts/`, `docs/`, `tests/` and
  `benchmark/` of the checkout were 92 % of an installed vault's chunks and
  outranked the user's pages in `recall` (issue #29.2). Code is indexed per
  repository, the checkout included when its owner asks.
- **The legacy BM25 benchmark gates are retired.** `run_benchmark.py
  --legacy-only` and the generated-query run measured recall over the pages
  the repository used to ship; with no pages shipped there is nothing to
  measure. `run_benchmark.py` is the retrieval-v2 entry point; the CI step
  and `benchmark/legacy-60-v1.json` are gone.
- **The vault stands are retired**: retrieval, application, contamination
  and lift attribution, with their answer-key and entry-point helpers. Their
  questions and gold pages were the owner's decision pages; with those pages
  private there is nothing public to run them on. The synthetic
  `retrieval-v2.json` corpus (which carries cross-language queries) and the
  LongMemEval stand are the public measurements.
- **Cursor and Antigravity are no longer supported platforms.** The owner uses
  neither, and carrying two hosts nobody exercises meant two managed hook
  formats, two doctor checks, two installer detections, and two event
  projections whose only evidence was their own tests. Claude Code, OpenCode,
  and Codex CLI remain supported. Gone: `integrations/cursor/`,
  `integrations/antigravity/`, the `--cursor-hooks` / `--antigravity-hooks`
  install flags, installer detection in `install.sh` and `install.ps1`, the
  IDE branches in `integration_adapter.py`, the two `event_envelope.py` agent
  patterns, the two `doctor.py` integration hosts, and the `cursor` /
  `antigravity` values of `flush_memory.py --agent`.
- What an existing user of those hosts loses: automatic capture and injected
  session context. MCP reads and actions were never platform-specific and are
  unaffected — any agent that speaks MCP can still use the vault.

### Fixed

- **The health and context resources are readable.** The server's read handler
  returned the protocol model `TextResourceContents` where the MCP SDK 1.29
  expects its helper `ReadResourceContents`, so every `resources/read` failed with
  `'TextResourceContents' object has no attribute 'content'`; the unit tests had
  replaced the type with a stand-in. A test now reads both resources through a
  real stdio session. See
  `docs/research/2026-09-25-the-health-resource-is-readable.md`.

- **A foreground subagent's report is kept with its session.** The session record
  dropped every tool result, so the conclusion a foreground subagent returned was
  missing from it; it is now kept as `**subagent report:**`, bounded to 8 000
  characters. A background launch's receipt is skipped: its report arrives as a
  notification the record already keeps. There is no separate subagent capture,
  which would classify each subagent again and duplicate the report. See
  `docs/research/2026-09-24-a-subagents-report-is-kept-with-its-session.md`.
- **The documents say what the code does.** The three READMEs, USER-GUIDE,
  ARCHITECTURE, STRUCTURE and the operating model no longer tell a reader to keep
  the retired legacy index, no longer say search falls back to it, name the pinned
  embedding and reranker models, describe the one automatic Git operation (the
  nightly fast-forward of the default branch) and the retention purges, and show
  `build_context.py <slug>` as it is parsed. Three superseded status documents are
  marked as history. Doctor has a `backup` check: the nightly knowledge snapshot
  must be at most two days old. A test pins the corrected claims. See
  `docs/research/2026-09-24-the-documents-say-what-the-code-does.md`.
- **The vault follows its default branch.** The nightly update fast-forwarded
  whatever branch was checked out, up to what `main` held, so a vault left on a
  working branch never received what reached `main` from any other branch and
  still said `current`. It now updates only on the default branch and answers
  `skipped: not_on_default_branch` (naming the branch) anywhere else; a checkout
  ahead of its remote says `ahead_of_remote`. See
  `docs/research/2026-09-24-the-vault-follows-its-default-branch.md`.
- **A long session in the vault is captured, and each capture keeps its window.**
  The prompt hook skipped every prompt inside the vault as "maintenance", so the
  owner's own sessions there never reached the every-20th-prompt capture; the
  adapter's reentry marker already filters the memory's own processes, and the
  vault rule is gone. A second, different record of a session on one day is
  written beside the first (`<session>@<digest>.md`) instead of replacing it, so
  the earlier window survives and consolidation reads the new one. A hook
  delegate that exits non-zero is recorded as a lost capture, and the prompt hook
  no longer runs on no-op stand-ins when its state module will not import. A rule
  no longer reads "When When". The 25 dead capture tasks of August and September
  are archived by the weekly purge rather than redriven under today's date. See
  `docs/research/2026-09-24-a-long-session-in-the-vault-is-captured.md`.
- **Every store that only grew has a bound.** Settled transaction rows and the
  attempts of committed checkpoints are dropped after 90 days (checkpoint rows
  and quarantined transactions stay), and doctor reports exact row totals by
  state. The weekly runs the designed queue purge: finished work older than
  `queue_result_retention_days` is exported to the private
  `knowledge/raw/queue-archive/<date>/` and removed from `run/`. The hook error
  log is trimmed with the scheduler logs; old lock probes, empty intent shards
  and staged state links are swept (the state keeper no longer leaves one when
  `rename` finds the same inode); benchmark run directories untouched for 30 days
  are retired while dataset caches stay; and checkouts under the temporary or
  host job directory are neither refreshed nor followed, and their code
  generations are retired. See `docs/research/2026-09-24-every-store-has-a-bound.md`.
- **An answer says how old its index is, and the index follows the compile.** A
  page compiled during the day was not searchable until the next nightly, and
  `recall` said `fresh`. A successful command-line compile now refreshes the
  memory generation (a no-op when nothing changed); `recall` reports its lexical
  and dense components `stale` when a page changed after the generation was
  built, and the envelope's `index_timestamp`, declared and never filled, now
  names the build time. Graph is no longer reported "missing" from an answer
  whose mode never asks for it. Doctor calls a generation stale when changed
  sources waited a day unrefreshed, not for every daily-log append nor for age
  alone. `lookup_mode` reports the mode search runs in (HYBRID, BASE, DIRECT)
  instead of a page-count recommendation nothing followed, and `wiki_overview`
  carries the same. `prune_generations` counts only memory publications as
  pending. Command-line hits show their text, not only their heading, and the
  model-loading progress bar is gone. See
  `docs/research/2026-09-24-an-answer-says-how-old-its-index-is.md`.
- **The memory server reloads its own code.** A Claude Code session keeps its
  stdio MCP server for its whole life, and the nightly changes the code under it:
  on 2026-09-23 a server kept its old `bounded_io`, imported a new
  `evidence_graph` lazily, and answered `get_architecture` with
  `operation_failed` for the rest of the session. `mcp_server.py`, run as a
  program, is now a standard-library supervisor (`mcp_supervisor.py`) that serves
  the tools from a child process. When a request arrives, none is in flight and
  the code under `scripts/` has changed, it restarts the child and replays the
  client's initialisation; a child that exits answers what was in flight with an
  error and is started again. The registered command is unchanged, and
  `--help` no longer starts a server. See
  `docs/research/2026-09-24-the-memory-server-reloads-its-own-code.md`.
- **A vanished project is rebuilt by the night.** A project whose directory was
  deleted while its committed checkpoints remained blocked every later checkpoint
  with `ProjectJournalRebuildRequired`; the nightly re-attempted it, printed
  `failed` and exited 0, and the pass said `failures=0`. The nightly now runs the
  same `rebuild_journal` + `recover` the manual `project_journal.py --rebuild`
  runs, when the journal is behind the store (never over a journal that is ahead),
  and exits 1 for a row it could not settle. Doctor ages a stuck sequence from its
  first attempt, so the night's own retry no longer hides it, and calls an
  unreadable checkpoint database `degraded`. See
  `docs/research/2026-09-24-a-vanished-project-is-rebuilt-by-the-night.md`.
- **The weekly pass has its own record, and doctor reads it.** The weekly failed
  on 2026-09-13 and 2026-09-20 and nothing said so: a failure before its first
  step went into the nightly's field, which the next nightly overwrote, a failure
  inside it was recorded nowhere, and doctor had no weekly check. It now writes
  `last_weekly_status`/`last_weekly_at`/`last_weekly_failure`; doctor's scheduler
  check calls a failed weekly an `error` and one silent for more than eight days
  `degraded`. The weekly no longer re-runs the whole nightly an hour after it
  (a second compile, repository refresh, self-update and prune). Step labels in
  both logs are numbered by the logger in the order the steps run
  (`StepLog`); "Step 3c" had come to name three different steps. See
  `docs/research/2026-09-24-the-weekly-pass-has-its-own-record.md`.
- **A stray candidate is retired where it refuses.** A provably stray
  pre-adoption candidate (complete adoption, no adoption in flight, no row in any
  data table, no live owner) is moved into `run/coordinator-quarantine/` or
  `run/queue-quarantine/` by the writer that meets it, not only by
  `doctor --repair`, which nothing ran on a schedule: on 2026-09-17..23 one stray
  refused capture, the nightly and the weekly for six days. The queue candidate is
  covered too, the quarantine directory is created `0700`, and doctor's adoption
  check counts what has been quarantined. One rule, in
  `installed_memory_repair.retire_stray_candidates`. See
  `docs/research/2026-09-24-a-stray-candidate-is-retired-where-it-refuses.md`.
- **A test no longer reads a pid file before it is written.** The push run on
  `main` after PR 41 failed on Windows: a processor killed at its 1-second deadline
  between creating its pid file and writing it left an empty file, and the test
  parsed it. Pid files in the queue tests are now written through a temporary name
  and `os.replace`, read with a bounded wait (`tests/pid_files.py`), and the kill
  test's deadline is 10 s (CI run 36023732204, 2026-09-24). See
  `docs/research/2026-09-24-a-pid-file-read-before-it-is-written.md`.
- **Telemetry schema is created inside the write transaction.** Eight processes
  recording into a fresh telemetry database on Windows: five failed at once with
  `database is locked` although the connection had a busy timeout, because
  `CREATE … IF NOT EXISTS` outside a transaction promotes a read lock to a write
  lock, the one wait SQLite refuses. `record_events` and `trace_ingest.open_store`
  now create their schema under `BEGIN IMMEDIATE` (CI run 35941975284, 2026-09-24).
- **A pinned download that meets a transient network error is tried again.**
  `pinned_download.retry_transient` runs a download up to three times, after 1 s
  and 4 s, for a connection reset, a timeout, HTTP 408, 429 or 5xx, inside the
  install deadline; a 4xx, a redirect or a digest mismatch is raised at once. The
  language-server installer uses it, truncating the target between attempts; one
  reset had ended a CI install (run 35926589114, 2026-09-23). See
  `docs/research/2026-09-24-two-post-merge-failures-on-main.md`.
- **The adoption gate names its cause and the queue waits out a busy
  database.** `reliability_v3_record_invalid` now carries what the validation
  saw (`code: Cause: message`), and the adopted queue validates adoption
  through the coordinator's retried, cached check instead of a bare call:
  on the live vault a `doctor --rebuild-generation` at 18:27 UTC failed its
  queue repair with the bare code while a later run and a standalone check
  passed. See
  `docs/research/2026-09-23-the-adoption-gate-names-its-cause-and-waits-out-contention.md`.
- **A barrier proves concurrency; a stopwatch measured the machine.** The
  three parallel version probes of `detect_code_tools` were asserted to finish
  under 0.35 s; on a Windows runner under four shards they took 0.60 s (CI run
  35857662331) with nothing wrong. They now meet at one `threading.Barrier`,
  which a sequential probe would break. See
  `docs/research/2026-09-23-a-barrier-proves-concurrency.md`.
- **A dead task names its reason.** Every exception a processor raised became
  the one code `processor_failed`; on the live vault 225 failed attempts of 25
  dead `flush` tasks said only that, while the failure trail held the actual
  reason (`intent_fence_lost`) for seven of them. A failed attempt now carries
  a queue error's own code, or `processor_failed:<reason>` naming the
  exception's type or its code-shaped message, bounded to the column's 64
  bytes and never the message text. See
  `docs/research/2026-09-23-a-dead-task-names-its-reason.md`.
- **A scheduled pass that fails before it starts is recorded as failed.** The
  nightly and weekly passes take their fence first; a refusal there exited 1
  and left `last_nightly_status = success` for six nights while session start
  said nothing. Doctor also says when a transaction scan stopped at its row
  bound (its counts are lower bounds), admits a `run/state.json` up to 4 MiB (the writer's
  own bound exceeds the old 256 KiB), and rebuilds the legacy index with the
  same page collector its freshness check uses, so the repair can succeed.
  Session start counts daily logs at the top level (receipts are not logs), the
  guard-rails block joins a wrapped one-sentence summary, a compile dry run moves
  no clock, pending checkpoint events older than 30 days of vault activity are
  dropped and noted, and the nightly retires LSP failure roots older than 14 days
  beyond the newest 20 (`scripts/retire_lsp_evidence.py`). Session
  consolidation gives its provider the compile's 300 s ceiling instead of the
  client's 90 s default, and `install_pyright.py` reinstalls over a receipt that
  predates the tree digest instead of refusing the repair it is recommended for,
  and re-validates an installed tree by its files only (pyright ships
  `dist/typeshed-fallback/` as a directory, which every re-run had read as a file).
  `docs/research/2026-09-23-the-rest-of-the-live-audit.md`.
- **A stray pre-adoption candidate no longer stops the memory in silence.** A
  pytest session whose state root resolved to the live vault left an empty
  `run/markdown-transactions-v3.candidate.sqlite3` there on 2026-09-17; the
  adoption boundary refused every capture, checkpoint and compile for six days
  while doctor named only the symptoms. Doctor now has an `adoption` check that
  reports the refusal as an error with its cause and the stray path, and
  `--repair` moves an empty, ownerless candidate to `run/coordinator-quarantine/`
  (retained evidence, never deleted) once no adoption is in flight. The test
  harness refuses an external state root that is the vault or inside it, and
  the per-test progress file gets its directory before the first test.
  `docs/research/2026-09-23-a-stray-candidate-stopped-the-memory-for-six-days.md`.

- **A repeated append no longer hashes the file it only has to find.** The
  2026-09-18 presence check read and digested the whole daily log outside any
  lock, so under eighteen concurrent writers it refused with `transaction
  target changed while hashing` and a line already on disk was reported as a
  failed capture (one red job on `main` after PR 36). Presence is now one
  `lstat`. `docs/research/2026-09-23-a-presence-check-does-not-hash.md`.

- **A timing ratio under a tenth of a second measured the machine.** Five ratio
  gates used a 0.05 s floor and a sixth derived one from the process clock tick;
  a macOS shard failed `0.0946 <= 0.0714` on a step whose quiet time is 22 ms,
  after already taking the best of five attempts. They now share
  `tests/timing_floor.noise_floor_seconds()` — `max(0.25 s, 8 clock ticks)` — and
  the deterministic half of those tests, the counted scanner calls that double
  exactly, is what still proves the linearity claim.

- **Two seconds for a Git probe was a Linux figure.**
  `repository_scope.GIT_TIMEOUT_SECONDS` was 2.0 while `repository_index` gives the
  same probe 10.0, and on a hosted Windows runner `git rev-parse` crossed it: a
  navigation test failed with "repository scope deadline reached during Git probe"
  with nothing wrong with the repository. One operation, one bound — it is ten now,
  and a test pins the two constants together.

- **A retry of a completion must replay its own request.** `complete_task` built
  its record with a fresh clock reading, so a completion interrupted by
  `database is locked` could never be finished: the operation id was the claim's,
  the payload was not, and the transaction layer adopts a bound operation only
  when the block matches. It takes `completed_at` now, defaulting to the moment it
  runs, and a retry-aware caller replays the value it used first. Main run
  34760092169 failed on exactly this. See
  `docs/research/2026-09-13-a-retry-must-replay-the-same-request.md`.
- **A refusal says what the provider said.** Eighteen of nineteen rows of a
  LongMemEval run carried only "grounded QA provider returned invalid JSON" while
  the provider had answered `API Error: ... safeguards flagged this message`,
  which named both the cause and the fix. The refusal now carries the response
  length and its first 200 characters, whitespace collapsed and redacted. See
  `docs/research/2026-09-13-an-unparsable-answer-must-say-what-it-said.md`.

- **The Windows job cap had no headroom.** `timeout-minutes: 40` sat a few
  minutes above the measured 22-30 minute range, and one shard that took 45 on a
  slow runner was cancelled at the cap — which GitHub reports as `cancelled`, not
  `failure`, and refuses to re-run, so a branch could not go green without a new
  commit. The Windows class now has 60 minutes; Linux and macOS keep 20, where the
  range is 6-12.

- **An answer stops paying for whitespace and for a module name the path already
  spells.** A tool answer is serialized compactly, and a row that carries
  `scripts/retrieval.py` no longer repeats `scripts.retrieval` inside its
  qualified name. One `callers` question on the installed vault: 836 tokens
  before, 561 after. Nothing is dropped, and the test callers the other tool
  filters out stay.
- **A line the file has moved on from is read from the file.** The generation is
  rebuilt by the nightly pass, so a definition edited today used to answer with
  yesterday's line — measured, 3030 against the 3054 it had moved to. Every file
  an answer names is now parsed when its digest no longer matches, at most 20 per
  answer, inside the caller's deadline, and nothing is written: no generation, no
  catalog row, no active pointer. A stale code snippet is read from the file the
  same way, so its text is the text that is there. On the same tree the parity
  stand goes from 15 of 16 to 16 of 16 on both our columns, and the other tool is
  the one answering T04 with a stale line. See
  `docs/research/2026-09-13-a-shorter-answer-and-a-fresher-line.md`.

- **A graded line number is read from the tree, not frozen in the gold.** T04
  asks where `_page_diverse` is defined and graded on the literal `3030`; the
  slot fix moved that definition to 3054, so every side would have graded wrong
  whatever it answered, and CI said so on shard 2. The term is now
  `{line:scripts/retrieval.py:_page_diverse}`, resolved when the answer is
  graded, and a guard test fails if it resolves to nothing.

- **One argument, one slot.** A compiled note could take three of ten visible
  rows with three headings of the same page, because since 2026-09-08 a slot
  belonged to a page *and* heading — right for a daily log, whose headings are
  separate sessions, wrong for a note, whose headings are sections of one
  argument. The unit of a repeat is now the episode under `knowledge/daily/**`
  and `knowledge/raw/**` and the page everywhere else; nothing is dropped, the
  repeats still follow. Measured on the live vault's notes: pages surfacing in a
  ten-row window rose from 70 to 80 of 100. The selective-forgetting stand, which
  found this, now decides presence over a window of 200 — every page it called
  forgotten was retrievable at rank 11 to 13 — and reports the ten-row share
  instead of gating on it. All nine of its gates pass. See
  `docs/research/2026-09-13-one-argument-one-slot.md`.

- **A definition's line number is not a fact worth storing.** The parity gold
  cited `scripts/search_memory.py:5038 (def _legacy_vector_source_membership)`,
  and the guard test turned the suite red twice in one day because edits above
  that definition moved it — while the gold's claim stayed true. Eleven anchors
  that name a definition now name the file and the definition and leave the line
  to be resolved from the tree; the guard still fails loudly when a definition
  leaves its file, and every *graded* line number keeps its exact check, because
  those numbers are the measurement. See
  `docs/research/2026-09-13-what-the-stands-must-show-after-the-verdict-cache.md`.

- **A digest recomputed over damaged rows cannot catch them.** Moving the chunk
  walk off the read path took the corruption fallback with it: five kinds of row
  damage — a heading ancestry that is not a JSON list, a broken `chunk_order`, a
  `source_sha256` or `chunk_id` that is not a sha256, blank content — were served
  from the generation instead of falling back to lexical search, because the test
  that damages a row also refreshes the manifest descriptor. The walk is back on
  the read path and its verdict is now remembered by the artifact's content
  digest, so it is paid once per distinct bytes instead of once per process; a
  deep check still walks and still re-derives. Two fake catalogs in the code-graph
  tests learned to answer the code-generation question the reader now asks first,
  and the autonomous-bootstrap LSP test no longer uses one 0.8 s number for both
  a real server start and the replacement budget it measures — a Windows runner
  failed the start. See
  `docs/research/2026-09-12-the-rows-are-checked-once-per-distinct-bytes.md`.

- **The parity gold described the tree of 2026-08-28, and graded on it.** Eight
  of the sixteen tasks in `benchmark/code-parity-v2.json` cited line numbers
  that resolve to nothing — `scripts/retrieval.py:1378 (def fuse_rrf)` when
  `fuse_rrf` sat at 1568 in the very commit whose message says the gold was read
  by hand from the working tree. A nested `must` entry is alternatives, not a
  conjunction, so most of those stale numbers cost no grade; two tasks were
  genuinely unsatisfiable — T04 requires the line `_page_diverse` no longer
  occupies as a separate term, and T10 requires a caller this tree does not
  have. The two-hop task named `retrieve` as the only
  second-hop caller of `_fused_candidates`; the callers are
  `_partial_candidates` and `_executed_plan`. T07 asked whether
  `_search_backends` is dead code after the H1 deletion had removed it from the
  tree; it is retired with its reason recorded in the file and replaced by the
  same question about `_legacy_vector_source_membership`, whose name occurs
  exactly once in the repository — its own `def`. Every number is re-read from
  the tree on 2026-09-12, and `tests/test_parity_gold_resolves.py` fails when a
  citation stops resolving or a graded line number is one no citation names. A
  stale benchmark does not crash; it publishes.

- **A replay over a committed transaction is a duplicate, not a quarantine.**
  A project checkpoint row and its transaction do not change in the same
  instant, so a second caller could find the row still `reserved` while the
  transaction another caller ran was already `committed`; it then asked to
  refresh the lease precondition of a transaction that was no longer
  `prepared`, was told `precondition_failed`, and quarantined an attempt
  whose work was durably in the journal. `precondition_failed` is now graded
  against the transaction: committed means the caller gets the ordinary
  duplicate receipt, anything else keeps the old fence error. Found by the
  Windows job of CI run 34655557302, which is where the window is widest.

- **A capture worker that loses its intent fence no longer counts a lost capture.** Every `adapter_capture_worker` "lost" row since 2026-09-07 carried `QueueOperationError: intent_fence_lost`, and on 2026-09-11 every intent those rows named had a succeeded task, with no ready intent left without one. Losing the fence moves authority, not data: the intent is durable, a lapsed lease is recovered, an undispatched intent is adopted. That code is now recorded as deferred; every other queue error stays a loss.

- **A refused compile of a day that is compiled now no longer keeps health red.** Since 2026-08-25 the doctor reported one refused attempt "whose work never happened": a DLP refusal had staged receipts for eight snapshots of one day, snapshots that no longer exist and whose receipts can never be written. A refused attempt that meant to create only compile receipts is now history when every day its staged receipts name has a committed receipt for every part of its current bytes. Anything unreadable or unexpected keeps the finding. On the live vault the transaction check reads `ok` with this rule.

- **A claim that is already quarantined is not written again.** The nightly compile of 2026-09-11 failed with `FileExistsError`: a pending quarantined daily was planned again, the model proposed the same claim over the same evidence, and the candidate create met the file written on 2026-09-07. A candidate file that embeds the same claim id, fingerprint and evidence now counts as present; a retry of the same attempt returns its commit, and a new attempt reports "batch still quarantined" instead of failing the run. A foreign file at the path still refuses the write.

- The scale stand's exact cell no longer grades itself: it reports no recall, with the reason, instead of 1.0 by construction (audit M11).
- A queue heartbeat thread that does not stop within twice its heartbeat is refused by name (`heartbeat_stop_timeout`) instead of joined forever (audit OPS-18).
- Impact analysis no longer names the symbol after a grown line as changed: a hunk is matched against the generation's occurrences by its old byte range, the coordinate system the generation indexed (audit M13).
- **Git warnings are not diff records.** `impact_analysis` read Git's
  stderr together with the `-z` record stream, so on a checkout with
  `core.autocrlf=true` the advisory line about line endings made every
  impact answer empty and partial (`malformed zero-delimited Git diff
  record`, PR30 runs 34535006773–34550352312 on Windows). Stderr is kept
  apart and quoted only on failure. Research:
  `docs/research/2026-09-11-git-warnings-are-not-diff-records.md`.
- **`claim_operation` is a pipeline under CCN 5** (the gate refused
  `mutate` at 13 in a file the OPS-21 fix touched).
- **One answer to "is this process alive".** `scripts/process_liveness.py`
  holds the three-state probe (`alive`, `dead`, `unknown`) lifted from the
  doctor; `memory_state`, `markdown_transaction` and the doctor delegate to
  it, and a legacy lock treats doubt as alive — a process owned by another
  user is no longer read as dead and stolen (audit OPS-08). Research:
  `docs/research/2026-09-11-one-answer-to-is-this-process-alive.md`.
- **The compile-lock owner token travels in return values.** `maybe_compile`
  no longer keeps the claimant's token in a module global; the claim returns
  it and the release takes it (audit OPS-22).
- **`compile_memory` imports its lock module once.** Two per-call
  `sys.path.insert` grew the import path on every compile-lock check
  (audit M10).
- **A skipped night and a failed adoption say why.** The doctor's stale
  nightly message names the last pass's skip reason when the skip is newer
  than the last run; `install.sh` keeps the V3 adoption's stderr in
  `logs/install-adoption.err.log` and quotes its tail on failure instead of
  telling the user to rerun the command blind (audit OPS-23, OPS-20).
  Research: `docs/research/2026-09-11-a-skipped-night-and-a-failed-adoption-say-why.md`.
- **Nine small findings closed at once.** A bad `MEMORY_LLM_TIMEOUT_S` is
  refused by name; the compile budget is written once; the session record
  renders its transcript once; the self-alias of `GenerationSealChanged` is
  gone; a generation's descriptors close with `OSError` handling and one
  named error; the flush docstring says "bounded"; `--status` opens the
  legacy index read-only; the optional-stage worker and the detached
  provider no longer swallow interrupts; `merge_claude_settings` is under
  the complexity gate (audit L5–L12). Research:
  `docs/research/2026-09-11-nine-small-findings-closed-at-once.md`.
- **A dropped best-effort write is counted, and one bound is declared once.**
  Six best-effort writes (capture-operation state, feedback capture, the
  three MCP telemetry emitters) now count their failure in the
  capture-failure trail instead of `pass`; the nightly hands the repository
  refresh its own budget (`--budget-seconds`) and waits a margin longer, so
  the child's graceful deferral runs before the parent's kill (audit OPS-21,
  OPS-10). Research:
  `docs/research/2026-09-10-a-dropped-best-effort-write-is-counted-and-one-bound-is-declared-once.md`.
- **A silent fallback names its cause.** An encode that raised, a corrupt
  `vectors.npy` or catalog, an unusable graph all looked like "no vectors
  yet"; the eight sites now record `Class: redacted message` by kind
  (`search_memory.degradation_reasons()`), say it once on stderr, and the
  health resource carries `retrieval_degradations` (audit M5, M6). Research:
  `docs/research/2026-09-10-a-silent-fallback-names-its-cause.md`.
- **A page that cannot be flushed is named.** `access_tracking.py` was the
  one module under `scripts/` the complexity gate refused (four functions
  up to CCN 15, six bare excepts); a page whose frontmatter export failed
  was skipped in silence while the cursor moved on. The module is a pipeline
  of small steps with the same behaviour, and a failed page is reported with
  its reason (`last_flush_failures()`, stderr, `--flush` output) while the
  bounded scan still advances (audit H2). Research:
  `docs/research/2026-09-10-a-page-that-cannot-be-flushed-is-named.md`.
- **A failing in-process step says why, not only its class.** The nightly's
  health report, three doctor repair paths and the self-update reported a
  failure as `RuntimeError` alone; they now report
  `Class: redacted message` through one helper, and the tests assert the
  message (audit OPS-09, the rest of OPS-17). Research:
  `docs/research/2026-09-10-a-failing-step-says-why-not-only-its-class.md`.
- **A stale lock is moved aside and checked before it is removed.** The
  three legacy lock stealers (`run/compile.pid`, `run/state.json.lock`, the
  legacy `run/maintenance.lock`) decided "stale" from one read and then
  unlinked whatever was at the path, so two stealers could remove each
  other's fresh lock and both proceed. One helper now renames the lock aside
  (one winner), deletes it only while it still holds the judged bytes, and
  puts a fresh owner's lock back; the legacy marker is judged by its process,
  not by age (audit OPS-07). Research:
  `docs/research/2026-09-10-a-stale-lock-is-moved-aside-and-checked-before-it-is-removed.md`.
- **A maintenance step that times out takes its children with it.** The
  nightly and weekly runner killed only the direct child at the bound; a
  step's own workers (queue processors, repository refresh, model download)
  kept writing while the pass moved on. Steps now run through the tree
  runner the sync already used, and the log says whether the tree ended
  (audit OPS-06). Research:
  `docs/research/2026-09-10-a-step-that-times-out-takes-its-children-with-it.md`.
- **A failed MCP warm-up is in the health answer.** The retrieval warm-up
  swallowed every failure, including `KeyboardInterrupt` and `MemoryError`,
  and the first answers of a session silently fell back to the lexical leg.
  It now records its state (`not_started`, `running`, `warm` with seconds,
  `failed` with stage and error class), prints one line to the server log,
  and `llm-wiki://health` carries it with a warning (audit OPS-13). Research:
  `docs/research/2026-09-10-a-failed-warm-up-is-in-the-health-answer.md`.
- **Docstrings and docs say what the code does.** The nightly's header
  names its scheduler and its steps, `maybe_compile` and its tests describe
  the lock they have, the search module quotes measured costs instead of
  "<10 ms", and the last LanceDB and `SETUP-COGNEE.md` mentions are gone
  (audit OPS-16, L1, L2, L3).
- **The status names what the search reads.** `search_memory.py --status`
  reports the active generation (id, extractor, vector state, model) before
  the legacy index, `--rebuild` says it rebuilds the legacy index only, and
  the user guide documents the on-by-default vectors with `--no-semantic`
  and points an empty search at `doctor.py` (audit M2, M3). Research:
  `docs/research/2026-09-10-the-status-names-what-the-search-reads.md`.
- **One page ceiling for every reader of `knowledge/`.** The guardrails
  snapshot, the compile after-image, the index rebuild and access telemetry
  refused a page at 4 MiB, backlink repair at 512 KiB, while the journal,
  claim tree, corpus and search accepted 8 MiB — the family of the journal
  incident (audit M4). `bounded_io.MAX_KNOWLEDGE_PAGE_BYTES` is now the one
  declaration and eleven readers alias it; a test holds them equal. Research:
  `docs/research/2026-09-10-one-page-ceiling-for-every-reader-of-knowledge.md`.
- **The Windows installer says "owned" only after the transaction committed.**
  A failed install-ownership transaction no longer prints
  `Claude settings owned by the install transaction` or lists Claude Code as
  active automatic (audit OPS-05). Research:
  `docs/research/2026-09-10-the-installer-says-owned-only-after-the-transaction-committed.md`.
- **A lost session record is written down.** `write_session_evidence`
  still never raises, and a refused write now lands in the capture-failure
  trail and counters with its reason and session (audit H5). Research:
  `docs/research/2026-09-10-a-lost-session-record-is-written-down.md`.
- **Every hang bound in the tests comes from one place.** 330 literal
  bounds on `join`, `result`, `get` and `wait` in 26 files under `tests/` now
  name `SHORT_TIMEOUT` or `LONG_TIMEOUT` from `tests/slow_machine.py`; a
  bound the test expects to elapse, or a pause whose result the test
  discards, stays literal by design, and
  `test_no_test_carries_a_literal_hang_bound` keeps the count at zero
  (audit OPS-04). Research:
  `docs/research/2026-09-10-every-hang-bound-in-the-tests-comes-from-one-place.md`.
- **A compile lock lives as long as its process, not thirty minutes.** The
  legacy lock declared a live compile stale after 30 minutes, so the nightly
  ran lint, backlink repair and the index rebuild on top of it and the next
  trigger reported a race that never happened (audit OPS-01, OPS-11, H4).
  One predicate now answers absent, stale or live for every reader; a lost
  claim names the lock that won; `maybe_compile` decides its exit code from
  the outcome and not from the reason text; `compile_memory` refuses to run
  when the lock cannot be taken or read and says why. Research:
  `docs/research/2026-09-10-a-lock-lives-as-long-as-its-process-not-thirty-minutes.md`.
- **Warm structural answers on every supported Python, and the tests that
  prove them pass on every runner.** PR30 run 34500804888 was red in 13 of 50
  jobs. The reader cache demanded a serialized sqlite3 build (`threadsafety
  == 3`), which Python 3.10 never reports, so on 3.10 every answer reopened
  its generation and the MCP answer lost its `freshness` block; a lease
  already hands the reader to one thread at a time, which is all SQLite's
  multi-thread mode asks, so only a single-thread build is refused now.
  Three Windows-only test defects were fixed at the class: the idle-reader
  test drives the cache's clock instead of trusting a 15.6 ms monotonic step,
  a test removes a Git checkout through `tests/filesystem.py::remove_tree`
  (read-only objects), and the timeout-scale test no longer starts a child
  Python without `SYSTEMROOT`. Research:
  `docs/research/2026-09-10-a-cached-reader-needs-one-thread-at-a-time-not-a-serialized-build.md`.
- **The compile commits again; a project journal is no longer a claim page.**
  No compile had committed since 2026-09-07: the claim index read
  `knowledge/projects/*/journal.md` and refused the 4.2 MB `another-project`
  journal at a 4 MiB cap the journal itself did not have, so every draft
  was recorded as a validation error. The claim tree, the claim index and
  lint now share one file set, `context.md` and `state.md`: the journal is
  the event log the state is projected from, carries no claim ledger, and
  is not scanned. Every reader that can still meet a project page accepts
  the journal's own 8 MiB ceiling, and one test holds the bounds together.
- **Tests wait as long as the slowest supported machine needs.** Two
  Windows jobs went red on waits sized for a fast disk. Every wait in the
  tests now comes from `tests/slow_machine.py` as CPython's
  `SHORT_TIMEOUT`/`LONG_TIMEOUT`, scaled by `LLM_WIKI_TEST_TIMEOUT_SCALE`,
  and a generation build reports the seconds each phase cost in its
  outcome, built or deferred, which the nightly log prints.
- **A generation built by an older extractor is served, not refused.** The
  FTS content check re-derived every chunk with the current chunker and
  compared; after the 2026-09-08 chunker change every generation built
  before it was "semantically invalid", retrieval fell to the legacy BM25
  index (`generation_unavailable`, lexical-only, empty for Russian) and the
  six failing validations cost 40 s on every search of every process. The
  content check now applies only to generations this extractor built; an
  older one is validated structurally and served until the nightly rebuilds
  it, and a refusal is remembered under the same hashed identity as a success.

- Two users' first-day findings (issues #17–#29, PR #27), each with its test
  and, where the design changed, a dated note under `docs/research/`:
  a fresh install adopts Reliability V3 so capture works at once (#17); the
  nightly follows a slow compile instead of failing it and prunes superseded
  generations (#21, #29.4); the provider and model chosen at install reach
  the hooks and the scheduler units (#22); a deleted project's journal can
  be rebuilt from its checkpoints (#20); a compile names every claim it drops
  and widens a partial quote to its line instead of dropping it (#28);
  `doctor` reads the vector state without the budget (#29.1); an empty
  `recall` names the generation it searched and the envelope repeats the
  trace's partial and fallback state (#26.1); a compile says `published` or
  `quarantined` per batch and records `last_compile_outcome` (#26.2); a
  capture write deferred by a writer race is counted apart from a lost one
  (#26.3); the claim check names each code's cause, the pages and the repair
  (#29.5); session start reads the nightly's health report instead of saying
  "not measured" every morning (#23.5); a busy maintenance fence names its
  holder (#29.6); Codex hooks are discovered natively and the internal
  classifier no longer captures itself (PR #27).
- On a vault that adopted Reliability V3 before its queue was ever used, the
  doctor reported the v2 queue migration as pending for ever and `--repair`
  aborted on the v2 tombstone before repairing anything; adoption retires
  that migration and both now say so. A capture write refused by a writer
  race is classified by the exception's type and code, never by its text;
  the state-lock timeout and the bound-elsewhere refusal are typed for it.
- The Codex hook probe waits for the peer it killed to be reaped within its
  own cleanup budget instead of the already-expired probe deadline; on
  Windows it had returned while the peer was still exiting.
- `uninstall` and `rollback` still take back a Cursor or Antigravity hook
  fragment written by an install from before the retirement. Deleting the
  writing code outright would have made the manifest name a resource the code
  no longer supplies, and the control plane fails closed on that
  (`install_resource_request_mismatch`) — leaving the fragment in
  `~/.cursor/hooks.json` or `~/.gemini/config/hooks.json` pointing at a vault
  nothing maintains, with no way to remove it. The projection readers and
  writers for both formats are therefore kept as a removal-only path;
  `write_owned` on them refuses with `install_resource_retired`.

- The grounded answer now captures the same corpus retrieval searched. It read
  the vault without the approved code roots, so every candidate under `docs/`
  or `scripts/` fell out of the snapshot and the answer refused itself for lack
  of evidence on questions search had just answered.
- A grounded answer sheds its weakest retrieved span instead of refusing when
  the selection overflows the context budget. Long pages made the mandatory set
  exceed the budget outright.
- The grounded-answer deadline is 120 s, measured: one provider round trip for
  a 4 KiB evidence prompt takes 32.5 s and the previous 30 s bound could not
  complete a single real call.
- A cited span must agree with the claim's own figures. Entailment is still not
  verified and not claimed; what is refused is the citation from the right page
  and the wrong sentence — "expires after 30 seconds" supported by the line
  about refreshing every 10.
- The installer-timeout test no longer depends on the kernel scheduling the
  child inside the installer's half-second escalation window, and its failure
  now reports which markers the child left behind.

## [4.0.0] — 2026-08-25

First release of the v4 line. The version has carried 4.0.0 since the platform
work landed, but nothing was ever tagged; this release publishes the audited
state, including everything found and fixed during the audit week.

### Since the audit (2026-08-18 → 2026-08-25)

#### Added

- Session records age out of the active tree after ninety days, moving to
  `knowledge/raw/sessions/archive/<YYYY-MM>/` — same bytes, one directory
  deeper, never deleted, in the weekly pass.
- `archive_stale.py --restore <slug>` brings an archived page back: archiving
  is dormancy, not deletion, so reactivation is part of the contract.
- A second benchmark stand measures *applying* memory rather than recalling it:
  each case counts only when the exact token needed to act reaches the reader.
  Live vault: 0.857 against grep's 0.429.
- The redactor knows the prefixed secret shapes a 2026 scanner catches —
  `gho_`/`ghu_`/`ghs_`/`ghr_`, `github_pat_`, `sk_live_`/`rk_test_`, `xapp-`,
  `npm_`, `hf_`, `pypi-`, `GOCSPX-`.

#### Fixed

- `doctor` answers every check at the default budget again. The corpus-wide
  generation check ran first and spent the whole budget; it now runs last. And
  `run/state.json` had outgrown the 256 KiB bound its readers use, which
  silently blinded the scheduler and capture checks — writers now keep it under
  three quarters of that.
- A cold CLI query costs 13 s instead of 47: the cross-encoder is loaded only
  when asked for with `--rerank`, since a one-shot call pays that load every
  time while the resident MCP server pays it once.
- A compiled page's citation resolves again. A day longer than 16 KiB is
  compiled in parts, and the page cites the part it was written from, but the
  resolver only ever compared the whole file — so every page compiled from a
  split day failed its own evidence check. The reader now accepts an
  entry-aligned slice that starts where a part starts and still hashes to what
  the page recorded; an edit inside the cited region still fails.

- The nightly pass adds the backlinks the vault owes instead of leaving them as
  findings for a person to clear. Compile writes pages that link outward and
  cannot edit the pages they name; the repair appends the missing link through
  the same transaction machinery as every other automatic writer, and never
  names a private page inside a published one. A superseded or archived page is
  history and is no longer asked to link forward.

- `doctor` calls a vault with registered generations and no active one
  degraded. An empty pointer used to read as "not activated yet", which is true
  for a young vault and false for one whose main read path just went away.

- A lost maintenance fence names itself: which check saw it, and what the owner
  row held at that moment. Three checks used to raise the same bare string, so a
  deferred nightly rebuild could only say that the fence was gone.

- Completing a blackboard task and resolving a blackboard conflict survive a
  retry. Both publish under a stable operation id but stamped the record with
  the moment of the write, so a caller retrying after a transient failure was
  refused with `operation_id is already bound to a different request` instead
  of finding its own earlier publication already there.

- `doctor` reports a locked FTS index as busy instead of as an exhausted time
  budget. The two verdicts were decided by the clock, so on a slow machine the
  wait for the lock consumed the budget and the more actionable answer was
  lost.

- A new structural test fails when any module we own binds the same top-level
  name twice. A second definition silently replaces the first, so an edit to
  the wrong copy changes nothing and an edit to the right one changes
  everything.

- Read-only opens on the generation catalog, retrieval telemetry, the MCP queue
  view, and `doctor` wait out a lock instead of failing on one. A commit on a
  rollback-journal database locks readers out for milliseconds, and a
  zero-length wait turned that normal moment into `database is locked` — a
  flaky test failure and a false `doctor` finding about an unreadable database.
  `doctor` spends at most half of its remaining budget waiting, so a genuinely
  busy database is still reported as busy rather than as an exhausted budget.

- A generation-catalog writer without a caller deadline waits out contention
  instead of surfacing `database is locked`. Two compare-and-swaps on a loaded
  machine can hold the write lock for longer than the previous five-second
  window, which turned a decided race into a raw SQLite error.

- Windows cleanup verification reads the process tree from the kernel snapshot.
  It used to ask `wmic`, which current Windows no longer ships, and fall back to
  a PowerShell CIM enumeration that does not finish inside its budget on a
  loaded machine; without an answer the worker reported
  `process_cleanup_failed` for a tree it had already terminated. Both tools are
  removed rather than kept as fallbacks.

- A grounded answer now fails when a cited span shares no content with the claim
  it is offered for, which is the case where a truthful citation about a
  different subject passed every other gate. It is a necessary condition, not
  entailment: support itself is still unverified and is not claimed.

- A Markdown write no longer fails when the writer-gate heartbeat is starved by a
  busy database. Ownership loss is now decided by the projection row, which a
  reclaim deletes, and the error names its cause. A queue worker likewise waits
  out a busy database instead of ending its run with `database is locked`, and a
  concurrent Pyright installer waits for a lock it does not own instead of
  deleting it.
- `doctor` reports lost captures: a new `capture` check names the per-kind counts
  from `state.json` and points at `logs/capture-failures.jsonl`. The two capture
  wrappers now record that loss when they are invoked directly and their detached
  flush cannot start. `doctor.py` also accepts `--time-budget` so a slow machine
  can finish its checks instead of reporting an exhausted budget.
- The installed OpenCode plugin carries the vault root it was installed from, so
  an OpenCode started from a desktop launcher captures instead of silently
  disabling itself. The environment still wins when it is set.
- The structural lint no longer counts `knowledge/daily/README.md` as an
  uncompiled daily log, and `docs/EXPORTING.md` no longer presents an export as a
  way to migrate a vault: an export carries committed files only.

- Made the v4 reliability platform fail closed: Windows path/handle identity no
  longer truncates IDs or compares incompatible creation/change times; SQLite
  readers close explicitly; MCP timeout tests drain late workers; metadata
  publication checks POSIX directory sync and Win32 flush/write-through moves;
  and locked production, optional, and development profiles directly own their
  required dependencies. Installers now run a bounded production MCP smoke
  instead of requiring pytest.
- Added a nonmutating installed-vault Reliability V3 inspector and redacted CLI,
  deterministic fresh/upgrade cutover validation, retained byte-identical v2 evidence,
  tombstone and crash-resume checks, and read-only SQLite validation. Public apply/adoption
  performs the offline v3 cutover on a fresh or quiescent vault (the earlier
  `reliability_v3_runtime_activation_incomplete` refusal was lifted once the v3 queue
  writers and canonical ownership landed); no runtime state is deleted. Since
  2026-09-10 the installer runs it, because session capture is refused until it has
  (issue #17).
- Bounded Claude and Codex integration configuration backups to 10 files, 90 days,
  and 100 MiB per integration. Changed merges now verify a byte-exact sibling
  preimage before atomic publication, preserve the newest restore point, prune only
  exact LLM-Wiki-owned backup names after verification, and create no backup for a
  no-op merge.

### Changed

- The Pyright qualification gate `warm_overhead_p95_ms` is 30 ms instead of 20.
  Three consecutive four-vCPU hosted runs measured 22.80, 22.08, and 22.16 ms,
  a spread under one millisecond, so the number described the machine rather
  than a regression: the navigation facade pays for its freshness guarantee
  with an extra workspace-revision walk. The gate still fails closed and now
  names the slowest supported machine class. See
  `knowledge/notes/warm-navigation-overhead-threshold-decision.md`.

### Added

- `benchmark/run_flush_classification.py` measures what session classification
  keeps and what it drops: tier accuracy, durable-content recall, and the
  false-promotion rate against a labelled corpus, scored through the product's
  own classification prompt. The shipped corpus is nine public synthetic cases;
  a real answer needs an installed vault's own sessions via `--corpus`.

- `memory_queue.py restore --export <path>` brings the work in one verified purge
  export back as new ready tasks, refusing the whole export if the manifest, the
  records digest, any result digest, or the id list fails to verify.
- `memory_queue.py purge --include-dead` retires attempts-exhausted tasks through
  that same export-first path. Without the flag they are retained, because a dead
  task records work the system promised and never did.

- Read-only Python code navigation through pinned Pyright 1.1.411: bounded stdlib
  LSP protocol, generation-aware process lifecycle ownership, workspace-revision
  freshness retry, normalized navigation facade, deterministic renderer, precise
  `get_architecture` modes, Pyright doctor diagnostics, seven-day LSP runtime
  retention, and a deterministic 100 KLOC qualification corpus with closed gates.
  Navigation adds no MCP tool, graph, semantic cache, daemon, or runtime root;
  Pyright is installed by one explicit operator command and never downloads
  during a query. Market superiority remains unclaimed.

- Documented the integrated Tasks 1-29 unified evidence contracts: Markdown/Git/
  project-journal authority, immutable derived generations, register-then-CAS
  activation, seal validation, prior-generation recovery, cache deletion, truthful
  retrieval fallback, token-source labels, and verified grounded-answer citations.
- Added a non-destructive legacy FTS/NumPy/Lance migration and rollback guide. Legacy
  caches remain readable and must be retained until installed-vault migration evidence
  proves removal safe; rollback never removes knowledge, Git history, journals, or
  operational `run/` state.
- Recorded the exact unchanged 12-tool MCP surface and current behavior. `recall`
  exposes retrieval trace fields; evidence reads fail closed; contradiction checks are
  structured; code/dead-code/architecture/impact paths are store-first with explicit
  live fallback; `doctor` retains nine closed operator actions. Planned token-budgeted
  MCP context, expanded architecture modes, and per-component envelope freshness are
  labelled **evidence pending** rather than shipped.
- Recorded truthful release-evidence limits: the model matrix currently selects no
  new embedding or reranker, deterministic comparative smoke does not execute
  Graphify, and no model-superiority, Graphify-parity, quality, or token-ratio claim is
  made. Real paired evidence remains pending.
- Documented Stage 2 reliable memory operations: Markdown-authoritative recoverable
  transactions and undo, fenced project journals, rollback-journal SQLite queue
  migration/work/redrive/export-first purge, content-addressed compile receipts,
  90-day-hot immutable BagIt archives, logical evidence, and quarantined claims.
- Added operator commands and deletion safety for source failures, retained
  transactions/tasks/results, the 30-day undo window, and live owners.
- Recorded the local-filesystem requirement, current `synchronous=FULL`/no-WAL
  policy, bounded defaults and CLI overrides, cooperating-writer CAS boundary, and
  explicit non-goals. The full regression suite remains the release gate.
- Added canonical `repository-scope/v1` binding for repositories, linked worktrees,
  checkout roots, Git common directories, and captured commits. Generation readers
  reject the wrong repository/worktree scope instead of returning cross-checkout
  evidence.
- Implemented complete `corpus-generation/v2` publication. Canonical
  `source-manifest.json` binds exact source membership and hashes;
  `incremental-manifest.json` records source ownership, invalidation, and reuse.
  Evidence Graph v2 and FTS are required artifacts built from the same immutable
  snapshot, and no partial, stale, raced, or changed candidate can become active.
  Catalog selection, CAS activation, same-scope fallback, orphan recovery, and
  bounded nightly/doctor maintenance preserve the previous valid generation.
- Added deterministic workspace-level code extraction and incremental invalidation
  across supported source languages, including cross-file symbols, occurrences,
  calls, imports, routes, and dependencies where the extractor can prove them.
- Extended the existing MCP `recall` tool with opt-in grounded QA and verified
  citations without changing the 12-tool surface. MCP context, grounded QA,
  SessionStart, project handoff, and lifecycle injection now route final packing
  through the shared Context Compiler and one bounded token budget.
- Hardened deadlines, cancellation, bounded reads, repository resolution, generation
  publication, retrieval errors, and cleanup paths. Installers now preserve the real
  pytest exit status and clean up spawned test processes/process trees on interruption.
  Legacy FTS, NumPy, and Lance caches remain readable fallback state; there is no
  daemon, automatic migration, or automatic legacy-cache removal.
- Centralized LSP lifecycle transitions in one coordinator with serialized restart,
  heartbeat, and cleanup ownership. Terminal identity and deadlines are immutable,
  lease refresh remains independent during cleanup, and Windows process and file handles
  remain owned until exact PID exit and successful handle closure are proven.
- Added the repository-scoped Pyright provider-session core with exact initialization,
  capability/readiness evidence, bounded document opening, semantic locations, hover,
  call hierarchy, push diagnostics, progress handling, restart replay, document
  synchronization, session-manager capacity, the normalized navigation facade,
  deterministic rendering, and precise MCP routing.

### Fixed

- Bound untrusted LSP runtime JSON nesting before decoding, independently of the
  process-wide Python recursion limit changed by optional dependencies.
- Reuse one bounded revision-keyed workspace inventory proof between immediate
  pre/post navigation checks. Directory, file, Git-index, semantics, cancellation,
  and race fences remain fail-closed, while unchanged 100 KLOC queries avoid a
  duplicate tree enumeration.
- Harden Pyright session ownership, generation-scoped `didOpen`, aggregate source and
  diagnostic bounds, interrupt cleanup, and lifecycle-owned readiness promotion. On
  POSIX, execute digest-verified server bytes from a held read-only descriptor through
  a bounded Node loader that preserves the original CommonJS filename and arguments.
- Requalify the pinned Pyright server before every initial or replacement LSP
  generation, hold its launch guard through bootstrap, and reject changed
  executables before a candidate generation can become active.
- Cap Pyright session startup at 60 seconds or the shorter caller deadline,
  apply that absolute deadline to preflight and cleanup, and preserve the
  entry-time budget for an autonomous configured restart.
- Require PATH-resolved Node executables to be local regular files outside reparse
  points and network filesystems, probe versions without reader threads through
  platform-qualified whole-tree cleanup before reading inherited pipes, and
  recursively freeze attested Pyright configuration values behind an explicit
  thaw-copy boundary.
- Prevent Pyright source-category laundering, block ancestor config search when a
  bounded root `pyproject.toml` has no object `[tool.pyright]`, fingerprint ordered
  per-file `pyrightconfig.json`/`pyproject.toml` inheritance, retain empty managed
  version roots as broken-install evidence, and reject recursive or unsupported
  config and receipt domains before canonicalization.
- Fail closed on LSP diagnostic text over the 256 KiB strict UTF-8 byte ceiling
  or containing lone surrogates before semantic scanning. Use O(1) code-point
  pre-rejection plus bounded allocation-free byte sizing, normalize and redact
  complete accepted values before the 1024-character output cap, scan canceled
  POSIX path components without overlapping retries, accept only local extended
  Windows drive aliases, and treat encoded backslashes as POSIX filename
  characters while continuing to reject encoded path separators.
- Normalize bounded POSIX LSP log paths and local file URIs lexically, with
  strict one-pass UTF-8 decoding, local-authority and sibling boundaries, and
  matching Windows spaced roots after dot-segment normalization.
- Complete the LSP security scanner with restartable nested terminal states,
  safe-space control boundaries, immediate component-prefix Windows root matching,
  canonical spaced-root aliases, and bounded one-character failed-candidate recovery.
- Finish LSP log token parsing by consuming ISO escape intermediates and
  recognizing Windows line/column, quote, and terminal-punctuation suffixes
  with one bounded linear component scan.
- Finish LSP alias containment by consuming two-byte ESC controls, matching
  mixed long/8.3 Windows path components through a bounded local API, keeping
  percent triplets literal in native paths, and scanning through quoted names.
- Reject Windows components that exact handle-relative opens reveal despite an
  omitted directory entry, scan URL authorities without treating RFC sub-delimiters
  as token boundaries, strip complete terminal control strings before credential
  matching, and redact local-drive path tokens through bounded lexical normalization.
- Normalize control and Unicode-format aliases before LSP credential redaction,
  scan every punctuation-separated URL authority, and match Windows path roots by
  normalized components including dot segments, trailing dots/spaces, and encoded
  Unicode case aliases without matching sibling prefixes.
- Publish explicit LSP recovery transitions with one sticky worker handoff. Recovery
  retries incomplete restart-failure cleanup with fresh bounded deadlines and event
  backoff, while the startup registry retains only coordinators with live ownership.
  Windows Job cleanup also gives captured process identities a bounded settle window
  after stable active-zero accounting without making reusable PIDs authoritative.
- Preserve exact startup and restart failure intent before deadline-bounded lifecycle
  transitions. Cleanup retries can no longer commit success, delete scratch, or drop
  startup recovery before immutable failure evidence reaches `STOPPED_FAILURE`.
- Complete second-fatal LSP cleanup autonomously from the recovery owner. The
  recovery thread quiesces its own tracked role, joins the heartbeat, releases all
  generation owners and handles, removes the live lease, and reaches
  `STOPPED_FAILURE` without a later caller `close()`.
- Retry Windows LSP lease replacement only for access-denied, sharing-violation,
  and lock-violation errors. Stop-aware waits are bounded by both the operation
  deadline and the previously published lease expiry, so a reader cannot cause a
  refresh to outlive the lease silently.
- Retain incomplete LSP startup ownership in a bounded module registry before the
  first owned mutation. Normal interpreter exit retries unadopted owners under one
  deadline; higher-level sessions can adopt returned cleanup ownership without
  consuming registry capacity and use the same bounded retry path.
- Bound retained LSP cleanup diagnostics to one sanitized current record per step.
  Stored records contain no raw exception, message, path, traceback, or frame graph,
  and successful retries clear stale records.
- Use current `ActiveProcesses`, not lifetime Job totals or a racing PID-list
  snapshot, for Windows LSP process bounds and cleanup completion. PID capture is
  now best effort, and an earlier snapshot error is discarded after direct-process
  reap plus stable active zero. An unavailable, over-bound, or persistently nonzero
  active count remains a retryable cleanup failure.
- Close each CPython 3.10 Windows LSP process handle after direct-process reap and
  all protocol, stderr, and exit-monitor joins. Handle-close failures now retain
  the generation, Job, lease, and `CLEANUP_PENDING` state for idempotent retry;
  retained `Popen` objects keep their cached return code and `poll()` behavior.
- Linearize each LSP generation's expected and observed exits under one lock.
  An unexpected process death recorded before shutdown now remains a terminal
  failure, while shutdown marked before death remains successful; duplicate
  process and protocol callbacks still select exactly one failure intent.
- Publish LSP failure evidence only once after exact durable validation. Public
  `FAILED` state and waiter notification now follow that evidence barrier; failed
  evidence remains retryable with live ownership and a `DEGRADED` public state.
- Treat false Windows directory flushes and failed POSIX directory `fsync` calls as
  incomplete LSP evidence publication. Retries repeat the barrier before terminal
  `FAILED` state, lease removal, or owner release.
- Qualify LSP containment by platform: Windows Job Objects own assigned trees;
  POSIX process groups cover pinned Pyright descendants only while they remain
  in-group, and hostile `setsid()` escape is explicitly unsupported.
- Retain failed LSP evidence and lease temporary-file cleanup as bounded owned
  state. On Windows, the unique name is reserved before creation so post-create
  identity validation remains recoverable. Cleanup retries the exact handle-relative
  name before another publish or terminal finalization, so no hidden temp can release
  the lease or owner.

### v4.0 foundation

- **LanceDB embedded vector backend** (`scripts/lance_store.py`) — HNSW vector
  search, embedded (no daemon). Replaces PostgreSQL. `--extra hybrid` to enable.
- **Memory-mapped vector cache** — `vectors.npy` replaces `vectors.json`. Binary
  numpy format loads instantly via mmap instead of JSON parsing.
- **bge-small-en-v1.5 embedding** — pinned legacy optional-vector compatibility model.
- **Cross-encoder reranker** (`scripts/reranker.py`) — bge-reranker ONNX INT8,
  re-ranks top-20 results. `--extra reranker`.
- **Access tracking + Ebbinghaus forgetting** (`scripts/access_tracking.py`) —
  retrieval analytics, decay scoring, hybrid archive (time + access).
- **Multi-pass compile** — draft → critique pass, drops low-quality operations.
- **Typed edges** — `refines` (page stays alive) alongside `superseded_by`.
- **A-MEM reflection** (`scripts/reflection.py`) — weekly page consolidation.
- **MCP-first agent interface** (`scripts/mcp_server.py`) — 12 task-shaped tools
  including `doctor`, uniform response envelopes, and health/context resources.
- **Thin lifecycle boundary** (`scripts/integration_adapter.py`) — native hooks,
  plugins, and wrappers normalize host events that MCP cannot observe.
- **Automatic health** — SessionStart injects only degraded/error findings;
  healthy checks remain quiet and repairs require explicit opt-in.
- **Fenced recovery hardening** — fail-closed runtime deletion checks, bounded
  no-follow SessionStart recovery, background maintenance heartbeats, shared
  deadlines, safe operational SQLite opens, and protocol-visible MCP failures.
- **Local retrieval architecture** — SQLite FTS5 BM25 with optional local
  vectors/LanceDB, graph neighbors, and reranking. Removed active QMD wiring.
- **Obsidian viewer-only integration** — removed the bundled ingestion template;
  Obsidian remains an optional Markdown viewer, not a required frontend.
- **Code graph** (`scripts/code_graph.py`) — lazy tree-sitter parsing and
  materialized `.scm` queries for 12 languages, scoped import captures,
  nine-language regex fallback, evidence-aware Python import/call resolution,
  and rename-aware git co-change refinement.
- **LINK Layer** (`scripts/impact_analysis.py`) — git diff → stale wiki pages.
- **L0/L1/L2 tiered loading** (`scripts/build_tiers.py`) — progressive disclosure.
- **Scheduled jobs integration** — nightly: access flush + code graph.
  Weekly: A-MEM reflection + L1 tier generation.
- **SessionStart impact advisory** — stale wiki pages from code changes.
- **MCP config in install scripts** — Claude Code + OpenCode auto-config.
- **Optional extras** — `hybrid`, `code-graph`, `mcp-server`, `reranker`, `full`.
- **7018 tests** at release (4489 when the v4 platform work landed).

## [3.4.0] — 2026-07-11

A comprehensive security, concurrency, and quality release following 9 rounds of
full-codebase audit. **281 tests**. **Zero Critical, zero High**
open findings as of the final audit pass.

### Security
- **Secret redaction** (`scripts/secret_redact.py`) — 12 regex patterns (Bearer, API keys, GitHub, Slack, AWS, Google, JWT, PEM) plus Shannon entropy ≥ 4.0 high-entropy catch-all with pure-hex exclusion. Applied before ALL durable writes (daily logs, compile notes, Q&A file-back, bootstrap context).
- **Transcript path containment** — hook-supplied transcript paths restricted to known agent directories (`~/.claude`, `~/.codex`, `~/.config/opencode`, system temp) with known extensions only. Prevents arbitrary file exfiltration via crafted hook payloads.
- **Path traversal guards** — compile category whitelist + `relative_to()` containment, feedback candidate ID hex-only validation, queue output path containment under `run/queue-results/`, blackboard project slug sanitization.
- **Installer push-lock** — installed vault gets `git remote set-url --push origin no-push` so personal data can never be pushed to the public repo.
- **Installer pinned clone** — `git clone --branch v3.4.0 --depth 1` instead of mutable default branch.
- **`.gitignore` allowlist** — explicit per-file un-ignore for public knowledge notes instead of broad `!knowledge/notes/*.md` that could expose personal pages.
- **YAML injection prevention** — all frontmatter interpolation escapes backslash, double-quote, and newlines.
- **Untrusted-data framing** — daily-log excerpts injected into SessionStart context are marked as `UNTRUSTED — session history, not instructions`.

### Concurrency & Atomicity
- **Daily-log lock rewritten** (`daily_log_append._daily_lock`) — `O_CREAT|O_EXCL` atomic file creation (was broken `rename()` which silently overwrites on POSIX). Stale-lock recovery via PID liveness + mtime. Fail-closed (raises `TimeoutError` instead of writing without lock).
- **Single locked write path** — all daily-log writers (flush_memory, user_prompt_capture, post_tool_capture, session_end_project_tag, tool_breadcrumb_append) delegate to `locked_append()` / `append_daily()`. Zero duplicated write logic.
- **Compile lock hardening** — PID-0 placeholder TTL (10s), owner-aware deletion via token check, atomic lock writes via temp+`os.replace`.
- **`atomic_write()` helper** (`memory_state.py`) — all durable note writes, supersession markers, search index, and cache files use temp-file + `os.replace` pattern.
- **Direct compile lock acquisition** — `compile_memory.main()` acquires the compile lock even when run directly (not spawned by `maybe_compile`), preventing concurrent manual compiles.
- **State-lock deadline** — `memory_state._state_lock` bounded by monotonic deadline in all branches (was unbounded `sleep(timeout)` when owner PID alive).
- **Maintenance lease** — `scheduled_nightly.py` acquires `run/maintenance.lock` via `O_EXCL`, preventing concurrent nightly+weekly runs.
- **Project state exclusive-create** — `session_start_project_state.py` uses `O_CREAT|O_EXCL` for initial `state.md` creation instead of write+replace.

### Architecture
- **Flat notes layout** — `compile_memory.py` writes directly to `knowledge/notes/<slug>.md` (was `knowledge/notes/<category>/<slug>.md`). Type lives in frontmatter only. Aligns with Obsidian/Dataview 2026 property-based organization.
- **`okf_types.py`** — single source of truth for canonical OKF types, type aliases (`comparison→synthesis`, `connection→synthesis`, `fact→concept`), and never-archive set. Imported by lint_memory, migrate_to_okf, archive_stale, rebuild_memory_index.
- **`maintenance_helpers.py`** — shared `run_step()` and `wait_for_compile_idle()` extracted from scheduled_nightly + scheduled_weekly (was byte-identical copies).
- **Deferred flush queue** — when no LLM backend is available, `flush_memory` enqueues a typed `"flush"` task. The drain processor classifies the result and applies it to the daily log, restoring the deferred-work contract.
- **Queue stale-lease recovery** — `memory_queue.recover_stale_leases()` re-queues `.processing` files older than 10 minutes.

### Search & Retrieval
- **Superseded/archived exclusion** — `_collect_pages()` in search_memory, `_build_link_graph()` in graph_neighbors, `existing_knowledge_snapshot()` in compile_memory, and `rebuild_memory_index.py` all skip pages with `status: superseded` or `status: archived`.
- **Atomic FTS index rebuild** — search index built in `index.sqlite.tmp`, then atomically replaced via `os.replace`.
- **Path manifest** — `.paths-manifest` sidecar detects deleted pages and triggers search rebuild.
- **JSON vector cache** — `vectors.json` (was `pickle`, now safe `json.loads`).

### Lint (14 checks)
- **14th check: `invalid_type_value`** — flags pages whose `type:` is not in `CANONICAL_TYPES` after alias normalization.
- **`TYPE_ALIASES` applied** — `lint_memory.check_invalid_type_value` normalizes alias types before validation.
- **`orphan_gaps` frontmatter scan** — scans by `type: gap` frontmatter instead of looking for a `gaps/` subdirectory.
- **Skills/rules scope** — `--scope all` now includes `skills/` and `rules/` for OKF frontmatter conformance.
- **Temporal validity** — non-date `valid_to` values (e.g. "forever") are skipped instead of causing false positives.

### Testing (281 tests, up from 226)
- **`test_security_invariants.py`** (47 tests) — property-based tests covering path traversal, YAML injection, secret redaction, status filtering, daily-lock exclusivity (5 concurrent threads), compile evidence enforcement, and legacy path detection.
- **`test_quality_guards.py`** expanded — docs-equality tests for lint count, runtime dir names, installer version tags, daily-writer lock usage, clean-clone import resolution, and untracked module detection.
- **Behavioral tests** — concurrent writers (no interleaving), compile evidence (empty→drop, valid→pass), snapshot exclusion, lock fail-closed behavior.

### Documentation
- **Full i18n sync** — README.md, README.ru.md, README.zh-CN.md synchronized: version, test count, lint count (14), benchmark methodology (exact title + keywords, not paraphrased), runtime dirs (`cache/cognee/`), installer tags.
- **`docs/STRUCTURE.md`** — canonical structure reference with env contracts, runtime zone, and forbidden directories.
- **Skills updated** — all 9 skills use flat `knowledge/notes/<slug>.md` paths.
- **`operating-model.md`** — flat layout paths, multi-agent intro.
- **Knowledge notes** — legacy `memory/` references updated to `knowledge/daily/` + `knowledge/notes/`. Trust fields (`confidence`, `source_authority`) added to workflow pages. Taxonomy aligned with canonical types.

### Installer
- **`uv sync --locked`** — verifies lockfile is up-to-date, fails if stale.
- **Env-var overwrite warnings** — both installers warn before clobbering existing `LLM_WIKI_ROOT` / `LLM_WIKI_STATE_ROOT`.
- **Bounded cron cleanup** — install.sh removes only lines between `# LLM-Wiki-cron-start` / `-end` markers (was broad `grep -v` that could delete unrelated jobs).
- **Windows `cache\cognee`** — install.ps1 now creates `cache\cognee` instead of bare `cognee\`.
- **`install-scheduled-tasks.ps1`** — removed `_SafeExit` function (was broken when dot-sourced); inline `return`/`exit` at call sites.

### CI
- **Gitleaks** — SHA-pinned GitHub Action, enforced by regression test.
- **Cross-platform matrix** — Ubuntu + Windows + macOS, Python 3.10 + 3.13.
- **`uv sync --locked --dev`** — lockfile enforced in CI.

## [3.3.3] — 2026-07-10

### Fixed
- **GitHub Actions Gitleaks** — upgraded to the Node 24 `v3.0.0` action pinned by immutable commit SHA. The previous action attempted to download the removed Gitleaks 8.24.3 Windows archive and failed before tests ran.

### Tests
- **281 tests** — added a regression guard that prevents CI from reverting to the unavailable Gitleaks action.

### Docs
- **Benchmark numbers refreshed** — `benchmark/report.md` now reports MRR 0.9667, p50 6ms (BM25-only mode, 60 queries). `docs/ARCHITECTURE.md` search layer updated to match; previously cited stale MRR 0.942 / p50 41ms figures.

## [3.3.2] — 2026-07-09

### Fixed
- **Three-zone layout hardening** — removed machine-local `D:\projects\` / `D:\tools-agent\` paths from public `AGENTS.md` + `CLAUDE.md` (they leaked the author's disk layout into a public repo)
- **maybe_compile PID race** — placeholder PID-0 lock is now treated as "alive", preventing a concurrent-spawn race during the detached-spawn window
- **agent_timeline breadcrumb regex** — now matches the real writer format (`tool | sid | slug | tool\` target`); tool-event attribution was silently dead
- **bootstrap_project secret redaction** — git remote URL is now passed through `secret_redact` before being written to `knowledge/projects/<slug>/bootstrap.md`
- **archive_stale path doubling** — archived pages no longer land at a doubled `knowledge/notes/` prefix
- **blackboard complete_task race** — switched from non-atomic read-modify-rewrite of `tasks.jsonl` to an append-only `completed.jsonl` (prevents silent completion loss when two agents finish different tasks in the same window)
- **compile_memory singularize** — replaced `rstrip('s')` (mangled entities→entitie, syntheses→synthese) with an explicit `CATEGORY_SINGULAR` map
- **loop_detector / agent_timeline unicode** — topic-signature regex now matches non-ASCII letters (was ASCII-only `[a-z]{5,}`)
- **cognee_sync SKIP_SUBTREES** — pointed `projects/` skip at `knowledge/projects` (was `knowledge/notes/projects`, a no-op)
- **export_vault forbidden paths** — verify list now blocks the three-zone forbidden dirs at vault root (`cache/`, `logs/`, `run/`, `state/`, `wiki/`, `memory/`, `outputs/`, `.ci-lint-state/`)
- **codex-memory-wrapper.ps1** — removed legacy `memory-state/` fallback, quoted the daily-log path, renamed shadowed `$args` automatic variable

### Changed
- **flush_memory → maybe_compile** — `maybe_trigger_compile` now delegates to `maybe_compile.spawn_compile_if_idle` (PID lock is the single concurrency gate; hooks/wrappers/schedulers no longer spawn `compile_memory.py` directly)
- **search_memory `--as-of` + source_authority** — temporal validity windows and typed-provenance weights (`user` > `web` > `ai-derived` > `inferred`) applied to ranking; `_vector_search` takes `as_of` as an explicit parameter (was a misleading "thread-local-ish" global)
- **feedback_capture stdin contract** — OpenCode plugin's `feedback_capture.py` JSON-on-stdin path now actually parses and captures (was list/promote only)
- **Claude Code hooks** — `UserPromptSubmit` + `PostToolUse` wired into `integrations/claude-code/settings.json`
- **install.ps1** — copies the OpenCode plugin (was mkdir-only); detects Antigravity; writes Codex wrapper via `$env:LLM_WIKI_ROOT` (survives vault relocation)
- **migrate_to_okf** — now imports `ROOT` from `memory_state` (honors `LLM_WIKI_ROOT` + worktree-aware git resolution)

### Removed
- `.ci-lint-state/` leaked runtime artifact at vault root; added explicit `.gitignore` defense
- Dead `WIKI_DIR` aliases (kept as backward-compat shims for tests), stale `import re`/`hashlib`, legacy `--scope memory|wiki` lint scopes collapsed to a single `knowledge/notes` tree

### Tests
- **217 tests** (+1 path-migration guard that scans scripts/ for forbidden legacy tokens; +deepened settings.json hooks test that verifies referenced scripts exist + timeouts; +27 structure invariants)
- `tests/README.md` refreshed: full coverage table, hermetic-isolation note, `LLM_WIKI_TEST_USE_EXTERNAL_STATE` opt-in documented

### Docs
- `AGENTS.md` / `CLAUDE.md` / `ARCHITECTURE.md` / `SETUP-COGNEE.md` / `EXPORTING.md` / `integrations/README.md` synced to the three-zone layout, current benchmark numbers (MRR 0.942, ~41ms p50), 13 lint checks, and portable path conventions
- Knowledge notes with live `raw/` / `inbox/` / `memory/` instructions repointed to `knowledge/raw/` / `knowledge/inbox/` / `knowledge/` (historical editorial mentions left verbatim per append-only contract)
- Broken Evidence citation `[22:41:34]` in `prospective-memory-page-drift.md` repointed to the real `[23:13:01]` fixture block

---

## [3.3.1] — 2026-07-09

### Added
- Claude Code user-settings merge (`scripts/merge_claude_settings.py`) — safe install-time hook wiring with backup
- Secret redaction for daily capture (`scripts/secret_redact.py`)
- Public Evidence daily fixtures + curated sample notes under `knowledge/`
- Regression suite for path-safety, fake-LLM compile e2e, Claude merge

### Changed
- **Three-zone layout** complete: CODE / `knowledge/*` / runtime only under `$LLM_WIKI_STATE_ROOT/{run,logs,cache}`
- Installers (`install.ps1` / `install.sh`): correct `Ekgardt/llm-wiki` URLs, `run|logs|cache` dirs, OpenCode force-copy, Codex paths
- Compile: category whitelist + path containment, dry-run no side effects, AGENTS from `docs/AGENTS.md`
- Queue drain works without `output_path`; atomic `maybe_compile` lock
- Capture hooks use `update_state` + redaction; projects path under `knowledge/projects`
- Docs, skills, Cursor rules aligned to `knowledge/` (no live root `memory/` / `wiki/`)
- Search: FTS quote escape, vector `as_of`, JSON vector cache (no pickle)
- Archive under `knowledge/notes/archive/`; export forbids `.obsidian/`
- Benchmark scans flat notes (reproducible on public tree)

### Fixed
- Path traversal via LLM `category`; Codex wrapper `exit` killing shell; flush `--event` mapping
- OpenCode timestamp format (`[HH:MM:SS]`); broken QA dir; lint double-scan / wrong index path
- Doc falsehoods (test counts, install URLs); tracked wikilinks (0 missing)

### Security
- Capture redaction for common secret patterns
- Compile/feedback/blackboard path containment
- Gitleaks in CI; `uv sync --locked`

### Tests
- **178** pytest tests (hermetic state under `.pytest_cache/`)

## [3.3.0] — 2026-07-04

### Added
- **Vector warm-start** — plugin preloads sentence-transformers model + builds vector cache at session start
- **Russian / Chinese READMEs** + language selector
- **GitHub badges** (CI, license, tests, benchmark)

### Changed
- Cross-platform install (`install.sh` / `install.ps1`)
- Portable OpenCode plugin via `$LLM_WIKI_ROOT`
- Benchmark methodology disclosure

### Tests
- 155 tests at this release tag

---

## [3.2.0] — 2026-07-03 — "Proactive Intelligence + Multi-Agent Coordination"

### Added
- **Cursor integration** — `.cursor/rules/llm-wiki.mdc` rules file for vault access
- **Antigravity integration** — `AGENTS.md` snippet for vault access
- **IDE integrations guide** (`integrations/README.md`) — how IDE agents differ from CLI agents
- **Guard rails** (`scripts/build_guardrails.py`) — auto-injects learned corrections at SessionStart, preventing agents from repeating past mistakes
- **Feedback capture** (`scripts/feedback_capture.py`) — detects user corrections/preferences in session transcripts, saves as candidates for promotion to knowledge pages
- **Agent timeline** (`scripts/agent_timeline.py`) — attribution: shows which agent made which decision and when
- **Blackboard coordination** (`scripts/blackboard.py`) — parallel agents claim tasks, signal completion, detect conflicts (O(n) instead of O(n²) coordination)
- **Loop detector** (`scripts/loop_detector.py`) — prevents infinite "fix → review → redo" cycles across agents
- **Bootstrap from git** (`scripts/bootstrap_project.py`) — auto-generates project context from README, git log, tech stack, docs
- **Per-project context builder** (`scripts/build_context.py`) with auto-detect agent strengths
- **Graph-neighbor search** (`scripts/graph_neighbors.py`) — 3rd retrieval signal via wikilink graph RRF
- **Weighted RRF fusion** — BM25=2.0, Vector=1.0, Graph=0.5 (prevents search regression)
- **Title + filename boost** — exact match → 10x score, prevents duplicate-page confusion
- **Real-time contradiction check** — pre-write supersede detection in compile pipeline
- **Smart auto-archive** (`scripts/archive_stale.py`) — type-aware thresholds (decisions never archive, debugging at 60 days)
- **Temporal validity lint** (check #13) — `valid_from`/`valid_to` frontmatter validation
- **Benchmark suite** (`benchmark/run_benchmark.py`) — Recall@K, MRR, latency measurement

### Benchmark Results
| Metric | Value |
|---|---|
| Recall@2 | **100%** |
| Recall@5 | **100%** |
| Recall@10 | **100%** |
| MRR | **0.952** |
| Latency p50 | **28ms** |
| Token cost | **0** |

### Changed
- Search pipeline: BM25-only → BM25+Vector → BM25+Vector+Graph triple fusion
- Compile pipeline: added feedback capture integration at FLUSH classification
- SessionStart: added guard rails + advisory blocks before metacognitive context
- Nightly task: added FTS5 index rebuild + graph cache rebuild
- Weekly task: added auto-archive with type-aware thresholds
- FTS5 query: per-word quoting (prevents column-name interpretation, preserves AND semantics)

### Security
- All personal data scrubbed from git history (git-filter-repo)
- 0 dead imports, 0 TODO/FIXME, 0 absolute paths in tracked files
- Gitleaks: no leaks found

---

## [2.1] — 2026-07-03 — "Multi-tool, zero ops"

### Added
- Universal LLM client (5 backends: OpenCode/Codex/Claude/OpenAI/Ollama)
- Persistent deferred-task queue
- Concurrency-safe compile pipeline with PID lock
- 3-tier FLUSH classifier (MAJOR/MINOR/OK)
- OKF v0.1 frontmatter migration (100% conformant)
- Metacognitive SessionStart context
- Crystallize-playbook skill
- Windows Task Scheduler (nightly + weekly)
- OpenCode plugin + Codex PowerShell wrapper + Claude Code hooks

---

## [1.0] — 2026-04 — "Karpathy-style vault with session memory"

### Added
- 3-layer architecture: raw/ (immutable) / knowledge/notes/ (compiled) / memory/ (session lore)
- 7-check structural lint with LLM contradiction detection
- Multi-project slug system with 5-step collision resolution
- QMD hybrid search (BM25 + vector + reranker)
- Promotion pipeline from project memory to cross-cutting wiki
