"""The retrieval v2 SQLite lexical adapters index, fuse and clean up exactly as configured."""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import pytest

from tests.retrieval_v2_helpers import (
    CORPUS,
    SCHEMA,
    PinnedJieba,
    all_normalized,
    lexical_candidate,
)

BENCHMARK = Path(__file__).resolve().parent.parent / "benchmark"
if str(BENCHMARK) not in sys.path:
    sys.path.insert(0, str(BENCHMARK))

import retrieval_v2_adapters  # noqa: E402
import retrieval_v2_contract  # noqa: E402
import retrieval_v2_corpus  # noqa: E402
import retrieval_v2_environment  # noqa: E402
import retrieval_v2_metrics  # noqa: E402
import retrieval_v2_outputs  # noqa: E402
import retrieval_v2_run  # noqa: E402
import retrieval_v2_sqlite_lexical  # noqa: E402


def _trace_scores(report) -> list[float]:
    return [
        item["score"] for trace in report["traces"] for item in trace["ranked_evidence"]
    ]


def _answerable_traces(report, corpus) -> list[dict]:
    unanswerable = {
        query["query_id"]
        for query in corpus["queries"]
        if query["answerability"] == "unanswerable"
    }
    return [trace for trace in report["traces"] if trace["query_id"] not in unanswerable]


def _assert_gold_retrieved(adapter, candidates, query) -> None:
    ranked = adapter.rank(
        query["text"],
        retrieval_v2_metrics.query_scope(query),
        candidates,
        limit=retrieval_v2_contract.MAX_CANDIDATES,
        language=query["language"],
    )
    assert set(query["required_evidence_spans"]) <= {item.evidence_id for item in ranked}


def test_l1_fuses_unicode_and_porter_without_language_routing(tmp_path, monkeypatch):
    candidates = [
        lexical_candidate(
            "a-running", "The runner was running quickly.", language="RU"
        ),
        lexical_candidate("b-noise", "A static unrelated note."),
    ]
    adapter = retrieval_v2_sqlite_lexical.SQLiteLexicalAdapter(candidates, tmp_path, "L1")
    routed = []
    original = adapter._rank_index

    def observe(index_name, *args, **kwargs):
        routed.append(index_name)
        return original(index_name, *args, **kwargs)

    monkeypatch.setattr(adapter, "_rank_index", observe)
    try:
        ranked = adapter.rank(
            "runs",
            retrieval_v2_contract.QueryScope(projects=("alpha",), temporal_mode="current", as_of=None),
            candidates,
            limit=10,
            language="EN",
        )
    finally:
        adapter.close()

    assert routed == ["unicode", "english_porter"]
    assert ranked[0].evidence_id == "a-running"


@pytest.mark.parametrize(
    ("lexical_config", "expected_indexes"),
    [
        ("L0", {"unicode"}),
        ("L1", {"unicode", "english_porter"}),
        ("L2", {"unicode", "chinese_trigram"}),
        ("L3", {"unicode", "chinese_jieba"}),
        ("L4", {"unicode", "english_porter", "chinese_trigram", "chinese_jieba"}),
    ],
)
def test_every_side_index_contains_the_full_candidate_universe(
    tmp_path, lexical_config, expected_indexes
):
    candidates = [
        lexical_candidate("en", "English", language="EN"),
        lexical_candidate("ru", "\u0420\u0443\u0441\u0441\u043a\u0438\u0439", language="RU"),
        lexical_candidate("zh", "\u4e2d\u6587", language="ZH"),
    ]
    adapter = retrieval_v2_sqlite_lexical.SQLiteLexicalAdapter(
        candidates,
        tmp_path / lexical_config,
        lexical_config,
        test_segmenter=PinnedJieba(),
    )
    try:
        counts = {
            index_name: adapter._connection.execute(
                f"SELECT count(*) FROM fts_{index_name}"
            ).fetchone()[0]
            for index_name in expected_indexes
        }
    finally:
        adapter.close()

    assert counts == {index_name: len(candidates) for index_name in expected_indexes}


