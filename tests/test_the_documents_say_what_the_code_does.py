"""The public documents describe the code as it is, not as it was.

On 2026-09-24 the three READMEs told a reader to keep the retired legacy index,
three documents said the system makes no automatic Git operation although the
nightly fast-forwards, and two showed a `--slug` flag that does not exist. See
docs/research/2026-09-24-the-documents-say-what-the-code-does.md.
"""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
CURRENT_DOCUMENTS = (
    "README.md",
    "README.ru.md",
    "README.zh-CN.md",
    "docs/USER-GUIDE.md",
    "docs/ARCHITECTURE.md",
    "docs/STRUCTURE.md",
    "docs/MODULES.md",
    "docs/contracts/compile-and-nightly.md",
    "docs/contracts/corpus-and-navigation.md",
    "docs/contracts/integration-and-recovery.md",
    "docs/contracts/reliability-and-capture.md",
    "docs/operating-model.md",
)
STALE_CLAIMS = (
    re.compile(r"keep legacy `cache/index\.sqlite`", re.IGNORECASE),
    re.compile(r"falls back to the legacy BM25 index", re.IGNORECASE),
    re.compile(r"legacy FTS/vector/Lance paths", re.IGNORECASE),
    re.compile(r"performs no automatic Git operation", re.IGNORECASE),
    re.compile(r"build_context\.py --slug"),
    re.compile(r"removed during implementation"),
    # The nightly also commits the knowledge snapshot (audit 2026-09-27 C-17).
    re.compile(r"one automatic Git operation", re.IGNORECASE),
    # The v3 layout is adopted by the installers, not a target (audit 2026-09-27 C-17).
    re.compile(r"proposed target"),
)


@pytest.mark.parametrize("document", CURRENT_DOCUMENTS)
def test_no_current_document_repeats_a_retired_claim(document: str) -> None:
    text = (ROOT / document).read_text(encoding="utf-8")

    assert [pattern.pattern for pattern in STALE_CLAIMS if pattern.search(text)] == []


@pytest.mark.parametrize(
    "document",
    ["docs/DEVELOPER-AUDIT-STATUS-2026-08-14.md", "docs/DEVELOPER-AUDIT-STATUS-2026-08-18.md", "docs/ROADMAP-v5.md"],
)
def test_a_superseded_status_document_says_it_is_history(document: str) -> None:
    head = (ROOT / document).read_text(encoding="utf-8")[:600]

    assert "docs/AUDIT-2026-09-24-live.md" in head


def test_the_contract_names_when_a_server_earns_its_retries_back() -> None:
    """CLAUDE.md said three retries for good; a healthy run restores them (audit 2026-09-27 C-17)."""
    from llm_wiki.pyright_session_startup import HEALTHY_RUN_SECONDS

    contract = (ROOT / "CLAUDE.md").read_text(encoding="utf-8")

    assert f"ran {int(HEALTHY_RUN_SECONDS)} s before failing" in contract


SCRIPTS = ROOT / "scripts"
# A call such as `doctor.run_generation_maintenance()`; a bare `x.py` or `doctor.nightly_seconds`
# names a file or a setting.
CODE_REFERENCE = re.compile(r"`([a-z_][a-z0-9_]*)\.([A-Za-z_][A-Za-z0-9_]*)\(\)`")
REFERENCE_DOCUMENTS = (*CURRENT_DOCUMENTS, "CLAUDE.md", "AGENTS.md")


def _script_module(name: str) -> Path | None:
    candidates = (SCRIPTS / f"{name}.py", SCRIPTS / "llm_wiki" / f"{name}.py")
    return next((path for path in candidates if path.is_file()), None)


def _bound_names(module: Path) -> set[str]:
    """Names a module binds at its top level: definitions, assignments and imports."""
    names: set[str] = set()
    for node in ast.parse(module.read_text(encoding="utf-8")).body:
        names.update(_names_bound_by(node))
    return names


DEFINITIONS = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
IMPORTS = (ast.Import, ast.ImportFrom)


def _names_bound_by(node: ast.stmt) -> set[str]:
    if isinstance(node, DEFINITIONS):
        return {node.name}
    if isinstance(node, IMPORTS):
        return {_imported_name(alias) for alias in node.names}
    return _assigned_names(node)


def _imported_name(alias: ast.alias) -> str:
    return (alias.asname or alias.name).split(".")[0]


def _assigned_names(node: ast.stmt) -> set[str]:
    targets = node.targets if isinstance(node, ast.Assign) else [getattr(node, "target", None)]
    return {target.id for target in targets if isinstance(target, ast.Name)}


def _stale_code_references(document: str) -> list[str]:
    text = (ROOT / document).read_text(encoding="utf-8")
    stale = []
    for module_name, name in CODE_REFERENCE.findall(text):
        module = _script_module(module_name)
        if module is not None and name not in _bound_names(module):
            stale.append(f"{module_name}.{name}")
    return stale


@pytest.mark.parametrize("document", REFERENCE_DOCUMENTS)
def test_a_named_function_still_lives_in_the_named_module(document: str) -> None:
    """The corpus contract sent readers to `doctor.run_generation_maintenance()` after it moved."""
    assert _stale_code_references(document) == []


def test_a_moved_function_is_reported_as_stale(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(sys.modules[__name__], "ROOT", tmp_path)
    (tmp_path / "contract.md").write_text("Use `doctor.run_generation_maintenance()`.", encoding="utf-8")

    assert _stale_code_references("contract.md") == ["doctor.run_generation_maintenance"]
