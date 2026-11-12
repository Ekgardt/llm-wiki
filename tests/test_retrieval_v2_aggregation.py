"""The retrieval v2 selection aggregation recomputes every report it publishes."""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pytest
from reliable_memory import canonical_json_bytes

from tests.retrieval_v2_helpers import CORPUS, SCHEMA
from tests.retrieval_v2_reports_helpers import candidate_report

BENCHMARK = Path(__file__).resolve().parent.parent / "benchmark"
if str(BENCHMARK) not in sys.path:
    sys.path.insert(0, str(BENCHMARK))

import retrieval_v2_aggregation  # noqa: E402
import retrieval_v2_corpus  # noqa: E402
import retrieval_v2_environment  # noqa: E402
import retrieval_v2_matrix  # noqa: E402
import retrieval_v2_outputs  # noqa: E402
import retrieval_v2_recompute  # noqa: E402
import retrieval_v2_selection_gates  # noqa: E402


def _write_candidate_reports(selection, specs, directory: Path) -> list[Path]:
    directory.mkdir(parents=True, exist_ok=True)
    paths = []
    for offset, spec in enumerate(specs):
        path = directory / f"candidate-{offset}.json"
        path.write_bytes(candidate_report(selection, spec, offset=offset))
        paths.append(path)
    return paths


