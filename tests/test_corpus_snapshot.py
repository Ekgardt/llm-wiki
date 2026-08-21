from __future__ import annotations

import contextlib
import dataclasses
import hashlib
import json
import os
import stat
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import corpus_snapshot
import pytest
from code_languages import CODE_LANGUAGE_BY_SUFFIX, language_for_path
from corpus_snapshot import CorpusChanged, collect_corpus, validate_live_snapshot

EXPECTED_CODE_LANGUAGE_BY_SUFFIX = {
    ".bash": "bash",
    ".c": "c",
    ".cc": "cpp",
    ".cpp": "cpp",
    ".cs": "c_sharp",
    ".cxx": "cpp",
    ".go": "go",
    ".h": "c",
    ".hh": "cpp",
    ".hpp": "cpp",
    ".hxx": "cpp",
    ".java": "java",
    ".js": "javascript",
    ".jsx": "javascript",
    ".php": "php",
    ".py": "python",
    ".rb": "ruby",
    ".rs": "rust",
    ".sh": "bash",
    ".ts": "typescript",
    ".tsx": "typescript",
}


@pytest.fixture
def vault(tmp_path: Path) -> Path:
    root = tmp_path / "vault"
    (root / "knowledge/notes").mkdir(parents=True)
    (root / "knowledge/projects/demo").mkdir(parents=True)
    (root / "knowledge/daily").mkdir(parents=True)
    return root


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="")


def page(body: str, **metadata: object) -> str:
    fields = "\n".join(f"{key}: {value}" for key, value in metadata.items())
    return f"---\n{fields}\n---\n{body}"


def test_notes_keep_full_paths_and_exclude_historical_and_internals(vault: Path):
    write(vault / "knowledge/notes/concept/same.md", page("# One\nEnglish text.\n", type="concept"))
    write(vault / "knowledge/notes/pattern/same.md", page("# Two\nOther text.\n", type="pattern"))
    write(vault / "knowledge/notes/old.md", page("# Old\nPast.\n", type="concept", status="superseded"))
    write(vault / "knowledge/notes/archive/hidden.md", "# Hidden\n")
    write(vault / "knowledge/notes/README.md", "# Editorial\n")

    snapshot = collect_corpus(vault)

    assert [source.record.relative_path for source in snapshot.sources] == [
        "knowledge/notes/concept/same.md",
        "knowledge/notes/pattern/same.md",
    ]
    assert snapshot.sources[0].record.logical_id != snapshot.sources[1].record.logical_id
    assert snapshot.sources[0].content.startswith(b"---\n")
    assert snapshot.code_capture is None

    historical = collect_corpus(vault, include_historical=True)
    assert "knowledge/notes/old.md" in {
        source.record.relative_path for source in historical.sources
    }


def test_source_record_contract_and_logical_identity_survive_content_edits(vault: Path):
    target = vault / "knowledge/notes/page.md"
    write(target, page("# English\nOriginal content.\n", type="concept"))

    before = collect_corpus(vault).sources[0].record
    write(target, page("# English\nChanged content.\n", type="concept"))
    after = collect_corpus(vault).sources[0].record

    assert [field.name for field in dataclasses.fields(corpus_snapshot.SourceRecord)] == [
        "logical_id",
        "relative_path",
        "sha256",
        "size",
        "media_type",
        "language",
        "git_oid",
    ]
    assert before.logical_id == after.logical_id == "source:knowledge/notes/page.md"
    assert before.sha256 != after.sha256
    assert before.size != after.size
    assert before.media_type == "text/markdown"
    assert before.language == "en"
    assert before.git_oid is None


