# Completing the LLM Wiki decomposition

Date: 2026-11-02. Status: the plan below. This note cites no owner message, so it records no owner approval.
Base: bccb6a9c248f0abe5ded75144887d2f01ffc1a81, a separate working branch.

## Goal and checkable completion

One product and installation. Each module has one responsibility, an explicit
contract and a single owner of state. The full maintenance package
(sources, own tests, data, documentation, mandatory instructions,
the necessary dependency contracts) does not exceed 100000 o200k_base tokens.
Cross-module scenarios and historical materials are counted as well; moving tests
into a "shared" folder does not free them from the budget. Readiness of individual packages does not
mean completion of the project.

Completion requires:
- A full ownership registry checked against git ls-files: no lost files.
- A measurement of each package, including instructions and serialization separators.
- One-way checkable imports; each move is checked against consumers,
  dynamic calls, CLI, subprocess and resources, not only by AST.
- Behaviour, formats, atomicity, access permissions and public commands are kept.
- No duplicated implementations. For necessary compatibility aliases, the
  consumers and a checkable removal condition are stated.
- Related tests and the full regression run pass; platform skips
  are listed explicitly and are not presented as confirmed Windows/macOS qualification.
- CCN <=5 and the form limits are checked; the index is current.
- The token efficiency of a task is assessed separately from the size of the sources.
  Without paired measurements, no savings in model calls are claimed.

## Fresh inventory

2842 tracked files, 8090066 tokens. scripts: 1929206; tests: 2391344;
docs: 1874357; benchmark: 1430515. This is the sum of file text, without chat messages.
14 existing manifests include 154 files combined; this is context coverage,
not proven unique ownership. 2688 files are still outside these manifests.

Special large artifacts: uv.lock 334209, benchmark/contradiction-v1.json 209197,
docs/DEVELOPER-AUDIT-STATUS-2026-08-18.md 144744 tokens. They must not be hidden as
"not code". The lockfile stays the single input of the installer; maintenance needs
checkable views of records with full coverage and reverse assembly.
If the user requires that each physical file also be <=100000,
the single lockfile needs a separate decision: this is a stronger condition than
the module limit. It must not be declared met by selective reading.

Evidence: logs/decomposition-completion-inventory.json and
logs/decomposition-completion-graph.json in the installed vault. The built-in MCP
overview reported busy workers; a separate code_graph.get_architecture process
read the index. The graph is incomplete and is supplemented by the sources. The source index
matches bccb6a9c; docs and installers are checked separately.

## Proposed overall architectural mandate

Approve the following families of responsibilities and a step-by-step move
into scripts/llm_wiki/<responsibility>.py or a package of the same name when several
internal files are really needed. An existing standalone file can
be qualified in place without moving. Tests go to tests/modules/<responsibility>/,
the description and manifest to docs/modules/. The old public CLIs stay entry points.
Do not introduce a mandatory multi-level area/module/api structure.

The order is defined by dependencies, not by the number of files:

1. Foundation: configuration, safe file IO, SQLite contracts, admission and
   ownership. Remove the foundation's dependencies on executors without a second registry.
2. Queue: schema checks; source identity; migration planning;
   storage and transitions; results; operator control; executor.
   The SQL transaction and lease settlement keep a single owner.
3. Markdown transactions: file images and manifests; prepare/CAS/publication;
   recovery, undo and retention. Do not create competing coordinators.
4. Capture and breadcrumbs: ingress, immutable evidence, adoption, terminal
   records. The queue does not substitute for proof of saved Markdown.
5. Compilation: sources, planning, validation, publication, progress journal,
   call recovery. Do not declare the existing unfinished compile-progress
   finished by a mere rearrangement of code.
6. Knowledge: pages, claims, archives, project journal and blackboard, learning.
7. Models: provider selection and calls, DLP policy/gateway. One fail-closed barrier.
8. Generations and search: corpus, catalog, graph, vectors, retrieval, evidence
   resolution, answers, context, telemetry. A single active pointer.
9. Code: parsing, graph/impact, repository index/revision/retention, navigation.
10. LSP: protocol/security, launch/containment/cleanup, documents, lifecycle,
    profiles. Deadlines, process ownership and platform rules are kept.
11. Interfaces: MCP tools/transport/supervisor and lifecycle adapters.
12. Installation and maintenance: install transactions/configuration/release,
    language/model installation, health readers, maintenance, backup/repair.
    Nightly calls maintenance instead of borrowing the implementation from doctor.
13. Separate scenario packages: capture→terminal, compile→publication/recovery,
    queue→ownership, transaction→recovery, backup→restore, install→upgrade,
    retrieval→answer, code→navigation, LSP containment, nightly, MCP lifecycle.
14. Measurements and history: topical benchmarks and documents, fixtures and reports.
    Large data are split at existing records with a check of preservation;
    archived claims are not rewritten for the new code.

This is not a fixed number of future modules. For each responsibility,
before code, the exact symbols, consumers, needed context,
baseline and boundary violation test are recorded. If a package exceeds the budget, it is split by
a standalone duty, not by lines or the size of an arbitrary slice.
It is proposed to allow such refinements within the listed families without
a new sign-off for each file. Deviation from these bounds needs approval.

## What the mandate does not include

No installation into the live vault, no change of the runtime root, databases, formats, public
behaviour, security model, Python support, dependencies or release.
Deleting private knowledge or run/, weakening tests and limits, introducing a
daemon, services, a second product or a universal DI/event framework are forbidden.
A discovered defect is first reproduced and fixed in a separate step.