def test_l2_short_query_uses_unicode_and_long_query_adds_trigram(tmp_path, monkeypatch):
    candidates = [lexical_candidate("zh", "\u7f13\u5b58\u76ee\u5f55", language="ZH")]
    adapter = retrieval_v2_sqlite_lexical.SQLiteLexicalAdapter(candidates, tmp_path, "L2")
    routed = []
    original = adapter._rank_index

    def observe(index_name, *args, **kwargs):
        routed.append(index_name)
        return original(index_name, *args, **kwargs)

    monkeypatch.setattr(adapter, "_rank_index", observe)
    scope = retrieval_v2_contract.QueryScope(projects=("alpha",), temporal_mode="current", as_of=None)
    try:
        adapter.rank("\u7f13\u5b58", scope, candidates, limit=10, language="ZH")
        adapter.rank("\u7f13\u5b58\u76ee", scope, candidates, limit=10, language="ZH")
    finally:
        adapter.close()

    assert routed == ["unicode", "unicode", "chinese_trigram"]


def test_sqlite_trigram_tokenizes_normalized_mixed_query_and_documents_identically(tmp_path):
    candidates = [
        lexical_candidate("mixed", "\u76ee\u5f55ABC cache", language="ZH"),
        lexical_candidate("noise", "\u76ee\u5f55XYZ queue", language="EN"),
    ]
    adapter = retrieval_v2_sqlite_lexical.SQLiteLexicalAdapter(candidates, tmp_path, "L2")
    try:
        ranked = adapter._rank_index(
            "chinese_trigram", "\u76ee\u5f55ＡＢＣ", {"mixed", "noise"}, limit=10
        )
    finally:
        adapter.close()

    assert [evidence_id for evidence_id, _score in ranked] == ["mixed"]


class _FailingSegmenter:
    def cut(self, text, HMM=True):
        del text, HMM
        raise RuntimeError("segmenter failed")


class _ExcessiveSegmenter:
    def __init__(self, count):
        self.count = count

    def cut(self, text, HMM=True):
        del text, HMM
        return ("token" for _ in range(self.count))


class _SlowSegmenter:
    def cut(self, text, HMM=True):
        del HMM
        import time

        time.sleep(0.02)
        return text.split()


def test_l3_applies_pinned_segmentation_identically_to_documents_and_queries(tmp_path):
    jieba = PinnedJieba()
    candidates = [lexical_candidate("zh", "\u7f13\u5b58\u76ee\u5f55", language="ZH")]
    adapter = retrieval_v2_sqlite_lexical.SQLiteLexicalAdapter(candidates, tmp_path, "L3", test_segmenter=jieba)
    scope = retrieval_v2_contract.QueryScope(projects=("alpha",), temporal_mode="current", as_of=None)
    try:
        ranked = adapter.rank("\u7f13\u5b58\u76ee\u5f55", scope, candidates, limit=10, language="ZH")
    finally:
        adapter.close()

    assert ranked[0].evidence_id == "zh"
    assert len(jieba.calls) >= 2
    assert all(hmm is False for _text, hmm in jieba.calls)
    assert jieba.calls[-1] == ("\u7f13\u5b58\u76ee\u5f55", False)


@pytest.mark.parametrize("lexical_config", ["L0", "L1", "L2", "L3", "L4"])
def test_all_lexical_configs_retrieve_cross_language_gold(tmp_path, lexical_config):
    corpus = retrieval_v2_corpus.load_corpus(CORPUS, SCHEMA)
    candidates = retrieval_v2_corpus.build_candidates(corpus)
    adapter = retrieval_v2_sqlite_lexical.SQLiteLexicalAdapter(
        candidates,
        tmp_path / lexical_config,
        lexical_config,
        test_segmenter=PinnedJieba(),
    )
    try:
        _assert_cross_language_gold_retrieved(adapter, candidates, corpus["queries"])
    finally:
        adapter.close()


def _assert_cross_language_gold_retrieved(adapter, candidates, queries):
    for query in queries:
        if not query["cross_language"]:
            continue
        _assert_gold_retrieved(adapter, candidates, query)


@pytest.mark.parametrize("version", ["0.42.0", "0.43.0"])
def test_test_segmenter_never_claims_pinned_version(tmp_path, version):
    jieba = PinnedJieba()
    jieba.__version__ = version
    candidates = [lexical_candidate("zh", "\u7f13\u5b58\u76ee\u5f55", language="ZH")]

    adapter = retrieval_v2_sqlite_lexical.SQLiteLexicalAdapter(candidates, tmp_path, "L3", test_segmenter=jieba)
    try:
        assert adapter.segmentation_runtime == {
            "provenance": "injected-test",
            "quality_evidence": False,
            "version": None,
            "dictionary_sha256": None,
        }
    finally:
        adapter.close()


