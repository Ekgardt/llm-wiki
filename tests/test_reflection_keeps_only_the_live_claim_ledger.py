from copy import deepcopy
from pathlib import Path

import claims
import compile_memory as compiler
import pytest
import reflection

from tests.test_claim_v2_versioned_consumers import _page, _record


def _body(record, version):
    return _page([record], version).decode().split("---\n", 2)[2]


@pytest.mark.parametrize("version", ["v1", "v2"])
def test_real_reflection_preserves_omitted_original_ledger(version):
    record, _ = _record("Exact evidence.", "claim/"+version)
    body = _body(record, "claim-ledger/"+version)
    result = reflection._reflected_page(Path("page.md"), "", body, "# Version\nNew narrative.\n")
    parsed = claims.parse_claim_ledger(result.encode())
    assert parsed is not None
    assert parsed["claims"] == [record]
    assert body in result


def test_copied_ledger_and_nested_history_are_not_duplicate_live_claims():
    record, _ = _record("Exact evidence.")
    body = _body(record, "claim-ledger/v2")
    first = reflection._reflected_page(Path("page.md"), "", body, body)
    second = reflection._reflected_page(Path("page.md"), "", first, body)
    assert claims.parse_claim_ledger(second.encode())["claims"] == [record]
    assert first in second


def test_history_only_ledger_is_not_promoted():
    record, _ = _record("Historical evidence.")
    body = _body(record, "claim-ledger/v2")
    history = "# Current\n\n## History (pre-reflection 2026-10-04)\n<details>\n<summary>Original page before reflection</summary>\n\n"+body+"\n</details>\n"
    assert claims.parse_claim_ledger(history.encode()) is None


@pytest.mark.parametrize("rewrite", ["altered", "invented"])
def test_model_claim_invention_or_change_is_refused(rewrite):
    record, _ = _record("Exact evidence.")
    original = _body(record, "claim-ledger/v2")
    changed = deepcopy(record)
    changed["id"] = "invented-id"
    rewritten = _body(changed, "claim-ledger/v2")
    body = {"altered":original,"invented":"# Version\nOriginal narrative.\n"}[rewrite]
    with pytest.raises(ValueError):
        reflection._reflected_page(Path("page.md"), "", body, rewritten)


def test_code_fence_claim_sample_never_becomes_active_authority():
    record, _ = _record("Exact evidence.")
    body = _body(record, "claim-ledger/v2")
    sample = "````markdown\n"+body+"\n````\n"
    assert claims.parse_claim_ledger(sample.encode()) is None
    assert claims.parse_claim_ledger((sample+body).encode())["claims"] == [record]


@pytest.mark.parametrize("tail", ["", "\n</details>\n</details>\n"])
def test_malformed_owned_history_is_refused(tail):
    record, _ = _record("Exact evidence.")
    body = _body(record, "claim-ledger/v2")
    history = "\n## History (pre-reflection 2026-10-04)\n<details>\n<summary>Original page before reflection</summary>\nOld."
    with pytest.raises(ValueError):
        claims.parse_claim_ledger((body+history+tail).encode())


def test_compile_updates_only_active_json_and_preserves_history_bytes():
    record, _ = _record("Exact evidence.")
    body = _body(record, "claim-ledger/v2")
    reflected = reflection._reflected_page(Path("page.md"), "", body, body).encode()
    history = reflected[reflected.index(b"\n## History"):]
    added = deepcopy(record)
    added["id"] = "new-id"
    result = compiler._with_claim_ledger(reflected, [added])
    assert result.endswith(history)
    assert len(claims.parse_claim_ledger(result)["claims"]) == 2


def test_supersession_changes_only_active_authority():
    from contradiction_pipeline import LifecycleTarget, supersede_claims_in_page
    from markdown_transaction import sha256_bytes

    record, _ = _record("Exact evidence.")
    body = _body(record, "claim-ledger/v2")
    reflected = reflection._reflected_page(Path("page.md"), "", body, body).encode()
    history = reflected[reflected.index(b"\n## History"):]
    target = LifecycleTarget(Path("page.md"), record["id"], record["fingerprint"],
                             sha256_bytes(claims.claim_json_bytes(record)), record["evidence"]["sha256"])
    changed, ledger = supersede_claims_in_page(reflected, (target,), Path("page.md"))
    assert changed.endswith(history)
    assert ledger["claims"][0]["lifecycle"] == "superseded"
    assert claims.parse_claim_ledger(changed)["claims"][0]["lifecycle"] == "superseded"


def test_fenced_history_sample_does_not_hide_live_updates():
    body = "# Current\n\n````markdown\n## History (pre-reflection 2026-10-04)\n<details>\n````\n\n## Update (2026-10-04)\nLive.\n"
    assert reflection._live_and_earlier(body) == (body, "")


@pytest.mark.parametrize("opening", ["```markdown\n<details>\n```", "<details>\n<summary>Unowned summary</summary>"])
def test_incomplete_owned_container_header_is_refused(opening):
    body = "# Current\n\n## History (pre-reflection 2026-10-04)\n" + opening
    with pytest.raises(ValueError):
        claims.parse_claim_ledger(body.encode())


def test_fenced_closing_tag_inside_history_is_opaque():
    body = "# Original\n\n```html\n</details>\n```\n"
    result = reflection._reflected_page(Path("page.md"), "", body, "# Current\nNarrative.\n")
    assert body in result
    assert claims.parse_claim_ledger(result.encode()) is None


def test_actual_reflection_refuses_redaction_of_preserved_history(tmp_path, monkeypatch):
    original = "---\ntype: pattern\n---\n# Current\n\nOriginal immutable.\n"
    page = tmp_path / "page.md"
    page.write_text(original)
    written = []

    def rewrite(*_args):
        return "# Current\nNew narrative.\n", ""

    def redact(content):
        return content.replace("Original immutable.", "Changed history.")

    def publish(*args, **kwargs):
        written.append((args, kwargs))

    monkeypatch.setattr(reflection, "ROOT", tmp_path)
    monkeypatch.setattr(reflection, "_reflection", rewrite)
    monkeypatch.setattr(reflection, "redact_secrets", redact)
    monkeypatch.setattr(reflection, "mutate_knowledge", publish)
    with pytest.raises(ValueError, match="historical bytes"):
        reflection.reflect_page(page, apply=True)
    assert written == []
    assert page.read_text() == original
