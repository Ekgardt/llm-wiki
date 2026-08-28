"""Answer one LongMemEval question twice — without and with consolidation (MEM-13).

MEM-13 asks whether this vault's consolidation earns its cost. The only way to
attribute a difference to consolidation is to change nothing else, so both arms
run in **one process, on one vault, over one ingest**:

1. the haystack is ingested exactly as `longmemeval_vault` ingests it — session
   evidence plus one transactional daily entry per session;
2. **arm `baseline`** builds a generation over the daily files as they stand
   and answers the question;
3. `episode_consolidation.consolidate_day` then runs over every day that has
   session records — the nightly step, with the real provider;
4. **arm `consolidated`** rebuilds a generation over the daily files as they
   now stand, and answers the same question the same way.

The two generations are necessarily different — arm `consolidated`'s corpus is
arm `baseline`'s corpus plus the consolidation entries, which is the whole
point. Everything else is held fixed: same vault, same ingested sessions, same
builder configuration and reuse config, same retrieval profile resolution and
candidate count, same answer budget, same provider, same process.

Consolidation *appends*; it never replaces the raw entries. That is the product's
behaviour and this stand does not improve on it.

One product fact this stand had to accommodate:
`daily_log_append.append_daily` writes to **today's** daily file regardless of
the day being consolidated (it derives the filename from `datetime.now()`), so
the consolidation of a 2023 haystack day lands in `knowledge/daily/<today>.md`.
Both arms therefore discover their daily files by scanning the directory rather
than by reusing the ingest list, or arm `consolidated` would index a corpus that
does not contain its own consolidation output.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

BENCHMARK_DIR = Path(__file__).resolve().parent
if str(BENCHMARK_DIR) not in sys.path:
    sys.path.insert(0, str(BENCHMARK_DIR))

import longmemeval_vault as vault_harness  # noqa: E402

# The header `episode_consolidation.render_block` puts on its daily entry.
# `marker_is_live()` re-derives a real block and checks the marker still occurs
# in it, so a rename upstream is reported as a broken stand rather than silently
# measured as "consolidation reached nothing".
CONSOLIDATION_MARKER = "] episodes | "
ARM_BASELINE = "baseline"
ARM_CONSOLIDATED = "consolidated"


def marker_is_live() -> bool:
    """Does `CONSOLIDATION_MARKER` still occur in a rendered consolidation block?"""
    import episode_consolidation

    sample = episode_consolidation.render_block(
        "1970-01-01",
        [episode_consolidation.Lesson("lesson", "text", "quote", "session")],
        datetime(1970, 1, 1, 0, 0, 0),
    )
    return CONSOLIDATION_MARKER in sample


def daily_relative_paths(root: Path) -> list[str]:
    """Every daily file on disk, as the repository-relative paths the builder wants."""
    directory = Path(root) / "knowledge" / "daily"
    if not directory.is_dir():
        return []
    return sorted(f"knowledge/daily/{path.name}" for path in directory.glob("*.md"))


def marker_hits(text: object) -> int:
    return str(text).count(CONSOLIDATION_MARKER)


def rows_from_consolidation(rows: list[dict]) -> int:
    """How many retrieved candidates carry a consolidation entry.

    Best effort by design: a legacy retrieval row carries its text only when the
    backend supplied one, so this counts what is visible and the authoritative
    number is `prompt_consolidation_hits`, taken from the prompt the answer step
    actually built.
    """
    serialised = [json.dumps(row, ensure_ascii=False, default=str) for row in rows]
    return len([text for text in serialised if CONSOLIDATION_MARKER in text])


def _instrumented_generator(metrics: dict):
    """The shared provider client, measuring the prompt one retrieval handed it."""
    from llm_client import call_llm

    def generate(prompt: str, system_prompt: str, max_tokens: int) -> str | None:
        metrics["prompt_chars"] = len(prompt)
        metrics["est_prompt_tokens"] = round(len(prompt) / 4)
        metrics["prompt_consolidation_hits"] = marker_hits(prompt)
        started = time.monotonic()
        try:
            return call_llm(prompt, system_prompt, max_tokens)
        finally:
            metrics["provider_seconds"] = round(time.monotonic() - started, 2)

    return generate


def _answered(question: dict, root: Path, snapshot: object, rows: list[dict], profile: str) -> dict:
    """The product's grounded answer, with the MEM-10 budget and deadlines."""
    from context_budget import ContextBudget
    from query_memory import QA_MAX_OUTPUT_TOKENS, grounded_qa

    metrics: dict = {}
    try:
        document = grounded_qa(
            vault_harness.dated_question(question),
            vault=root,
            snapshot=snapshot,
            candidates=rows,
            generator=_instrumented_generator(metrics),
            profile=profile,
            budget=ContextBudget(
                None, vault_harness.ANSWER_INPUT_BUDGET, QA_MAX_OUTPUT_TOKENS, 512
            ),
            deadline=time.monotonic() + vault_harness.ANSWER_DEADLINE_SECONDS,
        )
    except Exception as exc:  # noqa: BLE001 - every failure is a scored outcome
        return {
            "status": "error",
            "hypothesis": "",
            "error": f"{type(exc).__name__}: {exc}"[:500],
            "error_kind": vault_harness.error_kind(exc),
            **metrics,
        }
    return {
        "status": str(document.get("status")),
        "hypothesis": vault_harness.hypothesis_of(document),
        "reason": document.get("reason"),
        "claims": len(document.get("claims") or []),
        "citations": len(document.get("citations") or []),
        "error": None,
        "error_kind": None,
        **metrics,
    }


