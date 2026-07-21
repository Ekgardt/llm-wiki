"""Tests for impact_analysis.py — LINK Layer (code → wiki connection)."""
from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from impact_analysis import (  # noqa: E402
    ImpactLimits,
    analyze_impact,
    collect_git_changes,
    extract_symbols_from_file,
    find_stale_wiki_pages,
    format_for_advisory,
)


def _git(root: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", *arguments], cwd=root, capture_output=True, text=True, check=True
    )
    return result.stdout.strip()


def _repository(tmp_path: Path) -> Path:
    root = tmp_path / "repository"
    root.mkdir()
    _git(root, "init")
    _git(root, "config", "user.email", "impact@example.test")
    _git(root, "config", "user.name", "Impact Test")
    (root / "alpha.py").write_text("def alpha():\n    return 1\n", encoding="utf-8")
    (root / "deleted.py").write_text("def removed():\n    return 1\n", encoding="utf-8")
    _git(root, "add", ".")
    _git(root, "commit", "-m", "initial")
    return root


class TestGitComparisons:
    def test_default_unions_staged_and_unstaged_changes(self, tmp_path):
        root = _repository(tmp_path)
        (root / "alpha.py").write_text("def alpha():\n    return 2\n", encoding="utf-8")
        _git(root, "add", "alpha.py")
        (root / "working.py").write_text("def working():\n    return 1\n", encoding="utf-8")
        _git(root, "add", "working.py")
        _git(root, "reset", "working.py")
        # Untracked files are not part of either Git diff endpoint. Make a tracked
        # worktree-only change instead.
        (root / "deleted.py").write_text("def removed():\n    return 2\n", encoding="utf-8")

        changes = collect_git_changes(root)

        assert {(item["new_path"], item["comparison"]) for item in changes} == {
            ("alpha.py", "index-HEAD"),
            ("deleted.py", "worktree-index"),
        }

    @pytest.mark.parametrize(
        ("comparison", "options", "expected"),
        [
            ("worktree-index", {}, {"deleted.py"}),
            ("index-HEAD", {}, {"alpha.py"}),
        ],
    )
    def test_explicit_index_endpoints(self, tmp_path, comparison, options, expected):
        root = _repository(tmp_path)
        (root / "alpha.py").write_text("def alpha():\n    return 2\n", encoding="utf-8")
        _git(root, "add", "alpha.py")
        (root / "deleted.py").write_text("def removed():\n    return 2\n", encoding="utf-8")

        changes = collect_git_changes(root, comparison=comparison, **options)

        assert {item["new_path"] for item in changes} == expected

    def test_two_commits_and_merge_base_branch_are_explicit(self, tmp_path):
        root = _repository(tmp_path)
        base = _git(root, "rev-parse", "HEAD")
        (root / "alpha.py").write_text("def alpha():\n    return 3\n", encoding="utf-8")
        _git(root, "commit", "-am", "change")
        target = _git(root, "rev-parse", "HEAD")

        commits = collect_git_changes(
            root, comparison="two-commits", base=base, target=target
        )
        branch = collect_git_changes(
            root, comparison="merge-base-branch", base=base, branch="HEAD"
        )

        assert [item["new_path"] for item in commits] == ["alpha.py"]
        assert [item["new_path"] for item in branch] == ["alpha.py"]

    def test_rename_and_delete_keep_old_blobs_and_unquoted_paths(self, tmp_path):
        root = _repository(tmp_path)
        _git(root, "mv", "alpha.py", "renamed file.py")
        _git(root, "rm", "deleted.py")

        changes = collect_git_changes(root, comparison="index-HEAD")
        renamed = next(item for item in changes if item["status"] == "R")
        deleted = next(item for item in changes if item["status"] == "D")

        assert (renamed["old_path"], renamed["new_path"]) == (
            "alpha.py",
            "renamed file.py",
        )
        assert renamed["old_blob"].startswith(b"def alpha")
        assert renamed["new_blob"].startswith(b"def alpha")
        assert deleted["old_blob"].startswith(b"def removed")
        assert deleted["new_blob"] is None

    def test_invalid_endpoint_shapes_fail_closed(self, tmp_path):
        root = _repository(tmp_path)
        with pytest.raises(ValueError, match="target"):
            collect_git_changes(root, comparison="two-commits", base="HEAD")
        with pytest.raises(ValueError, match="does not accept"):
            collect_git_changes(root, comparison="worktree-index", base="HEAD")
        with pytest.raises(ValueError, match="comparison"):
            collect_git_changes(root, comparison="HEAD..main")

    @pytest.mark.parametrize(
        "revision",
        ["-p", "bad\0rev", "bad\nrev", "bad\rrev", "x" * 1025],
    )
    def test_revision_input_is_rejected_before_git_diff(self, tmp_path, revision):
        root = _repository(tmp_path)

        with pytest.raises(ValueError, match="revision"):
            collect_git_changes(
                root,
                comparison="two-commits",
                base=revision,
                target="HEAD",
            )

    def test_git_output_option_revision_cannot_write_a_file(self, tmp_path):
        root = _repository(tmp_path)
        output = tmp_path / "must-not-exist.patch"

        with pytest.raises(ValueError, match="revision"):
            analyze_impact(
                root=root,
                comparison="two-commits",
                base=f"--output={output}",
                target="HEAD",
            )

        assert not output.exists()

    def test_diff_receives_only_verified_commit_oids(self, tmp_path, monkeypatch):
        import impact_analysis

        root = _repository(tmp_path)
        calls = []
        real_git = impact_analysis._git

        def recording_git(root, arguments, **kwargs):
            calls.append(tuple(arguments))
            return real_git(root, arguments, **kwargs)

        monkeypatch.setattr(impact_analysis, "_git", recording_git)
        collect_git_changes(
            root,
            comparison="two-commits",
            base="HEAD",
            target="HEAD",
        )

        assert calls[:2] == [
            ("rev-parse", "--verify", "--end-of-options", "HEAD^{commit}"),
            ("rev-parse", "--verify", "--end-of-options", "HEAD^{commit}"),
        ]
        diff = next(arguments for arguments in calls if arguments[0] == "diff")
        endpoints = diff[-3:-1]
        assert all(len(oid) in {40, 64} and int(oid, 16) >= 0 for oid in endpoints)

    def test_requested_root_ignores_ambient_git_repository_and_config_selectors(
        self, tmp_path, monkeypatch
    ):
        requested_parent = tmp_path / "requested"
        hostile_parent = tmp_path / "hostile"
        requested_parent.mkdir()
        hostile_parent.mkdir()
        requested = _repository(requested_parent)
        hostile = _repository(hostile_parent)
        (requested / "alpha.py").write_text("print('requested')\n", encoding="utf-8")
        monkeypatch.setenv("GIT_DIR", str(hostile / ".git"))
        monkeypatch.setenv("GIT_WORK_TREE", str(hostile))
        monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
        monkeypatch.setenv("GIT_CONFIG_KEY_0", "core.repositoryformatversion")
        monkeypatch.setenv("GIT_CONFIG_VALUE_0", "999")

        changes = collect_git_changes(requested, comparison="worktree-index")

        assert {item["new_path"] for item in changes} == {"alpha.py"}
        assert changes[0]["new_blob"].replace(b"\r\n", b"\n") == b"print('requested')\n"

    def test_repository_fsmonitor_command_never_executes(self, tmp_path):
        root = _repository(tmp_path)
        marker = tmp_path / "fsmonitor-ran"
        monitor = tmp_path / "monitor.py"
        monitor.write_text(
            "from pathlib import Path\n"
            f"Path({str(marker)!r}).write_text('ran')\n",
            encoding="utf-8",
        )
        _git(root, "config", "core.fsmonitor", f'"{sys.executable}" "{monitor}"')
        (root / "alpha.py").write_text("def alpha():\n    return 2\n", encoding="utf-8")

        changes = collect_git_changes(root, comparison="worktree-index")

        assert {item["new_path"] for item in changes} == {"alpha.py"}
        assert not marker.exists()


