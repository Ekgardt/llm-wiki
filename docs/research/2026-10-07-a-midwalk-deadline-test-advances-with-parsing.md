# A mid-walk deadline test advances with parsing

Checked on 2026-10-07. The original Windows failure observed zero parsed files
before a 50 ms deadline. Production correctly stops an already expired walk;
the fixture incorrectly assumed real scheduling would permit a first parse.

A controlled module-local schedule reproduced that assertion failure without
sleeping or changing the global time module. The corrected test advances its
local monotonic clock with actual parsing progress. Directory enumeration cannot
consume that logical deadline; the twentieth parse reaches it, and the existing
walk must stop before the next parse. The original `0 < parsed < 400` assertion
remains, with exact `parsed == 20` and unchanged global-clock identity added.
Twenty is the existing cancellation fixture's interruption point, not a new
production time allowance or limit.

A negative control that ignores the deadline fails with `DID NOT RAISE`. Thus the
test still requires the real stop check; it does not inject a fake timeout.
Production deadlines, directory checks, cancellation and registry extraction are
unchanged. The separate expired-before-first-parse test remains unchanged.

Rejected alternatives were longer real deadlines, sleeps, changing production,
and globally replacing `time.monotonic`. They either retain scheduling variance
or affect unrelated test/framework work. The test now covers deadline semantics
without claiming to measure real platform latency.

Three independent primary references checked on 2026-10-07:

- [Python monotonic clock](https://docs.python.org/3.10/library/time.html#time.monotonic): absolute references have no portable epoch; compare elapsed values.
- [pytest scoped monkeypatching](https://docs.pytest.org/en/stable/how-to/monkeypatch.html): replace the consuming module's attribute and restore it after the test.
- [Microsoft high-resolution timing](https://learn.microsoft.com/en-us/windows/win32/sysinfo/acquiring-high-resolution-time-stamps): clock measurement and scheduling are distinct concerns.

Actual CPython 3.10.20 related qualification passed 154 tests. The initial related
run retained the unrelated old provider-renderer branching-guard failure. Linux
controls do not constitute a native Windows run; the genuine Windows failure and
controlled original refusal are retained separately.
