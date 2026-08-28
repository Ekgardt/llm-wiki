"""Cases, gold grading and lift/neutral/harm arithmetic for MEM-15.

Everything in this file is deterministic and provider-free, so the grading
rubric can be read, tested and argued with on its own. The runner
(`run_lift_attribution.py`) is the only place a model is called.

Why a separate rubric at all: the task this stand answers is whether memory
*improves* an answer, and the obvious way to get that number wrong is to let
the thing being measured grade itself. So the grader here never sees the
retrieved context, the prompt the product built, its confidence envelope or
its citations. It sees the answer text and a gold that was fixed before any
call was made — for `world` cases by running a command on this machine and
recording its output, for `vault` cases by the token the application stand
already verified appears verbatim in the gold page.

Token matching is word-bounded for alphanumeric tokens, which matters more
than it looks: `"15"` must not be satisfied by an answer that says `"5"`, and
`"5"` must not be satisfied by one that says `"15"`. Tokens carrying
punctuation (`--ff-only`, `16 KiB`, `.gz`) are matched as substrings, because
a word boundary around them means nothing.
"""
from __future__ import annotations

import json
import random
import re
import sys
from dataclasses import dataclass
from pathlib import Path

BENCHMARK_DIR = Path(__file__).resolve().parent
ROOT = BENCHMARK_DIR.parent
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

from reliable_memory import validate_schema  # noqa: E402

CORPUS = BENCHMARK_DIR / "lift-attribution-v1.json"
SCHEMA = BENCHMARK_DIR / "lift-attribution-v1.schema.json"
INHERITED_CORPUS = BENCHMARK_DIR / "vault-application-v1.json"

VAULT = "vault"
WORLD = "world"
STRATA = (VAULT, WORLD)

LIFT = "lift"
NEUTRAL = "neutral"
HARM = "harm"
OUTCOMES = (LIFT, NEUTRAL, HARM)

# Where a collision degree is counted. Documents, not code objects: the
# question is how many things a retriever could return that carry the same
# entity token, which is what pins the lexical floor in arXiv 2605.29630.
COLLISION_ROOTS = ("knowledge", "docs", "benchmark")
COLLISION_SUFFIXES = (".md", ".json")
COLLISION_FILE_LIMIT = 4000


@dataclass(frozen=True)
class Case:
    """One question with a gold fixed before any model was called."""

    case_id: str
    stratum: str
    question: str
    expected_tokens: tuple[str, ...]
    forbidden_tokens: tuple[str, ...]
    collision_probes: tuple[str, ...]
    gold_evidence: str

    def as_dict(self) -> dict:
        return {
            "case_id": self.case_id,
            "stratum": self.stratum,
            "question": self.question,
            "expected_tokens": list(self.expected_tokens),
            "forbidden_tokens": list(self.forbidden_tokens),
            "collision_probes": list(self.collision_probes),
            "gold_evidence": self.gold_evidence,
        }


def load_corpus(path: Path = CORPUS) -> dict:
    corpus = json.loads(path.read_text(encoding="utf-8"))
    validate_schema(corpus, SCHEMA)
    return corpus


def _world_case(raw: dict) -> Case:
    return Case(
        case_id=str(raw["case_id"]),
        stratum=WORLD,
        question=str(raw["question"]),
        expected_tokens=tuple(raw["expected_tokens"]),
        forbidden_tokens=tuple(raw.get("forbidden_tokens", ())),
        collision_probes=tuple(raw["collision_probes"]),
        gold_evidence=str(raw["gold_evidence"]),
    )


def _vault_case(raw: dict) -> Case:
    """A case inherited from the application stand, unmodified.

    The question text is that stand's `task` field and the gold is its
    `expected_tokens`, which it verified appear verbatim in the gold page.
    Nothing is re-authored here, so the two stands cannot drift apart and a
    test can prove it.
    """
    tokens = tuple(raw["expected_tokens"])
    return Case(
        case_id=str(raw["case_id"]),
        stratum=VAULT,
        question=str(raw["task"]),
        expected_tokens=tokens,
        forbidden_tokens=(),
        collision_probes=tokens,
        gold_evidence=str(raw["gold_path"]),
    )


def inherited_cases(path: Path = INHERITED_CORPUS) -> list[Case]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    return [_vault_case(case) for case in raw["cases"]]


def all_cases(corpus: dict | None = None) -> list[Case]:
    """Both strata, vault first, in a fixed order so runs stay comparable."""
    corpus = load_corpus() if corpus is None else corpus
    world = [_world_case(case) for case in corpus["cases"]]
    return inherited_cases() + world


def _is_wordish(token: str) -> bool:
    return bool(re.fullmatch(r"[0-9a-zA-Zа-яА-ЯёЁ_-]+", token))


def _token_pattern(token: str) -> re.Pattern[str]:
    """A word-bounded pattern for bare tokens, a literal one for the rest."""
    escaped = re.escape(token.casefold())
    if not _is_wordish(token):
        return re.compile(escaped)
    return re.compile(rf"(?<![0-9a-zA-Zа-яА-ЯёЁ_]){escaped}(?![0-9a-zA-Zа-яА-ЯёЁ_])")


