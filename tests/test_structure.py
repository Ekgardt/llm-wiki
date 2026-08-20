"""Structural invariants for the llm-wiki repository.

These tests enforce the canonical three-zone layout and agent-contract
identity so that architectural drift is caught automatically (CI +
pre-commit) rather than discovered mid-task.

The canonical reference is `docs/STRUCTURE.md` + this file. If a test here
fails, the structure was changed without updating the canonical reference —
fix the reference first, then the code.
"""
from __future__ import annotations

import ast
import os
import re
import subprocess
from datetime import date, datetime
from pathlib import Path

import memory_state as early_memory_state
import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent


def _assert_literal_note_allowlist(lines: list[str]) -> None:
    """Require every knowledge/notes negation to name one literal Markdown file."""
    for line in lines:
        if not line.startswith("!"):
            continue
        relative = line[1:].removeprefix("/")
        if not relative.startswith("knowledge/notes/"):
            continue
        assert not any(char in relative for char in "*?[]"), (
            f"knowledge note allowlist rule must not contain glob syntax: {line}"
        )
        assert re.fullmatch(
            r"knowledge/notes/(?:[^/\\]+/)*[^/\\]+\.md", relative
        ), f"knowledge note allowlist rule must name one Markdown file: {line}"


def _required_frontmatter_scalars(
    text: str, expected: dict[str, str]
) -> dict[str, str]:
    """Load required root scalars after validating the YAML representation graph."""
    lines = text.splitlines()
    assert lines and lines[0] == "---", "decision must start with YAML frontmatter"
    try:
        closing = lines.index("---", 1)
    except ValueError as exc:
        raise AssertionError("decision frontmatter must have a closing delimiter") from exc
    frontmatter = "\n".join(lines[1:closing]) + "\n"

    try:
        tokens = yaml.scan(frontmatter, Loader=yaml.SafeLoader)
        assert not any(
            isinstance(token, (yaml.tokens.AliasToken, yaml.tokens.AnchorToken))
            for token in tokens
        ), "frontmatter aliases and anchors are not allowed"
        root = yaml.compose(frontmatter, Loader=yaml.SafeLoader)
    except yaml.YAMLError as exc:
        raise AssertionError("decision frontmatter must be valid safe YAML") from exc

    assert isinstance(root, yaml.nodes.MappingNode), "frontmatter must be a mapping"
    assert root.tag == "tag:yaml.org,2002:map", "frontmatter must use the standard map tag"

    seen: set[str] = set()
    for key_node, value_node in root.value:
        assert isinstance(key_node, yaml.nodes.ScalarNode), (
            "frontmatter keys must be scalar strings"
        )
        assert key_node.tag == "tag:yaml.org,2002:str", (
            "frontmatter keys must use the standard string tag"
        )
        assert key_node.value not in seen, (
            f"frontmatter key {key_node.value!r} must not be duplicated"
        )
        seen.add(key_node.value)

        pending = [value_node]
        while pending:
            node = pending.pop()
            assert node.tag.startswith("tag:yaml.org,2002:"), (
                "frontmatter custom tags are not allowed"
            )
            if isinstance(node, yaml.nodes.MappingNode):
                pending.extend(child for pair in node.value for child in pair)
            elif isinstance(node, yaml.nodes.SequenceNode):
                pending.extend(node.value)

    try:
        loaded = yaml.safe_load(frontmatter)
    except yaml.YAMLError as exc:
        raise AssertionError("decision frontmatter must be safe to load") from exc
    assert isinstance(loaded, dict), "frontmatter must load as a mapping"

    actual: dict[str, str] = {}
    for key, expected_value in expected.items():
        assert key in loaded, f"frontmatter requires {key!r}"
        value = loaded[key]
        if key == "date" and isinstance(value, (date, datetime)):
            value = value.date().isoformat() if isinstance(value, datetime) else value.isoformat()
        assert isinstance(value, str), f"frontmatter {key!r} must be a scalar string"
        actual[key] = value
        assert value == expected_value, f"frontmatter {key!r} has unexpected value"
    assert actual == expected
    return actual


