# A check names its cause

Date: 2026-09-28. Status: implemented on branch `install-truth`.

## What happened

On one machine the installer's adoption check reported `conflict` /
`reliability_v3_record_invalid`. The installer then printed "session capture is
disabled", and doctor agreed. A second check a minute later said `adopted`.

`inspect_installed_vault` caught every exception and answered with that one fixed
code, with no cause attached. The repair command's process boundary did the same
(`repair_backend_error`, `details: {}`), and so did doctor's adoption probe
(`unknown`). With a real `BEGIN EXCLUSIVE` held on `run/queue-v3.sqlite3`, the old
inspection waits out its busy timeout (5 s) and then reports `conflict`. So a
writer that is busy right now was reported as broken records.

The same install also synced only the baseline dependencies. `install_models`
fetches a pinned model only when that model's runtime is installed, so the e5
weights were never fetched or used, and doctor reported the missing
`onnxruntime` after every install.

## Decisions

1. **Inspection names the cause and tells "busy now" apart from "wrong".**
   - A busy or locked database becomes the state `unreadable` with the blocker
     `reliability_v3_state_unreadable`. SQLite reports this as result code
     `SQLITE_BUSY` (5) or `SQLITE_LOCKED` (6), and only the primary (low) byte
     is compared.
   - Python 3.10 exposes no result code, so there every `OperationalError`
     counts as busy.
   - Timeout, `BlockingIOError` and interruption also count as busy.
   - Anything else stays `conflict` / `reliability_v3_record_invalid`.
   - In both cases `details.error` carries the redacted cause chain, with
     absolute paths replaced by `<path>`. The line is at most 600 characters,
     which holds one line of `Class: message <- Cause` with two causes.
2. **No extra retry.**
   - The inspection's own SQLite busy timeout already waits: 5 s on the queue
     and 10 s on the coordinator, measured.
   - A retry loop on top would only lengthen the install. The honest move is to
     report the cause and say that nothing is known about capture.
3. **The installers say only what they know.**
   - `repair_installed_memory.py --check --summary` prints one plain line per
     state. Both installers print that line in their fallback branch instead of
     "capture is disabled".
   - Doctor treats `unreadable` as "could not be read just now (cause); this is
     not a finding that capture is disabled".
4. **The install brings `semantic`.**
   - The cost, from `uv.lock`, is about 22 MB: onnxruntime 18.7 MB and
     tokenizers 3.3 MB. numpy and huggingface-hub are needed anyway.
   - `reranker` stays the operator's choice. torch alone is 526.6 MB, plus
     about 800 MB of CUDA wheels on Linux.
   - The nightly update keeps the extra: `self_update.chosen_extras` finds the
     installed `onnxruntime`.
5. **The queue's eligible count uses the claim's own predicate.**
   - Ported from PR #49 (commit de44d610, by its author): a ready capture task
     was counted as remaining work that `work` never claims.
   - The same class had a second member: a task under a source fence is skipped
     by the claim, but the count still included it.
   - The count and the claim now share one SQL predicate,
     `_V3_WORK_CLAIMABLE`.

## Class guards

- `tests/test_a_broad_handler_carries_its_cause.py` walks every module in
  `scripts/`. It refuses any `except Exception` / `BaseException` / bare handler
  that never reads what it caught and still answers with a verdict: a returned
  failure word, a returned report/result/error/failure builder, or `raise … from
  None`.
  - It found 5 more beyond the original three: `build_tiers` ×1, doctor's
    Pyright probe, `llm_client` DLP ×2, and `repair_installed_memory`. All five
    now carry the cause.
  - The DLP scanner failure carries only the exception class, because its
    message may quote the scanned text.
  - Not covered: ranking fallbacks that record a reason code next to a degraded
    result (`reranker_error`, `graph_error`). Their cause is still dropped, and
    this is left open.
