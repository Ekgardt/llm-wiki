# The caller sets the context budget

Research date: 2026-09-29. Audit finding: `get_context` refuses a requested budget
above 32,768 before reading any source. Its own comment says the number has no
established basis. A fixed minimum of 256 likewise precedes the existing check
of whether the actual answer fits. Neither number is a model or MCP constraint.

Codebase Memory and source inspection trace the request through the published
tool schema, direct-call validation, corpus selection, the shared context
compiler and whole-answer accounting. The budget is an allowance, not an
allocation: the compiler materializes the selected finite sources and packs
existing items. It does not create a buffer or loop once per requested token.
The existing request deadline and corpus admission remain in effect. A larger
allowance can return more existing evidence; it cannot create missing evidence.

Remove the unsupported upper ceiling and accept a positive integer allowance.
Keep the actual context-package fit check: when its metadata cannot fit, return its
existing explicit error. Keep the existing default for callers who omit the
argument; this change does not establish a new default. Reject booleans,
non-integers and nonpositive values in both entry paths.

Alternatives: increasing the ceiling merely substitutes another unsupported
number; a new configuration ceiling adds an environment/settings contract
without demonstrated resource need; guessing the caller's model confuses a
local retrieval tool with the host that owns the model and remaining context.
The selected correction uses the caller's existing explicit budget. Larger
answers can cost the caller more tokens; the caller chooses that allowance and
the result reports its estimate. The estimate is not a provider tokenizer count.

Accounting boundary: `_get_context` checks the complete context package (text
and its item/provenance fields). `_answer_text` subsequently adds the standard
MCP envelope and optional cost telemetry. Those extra fields are outside this
allowance. The input description must say this explicitly; the correction is
not a guarantee that the complete wire response or the host's model prompt fits
that number. Exact final-response/host accounting remains separate audit work.

Primary sources checked on the research date:

- [MCP tools](https://modelcontextprotocol.io/specification/2025-11-25/server/tools):
  tool arguments are described and validated by their JSON Schema. The tool's
  application-specific token allowance is not a protocol-wide model window.
- [JSON Schema numeric validation](https://json-schema.org/understanding-json-schema/reference/numeric):
  integer type, minimum and maximum are separate constraints. Removing an
  unsupported maximum does not remove type or positive-range validation.
- [Python numeric types](https://docs.python.org/3/library/stdtypes.html#numeric-types-int-float-complex):
  integers have unlimited precision, whereas booleans are an integer subtype;
  direct Python validation must continue to exclude booleans explicitly.

This remains compatible with Python 3.10 and the existing JSON Schema vocabulary.
It adds no dependency, persisted schema, path, environment contract or runtime
location. Existing request fields and successful responses retain their shapes.
The local checks use MCP 1.29.0, jsonschema 4.26.0 and Python 3.12.3.
The other admission and context-expansion limits remain separate unresolved
audit work; this correction does not certify them.

Qualification: first reproduce refusal above the old boundary, then exercise
both tool-schema and real compiler paths with larger caller budgets, actual
context-package accounting and a no-fit small budget. Measure elapsed time and peak
Python allocation on the same finite corpus at different allowances; a large
allowance must not manufacture padding or a model call. Retain malformed-input,
source selection, deadline and budget-packing regressions. Run the local CCN
and branch-shape checks after editing and remove the unused ceiling constant.

The old validation failed four regression cases (three budgets above the
ceiling and a positive budget below the old floor). After the correction, 439
related tests passed with one skip and three warnings. The final focused module,
including a source large enough to produce an answer above 32,768 estimated
tokens, passes all 20 tests. Invalid inputs still fail; insufficient positive
budgets fail against actual answer size.

On the same three-page fixture, allowances of 8,192 / 32,768 / 32,769 / 65,536 /
1,000,000 took 0.026139 / 0.025079 / 0.026435 / 0.027026 / 0.024947 seconds, with
peak traced Python allocation between 1,068,282 and 1,068,970 bytes. Returned
text/items stayed identical and a model call would fail the test. These are
local, small-corpus measurements during concurrent verification, not throughput
guarantees or full-product token-efficiency qualification. A second run remains
in the test output; no single timing is presented as a universal bound.
