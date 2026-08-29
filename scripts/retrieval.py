"""Deterministic retrieval contract: query analysis, profiles, RRF fusion."""
from __future__ import annotations

import hashlib
import json
import os
import queue
import re
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from dataclasses import field as dataclass_field
from pathlib import Path
from typing import Any

from provenance import authority_weight, curated_pages_first, source_type_weight

MAX_OPTIONAL_STRAGGLERS = 2

# The ceiling on one optional stage, on top of the share of the caller's budget
# that `_optional_stage_deadline` already grants it. It exists for the caller who
# passes a very generous deadline: half of ten minutes is still five minutes to
# wait on a signal nobody needs.
#
# It arrived as a bare 0.5 with no measurement and silently overrode the share
# for everyone. Measured on this vault: the warm dense stage of one recall costs
# 0.99-1.33 s and the cold one — the first in a process, which now carries the
# model load — costs 8.85 s. Every one of those is above 0.5, so no caller that
# passed a deadline could ever use the semantic leg: six recall calls in one
# server, all six `optional_stage_timeout`, lexical only.
#
# 12 s is above the measured cold stage with room for a loaded machine, so a
# caller that granted enough budget can pay the one-time load on its only call —
# which is what a one-shot CLI answer needs, having no second call to be warm
# for. Short-budget callers are unaffected by the ceiling because the share
# binds first: the MCP path's ten seconds still grant an optional stage five.
# What it costs: a stage that will never finish now delays an answer by up to
# 12 s instead of 0.5 s, and only for a caller that granted at least 24 s.
OPTIONAL_STAGE_MAX_SECONDS = 12.0
_OPTIONAL_STAGE_SLOTS = threading.BoundedSemaphore(MAX_OPTIONAL_STRAGGLERS)

# One straggler slot per kind of optional stage, instead of one shared pool of
# `MAX_OPTIONAL_STRAGGLERS` that every kind competes for.
#
# The shared pool made a straggler of one kind refuse admission to a stage of
# another kind, before that stage waited for anything. Measured on the live
# vault, six recall calls in one process at the 10 s MCP budget: call 1
# abandoned both of its stages, they held both slots for the length of their
# work, and calls 2, 4 and 6 were refused `optional stage capacity exhausted`
# at 0.00 s -- the dense leg reached the answer in one call out of six, 1.75 of
# six averaged over four such rounds. The two hogs are model loads: about 9 s
# for the embedding model, about 20 s for the cross-encoder, both far longer
# than the 5 s an MCP-budget stage may wait, so one abandoned rerank load shut
# the dense leg out of every call behind it. Partitioned, the same measurement
# gives 3.5 of six; see
# `docs/research/2026-08-26-who-pays-for-an-abandoned-optional-stage.md`.
#
# Partitioning by kind is the bulkhead rule: capacity for one dependency is
# reserved from capacity for all the others, so a slow one exhausts only its
# own. It also makes each kind single-flight, which is stricter than the shared
# pool was -- two dense stragglers could previously load the embedding model
# twice at about 1.1 GiB each, a cost `search_memory._lazy_generation_query_encoder`
# accepts in its docstring and this now prevents.
#
# What it costs. A second stage of a kind already in flight is refused
# immediately rather than queued behind it: the caller could instead wait for
# the straggler and then run warm, but the measured loads (9 s, 20 s) do not
# fit in an MCP-budget stage (5 s), so that wait would spend the caller's
# budget and delay the lexical answer for a leg that still could not finish.
# The live thread bound is unchanged at two, one per kind, and the kinds are a
# fixed tuple rather than caller-supplied, so no call can widen it.
OPTIONAL_STAGE_KINDS = ("dense", "rerank")
_OPTIONAL_STAGE_KIND_SLOTS = {
    kind: threading.BoundedSemaphore(1) for kind in OPTIONAL_STAGE_KINDS
}


def _optional_stage_slots(kind: str | None) -> threading.BoundedSemaphore:
    """The straggler slots this stage competes for; unlabelled work shares one pool."""
    return _OPTIONAL_STAGE_KIND_SLOTS.get(kind, _OPTIONAL_STAGE_SLOTS)


def _normalized_filename_stem(value: str) -> str:
    name = Path(value.strip()).name.casefold()
    if name.endswith(".md"):
        name = name[:-3]
    return "-".join(part for part in re.split(r"[\s_-]+", name) if part)


def _first_exact_filename(
    candidates: Sequence[RetrievalCandidate], normalized: str
) -> RetrievalCandidate | None:
    for candidate in candidates:
        if _normalized_filename_stem(candidate.relative_path) == normalized:
            return candidate
    return None


def _promote_exact_filename(
    candidates: Sequence[RetrievalCandidate], query: str
) -> tuple[RetrievalCandidate, ...]:
    normalized = _normalized_filename_stem(query)
    if not normalized:
        return tuple(candidates)
    exact = _first_exact_filename(candidates, normalized)
    if exact is None:
        return tuple(candidates)
    return (exact, *(candidate for candidate in candidates if candidate is not exact))


class OptionalStageTimeout(TimeoutError):
    """An optional uninterruptible stage exceeded its isolated budget."""


# What one kind of optional stage last cost, when a run of it finished --
# whether the caller waited for that run or abandoned it and the daemon
# straggler finished it later. This is the cost model admission uses.
#
# One sample deep on purpose. A mean would remember the cold model load for the
# rest of the process and refuse a leg that has been warm for an hour; the last
# finished run is the only figure that tracks the thing that actually changes,
# which is whether the model is resident. It self-corrects in one call in
# either direction, and a wrongly skipped stage degrades to the lexical answer,
# which is the designed fallback rather than a failure.
_OPTIONAL_STAGE_OBSERVED: dict[str, float] = {}
_OPTIONAL_STAGE_OBSERVED_LOCK = threading.Lock()


def _observe_optional_stage(kind: str | None, seconds: float) -> None:
    """Record what a finished run of this kind cost, in units the decision can use.

    Clamped at the ceiling, because the figure is only ever compared against a
    window and no window may exceed `OPTIONAL_STAGE_MAX_SECONDS`. Above that
    everything means the same thing -- "longer than any stage is ever allowed
    to wait" -- so keeping more precision than the comparison can spend only
    creates differences that behave identically. It also stops one pathological
    run governing far longer than it should: a stage that hung for thirty
    seconds is recorded as "does not fit", not as thirty seconds, and the next
    run that finishes replaces it either way.
    """
    if kind is None:
        return
    with _OPTIONAL_STAGE_OBSERVED_LOCK:
        _OPTIONAL_STAGE_OBSERVED[kind] = min(seconds, OPTIONAL_STAGE_MAX_SECONDS)


def _observed_optional_stage_cost(kind: str | None) -> float | None:
    if kind is None:
        return None
    with _OPTIONAL_STAGE_OBSERVED_LOCK:
        return _OPTIONAL_STAGE_OBSERVED.get(kind)


# The time between `_optional_stage_deadline` granting a window and
# `_optional_stage_fits` measuring it: a few function calls and a semaphore.
# It exists so that "was this stage given the whole ceiling?" is not decided by
# clock slop.
_OPTIONAL_STAGE_WINDOW_SLACK_SECONDS = 0.25


def _unknown_cost_stage_fits(window: float) -> bool:
    """Whether to wait for a kind nothing has been observed for yet.

    Only when the caller granted enough budget that the ceiling, not its own
    share, is what bounds the stage. That is the case `OPTIONAL_STAGE_MAX_SECONDS`
    already exists for and says so: a one-shot CLI answer has no second call to
    be warm for, so it must be allowed to pay the one-time model load itself.

    A caller on the MCP budget is the other case. Its share is around 3.5 s
    against a measured cold load of 10.13 s, so waiting cannot succeed -- and it
    does not have to, because the worker below has already been started and the
    straggler that finishes it records what it cost. The next call reads that
    and waits for a warm stage that fits. The cost is learned without anyone
    paying for it twice.
    """
    return window >= OPTIONAL_STAGE_MAX_SECONDS - _OPTIONAL_STAGE_WINDOW_SLACK_SECONDS


def _optional_stage_fits(kind: str | None, deadline: float) -> bool:
    """Whether a run of this kind is expected to finish in the window on offer.

    Unlabelled work is always admitted: the cost model is per kind, and a stage
    with no kind has nothing to be modelled against.
    """
    if kind is None:
        return True
    window = deadline - time.monotonic()
    observed = _observed_optional_stage_cost(kind)
    if observed is None:
        return _unknown_cost_stage_fits(window)
    return observed <= window


def _require_optional_stage_time(
    deadline: float, cancelled: Callable[[], bool] | None
) -> None:
    if deadline - time.monotonic() <= 0:
        raise OptionalStageTimeout("optional stage deadline reached")
    if cancelled is not None and cancelled():
        raise OptionalStageTimeout("optional stage deadline reached")


def _optional_stage_admitted(
    kind: str | None, deadline: float, cancelled: Callable[[], bool] | None
) -> bool:
    """Whether the caller may *wait* for this stage.

    Distinct from whether the stage may run. A stage the caller cannot wait for
    is still worth starting, because the straggler that finishes it leaves the
    model resident and records what it cost, which is what makes the next call
    cheap. Only the wait is refused.
    """
    if cancelled is not None and cancelled():
        return False
    if deadline - time.monotonic() <= 0:
        return False
    return _optional_stage_fits(kind, deadline)


def _run_optional_bounded(
    operation: Callable[[], Any],
    *,
    deadline: float,
    cancelled: Callable[[], bool] | None,
    kind: str | None = None,
) -> Any:
    """Run optional work with a hard wait bound and capped daemon stragglers.

    The stage is always started; what varies is whether the caller waits for
    it. That split is the point: warming is never refused, only the spending of
    a budget that cannot buy a result.
    """
    # Decided before the worker starts, and deliberately so: this run is about
    # to record its own cost, and a fast one would otherwise overwrite the
    # observation the decision is being made from.
    admitted = _optional_stage_admitted(kind, deadline, cancelled)
    slots = _optional_stage_slots(kind)
    if not slots.acquire(blocking=False):
        raise OptionalStageTimeout("optional stage capacity exhausted")
    completed = threading.Event()
    result: queue.Queue[tuple[bool, Any]] = queue.Queue(maxsize=1)
    _start_optional_worker(operation, result, completed, slots, kind)
    _require_admitted_optional_stage(admitted)
    _await_optional_stage(completed, deadline, cancelled)
    ok, value = result.get_nowait()
    if ok:
        return value
    raise value


def _require_admitted_optional_stage(admitted: bool) -> None:
    """Decline the wait for a stage that is not expected to finish in time.

    The worker is already running when this refuses, and that is deliberate:
    the straggler warming the model is the whole point of the daemon design,
    and only the caller's *wait* is being declined. Measured on this vault, a
    cold embedding load costs 10.13 s against the ~3.5 s window a 10 s
    operation can offer, so waiting for it spends a third of the budget to
    arrive at the same lexical answer the caller would have had for nothing.
    """
    if not admitted:
        raise OptionalStageTimeout("optional stage exceeds the budget on offer")


def _start_optional_worker(
    operation: Callable[[], Any],
    result: queue.Queue[tuple[bool, Any]],
    completed: threading.Event,
    slots: threading.BoundedSemaphore,
    kind: str | None = None,
) -> None:
    def run() -> None:
        started = time.monotonic()
        try:
            value = operation()
        except BaseException as exc:
            result.put((False, exc))
        else:
            # Only a run that produced something is a cost observation. A fast
            # failure is not evidence that the work is cheap.
            _observe_optional_stage(kind, time.monotonic() - started)
            result.put((True, value))
        finally:
            completed.set()
            slots.release()

    worker = threading.Thread(target=run, name="llm-wiki-optional-retrieval", daemon=True)
    try:
        worker.start()
    except BaseException:
        slots.release()
        raise


def _await_optional_stage(
    completed: threading.Event,
    deadline: float,
    cancelled: Callable[[], bool] | None,
) -> None:
    """Wait out one optional stage; a straggler keeps running as a daemon."""
    stage_deadline = min(deadline, time.monotonic() + OPTIONAL_STAGE_MAX_SECONDS)
    while not completed.is_set():
        if cancelled is not None and cancelled():
            raise OptionalStageTimeout("optional stage cancelled")
        wait = stage_deadline - time.monotonic()
        if wait <= 0:
            raise OptionalStageTimeout("optional stage deadline reached")
        completed.wait(min(wait, 0.01))

PROFILES = (
    "DIRECT",
    "EXACT",
    "BASE",
    "HYBRID",
    "GRAPH",
    "TEMPORAL",
    "REPO_MAP",
    "IMPACT",
    "GLOBAL",
    "CACHED_FULL",
)

# Declared signals each profile may request. Actual use is reported at runtime.
PROFILE_SIGNALS: dict[str, tuple[str, ...]] = {
    "DIRECT": ("lexical",),
    "EXACT": ("lexical",),
    "BASE": ("lexical",),
    "HYBRID": ("lexical", "dense"),
    "GRAPH": ("lexical", "graph"),
    "TEMPORAL": ("lexical",),
    "REPO_MAP": ("lexical", "graph"),
    "IMPACT": ("lexical", "graph"),
    "GLOBAL": ("lexical", "dense", "graph"),
    "CACHED_FULL": ("lexical",),
}

RRF_K = 60
# A short answer needs a long candidate pool: several chunks of one page are
# one answer repeated, and only a pool wider than the answer can hold the
# pages the page-diverse order then leads with.
CANDIDATE_FANOUT = 8
MIN_CANDIDATE_POOL = 40
MAX_CANDIDATE_POOL = 200
BM25_WEIGHT = 2.0
DENSE_WEIGHT = 1.0
GRAPH_WEIGHT = 0.5

GRAPH_MAX_HOPS = 1
GRAPH_SEED_LIMIT = 5
GRAPH_PER_SEED_LIMIT = 4
GRAPH_GLOBAL_LIMIT = 12
GRAPH_EDGE_DECAY: dict[str, float] = {
    "BELONGS_TO_PROJECT": 0.65,
    "CALLS": 0.85,
    "CHECKPOINT_CHANGED_FILE": 0.75,
    "CHECKPOINT_EVIDENCED_BY_EVENT": 0.65,
    "CHECKPOINT_HAS_BLOCKER": 0.75,
    "CHECKPOINT_RECORDED_DECISION": 0.75,
    "CO_CHANGED_WITH": 0.45,
    "CONTAINS": 0.70,
    "DEFINES": 0.80,
    "EVIDENCED_BY": 0.70,
    "EXPOSES": 0.80,
    "IMPLEMENTS": 0.80,
    "IMPORTS": 0.80,
    "INHERITS": 0.80,
    "LINKS_TO": 0.65,
    "PROJECT_HAS_CHECKPOINT": 0.70,
    "READS": 0.75,
    "REFERENCES_SYMBOL": 0.75,
    "SUPERSEDES": 0.70,
    "WRITES": 0.75,
}
GRAPH_PROFILE_DIRECTIONS: dict[str, tuple[str, ...]] = {
    "GRAPH": ("in", "out"),
    "REPO_MAP": ("out",),
    "IMPACT": ("in",),
    "GLOBAL": ("in", "out"),
}
GRAPH_PROFILE_EDGE_TYPES: dict[str, tuple[str, ...]] = {
    "GRAPH": tuple(GRAPH_EDGE_DECAY),
    "REPO_MAP": (
        "CONTAINS",
        "DEFINES",
        "EXPOSES",
        "IMPLEMENTS",
        "IMPORTS",
        "INHERITS",
    ),
    "IMPACT": (
        "CALLS",
        "CHECKPOINT_CHANGED_FILE",
        "CO_CHANGED_WITH",
        "IMPORTS",
        "READS",
        "REFERENCES_SYMBOL",
        "WRITES",
    ),
    "GLOBAL": tuple(GRAPH_EDGE_DECAY),
}

BackendFn = Callable[..., Sequence[Mapping[str, Any]] | None]


class GenerationSealChanged(RuntimeError):
    """The active immutable generation changed during one retrieval."""

