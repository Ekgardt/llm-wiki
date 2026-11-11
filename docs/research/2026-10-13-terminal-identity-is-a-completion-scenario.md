# Terminal identity is a completion scenario

Date: 2026-10-13. Scope: one LSP identity-preservation fixture.

CI38336899409 Windows Python3.13 shard5 failed the first-terminal-identity
scenario with `LSP background cleanup failed (TimeoutError)` after 2.25 seconds.
It inherited the product's two-second autonomous cleanup allowance while
requiring cleanup to finish before inspecting the first failure's identity.
The same original scenario passed locally in 2.73 seconds. That local pass
does not qualify the Windows failure or identify its exclusive slow phase.

A causal regression executes the original scenario with real processes and
real evidence publication. After publishing evidence it debits the cleanup
deadline by twice the original allowance, simulating longer publication.
The original scenario fails with the same background-cleanup TimeoutError.
No sleep, synthetic successful evidence or swallowed exception is used.
This logical duration injection is not a latency measurement and does not
prove that evidence publication was the slow Windows phase. A first diagnostic
had its positional fixture arguments reversed; its TypeError is not counted
as causal evidence. The corrected regression detects the behavioral failure.

Only this completion fixture selects the existing LONG_TIMEOUT for its
autonomous cleanup and kept caller deadlines. It retains the injected real
heartbeat failure and every assertion on first code, generation nonce and pid.
The regression then passes. Finally it restores normal evidence publication
and closes any fixture process so a failing regression does not leak an owner.
The product's two-second allowance, error retention and caller deadline
behavior remain unchanged. The related short-deadline contention test still
requires the background TimeoutError to be reported. Actual Windows
qualification is pending the next full matrix.

Changing production allowance without resource evidence, hiding the background
error, skipping the case, weakening identity assertions or retrying until green
would not resolve this fixture's mixed responsibilities. The existing slow
machine policy already requires completion deadlines to use LONG_TIMEOUT;
this introduces no new numeric limit or runtime contract.

Primary sources checked on this date:
- CPython's distinction between a completion/hang bound and a speed assertion:
  https://docs.python.org/3/library/test.html#test.support.LONG_TIMEOUT
- pytest's discussion of uncontrolled state and overly strict timing:
  https://docs.pytest.org/en/stable/explanation/flaky.html
- Microsoft thread scheduling and creation semantics:
  https://learn.microsoft.com/en-us/windows/win32/procthread/creating-threads

Sources in the project: `tests/slow_machine.py`, the original scenario in
`tests/test_lsp_process.py`, `_drain_failure_intents`, `_drive_terminal_cleanup`,
`_remember_background_cleanup_error` and `_raise_background_cleanup_error` in
`scripts/lsp_process.py`. No production LSP implementation is changed.