# ---------------------------------------------------------------------------
# Agent contract identity — AGENTS.md and CLAUDE.md MUST be byte-identical.
# ---------------------------------------------------------------------------


def test_agents_md_and_claude_md_are_identical():
    """AGENTS.md and CLAUDE.md serve the same purpose (agent operating
    contract) and must be byte-identical so every agent reads the same rules
    regardless of which file it loads. If this fails, sync one to the other.
    """
    agents = (ROOT / "AGENTS.md").read_bytes()
    claude = (ROOT / "CLAUDE.md").read_bytes()
    assert agents == claude, (
        "AGENTS.md and CLAUDE.md have diverged. Run: "
        "Copy-Item AGENTS.md CLAUDE.md -Force  (or vice versa)"
    )
    contract = agents.decode("utf-8")
    contract_words = " ".join(contract.split())
    for value in (
        "cache/evidence-graph/catalog.sqlite3",
        "cache/evidence-graph/generations/<generation-id>/",
        "immutable after activation",
        "rollback-journal",
        "synchronous=FULL",
        "no WAL",
        "local filesystem",
        "no persistent daemon",
        "No generation database belongs under `run/`",
        "target generation layout",
        "cache/index.sqlite",
        "cache/vectors.npy",
        "cache/vectors_meta.json",
        "cache/lancedb/",
        "remain readable during migration",
        "disposable derived caches",
        "not members of a generation",
        "installed-vault migration evidence",
    ):
        assert value in contract_words, f"agent contracts must document {value!r}"


def test_superset_contract_is_canonical() -> None:
    agents = (ROOT / "AGENTS.md").read_bytes()
    claude = (ROOT / "CLAUDE.md").read_bytes()
    structure = (ROOT / "docs" / "STRUCTURE.md").read_text(encoding="utf-8")
    decision = "knowledge/notes/solo-operator-superset-product-decision.md"
    implemented_checkpoint = structure.split(
        "## Implemented corpus-generation checkpoint", 1
    )[
        1
    ].split("\n## ", 1)[0]
    assert agents == claude
    assert decision.encode() in agents
    assert (ROOT / decision).is_file()
    for value in (
        "corpus-generation/v2",
        "repository_scope",
        "source-manifest.json",
        "incremental-manifest.json",
        "evidence.sqlite3",
        "search.sqlite3",
    ):
        assert value in implemented_checkpoint
    for unimplemented_path in (
        "workspace_registry.py",
        "code_index.py",
        "temporal_claims.py",
        "task_control.py",
        "operator_console.py",
        "workspace-registry.sqlite3",
        "task-control.sqlite3",
    ):
        assert unimplemented_path not in implemented_checkpoint


def test_code_navigation_python_slice_is_reported_as_current() -> None:
    structure = (ROOT / "docs/STRUCTURE.md").read_text(encoding="utf-8")
    current = structure.split("## Implemented corpus-generation checkpoint", 1)[
        1
    ].split("\n## ", 1)[0]
    navigation = structure.split("## Implemented Python code navigation", 1)[1].split(
        "\n## ", 1
    )[0]
    old_decision_path = (
        ROOT / "knowledge/notes/persistent-code-intelligence-kernel-decision.md"
    )
    decision_path = (
        ROOT / "knowledge/notes/read-only-lsp-navigation-engine-decision.md"
    )
    agents = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
    claude = (ROOT / "CLAUDE.md").read_text(encoding="utf-8")

    assert decision_path.is_file()
    assert old_decision_path.is_file()
    decision = decision_path.read_text(encoding="utf-8")
    old_decision = old_decision_path.read_text(encoding="utf-8")
    _required_frontmatter_scalars(
        decision,
        {
            "type": "decision",
            "status": "active",
            "confidence": "high",
            "source_authority": "user",
            "date": "2026-07-22",
        },
    )
    _required_frontmatter_scalars(
        old_decision,
        {
            "type": "decision",
            "status": "superseded",
            "superseded_by": "[[read-only-lsp-navigation-engine-decision]]",
        },
    )
    assert "evidence-graph/v2" in current
    assert "evidence-graph/v3" not in current
    navigation_words = " ".join(navigation.split())
    for value in (
        "existing structural Evidence Graph",
        "read-only LSP runtime owned by LLM Wiki",
        "implemented Python/Pyright slice",
        "document synchronization",
        "session-manager capacity",
        "normalized navigation facade",
        "existing 12 task-shaped MCP tools",
        "no Serena runtime dependency",
        "not written into an active generation",
        "cache/code-tools/pyright/1.1.411/",
        "run/lsp/<owner-nonce>/",
        "Python 3.10",
    ):
        assert value in navigation_words
    for text in (agents, claude, decision):
        normalized = " ".join(text.split())
        assert "read-only LSP" in normalized
        assert "Serena runtime dependency" in normalized
        assert "persistent daemon" in normalized
    for stale in (
        "normalized navigation is not implemented",
        "normalized navigation facade, and MCP routing remain unimplemented",
    ):
        assert stale not in structure
        assert stale not in agents
        assert stale not in claude
    assert "Tasks 6-16 are superseded" in " ".join(decision.split())
    assert (ROOT / "AGENTS.md").read_bytes() == (ROOT / "CLAUDE.md").read_bytes()