_QUOTE_RE = re.compile(r'"([^"\n]{1,256})"')
_PATH_RE = re.compile(
    r"(?P<path>"
    r"(?:[A-Za-z0-9_.-]+/)+[A-Za-z0-9_.-]+"
    r"\.(?:md|py|ts|tsx|js|jsx|rs|go|java|cs|sqlite3?|json|yaml|yml|toml)"
    r")"
)
_FILENAME_RE = re.compile(
    r"(?<![A-Za-z0-9_/.-])"
    r"(?P<file>[A-Za-z0-9_.-]+\.(?:md|py|ts|tsx|js|jsx|rs|go|java|cs|sqlite3?|json|yaml|yml|toml))"
    r"(?![A-Za-z0-9_/.-])"
)
_SLUG_RE = re.compile(r"\b(?P<slug>[a-z0-9]+(?:-[a-z0-9]+){1,12})\b")
_SNAKE_RE = re.compile(r"\b(?P<snake>[a-z][a-z0-9]*(?:_[a-z0-9]+)+)\b")
_CAMEL_RE = re.compile(r"\b(?P<camel>[A-Z][a-z0-9]+(?:[A-Z][a-z0-9]+)+)\b")
_QUESTION_RE = re.compile(
    r"(?:"
    r"^(?P<q_en>who|what|when|where|why|how|which|does|do|is|are|can|should)\b"
    r"|^(?P<q_ru>кто|что|когда|где|почему|зачем|как|какой|какая|какие|какое)\b"
    r"|^(?P<q_zh>谁|什么|何时|哪里|哪儿|为什么|怎么|如何|哪)"
    r"|[?？]"
    r")",
    re.IGNORECASE,
)
# What may follow a relational preposition and make it about time.
_WHEN = (
    r"(?:\d{4}(?:-\d{2}(?:-\d{2})?)?"
    r"|yesterday|today|tomorrow|now"
    r"|вчера|сегодня|завтра|сейчас)"
)
# A relational preposition is temporal only when something temporal follows it.
# `после` and `after` are ordinary sequence words, and reading them alone as a
# time window costs the answer: measured on this vault, "как устроен повтор
# после карантина" was routed to TEMPORAL, TEMPORAL declares no dense signal,
# so the grounded answer never asked the vectors. Retrieval returned six lexical
# rows — four of them the same status document, none of them the decision page
# the vault holds — and the provider honestly reported insufficient evidence.
# Every other alternative in this group was already anchored to a real time
# expression; these prepositions were the ones that were not.
_TEMPORAL_RE = re.compile(
    r"(?:"
    r"\b(?P<t_en_anchored>since|before|after|until|as of|from)\s+" + _WHEN + r"\b"
    r"|\b(?P<t_en>yesterday|today|last week|last month|last year"
    r"|in \d{4}|from \d{4}|between \d{4})\b"
    r"|(?P<t_ru_anchored>(?:с|со|после|до|от)\s+" + _WHEN + r")"
    r"|(?P<t_ru>вчера|сегодня|на этой неделе)"
    r"|(?P<t_zh>自\s*\d{4}|以来|自从)"
    r")",
    re.IGNORECASE,
)
_GRAPH_RE = re.compile(
    r"(?:"
    r"\b(?P<g_en>depends on|depended on|dependency|dependencies|calls|callers|"
    r"callees|related to|linked to|neighbors?|imports?|imported by)\b"
    r"|(?P<g_ru>зависит|зависимости|связан)"
    r"|(?P<g_zh>依赖|什么依赖|相关联)"
    r")",
    re.IGNORECASE,
)
_REPO_MAP_RE = re.compile(
    r"(?:"
    r"\b(?P<r_en>repo map|repository map|codebase map|architecture overview|"
    r"project structure|directory structure)\b"
    r"|(?P<r_ru>карт[ауи]\s+репозитори[яию]|структур[аыу]\s+проекта)"
    r"|(?P<r_zh>仓库地图|显示仓库地图|项目结构)"
    r")",
    re.IGNORECASE,
)
_IMPACT_RE = re.compile(
    r"(?:"
    r"\b(?P<i_en>impact of|what breaks|affected by|blast radius|stale wiki|"
    r"downstream impact)\b"
    r"|(?P<i_ru>влияние|что сломается)"
    r"|(?P<i_zh>影响|的影响)"
    r")",
    re.IGNORECASE,
)
_GLOBAL_RE = re.compile(
    r"(?:"
    r"\b(?P<s_en>synthesize|synthesis|across all projects|across projects|"
    r"overall architecture|compare across|global overview)\b"
    r"|(?P<s_ru>синтез|глобальный обзор)"
    r"|(?P<s_zh>跨项目|综合架构|全局概览)"
    r")",
    re.IGNORECASE,
)
_CROSS_LANG_RE = re.compile(
    r"(?:[\u0400-\u04FF].*[A-Za-z]|[A-Za-z].*[\u0400-\u04FF]|"
    r"[\u4e00-\u9fff].*[A-Za-z]|[A-Za-z].*[\u4e00-\u9fff])"
)


@dataclass(frozen=True)
class QueryAnalysis:
    query: str
    normalized_query: str
    intents: tuple[str, ...]
    exact_identifiers: tuple[str, ...]
    quoted_phrases: tuple[str, ...]
    recommended_profile: str


@dataclass(frozen=True)
class RetrievalTrace:
    requested_mode: str
    effective_mode: str
    signals_used: tuple[str, ...]
    fallback_reason: str | None
    corpus_generation: str
    partial: bool
    reranker_applied: bool = False
    reranker_model_id: str | None = None
    reranker_model_revision: str | None = None
    reranker_depth: int | None = None
    reranker_duration_ms: int | None = None
    reranker_fallback_reason: str | None = None


@dataclass(frozen=True)
class RetrievalCandidate:
    candidate_id: str
    parent_id: str
    relative_path: str
    heading_path: tuple[str, ...]
    source_sha256: str
    byte_start: int
    byte_end: int
    bm25_rank: int | None
    bm25_score: float | None
    vector_rank: int | None
    vector_score: float | None
    graph_rank: int | None
    graph_score: float | None
    rrf_score: float
    rerank_score: float | None
    final_score: float
    evidence_ids: tuple[str, ...]
    # Typed provenance weighs on the score that decides the order; see
    # scripts/provenance.py. 1.0 means unknown or unweighted provenance.
    authority_weight: float = 1.0
    # What the page is — the second factor of the same weight. 1.0 means a type
    # this table does not rank, which includes code.
    type_weight: float = 1.0


@dataclass(frozen=True)
class RetrievalResult:
    candidates: tuple[RetrievalCandidate, ...]
    trace: RetrievalTrace
    analysis: QueryAnalysis
    display_meta: Mapping[str, Mapping[str, Any]] | None = None


def _profile_text(value: object) -> str:
    """The canonical spelling of a profile name, or a type error."""
    if not isinstance(value, str):
        raise TypeError("profile must be a string")
    return value.strip().upper().replace("-", "_")