def test_canonical_source_manifest_is_shared_ordered_and_invalidates_contract(vault: Path):
    write(vault / "knowledge/notes/z.md", page("# Z\nZulu.\n", type="concept"))
    write(vault / "knowledge/notes/a.md", page("# A\nAlpha.\n", type="concept"))
    snapshot = collect_corpus(vault)
    records = [source.record for source in snapshot.sources]

    manifest = corpus_snapshot.canonical_source_manifest(
        reversed(records), snapshot.policy
    )
    digest = corpus_snapshot.canonical_source_manifest_sha256(
        reversed(records), snapshot.policy
    )

    assert digest == snapshot.corpus_sha256
    assert corpus_snapshot.validate_canonical_source_manifest(manifest) == manifest
    assert [item["relative_path"] for item in manifest["sources"]] == sorted(
        item.record.relative_path for item in snapshot.sources
    )

    first = records[0]
    changed_hash = dataclasses.replace(first, sha256="f" * 64)
    changed_path = dataclasses.replace(first, relative_path="knowledge/notes/renamed.md")
    changed_policy = dataclasses.replace(snapshot.policy, include_historical=True)
    variants = (
        corpus_snapshot.canonical_source_manifest_sha256(records[1:], snapshot.policy),
        corpus_snapshot.canonical_source_manifest_sha256(
            [changed_hash, *records[1:]], snapshot.policy
        ),
        corpus_snapshot.canonical_source_manifest_sha256(
            [changed_path, *records[1:]], snapshot.policy
        ),
        corpus_snapshot.canonical_source_manifest_sha256(records, changed_policy),
        corpus_snapshot.canonical_source_manifest_sha256(
            records, snapshot.policy, extractor_version="other-extractor/v1"
        ),
        corpus_snapshot.canonical_source_manifest_sha256(
            records, snapshot.policy, collector_version="other-collector/v1"
        ),
    )
    assert all(value != digest for value in variants)


def test_as_of_uses_validity_and_frontmatter_must_be_mapping(vault: Path):
    write(
        vault / "knowledge/notes/old.md",
        page(
            "# Old\nPast.\n",
            type="decision",
            status="superseded",
            valid_from="2025-01-01T00:00:00Z",
            valid_to="2026-01-01T00:00:00Z",
        ),
    )
    write(vault / "knowledge/notes/bad.md", "---\n- not\n- a mapping\n---\n# Bad\n")

    with pytest.raises(ValueError, match="frontmatter must be a mapping"):
        collect_corpus(vault, include_historical=True)

    (vault / "knowledge/notes/bad.md").unlink()
    past = collect_corpus(vault, as_of=datetime(2025, 6, 1, tzinfo=timezone.utc))
    future = collect_corpus(vault, as_of="2027-01-01T00:00:00Z")
    assert [source.record.relative_path for source in past.sources] == [
        "knowledge/notes/old.md"
    ]
    assert future.sources == ()


def test_projects_daily_and_code_are_explicitly_bounded_by_policy(vault: Path):
    write(vault / "knowledge/projects/demo/state.md", "# State\nNow.\n")
    write(vault / "knowledge/projects/demo/journal.md", "# Journal\nEvent.\n")
    write(vault / "knowledge/projects/demo/private.md", "# Private\nNo.\n")
    write(vault / "knowledge/daily/allowed.md", "# Daily\nEvidence.\n")
    write(vault / "knowledge/daily/other.md", "# Other\nNo.\n")
    write(vault / "scripts/app.py", "print('hello')\n")

    default = collect_corpus(vault)
    assert {source.record.relative_path for source in default.sources} == {
        "knowledge/projects/demo/journal.md",
        "knowledge/projects/demo/state.md",
    }

    selected = collect_corpus(
        vault,
        daily_paths=("knowledge/daily/allowed.md",),
        code_roots=("scripts",),
    )
    assert {source.record.relative_path for source in selected.sources} == {
        "knowledge/projects/demo/journal.md",
        "knowledge/projects/demo/state.md",
        "knowledge/daily/allowed.md",
        "scripts/app.py",
    }
    assert next(
        s for s in selected.sources if s.record.relative_path == "scripts/app.py"
    ).metadata.type == "code"


@pytest.mark.parametrize(
    ("suffix", "language"),
    sorted(EXPECTED_CODE_LANGUAGE_BY_SUFFIX.items()),
)
@pytest.mark.parametrize(
    "body",
    [
        "english_identifier = another_identifier\n",
        "пример = другой_идентификатор\n",
    ],
)
def test_supported_code_suffixes_override_natural_language_inference(
    vault: Path, suffix: str, language: str, body: str
):
    path = vault / f"scripts/example{suffix}"
    write(path, body)

    snapshot = collect_corpus(vault, code_roots=(path.relative_to(vault).as_posix(),))

    assert snapshot.sources[0].record.language == language
    assert snapshot.chunks[0].language == language


def test_code_language_map_exactly_matches_independent_complete_contract():
    assert dict(CODE_LANGUAGE_BY_SUFFIX) == EXPECTED_CODE_LANGUAGE_BY_SUFFIX