def active_generation_id(state: Path) -> str | None:
    from generation_catalog import GenerationCatalog

    active = GenerationCatalog(state).get_active() or {}
    identifier = active.get("generation_id")
    return None if identifier is None else str(identifier)


def _reuse_config(snapshot: object) -> object:
    return vault_harness._reuse_config(snapshot)


def build_generation(root: Path, state: Path, daily_files: list[str]) -> tuple[object, dict]:
    """Build and activate one generation, advancing from the generation we saw.

    Not `longmemeval_vault.build_generation`, and the difference is the whole
    reason this function exists. That one builds into an empty catalog and
    passes `expected_active=None`; this stand builds a *second* generation into
    a catalog that already has an active one, and
    `GenerationCatalog._cas_activate` compares the live pointer against
    `expected_active` before it will move. `None` against a live pointer is a
    declined compare-and-swap, and the build reports `generation build finished
    but did not activate` — measured on the 2026-08-28 pilot, question
    60036106, which lost both arms to it. Each arm now names the generation it
    started from. The MEM-10 worker is not edited: it is correct for one arm.
    """
    from corpus_snapshot import collect_corpus
    from doctor import (
        _corpus_policy,
        _fresh_generation_id,
        _generation_source_bytes,
        _generation_source_extractor,
        _generation_source_rows,
    )
    from evidence_graph_builder import build_incremental_generation
    from generation_catalog import GenerationCatalog
    from repository_scope import resolve_repository_scope

    deadline = time.monotonic() + vault_harness.BUILD_DEADLINE_SECONDS
    snapshot = collect_corpus(root, code_roots=(), daily_paths=daily_files, deadline=deadline)
    scope = resolve_repository_scope(root)
    catalog = GenerationCatalog(state)
    expected_active = active_generation_id(state)
    built = build_incremental_generation(
        catalog,
        sources=_generation_source_rows(snapshot),
        source_bytes=_generation_source_bytes(snapshot),
        extractor=_generation_source_extractor(snapshot, scope.repository_id),
        reuse_config=_reuse_config(snapshot),
        generation_id=_fresh_generation_id(catalog),
        parent_generation_id=None,
        policy=_corpus_policy(snapshot),
        expected_active=expected_active,
        deadline=deadline,
        cancelled=None,
        repository_scope=scope,
        snapshot=snapshot,
        publication_root=root,
        coordinator=None,
    )
    if not built.activated:
        raise RuntimeError(
            f"generation {built.generation_id} did not activate "
            f"(expected_active={expected_active})"
        )
    active = catalog.get_active() or {}
    info = {
        "generation_id": built.generation_id,
        "previous_generation_id": expected_active,
        "vector_state": active.get("vector_state"),
        "sources": len(snapshot.sources),
        "chunks": len(snapshot.chunks),
    }
    return snapshot, info


