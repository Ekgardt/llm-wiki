"""Answer one LongMemEval question from a disposable llm-wiki vault (MEM-10).

Run as a subprocess, one process per question, because the product resolves
`LLM_WIKI_ROOT` / `LLM_WIKI_STATE_ROOT` at import time: this module sets both
to a throwaway directory *before* any `scripts/` import, so nothing here can
touch the live vault.

The pipeline is the product's own write and read path, not a mock:

1. a fresh vault is adopted onto the Reliability V3 pair
   (`installed_memory_repair.repair_installed_vault`);
2. every haystack session is written as session evidence
   (`session_evidence.write_session_evidence`) and as a daily entry through
   the locked transactional daily writer (`flush_memory.append_daily`),
   exactly the artifacts capture leaves behind;
3. one immutable corpus generation is built and activated by the maintenance
   builder (`evidence_graph_builder.build_incremental_generation` with the
   helpers `doctor` uses), with the ingested daily files named as
   `daily_paths` — the stock nightly indexes only compiled pages, so naming
   the daily evidence explicitly is the one deliberate deviation, recorded
   in the research note;
4. the question is answered through `retrieval.retrieve_via_search_memory`
   and `query_memory.grounded_qa` with the real provider.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
import time
from collections.abc import Mapping
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
BUILD_DEADLINE_SECONDS = 900.0
RETRIEVE_DEADLINE_SECONDS = 180.0
ANSWER_DEADLINE_SECONDS = 420.0
QA_CANDIDATES = 12
# The product's stock grounded-answer budget is 8192 byte-counted tokens; one
# LongMemEval session entry is ~10 KB, so under the stock budget every span is
# shed and the answer refuses itself (measured on question 25e5aa4f). The
# budget is a first-party `grounded_qa` parameter; this value keeps the whole
# prompt under ~28 KB ≈ ~7k estimated tokens — the same retrieval envelope
# Mem0's "<7000 tokens" claim describes, so the cost comparison stays fair.
ANSWER_INPUT_BUDGET = 28_672


def _answer_budget() -> int:
    """The answer window for this arm, so a sweep needs no code edit.

    The stock value keeps the whole prompt near 7 000 estimated tokens to match
    the envelope Mem0's cost claim describes. Whether that ceiling is costing
    accuracy has never been measured — the reader accepts two hundred thousand,
    and 2026 work reports that a fixed compression budget applied regardless of
    context breaks the scaling a stronger reader should give. This makes the
    ceiling an arm of the stand instead of a constant.
    """
    raw = os.environ.get("LLMWIKI_BENCH_ANSWER_BUDGET", "").strip()
    if not raw.isdigit():
        return ANSWER_INPUT_BUDGET
    return max(4096, int(raw))

_ERROR_KINDS = (
    ("provider returned no response", "provider_no_response"),
    ("provider returned invalid JSON", "provider_invalid_json"),
)


def _leave_the_repository(root: Path) -> None:
    """Run from the throwaway vault so the provider inherits no project memory.

    Measured 2026-08-28, same prompt and flags one minute apart: `claude -p`
    with the process working directory at this repository answered a one-word
    prompt in 175.42s with 980 characters about a pytest permission prompt,
    while the same call from a neutral directory answered `pong` in 12.59s.
    `--setting-sources ""` excludes settings.json; it does not exclude
    `CLAUDE.md`, and this vault's CLAUDE.md `@`-imports `knowledge/index.md`
    and `knowledge/log.md` — ~300 KB of operating instructions that turn every
    benchmark call into an agent turn answering something else. Every provider
    failure in the 2026-08-28 partial run has this cause.
    """
    os.chdir(root)


def prepare_environment(work: Path) -> tuple[Path, Path]:
    """Point the product at a throwaway vault before any scripts import."""
    root = work / "vault"
    state = work / "state"
    os.environ["LLM_WIKI_ROOT"] = str(root)
    os.environ["LLM_WIKI_STATE_ROOT"] = str(state)
    os.environ.setdefault("MEMORY_LLM_PROVIDER", "claude")
    os.environ.setdefault("MEMORY_LLM_TIMEOUT_S", "240")
    scripts = str(REPO / "scripts")
    if scripts not in sys.path:
        sys.path.insert(0, scripts)
    (root / "scripts").mkdir(parents=True, exist_ok=True)
    (root / "scripts/integration_adapter.py").write_bytes(
        (REPO / "scripts/integration_adapter.py").read_bytes()
    )
    (root / "knowledge/notes").mkdir(parents=True, exist_ok=True)
    (root / "knowledge/daily").mkdir(parents=True, exist_ok=True)
    _leave_the_repository(root)
    return root, state


def _adopt(root: Path, state: Path) -> None:
    from installed_memory_repair import repair_installed_vault

    report = repair_installed_vault(
        root=root,
        state_root=state,
        adopt_ownership_v3=True,
        confirm_all_agents_stopped=True,
    )
    if report["overall_status"] != "ok":
        raise RuntimeError(f"vault adoption failed: {report['overall_status']}")


def day_of(date_text: str) -> str:
    """`2023/05/20 (Sat) 02:21` -> `2023-05-20`."""
    return str(date_text).split()[0].replace("/", "-")


def time_of(date_text: str) -> str:
    """`2023/05/20 (Sat) 02:21` -> `02:21:00`."""
    clock = str(date_text).split()[-1]
    if clock.count(":") == 1:
        clock += ":00"
    return clock


def transcript_jsonl(turns: list[dict]) -> str:
    """The haystack turns in the transcript shape capture reads."""
    lines = []
    for turn in turns:
        role = "assistant" if turn.get("role") == "assistant" else "user"
        text = str(turn.get("content", ""))
        block = {"type": "text", "text": text}
        lines.append(json.dumps({"type": role, "message": {"content": [block]}}))
    return "\n".join(lines)


def daily_block(session_id: str, date_text: str, rendered: str) -> str:
    """One capture-shaped daily entry; the session date is inside the entry."""
    header = f"## [{time_of(date_text)}] session_end | {session_id}"
    stamp = f"_captured: {day_of(date_text)} {time_of(date_text)}_"
    return f"{header}\n\n{stamp}\n\n{rendered}\n"


def _ingest_one(
    root: Path, session_id: str, date_text: str, turns: list[dict], position: int
) -> str | None:
    import flush_memory
    from session_evidence import render_transcript, write_session_evidence

    transcript = transcript_jsonl(turns)
    rendered = render_transcript(transcript).strip()
    if not rendered:
        return None
    fields = {
        "session": session_id,
        "project": "longmemeval",
        "host": "benchmark",
        "event": "session_end",
        "captured_at": f"{day_of(date_text)}T{time_of(date_text)}Z",
    }
    write_session_evidence(root, fields, transcript)
    day = day_of(date_text)
    block = daily_block(session_id, date_text, rendered)
    flush_memory.append_daily(day, block, operation_id=f"longmemeval-{position}-{session_id}")
    return f"knowledge/daily/{day}.md"


def ingest_sessions(root: Path, question: dict) -> tuple[list[str], int]:
    """Write every haystack session as evidence + daily entry; return daily files.

    The idempotency key carries the haystack position, not the session id
    alone: LongMemEval haystacks repeat a session id with different turns
    (question `gpt4_76048e76` lists `8fcaf3a9_2` twice), and the product
    correctly refuses to reuse one key for two different bodies. Keying on the
    id alone lost the whole question to
    `operation_id is already bound to a different request`.
    """
    daily_files: dict[str, None] = {}
    ingested = 0
    sessions = question["haystack_sessions"]
    ids = question["haystack_session_ids"]
    dates = question["haystack_dates"]
    for position, (session_id, date_text, turns) in enumerate(zip(ids, dates, sessions)):
        written = _ingest_one(root, str(session_id), str(date_text), turns, position)
        if written is not None:
            daily_files[written] = None
            ingested += 1
    return list(daily_files), ingested


def _reuse_config(snapshot: object) -> object:
    import hashlib

    from doctor import _maintenance_extractor_identity, _workspace_manifest_sha256
    from evidence_graph_builder import GRAPH_SCHEMA_VERSION, IncrementalReuseConfig

    return IncrementalReuseConfig(
        extractor_version=_maintenance_extractor_identity(),
        grammar_version="builtin-grammars/v1",
        compiler_version=f"python-{sys.version_info.major}.{sys.version_info.minor}",
        resolver_config_sha256=hashlib.sha256(
            b"llm-wiki-maintenance-resolver/v1"
        ).hexdigest(),
        schema_version=GRAPH_SCHEMA_VERSION,
        workspace_manifest_sha256=_workspace_manifest_sha256(snapshot),
    )


def build_generation(root: Path, state: Path, daily_files: list[str]) -> tuple[object, dict]:
    """Build and activate one generation over the ingested daily evidence."""
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

    deadline = time.monotonic() + BUILD_DEADLINE_SECONDS
    snapshot = collect_corpus(root, code_roots=(), daily_paths=daily_files, deadline=deadline)
    scope = resolve_repository_scope(root)
    catalog = GenerationCatalog(state)
    built = build_incremental_generation(
        catalog,
        sources=_generation_source_rows(snapshot),
        source_bytes=_generation_source_bytes(snapshot),
        extractor=_generation_source_extractor(snapshot, scope.repository_id),
        reuse_config=_reuse_config(snapshot),
        generation_id=_fresh_generation_id(catalog),
        parent_generation_id=None,
        policy=_corpus_policy(snapshot),
        expected_active=None,
        deadline=deadline,
        cancelled=None,
        repository_scope=scope,
        snapshot=snapshot,
        publication_root=root,
        coordinator=None,
    )
    if not built.activated:
        raise RuntimeError("generation build finished but did not activate")
    active = catalog.get_active() or {}
    info = {
        "generation_id": built.generation_id,
        "vector_state": active.get("vector_state"),
        "sources": len(snapshot.sources),
        "chunks": len(snapshot.chunks),
    }
    return snapshot, info


def profile_for(question_text: str) -> str:
    from retrieval import analyze_query

    return analyze_query(question_text).recommended_profile.upper()


def _retrieved_rows(question_text: str, profile: str) -> list[dict]:
    from retrieval import retrieve_via_search_memory

    return list(
        retrieve_via_search_memory(
            question_text,
            limit=QA_CANDIDATES,
            semantic=True,
            profile=profile,
            deadline_monotonic=time.monotonic() + RETRIEVE_DEADLINE_SECONDS,
        )
    )


# Whether retrieval put the answer in front of the answerer, decided by the
# dataset's own label rather than by searching for the gold string.
#
# This is the instrument the abstention question needs, and without it the
# question cannot be answered at all. Measured 2026-08-28 on n=50: 26 of 50
# answers were `insufficient_evidence`, and accuracy when the system does
# answer is 14 of 18 = 0.78 — so refusal binds the score, not error. But an
# abstention means two opposite things. With an answer session retrieved it is
# a calibration failure: the evidence was there and the answerer refused it.
# Without one it is retrieval refusing, and the abstention was correct.
# Spending effort on the prompt before knowing which would be guessing.
#
# The match is on the session id, not on the answer text, because a gold answer
# is often a word like `2018` or `blue` and a substring search over evidence
# would confirm itself. `ingest_sessions` writes each daily entry under a
# heading naming its session, so the id travels with the chunk.
def _row_text(row: Mapping[str, object]) -> str:
    parts = [str(row.get(key, "")) for key in ("summary", "title", "path")]
    ancestry = row.get("heading_ancestry")
    if isinstance(ancestry, (list, tuple)):
        parts.extend(str(item) for item in ancestry)
    return "\n".join(parts)


def _labelled_sessions(question: Mapping[str, object]) -> set[str]:
    return {str(item) for item in question.get("answer_session_ids") or []}


def _names_a_labelled_session(row: Mapping[str, object], labelled: set[str]) -> bool:
    text = _row_text(row)
    return any(item in text for item in labelled)


def answer_sessions_retrieved(question: Mapping[str, object], rows: list[dict]) -> int:
    """How many retrieved candidates come from a labelled answer session."""
    labelled = _labelled_sessions(question)
    if not labelled:
        return 0
    return sum(_names_a_labelled_session(row, labelled) for row in rows)


# The session signal above says the right *session* reached the answerer. It
# does not say the *span* carrying the fact did, and that is the difference
# that matters.
#
# Measured 2026-08-30: of 21 refusals with an answer session retrieved, 3 were
# abstention-type questions the system correctly refused, leaving 18 on
# answerable ones — and the model's own stated reasons name what it saw. On a
# question whose gold is `20` it reports "the only figure in evidence is ...
# '30 dozen' eggs"; on one whose gold is `11` it reports evidence about an
# Alex who is "a 21-year-old work intern". The session was there; the sentence
# was not.
#
# `gold_in_candidates` is a weak signal on purpose and must be read as one: a
# gold answer of `20` matches any candidate containing that substring, so it
# over-counts. It is useful only against the session signal — session present
# and gold absent is span-level loss, and both present is a genuine refusal
# with the answer in hand.
def gold_in_candidates(question: Mapping[str, object], rows: list[dict]) -> bool:
    """Whether the gold answer's text appears at all in what retrieval handed over."""
    gold = " ".join(str(question.get("answer", "")).split())
    if not gold:
        return False
    return any(gold.casefold() in _row_text(row).casefold() for row in rows)


