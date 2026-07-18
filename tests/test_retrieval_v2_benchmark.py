from __future__ import annotations

import hashlib
import importlib.util
import json
import math
import os
import re
import subprocess
import sys
import weakref
from collections import Counter
from datetime import date
from pathlib import Path

import pytest
from reliable_memory import SchemaValidationError, canonical_json_bytes, validate_schema

ROOT = Path(__file__).resolve().parent.parent
BENCHMARK = ROOT / "benchmark"
CORPUS = BENCHMARK / "retrieval-v2.json"
SCHEMA = BENCHMARK / "retrieval-v2.schema.json"
RUNNER = BENCHMARK / "run_retrieval_v2.py"


def _runner_module():
    spec = importlib.util.spec_from_file_location("run_retrieval_v2", RUNNER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _effectiveness(report: dict) -> dict:
    copy = json.loads(json.dumps(report))
    copy["measurements"] = {
        "build_time_ms": "measured",
        "latency_p50_ms": "measured",
        "latency_p95_ms": "measured",
        "peak_rss_bytes": "measured-or-unavailable",
    }
    for trace in copy["traces"]:
        trace.pop("latency_ms", None)
    return copy


def test_required_v2_artifacts_exist():
    assert SCHEMA.is_file()
    assert CORPUS.is_file()
    assert RUNNER.is_file()


def test_schema_is_closed_at_every_object_level():
    schema = json.loads(SCHEMA.read_text(encoding="utf-8"))

    def visit(rule: object) -> None:
        if isinstance(rule, dict):
            if rule.get("type") == "object":
                assert rule.get("additionalProperties") is False
                assert set(rule.get("required", ())) == set(rule.get("properties", ()))
            for value in rule.values():
                visit(value)
        elif isinstance(rule, list):
            for value in rule:
                visit(value)

    visit(schema)
    assert "$ref" not in SCHEMA.read_text(encoding="utf-8")


def test_corpus_is_canonical_closed_and_semantically_valid():
    runner = _runner_module()
    raw = CORPUS.read_bytes()
    corpus = json.loads(raw)

    validate_schema(corpus, SCHEMA)
    assert canonical_json_bytes(corpus) + b"\n" == raw
    assert runner.load_corpus(CORPUS, SCHEMA) == corpus


def test_corpus_has_balanced_public_synthetic_coverage():
    corpus = json.loads(CORPUS.read_text(encoding="utf-8"))
    queries = corpus["queries"]
    languages = Counter(query["language"] for query in queries)
    assert all(languages[language] >= 6 for language in ("EN", "RU", "ZH"))
    assert sum(query["cross_language"] for query in queries) >= 3
    for language in ("EN", "RU", "ZH"):
        language_queries = [query for query in queries if query["language"] == language]
        assert {query["answerability"] for query in language_queries} == {
            "answerable",
            "unanswerable",
        }
    required_types = {
        "exact-name",
        "relative-path",
        "command",
        "code-symbol",
        "paraphrase",
        "cross-language",
        "temporal-decision",
        "supersession",
        "contradiction",
        "multi-parent-synthesis",
        "no-answer",
        "prompt-injection",
    }
    assert required_types <= {query["query_type"] for query in queries}
    assert corpus["description"].lower().startswith("public synthetic")
    assert all(
        document["relative_path"].startswith("synthetic/")
        for document in corpus["documents"]
    )


def test_loader_verifies_offsets_hashes_references_and_privacy(tmp_path):
    runner = _runner_module()
    corpus = json.loads(CORPUS.read_text(encoding="utf-8"))
    document = corpus["documents"][0]
    span = document["evidence_spans"][0]
    encoded = document["parent_text"].encode("utf-8")
    assert encoded[span["byte_start"] : span["byte_end"]].decode("utf-8") == span["text"]

    bad_cases = []
    bad_hash = json.loads(json.dumps(corpus))
    bad_hash["documents"][0]["evidence_spans"][0]["span_sha256"] = "0" * 64
    bad_cases.append(bad_hash)
    bad_ref = json.loads(json.dumps(corpus))
    bad_ref["queries"][0]["required_evidence_spans"][0] = "missing-evidence"
    bad_cases.append(bad_ref)
    private_path = json.loads(json.dumps(corpus))
    private_path["documents"][0]["relative_path"] = "C:/Users/Alice/private.md"
    bad_cases.append(private_path)
    duplicate_id = json.loads(json.dumps(corpus))
    duplicate_id["documents"][1]["parent_id"] = duplicate_id["documents"][0]["parent_id"].upper()
    bad_cases.append(duplicate_id)
    overlap = json.loads(json.dumps(corpus))
    first_span = overlap["documents"][0]["evidence_spans"][0]
    overlap["documents"][0]["evidence_spans"].append(dict(first_span, evidence_id="overlap"))
    bad_cases.append(overlap)
    query_leak = json.loads(json.dumps(corpus))
    query_leak["queries"][0]["answer_text"] = "gold answer"
    bad_cases.append(query_leak)

    for index, bad in enumerate(bad_cases):
        path = tmp_path / f"bad-{index}.json"
        path.write_bytes(canonical_json_bytes(bad) + b"\n")
        with pytest.raises((ValueError, SchemaValidationError)):
            runner.load_corpus(path, SCHEMA)


def test_loader_rejects_answerability_temporal_and_project_inconsistency(tmp_path):
    runner = _runner_module()
    corpus = json.loads(CORPUS.read_text(encoding="utf-8"))
    bad = json.loads(json.dumps(corpus))
    query = next(item for item in bad["queries"] if item["answerability"] == "answerable")
    query["relevant_parents"] = []
    path = tmp_path / "bad-answerability.json"
    path.write_bytes(canonical_json_bytes(bad) + b"\n")
    with pytest.raises((ValueError, SchemaValidationError)):
        runner.load_corpus(path, SCHEMA)

    bad = json.loads(json.dumps(corpus))
    query = next(item for item in bad["queries"] if item["answerability"] == "answerable")
    query["project_scope"] = ["unrelated-project"]
    path.write_bytes(canonical_json_bytes(bad) + b"\n")
    with pytest.raises(ValueError, match="project"):
        runner.load_corpus(path, SCHEMA)


def test_loader_rejects_extra_relevant_parent_without_required_evidence(tmp_path):
    runner = _runner_module()
    corpus = json.loads(CORPUS.read_text(encoding="utf-8"))
    query = next(item for item in corpus["queries"] if item["answerability"] == "answerable")
    extra = next(
        document["parent_id"]
        for document in corpus["documents"]
        if document["parent_id"] not in query["relevant_parents"]
    )
    query["relevant_parents"].append(extra)
    path = tmp_path / "extra-relevant-parent.json"
    path.write_bytes(canonical_json_bytes(corpus) + b"\n")

    with pytest.raises(ValueError, match="exactly match required evidence parents"):
        runner.load_corpus(path, SCHEMA)


def test_loader_rejects_missing_relevant_parent_for_required_evidence(tmp_path):
    runner = _runner_module()
    corpus = json.loads(CORPUS.read_text(encoding="utf-8"))
    query = next(item for item in corpus["queries"] if len(item["relevant_parents"]) > 1)
    query["relevant_parents"].pop()
    path = tmp_path / "missing-relevant-parent.json"
    path.write_bytes(canonical_json_bytes(corpus) + b"\n")

    with pytest.raises(ValueError, match="exactly match required evidence parents"):
        runner.load_corpus(path, SCHEMA)


def test_loader_rejects_invalid_dates_intervals_and_unscoped_negatives(tmp_path):
    runner = _runner_module()
    corpus = json.loads(CORPUS.read_text(encoding="utf-8"))

    invalid_date = json.loads(json.dumps(corpus))
    invalid_date["documents"][0]["valid_from"] = "2025-02-31"
    path = tmp_path / "invalid-date.json"
    path.write_bytes(canonical_json_bytes(invalid_date) + b"\n")
    with pytest.raises(ValueError, match="date"):
        runner.load_corpus(path, SCHEMA)

    reversed_interval = json.loads(json.dumps(corpus))
    superseded = next(item for item in reversed_interval["documents"] if item["valid_to"])
    superseded["valid_to"] = date.fromisoformat(superseded["valid_from"]).isoformat()
    path = tmp_path / "reversed-interval.json"
    path.write_bytes(canonical_json_bytes(reversed_interval) + b"\n")
    with pytest.raises(ValueError, match="interval"):
        runner.load_corpus(path, SCHEMA)

    no_eligible_negative = json.loads(json.dumps(corpus))
    query = next(item for item in no_eligible_negative["queries"] if item["query_id"] == "q-en-temporal")
    query["negative_candidates"] = ["aurora-rollback-current"]
    path = tmp_path / "unscoped-negative.json"
    path.write_bytes(canonical_json_bytes(no_eligible_negative) + b"\n")
    with pytest.raises(ValueError, match="eligible negative"):
        runner.load_corpus(path, SCHEMA)


@pytest.mark.parametrize(
    ("field", "duplicate"),
    [
        ("relevant_parents", None),
        ("required_evidence_spans", None),
        ("negative_candidates", None),
        ("graded_evidence", {"gain": 1}),
        ("graded_evidence", {"gain": 3}),
    ],
)
def test_loader_rejects_duplicate_query_candidate_ids(tmp_path, field, duplicate):
    runner = _runner_module()
    corpus = json.loads(CORPUS.read_text(encoding="utf-8"))
    query = next(item for item in corpus["queries"] if item["answerability"] == "answerable")
    if field == "graded_evidence":
        item = dict(query[field][0])
        item.update(duplicate)
    else:
        item = query[field][0]
    query[field].append(item)
    path = tmp_path / f"duplicate-{field}.json"
    path.write_bytes(canonical_json_bytes(corpus) + b"\n")

    with pytest.raises(ValueError, match=f"duplicate normalized {field.replace('_', ' ')}"):
        runner.load_corpus(path, SCHEMA)


def test_hand_calculated_fractional_metrics():
    runner = _runner_module()
    ranked = ["noise", "e2", "e1", "other"]
    relevant = {"e1", "e2", "e3"}

    assert runner.recall_at_k(ranked, relevant, 2) == pytest.approx(1 / 3)
    assert runner.recall_at_k(ranked, relevant, 3) == pytest.approx(2 / 3)
    assert runner.all_required_at_k(ranked, relevant, 3) == 0.0
    assert runner.all_required_at_k(ranked + ["e3"], relevant, 5) == 1.0
    assert runner.parent_recall_at_k(["p2", "x", "p1"], {"p1", "p2", "p3"}, 2) == pytest.approx(1 / 3)
    assert runner.reciprocal_rank_at_k(ranked, relevant, 10) == 0.5
    assert runner.reciprocal_rank_at_k(ranked, {"missing"}, 10) == 0.0

    gains = {"e1": 3, "e2": 2, "e3": 1}
    expected_dcg = 2 / math.log2(3) + 3 / math.log2(4)
    ideal_dcg = 3 + 2 / math.log2(3) + 1 / math.log2(4)
    assert runner.ndcg_at_k(ranked, gains, 10) == pytest.approx(expected_dcg / ideal_dcg)


def test_false_answer_macro_and_language_gap_math():
    runner = _runner_module()
    traces = [
        {"answerability": "unanswerable", "abstained": False, "language": "EN"},
        {"answerability": "unanswerable", "abstained": True, "language": "EN"},
        {"answerability": "answerable", "abstained": False, "language": "RU"},
    ]
    assert runner.false_answer_rate(traces) == 0.5
    assert runner.false_answer_rate([traces[-1]]) is None
    slices = {
        "EN": {"parent_recall_at_10": None},
        "RU": {"parent_recall_at_10": 0.96},
        "ZH": {"parent_recall_at_10": 0.98},
    }
    slices["cross-language"] = {"parent_recall_at_10": 1.0}
    assert runner.macro_average(slices, "parent_recall_at_10") == pytest.approx(0.98)
    slices["EN"]["parent_recall_at_10"] = 0.94
    assert runner.language_gate_gaps(slices, "parent_recall_at_10", 0.95) == {
        "EN": pytest.approx(-0.01),
        "RU": pytest.approx(0.01),
        "ZH": pytest.approx(0.03),
    }

    passing = {
        "parent_recall_at_10": 1.0,
        "all_required_evidence_recall_at_20": 1.0,
        "ndcg_at_10": 1.0,
        "mrr_at_10": 1.0,
        "no_answer_false_answer_rate": 0.0,
    }
    gate_slices = {language: dict(passing) for language in ("EN", "RU", "ZH")}
    gate_slices["EN"]["no_answer_false_answer_rate"] = 0.04
    assert runner._gate_results(passing, gate_slices)["language_results"]["EN"] is False


def test_aggregate_marks_positive_metrics_unavailable_without_answerable_queries():
    runner = _runner_module()
    aggregate = runner._aggregate(
        [{"answerability": "unanswerable", "abstained": True}]
    )

    assert aggregate["query_count"] == 1
    assert aggregate["positive_query_count"] == 0
    assert aggregate["no_answer_query_count"] == 1
    for metric in runner.EFFECTIVENESS_FIELDS:
        if metric != "no_answer_false_answer_rate":
            assert aggregate[metric] is None
    assert aggregate["no_answer_false_answer_rate"] == 0.0


def test_fake_adapter_interfaces_do_not_receive_gold_metadata():
    runner = _runner_module()
    corpus = runner.load_corpus(CORPUS, SCHEMA)
    candidates = runner.build_candidates(corpus)
    scope = runner.QueryScope(projects=("aurora",), temporal_mode="current", as_of=None)
    lexical = runner.FakeLexicalAdapter()
    embedding = runner.FakeEmbeddingAdapter()
    reranker = runner.FakeRerankerAdapter()
    qa = runner.FakeQAAdapter()

    lexical_results = lexical.rank("cache cleanup command", scope, candidates, limit=50)
    embedding_results = embedding.rank("cache cleanup command", scope, candidates, limit=50)
    reranked = reranker.rank(
        "cache cleanup command", scope, lexical_results + embedding_results, limit=50
    )
    answer = qa.answer("cache cleanup command", scope, reranked)

    assert lexical_results and embedding_results and reranked
    assert set(answer) == {"abstained", "reason"}
    for adapter in (lexical, embedding, reranker, qa):
        assert not hasattr(adapter, "corpus")
        assert not hasattr(adapter, "queries")


def test_prompt_injection_is_inert_untrusted_candidate_data(tmp_path):
    runner = _runner_module()
    corpus = runner.load_corpus(CORPUS, SCHEMA)
    injection = next(
        candidate
        for candidate in runner.build_candidates(corpus)
        if "ignore previous instructions" in candidate.text.lower()
    )
    before = dict(os.environ)
    report = runner.run_benchmark(corpus, cache_root=tmp_path / "cache")

    assert injection.evidence_id in {
        item["evidence_id"] for trace in report["traces"] for item in trace["ranked_evidence"]
    }
    assert dict(os.environ) == before
    assert set(report) == runner.REPORT_FIELDS
    assert report["adapter_kind"] == "deterministic-fake"
    assert report["quality_claim"] is False


def test_fake_run_is_deterministic_except_measured_resources(tmp_path):
    runner = _runner_module()
    corpus = runner.load_corpus(CORPUS, SCHEMA)
    first = runner.run_benchmark(corpus, cache_root=tmp_path / "one")
    second = runner.run_benchmark(corpus, cache_root=tmp_path / "two")

    assert _effectiveness(first) == _effectiveness(second)


def test_report_has_every_metric_slice_gate_and_trace(tmp_path):
    runner = _runner_module()
    corpus = runner.load_corpus(CORPUS, SCHEMA)
    report = runner.run_benchmark(corpus, cache_root=tmp_path / "cache")
    required_metrics = {
        "evidence_recall_at_10",
        "evidence_recall_at_20",
        "evidence_recall_at_50",
        "all_required_evidence_recall_at_20",
        "parent_recall_at_10",
        "ndcg_at_10",
        "mrr_at_10",
        "no_answer_false_answer_rate",
    }
    assert required_metrics <= report["overall"].keys()
    assert set(report["slices"]) == {"EN", "RU", "ZH", "cross-language"}
    assert set(report["macro_average"]) == required_metrics
    assert report["thresholds"] == {
        "parent_recall_at_10": 0.95,
        "all_required_evidence_recall_at_20": 0.85,
        "ndcg_at_10": 0.80,
        "mrr_at_10": 0.85,
        "max_language_gate_gap": 0.03,
        "no_answer_false_answer_rate": 0.03,
    }
    assert report["gates"]["release_evidence"] is False
    assert report["gates"]["interpretation"] == "orchestration-only"
    assert report["gates"]["passed_for_orchestration"] is True
    assert report["gates"]["qa_contract_passed"] is True
    assert report["overall"]["no_answer_false_answer_rate"] == 0.0
    assert report["slices"]["cross-language"]["no_answer_false_answer_rate"] is None
    assert report["macro_average"]["no_answer_false_answer_rate"] == 0.0
    assert "represented as JSON null" in report["methodology"]["unavailable_metrics"]
    json.dumps(report, allow_nan=False)
    assert all(trace["abstention_contract_valid"] for trace in report["traces"])
    assert len(report["traces"]) == len(corpus["queries"])
    assert {trace["query_id"] for trace in report["traces"]} == {
        query["query_id"] for query in corpus["queries"]
    }
    assert set(report["measurements"]) == {
        "latency_p50_ms",
        "latency_p95_ms",
        "cold_first_query_latency_ms",
        "warm_latency_p50_ms",
        "warm_latency_p95_ms",
        "peak_rss_bytes",
        "peak_rss_status",
            "build_time_ms",
            "lexical_build_ms",
        "indexing_throughput_chunks_per_second",
        "index_size_bytes",
        "measurement_status",
    }
    assert report["measurements"]["index_size_bytes"] == (
        tmp_path / "cache" / "fake-index.json"
    ).stat().st_size


def test_filters_apply_before_scoring_and_keep_eligible_negatives():
    runner = _runner_module()
    corpus = runner.load_corpus(CORPUS, SCHEMA)
    candidates = runner.build_candidates(corpus)
    current = runner.filter_candidates(
        candidates,
        runner.QueryScope(projects=("aurora",), temporal_mode="current", as_of=None),
    )
    historical = runner.filter_candidates(
        candidates,
        runner.QueryScope(projects=("aurora",), temporal_mode="as_of", as_of="2025-03-01"),
    )
    assert current
    assert all(candidate.project == "aurora" for candidate in current)
    assert all(candidate.status == "active" for candidate in current)
    assert any(candidate.status == "superseded" for candidate in historical)
    for query in corpus["queries"]:
        scoped = runner.filter_candidates(candidates, runner._query_scope(query))
        assert set(query["negative_candidates"]) & {
            candidate.evidence_id for candidate in scoped
        }


def test_adapters_never_score_candidates_excluded_by_hard_filters(monkeypatch):
    runner = _runner_module()
    corpus = runner.load_corpus(CORPUS, SCHEMA)
    candidates = runner.build_candidates(corpus)
    scope = runner.QueryScope(projects=("aurora",), temporal_mode="current", as_of=None)
    eligible = {candidate.evidence_id for candidate in runner.filter_candidates(candidates, scope)}
    scored = []
    original = runner._lexical_score

    def observe(query_text, candidate):
        scored.append(candidate.evidence_id)
        return original(query_text, candidate)

    monkeypatch.setattr(runner, "_lexical_score", observe)
    runner.FakeLexicalAdapter().rank("catalog", scope, candidates, limit=50)

    assert set(scored) == eligible

    reranker_scored = []

    def observe_reranker(query_text, candidate):
        reranker_scored.append(candidate.evidence_id)
        return original(query_text, candidate)

    monkeypatch.setattr(runner, "_lexical_score", observe_reranker)
    supplied = [runner.ScoredCandidate(candidate, 0.0) for candidate in candidates]
    runner.FakeRerankerAdapter().rank("catalog", scope, supplied, limit=50)

    assert set(reranker_scored) == eligible


def test_cross_language_queries_require_language_transfer_not_filename_overlap():
    runner = _runner_module()
    corpus = runner.load_corpus(CORPUS, SCHEMA)
    evidence_languages = {
        span["evidence_id"]: document["language"]
        for document in corpus["documents"]
        for span in document["evidence_spans"]
    }
    cross_language = [query for query in corpus["queries"] if query["cross_language"]]

    assert len(cross_language) >= 3
    assert all(
        evidence_languages[evidence_id] != query["language"]
        for query in cross_language
        for evidence_id in query["required_evidence_spans"]
    )
    assert all(not re.search(r"\b[\w-]+\.(?:py|sqlite3|md)\b", query["text"]) for query in cross_language)


def test_cache_is_isolated_and_no_network_or_model_access(tmp_path, monkeypatch):
    runner = _runner_module()
    corpus = runner.load_corpus(CORPUS, SCHEMA)
    source_cache = ROOT / "cache"
    source_cache_before = (
        sorted(path.relative_to(source_cache).as_posix() for path in source_cache.rglob("*"))
        if source_cache.exists()
        else None
    )
    vault = tmp_path / "vault"
    vault.mkdir()
    monkeypatch.setenv("LLM_WIKI_ROOT", str(vault))
    monkeypatch.setattr(
        runner,
        "urlopen",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("network accessed")),
    )

    isolated = tmp_path / "isolated"
    runner.run_benchmark(corpus, cache_root=isolated)
    assert (isolated / "fake-index.json").is_file()
    source_cache_after = (
        sorted(path.relative_to(source_cache).as_posix() for path in source_cache.rglob("*"))
        if source_cache.exists()
        else None
    )
    assert source_cache_after == source_cache_before
    with pytest.raises(ValueError):
        runner.run_benchmark(corpus, cache_root=ROOT / "cache")
    with pytest.raises(ValueError):
        runner.run_benchmark(corpus, cache_root=vault / "cache")
    with pytest.raises(ValueError, match="only deterministic-fake"):
        runner.run_benchmark(corpus, cache_root=tmp_path / "real", adapter="real")


