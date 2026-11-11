# Shared prefixes pay their actual cost

Research date: 2026-10-03. Python 3.12.3 installed, Python 3.10 supported.

Actual private get_architecture callers for _prefixable_column, _cheaper_of and _as_columnar precede implementation, using fresh ROOT0a18550f. Partial envelopes retained at logs/audit-2026-10-03-prefix-cost-before-architecture.json; relevant unchanged source checked directly. No schema, structure, environment, resource budget or runtime location changes.

The path-prefix compactor already compares the complete original and compacted JSON shapes including the prefix report. A separate eight-character cutoff rejects shorter prefixes before that comparison, regardless of savings. Choose to remove that cutoff and use the existing measured-shape decision alone. Increasing or configuring eight merely retains a redundant heuristic. Always compacting could enlarge short answers; the actual cost gate remains. Character count cannot stand in for token savings, especially for Unicode. The existing UTF-8 byte estimate is a local conservative proxy, not a provider bill or tokenizer count; no true token or full-task latency improvement is asserted here.

Independent primary sources checked on the research date:

- [Python 3.10 commonprefix](https://docs.python.org/3.10/library/os.path.html#os.path.commonprefix): it computes a character prefix; the existing final-separator cut remains, so no filesystem or commonpath claim is made.
- [RFC 8259](https://www.rfc-editor.org/rfc/rfc8259): JSON strings represent Unicode sequences and arrays retain order. Both row values and the shared prefix remain in the response, permitting exact reconstruction.
- [OpenAI tiktoken](https://github.com/openai/tiktoken): BPE encodes text into tokens, so a character-count threshold is not a model-cost measurement. No dependency or tokenizer/provider change is introduced.

Required regression: short repeated prefix must be hoisted when complete compacted representation costs less, exact original paths reconstruct including Unicode; an unprofitable prefix must remain unchanged. Existing answer shaping/budget/citation tests must pass. Remove the obsolete constant and its comment; retain other outstanding limits openly. This removes one unsupported threshold and does not close the full law-9 finding.

Candidate evidence: genuine original-code regression2 failed/1 passed0.48s. After removing the cutoff,29 answer-budget/new tests passed1.25s;3 new weight tests passed0.45s. Actual Lizard/AST inspection across both pending changes:21 records maxCCN4;fullRuff passed. Installation and actual provider token/full-task improvement are not claimed by these temporary tests.

Additional affected answer/architecture/weight qualification:53 passed3.67s. FullRuff passed and Gitleaks42.66MB passed4.07s. No unsupported depth cap was disguised as fixed; it remains a separate open item.
