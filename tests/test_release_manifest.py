"""A release has to state the exact OID the bootstrap will accept."""
from __future__ import annotations

import sys
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))


def test_the_manifest_names_a_full_commit_oid():
    """Branch and tag names are rejected by the bootstrap; only an OID works."""
    import release_manifest

    oid = release_manifest.commit_of("HEAD")

    assert len(oid) == 40
    assert all(character in "0123456789abcdef" for character in oid)


def test_every_pinned_file_is_hashed():
    import release_manifest

    hashes = release_manifest.manifest("HEAD")

    assert set(hashes) == set(release_manifest.PINNED_FILES)
    assert all(len(digest) == 64 for digest in hashes.values())


def test_the_release_note_form_carries_the_oid_and_the_command():
    import release_manifest

    oid = release_manifest.commit_of("HEAD")
    note = release_manifest._markdown("v-test", oid, {"install.sh": "0" * 64})

    assert oid in note
    assert f"LLM_WIKI_COMMIT={oid}" in note
