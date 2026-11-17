# A test peer keeps its OS failure

Current status, 2026-09-30: the qualified source changes described here are installed. See [the installation evidence](2026-09-30-durable-capture-installation.md) for the final regression, live capture and nightly results. The checkpoints below retain their original dates and describe the state at that checkpoint; their pending-installation statements are historical. Installation does not close the remaining warning review, every native-event qualification, or the wider limit audit.

Date: 2026-09-30. Status: isolated correction tested; not installed.

## Reproduced cause

The whole-suite attempt recorded in `audit-2026-09-30-fixture-repair-full-next-failure.txt`
stopped after 489 passed and 2 skipped. An LSP peer could not send its progress
notification: `socketpair.sendall` raised `PermissionError` with errno `EPERM`.
`FakeLspServer._run_handler` suppressed every `OSError`, so the test saw only
its existing 30-second event timeout. A direct diagnostic through that same
peer recorded the actual send error and confirmed an empty failure list.

The native index was checked fresh at
`generation-18d9f1a4ef8cf06a-b10d4e19`. Graph queries for the helper and handler
returned no edges and explicitly reported incomplete coverage. Source inspection
confirmed the thread target, its peer-send path, failure list and teardown, and
its three fixture consumers in the protocol, oversized-reply and deadline tests.
An empty graph result was not treated as proof of no consumers.

## Correction and alternatives

The test-only handler still tolerates the existing `ConnectionError` family:
protocol tests intentionally disconnect their peer. It no longer suppresses the
larger `OSError` family. Other errors reach the existing failure list and are
re-raised by teardown as the original exception object, with the original trace.
There is no production protocol change, extra retry, new timeout, socket
substitute, or permission change.

Increasing the event timeout would hide the cause for longer. Skipping this test
or changing it to report success would remove coverage. Replacing all socket
tests with mocks would not qualify the real protocol. Introducing a second
thread-error channel is unnecessary because this helper already has a failure
list and a teardown that raises it. The tradeoff is that previously hidden OS
errors now fail tests, as they should; the existing event timeout still happens
before teardown in the real blocked-socket test.

Primary references checked on 2026-09-30:

- [Python 3.12 exception hierarchy](https://docs.python.org/3.12/library/exceptions.html#ConnectionError)
  distinguishes connection errors from permission errors and other OS failures.
- [pytest thread-failure guidance](https://docs.pytest.org/en/stable/how-to/failures.html#warning-about-unraisable-exceptions-and-unhandled-thread-exceptions)
  describes how unhandled thread exceptions are surfaced to the test runner.
  Here the helper catches them, so its own existing reporting path must retain them.
- [Linux send manual](https://man7.org/linux/man-pages/man2/send.2.html)
  distinguishes disconnection, descriptor, permission and resource failures.
  The observed EPERM itself comes from the local reproduction, not an assertion
  that the manual enumerates every sandbox policy outcome.

The Open Group send page could not be fetched and is not supporting evidence.
This uses existing Python exception classes available at the project's Python
3.10 floor. The executed runtime remains Python 3.12.3 and pytest 9.0.3; no
version, dependency, runtime directory or architecture contract was changed.

## Qualification and limits

Nine regression cases check EPERM, EACCES, I/O failure, invalid descriptor,
timeout and programming errors, plus three expected disconnects. Before the
correction, five failed and four passed. After it, all nine and the eleven real
global CCN/branch-shape gates passed: **20 passed in 27.85 seconds**. Ruff passed
for both changed files.

The complete related deadline-test file then produced **5 passed, 1 failed,
1 teardown error in 30.77 seconds**. The failing test is still blocked by EPERM;
the teardown now reports the original permission error from `sendall`. This
does not prove successful socket transport, and the full socket-backed protocol
suite and Windows/macOS qualification remain unverified.

The candidate changes are only in
`/tmp/llm-wiki-provider-verification-ewdevhsa/tests/`. Evidence, before/after
hashes and a reviewable patch are under `logs/audit-2026-09-30-lsp-fixture-*`.
No production runtime or knowledge record was mutated by this correction.
The currently observed capture log still includes the 00:27:21 lost
`post_tool_append` caused by a transaction deadline; this correction does not
claim to fix capture or nightly maintenance.

The installed namespace correction, safe old-client transition, complete test
qualification, production index/private-log updates, and post-install cleanup
remain pending as documented in the preceding audit checkpoints. No parallel
replacement helper was introduced. The broader audit is not complete and is
not certified as satisfying all nine laws at release level.

## Follow-up: a failed peer closes its connection

The next diagnostic full-suite attempt stopped at 13%, after the oversized-reply
test waited for a response from a failed scripted handler. Its 120-second stack
dump showed the request blocked in `_wait_for_completion`; that test allows
300 seconds per request. The handler retained its exception but left the server
socket open, so the protocol could not observe EOF until fixture teardown.
The diagnostic run was interrupted with exit 130 and archived; it is not a
complete regression result.

The test-only handler now closes its own peer after an unexpected exception.
`FakeLspPeer.close` closes both its `makefile` reader and the socket. A cleanup
exception is retained after the original handler exception, so teardown still
raises the original cause. Successful handlers keep their existing idle-peer
behavior. No production transport, timeout or permission is changed.

Alternatives rejected: waiting until teardown reproduces the delay; reducing
request deadlines changes protocol test conditions; directly forcing the client
to be fatal bypasses observation of the real closed connection; swallowing the
error or skipping the test hides a failed scenario. The tradeoff is that a failed
scripted peer now produces an immediate connection failure in the test body,
while teardown retains the original error. A failure to close is preserved too,
but cannot guarantee an immediate disconnect.

Primary sources checked on 2026-09-30:

- [Python 3.12 socket ownership](https://docs.python.org/3.12/library/socket.html#socket.socket.close)
  requires closing the socket and all associated file objects to release it.
- [pytest fixture finalization](https://docs.pytest.org/en/stable/how-to/fixtures.html#teardown-cleanup-aka-fixture-finalization)
  runs yielded cleanup after the test, which cannot interrupt a request already
  waiting inside that test.
- [JSON-RPC 2.0 response contract](https://www.jsonrpc.org/specification#response_object)
  distinguishes a valid result or error response; a crashed handler has produced
  neither. The correction does not fabricate a response on its behalf.

The native index was fresh at `generation-18da0d84c5f31d4f-e99fe7d3` before edits.
Graph queries for the fixture again returned no edges with incomplete coverage;
the thread target and all three known fixture consumers were verified in source.
Two new tests failed before the correction: one used the actual socket-backed
protocol and observed no fatal notification before teardown, and one showed a
cleanup error was not retained. Afterward, the regression group including actual
CCN/AST checks passed 41 tests in 30.53 seconds; Ruff passed. The group also
includes the compile-lock fixture correction below. The real oversized-reply
file still failed three socket cases with EPERM, now in 0.80 seconds rather than
waiting for each request deadline (one in-memory case passed; three teardown
errors preserve EPERM). It does not qualify successful host transport.

The compile-lock fixture previously wrote a child's lock after the child had
already exited, so it stored no start identity. It now holds the child on stdin,
writes the real lock while identity is observable, then closes stdin and reaps
the child before checking recovery. The failed original case was reproduced
separately. This changes no lock format or runtime recovery rule.

Evidence prefix: `logs/audit-2026-09-30-peer-close-*`. These are isolated test
corrections; safe installation and legacy writer recovery remain unresolved.
