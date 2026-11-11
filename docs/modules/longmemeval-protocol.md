# LongMemEval protocol and interchange

Owns benchmark/longmemeval_official.py and benchmark/longmemeval_hypotheses.py
at their existing paths, byte-identical. Templates, abstention detection, refusal
rendering, label parsing, accuracy and hypothesis JSONL belong together. The thin
CLI reads a supplied local file and writes hypotheses; it does not call a judge.

Preserve the existing yes-substring parser, bool-only labels, rounding, missing
values, Unicode JSON, blank-line handling, output naming and errors. These are
compatibility contracts, not newly validated statistical choices. Input files are
operator-supplied; the exporter loads them wholly and is not an untrusted-input
sandbox. No runtime root, dependency, public command or model configuration changes.

Eight original pure/export tests move into this owner's test package; assertions
are unchanged. The exporter fixture explicitly writes and reads UTF-8: the shared
encoding guard caught its inherited host-code-page dependency after the move.
The synthetic row helper has one location and four retained judge integrations
import it. No assertion is removed or duplicated. Run tests/modules/longmemeval_protocol
and tests/test_the_official_protocol_on_our_judge.py through normal pytest bootstrap.
Complete source, tests, instructions and September 8 history stay in the packet,
measured with o200k_base at at most 100000 tokens and reconstructed exactly.
The historical record also covers judge execution and remains shared context.
Historical scores and source-header claims are not fresh leaderboard evidence.

## Qualification research — 2026-11-03

The built-in index at 00d38f78, get_architecture, callers and full source search
identify the judge and exporter consumers; all 12 original tests pass. Keeping
these two existing cohesive files avoids either coupling protocol values to model
execution or fragmenting a small interchange adapter into an arbitrary owner.
Python 3.14 is already approved; no dependency or runtime upgrade is involved.

Sources checked: [bounded contexts](https://martinfowler.com/bliki/BoundedContext.html),
[import contracts](https://import-linter.readthedocs.io/en/stable/contract_types/),
[JSON semantics](https://docs.python.org/3.14/library/json.html), and the
[upstream evaluator](https://github.com/xiaowu0162/LongMemEval/blob/main/src/evaluation/evaluate_qa.py).
These support explicit ownership and unchanged interchange semantics. This work
neither runs the upstream judge nor claims that different judges yield equal scores.

## Canonical structure contract — 2026-11-03

[LongMemEval protocol](longmemeval-protocol.md) owns the existing local
grading templates, label/accuracy calculations and hypothesis JSONL exporter.
It performs no model calls. Judge execution and reports remain with consumers.
Public paths, source bytes and all original assertions are preserved.
