# LSP wire boundary

Date: 2026-11-03. Status: designed under the approved decomposition mandate;
implemented; qualification recorded below. Base: 9c38cbd5, isolated candidate.

## Responsibility and research

The byte codec will own strict JSON walking, message-shape validation,
Content-Length framing, bounded oversized-frame consumption and encoding in
`scripts/llm_wiki/lsp_wire.py`. Transport scheduling, Windows handles, threads,
request cancellation, clocks and process ownership remain in `lsp_protocol.py`.
The codec consumes the binary stream supplied by its caller; it does not own
or close that stream. Existing public imports remain aliases, not second bodies.

Three independent primary publishers checked today:
- Microsoft LSP 3.17 base protocol: ASCII headers, byte-counted content, UTF-8,
  and signed 32-bit identifiers. This is the existing compatibility contract,
  not a claim that 3.17 is the latest specification.
  https://raw.githubusercontent.com/microsoft/language-server-protocol/gh-pages/_specifications/lsp/3.17/specification.md
- JSON-RPC Working Group: requests, notifications and mutually exclusive result
  and error fields are independent of transport scheduling.
  https://www.jsonrpc.org/specification
- Python 3.10 JSON documentation: default JSON decoding accepts non-finite values;
  strict hooks and explicit finite checks must remain. The page now labels 3.10
  unsupported; this move preserves the project's existing minimum version and
  does not claim continued upstream support or change deployment requirements.
  https://docs.python.org/3.10/library/json.html

Alternatives: retaining the monolith couples codec maintenance to thread owners;
using a third-party JSON-RPC stack changes rejection and lifecycle semantics;
separate header/JSON micro-modules add boundaries without independent consumers.
Choose one cohesive codec with stdlib plus bounded_io's existing chunk size.
No protocol upgrade or limit change. Existing refusal of null response IDs and
batches is deliberately preserved, even where generic JSON-RPC is broader.

## Exact transfer and compatibility

Move ProtocolViolation, FrameTooLarge, _JsonWalk, JsonRpcFrameReader and every
function from _strict_string_size through encode_frame. Transfer their constants
and rationale comments. Keep transport-only subclasses and all later functions.
Keep root aliases used by product/test consumers, including encode_frame, which
is deliberately patched in the lsp_process cancellation qualification. Preserve
public Python module identities for serialization. Verify original function ASTs.

Move only BytesIO/strict JSON tests and their two helpers from test_lsp_protocol.
They do not construct a transport or observe _CLOSE_WAITS_FOR_A_READ. Its two-way
autouse close fixture remains on transport tests; applying it to the codec would
repeat identical cases without exercising either close implementation. Preserve
all moved assertions, parameters and function bodies. Check later pure JSON
cases too, rather than duplicating _framed. New isolated-import guards must fail
before extraction and pass after it. Full source dependency closure, guide closure,
pytest bootstrap, actual serialization and <=100000 o200k_base tokens are required.

## Investigation evidence

Fresh built-in index was rebuilt at 9c38cbd5. Source search found the deliberate
root encode_frame patch; no patches to moved codec helpers/constants. Consumers:
lsp_process, pyright_session, fake peer and protocol/oversize tests. Graph results,
AST proof, tests, complexity and exact bundle measurements will be recorded before
claiming this boundary ready. Native Windows/macOS CI remains unavailable pending
operator dispatch; Linux close-path simulation is not native platform evidence.

## Qualification

Fresh built-in source generation `generation-18e46c71070b34c6-5e8b944e`
corresponded to 9c38cbd5 before edits. Graph caller results were supplemented by
all-source imports and fault-injection search because the graph is incomplete.
Evidence: `logs/lsp-wire-graph.json`, `logs/lsp-wire-import-consumers.json`.

Three boundary guards failed before extraction. All 38 moved production definitions
and every remaining production definition retain their AST bodies. Eighteen pure
test functions and two helpers retain bodies and parameter decorators. Removing
only their unused close fixture avoids 52 duplicate executions; the transport's
fixture and assertions remain unchanged. Seven public pickle forms are byte-equal.
Actual CCN and nesting/shape checks pass, as does Ruff.

Baseline protocol/oversize suite: 314 passed, 2 skipped. After extraction plus three
boundary guards: 265 passed, 2 skipped. With the budget guard included: Python 3.10
266 passed, 2 skipped. Process/session/deadline consumers: 393 passed, 44 skipped.
Ownership, source/guide completeness, structure, limit reasons and shard weights:
181 passed. These are Linux runs; simulated Windows close paths do not qualify
native Windows. No original assertion was removed or weakened.

The earlier detached full regression at 7854a031 completed: 15705 passed,
244 skipped, 5 warnings, 1789.95 seconds. It predates this extraction and the two
capture-path commits, and is not presented as a full current-HEAD result.

Pre-final-documentation measurement: 49 actual serialized packets decoded back to
all original paths and bytes, all below 100000 tokens. Wire packet 83024 tokens;
largest packet 96670. These overlapping packet measurements are not a whole-project
unique token total or a paired LLM-task savings claim. Final measurements and global
qualification are recorded with the committed checkpoint.

Global module, privacy, CI, complexity and structure qualification: 1181 passed,
2 skipped. Added a permanent parameterized root-facade test for public object and
pickle identity; the unchanged two-close-path fixture exercises it ten times.
The weighted root protocol suite then passed 216 tests, with 2 native skips.