def test_l3_fails_closed_when_pinned_tokenizer_is_unavailable(tmp_path, monkeypatch):
    candidates = [lexical_candidate("zh", "\u7f13\u5b58\u76ee\u5f55", language="ZH")]
    monkeypatch.setattr(
        retrieval_v2_environment,
        "load_pinned_jieba",
        lambda cache_root: (_ for _ in ()).throw(
            ValueError("requires installed jieba 0.42.1")
        ),
    )

    with pytest.raises(ValueError, match="jieba 0.42.1"):
        retrieval_v2_sqlite_lexical.SQLiteLexicalAdapter(candidates, tmp_path, "L3")


def test_injected_segmenter_reports_test_only_provenance(tmp_path):
    corpus = retrieval_v2_corpus.load_corpus(CORPUS, SCHEMA)

    report = retrieval_v2_run.run_benchmark(
        corpus,
        cache_root=tmp_path,
        lexical_config="L3",
        test_segmenter=PinnedJieba(),
    )

    assert report["methodology"]["segmentation_runtime"] == {
        "provenance": "injected-test",
        "quality_evidence": False,
        "version": None,
        "dictionary_sha256": None,
    }


@pytest.mark.parametrize("lexical_config", ["L3", "L4"])
def test_real_jieba_cache_stays_inside_isolated_cache_and_global_dt_is_untouched(
    tmp_path, monkeypatch, lexical_config
):
    jieba = pytest.importorskip("jieba")
    assert jieba.dt.initialized is False
    external = tmp_path / "external-temp"
    external.mkdir()
    sentinel = external / "sentinel"
    sentinel.write_text("unchanged", encoding="utf-8")
    before = sorted(path.name for path in external.iterdir())
    created = []
    real_mkstemp = jieba.tempfile.mkstemp

    def monitored_mkstemp(*args, **kwargs):
        descriptor, path = real_mkstemp(*args, **kwargs)
        created.append(Path(path).resolve())
        return descriptor, path

    monkeypatch.setattr(jieba.tempfile, "gettempdir", lambda: str(external))
    monkeypatch.setattr(jieba.tempfile, "mkstemp", monitored_mkstemp)
    cache_root = tmp_path / f"cache-{lexical_config}"
    candidates = [
        lexical_candidate("zh", "缓存目录", language="ZH")
    ]

    adapter = retrieval_v2_sqlite_lexical.SQLiteLexicalAdapter(candidates, cache_root, lexical_config)
    try:
        assert adapter._jieba is not jieba.dt
        assert adapter._jieba.initialized is True
        jieba_cache = (cache_root / "jieba").resolve()
        assert Path(adapter.segmentation_runtime["cache_path"]).parent == jieba_cache
        assert created
        assert all(path.parent == jieba_cache for path in created)
        assert list(jieba_cache.glob("*.cache"))
        assert jieba.dt.initialized is False
        assert sorted(path.name for path in external.iterdir()) == before
        assert sentinel.read_text(encoding="utf-8") == "unchanged"
    finally:
        adapter.close()
    assert list((cache_root / "jieba").glob("*.cache"))
    assert jieba.dt.initialized is False


@pytest.mark.parametrize("lexical_config", ["L1", "L2", "L3", "L4"])
def test_fused_scores_preserve_downstream_abstention_contract(tmp_path, lexical_config):
    corpus = retrieval_v2_corpus.load_corpus(CORPUS, SCHEMA)

    report = retrieval_v2_run.run_benchmark(
        corpus,
        cache_root=tmp_path / lexical_config,
        lexical_config=lexical_config,
        test_segmenter=PinnedJieba(),
    )

    scores = _trace_scores(report)
    answerable = _answerable_traces(report, corpus)
    assert scores and all_normalized(scores)
    assert any(not trace["abstained"] for trace in answerable)
    assert report["overall"]["no_answer_false_answer_rate"] == pytest.approx(1 / 3)
    assert report["gates"]["qa_contract_passed"] is False
    assert report["quality_claim"] is False


