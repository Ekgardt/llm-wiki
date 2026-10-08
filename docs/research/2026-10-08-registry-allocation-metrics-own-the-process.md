# Registry allocation metrics own the measurement process

Research date: 2026-10-08. Python 3.10 remains supported.

The macOS Python 3.10 full CI shard for revision 48f3831a reported 5.47 MiB for a cancelled registry walk and 2.00 MiB for a full walk. The original test started tracemalloc in the shared pytest interpreter. The CI log also contains late worker diagnostics, but it does not include an allocation snapshot: the allocator responsible for that particular extra memory is unknown.

A causal replay of the unchanged test allocated an unrelated 8 MiB block on another thread after the second tracing window started. The test attributed it to cancellation and failed at 8.46 MiB against 1.92 MiB. This establishes contamination of the measurement, not a production cancellation failure.

Measure each real walk in a fresh interpreter. Keep the original less-than-half peak assertion. Also check that the complete walk parsed all 200 fixture files and that the cancelled walk parsed exactly 20 and raised cancellation. The helper uses the existing parser-count fixture and the existing calibrated test-process hang deadline. It adds no production limit, runtime service or dependency. A separate guard keeps unrelated parent memory outside the measured peak. Replaying that guard with in-process measurement fails; deliberately ignoring real cancellation also fails the strengthened original test.

Alternatives considered: relaxing the ratio or skipping macOS would weaken the test; fixed sleeps or retries would conceal interference; filename-filtered snapshots do not measure the same peak and can exclude allocations made by dependencies of the walk. A dedicated measurement interpreter preserves the measured operation and its threshold while separating unrelated allocations. Cold startup outside the tracing window is test setup, not a claimed product latency improvement.

Independent primary sources:

- Python 3.10 tracemalloc documents the current and peak size of all traced Python memory blocks, and attribution through snapshots: https://docs.python.org/3.10/library/tracemalloc.html
- pytest documents uncontrolled system state and insufficient isolation as causes of flaky tests: https://docs.pytest.org/en/stable/explanation/flaky.html
- Valgrind's Massif manual explains heap profiling and the need to identify allocation sites; it is a comparison, not a new dependency: https://valgrind.org/docs/manual/ms-manual.html

The dedicated tests pass locally on Python 3.12 and Python 3.10. Native macOS qualification still requires the next CI run. No claim is made that unrelated production workers have all been eliminated.
