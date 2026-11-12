# LSP process-tree ownership

`scripts/lsp_process_tree.py` owns one launched process tree, its process-group or
Windows Job Object membership, descriptor inheritance, termination and cleanup.
It uses only the standard library. Public import paths, exceptions, deadlines,
process evidence rules and native flags remain unchanged. Callers retain their
own LSP protocol, lifecycle and recovery responsibilities.

POSIX descendants are contained only while they stay in the assigned process
group; hostile setsid escape is unsupported. Unknown or unreadable process
metadata cannot prove the group empty. Windows cleanup preserves Job and process
handle ownership until the existing terminal conditions are met. No daemon,
new runtime directory or operating-system sandbox is introduced.

The complete focused process-tree suite and pure proc-evidence regressions belong
to this packet. `tests/fake_process_tree.py` owns shared process-only subprocess
behavior; the fake LSP server calls the same helpers. Its protocol/semantic modes
belong to their own future packet. Mixed LSP lifecycle regression tests remain in
their original suite. No old assertion or subprocess behavior is removed.

The manifest includes full instructions, pytest bootstrap, implementation, tests,
fixture, source rationale and measurement code. Source-closure, owner-guide,
standard-library import and actual isolated-import guards apply. Windows skips
on Linux remain unqualified until native CI executes them. Python 3.14 is now
the compatibility floor. Run `pytest tests/modules/lsp_process_tree
 tests/test_lsp_process_tree.py -q` and the dependent LSP integration suites.

The Linux test observer accepts ENOENT and ESRCH as process disappearance. A
process may be reaped between opening and reading its procfs stat file: an open
procfs descriptor does not keep the task alive. The observer uses one reader for
both the orphan-settle loop and the final liveness assertion, and propagates
permission and IO failures. The regression opens a real child's stat descriptor,
reaps that child, then reads the retained descriptor; no timing delay is needed.
Production evidence readers keep their conservative unknown result on unreadable
metadata. This test correction changes no process ownership or cleanup policy.
Evidence: Linux CI job 126085901978 at 09922834 and the
[Linux procfs descriptor contract](https://www.kernel.org/doc/html/v6.8/filesystems/proc.html#process-specific-subdirectories).


## Preserved structure reference — 2026-11-03

The process-tree qualification now separates its shared test subprocess fixture
from fake LSP protocol/semantic behavior; production `lsp_process_tree` stays in
place. The process-only proc-scanning regressions move intact to the owner suite.
See `docs/research/2026-11-03-lsp-process-tree-packet.md`; this boundary is covered
by the approved autonomous decomposition mandate. Its complete packet passes the
budget and focused Linux checks; native platform CI remains pending.
