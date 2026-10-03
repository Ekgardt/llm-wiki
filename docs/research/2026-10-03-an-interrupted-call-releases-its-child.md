# An interrupted call releases its child

Research and reproduction date: 2026-10-03. Python 3.10 support remains unchanged.
No new runtime paths, model/provider settings, environment contracts or budgets.

An installed manual compile was interrupted through its verified own pidfd.
The traceback reached the real Codex subprocess call; its incomplete response
and token usage are unknown, not zero. The observed attempt cost 740.814 seconds.
A nonblocking external stack sample was denied by the operating system. Neither
ptrace policy nor process permissions were relaxed. These facts do not establish
which earlier stage consumed the CPU or a historical orphan's identity.

Inspection showed that the shared process launcher cleaned the owned process
tree after TimeoutExpired only. KeyboardInterrupt, SystemExit or a communication
error could escape while the child remained live. Three regression cases spawn
a real POSIX Python child, synchronize through its ready line, inject the actual
exception type at communicate, and require the same exception object and a
terminated child. All three failed before the fix; teardown ended those test
children, so the red qualification did not leave running children.

The launcher now applies its existing tree-kill and pipe-drain cleanup to an
interrupted communication before re-raising the original exception. An unproven
cleanup stays attached as cleanup_error and is named on stderr. A fourth test
checks that failure reporting preserves the original exception and ends the
real direct child through the existing fallback. It first found a missing sys
import; the corrected full group passed. These tests are not genuine Windows
event qualification or proof about a previously interrupted provider's usage.

Alternatives: retrying a model call after cancellation defeats caller intent and
can spend more tokens; killing only the wrapper can leave descendants; replacing
the original exception hides why the operation stopped; changing security policy
to obtain a stack dump is unnecessary. Reusing the existing platform cleanup
keeps timeout and cancellation ownership consistent without another launcher.
POSIX containment is still the owned process group of trusted CLI children; it
is not an OS sandbox and does not cover a hostile descendant that leaves it.

Primary sources checked on the research date:

- [Python 3.10 subprocess documentation](https://docs.python.org/3.10/library/subprocess.html#subprocess.Popen.communicate)
  describes communication, cleanup after timeout and the caller's responsibility
  when using Popen directly. Interruption must not be mistaken for child exit.
- [Linux kill(2)](https://man7.org/linux/man-pages/man2/kill.2.html)
  defines signal delivery to a process group and the relevant permission boundary.
- [Microsoft taskkill documentation](https://learn.microsoft.com/en-us/windows-server/administration/windows-commands/taskkill)
  defines tree termination with /T. The existing Windows path is reused; no new
  signal implementation or guarantee is introduced by these POSIX regressions.

Private audit logs retain failure and qualification evidence. This repair alone
does not prove completed compilation, fresh memory publication, answer quality
or full-cycle model token efficiency.