class _Graph:
    generation_id = "generation-24"

    def __init__(self):
        self.nodes = {
            "symbol": {"node_id": "symbol", "kind": "function", "identity_key": "alpha", "metadata": {"name": "alpha", "path": "alpha.py"}},
            "caller": {"node_id": "caller", "kind": "function", "identity_key": "call_alpha", "metadata": {"name": "call_alpha", "path": "service.py"}},
            "test": {"node_id": "test", "kind": "function", "identity_key": "test_alpha", "metadata": {"name": "test_alpha", "path": "tests/test_alpha.py"}},
            "module": {"node_id": "module", "kind": "module", "identity_key": "alpha", "metadata": {"name": "alpha", "path": "alpha.py"}},
            "importer": {"node_id": "importer", "kind": "module", "identity_key": "tests.test_import", "metadata": {"name": "tests.test_import", "path": "tests/test_import.py"}},
            "decision": {"node_id": "decision", "kind": "decision", "identity_key": "knowledge/notes/alpha.md", "metadata": {"page_type": "decision"}},
            "page": {"node_id": "page", "kind": "knowledge-page", "identity_key": "knowledge/notes/guide.md", "metadata": {}},
            "checkpoint": {"node_id": "checkpoint", "kind": "checkpoint", "identity_key": "project:7", "metadata": {"sequence": 7}},
            "project-file": {"node_id": "project-file", "kind": "file", "identity_key": "project:file-1", "metadata": {"value": "alpha.py"}},
        }
        self._edges = [
            {"assertion_id": "defines", "source_node_id": "module", "target_node_id": "symbol", "edge_type": "DEFINES", "confidence": "high"},
            {"assertion_id": "imports", "source_node_id": "importer", "target_node_id": "module", "edge_type": "IMPORTS", "confidence": "high"},
            {"assertion_id": "call", "source_node_id": "caller", "target_node_id": "symbol", "edge_type": "CALLS", "confidence": "high"},
            {"assertion_id": "test-call", "source_node_id": "test", "target_node_id": "symbol", "edge_type": "CALLS", "confidence": "high"},
            {"assertion_id": "documents", "source_node_id": "decision", "target_node_id": "symbol", "edge_type": "REFERENCES_SYMBOL", "confidence": "high"},
            {"assertion_id": "page-documents", "source_node_id": "page", "target_node_id": "symbol", "edge_type": "REFERENCES_SYMBOL", "confidence": "high"},
            {"assertion_id": "checkpoint-file", "source_node_id": "checkpoint", "target_node_id": "project-file", "edge_type": "CHECKPOINT_CHANGED_FILE", "confidence": "high"},
        ]

    def find_nodes(self, *, path=None, kinds=None, **_options):
        nodes = list(self.nodes.values())
        if path is not None:
            nodes = [node for node in nodes if node["metadata"].get("path") == path]
        if kinds is not None:
            nodes = [node for node in nodes if node["kind"] in kinds]
        return nodes

    def occurrences(self, node_id, **_options):
        if node_id == "symbol":
            return [{"relative_path": "alpha.py", "byte_start": 0, "byte_end": 29, "line_start": 1, "line_end": 2}]
        return []

    def edges(self, **_options):
        return list(self._edges)

    def node(self, node_id):
        return self.nodes.get(node_id)

    def evidence(self, *, assertion_id, **_options):
        return [{"relative_path": "evidence/" + assertion_id, "line_start": 1, "line_end": 1}]

    def close(self):
        pass


