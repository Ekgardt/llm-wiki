from __future__ import annotations

import json
import os
import stat
from dataclasses import replace
from pathlib import Path

import pytest
from compile_cache import (
    CompileActionDescriptor,
    CompileCache,
    CompileCallDescriptor,
    SourceDescriptor,
)


def _call(**changes) -> CompileCallDescriptor:
    values = {
        "prompt_program_hash": "1" * 64,
        "provider": "codex",
        "model": "gpt-5",
        "capabilities": {"structured_output": False},
        "inference_settings": {"reasoning": "low", "max_tokens": 2000},
        "structured_output": "prompt",
        "fallback_from": (),
    }
    values.update(changes)
    return CompileCallDescriptor(**values)


def _action(**changes) -> CompileActionDescriptor:
    values = {
        "compiler_version": "2.0.0",
        "schema_hash": "2" * 64,
        "normalization_version": "normalize-v2",
        "feature_flags": {"critique": True},
        "draft_calls": (_call(),),
        "critique_calls": (_call(prompt_program_hash="3" * 64),),
        "sources": (
            SourceDescriptor("AGENTS.md", 30, "4" * 64),
            SourceDescriptor("knowledge/daily/2026-07-13.md", 20, "5" * 64),
            SourceDescriptor("knowledge/generated-snapshot.json", 10, "6" * 64),
            SourceDescriptor("knowledge/log.md#tail", 40, "7" * 64),
            SourceDescriptor("knowledge/notes/active.md", 50, "8" * 64),
        ),
    }
    values.update(changes)
    return CompileActionDescriptor(**values)


@pytest.mark.parametrize(
    "changed",
    [
        lambda a: replace(a, compiler_version="2.0.1"),
        lambda a: replace(a, schema_hash="a" * 64),
        lambda a: replace(a, normalization_version="normalize-v3"),
        lambda a: replace(a, feature_flags={"critique": False}),
        lambda a: replace(a, draft_calls=(replace(a.draft_calls[0], prompt_program_hash="b" * 64),)),
        lambda a: replace(a, draft_calls=(replace(a.draft_calls[0], provider="claude"),)),
        lambda a: replace(a, draft_calls=(replace(a.draft_calls[0], model="opus-4"),)),
        lambda a: replace(a, draft_calls=(replace(a.draft_calls[0], capabilities={"structured_output": True}),)),
        lambda a: replace(a, draft_calls=(replace(a.draft_calls[0], inference_settings={"reasoning": "high"}),)),
        lambda a: replace(a, draft_calls=(replace(a.draft_calls[0], structured_output="native"),)),
        lambda a: replace(a, draft_calls=(replace(a.draft_calls[0], fallback_from=("openai:gpt-4o:provider_error",)),)),
        lambda a: replace(a, sources=(SourceDescriptor("knowledge/daily/2026-07-13.md", 21, "5" * 64),)),
    ],
    ids=[
        "compiler_version",
        "schema_hash",
        "normalization_version",
        "feature_flags",
        "prompt_program_hash",
        "provider",
        "model",
        "capabilities",
        "inference_settings",
        "structured_output",
        "fallback_lineage",
        "source_manifest_hash",
    ],
)
def test_every_effective_dimension_changes_action_key(tmp_path, changed):
    cache = CompileCache(tmp_path)
    base = _action()

    assert cache.key(changed(base)) != cache.key(base)


def test_source_manifest_is_sorted_in_canonical_action(tmp_path):
    cache = CompileCache(tmp_path)
    action = _action(sources=tuple(reversed(_action().sources)))

    canonical = action.canonical()

    assert canonical["source_manifest"] == sorted(canonical["source_manifest"])
    assert len(canonical["source_manifest_hash"]) == 64
    assert cache.key(action) is not None


def test_restored_preferred_provider_does_not_hit_fallback_cache(tmp_path):
    cache = CompileCache(tmp_path)
    fallback = _action(
        draft_calls=(
            _call(
                provider="ollama",
                model="qwen3:0.6b",
                fallback_from=("codex:gpt-5:provider_error",),
            ),
        )
    )
    preferred = _action()
    cache.put(fallback, {"operations": []})

    assert cache.get(preferred) is None
    assert cache.get(fallback) == {"operations": []}