# Where the answer session stands in the ranking, because that is what decides
# whether it survives.
#
# `query_memory._fitted_selection` sheds from the tail: it keeps the top-ranked
# candidates and drops the lowest-ranked ones until the rest fit the answer
# budget. A LongMemEval session entry is about 10 KB against a 28 KB budget, so
# only a handful survive. A right session ranked below that line is retrieved
# and never seen — which is exactly the shape of the 18 evidenced refusals on
# answerable questions measured 2026-08-30.
#
# One-based, and 0 when no candidate comes from a labelled answer session, so
# the value is directly comparable with how many candidates the budget kept.
def answer_session_rank(question: Mapping[str, object], rows: list[dict]) -> int:
    """The position of the first candidate drawn from a labelled answer session."""
    labelled = _labelled_sessions(question)
    if not labelled:
        return 0
    for position, row in enumerate(rows, start=1):
        if _names_a_labelled_session(row, labelled):
            return position
    return 0


def _instrumented_generator(metrics: dict, gold: str = ""):
    """The shared provider client, measuring what one retrieval hands it.

    `gold_in_prompt` is the measurement the earlier signals could not make.
    `gold_in_candidates` read a candidate's summary and heading, which almost
    never carry the answering sentence: measured 2026-08-30 it was true for 2
    of 50 questions, including 0 of the 17 the system answered, so it measured
    nothing. This reads the prompt the model actually received.
    """
    from llm_client import call_llm

    needle = " ".join(str(gold).split()).casefold()

    def generate(prompt: str, system_prompt: str, max_tokens: int) -> str | None:
        metrics["prompt_chars"] = len(prompt)
        metrics["prompt_bytes"] = len(prompt.encode("utf-8"))
        metrics["system_chars"] = len(system_prompt)
        metrics["est_prompt_tokens"] = round(len(prompt) / 4)
        # What the model is actually charged for. `est_prompt_tokens` counts the
        # user prompt only, so every token-cost figure this stand has produced
        # was short by the system prompt — and the comparison points the field
        # publishes (~6 900 for Mem0, ~720 added for Supermemory) are whole-
        # context numbers. The old field stays so earlier reports remain
        # readable next to new ones. Both are `chars / 4`, not a tokenizer:
        # a real count needs a network round trip, which this path will not make.
        metrics["est_total_prompt_tokens"] = round(
            (len(prompt) + len(system_prompt)) / 4
        )
        metrics["gold_in_prompt"] = bool(needle) and needle in prompt.casefold()
        started = time.monotonic()
        try:
            reply = call_llm(prompt, system_prompt, max_tokens)
            # What the citation gates may be about to destroy. Measured
            # 2026-09-01: 28 of 200 answers were produced and then thrown away
            # by `verify_grounded_answer`, and the row kept only the gate's
            # message — so the accuracy of what the strictest rule discards
            # could only be bounded, 0 to +0.14, never priced. Recording the
            # reply changes no answer and no verdict; it lets the discarded
            # ones be judged on their own.
            metrics["raw_reply"] = (reply or "")[:8000]
            return reply
        finally:
            metrics["provider_seconds"] = round(time.monotonic() - started, 2)

    return generate


