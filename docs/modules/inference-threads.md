# Process-local inference thread lifetime

`inference_threads` owns its process-local registry, lock, stopping event and lazy
atexit registration. It starts tracked daemon threads, removes them in finally,
requests cooperative stopping and joins within the caller's timeout. Unfinished
thread names are returned; threads cannot be forcibly stopped. A still-running
inference can remain unsafe at interpreter exit, so bounded settlement is not an
unconditional shutdown guarantee. The existing 30-second exit bound is preserved.

Import creates synchronization values but starts no inference and registers no
exit handler until start. Start failure removes the thread from the registry; completed
threads clear stopping when the registry drains. No daemon process, new scheduler
or alternative registry is introduced. Production source stays byte-identical.

Own tests cover synchronization, idle and unfinished settlement, failed starts,
cleanup and normal process exit. MCP warmup, retrieval interruption and server
shutdown remain consumer obligations. Historical no-model-running-at-exit and
every-process-settles-its-inference-at-exit research remains in the packet.
Run `pytest tests/modules/inference_threads
 tests/test_every_process_settles_its_inference_at_exit.py
 tests/test_no_model_running_at_exit.py -q` as one command.

Shared canonical contract: [Maintenance qualification](context-tooling.md).
