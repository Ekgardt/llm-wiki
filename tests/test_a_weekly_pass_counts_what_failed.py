"""A weekly step that fails is counted, and a weekly that did not run says why.

Reflection and tiers logged their exceptions and the pass reported success; a
weekly skipped for a held fence wrote nothing. See
docs/research/2026-09-25-a-weekly-pass-counts-what-failed.md.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from types import ModuleType

import doctor
import scheduled_weekly


class _Log(list):
    def __call__(self, line: str) -> None:
        self.append(line)

    def step(self, line: str) -> None:
        self.append(line)


def test_a_failing_tier_build_is_a_counted_failure(monkeypatch) -> None:
    broken = ModuleType("build_tiers")

    def build_all_tiers(**_options):
        raise RuntimeError("tiers could not be written")

    broken.build_all_tiers = build_all_tiers
    monkeypatch.setitem(sys.modules, "build_tiers", broken)

    assert scheduled_weekly._build_tiers(_Log()) == 1


def test_a_stale_weekly_names_its_skip() -> None:
    now = datetime(2026, 9, 25, tzinfo=timezone.utc)
    state = {
        "last_weekly_at": (now - timedelta(days=10)).isoformat(),
        "last_weekly_skip": {"skipped_at": (now - timedelta(days=3)).isoformat(), "reason": "fence held"},
    }

    status, message = doctor._weekly_verdict(state, now)

    assert (status, "skipped" in message, "fence held" in message) == ("degraded", True, True)
