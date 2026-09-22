"""Load and sample the LongMemEval dataset (MEM-10).

The dataset is the `longmemeval_s` variant of `xiaowu0162/longmemeval`
(500 questions, ~50 haystack chat sessions each, MIT license). It is cached
under `cache/benchmarks/longmemeval/` — the disposable cache tier — and is
NEVER generated: when the file is absent this module refuses with the exact
download instruction instead of inventing a synthetic stand-in.

Note recorded for honesty: Hugging Face marks this dataset as deprecated in
favour of `longmemeval-cleaned`; the original is still what the published
Mem0 / Zep claims were measured on, so the first number is taken here.
"""
from __future__ import annotations

import hashlib
import json
import random
import re
from pathlib import Path

DATASET_DIR = Path(__file__).resolve().parent.parent / "cache" / "benchmarks" / "longmemeval"
DATASET_FILE = DATASET_DIR / "longmemeval_s.json"
DATASET_URL = (
    "https://huggingface.co/datasets/xiaowu0162/longmemeval/resolve/main/longmemeval_s"
)
EXPECTED_QUESTIONS = 500
REQUIRED_KEYS = frozenset(
    {
        "question_id",
        "question_type",
        "question",
        "answer",
        "question_date",
        "haystack_dates",
        "haystack_session_ids",
        "haystack_sessions",
    }
)


class DatasetUnavailable(RuntimeError):
    """The real dataset is not on disk; a synthetic stand-in is forbidden."""


def load_dataset(path: Path = DATASET_FILE) -> list[dict]:
    if not path.exists():
        raise DatasetUnavailable(
            f"LongMemEval is not cached at {path}. Download the real file "
            f"(278 MB) from {DATASET_URL} and save it there. This harness "
            "never generates a synthetic stand-in."
        )
    data = json.loads(path.read_text(encoding="utf-8"))
    require_dataset_shape(data)
    return data


def require_dataset_shape(data: object) -> None:
    if not isinstance(data, list) or not data:
        raise DatasetUnavailable("dataset must be a non-empty JSON array of questions")
    missing = REQUIRED_KEYS - set(data[0])
    if missing:
        raise DatasetUnavailable(f"dataset questions lack required keys: {sorted(missing)}")


def is_abstention(question: dict) -> bool:
    """LongMemEval marks abstention questions by a `_abs` question-id suffix."""
    return str(question["question_id"]).endswith("_abs")


# A twin is an answerable question with its evidence sessions taken out of the
# haystack, so refusing it is right: the same question measured in both
# directions, with no new label (AgentAbstain's pair; RESEARCH-B §5). The stand
# builds it here, where the question set is prepared; the vault builder needs
# no hook, because a twin is an ordinary question with fewer sessions.
# See `docs/research/2026-09-22-quote-then-answer-and-a-refusal-calibrated-on-twins.md`.
TWIN_SUFFIX = "_twin"
TWIN_CATEGORY = "twin"
# LoCoMo names its evidence as `D<session>:<turn>`, one-based within the
# session; the converted question carries the list under `locomo_evidence`,
# its sessions are `session_<n>`, and the converter keeps every turn in order
# (checked on the source file 2026-09-22: 5 882 turns, none empty, every
# `dia_id` equal to its position). The stand read `answer_session_ids` and
# `has_answer` only, so on the recorded LoCoMo run of 2026-09-19 every
# coverage figure was zero and no twin could be built. `labelled_by_evidence`
# writes both labels at question preparation.
# Found inside the item rather than matched whole: 6 of the source's 2 815
# references are compound ("D8:6; D9:17", "D9:1 D4:4 D4:6"), and a bare "D"
# names nothing.
_LOCOMO_EVIDENCE = re.compile(r"D(\d+):(\d+)")


def is_twin(question: dict) -> bool:
    return str(question["question_id"]).endswith(TWIN_SUFFIX)


def expects_silence(question: dict) -> bool:
    """Whether the right answer to this question is a refusal."""
    return is_abstention(question) or is_twin(question)


def category_of(question: dict) -> str:
    """Reporting category: the paper scores abstention as its own row, and so is a twin."""
    if is_abstention(question):
        return "abstention"
    if is_twin(question):
        return TWIN_CATEGORY
    return str(question["question_type"])


def locomo_references(question: dict) -> list[tuple[str, int]]:
    """Each `D<session>:<turn>` reference as (session id, one-based turn), in order."""
    joined = " ".join(str(item) for item in question.get("locomo_evidence") or ())
    return [_reference(match) for match in _LOCOMO_EVIDENCE.finditer(joined)]


def _reference(match: re.Match) -> tuple[str, int]:
    return f"session_{match.group(1)}", int(match.group(2))


def _locomo_sessions(question: dict) -> set[str]:
    return {session for session, _turn in locomo_references(question)}


def evidence_sessions_of(question: dict) -> set[str]:
    """The sessions the dataset labels as carrying the answer, by either dataset's label."""
    labelled = {str(item) for item in question.get("answer_session_ids") or ()}
    return labelled | _locomo_sessions(question)


def _flagged(turn: object, flagged: bool) -> object:
    if not flagged or not isinstance(turn, dict):
        return turn
    return {**turn, "has_answer": True}


