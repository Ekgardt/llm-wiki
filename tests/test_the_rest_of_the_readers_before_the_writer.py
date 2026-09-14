"""One odd record in a note or a source file no longer fails the whole generation.

Each case below was reproduced end to end — collect, extract, write — failing the
build before the fix. Research:
`docs/research/2026-09-14-the-rest-of-the-readers-before-the-writer.md`.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import evidence_graph  # noqa: E402
from corpus_snapshot import collect_corpus  # noqa: E402

LONG_SIGNATURE = "def option(" + ", ".join(f"p{index}: int = {index}" for index in range(900)) + "):\n    pass\n"

CODE = {
    "long-signature": LONG_SIGNATURE,
    "old-mac-endings": "class A:\r    def f(self):\r        return 1\r",
    "table-with-newline": 'class T:\n    __tablename__ = "a\\nb"\n',
}
NOTES = {
    "multiline-project": "---\ntype: concept\nproject: |\n  one\n  two\n---\n# P\n",
    "date-status": "---\ntype: concept\nstatus: 2026-09-01\n---\n# D\n",
    "float-type": "---\ntype: 1.5\n---\n# F\n",
}


def _written(tmp_path: Path, files: dict[str, bytes], extract) -> int:
    vault = tmp_path / "vault"
    for relative, data in files.items():
        path = vault / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    (vault / "knowledge/notes").mkdir(parents=True, exist_ok=True)
    (vault / "src").mkdir(parents=True, exist_ok=True)
    snapshot = collect_corpus(vault, code_roots=("src",), approved_code_roots=("src",))
    sources, result = extract(snapshot.sources)
    evidence_graph.create_generation_database(
        tmp_path / "evidence.sqlite3",
        sources=[_source_row(source) for source in sources],
        source_bytes={source.record.logical_id: source.content for source in sources},
        nodes=result.nodes,
        occurrences=result.occurrences,
        assertions=result.assertions,
        evidence=result.evidence,
        observations=result.observations,
        dependencies=getattr(result, "dependencies", ()),
    )
    return len(sources)


def _source_row(source) -> dict[str, object]:
    record = source.record
    fields = ("relative_path", "sha256", "size", "media_type", "language", "git_oid")
    return {"source_id": record.logical_id, **{name: getattr(record, name) for name in fields}}


def _code(sources):
    from code_extractor import extract_code

    code = tuple(source for source in sources if source.record.relative_path.startswith("src/"))
    return code, extract_code(code, repository_id="repo")


def _knowledge(sources):
    from knowledge_extractor import extract_knowledge

    notes = tuple(source for source in sources if source.record.relative_path.startswith("knowledge/notes/"))
    return notes, extract_knowledge(notes)


@pytest.mark.parametrize("name", sorted(CODE))
def test_a_source_file_the_writer_used_to_refuse_is_written(tmp_path, name):
    assert _written(tmp_path, {f"src/{name.replace('-', '_')}.py": CODE[name].encode()}, _code) == 1


@pytest.mark.parametrize("name", sorted(NOTES))
def test_a_note_with_raw_yaml_values_is_written(tmp_path, name):
    assert _written(tmp_path, {f"knowledge/notes/{name}.md": NOTES[name].encode()}, _knowledge) == 1


@pytest.mark.skipif(os.name == "nt", reason="Windows cannot create a file name with a line break or backslash")
def test_a_file_the_graph_cannot_name_is_left_out_not_fatal(tmp_path):
    files = {"src/good.py": b"x = 1\n", "src/bad\nname.py": b"y = 2\n", "src/back\\slash.py": b"z = 3\n"}

    assert _written(tmp_path, files, _code) == 1


def test_an_unstorable_key_becomes_its_digest():
    key = "k" * 5000

    assert evidence_graph.storable_identity_key(key) == evidence_graph.storable_identity_key(key)
    assert evidence_graph.storable_identity_key(key).startswith("sha256:")


def test_a_page_that_is_not_utf8_is_that_pages_error_in_the_tier_build(tmp_path):
    import build_tiers

    page = tmp_path / "latin.md"
    page.write_bytes(b"# Caf\xe9\n")

    assert build_tiers._build_page_tier(page, verbose=False) == "errors"