def test_unknown_model_disables_persistent_key_and_write(tmp_path):
    cache = CompileCache(tmp_path)
    action = _action(draft_calls=(replace(_call(), model=None),))

    assert cache.key(action) is None
    with pytest.raises(ValueError, match="model identity"):
        cache.put(action, {"operations": []})


def test_cache_filename_is_only_sha_and_payload_has_digest_and_schema(tmp_path):
    cache = CompileCache(tmp_path)
    action = _action()

    path = cache.put(action, {"operations": []})
    record = json.loads(path.read_text(encoding="utf-8"))

    assert path.parent == tmp_path / "cache" / "compile"
    assert path.name == f"{cache.key(action)}.json"
    assert str(tmp_path) not in path.name
    assert record["schema_version"] == 1
    assert len(record["payload_digest"]) == 64


def test_owner_only_directory_and_file_where_permission_bits_are_supported(tmp_path):
    cache = CompileCache(tmp_path)
    path = cache.put(_action(), {"operations": []})

    if os.name == "posix":
        assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
        assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_empty_successful_plan_is_cached_and_validator_runs_on_every_hit(tmp_path):
    cache = CompileCache(tmp_path)
    action = _action()
    calls = []
    cache.put(action, {"operations": []})

    def validator(plan):
        calls.append(plan)
        return True

    assert cache.get(action, validator) == {"operations": []}
    assert cache.get(action, validator) == {"operations": []}
    assert len(calls) == 2


@pytest.mark.parametrize(
    "failure",
    ["provider", "parse", "critique", "schema", "evidence", "path"],
)
def test_failure_classes_are_not_cacheable(tmp_path, failure):
    cache = CompileCache(tmp_path)

    with pytest.raises(ValueError, match="successful"):
        cache.put(_action(), {"operations": []}, failure_class=failure)
    assert list((tmp_path / "cache" / "compile").glob("*.json")) == []


def test_digest_tamper_and_validator_rejection_fail_closed(tmp_path):
    cache = CompileCache(tmp_path)
    action = _action()
    path = cache.put(action, {"operations": []})
    record = json.loads(path.read_text(encoding="utf-8"))
    record["payload"] = {"operations": [{"action": "create"}]}
    path.write_text(json.dumps(record), encoding="utf-8")

    assert cache.get(action) is None

    cache.put(action, {"operations": []})
    assert cache.get(action, lambda plan: False) is None


def test_validator_exception_on_hit_fails_closed(tmp_path):
    cache = CompileCache(tmp_path)
    action = _action()
    cache.put(action, {"operations": []})

    def broken_validator(plan):
        raise RuntimeError("schema implementation failed")

    assert cache.get(action, broken_validator) is None


def test_non_normalized_plan_is_rejected(tmp_path):
    cache = CompileCache(tmp_path)

    with pytest.raises(ValueError, match="normalized compile plan"):
        cache.put(_action(), {"operations": []}, validator=lambda plan: False)


def test_absolute_and_parent_source_paths_are_rejected():
    with pytest.raises(ValueError, match="logical path"):
        _action(sources=(SourceDescriptor(str(Path.cwd() / "daily.md"), 1, "9" * 64),)).canonical()
    with pytest.raises(ValueError, match="logical path"):
        _action(sources=(SourceDescriptor("../daily.md", 1, "9" * 64),)).canonical()


def test_symlinked_cache_directory_fails_closed(tmp_path):
    target = tmp_path / "outside"
    target.mkdir()
    cache_parent = tmp_path / "cache"
    cache_parent.mkdir()
    try:
        (cache_parent / "compile").symlink_to(target, target_is_directory=True)
    except OSError:
        pytest.skip("directory symlinks are unavailable")

    with pytest.raises(PermissionError, match="cache"):
        CompileCache(tmp_path).put(_action(), {"operations": []})