def _assert_retained_report(repo: Path, retained: Path, evidence) -> None:
    """Each retained report is content-addressed and stays canonical."""
    path = repo / Path(evidence["retained_path"])
    assert path.parent == retained
    assert path.name == f"{evidence['raw_report_sha256']}.json"
    raw = path.read_bytes()
    assert hashlib.sha256(raw).hexdigest() == evidence["raw_report_sha256"]
    parsed = json.loads(raw)
    assert raw == (
        json.dumps(parsed, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
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
    selection = retrieval_v2_matrix.load_model_selection(
        BENCHMARK / "model-matrix-v1.json",
        CORPUS,
        model_id="BAAI/bge-small-en-v1.5",
        variant_id="float32-384d",
    )
    paths = []
    for offset, spec in enumerate(retrieval_v2_matrix.required_candidate_specs(selection.matrix)):
        report = json.loads(candidate_report(selection, spec, offset=offset))
        if offset == 0:
            fabricate(report)
        path = tmp_path / f"candidate-{offset}.json"
        path.write_bytes(retrieval_v2_outputs.canonical_report_bytes(report))
        paths.append(path)
    repo = tmp_path / "repo"
    baseline = repo / selection.matrix["selection"]["baseline"]["raw_report_path"]
    baseline.parent.mkdir(parents=True)
    baseline.write_bytes((BENCHMARK / "baseline-2026-07-16-retrieval.json").read_bytes())

    with pytest.raises(ValueError, match="trace|metric|gold|eligible"):
        retrieval_v2_aggregation.aggregate_selection(
            paths,
            matrix_path=BENCHMARK / "model-matrix-v1.json",
            corpus_path=CORPUS,
            repo_root=repo,
            output_path=repo / "benchmark/results/selection.json",
            measured_at="2026-07-17T12:00:00Z",
        )


def test_aggregation_rejects_abstention_not_derived_from_normalized_confidence(tmp_path):
    selection = retrieval_v2_matrix.load_model_selection(
        BENCHMARK / "model-matrix-v1.json",
        CORPUS,
        model_id="BAAI/bge-small-en-v1.5",
        variant_id="float32-384d",
    )
    spec = retrieval_v2_matrix.required_candidate_specs(selection.matrix)[0]
    report = json.loads(candidate_report(selection, spec, offset=0))
    trace = report["traces"][0]
    trace["abstained"] = not trace["abstained"]

    with pytest.raises(ValueError, match="confidence|abstention"):
        retrieval_v2_recompute.recompute_report_metrics(retrieval_v2_corpus.load_corpus(CORPUS, SCHEMA), report)


def test_aggregation_requires_complete_reports_and_writes_canonical_selection(tmp_path):
    selection = retrieval_v2_matrix.load_model_selection(
        BENCHMARK / "model-matrix-v1.json",
        CORPUS,
        model_id="BAAI/bge-small-en-v1.5",
        variant_id="float32-384d",
    )
    specs = retrieval_v2_matrix.required_candidate_specs(selection.matrix)
    raw_paths = _write_candidate_reports(selection, specs, tmp_path / "raw")
    repo = tmp_path / "repo"
    baseline = repo / selection.matrix["selection"]["baseline"]["raw_report_path"]
    baseline.parent.mkdir(parents=True)
    baseline.write_bytes((BENCHMARK / "baseline-2026-07-16-retrieval.json").read_bytes())
    output = repo / "benchmark" / "results" / "synthetic-selection.json"

    with pytest.raises(ValueError, match="complete candidate report set"):
        retrieval_v2_aggregation.aggregate_selection(
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
        retrieval_v2_aggregation.aggregate_selection(
            raw_paths,
            matrix_path=BENCHMARK / "model-matrix-v1.json",
            corpus_path=CORPUS,
            repo_root=repo,
            output_path=output,
            measured_at="2026-07-17T12:00:00Z",
        )
    raw_paths[0].write_bytes(canonical)

    artifact = retrieval_v2_aggregation.aggregate_selection(
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
    assert artifact["baseline"]["observed_runtime"] == retrieval_v2_environment.observed_runtime_environment(
        "numpy-exact", lexical_config="L4"
    )
    assert artifact["matrix_sha256"] == selection.matrix_sha256
    assert artifact["matrix_policy_sha256"] == retrieval_v2_environment.matrix_policy_fingerprint(selection.matrix)
    assert artifact["matrix_policy_sha256"] != artifact["matrix_sha256"]
    assert artifact["corpus_sha256"] == selection.corpus_sha256
    assert output.read_bytes() == canonical_json_bytes(artifact) + b"\n"
    assert artifact["selected"] in [item["candidate"] for item in artifact["pareto"]]
    retained = repo / "benchmark" / "results" / "reports"
    assert retained.is_dir()
    assert len(list(retained.glob("*.json"))) == len(specs)
    for evidence in artifact["candidate_reports"]:
        _assert_retained_report(repo, retained, evidence)
    with pytest.raises(ValueError, match="already exists"):
        retrieval_v2_aggregation.aggregate_selection(
            raw_paths,
            matrix_path=BENCHMARK / "model-matrix-v1.json",
            corpus_path=CORPUS,
            repo_root=repo,
            output_path=output,
            measured_at="2026-07-17T12:00:00Z",
        )


def test_complete_no_winner_writes_canonical_nonrelease_evidence(tmp_path, monkeypatch):
    selection = retrieval_v2_matrix.load_model_selection(
        BENCHMARK / "model-matrix-v1.json",
        CORPUS,
        model_id="BAAI/bge-small-en-v1.5",
        variant_id="float32-384d",
    )
    raw_paths = []
    for offset, spec in enumerate(retrieval_v2_matrix.required_candidate_specs(selection.matrix)):
        path = tmp_path / "raw" / f"candidate-{offset}.json"
        path.parent.mkdir(exist_ok=True)
        path.write_bytes(candidate_report(selection, spec, offset=offset))
        raw_paths.append(path)
    repo = tmp_path / "repo"
    baseline = repo / selection.matrix["selection"]["baseline"]["raw_report_path"]
    baseline.parent.mkdir(parents=True)
    baseline.write_bytes((BENCHMARK / "baseline-2026-07-16-retrieval.json").read_bytes())
    output = repo / "benchmark/results/no-winner.json"
    original = retrieval_v2_selection_gates.selection_report_gates

    def reject_all(matrix, report, embedding, reranker):
        gates, objective = original(matrix, report, embedding, reranker)
        gates["overall"] = False
        return gates, objective

    monkeypatch.setattr(retrieval_v2_selection_gates, "selection_report_gates", reject_all)
    artifact = retrieval_v2_aggregation.aggregate_selection(
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
        retrieval_v2_aggregation.aggregate_reports(
            raw_paths,
            matrix_path=BENCHMARK / "model-matrix-v1.json",
            corpus_path=CORPUS,
            repo_root=tmp_path / "forged-repo",
            output_path=tmp_path / "forged-repo/benchmark/results/no-winner.json",
            measured_at="2026-07-17T12:00:00Z",
            _attested_results=[object() for _path in raw_paths],
        )


def test_retained_report_conflict_rolls_back_new_copies_and_selection(tmp_path):
    selection = retrieval_v2_matrix.load_model_selection(
        BENCHMARK / "model-matrix-v1.json",
        CORPUS,
        model_id="BAAI/bge-small-en-v1.5",
        variant_id="float32-384d",
    )
    raw_paths = []
    for offset, spec in enumerate(retrieval_v2_matrix.required_candidate_specs(selection.matrix)):
        path = tmp_path / "raw" / f"candidate-{offset}.json"
        path.parent.mkdir(exist_ok=True)
        path.write_bytes(candidate_report(selection, spec, offset=offset))
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
        retrieval_v2_aggregation.aggregate_selection(
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
    selection = retrieval_v2_matrix.load_model_selection(
        BENCHMARK / "model-matrix-v1.json",
        CORPUS,
        model_id="BAAI/bge-small-en-v1.5",
        variant_id="float32-384d",
    )
    raw_paths = []
    for offset, spec in enumerate(retrieval_v2_matrix.required_candidate_specs(selection.matrix)):
        path = tmp_path / "raw" / f"candidate-{offset}.json"
        path.parent.mkdir(exist_ok=True)
        path.write_bytes(candidate_report(selection, spec, offset=offset))
        raw_paths.append(path)
    repo = tmp_path / "repo"
    baseline = repo / selection.matrix["selection"]["baseline"]["raw_report_path"]
    baseline.parent.mkdir(parents=True)
    baseline.write_bytes((BENCHMARK / "baseline-2026-07-16-retrieval.json").read_bytes())
    output = repo / "benchmark" / "results" / "selection.json"
    monkeypatch.setattr(
        retrieval_v2_outputs,
        "write_new_output",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("publication failed")),
    )

    with pytest.raises(OSError, match="publication failed"):
        retrieval_v2_aggregation.aggregate_selection(
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
        retrieval_v2_aggregation.aggregate_selection(
            raw_paths,
            matrix_path=BENCHMARK / "model-matrix-v1.json",
            corpus_path=CORPUS,
            repo_root=repo,
            output_path=repo / "private" / "selection.json",
            measured_at="2026-07-17T12:00:00Z",
        )


def test_selection_recomputes_language_baseline_resource_material_and_shipping_gates():
    selection = retrieval_v2_matrix.load_model_selection(
        BENCHMARK / "model-matrix-v1.json",
        CORPUS,
        model_id="BAAI/bge-small-en-v1.5",
        variant_id="float32-384d",
    )
    spec = retrieval_v2_matrix.required_candidate_specs(selection.matrix)[0]
    base = json.loads(candidate_report(selection, spec, offset=0))
    embedding, reranker = retrieval_v2_recompute.candidate_policy(selection.matrix, spec)

    language = json.loads(json.dumps(base))
    language["slices"]["RU"]["mrr_at_10"] = 0.0
    assert retrieval_v2_selection_gates.selection_report_gates(
        selection.matrix, language, embedding, reranker
    )[0]["per_language"]["RU"] is False

    parent = json.loads(json.dumps(base))
    parent["overall"]["parent_recall_at_10"] = 0.99
    assert retrieval_v2_selection_gates.selection_report_gates(
        selection.matrix, parent, embedding, reranker
    )[0]["no_parent_recall_at_10_regression"] is False

    latency = json.loads(json.dumps(base))
    latency["measurements"]["warm_latency_p95_ms"] = 1001
    assert retrieval_v2_selection_gates.selection_report_gates(
        selection.matrix, latency, embedding, reranker
    )[0]["required"]["latency"] is False

    ram = json.loads(json.dumps(base))
    ram["measurements"]["peak_rss_bytes"] = 4 * 1024**3 + 1
    assert retrieval_v2_selection_gates.selection_report_gates(
        selection.matrix, ram, embedding, reranker
    )[0]["required"]["ram"] is False

    material = json.loads(json.dumps(base))
    material["overall"].update(
        parent_recall_at_10=1.0,
        all_required_evidence_recall_at_20=0.85,
        ndcg_at_10=0.80,
        mrr_at_10=0.85,
    )
    assert retrieval_v2_selection_gates.selection_report_gates(
        selection.matrix, material, embedding, reranker
    )[0]["material_improvement"] is False

    ineligible = json.loads(json.dumps(embedding))
    ineligible["shipping_eligible"] = False
    assert retrieval_v2_selection_gates.selection_report_gates(
        selection.matrix, base, ineligible, reranker
    )[0]["shipping_eligible"] is False