class TestGraphImpact:
    def test_maps_ranges_and_returns_typed_affected_nodes_with_evidence(self, tmp_path):
        root = _repository(tmp_path)
        (root / "alpha.py").write_text("def alpha():\n    return 200\n", encoding="utf-8")

        result = analyze_impact(root=root, graph=_Graph())

        assert result["classification"] == "exact"
        assert {item["node_id"] for item in result["changed_symbols"]} == {"symbol"}
        assert [item["node_id"] for item in result["affected"]["decisions"]] == ["decision"]
        assert [item["node_id"] for item in result["affected"]["pages"]] == ["page"]
        assert {item["node_id"] for item in result["affected"]["tests"]} == {"test", "importer"}
        assert [item["node_id"] for item in result["affected"]["checkpoints"]] == ["checkpoint"]
        assert all(item["evidence"] for group in result["affected"].values() for item in group)

    def test_missing_edge_evidence_downgrades_otherwise_resolved_impact(self, tmp_path):
        class GraphWithoutEvidence(_Graph):
            def evidence(self, **_options):
                return []

        root = _repository(tmp_path)
        (root / "alpha.py").write_text("def alpha():\n    return 200\n", encoding="utf-8")

        result = analyze_impact(root=root, graph=GraphWithoutEvidence())

        assert result["classification"] == "conservative"
        assert result["partial"] is True
        assert any("evidence" in warning.lower() for warning in result["warnings"])

    def test_deleted_file_maps_only_the_old_symbol_side(self, tmp_path):
        root = _repository(tmp_path)
        _git(root, "rm", "alpha.py")

        result = analyze_impact(
            root=root,
            comparison="index-HEAD",
            graph=_Graph(),
        )

        assert result["changed_symbols"][0]["sides"] == ["old"]

    def test_missing_graph_is_unresolved_and_text_fallback_is_separate(self, tmp_path, monkeypatch):
        import impact_analysis

        root = _repository(tmp_path)
        notes = root / "knowledge" / "notes"
        notes.mkdir(parents=True)
        (notes / "alpha.md").write_text("# Alpha\n\nUses alpha.\n", encoding="utf-8")
        (root / "alpha.py").write_text("def alpha():\n    return 20\n", encoding="utf-8")
        monkeypatch.setattr(impact_analysis, "KNOWLEDGE_DIR", notes)

        result = analyze_impact(root=root, graph=None)

        assert result["classification"] == "unresolved"
        assert result["affected"] == {"decisions": [], "pages": [], "tests": [], "checkpoints": []}
        assert result["textual_fallback"][0]["confidence"] == "low"
        assert result["textual_fallback"][0]["method"] == "textual-name-match"

    def test_read_and_record_limits_return_conservative_partial_result(self, tmp_path):
        root = _repository(tmp_path)
        (root / "alpha.py").write_bytes(b"x" * 128)

        result = analyze_impact(root=root, graph=_Graph(), limits=ImpactLimits(max_blob_bytes=32))

        assert result["classification"] == "conservative"
        assert result["partial"] is True
        assert result["warnings"]


