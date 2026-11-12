"""Deterministic lexical scoring, hash vectors, FTS5 token checks and reciprocal-rank fusion
shared by the adapters."""
from __future__ import annotations

import hashlib
import math
import re
import sqlite3
import time
import unicodedata
from collections.abc import Sequence

from retrieval_v2_contract import (
    ADAPTER_KIND,
    LEXICAL_CONFIGURATIONS,
    Candidate,
    QueryScope,
    ScoredCandidate,
)
from retrieval_v2_corpus import filter_candidates

ALIASES = {
    "каталог": "catalog",
    "目录": "catalog",
    "путь": "path",
    "пути": "path",
    "路径": "path",
    "проверка": "check",
    "проверки": "check",
    "检查": "check",
    "健": "health",
    "康": "health",
    "检": "check",
    "查": "check",
    "команда": "command",
    "命令": "command",
    "命": "command",
    "令": "command",
    "очереди": "queue",
    "очередь": "queue",
    "队列": "queue",
    "доставки": "delivery",
    "доставка": "delivery",
    "交付": "delivery",
    "текущий": "current",
    "текущему": "current",
    "当前": "current",
    "решение": "decision",
    "决定": "decision",
    "удалить": "delete",
    "удаления": "delete",
    "删除": "delete",
    "кэша": "cache",
    "缓存": "cache",
}
STOPWORDS = {
    "a",
    "an",
    "is",
    "the",
    "what",
    "where",
    "which",
    "как",
    "какая",
    "какой",
    "что",
    "的",
    "哪",
    "是",
    "什",
    "么",
}


def tokens(value: str) -> tuple[str, ...]:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    # Keep code symbols intact while splitting paths into searchable components.
    raw = re.findall(r"[a-zа-яё0-9_]+|[\u3400-\u9fff]", normalized)
    return tuple(ALIASES.get(token, token) for token in raw if token and token not in STOPWORDS)


def candidate_text(candidate: Candidate) -> str:
    return " ".join(
        (
            candidate.parent_id,
            candidate.relative_path,
            " ".join(candidate.heading_path),
            candidate.text,
        )
    )


def lexical_score(query_text: str, candidate: Candidate) -> float:
    query = set(tokens(query_text))
    text = set(tokens(candidate_text(candidate)))
    if not query or not text:
        return 0.0
    overlap = query & text
    phrase_bonus = 0.5 if unicodedata.normalize("NFKC", query_text).casefold() in candidate_text(candidate).casefold() else 0.0
    return len(overlap) / math.sqrt(len(query) * len(text)) + phrase_bonus


def _unit_vector(vector: list[float]) -> tuple[float, ...]:
    norm = math.sqrt(sum(component * component for component in vector))
    if norm:
        return tuple(component / norm for component in vector)
    return tuple(vector)


def hash_vector(value: str, dimensions: int = 64) -> tuple[float, ...]:
    vector = [0.0] * dimensions
    for token in tokens(value):
        digest = hashlib.sha256(token.encode("utf-8")).digest()
        index = int.from_bytes(digest[:4], "big") % dimensions
        vector[index] += 1.0 if digest[4] & 1 else -1.0
    return _unit_vector(vector)


def dot(left: Sequence[float], right: Sequence[float]) -> float:
    return sum(a * b for a, b in zip(left, right))


class FakeLexicalAdapter:
    kind = ADAPTER_KIND

    def rank(
        self,
        query_text: str,
        scope: QueryScope,
        candidates: Sequence[Candidate],
        *,
        limit: int,
    ) -> list[ScoredCandidate]:
        eligible = filter_candidates(candidates, scope)
        scored = [ScoredCandidate(candidate, lexical_score(query_text, candidate)) for candidate in eligible]
        return sorted(scored, key=lambda item: (-item.score, item.evidence_id))[:limit]


_LEXICAL_DEADLINE_MESSAGE = "lexical benchmark absolute deadline exceeded"


def _allowed_fts5_tokenizers() -> set[str]:
    return {
        specification
        for configuration in LEXICAL_CONFIGURATIONS.values()
        for specification in configuration["indexes"].values()
    }


def _require_lexical_deadline(deadline: float | None) -> None:
    if deadline is not None and time.perf_counter() >= deadline:
        raise TimeoutError(_LEXICAL_DEADLINE_MESSAGE)


def _fts5_vocabulary(connection: sqlite3.Connection, value: str, tokenizer: str) -> tuple[str, ...]:
    connection.execute(f"CREATE VIRTUAL TABLE token_source USING fts5(text, tokenize='{tokenizer}')")
    connection.execute("CREATE VIRTUAL TABLE token_vocab USING fts5vocab(token_source, 'row')")
    connection.execute("INSERT INTO token_source(text) VALUES (?)", (value,))
    return tuple(row[0] for row in connection.execute("SELECT term FROM token_vocab ORDER BY term"))


def _fts5_failure(deadline: float | None, tokenizer: str) -> Exception:
    if deadline is not None and time.perf_counter() >= deadline:
        return TimeoutError(_LEXICAL_DEADLINE_MESSAGE)
    return ValueError(f"required SQLite FTS5 tokenizer unavailable: {tokenizer}")


def fts5_tokens(
    value: str, tokenizer: str, *, deadline: float | None = None
) -> tuple[str, ...]:
    if tokenizer not in _allowed_fts5_tokenizers():
        raise ValueError(f"unsupported FTS5 tokenizer: {tokenizer}")
    connection = sqlite3.connect(":memory:")
    try:
        _require_lexical_deadline(deadline)
        if deadline is not None:
            connection.set_progress_handler(lambda: int(time.perf_counter() >= deadline), 1000)
        result = _fts5_vocabulary(connection, value, tokenizer)
        _require_lexical_deadline(deadline)
        return result
    except sqlite3.Error as exc:
        raise _fts5_failure(deadline, tokenizer) from exc
    finally:
        connection.close()


def _add_rrf_contributions(scores: dict[str, float], ranking: Sequence[tuple[str, float]], k: int) -> None:
    seen: set[str] = set()
    for rank, (evidence_id, _raw_bm25) in enumerate(ranking, 1):
        if evidence_id in seen:
            continue
        seen.add(evidence_id)
        scores[evidence_id] = scores.get(evidence_id, 0.0) + 1.0 / (k + rank)


def _active_rankings(rankings: dict[str, Sequence[tuple[str, float]]]) -> dict[str, Sequence[tuple[str, float]]]:
    return {name: ranking for name, ranking in rankings.items() if ranking}


def reciprocal_rank_fusion(
    rankings: dict[str, Sequence[tuple[str, float]]], *, k: int
) -> list[tuple[str, float]]:
    active_rankings = _active_rankings(rankings)
    if not active_rankings:
        return []
    scores: dict[str, float] = {}
    for index_name in sorted(active_rankings):
        _add_rrf_contributions(scores, active_rankings[index_name], k)
    theoretical_max = len(active_rankings) / (k + 1)
    scores = {evidence_id: score / theoretical_max for evidence_id, score in scores.items()}
    return sorted(scores.items(), key=lambda item: (-item[1], item[0]))
