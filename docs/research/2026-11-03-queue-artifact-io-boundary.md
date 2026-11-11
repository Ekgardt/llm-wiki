# Queue artifact IO ownership

2026-11-03; approved completion mandate, baseline 88a6b897.
Separate 22 file/stream functions and _ArtifactOutput from memory_queue into
llm_wiki.queue_artifacts. They own owner-only metadata checks, stable digest/read,
exclusive temporary creation, sequential producer writes, link-if-absent
publication, same-content conflict verification, checked file/directory flushing,
and export scratch cleanup. Keep all function/class ASTs and signatures.
The queue retains identity aliases while existing callers and fault probes use
them. There is one implementation. No SQL transition, lease, source-fence,
retention decision, repair authority or queue class moves into this IO owner.

_verify_open_artifact stays in the queue: it maps verification to the compile
journal's ProgressCorrupt domain error. It calls the extracted primitive checks.
The new IO owner imports queue value contracts, SQLite deadline checks, runtime
permission/sync owners and Windows ACL policy directly. It never imports the
queue executor, compile journal or transaction coordinator. Publication ordering
and cleanup conditions remain exactly as before; no new general storage facade.

Three independent primary publishers checked today:
- Python 3.10 os documents descriptor lifetime, fdopen, link, fsync and OS limits:
  https://docs.python.org/3.10/library/os.html
- SQLite atomic commit explains ordering, flush assumptions and the distinction
  between visibility and durability: https://www.sqlite.org/atomiccommit.html
- Microsoft documents buffered IO and hardware-cache limits:
  https://learn.microsoft.com/en-us/windows/win32/fileio/file-buffering

Choose preservation of the product's existing platform-qualified primitives.
Replacing this with rename-overwrite would change create-only conflict semantics;
whole-buffer serialization would lose streaming; a new storage class adds no
needed ownership boundary; unbuffered native IO changes alignment/platform
requirements. None is needed for decomposition. Python >=3.10 compatibility
remains despite upstream EOL. No external dependency, runtime path or bound is
added. Existing artifact digest/size, exclusive creation, owner permissions and
failure evidence must remain observable. Kernel durability assumptions are not
turned into a stronger cross-platform guarantee by moving code.

Audit callers and injection bindings through the fresh built-in graph plus
source-wide inspection. Class and function ASTs must match; tests must preserve
all original behavioral assertions. Add own real-file publication, conflict,
producer-failure, deadline, stable-read and cleanup checks; measure the complete
source/dependency/test packet and rerun affected queue/adoption/capture cases.

Qualification evidence: source research used fresh built-in generation
18e4663f5e001813-d802f1b2 at 88a6b897, including callers of the writer, reader,
cleanup and permission functions; source inspection supplemented incomplete graph
edges. Baseline201 passed/1 skipped. First extraction run reproduced one stale
permission-observer binding (1 failed/200 passed/1 skipped). The observer now
covers both queue orchestration and artifact temporary creation, retaining every
original assertion; no other injection binding needed relocation.

All 23 extracted definitions and every remaining queue definition are AST-equal
to baseline. Weighted affected tests140 passed; Python3.10 139 passed; broader
CLI/export/corruption/capture/retention/adoption integrations167 passed/1 skipped;
module/bootstrap/boundary/complexity/structure/privacy/CI/limit/weight guards978
passed/2 skipped. Two extra staging-cleanup scenarios were added after that run;
their complete 13-test file passed. CCN initially found6 in a new boundary test;
extracting its source-definition reader reduced it within5 without changing
assertions. Local CCN/shape and Ruff pass. All44 bundles round-trip complete paths
and bytes. The separate full regression is pinned to8d8df4c4, not this phase.

Native Windows/macOS qualification remains pending. Dispatching the existing
GitHub tests workflow for saved88a6b897 was refused with HTTP403 (repository
administrator rights required); the user was asked asynchronously about manual
launch. This does not authorize installation or prevent independent local work.
