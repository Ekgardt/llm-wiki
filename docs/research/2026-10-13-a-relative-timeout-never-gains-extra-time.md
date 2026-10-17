# A relative timeout never gains extra time

Date: 2026-10-13. Scope: dependency synchronization's existing budget.

CI38329687452 Windows Python3.10 shard2 failed the unchanged assertion that
`uv` receives no more than the default30seconds. It received
30.000000000000455. The default action formed an absolute floating-point
deadline and subtracted the clock. At monotonic4095.02, adding and subtracting
30 reproduces the exact excess locally. Running the original dependency
scenario with that clock reproduces its original strict assertion failure.

The default relative budget now bounds the computed remainder from above.
An explicitly supplied whole-action deadline retains its existing precedence:
it is not silently shortened to the dependency default. The same expired
budget refusal remains. Command dispatch observes the positive remainder and
passes that value without the previous arbitrary one-millisecond floor; a
500-microsecond remainder previously grew to1000microseconds. A second causal
regression fails on that original behavior. No tolerance is added to tests,
no timeout is rounded upward, and no dependency deadline is extended.

The obsolete private absolute-deadline helper is replaced by the one clock
constructor that distinguishes the existing relative-default and explicit
absolute-deadline contracts. There are no external callers of the old helper.
The production30-second value and its separately outstanding historical basis
are unchanged; this corrects enforcing a supplied bound, not its rationale.

Alternatives rejected: weakening the assertion to approximate equality would
hide extension of a hard upper bound; decimal or nanosecond time throughout
this pipeline would be a broader rewrite; clamping explicit caller deadlines
to30seconds would introduce a new restriction. Clamping only the default
relative allowance is the direct arithmetic correction.

Primary sources consulted:
- CPython binary floating-point representation and rounding:
  https://docs.python.org/3/tutorial/floatingpoint.html
- Microsoft high-resolution timestamps and precision:
  https://learn.microsoft.com/en-us/windows/win32/sysinfo/acquiring-high-resolution-time-stamps
- Goldberg's original floating-point analysis, hosted by Oracle:
  https://docs.oracle.com/cd/E19957-01/806-3568/ncg_goldberg.html

Qualification covers the exact original failure, sub-millisecond remainder,
expired refusal without starting a command, explicit larger caller budget,
shared deadline shrinkage and dependency process cleanup. Full CI is required
before installation. No setting, environment contract, path or dependency is
added.