def _normalize_profile(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = _profile_text(value)
    if normalized not in PROFILES:
        raise ValueError(f"unknown retrieval profile: {value}")
    return normalized


_INTENT_PATTERNS = (
    ("temporal", _TEMPORAL_RE),
    ("graph_relation", _GRAPH_RE),
    ("repo_map", _REPO_MAP_RE),
    ("impact", _IMPACT_RE),
    ("global_synthesis", _GLOBAL_RE),
)

# First match wins; the order is the priority the planner promises.
_PROFILE_BY_INTENT = (
    ("global_synthesis", "GLOBAL"),
    ("impact", "IMPACT"),
    ("repo_map", "REPO_MAP"),
    ("graph_relation", "GRAPH"),
    ("temporal", "TEMPORAL"),
    ("quoted_phrase", "EXACT"),
    ("exact_identifier", "EXACT"),
    ("question", "HYBRID"),
)


def _quoted_phrases(stripped: str) -> tuple[str, ...]:
    return tuple(
        match.group(1).strip()
        for match in _QUOTE_RE.finditer(stripped)
        if match.group(1).strip()
    )


def _slug_is_redundant(slug: str, identifiers: list[str]) -> bool:
    """A slug already covered by a path or a longer identifier adds nothing."""
    if "/" in slug or slug in identifiers:
        return True
    return any(slug in item for item in identifiers)


def _exact_identifiers(stripped: str) -> tuple[str, ...]:
    identifiers: list[str] = []
    for pattern, group in (
        (_PATH_RE, "path"),
        (_FILENAME_RE, "file"),
        (_CAMEL_RE, "camel"),
        (_SNAKE_RE, "snake"),
    ):
        identifiers.extend(match.group(group) for match in pattern.finditer(stripped))
    for match in _SLUG_RE.finditer(stripped):
        slug = match.group("slug")
        if not _slug_is_redundant(slug, identifiers):
            identifiers.append(slug)
    return tuple(dict.fromkeys(identifiers))


def _is_question(stripped: str, normalized: str) -> bool:
    if _QUESTION_RE.search(normalized):
        return True
    return stripped.endswith("?") or stripped.endswith("？")


def _shape_intents(
    stripped: str, normalized: str, phrases: tuple[str, ...], identifiers: tuple[str, ...]
) -> list[str]:
    """Intents that come from the query's shape rather than its wording."""
    present = (
        (bool(phrases), "quoted_phrase"),
        (bool(identifiers), "exact_identifier"),
        (_is_question(stripped, normalized), "question"),
    )
    return [intent for found, intent in present if found]


def _query_intents(
    stripped: str, normalized: str, phrases: tuple[str, ...], identifiers: tuple[str, ...]
) -> tuple[str, ...]:
    intents = _shape_intents(stripped, normalized, phrases, identifiers)
    intents.extend(
        name for name, pattern in _INTENT_PATTERNS if pattern.search(normalized)
    )
    if _CROSS_LANG_RE.search(stripped):
        intents.append("cross_language")
    return tuple(intents)


def _recommended_profile(intents: tuple[str, ...]) -> str:
    intent_set = set(intents)
    for intent, profile in _PROFILE_BY_INTENT:
        if intent in intent_set:
            return profile
    return "BASE"


def analyze_query(query: str) -> QueryAnalysis:
    """Deterministic query analysis used by the retrieval planner."""
    if not isinstance(query, str):
        raise TypeError("query must be a string")
    stripped = query.strip()
    normalized = " ".join(stripped.split())
    phrases = _quoted_phrases(stripped)
    identifiers = _exact_identifiers(stripped)
    intents = _query_intents(stripped, normalized, phrases, identifiers)
    return QueryAnalysis(
        query=stripped,
        normalized_query=normalized,
        intents=intents,
        exact_identifiers=identifiers,
        quoted_phrases=phrases,
        recommended_profile=_recommended_profile(intents),
    )


def _as_float(value: object) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _as_int(value: object, default: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        return default
    return value


def _heading_path(value: object) -> tuple[str, ...]:
    if isinstance(value, (tuple, list)):
        return tuple(str(item) for item in value)
    if isinstance(value, str) and value:
        return (value,)
    return ()


def _candidate_key(row: Mapping[str, Any]) -> str:
    for key in ("candidate_id", "chunk_id", "path", "relative_path"):
        value = row.get(key)
        if isinstance(value, str) and value:
            return value
    raise ValueError("retrieval hit is missing candidate identity")


def _hit_path(row: Mapping[str, Any]) -> str:
    for key in ("relative_path", "path", "source_path"):
        value = row.get(key)
        if isinstance(value, str) and value:
            return value
    return _candidate_key(row)


def _graph_seeds(
    lexical: Sequence[Mapping[str, Any]] | None,
    dense: Sequence[Mapping[str, Any]] | None,
) -> tuple[Mapping[str, Any], ...]:
    """Select rank-qualified seeds without comparing backend score magnitudes."""
    selected: dict[str, Mapping[str, Any]] = {}
    for rows in (lexical or (), dense or ()):
        _collect_seeds(selected, rows)
    return tuple(selected.values())[:GRAPH_SEED_LIMIT]


def _collect_seeds(
    selected: dict[str, Mapping[str, Any]], rows: Sequence[Mapping[str, Any]]
) -> None:
    for rank, row in enumerate(rows, start=1):
        if _seed_qualifies(row, rank):
            selected.setdefault(_candidate_key(row), row)


def _seed_qualifies(row: Mapping[str, Any], rank: int) -> bool:
    """A stated high confidence qualifies; otherwise the rank has to."""
    confidence = row.get("retrieval_confidence")
    if confidence is None:
        return rank <= GRAPH_SEED_LIMIT
    return str(confidence).casefold() == "high"


def _query_terms(text: str) -> frozenset[str]:
    return frozenset(re.findall(r"[\w-]+", text.casefold(), flags=re.UNICODE))


def _graph_text_relevance(query: str, row: Mapping[str, Any]) -> tuple[int, int]:
    terms = _query_terms(query)
    text = " ".join(
        str(row.get(field) or "")
        for field in ("title", "summary", "content", "relative_path", "path")
    )
    row_terms = _query_terms(text)
    return len(terms & row_terms), int(bool(terms) and terms.issubset(row_terms))


def _assertion_path(row: Mapping[str, Any]) -> tuple[dict[str, Any], ...]:
    raw_path = row.get("assertion_path")
    if not isinstance(raw_path, (list, tuple)) or not raw_path:
        return ()
    path: list[dict[str, Any]] = []
    for raw_step in raw_path:
        step = _assertion_step(raw_step)
        if step is None:
            return ()
        path.append(step)
    return tuple(path)


def _step_has_identity(raw_step: object) -> bool:
    """A step worth reading at all: a mapping that names its assertion."""
    if not isinstance(raw_step, Mapping):
        return False
    assertion_id = raw_step.get("assertion_id")
    return isinstance(assertion_id, str) and bool(assertion_id)


def _assertion_step(raw_step: object) -> dict[str, Any] | None:
    """One path step, or None when its identity or evidence is unusable."""
    if not _step_has_identity(raw_step):
        return None
    assert isinstance(raw_step, Mapping)
    evidence_ids = _evidence_id_tuple(raw_step.get("evidence_ids"))
    if not evidence_ids:
        return None
    step = dict(raw_step)
    step["evidence_ids"] = evidence_ids
    return step


def _evidence_id_tuple(raw_evidence: object) -> tuple[str, ...]:
    if not isinstance(raw_evidence, (list, tuple)):
        return ()
    return tuple(item for item in raw_evidence if isinstance(item, str) and item)


def _graph_row_is_valid(
    row: Mapping[str, Any],
    path: tuple[dict[str, Any], ...],
    allowed_directions: set[str],
    allowed_edges: set[str],
) -> bool:
    """A usable expansion: in-range hop, allowed edge and direction, real path."""
    hop = _as_int(row.get("hop"), 0)
    if not 1 <= hop <= GRAPH_MAX_HOPS:
        return False
    if not _graph_edge_allowed(row, allowed_directions, allowed_edges):
        return False
    return bool(path)


def _graph_edge_allowed(
    row: Mapping[str, Any], allowed_directions: set[str], allowed_edges: set[str]
) -> bool:
    """The row travels an allowed direction along an allowed edge family."""
    return (
        row.get("direction") in allowed_directions
        and row.get("edge_type") in allowed_edges
    )


def _path_evidence_ids(path: Sequence[Mapping[str, Any]]) -> tuple[str, ...]:
    return tuple(
        dict.fromkeys(
            evidence_id for step in path for evidence_id in step["evidence_ids"]
        )
    )


def _scored_graph_row(
    row: dict[str, Any],
    *,
    query: str,
    direct_graph_query: bool,
    path: tuple[dict[str, Any], ...],
) -> tuple[tuple[object, ...], dict[str, Any]] | None:
    """Rank key and enriched row, or None when the text says it is unrelated."""
    overlap, full_match = _graph_text_relevance(query, row)
    if overlap == 0 and not direct_graph_query:
        return None
    hop = _as_int(row.get("hop"), 0)
    decay = GRAPH_EDGE_DECAY[str(row.get("edge_type"))] ** hop
    row.update(
        assertion_path=path,
        evidence_ids=_path_evidence_ids(path),
        graph_boost=decay,
        graph_text_overlap=overlap,
    )
    key = (-full_match, -overlap, hop, -decay, str(row.get("seed_id")), _candidate_key(row))
    return key, row


def _prepared_graph_row(
    raw: Mapping[str, Any],
    backend_rank: int,
    *,
    query: str,
    direct_graph_query: bool,
    seed_ids: set[str],
    allowed_directions: set[str],
    allowed_edges: set[str],
) -> tuple[tuple[object, ...], dict[str, Any]] | None:
    row = dict(raw)
    seed_id = row.get("seed_id")
    if seed_id is None:
        # Independent pre-Task22 graph retrievers remain rank-only inputs.
        return (0, 0, 0, backend_rank, _candidate_key(row)), row
    if not isinstance(seed_id, str) or seed_id not in seed_ids:
        return None
    return _validated_graph_row(
        row,
        query=query,
        direct_graph_query=direct_graph_query,
        allowed_directions=allowed_directions,
        allowed_edges=allowed_edges,
    )


def _validated_graph_row(
    row: dict[str, Any],
    *,
    query: str,
    direct_graph_query: bool,
    allowed_directions: set[str],
    allowed_edges: set[str],
) -> tuple[tuple[object, ...], dict[str, Any]] | None:
    """Score a seeded row once its hop, edge, direction and path check out."""
    path = _assertion_path(row)
    if not _graph_row_is_valid(row, path, allowed_directions, allowed_edges):
        return None
    return _scored_graph_row(
        row, query=query, direct_graph_query=direct_graph_query, path=path
    )


def _merged_assertion_path(
    existing: Mapping[str, Any], row: Mapping[str, Any]
) -> tuple[Any, ...]:
    merged = list(existing.get("assertion_path") or ())
    for step in row.get("assertion_path") or ():
        if step not in merged:
            merged.append(step)
    return tuple(merged)


def _merge_graph_duplicate(existing: dict[str, Any], row: Mapping[str, Any]) -> None:
    """Two paths to the same candidate keep both routes and the stronger boost."""
    existing["assertion_path"] = _merged_assertion_path(existing, row)
    existing["evidence_ids"] = tuple(
        dict.fromkeys((*existing.get("evidence_ids", ()), *row.get("evidence_ids", ())))
    )
    existing["graph_boost"] = max(
        float(existing.get("graph_boost") or 0.0),
        float(row.get("graph_boost") or 0.0),
    )


def _accept_graph_row(
    selected: dict[str, dict[str, Any]],
    row: Mapping[str, Any],
    global_limit: int,
) -> bool:
    """Take the row, or merge it into the candidate already chosen."""
    candidate_id = _candidate_key(row)
    existing = selected.get(candidate_id)
    if existing is not None:
        _merge_graph_duplicate(existing, row)
        return True
    if len(selected) >= global_limit:
        return False
    selected[candidate_id] = dict(row)
    return True


def _select_graph_hits(
    prepared: list[tuple[tuple[object, ...], dict[str, Any]]],
    *,
    per_seed_limit: int,
    global_limit: int,
) -> tuple[Mapping[str, Any], ...]:
    selected: dict[str, dict[str, Any]] = {}
    per_seed: dict[str, int] = {}
    for _key, row in sorted(prepared, key=lambda item: item[0]):
        seed_id = str(row.get("seed_id") or "")
        if _seed_quota_left(per_seed, seed_id, per_seed_limit):
            _take_graph_row(selected, per_seed, row, seed_id, global_limit)
    return tuple(selected.values())


def _seed_quota_left(per_seed: dict[str, int], seed_id: str, per_seed_limit: int) -> bool:
    if not seed_id:
        return True
    return per_seed.get(seed_id, 0) < per_seed_limit


def _take_graph_row(
    selected: dict[str, dict[str, Any]],
    per_seed: dict[str, int],
    row: Mapping[str, Any],
    seed_id: str,
    global_limit: int,
) -> None:
    if not _accept_graph_row(selected, row, global_limit):
        return
    if seed_id:
        per_seed[seed_id] = per_seed.get(seed_id, 0) + 1


def _prepare_graph_hits(
    rows: Sequence[Mapping[str, Any]],
    *,
    query: str,
    requested_profile: str,
    seeds: Sequence[Mapping[str, Any]],
    directions: Sequence[str],
    edge_types: Sequence[str],
    per_seed_limit: int,
    global_limit: int,
) -> tuple[Mapping[str, Any], ...]:
    """Validate, text-rerank, decay, and cap graph expansions deterministically."""
    prepared: list[tuple[tuple[object, ...], dict[str, Any]]] = []
    for backend_rank, raw in enumerate(rows, start=1):
        item = _prepared_graph_row(
            raw,
            backend_rank,
            query=query,
            direct_graph_query=requested_profile == "GRAPH",
            seed_ids={_candidate_key(seed) for seed in seeds},
            allowed_directions=set(directions),
            allowed_edges=set(edge_types),
        )
        if item is not None:
            prepared.append(item)
    return _select_graph_hits(
        prepared, per_seed_limit=per_seed_limit, global_limit=global_limit
    )


def _normalized_graph_directions(directions: Sequence[str]) -> tuple[str, ...]:
    normalized = tuple(dict.fromkeys(str(item) for item in directions))
    if not normalized or any(item not in {"in", "out"} for item in normalized):
        raise ValueError("graph directions must contain only in or out")
    return normalized


def _normalized_graph_edges(edge_types: Sequence[str]) -> tuple[str, ...]:
    normalized = tuple(sorted(dict.fromkeys(str(item) for item in edge_types)))
    if not normalized or len(normalized) > 64:
        raise ValueError("graph edge types must contain between 1 and 64 values")
    return normalized


def _direction_columns(direction: str) -> tuple[str, str]:
    if direction == "out":
        return "source_node_id", "target_node_id"
    return "target_node_id", "source_node_id"


def _neighbour_rows(
    graph: Any,
    *,
    seed_path: str,
    direction: str,
    edges: tuple[str, ...],
    per_seed_limit: int,
    deadline_monotonic: float | None,
) -> Any:
    """One evidenced typed hop out of the sealed graph, in deterministic order."""
    source_column, target_column = _direction_columns(direction)
    edge_placeholders = ",".join("?" for _ in edges)
    rows = graph._execute(
        f"""
        SELECT a.assertion_id, a.source_node_id, a.target_node_id,
               a.edge_type, a.confidence, a.authority, a.extractor,
               neighbor.node_id, neighbor.kind, neighbor.metadata_json,
               target_occ.byte_start, target_occ.byte_end,
               target_source.relative_path, target_source.sha256,
               target_source.content,
               e.evidence_id, e.source_id AS evidence_source_id,
               e.byte_start AS evidence_byte_start,
               e.byte_end AS evidence_byte_end, e.span_sha256,
               evidence_source.relative_path AS evidence_relative_path
        FROM occurrence seed_occ
        JOIN source seed_source ON seed_source.source_id = seed_occ.source_id
        JOIN assertion a ON a.{source_column} = seed_occ.node_id
        JOIN node neighbor ON neighbor.node_id = a.{target_column}
        JOIN occurrence target_occ ON target_occ.node_id = neighbor.node_id
        JOIN source target_source ON target_source.source_id = target_occ.source_id
        JOIN evidence e ON e.assertion_id = a.assertion_id
        JOIN source evidence_source ON evidence_source.source_id = e.source_id
        WHERE seed_source.relative_path = ?
          AND a.resolution = 'resolved'
          AND a.target_node_id IS NOT NULL
          AND a.edge_type IN ({edge_placeholders})
          AND target_occ.occurrence_id = (
            SELECT min(chosen.occurrence_id) FROM occurrence chosen
            WHERE chosen.node_id = neighbor.node_id
          )
        ORDER BY a.edge_type, neighbor.node_id, a.assertion_id, e.evidence_id
        LIMIT ?
        """,
        (seed_path, *edges),
        max_rows=min(1000, max(32, per_seed_limit * 16)),
        deadline=deadline_monotonic,
    )
    return rows


def _evidence_record(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "evidence_id": str(row["evidence_id"]),
        "source_id": str(row["evidence_source_id"]),
        "relative_path": str(row["evidence_relative_path"]),
        "byte_start": int(row["evidence_byte_start"]),
        "byte_end": int(row["evidence_byte_end"]),
        "span_sha256": str(row["span_sha256"]),
    }


def _decoded_content(value: object) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def _assertion_step_record(row: Mapping[str, Any], direction: str) -> dict[str, Any]:
    return {
        "assertion_id": str(row["assertion_id"]),
        "source_node_id": str(row["source_node_id"]),
        "target_node_id": str(row["target_node_id"]),
        "edge_type": str(row["edge_type"]),
        "direction": direction,
        "confidence": str(row["confidence"]),
        "authority": str(row["authority"]),
        "extractor": str(row["extractor"]),
        "evidence_ids": [],
        "evidence": [],
    }


def _neighbour_item(
    row: Mapping[str, Any], *, seed_id: str, direction: str, graph: Any
) -> dict[str, Any]:
    metadata = json.loads(str(row["metadata_json"]))
    relative_path = str(row["relative_path"])
    return {
        "candidate_id": str(row["node_id"]),
        "parent_id": relative_path,
        "relative_path": relative_path,
        "source_sha256": str(row["sha256"]),
        "byte_start": int(row["byte_start"]),
        "byte_end": int(row["byte_end"]),
        "title": metadata.get("name") or Path(relative_path).stem,
        "content": _decoded_content(row["content"]),
        "seed_id": seed_id,
        "hop": 1,
        "direction": direction,
        "edge_type": str(row["edge_type"]),
        "assertion_path": [_assertion_step_record(row, direction)],
        "evidence_ids": [],
        "generation": getattr(graph, "generation_id", None),
    }


def _freeze_neighbour(item: dict[str, Any]) -> dict[str, Any]:
    step = item["assertion_path"][0]
    step["evidence_ids"] = tuple(step["evidence_ids"])
    step["evidence"] = tuple(step["evidence"])
    item["assertion_path"] = tuple(item["assertion_path"])
    item["evidence_ids"] = tuple(item["evidence_ids"])
    return item


def _group_neighbour_rows(
    rows: Any,
    *,
    seed_id: str,
    direction: str,
    graph: Any,
    deadline_monotonic: float | None,
    cancelled: Callable[[], bool] | None,
) -> list[dict[str, Any]]:
    """One item per (assertion, neighbour), with its evidence collected."""
    grouped: dict[tuple[str, str], dict[str, Any]] = {}
    for row in rows:
        _check_stopped(deadline_monotonic, cancelled)
        key = (str(row["assertion_id"]), str(row["node_id"]))
        item = grouped.get(key)
        if item is None:
            item = _neighbour_item(row, seed_id=seed_id, direction=direction, graph=graph)
            grouped[key] = item
        evidence = _evidence_record(row)
        step = item["assertion_path"][0]
        step["evidence_ids"].append(evidence["evidence_id"])
        step["evidence"].append(evidence)
        item["evidence_ids"].append(evidence["evidence_id"])
    return [_freeze_neighbour(item) for item in grouped.values()]


def _seed_expansion(
    graph: Any,
    seed: Mapping[str, Any],
    *,
    directions: tuple[str, ...],
    edges: tuple[str, ...],
    per_seed_limit: int,
    deadline_monotonic: float | None,
    cancelled: Callable[[], bool] | None,
) -> list[dict[str, Any]]:
    seed_id = _candidate_key(seed)
    seed_path = _hit_path(seed)
    seed_results: list[dict[str, Any]] = []
    for direction in directions:
        _check_stopped(deadline_monotonic, cancelled)
        rows = _neighbour_rows(
            graph,
            seed_path=seed_path,
            direction=direction,
            edges=edges,
            per_seed_limit=per_seed_limit,
            deadline_monotonic=deadline_monotonic,
        )
        seed_results.extend(
            _group_neighbour_rows(
                rows,
                seed_id=seed_id,
                direction=direction,
                graph=graph,
                deadline_monotonic=deadline_monotonic,
                cancelled=cancelled,
            )
        )
    seed_results.sort(
        key=lambda item: (
            str(item["edge_type"]),
            str(item["candidate_id"]),
            str(item["direction"]),
            str(item["assertion_path"][0]["assertion_id"]),
        )
    )
    return seed_results[:per_seed_limit]


def expand_evidence_graph(
    graph: Any,
    *,
    seeds: Sequence[Mapping[str, Any]],
    directions: Sequence[str],
    edge_types: Sequence[str],
    per_seed_limit: int = GRAPH_PER_SEED_LIMIT,
    global_limit: int = GRAPH_GLOBAL_LIMIT,
    deadline_monotonic: float | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> tuple[Mapping[str, Any], ...]:
    """Read one evidenced typed hop from a sealed ``EvidenceGraph`` handle."""
    if not 1 <= per_seed_limit <= 100 or not 1 <= global_limit <= 1000:
        raise ValueError("graph expansion caps are outside supported bounds")
    normalized_directions = _normalized_graph_directions(directions)
    normalized_edges = _normalized_graph_edges(edge_types)
    results: list[Mapping[str, Any]] = []
    for seed in seeds:
        _check_stopped(deadline_monotonic, cancelled)
        results.extend(
            _seed_expansion(
                graph,
                seed,
                directions=normalized_directions,
                edges=normalized_edges,
                per_seed_limit=per_seed_limit,
                deadline_monotonic=deadline_monotonic,
                cancelled=cancelled,
            )
        )
        if len(results) >= global_limit:
            break
    return tuple(results[:global_limit])


def _weigh_by_trust(
    scores: Mapping[str, float],
    meta: dict[str, dict[str, Any]],
    *,
    curated_first: bool,
) -> dict[str, float]:
    """Multiply each fused score by who said it and by what the page is.

    Both factors are recorded on the candidate, separately, so the ordering can
    be explained by name rather than by one opaque number. `curated_first` is
    what the query analysis decided: a code-shaped question turns the
    curated-knowledge prior off, and everything else keeps it.
    """
    weighted: dict[str, float] = {}
    for key, value in scores.items():
        authority = authority_weight(meta[key].get("authority"))
        page = source_type_weight(
            meta[key].get("type"),
            meta[key].get("relative_path"),
            curated_first=curated_first,
        )
        meta[key]["authority_weight"] = authority
        meta[key]["type_weight"] = page
        weighted[key] = value * authority * page
    return weighted


_GRAPH_META_FIELDS = (
    "graph_seed_id",
    "graph_direction",
    "graph_edge_type",
    "assertion_path",
    "graph_text_overlap",
)

# Display fields any backend may fill; the first non-empty value wins.
_MERGEABLE_META_FIELDS = (
    "title",
    "summary",
    "content",
    "project",
    "timestamp",
    "chunk_id",
    "authority",
    "confidence",
    "status",
    "type",
    "valid_from",
    "valid_to",
    "language",
    "source_id",
    "lance_distance",
    *_GRAPH_META_FIELDS,
)


# What a citation carries when the digest of its source is genuinely unknown.
# It satisfies the citation schema's shape, so it has to stay reachable, but it
# identifies nothing and is only ever used when the file cannot be read.
_UNKNOWN_DIGEST = "0" * 64


def _vault_root() -> Path:
    """Where the pages a retrieval row names actually live."""
    configured = os.environ.get("LLM_WIKI_ROOT")
    if configured:
        return Path(configured)
    return Path(__file__).resolve().parent.parent


def _digest_of_page(relative_path: str) -> str | None:
    """The real digest of this page, or None when it cannot be read."""
    if not relative_path:
        return None
    path = _vault_root() / relative_path
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except (OSError, ValueError):
        return None


def _is_real_digest(sha: object) -> bool:
    return isinstance(sha, str) and len(sha) == 64 and sha != _UNKNOWN_DIGEST


def _source_sha256(row: Mapping[str, Any]) -> str:
    """The digest of the row's source.

    The legacy FTS index stores no digest, so a row from it carried sixty-four
    zeros — a value that passes the citation schema while identifying nothing.
    The page is right there, so it is hashed instead, and the placeholder is
    left for the case where the file genuinely cannot be read.
    """
    sha = row.get("source_sha256") or row.get("sha256")
    if _is_real_digest(sha):
        return str(sha)
    return _digest_of_page(_hit_path(row)) or _UNKNOWN_DIGEST


def _evidence_ids_of(row: Mapping[str, Any]) -> tuple[str, ...]:
    return tuple(
        str(item)
        for item in (row.get("evidence_ids") or ())
        if isinstance(item, str) and item
    )


def _graph_meta(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "graph_seed_id": row.get("seed_id"),
        "graph_direction": row.get("direction"),
        "graph_edge_type": row.get("edge_type"),
        "assertion_path": _assertion_path(row),
        "graph_text_overlap": row.get("graph_text_overlap"),
    }


def _new_candidate_meta(key: str, row: Mapping[str, Any]) -> dict[str, Any]:
    """The display and provenance record for a candidate seen for the first time."""
    path = _hit_path(row)
    meta: dict[str, Any] = {
        "candidate_id": key,
        "parent_id": str(_first_present(row, ("parent_id", "parent_page"), path)),
        "relative_path": path,
        "heading_path": _heading_path(
            _first_present(row, ("heading_path", "heading_ancestry"), None)
        ),
        "source_sha256": _source_sha256(row),
        "byte_start": _as_int(row.get("byte_start"), 0),
        "byte_end": _as_int(row.get("byte_end"), 0),
        "bm25_rank": None,
        "bm25_score": None,
        "vector_rank": None,
        "vector_score": None,
        "graph_rank": None,
        "graph_score": None,
        "evidence_ids": _evidence_ids_of(row),
    }
    meta.update(
        {
            field: row.get(field)
            for field in ("title", "summary", "project", "timestamp", "authority",
                          "confidence", "status", "type", "valid_from", "valid_to",
                          "language", "source_id", "lance_distance")
        }
    )
    meta["content"] = row.get("content") or row.get("summary")
    meta["chunk_id"] = row.get("chunk_id") or key
    meta.update(_graph_meta(row))
    return meta


_EMPTY_GRAPH_VALUES = (None, "", ())
_EMPTY_DISPLAY_VALUES = (None, "")


def _fill_blank_fields(
    meta: dict[str, Any],
    values: Mapping[str, Any],
    empty: tuple[Any, ...],
) -> None:
    for field, value in values.items():
        if meta.get(field) in empty and value not in empty:
            meta[field] = value


def _merge_evidence_ids(meta: dict[str, Any], row: Mapping[str, Any]) -> None:
    incoming = _evidence_ids_of(row)
    if incoming:
        meta["evidence_ids"] = tuple(dict.fromkeys((*meta["evidence_ids"], *incoming)))


def _merge_candidate_meta(meta: dict[str, Any], row: Mapping[str, Any]) -> None:
    """Fill blanks from another backend's view of the same candidate."""
    _fill_blank_fields(meta, _graph_meta(row), _EMPTY_GRAPH_VALUES)
    _merge_evidence_ids(meta, row)
    _fill_blank_fields(
        meta,
        {field: row.get(field) for field in _MERGEABLE_META_FIELDS},
        _EMPTY_DISPLAY_VALUES,
    )


# Per-backend fusion contribution: rank field, score field, raw source field.
# The weights are read per call so a caller can still tune them at runtime.
_FUSION_BACKENDS = (
    ("lexical", "bm25_rank", "bm25_score", "bm25_score"),
    ("dense", "vector_rank", "vector_score", "vector_score"),
    ("graph", "graph_rank", "graph_score", "graph_boost"),
)


def _fusion_weights() -> dict[str, float]:
    return {"lexical": BM25_WEIGHT, "dense": DENSE_WEIGHT, "graph": GRAPH_WEIGHT}


def _raw_backend_score(row: Mapping[str, Any], field: str) -> float | None:
    """The backend's own magnitude, kept for display but never fused."""
    if field in row:
        return _as_float(row.get(field))
    return _as_float(row.get("score"))


def _accumulate_backend(
    rows: Sequence[Mapping[str, Any]],
    *,
    weight: float,
    rank_field: str,
    score_field: str,
    raw_field: str,
    k: int,
    scores: dict[str, float],
    meta: dict[str, dict[str, Any]],
) -> None:
    for rank, row in enumerate(rows, start=1):
        key = _ensure_candidate_meta(meta, row)
        scores[key] = scores.get(key, 0.0) + weight / (k + rank)
        meta[key][rank_field] = rank
        meta[key][score_field] = _raw_backend_score(row, raw_field)


def _ensure_candidate_meta(meta: dict[str, dict[str, Any]], row: Mapping[str, Any]) -> str:
    key = _candidate_key(row)
    if key not in meta:
        meta[key] = _new_candidate_meta(key, row)
        return key
    _merge_candidate_meta(meta[key], row)
    return key


def _fused_candidate(
    key: str, info: Mapping[str, Any], rrf: float, final: float
) -> RetrievalCandidate:
    del key
    return RetrievalCandidate(
        candidate_id=info["candidate_id"],
        parent_id=info["parent_id"],
        relative_path=info["relative_path"],
        heading_path=info["heading_path"],
        source_sha256=info["source_sha256"],
        byte_start=info["byte_start"],
        byte_end=info["byte_end"],
        bm25_rank=info["bm25_rank"],
        bm25_score=info["bm25_score"],
        vector_rank=info["vector_rank"],
        vector_score=info["vector_score"],
        graph_rank=info["graph_rank"],
        graph_score=info["graph_score"],
        rrf_score=rrf,
        rerank_score=None,
        final_score=final,
        evidence_ids=info["evidence_ids"],
        authority_weight=info["authority_weight"],
        type_weight=info["type_weight"],
    )


def fuse_rrf(
    *,
    lexical: Sequence[Mapping[str, Any]] | None,
    dense: Sequence[Mapping[str, Any]] | None,
    graph: Sequence[Mapping[str, Any]] | None,
    k: int = RRF_K,
    intents: Sequence[str] | None = None,
) -> tuple[tuple[RetrievalCandidate, ...], dict[str, dict[str, Any]]]:
    """Fuse independent ranked lists with weighted rank-only RRF.

    Larger final_score wins. Raw backend magnitudes are preserved on the
    candidate but never enter the fusion formula. Equal RRF ties break by
    candidate_id ascending.

    `intents` is what the query analysis read. `None` means the caller did not
    analyse the query at all, and then this ranks exactly as it did before the
    trust weight became conditional; an analysed query — including one whose
    analysis found no intent — is answered under the vault's own rule.
    """
    scores: dict[str, float] = {}
    meta: dict[str, dict[str, Any]] = {}
    supplied = {"lexical": lexical, "dense": dense, "graph": graph}
    weights = _fusion_weights()
    for name, rank_field, score_field, raw_field in _FUSION_BACKENDS:
        rows = supplied[name]
        if rows:
            _accumulate_backend(
                rows,
                weight=weights[name],
                rank_field=rank_field,
                score_field=score_field,
                raw_field=raw_field,
                k=k,
                scores=scores,
                meta=meta,
            )
    analysed = intents is not None and curated_pages_first(intents)
    weighted = _weigh_by_trust(scores, meta, curated_first=analysed)
    ordered = sorted(weighted, key=lambda item: (-weighted[item], item))
    candidates = [
        _fused_candidate(key, meta[key], round(scores[key], 6), round(weighted[key], 6))
        for key in ordered
    ]
    return tuple(candidates), meta


def _dense_is_missing(ran_dense: bool, dense_available: bool | None) -> bool:
    if dense_available is False:
        return True
    return dense_available is None and not ran_dense


def _dense_signal(
    wanted: Sequence[str], ran_dense: bool, dense_available: bool | None
) -> tuple[str | None, str | None]:
    """(signal, fallback reason) for the dense backend."""
    if "dense" not in wanted:
        return None, None
    return _dense_backend_signal(ran_dense, dense_available)


def _dense_backend_signal(
    ran_dense: bool, dense_available: bool | None
) -> tuple[str | None, str | None]:
    """What a wanted dense backend reports about itself."""
    if dense_available is True and ran_dense:
        return "dense", None
    if _dense_is_missing(ran_dense, dense_available):
        return None, "dense_unavailable"
    return None, None


def _graph_signal(
    wanted: Sequence[str],
    ran_graph: bool,
    graph_available: bool | None,
    graph_enabled: bool,
) -> tuple[str | None, str | None]:
    """(signal, fallback reason) for the graph backend."""
    if "graph" not in wanted:
        return None, None
    if not graph_enabled:
        return None, "graph_disabled"
    return _graph_backend_signal(ran_graph, graph_available)


def _graph_backend_signal(
    ran_graph: bool, graph_available: bool | None
) -> tuple[str | None, str | None]:
    """What a wanted, enabled graph backend reports about itself."""
    if graph_available is True and ran_graph:
        return "graph", None
    return None, "graph_unavailable"


def _effective_for_hybrid(signals: set[str], requested: str) -> str:
    if "dense" in signals:
        return "HYBRID"
    return _lexical_or_requested(signals, requested)


def _effective_for_graph(signals: set[str], requested: str) -> str:
    if "graph" in signals:
        return "GRAPH"
    return _lexical_or_requested(signals, requested)


def _effective_for_global(signals: set[str], requested: str) -> str:
    if "dense" in signals and "graph" in signals:
        return "GLOBAL"
    if "dense" in signals:
        return "HYBRID"
    return _effective_for_graph(signals, requested)


def _effective_for_graph_profile(signals: set[str], requested: str) -> str:
    """REPO_MAP and IMPACT keep their name only while the graph answered."""
    if "graph" in signals:
        return requested
    return _lexical_or_requested(signals, requested)


def _lexical_or_requested(signals: set[str], requested: str) -> str:
    if "lexical" in signals:
        return "BASE"
    return requested


def _effective_mode(requested: str, signals: set[str]) -> str:
    resolvers = {
        "HYBRID": _effective_for_hybrid,
        "GRAPH": _effective_for_graph,
        "GLOBAL": _effective_for_global,
        "REPO_MAP": _effective_for_graph_profile,
        "IMPACT": _effective_for_graph_profile,
    }
    resolver = resolvers.get(requested)
    if resolver is not None:
        return resolver(signals, requested)
    if signals:
        return requested
    return "BASE"


def _resolve_effective_mode(
    requested: str,
    *,
    wanted: Sequence[str],
    ran_lexical: bool,
    ran_dense: bool,
    ran_graph: bool,
    dense_available: bool | None,
    graph_available: bool | None,
    graph_enabled: bool,
) -> tuple[str, str | None, tuple[str, ...]]:
    """Compute truthful effective mode, signals, and a single fallback reason."""
    dense_signal, fallback = _dense_signal(wanted, ran_dense, dense_available)
    graph_signal, graph_fallback = _graph_signal(
        wanted, ran_graph, graph_available, graph_enabled
    )
    signals = _collected_signals(
        _lexical_signal(wanted, ran_lexical), dense_signal, graph_signal, ran_lexical
    )
    return (
        _effective_mode(requested, set(signals)),
        fallback or graph_fallback,
        tuple(signals),
    )


def _collected_signals(
    lexical: list[str], dense: str | None, graph: str | None, ran_lexical: bool
) -> list[str]:
    """Signals that actually answered; a lexical-only run still says so."""
    signals = [*lexical, *(item for item in (dense, graph) if item is not None)]
    if not signals and ran_lexical:
        return ["lexical"]
    return signals


def _lexical_signal(wanted: Sequence[str], ran_lexical: bool) -> list[str]:
    if "lexical" in wanted and ran_lexical:
        return ["lexical"]
    return []


class RetrievalStopped(TimeoutError):
    """The caller's own deadline or cancel flag stopped the plan.

    Deliberately distinct from a `TimeoutError` a backend raises. A backend
    timeout says the work failed; this one says *we* stopped the work, and
    whatever earlier legs finished is still in hand. Only this class is
    salvageable, so a backend that times out keeps propagating exactly as it
    did before. It stays a `TimeoutError` so every existing caller and test
    that catches or matches one is unaffected, message included.
    """

    def __init__(self, message: str, reason: str) -> None:
        super().__init__(message)
        self.reason = reason


def _check_deadline(deadline_monotonic: float | None) -> None:
    if deadline_monotonic is None:
        return
    import time

    if time.monotonic() >= float(deadline_monotonic):
        raise RetrievalStopped(
            "retrieval deadline exceeded", "deadline_expired_partial_result"
        )


def _check_stopped(
    deadline_monotonic: float | None, cancelled: Callable[[], bool] | None
) -> None:
    _check_deadline(deadline_monotonic)
    if cancelled is not None and cancelled():
        raise RetrievalStopped("retrieval cancelled", "cancelled_partial_result")


# An optional stage may spend this share of what is left of the operation
# budget, never all of it. Spending all of it is how an optional signal turns
# into a failed answer: the mandatory legs and the caller's own fallback are then
# left with nothing. Measured on this vault: a cold embedding model load takes
# about ten seconds against a ten-second MCP budget, and the lexical answer that
# was ready in 1.3 s was lost with it.
OPTIONAL_STAGE_BUDGET_SHARE = 0.5

# The mandatory tail that must survive every optional stage: fusion, ranking,
# rendering the rows, the closing seal re-check, and the caller's own metadata.
# No optional stage may run past `deadline - this`.
#
# The share alone did not protect the tail. A share is taken from what is
# *left*, so two optional stages in sequence take half, then half of the rest --
# three quarters of the remaining budget -- and neither of them owes the tail
# anything. Measured on this vault under load: the dense and rerank stages
# together spent 4.4-5.5 s of a 10 s operation and returned nothing, the
# deadline then fell during the mandatory tail, and the lexical answer that was
# already computed was discarded. 18 of 36 calls across three runs raised
# instead of answering; not one of them returned a degraded answer.
#
# The reserve covers two measured things, not one.
#
# The tail itself: 0.59-0.86 s over eight instrumented calls, load average 5-16.
#
# And the wait's own overshoot. `_await_optional_stage` polls a `threading.Event`
# every 10 ms, which lands within 3 ms of its deadline in isolation and within
# 73 ms against busy pure-Python siblings -- but a stage that is loading a model
# holds the GIL in long native-adjacent stretches, and the waiter cannot check
# its clock until it gets the interpreter back. Instrumented on the real path:
# a stage granted 3.546 s returned after 4.445 s, 0.9 s late. Nothing in the
# waiter can prevent that, so the reserve absorbs it.
#
# 2.5 s is the measured worst tail (0.86 s) plus the measured worst overshoot
# (0.9 s) with margin. A caller with a generous budget is unaffected: the share
# and `OPTIONAL_STAGE_MAX_SECONDS` bind long before the reserve does.
#
# This is the reserve-for-your-own-response-path rule that deadline propagation
# has always carried: what you hand downstream must not be the whole of what you
# have left. See `docs/research/2026-08-29-what-an-optional-stage-may-spend.md`.
OPTIONAL_STAGE_TAIL_RESERVE_SECONDS = 2.5


def _optional_stage_deadline(deadline: float) -> float:
    """The slice an optional stage may use before it is abandoned."""
    now = time.monotonic()
    remaining = deadline - now
    if remaining <= 0:
        return deadline
    share = now + remaining * OPTIONAL_STAGE_BUDGET_SHARE
    return min(share, deadline - OPTIONAL_STAGE_TAIL_RESERVE_SECONDS)


def _call_dense(
    dense_backend: BackendFn,
    filters: Mapping[str, Any],
    *,
    deadline_monotonic: float | None,
    cancelled: Callable[[], bool] | None,
) -> Sequence[Mapping[str, Any]] | None:
    if deadline_monotonic is None:
        return dense_backend(**filters)
    return _run_optional_bounded(
        lambda: dense_backend(**filters),
        deadline=_optional_stage_deadline(deadline_monotonic),
        cancelled=cancelled,
        kind="dense",
    )


def _run_dense_backend(
    dense_backend: BackendFn,
    filters: Mapping[str, Any],
    *,
    deadline_monotonic: float | None,
    cancelled: Callable[[], bool] | None,
) -> tuple[Sequence[Mapping[str, Any]] | None, bool, bool | None, bool]:
    """(hits, ran, available, timed out).

    None hits mean the backend is unavailable; an empty sequence means it
    answered and found nothing.
    """
    try:
        hits = _call_dense(
            dense_backend,
            filters,
            deadline_monotonic=deadline_monotonic,
            cancelled=cancelled,
        )
    except OptionalStageTimeout:
        return None, False, False, True
    return hits, True, hits is not None, False


def _profile_edge_types(
    requested: str, graph_edge_families: Mapping[str, bool] | None
) -> tuple[str, ...]:
    """Edge types this profile reads, minus any family the caller switched off."""
    edge_types = GRAPH_PROFILE_EDGE_TYPES.get(requested, tuple(GRAPH_EDGE_DECAY))
    if not graph_edge_families:
        return tuple(edge_types)
    return tuple(
        edge_type
        for edge_type in edge_types
        if graph_edge_families.get(edge_type, True)
    )


def _run_graph_backend(
    graph_backend: BackendFn,
    filters: Mapping[str, Any],
    *,
    analysis: QueryAnalysis,
    requested: str,
    lexical_hits: Sequence[Mapping[str, Any]] | None,
    dense_hits: Sequence[Mapping[str, Any]] | None,
    graph_edge_families: Mapping[str, bool] | None,
    per_seed_limit: int,
    global_limit: int,
    corpus_generation: str,
    deadline_monotonic: float | None,
) -> tuple[Sequence[Mapping[str, Any]] | None, bool, bool | None, str | None]:
    """(hits, ran, available, failure reason).

    A deadline or a changed generation seal is the caller's problem and is
    re-raised; any other backend error degrades the graph signal instead of
    failing the whole retrieval.
    """
    directions = GRAPH_PROFILE_DIRECTIONS.get(requested, ("out",))
    edge_types = _profile_edge_types(requested, graph_edge_families)
    seeds = _graph_seeds(lexical_hits, dense_hits)
    try:
        raw_hits = graph_backend(
            **filters,
            seeds=seeds,
            max_hops=GRAPH_MAX_HOPS,
            directions=directions,
            edge_types=edge_types,
            edge_decay={edge: GRAPH_EDGE_DECAY[edge] for edge in edge_types},
            per_seed_limit=per_seed_limit,
            global_limit=global_limit,
            deadline_monotonic=deadline_monotonic,
            corpus_generation=corpus_generation,
        )
    except (TimeoutError, GenerationSealChanged):
        raise
    except Exception:  # noqa: BLE001 - a broken graph degrades one signal only
        return None, False, False, "graph_error"
    return _prepared_graph_outcome(
        raw_hits,
        analysis=analysis,
        requested=requested,
        seeds=seeds,
        directions=directions,
        edge_types=edge_types,
        per_seed_limit=per_seed_limit,
        global_limit=global_limit,
    )


def _prepared_graph_outcome(
    raw_hits: Sequence[Mapping[str, Any]] | None,
    *,
    analysis: QueryAnalysis,
    requested: str,
    seeds: Sequence[Mapping[str, Any]],
    directions: Sequence[str],
    edge_types: Sequence[str],
    per_seed_limit: int,
    global_limit: int,
) -> tuple[Sequence[Mapping[str, Any]] | None, bool, bool | None, str | None]:
    if raw_hits is None:
        return None, True, False, None
    hits = _prepare_graph_hits(
        raw_hits,
        query=analysis.normalized_query or analysis.query,
        requested_profile=requested,
        seeds=seeds,
        directions=directions,
        edge_types=edge_types,
        per_seed_limit=per_seed_limit,
        global_limit=global_limit,
    )
    return hits, True, True, None


@dataclass
class _RerankTrace:
    """What the reranking stage did, for the truthful retrieval trace."""

    applied: bool = False
    model_id: object = None
    model_revision: object = None
    depth: object = None
    duration_ms: object = None
    fallback_reason: str | None = None
    optional_timeout: bool = False


def _as_optional_str(value: object) -> str | None:
    if not value:
        return None
    return str(value)


def _as_optional_int(value: object) -> int | None:
    if not isinstance(value, int) or isinstance(value, bool):
        return None
    return value


def _rerank_row(
    candidate: RetrievalCandidate, info: Mapping[str, Any]
) -> dict[str, Any]:
    summary = str(info.get("summary") or "")
    return {
        "candidate_id": candidate.candidate_id,
        "path": candidate.relative_path,
        "relative_path": candidate.relative_path,
        "summary": summary,
        "content": str(info.get("content") or summary or ""),
        "title": str(info.get("title") or Path(candidate.relative_path).stem),
        "rrf_score": candidate.rrf_score,
        "score": candidate.rrf_score,
        "bm25_rank": candidate.bm25_rank,
        "vector_rank": candidate.vector_rank,
        "bm25_score": candidate.bm25_score,
        "vector_score": candidate.vector_score,
        "graph_rank": candidate.graph_rank,
        "graph_score": candidate.graph_score,
        "source_sha256": candidate.source_sha256,
        "heading_path": candidate.heading_path,
        "parent_id": candidate.parent_id,
        "byte_start": candidate.byte_start,
        "byte_end": candidate.byte_end,
        "evidence_ids": candidate.evidence_ids,
        "lance_distance": info.get("lance_distance"),
        "authority": info.get("authority"),
        "authority_weight": candidate.authority_weight,
        "type_weight": candidate.type_weight,
    }


def _rerank_rows(
    candidates: Sequence[RetrievalCandidate],
    display_meta: Mapping[str, Mapping[str, Any]],
    query_norm: str,
) -> tuple[list[dict[str, Any]], bool]:
    """Rows the reranker reads, and whether one title matches the query exactly."""
    rows = [
        _rerank_row(candidate, display_meta.get(candidate.candidate_id, {}))
        for candidate in candidates
    ]
    exact_title_hit = any(
        str(row["title"]).casefold().strip() == query_norm for row in rows
    )
    return rows, exact_title_hit


def _promote_exact_title(
    candidates: Sequence[RetrievalCandidate],
    rows: list[dict[str, Any]],
    query_norm: str,
) -> tuple[RetrievalCandidate, ...]:
    """A title equal to the query goes first, before any reranking."""
    rows.sort(key=lambda row: _exact_title_order(row, query_norm))
    by_id = {candidate.candidate_id: candidate for candidate in candidates}
    promoted = [
        by_id[str(row["candidate_id"])]
        for row in rows
        if str(row["candidate_id"]) in by_id
    ]
    return tuple(promoted) or tuple(candidates)


def _exact_title_order(row: Mapping[str, Any], query_norm: str) -> tuple[Any, ...]:
    exact = str(row.get("title", "")).casefold().strip() == query_norm
    return (0 if exact else 1, -float(row.get("rrf_score") or 0.0), str(row.get("candidate_id") or ""))


def _candidate_from_rerank_row(row: Mapping[str, Any]) -> RetrievalCandidate:
    path = str(_first_present(row, ("relative_path", "path"), ""))
    return RetrievalCandidate(
        candidate_id=str(_first_present(row, ("candidate_id", "path"), "")),
        parent_id=str(_first_present(row, ("parent_id", "path"), "")),
        relative_path=path,
        heading_path=_heading_path(row.get("heading_path")),
        source_sha256=_source_sha256(row),
        byte_start=_as_int(row.get("byte_start"), 0),
        byte_end=_as_int(row.get("byte_end"), 0),
        bm25_rank=_as_optional_int(row.get("bm25_rank")),
        bm25_score=_as_float(row.get("bm25_score")),
        vector_rank=_as_optional_int(row.get("vector_rank")),
        vector_score=_as_float(row.get("vector_score")),
        graph_rank=_as_optional_int(row.get("graph_rank")),
        graph_score=_as_float(row.get("graph_score")),
        rrf_score=float(_first_present(row, ("rrf_score",), 0.0)),
        rerank_score=_as_float(row.get("rerank_score")),
        authority_weight=float(_first_present(row, ("authority_weight",), 1.0)),
        type_weight=float(_first_present(row, ("type_weight",), 1.0)),
        final_score=float(_first_present(row, ("final_score", "rrf_score"), 0.0)),
        evidence_ids=_evidence_ids_of(row),
    )


def _rerank_pool_limit(limit: int, max_candidates: int | None) -> int:
    pool_limit = max(limit, 20) if limit > 0 else 20
    if max_candidates is not None and int(max_candidates) > 0:
        return min(pool_limit, int(max_candidates))
    return pool_limit


def _record_reranked(
    reranked: Sequence[Mapping[str, Any]], trace: _RerankTrace
) -> None:
    head = reranked[0]
    trace.model_id = head.get("reranker_model_id")
    trace.model_revision = head.get("reranker_model_revision")
    trace.depth = head.get("reranker_depth")
    trace.duration_ms = head.get("reranker_duration_ms")
    if head.get("reranker_applied"):
        trace.applied = True
        trace.fallback_reason = None
        return
    trace.fallback_reason = str(
        head.get("reranker_fallback_reason") or "reranker_unavailable"
    )


def _run_reranker(
    rows: list[dict[str, Any]],
    *,
    query: str,
    pool_limit: int,
    deadline_monotonic: float | None,
    cancelled: Callable[[], bool] | None,
) -> Sequence[Mapping[str, Any]]:
    from reranker import rerank as _rerank

    def call() -> Sequence[Mapping[str, Any]]:
        return _rerank(query, rows[:pool_limit], limit=pool_limit, text_field="content")

    if deadline_monotonic is None:
        return call()
    return _run_optional_bounded(
        call,
        deadline=_optional_stage_deadline(deadline_monotonic),
        cancelled=cancelled,
        kind="rerank",
    )


def _reranked_candidates(
    candidates: Sequence[RetrievalCandidate],
    rows: list[dict[str, Any]],
    *,
    analysis: QueryAnalysis,
    requested: str,
    limit: int,
    max_candidates: int | None,
    rerank_enabled: bool,
    deadline_monotonic: float | None,
    cancelled: Callable[[], bool] | None,
    trace: _RerankTrace,
) -> tuple[RetrievalCandidate, ...]:
    from reranker import should_rerank

    apply, skip_reason = should_rerank(
        profile=requested,
        candidates=rows,
        analysis_intents=analysis.intents,
        rerank_enabled=rerank_enabled,
    )
    if not apply:
        trace.fallback_reason = skip_reason
        return tuple(candidates)
    reranked = _run_reranker(
        rows,
        query=analysis.normalized_query or analysis.query,
        pool_limit=_rerank_pool_limit(limit, max_candidates),
        deadline_monotonic=deadline_monotonic,
        cancelled=cancelled,
    )
    _check_stopped(deadline_monotonic, cancelled)
    return _candidates_after_rerank(candidates, reranked, trace)


def _candidates_after_rerank(
    candidates: Sequence[RetrievalCandidate],
    reranked: Sequence[Mapping[str, Any]],
    trace: _RerankTrace,
) -> tuple[RetrievalCandidate, ...]:
    """The reranked order, then the pool the reranker never saw."""
    if not reranked:
        return tuple(candidates)
    _record_reranked(reranked, trace)
    if not trace.applied:
        return tuple(candidates)
    scored = tuple(_candidate_from_rerank_row(row) for row in reranked)
    return scored + _below_rerank_pool(candidates, scored)


def _below_rerank_pool(
    candidates: Sequence[RetrievalCandidate], scored: Sequence[RetrievalCandidate]
) -> tuple[RetrievalCandidate, ...]:
    """Candidates below the reranker's bounded pool, in their fused order.

    The reranker reads a bounded prefix, so dropping the rest here decides the
    answer before the page-diverse order runs: measured on this vault, a query
    reached that order with twenty candidates drawn from four pages, and the
    last two visible slots could only repeat a page. Keeping the tail costs
    nothing — it stays behind every reranked row, and the final cut is the same
    one it always was.
    """
    scored_ids = {item.candidate_id for item in scored}
    return tuple(item for item in candidates if item.candidate_id not in scored_ids)


def _apply_reranking(
    candidates: Sequence[RetrievalCandidate],
    display_meta: Mapping[str, Mapping[str, Any]],
    *,
    analysis: QueryAnalysis,
    requested: str,
    limit: int,
    max_candidates: int | None,
    rerank_enabled: bool,
    deadline_monotonic: float | None,
    cancelled: Callable[[], bool] | None,
    trace: _RerankTrace,
) -> tuple[RetrievalCandidate, ...]:
    """Reorder by cross-encoder when it is worth it; degrade without failing."""
    query_norm = (analysis.normalized_query or analysis.query).casefold().strip()
    try:
        return _rerank_or_promote(
            candidates,
            display_meta,
            query_norm,
            analysis=analysis,
            requested=requested,
            limit=limit,
            max_candidates=max_candidates,
            rerank_enabled=rerank_enabled,
            deadline_monotonic=deadline_monotonic,
            cancelled=cancelled,
            trace=trace,
        )
    except OptionalStageTimeout:
        trace.fallback_reason = "optional_stage_timeout"
        trace.optional_timeout = True
    except TimeoutError:
        raise
    except Exception:  # noqa: BLE001 - a failed reranker keeps the fused order
        trace.fallback_reason = "reranker_error"
    return tuple(candidates)


def _rerank_or_promote(
    candidates: Sequence[RetrievalCandidate],
    display_meta: Mapping[str, Mapping[str, Any]],
    query_norm: str,
    *,
    analysis: QueryAnalysis,
    requested: str,
    limit: int,
    max_candidates: int | None,
    rerank_enabled: bool,
    deadline_monotonic: float | None,
    cancelled: Callable[[], bool] | None,
    trace: _RerankTrace,
) -> tuple[RetrievalCandidate, ...]:
    rows, exact_title_hit = _rerank_rows(candidates, display_meta, query_norm)
    if exact_title_hit:
        trace.fallback_reason = "exact_title_bypass"
        return _promote_exact_title(candidates, rows, query_norm)
    return _reranked_candidates(
            candidates,
            rows,
            analysis=analysis,
            requested=requested,
            limit=limit,
            max_candidates=max_candidates,
            rerank_enabled=rerank_enabled,
            deadline_monotonic=deadline_monotonic,
            cancelled=cancelled,
            trace=trace,
        )


def _wanted_backend(backend: BackendFn | None, wanted: bool) -> BackendFn | None:
    """Hand `retrieve` only the backends this profile and these switches allow."""
    if wanted:
        return backend
    return None


def _fusion_input(
    hits: Sequence[Mapping[str, Any]] | None,
    signal: str,
    signals: Sequence[str],
) -> Sequence[Mapping[str, Any]] | None:
    """Only a backend that actually answered contributes a list to the fusion."""
    if signal not in signals:
        return None
    return hits


def _require_known_edge_families(families: Mapping[str, bool] | None) -> None:
    if families is None:
        return
    if not _edge_families_are_known(families):
        raise ValueError("graph_edge_families must map known edge types to booleans")


def _edge_families_are_known(families: object) -> bool:
    """A mapping of known edge types to booleans, and nothing else."""
    if not isinstance(families, Mapping):
        return False
    return not any(
        _is_unknown_edge_family(edge, enabled) for edge, enabled in families.items()
    )


def _is_unknown_edge_family(edge: object, enabled: object) -> bool:
    return edge not in GRAPH_EDGE_DECAY or not isinstance(enabled, bool)


@dataclass
class _BackendRun:
    """What each backend produced, and what that means for the trace."""

    lexical_hits: Sequence[Mapping[str, Any]] | None = None
    dense_hits: Sequence[Mapping[str, Any]] | None = None
    graph_hits: Sequence[Mapping[str, Any]] | None = None
    ran_lexical: bool = False
    ran_dense: bool = False
    ran_graph: bool = False
    dense_available: bool | None = None
    graph_available: bool | None = None
    graph_failure: str | None = None
    optional_failure: str | None = None
    partial: bool = False


def _run_lexical_stage(
    run: _BackendRun,
    lexical_backend: BackendFn | None,
    filters: Mapping[str, Any],
    *,
    wanted: Sequence[str],
    deadline_monotonic: float | None,
    cancelled: Callable[[], bool] | None,
) -> None:
    if lexical_backend is None or "lexical" not in wanted:
        return
    _check_stopped(deadline_monotonic, cancelled)
    run.lexical_hits = lexical_backend(**filters) or ()
    run.ran_lexical = True
    _check_stopped(deadline_monotonic, cancelled)


def _run_dense_stage(
    run: _BackendRun,
    dense_backend: BackendFn | None,
    filters: Mapping[str, Any],
    *,
    wanted: Sequence[str],
    deadline_monotonic: float | None,
    cancelled: Callable[[], bool] | None,
) -> None:
    if dense_backend is None or "dense" not in wanted:
        return
    _check_stopped(deadline_monotonic, cancelled)
    run.dense_hits, run.ran_dense, run.dense_available, timed_out = _run_dense_backend(
        dense_backend,
        filters,
        deadline_monotonic=deadline_monotonic,
        cancelled=cancelled,
    )
    if timed_out:
        run.optional_failure = "optional_stage_timeout"
        run.partial = True
    _check_stopped(deadline_monotonic, cancelled)


def _graph_stage_runnable(
    run: _BackendRun, graph_enabled: bool, graph_backend: BackendFn | None
) -> bool:
    """A disabled graph records its unavailability; a missing backend is silent."""
    if not graph_enabled:
        run.graph_available = False
        return False
    return graph_backend is not None


def _run_graph_stage(
    run: _BackendRun,
    graph_backend: BackendFn | None,
    filters: Mapping[str, Any],
    *,
    analysis: QueryAnalysis,
    requested: str,
    wanted: Sequence[str],
    graph_enabled: bool,
    graph_edge_families: Mapping[str, bool] | None,
    graph_per_seed_limit: int,
    graph_global_limit: int,
    corpus_generation: str,
    deadline_monotonic: float | None,
    cancelled: Callable[[], bool] | None,
) -> None:
    if "graph" not in wanted:
        return
    if not _graph_stage_runnable(run, graph_enabled, graph_backend):
        return
    _check_stopped(deadline_monotonic, cancelled)
    (
        run.graph_hits,
        run.ran_graph,
        run.graph_available,
        run.graph_failure,
    ) = _run_graph_backend(
        graph_backend,
        filters,
        analysis=analysis,
        requested=requested,
        lexical_hits=run.lexical_hits,
        dense_hits=run.dense_hits,
        graph_edge_families=graph_edge_families,
        per_seed_limit=graph_per_seed_limit,
        global_limit=graph_global_limit,
        corpus_generation=corpus_generation,
        deadline_monotonic=deadline_monotonic,
    )
    _check_stopped(deadline_monotonic, cancelled)


def _run_backends(
    run: _BackendRun,
    filters: Mapping[str, Any],
    *,
    analysis: QueryAnalysis,
    requested: str,
    wanted: Sequence[str],
    lexical_backend: BackendFn | None,
    dense_backend: BackendFn | None,
    graph_backend: BackendFn | None,
    graph_enabled: bool,
    graph_edge_families: Mapping[str, bool] | None,
    graph_per_seed_limit: int,
    graph_global_limit: int,
    corpus_generation: str,
    deadline_monotonic: float | None,
    cancelled: Callable[[], bool] | None,
) -> _BackendRun:
    """Ask each wanted backend in turn; the graph one reads the earlier hits."""
    _run_lexical_stage(
        run,
        lexical_backend,
        filters,
        wanted=wanted,
        deadline_monotonic=deadline_monotonic,
        cancelled=cancelled,
    )
    _run_dense_stage(
        run,
        dense_backend,
        filters,
        wanted=wanted,
        deadline_monotonic=deadline_monotonic,
        cancelled=cancelled,
    )
    _run_graph_stage(
        run,
        graph_backend,
        filters,
        analysis=analysis,
        requested=requested,
        wanted=wanted,
        graph_enabled=graph_enabled,
        graph_edge_families=graph_edge_families,
        graph_per_seed_limit=graph_per_seed_limit,
        graph_global_limit=graph_global_limit,
        corpus_generation=corpus_generation,
        deadline_monotonic=deadline_monotonic,
        cancelled=cancelled,
    )
    return run


def _first_fallback_reason(
    rows: Sequence[Mapping[str, Any]], current: str | None
) -> str | None:
    for row in rows:
        reason = row.get("fallback_reason")
        if reason:
            return str(reason)
    return current


def _filtered_hits(
    rows: Sequence[Mapping[str, Any]], filters: Mapping[str, Any]
) -> Sequence[Mapping[str, Any]]:
    """Backend rows as candidate hits, with the caller's hard filters applied."""
    import search_memory

    hits = [_backend_hit_from_legacy(row) for row in rows]
    return search_memory.apply_hard_filters(
        hits,
        project=filters.get("project"),
        since=filters.get("since"),
        as_of=filters.get("as_of"),
        scope=filters.get("scope", "all"),
    )


def _require_unchanged_generation(
    catalog: Any, context: Mapping[str, Any], stop: Mapping[str, Any]
) -> None:
    """The seal must hold before the read and still hold after it."""
    import search_memory

    if not search_memory._generation_consumption_unchanged(
        catalog,
        context["manifest"],
        context["artifact_names"],
        context["seal"],
        **stop,
    ):
        raise GenerationSealChanged


_GENERATION_DENSE_BLOCKING_FALLBACKS = frozenset(
    {"generation_seal_changed", "generation_corrupt", "generation_unavailable"}
)


def _generation_seal_holds(
    catalog: Any, context: Mapping[str, Any], stop: Mapping[str, Any]
) -> bool:
    import search_memory

    return bool(
        search_memory._generation_consumption_unchanged(
            catalog,
            context["manifest"],
            context["artifact_names"],
            context["seal"],
            **stop,
        )
    )


def _generation_dense_unusable(
    generation_fallback: str | None,
    *,
    embedder: object,
    model_id: object,
    model_revision: object,
) -> str | None:
    """Why the generation's vectors cannot be read, or None when they can."""
    if generation_fallback in _GENERATION_DENSE_BLOCKING_FALLBACKS:
        return generation_fallback
    if embedder is None or model_id is None or model_revision is None:
        return "generation_vectors_unavailable"
    return None


def _dense_filtered_hits(
    rows: Sequence[Mapping[str, Any]], filters: Mapping[str, Any]
) -> Sequence[Mapping[str, Any]]:
    """Vector rows carry their distance in `score`; fusion reads vector_score."""
    return _filtered_hits(
        [{**row, "vector_score": row.get("score")} for row in rows], filters
    )


def _require_seal(catalog: Any, context: Mapping[str, Any], stop: Mapping[str, Any]) -> None:
    if not _generation_seal_holds(catalog, context, stop):
        raise GenerationSealChanged


def _generation_graph_hits(
    active_graph: Any,
    filters: Mapping[str, Any],
    *,
    catalog: Any,
    context: Mapping[str, Any],
    stop: Mapping[str, Any],
    cancelled: Callable[[], bool] | None,
) -> Sequence[Mapping[str, Any]]:
    """One hop out of the active generation, sealed before and after the read."""
    _require_seal(catalog, context, stop)
    rows = expand_evidence_graph(
        active_graph,
        seeds=filters["seeds"],
        directions=filters["directions"],
        edge_types=filters["edge_types"],
        per_seed_limit=filters["per_seed_limit"],
        global_limit=filters["global_limit"],
        deadline_monotonic=filters["deadline_monotonic"],
        cancelled=cancelled,
    )
    _require_seal(catalog, context, stop)
    return rows


def _neighbour_boost_hits(
    lexical_backend: BackendFn, filters: Mapping[str, Any]
) -> Sequence[Mapping[str, Any]]:
    """Legacy path: neighbours of the lexical hits, scored by their boost."""
    from graph_neighbors import boost_graph_neighbors

    seed_filters = {
        key: filters[key]
        for key in ("query", "scope", "limit", "project", "since", "as_of")
    }
    seeds = list(lexical_backend(**seed_filters))
    boosts = boost_graph_neighbors(
        [{"path": hit["path"], "score": hit.get("score", 0)} for hit in seeds], None
    )
    return [
        _backend_hit_from_legacy(
            {
                "path": item["path"],
                "candidate_id": item["path"],
                "score": item.get("graph_boost", 0.0),
                "graph_boost": item.get("graph_boost", 0.0),
            }
        )
        for item in boosts
    ]


def _drop_generation_connection(context: dict[str, Any], connection: Any) -> None:
    connection.close()
    context["connection"] = None


def _generation_connection_for(
    search_memory: Any,
    catalog: Any,
    manifest: Mapping[str, Any],
    seal: object,
    stop: Mapping[str, Any],
) -> Any:
    """No seal means no readable generation, so there is nothing to open."""
    if seal is None:
        return None
    return search_memory._generation_connection(catalog, manifest, **stop)


def _close_generation_handles(context: Mapping[str, Any]) -> None:
    """Close the graph and the database of the generation this call opened."""
    for key in ("graph", "connection"):
        handle = context.get(key)
        if handle is None:
            continue
        try:
            handle.close()
        except Exception:  # noqa: BLE001 - a close failure must not mask the result
            pass


_DENSE_REPLACEABLE_REASONS = frozenset({None, "dense_unavailable"})


def _reported_fallback(
    trace_reason: str | None,
    *,
    dense_fallback: str | None,
    generation_fallback: str | None,
    legacy_fallback: str | None,
) -> str | None:
    """One reason, in the order the operator needs to hear it."""
    if legacy_fallback:
        return legacy_fallback
    if _dense_reason_wins(trace_reason, dense_fallback):
        return str(dense_fallback)
    return _generation_or_trace_reason(trace_reason, generation_fallback)


def _generation_or_trace_reason(
    trace_reason: str | None, generation_fallback: str | None
) -> str | None:
    """A generation reason speaks only while the trace itself is silent."""
    if generation_fallback and trace_reason is None:
        return generation_fallback
    return trace_reason


def _dense_reason_wins(trace_reason: str | None, dense_fallback: str | None) -> bool:
    if not dense_fallback:
        return False
    return trace_reason in _DENSE_REPLACEABLE_REASONS


def _is_exact_filename_answer(result: RetrievalResult, query: str) -> bool:
    """An exact filename stays the answer after fusion and reranking."""
    if not result.candidates:
        return False
    first = _normalized_filename_stem(result.candidates[0].relative_path)
    return first == _normalized_filename_stem(query)


def _reported_mode(result: RetrievalResult, query: str) -> str:
    if _is_exact_filename_answer(result, query):
        return "EXACT"
    return result.trace.effective_mode


def _trace_unchanged(
    trace: RetrievalTrace, effective_mode: str, fallback_reason: str | None, partial: bool
) -> bool:
    return (
        effective_mode == trace.effective_mode
        and fallback_reason == trace.fallback_reason
        and partial == trace.partial
    )


def _with_reported_trace(
    result: RetrievalResult,
    *,
    query: str,
    dense_fallback: str | None,
    generation_fallback: str | None,
    legacy_fallback: str | None,
) -> RetrievalResult:
    trace = result.trace
    effective_mode = _reported_mode(result, query)
    fallback_reason = _reported_fallback(
        trace.fallback_reason,
        dense_fallback=dense_fallback,
        generation_fallback=generation_fallback,
        legacy_fallback=legacy_fallback,
    )
    partial = trace.partial or legacy_fallback is not None
    if _trace_unchanged(trace, effective_mode, fallback_reason, partial):
        return result
    return RetrievalResult(
        candidates=result.candidates,
        trace=replace(
            trace,
            effective_mode=effective_mode,
            fallback_reason=fallback_reason,
            partial=partial,
        ),
        analysis=result.analysis,
        display_meta=result.display_meta,
    )


def _impression_candidate_id(item: Mapping[str, Any]) -> str:
    identity = _first_present(item, ("chunk_id", "slug", "candidate_id"), None)
    if identity is not None:
        return str(identity)
    return Path(str(item.get("path", ""))).stem


def _record_impressions(
    rows: Sequence[Mapping[str, Any]],
    *,
    query: str,
    corpus_generation: str,
    source_tool: str,
) -> None:
    """Best-effort telemetry: never let it affect the answer."""
    if not rows:
        return
    try:
        _emit_impressions(
            rows, query=query, corpus_generation=corpus_generation, source_tool=source_tool
        )
    except Exception:  # noqa: BLE001 - telemetry is never load-bearing
        pass


def _impression_event(
    item: Mapping[str, Any],
    rank: int,
    *,
    query: str,
    corpus_generation: str,
    source_tool: str,
):
    from retrieval_telemetry import best_effort_make_event

    return best_effort_make_event(
        event_kind="impression",
        query=query,
        retrieval_mode=str(item.get("effective_mode") or "base").lower(),
        candidate_id=_impression_candidate_id(item),
        rank=rank,
        generation=str(item.get("generation") or corpus_generation),
        source_tool=source_tool,
    )


def _emit_impressions(
    rows: Sequence[Mapping[str, Any]],
    *,
    query: str,
    corpus_generation: str,
    source_tool: str,
) -> None:
    from retrieval_telemetry import best_effort_record_events

    events = [
        _impression_event(
            item,
            rank,
            query=query,
            corpus_generation=corpus_generation,
            source_tool=source_tool,
        )
        for rank, item in enumerate(rows, start=1)
    ]
    best_effort_record_events([event for event in events if event is not None])


def _generation_dense_hits(
    filters: Mapping[str, Any],
    *,
    catalog: Any,
    context: dict[str, Any],
    stop: Mapping[str, Any],
    require_seal: Callable[[], None],
    own_connection: bool,
    embedder: object,
    model_id: object,
    model_revision: object,
) -> Sequence[Mapping[str, Any]] | None:
    """Vectors from the active generation, sealed before and after the read.

    Under a hard deadline the search gets its own connection so a straggler
    cannot outlive the caller on the shared one.
    """
    import search_memory

    owned = None
    try:
        require_seal()
        connection, owned = _dense_connection(
            search_memory, catalog, context, stop, own_connection
        )
        if connection is None:
            return None
        return _dense_hits_or_none(
            search_memory,
            filters,
            catalog=catalog,
            context=context,
            connection=connection,
            embedder=embedder,
            model_id=model_id,
            model_revision=model_revision,
            stop=stop,
            require_seal=require_seal,
        )
    except (GenerationSealChanged, TimeoutError):
        raise
    except Exception:  # noqa: BLE001 - unreadable vectors degrade one signal
        require_seal()
        context["dense_fallback"] = "generation_vectors_unavailable"
        return None
    finally:
        _close_quietly(owned)


def _close_quietly(handle: Any) -> None:
    if handle is not None:
        handle.close()


def _dense_connection(
    search_memory: Any,
    catalog: Any,
    context: dict[str, Any],
    stop: Mapping[str, Any],
    own_connection: bool,
) -> tuple[Any, Any]:
    """(connection, owned) — `owned` is the caller's to close; None means unusable."""
    if not own_connection:
        return context["connection"], None
    owned = search_memory._generation_connection(catalog, context["manifest"], **stop)
    if owned is None:
        context["dense_fallback"] = "generation_vectors_unavailable"
        return None, None
    return owned, owned


def _dense_hits_or_none(
    search_memory: Any,
    filters: Mapping[str, Any],
    *,
    catalog: Any,
    context: dict[str, Any],
    connection: Any,
    embedder: object,
    model_id: object,
    model_revision: object,
    stop: Mapping[str, Any],
    require_seal: Callable[[], None],
) -> Sequence[Mapping[str, Any]] | None:
    rows = search_memory._generation_vectors_search(
        filters["query"],
        catalog,
        context["manifest"],
        connection,
        embedder=embedder,
        model_id=model_id,
        model_revision=model_revision,
        scope=filters["scope"],
        limit=filters["limit"],
        project=filters["project"],
        since=filters["since"],
        as_of=filters["as_of"],
        **stop,
    )
    require_seal()
    if rows is None:
        context["dense_fallback"] = "generation_vectors_unavailable"
        return None
    return _dense_filtered_hits(rows, filters)


def _generation_lexical_hits(
    filters: Mapping[str, Any],
    *,
    catalog: Any,
    context: Mapping[str, Any],
    stop: Mapping[str, Any],
) -> Sequence[Mapping[str, Any]]:
    import search_memory

    _require_unchanged_generation(catalog, context, stop)
    rows = search_memory._generation_fts_search(
        filters["query"],
        context["manifest"],
        context["connection"],
        scope=filters["scope"],
        limit=filters["limit"],
        project=filters["project"],
        since=filters["since"],
        as_of=filters["as_of"],
        **stop,
    )
    _require_unchanged_generation(catalog, context, stop)
    return _filtered_hits(rows, filters)


def _requested_or_recommended(requested_profile: object, analysis: QueryAnalysis) -> str:
    return _normalize_profile(requested_profile) or analysis.recommended_profile


def _exact_query(analysis: QueryAnalysis) -> str:
    return analysis.normalized_query or analysis.query


def _any_true(*flags: object) -> bool:
    return any(bool(flag) for flag in flags)


def _first_reason(*reasons: object) -> Any:
    """The first reason that says something; None when none of them does."""
    for reason in reasons:
        if reason:
            return reason
    return None


def _candidate_pool(limit: int) -> int:
    """How many chunks to ask each backend for, so `limit` pages can exist.

    Several chunks of one page are one answer repeated, not several answers. A
    pool the size of the answer therefore cannot hold `limit` distinct pages:
    measured on this vault, twenty-five chunks carried three to seven pages. The
    pool is what makes the page-diverse order downstream have anything to choose
    from, and it costs one bounded index read per backend.
    """
    if limit <= 0:
        return MIN_CANDIDATE_POOL
    return max(MIN_CANDIDATE_POOL, min(limit * CANDIDATE_FANOUT, MAX_CANDIDATE_POOL))


def _backend_limit(limit: int, max_candidates: int | None) -> int:
    """How many rows each backend may return before fusion trims them."""
    pool = _candidate_pool(limit)
    if max_candidates is None or int(max_candidates) <= 0:
        return pool
    return min(pool, int(max_candidates))


def _place_by_page(
    candidate: RetrievalCandidate,
    seen: set[str],
    first: list[RetrievalCandidate],
    extras: list[RetrievalCandidate],
) -> None:
    page = candidate.relative_path
    if page in seen:
        extras.append(candidate)
        return
    seen.add(page)
    first.append(candidate)


def _supporting(candidate: RetrievalCandidate) -> bool:
    """Kinds the trust table puts below neutral: raw evidence and gap stubs.

    They support an answer rather than leading it, which is the vault's own
    retrieval rule — answer from the compiled pages, read raw material only when
    the pages are missing, stale or contradictory. Measured: importing 236 past
    sessions filled every place with transcripts of the discussions the decision
    pages were compiled from, and the stand fell from hit@5 0.7 to 0.0.
    """
    return candidate.type_weight < 1.0


def _page_diverse(
    candidates: Sequence[RetrievalCandidate],
) -> tuple[RetrievalCandidate, ...]:
    """One chunk per page first, then every chunk that repeats a page.

    This is the last word on the order, so it is where a page is stopped from
    taking several visible slots. Nothing is dropped — the repeats follow the
    first pass — so a caller that wanted every chunk of one page still receives
    them, in order. The remedies that compare candidates to each other (maximal
    marginal relevance, semantic deduplication) are not needed here: the
    duplication is structural.

    A slot is claimed in `_diversity_groups` order, which keeps the reranker's
    judgement ahead of the pool it never read.
    """
    first: list[RetrievalCandidate] = []
    extras: list[RetrievalCandidate] = []
    seen: set[str] = set()
    for group in _diversity_groups(candidates):
        _place_group(group, seen, first, extras)
    return tuple(first + extras)


def _place_group(
    group: Sequence[RetrievalCandidate],
    seen: set[str],
    first: list[RetrievalCandidate],
    extras: list[RetrievalCandidate],
) -> None:
    for candidate in group:
        _place_by_page(candidate, seen, first, extras)


def _diversity_groups(
    candidates: Sequence[RetrievalCandidate],
) -> tuple[list[RetrievalCandidate], ...]:
    """Four passes over the pool, strongest claim on a visible slot first.

    What the reranker actually scored leads what it never saw: a slot is filled
    from the bounded pool it read before the tail below that pool is used to
    replace a repeat. Within each of those, the vault's own rule holds —
    compiled pages before raw evidence. With no reranking, or no tail, every
    candidate lands in one group and the order is the one this always had.
    """
    groups: list[list[RetrievalCandidate]] = []
    for tier in _scored_then_unseen(candidates):
        groups.extend(_by_kind(tier))
    return tuple(groups)


def _scored_then_unseen(
    candidates: Sequence[RetrievalCandidate],
) -> tuple[list[RetrievalCandidate], list[RetrievalCandidate]]:
    """What the reranker scored, then what it never read."""
    return (
        [item for item in candidates if item.rerank_score is not None],
        [item for item in candidates if item.rerank_score is None],
    )


def _by_kind(
    candidates: Sequence[RetrievalCandidate],
) -> tuple[list[RetrievalCandidate], list[RetrievalCandidate]]:
    """Compiled pages lead; raw evidence and gap stubs support."""
    return (
        [item for item in candidates if not _supporting(item)],
        [item for item in candidates if _supporting(item)],
    )


def _require_bounded_int(value: object, low: int, high: int, name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} must be between {low} and {high}")
    if not low <= value <= high:
        raise ValueError(f"{name} must be between {low} and {high}")


def _fused_candidates(
    backends: _BackendRun, signals: Sequence[str], intents: Sequence[str] | None = None
) -> tuple[tuple[RetrievalCandidate, ...], dict[str, dict[str, Any]]]:
    return fuse_rrf(
        lexical=_fusion_input(backends.lexical_hits, "lexical", signals),
        dense=_fusion_input(backends.dense_hits, "dense", signals),
        graph=_fusion_input(backends.graph_hits, "graph", signals),
        intents=intents,
    )


def _capped(
    candidates: Sequence[RetrievalCandidate], cap: int | None
) -> tuple[RetrievalCandidate, ...]:
    """A non-positive or absent cap means every candidate stays."""
    if cap is None or int(cap) <= 0:
        return tuple(candidates)
    return tuple(candidates[: int(cap)])


def _rerank_signals(trace: _RerankTrace) -> tuple[str, ...]:
    if trace.applied:
        return ("reranker",)
    return ()


def _rerank_failure(trace: _RerankTrace, current: str | None) -> str | None:
    if trace.optional_timeout:
        return current or "optional_stage_timeout"
    return current


def _maybe_rerank(
    candidates: tuple[RetrievalCandidate, ...],
    display_meta: Mapping[str, Mapping[str, Any]],
    *,
    analysis: QueryAnalysis,
    requested: str,
    limit: int,
    max_candidates: int | None,
    rerank_enabled: bool,
    deadline_monotonic: float | None,
    cancelled: Callable[[], bool] | None,
    trace: _RerankTrace,
) -> tuple[RetrievalCandidate, ...]:
    """Rerank only when there is something to rerank and it is switched on."""
    if not candidates or not rerank_enabled:
        return candidates
    _check_stopped(deadline_monotonic, cancelled)
    return _apply_reranking(
        candidates,
        display_meta,
        analysis=analysis,
        requested=requested,
        limit=limit,
        max_candidates=max_candidates,
        rerank_enabled=rerank_enabled,
        deadline_monotonic=deadline_monotonic,
        cancelled=cancelled,
        trace=trace,
    )


def _retrieval_trace(
    *,
    requested: str,
    effective: str,
    signals: Sequence[str],
    fallback: str | None,
    corpus_generation: str,
    partial: bool,
    rerank_trace: _RerankTrace,
) -> RetrievalTrace:
    return RetrievalTrace(
        requested_mode=requested,
        effective_mode=effective,
        signals_used=tuple(dict.fromkeys(signals)),
        fallback_reason=fallback,
        corpus_generation=corpus_generation,
        partial=partial,
        reranker_applied=rerank_trace.applied,
        reranker_model_id=_as_optional_str(rerank_trace.model_id),
        reranker_model_revision=_as_optional_str(rerank_trace.model_revision),
        reranker_depth=_as_optional_int(rerank_trace.depth),
        reranker_duration_ms=_as_optional_int(rerank_trace.duration_ms),
        reranker_fallback_reason=rerank_trace.fallback_reason,
    )


@dataclass
class _PlanProgress:
    """What the plan has finished, carried so an expiry need not discard it.

    `retrieve()` runs its legs in sequence and checks the caller's stop flag
    between them. Before this existed, that check raised and every finished leg
    went in the bin. Measured on this vault under load 13-17: 4.4-5.5 s of a
    10 s budget went to optional stages that returned nothing, and then the
    lexical answer that was already computed was discarded -- 18 of 36 calls
    raised rather than answering. The fields below are filled as work
    completes, so the stop path has something truthful to hand back.
    """

    analysis: QueryAnalysis
    requested: str
    wanted: Sequence[str]
    limit: int
    corpus_generation: str
    graph_enabled: bool
    backends: _BackendRun = dataclass_field(default_factory=_BackendRun)
    rerank_trace: _RerankTrace = dataclass_field(default_factory=_RerankTrace)
    candidates: tuple[RetrievalCandidate, ...] | None = None
    display_meta: dict[str, dict[str, Any]] | None = None


def _any_leg_finished(backends: _BackendRun) -> bool:
    return backends.ran_lexical or backends.ran_dense or backends.ran_graph


def _partial_candidates(
    progress: _PlanProgress, signals: Sequence[str]
) -> tuple[tuple[RetrievalCandidate, ...], dict[str, dict[str, Any]]]:
    """Fusion already done is reused; otherwise the rows in hand are fused now."""
    if progress.candidates is not None:
        return progress.candidates, progress.display_meta or {}
    return _fused_candidates(progress.backends, signals, progress.analysis.intents)


def _assembled_partial(progress: _PlanProgress, reason: str) -> RetrievalResult:
    """The legs that finished, ranked and labelled. No new work, no new budget.

    Everything here is in-memory work over rows already paid for: fusion,
    exact-filename promotion, page diversity, the cap. No backend is called, the
    reranker is not run, and no stop check is made -- the stop already happened
    and this is the unwinding, not more retrieval.

    The stop reason leads `fallback_reason` because it is the dominant fact: the
    answer is short because the clock ran out. Which legs are missing is not
    lost with it -- `effective_mode` and `signals_used` name exactly the ones
    that finished.
    """
    backends = progress.backends
    effective, fallback, signals = _resolve_effective_mode(
        progress.requested,
        wanted=progress.wanted,
        ran_lexical=backends.ran_lexical,
        ran_dense=backends.ran_dense,
        ran_graph=backends.ran_graph,
        dense_available=backends.dense_available,
        graph_available=backends.graph_available,
        graph_enabled=progress.graph_enabled,
    )
    candidates, display_meta = _partial_candidates(progress, signals)
    candidates = _promote_exact_filename(candidates, _exact_query(progress.analysis))
    return RetrievalResult(
        candidates=_capped(_page_diverse(candidates), progress.limit),
        trace=_retrieval_trace(
            requested=progress.requested,
            effective=effective,
            signals=signals,
            fallback=_first_reason(reason, fallback),
            corpus_generation=progress.corpus_generation,
            partial=True,
            rerank_trace=progress.rerank_trace,
        ),
        analysis=progress.analysis,
        display_meta=display_meta,
    )


def _partial_or_reraise(
    progress: _PlanProgress, stopped: RetrievalStopped
) -> RetrievalResult:
    """Hand back what finished; when nothing did, the stop is the only truth.

    A result with no rows and no signals would be a refusal wearing the clothes
    of an answer, and this vault has ruled against that shape before.
    """
    if not _any_leg_finished(progress.backends):
        raise stopped
    return _assembled_partial(progress, stopped.reason)


def _executed_plan(
    progress: _PlanProgress,
    filters: Mapping[str, Any],
    *,
    lexical_backend: BackendFn | None,
    dense_backend: BackendFn | None,
    graph_backend: BackendFn | None,
    graph_edge_families: Mapping[str, bool] | None,
    graph_per_seed_limit: int,
    graph_global_limit: int,
    rerank_enabled: bool,
    partial: bool,
    max_candidates: int | None,
    deadline_monotonic: float | None,
    cancelled: Callable[[], bool] | None,
) -> RetrievalResult:
    """The plan itself. Every stop inside it leaves `progress` truthful."""
    backends = _run_backends(
        progress.backends,
        filters,
        analysis=progress.analysis,
        requested=progress.requested,
        wanted=progress.wanted,
        lexical_backend=lexical_backend,
        dense_backend=dense_backend,
        graph_backend=graph_backend,
        graph_enabled=progress.graph_enabled,
        graph_edge_families=graph_edge_families,
        graph_per_seed_limit=graph_per_seed_limit,
        graph_global_limit=graph_global_limit,
        corpus_generation=progress.corpus_generation,
        deadline_monotonic=deadline_monotonic,
        cancelled=cancelled,
    )
    optional_failure = backends.optional_failure
    partial = _any_true(partial, backends.partial)

    effective, fallback, signals = _resolve_effective_mode(
        progress.requested,
        wanted=progress.wanted,
        ran_lexical=backends.ran_lexical,
        ran_dense=backends.ran_dense,
        ran_graph=backends.ran_graph,
        dense_available=backends.dense_available,
        graph_available=backends.graph_available,
        graph_enabled=progress.graph_enabled,
    )
    fallback = _first_reason(backends.graph_failure, fallback)

    candidates, display_meta = _fused_candidates(
        backends, signals, progress.analysis.intents
    )
    exact_query = _exact_query(progress.analysis)
    candidates = _promote_exact_filename(candidates, exact_query)
    # Capped before the check, not after, so that what `progress` holds is
    # exactly what the plan holds: a salvage must not answer from a wider pool
    # than the run itself was working with. `_capped` is a pure slice and makes
    # no stop check, so moving it earlier changes nothing else.
    candidates = _capped(candidates, max_candidates)
    progress.candidates = candidates
    progress.display_meta = display_meta
    _check_stopped(deadline_monotonic, cancelled)

    rerank_trace = progress.rerank_trace
    candidates = _maybe_rerank(
        candidates,
        display_meta,
        analysis=progress.analysis,
        requested=progress.requested,
        limit=progress.limit,
        max_candidates=max_candidates,
        rerank_enabled=rerank_enabled,
        deadline_monotonic=deadline_monotonic,
        cancelled=cancelled,
        trace=rerank_trace,
    )
    signal_list = [*signals, *_rerank_signals(rerank_trace)]
    optional_failure = _rerank_failure(rerank_trace, optional_failure)
    partial = _any_true(partial, rerank_trace.optional_timeout)

    candidates = _promote_exact_filename(candidates, exact_query)
    progress.candidates = candidates
    _check_stopped(deadline_monotonic, cancelled)
    candidates = _capped(_page_diverse(candidates), progress.limit)

    return RetrievalResult(
        candidates=candidates,
        trace=_retrieval_trace(
            requested=progress.requested,
            effective=effective,
            signals=signal_list,
            fallback=_first_reason(optional_failure, fallback),
            corpus_generation=progress.corpus_generation,
            partial=partial,
            rerank_trace=rerank_trace,
        ),
        analysis=progress.analysis,
        display_meta=display_meta,
    )


def retrieve(
    query: str,
    *,
    requested_profile: str | None = None,
    scope: str = "all",
    limit: int = 10,
    project: str | None = None,
    since: str | None = None,
    as_of: str | None = None,
    lexical_backend: BackendFn | None = None,
    dense_backend: BackendFn | None = None,
    graph_backend: BackendFn | None = None,
    corpus_generation: str = "legacy",
    graph_enabled: bool = True,
    rerank_enabled: bool = True,
    partial: bool = False,
    deadline_monotonic: float | None = None,
    max_candidates: int | None = None,
    cancelled: Callable[[], bool] | None = None,
    graph_per_seed_limit: int = GRAPH_PER_SEED_LIMIT,
    graph_global_limit: int = GRAPH_GLOBAL_LIMIT,
    graph_edge_families: Mapping[str, bool] | None = None,
) -> RetrievalResult:
    """Plan and execute retrieval with truthful mode/signal reporting.

    Lexical and dense backends are invoked independently with identical hard
    filters. Fusion is rank-only RRF. Raw backend scores stay on candidates.

    On expiry the finished legs are returned rather than discarded, labelled
    `partial` with the stop as `fallback_reason`. No new work starts after the
    deadline, so the deadline still binds every backend call; what changes is
    only what happens to work already paid for. When nothing finished there is
    nothing to hand back and the stop propagates as before. See
    `docs/research/2026-08-29-what-an-expired-retrieval-still-owes.md`.
    """
    _check_stopped(deadline_monotonic, cancelled)
    analysis = analyze_query(query)
    requested = _requested_or_recommended(requested_profile, analysis)
    backend_limit = _backend_limit(limit, max_candidates)
    filters = {
        "query": analysis.normalized_query or analysis.query,
        "scope": scope,
        "limit": backend_limit,
        "project": project,
        "since": since,
        "as_of": as_of,
    }

    wanted = PROFILE_SIGNALS[requested]
    _require_bounded_int(graph_per_seed_limit, 1, 100, "graph_per_seed_limit")
    _require_bounded_int(graph_global_limit, 1, 1000, "graph_global_limit")
    _require_known_edge_families(graph_edge_families)
    progress = _PlanProgress(
        analysis=analysis,
        requested=requested,
        wanted=wanted,
        limit=limit,
        corpus_generation=corpus_generation,
        graph_enabled=graph_enabled,
    )
    try:
        return _executed_plan(
            progress,
            filters,
            lexical_backend=lexical_backend,
            dense_backend=dense_backend,
            graph_backend=graph_backend,
            graph_edge_families=graph_edge_families,
            graph_per_seed_limit=graph_per_seed_limit,
            graph_global_limit=graph_global_limit,
            rerank_enabled=rerank_enabled,
            partial=partial,
            max_candidates=max_candidates,
            deadline_monotonic=deadline_monotonic,
            cancelled=cancelled,
        )
    except RetrievalStopped as stopped:
        return _partial_or_reraise(progress, stopped)


def trace_to_dict(trace: RetrievalTrace) -> dict[str, object]:
    return {
        "schema_version": "retrieval-trace/v1",
        "requested_mode": trace.requested_mode,
        "effective_mode": trace.effective_mode,
        "signals_used": list(trace.signals_used),
        "fallback_reason": trace.fallback_reason,
        "corpus_generation": trace.corpus_generation,
        "partial": trace.partial,
        "reranker_applied": trace.reranker_applied,
        "reranker_model_id": trace.reranker_model_id,
        "reranker_model_revision": trace.reranker_model_revision,
        "reranker_depth": trace.reranker_depth,
        "reranker_duration_ms": trace.reranker_duration_ms,
        "reranker_fallback_reason": trace.reranker_fallback_reason,
    }


_LEGACY_DISPLAY_FIELDS = (
    "authority",
    "confidence",
    "status",
    "type",
    "valid_from",
    "valid_to",
    "language",
    "source_id",
    "content",
    "lance_distance",
    "graph_seed_id",
    "graph_direction",
    "graph_edge_type",
    "graph_text_overlap",
)


def _legacy_trace_fields(trace: RetrievalTrace) -> dict[str, Any]:
    return {
        "requested_mode": trace.requested_mode,
        "effective_mode": trace.effective_mode,
        "signals_used": list(trace.signals_used),
        "fallback_reason": trace.fallback_reason,
        "generation": trace.corpus_generation,
        "partial": trace.partial,
        "reranker_applied": trace.reranker_applied,
        "reranker_model_id": trace.reranker_model_id,
        "reranker_model_revision": trace.reranker_model_revision,
        "reranker_depth": trace.reranker_depth,
        "reranker_duration_ms": trace.reranker_duration_ms,
        "reranker_fallback_reason": trace.reranker_fallback_reason,
    }


def _legacy_scores(candidate: RetrievalCandidate) -> dict[str, Any]:
    return {
        "candidate_id": candidate.candidate_id,
        "source_sha256": candidate.source_sha256,
        "heading_ancestry": list(candidate.heading_path),
        "bm25_rank": candidate.bm25_rank,
        "bm25_score": candidate.bm25_score,
        "vector_rank": candidate.vector_rank,
        "vector_score": candidate.vector_score,
        "graph_rank": candidate.graph_rank,
        "graph_score": candidate.graph_score,
        "rrf_score": round(candidate.rrf_score, 4),
        "rerank_score": candidate.rerank_score,
        "final_score": round(candidate.final_score, 4),
        "score": round(candidate.final_score, 4),
    }


def _display_value(
    override: Mapping[str, str], info: Mapping[str, Any], path: str, key: str, fallback: str
) -> str:
    """Caller-supplied text wins, then the candidate's own metadata."""
    return override.get(path) or info.get(key) or fallback


def _legacy_display(
    path: str,
    info: Mapping[str, Any],
    overrides: Mapping[str, Mapping[str, str]],
) -> dict[str, Any]:
    return {
        "path": path,
        "title": _display_value(overrides["titles"], info, path, "title", Path(path).stem),
        "summary": _display_value(overrides["summaries"], info, path, "summary", ""),
        "project": _display_value(overrides["projects"], info, path, "project", ""),
        "timestamp": _display_value(overrides["timestamps"], info, path, "timestamp", ""),
    }


def _legacy_assertion_path(info: Mapping[str, Any]) -> list[dict[str, Any]] | None:
    steps = info.get("assertion_path")
    if not steps:
        return None
    return [
        {**dict(step), "evidence_ids": list(step.get("evidence_ids") or ())}
        for step in steps
    ]


def _legacy_extras(
    candidate: RetrievalCandidate, info: Mapping[str, Any]
) -> dict[str, Any]:
    extras: dict[str, Any] = {
        key: info[key]
        for key in _LEGACY_DISPLAY_FIELDS
        if info.get(key) not in (None, "")
    }
    assertion_path = _legacy_assertion_path(info)
    if assertion_path is not None:
        extras["assertion_path"] = assertion_path
    if candidate.evidence_ids:
        extras["evidence_ids"] = list(candidate.evidence_ids)
    return extras


def _display_source(
    display_meta: Mapping[str, Mapping[str, Any]] | None, result: RetrievalResult
) -> Mapping[str, Mapping[str, Any]]:
    if display_meta is not None:
        return display_meta
    return result.display_meta or {}


def _display_overrides(
    titles: Mapping[str, str] | None,
    summaries: Mapping[str, str] | None,
    projects: Mapping[str, str] | None,
    timestamps: Mapping[str, str] | None,
) -> dict[str, Mapping[str, str]]:
    return {
        "titles": titles or {},
        "summaries": summaries or {},
        "projects": projects or {},
        "timestamps": timestamps or {},
    }


def candidates_to_legacy(
    result: RetrievalResult,
    *,
    titles: Mapping[str, str] | None = None,
    summaries: Mapping[str, str] | None = None,
    projects: Mapping[str, str] | None = None,
    timestamps: Mapping[str, str] | None = None,
    display_meta: Mapping[str, Mapping[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Convert orchestrator output to the historical search() dict rows."""
    overrides = _display_overrides(titles, summaries, projects, timestamps)
    meta = _display_source(display_meta, result)
    trace_fields = _legacy_trace_fields(result.trace)
    return [
        _legacy_row(candidate, _candidate_info(meta, candidate), overrides, trace_fields)
        for candidate in result.candidates
    ]


def _candidate_info(meta: object, candidate: RetrievalCandidate) -> Mapping[str, Any]:
    if not isinstance(meta, dict):
        return {}
    return meta.get(candidate.candidate_id, {})


def _legacy_row(
    candidate: RetrievalCandidate,
    info: Mapping[str, Any],
    overrides: Mapping[str, Mapping[str, str]],
    trace_fields: Mapping[str, Any],
) -> dict[str, Any]:
    row = _legacy_display(candidate.relative_path, info, overrides)
    row.update(_legacy_scores(candidate))
    row["chunk_id"] = info.get("chunk_id") or candidate.candidate_id
    row.update(trace_fields)
    row.update(_legacy_extras(candidate, info))
    return row


# Fields a backend may supply that pass through untouched when present.
_PASSTHROUGH_HIT_FIELDS = (
    "bm25_score",
    "vector_score",
    "graph_boost",
    "evidence_ids",
    "authority",
    "confidence",
    "status",
    "type",
    "valid_from",
    "valid_to",
    "language",
    "source_id",
    "generation",
    "content",
    "lance_distance",
    "seed_id",
    "hop",
    "direction",
    "edge_type",
    "assertion_path",
    "retrieval_confidence",
)


def _first_present(row: Mapping[str, Any], keys: tuple[str, ...], fallback: Any) -> Any:
    """First usable value among `keys`, else the fallback (empty counts as absent)."""
    for key in keys:
        value = row.get(key)
        if value:
            return value
    return fallback


def _legacy_candidate_id(row: Mapping[str, Any], path: str) -> str:
    fallback = Path(path).stem or path
    return str(_first_present(row, ("candidate_id", "chunk_id", "slug"), fallback))


def _base_hit(row: Mapping[str, Any], path: str, score_key: str) -> dict[str, Any]:
    candidate_id = _legacy_candidate_id(row, path)
    return {
        "candidate_id": candidate_id,
        "chunk_id": _first_present(row, ("chunk_id",), candidate_id),
        "parent_id": str(_first_present(row, ("parent_id", "parent_page"), path)),
        "relative_path": path,
        "path": path,
        "heading_path": _first_present(row, ("heading_path", "heading_ancestry"), ()),
        "source_sha256": _source_sha256(row),
        "byte_start": int(_first_present(row, ("byte_start",), 0)),
        "byte_end": int(_first_present(row, ("byte_end",), 0)),
        "score": float(_first_present(row, (score_key, "score"), 0.0)),
        "title": _first_present(row, ("title",), Path(path).stem),
        "summary": _first_present(row, ("summary",), ""),
        "project": _first_present(row, ("project",), ""),
        "timestamp": _first_present(row, ("timestamp",), ""),
    }


def _backend_hit_from_legacy(
    row: Mapping[str, Any], *, score_key: str = "score"
) -> dict[str, Any]:
    path = str(_first_present(row, ("path", "relative_path"), ""))
    hit = _base_hit(row, path, score_key)
    hit.update({key: row[key] for key in _PASSTHROUGH_HIT_FIELDS if key in row})
    hit.setdefault("content", hit.get("summary") or "")
    return hit


def _requested_profile(
    profile: str | None, analysis: QueryAnalysis, *, semantic: bool
) -> str:
    """`semantic=False` forces the lexical profile whatever the planner says."""
    if not semantic:
        return "BASE"
    requested = _normalize_profile(profile)
    if requested is not None:
        return requested
    return "HYBRID"


def _wanted_signals(requested: str, *, semantic: bool) -> tuple[str, ...]:
    wanted = PROFILE_SIGNALS[requested]
    if semantic:
        return tuple(wanted)
    return tuple(signal for signal in wanted if signal != "dense") or ("lexical",)


def _selected_catalog(catalog: Any, search_memory: Any) -> Any:
    if catalog is not None:
        return catalog
    return search_memory._active_generation_catalog()


def _generation_stop(
    deadline_monotonic: float | None, cancelled: Callable[[], bool] | None
) -> dict[str, Any]:
    stop: dict[str, Any] = {}
    if deadline_monotonic is not None:
        stop["deadline"] = deadline_monotonic
    if cancelled is not None:
        stop["cancelled"] = cancelled
    return stop


def _optional_stop(hard_deadline: bool, stop: dict[str, Any]) -> dict[str, Any]:
    """Optional stages get no deadline of their own under a hard one."""
    if hard_deadline:
        return {}
    return stop


def _optional_value(hard_deadline: bool, value: Any) -> Any:
    if hard_deadline:
        return None
    return value


def _catalog_requested(catalog: Any, force_rebuild: bool, page_paths: Any) -> bool:
    return catalog is not None and not force_rebuild and page_paths is None


def _wants_vectors(wanted: Sequence[str], semantic: bool) -> bool:
    return "dense" in wanted and semantic


def _generation_naming(
    use_generation: bool,
    catalog_requested: bool,
    context: Mapping[str, Any],
    corpus_generation: str,
) -> tuple[str, str | None]:
    """(corpus generation, generation fallback) as the trace must report them."""
    if use_generation:
        return str(context["manifest"]["generation_id"]), None
    if catalog_requested:
        # Always continue through retrieve(); report truthful generation failure.
        return corpus_generation, "generation_unavailable"
    return corpus_generation, None


def _active_manifest_for(
    catalog: Any,
    search_memory: Any,
    *,
    deadline: float | None,
    cancelled: Callable[[], bool] | None,
    stop: Mapping[str, Any],
) -> tuple[Any, Any] | None:
    try:
        from repository_scope import resolve_repository_scope

        scope = resolve_repository_scope(
            search_memory.ROOT, deadline=deadline, cancelled=cancelled
        )
        manifest = catalog.get_active_for_repository(scope, **stop)
    except TimeoutError:
        raise
    except Exception:  # noqa: BLE001 - no usable generation is not an error
        return None
    if not isinstance(manifest, dict):
        return None
    return scope, manifest


def _note_stale_vectors(
    context: dict[str, Any], manifest: Mapping[str, Any], want_vectors: bool
) -> None:
    if not want_vectors or manifest.get("vector_state") != "stale":
        return
    context["dense_fallback"] = "generation_vectors_unavailable"
    context["legacy_dense_blocked"] = True


def _generation_lexical_or_raise(
    filters: Mapping[str, Any],
    *,
    catalog: Any,
    context: Mapping[str, Any],
    stop: Mapping[str, Any],
    note: Callable[[str], None],
) -> Sequence[Mapping[str, Any]]:
    """Lexical hits from the generation; any failure sends the caller to legacy."""
    try:
        return _generation_lexical_hits(
            filters, catalog=catalog, context=context, stop=stop
        )
    except GenerationSealChanged:
        note("generation_seal_changed")
        raise
    except TimeoutError:
        raise
    except Exception:
        note("generation_corrupt")
        raise GenerationSealChanged


def _generation_dense_backend_hits(
    filters: Mapping[str, Any],
    *,
    catalog: Any,
    context: dict[str, Any],
    stop: Mapping[str, Any],
    require_seal: Callable[[], None],
    own_connection: bool,
    generation_fallback: str | None,
    embedder: object,
    model_id: object,
    model_revision: object,
) -> Sequence[Mapping[str, Any]] | None:
    unusable = _generation_dense_unusable(
        generation_fallback,
        embedder=embedder,
        model_id=model_id,
        model_revision=model_revision,
    )
    if unusable is not None:
        context["dense_fallback"] = unusable
        return None
    return _generation_dense_hits(
        filters,
        catalog=catalog,
        context=context,
        stop=stop,
        require_seal=require_seal,
        own_connection=own_connection,
        embedder=embedder,
        model_id=model_id,
        model_revision=model_revision,
    )


def _legacy_dense_backend_hits(
    search_memory: Any,
    filters: Mapping[str, Any],
    *,
    page_paths: Any,
    deadline: float | None,
    cancelled: Callable[[], bool] | None,
) -> Sequence[Mapping[str, Any]] | None:
    rows = search_memory._legacy_dense_hits(
        filters["query"],
        scope=filters["scope"],
        limit=filters["limit"],
        project=filters["project"],
        since=filters["since"],
        as_of=filters["as_of"],
        page_paths=page_paths,
        deadline=deadline,
        cancelled=cancelled,
    )
    if rows is None:
        return None
    return _dense_filtered_hits(rows, filters)


def _generation_graph_backend_hits(
    filters: Mapping[str, Any],
    *,
    catalog: Any,
    context: Mapping[str, Any],
    stop: Mapping[str, Any],
    cancelled: Callable[[], bool] | None,
) -> Sequence[Mapping[str, Any]] | None:
    active_graph = context.get("graph")
    if active_graph is None:
        return None
    return _generation_graph_hits(
        active_graph,
        filters,
        catalog=catalog,
        context=context,
        stop=stop,
        cancelled=cancelled,
    )


def _neighbour_boost_or_none(
    lexical_backend: Callable[..., Sequence[Mapping[str, Any]]],
    filters: Mapping[str, Any],
) -> Sequence[Mapping[str, Any]] | None:
    try:
        return _neighbour_boost_hits(lexical_backend, filters)
    except TimeoutError:
        raise
    except Exception:  # noqa: BLE001 - the graph signal degrades on its own
        return None


def _resolved_query_encoder(
    search_memory: Any,
    *,
    semantic: bool,
    embedder: object | None,
    model_id: str | None,
    model_revision: str | None,
) -> tuple[object | None, str | None, str | None]:
    """Give this entry point the encoder that `search()` resolves for itself.

    The two entry points disagreed about the semantic flag until 2026-08-25 and
    the flag was fixed then; they still disagreed about the encoder, which is
    the other half of the same trap. `search()` resolves one before delegating,
    so a caller that enters here directly — the grounded answer does — left it
    None and the generation reader refused the dense leg as
    `generation_vectors_unavailable`. Measured on this vault: the grounded
    answer to "как устроен повтор после карантина" was built from six lexical
    rows, four of them the same status document, and reported insufficient
    evidence for a page the vault holds.
    """
    if not semantic or embedder is not None:
        return embedder, model_id, model_revision
    return search_memory._resolved_generation_embedder(True, None, model_id, model_revision)


def retrieve_via_search_memory(
    query: str,
    *,
    scope: str = "all",
    limit: int = 10,
    force_rebuild: bool = False,
    project: str | None = None,
    since: str | None = None,
    as_of: str | None = None,
    # The two entry points disagreed until 2026-08-25: `search_memory.search`
    # asks for the semantic leg, this one did not, so a caller reaching the
    # lower entry directly got lexical-only answers and no sign of it. Every
    # real caller passes the flag explicitly, so this only closes a trap.
    semantic: bool = True,
    page_paths: list[Path] | None = None,
    graph: bool = True,
    rerank: bool = True,
    source_tool: str = "search_memory",
    emit_telemetry: bool = True,
    profile: str | None = None,
    catalog: Any = None,
    generation_embedder: object | None = None,
    generation_model_id: str | None = None,
    generation_model_revision: str | None = None,
    deadline_monotonic: float | None = None,
    max_candidates: int | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> list[dict[str, Any]]:
    """Public search path: independent backends → retrieve() → legacy rows."""
    import search_memory

    _check_stopped(deadline_monotonic, cancelled)
    (
        generation_embedder,
        generation_model_id,
        generation_model_revision,
    ) = _resolved_query_encoder(
        search_memory,
        semantic=semantic,
        embedder=generation_embedder,
        model_id=generation_model_id,
        model_revision=generation_model_revision,
    )

    _GenerationSealChanged = GenerationSealChanged

    analysis = analyze_query(query)
    requested = _requested_profile(profile, analysis, semantic=semantic)
    wanted_tuple = _wanted_signals(requested, semantic=semantic)
    hard_deadline = deadline_monotonic is not None

    selected_catalog = _selected_catalog(catalog, search_memory)
    corpus_generation = "legacy"
    generation_ctx: dict[str, Any] = {
        "manifest": None,
        "connection": None,
        "graph": None,
        "seal": None,
        "dense_fallback": None,
        "legacy_dense_blocked": False,
    }
    generation_stop = _generation_stop(deadline_monotonic, cancelled)
    optional_generation_stop = _optional_stop(hard_deadline, generation_stop)
    optional_deadline = _optional_value(hard_deadline, deadline_monotonic)
    optional_cancelled = _optional_value(hard_deadline, cancelled)

    def _artifact_names_for(manifest: dict[str, object], *, want_vectors: bool) -> tuple[str, ...]:
        names: list[str] = [search_memory.GENERATION_FTS_ARTIFACT]
        if want_vectors and manifest.get("vector_state") == "complete":
            names.extend(search_memory.GENERATION_VECTOR_ARTIFACTS)
        if "graph" in wanted_tuple and search_memory._generation_artifact(
            manifest, "evidence.sqlite3"
        ):
            names.append("evidence.sqlite3")
        return tuple(names)

    def _resolved_manifest(want_vectors: bool) -> tuple[Any, Any] | None:
        """(scope, manifest) for the active generation, or None when unusable."""
        resolved = _active_manifest_for(
            selected_catalog,
            search_memory,
            deadline=deadline_monotonic,
            cancelled=cancelled,
            stop=generation_stop,
        )
        if resolved is None:
            return None
        _note_stale_vectors(generation_ctx, resolved[1], want_vectors)
        return resolved

    def _attach_graph(repository_scope: Any, connection: Any) -> bool:
        try:
            from evidence_graph import EvidenceGraph

            generation_ctx["graph"] = EvidenceGraph.open_active_for_repository(
                selected_catalog,
                repository_scope,
                deadline=deadline_monotonic,
                cancelled=cancelled,
            )
        except TimeoutError:
            _drop_generation_connection(generation_ctx, connection)
            raise
        except Exception:  # noqa: BLE001 - an unusable graph drops the generation
            _drop_generation_connection(generation_ctx, connection)
            generation_ctx["graph"] = None
            return False
        if generation_ctx["graph"] is None:
            _drop_generation_connection(generation_ctx, connection)
            return False
        return True

    def _open_generation(*, want_vectors: bool) -> bool:
        if not _catalog_requested(selected_catalog, force_rebuild, page_paths):
            return False
        resolved = _resolved_manifest(want_vectors)
        if resolved is None:
            return False
        return _adopt_generation(resolved, want_vectors=want_vectors)

    def _adopt_generation(resolved: Any, *, want_vectors: bool) -> bool:
        """Publish one resolved manifest as the generation this query reads."""
        repository_scope, manifest = resolved
        artifact_names = _artifact_names_for(manifest, want_vectors=want_vectors)
        seal = search_memory._generation_consumption_seal(
            selected_catalog, manifest, artifact_names, **generation_stop
        )
        connection = _generation_connection_for(
            search_memory, selected_catalog, manifest, seal, generation_stop
        )
        if connection is None:
            return False
        generation_ctx["manifest"] = manifest
        generation_ctx["connection"] = connection
        generation_ctx["seal"] = seal
        generation_ctx["artifact_names"] = artifact_names
        if "evidence.sqlite3" not in artifact_names:
            return True
        return _attach_graph(repository_scope, connection)

    catalog_requested = _catalog_requested(selected_catalog, force_rebuild, page_paths)
    want_vectors = _wants_vectors(wanted_tuple, semantic)
    use_generation = _open_generation(want_vectors=want_vectors)
    legacy_fallback: str | None = None
    corpus_generation, generation_fallback = _generation_naming(
        use_generation, catalog_requested, generation_ctx, corpus_generation
    )

    def _note_generation_fallback(reason: str) -> None:
        nonlocal generation_fallback
        generation_fallback = generation_fallback or reason

    def lexical_backend(**filters: Any) -> Sequence[Mapping[str, Any]]:
        nonlocal legacy_fallback
        if use_generation:
            return _generation_lexical_or_raise(
                filters,
                catalog=selected_catalog,
                context=generation_ctx,
                stop=generation_stop,
                note=_note_generation_fallback,
            )
        rows = search_memory._legacy_lexical_hits(
            filters["query"],
            scope=filters["scope"],
            limit=filters["limit"],
            force_rebuild=force_rebuild,
            project=filters["project"],
            since=filters["since"],
            as_of=filters["as_of"],
            page_paths=page_paths,
            deadline=deadline_monotonic,
            cancelled=cancelled,
        )
        legacy_fallback = _first_fallback_reason(rows, legacy_fallback)
        return _filtered_hits(rows, filters)

    def dense_backend(**filters: Any) -> Sequence[Mapping[str, Any]] | None:
        nonlocal generation_fallback

        def require_seal() -> None:
            if _generation_seal_holds(
                selected_catalog, generation_ctx, optional_generation_stop
            ):
                return
            nonlocal generation_fallback
            generation_ctx["dense_fallback"] = "generation_seal_changed"
            generation_fallback = "generation_seal_changed"
            raise _GenerationSealChanged

        if "dense" not in wanted_tuple or generation_ctx["legacy_dense_blocked"]:
            return None
        if use_generation:
            return _generation_dense_backend_hits(
                filters,
                catalog=selected_catalog,
                context=generation_ctx,
                stop=optional_generation_stop,
                require_seal=require_seal,
                own_connection=hard_deadline,
                generation_fallback=generation_fallback,
                embedder=generation_embedder,
                model_id=generation_model_id,
                model_revision=generation_model_revision,
            )
        return _legacy_dense_backend_hits(
            search_memory,
            filters,
            page_paths=page_paths,
            deadline=optional_deadline,
            cancelled=optional_cancelled,
        )

    def graph_backend(**filters: Any) -> Sequence[Mapping[str, Any]] | None:
        if not graph or "graph" not in wanted_tuple:
            return None
        if use_generation:
            return _generation_graph_backend_hits(
                filters,
                catalog=selected_catalog,
                context=generation_ctx,
                stop=generation_stop,
                cancelled=cancelled,
            )
        return _neighbour_boost_or_none(lexical_backend, filters)

    def run_retrieval() -> RetrievalResult:
        return retrieve(
            query,
            requested_profile=requested,
            scope=scope,
            limit=limit,
            project=project,
            since=since,
            as_of=as_of,
            lexical_backend=_wanted_backend(lexical_backend, "lexical" in wanted_tuple),
            dense_backend=_wanted_backend(
                dense_backend, "dense" in wanted_tuple and semantic
            ),
            graph_backend=_wanted_backend(
                graph_backend, "graph" in wanted_tuple and graph
            ),
            corpus_generation=corpus_generation,
            graph_enabled=graph,
            rerank_enabled=rerank,
            partial=False,
            deadline_monotonic=deadline_monotonic,
            max_candidates=max_candidates,
            cancelled=cancelled,
        )

    def run_under_seal() -> RetrievalResult:
        """One run, valid only while the generation seal still holds."""
        outcome = run_retrieval()
        if use_generation and not _generation_seal_holds(
            selected_catalog, generation_ctx, generation_stop
        ):
            nonlocal generation_fallback
            generation_fallback = "generation_seal_changed"
            raise _GenerationSealChanged
        return outcome

    try:
        try:
            result = run_under_seal()
        except _GenerationSealChanged:
            use_generation = False
            corpus_generation = "legacy"
            result = run_retrieval()
    finally:
        _close_generation_handles(generation_ctx)

    result = _with_reported_trace(
        result,
        query=query,
        dense_fallback=_first_reason(
            generation_ctx.get("dense_fallback"), generation_fallback
        ),
        generation_fallback=generation_fallback,
        legacy_fallback=legacy_fallback,
    )
    rows = candidates_to_legacy(result, display_meta=result.display_meta)
    if emit_telemetry:
        _record_impressions(
            rows, query=query, corpus_generation=corpus_generation, source_tool=source_tool
        )
    return rows