def _flagged_session(session: list, turns: set[int]) -> list:
    return [_flagged(turn, position in turns) for position, turn in enumerate(session, start=1)]


def _flagged_sessions(question: dict, references: list[tuple[str, int]]) -> list[list]:
    wanted: dict[str, set[int]] = {}
    for session, turn in references:
        wanted.setdefault(session, set()).add(turn)
    ids = [str(item) for item in question["haystack_session_ids"]]
    return [
        _flagged_session(session, wanted.get(session_id, set()))
        for session_id, session in zip(ids, question["haystack_sessions"])
    ]


def labelled_by_evidence(question: dict) -> dict:
    """The question with `answer_session_ids` and `has_answer` written from its LoCoMo references.

    A question that already carries session labels, or names no reference, is
    returned as it is: LongMemEval labels its own evidence and is untouched.
    """
    references = locomo_references(question)
    if question.get("answer_session_ids") or not references:
        return question
    return {
        **question,
        "answer_session_ids": _referenced_sessions(question, references),
        "haystack_sessions": _flagged_sessions(question, references),
    }


def _referenced_sessions(question: dict, references: list[tuple[str, int]]) -> list[str]:
    """The referenced sessions the haystack holds, each once, in reference order."""
    present = {str(item) for item in question["haystack_session_ids"]}
    return list(dict.fromkeys(session for session, _turn in references if session in present))


def _kept_positions(question: dict, removed: set[str]) -> list[int]:
    ids = question["haystack_session_ids"]
    return [position for position, session_id in enumerate(ids) if str(session_id) not in removed]


def _haystack_without(question: dict, removed: set[str]) -> dict:
    kept = _kept_positions(question, removed)
    return {
        key: [question[key][position] for position in kept]
        for key in ("haystack_sessions", "haystack_session_ids", "haystack_dates")
    }


def twin_of(question: dict) -> dict | None:
    """The same question with its evidence sessions removed; None when it has none to remove."""
    removed = evidence_sessions_of(question)
    if expects_silence(question) or not removed:
        return None
    return {
        **question,
        **_haystack_without(question, removed),
        "question_id": str(question["question_id"]) + TWIN_SUFFIX,
        "answer": "",
        "answer_session_ids": [],
        "locomo_evidence": [],
        "twin_of": str(question["question_id"]),
    }


def original_of(question_id: str) -> str:
    """The id a twin was built from; an id that is no twin is its own original."""
    return str(question_id).removesuffix(TWIN_SUFFIX)


# The tune / decide split, fixed before any threshold was set and never moved:
# LongMemEval by question-id hash parity; LoCoMo conversations 1–5 tune, 6–10
# decide, in the order the source file lists them. A threshold is chosen on
# tune and reported on decide. Plan rule 2 of
# `docs/research/2026-09-22-a-class-level-plan-to-pass-the-field.md`.
TUNE = "tune"
DECIDE = "decide"
LOCOMO_TUNE_CONVERSATIONS = frozenset({"conv-26", "conv-30", "conv-41", "conv-42", "conv-43"})
_LOCOMO_ID = re.compile(r"^(conv-\d+)_")


def _locomo_split(conversation: str) -> str:
    if conversation in LOCOMO_TUNE_CONVERSATIONS:
        return TUNE
    return DECIDE


def split_of(question_id: str) -> str:
    """Which half of the stand a question belongs to, by its dataset's rule."""
    original = original_of(question_id)
    conversation = _LOCOMO_ID.match(original)
    if conversation:
        return _locomo_split(conversation.group(1))
    digest = hashlib.sha256(original.encode("utf-8")).hexdigest()
    if int(digest[-1], 16) % 2 == 0:
        return TUNE
    return DECIDE


def _stratum_of(question: dict) -> tuple[str, bool]:
    return (str(question["question_type"]), is_abstention(question))


def _grouped(data: list[dict]) -> dict[tuple[str, bool], list[dict]]:
    groups: dict[tuple[str, bool], list[dict]] = {}
    for question in data:
        groups.setdefault(_stratum_of(question), []).append(question)
    return groups


def _largest_remainder(quotas: dict[tuple, float], size: int) -> dict[tuple, int]:
    """Integer allocation that keeps every stratum proportional to its share."""
    base = {key: int(quota) for key, quota in quotas.items()}
    short = size - sum(base.values())
    by_fraction = sorted(quotas, key=lambda key: (quotas[key] - int(quotas[key]), key), reverse=True)
    for key in by_fraction[:short]:
        base[key] += 1
    return base


def stratified_sample(data: list[dict], size: int, seed: int) -> list[dict]:
    """A deterministic proportional sample across (question_type, abstention).

    Deterministic for a (data, size, seed) triple: strata are visited in
    sorted order and rows are sorted by question id before the draw, so the
    same command line names the same questions on every machine.
    """
    if size >= len(data):
        return list(data)
    groups = _grouped(data)
    quotas = {key: size * len(rows) / len(data) for key, rows in groups.items()}
    allocation = _largest_remainder(quotas, size)
    rng = random.Random(seed)
    picked: list[dict] = []
    for key in sorted(groups):
        rows = sorted(groups[key], key=lambda question: str(question["question_id"]))
        picked.extend(rng.sample(rows, min(allocation[key], len(rows))))
    return picked