def dated_question(question: dict) -> str:
    """The question plus its date, as the paper's reading prompt states it."""
    return f"{question['question']}\n(Current date: {question['question_date']})"


def hypothesis_of(document: dict) -> str:
    claims = document.get("claims") or []
    return " ".join(str(claim.get("text", "")) for claim in claims).strip()


def error_kind(exc: BaseException) -> str:
    if isinstance(exc, TimeoutError):
        return "provider_deadline"
    text = str(exc)
    for needle, kind in _ERROR_KINDS:
        if needle in text:
            return kind
    return "verification_or_gate"


# What the compiler decided, read straight from its own trace instead of
# inferred from the answer.
#
# Measured 2026-08-30: the answer session ranked first for 37 of 50 questions
# and the gold text reached the model in 14 of them. Reading the code says the
# compiler places a broad whole-file item for every parent page before the
# narrow evidence spans, and a LongMemEval daily file holds dozens of sessions
# against a 28 KB budget — but that is a reading, not a measurement. These
# three numbers settle it: `evidence_missed` counts requested evidence chunks
# the compiler could not place at all, `dropped` counts items the packer threw
# out for budget, and the level counts say what the budget was spent on.
def _compile_trace_metrics(context: object) -> dict[str, object]:
    trace = getattr(context, "compile_trace", None)
    if trace is None:
        return {}
    return {
        "compile_l0": trace.l0_count,
        "compile_l1": trace.l1_count,
        "compile_l2": trace.l2_count,
        "evidence_missed": len(trace.retrieval.missed_evidence_chunk_ids),
        "evidence_requested": len(trace.retrieval.evidence_chunk_ids),
        "packer_dropped": len(trace.packing.dropped),
        "packed_tokens": trace.packing.packed_tokens,
    }