## Research and alternatives

Three independent primary sources were rechecked on 2026-11-02:
- https://martinfowler.com/bliki/BoundedContext.html — boundaries by model and
  responsibility; it does not justify a predefined number of files.
- https://import-linter.readthedocs.io/en/stable/contract_types/ — checkable
  forbidden/protected/layers/independence/acyclic contracts. The tool is not
  installed by this plan; imports do not prove runtime isolation.
- https://peps.python.org/pep-0544/ — narrow structural Protocols when
  alternative executors are needed. They do not replace runtime validation and are not needed everywhere.

Choice: step-by-step decomposition inside one distribution. Alternatives:
context selection alone leaves reverse dependencies; mechanical splitting
leaves coupling; separate distributions/services add versions and failures
without a confirmed need. Data compression is acceptable only with proof of
preservation; minification and deleting needed context are rejected.
The Python >=3.10 contract is kept; no new technologies or versions are added.

## Refining the package completeness check

After the SQLite contracts it was found that all 16 manifests missed the automatically
loaded tests/conftest.py (3024 tokens) and tests/code_kernel_helpers.py
(4487). This is a shared measurement gap, not a code reduction. Reproduction:
logs/module-pytest-bootstrap-underestimate.json. Even with them, all 16 preliminarily
fit within the limit; the earlier numbers were too low. A check of the mandatory
bootstrap from the actual pytest_plugins and test parents is required, not a manual list.

A single test-only meter, tests/module_context.py, will replace the repeated code
of 16 counters: it expands the declared files, adds the checkable mandatory
bootstrap and its own code to the manifest requirements, counts the real
serialized content with file names and boundaries. Silently adding
what is missing and presenting the old manifest as complete is forbidden: absence is an explicit refusal.
The limit, the encoding and the files are not cut. The shared conftest and its guards stay
included; isolation of product imports is checked separately in a subprocess.
The pytest plugin imports integration libraries, but the plugin's own fixtures
are not called by pure module tests; their full transitive sources
do not become the property of each module. The contract/call graph is still
checked separately. This meter does not prove that all related tests
of the whole repository are already distributed among owners.

Primary sources checked on 2026-11-02: pytest writing_plugins (autoloading
of conftest and pytest_plugins), OpenAI tiktoken README (real token encoding),
Python pathlib (explicit file enumeration). No new dependencies are introduced.

## Status update 2026-11-09: the decomposition is not complete

The remainder was taken on in this task. Measured after that work on 2026-11-09:
4376 tracked files, 3134 owned by a module packet, 1242 unassigned, about 3.95 million
o200k_base tokens unassigned. Every packet measures at most 100000 tokens (the largest
97343), and no single file is over the limit any more: `uv.lock`, the benchmark data and
the large studies are split at existing records with a byte-exact catalog, and
`scripts/memory_queue.py` is 98298 tokens after four cohesive helpers moved out and its
dead aliases were deleted.

What remains is code: 133 scripts, 1089 tests and 19 benchmark runners. Schemas, queries
and test data now have owner packets, except `tests/fixtures/code_kernel/python/pkg/broken.py`:
it is invalid Python on purpose and the packet validator parses every `.py` it is given.
Only 5 of 134 scripts and 83 of 1170 tests fit their full-source import closure in a
packet beside the roughly 60000-token base; orchestrators reach 1.75 million tokens
across 232 files, including one 77-file import cycle. `memory_queue.py` itself owns no
packet, because the file alone leaves no room for its tests and dependencies.

Those files need interface-only dependencies. The qualified-dependency proposal of
2026-11-04 describes them, but `_manifest_bundle` refuses a `qualified_dependencies` entry until a
boundary passes fresh qualification, and the rollout proposal authorizes no new paired
agents. Lifting that refusal without the evidence would weaken the check, so it was left
in place. Law 11 stays unconfirmed for the unassigned files.
The task request asks to finish the module decomposition and to "do everything in this
task" (the owner's words, rendered in English), so the branch is not handed over until the
ownership check passes.

Measured on 2026-11-09 after the structure reference was split: 1243 files are unassigned.
A packet has about 52000 tokens left beside its base. 15 scripts stay over that even with
every dependency reduced to an interface (`doctor.py`, `mcp_server.py`, `compile_memory.py`
and `memory_queue.py` first). 335 test files, 1.09 million tokens, read or patch private
names of providers whose full source does not fit beside them. 106 providers are reached
privately and 127 are imported by tests.

This plan is complete when the ownership registry covers `git ls-files` without a lost
file and every packet measures at most 100000 tokens.

## Status update 2026-11-11: the decomposition is complete

`python -m tests.module_ownership --check-complete` exits 0: 7736 tracked files, 7735
owned by modules, 1 pinned vendored data (the o200k_base vocabulary, covered by
`PINNED_VOCABULARIES`, see `2026-11-11-the-tokenizer-vocabulary-is-pinned-data.md`),
0 unassigned. 1722 module packets each measure at most 100000 o200k_base tokens;
the largest are `lsp-protocol-tests` (99968) and
`test-workspace-revision-hashing-and-git-bounds` (99883). Files too large for a packet
became facades over concern modules, large test files were split by concern, and
providers reached only through public names are carried as qualified interface
descriptions. `tests/test_module_registry_integration.py::test_every_tracked_file_belongs_to_a_module`
fails on any tracked file left without an owner, so the completion cannot regress
silently. Headroom is small in the largest packets and in `module-map`, which owns
`docs/MODULES.md` and grows with every module.