- `tests/test_the_installer_brings_the_runtime_of_every_model_it_fetches.py`
  checks two things:
  - the sync plan carries the embedding model's runtime;
  - every pinned model's runtime is one declared extra away.

## Sources

1. SQLite, "Result and Error Codes", https://www.sqlite.org/rescode.html:
   - "The SQLITE_BUSY result code indicates that the database file could not
     be written (or in some cases read) because of concurrent activity by some
     other database connection";
   - "The least significant 8 bits of the result code define a broad category
     and are called the 'primary result code'."
2. Python documentation, `sqlite3.Error.sqlite_errorcode`: "The numeric error
   code from the SQLite API. Added in version 3.11."
   https://docs.python.org/3/library/sqlite3.html
3. PEP 3134, "Exception Chaining and Embedded Tracebacks":
   - "exception B is propagated outward and exception A is lost";
   - `__context__` is set automatically, so a `raise` inside a handler keeps
     the cause, which is why the guard does not refuse it.
   https://peps.python.org/pep-3134/
4. OWASP Error Handling Cheat Sheet: the error details are logged "for
   investigation, and not returned to the user", and paths such as installation
   directories are disclosure. This is why paths are replaced in a report that
   gets printed and pasted.
   https://cheatsheetseries.owasp.org/cheatsheets/Error_Handling_Cheat_Sheet.html
5. uv, "Syncing": "uv does not sync extras by default. Use the `--extra`
   option"; "To retain extraneous packages, use the `--inexact` flag."
   https://docs.astral.sh/uv/concepts/projects/sync/

## Alternatives considered

- **Retry the check in the installer.** Rejected: the busy timeout already
  retries, and a loop hides a lock that is held for a long time.
- **Match "database is locked" in the message.** Rejected: that matches the
  wording, not the mechanism. Result codes are the contract.
- **Install `hybrid` by default.** Rejected for its GB-scale cost; `semantic`
  is what the default read path needs.
- **Skip the weights when the extra is absent.** This is already the behaviour.
  The defect was that the install never brought the extra.

## Scope extension: the warnings an install ended with (same day)

The owner's latest install ended with warnings they could not act on. Each one,
with its cause and what the product now does:

1. **Codex MCP "conflict … Merge manually".**
   - Cause: `codex_memory._mcp_entry_state` called every entry that was not today's
     exact form a conflict. That included the entries this product's own earlier
     releases wrote: `uv run --directory <vault> python scripts/mcp_server.py`
     from before `--locked --no-sync` (2026-07-13 to 2026-08-14), or an entry
     naming a vault in another directory. It also counted an entry the operator
     had switched off.
   - Now there are four states:
     - `stale` means our own shape. It is rewritten to this vault's entry.
     - `conflict` means someone else's entry. It is replaced only with
       `--replace-codex-mcp` / `-ReplaceCodexMcp`.
     - `disabled` is left as the operator set it.
     - `not-rewritable` covers inline or split tables, which are left unchanged.
   - Every rewrite goes through `publish_configuration`, which keeps a verified
     preimage beside `config.toml`, and only after the rewritten file parses to
     the old document with the entry alone replaced.
   - Both installers print the helper's own line (`config-advice`). No line asks
     for a manual merge.
   - Codex hook trust (`runtime_hooks_untrusted`) cannot be granted by an
     installer, because Codex asks a person in `/hooks`. Doctor now says that
     once, in plain words.
2. **Pyright `pyright_manifest_predates_tree_digest`.**
   - `install_pyright.py` is the documented repair: it retires a pre-digest
     install and installs the pinned release. Both installers now run it when a
     managed Pyright directory exists, and never install one the operator did
     not choose.
   - `pyright_node_executable_unsafe` is the first `node` on PATH being a
     symbolic link, sitting under a linked or network directory, or not being a
     regular file. This machine's `node` is a regular file, so this is not
     reproduced here. Doctor now names the action in words.
