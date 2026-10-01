# A compiler outlives the nightly service

Research checked 2026-10-01 for the installed systemd 255 and Python 3.14 runtime.
The same process identity must survive a scheduler exit; a POSIX session is not
an independent cgroup. A nightly that defers work must not kill that work.

Primary sources:

- Linux kernel cgroup-v2 documentation, https://docs.kernel.org/admin-guide/cgroup-v2.html : fork inherits cgroup membership, migration changes membership.
- systemd v255 implementation and manual, https://github.com/systemd/systemd/blob/v255/src/run/run.c and https://github.com/systemd/systemd/blob/v255/man/systemd-run.xml : local scope registers its own PID then execs the command, inheriting environment and streams. Scope mode does not expand command arguments by default. It is distinct from a service started by the manager.
- CPython subprocess contract, https://github.com/python/cpython/blob/3.14/Doc/library/subprocess.rst : start_new_session calls setsid; it does not promise escape from a service manager's cgroup. Successful Popen proves process creation, not application readiness.

Rejected alternatives: disabling service KillMode loses ordinary child cleanup;
raising the nightly wait only moves the same failure; a second daemon or dedicated
service/environment-copy protocol adds unnecessary lifetime and credential
handling. A native transient user scope preserves the existing asynchronous
entry, environment, streams, PID and compile-lock owner token. Linux launches
from a systemd user service (INVOCATION_ID and actual cgroup membership under
an instantiated `user@.service` manager ending in a service unit) use that scope; other native launch modes
remain unchanged. A manager refusal must not silently fall back to the cgroup
that is known to be unsafe.

The full path is scheduled_nightly -> maybe_compile -> spawn_detached ->
systemd-run scope -> compile_memory lock admission -> state outcome -> nightly
and MCP health. All hook/CLI scheduler compile requests share maybe_compile;
in-process direct MCP compiles retain their caller's bounded lifetime. Other
spawn_detached clients have separate job/owner contracts and are not changed
without evidence that they need this compile lifecycle.

An asynchronous manager can fail before compile_memory records its start. The
spawner therefore records `starting` under its claimed compile lock before
creating the process. The claim first names the live spawning PID, so a slow
state write cannot expire the historical PID-0 placeholder window. The same
owner token then passes to the child. Compile entry changes it to `running`; an exited starting
process is an incomplete attempt, not a successful compile. Existing running
records remain accepted and final outcomes/history are not rewritten.

Native synthetic qualification: a Type=oneshot parent service exited normally;
its child remained in a separate scope and completed after an explicit FIFO
release. Spawn PID equalled final child PID, environment marker survived and
cgroups differed. This proves the platform primitive, not a finished production
compile. Unit regressions and the real installed compiler still qualify the
integration. No duration limit, process-count cap or retry reduction is added.

Full-path native synthetic qualification also passed: maybe_compile acquired and
handed off its actual lock, the scope ran compile_memory lock admission and
start/finish recording, and a child waiting for explicit release survived its
Type=oneshot parent. Lock PID equalled child PID; final state was ok and lock
absent. Real production provider work remains a post-install qualification.

System cron services must not acquire a new dependency on a user manager: their
long-lived parent does not exit with a nightly invocation. A regression rejects
INVOCATION_ID alone as proof of user-service ownership. Actual cgroup membership
also keeps a compile already inside a user scope from being needlessly moved.
