# What a seal may remember, and what a fallback needs to exist

2026-08-29. Research for two findings left open by
`docs/research/2026-08-29-what-an-optional-stage-may-spend.md`: the generation
consumption seal recomputed several times per query, and `get_decisions` having
no degraded answer at all. Measured on this machine unless marked otherwise;
measured and inferred are kept apart.

## Part 1 — the seal

### The question

`search_memory._generation_consumption_seal` is the reader's record of what it
saw, so a later check can prove nothing moved. It is computed when a query
adopts a generation, and again before and after every read. Each computation
hashes every sealed artifact end to end.

Measured on the live vault, instrumented at the function itself, three real
queries through `search_memory.search` with a 10 s deadline:

| query | seals | seal time |
|---|---|---|
| «как устроен повтор после карантина» | 4 | 0.34 s |
| "why a systemd timer and not cron" | 5 | 2.77 s |
| "lease epoch fencing" | 5 | 0.38 s |

The sealed set here is `search.sqlite3` (33 MB) plus `vectors.json` and
`vectors.npy` (6 MB). Individual seals ranged 0.037–2.058 s; the spread within
one artifact set is machine load, not size. The generation directory as a whole
is 259 MB, so a query whose artifact set includes `evidence.sqlite3` (207 MB)
pays proportionally more.

A generation is immutable after activation — a contract, not an accident — so
these four or five verdicts about the same bytes cannot differ.

### Why NEW-69's key does not work here

`NEW-69` closed the same shape in the same file: generation *validation* ran
five times per query and cost 9.75 s of a 10 s budget. The fix memoised the
verdict per process, keyed by the generation id, the manifest digest and the
verified artifact digests, and recorded the principle that matters: **the key is
earned by hashing, not assumed.**

That key is not available here, and the reason is structural rather than
incidental. In `NEW-69` the digests were a by-product the caller had already
paid for, and the cached thing was an expensive verdict *about* them — a walk
over 6240 rows re-deriving expected content. Here the digest **is** the work.
Keying a hash cache by the hash it is about to compute is circular: earning the
key costs exactly what the cache was meant to save.

So the question is not whether to reuse NEW-69's key. It is whether any key
exists that can be had for free and still means something.

### What the practice says

One does, and it is the oldest answer in the file-tools tradition: the
artifact's stat identity, which this seal's own return value already carries as
`(st_dev, st_ino, st_mode, st_size, st_mtime_ns, st_ctime_ns, checksum)`.

**Git.** Git's index stores cached stat data precisely so that content need not
be re-hashed on every status. The known hole is *resolution*, not principle: the
"racy git" problem exists because a file can be modified within the same second
as the stamp already recorded, so the cached hash can no longer be trusted for
that file. Git's answer is not to abandon the stat check but to bound it — an
entry whose mtime is not older than the index's own write time is treated as
"racily clean" and its content is compared. `core.trustctime` exists for the
opposite reason: ctime is *so* reliable a change signal that crawlers and backup
tools touching it cause false dirt.

**The kernel.** POSIX and the Single Unix Specification deliberately provide no
way for userspace to set ctime, because changing the inode's timestamps is
itself an inode change that must update ctime. `utimensat(2)` sets atime and
mtime and the kernel stamps ctime with the current time regardless. So a writer
that modifies content and then forges mtime back has still moved ctime, and
cannot move it back.

This is what makes stat identity stronger here than the weak "size and mtime"
check the quick-check tradition is usually criticised for. The seal compares six
fields including `st_ino` (a replaced file is a new inode) and `st_ctime_ns` (a
forged mtime is still caught).

### What was decided

`_sealed_file` keeps one bounded process-level observation: the checksum it
earned, keyed by the path, the full six-field stat identity, and the manifest
descriptor it was checked against. A later seal of the same path lstat()s the
file — microseconds — and reuses the checksum only on an exact key match.
Anything else re-hashes and therefore still fails the comparison that raises
`GenerationSealChanged`.

