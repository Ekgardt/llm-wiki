"""Every limit states its reason; the ones an operator may change are settings (law 9).

See `docs/research/2026-09-27-every-limit-states-its-reason.md`.
"""

from __future__ import annotations

import ast
import os
import re
from pathlib import Path

import doctor
import pytest
import rebuild_memory_index
import settings
from corpus_snapshot import collect_corpus

ROOT = Path(__file__).resolve().parents[1]
# Registry defaults. Corpus/extraction share the existing discovery entry budget.
REPLACED = {
    "index.max_pages": 2_000,
    "index.max_total_bytes": 32 * 1024 * 1024,
    "compile.max_sources": 2_000,
    "compile.max_total_source_bytes": 32 * 1024 * 1024,
    "corpus.max_files": 50_000,
    "corpus.max_total_bytes": 64 * 1024 * 1024,
    "claims.max_pages": 10_000,
    "claims.max_total_bytes": 32 * 1024 * 1024,
    "extraction.max_sources": 50_000,
    "search.max_pages": 10_000,
    "impact.max_note_files": 2_000,
    "impact.max_total_note_bytes": 32 * 1024 * 1024,
    "retention.report_days": 30,
    "retention.report_files": 60,
    "retention.report_bytes": 32 * 1024 * 1024,
    "retention.telemetry_days": 90,
    "retention.benchmark_run_days": 30,
    "retention.config_backup_days": 90,
    # Was COMPILE_PROVIDER_CEILING_S = 300; raised to 600 on the 2026-09-27 measurements.
    "generation.nightly_seconds": 2131,
    "generation.post_compile_seconds": 2131,
    "provider.draft_ceiling_seconds": 600,
    # Not a replaced constant: recall and get_decisions had MCP_OPERATION_SECONDS (10 s);
    # 14 s is the budget measured for the reranker (B-9).
    "mcp.retrieval_seconds": 14,
}
RETIRED_NAMES = (
    "MAX_PAGE_COUNT",
    "MAX_TOTAL_PAGE_BYTES",
    "MAX_SOURCE_COUNT",
    "MAX_TOTAL_SOURCE_BYTES",
    "MAX_CORPUS_FILES",
    "MAX_CORPUS_TOTAL_BYTES",
    "MAX_CLAIM_TREE_PAGES",
    "MAX_CLAIM_TREE_TOTAL_BYTES",
    "MAX_SEARCHABLE_PAGES",
    "DEFAULT_GENERATION_SOURCE_LIMIT",
    "REPORT_RETENTION_DAYS",
    "REPORT_RETENTION_FILES",
    "REPORT_RETENTION_BYTES",
    "ARTIFACT_RETENTION_FILES",
    "RUN_RETENTION_DAYS",
    "MAX_BACKUP_AGE_SECONDS",
    "LOCK_STALE_SECONDS",
    "COMPILE_PROVIDER_CEILING_S",
    "CONSOLIDATION_PROVIDER_CEILING_S",
)
# A limit by its name, as docs/LIMITS-2026-09-27.md inventories them.
LIMIT_NAME = re.compile(
    r"(^|_)(MAX|MIN|LIMIT|TIMEOUT|DEADLINE|BUDGET|CAP|CEILING|WINDOW|THRESHOLD|RETRIES|ATTEMPTS|"
    r"RETENTION|TTL|POLL|INTERVAL|DELAY|WAIT|DEPTH|BATCH|TOP|QUOTA|FLOOR|GRACE|STALE|LEASE|HEARTBEAT)(_|$)"
    r"|_(SECONDS|BYTES|DAYS|HOURS|MINUTES|MS|COUNT|ROWS|CHARS|TOKENS|SIZE|ENTRIES|LINES|PAGES|FILES|"
    r"ITEMS|RECORDS|LIMIT|RESULTS|CANDIDATES|DEPTH|WORKERS)$"
)
_NUMBER_NODES = (ast.Constant, ast.UnaryOp, ast.BinOp)


def _scripts() -> str:
    return "\n".join(path.read_text(encoding="utf-8") for path in (ROOT / "scripts").glob("*.py"))


def _vault(tmp_path: Path, notes: int, toml: str | None = None) -> Path:
    directory = tmp_path / "knowledge" / "notes"
    directory.mkdir(parents=True)
    for index in range(notes):
        (directory / f"note-{index}.md").write_text(f"---\ntype: concept\n---\n# Note {index}\n", encoding="utf-8")
    if toml is not None:
        (tmp_path / settings.SETTINGS_FILE_NAME).write_text(toml, encoding="utf-8")
    return tmp_path


def test_without_a_file_every_ceiling_has_its_declared_default(tmp_path: Path) -> None:
    values = settings.effective(tmp_path, environ={})
    assert {name: item.value for name, item in values.items()} == REPLACED
    assert {item.source for item in values.values()} == {settings.DEFAULT_SOURCE}
    assert not (tmp_path / settings.SETTINGS_FILE_NAME).exists()