def run_arm(question: dict, root: Path, state: Path) -> dict:
    """One arm: build a generation over the daily files as they stand, then answer."""
    build_started = time.monotonic()
    snapshot, build_info = build_generation(root, state, daily_relative_paths(root))
    plain = str(question["question"])
    profile = vault_harness.profile_for(plain)
    retrieve_started = time.monotonic()
    rows = vault_harness._retrieved_rows(plain, profile)
    answer_started = time.monotonic()
    outcome = _answered(question, root, snapshot, rows, profile)
    finished = time.monotonic()
    return {
        "profile": profile,
        "daily_files": len(daily_relative_paths(root)),
        "retrieved": len(rows),
        "retrieved_from_consolidation": rows_from_consolidation(rows),
        **build_info,
        **outcome,
        "build_seconds": round(retrieve_started - build_started, 2),
        "retrieve_seconds": round(answer_started - retrieve_started, 2),
        "answer_seconds": round(finished - answer_started, 2),
    }


def _counting_call(cost: dict):
    """`episode_consolidation`'s provider call, with its cost written down."""
    import episode_consolidation
    from llm_client import call_llm

    def call(prompt: str) -> str:
        cost["provider_calls"] += 1
        cost["prompt_chars"] += len(prompt)
        started = time.monotonic()
        try:
            return call_llm(
                prompt,
                episode_consolidation.CONSOLIDATION_SYSTEM_PROMPT,
                episode_consolidation.CONSOLIDATION_MAX_TOKENS,
            ) or ""
        finally:
            cost["seconds"] = round(cost["seconds"] + time.monotonic() - started, 2)

    return call


def _consolidate_one_day(root: Path, day: str, call, cost: dict) -> None:
    """One day's consolidation; a failed day is recorded, not fatal.

    `episode_consolidation.main` has the same rule — a day that raises ends that
    day and not the run — so a stand that aborted here would report a harness
    failure where the product reports a skipped day.
    """
    import episode_consolidation

    try:
        outcome = episode_consolidation.consolidate_day(root, day, call=call, state=None)
    except Exception as exc:  # noqa: BLE001 - the row needs a count, not a traceback
        cost.setdefault("failures", []).append(f"{day}: {type(exc).__name__}")
        return
    cost["items"] += int(outcome.get("items") or 0)
    cost["batches"] += int(outcome.get("batches") or 0)


def consolidate_vault(root: Path) -> dict:
    """Run the nightly consolidation step over every day that has records."""
    import episode_consolidation

    cost = {
        "provider_calls": 0,
        "prompt_chars": 0,
        "seconds": 0.0,
        "items": 0,
        "batches": 0,
        "failures": [],
        "marker_live": marker_is_live(),
    }
    days = episode_consolidation.pending_days(root, {})
    cost["days"] = len(days)
    call = _counting_call(cost)
    for day in days:
        _consolidate_one_day(root, day, call, cost)
    return cost


def _entries_written(root: Path) -> int:
    """Consolidation entries visible in the daily log, counted from disk."""
    texts = [
        (Path(root) / relative).read_text(encoding="utf-8", errors="ignore")
        for relative in daily_relative_paths(root)
    ]
    return sum(marker_hits(text) for text in texts)


def _arm_or_failure(question: dict, root: Path, state: Path) -> dict:
    """One arm's result, or its failure — never the other arm's loss.

    The 2026-08-28 pilot lost a whole answered baseline arm because the
    consolidated arm raised on generation activation and the exception left
    `run_question` entirely. An arm that fails is one arm's error.
    """
    try:
        return run_arm(question, root, state)
    except Exception as exc:  # noqa: BLE001 - the pair needs a record, not a traceback
        return {
            "status": "error",
            "hypothesis": "",
            "error": f"{type(exc).__name__}: {exc}"[:500],
            "error_kind": "arm_failure",
        }


