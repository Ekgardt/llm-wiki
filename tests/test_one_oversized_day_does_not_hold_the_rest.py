"""A day no part budget can take fails alone; the other days still compile.

Packing refused the whole run when one day's part did not fit, so one oversized day
held every other day uncompiled (2026-09-28). See
`docs/research/2026-09-28-a-long-entry-is-cut-inside-itself.md`.
"""
from __future__ import annotations

from argparse import Namespace

from reliable_memory import sha256_bytes

from tests.compile_paths import point_compile_path
from tests.compile_transactions_helpers import vault  # noqa: F401
from tests.one_bad_day_does_not_hold_the_rest_helpers import _plan

# A window whose input budget (16 000 - 4 000 - 1 024 tokens) holds a 1 KB day with
# its prompt but not a 14 KB one, at one token per byte.
SMALL_WINDOW_TOKENS = 16_000


def test_an_oversized_day_fails_alone_and_the_other_compiles(vault, monkeypatch):  # noqa: F811
    import compile_pipeline

    root, _state_root = vault
    oversized = root / "knowledge/daily/2026-07-14.md"
    fitting = root / "knowledge/daily/2026-07-15.md"
    oversized.write_bytes(b"a" * 14_000)
    fitting.write_bytes(b"b" * 1_000)
    state: dict[str, object] = {}
    asked: list[str] = []

    def resolve(inputs, cache, *, coordinator, batch, token_adapters=None):
        asked.extend(item.logical_path for item in inputs.dailies)
        return _plan(inputs)

    point_compile_path(monkeypatch, "COMPILE_CONTEXT_WINDOW_TOKENS", SMALL_WINDOW_TOKENS)
    point_compile_path(monkeypatch, "load_state", lambda: state)
    point_compile_path(monkeypatch, "update_state", lambda mutate: mutate(state))
    point_compile_path(monkeypatch, "mark_finished", lambda *args, **kwargs: None)
    point_compile_path(monkeypatch, "resolve_compile_plan", resolve)

    result = compile_pipeline.run(Namespace(file=None, all=False, dry_run=False, trigger="manual"))

    compiled = {fitting.name: sha256_bytes(fitting.read_bytes())}
    assert (result, asked, state["compiled_daily_hashes"]) == (1, ["knowledge/daily/2026-07-15.md"], compiled)
