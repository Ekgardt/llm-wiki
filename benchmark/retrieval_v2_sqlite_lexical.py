"""The SQLite FTS5 lexical adapter and its owned, deadline-bounded index."""
from __future__ import annotations

import contextlib
import os
import sqlite3
import time
import unicodedata
from collections.abc import Sequence
from pathlib import Path

import retrieval_v2_environment
import retrieval_v2_outputs
from retrieval_v2_contract import (
    DEFAULT_LEXICAL_DEADLINE_SECONDS,
    LEXICAL_CONFIGURATIONS,
    MAX_SEGMENTATION_INPUT_CHARS,
    MAX_SEGMENTATION_TOKENS,
    Candidate,
    QueryScope,
    ScoredCandidate,
)
from retrieval_v2_corpus import filter_candidates, normalized_id
from retrieval_v2_outputs import is_reparse_point, validate_cache_root
from retrieval_v2_scoring import candidate_text, fts5_tokens, reciprocal_rank_fusion

_OWNED_INDEX_FLAGS = (
    os.O_RDWR | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
)
_INJECTED_SEGMENTER_RUNTIME = {
    "provenance": "injected-test",
    "quality_evidence": False,
    "version": None,
    "dictionary_sha256": None,
}
_SEGMENTATION_END = object()


def _require_lexical_arguments(lexical_config: str, deadline_seconds: float) -> None:
    if lexical_config not in LEXICAL_CONFIGURATIONS:
        raise ValueError(f"unknown lexical configuration: {lexical_config}")
    if deadline_seconds <= 0:
        raise ValueError("lexical deadline must be positive")


def _candidate_maps(candidates: Sequence[Candidate]) -> tuple[dict[str, Candidate], dict[str, Candidate]]:
    normalized_ids = [normalized_id(candidate.evidence_id) for candidate in candidates]
    if len(normalized_ids) != len(set(normalized_ids)):
        raise ValueError("duplicate normalized candidate id")
    by_id = {candidate.evidence_id: candidate for candidate in candidates}
    universe = {normalized_id(candidate.evidence_id): candidate for candidate in candidates}
    return by_id, universe


def _prepared_lexical_root(requested_root: Path) -> Path:
    if requested_root.exists() and is_reparse_point(requested_root):
        raise ValueError("lexical cache root must not be a symlink or reparse point")
    requested_root.mkdir(parents=True, exist_ok=True)
    if is_reparse_point(requested_root):
        raise ValueError("lexical cache root must not be a symlink or reparse point")
    return validate_cache_root(requested_root).resolve(strict=True)


def _next_segment(iterator) -> object:
    try:
        return next(iterator)
    except StopIteration:
        return _SEGMENTATION_END
    except Exception as exc:
        raise ValueError(f"segmentation failed: {exc}") from exc



def _trigram_match_query(query_text: str) -> str | None:
    """A trigram index matches only three or more characters, quoted as one phrase."""
    normalized = unicodedata.normalize("NFKC", query_text).casefold()
    if len(normalized) < 3:
        return None
    return f'"{normalized.replace(chr(34), chr(34) * 2)}"'


