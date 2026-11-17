# Context budget module

Qualified in its existing location on 2026-11-02. This records an existing module,
not a new package or runtime architecture. `scripts/context_budget.py` remains its
sole implementation; no source, test, import path or public name is moved.

## Responsibility and interface

The module owns immutable token-accounting values (`ContextBudget`, `TokenUsage`,
`TokenCount`), complete context items and packing results (`ContextItem`,
`DroppedItem`, `PackedContext`, `BudgetFailure`, `BudgetExceededError`), token
counting and deterministic `pack_context`. It depends only on Python's standard
library. Tokenizer adapters are supplied by callers; the module does not select
providers, call models, discover files, read configuration or own runtime state.

Reported, tokenizer-measured, estimated and unknown counts remain distinct.
The UTF-8 byte estimate is conservative and explicitly estimated, not a measurement
of a model's tokens. Mandatory items stay complete or cause a structured refusal.
Optional items are ranked deterministically; configured section, source, parent
and emergency byte limits remain visible in dropped-item reasons. The final
rendered text, including separators, is counted. No caller policy or limit changes
in this qualification.

## Complete maintenance context

`context-budget-context.json` names the entire source file, both existing owned
test files, all inline test data, dependency declaration, shared instructions,
canonical structure document, preceding modularity studies and relevant packing
history. No function or test is shortened to meet the owner's 100,000-token cap.
The pinned development tokenizer is `tiktoken==0.14.0`, `o200k_base`; results are
not a claim about another model tokenizer or complete task API usage.

The ownership is deliberately one source file and two test files in their existing
locations. The automatic budget check counts complete files, including itself,
and fails if these files or this guide are missing from the packet. Adding owned
files requires updating this explicit ownership contract and its manifest.

Standalone checks use only this module, pytest and the development tokenizer:

```sh
PYTHONPATH=scripts uv run pytest --noconftest tests/test_context_budget.py tests/test_context_packing.py -q -s
```

This command does not load the repository's execution fixtures. The ordinary
pytest invocation still runs them and retains all global guards. An AST guard
checks direct and local imports against the Python standard library, including
relative imports; negative examples prove that executor imports are rejected.
It is an import boundary check, not an OS sandbox or proof against dynamic code.

## Integration boundary and evidence

On the unchanged source at `22963857`, all 56 existing owned cases passed in the
standalone run. The AST inventory found 22 test files importing this module;
only `test_context_budget.py` and `test_context_packing.py` have no other product
module dependencies. The other 20 remain integration tests, with their existing
owners and fixtures. They are not relabelled or removed to meet the packet cap.
Changing provider counting, compiler selection, MCP replies, project handoff or
retrieval needs those consumers' context and tests in addition to this packet.

The built-in graph was checked at the current indexed revision and supplemented
by source imports; its general completeness is not assumed. Source bytes and all
consumer imports remain unchanged by this qualification. The result establishes
a second bounded maintenance unit without extraction overhead; it does not prove
repository-wide completion or savings over a full LLM task cycle.

Evidence: local `logs/context-budget-baseline.txt`,
`logs/context-budget-graph-before.json`, `logs/context-budget-test-consumers.json`;
`tests/test_context_budget.py`, `tests/test_context_packing.py`.

## Existing context-budget boundary (qualified 2026-11-02)

`scripts/context_budget.py` already owns token-accounting values and deterministic
complete-item packing, with only standard-library imports. It remains at that
path with its existing public API and no new compatibility layer. Owned tests
remain `tests/test_context_budget.py` and `tests/test_context_packing.py`.
`docs/modules/context-budget.md` and its context manifest record this existing
boundary and enforce the 100,000-token maintenance ceiling. Consumer integration
suites keep their owners and are additional context for cross-module changes.
No runtime, provider, limit, source path or installation behavior changes.