@pytest.mark.parametrize("setting", settings.REGISTRY, ids=lambda setting: setting.name)
def test_every_setting_states_unit_and_reason_and_is_read_by_the_code(setting: settings.Setting) -> None:
    assert (bool(setting.unit), len(setting.reason) > 20, setting.default >= setting.lower) == (True, True, True)
    assert f'setting_value("{setting.name}"' in _scripts()


def test_the_retired_constants_are_gone() -> None:
    sources = _scripts()
    assert [name for name in RETIRED_NAMES if re.search(rf"\b{name}\b", sources)] == []


def test_no_setting_variable_collides_with_a_variable_the_product_already_reads() -> None:
    others = set(re.findall(r'"(LLM_WIKI_[A-Z_]+)"', _scripts()))
    assert {setting.environment_name for setting in settings.REGISTRY} & others == set()


def test_the_file_raises_a_ceiling_and_the_refusal_names_the_setting(tmp_path: Path) -> None:
    vault = _vault(tmp_path, notes=2, toml="[index]\nmax_pages = 1\n")
    with pytest.raises(ValueError, match=r"index\.max_pages in llm-wiki\.toml or LLM_WIKI_INDEX_MAX_PAGES"):
        rebuild_memory_index.build_index_bytes(vault)
    (vault / settings.SETTINGS_FILE_NAME).write_text("[index]\nmax_pages = 3\n", encoding="utf-8")
    assert rebuild_memory_index.build_index_bytes(vault).startswith(b"# ")


def test_the_corpus_reads_its_ceiling_from_the_vault(tmp_path: Path) -> None:
    vault = _vault(tmp_path, notes=2, toml="[corpus]\nmax_files = 1\n")
    with pytest.raises(ValueError, match=r"corpus\.max_files"):
        collect_corpus(vault)


def test_a_variable_beats_the_file(tmp_path: Path) -> None:
    _vault(tmp_path, notes=0, toml="[corpus]\nmax_files = 5\n")
    environ = {"LLM_WIKI_CORPUS_MAX_FILES": "7"}
    assert settings.setting_value("corpus.max_files", tmp_path, environ=environ) == 7
    assert settings.effective(tmp_path, environ=environ)["corpus.max_files"].source == "LLM_WIKI_CORPUS_MAX_FILES"
    assert settings.effective(tmp_path, environ={})["corpus.max_files"] == settings.Effective(5, "llm-wiki.toml")


@pytest.mark.parametrize(
    ("toml", "named"),
    [
        ("[index]\nmax_page = 5\n", "index.max_page"),
        ("[indexes]\nmax_pages = 5\n", "[indexes]"),
        ("[index]\nmax_pages = true\n", "index.max_pages"),
        ("[index]\nmax_pages = 0\n", "index.max_pages"),
        ("[index]\nmax_pages = '5'\n", "index.max_pages"),
        ("[index\n", "not valid TOML"),
    ],
)
def test_an_invalid_file_stops_with_the_key_it_names(tmp_path: Path, toml: str, named: str) -> None:
    _vault(tmp_path, notes=0, toml=toml)
    with pytest.raises(settings.SettingsError, match=re.escape(named)):
        settings.effective(tmp_path, environ={})


def test_an_invalid_variable_stops_with_its_name(tmp_path: Path) -> None:
    with pytest.raises(settings.SettingsError, match="LLM_WIKI_SEARCH_MAX_PAGES"):
        settings.setting_value("search.max_pages", tmp_path, environ={"LLM_WIKI_SEARCH_MAX_PAGES": "many"})


def test_doctor_names_overrides_and_their_source(tmp_path: Path) -> None:
    _vault(tmp_path, notes=1, toml="[search]\nmax_pages = 20000\n")
    check = doctor._settings_check(tmp_path)
    assert check["status"] == "ok"
    assert check["details"]["overridden"] == {"search.max_pages": {"value": 20000, "source": "llm-wiki.toml"}}


def test_doctor_warns_before_a_ceiling_stops_the_pipeline(tmp_path: Path) -> None:
    _vault(tmp_path, notes=4, toml="[index]\nmax_pages = 5\n")
    check = doctor._settings_check(tmp_path)
    assert check["status"] == "degraded"
    assert check["details"]["near_ceiling"]["index.max_pages"] == {"used": 4, "ceiling": 5, "share": 0.8}


def test_doctor_reports_an_invalid_file_as_an_error(tmp_path: Path) -> None:
    _vault(tmp_path, notes=0, toml="[index]\nmax_pages = -1\n")
    check = doctor._settings_check(tmp_path)
    assert check["status"] == "error"
    assert "index.max_pages" in check["message"]