def test_lsp_live_lease_layout_is_canonical() -> None:
    decision_path = ROOT / "knowledge/notes/lsp-live-lease-decision.md"
    structure = (ROOT / "docs/STRUCTURE.md").read_text(encoding="utf-8")
    design = (
        ROOT / "docs/superpowers/specs/2026-07-22-read-only-lsp-navigation-design.md"
    ).read_text(encoding="utf-8")
    agents = (ROOT / "AGENTS.md").read_text(encoding="utf-8")

    assert decision_path.is_file()
    decision = decision_path.read_text(encoding="utf-8")
    _required_frontmatter_scalars(
        decision,
        {
            "type": "decision",
            "status": "active",
            "confidence": "high",
            "source_authority": "user",
            "date": "2026-07-23",
        },
    )
    for text in (decision, structure, agents):
        normalized = " ".join(text.split())
        for value in (
            "run/lsp/<owner-nonce>/lease.json",
            "10 seconds",
            "30 seconds",
            "bounded mutable live lease",
            "owner.json",
            "failure.json",
        ):
            assert value in normalized
    for text in (structure, design):
        normalized = " ".join(text.split()).casefold()
        for value in (
            "hidden temporary name",
            "before the create call",
            "cleanup_pending",
            "lease removal",
            "owner close",
            "successful terminal layout",
        ):
            assert value in normalized
    existing = (
        ROOT / "knowledge/notes/read-only-lsp-navigation-engine-decision.md"
    ).read_text(encoding="utf-8")
    assert "[[lsp-live-lease-decision]]" in existing
    assert (ROOT / "AGENTS.md").read_bytes() == (ROOT / "CLAUDE.md").read_bytes()


