# Bounding non-cooperative code-graph work at the MCP boundary

Date: 2026-08-27. Written for the fix that makes `find_dead_code` and
`get_architecture` honour their operation deadline.

## The measured defect

On the live vault, both tools fell through to `code_graph`'s live extraction
because opening the active Evidence Graph raised
`PermissionError: active Evidence Graph changed while opening` (swallowed to
`None` by `_active_evidence_graph`, 2.7–4.0 s). The live path then called
`import_resolver.build_python_symbol_registry`, which ast-parses every `.py`
file under the directory and retains every tree: 7,545 files on this vault
(7,215 of them inside `.claude/` agent worktrees), ~1.5 GiB RSS per 1,000
files, ~86 s to reach 4,000 files, then the OOM killer ends the process
(exit 137) — a silent multi-minute hang with no output, exactly as observed
(9 minutes, killed externally). Neither `code_graph.find_dead_code` nor
`code_graph.get_architecture` accepts a deadline, so
`mcp_server._call_with_deadline` — which forwards deadlines only to helpers
whose signature accepts them — ran them unbounded.

## What current practice says

- **Deadlines must propagate to every step.** gRPC converts caller timeouts
  to absolute deadlines and forwards the remaining budget across every hop so
  no step works on a request the caller has abandoned; a downstream deadline
  must never exceed the upstream remainder ([gRPC deadlines
  guide](https://grpc.io/docs/guides/deadlines/)). Google SRE names deadline
  propagation a defence against cascading failures: without it, servers burn
  resources on abandoned requests. This fix propagates the one operation
  deadline into every step it controls.
- **A running thread cannot be cancelled from outside.** CPython offers no
  safe way to kill a thread; `concurrent.futures.Future.cancel()` refuses
  once the callable has started ([cpython
  discussion](https://github.com/python/cpython/issues/130975), [Cancellation
  in Concurrency,
  Eckel](https://bruceeckel.substack.com/p/cancellation-in-concurrency)).
  Cancellation is cooperative: the work must check a flag or deadline itself.
  `code_graph`'s live extraction checks nothing, so the only honest options
  are (a) make it cooperative, (b) run it somewhere killable, or (c) abandon
  it and answer the caller anyway.

## The decision

Wrap the two non-cooperative `code_graph` calls in an abandonable daemon
worker inside `mcp_server.py` (`_bounded_code_graph_call`): the caller waits
only until the deadline and then receives a bounded, named result
(`status: "timeout"`, what completed, what was skipped). A semaphore of 2
slots (`CODE_GRAPH_WORK_SLOTS`) refuses new live work by name while abandoned
runs still occupy their workers, so repeated timeouts cannot stack runaway
threads. Missing `directory` now reuses the existing named refusal
(`"directory is required"`) instead of a bare `KeyError`.

## Alternatives and why they lost

- **Cooperative deadline checks inside `code_graph.py`** — the correct
  long-term shape (it is what gRPC-style propagation wants), but the dominant
  cost sits inside `import_resolver.build_python_symbol_registry`, which
  builds its whole result before returning; per-file checks in
  `code_graph.py` cannot interrupt it mid-build. `code_graph.py` also carries
  58 managed-gate findings and `import_resolver.py` is outside this task's
  file set; the decomposition would dwarf the fix.
- **A killable subprocess** — processes are the unit the OS can actually
  stop, and killing the child would also stop the memory growth. It lost
  because the pinned contract in `tests/test_mcp_server.py`
  (`test_code_tool_helpers_forward_store_report` and neighbours) requires
  `mcp_server._find_dead_code` to call `code_graph.find_dead_code`
  in-process with `live`/`with_report`, and because a JSON round-trip plus
  spawn adds a second failure surface to a 10 s budget.
- **Refusing live extraction outright on large workspaces** — a policy about
  what the product may analyse, not a boundary fix; it would also break the
  legitimate fast case (small repositories, all tests).

## Honest residual

An abandoned live extraction keeps allocating until it finishes or the
process dies; on this vault a single run can still reach the OOM killer. The
deadline boundary makes the caller whole and caps concurrency, but the
memory-unbounded registry build in `import_resolver.py` remains the root
cause and is recorded here as out of scope for this fix.