def _is_number(node: ast.AST) -> bool:
    value = getattr(node, "value", None)
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _numeric(node: ast.AST) -> bool:
    """A number, or arithmetic over numbers (`32 * 1024 * 1024`)."""
    if isinstance(node, ast.Constant):
        return _is_number(node)
    children = [child for child in ast.iter_child_nodes(node) if isinstance(child, ast.expr)]
    return isinstance(node, _NUMBER_NODES) and all(map(_numeric, children))


def _target(node: ast.stmt) -> ast.AST | None:
    """The one target of `NAME = value` or `NAME: T = value`."""
    targets = getattr(node, "targets", None)
    if targets is None:
        return getattr(node, "target", None)
    single = len(targets) == 1
    return targets[0] if single else None


def _assigned(node: ast.stmt) -> tuple[str, ast.expr] | None:
    """The name and value of a module-level assignment to one name, or None."""
    target, value = _target(node), getattr(node, "value", None)
    if not isinstance(target, ast.Name) or value is None:
        return None
    return target.id, value


def _is_limit(node: ast.stmt) -> bool:
    pair = _assigned(node)
    return pair is not None and bool(LIMIT_NAME.search(pair[0].lstrip("_"))) and _numeric(pair[1])


_PRAGMA = re.compile(r"#\s*(noqa|type:\s*ignore|pragma|pyright:|fmt:)", re.IGNORECASE)


def _words_in_comment(line: str) -> bool:
    """A comment that says something: not a lint pragma, not a rule of dashes."""
    comment = line[line.index("#") :] if "#" in line else ""
    return bool(comment) and not _PRAGMA.search(comment) and bool(re.search(r"[A-Za-z]{3}", comment))


def _own_block(lines: list[str], lineno: int) -> list[str]:
    """The definition's line and the unbroken run of lines directly above it."""
    start = lineno - 1
    while start > 0 and lines[start - 1].strip():
        start -= 1
    return lines[start:lineno]


def _commented(lines: list[str], lineno: int) -> bool:
    """A worded comment in the definition's own block, not one borrowed across a blank line.

    Counting any `#` within four lines let an import's `# noqa: E402`, a section
    rule, or the comment of the constant above stand in for a basis.
    """
    return any(_words_in_comment(line) for line in _own_block(lines, lineno))


def _bare_limits(path: Path) -> set[str]:
    lines = path.read_text(encoding="utf-8").splitlines()
    tree = ast.parse("\n".join(lines))
    bare = [node for node in tree.body if _is_limit(node) and not _commented(lines, node.lineno)]
    return {f"{path.relative_to(ROOT).as_posix()}::{_assigned(node)[0]}" for node in bare}


def test_every_limit_is_a_setting_or_states_its_basis() -> None:
    """Every module-level limit in scripts/ has a worded basis in its own block or is a setting."""
    bare = set().union(*(_bare_limits(path) for path in sorted((ROOT / "scripts").rglob("*.py"))))
    assert bare == set()


def test_the_limit_scanner_sees_a_bare_constant() -> None:
    lines = ["import os", "", "MAX_WIDGETS = 10", "# Basis: the widget protocol allows ten.", "RETRY_SECONDS = 2.0"]
    tree = ast.parse("\n".join(lines))
    found = [node for node in tree.body if _is_limit(node) and not _commented(lines, node.lineno)]
    assert [_assigned(node)[0] for node in found] == ["MAX_WIDGETS"]


def test_a_pragma_a_rule_or_a_neighbour_is_not_a_basis() -> None:
    """The four-line window used to accept any `#`: an import's noqa, a dashed rule, the comment above a blank line."""
    lines = [
        "import os  # noqa: E402",
        "MAX_AFTER_IMPORT = 1",
        "",
        "# ------------------------------------------------",
        "MAX_UNDER_RULE = 2",
        "",
        "# Basis: the widget protocol allows three.",
        "",
        "MAX_ACROSS_BLANK = 3",
        "# Basis: both follow the same widget protocol.",
        "MAX_GROUPED = 4",
        "MAX_GROUPED_TOO = 5",
    ]
    tree = ast.parse("\n".join(lines))
    found = [node for node in tree.body if _is_limit(node) and not _commented(lines, node.lineno)]
    assert [_assigned(node)[0] for node in found] == ["MAX_AFTER_IMPORT", "MAX_UNDER_RULE", "MAX_ACROSS_BLANK"]



def test_a_rewrite_with_the_same_length_and_time_is_read_again(tmp_path: Path) -> None:
    """Keyed by size and modification time, the cache returned the old value."""
    path = tmp_path / settings.SETTINGS_FILE_NAME
    path.write_text("[index]\nmax_pages = 1\n", encoding="utf-8")
    stamp = path.stat().st_mtime_ns
    assert settings.setting_value("index.max_pages", tmp_path, environ={}) == 1
    path.write_text("[index]\nmax_pages = 3\n", encoding="utf-8")
    os.utime(path, ns=(stamp, stamp))
    assert settings.setting_value("index.max_pages", tmp_path, environ={}) == 3
