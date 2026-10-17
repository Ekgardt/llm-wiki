# A grounded question uses the shared window

2026-10-02. The correction is installed under the existing maintenance fence with preserved preimages and verified afterimages. The wider audit remains open.

Ordinary search had removed a separate question-length ceiling, but the grounded answer still rejected text longer than 16384 characters before checking its actual shared prompt window. Two regressions failed: a longer question fitting the existing window was rejected, and an over-window question reported the unrelated character ceiling. Type and nonempty-string refusals remained valid.

The corrected admission preserves the whole question and uses the existing ContextBudget, including the system prompt, output reservation and safety margin. It checks the minimum prompt before query analysis, retrieval or corpus collection, then retains the existing complete assembled-prompt check before the provider. The same question-block helper preserves exact delimiters and text in both checks. No ceiling is raised, context is not truncated, and no model flag, path, schema, setting or environment contract changes.

Simply deleting the character ceiling initially passed six functional cases but spent 62.95 seconds in the test file. A further regression proved an over-window request reached query analysis. The existing-window preflight now refuses that request before analysis; all six scenarios pass in 2.84 seconds. A fitting long question reaches the test generator once and whole, with a verified source citation; an over-window question never calls a model. These are controlled regression measurements, not native production latency or answer-quality evidence.

Primary sources checked on 2026-10-02:

- [MCP tools specification, 2025-11-25](https://modelcontextprotocol.io/specification/2025-11-25/server/tools): caller argument schemas do not prescribe a 16384-character question limit.
- [RFC 8259](https://www.rfc-editor.org/rfc/rfc8259): JSON string representation and permitted implementation limits do not justify this particular application ceiling.
- [Python 3.10 JSON documentation](https://docs.python.org/3.10/library/json.html): untrusted JSON needs resource controls; the implementation does not select this question-size rule.
- [OWASP API4:2023](https://api-security.owasp.org/editions/2023/en/0xa4-unrestricted-resource-consumption/): resource controls should protect the actual operation, including work done before a refusal.

Keeping or raising the independent character cap preserves two policies that disagree. Making it configurable adds an operator contract without establishing its basis. Deleting it without preflight admits unnecessary expensive analysis. The existing actual prompt window is the chosen admission boundary: fitting text stays whole; larger text receives the named shared-budget refusal. This does not certify the optimality of every other existing numerical value.

Final related and mandatory verification: 122 passed in 54.40 seconds. Actual Lizard measurement covers all eight changed/new functions including the nested generator, maximum CCN 4. Formatting validation found an import-order issue and corrected it without suppressing the check. No production fake provider is introduced. The earlier full native ten-case answer trial remains insufficient and is not declared successful by this fix.
