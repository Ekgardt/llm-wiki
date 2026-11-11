"""Hashes, the runner identity, environment provenance, locked-package verification and the
pinned jieba segmenter."""
from __future__ import annotations

import hashlib
import importlib
import json
import os
import re
import stat
import tomllib
from importlib import metadata as importlib_metadata
from pathlib import Path, PurePosixPath

from packaging.markers import Marker
from retrieval_v2_contract import ROOT
from retrieval_v2_outputs import is_reparse_point

# isort: split
from bounded_io import read_stable_bytes
from reliable_memory import canonical_json_bytes

JIEBA_VERSION = "0.42.1"
# SHA256 of jieba-0.42.1/jieba/dict.txt from the locked PyPI artifact.
JIEBA_DEFAULT_DICTIONARY_SHA256 = (
    "7197c3211ddd98962b036cdf40324d1ea2bfaa12bd028e68faa70111a88e12a8"
)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while block := handle.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def sha256_json(value: object) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


RUNNER_SOURCES = (
    "run_retrieval_v2.py",
    "retrieval_v2_adapters.py",
    "retrieval_v2_aggregation.py",
    "retrieval_v2_cli.py",
    "retrieval_v2_contract.py",
    "retrieval_v2_corpus.py",
    "retrieval_v2_environment.py",
    "retrieval_v2_matrix.py",
    "retrieval_v2_measurements.py",
    "retrieval_v2_metrics.py",
    "retrieval_v2_orchestration.py",
    "retrieval_v2_outputs.py",
    "retrieval_v2_recompute.py",
    "retrieval_v2_run.py",
    "retrieval_v2_scoring.py",
    "retrieval_v2_selection_gates.py",
    "retrieval_v2_sqlite_lexical.py",
    "retrieval_v2_transformers.py",
    "retrieval_v2_workspace.py",
)


def runner_sha256() -> str:
    """One digest over every source file the benchmark runner executes."""
    benchmark = Path(__file__).parent
    return sha256_json({name: sha256_file(benchmark / name) for name in RUNNER_SOURCES})


def _measurement_objects(policy: dict) -> tuple[list[dict], list[dict]]:
    quality_objects = [policy["lexical"]["quality"]]
    resource_objects = [policy["lexical"]["resource_measurements"]]
    for candidate in policy["embeddings"] + policy["rerankers"]:
        for variant in candidate["variants"]:
            quality_objects.append(variant["quality"])
            resource_objects.append(variant["resource_measurements"])
    return quality_objects, resource_objects


def _blank_quality(quality: dict) -> None:
    quality.update(
        claim=None,
        overall=None,
        per_language={language: None for language in ("EN", "RU", "ZH")},
        status="unmeasured",
    )


def _blank_resources(resources: dict) -> None:
    for field in resources:
        resources[field] = "unmeasured" if field == "status" else None


def matrix_policy_fingerprint(matrix: dict) -> str:
    policy = json.loads(json.dumps(matrix))
    policy["selection"].update(
        default_embedding=None,
        default_reranker=None,
        result_evidence=None,
        status="awaiting_raw_benchmark",
    )
    quality_objects, resource_objects = _measurement_objects(policy)
    for quality in quality_objects:
        _blank_quality(quality)
    for resources in resource_objects:
        _blank_resources(resources)
    return sha256_json(policy)


def _installed_version_or_none(name: str) -> str | None:
    try:
        return importlib_metadata.version(name)
    except importlib_metadata.PackageNotFoundError:
        return None


def environment_provenance(vector_backend: str | None) -> dict:
    names = ("sentence-transformers", "transformers", "torch", "numpy", "usearch")
    versions = {name: _installed_version_or_none(name) for name in names}
    if vector_backend not in {"usearch-exact", "usearch-hnsw"}:
        versions["usearch"] = None
    return {
        "packages": versions,
        "uv_lock_sha256": sha256_file(ROOT / "uv.lock"),
        "runner_sha256": runner_sha256(),
    }


