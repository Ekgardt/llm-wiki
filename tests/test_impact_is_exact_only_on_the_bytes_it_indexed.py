"""Impact is `exact` only when the generation indexed the diff's old bytes.

The hunk offsets are into the old side of the diff; a generation built from
other content matched them against other lines and still said `exact`. See
docs/research/2026-09-25-impact-is-exact-only-on-the-bytes-it-indexed.md.
"""

from __future__ import annotations

from impact_analysis import analyze_impact

from tests.test_impact_analysis import _Graph, _repository


class _OlderGeneration(_Graph):
    indexed = {"alpha.py": b"def alpha():\n    # an older revision\n    return 1\n"}


def test_offsets_into_other_bytes_are_approximate(tmp_path) -> None:
    root = _repository(tmp_path)
    (root / "alpha.py").write_text("def alpha():\n    return 2\n", encoding="utf-8")

    exact = analyze_impact(root=root, graph=_Graph())
    older = analyze_impact(root=root, graph=_OlderGeneration())

    assert (exact["changed_symbols"][0]["classification"], older["changed_symbols"][0]["classification"]) == (
        "exact",
        "approximate",
    )
