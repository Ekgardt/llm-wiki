# What an execution trace proves — and what it must never be allowed to claim

Date: 2026-08-28
Task: `CODE-09` (`ingest_traces`: execution traces into the graph)
Rule 2 note. Written before the design was implemented.

---

## 1. The measured problem this is for

Measured on this repository, active generation
`generation-18d015d5499fc78e-7d4cc1d8`, 2026-08-28:

| observation reason | edge type | rows |
|---|---|---|
| `dynamic_dispatch` | CALLS | **47,983** |
| `unresolved_reference` | CALLS | 22,669 |
| `missing_dependency` | CALLS | 3,152 |
| everything else | (various) | 1,952 |

Node counts in the same generation: 17,183 `function`, 3,563 `method`.

A `dynamic_dispatch` observation is the extractor saying, in the schema's own
words, *"a call happens here to something named `queue.recover_expired_leases`,
and I could not establish what the receiver is."* It is stored as an
`observation` row, never as a CALLS `assertion`, which is why
`find_callers("recover_expired_leases")` can honestly answer `callers: 0` while
`unresolved_caller_count` is non-zero (`NEW-124`).

Static analysis cannot close this class by getting better. Dynamic dispatch is
where the *program*, not the analyser, decides the target. The literature is
unambiguous that the gap is structural, not an implementation deficiency:
dynamic typing, reflection and runtime composition let the same symbol mean
different things in different executions, so "what appears fixed in source may,
in execution, be contingent"
([arXiv:2606.04990](https://arxiv.org/html/2606.04990v1)), and indirect control
transfers whose targets depend on runtime values simply stop static CFG
recovery ([arXiv:2605.29620](https://arxiv.org/abs/2605.29620)).

The standing answer in practice is hybrid: keep the static graph, and correlate
observed execution against it
([IN-COM](https://www.in-com.com/blog/advanced-call-graph-construction-in-languages-with-dynamic-dispatch/)),
which is exactly what this feature is.

## 2. What a trace proves

A recorded call edge `A → B` from a real run proves exactly one thing:

> **In this run, on this machine, with these inputs, `A` called `B` at least
> once.**

Everything else people want to read out of it is false:

- **It does not prove reachability in general.** One run is one path through the
  program. A different configuration, a different branch, a different platform
  reaches different targets.
- **Absence proves nothing at all.** A call not in the trace may be a call that
  the run never exercised. This is the asymmetry that decides the whole design:
  trace evidence can only ever *add* edges, never subtract or refute one. It
  must therefore never feed a dead-code answer, and never be allowed to turn
  `callers: N` into `callers: 0`.
- **It does not prove the static call site.** A trace says `A` called `B`; it
  does not say which line of `A` did it. Binding a trace edge back to a
  particular unresolved call site is an *inference* (matching the callee's name
  against the observation's `target_text`), and where `A` has two call sites to
  the same attribute name on different receivers, one trace edge is credited
  against both. The read path must say so rather than imply a 1:1 resolution.
- **It is not a static-analysis result and must not be stored as one.**

That last point is the one comparable tools get right and it is worth copying
deliberately. Graphify labels relationships `extracted`, `inferred` or
`ambiguous`, because "an agent should not present a resolved guess as if it were
written explicitly in the source"
([wavect](https://wavect.io/blog/graphify-review-codebase-knowledge-graph/)).
The provenance survey draws the same line as a schema distinction between
*static provenance* (the types of thing that may be asserted) and *dynamic
runtime provenance* (what a particular execution actually produced)
([arXiv:2606.04990](https://arxiv.org/html/2606.04990)). One of the search
results states the intended shape of this feature almost verbatim: record
unresolved edges honestly, and let "unresolved edges be enriched later by
runtime tracers without changing storage schema"
([gmap ADR 0007](https://github.com/Zenoguy/gmap/blob/main/documentation/adr/0007-call-graph-accuracy.md)).

This vault already has its own rule of the same shape, arrived at
independently: a zero must say which kind of zero it is (`NEW-124`,
`NEW-135`). An edge must likewise say how it was learned.

## 3. Candidate formats

### 3.1 `cProfile` / `pstats` — accepted, as the collection source

`cProfile` is a deterministic profiler in the standard library, C-implemented,
available on every Python this product supports (3.10+). What matters here is
not the timings but a detail of its data model: `pstats.Stats.stats` maps a
function key to a tuple whose last member is a **dict of its callers**, keyed by
the same kind of key
([pstats docs](https://docs.python.org/3.15/library/pstats.html),
[Zini](https://www.enricozini.org/blog/2019/python-profiling-data/)). So the
profile is already a caller→callee edge set with call counts. Each key is
`(filename, first line number, function name)`, and deterministic profilers
"provide exact call counts"
([pydevtools](https://pydevtools.com/handbook/reference/py-spy/)).

`(filename, first line number, function name)` is decisive, and it is why this
format wins. This vault's graph stores every function and method definition as
an `occurrence` row with `role='definition'` and a `line_start`. Measured on the
live generation: `scripts/code_graph.py::find_callers` is at `line_start` 1832,
and `def find_callers(` is on line 1832 of the file.

> **Corrected 2026-08-29.** This note originally claimed that CPython's
> `co_firstlineno` is the `def` line rather than the first decorator line. That
> is **false for decorated functions** — measured on Python 3.12.3, a function
> whose first decorator is on line 4 and whose `def` is on line 6 reports
> `co_firstlineno` 4. The original probe could not distinguish the two
> hypotheses. The consequence and the two-step binding rule that fixes it are in
> `docs/research/2026-08-29-what-a-profiler-frame-key-points-at.md`. The
> conclusion below survives: the join is still by location, not by name — it
> just needs a bounded decorator window on top of the exact match.

So a trace frame binds to a graph node by **(repository-relative path, definition
line, name)** — an exact structural join, not a name-similarity guess. That is a
materially stronger binding than the comparable verb offers: the
`codebase-memory-mcp` `ingest_traces` tool available in this very session takes
`{caller: str, callee: str, count: int}` — bare name strings, no file, no line,
no provenance field, and no stated bound. Names alone cannot distinguish the 296
`__init__` methods this repository's generation contains.

**But the on-disk `.prof` file is not the format we ingest.** `cProfile`
serialises with `marshal`
([Zini](https://www.enricozini.org/blog/2019/python-profiling-data/)), and
`marshal` is documented as not secure against maliciously constructed data. A
trace file is untrusted input by assumption. Unmarshalling one inside the
ingester would hand an attacker the interpreter.

The split that follows is the whole security design:

- a **collector** (`scripts/trace_collect.py`) reads a `.prof` the operator
  produced themselves, in their own process, from their own run, and writes a
  flat text edge list. Trusted input, by construction — the operator made it
  seconds earlier.
- an **ingester** (`scripts/trace_ingest.py`) reads *only* the flat text edge
  list, with `json.loads` per line and no deserialiser that can execute
  anything.

### 3.2 `sys.monitoring` (PEP 669) — rejected for now, on version grounds

PEP 669 is the right long-term collector: callback overhead "orders of
magnitude" below `sys.settrace` and much cheaper than `sys.setprofile`
([PEP 669](https://peps.python.org/pep-0669/)). Two reasons not to build on it
today. First, it is 3.12+, and this product's floor is 3.10 (`CROSS-07` was
precisely a 3.10 regression). Second, it is explicitly incompatible with
`sys.settrace`/`sys.setprofile` — they cannot both be active — so a
`sys.monitoring` collector would fight `coverage`, debuggers and `cProfile`
itself. The accepted file format is deliberately collector-agnostic, so a
`sys.monitoring` collector can be added later without changing the store or the
read path.

### 3.3 `sys.settrace` — rejected

Per-line callbacks into Python for every event. Order-of-magnitude slowdowns,
and the same mutual exclusion with coverage tooling. `cProfile` gets the same
call edges with a C callback.

### 3.4 OpenTelemetry spans — rejected, wrong granularity

Spans are operation-level (HTTP, gRPC, database), not function-level. There is
currently no mechanism to correlate profiling stack frames with active spans,
so "which functions were called during this span" is unanswerable without
manual per-function SDK instrumentation
([open-telemetry/opentelemetry-ebpf-instrumentation discussion
1715](https://github.com/open-telemetry/opentelemetry-ebpf-instrumentation/discussions/1715)).
Manually instrumenting every function to emit a span is a worse `cProfile`. And
this product has no service to trace: it is a local, no-daemon CLI and stdio MCP
server.

### 3.5 `py-spy` — rejected, statistically incomplete in the wrong direction

`py-spy` samples the stack periodically. The result is "a statistical
representation of where the application spends most of its time", and not all
function calls are captured; `--nonblocking` can additionally yield partial
stack frames
([pydevtools](https://pydevtools.com/handbook/reference/py-spy/),
[py-spy](https://github.com/benfred/py-spy)).

For a *profiler* that is a fine trade. For *this* feature it is fatal, and for a
specific reason: the entire value here is resolving a call the static extractor
missed. A sampling profiler misses cheap calls preferentially — and a call
through a dynamically-dispatched attribute is very often cheap. Sampling would
systematically miss the exact population we are trying to observe, while
producing a file that looks just as authoritative. Rejected.

## 4. Where the data may live

`CLAUDE.md` §1 settles this and leaves no room:

- A generation is **immutable after activation**, and query-time observations
  are explicitly not written into active generations. A trace is a runtime
  observation by definition, so it cannot be written into `evidence.sqlite3`.
- `cache/` is disposable derived state; `run/` is operational state under a
  deletion contract; Markdown, Git and project journals are the only authority.

Therefore: a sidecar store under `cache/execution-traces/`, sitting beside
`cache/evidence-graph/` under the same state root, disposable, regenerable by
re-ingesting, and read *alongside* the active generation rather than merged into
it. Deleting it loses nothing but the traces, which is the correct blast radius
for derived evidence.

Trace edges are stored as resolved `node_id` pairs plus the `generation_id` they
were resolved against. Node identity in this schema is content-derived over
`(repository, language, path, owner, name, signature)` and contains no line
number, so a node id survives a symbol moving within its file, and a generation
rebuild that does not change the symbol. When a stored edge names a node that no
longer exists — the signature changed, the file was deleted — the read path
drops it and **counts it** as stale rather than silently serving it or silently
losing it.

## 5. Decision

**Accept** a line-oriented JSON text format, `execution-trace/v1`: one header
object declaring the schema, then one object per line of the form
`{"caller": {"path", "line", "name"}, "callee": {...}, "count": n}` with
repository-relative paths.

**Collect** it with `cProfile` + `pstats`, via a separate collector the operator
runs on their own profile.

**Store** it in a disposable sidecar under `cache/execution-traces/`, keyed by
the SHA-256 of the trace file so that re-ingesting the same trace replaces
rather than accumulates.

**Serve** it in a field of its own — `trace_callers`, never `callers` — with the
call count and the trace digest that carries it, so that a reader can always
tell which edges were read out of the source and which were watched happening.

## 6. What this design deliberately does not do

- It does not write into a generation, ever.
- It does not remove, contradict or downgrade a static assertion. Trace evidence
  is additive only, because absence in a trace means nothing.
- It does not feed dead-code analysis. `find_dead_code` asks a question of the
  form "does anything reach this", and a format whose absences are meaningless
  must not be allowed to answer it.
- It does not claim the trace resolves a *particular* call site. It says: this
  caller was observed calling this callee, and here are the unresolved call
  sites in that caller whose attribute name matches.

## Sources

- [PEP 669 – Low Impact Monitoring for CPython](https://peps.python.org/pep-0669/)
- [pstats — Statistics for profilers](https://docs.python.org/3.15/library/pstats.html)
- [Python profilers — Python documentation](https://docs.python.org/3.15/library/profiling.html)
- [Enrico Zini, Python profiling data](https://www.enricozini.org/blog/2019/python-profiling-data/)
- [py-spy: Sampling Profiler for Python](https://pydevtools.com/handbook/reference/py-spy/)
- [benfred/py-spy](https://github.com/benfred/py-spy)
- [OpenTelemetry eBPF instrumentation, discussion 1715: per-span call graph construction](https://github.com/open-telemetry/opentelemetry-ebpf-instrumentation/discussions/1715)
- [arXiv:2606.04990 — From Agent Traces to Trust: Evidence Tracing and Execution Provenance](https://arxiv.org/html/2606.04990v1)
- [arXiv:2605.29620 — Control Flow Graph Recovery for Dynamically Loaded Code](https://arxiv.org/abs/2605.29620)
- [Advanced Call Graph Construction in Languages with Dynamic Dispatch](https://www.in-com.com/blog/advanced-call-graph-construction-in-languages-with-dynamic-dispatch/)
- [gmap ADR 0007 — call graph accuracy](https://github.com/Zenoguy/gmap/blob/main/documentation/adr/0007-call-graph-accuracy.md)
- [Graphify review — edge provenance labels](https://wavect.io/blog/graphify-review-codebase-knowledge-graph/)
