# What a profiler frame key actually points at

Date: 2026-08-29
Task: `CODE-09`, correcting a measured claim in
`docs/research/2026-08-28-what-an-execution-trace-proves.md`.

## The claim that was wrong

The 2026-08-28 note asserted, and the first version of `scripts/trace_ingest.py`
relied on:

> CPython's `co_firstlineno` — which is what `cProfile` keys on — is the `def`
> line, not the first decorator line.

**That is false for decorated functions.** The probe that appeared to support it
did not actually establish the line numbers it claimed; it read
`__wrapped__.__code__`, which is the *same* code object, so it could not
distinguish the two hypotheses at all.

## The measurement

`/tmp/code09/probe/sample_probe.py`, line numbers known by construction
(`@deco` on 4 and 5, `def target` on 6; `class K` on 9, `@deco` on 10,
`def m` on 11; `def plain` on 14). Python 3.12.3:

```
target co_firstlineno: 4     <- first decorator, not the def on 6
K.m    co_firstlineno: 10    <- the decorator, not the def on 11
plain  co_firstlineno: 14    <- undecorated: the def line
```

So a `cProfile` key for a decorated function points at its **first decorator
line**, and the offset from the `def` line is the number of decorator lines.
This vault's Evidence Graph stores the `def` line as the definition
occurrence's `line_start` (measured: `scripts/code_graph.py::find_callers` is
`line_start` 1832, and `def find_callers(` is on line 1832).

For undecorated functions the two coincide, which is why the exact join worked
at all and why the error was easy to miss.

## What it cost, measured

On the 2026-08-28 trace of ten test modules (15,271 edges), binding by exact
`(path, line, name)` left 21,301 unbound frame occurrences. 20,291 of those are
`.venv` — third-party code that is deliberately not indexed and can never bind,
which is correct. But **1,010 were in-repository frames** (`scripts` 588,
`tests` 422), and the top names show the decorator effect directly:
`test_manifest_schema_rejects_invalid_repository_scope` failed to bind 22 times
while sitting at line 732 in both the graph and a byte-identical working tree.
Its `def` carries `@pytest.mark.parametrize`.

The rest of the in-repository misses are code objects with no graph node at all
and should not bind: `<lambda>` (178), `<genexpr>` (152), `<module>` (43).

## The rule this forces

Bind in two steps, exact first:

1. **Exact** `(path, line, name)`. Measured on this generation, this key is
   unique across all 20,746 function and method definitions — zero collisions —
   so an exact hit is unambiguous and needs no tie-break.
2. **Decorator window.** Otherwise take candidates with the same `(path, name)`
   whose `def` line lies in `[frame_line, frame_line + MAX_DECORATOR_LINES]`,
   and bind only when exactly one candidate qualifies. A frame that matches two
   is left unbound rather than guessed.

The window is bounded and the bound is named. Real decorator stacks in this
repository are short; the window exists to absorb them, not to search. Binding
must stay a *join*, not a nearest-neighbour heuristic — a wrong caller is worse
than an unbound frame, because an unbound frame is visibly missing while a
wrong one is silently believed.

Note the direction: a decorator line is always **less than or equal to** the
`def` line, never greater, so the window opens forwards from the frame line
only. Searching backwards would let a frame bind to a definition that ends
before it starts.

## Sources

- Measured directly on this machine, CPython 3.12.3 (`/tmp/code09/probe/`),
  and against the live generation `generation-18d015d5499fc78e-7d4cc1d8`.
- [pstats — Statistics for profilers](https://docs.python.org/3.15/library/pstats.html)
  for the `(filename, line number, function name)` key shape.
- [Python profilers](https://docs.python.org/3.15/library/profiling.html).