3. **"16 dead tasks, 19 days, requires operator attention".**
   - Read-only on this machine's live queue: 25 dead `flush` tasks
     (`processor_failed`, 2026-08-27..09-08). All are capture-linked, and every
     one has a redrive that was not cancelled: 17 succeeded and 8 are ready
     again.
   - Doctor counted the parents anyway. A dead task that a redrive answered (it
     succeeded, is queued, or died and is counted itself) is now resolved.
   - The queue message names the count and the age instead of "requires
     operator attention".
   - No new redrive rule was added. `redrive-dead-captures` already runs nightly
     after a code change.
4. **"N captures lost", "N tool calls failed".**
   - These are not counters that last forever. Both verdicts already cover only
     the last seven days (`CAPTURE_RECENT_SECONDS`) and turn green after a quiet
     week, while the totals stay visible.
   - What misled was that the degraded line gave only the cumulative total. It
     now says "N in total; the last at T, within the last seven days".
   - The losses on this machine are current. The latest trail records are writer
     gate timeouts on 2026-09-28. Hiding them would be law 6, so they are not
     windowed further or acknowledged away.
5. **Final verdict.** The installers already exit 1 with `[FAIL]` on a failure.
   The "installed with warnings" banner now says the install completed, that
   this is not a failure, and that the `[WARN]` lines name what needs attention
   now.

Neighbour found and not fixed in this pass (fixed in the second pass below): the
adopted queue's claim still matches a fenced day by text (`instr(payload, date)`). The enqueue side moved to identity fields on
2026-09-18. A task that only mentions the fenced day's date waits for the fence to
lift. The count now agrees with the claim, but the claim is still wider than the
fence it serves.

