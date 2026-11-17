from types import SimpleNamespace

from llm_wiki import ledger_core as ledger


def test_every_record_of_a_turn_reaches_the_ledger() -> None:
    turn = SimpleNamespace(
        source_path="knowledge/daily/2026-09-01.md", byte_start=0, byte_end=10, span_sha256="a" * 64
    )
    names = ["road", "mountain", "cargo", "folding", "gravel", "track", "tandem", "touring", "fixie"]
    value = {"records": [{"kind": "bike", "thing": f"{name} bike"} for name in names]}

    assert len(ledger.records_of(turn, value)) == 9
