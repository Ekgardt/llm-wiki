# Byte-exact sentence pruning

Owns the unchanged `scripts/evidence_pruning.py` in place. It accepts source bytes,
a byte range, a question and an encoder callable, and returns source byte spans.
It owns turn recognition, sentence boundaries, cosine scoring, stable tie order,
selection under the existing byte budget and adjacent-span merging.

The first sentence and the existing minimum sentence count remain protected;
short turns remain whole. Both user and assistant turns can be pruned. Text used
for scoring is decoded with the existing replacement policy; citations still
refer to the original bytes. Existing thresholds and their documented reasons
are unchanged. The module reads no clock, generation, vault or network state.

`query_memory` owns context assembly, deadlines, encoding-provider selection and
citation verification. Four original unit regressions and their exact numerical
fixtures now live in this packet. The original context/hash integration imports
that same fixture and remains intact, as do clock and base-install integrations.
Those suites have their own module owners.

Whole packet: `evidence-pruning-context.json`, including full local source closure,
instructions, tests and dated research. NumPy remains a lazy external dependency;
checked imports and isolated import reject product coupling. Production hashes
and original test ASTs are retained as transfer evidence. No new pruning behavior
or end-to-end token/quality gain is claimed by this ownership change.

See [research](../research/2026-11-03-evidence-pruning-owner.md).
