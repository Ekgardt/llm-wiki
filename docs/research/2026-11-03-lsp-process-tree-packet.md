# Process-tree owner and its independent process fixture

2026-11-03; approved autonomous boundary mandate; baseline 058f5710.
The documentation decomposition reduces the complete existing process-tree source,
focused tests and full fake LSP fixture to 99122 tokens before the owner guide,
manifest, boundary guards and unread-proc regression. It still cannot be qualified
by discarding those requirements. Production lsp_process_tree stays byte-identical.

Extract the existing eight descendant/signal/PID helper bodies into
`tests/fake_process_tree.py`, sharing them with fake_lsp_server. Share the existing
process-tree argparse options through one function. The small fixture executable
supports these process-only flags; the original fake server keeps every semantic,
protocol and lifecycle flag and dispatch order. Both use the same descendant
implementation. The process-tree tests use this process fixture instead of loading
an unrelated LSP semantic server. Preserve every assertion and control-flow body.
Move the two pure proc-scanning regression tests and their _Entry fixture from the
mixed LSP regression file into the new owner's tests, unchanged; lifecycle and
descriptor tests remain in the mixed file. Include all applicable tests and their
real fixture dependencies in the measured packet.

Alternatives: omit the fixture or count only selected text (incomplete context),
copy its process behavior (two implementations), split the cohesive production
process-tree owner (adds coupling), or qualify just below the ceiling without
its remaining context (false success). Choose shared test fixture ownership and
unchanged production. Standard Python script/package import modes both remain
supported; no sys.path injection, third-party dependency or new resource limit.

Primary sources checked 2026-11-03:
- Python subprocess defines Popen lifetime, descriptor and platform behavior:
  https://docs.python.org/3.10/library/subprocess.html
- Microsoft Job Objects describes grouped child ownership and breakaway semantics:
  https://learn.microsoft.com/en-us/windows/win32/procthread/job-objects
- Martin Fowler's Bounded Context describes coherent responsibilities and explicit
  relations: https://martinfowler.com/bliki/BoundedContext.html
This is an application of established separation, not a new platform mechanism.
Python 3.10 compatibility is retained although upstream now marks it unsupported.
POSIX setsid escape remains unsupported; Windows skips are not native validation.

Qualification will compare AST bodies and all original assertion nodes, exercise
both entry points, run process/protocol/lifecycle consumers and module guards,
measure the fully serialized packet with exact round-trip, and run Python 3.10.
Whole-project completion and paired task-cycle savings remain unproven.

Fresh built-in graph: generation-18e4696d1c828aca-b8fcc882 at058f5710;
1838 sources/7135 chunks. The helper has one direct caller in fake_lsp_server;
proc scanning has the production scan and two targeted regression callers.
Direct source inspection supplements incomplete class/CLI graph edges: LSP tests
execute the fake server by absolute path from temporary working directories and
protocol tests import it as tests.fake_lsp_server. Both import modes must work.
Baseline:27 passed/29 platform skips. Evidence: logs/lsp-process-tree-packet-graph.json
and logs/lsp-process-tree-packet-baseline.txt. Live HEAD remains e95f6a4a.

Integration found a real fixture packaging dependency: the descriptor/path-swap
regression copied only fake_lsp_server.py, so its inherited descriptor now lacked
the shared helper module. Focused reproduction fails with protocol EOF; a raw
copied-script probe reports ModuleNotFoundError for fake_process_tree. Fix the
fixture package, not the production loader or security check: create one stdlib
executable ZIP with __main__.py and fake_process_tree.py from their actual source
bytes. The same descriptor remains open across replacement, and the original
initialization assertion remains intact. No sys.path or environment bypass.
Python documents self-contained application archives with all dependencies:
https://docs.python.org/3.10/library/zipapp.html (checked 2026-11-03).

Qualification so far: 30 focused checks passed and29 native platform cases were
skipped, on both Python 3.12 and3.10. The descriptor regression passed separately
on both interpreters after reproducing and repairing its incomplete fixture
package. The first integration run had557 passes/46 skips and only that known
failure; its complete repeat is pending. All1069 module/integration guards passed,
2 skipped. Ruff and actual CCN/shape checks pass. All original assertion ASTs
remain, the eight helper bodies are exact, and production source is byte-identical.
46 emitted packets round-trip exactly; process-tree is94484 tokens before this
evidence paragraph, and the largest existing packet is96132. Ownership361/3095
files,2734 unassigned: this is another module, not whole-project completion.

The complete integration repeat finished:558 passed,46 platform skips. Checkpoint
6780ef23 is committed/pushed; its full regression is now running separately in a
pinned worktree. No full pass for that checkpoint is claimed yet.
