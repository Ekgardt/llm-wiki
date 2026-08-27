"""The DLP unlock names the finding, not the file it lives in.

The 2026-08-25 decision made `allow_fingerprints` — the hash of the exact
whole payload — the only unlock, and its first application (2026-08-27)
fingerprinted 33 whole files: any edit to any of them re-blocked export.
Current practice (detect-secrets baselines, gitleaks fingerprints) allowlists
the finding itself, so unrelated edits survive. `allow_finding_fingerprints`
carries the hash of each replaced span; every span must match, so one new
secret still blocks, and a policy without the key keeps its original digest.
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import model_dlp  # noqa: E402
from model_dlp import (  # noqa: E402
    DLPContentBlocked,
    DLPPolicy,
    require_safe_content,
    require_safe_model_output,
)
from reliable_memory import canonical_json_bytes  # noqa: E402

FIXTURE_KEY = "sk-" + "a" * 40


def _finding_policy(*spans: str) -> DLPPolicy:
    return DLPPolicy(
        allow_finding_fingerprints=frozenset(
            model_dlp._fingerprint(span) for span in spans
        )
    )


def test_an_allowlisted_finding_passes() -> None:
    text = f"token = \"{FIXTURE_KEY}\"\n"
    scrubbed = model_dlp._scrubbed(text, DLPPolicy())
    assert scrubbed != text, "the fixture must actually be a finding"
    spans = model_dlp._finding_spans(text, scrubbed)
    require_safe_model_output(text, _finding_policy(*spans))


def test_an_edit_elsewhere_in_the_file_survives() -> None:
    """The whole point: the unlock is stable under unrelated edits."""
    original = f"# comment\ntoken = \"{FIXTURE_KEY}\"\n"
    scrubbed = model_dlp._scrubbed(original, DLPPolicy())
    policy = _finding_policy(*model_dlp._finding_spans(original, scrubbed))

    edited = f"# a completely rewritten comment, twice as long\ntoken = \"{FIXTURE_KEY}\"\nprint('new code')\n"
    require_safe_content(edited.encode(), policy)


def test_a_new_secret_still_blocks() -> None:
    original = f"token = \"{FIXTURE_KEY}\"\n"
    scrubbed = model_dlp._scrubbed(original, DLPPolicy())
    policy = _finding_policy(*model_dlp._finding_spans(original, scrubbed))

    second = original + "other = \"sk-" + "b" * 40 + "\"\n"
    with pytest.raises(DLPContentBlocked):
        require_safe_content(second.encode(), policy)


def test_an_empty_finding_list_never_allows() -> None:
    with pytest.raises(DLPContentBlocked):
        require_safe_model_output(
            f"token = \"{FIXTURE_KEY}\"", DLPPolicy()
        )


def _policy_file(tmp_path: Path, payload: dict) -> Path:
    digest = hashlib.sha256(canonical_json_bytes(payload)).hexdigest()
    document = dict(payload)
    document["sha256"] = digest
    path = tmp_path / "policy.json"
    path.write_text(json.dumps(document))
    return path


def test_a_pre_extension_policy_still_authenticates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The old digest, computed without the new key, must stay valid."""
    path = _policy_file(
        tmp_path,
        {"version": 1, "literals": [], "allow_fingerprints": []},
    )
    monkeypatch.setenv("LLM_WIKI_DLP_POLICY", str(path))
    policy = model_dlp.load_policy()
    assert policy.allow_finding_fingerprints == frozenset()


def test_a_finding_policy_loads_and_unlocks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    text = f"token = \"{FIXTURE_KEY}\"\n"
    scrubbed = model_dlp._scrubbed(text, DLPPolicy())
    spans = model_dlp._finding_spans(text, scrubbed)
    path = _policy_file(
        tmp_path,
        {
            "version": 1,
            "literals": [],
            "allow_fingerprints": [],
            "allow_finding_fingerprints": sorted(
                model_dlp._fingerprint(span) for span in spans
            ),
        },
    )
    monkeypatch.setenv("LLM_WIKI_DLP_POLICY", str(path))
    require_safe_content(text.encode(), model_dlp.load_policy())


def test_a_tampered_finding_list_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _policy_file(
        tmp_path,
        {"version": 1, "literals": [], "allow_fingerprints": []},
    )
    document = json.loads(path.read_text())
    document["allow_finding_fingerprints"] = ["c" * 64]
    path.write_text(json.dumps(document))
    monkeypatch.setenv("LLM_WIKI_DLP_POLICY", str(path))
    with pytest.raises(model_dlp.DLPPolicyError, match="digest"):
        model_dlp.load_policy()