def test_mcp_exposes_impact_as_get_architecture_mode_without_a_thirteenth_tool(monkeypatch):
    import asyncio
    import json

    import mcp_server

    expected = {"classification": "exact", "affected": {}}
    monkeypatch.setattr(mcp_server, "_analyze_impact", lambda **kwargs: expected)

    schema = mcp_server.TOOL_INPUT_SCHEMAS["get_architecture"]
    assert "mode" in schema["properties"]
    assert len(mcp_server.TOOL_INPUT_SCHEMAS) == 12
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    response = json.loads(loop.run_until_complete(mcp_server._handle_tool_call(
        "get_architecture",
        {"directory": str(Path.cwd()), "mode": "impact", "comparison": "index-HEAD"},
    )))

    assert response["data"] == expected
    assert response["components"] == {}
    assert set(response) == {
        "schema_version", "generated_at", "index_timestamp", "source_commit",
        "freshness", "coverage", "confidence", "fallback", "partial", "warnings",
        "components", "data",
    }


@pytest.mark.parametrize("bind_generation", [True, False], ids=("other-repository", "legacy-unbound"))
def test_active_graph_rejects_shared_state_generation_for_another_repository(
    tmp_path, monkeypatch, bind_generation
):
    import impact_analysis
    from evidence_graph_builder import build_full_generation
    from generation_catalog import GenerationCatalog
    from repository_scope import resolve_repository_scope

    repository_a = tmp_path / "first" / "same-name"
    repository_b = tmp_path / "second" / "same-name"
    repository_a.mkdir(parents=True)
    repository_b.mkdir(parents=True)
    state = tmp_path / "state"
    build_full_generation(
        GenerationCatalog(state),
        sources=(),
        source_bytes={},
        nodes=(),
        occurrences=(),
        assertions=(),
        evidence=(),
        observations=(),
        dependencies=(),
        generation_id="repository-a",
        **(
            {"repository_scope": resolve_repository_scope(repository_a)}
            if bind_generation
            else {}
        ),
    )
    monkeypatch.setattr(impact_analysis, "ROOT", repository_b)
    monkeypatch.setattr(impact_analysis, "STATE_ROOT", state)

    assert impact_analysis._active_graph(repository_b, time.monotonic() + 5) is None


