# How does an index stay fresh during the day without growing a daemon?

Date: 2026-08-28. Question behind it: `CODE-04` asks for "непрерывная свежесть:
ограниченный наблюдатель вместо только-ночной пересборки", with the gate
"`stale` исчезает днём, цена процессора названа". The product contract permits
"optional bounded watching" and forbids a persistent daemon. Today the only
thing that clears `stale` is the 03:00 pass, so the vault spends the whole
working day answering from an index that does not contain what was written that
day. Measured on this vault at 17:05 on 2026-08-28: 84 sources differ from the
active generation (33 changed, 51 added) — every one of them written after the
03:00 build, none of them findable until the next one.

## What current practice does

**File watching is a client responsibility, not a server one — and the stated
reason is CPU.** The LSP specification does not let a language server watch the
filesystem; it registers `workspace/didChangeWatchedFiles` with the client. The
rationale in the spec is explicit: getting filesystem watching right across
operating systems is hard, watching is not free — "especially if the
implementation uses some sort of polling and keeps a file system tree in memory
to compare time stamps" — and "a client usually starts more than one server, and
if every server runs its own file system watching it can become a CPU or memory
problem"
([LSP file watchers](https://emacs-lsp.github.io/lsp-mode/page/file-watchers/),
[neovim#16078](https://github.com/neovim/neovim/issues/16078)). The design rule
that generalises: *the observer belongs to a process that already exists*, never
to a process created for observing.

**The dominant no-daemon shape is opportunistic maintenance piggybacked on
commands that already run.** Git is the canonical instance: porcelain commands
that create objects run `git gc --auto`, which "first checks if any housekeeping
is required on the repo before executing" and "exits without doing any work" if
not; `git maintenance` generalises it to "automatic maintenance after Git
commands write data into the repository"
([git-gc](https://git-scm.com/docs/git-gc),
[git-maintenance](https://git-scm.com/docs/git-maintenance)). Two properties are
load-bearing and both are cheap-check-first: the piggybacked step must be able
to answer "nothing to do" for far less than the work it guards, and it must be
bounded so it cannot turn a fast command into a slow one.

**Watchers coalesce; the parameter is a settle window, and it is small.**
Watchman's `settle` is "how long the filesystem should be idle before dispatching
triggers", default 20 ms, and events are "consolidated and settled … before they
are dispatched so that your script won't start executing until after the files
have stopped changing" ([Watchman config](https://facebook.github.io/watchman/docs/config)).
Code indexers pick the same shape with larger numbers because their work is
larger: CodeGraph fires "after a 300 ms quiet window for lone saves (bursts of
edits still coalesce)" with `CODEGRAPH_WATCH_DEBOUNCE_MS` default 2000 ms, and
its documentation says to raise it to 5–10 s "when a build step or formatter
writes many files in a tight burst"
([CodeGraph indexing](https://colbymchenry.github.io/codegraph/guides/indexing/),
[auto-sync hooks](https://deepwiki.com/colbymchenry/codegraph/5.2-auto-sync-hooks)).
CKB's daemon watcher polls `.git/HEAD` every 2 s and debounces 5 s
([CKB index management](https://github.com/SimplyLiz/ckb/wiki/Index-Management)).
The settle window scales with the cost of the work it gates, not with the
editor.

**Polling is not disqualified; inotify is not free either.** `inotify` is
per-directory, capped (commonly 8192 watches per user) and "uses a lot of
resources for directories that have a lot of existing files", which is why
several projects ship `PollingObserver` deliberately; the measured trade is
inotify at ~15 ms median latency versus polling at ~85 ms, at roughly 5× the CPU
for polling ([watchdog#673](https://github.com/gorakhargosh/watchdog/issues/673),
[watchdog#577](https://github.com/gorakhargosh/watchdog/issues/577)). For a
consumer whose reaction costs minutes, an 85 ms detection latency is free and a
watch-descriptor budget is a liability.

**Staleness detection: timestamps are the cheap tier, hashes are the correct
one.** Make and Ninja decide by mtime; Bazel "tracks output staleness by
inspecting the content digests of the inputs", which is why it is deterministic
where timestamp systems produce spurious rebuilds
([Bazel remote caching](https://blogsystem5.substack.com/p/bazel-remote-caching),
[How Ninja works](https://fuchsia.dev/fuchsia-src/development/build/ninja_how)).
Both are used together in practice: timestamp first as a cheap filter, digest
second as the decision.

**Incremental indexing is a solved shape, and the win is in not re-embedding.**
Zoekt's delta builds tombstone files in older shards rather than rebuilding, and
"can check if existing shards match current build options and skip indexing if no
changes are needed"
([zoekt#29731](https://github.com/sourcegraph/sourcegraph-public-snapshot/issues/29731),
[zoekt indexing](https://deepwiki.com/sourcegraph/zoekt/4-indexing-system)). On
the RAG side the reported wins are all about selective re-embedding — chunk-level
content-addressed change detection re-embedding 10–15% instead of 100%, a
diff-overlap scheme reporting a 77% reduction
([incremental indexing for RAG](https://dev.to/guptaaayush8/building-a-production-ready-rag-system-with-incremental-indexing-4bme),
[strategies](https://medium.com/@vasanthancomrads/incremental-indexing-strategies-for-large-rag-systems-e3e5a9e2ced7)).

## What that implies here

1. **No new process whose job is to watch.** Both the LSP rationale and the git
   model point the same way: the check hangs off a process that already exists,
   and its "nothing to do" answer must be very cheap. This vault's existing
   processes are the MCP server (resident for a session), the lifecycle hooks
   (short-lived, and one of them has a 0.1 s budget pinned by a test), and the
   scheduled passes. So the unit of design is a **bounded step with a cheap
   negative answer**, not a loop.

2. **Three tiers, cheapest first**, which is Ninja-then-Bazel plus git's
   "check before doing":
   - tier 0, a hash-free stat walk of the same corpus selection: answers *has
     anything been touched since the active generation was built*, and *has the
     vault been quiet long enough*;
   - tier 1, the authoritative hash comparison (`collect_corpus` + the source
     manifest of the active generation): answers *is it actually stale*, and
     absorbs tier 0's known false positive (a touched file with unchanged bytes);
   - tier 2, the existing bounded fenced builder.

3. **The settle window is sized by the work it gates, not by the editor.**
   Watchman's 20 ms and CodeGraph's 300 ms guard sub-second work. Here tier 2
   costs minutes, and this vault has already recorded what happens when a
   rebuild races continuous writing: three attempts in a row deferred with
   `corpus_changed`, one after 515 seconds (`knowledge/log.md`, 2026-08-24). So
   the quiet window belongs in the tens of seconds and must be defended by a
   measurement of how often this vault actually changes, not chosen.

4. **Stateless quiet beats remembered quiet.** A debounce implemented as
   "compare this poll to the previous poll" forces the watcher to be a loop that
   owns memory, which is the daemon shape again. `now − max(mtime)` over the
   corpus answers the same question from a single probe, so any caller can ask
   it once and leave. This is the one place where the timestamp weakness is
   actually a strength: an mtime that is newer than it should be delays a
   rebuild, and delaying is the safe error.

5. **What cannot be fixed from here, and should be said rather than implied.**
   `evidence_graph_builder.build_incremental_generation` already implements the
   shape the literature recommends — reuse the parent's records for sources
   whose digests did not move — but on this corpus it never engages. The reuse
   state lives in a per-generation `incremental-manifest.json`, and
   `_stored_incremental_manifest` drops it when it exceeds
   `MAX_STORED_INCREMENTAL_MANIFEST_BYTES` (64 MiB). Measured on 2026-08-28 on a
   copy of this vault's corpus: the manifest is **158,075,010 bytes**, because it
   carries one record-dependency row per record and the generation holds 349,306
   records. No published generation on this machine — none of the 33 live ones,
   none of the temporary ones — contains that file. So the parent is never
   reusable, `doctor._parent_is_current` can never be true, and every pass is a
   build from nothing plus a full re-embedding of every chunk. That is a change
   inside `evidence_graph_builder.py` and `search_memory.py`, which this task may
   not touch; it is named here rather than attempted, because a watcher that
   triggers an 800-CPU-second rebuild for a one-line note is not freshness, it is
   a heater.

## What this research does not settle

Whether the resulting cost is defensible. Nothing above says what a stat walk
over 831 files costs on this machine, how often this vault is written to during
working hours, or how long a refresh takes when only a few files changed. Those
are measurements, and the gate for `CODE-04` is a measurement, not a design.
