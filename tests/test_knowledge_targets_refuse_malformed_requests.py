from __future__ import annotations

import sqlite3
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from llm_wiki import markdown_store, markdown_targets
from markdown_transaction import MarkdownChange, MarkdownCoordinator

_HASH = "a" * 64
_NOTE = "knowledge/notes/page.md"


def _lease(**overrides: object) -> dict[str, object]:
    lease: dict[str, object] = {
        "project": "demo",
        "lease_token": "token",
        "fencing_epoch": 1,
        "expires_at": "2026-11-17T00:00:00Z",
    }
    lease.update(overrides)
    return lease


def _intent_fence(**overrides: object) -> dict[str, object]:
    fence: dict[str, object] = {
        "intent_id": _HASH,
        "mode": "capture",
        "token": "token",
        "fencing_epoch": 1,
        "expires_at": "2026-11-17T00:00:00Z",
    }
    fence.update(overrides)
    return fence


def _claim(**overrides: object) -> dict[str, object]:
    claim: dict[str, object] = {
        "page": _NOTE,
        "claim_id": "claim-1",
        "fingerprint": _HASH,
        "record_hash": _HASH,
        "evidence_hash": _HASH,
    }
    claim.update(overrides)
    return claim


@pytest.fixture
def vault(tmp_path: Path) -> Path:
    root = tmp_path / "vault"
    (root / "knowledge/notes").mkdir(parents=True)
    (root / "knowledge/index.md").write_bytes(b"index\n")
    return root


@pytest.fixture
def coordinator(vault: Path, tmp_path: Path) -> MarkdownCoordinator:
    return MarkdownCoordinator(vault, tmp_path / "state")


def _prepare_with(coordinator: MarkdownCoordinator, preconditions: object) -> None:
    coordinator.prepare(
        [MarkdownChange.create(_NOTE, b"page\n")],
        operation_id="refused-precondition",
        preconditions=cast(dict[str, object], preconditions),
    )


@pytest.mark.parametrize(
    ("preconditions", "error", "message"),
    [
        ({"claim_targets": "page"}, TypeError, "claim_targets precondition must be an array"),
        ({"claim_targets": []}, ValueError, "claim_targets precondition must be bounded"),
        ({"claim_targets": ["page"]}, ValueError, "claim_targets precondition has invalid fields"),
        (
            {"claim_targets": [_claim(claim_id="")]},
            ValueError,
            "claim_targets precondition has invalid claim id",
        ),
        (
            {"claim_targets": [_claim(record_hash="short")]},
            ValueError,
            "claim_targets precondition has invalid hash",
        ),
        (
            {"claim_targets": [_claim(), _claim()]},
            ValueError,
            "claim_targets precondition contains duplicates",
        ),
        ({"project_lease": ["demo"]}, TypeError, "project_lease precondition must be a mapping"),
        (
            {"project_lease": {"project": "demo"}},
            ValueError,
            "project_lease precondition has invalid fields",
        ),
        (
            {"project_lease": _lease(fencing_epoch=0)},
            ValueError,
            "project_lease precondition has invalid values",
        ),
        (
            {"intent_fence": _intent_fence(mode="reader")},
            ValueError,
            "intent_fence precondition has invalid values",
        ),
        (
            {"intent_fence": _intent_fence(fencing_epoch=True)},
            ValueError,
            "intent_fence precondition has invalid values",
        ),
        (
            {
                "capture_binding": {
                    "intent_id": _HASH,
                    "task_id": "",
                    "active_link_digest": _HASH,
                    "seal_digest": _HASH,
                }
            },
            ValueError,
            "capture_binding precondition has invalid values",
        ),
        (
            {_NOTE: "not-a-hash"},
            ValueError,
            "precondition values must be 'absent' or SHA-256 hashes",
        ),
        (["not", "a", "mapping"], TypeError, "preconditions must be a mapping"),
    ],
)
def test_a_malformed_precondition_is_refused_before_anything_is_written(
    coordinator: MarkdownCoordinator,
    vault: Path,
    preconditions: object,
    error: type[Exception],
    message: str,
) -> None:
    with pytest.raises(error, match=message):
        _prepare_with(coordinator, preconditions)

    assert not (vault / _NOTE).exists()


def test_claim_targets_are_kept_in_page_and_claim_order(
    coordinator: MarkdownCoordinator,
) -> None:
    second = _claim(claim_id="claim-2")
    first = _claim(claim_id="claim-1")

    validated = coordinator._validate_preconditions({"claim_targets": [second, first]})

    assert validated == {"claim_targets": [first, second]}


_DEEP_PATH = "knowledge/notes/" + "d/" * 10 + "page.md"
_LONG_PATH = "knowledge/notes/" + ("n" * 100 + "/") * 5 + "page.md"
_WIDE_PATH = "knowledge/notes/" + "w" * 130 + ".md"
_DECOMPOSED_PATH = "knowledge/notes/e" + chr(0x301) + ".md"


