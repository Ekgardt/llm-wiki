# Warming the graph before the first question

> **Superseded the same evening.** The owner called the warm-up a crutch, and it
> was one: it moved the cost instead of removing it. The structural fix is in
> `docs/research/2026-09-12-a-verdict-worth-remembering-across-processes.md` —
> the verified digest is remembered by stat identity, Git's way — and the warm-up
> was deleted with it. What follows is the research that led to the wrong choice,
> kept because the measurements and the option prices in it are still true, and
> because option D in it is what was done.

Dated 2026-09-12, at the owner's request, after the digest decision cut a cold
code answer from 4.9 s to 2.22 s
(`docs/research/2026-09-12-a-reader-checks-the-digest-a-writer-derives.md`).
This is the research for the one remaining shape of that cost, and it names what
I would do rather than doing it.

## What is left to win, measured

Same call, same machine, installed vault: **2.22 s** on the first call in a
process, **0.31 s** on every later one. The other tool answers in about 2.0 s
every time. So the only remaining gap is the *first* question of a session, and
it is worth 1.9 s once.

The 2.22 s is the generation open: resolving the repository scope, hashing the
artifacts against the manifest, and the seal. It is paid once per process because
the validated reader is then cached (`evidence_reader_cache`, `IDLE_SECONDS =
600`).

## Practice on this date

1. **A background worker that warms an index after start is the standard answer
   to exactly this stall.** PostgreSQL ships `autoprewarm` as part of
   `pg_prewarm`: it periodically records which blocks are in shared buffers and,
   on restart, a background worker loads those blocks back before the workload
   arrives
   ([PostgreSQL index warmup](https://mjmichael.medium.com/postgresql-index-warmup-stop-the-stall-the-guide-to-9bf6919b36c1)).
2. **Warm deliberately and narrowly.** Cache-warming guidance is to pre-populate
   before the first real request, throttle the warming requests, and "start with
   your 20–100 highest-priority URLs, not your entire sitemap"
   ([cache warming with Redis](https://oneuptime.com/blog/post/2026-03-31-redis-implement-cache-warming-strategies-with-redis/view),
   [warmup cache requests](https://denebrixai.com/blog/warmup-cache-request/)).
3. **Never lazy-load what the first meaningful response needs; defer the rest.**
   The eager/lazy decision is a prioritisation, not a preference
   ([lazy vs eager loading](https://webeyez.com/insights/guides/lazy-loading-vs-eager-loading-optimization-guide)).
4. **Threading rule for the reader.** SQLite's multi-thread mode requires that no
   two threads use one connection at the same time; a connection per thread, or
   serialisation by the caller
   ([SQLite and threads](https://sqlite.org/threadsafe.html),
   [Python, SQLite and thread safety](https://ricardoanderegg.com/posts/python-sqlite-thread-safety/)).

## What this codebase already has

- **The precedent is in the same function.** `run_server` already calls
  `_start_encoder_warmup`, which runs `warmup_retrieval_path` on a **daemon
  thread** with its own deadline and an `LLMWIKI_NO_ENCODER_WARMUP=1` kill
  switch. The owner accepted that trade on 2026-08-27: a resident server pays the
  memory up front so the first question does not pay the latency.
- **The reader cache is already safe from another thread.** A lease holds the
  entry lock, so one thread uses the reader at a time — SQLite's multi-thread
  condition — and `shared_readers_supported()` refuses only a single-thread build
  (`docs/research/2026-09-10-a-cached-reader-needs-one-thread-at-a-time-not-a-serialized-build.md`).
- **Two gaps to respect.** `leased_graph` does not deduplicate concurrent misses:
  a question arriving while the warm-up is still opening will open a second
  reader and one of the two is dropped — wasted work, never a wrong answer. And
  an entry nobody uses within 600 s is closed, so a warm-up whose session asks
  no code question wasted its 2.2 s of CPU.

## Options, and what each costs

- **A. Leave it.** The first code question of a session costs 2.22 s; the rest
  0.31 s. Nothing to build, nothing to hold.
- **B. Warm the vault's graph on a daemon thread at server start**, exactly as
  the encoder is warmed: one open, one kill switch, one deadline. Wins the whole
  1.9 s on the first code question. Costs 2.2 s of background CPU per server
  start and the reader's memory for at least 10 minutes, spent even in sessions
  that ask nothing about code.
- **C. Warm on the first tool call instead of at start.** Narrower: only a
  session that is already working pays, and the cost lands while the user is
  waiting for something else anyway. Slightly more code — the trigger has to be
  once-only and not re-entrant with the call it rides along with.
- **D. Make a cold open cheap instead of hiding it.** The seal-and-digest verdict
  is remembered per process today; persisting it (keyed by the entry seal, under
  `run/`) would make the *first* open in a *new* process cheap too, which is what
  the benchmark's fresh-process-per-call shape measures and what a CLI caller
  pays. Bigger than B and C, touches the fence again, and would need its own
  decision.

## What I would do, and why

**B now, D as the next structural step, and not C.**

B reuses a mechanism this server already has, with the same kill switch and the
same accepted trade; it adds no contract, no daemon (the process already exists),
and no new lifecycle. Rule 4's "быстрая" is satisfied for the operator's first
question, which is the one that decides how the product feels. C's saving over B
is the CPU of sessions that never ask about code — real, but it buys that by
adding a trigger with re-entrancy to get wrong, and B's waste is 2.2 s of one
background thread.

D is the honest end state: warming moves a cost, persisting the verdict removes
it. It belongs after B because it is a change to how the fence is remembered
across processes, not just when it is paid.

Bounds I would hold B to: the vault's own directory only — never every
registered repository, which would open N readers and hold N of them; one
attempt, no retry; a deadline of its own; failures swallowed the way the encoder
warm-up swallows them; and a test that the thread is a daemon so the process can
still exit.

Files: `scripts/mcp_server.py`, `scripts/evidence_reader_cache.py`,
`docs/research/2026-09-12-warming-the-graph-before-the-first-question.md`.