def test_active_graph_uses_configured_shared_state_for_external_repository(
    tmp_path, monkeypatch
):
    import impact_analysis
    from evidence_graph_builder import build_full_generation
    from generation_catalog import GenerationCatalog
    from repository_scope import resolve_repository_scope

    repository = tmp_path / "external"
    repository.mkdir()
    state = tmp_path / "shared-state"
    build_full_generation(
        GenerationCatalog(state),
        sources=(),
        source_bytes={},
        nodes=(),
        occurrences=(),
        assertions=(),
        evidence=(),
        observations=(),
        dependencies=(),
        generation_id="external-generation",
        repository_scope=resolve_repository_scope(repository),
    )
    monkeypatch.setattr(impact_analysis, "STATE_ROOT", state)

    graph = impact_analysis._active_graph(repository, time.monotonic() + 5)

    assert graph is not None
    assert graph.generation_id == "external-generation"
    graph.close()


@pytest.mark.parametrize("timeout_source", ["scope", "graph"])
def test_active_graph_propagates_deadline_timeout(tmp_path, monkeypatch, timeout_source):
    import evidence_graph
    import impact_analysis
    import repository_scope

    root = tmp_path / "repository"
    root.mkdir()
    state = tmp_path / "state"
    catalog_path = state / "cache" / "evidence-graph" / "catalog.sqlite3"
    catalog_path.parent.mkdir(parents=True)
    catalog_path.touch()
    monkeypatch.setattr(impact_analysis, "ROOT", root)
    monkeypatch.setattr(impact_analysis, "STATE_ROOT", state)

    def timed_out(*_args, **_kwargs):
        raise TimeoutError("deadline exceeded")

    if timeout_source == "scope":
        monkeypatch.setattr(repository_scope, "resolve_repository_scope", timed_out)
    else:
        monkeypatch.setattr(
            evidence_graph.EvidenceGraph, "open_active_for_repository", timed_out
        )

    with pytest.raises(TimeoutError, match="deadline exceeded"):
        impact_analysis._active_graph(root, time.monotonic() + 5)


def test_public_analyze_impact_propagates_graph_open_timeout(tmp_path, monkeypatch):
    import impact_analysis

    root = _repository(tmp_path)

    def timed_out(*_args, **_kwargs):
        raise TimeoutError("graph deadline exceeded")

    monkeypatch.setattr(impact_analysis, "_active_graph", timed_out)

    with pytest.raises(TimeoutError, match="graph deadline exceeded"):
        analyze_impact(root=root)


@pytest.mark.parametrize("stage", ["git", "graph", "text"])
def test_public_analyze_impact_propagates_all_deadline_timeouts(
    tmp_path, monkeypatch, stage
):
    import impact_analysis

    root = _repository(tmp_path)
    (root / "alpha.py").write_text("def alpha():\n    return 2\n", encoding="utf-8")

    if stage == "git":
        monkeypatch.setattr(
            impact_analysis,
            "collect_git_changes",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                TimeoutError("git deadline")
            ),
        )
    elif stage == "graph":
        graph = _Graph()
        monkeypatch.setattr(
            graph,
            "evidence",
            lambda **_kwargs: (_ for _ in ()).throw(
                TimeoutError("evidence deadline")
            ),
        )
    else:
        graph = None

        def text_timeout(_symbols, *, deadline=None, **_kwargs):
            assert deadline is not None
            raise TimeoutError("text deadline")

        monkeypatch.setattr(impact_analysis, "find_stale_wiki_pages", text_timeout)

    with pytest.raises(TimeoutError, match="deadline"):
        analyze_impact(root=root, graph=locals().get("graph"))


