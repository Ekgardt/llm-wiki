"""Question gpt4_93159ced: an answer spread over two sessions of one day.

"How long have I been working before I started my current job at NovaTech?"
needs "working professionally for 9 years" from the 00:34 session and "at
NovaTech for about 4 years and 3 months" from the 01:29 one. Both sat in one
daily file, so every excerpt read `knowledge/daily/2023-05-25.md` and the
reader could not tell the sessions apart, and the user's restated introduction
reached it twice. Now each excerpt carries its entry's date and time, and a
passage its own session already said is read once.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
BENCHMARK = Path(__file__).resolve().parents[1] / "benchmark"
for folder in (SCRIPTS, BENCHMARK):
    if str(folder) not in sys.path:
        sys.path.insert(0, str(folder))

from context_budget import ContextBudget  # noqa: E402
from corpus_snapshot import collect_corpus  # noqa: E402
from longmemeval_vault import daily_block, transcript_jsonl  # noqa: E402
from query_memory import GroundedContext, build_grounded_context  # noqa: E402
from session_evidence import render_transcript  # noqa: E402

DAILY = "knowledge/daily/2023-05-25.md"
NOVATECH = (
    "I'm a software engineer, specifically a backend developer, and I've been in this field since "
    "I graduated with a degree in Computer Science from the University of California, Berkeley. "
    "I've been working at NovaTech for about 4 years and 3 months now."
)
RESTATED = "I'll start by saying that " + NOVATECH
NINE_YEARS = (
    "One thing I'm struggling with is keeping track of my tasks and projects. I've been working "
    "professionally for 9 years and I'm currently using a physical notebook to jot down notes."
)


def _session(*user_turns: str) -> str:
    turns: list[dict[str, str]] = []
    for text in user_turns:
        turns += [
            {"role": "user", "content": text},
            {"role": "assistant", "content": "Noted, thank you."},
        ]
    return str(render_transcript(transcript_jsonl(turns))).strip()


def _vault(tmp_path: Path) -> Path:
    root = tmp_path / "vault"
    (root / "knowledge" / "daily").mkdir(parents=True)
    (root / "knowledge" / "notes").mkdir(parents=True)
    body = (
        "# 2023-05-25\n\n"
        + daily_block("answer_e5131a1b_1", "2023/05/25 (Thu) 00:34", _session(NINE_YEARS))
        + "\n"
        + daily_block(
            "answer_e5131a1b_2",
            "2023/05/25 (Thu) 01:29",
            _session(NOVATECH + " Which monitoring tool fits?", RESTATED),
        )
    )
    (root / DAILY).write_text(body, encoding="utf-8")
    return root


def _context(tmp_path: Path) -> GroundedContext:
    vault = _vault(tmp_path)
    snapshot = collect_corpus(vault, code_roots=(), daily_paths=[DAILY])
    user_turns = tuple(chunk for chunk in snapshot.chunks if "**user:**" in chunk.text)
    return build_grounded_context(
        snapshot,
        user_turns,
        vault=vault,
        profile="BASE",
        budget=ContextBudget(None, 65_536, 1200, 512),
    )


def _manifest(context: GroundedContext) -> list[dict[str, str]]:
    body = context.prompt_context.split("<evidence_manifest>\n", 1)[1].split(
        "\n</evidence_manifest>", 1
    )[0]
    entries: list[dict[str, str]] = json.loads(body)
    return entries


def _dated(context: GroundedContext, words: str) -> set[str]:
    return {entry.get("date", "") for entry in _manifest(context) if words in entry["text"]}


def test_each_session_reaches_the_reader_with_its_own_date_and_time(tmp_path: Path) -> None:
    context = _context(tmp_path)

    assert _dated(context, "9 years") == {"2023-05-25 00:34:00"}
    assert _dated(context, "NovaTech for") == {"2023-05-25 01:29:00"}
    assert {item.session for item in context.evidence} == {"answer_e5131a1b_1", "answer_e5131a1b_2"}


def test_the_restated_introduction_is_read_once(tmp_path: Path) -> None:
    context = _context(tmp_path)

    texts = " ".join(item.text for item in context.evidence)
    assert texts.count("4 years and 3 months") == 1