def _measured_compile(root: Path, snapshot: object, rows: list[dict], profile: str) -> dict:
    """One extra compile, for its trace only. No provider call, no effect on the answer."""
    from context_budget import ContextBudget
    from query_memory import QA_MAX_OUTPUT_TOKENS, build_grounded_context

    try:
        context = build_grounded_context(
            snapshot,
            rows,
            vault=root,
            profile=profile,
            budget=ContextBudget(None, _answer_budget(), QA_MAX_OUTPUT_TOKENS, 512),
        )
    except Exception:  # noqa: BLE001 - a measurement never fails the question
        return {}
    return _compile_trace_metrics(context)


def _answer_outcome(
    question_text: str,
    root: Path,
    snapshot: object,
    rows: list[dict],
    metrics: dict,
    profile: str,
    gold: str = "",
) -> dict:
    from context_budget import ContextBudget
    from query_memory import QA_MAX_OUTPUT_TOKENS, grounded_qa

    try:
        document = grounded_qa(
            question_text,
            vault=root,
            snapshot=snapshot,
            candidates=rows,
            generator=_instrumented_generator(metrics, gold),
            profile=profile,
            budget=ContextBudget(None, _answer_budget(), QA_MAX_OUTPUT_TOKENS, 512),
            deadline=time.monotonic() + ANSWER_DEADLINE_SECONDS,
        )
    except Exception as exc:  # noqa: BLE001 - every failure is a scored outcome
        return {
            "status": "error",
            "hypothesis": "",
            "error": f"{type(exc).__name__}: {exc}"[:500],
            "error_kind": error_kind(exc),
        }
    return {
        "status": str(document.get("status")),
        "hypothesis": hypothesis_of(document),
        "reason": document.get("reason"),
        "claims": len(document.get("claims") or []),
        "citations": len(document.get("citations") or []),
        "error": None,
        "error_kind": None,
    }