class SQLiteLexicalAdapter:
    kind = "sqlite-fts5-bm25"

    def __init__(
        self,
        candidates: Sequence[Candidate],
        cache_root: Path | str,
        lexical_config: str,
        *,
        test_segmenter=None,
        deadline_seconds: float = DEFAULT_LEXICAL_DEADLINE_SECONDS,
        deadline: float | None = None,
    ) -> None:
        _require_lexical_arguments(lexical_config, deadline_seconds)
        self.configuration = LEXICAL_CONFIGURATIONS[lexical_config]
        self._candidates, self._candidate_universe = _candidate_maps(candidates)
        self._root = _prepared_lexical_root(Path(cache_root))
        self._root_identity = self._root.stat(follow_symlinks=False)
        self.path = self._root / f"lexical-{lexical_config}.sqlite3"
        self._deadline = deadline or time.perf_counter() + deadline_seconds
        self._jieba, self.segmentation_runtime = self._segmenter(test_segmenter)
        self._descriptor = os.open(self.path, _OWNED_INDEX_FLAGS, 0o600)
        self._file_identity = os.fstat(self._descriptor)
        self._connection: sqlite3.Connection | None = None
        self._closed = False
        try:
            self._initialize(candidates)
        except BaseException:
            self._cleanup(remove=True)
            raise

    def _segmenter(self, test_segmenter) -> tuple[object | None, dict | None]:
        if self.configuration["segmentation"] is None:
            return None, None
        if test_segmenter is None:
            return retrieval_v2_environment.load_pinned_jieba(self._root)
        return self._injected_segmenter(test_segmenter)

    @staticmethod
    def _injected_segmenter(test_segmenter) -> tuple[object, dict]:
        if not callable(getattr(test_segmenter, "cut", None)):
            raise ValueError("test segmenter has no callable cut function")
        return test_segmenter, dict(_INJECTED_SEGMENTER_RUNTIME)

    def _initialize(self, candidates: Sequence[Candidate]) -> None:
        self._check_deadline()
        self._connection = self._connect_owned_file()
        self._connection.set_progress_handler(self._progress_handler, 1000)
        self._connection.execute("PRAGMA journal_mode=DELETE")
        self._connection.execute("PRAGMA synchronous=FULL")
        self._build(candidates)
        self._check_owned_identity()

    def _check_deadline(self) -> None:
        if time.perf_counter() >= self._deadline:
            raise TimeoutError("lexical benchmark absolute deadline exceeded")

    def _progress_handler(self) -> int:
        return int(time.perf_counter() >= self._deadline)

    def _remaining_seconds(self) -> float:
        self._check_deadline()
        return max(0.001, self._deadline - time.perf_counter())

    def _descriptor_uri(self) -> str | None:
        if os.name != "posix":
            return None
        for base in (Path("/proc/self/fd"), Path("/dev/fd")):
            descriptor_path = base / str(self._descriptor)
            if descriptor_path.exists():
                return f"{descriptor_path.as_uri()}?mode=rw"
        return None

    def _connect_owned_file(self) -> sqlite3.Connection:
        descriptor_uri = self._descriptor_uri()
        if descriptor_uri is not None:
            connection = self._connect_through_descriptor(descriptor_uri)
            if connection is not None:
                return connection
        self._check_owned_identity()
        connection = sqlite3.connect(
            f"{self.path.as_uri()}?mode=rw",
            uri=True,
            timeout=self._remaining_seconds(),
        )
        try:
            self._check_owned_identity()
        except BaseException:
            connection.close()
            raise
        return connection

    def _connect_through_descriptor(self, descriptor_uri: str) -> sqlite3.Connection | None:
        connection = None
        try:
            connection = sqlite3.connect(
                descriptor_uri, uri=True, timeout=self._remaining_seconds()
            )
            connection.set_progress_handler(self._progress_handler, 1000)
            connection.execute("PRAGMA journal_mode=DELETE")
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("CREATE TABLE __descriptor_probe(value)")
            connection.rollback()
            return connection
        except sqlite3.Error:
            if connection is not None:
                connection.close()
            self._check_deadline()
            os.ftruncate(self._descriptor, 0)
            os.lseek(self._descriptor, 0, os.SEEK_SET)
        return None

    def _check_owned_identity(self) -> None:
        if not retrieval_v2_outputs.same_path_identity(self._root, self._root_identity):
            raise PermissionError("lexical cache root changed during index operation")
        if not retrieval_v2_outputs.same_path_identity(self.path, self._file_identity):
            raise PermissionError("lexical index path changed during index operation")

    def _build(self, candidates: Sequence[Candidate]) -> None:
        assert self._connection is not None
        for index_name, tokenizer in self.configuration["indexes"].items():
            self._check_deadline()
            table = f"fts_{index_name}"
            self._connection.execute(
                f"CREATE VIRTUAL TABLE {table} USING fts5(evidence_id UNINDEXED, text, "
                f"tokenize='{tokenizer}')"
            )
            rows = []
            for candidate in candidates:
                self._check_deadline()
                rows.append((candidate.evidence_id, self._index_text(index_name, candidate)))
            self._connection.executemany(
                f"INSERT INTO {table}(evidence_id, text) VALUES (?, ?)", rows
            )
        self._connection.commit()
        self._check_deadline()

    def _segmentation_iterator(self, value: str):
        if len(value) > MAX_SEGMENTATION_INPUT_CHARS:
            raise ValueError("segmentation input limit exceeded")
        try:
            return iter(self._jieba.cut(value, HMM=False))
        except Exception as exc:
            raise ValueError(f"segmentation failed: {exc}") from exc

    def _segment(self, value: str) -> str:
        assert self._jieba is not None
        self._check_deadline()
        tokens = self._segment_tokens(self._segmentation_iterator(value))
        self._check_deadline()
        return " ".join(tokens)

    def _segment_tokens(self, iterator) -> list[str]:
        tokens: list[str] = []
        while True:
            self._check_deadline()
            token = _next_segment(iterator)
            if token is _SEGMENTATION_END:
                return tokens
            self._append_segment_token(tokens, str(token))

    @staticmethod
    def _append_segment_token(tokens: list[str], normalized: str) -> None:
        if not normalized:
            return
        tokens.append(normalized)
        if len(tokens) > MAX_SEGMENTATION_TOKENS:
            raise ValueError("segmentation token limit exceeded")

    def _index_text(self, index_name: str, candidate: Candidate) -> str:
        value = candidate_text(candidate)
        if index_name == "chinese_jieba":
            return self._segment(value)
        if index_name == "chinese_trigram":
            return unicodedata.normalize("NFKC", value).casefold()
        return value

    def _query_terms(self, index_name: str, query_text: str) -> tuple[str, ...]:
        value = self._segment(query_text) if index_name == "chinese_jieba" else query_text
        tokenizer = self.configuration["indexes"][index_name]
        return fts5_tokens(value, tokenizer, deadline=self._deadline)

    def _fts_match_query(self, index_name: str, query_text: str) -> str | None:
        if index_name == "chinese_trigram":
            return _trigram_match_query(query_text)
        terms = self._query_terms(index_name, query_text)
        if not terms:
            return None
        return " OR ".join(f'"{term.replace(chr(34), chr(34) * 2)}"' for term in terms)

    def _bm25_rows(self, index_name: str, fts_query: str, eligible_ids: set[str], limit: int) -> list:
        assert self._connection is not None
        table = f"fts_{index_name}"
        placeholders = ",".join("?" for _ in eligible_ids)
        parameters = [fts_query, *sorted(eligible_ids), limit]
        try:
            return self._connection.execute(
                f"SELECT evidence_id, bm25({table}) AS score FROM {table} "
                f"WHERE {table} MATCH ? AND evidence_id IN ({placeholders}) "
                "ORDER BY score ASC, evidence_id ASC LIMIT ?",
                parameters,
            ).fetchall()
        except sqlite3.Error as exc:
            if time.perf_counter() >= self._deadline:
                raise TimeoutError("lexical benchmark absolute deadline exceeded") from exc
            raise ValueError(f"FTS5 query failed for {index_name}") from exc

    def _rank_index(
        self,
        index_name: str,
        query_text: str,
        eligible_ids: set[str],
        *,
        limit: int,
    ) -> list[tuple[str, float]]:
        self._check_deadline()
        if not eligible_ids:
            return []
        fts_query = self._fts_match_query(index_name, query_text)
        if fts_query is None:
            return []
        rows = self._bm25_rows(index_name, fts_query, eligible_ids, limit)
        self._check_deadline()
        return [(str(evidence_id), float(score)) for evidence_id, score in rows]

    def _active_indexes(self, query_text: str) -> tuple[str, ...]:
        indexes = list(self.configuration["indexes"])
        normalized = unicodedata.normalize("NFKC", query_text).casefold()
        if "chinese_trigram" in indexes and len(normalized) < 3:
            indexes.remove("chinese_trigram")
        return tuple(indexes)

    def _require_candidate_universe(self, candidates: Sequence[Candidate]) -> None:
        supplied: dict[str, Candidate] = {}
        for candidate in candidates:
            normalized = normalized_id(candidate.evidence_id)
            if normalized in supplied:
                raise ValueError("candidate universe mismatch: duplicate normalized id")
            supplied[normalized] = candidate
        if supplied != self._candidate_universe:
            raise ValueError("candidate universe mismatch")

    def _scored(self, rankings: dict[str, list[tuple[str, float]]], limit: int) -> list[ScoredCandidate]:
        if self.configuration["fusion"] is not None:
            fused = reciprocal_rank_fusion(rankings, k=int(self.configuration["fusion"]["k"]))[:limit]
            return [ScoredCandidate(self._candidates[evidence_id], score) for evidence_id, score in fused]
        only_ranking = next(iter(rankings.values()), [])
        return [
            ScoredCandidate(self._candidates[evidence_id], -raw_bm25)
            for evidence_id, raw_bm25 in only_ranking[:limit]
        ]

    def _ranked(
        self, query_text: str, scope: QueryScope, candidates: Sequence[Candidate], limit: int
    ) -> list[ScoredCandidate]:
        self._check_deadline()
        self._require_candidate_universe(candidates)
        eligible_ids = {candidate.evidence_id for candidate in filter_candidates(candidates, scope)}
        rankings = {
            index_name: self._rank_index(index_name, query_text, eligible_ids, limit=limit)
            for index_name in self._active_indexes(query_text)
        }
        return self._scored(rankings, limit)

    def rank(
        self,
        query_text: str,
        scope: QueryScope,
        candidates: Sequence[Candidate],
        *,
        limit: int,
        language: str,
    ) -> list[ScoredCandidate]:
        del language
        try:
            return self._ranked(query_text, scope, candidates, limit)
        except BaseException:
            self._cleanup(remove=True)
            raise

    def close(self) -> None:
        self._cleanup(remove=False)

    def _owns_index(self) -> bool:
        path_owned = retrieval_v2_outputs.same_path_identity(self.path, self._file_identity)
        root_owned = retrieval_v2_outputs.same_path_identity(self._root, self._root_identity)
        return root_owned and path_owned

    def _close_connection(self) -> None:
        if self._connection is None:
            return
        with contextlib.suppress(sqlite3.Error):
            self._connection.set_progress_handler(None, 0)
        with contextlib.suppress(sqlite3.Error):
            self._connection.close()
        self._connection = None

    def _remove_index_files(self) -> None:
        self.path.unlink(missing_ok=True)
        for suffix in ("-journal", "-shm", "-wal"):
            (self._root / f"{self.path.name}{suffix}").unlink(missing_ok=True)

    def _cleanup(self, *, remove: bool) -> None:
        if self._closed:
            return
        owned = self._owns_index()
        self._close_connection()
        os.close(self._descriptor)
        self._closed = True
        if remove and owned:
            self._remove_index_files()
