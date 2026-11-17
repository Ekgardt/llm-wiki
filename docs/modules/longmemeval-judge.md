# Optional LLM-judge pass over LongMemEval results (MEM-10)

`benchmark/longmemeval_judge.py` and the tests that exercise them most.

LongMemEval's official metric is an LLM judge; the deterministic metrics in `longmemeval_score.py` under-credit answers that state a correct alias or paraphrase. This pass reads the results JSONL written by `run_longmemeval.py`, asks the configured provider one short grading question per answered row, and writes a judged JSONL next to it (resumable).

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.