def observed_runtime_environment(
    vector_backend: str | None, *, lexical_config: str | None = None
) -> dict:
    observed = environment_provenance(vector_backend)
    if lexical_config in {"L3", "L4"}:
        observed["packages"]["jieba"] = _installed_version_or_none("jieba")
    return observed


def _lock_packages(lock_path: Path) -> list[dict]:
    raw = read_stable_bytes(lock_path, 16 * 1024 * 1024, label="uv lock")
    return tomllib.loads(raw.decode("utf-8")).get("package", [])


def _package_name(package: dict) -> str:
    return package["name"].casefold().replace("_", "-")


def _resolution_applies(package: dict) -> bool:
    markers = package.get("resolution-markers", [])
    return not markers or any(Marker(marker).evaluate() for marker in markers)


def locked_package_versions(lock_path: Path) -> dict[str, str]:
    observed: dict[str, set[str]] = {}
    for package in _lock_packages(lock_path):
        if not _resolution_applies(package):
            continue
        observed.setdefault(_package_name(package), set()).add(package["version"])
    return {name: next(iter(versions)) for name, versions in observed.items() if len(versions) == 1}


def is_recorded_digest(value: object) -> bool:
    """True when a report field is a well-formed recorded SHA-256.

    Used where a digest is provenance rather than a live check: the baseline
    records the `uv.lock` digest it was measured against, and that value must
    still be a real digest even though it is no longer compared to the current
    file.
    """
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def locked_package_version_sets(lock_path: Path) -> dict[str, set[str]]:
    observed: dict[str, set[str]] = {}
    for package in _lock_packages(lock_path):
        observed.setdefault(_package_name(package), set()).add(package["version"])
    return observed


def _required_locked_packages(vector_backend: str, lexical_config: str) -> set[str]:
    required = {"sentence-transformers", "transformers", "torch", "numpy"}
    if vector_backend in {"usearch-exact", "usearch-hnsw"}:
        required.add("usearch")
    if lexical_config in {"L3", "L4"}:
        required.add("jieba")
    return required


def _verified_installed_version(name: str, locked: dict[str, str], version_getter) -> str:
    if name not in locked:
        raise ValueError(f"{name} is absent from uv.lock")
    try:
        installed = version_getter(name)
    except (KeyError, importlib_metadata.PackageNotFoundError) as exc:
        raise ValueError(f"{name} locked dependency is not installed") from exc
    if installed != locked[name]:
        raise ValueError(f"{name} installed version {installed} does not match locked {locked[name]}")
    return installed


def verify_locked_environment(
    vector_backend: str,
    *,
    lexical_config: str,
    version_getter=importlib_metadata.version,
) -> dict:
    locked = locked_package_versions(ROOT / "uv.lock")
    required = _required_locked_packages(vector_backend, lexical_config)
    verified = {name: _verified_installed_version(name, locked, version_getter) for name in sorted(required)}
    return {
        "packages": verified,
        "package_map_sha256": sha256_json(verified),
        "uv_lock_sha256": sha256_file(ROOT / "uv.lock"),
    }


def _pinned_jieba_distribution() -> tuple[object, object]:
    try:
        module = importlib.import_module("jieba")
        distribution = importlib_metadata.distribution("jieba")
    except (ImportError, importlib_metadata.PackageNotFoundError) as exc:
        raise ValueError(f"lexical configuration requires installed jieba {JIEBA_VERSION}") from exc
    if distribution.version != JIEBA_VERSION:
        raise ValueError(f"lexical configuration requires exactly jieba {JIEBA_VERSION}")
    if importlib_metadata.packages_distributions().get("jieba", []) != ["jieba"]:
        raise ValueError("jieba import does not have exact distribution provenance")
    return module, distribution