def run_question(question: dict, work: Path) -> dict:
    """The whole per-question pipeline; always returns a result record."""
    import longmemeval_data

    started = time.monotonic()
    root, state = prepare_environment(work)
    _adopt(root, state)
    ingest_started = time.monotonic()
    daily_files, ingested = ingest_sessions(root, question)
    build_started = time.monotonic()
    snapshot, build_info = build_generation(root, state, daily_files)
    plain = str(question["question"])
    profile = profile_for(plain)
    retrieve_started = time.monotonic()
    rows = _retrieved_rows(plain, profile)
    answer_started = time.monotonic()
    metrics: dict = {}
    outcome = _answer_outcome(
        dated_question(question), root, snapshot, rows, metrics, profile,
        str(question.get("answer", "")),
    )
    finished = time.monotonic()
    return {
        "question_id": str(question["question_id"]),
        "question_type": str(question["question_type"]),
        "category": longmemeval_data.category_of(question),
        "is_abstention": longmemeval_data.is_abstention(question),
        "question": plain,
        "gold": str(question["answer"]),
        "profile": profile,
        "sessions_total": len(question["haystack_sessions"]),
        "sessions_ingested": ingested,
        "daily_files": len(daily_files),
        "retrieved": len(rows),
        "answer_sessions_retrieved": answer_sessions_retrieved(question, rows),
        "answer_sessions_labelled": len(_labelled_sessions(question)),
        "gold_in_candidates": gold_in_candidates(question, rows),
        "answer_session_rank": answer_session_rank(question, rows),
        **_measured_compile(root, snapshot, rows, profile),
        **build_info,
        **outcome,
        **metrics,
        "ingest_seconds": round(build_started - ingest_started, 2),
        "build_seconds": round(retrieve_started - build_started, 2),
        "retrieve_seconds": round(answer_started - retrieve_started, 2),
        "answer_seconds": round(finished - answer_started, 2),
        "total_seconds": round(finished - started, 2),
    }