def test_rank_rejects_changed_or_incomplete_candidate_universe(tmp_path):
    candidates = [
        lexical_candidate("one", "catalog"),
        lexical_candidate("two", "queue"),
    ]
    changed = retrieval_v2_contract.Candidate(**{**candidates[0].__dict__, "text": "changed"})
    scope = retrieval_v2_contract.QueryScope(projects=("alpha",), temporal_mode="current", as_of=None)
    adapter = retrieval_v2_sqlite_lexical.SQLiteLexicalAdapter(candidates, tmp_path / "changed", "L0")

    with pytest.raises(ValueError, match="candidate universe mismatch"):
        adapter.rank("catalog", scope, [changed, candidates[1]], limit=10, language="EN")
    assert not adapter.path.exists()

    retry = retrieval_v2_sqlite_lexical.SQLiteLexicalAdapter(candidates, tmp_path / "changed", "L0")
    try:
        with pytest.raises(ValueError, match="candidate universe mismatch"):
            retry.rank("catalog", scope, candidates[:1], limit=10, language="EN")
    finally:
        retry.close()


def test_constructor_rejects_duplicate_normalized_candidate_ids(tmp_path):
    candidates = [
        lexical_candidate("Evidence", "one"),
        lexical_candidate("EVIDENCE", "two"),
    ]

    with pytest.raises(ValueError, match="duplicate normalized candidate id"):
        retrieval_v2_sqlite_lexical.SQLiteLexicalAdapter(candidates, tmp_path, "L0")
    assert not (tmp_path / "lexical-L0.sqlite3").exists()


def test_lexical_index_rejects_preexisting_symlink_and_preserves_target(tmp_path):
    candidates = [lexical_candidate("one", "catalog")]
    cache = tmp_path / "cache"
    cache.mkdir()
    protected = tmp_path / "protected"
    protected.write_text("unchanged", encoding="utf-8")
    index = cache / "lexical-L0.sqlite3"
    try:
        index.symlink_to(protected)
    except OSError:
        pytest.skip("symlink creation unavailable")

    with pytest.raises((FileExistsError, ValueError)):
        retrieval_v2_sqlite_lexical.SQLiteLexicalAdapter(candidates, cache, "L0")
    assert protected.read_text(encoding="utf-8") == "unchanged"
    assert index.is_symlink()


def test_lexical_index_rejects_symlink_cache_root(tmp_path):
    candidates = [lexical_candidate("one", "catalog")]
    real_cache = tmp_path / "real-cache"
    real_cache.mkdir()
    linked_cache = tmp_path / "linked-cache"
    try:
        linked_cache.symlink_to(real_cache, target_is_directory=True)
    except OSError:
        pytest.skip("directory symlink creation unavailable")

    with pytest.raises(ValueError, match="cache root.*symlink"):
        retrieval_v2_sqlite_lexical.SQLiteLexicalAdapter(candidates, linked_cache, "L0")
    assert not (real_cache / "lexical-L0.sqlite3").exists()


def test_lexical_index_rejects_cache_identity_race_and_allows_retry(tmp_path, monkeypatch):
    candidates = [lexical_candidate("one", "catalog")]
    cache = tmp_path / "cache"
    original = retrieval_v2_outputs.same_path_identity
    raced = False

    def race_once(path, expected):
        nonlocal raced
        if Path(path) == cache.resolve() and not raced:
            raced = True
            return False
        return original(path, expected)

    monkeypatch.setattr(retrieval_v2_outputs, "same_path_identity", race_once)
    with pytest.raises(PermissionError, match="cache root changed"):
        retrieval_v2_sqlite_lexical.SQLiteLexicalAdapter(candidates, cache, "L0")
    assert not (cache / "lexical-L0.sqlite3").exists()

    monkeypatch.undo()
    retry = retrieval_v2_sqlite_lexical.SQLiteLexicalAdapter(candidates, cache, "L0")
    retry.close()
    assert retry.path.exists()


def test_lexical_index_is_owner_only_on_posix(tmp_path):
    if os.name != "posix":
        pytest.skip("POSIX mode bits unavailable")
    candidates = [lexical_candidate("one", "catalog")]
    adapter = retrieval_v2_sqlite_lexical.SQLiteLexicalAdapter(candidates, tmp_path, "L0")
    try:
        assert adapter.path.stat().st_mode & 0o077 == 0
    finally:
        adapter.close()


