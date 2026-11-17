from __future__ import annotations

from pathlib import Path

import pytest
from install_state import MAX_PREIMAGE_BYTES, InstallControlError, ManagedResource
from llm_wiki.install_profile import profile_resource
from llm_wiki.install_records import PROFILE_END, PROFILE_START


@pytest.fixture
def profile(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("MEMORY_LLM_PROVIDER", "fake")
    return tmp_path / "home" / ".profile"


def _resource(profile: Path) -> ManagedResource:
    root = profile.parent.parent
    return profile_resource(profile, root / "vault", root / "state")


def _block(root_line: bytes, state_line: bytes) -> bytes:
    return b"\n".join((PROFILE_START, root_line, state_line, PROFILE_END))


def test_a_profile_swapped_for_a_link_is_refused(profile: Path) -> None:
    resource = _resource(profile)
    profile.parent.mkdir(parents=True)
    (profile.parent / "elsewhere").write_bytes(b"")
    profile.symlink_to(profile.parent / "elsewhere")
    with pytest.raises(InstallControlError, match="install_profile_symlink"):
        resource.read_owned()


def test_a_profile_that_is_a_directory_is_refused(profile: Path) -> None:
    profile.mkdir(parents=True)
    with pytest.raises(InstallControlError, match="install_profile_invalid"):
        _resource(profile)


def test_a_profile_larger_than_a_preimage_is_refused(profile: Path) -> None:
    profile.parent.mkdir(parents=True)
    profile.write_bytes(b"#" * (MAX_PREIMAGE_BYTES + 1))
    with pytest.raises(InstallControlError, match="install_profile_invalid"):
        _resource(profile)


@pytest.mark.parametrize(
    "content",
    [
        PROFILE_START + b"\n" + PROFILE_START + b"\n" + PROFILE_END + b"\n",
        PROFILE_START + b"\n",
        b"echo " + PROFILE_START + b"\n" + PROFILE_END + b"\n",
        PROFILE_START + b"\n" + PROFILE_END + b" trailing\n",
    ],
    ids=["two-starts", "no-end", "start-mid-line", "end-mid-line"],
)
def test_a_block_whose_markers_are_not_ours_is_refused(profile: Path, content: bytes) -> None:
    profile.parent.mkdir(parents=True)
    profile.write_bytes(content)
    resource = _resource(profile)
    with pytest.raises(InstallControlError, match="install_profile_ownership_invalid"):
        resource.read_owned()


def test_the_rendered_block_is_recognized(profile: Path) -> None:
    resource = _resource(profile)
    assert resource.recognizes(resource.desired)


@pytest.mark.parametrize(
    "block",
    [
        b"\xff\xfe",
        PROFILE_START + b"\n" + PROFILE_END,
        b"# other\n" + b"export LLM_WIKI_ROOT=/v\nexport LLM_WIKI_STATE_ROOT=/s\n" + PROFILE_END,
        _block(b"export LLM_WIKI_ROOT=relative/vault", b"export LLM_WIKI_STATE_ROOT=/s"),
        _block(b"export LLM_WIKI_ROOT='/unterminated", b"export LLM_WIKI_STATE_ROOT=/s"),
        _block(b"export LLM_WIKI_ROOT=/a /b", b"export LLM_WIKI_STATE_ROOT=/s"),
        _block(b"export LLM_WIKI_ROOT=/v", b"export OTHER=/s"),
    ],
    ids=[
        "undecodable",
        "too-few-lines",
        "wrong-start",
        "relative",
        "unbalanced-quote",
        "two-values",
        "wrong-name",
    ],
)
def test_a_block_the_installer_did_not_render_is_not_recognized(
    profile: Path, block: bytes
) -> None:
    assert not _resource(profile).recognizes(block)


def test_a_block_with_quoted_absolute_paths_is_recognized(profile: Path) -> None:
    block = _block(b"export LLM_WIKI_ROOT='/my vault'", b"export LLM_WIKI_STATE_ROOT=/state")
    assert _resource(profile).recognizes(block)


def test_an_existing_block_is_replaced_and_its_neighbours_kept(profile: Path) -> None:
    profile.parent.mkdir(parents=True)
    old = _block(b"export LLM_WIKI_ROOT=/old", b"export LLM_WIKI_STATE_ROOT=/old")
    profile.write_bytes(b"before\n" + old + b"\nafter\n")
    resource = _resource(profile)
    resource.write_owned(resource.desired)
    assert profile.read_bytes() == b"before\n" + resource.desired + b"\nafter\n"
    assert resource.read_owned() == resource.desired


def test_a_block_ending_in_a_crlf_is_removed_with_its_line_end(profile: Path) -> None:
    profile.parent.mkdir(parents=True)
    resource = _resource(profile)
    profile.write_bytes(b"keep\n" + resource.desired + b"\r\nafter\n")
    resource.write_owned(None)
    assert profile.read_bytes() == b"keep\nafter\n"


def test_a_block_at_the_end_without_a_line_end_is_removed(profile: Path) -> None:
    profile.parent.mkdir(parents=True)
    resource = _resource(profile)
    profile.write_bytes(b"keep\n" + resource.desired)
    resource.write_owned(None)
    assert profile.read_bytes() == b"keep\n"


def test_removing_from_a_profile_without_a_block_changes_nothing(profile: Path) -> None:
    profile.parent.mkdir(parents=True)
    profile.write_bytes(b"untouched")
    profile.chmod(0o640)
    before = profile.stat().st_mode & 0o777
    resource = _resource(profile)
    resource.write_owned(None)
    assert profile.read_bytes() == b"untouched"
    assert profile.stat().st_mode & 0o777 == before


def test_a_block_appended_after_an_unterminated_line_is_removed_with_its_separator(
    profile: Path,
) -> None:
    profile.parent.mkdir(parents=True)
    profile.write_bytes(b"no newline")
    resource = _resource(profile)
    resource.write_owned(resource.desired)
    assert profile.read_bytes() == b"no newline\n" + resource.desired + b"\n"
    resource.write_owned(None)
    assert profile.read_bytes() == b"no newline"


def test_a_profile_the_installer_created_is_deleted_when_emptied(profile: Path) -> None:
    resource = _resource(profile)
    resource.write_owned(resource.desired)
    assert profile.read_bytes() == resource.desired + b"\n"
    resource.write_owned(None)
    assert not profile.exists()
