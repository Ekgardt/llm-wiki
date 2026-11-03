# State JSON values

scripts/llm_wiki/state_json_values.py owns JSON object decoding, exact encoded
byte measurement, ordered disposable-cache eviction and serialization. It receives
the existing target byte budget explicitly; it imports no runtime or IO owner.

Decoding retains UTF-8 then json.loads semantics and accepts only dictionaries.
Malformed UTF-8/JSON or another JSON top-level type returns None. Other exceptions
retain their meaning. Encoding retains indent=2, ensure_ascii=False, insertion
order and the standard encoder's existing behavior, including its scalar and error
semantics. The measured size is the exact encoded UTF-8 length.

Only the existing ordered tool_capture_dedupe, prompt_capture_dedupe and
project_checkpoint_reducers maps are evictable. The first nonempty map loses its
oldest insertion; the process repeats while above budget. It stops if nothing can
be evicted. Serialization raises the existing EFBIG error if protected content still
does not fit. Dictionary mutation before refusal retains its old behavior; the
value layer promises no rollback of an in-memory dictionary.

memory_state keeps MAX_STATE_TARGET_BYTES and supplies its current value through
two single-call compatibility adapters. Existing consumers deliberately override
that constant, so no copied/frozen default is introduced. _state_bytes and
_decoded_state remain imported names. These adapters preserve the supported API
and can disappear only when its callers no longer require that budget binding.
There is one implementation of the algorithms.

All root selection, lock ownership, corrupt/previous copies, atomic publication,
and durability stay in memory_state. Real update_state refusal tests confirm that
oversized protected state does not replace either current or previous file.

## Qualification — 2026-11-03

The approved whole-project plan permits this internal boundary. Keeping value
operations in the state coordinator couples tests to unrelated runtime initialization.
Duplicating eviction in producers risks drift; a new framework or a frozen budget
would change contracts. Explicit existing-budget injection retains responsibility
at the caller and adds no limit, setting or dependency.

Research checked [bounded contexts](https://martinfowler.com/bliki/BoundedContext.html),
[dependency contracts](https://import-linter.readthedocs.io/en/stable/contract_types/)
and [Python 3.14 JSON](https://docs.python.org/3.14/library/json.html).
Python's order and encoding behavior is part of the byte-budget contract.

AST comparison allows only the explicit target parameter and its propagation.
Differential cases check output, exceptions and mutated state against the old
implementation. Original real persistence/recovery/refusal checks remain intact.
Owner boundary/value tests, actual complexity, full context and exact reconstruction
must pass. Historical budget and recovery research remains complete context. No
whole-task model savings or live deployment is claimed.


## Preserved structure reference — 2026-11-03

The value layer receives the existing byte budget explicitly; memory_state keeps
its public budget binding, locks, recovery and durable publication.
