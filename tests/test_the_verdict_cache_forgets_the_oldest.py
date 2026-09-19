"""When the verdict cache is full it forgets what was verified longest ago.

Research:
`docs/research/2026-09-17-the-verdict-cache-forgets-the-oldest-not-the-first-in-the-alphabet.md`.
"""

from __future__ import annotations

import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))


def _saved(state_root: Path, digests: list[str]) -> None:
    import verified_artifacts

    cache = verified_artifacts.VerifiedArtifacts(state_root)
    for digest in digests:
        cache.remember_verdict("rows", digest)
    cache.save()


def _known(state_root: Path, digest: str) -> bool:
    import verified_artifacts

    return verified_artifacts.VerifiedArtifacts(state_root).verdict("rows", digest) == "ok"


def test_a_full_cache_keeps_the_newest_verdict_even_when_it_sorts_first(tmp_path):
    import verified_artifacts

    old = [f"z{number:04d}" for number in range(verified_artifacts.MAX_ENTRIES)]
    _saved(tmp_path, old)
    _saved(tmp_path, ["a-newer"])
    _saved(tmp_path, ["z-newest"])

    assert (_known(tmp_path, "a-newer"), _known(tmp_path, old[0]), _known(tmp_path, old[1])) == (True, False, False)


def test_a_verdict_verified_again_is_no_longer_the_oldest(tmp_path):
    import verified_artifacts

    old = [f"m{number:04d}" for number in range(verified_artifacts.MAX_ENTRIES)]
    _saved(tmp_path, old)
    _saved(tmp_path, [old[0]])
    _saved(tmp_path, ["z-newest"])

    assert (_known(tmp_path, old[0]), _known(tmp_path, old[1]), _known(tmp_path, "z-newest")) == (True, False, True)