def _jieba_dictionary_path(module: object, distribution) -> Path:
    files = {str(path).replace("\\", "/") for path in distribution.files or ()}
    if not {"jieba/__init__.py", "jieba/dict.txt"} <= files:
        raise ValueError("jieba distribution provenance lacks required package files")
    module_path = Path(getattr(module, "__file__", "")).resolve()
    expected_module = Path(distribution.locate_file("jieba/__init__.py")).resolve()
    if module_path != expected_module:
        raise ValueError("jieba import path does not match distribution provenance")
    return Path(distribution.locate_file("jieba/dict.txt")).resolve()


def _pinned_jieba_dictionary_hash(dictionary_path: Path) -> str:
    dictionary_hash = sha256_file(dictionary_path)
    if dictionary_hash != JIEBA_DEFAULT_DICTIONARY_SHA256:
        raise ValueError("jieba default dictionary SHA256 does not match pinned artifact")
    return dictionary_hash


def _jieba_tokenizer_type(module: object):
    tokenizer_type = getattr(module, "Tokenizer", None)
    if not callable(tokenizer_type):
        raise ValueError("installed jieba package has no Tokenizer class")
    return tokenizer_type


def _require_not_reparse(path: Path, message: str) -> None:
    if path.exists() and is_reparse_point(path):
        raise ValueError(message)


def _jieba_cache_directory(cache_root: Path) -> Path:
    cache_dir = cache_root / "jieba"
    _require_not_reparse(cache_dir, "jieba cache directory must not be a symlink or reparse point")
    cache_dir.mkdir(mode=0o700, exist_ok=True)
    if is_reparse_point(cache_dir):
        raise ValueError("jieba cache directory must not be a symlink or reparse point")
    resolved_cache_dir = cache_dir.resolve(strict=True)
    if resolved_cache_dir.parent != cache_root or cache_root not in resolved_cache_dir.parents:
        raise ValueError("jieba cache directory escaped lexical cache root")
    return resolved_cache_dir


def _require_owner_only_cache(resolved_cache_dir: Path) -> None:
    cache_stat = resolved_cache_dir.stat(follow_symlinks=False)
    if hasattr(os, "getuid") and cache_stat.st_uid != os.getuid():
        raise PermissionError("jieba cache directory is not owned by the current user")
    if os.name == "posix" and stat.S_IMODE(cache_stat.st_mode) & 0o077:
        raise PermissionError("jieba cache directory must be owner-controlled")


def _jieba_cache_file(resolved_cache_dir: Path) -> tuple[str, Path]:
    cache_name = f"jieba-{JIEBA_VERSION}.cache"
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", cache_name):
        raise ValueError("jieba cache filename is not a bounded basename")
    cache_path = resolved_cache_dir / cache_name
    _require_not_reparse(cache_path, "jieba cache file must not be a symlink or reparse point")
    return cache_name, cache_path


def _initialized_jieba_tokenizer(tokenizer_type, resolved_cache_dir: Path, cache_name: str, cache_path: Path):
    tokenizer = tokenizer_type()
    tokenizer.tmp_dir = str(resolved_cache_dir)
    tokenizer.cache_file = cache_name
    tokenizer.initialize()
    if not cache_path.is_file() or is_reparse_point(cache_path):
        raise ValueError("jieba cache file was not safely created")
    return tokenizer


def load_pinned_jieba(cache_root: Path) -> tuple[object, dict]:
    module, distribution = _pinned_jieba_distribution()
    dictionary_hash = _pinned_jieba_dictionary_hash(_jieba_dictionary_path(module, distribution))
    tokenizer_type = _jieba_tokenizer_type(module)
    resolved_cache_dir = _jieba_cache_directory(cache_root)
    _require_owner_only_cache(resolved_cache_dir)
    cache_name, cache_path = _jieba_cache_file(resolved_cache_dir)
    tokenizer = _initialized_jieba_tokenizer(tokenizer_type, resolved_cache_dir, cache_name, cache_path)
    return tokenizer, {
        "provenance": "pinned-installed",
        "quality_evidence": True,
        "version": JIEBA_VERSION,
        "dictionary_sha256": dictionary_hash,
        "cache_path": PurePosixPath(cache_path.as_posix()).as_posix(),
    }
