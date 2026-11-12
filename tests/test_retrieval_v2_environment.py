"""The retrieval v2 runner pins its lexical extra, its segmenter and its locked environment."""
from __future__ import annotations

import importlib
import sys
from importlib import metadata as importlib_metadata
from pathlib import Path

import pytest

from tests.retrieval_v2_helpers import ROOT

BENCHMARK = Path(__file__).resolve().parent.parent / "benchmark"
if str(BENCHMARK) not in sys.path:
    sys.path.insert(0, str(BENCHMARK))

import retrieval_v2_environment  # noqa: E402


def test_lexical_benchmark_extra_and_dictionary_hash_are_exactly_pinned():
    project = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    lock = (ROOT / "uv.lock").read_text(encoding="utf-8")

    assert 'lexical-benchmark = ["jieba==0.42.1"]' in project
    assert 'name = "jieba"' in lock
    assert retrieval_v2_environment.JIEBA_DEFAULT_DICTIONARY_SHA256 == (
        "7197c3211ddd98962b036cdf40324d1ea2bfaa12bd028e68faa70111a88e12a8"
    )


def test_pinned_segmenter_rejects_wrong_distribution_provenance(tmp_path, monkeypatch):
    package = tmp_path / "site-packages" / "jieba"
    package.mkdir(parents=True)
    module_file = package / "__init__.py"
    dictionary = package / "dict.txt"
    module_file.write_text("", encoding="utf-8")
    dictionary.write_text("dictionary", encoding="utf-8")
    module = type("Jieba", (), {"__file__": str(module_file), "cut": lambda *args: []})()

    class Distribution:
        version = "0.42.1"
        files = (Path("jieba/__init__.py"), Path("jieba/dict.txt"))

        def locate_file(self, relative):
            return tmp_path / "site-packages" / relative

    monkeypatch.setattr(importlib, "import_module", lambda name: module)
    monkeypatch.setattr(importlib_metadata, "distribution", lambda name: Distribution())
    monkeypatch.setattr(
        importlib_metadata,
        "packages_distributions",
        lambda: {"jieba": ["not-jieba"]},
    )

    with pytest.raises(ValueError, match="distribution provenance"):
        retrieval_v2_environment.load_pinned_jieba(tmp_path / "cache")


def test_pinned_segmenter_rejects_wrong_default_dictionary_hash(tmp_path, monkeypatch):
    package = tmp_path / "site-packages" / "jieba"
    package.mkdir(parents=True)
    module_file = package / "__init__.py"
    dictionary = package / "dict.txt"
    module_file.write_text("", encoding="utf-8")
    dictionary.write_text("tampered", encoding="utf-8")
    module = type("Jieba", (), {"__file__": str(module_file), "cut": lambda *args: []})()

    class Distribution:
        version = "0.42.1"
        files = (Path("jieba/__init__.py"), Path("jieba/dict.txt"))

        def locate_file(self, relative):
            return tmp_path / "site-packages" / relative

    monkeypatch.setattr(importlib, "import_module", lambda name: module)
    monkeypatch.setattr(importlib_metadata, "distribution", lambda name: Distribution())
    monkeypatch.setattr(
        importlib_metadata,
        "packages_distributions",
        lambda: {"jieba": ["jieba"]},
    )

    with pytest.raises(ValueError, match="dictionary SHA256"):
        retrieval_v2_environment.load_pinned_jieba(tmp_path / "cache")


def test_locked_environment_rejects_installed_version_mismatch(monkeypatch):
    locked = retrieval_v2_environment.locked_package_versions(ROOT / "uv.lock")
    installed = {name: version for name, version in locked.items()}
    installed["numpy"] = "0.0-forged"

    with pytest.raises(ValueError, match="numpy.*locked"):
        retrieval_v2_environment.verify_locked_environment(
            "numpy-exact",
            lexical_config="L0",
            version_getter=lambda name: installed[name],
        )
