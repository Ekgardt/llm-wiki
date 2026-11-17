# Giant files become concern modules behind their old names

Date: 2026-11-10.

## What changed

Four files were each larger than one module may be under the 100 000-token
limit, so no module could own them. Each now keeps its file name and import path
as a facade, and its code lives in concern modules under `scripts/llm_wiki/`:

| Facade | Parts |
|---|---|
| `scripts/doctor.py` | `doctor_{base,capture,deletion,generation,integrations,lsp,queue,repair,schedule,transactions}.py` |
| `scripts/markdown_transaction.py` | `markdown_{abort,apply,checkpoints,coordinator_base,fences,operations,preconditions,prepare,records,records_access,recovery,store,targets,undo,writer_gate}.py` |
| `QueueV3CandidateReader` in `scripts/queue_core.py` | mixins `queue_reader_{base,capture,compile,purge,quarantine,semantic,sources,tasks}.py`, shared helpers in `queue_store.py` |
| `scripts/compile_pipeline.py` | `compile_pipeline_{base,sources,claims,operations,prompts,batches,receipts,apply,selection}.py` |

Every command, entry point and documented import keeps working. Code inside the
product and the tests imports a name from the part that defines it, not from the
facade.

## Rules the split follows

1. **Patch where the name is looked up.** A facade re-export is a second binding:
   patching it leaves the part's callers on the real function, and a test can pass
   while the patch never runs. Tests therefore patch the defining part, and
   `tests/compile_paths.py::point_compile_path` patches every compile module that
   holds its own copy of a path.
2. **A legacy name stays absent from the whole split.** A check that a removed
   function is gone (`run_compile`, `_execute_plan`, `critique_plan`,
   `_mark_superseded`, ...) asks the facade and every part
   (`compile_pipeline_binds`); asking only the facade would pass even if the
   function came back in a part.
3. **Moved code must not resolve paths from `__file__`.** A part lives one
   directory deeper, so `Path(__file__).with_name("schemas")` names a directory
   that does not exist. This happened twice (the purge reader, then the compile
   plan schema), both times caught by the compile and queue tests before a
   commit. The parts now name `Path(__file__).parents[1] / "schemas"`. The
   one-off script that performed the moves (not kept in the repository) refuses
   code that uses `__file__`.
4. **A limit keeps its reason.** A block of constants that shares one comment
   explaining its numbers moves as one block, so
   `tests/test_every_limit_states_its_reason.py` still finds the reason beside
   each limit.
5. **No new cycle.** `tests/test_the_import_graph_stays_acyclic.py` runs over the
   parts.

Static checks that read a module's source (security invariants, adopted-queue
routing, module consumer boundaries) read the facade and all its parts through
`tests/script_sources.py::split_module_files`; checks over the queue engine read
`tests/queue_engine_sources.py`.

## Alternatives

| Option | Verdict |
|---|---|
| Facade at the old path plus concern parts | Chosen. Callers, scheduler entries and documentation keep working, and each part can be owned by a module. |
| Turn each file into a package (`doctor/__init__.py`) | Rejected. A package directory and the script file cannot share a name, so every `python scripts/doctor.py` entry point, installer and scheduler definition would change. |
| Keep the files and exempt them from the limit | Rejected. The user's limit allows no exemptions. |
| Rewrite the files' internals | Rejected. A move keeps behaviour provable by the existing suite; a rewrite would not. |

## Sources

Read on 2026-11-10; quotes are verbatim from the named section.

- [CPython `unittest.mock`](https://docs.python.org/3/library/unittest.mock.html#where-to-patch),
  "Quick Guide": "With `patch()` it matters that you patch objects in the namespace
  where they are looked up."
- [PostHog engineering skill, "Splitting oversized modules"](https://skills.cat/skills/posthog/posthog/splitting-oversized-modules),
  "Traps worth knowing": "A re-export shim in `__init__.py` gives every symbol two
  bindings, so patching one leaves internal callers on the real implementation: the
  patch applies, the test passes, and nothing intercepted", and "Relative imports
  break one level down." The page does not discuss `__file__`; the rule on
  `__file__` paths rests on this project's own evidence: the schema path and the
  doctor's default vault root both resolved to `scripts/` after the move, and
  `tests/test_doctor.py` now refuses a part that steps up only one level.
- [PEP 8, "Public and Internal Interfaces"](https://peps.python.org/pep-0008/#public-and-internal-interfaces):
  "Imported names should always be considered an implementation detail" and "All
  undocumented interfaces should be assumed to be internal." The
  [typing specification](https://typing.python.org/en/latest/spec/distributing.html#import-conventions),
  "Import Conventions": "`from Y import X as X` (a redundant symbol alias):
  re-exports `X`", the form the facades use. The specification governs what type
  checkers treat as public, not runtime patching.

## Trade-offs and what remains

- A consumer that patches the facade by habit gets a silent no-op. The rule is
  written here and in the tests named above; there is no automatic detector for
  every such patch.
- The facade still re-exports names for its documented consumers and for the
  historical pickle names, so it is not empty.
- Splitting files is not module completion: most product files still belong to no
  module (`python tests/module_ownership.py --check-complete`), and many single
  scripts reach more than 100 000 tokens of required context through their imports.
