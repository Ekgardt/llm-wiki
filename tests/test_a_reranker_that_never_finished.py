"""The reranker must finish inside its stage, or it is a 12-second tax.

Measured 2026-09-07 over 600 benchmark questions: retrieval took exactly the
12 s stage bound on 483 of them, because the pinned cross-encoder needs 10 s
for twelve pairs on a quiet four-core machine and 18–21 s beside other work.
The stage was abandoned every time and the fused order stood; the reranker
had taken part in almost nothing it was credited with.

Int8 dynamic quantisation of the Linear layers at load time takes the same
twelve pairs to 4.2 s and keeps Spearman 0.81–0.95 with the fp32 order. Same
weights, same revision, no new files; `LLMWIKI_RERANKER_PRECISION=fp32` is the
way back. See `docs/research/2026-09-07-a-reranker-that-never-finished.md`.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import reranker  # noqa: E402

torch = pytest.importorskip("torch")


class _Tiny(torch.nn.Module):
    def __init__(self) -> None:
        super().__init__()
        self.dense = torch.nn.Linear(4, 1)

    def forward(self, x):
        return self.dense(x)


def _is_quantized(model) -> bool:
    return any("quantized" in type(module).__module__ for module in model.modules())


def _top3(values: list[float]) -> list[int]:
    return sorted(range(len(values)), key=lambda i: -values[i])[:3]


def test_the_default_is_int8_on_the_linear_layers(monkeypatch):
    monkeypatch.delenv(reranker.PRECISION_ENV, raising=False)

    model, precision = reranker._cpu_precision(_Tiny().eval())

    assert precision == reranker.INT8_DYNAMIC
    assert _is_quantized(model)


def test_fp32_by_name_keeps_the_model_as_loaded(monkeypatch):
    monkeypatch.setenv(reranker.PRECISION_ENV, "FP32")
    original = _Tiny().eval()

    model, precision = reranker._cpu_precision(original)

    assert model is original
    assert precision == reranker.FP32


def test_anything_but_fp32_means_int8(monkeypatch):
    for value in ("", "int8", "nonsense"):
        monkeypatch.setenv(reranker.PRECISION_ENV, value)
        assert reranker.requested_precision() == reranker.INT8_DYNAMIC


def test_a_fake_model_that_is_not_a_module_is_left_alone(monkeypatch):
    monkeypatch.delenv(reranker.PRECISION_ENV, raising=False)
    fake = object()

    model, precision = reranker._cpu_precision(fake)

    assert model is fake
    assert precision == reranker.FP32


def test_a_quantized_model_still_scores_the_same_order(monkeypatch):
    monkeypatch.delenv(reranker.PRECISION_ENV, raising=False)
    torch.manual_seed(3)
    original = _Tiny().eval()
    x = torch.randn(16, 4)
    with torch.inference_mode():
        before = original(x).view(-1).tolist()

    quantized, _ = reranker._cpu_precision(original)

    with torch.inference_mode():
        after = quantized(x).view(-1).tolist()
    assert _top3(before) == _top3(after)


def test_quantisation_happens_in_place_so_no_second_copy_of_the_weights_exists(monkeypatch):
    monkeypatch.delenv(reranker.PRECISION_ENV, raising=False)
    original = _Tiny().eval()

    quantized, _ = reranker._cpu_precision(original)

    assert quantized is original
