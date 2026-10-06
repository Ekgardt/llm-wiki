# The draft explains its immutable-decision boundary

Observed on 2026-10-06, at installed commit ee3cb687: a normal real source-work draft and critique proposed an update to an existing decision. Normalization correctly refused it twice. The retry reused the same request. The source-address repair did not repair this separate target-selection problem.

The draft's own trusted instructions did not explain the immutable-decision rule enforced by `_require_mutable_compile_target`. Existing pages are untrusted source content; depending on the selected context to carry an operating rule is insufficient. This is a reproduced instruction/validator mismatch, not proof that instructions can guarantee semantic correctness.

The candidate explains that an existing page with YAML frontmatter `type: decision` cannot be updated or targeted by a create operation. It preserves new durable knowledge in a separate evidenced page linked to the existing decision, and permits genuinely new decisions. The draft program version changes so earlier cached drafts are not mistaken for drafts made under this instruction. Source IDs, source bytes, schema, evidence binding, DLP, immutable-target validation and ordinary updates remain unchanged.

Alternatives considered: removing the validator would violate the contract; silently dropping decision operations would lose knowledge; returning raw exception/source content as trusted retry instructions could introduce untrusted authority; narrowing the schema to only creates would remove legitimate ordinary updates. A direct rule in the existing trusted instruction is the smallest compatible correction. Specific validated retry feedback remains a separate possible improvement; it is not implemented here.

Current primary sources, checked 2026-10-06:

- [OpenAI structured outputs](https://developers.openai.com/api/docs/guides/structured-outputs): structural conformance does not eliminate semantic mistakes; instructions and independent validation are still necessary. This is a general principle, not a claim that the Codex CLI uses that API feature.
- [JSON Schema 2020-12 validation](https://json-schema.org/draft/2020-12/json-schema-validation): schema keywords validate instance properties; the current general action enum does not describe repository-specific target immutability.
- [OWASP prompt injection prevention](https://cheatsheetseries.owasp.org/cheatsheets/LLM_Prompt_Injection_Prevention_Cheat_Sheet.html): separate instructions from untrusted data and retain validation. No source-authored instruction is promoted to authority by this change.

Qualification starts with three failing instruction/cache tests against the installed compiler. Existing real-path tests continue to prove immutable decisions are refused, ordinary updates and new decisions work, and explicit recoverable operator transactions remain possible. Real useful-result quality, all retries, time and token cost still require measurement after installation. This candidate alone does not close audit point 7.

Evidence: `logs/audit-2026-10-06-step7-normal-current-day-after-source-address-repair.json`, its stderr, `logs/audit-2026-10-06-step7-decision-instruction-navigation.json`, `logs/audit-2026-10-06-step7-decision-instruction-original-red.log`, and the related-test log/XML.
