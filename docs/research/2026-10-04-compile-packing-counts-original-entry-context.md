# Compile packing counts its complete original-entry metadata

Research and reproduction date: 2026-10-04. Minimum Python 3.10; qualification
runtime Python 3.12.3. No structure, environment, persisted format or runtime
location changes are introduced by this correction.

The v4 compiler added original-entry metadata to its actual draft prompt. Its
optimized additive byte planner still counted only the empty prompt and source
blobs. It therefore filled optional context into space already used by that
metadata. Actual provider admission refused the resulting requests before a
model call. On the immutable problematic daily source, the first physical part
was planned as 22,034 bytes although its actual serialized request had 22,281.
A real failed compilation reproduced this error repeatedly; its failures and
controlled interruption remain private evidence.

The byte planner now includes the exact UTF-8 size of the selected original-entry
metadata, less the empty metadata already included in its fixed base. This retains
optimized source-size reuse and shares the production metadata serializer.
The actual per-model tokenizer path remains non-additive. Byte estimates are
planning estimates, never provider-independent upper-bound guarantees or evidence
of lower monetary cost.

Alternatives were to serialize every complete request for every prospective
optional source (correct but repeats large source serialization), or add a fixed
slack for metadata (incorrect because paths, offsets and entry IDs vary). Exact
serialized metadata retains the existing source optimization without a new limit,
constant allowance or setting. Nothing is removed from the model's input.

Fresh primary research: [RFC 8259](https://www.rfc-editor.org/rfc/rfc8259)
describes JSON string serialization and UTF-8; [Python 3.10.22 str.encode](https://docs.python.org/3.10/library/stdtypes.html#str.encode)
specifies conversion from text to bytes; [Anthropic token counting](https://platform.claude.com/docs/en/build-with-claude/token-counting)
accepts complete structured requests including system context and tools. The
Anthropic endpoint is not called by this repair, and the byte estimator is not
claimed to reproduce a provider tokenizer or its hidden host instructions.

Guards compare the planner with the actual complete serialized request for legacy
and original-context inputs, with and without Unicode optional sources. A boundary
guard constructs an optional source whose complete request exceeds the existing
budget by one byte: the original implementation wrongly admitted it. Before the
fix, three guards failed and two passed. No previous assertion is weakened.
Related transaction, continuation and packing checks are run in an isolated
checkout; actual changed callable complexity is measured with Lizard and AST
shape checks. The full useful problematic-day compile and corrected nightly are
separate qualification requirements; this repair alone does not establish them.