The residual risk is exactly git's, and it is handled explicitly rather than
inherited. A second same-size write landing in the same clock tick as the one
that was hashed would carry the same stamp. Linux stamps inodes from the coarse
clock, one jiffy wide — 10 ms at the lowest supported `CONFIG_HZ` — so an
observation is kept only once the artifact's stamps are older than a 20 ms
settle margin. Git infers this window from the index's own write time; naming
the tick is the same rule with the constant made visible.

The manifest expectation is part of the key, so a seal taken against a
descriptor is never reused for one taken without it.

Rejected: keying by generation id and manifest digest alone. That trusts the
immutability contract instead of checking it, and the seal exists precisely to
catch the case where the contract was violated.

Rejected: computing the seal once per query and sharing the value with the
verification sites. That turns `_generation_consumption_unchanged` into a
comparison of a value against itself — it would always return true, and the
check would be removed rather than made cheaper.

### What it is worth

Instrumented on the same three queries, counting hashes rather than seals:

| | seals | artifacts hashed | seal time |
|---|---|---|---|
| «как устроен повтор…» | 5 | 4 | 0.04 s |
| "why a systemd timer…" | 4 | 0 | 0.00 s |
| "lease epoch fencing" | 4 | 0 | 0.00 s |

Each artifact is hashed once per process. The 0.34–2.77 s a query spent proving
what it already knew becomes 0.04 s on the first query and 0.00 s after.

Paired at the MCP tool boundary, arms interleaved in alternating fresh
processes, three rounds of 18 calls per tool per arm, load average 12–16 on
four cores:

| round | tool | old over | old p50 | new over | new p50 |
|---|---|---|---|---|---|
| 1 | `recall` | 5/18 | 7.88 s | 2/18 | 6.81 s |
| 2 | `recall` | 3/18 | 8.02 s | 4/18 | 6.68 s |
| 3 | `recall` | 2/18 | 7.20 s | 2/18 | 6.33 s |
| 1 | `get_decisions` | 4/18 | 6.92 s | 2/18 | 6.34 s |
| 2 | `get_decisions` | 4/18 | 7.34 s | 3/18 | 6.41 s |
| 3 | `get_decisions` | 2/18 | 6.71 s | 2/18 | 5.89 s |

Every `new` p50 is below every `old` p50, in both tools, in all three rounds —
6.33–6.81 s against 7.20–8.02 s for `recall`, 5.89–6.41 s against 6.71–7.34 s
for `get_decisions`. That separation is clean.

The over-budget rate is not settled by this and is not claimed: 10/54 against
8/54 for `recall` and 10/54 against 7/54 for `get_decisions` is the right
direction at a sample size that cannot carry it. The prior note's finding still
holds — the calls that miss the budget are the cold ones, where the cost is
mandatory work, and a cheaper seal does not make a model load fit.

### What the quality stands say, and what they could not say today

The retrieval stand passed on the live vault: `hit@1` 0.4, `hit@5` 0.8 against a
`grep` baseline of 0.0, gate 0.6, gates passed.

A first application-stand run reported `applied@5` 0.4286 and a failed gate. That
number was load, not the change, and saying so required measuring rather than
arguing — the seal change is an integrity check and not a ranking input, but it
can still reach the answer indirectly by leaving more budget for optional stages.
That run was taken at load average ~15 with the paired timing measurement running
beside it, and its companion retrieval run reported nine of ten cases
`budget_degraded`.

So both stands were run paired, cache on against cache off, in one process,
against identical stand code and the same active generation:

| arm | `hit@1` | `hit@5` | gate | `applied@5` | `grep` | gate |
|---|---|---|---|---|---|---|
| cache off (pre-change) | 0.4 | **0.7** | pass | **0.8571** | 0.1429 | pass |
| cache on (the change) | 0.3 | **0.7** | pass | **0.8571** | 0.1429 | pass |