Additional sources (fetched 2026-09-28):
- Codex MCP documentation (developers.openai.com/codex/mcp, which redirects to
  https://learn.chatgpt.com/docs/extend/mcp?surface=cli): an entry is
  `[mcp_servers.<name>]` with `command`, `args`, and `enabled` ("Set `false` to
  disable a server without deleting it"). That is why a switched-off entry is the
  operator's choice and is left alone.
- Codex hooks (https://learn.chatgpt.com/docs/hooks): "Before a non-managed hook
  can run, Codex requires you to review and trust the exact hook definition" and
  "Codex records trust against the hook's current hash". That is why no installer
  can grant it.
- Amazon SQS, dead-letter queue redrive
  (https://docs.aws.amazon.com/AWSSimpleQueueService/latest/SQSDeveloperGuide/sqs-configure-dead-letter-queue-redrive.html):
  redrive moves "unconsumed messages from a dead-letter queue to another
  destination for processing", and "All redriven messages are considered new
  messages". That is why the redriven copy, not the parent, carries the
  outcome.

## Second pass, same day: the answer paths and the fence neighbour

Facts (read in the code, 2026-09-28):

- The reranker answered `reranker_error` from two scorers, retrieval answered
  `graph_error` and `reranker_error`, the graph neighbour boost returned `None`, a
  navigation callback answered `NavigationStatus.ERROR` or set
  `structural_failed`, the code graph fell back to regex parsing from four
  handlers, and a failed language-server request was answered `Internal error`.
  Every one dropped the exception. The class guard did not see them: it looked
  only at a returned string or builder call.
- The claim and the eligible count of both queues matched a fence with `instr`
  over the payload text (the legacy count did not look at fences at all), while
  the enqueue and the fence check had read identity fields since 2026-09-18.

What changed:

- Each fallback keeps its behaviour and names its cause. Retrieval and the
  reranker record it through `search_memory.note_degradation`, read by
  `vault_status.retrieval_degradations` (the channel the dense and generation
  fallbacks already use). The code graph puts it in the parse result
  (`parser_fallback_reason`) and in the call Jedi could not infer
  (`semantic_error`); `path_coverage` says `grammar unavailable: <cause>`.
  Navigation warnings read `symbol resolver failed: <ExceptionClass>`: the class
  only, because the existing contract keeps every callback's exception text out of
  the answer (`test_callback_exceptions_are_redacted_at_every_boundary`; a message
  can quote any path, and `redact_lsp_text` removes only home and checkout roots).
  The language server is still told only `Internal error`; the protocol's warning
  callback is told the redacted cause. Uncertainty: production constructs
  `LspProtocol` without a warning callback (`lsp_process._start_generation_protocol`),
  so today that cause reaches no reader; wiring a sink is a separate change.
- The guard now refuses a broad handler that does not read what it caught and
  names a failure anywhere in its body (a failure string, a failure attribute, a
  builder call, `raise ... from None`), and, in the four answer modules, any value
  fallback. It accepts `traceback.format_exc`, `sys.exc_info` and
  `logging.exception` as reading the exception, and a re-raise as carrying it.
  Checked against the old files: it refuses all thirteen handlers above.
- Both queues' claim and count call one SQL function,
  `payload_references_source`, registered per connection from
  `MemoryQueue._payload_references_source`, so SQL and the enqueue apply the same
  rule. A task that only carries a timestamp of the fenced day is claimed and
  counted; a task of the fenced source is neither.

Alternatives weighed:

- Guarding every broad handler in `scripts/`: 80 broad handlers (one a close
  cleanup in retrieval) still drop what they caught without naming a failure (49 return
  a default, 28 only pass or continue, 3 do something else) — hooks that must exit
  0, best-effort cleanups, optional probes. Refusing them all at once would need 80 individual judgements or an
  allowlist with no reasons, so the guard covers the answer modules and every
  handler that names a failure; the rest is named here as not yet covered.
- Filtering fenced tasks in Python after an unfenced `SELECT`: the claim would
  have to loop and the count to read every ready payload, and SQL and Python would
  again be two rules. A registered function keeps one predicate.
- Putting the cause into the retrieval trace: the trace's reason fields are codes
  under a schema (`^[a-z][a-z0-9_]*$`), which MCP clients read; the degradation
  channel already carries causes, so no schema change was needed.

Compromises and uncertainty: the degradation channel keeps the latest cause per
kind in the process, not per query. A payload that fails to decode still counts
as referencing every fence (the conservative answer, unchanged). The function
is not part of any schema or trigger, so a connection without it (the sqlite3 CLI,
another tool) can still open the database; only these queries require it.

Sources (fetched 2026-09-28):
- Ruff BLE001 blind-except (https://docs.astral.sh/ruff/rules/blind-except/):
  flags `except BaseException` and `except Exception`; "Exceptions that are
  re-raised will _not_ be flagged" and neither are those "logged with `exc_info`
  enabled". The guard's exemptions follow the same two.
- MITRE CWE-390, Detection of Error Condition Without Action
  (https://cwe.mitre.org/data/definitions/390.html): "The product detects a
  specific error, but takes no actions to handle the error"; mitigation: handle
  each exception by fixing it, alerting, or terminating.
- Python `sqlite3.Connection.create_function`
  (https://docs.python.org/3/library/sqlite3.html): `deterministic=True` marks the
  function deterministic, "which allows SQLite to perform additional
  optimizations"; errors in user-defined function callbacks are logged as
  unraisable exceptions. `_payload_references_source` answers every input without
  raising (an unreadable payload answers True).
- SQLite, Deterministic SQL Functions (https://www.sqlite.org/deterministic.html):
  deterministic functions are required only in CHECK constraints, partial and
  expression indexes and generated columns; none of those use this function.

## Addendum (2026-09-28): the protocol warning reaches a log

The first pass sent a failed server request's redacted cause to
`LspProtocol`'s warning callback, and production built the protocol without one,
so the cause, like every earlier protocol warning (an oversized frame, a
diagnostic flood), was dropped. `lsp_process` now passes
`_report_protocol_warning`, which prints `llm-wiki lsp: <message>` to stderr.

Sources, read today:
- MCP specification 2025-06-18, Transports, stdio: "The server MAY write UTF-8
  strings to its standard error (stderr) for logging purposes. Clients MAY
  capture, forward, or ignore this logging." and "MUST NOT write anything to its
  stdout that is not a valid MCP message."
  https://modelcontextprotocol.io/specification/2025-06-18/basic/transports
- The Twelve-Factor App, XI Logs: a process writes its event stream unbuffered to
  its standard stream and leaves routing to the environment. stdout is taken by
  the MCP messages, so stderr is the stream left. https://12factor.net/logs
- Python Logging HOWTO: a warning the caller cannot act on belongs to
  `logger.warning()`; with no handler configured it goes to the last-resort
  handler, which writes to `sys.stderr`.
  https://docs.python.org/3/howto/logging.html

Alternatives: `logging.getLogger(...).warning` reaches the same stderr today and
could be routed later, but no LLM Wiki module configures logging and
`mcp_server.py` already reports worker failures with `print(..., file=sys.stderr)`;
one idiom per process was kept. A file under `logs/` would add a new log with a
rotation obligation for a rare event. Trade-off: stderr is read only if the
client keeps it (Claude Code writes MCP stderr to its own log).
Guard: `tests/test_a_protocol_warning_reaches_the_log.py` refuses an
`LspProtocol(...)` in `lsp_process.py` built without `warning_callback`.

## Addendum (2026-09-28, live update): doctor waits out a busy admission as writers do

Observed on the live vault while it was being updated to this branch: `install.sh`
aborted in its production smoke twice. The first refusal said only "Doctor did not
return valid JSON" and dropped doctor's exit code and stderr, so its cause is not
known; 85 later direct runs of `doctor.py --json` all returned JSON. The second,
reproduced by running the smoke exactly as the installer does (one run in three),
was `Doctor reported error in: adoption`. Traceback:
`_validate_active_database_reference` -> `PRAGMA foreign_key_check` ->
`sqlite3.OperationalError: database is locked`, on attempt 7 of a loop over
`_load_complete_adoption` while live hooks were writing. One check takes about
0.36 s here.

Cause: writers admit themselves through `_validate_adoption_with_retry`, which
waits out a busy database for `_ADOPTION_VALIDATION_SECONDS`; doctor's adoption
check called the single attempt beneath it, so a moment's lock became "Every
Markdown writer is refused", which was false, and the smoke stopped the update.

Sources, read today:
- SQLite result codes, SQLITE_BUSY: "could not be written (or in some cases read)
  because of concurrent activity by some other database connection"; the remedy
  is to wait, with busy_timeout or a busy handler. https://www.sqlite.org/rescode.html
- SQLite file locking (rollback journal): while a writer holds PENDING "no new
  SHARED locks are permitted", and EXCLUSIVE allows no other lock at all, so a
  reader in rollback-journal mode meets BUSY whenever a writer commits.
  https://www.sqlite.org/lockingv3.html
- Python `sqlite3.connect(timeout=...)`: how long a connection waits for a lock
  before raising OperationalError. https://docs.python.org/3/library/sqlite3.html

Fix: the retry loop is `markdown_transaction.require_adopted_through_contention`,
used by writers (after the stray retiral they alone may do) and by doctor, so the
two give one verdict. A lock that outlasts the deadline still refuses both, and
still says so. The smoke's JSON refusal now carries `exit N; stderr: <tail>`.
Alternative considered: a busy timeout on the read-only opener. It would also
serve other readers, but `open_readonly_operational_db` lives in
`reliable_memory.py`, and its callers' deadlines were chosen with `busy_ms=0`;
changing them all is a separate change with its own measurement.
Guard: `tests/test_doctor_waits_out_a_busy_admission.py` (a real exclusive lock
held for 0.5 s; fails on the old code) and the smoke's named-cause test.
