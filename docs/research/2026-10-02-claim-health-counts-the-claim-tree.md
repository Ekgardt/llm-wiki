# Claim health counts the claim tree

2026-10-02. Checking the compile forecast's related scopes found the same mismatch in claim health. Doctor counted every project Markdown file, including large journals and project README. The claim tree accepts only project state.md and context.md, plus note pages. In the working vault, the generic forecast reported about 28.9 MB while the actual claim selector returned 1211 files and 3,215,677 bytes. This discrepancy is a false forecast, not evidence that the existing numerical ceiling is justified.

Three regressions reproduced the original defect. A real readonly claim snapshot read 66 bytes while health reported 8258 bytes. The corrected forecast shares the claim reader's existing source discovery. Discovery is separated from admission: health can report an overflow, while the claim reader still rejects it. No numerical ceiling, schema, setting, runtime location or path contract changes. Other pipeline scopes remain separately defined; discovery limits can count files before content filters, so a final result filter alone does not define their scope.

Primary sources checked on 2026-10-02:

- [Python 3.10 pathlib glob](https://docs.python.org/3.10/library/pathlib.html#pathlib.Path.glob), for explicit traversal scope.
- [OpenTelemetry Metrics Data Model, stable](https://opentelemetry.io/docs/specs/otel/metrics/data-model/), for measurement entity and units.
- [Prometheus instrumentation guidance](https://prometheus.io/docs/practices/instrumentation/), for useful, clearly defined diagnostics.

Raising the ceiling would preserve the faulty measurement. Copying the project's accepted filenames into doctor risks policy drift. Sharing discovery preserves one source policy and retains the existing admission checks. A forecast does not provide a filesystem stability or writer-admission guarantee; filesystem refusal remains visible.

Verification: original three cases failed; corrected new and related health tests passed 216 cases with three skips. Claim-reader and compiler regressions plus mandatory guards passed 162 cases. Eight changed/new functions, including tests, were measured by Lizard; maximum CCN 3. Ruff initially rejected test import order and was corrected. Installation and its native index update remain pending at this checkpoint. Exact c3f91a7 full baseline: 10862 passed, 206 skipped, nine warnings in 1740.28 seconds. That baseline does not claim to be a full run of this later change.

Historical measurements and retained undo evidence remain evidence; no queue, transaction or knowledge record is deleted by this change.