def test_query_deadline_removes_index_and_allows_retry(tmp_path):
    candidates = [lexical_candidate("one", "catalog")]
    scope = retrieval_v2_contract.QueryScope(projects=("alpha",), temporal_mode="current", as_of=None)
    # Build under a generous deadline — a loaded runner can spend longer than
    # any short budget on the index itself — then expire it for the query.
    adapter = retrieval_v2_sqlite_lexical.SQLiteLexicalAdapter(candidates, tmp_path, "L0")
    adapter._deadline = time.perf_counter() - 0.01

    with pytest.raises(TimeoutError, match="deadline"):
        adapter.rank("catalog", scope, candidates, limit=10, language="EN")
    assert not adapter.path.exists()

    retry = retrieval_v2_sqlite_lexical.SQLiteLexicalAdapter(candidates, tmp_path, "L0")
    retry.close()
    assert retry.path.exists()


def test_segmentation_build_obeys_absolute_deadline_and_cleans_up(tmp_path):
    candidates = [lexical_candidate("one", "\u7f13\u5b58\u76ee\u5f55", language="ZH")]

    with pytest.raises(TimeoutError, match="deadline"):
        retrieval_v2_sqlite_lexical.SQLiteLexicalAdapter(
            candidates,
            tmp_path,
            "L3",
            test_segmenter=_SlowSegmenter(),
            deadline_seconds=0.005,
        )
    assert not (tmp_path / "lexical-L3.sqlite3").exists()


@pytest.mark.parametrize(
    ("segmenter", "error"),
    [
        (_FailingSegmenter(), "segmentation failed"),
        (_ExcessiveSegmenter(10_001), "token limit"),
    ],
)
def test_segmentation_failure_removes_partial_index_and_allows_retry(
    tmp_path, segmenter, error
):
    candidates = [lexical_candidate("one", "\u7f13\u5b58\u76ee\u5f55", language="ZH")]

    with pytest.raises(ValueError, match=error):
        retrieval_v2_sqlite_lexical.SQLiteLexicalAdapter(
            candidates, tmp_path, "L3", test_segmenter=segmenter
        )
    assert not (tmp_path / "lexical-L3.sqlite3").exists()

    retry = retrieval_v2_sqlite_lexical.SQLiteLexicalAdapter(
        candidates, tmp_path, "L3", test_segmenter=PinnedJieba()
    )
    retry.close()
    assert retry.path.exists()


def test_segmentation_input_is_bounded_before_calling_segmenter(tmp_path):
    segmenter = PinnedJieba()
    candidates = [
        lexical_candidate(
            "one",
            "x" * (retrieval_v2_contract.MAX_SEGMENTATION_INPUT_CHARS + 1),
            language="ZH",
        )
    ]

    with pytest.raises(ValueError, match="input limit"):
        retrieval_v2_sqlite_lexical.SQLiteLexicalAdapter(
            candidates, tmp_path, "L3", test_segmenter=segmenter
        )
    assert segmenter.calls == []
    assert not (tmp_path / "lexical-L3.sqlite3").exists()


def test_run_failure_closes_and_removes_lexical_index_for_retry(tmp_path, monkeypatch):
    corpus = retrieval_v2_corpus.load_corpus(CORPUS, SCHEMA)
    cache = tmp_path / "cache"
    monkeypatch.setattr(
        retrieval_v2_adapters.FakeQAAdapter,
        "answer",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("qa failed")),
    )

    with pytest.raises(RuntimeError, match="qa failed"):
        retrieval_v2_run.run_benchmark(corpus, cache_root=cache, lexical_config="L1")
    assert not (cache / "lexical-L1.sqlite3").exists()

    monkeypatch.undo()
    report = retrieval_v2_run.run_benchmark(corpus, cache_root=cache, lexical_config="L1")
    assert report["quality_claim"] is False


