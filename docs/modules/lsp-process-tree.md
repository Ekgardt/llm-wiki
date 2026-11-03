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
