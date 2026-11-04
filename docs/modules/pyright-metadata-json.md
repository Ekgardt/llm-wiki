# Pyright metadata JSON values

Under the approved decomposition mandate, the existing strict JSON
object decoder, JSONC comment/trailing-comma passes and shared metadata exception
move to `llm_wiki.pyright_metadata_json`. Every function/class body is preserved with
direct aliases in pyright_profile, including the exception's serialization path.
This is the existing Pyright metadata dialect, not a new general JSON standard.

The strict decoder keeps UTF-8 validation, object-only results, duplicate-key
refusal, integral-number policy and caller-selected malformed/recursion codes.
The JSONC passes keep string/escape behavior, newline preservation and the existing
trailing-comma rules. Normalizing comments is not byte-offset preservation and is
not full syntax validation; the strict decoder still follows it. Filesystem reads,
size/domain/depth limits, deadlines, configuration inheritance and server discovery
remain with their existing callers. No limits or error codes are added or relaxed.

The owner uses only standard libraries. Original repository/configuration and
lockfile integration tests remain unchanged. Isolated tests compare exact normalized
bytes and accepted/error outcomes with a baseline recorded before transfer; real
profile/discovery integration remains mandatory. The complete original scanner
refactoring research accompanies this packet, not a shortened paraphrase.

## Research — 2026-11-04

Checked [Python JSON](https://docs.python.org/3.14/library/json.html),
[RFC8259](https://www.rfc-editor.org/info/rfc8259/),
[pinned Pyright configuration](https://raw.githubusercontent.com/microsoft/pyright/1.1.411/docs/configuration.md)
and the explicitly draft [JSONC specification](https://jsonc.org/).
JSONC trailing-comma behavior is configurable across implementations; the draft
is not a reason to change this product's already tested dialect. A replacement
parser or JSON5 dependency could change duplicate-key, numeric and malformed-input
behavior. Regex-based comment removal cannot preserve strings correctly. Keep the
existing two-pass scanner and strict decoder in one value owner; keep IO outside.
No package/runtime upgrade or claim of complete upstream Pyright parser equivalence.

Gates: exact original ASTs, baseline bytes/outcomes, unchanged integration assertions,
isolated stdlib import, actual per-function complexity/shape, measured test weights,
full source/context packet <=100000 o200k_base tokens and byte-exact reconstruction.
A finished packet is not completion of whole-project decomposition or deployment.

The installer has a separate strict JSON path with PyrightInstallError, explicit
64-level/4096-node traversal and canonicalizability checks. This transfer does
not silently replace it with discovery's decoder: their error/domain contracts
differ. Those existing limits are not introduced or changed here. Any later
shared loader must preserve both callers' checks and error causes explicitly.

A new test initially assumed JSON nesting at sys.getrecursionlimit()+1 would
raise. On the qualified Python 3.14.7 runtime, both the original decoder and the
transferred one accept depth 1001; this was a test assumption, not a transfer
regression. The caller-selected recursion-error mapping is checked by explicit
fault injection at json.loads. The fifteen real-input before/after cases remain,
as do all original configuration integration assertions. No numeric nesting limit
is invented or changed to make that test pass.