def test_changed_ranges_rejects_expired_deadline():
    import impact_analysis

    with pytest.raises(TimeoutError, match="deadline"):
        impact_analysis._changed_ranges(b"old\n", b"new\n", deadline=time.monotonic() - 1)


def test_worktree_blob_rejects_file_identity_swap_before_descriptor_open(
    tmp_path, monkeypatch
):
    import bounded_io
    import impact_analysis

    root = tmp_path / "repository"
    root.mkdir()
    source = root / "page.py"
    source.write_bytes(b"safe")
    outside = tmp_path / "outside.py"
    outside.write_bytes(b"secret")
    real_open = bounded_io.os.open
    swapped = False

    def swapping_open(path, flags, *args, **kwargs):
        nonlocal swapped
        if Path(path) == source and not swapped:
            swapped = True
            os.replace(outside, source)
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(bounded_io.os, "open", swapping_open)

    with pytest.raises(PermissionError, match="changed before open"):
        impact_analysis._worktree_blob(root, "page.py", 1024)

    assert swapped is True


def test_analyze_impact_uses_caller_absolute_deadline(tmp_path, monkeypatch):
    import impact_analysis

    root = _repository(tmp_path)
    captured = []
    monkeypatch.setattr(
        impact_analysis,
        "collect_git_changes",
        lambda *_args, **kwargs: captured.append(kwargs["deadline"]) or [],
    )
    deadline = time.monotonic() + 30

    analyze_impact(root=root, deadline=deadline)

    assert captured == [deadline]


def test_changed_ranges_uses_linear_common_prefix_and_suffix():
    import impact_analysis

    old = b"same\nold-a\nold-b\ntail\n"
    new = b"same\nnew-a\nnew-b\ntail\n"

    ranges = impact_analysis._changed_ranges(old, new)

    assert ranges == [
        {
            "old": {
                "line_start": 2,
                "line_end": 3,
                "byte_start": 5,
                "byte_end": 17,
            },
            "new": {
                "line_start": 2,
                "line_end": 3,
                "byte_start": 5,
                "byte_end": 17,
            },
        }
    ]


@pytest.mark.parametrize(
    "limit_name,limit_value,files,match",
    [
        ("max_note_files", 1, {"a.md": "x", "b.md": "x"}, "file ceiling"),
        ("max_note_bytes", 4, {"a.md": "12345"}, "file byte ceiling"),
        (
            "max_total_note_bytes",
            7,
            {"a.md": "1234", "b.md": "5678"},
            "total byte ceiling",
        ),
    ],
)
def test_note_discovery_enforces_file_and_byte_bounds(
    tmp_path, monkeypatch, limit_name, limit_value, files, match
):
    import impact_analysis

    notes = tmp_path / "notes"
    notes.mkdir()
    for name, content in files.items():
        (notes / name).write_text(content, encoding="utf-8")
    monkeypatch.setattr(impact_analysis, "KNOWLEDGE_DIR", notes)
    limits = ImpactLimits(**{limit_name: limit_value})

    with pytest.raises(ValueError, match=match):
        find_stale_wiki_pages(["x"], limits=limits)


def test_note_discovery_enforces_directory_bound(tmp_path, monkeypatch):
    import impact_analysis

    notes = tmp_path / "notes"
    (notes / "a").mkdir(parents=True)
    (notes / "b").mkdir()
    monkeypatch.setattr(impact_analysis, "KNOWLEDGE_DIR", notes)

    with pytest.raises(ValueError, match="directory ceiling"):
        find_stale_wiki_pages(["x"], limits=ImpactLimits(max_note_dirs=1))


