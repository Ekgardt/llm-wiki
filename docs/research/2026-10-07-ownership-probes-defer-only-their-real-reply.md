# Ownership probes defer only their real reply

The benchmark's timeout and cancellation checks previously depended on a real
server replying slowly. A fast valid response could win all five attempts; the
benchmark correctly reported the interruption as unmeasured. Preparation and
cleanup time in an attempt is not evidence of request latency.

The benchmark now temporarily holds the exact probe's actual response, selected
by generation nonce, request ID and the existing workspace/symbol parameters.
The reader keeps running. The original deadline or cancellation must terminate
the real request. In finally, original handlers are restored and retained replies
are delivered before the existing process reset. Overlapping scopes, duplicate
probe requests and duplicate replies refuse qualification. Unrelated responses
and ordinary navigation are unchanged. Production LSP code is unchanged.

This is controlled response-delivery fault injection. It qualifies actual request
deadlines, cancellation and process cleanup; it does not measure natural timeout
frequency. The existing v1 report schema stays unchanged. Separate qualification
evidence identifies the fault mode. No extra sleep, retry, timeout budget or
synthetic successful dispatch is used.

Research checked on 2026-10-07:

- [Python 3.10 threading](https://docs.python.org/3.10/library/threading.html):
  completion signals and joins establish worker termination; timer scheduling is
  not an exact latency guarantee. Qualification uses CPython 3.10.20.
- [Microsoft LSP 3.17 cancellation](https://microsoft.github.io/language-server-protocol/specifications/lsp/3.17/specification/#cancelRequest):
  response IDs correlate requests, and cancellation does not remove the response
  obligation. Actual late responses are forwarded rather than fabricated.
- [Martin Fowler, non-deterministic tests](https://martinfowler.com/articles/nonDeterminism.html):
  synchronization and visible teardown results are preferable to timing guesses.

Blocking the reader on an Event was rejected: request error cleanup can join that
reader. The deferred buffer adds no waiting reader or extra thread. A normal fast
framed reply remains an ordinary successful request outside the benchmark scope.
Linux qualification does not establish Windows or macOS native results; CI must
exercise those platforms with their original ownership checks.