def test_cache_index_replaces_symlink_without_following_it(tmp_path):
    runner = _runner_module()
    corpus = runner.load_corpus(CORPUS, SCHEMA)
    cache = tmp_path / "cache"
    cache.mkdir()
    protected = tmp_path / "protected.txt"
    protected.write_text("unchanged", encoding="utf-8")
    link = cache / "fake-index.json"
    try:
        link.symlink_to(protected)
    except OSError:
        pytest.skip("symlink creation unavailable")

    runner.run_benchmark(corpus, cache_root=cache)

    assert protected.read_text(encoding="utf-8") == "unchanged"
    assert link.is_file()
    assert not link.is_symlink()


def test_cache_publication_fails_closed_if_root_path_is_replaced(tmp_path, monkeypatch):
    runner = _runner_module()
    corpus = runner.load_corpus(CORPUS, SCHEMA)
    cache = tmp_path / "cache"
    moved = tmp_path / "moved-cache"
    original_replace = runner.os.replace

    def replace_then_swap(source, destination, *args, **kwargs):
        result = original_replace(source, destination, *args, **kwargs)
        cache.rename(moved)
        cache.mkdir()
        (cache / "fake-index.json").write_text("replacement-directory", encoding="utf-8")
        return result

    monkeypatch.setattr(runner.os, "replace", replace_then_swap)

    with pytest.raises(PermissionError, match="cache root changed"):
        runner.run_benchmark(corpus, cache_root=cache)
    assert (cache / "fake-index.json").read_text(encoding="utf-8") == "replacement-directory"