def test_note_discovery_ignores_symlinks(tmp_path, monkeypatch):
    import impact_analysis

    notes = tmp_path / "notes"
    notes.mkdir()
    outside = tmp_path / "outside.md"
    outside.write_text("secretSymbol", encoding="utf-8")
    link = notes / "linked.md"
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip("symlink creation is unavailable")
    monkeypatch.setattr(impact_analysis, "KNOWLEDGE_DIR", notes)

    assert find_stale_wiki_pages(["secretSymbol"]) == []


def test_note_discovery_propagates_cancellation(tmp_path, monkeypatch):
    import impact_analysis

    notes = tmp_path / "notes"
    notes.mkdir()
    (notes / "page.md").write_text("needle", encoding="utf-8")
    monkeypatch.setattr(impact_analysis, "KNOWLEDGE_DIR", notes)

    with pytest.raises(TimeoutError, match="cancelled"):
        find_stale_wiki_pages(["needle"], cancelled=lambda: True)


def test_note_capture_rejects_same_size_replacement_before_open(tmp_path, monkeypatch):
    import bounded_io
    import impact_analysis

    notes = tmp_path / "notes"
    notes.mkdir()
    page = notes / "page.md"
    page.write_text("safe-symbol", encoding="utf-8")
    replacement = tmp_path / "replacement.md"
    replacement.write_text("evil-symbol", encoding="utf-8")
    assert page.stat().st_size == replacement.stat().st_size
    real_open = bounded_io.os.open
    swapped = False

    def swapping_open(path, flags, *args, **kwargs):
        nonlocal swapped
        if (Path(path) == page or os.fspath(path) == page.name) and not swapped:
            swapped = True
            os.replace(replacement, page)
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(impact_analysis, "KNOWLEDGE_DIR", notes)
    monkeypatch.setattr(bounded_io.os, "open", swapping_open)

    with pytest.raises(PermissionError, match="changed"):
        find_stale_wiki_pages(["safe-symbol"])

    assert swapped is True


def test_note_capture_rejects_parent_identity_swap(tmp_path, monkeypatch):
    import impact_analysis

    notes = tmp_path / "notes"
    original = notes / "section"
    original.mkdir(parents=True)
    (original / "page.md").write_text("same-symbol", encoding="utf-8")
    replacement = tmp_path / "replacement-section"
    replacement.mkdir()
    (replacement / "page.md").write_text("same-symbol", encoding="utf-8")
    parked = tmp_path / "parked-section"
    real_seal = impact_analysis._seal_path
    swapped = False

    def swapping_seal(*args, **kwargs):
        nonlocal swapped
        seal = real_seal(*args, **kwargs)
        if not swapped:
            swapped = True
            os.replace(original, parked)
            os.replace(replacement, original)
        return seal

    monkeypatch.setattr(impact_analysis, "KNOWLEDGE_DIR", notes)
    monkeypatch.setattr(impact_analysis, "_seal_path", swapping_seal)

    with pytest.raises(PermissionError, match="ancestor changed"):
        find_stale_wiki_pages(["same-symbol"])

    assert swapped is True


class TestExtractSymbols:
    """Test symbol extraction from source files."""

    def test_extract_from_python(self, tmp_path):
        f = tmp_path / "test.py"
        f.write_text("def hello():\n    pass\nclass World:\n    pass\n", encoding="utf-8")
        symbols = extract_symbols_from_file(f)
        assert "hello" in symbols
        assert "World" in symbols

    def test_extract_from_javascript(self, tmp_path):
        f = tmp_path / "app.js"
        f.write_text("function greet() {}\n", encoding="utf-8")
        symbols = extract_symbols_from_file(f)
        assert "greet" in symbols

    def test_extract_from_nonexistent(self, tmp_path):
        symbols = extract_symbols_from_file(tmp_path / "nope.py")
        assert symbols == []

    def test_extract_deduplicates(self, tmp_path):
        f = tmp_path / "dup.py"
        f.write_text("def foo():\n    foo()\n    foo()\n", encoding="utf-8")
        symbols = extract_symbols_from_file(f)
        assert symbols.count("foo") == 1


