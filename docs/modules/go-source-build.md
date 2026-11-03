# Managed Go source builds

`scripts/go_source_build.py` remains byte-identical at its public path. Given an
existing staging directory and validated SourceBuild declaration, it runs the
declared toolchain to install the declared module version, requires the expected
binary and prunes build-only caches. It does not fetch or verify the toolchain
archive, select platform pins, publish an installation or launch a language server.
The installer retains those responsibilities and records the resulting digest.

## Existing behavior

The helper sets Go work/module/build caches and output directory inside staging,
sets GOTOOLCHAIN=local and GOFLAGS=-mod=mod, and creates the staged HOME. Only the
existing named OS variables are inherited. This scopes Go configuration/cache
paths; it is not an OS sandbox or proof that a hostile compiler cannot access
other files. The supplied staging tree and verified toolchain remain trusted.

The command is the supplied toolchain path plus `install module@version`, with
staging as cwd and the declaration's existing timeout. OS launch failures and
TimeoutExpired become SourceBuildError. Nonzero exit reports the last three
stderr lines, or stdout when stderr is empty. A missing toolchain or output
binary is a failure. No new process-tree containment guarantee is introduced.

After success, only the existing gocache/gopath build directories are retired;
their permissions are cleared to handle read-only Go cache entries. Existing
best-effort cleanup error handling remains unchanged. The built binary and
toolchain stay available. No changes to limits, runtime roots or pins are made.

## Qualification — 2026-11-03

The approved autonomous plan retains the existing dedicated compiler step.
Inlining it into the installer would mix responsibilities; a new build framework
or service adds no demonstrated benefit. Complete SourceBuild implementation and
its profile guide remain explicit dependencies, not signature-only summaries.

Sources checked: [bounded contexts](https://martinfowler.com/bliki/BoundedContext.html),
[import contracts](https://import-linter.readthedocs.io/en/stable/contract_types/),
[PEP 544](https://peps.python.org/pep-0544/),
[Go toolchain selection](https://go.dev/doc/toolchain),
[versioned Go install](https://go.dev/ref/mod#go-install) and
[Python subprocess behavior](https://docs.python.org/3.14/library/subprocess.html).
The September 12 design stays complete historical context. Its measurements and
pins are preserved as dated evidence; this step makes no new release selection.

Five original build tests, their fixtures and Windows skip decorators move
unchanged. The mixed native-server suite imports the same shared declaration
factory for its retained profile test. Additional checks use real child processes
and temporary cache trees. The POSIX shell fixture remains a test compiler, not
a real Go installation; this qualification does not claim a newly verified
native Windows gopls build. Full context must fit 100000 tokens and reconstruct
exactly. No live installation or paired LLM task-cost improvement is claimed.


## Preserved structure reference — 2026-11-03

The staging-directory compiler step retains its profile dependency; downloads,
installation publication and server lifecycle remain with their existing owners.
