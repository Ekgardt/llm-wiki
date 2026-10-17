"""Seal unchanged source identity while unrelated sibling names change."""
import os
from pathlib import Path

import corpus_snapshot as c
import pytest


def sealed_source(tmp_path: Path):
    root = tmp_path / "vault"
    parent = root / "knowledge/notes"
    parent.mkdir(parents=True)
    source = parent / "source.md"
    source.write_text("# Verified source\nUnchanged source bytes.\n")
    return root, parent, source, c._seal_path(root, source, target_directory=False, max_components=len(source.relative_to(root).parts))


@pytest.mark.parametrize("operation", ["create", "publish"])
def test_an_unrelated_sibling_does_not_change_the_sealed_source(tmp_path: Path, operation: str):
    _root, parent, source, seal = sealed_source(tmp_path)
    before = source.read_bytes()
    sibling = parent / "unrelated.tmp"
    sibling.write_bytes(b"other data")
    if operation == "publish":
        os.replace(sibling, parent / "unrelated.md")
    c._verify_seal(seal)
    assert source.read_bytes() == before


@pytest.mark.parametrize("operation", ["replace", pytest.param("permissions", marks=pytest.mark.skipif(os.name != "posix", reason="POSIX permission bits; Windows uses ACLs"))])
def test_real_ancestor_replacement_or_permission_change_is_still_refused(tmp_path: Path, operation: str):
    _root, parent, source, seal = sealed_source(tmp_path)
    before = source.read_bytes()
    if operation == "replace":
        parent.rename(parent.with_name("retained"))
        parent.mkdir()
        source.write_bytes(before)
    if operation == "permissions":
        parent.chmod(0o700)
    with pytest.raises(c.CorpusChanged, match="ancestor.*changed"):
        c._verify_seal(seal)


def test_source_content_changes_are_still_refused(tmp_path: Path):
    _root, _parent, source, seal = sealed_source(tmp_path)
    source.write_text("# Replaced content\n")
    with pytest.raises(c.CorpusChanged, match="ancestor.*changed"):
        c._verify_seal(seal)


@pytest.mark.parametrize("field", ["st_uid", "st_gid"])
def test_directory_ownership_remains_part_of_its_identity(tmp_path: Path, field: str):
    from types import SimpleNamespace

    _root, parent, _source, _seal = sealed_source(tmp_path)
    info = parent.lstat()
    original = c._identity(parent, info)
    values = {name: getattr(info, name) for name in ("st_dev", "st_ino", "st_mode", "st_size", "st_ctime_ns", "st_uid", "st_gid")}
    values[field] += 1
    assert c._identity(parent, SimpleNamespace(**values)) != original