def states(text: str, token: str) -> bool:
    """True when the answer really says this token, not merely contains it."""
    return bool(_token_pattern(token).search(text.casefold()))


def grade(answer: str | None, case: Case) -> bool:
    """Correct = every expected token said, no forbidden token said."""
    if not answer:
        return False
    if not all(states(answer, token) for token in case.expected_tokens):
        return False
    return not any(states(answer, token) for token in case.forbidden_tokens)


def classify(without_memory: bool, with_memory: bool) -> str:
    """The three outcomes the roadmap item asks for, and only those."""
    if with_memory and not without_memory:
        return LIFT
    if without_memory and not with_memory:
        return HARM
    return NEUTRAL


def _collision_files(vault: Path) -> list[Path]:
    found: list[Path] = []
    for name in COLLISION_ROOTS:
        found.extend(_files_under(vault / name))
    return found[:COLLISION_FILE_LIMIT]


def _files_under(root: Path) -> list[Path]:
    if not root.is_dir():
        return []
    return sorted(p for p in root.rglob("*") if p.suffix in COLLISION_SUFFIXES)


def _mentions_all(path: Path, probes: tuple[str, ...]) -> bool:
    try:
        text = path.read_text(encoding="utf-8", errors="replace").casefold()
    except OSError:
        return False
    return all(probe.casefold() in text for probe in probes)


def collision_degree(vault: Path, probes: tuple[str, ...]) -> int:
    """How many vault documents carry this case's entity tokens.

    The paper builds distractors that share the answer's entity tokens so the
    lexical floor is pinned by construction. We do not synthesise anything —
    the collision is already here — so this counts it instead, and the stand
    reports outcomes stratified by it.
    """
    return sum(1 for path in _collision_files(vault) if _mentions_all(path, probes))


def _counted(outcomes: list[str]) -> dict:
    return {name: outcomes.count(name) for name in OUTCOMES}


def _rate(count: int, total: int) -> float | None:
    if total == 0:
        return None
    return round(count / total, 4)


def summarise(outcomes: list[str]) -> dict:
    """Three fractions and the net, with n carried next to every one."""
    counts = _counted(outcomes)
    total = len(outcomes)
    rates = {f"{name}_rate": _rate(counts[name], total) for name in OUTCOMES}
    net = _net_lift(counts, total)
    return {"n": total, **counts, **rates, "net_lift_rate": net}


def _net_lift(counts: dict, total: int) -> float | None:
    if total == 0:
        return None
    return round((counts[LIFT] - counts[HARM]) / total, 4)


def by_stratum(rows: list[dict]) -> dict:
    """Stratified exactly as the paper insists: never one mixed average."""
    report = {}
    for name in STRATA:
        picked = [row["outcome"] for row in rows if row.get("stratum") == name]
        report[name] = summarise(picked)
    return report


# The paper sweeps five collision degrees. We do not synthesise a degree, we
# measure the one this vault already has, so the axis is banded rather than
# swept: how many documents here carry the case's entity tokens.
COLLISION_BANDS = (("none", 0, 1), ("low", 2, 9), ("high", 10, 10**9))


def _band_of(degree: int) -> str:
    for name, low, high in COLLISION_BANDS:
        if low <= degree <= high:
            return name
    return "high"


def by_collision(rows: list[dict]) -> dict:
    """Outcomes against how contested the question's entity tokens are here."""
    report = {}
    for name, _low, _high in COLLISION_BANDS:
        picked = [row["outcome"] for row in rows if _band_of(row.get("collision_degree", 0)) == name]
        report[name] = summarise(picked)
    return report


def _resampled(outcomes: list[str], rng: random.Random) -> list[str]:
    return [rng.choice(outcomes) for _ in outcomes]


def _net_of(outcomes: list[str]) -> float:
    total = len(outcomes)
    if total == 0:
        return 0.0
    return (outcomes.count(LIFT) - outcomes.count(HARM)) / total


def bootstrap_net_ci(outcomes: list[str], draws: int = 2000, seed: int = 20260828) -> dict:
    """Paired bootstrap over the per-case outcomes, as the paper reports CIs.

    Paired is what the outcome label already is: each case contributes one
    label computed from both of its answers, so resampling cases resamples
    pairs. Seeded, so the interval is reproducible from the results file.
    """
    if not outcomes:
        return {"low": None, "high": None, "draws": 0}
    rng = random.Random(seed)
    nets = sorted(_net_of(_resampled(outcomes, rng)) for _ in range(draws))
    return {
        "low": round(nets[int(0.025 * draws)], 4),
        "high": round(nets[int(0.975 * draws) - 1], 4),
        "draws": draws,
    }


def indistinguishable(net_rate: float | None, noise_points: float) -> bool:
    """True when the delta is inside this provider's own disagreement rate.

    `NEW-122`, measured 2026-08-28: at a byte-identical prompt this provider
    disagrees with itself on 2 of 23 questions, 8.7 points. A net lift under
    that is not a win, and this stand refuses to call it one.
    """
    if net_rate is None:
        return True
    return abs(net_rate) * 100 < noise_points
