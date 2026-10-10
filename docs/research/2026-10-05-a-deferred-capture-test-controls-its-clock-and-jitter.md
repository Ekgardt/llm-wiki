# A deferred capture test controls its clock and jitter

Research date: 2026-10-05.

The macOS Python 3.10 shard rejected the assertion that a failed capture stayed ready. The same assertion fails locally when the existing full-jitter sampler returns zero: the worker can immediately retry that task successfully. The actual sample from the remote failure was not recorded; this reproduction establishes the allowed scenario, not its precise historical random value.

The test intends to verify that a durably deferred failure does not block a second ready capture. Its conditions now hold the queue clock at the actual instant after both intents are created and select the existing retry policy ceiling for its random sample. All original state, error, success, call-count and diagnostic assertions remain. The clock and sampler replacements are scoped by pytest monkeypatch and restored afterwards. Production retries, clocks, ownership, leases and deadlines remain unchanged.

Changing production full jitter or adding a minimum delay would alter valid runtime behavior. Adding a real sleep or assuming a fast runner would retain a timing race. Controlling both test inputs is the smallest reproducible fixture change. The selected delay comes from the existing policy, rather than a new numerical limit. The clock freeze also makes the test independent of how long the runner takes.

Primary references, checked on the research date:

- [AWS: Exponential Backoff and Jitter](https://aws.amazon.com/blogs/architecture/exponential-backoff-and-jitter/) describes full jitter and distinguishes alternatives that keep a minimum backoff.
- [Python 3.10 random](https://docs.python.org/3.10/library/random.html) documents uniform sampling and its endpoints.
- [pytest monkeypatch](https://docs.pytest.org/en/stable/how-to/monkeypatch.html) documents temporary attribute replacements and automatic restoration.

Repository evidence: `memory_queue._retry_available_at`, `integration_adapter._drain_capture_work`, and `test_adapter_continues_after_a_committed_retry`. The separate production-health snapshot reconciliation work is not completed by this fixture repair.