def test_lsp_process_containment_contract_is_platform_qualified() -> None:
    decision_path = ROOT / "knowledge/notes/lsp-process-containment-decision.md"
    assert decision_path.is_file()
    decision = decision_path.read_text(encoding="utf-8")
    _required_frontmatter_scalars(
        decision,
        {
            "type": "decision",
            "status": "active",
            "confidence": "high",
            "source_authority": "user",
            "date": "2026-07-24",
        },
    )

    structure = (ROOT / "docs/STRUCTURE.md").read_text(encoding="utf-8")
    design = (
        ROOT / "docs/superpowers/specs/2026-07-22-read-only-lsp-navigation-design.md"
    ).read_text(encoding="utf-8")
    plan = (
        ROOT / "docs/superpowers/plans/2026-07-22-python-pyright-navigation.md"
    ).read_text(encoding="utf-8")
    task6 = plan.split("### Task 6:", 1)[1].split("### Task 7:", 1)[0]
    task15 = plan.split("### Task 15:", 1)[1]
    module = (ROOT / "scripts/lsp_process_tree.py").read_text(encoding="utf-8")
    for text in (decision, structure, design, task6, task15, module):
        normalized = " ".join(text.split())
        assert "Windows Job Object" in normalized
        assert "POSIX process group" in normalized
        assert "setsid()" in normalized
        assert "unsupported" in normalized
        assert "trusted" in normalized

    decision_words = " ".join(decision.split())
    assert "delegated cgroup v2" in decision_words
    assert "future" in decision_words
    assert "ancestry scan" in decision_words
    assert "what is true today" in decision_words
    assert "what we want it to become" in decision_words

    navigation = (
        ROOT / "knowledge/notes/read-only-lsp-navigation-engine-decision.md"
    ).read_text(encoding="utf-8")
    lease = (ROOT / "knowledge/notes/lsp-live-lease-decision.md").read_text(
        encoding="utf-8"
    )
    for related in (navigation, lease):
        assert "[[lsp-process-containment-decision]]" in related
    assert "[[read-only-lsp-navigation-engine-decision]]" in decision
    assert "[[lsp-live-lease-decision]]" in decision

    index = (ROOT / "knowledge/index.md").read_text(encoding="utf-8")
    log = (ROOT / "knowledge/log.md").read_text(encoding="utf-8")
    assert "[[knowledge/notes/lsp-process-containment-decision]]" in index
    assert "2026-07-24" in log and "process containment" in log

    gitignore = (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    allowlist = "!knowledge/notes/lsp-process-containment-decision.md"
    assert gitignore.count(allowlist) == 1
    _assert_literal_note_allowlist(gitignore)


def test_agent_contract_mentions_three_zone_process_rule():
    """The contract must document the 'architecture changes require sign-off'
    rule so future agents don't improvise structural changes mid-task.
    """
    text = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
    assert "sign-off" in text.lower() or "explicit" in text.lower(), (
        "AGENTS.md must mention the architecture-change sign-off process"
    )
    template = ROOT / "integrations" / "obsidian" / "Article-to-Inbox.json"
    assert not template.exists(), (
        "Obsidian is an optional Markdown viewer; do not bundle ingestion wiring"
    )


# ---------------------------------------------------------------------------
# Three-zone layout — directory invariants.
# ---------------------------------------------------------------------------

CODE_DIRS = {
    "scripts", "tests", "docs", "skills", "rules", "integrations", "benchmark",
}
KNOWLEDGE_DIRS = {
    "daily", "notes", "projects", "raw", "inbox", "feedback",
}
RUNTIME_DIRS = {
    "cache", "logs", "run",
}
FORBIDDEN_ROOT_DIRS = {
    "wiki", "memory", "outputs", "state", "LLM-wiki-state",
}


@pytest.mark.parametrize("name", sorted(CODE_DIRS))
def test_code_zone_dirs_exist(name: str):
    d = ROOT / name
    assert d.is_dir(), f"CODE zone dir missing: {name}/"


@pytest.mark.parametrize("name", sorted(KNOWLEDGE_DIRS))
def test_knowledge_zone_dirs_exist(name: str):
    d = ROOT / "knowledge" / name
    assert d.is_dir(), f"KNOWLEDGE zone dir missing: knowledge/{name}/"


@pytest.mark.parametrize("name", sorted(RUNTIME_DIRS))
def test_runtime_dirs_are_gitignored(name: str):
    """Runtime dirs may or may not exist (created on demand), but if they
    exist they MUST be gitignored — never tracked.
    """
    d = ROOT / name
    if not d.exists():
        return
    import subprocess

    result = subprocess.run(
        ["git", "check-ignore", name],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        f"runtime dir {name}/ exists at vault root but is NOT gitignored"
    )


@pytest.mark.parametrize("name", sorted(FORBIDDEN_ROOT_DIRS))
def test_forbidden_root_dirs_absent(name: str):
    d = ROOT / name
    assert not d.exists(), (
        f"forbidden dir {name}/ exists at vault root — three-zone violation"
    )


def test_no_tracked_files_in_runtime_dirs():
    """No file under cache/, logs/, run/ should be tracked by git."""
    import subprocess

    result = subprocess.run(
        ["git", "ls-files", "cache/", "logs/", "run/", "cognee/"],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
    )
    tracked = [line for line in result.stdout.strip().splitlines() if line]
    assert not tracked, (
        f"runtime dirs have tracked files (should be gitignored): {tracked}"
    )


# ---------------------------------------------------------------------------
# memory_state.py default — STATE_ROOT must default to the vault root.
# ---------------------------------------------------------------------------


def test_memory_state_default_is_vault_root():
    """The canonical layout puts runtime INSIDE the vault (gitignored). The
    default STATE_ROOT in memory_state.py must be ROOT (the vault), not a
    sibling LLM-wiki-state/ dir. This catches accidental regression to the
    old sibling layout.
    """
    src = (ROOT / "scripts" / "memory_state.py").read_text(encoding="utf-8")
    # The default expression must resolve to ROOT, not ROOT.parent / ...
    assert 'os.environ.get("LLM_WIKI_STATE_ROOT", str(ROOT))' in src, (
        "memory_state.py STATE_ROOT default must be str(ROOT), not "
        "ROOT.parent / 'LLM-wiki-state'. See docs/STRUCTURE.md."
    )
    assert "ROOT.parent" not in src.split("STATE_ROOT")[1].split("\n")[0], (
        "STATE_ROOT line references ROOT.parent — sibling layout regression"
    )


def test_imported_memory_state_uses_the_hermetic_test_root():
    assert early_memory_state.STATE_ROOT == Path(os.environ["LLM_WIKI_STATE_ROOT"]).resolve()


def test_lsp_runtime_paths_and_navigation_are_canonical() -> None:
    state_root = Path(os.environ["LLM_WIKI_STATE_ROOT"]).resolve()
    structure = (ROOT / "docs/STRUCTURE.md").read_text(encoding="utf-8")
    navigation = structure.split("## Implemented Python code navigation", 1)[1].split(
        "\n## ", 1
    )[0]

    assert early_memory_state.CODE_TOOLS_DIR == state_root / "cache/code-tools"
    assert early_memory_state.LSP_RUN_DIR == state_root / "run/lsp"
    assert "scripts/lsp_paths.py" in structure
    assert "path helpers are implemented" in navigation
    assert "normalized navigation facade" in navigation
    assert "precise live navigation" in navigation


# ---------------------------------------------------------------------------
# README i18n structural parity (section count + claims).
# ---------------------------------------------------------------------------


def test_readmes_have_same_h2_section_count():
    """All three READMEs must have the same number of top-level sections.
    Drift here is the #1 cause of 'RU/ZH is a compressed digest, not a
    translation'.
    """
    import re

    counts = {}
    for name in ("README.md", "README.ru.md", "README.zh-CN.md"):
        text = (ROOT / name).read_text(encoding="utf-8")
        h2s = re.findall(r"^## ", text, re.MULTILINE)
        counts[name] = len(h2s)
    vals = list(counts.values())
    assert len(set(vals)) == 1, (
        f"README H2 section count drift: {counts}. "
        "All three must have the same number of top-level sections."
    )


def test_docs_name_stage_two_runtime_artifacts():
    structure = (ROOT / "docs" / "STRUCTURE.md").read_text(encoding="utf-8")
    structure_words = " ".join(structure.split())
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    project_dependencies, dev_dependencies = pyproject.split("[dependency-groups]", 1)
    assert "pyyaml" not in project_dependencies.casefold(), (
        "PyYAML must remain a test/dev dependency, not a project dependency"
    )
    assert '"pyyaml>=6.0.3,<7"' in dev_dependencies.casefold()
    for value in (
        "run/markdown-transactions.sqlite3",
        "run/transactions/",
        "run/queue.sqlite3",
        "run/queue-results/",
        "cache/compile/",
        "cache/claims.sqlite3",
        "scripts/schemas/",
        "knowledge/daily/receipts/",
        "knowledge/projects/<slug>/journal.md",
        "knowledge/daily/archive/YYYY-MM/bag-",
    ):
        assert value in structure

    generation_files = {
        "manifest.json",
        "source-manifest.json",
        "incremental-manifest.json",
        "evidence.sqlite3",
        "search.sqlite3",
        "vectors.npy",
        "vectors.json",
    }
    for value in (
        "cache/evidence-graph/catalog.sqlite3",
        "cache/evidence-graph/telemetry.sqlite3",
        "cache/evidence-graph/generations/<generation-id>/",
        "immutable after activation",
        "one active generation",
        "absent, complete, or explicitly stale",
        "No generation database belongs under `run/`",
        "operational state only",
        "implemented v2 layout",
        "cache/index.sqlite",
        "cache/vectors.npy",
        "cache/vectors_meta.json",
        "cache/lancedb/",
        "remain readable during migration",
        "disposable derived caches",
        "not members of a generation",
        "installed-vault migration evidence",
    ):
        assert value in structure_words, f"STRUCTURE.md must document {value!r}"
    generation_block = structure.split(
        "cache/evidence-graph/generations/<generation-id>/", 1
    )[1].split("```", 1)[0]
    documented_files = {
        line.strip().removeprefix("├── ").removeprefix("└── ").split()[0]
        for line in generation_block.splitlines()
        if line.strip().startswith(("├── ", "└── "))
    }
    assert documented_files == generation_files
    assert "run/evidence-graph" not in structure
    assert "cross-generation" in structure_words
    assert "private" in structure_words
    assert "not authoritative" in structure_words
    assert "run/generations/" not in structure

    gitignore = (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    decision_allowlist = "!knowledge/notes/derived-evidence-generation-decision.md"
    assert gitignore.count(decision_allowlist) == 1
    _assert_literal_note_allowlist(gitignore)
    for broad_rule in (
        "!knowledge/notes/*",
        "!knowledge/notes/**",
        "!knowledge/notes/*.md",
        "!/knowledge/notes/**/public.md",
        "!knowledge/notes/public?/page.md",
        "!knowledge/notes/[ab].md",
        "!knowledge/notes/public/",
    ):
        with pytest.raises(AssertionError):
            _assert_literal_note_allowlist([broad_rule])

    decision = ROOT / "knowledge" / "notes" / "derived-evidence-generation-decision.md"
    assert decision.is_file()
    text = decision.read_text(encoding="utf-8")
    decision_words = " ".join(text.split())
    required_scalars = {
        "type": "decision",
        "status": "active",
        "confidence": "high",
        "source_authority": "user",
        "date": "2026-07-17",
    }
    _required_frontmatter_scalars(text, required_scalars)
    quoted_frontmatter = text
    for old, new in (
        ("type: decision", '"type": "decision" # public decision'),
        ("status: active", "'status': 'active' # current status"),
        ("confidence: high", 'confidence: "high" # reviewed'),
        ("source_authority: user", "source_authority: 'user' # approved"),
        ("date: 2026-07-17", 'date: "2026-07-17" # implementation date'),
    ):
        quoted_frontmatter = quoted_frontmatter.replace(old, new, 1)
    _required_frontmatter_scalars(quoted_frontmatter, required_scalars)
    duplicate_type = text.replace("type: decision", "type: decision\ntype: concept", 1)
    quoted_duplicate_type = text.replace(
        "type: decision", "'type': decision\n\"type\": concept", 1
    )
    empty_status = text.replace("status: active", "status: # missing", 1)
    alias_value = text.replace(
        "type: decision\n", "type: &decision_type decision\n", 1
    ).replace("status: active", "status: *decision_type", 1)
    custom_tag = text.replace("type: decision", "type: !private decision", 1)
    complex_key = text.replace("type: decision", "? [type]\n: decision", 1)
    sequence_root = text.replace("type: decision", "- type: decision", 1)
    for malformed in (
        duplicate_type,
        quoted_duplicate_type,
        empty_status,
        alias_value,
        custom_tag,
        complex_key,
        sequence_root,
    ):
        with pytest.raises(AssertionError):
            _required_frontmatter_scalars(malformed, required_scalars)
    for value in (
        "Markdown, Git, and project journals",
        "graph, FTS, vector, tier, and telemetry",
        "disposable",
        "immutable after activation",
        "No persistent daemon",
        "docs/superpowers/plans/2026-07-16-unified-evidence-retrieval.md",
        "https://www.sqlite.org/atomiccommit.html",
        "https://www.sqlite.org/lockingv3.html",
        "## Rejected alternatives",
        "## Consequences",
        "target generation layout",
        "cache/index.sqlite",
        "cache/vectors.npy",
        "cache/vectors_meta.json",
        "cache/lancedb/",
        "remain readable during migration",
        "disposable derived caches",
        "not members of a generation",
        "installed-vault migration evidence",
    ):
        assert value in decision_words, f"public decision must document {value!r}"
    private_patterns = {
        "Windows absolute path": r"(?i)(?:^|[^A-Za-z0-9])[A-Z]:[\\/]",
        "UNC path": r"\\\\[^\\/\s]+[\\/][^\\/\s]+",
        "user home path": r"/(?:home|Users)/[^/\s]+/",
        "private/session marker": (
            r"(?i)\b(?:transcript[_-](?:id|path)|session[_-](?:id|path)|"
            r"private[_-]data)\b"
        ),
    }
    for label, pattern in private_patterns.items():
        assert re.search(pattern, text) is None, f"public decision contains {label}"
    private_examples = {
        "Windows absolute path": r"[D:\vault\private.md] and 'E:/vault/private.md'",
        "UNC path": r"[\\server\share\private.md] and '\\host\docs\note.md'",
        "user home path": "'/home/alice/private.md' and [\"/Users/bob/private.md\"]",
        "private/session marker": "transcript_path session_id private-data",
    }
    for label, example in private_examples.items():
        assert re.search(private_patterns[label], example), (
            f"privacy pattern does not reject representative {label}"
        )
    safe_public_examples = (
        "session",
        "Session data is excluded from this public product decision.",
        "https://www.sqlite.org/atomiccommit.html",
        "docs/superpowers/plans/2026-07-16-unified-evidence-retrieval.md",
    )
    for example in safe_public_examples:
        for label, pattern in private_patterns.items():
            assert re.search(pattern, example) is None, (
                f"privacy pattern {label!r} rejected safe public text: {example}"
            )


def _duplicate_top_level_names(source: str) -> list[str]:
    """Names a module binds twice at top level, where the second silently wins."""
    seen: set[str] = set()
    duplicates: list[str] = []
    definition = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
    for node in ast.parse(source).body:
        if not isinstance(node, definition):
            continue
        if node.name in seen:
            duplicates.append(node.name)
        seen.add(node.name)
    return duplicates


def _python_sources() -> list[Path]:
    """Every source we own; fixtures include a deliberately unparsable file."""
    directories = ("scripts", "tests", "benchmark", "integrations")
    found = (
        path for name in directories for path in sorted((ROOT / name).rglob("*.py"))
    )
    return [path for path in found if "fixtures" not in path.parts]


def test_the_duplicate_detector_sees_a_shadowed_definition() -> None:
    shadowed = "def a():\n    pass\n\n\ndef a():\n    pass\n"
    assert _duplicate_top_level_names(shadowed) == ["a"]
    assert _duplicate_top_level_names("def a():\n    pass\n\n\ndef b():\n    pass\n") == []


def test_no_module_defines_the_same_top_level_name_twice() -> None:
    """A second definition silently replaces the first, so an edit can go unnoticed."""
    offenders = {}
    for path in _python_sources():
        duplicates = _duplicate_top_level_names(path.read_text(encoding="utf-8"))
        if duplicates:
            offenders[str(path.relative_to(ROOT))] = duplicates

    assert offenders == {}


def _decorator_root_name(node: ast.expr) -> str:
    """The bare name a decorator expression ultimately refers to."""
    while isinstance(node, ast.Call):
        node = node.func
    while isinstance(node, ast.Attribute):
        node = node.value
    return node.id if isinstance(node, ast.Name) else ""


def _binds_instance(function: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    arguments = function.args.posonlyargs + function.args.args
    return bool(arguments) and arguments[0].arg in {"self", "cls"}


def _declared_static(function: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    names = {_decorator_root_name(item) for item in function.decorator_list}
    return bool(names & {"staticmethod", "classmethod"})


def _unbound_methods(source: str) -> list[str]:
    """Methods that take no instance and never said they were static."""
    offenders: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.ClassDef):
            offenders.extend(_unbound_class_methods(node))
    return offenders


def _unbound_class_methods(node: ast.ClassDef) -> list[str]:
    functions = [
        item
        for item in node.body
        if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
    ]
    return [
        f"{node.name}.{item.name}"
        for item in functions
        if not _binds_instance(item) and not _declared_static(item)
    ]


def test_the_unbound_method_detector_sees_a_lost_staticmethod() -> None:
    lost = "class A:\n    def f(value):\n        return value\n"
    assert _unbound_methods(lost) == ["A.f"]
    kept = "class A:\n    @staticmethod\n    def f(value):\n        return value\n"
    assert _unbound_methods(kept) == []
    assert _unbound_methods("class A:\n    def f(self):\n        return self\n") == []


def test_no_method_silently_lost_its_staticmethod_decorator() -> None:
    """Without the decorator the first argument becomes the instance, quietly."""
    offenders = {}
    for path in _python_sources():
        unbound = _unbound_methods(path.read_text(encoding="utf-8"))
        if unbound:
            offenders[str(path.relative_to(ROOT))] = unbound

    assert offenders == {}


def _decorator_name(node: ast.expr) -> str:
    """The bare name of a decorator, whether or not it is called or dotted."""
    target = node.func if isinstance(node, ast.Call) else node
    if isinstance(target, ast.Attribute):
        return target.attr
    return getattr(target, "id", "")


_GENERATOR_DECORATORS = frozenset({"contextmanager", "asynccontextmanager"})


def _yields(node: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    """Whether this function body itself yields, ignoring nested functions."""
    nested = tuple(
        child
        for child in ast.walk(node)
        if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef) and child is not node
    )
    inner = {id(descendant) for holder in nested for descendant in ast.walk(holder)}
    return any(
        isinstance(child, ast.Yield | ast.YieldFrom) and id(child) not in inner
        for child in ast.walk(node)
    )


def _context_managers_without_yield(source: str) -> list[str]:
    """Functions a `@contextmanager` decorates that can never enter a `with`."""
    offenders: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        names = {_decorator_name(item) for item in node.decorator_list}
        if names & _GENERATOR_DECORATORS and not _yields(node):
            offenders.append(node.name)
    return offenders


def test_a_context_manager_decorator_stayed_on_a_function_that_yields() -> None:
    """A patch that lands a definition under the decorator breaks every `with`."""
    misplaced = "@contextmanager\ndef f():\n    return 1\n"
    kept = "@contextmanager\ndef f():\n    yield 1\n"
    assert _context_managers_without_yield(misplaced) == ["f"]
    assert _context_managers_without_yield(kept) == []
    nested = "@contextmanager\ndef f():\n    def g():\n        yield 1\n    return g\n"
    assert _context_managers_without_yield(nested) == ["f"]

    offenders = {}
    for path in _python_sources():
        found = _context_managers_without_yield(path.read_text(encoding="utf-8"))
        if found:
            offenders[str(path.relative_to(ROOT))] = found

    assert offenders == {}

_VAULT_METADATA_FILES = ("knowledge/index.md", "knowledge/log.md")


def _tracked_note_paths() -> set[str]:
    """Every note this repository actually publishes."""
    result = subprocess.run(
        ["git", "ls-files", "-z", "knowledge/notes"],
        capture_output=True,
        check=True,
        cwd=ROOT,
    )
    return {path for path in result.stdout.decode("utf-8").split("\0") if path}


def _linked_note_paths(text: str) -> set[str]:
    """The note pages this file links to by path."""
    return {
        f"knowledge/notes/{name}.md"
        for name in re.findall(r"\[\[knowledge/notes/([^\]|]+)", text)
    }


def test_the_vault_index_and_log_name_only_published_notes() -> None:
    """A running vault rewrites these two files, and they are the only tracked
    knowledge files it writes. If one of them names a page this repository does
    not publish, the page is personal and the file must not be committed."""
    sample = "- [[knowledge/notes/private-thing]] — a page this repo does not ship.\n"
    assert _linked_note_paths(sample) == {"knowledge/notes/private-thing.md"}

    tracked = _tracked_note_paths()
    leaked = {}
    for name in _VAULT_METADATA_FILES:
        linked = _linked_note_paths((ROOT / name).read_text(encoding="utf-8"))
        unpublished = sorted(linked - tracked)
        if unpublished:
            leaked[name] = unpublished

    assert leaked == {}