class TestFindStaleWikiPages:
    """Test finding wiki pages that reference changed symbols."""

    def test_finds_page_mentioning_symbol(self, tmp_path, monkeypatch):
        """A wiki page that mentions a changed function should be found."""
        import impact_analysis

        notes = tmp_path / "notes"
        notes.mkdir()
        page = notes / "auth-decision.md"
        page.write_text(
            "---\ntype: decision\n---\n\n"
            "# Auth Decision\n\n"
            "We use verifyToken for auth.\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(impact_analysis, "KNOWLEDGE_DIR", notes)

        results = find_stale_wiki_pages(["verifyToken"])
        assert len(results) == 1
        assert results[0]["slug"] == "auth-decision"
        assert "verifyToken" in results[0]["matched_symbols"]

    def test_no_match_returns_empty(self, tmp_path, monkeypatch):
        import impact_analysis

        notes = tmp_path / "notes"
        notes.mkdir()
        page = notes / "unrelated.md"
        page.write_text("# Unrelated\n\nNothing about code.\n", encoding="utf-8")
        monkeypatch.setattr(impact_analysis, "KNOWLEDGE_DIR", notes)

        results = find_stale_wiki_pages(["nonexistentSymbol"])
        assert results == []

    def test_skips_superseded(self, tmp_path, monkeypatch):
        """Superseded pages should not be flagged as stale."""
        import impact_analysis

        notes = tmp_path / "notes"
        notes.mkdir()
        page = notes / "old.md"
        page.write_text(
            "---\nstatus: superseded\n---\n\n"
            "# Old\n\nMentions verifyToken.\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(impact_analysis, "KNOWLEDGE_DIR", notes)

        results = find_stale_wiki_pages(["verifyToken"])
        assert len(results) == 0

    def test_confidence_levels(self, tmp_path, monkeypatch):
        """Multiple symbol matches → high confidence."""
        import impact_analysis

        notes = tmp_path / "notes"
        notes.mkdir()
        page = notes / "multi.md"
        page.write_text(
            "# Multi\n\nUses funcA, funcB, and funcC.\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(impact_analysis, "KNOWLEDGE_DIR", notes)

        results = find_stale_wiki_pages(["funcA", "funcB", "funcC"])
        assert len(results) == 1
        assert results[0]["confidence"] == "high"

    def test_word_boundary_matching(self, tmp_path, monkeypatch):
        """Symbol 'auth' should not match 'authentication'."""
        import impact_analysis

        notes = tmp_path / "notes"
        notes.mkdir()
        page = notes / "page.md"
        page.write_text("# Page\n\nAbout authentication.\n", encoding="utf-8")
        monkeypatch.setattr(impact_analysis, "KNOWLEDGE_DIR", notes)

        results = find_stale_wiki_pages(["auth"])
        # 'auth' as a word boundary should NOT match 'authentication'
        assert len(results) == 0


class TestFormatForAdvisory:
    """Test advisory formatting for SessionStart."""

    def test_empty_stale_returns_empty(self):
        result = format_for_advisory({"stale_pages": [], "summary": "nothing"})
        assert result == ""

    def test_formats_pages(self):
        impact = {
            "summary": "3 files, 5 symbols, 2 stale pages.",
            "stale_pages": [
                {"slug": "page-a", "confidence": "high", "reason": "mentions 3 symbols", "matched_symbols": ["a", "b", "c"]},
                {"slug": "page-b", "confidence": "medium", "reason": "mentions 1 symbol", "matched_symbols": ["d"]},
            ],
        }
        result = format_for_advisory(impact)
        assert "Code-Knowledge Impact" in result
        assert "page-a" in result
        assert "page-b" in result

    def test_limits_to_max_pages(self):
        impact = {
            "summary": "many changes",
            "stale_pages": [
                {"slug": f"page-{i}", "confidence": "medium", "reason": "test", "matched_symbols": ["x"]}
                for i in range(10)
            ],
        }
        result = format_for_advisory(impact, max_pages=3)
        assert "page-0" in result
        assert "page-2" in result
        assert "7 more" in result