@pytest.mark.parametrize(
    ("change", "error", "message"),
    [
        (
            MarkdownChange(cast(Any, "rename"), _NOTE, b"x"),
            ValueError,
            "unsupported change kind: rename",
        ),
        (MarkdownChange("delete", _NOTE, b"x"), ValueError, "delete content must be absent"),
        (
            MarkdownChange("create", _NOTE, cast(bytes, "text")),
            TypeError,
            "create and replace content must be bytes",
        ),
        (
            MarkdownChange.create(_NOTE, b"x", max_before_bytes=-1),
            ValueError,
            "max_before_bytes must be a non-negative integer or None",
        ),
        (
            MarkdownChange("create", _NOTE, b"x", cast(int, True)),
            ValueError,
            "max_before_bytes must be a non-negative integer or None",
        ),
        (MarkdownChange.create(_DEEP_PATH, b"x"), ValueError, "target path depth exceeds limit"),
        (MarkdownChange.create(_LONG_PATH, b"x"), ValueError, "target path exceeds length limit"),
        (
            MarkdownChange.create(_WIDE_PATH, b"x"),
            ValueError,
            "target path component exceeds length limit",
        ),
        (
            MarkdownChange.create(_DECOMPOSED_PATH, b"x"),
            ValueError,
            "path must use NFC Unicode normalization",
        ),
        (
            MarkdownChange.create("knowledge/index.md/child.md", b"x"),
            ValueError,
            "path is outside every allowed Markdown root",
        ),
    ],
)
def test_a_malformed_change_is_refused_before_anything_is_written(
    coordinator: MarkdownCoordinator,
    vault: Path,
    change: MarkdownChange,
    error: type[Exception],
    message: str,
) -> None:
    with pytest.raises(error, match=message):
        coordinator.prepare([change], operation_id="refused-change")

    assert sorted(path.name for path in (vault / "knowledge/notes").iterdir()) == []


def test_a_change_that_is_not_a_markdown_change_is_refused(
    coordinator: MarkdownCoordinator,
) -> None:
    with pytest.raises(TypeError, match="changes must contain MarkdownChange values"):
        coordinator.prepare([cast(MarkdownChange, ("create", _NOTE))], operation_id="not-a-change")


def test_content_past_the_target_bound_is_refused(
    coordinator: MarkdownCoordinator, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(markdown_store, "MAX_KNOWLEDGE_TARGET_BYTES", 4)

    with pytest.raises(ValueError, match="transaction target size exceeds limit"):
        coordinator.prepare([MarkdownChange.create(_NOTE, b"12345")], operation_id="too-large")


def test_a_missing_parent_is_created_only_through_safe_directories(
    coordinator: MarkdownCoordinator, vault: Path
) -> None:
    created = coordinator.ensure_target_parent("knowledge/notes/topic/sub/page.md")

    assert (created, created.parent.is_dir()) == (
        vault / "knowledge/notes/topic/sub/page.md",
        True,
    )


def test_a_parent_that_is_a_file_is_refused(coordinator: MarkdownCoordinator, vault: Path) -> None:
    (vault / "knowledge/notes/topic").write_bytes(b"not a directory")

    with pytest.raises(ValueError, match="target parent is not a directory"):
        coordinator.ensure_target_parent("knowledge/notes/topic/page.md")


def test_a_parent_path_deeper_than_the_bound_is_refused(
    coordinator: MarkdownCoordinator, vault: Path
) -> None:
    with pytest.raises(ValueError, match="target path depth exceeds limit"):
        coordinator.ensure_target_parent(_DEEP_PATH)

    assert not (vault / "knowledge/notes/d").exists()


def test_making_a_directory_that_another_writer_made_first_is_not_an_error(
    tmp_path: Path,
) -> None:
    existing = tmp_path / "made-by-someone-else"
    existing.mkdir()

    markdown_targets._make_directory(existing)

    assert existing.is_dir()


@pytest.mark.parametrize("value", [True, 1 << 64, -(1 << 63) - 1, "7"])
def test_a_filesystem_identity_outside_64_bits_is_refused(value: object) -> None:
    with pytest.raises(ValueError, match="filesystem identity is not a 64-bit integer"):
        markdown_targets._stat_identity(SimpleNamespace(st_dev=value, st_ino=1))


def test_a_negative_device_number_is_read_as_its_unsigned_twin() -> None:
    identity = markdown_targets._stat_identity(SimpleNamespace(st_dev=-1, st_ino=5))

    assert identity == ((1 << 64) - 1, 5)


@pytest.mark.parametrize("value", [1 << 63, False, "1"])
def test_a_persisted_identity_outside_signed_64_bits_is_refused(value: object) -> None:
    with pytest.raises(ValueError, match="persisted filesystem identity"):
        markdown_targets.decode_filesystem_id(value)


def _row(**values: object) -> sqlite3.Row:
    database = sqlite3.connect(":memory:")
    database.row_factory = sqlite3.Row
    columns = ", ".join(f"? AS {name}" for name in values)
    row = database.execute(f"SELECT {columns}", tuple(values.values())).fetchone()
    database.close()
    assert isinstance(row, sqlite3.Row)
    return row


def test_an_operation_without_a_recorded_parent_identity_is_refused() -> None:
    row = _row(path=_NOTE, parent_device=None, parent_inode=7)

    with pytest.raises(RuntimeError, match=f"transaction lacks parent identity for {_NOTE}"):
        markdown_targets._required_parent_identity(row)
