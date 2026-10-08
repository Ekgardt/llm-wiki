# Release persisted graph working data before vector encoding

Research date: 2026-10-08. This is a Python object-lifetime correction within
existing generation builders, not a new runtime layout or an encoder change.

## Observed problem

The installed generation process was killed by the kernel twice for machine-wide
memory exhaustion. The second attempt had written approximately 97% of vector
rows when last sampled; it never published an active generation. Its process used
about 3 GiB while running ONNX, alongside a resident MCP process and other users
of the machine. The initial free-memory check did not guarantee completion.

Separately, a hermetic real generation build with 1,000 sources observed retained
Python graph working data at the vector boundary: 3,888,939 bytes on extraction
and 4,210,572 bytes on parent reuse. These are measurements of that fixture's
reachable objects, not an attribution of the installed process's total RSS.
The full builder retained its materialized rows after SQLite persistence. The
incremental caller additionally retained the merged dictionaries and extractor.
Three regression scenarios reproduce those references remaining alive, using
weak references at the actual pre-vector boundary: full, incremental extraction,
and incremental parent reuse. The original full test's first run had missing
required fixture arguments; only the corrected isolated run is causal evidence.

Local evidence (private):
- `logs/step7-qualified-resources-generation-kernel-exit-20261008.txt`
- `logs/step7-graph-object-lifetime-diagnostic-20261008.json`
- `/dev/shm/step7-graph-rows-lifetime-causal-red-isolated-20261008.xml`

## Primary sources and alternatives

Three independent primary sources checked on the research date:

1. [Python 3.10 object lifetime](https://docs.python.org/3.10/reference/datamodel.html):
   reachable objects cannot be collected; CPython uses reference counting but
   immediate finalization is not a portable resource-management guarantee.
2. [SQLite memory ownership](https://www.sqlite.org/malloc.html): SQLite manages
   its own caches and allocations; application-owned objects remain the
   application's responsibility. Changing SQLite's allocator cannot release
   Python containers the caller still owns.
3. [ONNX Runtime memory tuning](https://onnxruntime.ai/docs/performance/tune-performance/memory.html):
   shared arenas and alternative allocators address runtime allocations. They
   do not justify changing model semantics or dropping necessary source context.

Chosen correction: stop retaining *owned disposable graph working containers*
once their complete validated rows have been persisted. The incremental builder
transfers its local merged families through exhausting iterators and releases its
extraction and ownership bookkeeping before entering the full builder. The full
builder releases its materialized rows and consumed input references after the
SQLite write. Caller-owned input collections must remain unchanged.

Alternatives considered: forcing garbage collection leaves reachable containers
alive; shrinking the source corpus or model context changes quality; switching
allocators introduces a platform dependency without addressing these references;
rewriting extraction as a new streaming database pipeline changes more contracts
than necessary. None is required for this object-lifetime correction.

No model, schema, source selection, citation, batch size, deadline, or publication
check is changed. Graph and search artifacts, cancellation, failure cleanup,
parent reuse, semantic validation, and atomic publication must remain correct.
The fixture's released objects do not establish that machine-wide OOM is solved.
A complete installed run and full useful compile remain required for point 7.

## Qualification so far

The corrected isolated Python 3.10 causal run fails all three scenarios on the
previous builder and passes all three on the correction. Related generation,
recovery, snapshot, integration and catalog checks: 490 passed, 6 existing skips.
Ruff passes. Actual source AST plus Lizard measures all 13 changed/new Python
callables at CCN <= 5, at most two if statements and at most two nested control
levels. The new test file's shard weight comes from its successful JUnit timings.

Repeating the same 1,000-source lifetime diagnostic after the correction observes
567 bytes (four container objects) in both cases; source/graph table comparisons
pass. This is an ownership measurement, not a claim that total process RSS falls
by the same amount, or that all seven tables and all vectors have been paired on
the installed corpus. The public caller's inputs are not cleared or mutated.

The second OOM candidate was retired through the normal catalog operation after
proving its owner absent and finding no open process references. Artifact hashes
and kernel evidence remain. The active memory generation was unchanged.
