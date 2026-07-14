from __future__ import annotations

import json
import os
import stat
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import compile_cache
import llm_client
import pytest
from compile_cache import (
    COMPILE_PLAN_SCHEMA_HASH,
    COMPILE_PLAN_SCHEMA_VERSION,
    CompileActionDescriptor,
    CompileCache,
    CompileCallDescriptor,
    SourceDescriptor,
)
from reliable_memory import canonical_json_bytes, sha256_bytes


def _accept_application(plan) -> bool:
    return True


def _plan(*operations) -> dict[str, object]:
    return {
        "schema_version": COMPILE_PLAN_SCHEMA_VERSION,
        "operations": list(operations),
    }


def _call(**changes) -> CompileCallDescriptor:
    values = {
        "prompt_program_hash": "1" * 64,
        "provider": "codex",
        "model": "gpt-5",
        "capabilities": {"structured_output": False},
        "inference_settings": {"max_tokens": 2000, "reasoning": "low"},
        "structured_output": "prompt",
        "fallback_from": (),
    }
    values.update(changes)
    return CompileCallDescriptor(**values)


def _action(**changes) -> CompileActionDescriptor:
    values = {
        "compiler_version": "2.0.0",
        "schema_version": COMPILE_PLAN_SCHEMA_VERSION,
        "schema_hash": COMPILE_PLAN_SCHEMA_HASH,
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
        lambda a: replace(a, schema_version="compile-plan/v3"),
        lambda a: replace(a, schema_hash="a" * 64),
        lambda a: replace(a, normalization_version="normalize-v3"),
        lambda a: replace(a, feature_flags={"critique": False}),
        lambda a: replace(a, draft_calls=(replace(a.draft_calls[0], prompt_program_hash="b" * 64),)),
        lambda a: replace(a, draft_calls=(replace(a.draft_calls[0], provider="claude"),)),
        lambda a: replace(a, draft_calls=(replace(a.draft_calls[0], model="opus-4"),)),
        lambda a: replace(a, draft_calls=(replace(a.draft_calls[0], capabilities={"structured_output": True}),)),
        lambda a: replace(a, draft_calls=(replace(a.draft_calls[0], inference_settings={"reasoning": "high"}),)),
        lambda a: replace(
            a,
            draft_calls=(
                replace(
                    a.draft_calls[0],
                    inference_settings={"max_tokens": 4096, "reasoning": "low"},
                ),
            ),
        ),
        lambda a: replace(a, draft_calls=(replace(a.draft_calls[0], structured_output="native"),)),
        lambda a: replace(a, draft_calls=(replace(a.draft_calls[0], fallback_from=("openai:gpt-4o:provider_error",)),)),
        lambda a: replace(a, sources=(SourceDescriptor("knowledge/daily/2026-07-13.md", 21, "5" * 64),)),
    ],
    ids=[
        "compiler_version",
        "schema_version",
        "schema_hash",
        "normalization_version",
        "feature_flags",
        "prompt_program_hash",
        "provider",
        "model",
        "capabilities",
        "inference_settings",
        "max_tokens",
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


def test_stable_golden_action_key_uses_exact_canonical_descriptor(tmp_path):
    action = _action(critique_calls=())
    manifest = [source.canonical() for source in sorted(action.sources)]
    expected = {
        "compiler_version": "2.0.0",
        "schema_version": COMPILE_PLAN_SCHEMA_VERSION,
        "schema_hash": COMPILE_PLAN_SCHEMA_HASH,
        "normalization_version": "normalize-v2",
        "feature_flags": {"critique": True},
        "draft_calls": [
            {
                "prompt_program_hash": "1" * 64,
                "provider": "codex",
                "model": "gpt-5",
                "capabilities": {"structured_output": False},
                "inference_settings": {"max_tokens": 2000, "reasoning": "low"},
                "structured_output": "prompt",
                "fallback_lineage": [],
            }
        ],
        "critique_calls": [],
        "source_manifest": manifest,
        "source_manifest_hash": sha256_bytes(canonical_json_bytes(manifest)),
    }

    assert action.canonical() == expected
    assert CompileCache(tmp_path).key(action) == sha256_bytes(canonical_json_bytes(expected))
    assert CompileCache(tmp_path).key(action) == (
        "7b90d06d855dd0fd0a41ba180b24a7e451e7f9e40e3d81e0e5b809d5a3154815"
    )


def test_schema_hash_is_canonical_and_independent_of_checkout_line_endings():
    schema_path = Path(__file__).resolve().parent.parent / "scripts" / "schemas" / "compile-plan-v2.json"
    schema = json.loads(schema_path.read_text(encoding="utf-8"))

    assert COMPILE_PLAN_SCHEMA_HASH == sha256_bytes(canonical_json_bytes(schema))


def test_real_openai_and_ollama_descriptors_produce_distinct_keys(tmp_path, monkeypatch):
    monkeypatch.setenv("MEMORY_LLM_MODEL", "resolved-model")
    openai = llm_client.provider_candidates("openai", max_tokens=321)[0]
    ollama = llm_client.provider_candidates("ollama", max_tokens=321)[0]

    def call(provider):
        return _call(
            provider=provider.provider,
            model=provider.model,
            capabilities=provider.capabilities,
            inference_settings=provider.inference_settings,
            structured_output="native",
        )

    cache = CompileCache(tmp_path)
    openai_key = cache.key(_action(draft_calls=(call(openai),), critique_calls=()))
    ollama_key = cache.key(_action(draft_calls=(call(ollama),), critique_calls=()))

    assert openai_key is not None
    assert ollama_key is not None
    assert openai_key != ollama_key


def test_same_provider_different_effective_endpoints_produce_distinct_keys(tmp_path, monkeypatch):
    monkeypatch.setenv("MEMORY_LLM_BASE_URL", "https://first.example/v1")
    first = llm_client.provider_candidates("openai", max_tokens=321)[0]
    monkeypatch.setenv("MEMORY_LLM_BASE_URL", "https://second.example/v1")
    second = llm_client.provider_candidates("openai", max_tokens=321)[0]

    def call(provider):
        return _call(
            provider=provider.provider,
            model=provider.model,
            capabilities=provider.capabilities,
            inference_settings=provider.inference_settings,
            structured_output="native",
        )

    cache = CompileCache(tmp_path)
    first_key = cache.key(_action(draft_calls=(call(first),), critique_calls=()))
    second_key = cache.key(_action(draft_calls=(call(second),), critique_calls=()))

    assert first_key != second_key


def test_rejected_endpoint_credentials_never_create_cache_files(tmp_path, monkeypatch):
    raw = "https://user:password@private.example/v1?api_key=secret#fragment"
    monkeypatch.setenv("MEMORY_LLM_BASE_URL", raw)

    candidate = llm_client.provider_candidates("openai", max_tokens=321)[0]
    assert candidate.resolution_failure == "invalid_configuration"
    assert not (tmp_path / "cache" / "compile").exists()


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
    cache.put(fallback, _plan())

    assert cache.get(preferred, _accept_application) is None
    assert cache.get(fallback, _accept_application) == _plan()


def test_unknown_model_disables_persistent_key_and_write(tmp_path):
    cache = CompileCache(tmp_path)
    action = _action(draft_calls=(replace(_call(), model=None),))

    assert cache.key(action) is None
    with pytest.raises(ValueError, match="model identity"):
        cache.put(action, _plan())


def test_cache_filename_is_only_sha_and_payload_has_digest_and_schema(tmp_path):
    cache = CompileCache(tmp_path)
    action = _action()

    path = cache.put(action, _plan())
    record = json.loads(path.read_text(encoding="utf-8"))

    assert path.parent == tmp_path / "cache" / "compile"
    assert path.name == f"{cache.key(action)}.json"
    assert str(tmp_path) not in path.name
    assert record["schema_version"] == 1
    assert len(record["payload_digest"]) == 64


def test_owner_only_directory_and_file_where_permission_bits_are_supported(tmp_path):
    cache = CompileCache(tmp_path)
    path = cache.put(_action(), _plan())

    if os.name == "posix":
        assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
        assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_broadened_windows_acl_is_not_owner_only(monkeypatch, tmp_path):
    output = (
        f"{tmp_path} DOMAIN\\user:(F)\r\n"
        "             BUILTIN\\Users:(RX)\r\n"
        "Successfully processed 1 files\r\n"
    ).encode("ascii")
    monkeypatch.setattr(compile_cache, "_windows_acl_identity", lambda: "DOMAIN\\user")
    monkeypatch.setattr(
        compile_cache,
        "_run_acl_command",
        lambda command: SimpleNamespace(returncode=0, stdout=output, stderr=b""),
    )

    assert compile_cache._windows_acl_is_owner_only(tmp_path) is False


def test_similar_windows_acl_identity_is_not_treated_as_owner(monkeypatch, tmp_path):
    output = f"{tmp_path} DOMAIN\\user-backup:(F)\r\n".encode("ascii")
    monkeypatch.setattr(compile_cache, "_windows_acl_identity", lambda: "DOMAIN\\user")
    monkeypatch.setattr(
        compile_cache,
        "_run_acl_command",
        lambda command: SimpleNamespace(returncode=0, stdout=output, stderr=b""),
    )

    assert compile_cache._windows_acl_is_owner_only(tmp_path) is False


def test_broadened_acl_cache_hit_fails_closed(tmp_path, monkeypatch):
    cache = CompileCache(tmp_path)
    action = _action()
    cache.put(action, _plan())
    monkeypatch.setattr(compile_cache, "_is_owner_only", lambda path, mode: False)

    assert cache.get(action, _accept_application) is None


def test_remote_state_root_rejects_write_and_hit(tmp_path, monkeypatch):
    cache = CompileCache(tmp_path)
    action = _action()
    monkeypatch.setattr(compile_cache, "_known_network_path", lambda path: True)

    with pytest.raises(PermissionError, match="local state root"):
        cache.put(action, _plan())
    assert cache.get(action, _accept_application) is None


def test_empty_successful_plan_is_cached_and_validator_runs_on_every_hit(tmp_path):
    cache = CompileCache(tmp_path)
    action = _action()
    calls = []
    cache.put(action, _plan())

    def validator(plan):
        calls.append(plan)
        return True

    assert cache.get(action, validator) == _plan()
    assert cache.get(action, validator) == _plan()
    assert len(calls) == 2


@pytest.mark.parametrize(
    "failure",
    ["provider", "parse", "critique", "schema", "evidence", "path"],
)
def test_failure_classes_are_not_cacheable(tmp_path, failure):
    cache = CompileCache(tmp_path)

    with pytest.raises(ValueError, match="successful"):
        cache.put(_action(), _plan(), failure_class=failure)
    assert list((tmp_path / "cache" / "compile").glob("*.json")) == []


def test_digest_tamper_and_validator_rejection_fail_closed(tmp_path):
    cache = CompileCache(tmp_path)
    action = _action()
    path = cache.put(action, _plan())
    record = json.loads(path.read_text(encoding="utf-8"))
    record["payload"] = _plan(
        {"kind": "create", "path": "knowledge/notes/tampered.md", "content": "x"}
    )
    path.write_bytes(canonical_json_bytes(record))

    assert cache.get(action, _accept_application) is None

    cache.put(action, _plan())
    assert cache.get(action, lambda plan: False) is None


def test_validator_exception_on_hit_fails_closed(tmp_path):
    cache = CompileCache(tmp_path)
    action = _action()
    cache.put(action, _plan())

    def broken_validator(plan):
        raise RuntimeError("schema implementation failed")

    assert cache.get(action, broken_validator) is None


def test_get_requires_application_validator(tmp_path):
    cache = CompileCache(tmp_path)
    action = _action()
    cache.put(action, _plan())

    with pytest.raises(TypeError, match="validator"):
        cache.get(action)


@pytest.mark.parametrize(
    "plan",
    [
        {"operations": []},
        _plan({"kind": "create", "path": "knowledge/notes/a.md"}),
        _plan(
            {
                "kind": "create",
                "path": "knowledge/notes/a.md",
                "content": "x",
                "unexpected": True,
            }
        ),
    ],
)
def test_put_rejects_plan_that_violates_committed_schema(tmp_path, plan):
    with pytest.raises(ValueError, match="compile plan"):
        CompileCache(tmp_path).put(_action(), plan)


@pytest.mark.parametrize(
    "path",
    [".", "../outside.md", "/absolute.md", "knowledge\\notes\\a.md"],
)
def test_put_rejects_unsafe_operation_path(tmp_path, path):
    plan = _plan({"kind": "create", "path": path, "content": "x"})

    with pytest.raises(ValueError, match="path"):
        CompileCache(tmp_path).put(_action(), plan)


def test_get_rejects_schema_invalid_payload_even_with_recomputed_digest(tmp_path):
    cache = CompileCache(tmp_path)
    action = _action()
    path = cache.put(action, _plan())
    record = json.loads(path.read_text(encoding="utf-8"))
    record["payload"] = _plan({"kind": "create", "path": "knowledge/notes/a.md"})
    record["payload_digest"] = sha256_bytes(canonical_json_bytes(record["payload"]))
    path.write_bytes(canonical_json_bytes(record))
    calls = []

    assert cache.get(action, lambda plan: calls.append(plan) or True) is None
    assert calls == []


def test_get_rejects_oversize_entry_using_bounded_fd_read(tmp_path, monkeypatch):
    cache = CompileCache(tmp_path)
    action = _action()
    path = cache.put(action, _plan())
    with path.open("r+b") as handle:
        handle.truncate(compile_cache.MAX_CACHE_ENTRY_BYTES + 1)
    opened = []
    real_open = compile_cache.os.open

    def tracked_open(target, flags, *args, **kwargs):
        opened.append(Path(target))
        return real_open(target, flags, *args, **kwargs)

    monkeypatch.setattr(compile_cache.os, "open", tracked_open)

    assert cache.get(action, _accept_application) is None
    assert path in opened


def test_get_rejects_entry_replaced_between_lstat_and_open(tmp_path, monkeypatch):
    cache = CompileCache(tmp_path)
    action = _action()
    target = cache.put(action, _plan())
    other_action = replace(action, compiler_version="2.0.1")
    replacement = cache.put(other_action, _plan())
    real_open = compile_cache.os.open
    replaced = False
    validator_calls = []

    def racing_open(path, flags, *args, **kwargs):
        nonlocal replaced
        if Path(path) == target and not replaced:
            replaced = True
            compile_cache.os.replace(replacement, target)
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(compile_cache.os, "open", racing_open)

    assert cache.get(action, lambda plan: validator_calls.append(plan) or True) is None
    assert replaced is True
    assert validator_calls == []


def test_fd_read_rejects_same_inode_change_between_lstat_and_open(tmp_path, monkeypatch):
    cache = CompileCache(tmp_path)
    path = cache.put(_action(), _plan())
    original = path.read_bytes()
    changed = original.replace(b'"schema_version":1', b'"schema_version":2')
    assert len(changed) == len(original)
    real_open = compile_cache.os.open
    modified = False

    def racing_open(target, flags, *args, **kwargs):
        nonlocal modified
        if Path(target) == path and not modified:
            modified = True
            path.write_bytes(changed)
        return real_open(target, flags, *args, **kwargs)

    monkeypatch.setattr(compile_cache.os, "open", racing_open)

    with pytest.raises(PermissionError, match="changed before open"):
        compile_cache._read_cache_entry(path)


def test_get_rejects_symlinked_entry_without_running_validator(tmp_path):
    cache = CompileCache(tmp_path)
    action = _action()
    target = cache.put(action, _plan())
    actual = target.with_suffix(".actual")
    target.replace(actual)
    try:
        target.symlink_to(actual)
    except OSError:
        pytest.skip("file symlinks are unavailable")
    validator_calls = []

    assert cache.get(action, lambda plan: validator_calls.append(plan) or True) is None
    assert validator_calls == []


def test_put_rejects_oversize_entry_before_creating_temp_file(tmp_path, monkeypatch):
    plan = _plan(
        {
            "kind": "create",
            "path": "knowledge/notes/large.md",
            "content": "x" * compile_cache.MAX_CACHE_ENTRY_BYTES,
        }
    )
    monkeypatch.setattr(
        compile_cache.tempfile,
        "mkstemp",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("temp created")),
    )

    with pytest.raises(ValueError, match="too large"):
        CompileCache(tmp_path).put(_action(), plan)


def test_put_fsyncs_cache_directory_after_replace(tmp_path, monkeypatch):
    synced = []
    monkeypatch.setattr(compile_cache, "fsync_directory", lambda path: synced.append(Path(path)))
    cache = CompileCache(tmp_path)

    cache.put(_action(), _plan())

    assert synced == [cache.cache_dir]


def test_cache_rejects_action_for_uncommitted_schema(tmp_path):
    action = replace(_action(), schema_version="compile-plan/v3", schema_hash="a" * 64)

    with pytest.raises(ValueError, match="committed compile-plan"):
        CompileCache(tmp_path).put(action, _plan())


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
        CompileCache(tmp_path).put(_action(), _plan())
