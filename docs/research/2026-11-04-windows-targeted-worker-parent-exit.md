# Windows targeted-worker process exit: diagnosis in progress

Date: 2026-11-04. Status: native failure observed; root cause and repair unproven.

Run41804515315 at55eb6f95, Windows full shard2, job126488115822 exited with code1
at09:37:28UTC. The console has passing/skipped progress through about85%, no
pytest failure summary and no JUnit. Its uploaded artifact13311785558 contains
only progress.txt. The last entered case is
`tests/test_queue_targeted_worker.py::test_targeted_parent_executes_requested_compile_and_requires_completion`.
Other Windows shards were still running when this report was written. Linux and
macOS jobs had passed. A prior complete native run at43d6bade passed; this does not
make the current exit harmless or establish that annotation changes caused it.

Queue cleanup currently discovers descendants from PID/PPID pairs and uses
`taskkill /T /F`. Process identity/reuse is a plausible investigation direction,
not an established explanation for this event. Exit without pytest teardown
could also have another native or external cause. Do not rewrite cleanup or
increase timeouts based only on the last test name. No production cleanup code
has been changed by this diagnostic step.

The existing test now records a flushed/fsynced JSON line before delegating each
real taskkill: caller/parent/target PIDs, the native process snapshot and observed
creation identities. It records normal return and test start/finish separately.
The file sits beside the existing LLM_WIKI_TEST_PROGRESS_FILE in CI's existing
uploaded test-artifact directory; local runs use pytest tmp_path. No new product
environment variable, runtime directory, dependency, skip, timeout or weakened
assertion is introduced. Logging can affect timing, so failure to reproduce is
not a repair. The fixture patches only the cleanup owner's callable and invokes
its original implementation unchanged. A unit check proves evidence precedes a
delegate that raises SystemExit; this is not a Windows kill reproduction.

Local qualification: four targeted-worker tests passed in2.44s;11functions satisfy
CCN<=5, at most2ifs and nesting<=2; Ruff clean. The whole160-packet decomposition
checkpoint at56c549cd separately passed3736checks with3skips/1existingwarning,
all exact bundle roundtrips and max99915tokens. It does not qualify this Windows
failure. New native diagnostic evidence is still required.

Update 2026-11-09. Run 41811844489 (job 126512751511) failed in the diagnostic
itself: `_termination_snapshot` asked `process_start_identity` about every PID of the
Toolhelp snapshot, which always lists the System Idle Process as PID 0, and the probe
rejects a non-positive PID with `ValueError`. Only Windows builds that snapshot, and
the test does not exist on `main`, so `main` stayed green. Reproduced on Linux by
substituting a snapshot containing PID 0. A PID the snapshot lists but the probe
cannot read is now recorded as `unobservable: <error>`, so the evidence survives.
The native exit of run 41804515315 stays unexplained: this fix only lets the next
occurrence write `queue-termination.jsonl`, and no evidence yet names what ended it.

Primary references checked on2026-11-04:

- [Microsoft PROCESSENTRY32](https://learn.microsoft.com/en-us/windows/win32/api/tlhelp32/ns-tlhelp32-processentry32)
  defines the recorded parent PID, without a creation timestamp in that structure.
- [Microsoft GetProcessTimes](https://learn.microsoft.com/en-us/windows/win32/api/processthreadsapi/nf-processthreadsapi-getprocesstimes)
  supplies process creation FILETIME and access-right requirements.
- [CPython3.14 multiprocessing](https://docs.python.org/3.14/library/multiprocessing.html)
  distinguishes direct process termination from descendant cleanup.
- [psutil's PID-reuse guidance](https://psutil.io/faq/#pid-reuse)
  explains using PID plus creation time for identity-sensitive operations.

These support collecting identity evidence, not causation in this run. psutil is
not added as a dependency. Microsoft's Old New Thing article was found in search,
but direct retrieval returned403; its text is not relied on here.

Private evidence: logs/ci-55eb6f95-windows-s2-native.log,
logs/ci-55eb6f95-windows-s2-progress.zip, logs/windows-parent-exit-graph.json and
logs/windows-parent-exit-diagnostics-weigh.txt. Fresh index for56c549cd:
generation-18e4ce360ddbdadf-30b64f36,2468sources; graph incomplete and supplemented
by actual queue runtime/cleanup, compile worker, fixtures and progress-writer source.