def test_code_language_classifier_identity_binds_version_and_canonical_map():
    from code_languages import (
        CLASSIFIER_IDENTITY,
        CLASSIFIER_MAP_SHA256,
        CLASSIFIER_VERSION,
    )

    canonical = json.dumps(
        EXPECTED_CODE_LANGUAGE_BY_SUFFIX,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")
    expected_digest = hashlib.sha256(canonical).hexdigest()

    assert CLASSIFIER_VERSION == "code-language-classifier/v1"
    assert CLASSIFIER_MAP_SHA256 == expected_digest
    assert CLASSIFIER_IDENTITY == f"{CLASSIFIER_VERSION}+sha256:{expected_digest}"


def test_corpus_and_code_graph_share_one_language_map():
    from code_graph import LANGUAGE_MAP

    assert LANGUAGE_MAP is CODE_LANGUAGE_BY_SUFFIX
    assert corpus_snapshot.language_for_path is language_for_path


def test_code_language_map_is_immutable():
    from code_graph import LANGUAGE_MAP

    try:
        with pytest.raises(TypeError):
            LANGUAGE_MAP[".unsupported"] = "python"
    finally:
        if isinstance(LANGUAGE_MAP, dict):
            LANGUAGE_MAP.pop(".unsupported", None)


@pytest.mark.parametrize(
    ("name", "language"),
    [("APP.PY", "python"), ("component.generated.D.TS", "typescript")],
)
def test_collect_corpus_uses_final_suffix_case_insensitively(
    vault: Path, name: str, language: str
):
    path = vault / "scripts" / name
    write(path, "EnglishIdentifier = AnotherIdentifier\n")

    snapshot = collect_corpus(vault, code_roots=(path.relative_to(vault).as_posix(),))

    assert snapshot.sources[0].record.language == language
    assert snapshot.chunks[0].language == language


def test_non_markdown_code_does_not_treat_yaml_looking_content_as_metadata(vault: Path):
    path = vault / "scripts/example.py"
    write(path, "---\nlanguage: custom\n---\nEnglishIdentifier = AnotherIdentifier\n")

    source = collect_corpus(vault, code_roots=("scripts/example.py",)).sources[0]

    assert source.metadata.language is None
    assert source.record.language == "python"


@pytest.mark.parametrize(
    ("body", "language"),
    [
        ("English prose remains inferred.\n", "en"),
        ("Русский текст по-прежнему определяется.\n", "ru"),
    ],
)
def test_unknown_text_suffixes_retain_natural_language_inference(
    vault: Path, body: str, language: str
):
    path = vault / "scripts/example.txt"
    write(path, body)

    snapshot = collect_corpus(vault, code_roots=("scripts/example.txt",))

    assert snapshot.sources[0].record.language == language
    assert snapshot.chunks[0].language == language


def test_internal_language_classifier_prefers_explicit_metadata():
    assert (
        corpus_snapshot._classify_language(
            explicit="custom", path=Path("example.py"), text="English text."
        )
        == "custom"
    )


def test_historical_policies_recover_archived_directory_sources(vault: Path):
    archived = vault / "knowledge/notes/archive/old.md"
    write(
        archived,
        page(
            "# Archived\nHistorical evidence.\n",
            type="concept",
            status="archived",
            valid_from="2024-01-01T00:00:00Z",
            valid_to="2025-01-01T00:00:00Z",
        ),
    )

    assert collect_corpus(vault).sources == ()
    historical = collect_corpus(vault, include_historical=True)
    as_of = collect_corpus(vault, as_of="2024-06-01T00:00:00Z")
    assert [source.record.relative_path for source in historical.sources] == [
        "knowledge/notes/archive/old.md"
    ]
    assert [source.record.relative_path for source in as_of.sources] == [
        "knowledge/notes/archive/old.md"
    ]


@pytest.mark.parametrize(
    ("name", "body", "language"),
    [
        ("en.md", "# Title\nEnglish evidence here.\n", "en"),
        ("ru.md", "# Заголовок\nРусское свидетельство.\n", "ru"),
        ("zh.md", "# 标题\n中文证据。\n", "zh"),
    ],
)
def test_chunks_have_exact_utf8_spans_hashes_and_language(
    vault: Path, name: str, body: str, language: str
):
    path = vault / "knowledge/notes" / name
    raw = page(body, type="concept").encode()
    path.write_bytes(raw)

    snapshot = collect_corpus(vault)
    source = snapshot.sources[0]
    chunk = snapshot.chunks[0]

    assert source.record.sha256 == hashlib.sha256(raw).hexdigest()
    assert chunk.source_hash == source.record.sha256
    assert raw[chunk.byte_start : chunk.byte_end].decode("utf-8") == chunk.text
    assert chunk.span_sha256 == hashlib.sha256(raw[chunk.byte_start : chunk.byte_end]).hexdigest()
    assert chunk.language == language
    assert chunk.line_start == 4
    assert chunk.line_end == 6
    assert source.record.language == language


def test_frontmatter_is_not_searchable_and_heading_ancestry_is_preserved(vault: Path):
    raw = page(
        "Intro text.\n# Parent\nParent text.\n## Child\nChild text.\n### Leaf\nLeaf text.\n",
        type="concept",
        language="ru",
        source_authority="user",
        confidence="high",
    )
    write(vault / "knowledge/notes/tree.md", raw)

    snapshot = collect_corpus(vault)
    chunks = snapshot.chunks

    assert [chunk.heading_ancestry for chunk in chunks] == [
        (),
        ("Parent",),
        ("Parent", "Child"),
        ("Parent", "Child", "Leaf"),
    ]
    assert all("source_authority" not in chunk.text for chunk in chunks)
    assert all(chunk.language == "ru" for chunk in chunks)
    assert snapshot.sources[0].metadata.authority == "user"
    assert snapshot.sources[0].metadata.confidence == "high"


def test_chunk_ids_are_stable_and_use_full_parent_identity(vault: Path):
    for directory in ("a", "b"):
        write(vault / f"knowledge/notes/{directory}/same.md", "# Same\nSame bytes.\n")

    first = collect_corpus(vault)
    second = collect_corpus(vault)

    assert [chunk.id for chunk in first.chunks] == [chunk.id for chunk in second.chunks]
    assert len({chunk.id for chunk in first.chunks}) == 2


def test_public_chunk_identity_and_spans_match_snapshot_extraction(vault: Path):
    from corpus_snapshot import canonical_chunk_id, canonical_retrieval_spans

    raw = page("Intro.\n# Parent\nBody.\n## Child\nDetail.\n", type="concept").encode()
    path = vault / "knowledge/notes/shared-algorithm.md"
    path.write_bytes(raw)
    snapshot = collect_corpus(vault)
    source = snapshot.sources[0]

    spans = canonical_retrieval_spans(source.record.relative_path, raw)

    assert spans == tuple(
        (chunk.byte_start, chunk.byte_end, chunk.heading_ancestry)
        for chunk in snapshot.chunks
    )
    assert [
        canonical_chunk_id(
            source_id=source.record.logical_id,
            source_path=source.record.relative_path,
            byte_start=chunk.byte_start,
            byte_end=chunk.byte_end,
            span_sha256=chunk.span_sha256,
            extractor_version=snapshot.extractor_version,
        )
        for chunk in snapshot.chunks
    ] == [chunk.id for chunk in snapshot.chunks]


def test_atx_headings_ignore_fences_and_preserve_meaningful_hashes(vault: Path):
    write(
        vault / "knowledge/notes/fences.md",
        "# C#\nBefore.\n```markdown\n## Not a heading\n```\n"
        "## Real ##\nBody.\n~~~\n### Also not a heading\n~~~\n",
    )

    snapshot = collect_corpus(vault)

    assert [chunk.heading_ancestry for chunk in snapshot.chunks] == [
        ("C#",),
        ("C#", "Real"),
    ]
    assert "## Not a heading" in snapshot.chunks[0].text
    assert "### Also not a heading" in snapshot.chunks[1].text


def test_heading_free_markdown_and_code_emit_one_unique_chunk_each(vault: Path):
    write(vault / "knowledge/notes/plain.md", "Plain Markdown content.\n")
    write(vault / "scripts/plain.py", "print('plain')\n")

    snapshot = collect_corpus(vault, code_roots=("scripts/plain.py",))
    chunks_by_source = {
        source.record.relative_path: [
            chunk for chunk in snapshot.chunks if chunk.source_id == source.record.logical_id
        ]
        for source in snapshot.sources
    }

    assert {path: len(chunks) for path, chunks in chunks_by_source.items()} == {
        "knowledge/notes/plain.md": 1,
        "scripts/plain.py": 1,
    }
    assert len({chunk.id for chunk in snapshot.chunks}) == 2


def test_empty_atx_heading_has_deterministic_ancestry_and_exact_offsets(vault: Path):
    raw = b"#\nBody.\n## Next\nMore.\n"
    (vault / "knowledge/notes/empty.md").write_bytes(raw)

    snapshot = collect_corpus(vault)

    assert [chunk.heading_ancestry for chunk in snapshot.chunks] == [
        ("",),
        ("", "Next"),
    ]
    assert [raw[chunk.byte_start : chunk.byte_end] for chunk in snapshot.chunks] == [
        b"#\nBody.\n",
        b"## Next\nMore.\n",
    ]


def test_heading_bomb_is_rejected_before_chunk_materialization(vault: Path, monkeypatch):
    monkeypatch.setattr(corpus_snapshot, "MAX_CORPUS_HEADINGS", 2)
    write(vault / "knowledge/notes/bomb.md", "# One\na\n# Two\nb\n# Three\nc\n")

    with pytest.raises(ValueError, match="heading.*ceiling"):
        collect_corpus(vault)


def test_cancellation_is_checked_inside_markdown_line_scan(vault: Path):
    write(vault / "knowledge/notes/long.md", "".join(f"line {index}\n" for index in range(100)))
    checks = 0

    def cancelled() -> bool:
        nonlocal checks
        checks += 1
        return checks >= 10

    with pytest.raises(TimeoutError, match="cancel"):
        collect_corpus(vault, cancelled=cancelled)

    assert checks == 10


@pytest.mark.parametrize("mutation", ["edit", "add", "delete", "replace"])
def test_live_validation_detects_membership_and_hash_changes_with_preserved_mtime(
    vault: Path, mutation: str
):
    target = vault / "knowledge/notes/page.md"
    write(target, "# Page\nBefore.\n")
    snapshot = collect_corpus(vault)
    original = target.stat()

    if mutation == "edit":
        target.write_bytes(b"# Page\nChanged\n")
        os.utime(target, ns=(original.st_atime_ns, original.st_mtime_ns))
    elif mutation == "add":
        write(vault / "knowledge/notes/added.md", "# Added\nNew.\n")
    elif mutation == "delete":
        target.unlink()
    else:
        target.unlink()
        target.write_bytes(b"# Page\nReplaced.\n")

    with pytest.raises(CorpusChanged):
        validate_live_snapshot(snapshot, vault)


@pytest.mark.skipif(os.name == "posix", reason="Windows pathname fallback only")
def test_collection_uses_writer_gate_and_rejects_external_edit_race(vault: Path, monkeypatch):
    target = vault / "knowledge/notes/page.md"
    write(target, "# Page\nBefore.\n")

    class Coordinator:
        entered = False

        @contextlib.contextmanager
        def writer_gate(self, *, wait_seconds: float | None = None):
            self.entered = True
            yield

    coordinator = Coordinator()
    real_read = corpus_snapshot.read_stable_bytes
    reads = 0

    def racing_read(path: Path, limit: int, *, label: str):
        nonlocal reads
        content = real_read(path, limit, label=label)
        reads += 1
        if reads == 1:
            target.write_bytes(b"# Page\nAfter!\n")
        return content

    monkeypatch.setattr(corpus_snapshot, "read_stable_bytes", racing_read)
    with pytest.raises(CorpusChanged):
        collect_corpus(vault, coordinator=coordinator)
    assert coordinator.entered is True


def test_writer_gate_receives_remaining_deadline_and_releases_on_error(
    vault: Path,
):
    (vault / "knowledge/notes/bad.md").write_bytes(b"# Bad\n\xff")

    class Coordinator:
        wait_seconds: float | None = None
        released = False

        @contextlib.contextmanager
        def writer_gate(self, *, wait_seconds: float | None = None):
            self.wait_seconds = wait_seconds
            try:
                yield
            finally:
                self.released = True

    coordinator = Coordinator()
    with pytest.raises(UnicodeDecodeError):
        collect_corpus(vault, coordinator=coordinator, deadline_seconds=5)
    assert coordinator.wait_seconds is not None
    assert 0 <= coordinator.wait_seconds <= 5
    assert coordinator.released is True


def test_rejects_unsafe_paths_and_enforces_file_total_depth_and_deadline(vault: Path, monkeypatch):
    write(vault / "knowledge/notes/one.md", "# One\n1234\n")
    write(vault / "knowledge/notes/deep/two.md", "# Two\n5678\n")

    with pytest.raises(ValueError, match="relative POSIX"):
        collect_corpus(vault, daily_paths=("../outside.md",))
    with pytest.raises(ValueError, match="file limit"):
        collect_corpus(vault, max_files=1)
    with pytest.raises(ValueError, match="total byte limit"):
        collect_corpus(vault, max_total_bytes=5)
    with pytest.raises(ValueError, match="depth limit"):
        collect_corpus(vault, max_depth=0)

    monkeypatch.setattr(corpus_snapshot.time, "monotonic", lambda: 10.0)
    with pytest.raises(TimeoutError, match="deadline"):
        collect_corpus(vault, deadline=9.0)


@pytest.mark.parametrize(
    "unsafe",
    [
        "/outside",
        "C:/outside",
        "C:outside",
        r"\\server\share",
        r"scripts\child",
        "scripts:child",
        ".",
        "..",
        "scripts/../outside",
        "knowledge/notes",
        "src",
    ],
)
def test_unsafe_code_roots_are_rejected_before_traversal(
    vault: Path, monkeypatch: pytest.MonkeyPatch, unsafe: str
):
    def forbidden_scandir(path: object):
        pytest.fail(f"scandir called before code-root rejection: {path}")

    monkeypatch.setattr(corpus_snapshot.os, "scandir", forbidden_scandir)
    with pytest.raises(ValueError, match="code root"):
        collect_corpus(vault, code_roots=(unsafe,))


def test_duplicate_and_overlapping_explicit_paths_are_rejected(vault: Path):
    write(vault / "knowledge/daily/one.md", "# Daily\n")

    with pytest.raises(ValueError, match="duplicate daily"):
        collect_corpus(
            vault,
            daily_paths=("knowledge/daily/one.md", "knowledge/daily/one.md"),
        )
    with pytest.raises(ValueError, match="overlapping code roots"):
        collect_corpus(vault, code_roots=("scripts", "scripts/pkg"))


def test_explicit_path_ancestor_seal_is_bounded_by_depth(vault: Path):
    (vault / "knowledge/projects/demo").rmdir()
    write(vault / "knowledge/daily/deep/nested/one.md", "# Daily\n")

    with pytest.raises(ValueError, match="ancestor depth limit"):
        collect_corpus(
            vault,
            daily_paths=("knowledge/daily/deep/nested/one.md",),
            max_depth=0,
        )


def test_date_validity_is_half_open_at_valid_to(vault: Path):
    write(
        vault / "knowledge/notes/dated.md",
        page(
            "# Dated\nEvidence.\n",
            type="concept",
            status="archived",
            valid_from="2025-01-01",
            valid_to="2025-01-02",
        ),
    )

    before = collect_corpus(vault, as_of="2025-01-01T23:59:59Z")
    boundary = collect_corpus(vault, as_of="2025-01-02")
    assert len(before.sources) == 1
    assert boundary.sources == ()


def test_symlinked_directory_is_rejected_instead_of_silently_skipped(vault: Path):
    outside = vault.parent / "outside"
    outside.mkdir()
    write(outside / "secret.md", "# Secret\n")
    link = vault / "knowledge/notes/linked"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("directory symlink creation is unavailable")

    with pytest.raises(PermissionError, match="unsafe corpus path"):
        collect_corpus(vault)


@pytest.mark.skipif(os.name == "posix", reason="Windows pathname fallback only")
def test_directory_replacement_during_scandir_is_rejected(
    vault: Path, monkeypatch: pytest.MonkeyPatch
):
    target = vault / "knowledge/notes/safe"
    moved = vault / "knowledge/notes/safe-original"
    write(target / "page.md", "# Before\n")
    real_scandir = corpus_snapshot.os.scandir
    swapped = False

    def swapping_scandir(path: object):
        nonlocal swapped
        if Path(path) == target and not swapped:
            target.rename(moved)
            write(target / "page.md", "# After\n")
            swapped = True
        return real_scandir(path)

    monkeypatch.setattr(corpus_snapshot.os, "scandir", swapping_scandir)
    with pytest.raises(CorpusChanged, match="ancestor.*changed|directory.*changed"):
        collect_corpus(vault)
    assert swapped is True


@pytest.mark.skipif(os.name == "posix", reason="Windows pathname fallback only")
def test_source_parent_same_byte_replacement_after_read_is_rejected(
    vault: Path, monkeypatch: pytest.MonkeyPatch
):
    parent = vault / "knowledge/notes/safe"
    moved = vault / "knowledge/notes/safe-original"
    target = parent / "page.md"
    content = b"# Stable\nSame bytes.\n"
    target.parent.mkdir()
    target.write_bytes(content)
    real_read = corpus_snapshot.read_stable_bytes
    swapped = False

    def swapping_read(path: Path, limit: int, *, label: str):
        nonlocal swapped
        result = real_read(path, limit, label=label)
        if Path(path) == target and not swapped:
            parent.rename(moved)
            parent.mkdir()
            (parent / "page.md").write_bytes(content)
            swapped = True
        return result

    monkeypatch.setattr(corpus_snapshot, "read_stable_bytes", swapping_read)
    with pytest.raises(CorpusChanged, match="ancestor.*changed"):
        collect_corpus(vault)
    assert swapped is True


@pytest.mark.skipif(os.name != "posix", reason="POSIX descriptor traversal only")
def test_posix_directory_handle_ignores_transient_swap_and_restore(
    vault: Path, monkeypatch: pytest.MonkeyPatch
):
    safe = vault / "knowledge/notes/safe"
    held = vault / "knowledge/notes/safe-held"
    outside = vault.parent / "outside-tree"
    write(safe / "inside.md", "# Inside\nAuthoritative.\n")
    write(outside / "outside.md", "# Outside\nMust not enter.\n")
    safe_identity = safe.stat()
    real_scandir = corpus_snapshot.os.scandir
    attacks = 0

    def swapping_scandir(directory: object):
        nonlocal attacks
        descriptor_target = (
            isinstance(directory, int)
            and os.fstat(directory).st_dev == safe_identity.st_dev
            and os.fstat(directory).st_ino == safe_identity.st_ino
        )
        pathname_target = not isinstance(directory, int) and Path(directory) == safe
        if descriptor_target or pathname_target:
            safe.rename(held)
            outside.rename(safe)
            try:
                iterator = real_scandir(directory)
            finally:
                safe.rename(outside)
                held.rename(safe)
            attacks += 1
            return iterator
        return real_scandir(directory)

    monkeypatch.setattr(corpus_snapshot.os, "scandir", swapping_scandir)
    snapshot = collect_corpus(vault)

    assert attacks >= 1
    assert [source.record.relative_path for source in snapshot.sources] == [
        "knowledge/notes/safe/inside.md"
    ]
    assert snapshot.sources[0].content == b"# Inside\nAuthoritative.\n"


@pytest.mark.skipif(os.name != "posix", reason="POSIX descriptor reads only")
def test_posix_file_handle_ignores_transient_parent_swap_and_restore(
    vault: Path, monkeypatch: pytest.MonkeyPatch
):
    safe = vault / "knowledge/notes/safe"
    held = vault / "knowledge/notes/safe-held"
    outside = vault.parent / "outside-parent"
    target = safe / "page.md"
    inside = b"# Inside\nAuthoritative bytes.\n"
    write(target, inside.decode())
    write(outside / "page.md", "# Outside\nTransient bytes.\n")
    target_identity = target.stat()
    real_read = corpus_snapshot.os.read
    real_stable_read = corpus_snapshot.read_stable_bytes
    descriptor_attacks = 0

    def swap() -> None:
        safe.rename(held)
        outside.rename(safe)

    def restore() -> None:
        safe.rename(outside)
        held.rename(safe)

    def swapping_descriptor_read(descriptor: int, size: int):
        nonlocal descriptor_attacks
        info = os.fstat(descriptor)
        if info.st_dev == target_identity.st_dev and info.st_ino == target_identity.st_ino:
            swap()
            try:
                result = real_read(descriptor, size)
            finally:
                restore()
            descriptor_attacks += 1
            return result
        return real_read(descriptor, size)

    def swapping_path_read(path: Path, limit: int, *, label: str):
        if Path(path) != target:
            return real_stable_read(path, limit, label=label)
        swap()
        try:
            return real_stable_read(path, limit, label=label)
        finally:
            restore()

    monkeypatch.setattr(corpus_snapshot.os, "read", swapping_descriptor_read)
    monkeypatch.setattr(corpus_snapshot, "read_stable_bytes", swapping_path_read)
    snapshot = collect_corpus(vault)

    assert descriptor_attacks >= 1
    assert snapshot.sources[0].content == inside


def test_scandir_entry_limit_is_enforced_before_full_iterator_consumption(
    vault: Path, monkeypatch: pytest.MonkeyPatch
):
    consumed = 0

    class Entry:
        def __init__(self, index: int):
            self.name = f"ignored-{index}.txt"
            self.path = str(vault / "knowledge/notes" / self.name)

        def stat(self, *, follow_symlinks: bool = True):
            return SimpleNamespace(st_mode=stat.S_IFREG, st_file_attributes=0)

        def is_symlink(self):
            return False

    class Scan:
        def __enter__(self):
            return self

        def __exit__(self, *args: object):
            return False

        def __iter__(self):
            return self

        def __next__(self):
            nonlocal consumed
            if consumed == 100:
                raise StopIteration
            consumed += 1
            return Entry(consumed)

    real_stat = corpus_snapshot.os.stat

    def descriptor_stat(path, *, dir_fd=None, follow_symlinks=True):
        if dir_fd is not None and str(path).startswith("ignored-"):
            return SimpleNamespace(st_mode=stat.S_IFREG, st_file_attributes=0)
        return real_stat(path, dir_fd=dir_fd, follow_symlinks=follow_symlinks)

    monkeypatch.setattr(corpus_snapshot.os, "scandir", lambda path: Scan())
    monkeypatch.setattr(corpus_snapshot.os, "stat", descriptor_stat)
    with pytest.raises(ValueError, match="entry limit"):
        collect_corpus(vault, max_entries=3)
    assert consumed == 4


def test_scandir_deadline_is_enforced_during_iterator_consumption(
    vault: Path, monkeypatch: pytest.MonkeyPatch
):
    consumed = 0
    clock = 0.0

    class Entry:
        def __init__(self, index: int):
            self.name = f"ignored-{index}.txt"
            self.path = str(vault / "knowledge/notes" / self.name)

        def stat(self, *, follow_symlinks: bool = True):
            return SimpleNamespace(st_mode=stat.S_IFREG, st_file_attributes=0)

        def is_symlink(self):
            return False

    class Scan:
        def __enter__(self):
            return self

        def __exit__(self, *args: object):
            return False

        def __iter__(self):
            return self

        def __next__(self):
            nonlocal consumed, clock
            if consumed == 100:
                raise StopIteration
            consumed += 1
            clock += 0.25
            return Entry(consumed)

    real_stat = corpus_snapshot.os.stat

    def descriptor_stat(path, *, dir_fd=None, follow_symlinks=True):
        if dir_fd is not None and str(path).startswith("ignored-"):
            return SimpleNamespace(st_mode=stat.S_IFREG, st_file_attributes=0)
        return real_stat(path, dir_fd=dir_fd, follow_symlinks=follow_symlinks)

    monkeypatch.setattr(corpus_snapshot.os, "scandir", lambda path: Scan())
    monkeypatch.setattr(corpus_snapshot.os, "stat", descriptor_stat)
    monkeypatch.setattr(corpus_snapshot.time, "monotonic", lambda: clock)
    with pytest.raises(TimeoutError, match="deadline"):
        collect_corpus(vault, deadline=2.0)
    assert consumed < 100


def test_strict_utf8_and_symlink_escape_are_rejected(vault: Path):
    (vault / "knowledge/notes/bad.md").write_bytes(b"# Bad\n\xff")
    with pytest.raises(UnicodeDecodeError):
        collect_corpus(vault)

    (vault / "knowledge/notes/bad.md").unlink()
    outside = vault.parent / "outside.md"
    outside.write_text("# Secret\n", encoding="utf-8")
    link = vault / "knowledge/notes/link.md"
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip("symlink creation is unavailable")
    with pytest.raises(PermissionError, match="unsafe corpus path"):
        collect_corpus(vault)

def test_one_binary_under_a_code_root_does_not_fail_the_whole_corpus(vault: Path):
    """Every vault that has run Python has compiled caches under its code roots."""
    write(vault / "scripts/app.py", "print('hello')\n")
    cache = vault / "scripts" / "__pycache__"
    cache.mkdir(parents=True)
    (cache / "app.cpython-312.pyc").write_bytes(b"\x00\x00\r\n\xff\xfe\x80")
    (vault / "scripts" / "logo.bin").write_bytes(b"\x89PNG\r\n\x1a\n\xff\xfe")

    snapshot = collect_corpus(vault, code_roots=("scripts",))

    paths = {source.record.relative_path for source in snapshot.sources}
    assert "scripts/app.py" in paths
    assert not [path for path in paths if path.endswith((".pyc", ".bin"))]
