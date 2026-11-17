"""A directory link standing in for a Windows reparse point keeps the runtime-parent refusal unchanged."""
from pathlib import Path
from types import SimpleNamespace

from llm_wiki import runtime_metadata

from tests import test_reliable_memory as runtime_cases


def _contains_real_directory_link(path):
    return any(parent.is_symlink() for parent in Path(path).parents)


def test_inside_reparse_control_preserves_the_windows_refusal(tmp_path, monkeypatch):
    monkeypatch.setattr(runtime_cases, "os", SimpleNamespace(name="nt"))
    monkeypatch.setattr(runtime_metadata, "windows_reparse_point", _contains_real_directory_link)
    runtime_cases.test_runtime_parent_authority_is_fresh_after_symlink_retarget(tmp_path)
