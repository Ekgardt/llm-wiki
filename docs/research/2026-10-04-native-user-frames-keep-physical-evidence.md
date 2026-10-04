# Native user frames retain their complete physical evidence

Research date: 2026-10-04. Product minimum: Python 3.10; qualification runtime:
Python 3.12.3. This implements the atomic-frame alternative selected in the
2026-10-03 audit investigation, within corpus-generation/v2 and evidence-graph/v2.
It adds no persisted schema, runtime root, module directory, environment contract,
or MCP tool. Extractor v6 identifies the changed physical chunk boundaries.

The actual closed daily source contains 460 native user prompts. Seventy-eight
encoded frames exceeded the existing 4096-byte paragraph chunk budget. All 460
inline frames matched their verified permanent breadcrumb input, and each
permanent input occupied one transport part. The old Markdown-only turn reader
recognized none. These source proofs do not by themselves prove canonical
transaction completion for every capture or successful model extraction.

The selected representation keeps a complete encoded JSON frame as one physical
retrieval chunk. The model sees the decoded prompt; its Turn still names the
physical file, byte range, whole-source digest and physical-span digest. A complete
native event embedded in one verified breadcrumb part uses that part's physical
JSON container. Search keys remain hints beside the original text and are never
citation authority. The original 4096-byte splitting rule still applies to
ordinary Markdown. Complete native frames may cost more context tokens; existing
provider admission and deadlines must refuse unsuitable work visibly.

Native facts require more than a heading. The reader verifies the permanent
head and complete part chain, compares the exact input, and reconstructs the
canonical producer journal block, including operation marker, clock and both
links. Journal byte offsets are indexed once per source. Permanent input and
head reads are cached within the caller's deadline. Daily and permanent aliases
of the same occurrence produce one turn, preferring the daily frame. Equal text
from distinct occurrences is not content-deduplicated. The nightly fact-key
collector retains its daily-only scope and selectively follows those canonical
links; ordinary raw session dumps remain excluded.

Multipart native user input without a complete physical frame remains explicitly
pending. Its verified physical parts still enter the corpus and remain searchable;
we do not attach all whole-event facts to the first fragment or invent a multi-span
citation. Multipart tool data creates no user facts. Unknown native versions,
invalid required payloads and malformed complete native input are visible refusals.
Documentation examples and ordinary indented JSON remain ordinary source data.

Alternatives considered: parsing each 4096-byte fragment misses boundary-crossing
JSON and creates false ownership; decoded fragments need a complete escape-to-byte
offset map before they can cite physical bytes; multi-chunk projections require
new persisted evidence semantics and a separate decision. The selected complete
physical container uses the existing single-span authority and does not distribute
one fact among unrelated spans.

The common default source admission now uses the maximum of the already supported
knowledge-page and daily-evidence read budgets. Explicit caller overrides remain
exact, and the Markdown writer's page budget is unchanged. The actual closed
15,052,338-byte daily source had exceeded the former default 8 MiB admission,
although the production fact-key collector already admitted daily evidence at
16 MiB. This change introduces no independent numerical setting.

## Fresh primary research and limits

[RFC 8259](https://www.rfc-editor.org/info/rfc8259/) describes JSON strings,
escapes and UTF-8 interoperability. [Python 3.10 JSON](https://docs.python.org/3.10/library/json.html)
documents decoding and duplicate-name behavior. Canonical re-encoding rejects
duplicate native fields and noncanonical or unsupported input; character indices
from decoding are never treated as UTF-8 byte offsets. The
[OWASP prompt-injection guidance](https://cheatsheetseries.owasp.org/cheatsheets/LLM_Prompt_Injection_Prevention_Cheat_Sheet.html)
supports treating captured text as data and validating outputs rather than giving
embedded role markers instruction authority.

[LongMemEval](https://arxiv.org/abs/2410.10813),
[LongMemEvalV2](https://arxiv.org/abs/2605.12493),
[Eywa](https://arxiv.org/abs/2605.30771) and
[AgentZeroMemory](https://arxiv.org/abs/2608.29606) were considered for evidence
preservation, retrieval separation and cost-sensitive evaluation. Their reported
benchmarks were not reproduced here. This repair does not introduce the papers'
additional agent or memory architectures and makes no general quality claim.

The real model qualification also exposed ASCII-only ledger canonicalization:
Russian extracted categories disappeared. The shared derived tokenizer now uses
stdlib NFC normalization, Unicode letters and combining marks, while retaining
English stopwords/plural handling, apostrophes, hyphens and the existing ASCII
digit/underscore separators. [Python re](https://docs.python.org/3.10/library/re.html),
[unicodedata](https://docs.python.org/3.10/library/unicodedata.html),
[UAX 29](https://www.unicode.org/reports/tr29/) and
[SQLite unicode61](https://www.sqlite.org/fts5.html#unicode61_tokenizer) explain
the available primitives and their differing scope. This is not a full UAX 29
language-specific segmenter: an uninterrupted CJK expression remains one word.
Python 3.10's Unicode table and the installed runtime's table also differ from the
current Unicode standard. No history is erased or automatically rekeyed. Existing
mixed-script derived rows can change canonical identities on future extraction;
their retained source pointers and existing-store inventory must be reviewed before
making any global count or migration claim.

Linked native turns inherit the capture day from verified source metadata, rather
than guessing a date from a digest filename. A ledger record still has dated=false
unless the user explicitly stated its date. The prompt and physical citation remain
unchanged.

## Qualification boundary

Genuine original failures and their later regressions are retained in private audit
logs. Focused checks cover complete Unicode/escaped frames, source/hash/range
identity, daily-only canonical following, alias preference, explicit override,
typed rejection, deadlines, multipart pending, documentation data, legacy reading,
Unicode ledger retention and a controlled collect-to-store-to-FTS physical hit.
The complete existing callable bodies, nested functions and lambda expressions
are measured with Lizard and AST shape checks. The real provider cycle and its
cost are separate qualification evidence; a controlled reply is not that cycle.
Passing this slice alone does not close the remaining original audit items or
qualify every nightly phase.

Current qualification: 282 related tests passed, three skipped; the native subset
has 28 passing cases. Actual source-diff qualification measured 86 changed or
written callable bodies, including nested callables: maximum CCN 5, no excess
if count or nesting. Ruff and diff whitespace checks passed. Original scope,
Unicode and real-source reader failures remain retained separately.

One read-only pass over the unchanged closed 15,052,338-byte daily source admitted
all 460 verified native user turns, including all 78 frames above 4096 bytes.
Every physical citation hash matched. Collection took 2.7725 seconds and source
authority took 1.5651 seconds: 4.3376 seconds total, with no model calls or
production state writes. This is one measured workload, not a speedup comparison.

The separate genuine-event useful cycle used two exact immutable source files,
real extraction and grounded-answer provider responses, nine posted ledger rows,
an active two-source generation and a matching physical search citation verified
against the original source bytes. Provider usage was 32,771 input and 557 output
tokens, with 24,576 cached input tokens; monetary cost remains unknown. Its failed
first run is preserved. Later qualification reuses the exact retained responses
only after matching the prompt, system and physical-source identities. It is
qualification of that real event, not proof that the whole vault's nightly pass
has completed; global corpus entry and aggregate-byte constraints remain separate
inventory items.