def test_sqlite_lexical_filters_match_legacy_hard_filter_scope(tmp_path):
    candidates = [
        lexical_candidate("eligible", "catalog", project="alpha"),
        lexical_candidate("wrong-project", "catalog", project="beta"),
        retrieval_v2_contract.Candidate(
            **{
                **lexical_candidate("superseded", "catalog", project="alpha").__dict__,
                "status": "superseded",
                "valid_to": "2025-06-01",
            }
        ),
    ]
    scope = retrieval_v2_contract.QueryScope(projects=("alpha",), temporal_mode="current", as_of=None)
    adapter = retrieval_v2_sqlite_lexical.SQLiteLexicalAdapter(candidates, tmp_path, "L0")
    try:
        ranked = adapter.rank("catalog", scope, candidates, limit=10, language="EN")
    finally:
        adapter.close()

    assert [item.evidence_id for item in ranked] == ["eligible"]
    assert {item.evidence_id for item in ranked} == {
        item.evidence_id for item in retrieval_v2_corpus.filter_candidates(candidates, scope)
    }


@pytest.mark.parametrize("lexical_config", ["L0", "L1", "L2", "L3", "L4"])
def test_explicit_lexical_run_is_isolated_and_reports_config_metrics(
    tmp_path, lexical_config
):
    corpus = retrieval_v2_corpus.load_corpus(CORPUS, SCHEMA)
    cache = tmp_path / lexical_config

    report = retrieval_v2_run.run_benchmark(
        corpus,
        cache_root=cache,
        lexical_config=lexical_config,
        test_segmenter=PinnedJieba(),
    )
    database = cache / f"lexical-{lexical_config}.sqlite3"

    assert database.is_file()
    assert not (cache / "fake-index.json").exists()
    assert report["methodology"]["lexical_configuration"] == (
        retrieval_v2_contract.LEXICAL_CONFIGURATIONS[lexical_config]
    )
    assert report["measurements"]["index_size_bytes"] == database.stat().st_size
    assert report["measurements"]["build_time_ms"] >= 0
    assert report["measurements"]["latency_p50_ms"] >= 0
    assert report["measurements"]["cold_first_query_latency_ms"] >= 0
    assert report["measurements"]["warm_latency_p50_ms"] >= 0
    assert report["measurements"]["warm_latency_p95_ms"] >= 0
    assert report["measurements"]["indexing_throughput_chunks_per_second"] > 0
    assert set(report["measurements"]["measurement_status"].values()) <= {
        "measured",
        "unavailable",
    }
    assert set(report["slices"]) == {"EN", "RU", "ZH", "cross-language"}
    assert set(report) == retrieval_v2_contract.REPORT_FIELDS
    assert report["corpus_sha256"] == retrieval_v2_environment.sha256_file(CORPUS)
    assert report["matrix_sha256"] == retrieval_v2_environment.sha256_file(BENCHMARK / "model-matrix-v1.json")
    assert report["benchmark_runner_sha256"] == retrieval_v2_environment.runner_sha256()
    assert report["benchmark_contract_sha256"] == retrieval_v2_environment.sha256_json(
        json.loads((BENCHMARK / "model-matrix-v1.json").read_bytes())["benchmark_contract"]
    )


def test_explicit_lexical_ablation_does_not_invoke_dense_or_reranker(
    tmp_path, monkeypatch
):
    corpus = retrieval_v2_corpus.load_corpus(CORPUS, SCHEMA)

    def forbidden(*args, **kwargs):
        raise AssertionError("dense model or reranker invoked before lexical ablation")

    monkeypatch.setattr(retrieval_v2_adapters.FakeEmbeddingAdapter, "rank", forbidden)
    monkeypatch.setattr(retrieval_v2_adapters.FakeRerankerAdapter, "rank", forbidden)

    report = retrieval_v2_run.run_benchmark(corpus, cache_root=tmp_path, lexical_config="L0")

    assert report["methodology"]["retrieval_order"].startswith("BM25-only")


def test_legacy_default_does_not_build_or_report_lexical_ablations(tmp_path):
    corpus = retrieval_v2_corpus.load_corpus(CORPUS, SCHEMA)

    report = retrieval_v2_run.run_benchmark(corpus, cache_root=tmp_path)

    assert (tmp_path / "fake-index.json").is_file()
    assert not list(tmp_path.glob("lexical-*.sqlite3"))
    assert "lexical_configuration" not in report["methodology"]
    assert report["adapter_kind"] == "deterministic-fake"