def _prepared_workdir(requested: str | None) -> str | None:
    """The parent directory for the throwaway vault, created if it is missing.

    A `--workdir` that does not exist failed every question in about two
    seconds with a `mkdtemp` traceback, and the report then read `accuracy 0.0`
    with `provider_failures 0` across all 50 — a harness that never ran,
    presented as a memory system that answered everything wrongly. Measured
    2026-08-29 on this repository.
    """
    if requested is None:
        return None
    path = Path(requested).resolve()
    path.mkdir(parents=True, exist_ok=True)
    return str(path)


def _failure_record(question: dict, exc: BaseException) -> dict:
    import longmemeval_data

    return {
        "question_id": str(question.get("question_id")),
        "question_type": str(question.get("question_type")),
        "category": longmemeval_data.category_of(question),
        "is_abstention": longmemeval_data.is_abstention(question),
        "gold": str(question.get("answer", "")),
        "status": "error",
        "hypothesis": "",
        "error": f"{type(exc).__name__}: {exc}"[:500],
        "error_kind": "harness_failure",
    }


def _parsed_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--question", required=True, help="path to one question JSON file")
    parser.add_argument("--out", required=True, help="path for the result JSON")
    parser.add_argument("--workdir", default=None, help="parent directory for the temp vault")
    parser.add_argument("--keep-vault", action="store_true")
    return parser.parse_args()


def main() -> int:
    """Every caller-supplied path is resolved before the working directory moves.

    `prepare_environment` chdirs into the throwaway vault to keep this
    repository's `CLAUDE.md` out of the provider call, so a relative `--out`
    handed in by the orchestrator would be written under the temporary vault
    and then deleted with it. Resolving first keeps the caller's meaning.
    """
    args = _parsed_args()
    out_path = Path(args.out).resolve()
    workdir = _prepared_workdir(args.workdir)
    question = json.loads(Path(args.question).resolve().read_text(encoding="utf-8"))
    work = Path(tempfile.mkdtemp(prefix="longmemeval-", dir=workdir))
    try:
        result = run_question(question, work)
    except Exception as exc:  # noqa: BLE001 - the orchestrator needs a record, not a traceback
        result = _failure_record(question, exc)
    finally:
        os.chdir(out_path.parent)
        if not args.keep_vault:
            shutil.rmtree(work, ignore_errors=True)
    out_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