def _guarded_consolidation(root: Path) -> dict:
    try:
        cost = consolidate_vault(root)
    except Exception as exc:  # noqa: BLE001 - a failed step is reported, not fatal
        return {"error": f"{type(exc).__name__}: {exc}"[:500], "items": 0}
    cost["entries_written"] = _entries_written(root)
    return cost


def run_question(question: dict, work: Path) -> dict:
    """Ingest once, answer twice; the pair is the measurement."""
    import longmemeval_data

    started = time.monotonic()
    root, state = vault_harness.prepare_environment(work)
    vault_harness._adopt(root, state)
    ingest_started = time.monotonic()
    _, ingested = vault_harness.ingest_sessions(root, question)
    baseline_started = time.monotonic()
    baseline = _arm_or_failure(question, root, state)
    consolidate_started = time.monotonic()
    cost = _guarded_consolidation(root)
    consolidated_started = time.monotonic()
    consolidated = _arm_or_failure(question, root, state)
    return {
        "question_id": str(question["question_id"]),
        "question_type": str(question["question_type"]),
        "category": longmemeval_data.category_of(question),
        "is_abstention": longmemeval_data.is_abstention(question),
        "question": str(question["question"]),
        "gold": str(question["answer"]),
        "sessions_total": len(question["haystack_sessions"]),
        "sessions_ingested": ingested,
        ARM_BASELINE: baseline,
        ARM_CONSOLIDATED: consolidated,
        "consolidation": {
            **cost,
            "wall_seconds": round(consolidated_started - consolidate_started, 2),
        },
        "ingest_seconds": round(baseline_started - ingest_started, 2),
        "total_seconds": round(time.monotonic() - started, 2),
    }


def _failure_record(question: dict, exc: BaseException) -> dict:
    import longmemeval_data

    failed = {"status": "error", "hypothesis": "", "error_kind": "harness_failure"}
    return {
        "question_id": str(question.get("question_id")),
        "question_type": str(question.get("question_type")),
        "category": longmemeval_data.category_of(question),
        "is_abstention": longmemeval_data.is_abstention(question),
        "gold": str(question.get("answer", "")),
        "error": f"{type(exc).__name__}: {exc}"[:500],
        ARM_BASELINE: dict(failed),
        ARM_CONSOLIDATED: dict(failed),
    }


def _parsed_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--question", required=True, help="path to one question JSON file")
    parser.add_argument("--out", required=True, help="path for the paired result JSON")
    parser.add_argument("--workdir", default=None, help="parent directory for the temp vault")
    parser.add_argument("--keep-vault", action="store_true")
    return parser.parse_args()


def main() -> int:
    """Caller paths are resolved before `prepare_environment` moves the cwd.

    The worker chdirs into its throwaway vault so `claude -p` does not inherit
    this repository's `CLAUDE.md` — the MEM-10 defect that turned every provider
    call into an agent turn (175 s and the wrong topic, against 12 s and the
    right answer from a neutral directory). A relative `--out` resolved after
    that move would be written inside the vault and deleted with it.
    """
    args = _parsed_args()
    out_path = Path(args.out).resolve()
    workdir = None if args.workdir is None else str(Path(args.workdir).resolve())
    question = json.loads(Path(args.question).resolve().read_text(encoding="utf-8"))
    work = Path(tempfile.mkdtemp(prefix="consolidation-", dir=workdir))
    try:
        result = run_question(question, work)
    except Exception as exc:  # noqa: BLE001 - the orchestrator needs a record
        result = _failure_record(question, exc)
    finally:
        os.chdir(out_path.parent)
        if not args.keep_vault:
            shutil.rmtree(work, ignore_errors=True)
    out_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
