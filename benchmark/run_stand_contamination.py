"""How much of each stand's score comes from the stand finding its own sheet.

Both vault stands keep questions and gold pages in a JSON file under
`benchmark/`, an approved corpus root, so the sheet is indexed alongside the
pages it grades. This measures what that is worth, paired: one retrieval per
question, scored three ways off the same ranking, so the difference between the
numbers is the exclusion policy and nothing else.

    raw      nothing excluded — what the stands would report if nobody had
             thought about this at all.
    shipped  the exclusions as they stood on 2026-08-28: each stand naming one
             path in a module constant.
    clean    every file that states a case verbatim, derived from disk by
             `benchmark/answer_key.py`, dropped from the ranking.

The two directions are opposite and worth stating before the numbers arrive.
For the retrieval stand a hit requires the gold page itself, so the sheet can
only take a slot away — excluding it can raise the score and never lower it.
For the application stand the sheet carries every expected token verbatim, so
retrieving it passes the case without the gold page — excluding it can lower
the score and never raise it. A `clean` applied@5 below `raw` is the true one.

    uv run python benchmark/run_stand_contamination.py
    uv run python benchmark/run_stand_contamination.py --json
    uv run python benchmark/run_stand_contamination.py --observations obs.json
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
for _extra in (ROOT / "scripts", ROOT / "benchmark"):
    if str(_extra) not in sys.path:
        sys.path.insert(0, str(_extra))

import answer_key  # noqa: E402
import run_vault_application as application  # noqa: E402
import run_vault_retrieval as retrieval  # noqa: E402
from retrieval_paths import DEFAULT_PATH, PATHS, observe, warm  # noqa: E402

TOP_K = retrieval.TOP_K
# The paths each stand named in a constant before the derived set replaced them.
SHIPPED_RANK_EXCLUSIONS = frozenset({"benchmark/vault-retrieval-v1.json"})
SHIPPED_TEXT_EXCLUSIONS = frozenset(
    {"benchmark/vault-retrieval-v1.json", "benchmark/vault-application-v1.json"}
)


def _corpora() -> tuple[dict, dict]:
    return (
        retrieval.load_corpus(retrieval.CORPUS),
        application.load_corpus(application.CORPUS),
    )


def _policies(keys: frozenset[str]) -> dict[str, tuple[frozenset[str], frozenset[str]]]:
    """name -> (dropped from the ranking, dropped from the scored text)."""
    empty: frozenset[str] = frozenset()
    return {
        "raw": (empty, empty),
        "shipped": (SHIPPED_RANK_EXCLUSIONS, SHIPPED_TEXT_EXCLUSIONS),
        "clean": (keys, keys),
    }


def _kept(paths: list[str], dropped: frozenset[str], limit: int) -> list[str]:
    return [path for path in paths if path not in dropped][:limit]


def _observe_one(question: str, depth: int, path: str) -> dict[str, object]:
    seen = observe(path, question, depth)
    return {
        "paths": seen.result_paths,
        "seconds": seen.seconds,
        "effective_mode": seen.trace.get("effective_mode"),
        "fallback_reason": seen.fallback_reason,
    }


def _asked(case: dict) -> str:
    return str(case.get("question") or case["task"])


def observe_corpus(
    corpus: dict, vault: Path, depth: int, path: str
) -> list[dict[str, object]]:
    """One retrieval and one grep per case, kept deep enough to re-score."""
    records = []
    for case in corpus["cases"]:
        question = _asked(case)
        record = dict(case)
        record.update(_observe_one(question, depth, path))
        record["grep_paths"] = retrieval.grep_ranking(vault, question, depth)
        records.append(record)
    return records


def _hit(record: dict, key: str, dropped: frozenset[str]) -> bool:
    ranked = _kept(list(record[key]), dropped, TOP_K)
    return str(record["gold_path"]) in ranked


def _rate(flags: list[bool]) -> float:
    if not flags:
        return 0.0
    return round(sum(1 for flag in flags if flag) / len(flags), 4)


def _first_hit(record: dict, key: str, dropped: frozenset[str]) -> bool:
    ranked = _kept(list(record[key]), dropped, 1)
    return str(record["gold_path"]) in ranked


def score_retrieval(
    records: list[dict], dropped: frozenset[str], _text_dropped: frozenset[str], vault: Path
) -> dict[str, float]:
    return {
        "hit_at_1": _rate([_first_hit(item, "paths", dropped) for item in records]),
        "hit_at_5": _rate([_hit(item, "paths", dropped) for item in records]),
        "grep_hit_at_5": _rate([_hit(item, "grep_paths", dropped) for item in records]),
    }


def _applied(record: dict, key: str, vault: Path, policy: tuple[frozenset, frozenset]) -> bool:
    dropped, text_dropped = policy
    ranked = _kept(list(record[key]), dropped, TOP_K)
    text = "\n".join(
        retrieval._readable(vault / path) for path in ranked if path not in text_dropped
    )
    return application.applied(text, [str(token) for token in record["expected_tokens"]])


def score_application(
    records: list[dict], dropped: frozenset[str], text_dropped: frozenset[str], vault: Path
) -> dict[str, float]:
    policy = (dropped, text_dropped)
    return {
        "applied_at_5": _rate([_applied(item, "paths", vault, policy) for item in records]),
        "grep_applied_at_5": _rate(
            [_applied(item, "grep_paths", vault, policy) for item in records]
        ),
    }


def _key_ranks(records: list[dict], keys: frozenset[str]) -> dict[str, list[int]]:
    """Where each sheet landed for the questions it holds the answers to."""
    ranks: dict[str, list[int]] = {name: [] for name in sorted(keys)}
    for record in records:
        _collect_ranks(list(record["paths"]), ranks)
    return ranks


def _collect_ranks(paths: list[str], ranks: dict[str, list[int]]) -> None:
    """Record only the sheets; every other retrieved path is not our subject."""
    for index, path in enumerate(paths, start=1):
        _record_rank(ranks, path, index)


def _record_rank(ranks: dict[str, list[int]], path: str, index: int) -> None:
    if path in ranks:
        ranks[path].append(index)


def _stand_report(
    records: list[dict], scorer, keys: frozenset[str], vault: Path
) -> dict[str, object]:
    scores = {
        name: scorer(records, dropped, text_dropped, vault)
        for name, (dropped, text_dropped) in _policies(keys).items()
    }
    return {
        "case_count": len(records),
        "scores": scores,
        "key_ranks_within_depth": _key_ranks(records, keys),
    }


def run(
    vault: Path, *, path: str = DEFAULT_PATH, depth: int | None = None, warmup: bool = True
) -> dict[str, object]:
    corpora = _corpora()
    keys = answer_key.answer_key_paths(vault, corpora)
    asked_depth = depth or TOP_K + len(keys)
    warmup_seconds = warm(path) if warmup else 0.0
    records = [observe_corpus(corpus, vault, asked_depth, path) for corpus in corpora]
    return {
        "retrieval_path": path,
        "depth": asked_depth,
        "warmup_seconds": warmup_seconds,
        "answer_key_paths": sorted(keys),
        "retrieval": _stand_report(records[0], score_retrieval, keys, vault),
        "application": _stand_report(records[1], score_application, keys, vault),
        "observations": {"retrieval": records[0], "application": records[1]},
    }


def _print_stand(name: str, report: dict) -> None:
    print(f"\n{name} (n={report['case_count']})")
    for policy, scores in report["scores"].items():
        rendered = "  ".join(f"{key}={value}" for key, value in scores.items())
        print(f"  {policy:<8} {rendered}")
    for path, ranks in report["key_ranks_within_depth"].items():
        print(f"  sheet in results: {path} at ranks {ranks}")


def _print_report(report: dict) -> None:
    print(f"retrieval_path: {report['retrieval_path']}  depth: {report['depth']}")
    print(f"answer key: {report['answer_key_paths']}")
    _print_stand("retrieval stand", report["retrieval"])
    _print_stand("application stand", report["application"])


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--vault", type=Path, default=ROOT)
    parser.add_argument("--path", choices=PATHS, default=DEFAULT_PATH)
    parser.add_argument("--depth", type=int, default=None)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--observations", type=Path, default=None)
    parser.add_argument("--no-warm", dest="warmup", action="store_false")
    return parser.parse_args(argv)


def _save(report: dict, destination: Path | None) -> None:
    if destination is None:
        return
    destination.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8"
    )


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    report = run(args.vault, path=args.path, depth=args.depth, warmup=args.warmup)
    _save(report, args.observations)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        _print_report(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