`hit@5` and `applied@5` are identical, both gates pass on both sides, and the
application stand misses the same single case (`clear-capture-counters`) either
way. `applied@5` 0.8571 is exactly what `75f842d` recorded. `hit@1` differs by
one case in ten, which this vault has already recorded as inside the run-to-run
wander of these stands.

Worth noting for whoever reads a stand number next: the two stands are sensitive
to machine load in a way their reports do not make obvious. The same code gave
`applied@5` 0.4286 under load and 0.8571 twice on a quieter machine. A stand
number taken while something else is running is not evidence.

## Part 2 — why `get_decisions` cannot degrade inside the caller's budget

`recall` catches `TimeoutError` and re-runs the search lexical-only;
`get_decisions` raises. The obvious repair is to give `get_decisions` the same
fallback. Measurement says that repair is theatre, and names what would have to
change instead.

### The fallback is entered after the money is gone

Instrumented at `search_memory.search`, recording the budget remaining when each
attempt began, over 36 real MCP calls at a 10 s operation budget:

| tool | over budget | p50 | fallback entered with |
|---|---|---|---|
| `recall` | 1/18 | 6.97 s | **−0.014 s** |
| `get_decisions` | 1/18 | 7.03 s | no fallback exists |

An earlier run caught the same thing at **−0.389 s**, where the second attempt
ran for 0.000 s and re-raised. This is structural: `_check_stopped` raises when
`time.monotonic() >= deadline`, so by construction the fallback begins after the
deadline has passed. `recall`'s fallback cannot produce an answer, and giving
`get_decisions` a copy of it would give it the same non-answer, more slowly.

### A reserve cannot create the room either

The natural upstream fix is the gRPC rule already cited in this vault's own
research: deduct a buffer for your own remaining work before propagating a
deadline down. Run the primary attempt against `deadline − reserve` so the
fallback has real room inside the caller's budget.

Measured, that reserve does not fit. A lexical-only pass —
`semantic=False, graph=False, rerank=False`, the exact shape the fallback runs —
costs:

| | p50 | p90 | max |
|---|---|---|---|
| warm, n=12 | 3.11 s | 3.50 s | 5.23 s |
| cold, n=6 | — | — | 2.80–13.28 s |

Reserving the warm p90 would leave the primary 6.5 s of a 10 s budget against a
measured p50 of 6.97–7.03 s. The reserve would cause the degradation it exists
to cushion, and would still not cover the cold case.

The deeper reason is that **the fallback repeats precisely the work that
overran.** The prior note measured the calls that miss the budget spending
2.0–6.1 s validating the generation catalog and 0.9–6.4 s in the mandatory
lexical leg. A lexical-only retry pays those again. No reserve can be smaller
than the thing it must contain.

### What would have to change

The prior note already records the fact that makes this solvable: *"The lexical
leg had already completed and its result was in hand; the optional stages then
ran the clock out and a later mandatory `_check_stopped` threw it away."*

The fix is therefore not a second attempt but a preserved first one — carrying
the completed legs out of `retrieval.retrieve()` when the deadline expires,
instead of discarding them and recomputing. That costs no budget at all,
respects the caller's deadline absolutely because it adds no work, and repairs
`recall` and `get_decisions` together.

It is not a small change and is deliberately not attempted here. `retrieve()`
has seventeen `_check_stopped` sites whose entire purpose is to abandon
in-flight work; making them yield partial results instead is a change to the
core fusion pipeline, needs its own design, and must be re-measured against the
retrieval and application stands rather than against latency alone.

Sources consulted 2026-08-29: git racy-index documentation and index format
(git-scm.com/docs/racy-git, git-scm.com/docs/index-format), `core.trustctime`
rationale; `utime(2)` and `utimensat(2)` Linux manual pages on ctime being set
by the kernel and not settable by userspace; linux-kernel discussion of ctime
semantics and of false `-dirty` detection from ctime in `setlocalversion`.
