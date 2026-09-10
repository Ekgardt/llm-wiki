"""Write our answers in the file format LongMemEval's own `evaluate_qa.py` reads.

    uv run python benchmark/longmemeval_hypotheses.py --results <results.jsonl>

One line per question: `{"question_id": ..., "hypothesis": ...}`. A refusal is
rendered as the sentence the product shows a person, so the authors'
abstention template has something to judge. Anyone with an OpenAI key can then
run `python3 evaluate_qa.py gpt-4o <this file> data/longmemeval_oracle.json`
and obtain the number the leaderboards publish, judged by the same model.
See `longmemeval_official.py`.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

BENCHMARK_DIR = Path(__file__).resolve().parent
if str(BENCHMARK_DIR) not in sys.path:
    sys.path.insert(0, str(BENCHMARK_DIR))

from longmemeval_official import hypothesis_lines  # noqa: E402


def _rows(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--results", required=True)
    parser.add_argument("--out", default=None, help="default: <results>.hypotheses.jsonl")
    args = parser.parse_args(argv)
    results = Path(args.results).resolve()
    out = Path(args.out).resolve() if args.out else results.with_suffix(".hypotheses.jsonl")
    lines = hypothesis_lines(_rows(results))
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"{len(lines)} hypotheses -> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