def test_cli_defaults_to_temp_cache_and_writes_only_when_requested(tmp_path):
    command = [sys.executable, str(RUNNER), "--json"]
    result = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report["quality_claim"] is False

    output = tmp_path / "report.json"
    result = subprocess.run(
        command + ["--output", str(output), "--cache-root", str(tmp_path / "cache")],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(output.read_text(encoding="utf-8"))["adapter_kind"] == "deterministic-fake"


def test_cli_fails_closed_for_invalid_corpus_and_paths(tmp_path):
    invalid = tmp_path / "invalid.json"
    invalid.write_text("{}", encoding="utf-8")
    result = subprocess.run(
        [sys.executable, str(RUNNER), "--corpus", str(invalid), "--json"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0
    assert not result.stdout.strip().startswith("{")

    existing_output = tmp_path / "existing.json"
    existing_output.write_text("unchanged", encoding="utf-8")
    result = subprocess.run(
        [sys.executable, str(RUNNER), "--output", str(existing_output)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0
    assert existing_output.read_text(encoding="utf-8") == "unchanged"

    dangling_target = tmp_path / "missing-target.json"
    dangling_output = tmp_path / "dangling-output.json"
    try:
        dangling_output.symlink_to(dangling_target)
    except OSError:
        pass
    else:
        result = subprocess.run(
            [sys.executable, str(RUNNER), "--output", str(dangling_output)],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode != 0
        assert not dangling_target.exists()

    result = subprocess.run(
        [sys.executable, str(RUNNER), "--model", "example/model"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0

    result = subprocess.run(
        [sys.executable, str(RUNNER), "--cache-root", str(ROOT / "cache")],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0


def test_output_publication_fails_closed_if_parent_identity_changes(tmp_path, monkeypatch):
    runner = _runner_module()
    output_parent = tmp_path / "output"
    output = output_parent / "report.json"

    monkeypatch.setattr(runner, "load_corpus", lambda *args: {"corpus_id": "test"})
    monkeypatch.setattr(
        runner,
        "run_benchmark",
        lambda *args, **kwargs: {
            "gates": {"passed_for_orchestration": True},
            "quality_claim": False,
        },
    )
    monkeypatch.setattr(runner.os.path, "samestat", lambda left, right: False)

    assert runner.main(["--output", str(output), "--cache-root", str(tmp_path / "cache")]) == 2


def test_loader_rejects_oversized_input_before_json_decode(tmp_path):
    runner = _runner_module()
    oversized = tmp_path / "oversized.json"
    oversized.write_bytes(b" " * (runner.MAX_CORPUS_BYTES + 1))

    with pytest.raises(ValueError, match="exceeds"):
        runner.load_corpus(oversized, SCHEMA)


def test_loader_rejects_oversized_schema_before_validation(tmp_path):
    runner = _runner_module()
    oversized = tmp_path / "oversized-schema.json"
    oversized.write_bytes(b" " * (64 * 1024 + 1))

    with pytest.raises(ValueError, match="retrieval schema exceeds"):
        runner.load_corpus(CORPUS, oversized)


def test_run_benchmark_defaults_to_v2_and_preserves_explicit_legacy(monkeypatch):
    legacy_path = BENCHMARK / "run_benchmark.py"
    spec = importlib.util.spec_from_file_location("legacy_benchmark_v2_dispatch", legacy_path)
    assert spec is not None and spec.loader is not None
    benchmark = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(benchmark)

    forwarded = []
    monkeypatch.setattr(sys, "argv", ["run_benchmark.py", "--json"])
    monkeypatch.setattr(benchmark, "_run_retrieval_v2", lambda args: forwarded.append(args) or 7)
    assert benchmark.main() == 7
    assert forwarded == [["--json"]]

    monkeypatch.setattr(sys, "argv", ["run_benchmark.py", "--semantic"])
    assert benchmark.main() == 7
    assert forwarded[-1] == ["--semantic"]

    monkeypatch.setattr(sys, "argv", ["run_benchmark.py", "--legacy-only"])
    monkeypatch.setattr(
        benchmark,
        "_load_legacy_corpus",
        lambda: {"version": "legacy-60-v1", "queries": [{"query": "q"}]},
    )
    monkeypatch.setattr(benchmark, "_tracked_knowledge_paths", lambda: [])
    monkeypatch.setattr(
        benchmark,
        "_run_benchmark",
        lambda *args, **kwargs: {"recall_at_k": {5: 1.0}},
    )
    assert benchmark.main() == 0


@pytest.mark.parametrize("flag", ["--semantic", "--report"])
def test_run_benchmark_rejects_legacy_mode_conflicts(monkeypatch, flag):
    legacy_path = BENCHMARK / "run_benchmark.py"
    spec = importlib.util.spec_from_file_location("legacy_benchmark_conflict", legacy_path)
    assert spec is not None and spec.loader is not None
    benchmark = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(benchmark)
    monkeypatch.setattr(sys, "argv", ["run_benchmark.py", "--legacy-only", flag])
    monkeypatch.setattr(
        benchmark,
        "_load_legacy_corpus",
        lambda: (_ for _ in ()).throw(AssertionError("legacy mode must not run")),
    )

    with pytest.raises(SystemExit) as raised:
        benchmark.main()
    assert raised.value.code == 2


def test_run_benchmark_help_describes_v2_default_and_task_10_reservations():
    result = subprocess.run(
        [sys.executable, str(BENCHMARK / "run_benchmark.py"), "--help"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0
    assert "retrieval-v2" in result.stdout
    assert "default" in result.stdout
    assert "--legacy-only" in result.stdout
    assert "Task 10" in result.stdout


def test_v2_cli_returns_nonzero_when_orchestration_gate_fails(monkeypatch, tmp_path):
    runner = _runner_module()
    monkeypatch.setattr(runner, "load_corpus", lambda *args: {"corpus_id": "test"})
    monkeypatch.setattr(
        runner,
        "run_benchmark",
        lambda *args, **kwargs: {
            "gates": {"passed_for_orchestration": False},
            "quality_claim": False,
        },
    )

    assert runner.main(["--cache-root", str(tmp_path / "cache")]) == 2


def _lexical_candidate(runner, evidence_id, text, *, language="EN", project="alpha"):
    return runner.Candidate(
        evidence_id=evidence_id,
        parent_id=f"parent-{evidence_id}",
        relative_path=f"synthetic/{evidence_id}.md",
        language=language,
        project=project,
        status="active",
        valid_from="2025-01-01",
        valid_to=None,
        heading_path=("Heading",),
        text=text,
    )


def test_lexical_configurations_are_explicit_and_closed():
    runner = _runner_module()

    assert set(runner.LEXICAL_CONFIGURATIONS) == {"L0", "L1", "L2", "L3", "L4"}
    assert runner.LEXICAL_CONFIGURATIONS["L0"] == {
        "id": "L0",
        "indexes": {"unicode": "unicode61 remove_diacritics 2"},
        "routing": {"strategy": "all-indexes", "candidate_universe": "all-eligible"},
        "fallback": None,
        "segmentation": None,
        "fusion": None,
    }
    assert set(runner.LEXICAL_CONFIGURATIONS["L1"]["indexes"]) == {
        "unicode",
        "english_porter",
    }
    assert runner.LEXICAL_CONFIGURATIONS["L1"]["indexes"]["english_porter"] == (
        "porter unicode61 remove_diacritics 2"
    )
    assert set(runner.LEXICAL_CONFIGURATIONS["L2"]["indexes"]) == {
        "unicode",
        "chinese_trigram",
    }
    assert set(runner.LEXICAL_CONFIGURATIONS["L3"]["indexes"]) == {
        "unicode",
        "chinese_jieba",
    }
    assert set(runner.LEXICAL_CONFIGURATIONS["L4"]["indexes"]) == {
        "unicode",
        "english_porter",
        "chinese_trigram",
        "chinese_jieba",
    }
    assert runner.LEXICAL_CONFIGURATIONS["L3"]["segmentation"] == {
        "dependency": "jieba",
        "version": "0.42.1",
        "HMM": False,
    }
    assert all(
        runner.LEXICAL_CONFIGURATIONS[level]["fusion"]
        == {"method": "reciprocal-rank-fusion", "k": 60}
        for level in ("L1", "L2", "L3", "L4")
    )
    assert all(
        set(configuration)
        == {"id", "indexes", "routing", "fallback", "segmentation", "fusion"}
        for configuration in runner.LEXICAL_CONFIGURATIONS.values()
    )


def test_lexical_benchmark_extra_and_dictionary_hash_are_exactly_pinned():
    runner = _runner_module()
    project = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    lock = (ROOT / "uv.lock").read_text(encoding="utf-8")

    assert 'lexical-benchmark = ["jieba==0.42.1"]' in project
    assert 'name = "jieba"' in lock
    assert runner.JIEBA_DEFAULT_DICTIONARY_SHA256 == (
        "7197c3211ddd98962b036cdf40324d1ea2bfaa12bd028e68faa70111a88e12a8"
    )


def test_actual_fts_tokenization_matches_unicode_and_english_porter_contracts():
    runner = _runner_module()

    assert set(runner._fts5_tokens("Caf\u00e9 \u041a\u0430\u0442\u0430\u043b\u043e\u0433", "unicode61 remove_diacritics 2")) == {
        "cafe",
        "\u043a\u0430\u0442\u0430\u043b\u043e\u0433",
    }
    assert runner._fts5_tokens(
        "running corrected", "porter unicode61 remove_diacritics 2"
    ) == ("correct", "run")


def test_l1_fuses_unicode_and_porter_without_language_routing(tmp_path, monkeypatch):
    runner = _runner_module()
    candidates = [
        _lexical_candidate(
            runner, "a-running", "The runner was running quickly.", language="RU"
        ),
        _lexical_candidate(runner, "b-noise", "A static unrelated note."),
    ]
    adapter = runner.SQLiteLexicalAdapter(candidates, tmp_path, "L1")
    routed = []
    original = adapter._rank_index

    def observe(index_name, *args, **kwargs):
        routed.append(index_name)
        return original(index_name, *args, **kwargs)

    monkeypatch.setattr(adapter, "_rank_index", observe)
    try:
        ranked = adapter.rank(
            "runs",
            runner.QueryScope(projects=("alpha",), temporal_mode="current", as_of=None),
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
    runner = _runner_module()
    candidates = [
        _lexical_candidate(runner, "en", "English", language="EN"),
        _lexical_candidate(runner, "ru", "\u0420\u0443\u0441\u0441\u043a\u0438\u0439", language="RU"),
        _lexical_candidate(runner, "zh", "\u4e2d\u6587", language="ZH"),
    ]
    adapter = runner.SQLiteLexicalAdapter(
        candidates,
        tmp_path / lexical_config,
        lexical_config,
        test_segmenter=_PinnedJieba(),
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
    runner = _runner_module()
    candidates = [_lexical_candidate(runner, "zh", "\u7f13\u5b58\u76ee\u5f55", language="ZH")]
    adapter = runner.SQLiteLexicalAdapter(candidates, tmp_path, "L2")
    routed = []
    original = adapter._rank_index

    def observe(index_name, *args, **kwargs):
        routed.append(index_name)
        return original(index_name, *args, **kwargs)

    monkeypatch.setattr(adapter, "_rank_index", observe)
    scope = runner.QueryScope(projects=("alpha",), temporal_mode="current", as_of=None)
    try:
        adapter.rank("\u7f13\u5b58", scope, candidates, limit=10, language="ZH")
        adapter.rank("\u7f13\u5b58\u76ee", scope, candidates, limit=10, language="ZH")
    finally:
        adapter.close()

    assert routed == ["unicode", "unicode", "chinese_trigram"]


def test_sqlite_trigram_tokenizes_normalized_mixed_query_and_documents_identically(tmp_path):
    runner = _runner_module()
    candidates = [
        _lexical_candidate(runner, "mixed", "\u76ee\u5f55ABC cache", language="ZH"),
        _lexical_candidate(runner, "noise", "\u76ee\u5f55XYZ queue", language="EN"),
    ]
    adapter = runner.SQLiteLexicalAdapter(candidates, tmp_path, "L2")
    try:
        ranked = adapter._rank_index(
            "chinese_trigram", "\u76ee\u5f55ＡＢＣ", {"mixed", "noise"}, limit=10
        )
    finally:
        adapter.close()

    assert [evidence_id for evidence_id, _score in ranked] == ["mixed"]


class _PinnedJieba:
    __version__ = "0.42.1"

    def __init__(self):
        self.calls = []

    def cut(self, text, HMM=True):
        self.calls.append((text, HMM))
        return text.replace("\u7f13\u5b58\u76ee\u5f55", "\u7f13\u5b58 \u76ee\u5f55").split()


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
    runner = _runner_module()
    jieba = _PinnedJieba()
    candidates = [_lexical_candidate(runner, "zh", "\u7f13\u5b58\u76ee\u5f55", language="ZH")]
    adapter = runner.SQLiteLexicalAdapter(candidates, tmp_path, "L3", test_segmenter=jieba)
    scope = runner.QueryScope(projects=("alpha",), temporal_mode="current", as_of=None)
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
    runner = _runner_module()
    corpus = runner.load_corpus(CORPUS, SCHEMA)
    candidates = runner.build_candidates(corpus)
    adapter = runner.SQLiteLexicalAdapter(
        candidates,
        tmp_path / lexical_config,
        lexical_config,
        test_segmenter=_PinnedJieba(),
    )
    try:
        for query in (item for item in corpus["queries"] if item["cross_language"]):
            ranked = adapter.rank(
                query["text"],
                runner._query_scope(query),
                candidates,
                limit=runner.MAX_CANDIDATES,
                language=query["language"],
            )
            assert set(query["required_evidence_spans"]) <= {
                item.evidence_id for item in ranked
            }
    finally:
        adapter.close()


@pytest.mark.parametrize("version", ["0.42.0", "0.43.0"])
def test_test_segmenter_never_claims_pinned_version(tmp_path, version):
    runner = _runner_module()
    jieba = _PinnedJieba()
    jieba.__version__ = version
    candidates = [_lexical_candidate(runner, "zh", "\u7f13\u5b58\u76ee\u5f55", language="ZH")]

    adapter = runner.SQLiteLexicalAdapter(candidates, tmp_path, "L3", test_segmenter=jieba)
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
    runner = _runner_module()
    candidates = [_lexical_candidate(runner, "zh", "\u7f13\u5b58\u76ee\u5f55", language="ZH")]
    monkeypatch.setattr(
        runner,
        "_load_pinned_jieba",
        lambda cache_root: (_ for _ in ()).throw(
            ValueError("requires installed jieba 0.42.1")
        ),
    )

    with pytest.raises(ValueError, match="jieba 0.42.1"):
        runner.SQLiteLexicalAdapter(candidates, tmp_path, "L3")


def test_injected_segmenter_reports_test_only_provenance(tmp_path):
    runner = _runner_module()
    corpus = runner.load_corpus(CORPUS, SCHEMA)

    report = runner.run_benchmark(
        corpus,
        cache_root=tmp_path,
        lexical_config="L3",
        test_segmenter=_PinnedJieba(),
    )

    assert report["methodology"]["segmentation_runtime"] == {
        "provenance": "injected-test",
        "quality_evidence": False,
        "version": None,
        "dictionary_sha256": None,
    }


def test_pinned_segmenter_rejects_wrong_distribution_provenance(tmp_path, monkeypatch):
    runner = _runner_module()
    package = tmp_path / "site-packages" / "jieba"
    package.mkdir(parents=True)
    module_file = package / "__init__.py"
    dictionary = package / "dict.txt"
    module_file.write_text("", encoding="utf-8")
    dictionary.write_text("dictionary", encoding="utf-8")
    module = type("Jieba", (), {"__file__": str(module_file), "cut": lambda *args: []})()

    class Distribution:
        version = "0.42.1"
        files = (Path("jieba/__init__.py"), Path("jieba/dict.txt"))

        def locate_file(self, relative):
            return tmp_path / "site-packages" / relative

    monkeypatch.setattr(runner.importlib, "import_module", lambda name: module)
    monkeypatch.setattr(runner.importlib_metadata, "distribution", lambda name: Distribution())
    monkeypatch.setattr(
        runner.importlib_metadata,
        "packages_distributions",
        lambda: {"jieba": ["not-jieba"]},
    )

    with pytest.raises(ValueError, match="distribution provenance"):
        runner._load_pinned_jieba(tmp_path / "cache")


def test_pinned_segmenter_rejects_wrong_default_dictionary_hash(tmp_path, monkeypatch):
    runner = _runner_module()
    package = tmp_path / "site-packages" / "jieba"
    package.mkdir(parents=True)
    module_file = package / "__init__.py"
    dictionary = package / "dict.txt"
    module_file.write_text("", encoding="utf-8")
    dictionary.write_text("tampered", encoding="utf-8")
    module = type("Jieba", (), {"__file__": str(module_file), "cut": lambda *args: []})()

    class Distribution:
        version = "0.42.1"
        files = (Path("jieba/__init__.py"), Path("jieba/dict.txt"))

        def locate_file(self, relative):
            return tmp_path / "site-packages" / relative

    monkeypatch.setattr(runner.importlib, "import_module", lambda name: module)
    monkeypatch.setattr(runner.importlib_metadata, "distribution", lambda name: Distribution())
    monkeypatch.setattr(
        runner.importlib_metadata,
        "packages_distributions",
        lambda: {"jieba": ["jieba"]},
    )

    with pytest.raises(ValueError, match="dictionary SHA256"):
        runner._load_pinned_jieba(tmp_path / "cache")


@pytest.mark.parametrize("lexical_config", ["L3", "L4"])
def test_real_jieba_cache_stays_inside_isolated_cache_and_global_dt_is_untouched(
    tmp_path, monkeypatch, lexical_config
):
    jieba = pytest.importorskip("jieba")
    runner = _runner_module()
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
        _lexical_candidate(runner, "zh", "缓存目录", language="ZH")
    ]

    adapter = runner.SQLiteLexicalAdapter(candidates, cache_root, lexical_config)
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


def test_fixed_rank_fusion_is_deterministic_and_ignores_bm25_magnitudes():
    runner = _runner_module()
    rankings = {
        "unicode": [("b", -0.0001), ("a", -1000.0)],
        "english_porter": [("a", -0.1), ("b", -0.2)],
    }

    first = runner._reciprocal_rank_fusion(rankings, k=60)
    second = runner._reciprocal_rank_fusion(dict(reversed(list(rankings.items()))), k=60)

    assert first == second
    assert [evidence_id for evidence_id, _score in first] == ["a", "b"]
    assert first[0][1] == first[1][1]
    assert first[0][1] == pytest.approx((1 / 61 + 1 / 62) / (2 / 61))
    assert all(0.0 <= score <= 1.0 for _evidence_id, score in first)
    assert runner._reciprocal_rank_fusion(
        {"one": [("winner", -1.0)], "two": [("winner", -999.0)]}, k=60
    ) == [("winner", 1.0)]
    assert runner._reciprocal_rank_fusion(
        {"one": [("winner", -1.0), ("winner", -2.0)]}, k=60
    ) == [("winner", 1.0)]


@pytest.mark.parametrize("lexical_config", ["L1", "L2", "L3", "L4"])
def test_fused_scores_preserve_downstream_abstention_contract(tmp_path, lexical_config):
    runner = _runner_module()
    corpus = runner.load_corpus(CORPUS, SCHEMA)

    report = runner.run_benchmark(
        corpus,
        cache_root=tmp_path / lexical_config,
        lexical_config=lexical_config,
        test_segmenter=_PinnedJieba(),
    )

    scores = [item["score"] for trace in report["traces"] for item in trace["ranked_evidence"]]
    answerable = [trace for trace in report["traces"] if trace["query_id"] not in {
        query["query_id"] for query in corpus["queries"] if query["answerability"] == "unanswerable"
    }]
    assert scores and all(0.0 <= score <= 1.0 for score in scores)
    assert any(not trace["abstained"] for trace in answerable)
    assert report["overall"]["no_answer_false_answer_rate"] == pytest.approx(1 / 3)
    assert report["gates"]["qa_contract_passed"] is False
    assert report["quality_claim"] is False


def test_rank_rejects_changed_or_incomplete_candidate_universe(tmp_path):
    runner = _runner_module()
    candidates = [
        _lexical_candidate(runner, "one", "catalog"),
        _lexical_candidate(runner, "two", "queue"),
    ]
    changed = runner.Candidate(**{**candidates[0].__dict__, "text": "changed"})
    scope = runner.QueryScope(projects=("alpha",), temporal_mode="current", as_of=None)
    adapter = runner.SQLiteLexicalAdapter(candidates, tmp_path / "changed", "L0")

    with pytest.raises(ValueError, match="candidate universe mismatch"):
        adapter.rank("catalog", scope, [changed, candidates[1]], limit=10, language="EN")
    assert not adapter.path.exists()

    retry = runner.SQLiteLexicalAdapter(candidates, tmp_path / "changed", "L0")
    try:
        with pytest.raises(ValueError, match="candidate universe mismatch"):
            retry.rank("catalog", scope, candidates[:1], limit=10, language="EN")
    finally:
        retry.close()


def test_constructor_rejects_duplicate_normalized_candidate_ids(tmp_path):
    runner = _runner_module()
    candidates = [
        _lexical_candidate(runner, "Evidence", "one"),
        _lexical_candidate(runner, "EVIDENCE", "two"),
    ]

    with pytest.raises(ValueError, match="duplicate normalized candidate id"):
        runner.SQLiteLexicalAdapter(candidates, tmp_path, "L0")
    assert not (tmp_path / "lexical-L0.sqlite3").exists()


def test_lexical_index_rejects_preexisting_symlink_and_preserves_target(tmp_path):
    runner = _runner_module()
    candidates = [_lexical_candidate(runner, "one", "catalog")]
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
        runner.SQLiteLexicalAdapter(candidates, cache, "L0")
    assert protected.read_text(encoding="utf-8") == "unchanged"
    assert index.is_symlink()


def test_lexical_index_rejects_symlink_cache_root(tmp_path):
    runner = _runner_module()
    candidates = [_lexical_candidate(runner, "one", "catalog")]
    real_cache = tmp_path / "real-cache"
    real_cache.mkdir()
    linked_cache = tmp_path / "linked-cache"
    try:
        linked_cache.symlink_to(real_cache, target_is_directory=True)
    except OSError:
        pytest.skip("directory symlink creation unavailable")

    with pytest.raises(ValueError, match="cache root.*symlink"):
        runner.SQLiteLexicalAdapter(candidates, linked_cache, "L0")
    assert not (real_cache / "lexical-L0.sqlite3").exists()


def test_lexical_index_rejects_cache_identity_race_and_allows_retry(tmp_path, monkeypatch):
    runner = _runner_module()
    candidates = [_lexical_candidate(runner, "one", "catalog")]
    cache = tmp_path / "cache"
    original = runner._same_path_identity
    raced = False

    def race_once(path, expected):
        nonlocal raced
        if Path(path) == cache.resolve() and not raced:
            raced = True
            return False
        return original(path, expected)

    monkeypatch.setattr(runner, "_same_path_identity", race_once)
    with pytest.raises(PermissionError, match="cache root changed"):
        runner.SQLiteLexicalAdapter(candidates, cache, "L0")
    assert not (cache / "lexical-L0.sqlite3").exists()

    monkeypatch.undo()
    retry = runner.SQLiteLexicalAdapter(candidates, cache, "L0")
    retry.close()
    assert retry.path.exists()


def test_lexical_index_is_owner_only_on_posix(tmp_path):
    if os.name != "posix":
        pytest.skip("POSIX mode bits unavailable")
    runner = _runner_module()
    candidates = [_lexical_candidate(runner, "one", "catalog")]
    adapter = runner.SQLiteLexicalAdapter(candidates, tmp_path, "L0")
    try:
        assert adapter.path.stat().st_mode & 0o077 == 0
    finally:
        adapter.close()


def test_query_deadline_removes_index_and_allows_retry(tmp_path):
    runner = _runner_module()
    candidates = [_lexical_candidate(runner, "one", "catalog")]
    scope = runner.QueryScope(projects=("alpha",), temporal_mode="current", as_of=None)
    adapter = runner.SQLiteLexicalAdapter(
        candidates, tmp_path, "L0", deadline_seconds=0.05
    )
    runner.time.sleep(0.06)

    with pytest.raises(TimeoutError, match="deadline"):
        adapter.rank("catalog", scope, candidates, limit=10, language="EN")
    assert not adapter.path.exists()

    retry = runner.SQLiteLexicalAdapter(candidates, tmp_path, "L0")
    retry.close()
    assert retry.path.exists()


def test_segmentation_build_obeys_absolute_deadline_and_cleans_up(tmp_path):
    runner = _runner_module()
    candidates = [_lexical_candidate(runner, "one", "\u7f13\u5b58\u76ee\u5f55", language="ZH")]

    with pytest.raises(TimeoutError, match="deadline"):
        runner.SQLiteLexicalAdapter(
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
    runner = _runner_module()
    candidates = [_lexical_candidate(runner, "one", "\u7f13\u5b58\u76ee\u5f55", language="ZH")]

    with pytest.raises(ValueError, match=error):
        runner.SQLiteLexicalAdapter(
            candidates, tmp_path, "L3", test_segmenter=segmenter
        )
    assert not (tmp_path / "lexical-L3.sqlite3").exists()

    retry = runner.SQLiteLexicalAdapter(
        candidates, tmp_path, "L3", test_segmenter=_PinnedJieba()
    )
    retry.close()
    assert retry.path.exists()


def test_segmentation_input_is_bounded_before_calling_segmenter(tmp_path):
    runner = _runner_module()
    segmenter = _PinnedJieba()
    candidates = [
        _lexical_candidate(
            runner,
            "one",
            "x" * (runner.MAX_SEGMENTATION_INPUT_CHARS + 1),
            language="ZH",
        )
    ]

    with pytest.raises(ValueError, match="input limit"):
        runner.SQLiteLexicalAdapter(
            candidates, tmp_path, "L3", test_segmenter=segmenter
        )
    assert segmenter.calls == []
    assert not (tmp_path / "lexical-L3.sqlite3").exists()


def test_run_failure_closes_and_removes_lexical_index_for_retry(tmp_path, monkeypatch):
    runner = _runner_module()
    corpus = runner.load_corpus(CORPUS, SCHEMA)
    cache = tmp_path / "cache"
    monkeypatch.setattr(
        runner.FakeQAAdapter,
        "answer",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("qa failed")),
    )

    with pytest.raises(RuntimeError, match="qa failed"):
        runner.run_benchmark(corpus, cache_root=cache, lexical_config="L1")
    assert not (cache / "lexical-L1.sqlite3").exists()

    monkeypatch.undo()
    report = runner.run_benchmark(corpus, cache_root=cache, lexical_config="L1")
    assert report["quality_claim"] is False


def test_sqlite_lexical_filters_match_legacy_hard_filter_scope(tmp_path):
    runner = _runner_module()
    candidates = [
        _lexical_candidate(runner, "eligible", "catalog", project="alpha"),
        _lexical_candidate(runner, "wrong-project", "catalog", project="beta"),
        runner.Candidate(
            **{
                **_lexical_candidate(runner, "superseded", "catalog", project="alpha").__dict__,
                "status": "superseded",
                "valid_to": "2025-06-01",
            }
        ),
    ]
    scope = runner.QueryScope(projects=("alpha",), temporal_mode="current", as_of=None)
    adapter = runner.SQLiteLexicalAdapter(candidates, tmp_path, "L0")
    try:
        ranked = adapter.rank("catalog", scope, candidates, limit=10, language="EN")
    finally:
        adapter.close()

    assert [item.evidence_id for item in ranked] == ["eligible"]
    assert {item.evidence_id for item in ranked} == {
        item.evidence_id for item in runner.filter_candidates(candidates, scope)
    }


@pytest.mark.parametrize("lexical_config", ["L0", "L1", "L2", "L3", "L4"])
def test_explicit_lexical_run_is_isolated_and_reports_config_metrics(
    tmp_path, lexical_config
):
    runner = _runner_module()
    corpus = runner.load_corpus(CORPUS, SCHEMA)
    cache = tmp_path / lexical_config

    report = runner.run_benchmark(
        corpus,
        cache_root=cache,
        lexical_config=lexical_config,
        test_segmenter=_PinnedJieba(),
    )
    database = cache / f"lexical-{lexical_config}.sqlite3"

    assert database.is_file()
    assert not (cache / "fake-index.json").exists()
    assert report["methodology"]["lexical_configuration"] == (
        runner.LEXICAL_CONFIGURATIONS[lexical_config]
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
    assert set(report) == runner.REPORT_FIELDS
    assert report["corpus_sha256"] == runner._sha256_file(CORPUS)
    assert report["matrix_sha256"] == runner._sha256_file(BENCHMARK / "model-matrix-v1.json")
    assert report["benchmark_runner_sha256"] == runner._sha256_file(RUNNER)
    assert report["benchmark_contract_sha256"] == runner._sha256_json(
        json.loads((BENCHMARK / "model-matrix-v1.json").read_bytes())["benchmark_contract"]
    )


def test_resource_measurements_separate_cold_warm_and_index_throughput():
    runner = _runner_module()

    measured = runner._resource_measurements(
        [100.0, 10.0, 20.0, 30.0],
        build_time_ms=2000.0,
        chunk_count=100,
        index_size_bytes=4096,
        peak_rss_bytes=8192,
        peak_rss_status="measured-test",
    )

    assert measured["cold_first_query_latency_ms"] == 100.0
    assert measured["warm_latency_p50_ms"] == 20.0
    assert measured["warm_latency_p95_ms"] == 30.0
    assert measured["indexing_throughput_chunks_per_second"] == 50.0
    assert measured["measurement_status"] == {
        "latency_p50_ms": "measured",
        "latency_p95_ms": "measured",
        "cold_first_query_latency_ms": "measured",
        "warm_latency_p50_ms": "measured",
        "warm_latency_p95_ms": "measured",
        "build_time_ms": "measured",
        "indexing_throughput_chunks_per_second": "measured",
        "peak_rss_bytes": "measured",
        "index_size_bytes": "measured",
    }


def test_resource_measurements_use_null_and_status_when_samples_are_unavailable():
    runner = _runner_module()

    unavailable = runner._resource_measurements(
        [],
        build_time_ms=0.0,
        chunk_count=0,
        index_size_bytes=0,
        peak_rss_bytes=None,
        peak_rss_status="unavailable",
    )

    assert unavailable["cold_first_query_latency_ms"] is None
    assert unavailable["warm_latency_p50_ms"] is None
    assert unavailable["warm_latency_p95_ms"] is None
    assert unavailable["indexing_throughput_chunks_per_second"] is None
    assert unavailable["measurement_status"]["cold_first_query_latency_ms"] == "unavailable"
    assert unavailable["measurement_status"]["warm_latency_p50_ms"] == "unavailable"
    assert unavailable["measurement_status"]["indexing_throughput_chunks_per_second"] == (
        "unavailable"
    )
    assert unavailable["measurement_status"]["peak_rss_bytes"] == "unavailable"


def test_explicit_lexical_ablation_does_not_invoke_dense_or_reranker(
    tmp_path, monkeypatch
):
    runner = _runner_module()
    corpus = runner.load_corpus(CORPUS, SCHEMA)

    def forbidden(*args, **kwargs):
        raise AssertionError("dense model or reranker invoked before lexical ablation")

    monkeypatch.setattr(runner.FakeEmbeddingAdapter, "rank", forbidden)
    monkeypatch.setattr(runner.FakeRerankerAdapter, "rank", forbidden)

    report = runner.run_benchmark(corpus, cache_root=tmp_path, lexical_config="L0")

    assert report["methodology"]["retrieval_order"].startswith("BM25-only")


def test_legacy_default_does_not_build_or_report_lexical_ablations(tmp_path):
    runner = _runner_module()
    corpus = runner.load_corpus(CORPUS, SCHEMA)

    report = runner.run_benchmark(corpus, cache_root=tmp_path)

    assert (tmp_path / "fake-index.json").is_file()
    assert not list(tmp_path.glob("lexical-*.sqlite3"))
    assert "lexical_configuration" not in report["methodology"]
    assert report["adapter_kind"] == "deterministic-fake"


def test_cli_requires_explicit_valid_lexical_configuration(tmp_path):
    result = subprocess.run(
        [
            sys.executable,
            str(RUNNER),
            "--lexical-config",
            "L0",
            "--cache-root",
            str(tmp_path / "cache"),
            "--json",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode in {0, 2}
    assert json.loads(result.stdout)["methodology"]["lexical_configuration"]["id"] == "L0"
    invalid = subprocess.run(
        [sys.executable, str(RUNNER), "--lexical-config", "unknown"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert invalid.returncode != 0


def test_model_matrix_selection_is_exact_pinned_and_fail_closed(tmp_path):
    runner = _runner_module()
    matrix_path = BENCHMARK / "model-matrix-v1.json"
    selection = runner.load_model_selection(
        matrix_path,
        CORPUS,
        model_id="Qwen/Qwen3-Embedding-0.6B",
        variant_id="float32-384d",
        reranker_id="BAAI/bge-reranker-v2-m3",
    )

    assert selection.embedding["revision"] == (
        "97b0c614be4d77ee51c0cef4e5f07c00f9eb65b3"
    )
    assert selection.variant["dimensions"] == 384
    assert selection.reranker["revision"] == (
        "953dc6f6f85a1b2dbfca4c34a2796e7dde08d41e"
    )
    assert selection.matrix_sha256 == runner._sha256_file(matrix_path)
    assert selection.corpus_sha256 == runner._sha256_file(CORPUS)

    with pytest.raises(ValueError, match="unknown embedding"):
        runner.load_model_selection(
            matrix_path, CORPUS, model_id="unknown/model", variant_id="float32-384d"
        )

    matrix = json.loads(matrix_path.read_text(encoding="utf-8"))
    gemma = next(
        item for item in matrix["embeddings"] if item["id"] == "google/embeddinggemma-300m"
    )
    gemma["shipping_eligible"] = True
    changed = tmp_path / "matrix.json"
    changed.write_bytes(canonical_json_bytes(matrix) + b"\n")
    with pytest.raises(ValueError, match="shipping policy"):
        runner.load_model_selection(
            changed,
            CORPUS,
            model_id=matrix["embeddings"][0]["id"],
            variant_id=matrix["embeddings"][0]["variants"][0]["variant_id"],
        )


def test_required_candidate_specs_cover_the_closed_canonical_matrix():
    runner = _runner_module()
    matrix = json.loads((BENCHMARK / "model-matrix-v1.json").read_bytes())

    specs = runner.required_candidate_specs(matrix)
    embedding_variants = {
        (candidate["id"], variant["variant_id"])
        for candidate in matrix["embeddings"]
        for variant in candidate["variants"]
    }
    rerankers = {candidate["id"] for candidate in matrix["rerankers"]}
    observed = {
        (
            spec["embedding"]["id"],
            spec["embedding"]["variant_id"],
            spec["reranker"]["id"] if spec["reranker"] else None,
        )
        for spec in specs
    }

    assert len(embedding_variants) == 9
    assert rerankers == {"BAAI/bge-reranker-v2-m3", "Qwen/Qwen3-Reranker-0.6B"}
    assert observed == {
        (model_id, variant_id, reranker_id)
        for model_id, variant_id in embedding_variants
        for reranker_id in {None, *rerankers}
    }
    assert len(specs) == len(observed) == 27
    assert any(
        spec["embedding"]["id"] == "google/embeddinggemma-300m" for spec in specs
    )


@pytest.mark.parametrize(
    "mutate",
    [
        lambda matrix: matrix["selection"].update(unknown=True),
        lambda matrix: matrix["selection"]["baseline"].update(unknown=True),
        lambda matrix: matrix["selection"]["limits"].update(unknown=True),
        lambda matrix: matrix["selection"]["aggregation_evidence_contract"].update(
            unknown=True
        ),
        lambda matrix: matrix["lexical"].update(unknown=True),
        lambda matrix: matrix["embeddings"][0]["variants"][0]["quality"].update(
            unknown=True
        ),
    ],
)
def test_model_matrix_runtime_rejects_unknown_nested_policy_fields(tmp_path, mutate):
    runner = _runner_module()
    matrix = json.loads((BENCHMARK / "model-matrix-v1.json").read_text(encoding="utf-8"))
    mutate(matrix)
    changed = tmp_path / "matrix.json"
    changed.write_bytes(canonical_json_bytes(matrix) + b"\n")

    with pytest.raises(ValueError, match="closed canonical matrix object"):
        runner.load_model_selection(
            changed,
            CORPUS,
            model_id="BAAI/bge-small-en-v1.5",
            variant_id="float32-384d",
        )


def test_embedding_adapter_applies_matrix_format_dimension_float32_and_no_gold():
    np = pytest.importorskip("numpy")
    runner = _runner_module()
    selection = runner.load_model_selection(
        BENCHMARK / "model-matrix-v1.json",
        CORPUS,
        model_id="Qwen/Qwen3-Embedding-0.6B",
        variant_id="float32-384d",
    )
    calls = []

    def encoder(texts, **options):
        calls.append((tuple(texts), options))
        vectors = np.zeros((len(texts), 1024), dtype=np.float64)
        for row in range(len(texts)):
            vectors[row, row % 2] = 1.0
        return vectors

    candidates = [
        _lexical_candidate(runner, "one", "first document"),
        _lexical_candidate(runner, "two", "second document"),
    ]
    adapter = runner.ModelEmbeddingAdapter(selection, encoder=encoder)
    ranked = adapter.rank(
        "find it",
        runner.QueryScope(projects=("alpha",), temporal_mode="current", as_of=None),
        candidates,
        limit=2,
    )

    assert calls[0][0] == tuple(runner._candidate_text(item) for item in candidates)
    assert calls[1][0] == (
        "Instruct: Given a web search query, retrieve relevant passages that answer the query\n"
        "Query:find it",
    )
    assert all(call[1]["batch_size"] == 8 for call in calls)
    assert all(call[1]["max_length"] == 512 for call in calls)
    assert adapter.document_vectors.dtype == np.float32
    assert adapter.document_vectors.shape == (2, 384)
    assert len(ranked) == 2
    assert not hasattr(adapter, "queries") and not hasattr(adapter, "gold")


def test_bge_m3_adapter_fuses_learned_sparse_as_a_separate_signal():
    np = pytest.importorskip("numpy")
    runner = _runner_module()
    selection = runner.load_model_selection(
        BENCHMARK / "model-matrix-v1.json",
        CORPUS,
        model_id="BAAI/bge-m3",
        variant_id="float32-1024d",
    )

    def encoder(texts, **options):
        del options
        dense = np.zeros((len(texts), 1024), dtype=np.float32)
        dense[:, 0] = 1.0
        sparse = []
        for text in texts:
            sparse.append({"42": 1.0} if "second" in text or text == "find it" else {"7": 1.0})
        return {"dense_vecs": dense, "lexical_weights": sparse}

    candidates = [
        _lexical_candidate(runner, "one", "first document"),
        _lexical_candidate(runner, "two", "second document"),
    ]
    adapter = runner.ModelEmbeddingAdapter(selection, encoder=encoder)
    ranked = adapter.rank(
        "find it",
        runner.QueryScope(projects=("alpha",), temporal_mode="current", as_of=None),
        candidates,
        limit=2,
    )

    assert [item.evidence_id for item in ranked] == ["two", "one"]
    assert adapter.last_trace["dense_candidate_ids"] == ["one", "two"]
    assert adapter.last_trace["learned_sparse_candidate_ids"] == ["two"]
    assert adapter.last_trace["fusion"] == {"method": "reciprocal-rank-fusion", "k": 60}
    assert adapter.learned_sparse_bytes > 0


def test_bound_baseline_is_a_recomputable_retrieval_v2_bge_small_report(monkeypatch):
    runner = _runner_module()
    matrix = json.loads((BENCHMARK / "model-matrix-v1.json").read_bytes())
    baseline = json.loads((ROOT / matrix["selection"]["baseline"]["raw_report_path"]).read_bytes())

    assert baseline["schema_version"] == "retrieval-report/v2"
    assert baseline["effective_mode"] == "model-matrix"
    assert baseline["candidate"] == {
        "embedding": {
            "dimensions": 384,
            "id": "BAAI/bge-small-en-v1.5",
            "revision": "5c38ec7c405ec4b44b94cc5a9bb96e735b38267a",
            "variant_id": "float32-384d",
        },
        "reranker": None,
    }
    overall, _slices = runner._verified_baseline_metrics(matrix, baseline, corpus_path=CORPUS)
    quality = round(
        sum(
            overall[name]
            for name in (
                "parent_recall_at_10",
                "all_required_evidence_recall_at_20",
                "ndcg_at_10",
                "mrr_at_10",
            )
        )
        / 4
        * 10_000
    )
    assert matrix["selection"]["baseline"]["overall_basis_points"] == quality
    assert matrix["selection"]["baseline"]["parent_recall_at_10_basis_points"] == round(
        overall["parent_recall_at_10"] * 10_000
    )
    assert matrix["selection"]["baseline"]["policy_sha256"] == runner._baseline_policy_sha256(
        baseline
    )

    current_resolution = runner._locked_package_versions(ROOT / "uv.lock")
    current_resolution["numpy"] = "2.2.6"
    monkeypatch.setattr(
        runner, "_locked_package_versions", lambda _lock_path: current_resolution
    )
    runner._verified_baseline_metrics(matrix, baseline, corpus_path=CORPUS)

    original_version = runner.importlib_metadata.version

    def base_environment_version(name):
        if name in {
            "jieba",
            "sentence-transformers",
            "transformers",
            "torch",
            "numpy",
            "usearch",
        }:
            raise runner.importlib_metadata.PackageNotFoundError(name)
        return original_version(name)

    monkeypatch.setattr(runner.importlib_metadata, "version", base_environment_version)
    runner._verified_baseline_metrics(matrix, baseline, corpus_path=CORPUS)
    observed = runner._observed_runtime_environment("numpy-exact", lexical_config="L4")
    assert observed["packages"] == {
        "jieba": None,
        "numpy": None,
        "sentence-transformers": None,
        "torch": None,
        "transformers": None,
        "usearch": None,
    }

    evolved = json.loads(json.dumps(matrix))
    evolved["embeddings"][-1]["variants"][-1]["quality"]["status"] = "measured"
    runner._verified_baseline_metrics(evolved, baseline, corpus_path=CORPUS)


def test_baseline_policy_fingerprint_rejects_mismatched_provenance():
    runner = _runner_module()
    matrix = json.loads((BENCHMARK / "model-matrix-v1.json").read_bytes())
    baseline = json.loads((BENCHMARK / "baseline-2026-07-16-retrieval.json").read_bytes())

    matrix["selection"]["baseline"]["policy_sha256"] = "0" * 64
    with pytest.raises(ValueError, match="policy fingerprint"):
        runner._verified_baseline_metrics(matrix, baseline, corpus_path=CORPUS)

    matrix = json.loads((BENCHMARK / "model-matrix-v1.json").read_bytes())
    baseline = json.loads((BENCHMARK / "baseline-2026-07-16-retrieval.json").read_bytes())
    baseline["methodology"]["environment_provenance"]["verified_lock"]["packages"][
        "numpy"
    ] = "0.0.0"
    matrix["selection"]["baseline"]["policy_sha256"] = runner._baseline_policy_sha256(
        baseline
    )
    with pytest.raises(ValueError, match="package contract"):
        runner._verified_baseline_metrics(matrix, baseline, corpus_path=CORPUS)

    matrix = json.loads((BENCHMARK / "model-matrix-v1.json").read_bytes())
    baseline = json.loads((BENCHMARK / "baseline-2026-07-16-retrieval.json").read_bytes())
    baseline["benchmark_runner_sha256"] = "1" * 64
    with pytest.raises(ValueError, match="policy fingerprint"):
        runner._verified_baseline_metrics(matrix, baseline, corpus_path=CORPUS)


def test_real_lexical_worker_emits_parent_accepted_canonical_bytes(tmp_path):
    runner = _runner_module()
    cache = tmp_path / "cache"
    cache.mkdir()
    argv = runner._lexical_ablation_worker_arguments(cache, 60.0)[0]

    payload = runner._run_bounded_model_worker(argv, deadline_seconds=60.0)

    assert isinstance(payload, runner._WorkerPayload)
    assert payload.canonical_bytes == runner._canonical_report_bytes(payload.report)
    assert payload.report["methodology"]["lexical_configuration"]["id"] == "L0"
    runner._validate_lexical_worker_payload(
        payload,
        matrix=json.loads((BENCHMARK / "model-matrix-v1.json").read_bytes()),
        corpus_path=CORPUS,
    )

def test_lexical_winner_requires_complete_comparable_l0_through_l4_evidence():
    runner = _runner_module()
    common = {
        "schema_version": "retrieval-report/v2",
        "corpus_sha256": "c" * 64,
        "benchmark_contract_sha256": "b" * 64,
        "benchmark_runner_sha256": "r" * 64,
        "acquisition_mode": None,
        "adapter_kind": "deterministic-fake",
        "effective_mode": "deterministic-fake",
    }
    reports = []
    for index, level in enumerate(("L0", "L1", "L2", "L3", "L4")):
        reports.append(
            {
                **common,
                "methodology": {
                    "lexical_configuration": json.loads(
                        json.dumps(runner.LEXICAL_CONFIGURATIONS[level])
                    )
                },
                "overall": {
                    "parent_recall_at_10": 0.8 + index / 100,
                    "all_required_evidence_recall_at_20": 0.8,
                    "ndcg_at_10": 0.8,
                    "mrr_at_10": 0.8,
                    "no_answer_false_answer_rate": 0.0,
                },
                "measurements": {"warm_latency_p95_ms": 10.0 + index},
            }
        )

    assert runner._select_lexical_winner(reports)["id"] == "L4"
    with pytest.raises(ValueError, match="complete.*L0.*L4"):
        runner._select_lexical_winner(reports[:-1])
    reports[-1]["corpus_sha256"] = "d" * 64
    with pytest.raises(ValueError, match="comparable"):
        runner._select_lexical_winner(reports)
    reports[-1]["corpus_sha256"] = "c" * 64
    reports[-1]["methodology"]["lexical_configuration"] = {"id": "L4", "extra": True}
    with pytest.raises(ValueError, match="configuration"):
        runner._select_lexical_winner(reports)


def test_baseline_verification_rejects_non_attested_quality_payload():
    runner = _runner_module()
    matrix = json.loads((BENCHMARK / "model-matrix-v1.json").read_bytes())
    baseline = json.loads((BENCHMARK / "baseline-2026-07-16-retrieval.json").read_bytes())
    baseline["quality_claim"] = False

    with pytest.raises(ValueError, match="comparable"):
        runner._verified_baseline_metrics(matrix, baseline, corpus_path=CORPUS)


def test_authoritative_worker_arguments_use_frozen_lexical_winner(tmp_path):
    runner = _runner_module()
    cache = tmp_path.resolve()
    lexical = runner._lexical_ablation_worker_arguments(cache, 30.0)
    assert [argv[argv.index("--lexical-config") + 1] for argv in lexical] == [
        "L0",
        "L1",
        "L2",
        "L3",
        "L4",
    ]
    candidate = runner._candidate_worker_arguments(
        {
            "embedding": {
                "id": "BAAI/bge-small-en-v1.5",
                "variant_id": "float32-384d",
            },
            "reranker": None,
        },
        cache,
        30.0,
        lexical_config="L3",
    )
    assert candidate[candidate.index("--lexical-config") + 1] == "L3"


def test_embeddinggemma_loader_uses_pinned_native_sentence_transformer(tmp_path, monkeypatch):
    runner = _runner_module()
    selection = runner.load_model_selection(
        BENCHMARK / "model-matrix-v1.json",
        CORPUS,
        model_id="google/embeddinggemma-300m",
        variant_id="float32-128d",
    )
    calls = []

    class Tokenizer:
        padding_side = None
        truncation_side = None

    class Model:
        tokenizer = Tokenizer()
        max_seq_length = None

        def encode(self, texts, **options):
            calls.append((list(texts), options))
            return [[1.0] * 768 for _text in texts]

    def sentence_transformer(model_id, **options):
        calls.append((model_id, options))
        return Model()

    monkeypatch.setitem(
        sys.modules,
        "sentence_transformers",
        type("SentenceTransformers", (), {"SentenceTransformer": sentence_transformer}),
    )

    encoder = runner._load_transformer_embedding(
        selection,
        cache_root=tmp_path,
        local_files_only=True,
        trust_remote_code=False,
    )
    encoded = encoder(
        ["task: search result | query: cache"],
        batch_size=8,
        max_length=512,
        pooling="mean",
        padding_side="right",
        truncation_side="right",
    )

    assert calls[0] == (
        "google/embeddinggemma-300m",
        {
            "cache_folder": str(tmp_path),
            "local_files_only": True,
            "model_kwargs": {"torch_dtype": "float32"},
            "revision": "57c266a740f537b4dc058e1b0cda161fd15afa75",
            "trust_remote_code": False,
        },
    )
    assert calls[1][1] == {
        "batch_size": 8,
        "convert_to_numpy": True,
        "normalize_embeddings": False,
        "show_progress_bar": False,
    }
    assert len(encoded) == 1
    assert len(encoded[0]) == 768


def test_bge_m3_loader_uses_pinned_native_sparse_linear_asset(tmp_path, monkeypatch):
    torch = pytest.importorskip("torch")
    runner = _runner_module()
    selection = runner.load_model_selection(
        BENCHMARK / "model-matrix-v1.json",
        CORPUS,
        model_id="BAAI/bge-m3",
        variant_id="float32-1024d",
    )
    asset = tmp_path / "sparse_linear.pt"
    weight = torch.zeros((1, 1024), dtype=torch.float32)
    weight[0, 0] = 1.0
    torch.save({"weight": weight}, asset)
    monkeypatch.setattr(runner, "BGE_M3_SPARSE_LINEAR_SHA256", runner._sha256_file(asset))
    calls = []

    class Tokenizer:
        padding_side = "right"
        truncation_side = "right"
        cls_token_id = 0
        eos_token_id = 1
        pad_token_id = 2
        unk_token_id = 3

        def __call__(self, texts, **options):
            calls.append((list(texts), options))
            return {
                "input_ids": torch.tensor([[0, 42, 7], [0, 42, 8]][: len(texts)]),
                "attention_mask": torch.ones((len(texts), 3), dtype=torch.long),
            }

    class Model:
        def eval(self):
            return self

        def __call__(self, **batch):
            hidden = torch.zeros((len(batch["input_ids"]), 3, 1024), dtype=torch.float32)
            hidden[:, 0, 0] = 1.0
            hidden[:, 1, 0] = 0.5
            hidden[:, 2, 0] = 0.25
            return type("Output", (), {"last_hidden_state": hidden})()

    class AutoTokenizer:
        @staticmethod
        def from_pretrained(*args, **kwargs):
            calls.append((args, kwargs))
            return Tokenizer()

    class AutoModel:
        @staticmethod
        def from_pretrained(*args, **kwargs):
            calls.append((args, kwargs))
            return Model()

    monkeypatch.setitem(
        sys.modules,
        "transformers",
        type("Transformers", (), {"AutoModel": AutoModel, "AutoTokenizer": AutoTokenizer}),
    )
    monkeypatch.setitem(
        sys.modules,
        "huggingface_hub",
        type(
            "Hub",
            (),
            {
                "hf_hub_download": staticmethod(
                    lambda **options: calls.append(("asset", options)) or str(asset)
                )
            },
        ),
    )

    encoder = runner._load_transformer_embedding(
        selection,
        cache_root=tmp_path,
        local_files_only=True,
        trust_remote_code=False,
    )
    encoded = encoder(
        ["first", "second"],
        batch_size=8,
        max_length=512,
        pooling="cls",
        padding_side="right",
        truncation_side="right",
    )

    assert encoded["dense_vecs"].shape == (2, 1024)
    assert encoded["lexical_weights"] == [
        {"42": 0.5, "7": 0.25},
        {"42": 0.5, "8": 0.25},
    ]
    asset_call = next(options for marker, options in calls if marker == "asset")
    assert asset_call == {
        "cache_dir": str(tmp_path),
        "filename": "sparse_linear.pt",
        "local_files_only": True,
        "repo_id": "BAAI/bge-m3",
        "revision": "5617a9f61b028005a4858fdac845db406aefb181",
    }


@pytest.mark.parametrize("bad", [float("nan"), float("inf")])
def test_embedding_adapter_rejects_nonfinite_and_invalid_vectors(bad):
    np = pytest.importorskip("numpy")
    runner = _runner_module()
    selection = runner.load_model_selection(
        BENCHMARK / "model-matrix-v1.json",
        CORPUS,
        model_id="BAAI/bge-small-en-v1.5",
        variant_id="float32-384d",
    )

    def encoder(texts, **options):
        del options
        vectors = np.ones((len(texts), 384), dtype=np.float32)
        vectors[0, 0] = bad
        return vectors

    adapter = runner.ModelEmbeddingAdapter(selection, encoder=encoder)
    with pytest.raises(ValueError, match="finite"):
        adapter.rank(
            "query",
            runner.QueryScope(projects=("alpha",), temporal_mode="current", as_of=None),
            [_lexical_candidate(runner, "one", "document")],
            limit=1,
        )


def test_usearch_exact_requires_numpy_id_and_score_parity():
    np = pytest.importorskip("numpy")
    runner = _runner_module()
    vectors = np.asarray([[1.0, 0.0], [0.0, 1.0]], dtype=np.float32)
    query = np.asarray([1.0, 0.0], dtype=np.float32)

    def matching(_vectors, _query, limit, exact):
        assert exact is True
        return np.asarray([0, 1])[:limit], np.asarray([1.0, 0.0])[:limit]

    ids, scores = runner._search_vectors(
        vectors, query, ("one", "two"), 2, backend="usearch-exact", usearch_search=matching
    )
    assert ids == ["one", "two"]
    assert scores == pytest.approx([1.0, 0.0])

    def mismatching(_vectors, _query, limit, exact):
        del limit, exact
        return np.asarray([1, 0]), np.asarray([1.0, 0.0])

    with pytest.raises(ValueError, match="parity"):
        runner._search_vectors(
            vectors,
            query,
            ("one", "two"),
            2,
            backend="usearch-exact",
            usearch_search=mismatching,
        )


def test_reranker_runs_all_depths_from_one_frozen_candidate_list():
    runner = _runner_module()
    selection = runner.load_model_selection(
        BENCHMARK / "model-matrix-v1.json",
        CORPUS,
        model_id="BAAI/bge-small-en-v1.5",
        variant_id="float32-384d",
        reranker_id="BAAI/bge-reranker-v2-m3",
    )
    candidates = [
        runner.ScoredCandidate(_lexical_candidate(runner, str(index), str(index)), 10 - index)
        for index in range(55)
    ]
    seen = []

    def scorer(inputs, **options):
        seen.append((list(inputs), options))
        return list(reversed(range(len(inputs))))

    adapter = runner.ModelRerankerAdapter(selection, scorer=scorer)
    ranked_by_depth = adapter.rank_all(
        "query",
        runner.QueryScope(projects=("alpha",), temporal_mode="current", as_of=None),
        candidates,
        limit=55,
    )

    assert len(seen) == 3
    assert [len(inputs) for inputs, _options in seen] == [10, 20, 50]
    frozen_ids = [str(i) for i in range(55)]
    assert adapter.last_trace["pre_rerank_candidate_ids"] == frozen_ids
    assert adapter.last_trace["pre_rerank_fingerprint"] == hashlib.sha256(
        canonical_json_bytes(frozen_ids)
    ).hexdigest()
    assert set(adapter.last_trace["depths"]) == {"10", "20", "50"}
    for depth, ranked in ranked_by_depth.items():
        assert [item.evidence_id for item in ranked[depth:]] == frozen_ids[depth:]
        assert adapter.last_trace["depths"][str(depth)]["candidate_ids"] == frozen_ids[:depth]


def test_qwen_longest_first_preserves_fixed_prefix_suffix_and_balances_pair():
    runner = _runner_module()
    matrix = json.loads((BENCHMARK / "model-matrix-v1.json").read_text(encoding="utf-8"))
    formatting = next(
        item["formatting"]
        for item in matrix["rerankers"]
        if item["id"] == "Qwen/Qwen3-Reranker-0.6B"
    )

    class Tokenizer:
        def __init__(self):
            self.ids = {}

        def __call__(self, text, add_special_tokens=False):
            assert add_special_tokens is False
            tokens = re.findall(r"<\|[^|]+\|>|\w+|[^\w\s]", text)
            return {"input_ids": [self.ids.setdefault(token, len(self.ids) + 1) for token in tokens]}

    tokenizer = Tokenizer()
    query = " ".join(f"query{i}" for i in range(800))
    document = " ".join(f"document{i}" for i in range(900))
    built = runner._qwen_reranker_input_ids(
        tokenizer, formatting, query, document, formatting["max_length_tokens"]
    )
    suffix_ids = tokenizer(formatting["assistant_suffix"], add_special_tokens=False)["input_ids"]
    prefix_ids = tokenizer(formatting["system_prefix"], add_special_tokens=False)["input_ids"]

    assert len(built["input_ids"]) == formatting["max_length_tokens"]
    assert built["input_ids"][: len(prefix_ids)] == prefix_ids
    assert built["input_ids"][-len(suffix_ids) :] == suffix_ids
    assert abs(built["query_tokens_kept"] - built["document_tokens_kept"]) <= 1
    assert built["query_tokens_kept"] < 800
    assert built["document_tokens_kept"] < 900


def test_model_cache_environment_is_scoped_and_offline_by_default(tmp_path, monkeypatch):
    runner = _runner_module()
    monkeypatch.setenv("HF_HOME", "original")
    before = dict(os.environ)
    cache = tmp_path / "isolated"

    with runner.model_cache_environment(cache, allow_download=False) as acquisition:
        assert acquisition == "offline-local-files-only"
        assert os.environ["HF_HUB_OFFLINE"] == "1"
        assert Path(os.environ["HF_HOME"]).is_relative_to(cache.resolve())
        assert Path(os.environ["TORCH_HOME"]).is_relative_to(cache.resolve())

    assert dict(os.environ) == before
    with runner.model_cache_environment(cache, allow_download=True) as acquisition:
        assert acquisition == "download-allowed-explicit-cache"
        assert os.environ["HF_HUB_OFFLINE"] == "0"
    assert dict(os.environ) == before


def test_real_cli_contract_requires_every_explicit_selector(tmp_path):
    parser = _runner_module()._parser()
    base = ["--adapter", "model-matrix"]
    for args in (
        base,
        base + ["--model-id", "BAAI/bge-small-en-v1.5"],
        base
        + [
            "--model-id",
            "BAAI/bge-small-en-v1.5",
            "--variant-id",
            "float32-384d",
            "--cache-root",
            str(tmp_path),
        ],
    ):
        parsed = parser.parse_args(args)
        with pytest.raises(ValueError, match="requires explicit"):
            _runner_module()._validate_cli_args(parsed)

    parsed = parser.parse_args(
        base
        + [
            "--model-id",
            "BAAI/bge-small-en-v1.5",
            "--variant-id",
            "float32-384d",
            "--cache-root",
            str(tmp_path),
            "--lexical-config",
            "L0",
            "--vector-backend",
            "numpy-exact",
        ]
    )
    with pytest.raises(ValueError, match="deadline-seconds"):
        _runner_module()._validate_cli_args(parsed)
    parsed.deadline_seconds = 30.0
    _runner_module()._validate_cli_args(parsed)


def test_internal_qwen_worker_uses_overall_deadline_for_lexical_lifetime(
    tmp_path, monkeypatch
):
    np = pytest.importorskip("numpy")
    runner = _runner_module()
    now = [0.0]
    monkeypatch.setattr(runner.time, "perf_counter", lambda: now[0])

    def delayed_loader(*args, **kwargs):
        del args, kwargs
        now[0] = 31.0
        return lambda texts, **options: np.ones((len(texts), 384), dtype=np.float32)

    monkeypatch.setattr(runner, "_load_transformer_embedding", delayed_loader)
    output = tmp_path / "qwen-worker-report.json"
    result = runner.main(
        [
            "--internal-worker",
            "--adapter",
            "model-matrix",
            "--model-id",
            "Qwen/Qwen3-Embedding-0.6B",
            "--variant-id",
            "float32-384d",
            "--cache-root",
            str(tmp_path / "cache"),
            "--lexical-config",
            "L0",
            "--vector-backend",
            "numpy-exact",
            "--deadline-seconds",
            "1200",
            "--output",
            str(output),
        ]
    )

    assert result in {0, 2}
    report = json.loads(output.read_bytes())
    assert report["effective_mode"] == "model-matrix"
    assert report["measurements"]["model_load_ms"] == 31_000.0


def test_model_worker_parent_timeout_remains_hard_bound(tmp_path, monkeypatch):
    import inspect

    runner = _runner_module()
    assert runner.DEFAULT_LEXICAL_DEADLINE_SECONDS == 30.0
    assert (
        inspect.signature(runner.run_benchmark)
        .parameters["lexical_deadline_seconds"]
        .default
        == 30.0
    )
    observed = []

    def timed_out(command, *, timeout, env=None):
        del command, env
        observed.append(timeout)
        raise TimeoutError("real benchmark worker deadline exceeded")

    monkeypatch.setattr(runner, "_run_process_tree", timed_out)
    with pytest.raises(TimeoutError, match="deadline exceeded"):
        runner._run_bounded_model_worker(
            ["--adapter", "model-matrix"], deadline_seconds=1200.0
        )
    assert observed == [1200.0]


def test_injected_real_run_reports_provenance_but_never_quality_or_release(tmp_path):
    np = pytest.importorskip("numpy")
    runner = _runner_module()
    corpus = runner.load_corpus(CORPUS, SCHEMA)
    loader_calls = []

    def loader(selection, **options):
        loader_calls.append((selection.embedding["id"], options, dict(os.environ)))

        def encode(texts, **encode_options):
            del encode_options
            vectors = np.zeros((len(texts), 384), dtype=np.float32)
            for row, text in enumerate(texts):
                vectors[row, hash(text) % 384] = 1.0
            return vectors

        return encode

    output = tmp_path / "report.json"
    report = runner.run_benchmark(
        corpus,
        corpus_path=CORPUS,
        cache_root=tmp_path / "cache",
        adapter="model-matrix",
        matrix_path=BENCHMARK / "model-matrix-v1.json",
        model_id="BAAI/bge-small-en-v1.5",
        variant_id="float32-384d",
        lexical_config="L0",
        vector_backend="numpy-exact",
        model_loader=loader,
        raw_output_written=True,
    )

    assert loader_calls[0][1] == {
        "cache_root": (tmp_path / "cache").resolve(),
        "local_files_only": True,
        "trust_remote_code": False,
    }
    assert loader_calls[0][2]["HF_HUB_OFFLINE"] == "1"
    assert report["adapter_kind"] == "model-matrix"
    assert report["model_id"] == "BAAI/bge-small-en-v1.5"
    assert report["variant_id"] == "float32-384d"
    assert report["revision"] == "5c38ec7c405ec4b44b94cc5a9bb96e735b38267a"
    assert report["matrix_sha256"] == runner._sha256_file(BENCHMARK / "model-matrix-v1.json")
    assert report["corpus_sha256"] == runner._sha256_file(CORPUS)
    assert report["acquisition_mode"] == "offline-local-files-only"
    assert report["vector_backend"] == "numpy-exact"
    assert report["quality_claim"] is False
    assert report["release_evidence"] is False
    assert report["gates"]["release_evidence"] is False
    assert all("pre_rerank_candidate_ids" in trace for trace in report["traces"])
    assert not output.exists()


def test_single_provenance_valid_run_is_quality_candidate_never_release(tmp_path, monkeypatch):
    np = pytest.importorskip("numpy")
    runner = _runner_module()
    corpus = runner.load_corpus(CORPUS, SCHEMA)

    def load_encoder(*args, **kwargs):
        del args, kwargs

        def encode(texts, **options):
            del options
            vectors = np.ones((len(texts), 384), dtype=np.float32)
            vectors[:, 1] = np.arange(1, len(texts) + 1)
            return vectors

        return encode

    monkeypatch.setattr(runner, "_load_transformer_embedding", load_encoder)
    report = runner.run_benchmark(
        corpus,
        corpus_path=CORPUS,
        cache_root=tmp_path / "cache",
        adapter="model-matrix",
        model_id="BAAI/bge-small-en-v1.5",
        variant_id="float32-384d",
        lexical_config="L0",
        vector_backend="numpy-exact",
        raw_output_written=True,
    )

    assert report["quality_claim"] is False
    assert report["release_evidence"] is False
    assert report["gates"]["release_evidence"] is False
    assert report["requested_mode"] == "model-matrix"
    assert report["effective_mode"] == "model-matrix"
    assert report["fallback_reason"] is None

    stdout_only = runner.run_benchmark(
        corpus,
        corpus_path=CORPUS,
        cache_root=tmp_path / "stdout-cache",
        adapter="model-matrix",
        model_id="BAAI/bge-small-en-v1.5",
        variant_id="float32-384d",
        lexical_config="L0",
        vector_backend="numpy-exact",
        raw_output_written=False,
    )
    assert stdout_only["quality_claim"] is False


def test_two_real_runs_reuse_model_cache_with_unique_cleaned_workspaces(tmp_path):
    np = pytest.importorskip("numpy")
    runner = _runner_module()
    corpus = runner.load_corpus(CORPUS, SCHEMA)
    cache = tmp_path / "persistent-model-cache"
    cache.mkdir()
    model_sentinel = cache / "model-sentinel.bin"
    model_sentinel.write_bytes(b"model")

    def encoder(texts, **options):
        del options
        return np.ones((len(texts), 1024), dtype=np.float32)

    reports = [
        runner.run_benchmark(
            corpus,
            corpus_path=CORPUS,
            cache_root=cache,
            adapter="model-matrix",
            model_id="BAAI/bge-m3",
            variant_id="float32-1024d",
            lexical_config="L0",
            vector_backend="numpy-exact",
            encoder=encoder,
        )
        for _ in range(2)
    ]

    assert all(report["effective_mode"] == "model-matrix" for report in reports)
    assert model_sentinel.read_bytes() == b"model"
    assert not (cache / "lexical-L0.sqlite3").exists()
    assert (cache / "runs").is_dir()
    assert not list((cache / "runs").iterdir())
    isolation = [report["methodology"]["cache_isolation"] for report in reports]
    assert isolation[0] == isolation[1]
    assert "nonce" not in json.dumps(isolation).casefold()


def test_bge_m3_report_separates_dense_and_learned_sparse_traces_and_bytes(tmp_path):
    np = pytest.importorskip("numpy")
    runner = _runner_module()
    corpus = runner.load_corpus(CORPUS, SCHEMA)

    def encoder(texts, **options):
        del options
        dense = np.zeros((len(texts), 1024), dtype=np.float32)
        dense[:, 0] = 1.0
        return {
            "dense_vecs": dense,
            "lexical_weights": [{str(index + 10): 0.5} for index, _text in enumerate(texts)],
        }

    report = runner.run_benchmark(
        corpus,
        corpus_path=CORPUS,
        cache_root=tmp_path / "cache",
        adapter="model-matrix",
        model_id="BAAI/bge-m3",
        variant_id="float32-1024d",
        lexical_config="L0",
        vector_backend="numpy-exact",
        encoder=encoder,
    )

    assert report["effective_mode"] == "model-matrix"
    assert report["measurements"]["learned_sparse_bytes"] > 0
    assert report["measurements"]["measurement_status"]["learned_sparse_bytes"] == "measured"
    assert all(
        set(trace["embedding_signals"]) == {
            "dense_candidate_ids",
            "fusion",
            "learned_sparse_candidate_ids",
        }
        for trace in report["traces"]
    )


def test_real_run_failure_cleans_workspace_and_retains_model_cache(tmp_path):
    runner = _runner_module()
    corpus = runner.load_corpus(CORPUS, SCHEMA)
    cache = tmp_path / "persistent-model-cache"
    cache.mkdir()
    sentinel = cache / "model-sentinel"
    sentinel.write_text("keep", encoding="utf-8")

    def interrupted(*args, **kwargs):
        del args, kwargs
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        runner.run_benchmark(
            corpus,
            corpus_path=CORPUS,
            cache_root=cache,
            adapter="model-matrix",
            model_id="BAAI/bge-small-en-v1.5",
            variant_id="float32-384d",
            lexical_config="L0",
            vector_backend="numpy-exact",
            model_loader=interrupted,
        )

    assert sentinel.read_text(encoding="utf-8") == "keep"
    assert not list((cache / "runs").iterdir())


def test_real_run_ignores_stale_workspace_and_rejects_symlinked_runs(tmp_path, monkeypatch):
    np = pytest.importorskip("numpy")
    runner = _runner_module()
    corpus = runner.load_corpus(CORPUS, SCHEMA)
    cache = tmp_path / "cache"
    stale = cache / "runs" / ("0" * 32)
    stale.mkdir(parents=True)
    os.chmod(cache / "runs", 0o700)
    stale_sentinel = stale / "stale"
    stale_sentinel.write_text("untouched", encoding="utf-8")
    tokens = iter(("0" * 32, "1" * 32))
    monkeypatch.setattr(runner.secrets, "token_hex", lambda _size: next(tokens))
    monkeypatch_target = tmp_path / "outside"
    monkeypatch_target.mkdir()

    report = runner.run_benchmark(
        corpus,
        corpus_path=CORPUS,
        cache_root=cache,
        adapter="model-matrix",
        model_id="BAAI/bge-small-en-v1.5",
        variant_id="float32-384d",
        lexical_config="L0",
        vector_backend="numpy-exact",
        encoder=lambda texts, **options: np.ones((len(texts), 384), dtype=np.float32),
    )
    assert report["effective_mode"] == "model-matrix"
    assert stale_sentinel.read_text(encoding="utf-8") == "untouched"
    assert list((cache / "runs").iterdir()) == [stale]

    second_cache = tmp_path / "symlink-cache"
    second_cache.mkdir()
    try:
        (second_cache / "runs").symlink_to(monkeypatch_target, target_is_directory=True)
    except OSError:
        pytest.skip("directory symlinks unavailable")
    with pytest.raises(ValueError, match="runs.*symlink|reparse"):
        runner.run_benchmark(
            corpus,
            corpus_path=CORPUS,
            cache_root=second_cache,
            adapter="model-matrix",
            model_id="BAAI/bge-small-en-v1.5",
            variant_id="float32-384d",
            lexical_config="L0",
            vector_backend="numpy-exact",
            encoder=lambda texts, **options: np.ones((len(texts), 384), dtype=np.float32),
        )
    assert not list(monkeypatch_target.iterdir())


def test_online_acquisition_can_never_be_quality_evidence(tmp_path, monkeypatch):
    np = pytest.importorskip("numpy")
    runner = _runner_module()
    corpus = runner.load_corpus(CORPUS, SCHEMA)

    def load_encoder(*args, **kwargs):
        del args, kwargs
        return lambda texts, **options: np.ones((len(texts), 384), dtype=np.float32)

    monkeypatch.setattr(runner, "_load_transformer_embedding", load_encoder)
    report = runner.run_benchmark(
        corpus,
        corpus_path=CORPUS,
        cache_root=tmp_path / "cache",
        adapter="model-matrix",
        model_id="BAAI/bge-small-en-v1.5",
        variant_id="float32-384d",
        lexical_config="L0",
        vector_backend="numpy-exact",
        allow_download=True,
        raw_output_written=True,
    )

    assert report["acquisition_mode"] == "download-allowed-explicit-cache"
    assert report["quality_claim"] is False


def test_forged_worker_environment_and_argument_never_self_attest(tmp_path, monkeypatch):
    np = pytest.importorskip("numpy")
    runner = _runner_module()
    corpus = runner.load_corpus(CORPUS, SCHEMA)
    monkeypatch.setenv("LLM_WIKI_BENCHMARK_WORKER", "forged")
    monkeypatch.setattr(
        runner,
        "_load_transformer_embedding",
        lambda *args, **kwargs: lambda texts, **options: np.ones(
            (len(texts), 384), dtype=np.float32
        ),
    )

    report = runner.run_benchmark(
        corpus,
        corpus_path=CORPUS,
        cache_root=tmp_path / "cache",
        adapter="model-matrix",
        model_id="BAAI/bge-small-en-v1.5",
        variant_id="float32-384d",
        lexical_config="L0",
        vector_backend="numpy-exact",
        raw_output_written=True,
    )

    assert report["quality_claim"] is False
    assert report["release_evidence"] is False


def test_prefetch_emits_non_quality_receipt_without_running_queries(tmp_path, monkeypatch):
    runner = _runner_module()
    selection = runner.load_model_selection(
        BENCHMARK / "model-matrix-v1.json",
        CORPUS,
        model_id="BAAI/bge-small-en-v1.5",
        variant_id="float32-384d",
    )
    calls = []
    monkeypatch.setattr(
        runner,
        "_load_transformer_embedding",
        lambda *args, **kwargs: calls.append("embedding"),
    )
    receipt = runner.prefetch_models(selection, cache_root=tmp_path / "cache")

    assert calls == ["embedding"]
    assert receipt["artifact_kind"] == "model-acquisition-receipt"
    assert receipt["quality_claim"] is False
    assert receipt["acquisition_mode"] == "download-allowed-explicit-cache"


def test_rrf_confidence_is_scale_invariant_and_normalized():
    runner = _runner_module()
    candidates = [
        runner.Candidate(str(index), str(index), f"synthetic/{index}", "EN", "p", "active", "2025-01-01", None, (), str(index))
        for index in range(3)
    ]
    first = [runner.ScoredCandidate(candidate, score) for candidate, score in zip(candidates, (9, 6, 3))]
    second = [runner.ScoredCandidate(candidate, score) for candidate, score in zip(reversed(candidates), (900, 600, 300))]
    fused = runner._fuse_rankings((first, second), candidates, limit=3)
    scaled = runner._fuse_rankings(
        (
            [runner.ScoredCandidate(item.candidate, item.score * 1000) for item in first],
            [runner.ScoredCandidate(item.candidate, item.score / 1000) for item in second],
        ),
        candidates,
        limit=3,
    )

    assert all(0.0 <= item.score <= 1.0 for item in fused)
    assert [(item.evidence_id, item.score) for item in fused] == [
        (item.evidence_id, item.score) for item in scaled
    ]


def test_bounded_worker_timeout_kills_and_cleans_report(tmp_path):
    runner = _runner_module()
    report = tmp_path / "worker.json"
    command = [
        sys.executable,
        "-c",
        f"import pathlib,time; pathlib.Path({str(report)!r}).write_text('partial'); time.sleep(60)",
    ]

    with pytest.raises(TimeoutError, match="deadline"):
        runner._run_process_tree(command, timeout=0.1)

    report.unlink(missing_ok=True)
    assert not report.exists()


def test_locked_environment_rejects_installed_version_mismatch(monkeypatch):
    runner = _runner_module()
    locked = runner._locked_package_versions(ROOT / "uv.lock")
    installed = {name: version for name, version in locked.items()}
    installed["numpy"] = "0.0-forged"

    with pytest.raises(ValueError, match="numpy.*locked"):
        runner._verify_locked_environment(
            "numpy-exact",
            lexical_config="L0",
            version_getter=lambda name: installed[name],
        )


def test_document_index_throughput_excludes_lexical_and_model_load():
    runner = _runner_module()
    measured = runner._resource_measurements(
        [10.0, 2.0, 3.0],
        build_time_ms=10_000.0,
        indexing_duration_ms=2_000.0,
        chunk_count=20,
        index_size_bytes=100,
        peak_rss_bytes=200,
        peak_rss_status="measured",
    )

    assert measured["indexing_throughput_chunks_per_second"] == 10.0


def test_injected_clock_separates_lexical_model_and_document_phases(tmp_path):
    np = pytest.importorskip("numpy")
    runner = _runner_module()
    corpus = runner.load_corpus(CORPUS, SCHEMA)
    ticks = iter((0.0, 1.0, 10.0, 12.0, 20.0, 24.0))

    report = runner.run_benchmark(
        corpus,
        corpus_path=CORPUS,
        cache_root=tmp_path / "cache",
        adapter="model-matrix",
        model_id="BAAI/bge-small-en-v1.5",
        variant_id="float32-384d",
        lexical_config="L0",
        vector_backend="numpy-exact",
        encoder=lambda texts, **options: np.ones((len(texts), 384), dtype=np.float32),
        clock=lambda: next(ticks),
    )

    assert report["measurements"]["lexical_build_ms"] == 1_000.0
    assert report["measurements"]["model_load_ms"] == 2_000.0
    assert report["measurements"]["document_encoding_index_build_ms"] == 4_000.0
    assert report["measurements"]["indexing_throughput_chunks_per_second"] == (
        len(runner.build_candidates(corpus)) / 4.0
    )


def test_model_load_failure_degrades_to_lexical_and_removes_semantic_artifacts(tmp_path):
    runner = _runner_module()
    corpus = runner.load_corpus(CORPUS, SCHEMA)
    cache = tmp_path / "cache"

    def unavailable(*args, **kwargs):
        del args, kwargs
        raise RuntimeError("model unavailable")

    report = runner.run_benchmark(
        corpus,
        corpus_path=CORPUS,
        cache_root=cache,
        adapter="model-matrix",
        model_id="BAAI/bge-small-en-v1.5",
        variant_id="float32-384d",
        lexical_config="L0",
        vector_backend="numpy-exact",
        model_loader=unavailable,
    )

    assert report["requested_mode"] == "model-matrix"
    assert report["effective_mode"] == "lexical-L0"
    assert report["fallback_reason"] == "RuntimeError: model unavailable"
    assert report["quality_claim"] is False
    assert report["release_evidence"] is False
    assert report["gates"]["degraded"] is True
    assert not (cache / "lexical-L0.sqlite3").exists()
    assert not list((cache / "runs").iterdir())


@pytest.mark.parametrize(
    "failure",
    [
        TypeError("bad model type"),
        AssertionError("model assertion"),
        MemoryError("model memory"),
    ],
)
def test_every_ordinary_model_failure_degrades_to_lexical(tmp_path, failure):
    runner = _runner_module()
    corpus = runner.load_corpus(CORPUS, SCHEMA)

    def unavailable(*args, **kwargs):
        del args, kwargs
        raise failure

    report = runner.run_benchmark(
        corpus,
        corpus_path=CORPUS,
        cache_root=tmp_path / "cache",
        adapter="model-matrix",
        model_id="BAAI/bge-small-en-v1.5",
        variant_id="float32-384d",
        lexical_config="L0",
        vector_backend="numpy-exact",
        model_loader=unavailable,
    )

    assert report["effective_mode"] == "lexical-L0"
    assert report["fallback_reason"] == f"{type(failure).__name__}: {failure}"
    assert report["gates"]["degraded"] is True


@pytest.mark.parametrize("failure", [KeyboardInterrupt(), SystemExit(7)])
def test_control_flow_model_exceptions_propagate_after_cleanup(tmp_path, failure):
    runner = _runner_module()
    corpus = runner.load_corpus(CORPUS, SCHEMA)
    cache = tmp_path / "cache"
    before = dict(os.environ)

    def interrupted(*args, **kwargs):
        del args, kwargs
        raise failure

    with pytest.raises(type(failure)):
        runner.run_benchmark(
            corpus,
            corpus_path=CORPUS,
            cache_root=cache,
            adapter="model-matrix",
            model_id="BAAI/bge-small-en-v1.5",
            variant_id="float32-384d",
            lexical_config="L0",
            vector_backend="numpy-exact",
            model_loader=interrupted,
        )

    assert dict(os.environ) == before
    assert not (cache / "lexical-L0.sqlite3").exists()
    assert not list((cache / "runs").iterdir())


def test_real_reranker_report_contains_all_depth_metrics_from_same_fingerprint(tmp_path):
    np = pytest.importorskip("numpy")
    runner = _runner_module()
    corpus = runner.load_corpus(CORPUS, SCHEMA)

    def encoder(texts, **options):
        del options
        vectors = np.ones((len(texts), 384), dtype=np.float32)
        vectors[:, 0] = np.arange(1, len(texts) + 1)
        return vectors

    def scorer(inputs, **options):
        del options
        return list(range(len(inputs)))

    report = runner.run_benchmark(
        corpus,
        corpus_path=CORPUS,
        cache_root=tmp_path / "cache",
        adapter="model-matrix",
        model_id="BAAI/bge-small-en-v1.5",
        variant_id="float32-384d",
        reranker_id="BAAI/bge-reranker-v2-m3",
        lexical_config="L0",
        vector_backend="numpy-exact",
        encoder=encoder,
        scorer=scorer,
    )

    assert set(report["reranker"]["depth_metrics"]) == {"10", "20", "50"}
    assert all(
        set(report["reranker"]["depth_metrics"][depth])
        == {
            "overall",
            "slices",
            "duration_ms",
            "inference_latencies_ms",
            "warm_latency_p95_ms",
            "shared_resources",
        }
        for depth in ("10", "20", "50")
    )
    for trace in report["traces"]:
        reranker_trace = trace["reranker"]
        assert set(reranker_trace["depths"]) == {"10", "20", "50"}
        assert all(
            reranker_trace["depths"][depth]["candidate_ids"]
            == reranker_trace["pre_rerank_candidate_ids"][: int(depth)]
            for depth in ("10", "20", "50")
        )


def test_embedding_is_collected_before_reranker_load_and_frozen_lists_survive(tmp_path):
    np = pytest.importorskip("numpy")
    runner = _runner_module()
    corpus = runner.load_corpus(CORPUS, SCHEMA)
    encoder_reference = [None]
    destroyed = []

    class Encoder:
        def __call__(self, texts, **options):
            del options
            vectors = np.ones((len(texts), 384), dtype=np.float32)
            vectors[:, 0] = np.arange(1, len(texts) + 1)
            return vectors

        def __del__(self):
            destroyed.append(True)

    def model_loader(*args, **kwargs):
        del args, kwargs
        loaded = Encoder()
        encoder_reference[0] = weakref.ref(loaded)
        return loaded

    def reranker_loader(*args, **kwargs):
        del args, kwargs
        assert encoder_reference[0]() is None
        assert destroyed == [True]
        return lambda inputs, **options: list(range(len(inputs)))

    report = runner.run_benchmark(
        corpus,
        corpus_path=CORPUS,
        cache_root=tmp_path / "cache",
        adapter="model-matrix",
        model_id="BAAI/bge-small-en-v1.5",
        variant_id="float32-384d",
        reranker_id="Qwen/Qwen3-Reranker-0.6B",
        lexical_config="L0",
        vector_backend="numpy-exact",
        model_loader=model_loader,
        reranker_loader=reranker_loader,
    )

    assert report["effective_mode"] == "model-matrix"
    assert report["methodology"]["phase_peak_rss"] == (
        "process peak RSS is run-level; per-phase peaks are unavailable"
    )
    assert all(
        trace["pre_rerank_candidate_ids"]
        == trace["reranker"]["pre_rerank_candidate_ids"]
        for trace in report["traces"]
    )


def test_embedding_cache_cleanup_failure_is_not_suppressed(tmp_path, monkeypatch):
    np = pytest.importorskip("numpy")
    runner = _runner_module()
    corpus = runner.load_corpus(CORPUS, SCHEMA)

    class Cuda:
        @staticmethod
        def is_available():
            return True

        @staticmethod
        def empty_cache():
            raise RuntimeError("CUDA cache cleanup failed")

    class Torch:
        cuda = Cuda()

    monkeypatch.setitem(runner.sys.modules, "torch", Torch())

    with pytest.raises(RuntimeError, match="CUDA cache cleanup failed"):
        runner.run_benchmark(
            corpus,
            corpus_path=CORPUS,
            cache_root=tmp_path / "cache",
            adapter="model-matrix",
            model_id="BAAI/bge-small-en-v1.5",
            variant_id="float32-384d",
            lexical_config="L0",
            vector_backend="numpy-exact",
            encoder=lambda texts, **options: np.ones(
                (len(texts), 384), dtype=np.float32
            ),
        )


def test_empty_stderr_worker_failure_reports_termination_details(tmp_path, monkeypatch):
    runner = _runner_module()
    monkeypatch.setattr(
        runner,
        "_run_process_tree",
        lambda *args, **kwargs: subprocess.CompletedProcess(args[0], -9, b"", b""),
    )

    with pytest.raises(
        ValueError,
        match=r"returncode=-9.*timeout_seconds=1200\.0.*termination=signal-9.*stderr=empty",
    ):
        runner._run_bounded_model_worker(
            ["--adapter", "model-matrix"], deadline_seconds=1200.0
        )


def test_reranker_scoring_failure_degrades_whole_run_to_lexical(tmp_path):
    np = pytest.importorskip("numpy")
    runner = _runner_module()
    corpus = runner.load_corpus(CORPUS, SCHEMA)

    def encoder(texts, **options):
        del options
        return np.ones((len(texts), 384), dtype=np.float32)

    def scorer(*args, **kwargs):
        del args, kwargs
        raise RuntimeError("reranker encode failed")

    report = runner.run_benchmark(
        corpus,
        corpus_path=CORPUS,
        cache_root=tmp_path / "cache",
        adapter="model-matrix",
        model_id="BAAI/bge-small-en-v1.5",
        variant_id="float32-384d",
        reranker_id="BAAI/bge-reranker-v2-m3",
        lexical_config="L0",
        vector_backend="numpy-exact",
        encoder=encoder,
        scorer=scorer,
    )

    assert report["effective_mode"] == "lexical-L0"
    assert report["fallback_reason"] == "RuntimeError: reranker encode failed"
    assert report["reranker"] is None
    assert report["quality_claim"] is False
    assert report["gates"]["degraded"] is True


def _candidate_report(runner, selection, spec, *, offset):
    embedding = spec["embedding"]
    reranker = spec["reranker"]
    corpus = runner.load_corpus(CORPUS, SCHEMA)
    candidates = runner.build_candidates(corpus)
    traces = []
    rows = []
    for query in corpus["queries"]:
        eligible = runner.filter_candidates(candidates, runner._query_scope(query))
        required = set(query["required_evidence_spans"])
        ordered = sorted(eligible, key=lambda item: (item.evidence_id not in required, item.evidence_id))
        top_confidence = 1.0 if query["answerability"] == "answerable" else 0.5
        ranked = [
            runner.ScoredCandidate(item, top_confidence - index / 1000)
            for index, item in enumerate(ordered)
        ]
        answer = {
            "abstained": query["answerability"] == "unanswerable",
            "reason": query["allowed_abstention_reason"] if query["answerability"] == "unanswerable" else None,
        }
        row = runner._evaluation_row(query, ranked, answer)
        rows.append(row)
        traces.append(
            {
                "query_id": query["query_id"],
                "ranked_evidence": [
                    {"evidence_id": item.evidence_id, "score": item.score} for item in ranked
                ],
                "ranked_parents": runner._expand_parents(ranked),
                "abstained": answer["abstained"],
                "abstention_reason": answer["reason"],
                "abstention_contract_valid": row["abstention_contract_valid"],
                "latency_ms": 10.0,
                "pre_rerank_candidate_ids": [item.evidence_id for item in ranked] if reranker else None,
                "reranker": None,
            }
        )
        if reranker is not None:
            traces[-1]["reranker"] = {
                "depths": {
                    str(depth): {
                        "ranked_evidence": [
                            {"evidence_id": item.evidence_id, "score": item.score}
                            for item in ranked
                        ],
                        "duration_ms": float(depth),
                        "query_latency_ms": 10.0 + depth,
                        "abstained": answer["abstained"],
                        "abstention_reason": answer["reason"],
                    }
                    for depth in (10, 20, 50)
                }
            }
    overall_metrics = runner._aggregate(rows)
    slice_metrics = {
        language: runner._aggregate([row for row in rows if row["language"] == language])
        for language in ("EN", "RU", "ZH")
    }
    slice_metrics["cross-language"] = runner._aggregate(
        [row for row in rows if row["cross_language"]]
    )
    report = {
        "schema_version": "retrieval-report/v2",
        "corpus_id": "public-synthetic-retrieval-v2",
        "adapter_kind": "model-matrix",
        "quality_claim": True,
        "release_evidence": False,
        "requested_mode": "model-matrix",
        "effective_mode": "model-matrix",
        "fallback_reason": None,
        "model_id": embedding["id"],
        "variant_id": embedding["variant_id"],
        "revision": embedding["revision"],
        "matrix_sha256": selection.matrix_sha256,
        "corpus_sha256": selection.corpus_sha256,
        "benchmark_contract_sha256": runner._sha256_json(selection.matrix["benchmark_contract"]),
        "benchmark_runner_sha256": runner._sha256_file(RUNNER),
        "candidate": spec,
        "methodology": {
            "environment_provenance": runner._environment_provenance("numpy-exact"),
            "confidence": {
                "fusion": "RRF divided by its theoretical maximum; bounded to [0,1]",
                "qa_threshold": 0.54,
                "reranker": (
                    "sigmoid_probability_from_sequence_classification_logit"
                    if reranker is not None and reranker["id"] == "BAAI/bge-reranker-v2-m3"
                    else "softmax_probability_of_yes_over_no"
                    if reranker is not None
                    else None
                ),
            },
        },
        "macro_average": {
            metric: runner.macro_average(slice_metrics, metric)
            for metric in runner.EFFECTIVENESS_FIELDS
        },
        "thresholds": dict(runner.THRESHOLDS),
        "gates": {},
        "traces": traces,
        "acquisition_mode": "offline-local-files-only",
        "vector_backend": "numpy-exact",
        "overall": overall_metrics,
        "slices": slice_metrics,
        "measurements": {
            "warm_latency_p95_ms": 100.0 + offset,
            "peak_rss_bytes": 1_000_000_000 + offset,
            "index_size_bytes": 100_000 + offset,
        },
        "reranker": (
            {
                "model_id": reranker["id"],
                "revision": reranker["revision"],
                "variant_id": reranker["variant_id"],
                "depths": [10, 20, 50],
                "depth_metrics": {
                    str(depth): {
                        "overall": overall_metrics,
                        "slices": slice_metrics,
                        "duration_ms": depth * len(traces),
                        "inference_latencies_ms": [float(depth)] * len(traces),
                        "warm_latency_p95_ms": 10.0 + depth,
                        "shared_resources": [
                            "peak_rss_bytes",
                            "index_size_bytes",
                            "vector_bytes",
                        ],
                    }
                    for depth in (10, 20, 50)
                },
                "formatting": {},
            }
            if reranker is not None
            else None
        ),
    }
    return (
        json.dumps(report, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        + "\n"
    ).encode()


@pytest.mark.parametrize(
    "fabricate",
    [
        lambda report: report["traces"].pop(),
        lambda report: report["traces"].append(dict(report["traces"][0])),
        lambda report: report["traces"][0]["ranked_evidence"].append(
            {"evidence_id": "impossible", "score": 1.0}
        ),
        lambda report: report["traces"][0].__setitem__("required_evidence_spans", []),
        lambda report: report["overall"].__setitem__("mrr_at_10", 1.0 - report["overall"]["mrr_at_10"]),
    ],
)
def test_aggregation_rejects_fabricated_trace_or_metric_evidence(tmp_path, fabricate):
    runner = _runner_module()
    selection = runner.load_model_selection(
        BENCHMARK / "model-matrix-v1.json",
        CORPUS,
        model_id="BAAI/bge-small-en-v1.5",
        variant_id="float32-384d",
    )
    paths = []
    for offset, spec in enumerate(runner.required_candidate_specs(selection.matrix)):
        report = json.loads(_candidate_report(runner, selection, spec, offset=offset))
        if offset == 0:
            fabricate(report)
        path = tmp_path / f"candidate-{offset}.json"
        path.write_bytes(runner._canonical_report_bytes(report))
        paths.append(path)
    repo = tmp_path / "repo"
    baseline = repo / selection.matrix["selection"]["baseline"]["raw_report_path"]
    baseline.parent.mkdir(parents=True)
    baseline.write_bytes((BENCHMARK / "baseline-2026-07-16-retrieval.json").read_bytes())

    with pytest.raises(ValueError, match="trace|metric|gold|eligible"):
        runner.aggregate_selection(
            paths,
            matrix_path=BENCHMARK / "model-matrix-v1.json",
            corpus_path=CORPUS,
            repo_root=repo,
            output_path=repo / "benchmark/results/selection.json",
            measured_at="2026-07-17T12:00:00Z",
        )


def test_aggregation_rejects_abstention_not_derived_from_normalized_confidence(tmp_path):
    runner = _runner_module()
    selection = runner.load_model_selection(
        BENCHMARK / "model-matrix-v1.json",
        CORPUS,
        model_id="BAAI/bge-small-en-v1.5",
        variant_id="float32-384d",
    )
    spec = runner.required_candidate_specs(selection.matrix)[0]
    report = json.loads(_candidate_report(runner, selection, spec, offset=0))
    trace = report["traces"][0]
    trace["abstained"] = not trace["abstained"]

    with pytest.raises(ValueError, match="confidence|abstention"):
        runner._recompute_report_metrics(runner.load_corpus(CORPUS, SCHEMA), report)


def test_aggregation_requires_complete_reports_and_writes_canonical_selection(tmp_path):
    runner = _runner_module()
    selection = runner.load_model_selection(
        BENCHMARK / "model-matrix-v1.json",
        CORPUS,
        model_id="BAAI/bge-small-en-v1.5",
        variant_id="float32-384d",
    )
    specs = runner.required_candidate_specs(selection.matrix)
    raw_paths = []
    for offset, spec in enumerate(specs):
        path = tmp_path / "raw" / f"candidate-{offset}.json"
        path.parent.mkdir(exist_ok=True)
        path.write_bytes(_candidate_report(runner, selection, spec, offset=offset))
        raw_paths.append(path)
    repo = tmp_path / "repo"
    baseline = repo / selection.matrix["selection"]["baseline"]["raw_report_path"]
    baseline.parent.mkdir(parents=True)
    baseline.write_bytes((BENCHMARK / "baseline-2026-07-16-retrieval.json").read_bytes())
    output = repo / "benchmark" / "results" / "synthetic-selection.json"

    with pytest.raises(ValueError, match="complete candidate report set"):
        runner.aggregate_selection(
            raw_paths[:-1],
            matrix_path=BENCHMARK / "model-matrix-v1.json",
            corpus_path=CORPUS,
            repo_root=repo,
            output_path=output,
            measured_at="2026-07-17T12:00:00Z",
        )
    assert not output.exists()

    canonical = raw_paths[0].read_bytes()
    raw_paths[0].write_text(json.dumps(json.loads(canonical), indent=2) + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="canonical JSON"):
        runner.aggregate_selection(
            raw_paths,
            matrix_path=BENCHMARK / "model-matrix-v1.json",
            corpus_path=CORPUS,
            repo_root=repo,
            output_path=output,
            measured_at="2026-07-17T12:00:00Z",
        )
    raw_paths[0].write_bytes(canonical)

    artifact = runner.aggregate_selection(
        raw_paths,
        matrix_path=BENCHMARK / "model-matrix-v1.json",
        corpus_path=CORPUS,
        repo_root=repo,
        output_path=output,
        measured_at="2026-07-17T12:00:00Z",
    )

    assert artifact["release_evidence"] is False
    assert artifact["quality_claim"] is False
    assert artifact["schema_version"] == "retrieval-comparison/v1"
    assert len(artifact["candidate_reports"]) == len(specs)
    assert {
        key: value for key, value in artifact["baseline"].items() if key != "observed_runtime"
    } == selection.matrix["selection"]["baseline"]
    assert artifact["baseline"]["observed_runtime"] == runner._observed_runtime_environment(
        "numpy-exact", lexical_config="L4"
    )
    assert artifact["matrix_sha256"] == selection.matrix_sha256
    assert artifact["matrix_policy_sha256"] == runner.matrix_policy_fingerprint(selection.matrix)
    assert artifact["matrix_policy_sha256"] != artifact["matrix_sha256"]
    assert artifact["corpus_sha256"] == selection.corpus_sha256
    assert output.read_bytes() == canonical_json_bytes(artifact) + b"\n"
    assert artifact["selected"] in [item["candidate"] for item in artifact["pareto"]]
    retained = repo / "benchmark" / "results" / "reports"
    assert retained.is_dir()
    assert len(list(retained.glob("*.json"))) == len(specs)
    for evidence in artifact["candidate_reports"]:
        path = repo / Path(evidence["retained_path"])
        assert path.parent == retained
        assert path.name == f"{evidence['raw_report_sha256']}.json"
        assert hashlib.sha256(path.read_bytes()).hexdigest() == evidence["raw_report_sha256"]
        parsed = json.loads(path.read_bytes())
        assert path.read_bytes() == (
            json.dumps(parsed, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
            + "\n"
        ).encode()
    with pytest.raises(ValueError, match="already exists"):
        runner.aggregate_selection(
            raw_paths,
            matrix_path=BENCHMARK / "model-matrix-v1.json",
            corpus_path=CORPUS,
            repo_root=repo,
            output_path=output,
            measured_at="2026-07-17T12:00:00Z",
        )


def test_complete_no_winner_writes_canonical_nonrelease_evidence(tmp_path, monkeypatch):
    runner = _runner_module()
    selection = runner.load_model_selection(
        BENCHMARK / "model-matrix-v1.json",
        CORPUS,
        model_id="BAAI/bge-small-en-v1.5",
        variant_id="float32-384d",
    )
    raw_paths = []
    for offset, spec in enumerate(runner.required_candidate_specs(selection.matrix)):
        path = tmp_path / "raw" / f"candidate-{offset}.json"
        path.parent.mkdir(exist_ok=True)
        path.write_bytes(_candidate_report(runner, selection, spec, offset=offset))
        raw_paths.append(path)
    repo = tmp_path / "repo"
    baseline = repo / selection.matrix["selection"]["baseline"]["raw_report_path"]
    baseline.parent.mkdir(parents=True)
    baseline.write_bytes((BENCHMARK / "baseline-2026-07-16-retrieval.json").read_bytes())
    output = repo / "benchmark/results/no-winner.json"
    original = runner._selection_report_gates

    def reject_all(matrix, report, embedding, reranker):
        gates, objective = original(matrix, report, embedding, reranker)
        gates["overall"] = False
        return gates, objective

    monkeypatch.setattr(runner, "_selection_report_gates", reject_all)
    artifact = runner.aggregate_selection(
        raw_paths,
        matrix_path=BENCHMARK / "model-matrix-v1.json",
        corpus_path=CORPUS,
        repo_root=repo,
        output_path=output,
        measured_at="2026-07-17T12:00:00Z",
    )

    assert artifact["selected"] is None
    assert artifact["pareto"] == []
    assert artifact["quality_claim"] is False
    assert artifact["release_evidence"] is False
    assert artifact["gates"]["outcome"] == "no-winner"
    assert artifact["gates"]["fallback"] == "current-bm25"
    assert artifact["measurements"] is None
    assert output.read_bytes() == canonical_json_bytes(artifact) + b"\n"

    with pytest.raises(TypeError, match="unexpected keyword"):
        runner._aggregate_reports(
            raw_paths,
            matrix_path=BENCHMARK / "model-matrix-v1.json",
            corpus_path=CORPUS,
            repo_root=tmp_path / "forged-repo",
            output_path=tmp_path / "forged-repo/benchmark/results/no-winner.json",
            measured_at="2026-07-17T12:00:00Z",
            _attested_results=[object() for _path in raw_paths],
        )


def test_mocked_orchestration_is_plumbing_only_and_uses_retained_lexical_winner(
    tmp_path, monkeypatch
):
    runner = _runner_module()
    selection = runner.load_model_selection(
        BENCHMARK / "model-matrix-v1.json",
        CORPUS,
        model_id="BAAI/bge-small-en-v1.5",
        variant_id="float32-384d",
    )
    repo = tmp_path / "repo"
    baseline = repo / selection.matrix["selection"]["baseline"]["raw_report_path"]
    baseline.parent.mkdir(parents=True)
    baseline.write_bytes((BENCHMARK / "baseline-2026-07-16-retrieval.json").read_bytes())
    shared_cache = tmp_path / "cache"
    shared_cache.mkdir()
    sentinel = shared_cache / "prefetched-model.sentinel"
    sentinel.write_bytes(b"prefetched")
    calls = []
    workspace_paths = []
    model_cache_identities = []
    lexical_reports = []

    def transport(argv, *, deadline_seconds):
        calls.append((argv, deadline_seconds))
        worker_cache = Path(argv[argv.index("--cache-root") + 1])
        assert worker_cache == shared_cache.resolve()
        assert (worker_cache / sentinel.name).read_bytes() == b"prefetched"
        lexical_config = argv[argv.index("--lexical-config") + 1]
        if "--model-id" not in argv:
            report = runner.run_benchmark(
                runner.load_corpus(CORPUS, SCHEMA),
                corpus_path=CORPUS,
                matrix_path=BENCHMARK / "model-matrix-v1.json",
                cache_root=tmp_path / f"lexical-{lexical_config}",
                lexical_config=lexical_config,
                test_segmenter=_PinnedJieba(),
            )
            lexical_reports.append(report)
            return runner._WorkerPayload(report, runner._canonical_report_bytes(report))
        workspace = runner._create_run_workspace(worker_cache)
        workspace_paths.append(workspace.path)
        model_cache_identities.append(workspace.model_cache_identity)
        runner._cleanup_run_workspace(workspace)
        model_id = argv[argv.index("--model-id") + 1]
        variant_id = argv[argv.index("--variant-id") + 1]
        reranker_id = argv[argv.index("--reranker-id") + 1] if "--reranker-id" in argv else None
        spec = next(
            item
            for item in runner.required_candidate_specs(selection.matrix)
            if item["embedding"]["id"] == model_id
            and item["embedding"]["variant_id"] == variant_id
            and (item["reranker"]["id"] if item["reranker"] else None) == reranker_id
        )
        report = json.loads(
            _candidate_report(runner, selection, spec, offset=len(calls) - 1)
        )
        report["quality_claim"] = False
        report["methodology"]["lexical_configuration"] = runner.LEXICAL_CONFIGURATIONS[
            lexical_config
        ]
        return runner._WorkerPayload(report, runner._canonical_report_bytes(report))

    monkeypatch.setattr(runner, "_run_bounded_model_worker", transport)
    monkeypatch.setattr(
        runner,
        "_verify_locked_environment",
        lambda *args, **kwargs: {
            "packages": {},
            "package_map_sha256": runner._sha256_json({}),
            "uv_lock_sha256": runner._sha256_file(ROOT / "uv.lock"),
        },
    )

    artifact = runner.orchestrate_selection(
        matrix_path=BENCHMARK / "model-matrix-v1.json",
        corpus_path=CORPUS,
        repo_root=repo,
        output_path=repo / "benchmark/results/selection.json",
        measured_at="2026-07-17T12:00:00Z",
        cache_root=shared_cache,
        deadline_seconds=30.0,
    )

    assert len(calls) == len(runner.required_candidate_specs(selection.matrix)) + 5
    assert all("--model-id" not in argv for argv, _deadline in calls[:5])
    assert "--model-id" in calls[5][0]
    winner = runner._select_lexical_winner(lexical_reports)["id"]
    model_calls = [argv for argv, _deadline in calls if "--model-id" in argv]
    assert all(argv[argv.index("--lexical-config") + 1] == winner for argv in model_calls)
    assert len(set(workspace_paths)) == len(workspace_paths)
    assert len(set(model_cache_identities)) == 1
    assert not list((shared_cache / "runs").iterdir())
    assert artifact["schema_version"] == "retrieval-comparison/v1"
    assert artifact["quality_claim"] is False
    assert artifact["release_evidence"] is False
    lexical_evidence = [item for item in artifact["candidate_reports"] if "lexical_configuration" in item]
    assert {item["lexical_configuration"] for item in lexical_evidence} == set(
        runner.LEXICAL_CONFIGURATIONS
    )
    model_evidence = [item for item in artifact["candidate_reports"] if "candidate" in item]
    assert all(
        json.loads((repo / item["retained_path"]).read_bytes())["quality_claim"] is False
        for item in model_evidence
    )


def test_authoritative_degraded_error_names_requested_candidate_and_fallback(
    tmp_path, monkeypatch
):
    runner = _runner_module()
    selection = runner.load_model_selection(
        BENCHMARK / "model-matrix-v1.json",
        CORPUS,
        model_id="BAAI/bge-small-en-v1.5",
        variant_id="float32-384d",
    )
    requested = runner.required_candidate_specs(selection.matrix)[0]
    report = json.loads(_candidate_report(runner, selection, requested, offset=0))
    report["quality_claim"] = False
    report["effective_mode"] = "lexical-L0"
    report["fallback_reason"] = "OSError: prefetched model not found"

    def transport(argv, **kwargs):
        del kwargs
        if "--model-id" in argv:
            degraded = json.loads(json.dumps(report))
            lexical = argv[argv.index("--lexical-config") + 1]
            degraded["effective_mode"] = f"lexical-{lexical}"
            return runner._WorkerPayload(degraded, runner._canonical_report_bytes(degraded))
        lexical = argv[argv.index("--lexical-config") + 1]
        lexical_report = runner.run_benchmark(
            runner.load_corpus(CORPUS, SCHEMA),
            corpus_path=CORPUS,
            matrix_path=BENCHMARK / "model-matrix-v1.json",
            cache_root=tmp_path / f"lexical-{lexical}",
            lexical_config=lexical,
            test_segmenter=_PinnedJieba(),
        )
        return runner._WorkerPayload(
            lexical_report, runner._canonical_report_bytes(lexical_report)
        )

    monkeypatch.setattr(runner, "_run_bounded_model_worker", transport)

    with pytest.raises(ValueError) as failure:
        runner.orchestrate_selection(
            matrix_path=BENCHMARK / "model-matrix-v1.json",
            corpus_path=CORPUS,
            repo_root=tmp_path / "repo",
            output_path=tmp_path / "repo/benchmark/results/selection.json",
            measured_at="2026-07-17T12:00:00Z",
            cache_root=tmp_path / "cache",
            deadline_seconds=30.0,
        )

    message = str(failure.value)
    assert requested["embedding"]["id"] in message
    assert "fallback_reason=OSError: prefetched model not found" in message


def test_authoritative_orchestration_has_no_public_callback_or_forgeable_result(tmp_path):
    import inspect

    runner = _runner_module()
    assert "worker" not in inspect.signature(runner.orchestrate_selection).parameters
    assert not hasattr(runner, "ParentAttestedResult")
    assert not hasattr(runner, "_ParentAttestedResult")
    assert not hasattr(runner, "_ATTESTATION_CAPABILITY")
    assert not hasattr(runner, "_orchestrate_selection_impl")
    assert not hasattr(runner, "_consume_execution_bound_payload")
    assert not hasattr(runner, "_make_execution_bound_worker")
    with pytest.raises(TypeError):
        runner.orchestrate_selection(
            matrix_path=BENCHMARK / "model-matrix-v1.json",
            corpus_path=CORPUS,
            repo_root=tmp_path,
            output_path=tmp_path / "benchmark/results/selection.json",
            measured_at="2026-07-17T12:00:00Z",
            cache_root=tmp_path / "cache",
            deadline_seconds=30.0,
            worker=lambda spec: spec,
        )


def test_mutated_completeness_helpers_cannot_attest_authoritative_evidence(
    tmp_path, monkeypatch
):
    runner = _runner_module()
    matrix = json.loads((BENCHMARK / "model-matrix-v1.json").read_bytes())
    repo = tmp_path / "repo"
    baseline = repo / matrix["selection"]["baseline"]["raw_report_path"]
    baseline.parent.mkdir(parents=True)
    baseline.write_bytes((BENCHMARK / "baseline-2026-07-16-retrieval.json").read_bytes())
    output = repo / "benchmark/results/no-winner.json"
    trusted_worker = runner._run_bounded_model_worker
    trusted_process_runner = runner._run_process_tree
    aggregate = runner._aggregate_reports

    lexical_argv = runner._lexical_ablation_worker_arguments
    monkeypatch.setattr(
        runner,
        "_lexical_ablation_worker_arguments",
        lambda cache, deadline: lexical_argv(cache, deadline)[:1],
    )
    monkeypatch.setattr(runner, "_select_lexical_winner", lambda _reports: {"id": "L0"})
    monkeypatch.setattr(runner, "required_candidate_specs", lambda _matrix: [])
    monkeypatch.setattr(runner, "_aggregate_reports", lambda *args, **kwargs: aggregate(*args, **kwargs))
    with pytest.raises(ValueError, match="authoritative.*contract|completeness"):
        runner.orchestrate_selection(
            matrix_path=BENCHMARK / "model-matrix-v1.json",
            corpus_path=CORPUS,
            repo_root=repo,
            output_path=output,
            measured_at="2026-07-17T12:00:00Z",
            cache_root=tmp_path / "cache",
            deadline_seconds=60.0,
        )

    assert runner._run_bounded_model_worker is trusted_worker
    assert runner._run_process_tree is trusted_process_runner
    assert not output.exists()
    assert matrix["selection"]["default_embedding"] is None
    assert matrix["selection"]["default_reranker"] is None


def test_stale_out_of_band_payloads_with_mutated_enumeration_fail_closed(
    tmp_path, monkeypatch
):
    runner = _runner_module()
    matrix = json.loads((BENCHMARK / "model-matrix-v1.json").read_bytes())
    repo = tmp_path / "repo"
    baseline = repo / matrix["selection"]["baseline"]["raw_report_path"]
    baseline.parent.mkdir(parents=True)
    baseline.write_bytes((BENCHMARK / "baseline-2026-07-16-retrieval.json").read_bytes())
    stale = {}
    for lexical in runner.LEXICAL_CONFIGURATIONS:
        report = runner.run_benchmark(
            runner.load_corpus(CORPUS, SCHEMA),
            corpus_path=CORPUS,
            matrix_path=BENCHMARK / "model-matrix-v1.json",
            cache_root=tmp_path / f"stale-{lexical}",
            lexical_config=lexical,
            test_segmenter=_PinnedJieba(),
        )
        stale[lexical] = runner._WorkerPayload(
            report, runner._canonical_report_bytes(report)
        )

    def replaced_worker(argv, **_kwargs):
        assert "--model-id" not in argv
        return stale[argv[argv.index("--lexical-config") + 1]]

    monkeypatch.setattr(runner, "required_candidate_specs", lambda _matrix: [])
    monkeypatch.setattr(runner, "_run_bounded_model_worker", replaced_worker)
    output = repo / "benchmark/results/stale.json"
    with pytest.raises(ValueError, match="authoritative completeness contract"):
        runner.orchestrate_selection(
            matrix_path=BENCHMARK / "model-matrix-v1.json",
            corpus_path=CORPUS,
            repo_root=repo,
            output_path=output,
            measured_at="2026-07-17T12:00:00Z",
            cache_root=tmp_path / "cache",
            deadline_seconds=30.0,
        )
    assert not output.exists()


@pytest.mark.parametrize("attack", ["wrong-candidate", "replay"])
def test_replaced_worker_wrong_binding_or_replay_cannot_publish(
    tmp_path, monkeypatch, attack
):
    runner = _runner_module()
    selection = runner.load_model_selection(
        BENCHMARK / "model-matrix-v1.json",
        CORPUS,
        model_id="BAAI/bge-small-en-v1.5",
        variant_id="float32-384d",
    )
    specs = runner.required_candidate_specs(selection.matrix)
    first_payload = None

    def replaced_worker(argv, **_kwargs):
        nonlocal first_payload
        lexical = argv[argv.index("--lexical-config") + 1]
        if "--model-id" not in argv:
            report = runner.run_benchmark(
                runner.load_corpus(CORPUS, SCHEMA),
                corpus_path=CORPUS,
                matrix_path=BENCHMARK / "model-matrix-v1.json",
                cache_root=tmp_path / f"lexical-{lexical}",
                lexical_config=lexical,
                test_segmenter=_PinnedJieba(),
            )
            return runner._WorkerPayload(report, runner._canonical_report_bytes(report))
        requested_id = argv[argv.index("--model-id") + 1]
        requested_variant = argv[argv.index("--variant-id") + 1]
        requested_reranker = (
            argv[argv.index("--reranker-id") + 1] if "--reranker-id" in argv else None
        )
        requested_index = next(
            index
            for index, spec in enumerate(specs)
            if spec["embedding"]["id"] == requested_id
            and spec["embedding"]["variant_id"] == requested_variant
            and (spec["reranker"]["id"] if spec["reranker"] else None)
            == requested_reranker
        )
        if attack == "replay" and requested_index == 1:
            return first_payload
        returned_spec = specs[1] if attack == "wrong-candidate" else specs[requested_index]
        report = json.loads(
            _candidate_report(runner, selection, returned_spec, offset=requested_index)
        )
        report["quality_claim"] = False
        report["methodology"]["lexical_configuration"] = runner.LEXICAL_CONFIGURATIONS[
            lexical
        ]
        payload = runner._WorkerPayload(report, runner._canonical_report_bytes(report))
        if requested_index == 0:
            first_payload = payload
        return payload

    monkeypatch.setattr(runner, "_run_bounded_model_worker", replaced_worker)
    monkeypatch.setattr(runner, "_verify_locked_environment", lambda *_args, **_kwargs: {})
    with pytest.raises(ValueError, match="requested matrix candidate"):
        runner.orchestrate_selection(
            matrix_path=BENCHMARK / "model-matrix-v1.json",
            corpus_path=CORPUS,
            repo_root=tmp_path / "repo",
            output_path=tmp_path / "repo/benchmark/results/adversarial.json",
            measured_at="2026-07-17T12:00:00Z",
            cache_root=tmp_path / "cache",
            deadline_seconds=30.0,
        )


def test_retained_report_conflict_rolls_back_new_copies_and_selection(tmp_path):
    runner = _runner_module()
    selection = runner.load_model_selection(
        BENCHMARK / "model-matrix-v1.json",
        CORPUS,
        model_id="BAAI/bge-small-en-v1.5",
        variant_id="float32-384d",
    )
    raw_paths = []
    for offset, spec in enumerate(runner.required_candidate_specs(selection.matrix)):
        path = tmp_path / "raw" / f"candidate-{offset}.json"
        path.parent.mkdir(exist_ok=True)
        path.write_bytes(_candidate_report(runner, selection, spec, offset=offset))
        raw_paths.append(path)
    repo = tmp_path / "repo"
    baseline = repo / selection.matrix["selection"]["baseline"]["raw_report_path"]
    baseline.parent.mkdir(parents=True)
    baseline.write_bytes((BENCHMARK / "baseline-2026-07-16-retrieval.json").read_bytes())
    reports = repo / "benchmark" / "results" / "reports"
    reports.mkdir(parents=True)
    conflicting_hash = hashlib.sha256(raw_paths[-1].read_bytes()).hexdigest()
    conflict = reports / f"{conflicting_hash}.json"
    conflict.write_bytes(b"conflict")
    output = repo / "benchmark" / "results" / "selection.json"

    with pytest.raises((FileExistsError, ValueError), match="retain|exist"):
        runner.aggregate_selection(
            raw_paths,
            matrix_path=BENCHMARK / "model-matrix-v1.json",
            corpus_path=CORPUS,
            repo_root=repo,
            output_path=output,
            measured_at="2026-07-17T12:00:00Z",
        )

    assert not output.exists()
    assert list(reports.iterdir()) == [conflict]
    assert conflict.read_bytes() == b"conflict"


def test_selection_publication_failure_removes_all_new_retained_reports(tmp_path, monkeypatch):
    runner = _runner_module()
    selection = runner.load_model_selection(
        BENCHMARK / "model-matrix-v1.json",
        CORPUS,
        model_id="BAAI/bge-small-en-v1.5",
        variant_id="float32-384d",
    )
    raw_paths = []
    for offset, spec in enumerate(runner.required_candidate_specs(selection.matrix)):
        path = tmp_path / "raw" / f"candidate-{offset}.json"
        path.parent.mkdir(exist_ok=True)
        path.write_bytes(_candidate_report(runner, selection, spec, offset=offset))
        raw_paths.append(path)
    repo = tmp_path / "repo"
    baseline = repo / selection.matrix["selection"]["baseline"]["raw_report_path"]
    baseline.parent.mkdir(parents=True)
    baseline.write_bytes((BENCHMARK / "baseline-2026-07-16-retrieval.json").read_bytes())
    output = repo / "benchmark" / "results" / "selection.json"
    monkeypatch.setattr(
        runner,
        "_write_new_output",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("publication failed")),
    )

    with pytest.raises(OSError, match="publication failed"):
        runner.aggregate_selection(
            raw_paths,
            matrix_path=BENCHMARK / "model-matrix-v1.json",
            corpus_path=CORPUS,
            repo_root=repo,
            output_path=output,
            measured_at="2026-07-17T12:00:00Z",
        )

    reports = repo / "benchmark" / "results" / "reports"
    assert not output.exists()
    assert not reports.exists() or not list(reports.iterdir())
    with pytest.raises(ValueError, match="benchmark/results"):
        runner.aggregate_selection(
            raw_paths,
            matrix_path=BENCHMARK / "model-matrix-v1.json",
            corpus_path=CORPUS,
            repo_root=repo,
            output_path=repo / "private" / "selection.json",
            measured_at="2026-07-17T12:00:00Z",
        )


def test_selection_recomputes_language_baseline_resource_material_and_shipping_gates():
    runner = _runner_module()
    selection = runner.load_model_selection(
        BENCHMARK / "model-matrix-v1.json",
        CORPUS,
        model_id="BAAI/bge-small-en-v1.5",
        variant_id="float32-384d",
    )
    spec = runner.required_candidate_specs(selection.matrix)[0]
    base = json.loads(_candidate_report(runner, selection, spec, offset=0))
    embedding, reranker = runner._candidate_policy(selection.matrix, spec)

    language = json.loads(json.dumps(base))
    language["slices"]["RU"]["mrr_at_10"] = 0.0
    assert runner._selection_report_gates(
        selection.matrix, language, embedding, reranker
    )[0]["per_language"]["RU"] is False

    parent = json.loads(json.dumps(base))
    parent["overall"]["parent_recall_at_10"] = 0.99
    assert runner._selection_report_gates(
        selection.matrix, parent, embedding, reranker
    )[0]["no_parent_recall_at_10_regression"] is False

    latency = json.loads(json.dumps(base))
    latency["measurements"]["warm_latency_p95_ms"] = 1001
    assert runner._selection_report_gates(
        selection.matrix, latency, embedding, reranker
    )[0]["required"]["latency"] is False

    ram = json.loads(json.dumps(base))
    ram["measurements"]["peak_rss_bytes"] = 4 * 1024**3 + 1
    assert runner._selection_report_gates(
        selection.matrix, ram, embedding, reranker
    )[0]["required"]["ram"] is False

    material = json.loads(json.dumps(base))
    material["overall"].update(
        parent_recall_at_10=1.0,
        all_required_evidence_recall_at_20=0.85,
        ndcg_at_10=0.80,
        mrr_at_10=0.85,
    )
    assert runner._selection_report_gates(
        selection.matrix, material, embedding, reranker
    )[0]["material_improvement"] is False

    ineligible = json.loads(json.dumps(embedding))
    ineligible["shipping_eligible"] = False
    assert runner._selection_report_gates(
        selection.matrix, base, ineligible, reranker
    )[0]["shipping_eligible"] is False
