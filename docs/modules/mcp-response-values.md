# MCP response schema and value rules

`scripts/llm_wiki/mcp_response_values.py` owns the thirteen existing deterministic
schema/value functions extracted from mcp_contract, plus unchanged SCHEMA_VERSION
and COMPONENT_FRESHNESS. It imports only the standard library. It does not choose
runtime roots, observe current time, read Git, load settings or write state.

## Contract

The output schema retains every existing field, required list, nullable type,
number bound, freshness enum and additionalProperties rule. Each call constructs
the same schema contents; tool registration still advertises it per tool.
Quality values retain the existing inclusive 0–1 check and boolean rejection.
Supplied datetimes normalize to UTC; a naive datetime is treated as UTC, without
reading the clock. Component validation retains exact generation/freshness fields,
name/generation length rules and errors. Freshness is stale if any component is
stale, fresh only for a nonempty all-fresh set, and unknown otherwise.

JSON normalization retains finite scalar values, converts nonfinite floats to
null, converts Paths and mapping keys to strings, recurses over mappings and
supported sequences, and uses the existing JSON-or-text fallback. It introduces
no cycle detection, ordering promise for sets, new coercion or validation policy.
The existing error behavior remains unchanged, including errors from user objects.

`mcp_contract.build_envelope` still chooses roots, reads current time and Git,
observes the commit cache, composes warnings and assembles the response. It imports
the pure helpers it uses. Existing public schema and constants are reexported as
the same objects for mcp_server and callers. Those public names remain required
while that API is supported. Private implementation helpers move to this owner;
repository graph/search found no external consumers requiring compatibility copies.
There is one implementation of each rule, no forwarding wrapper or service.

## Qualification — 2026-11-03

The approved autonomous plan permits this internal split. Keeping values mixed
with default runtime loading prevents independent qualification. Hiding that
dependency in the context manifest would be incorrect; adding a validation
framework would change dependencies and behavior. Moving the unchanged functions
provides the boundary while preserving the full-source gate.

Sources checked: [bounded contexts](https://martinfowler.com/bliki/BoundedContext.html),
[import contracts](https://import-linter.readthedocs.io/en/stable/contract_types/),
[PEP 544](https://peps.python.org/pep-0544/),
[MCP output schemas](https://modelcontextprotocol.io/specification/2025-11-25/server/tools),
[JSON Schema objects](https://json-schema.org/understanding-json-schema/reference/object)
and [Python JSON](https://docs.python.org/3.14/library/json.html).
No schema revision, dependency version or numeric bound changes.

The two original schema tests move intact; the shared field fixture has one owner.
The moved schema helper changes only its import target. IO and actual server
tests remain with their existing consumers. AST, differential-output, boundary,
value and integration checks accompany the move. The complete September 26 tool
schema budget record stays historical context: wire repetition is not silently
counted as prompt savings. The full packet must fit 100000 tokens and reconstruct
exactly; no paired task-cost reduction or live installation is claimed.
