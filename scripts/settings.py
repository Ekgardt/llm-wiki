"""The one registry of limits an operator may set, and where each value came from.

Law 9: a limit that depends on operating conditions is stated with its basis and,
where it can be, set by the operator rather than hidden in the code. Every such
limit is declared here once, with its default, unit, lower bound and one line of
reason, and code reads it only through `setting_value`.

An operator overrides a default in an optional `llm-wiki.toml` at the vault root
(gitignored, absent by default) or, for one run, in `LLM_WIKI_<SECTION>_<KEY>`:

    [corpus]
    max_files = 20000

Precedence is default < file < environment, as in uv. An unknown section or key, a
value that is not an integer, or one below its bound stops the caller with the
key's name and its source; a limit never falls back silently. With no file and no
variable every value is its default, exactly the constant it replaced. See
`docs/research/2026-09-27-every-limit-states-its-reason.md`.
"""

from __future__ import annotations

import hashlib
import os
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover - exercised on the Python 3.10 CI job
    import tomli as tomllib

from bounded_io import read_stable_bytes

SETTINGS_FILE_NAME = "llm-wiki.toml"
ENVIRONMENT_PREFIX = "LLM_WIKI_"
# A file that sets every registered limit is well under 2 KiB; 64 KiB refuses a
# mistaken file (a log pasted in its place) before TOML parsing ever sees it.
MAX_SETTINGS_FILE_BYTES = 64 * 1024
DEFAULT_SOURCE = "default"
MIB = 1024 * 1024

# The vault-size ceilings (class e of docs/LIMITS-2026-09-27.md) share one reason:
# each pipeline holds its whole input in memory at once, so the ceiling is the memory
# the operator allows it. The values are the constants they replaced; none was
# measured, and each stops the pipeline with the setting to raise.
_HELD_IN_MEMORY = "the whole input is held in memory at once; the value predates measurement"
# Retention of disposable runtime evidence (class c): how much history to keep is a
# disk-against-hindsight choice the operator's machine decides, not the code. None of
# these files is authoritative; each is diagnostic or regenerable.
# One provider call that must return a whole plan (a compile draft, an episode batch).
# Measured: 2026-08-28 one call over 90 s, the pass 225 s, and the day compiled at 600 s;
# 2026-09-27 24 compile drafts through the claude CLI on a copy of the vault took 99 to
# 418 s each while test runs loaded the machine. 600 s is the largest observed call with
# about 1.4 times headroom and the value that compiled. Unbounded is not an option: a
# stuck provider would hold the compile lock and the nightly pass until the scheduler
# killed it, losing every step after. MEMORY_LLM_TIMEOUT_S still overrides every call.
_DRAFT_CEILING_REASON = (
    "a whole-plan provider call; 99-418 s measured under load, 600 s compiled "
    "(docs/research/2026-09-27-every-limit-states-its-reason.md)"
)
_RETRIEVAL_BUDGET_REASON = (
    "recall and get_decisions wait for the reranker; its warm p95 was 5.68 s on 4 idle "
    "cores, and 14 s leaves it a 6.25 s window (7.95 s under load: raise it there) "
    "(docs/research/2026-09-27-a-rerank-has-the-window-it-was-measured-to-need.md)"
)
_REPORT_REASON = (
    "maintenance reports and step output are diagnostics "
    "(docs/research/2026-09-25-a-report-link-outlives-no-report.md)"
)


@dataclass(frozen=True)
class Setting:
    """One tunable limit: its default, unit, lower bound and reason."""

    section: str
    key: str
    default: int
    unit: str
    reason: str
    lower: int = 1

    @property
    def name(self) -> str:
        return f"{self.section}.{self.key}"

    @property
    def environment_name(self) -> str:
        return f"{ENVIRONMENT_PREFIX}{self.section}_{self.key}".upper()


@dataclass(frozen=True)
class Effective:
    """A setting's value and where it came from: the default, the file or a variable."""

    value: int
    source: str


REGISTRY: tuple[Setting, ...] = (
    Setting("index", "max_pages", 2_000, "pages", "knowledge/index.md is rebuilt from every note; " + _HELD_IN_MEMORY),
    Setting("index", "max_total_bytes", 32 * MIB, "bytes", "the notes the index is rebuilt from; " + _HELD_IN_MEMORY),
    Setting("compile", "max_sources", 2_000, "sources", "daily logs and notes one compile reads; " + _HELD_IN_MEMORY),
    Setting("compile", "max_total_source_bytes", 32 * MIB, "bytes", "the sources one compile reads; " + _HELD_IN_MEMORY),
    Setting("corpus", "max_files", 10_000, "files", "the corpus a search generation is built from; " + _HELD_IN_MEMORY),
    Setting("corpus", "max_total_bytes", 64 * MIB, "bytes", "the corpus a search generation is built from; " + _HELD_IN_MEMORY),
    Setting("claims", "max_pages", 10_000, "pages", "notes and journals the claim tree hashes; " + _HELD_IN_MEMORY),
    Setting("claims", "max_total_bytes", 32 * MIB, "bytes", "the pages the claim tree hashes; " + _HELD_IN_MEMORY),
    Setting("extraction", "max_sources", 10_000, "sources", "sources one knowledge extraction reads; " + _HELD_IN_MEMORY),
    Setting("search", "max_pages", 10_000, "pages", "notes a search without a generation walks; " + _HELD_IN_MEMORY),
    Setting("impact", "max_note_files", 2_000, "files", "notes one impact request scans; " + _HELD_IN_MEMORY),
    Setting("impact", "max_total_note_bytes", 32 * MIB, "bytes", "the notes one impact request scans; " + _HELD_IN_MEMORY),
    Setting("retention", "report_days", 30, "days", _REPORT_REASON),
    Setting("retention", "report_files", 60, "files", _REPORT_REASON + "; two nightly reports a day for 30 days"),
    Setting("retention", "report_bytes", 32 * MIB, "bytes", _REPORT_REASON + "; the only bound on the scheduler's own log"),
    Setting("retention", "telemetry_days", 90, "days", "retrieval telemetry is kept for the archive's 90 hot days"),
    Setting("retention", "benchmark_run_days", 30, "days", "a benchmark run directory is evidence for the report written from it"),
    Setting("retention", "config_backup_days", 90, "days", "agent-config backups undo an installer rewrite; the archive's 90 hot days"),
    Setting("provider", "draft_ceiling_seconds", 600, "seconds", _DRAFT_CEILING_REASON),
    Setting("mcp", "retrieval_seconds", 14, "seconds", _RETRIEVAL_BUDGET_REASON, lower=5),
    Setting("mcp", "doctor_seconds", 10, "seconds", "full current health and runtime validation; tune to measured ledger/artifact size, not just model latency", lower=2),
    Setting("mcp", "doctor_return_seconds", 1, "seconds", "return the health report before the caller deadline; a bounded scan returned 0.239s past its check budget on 2026-10-01"),
)
_BY_NAME = {setting.name: setting for setting in REGISTRY}
_SECTIONS = frozenset(setting.section for setting in REGISTRY)

# Parsed files by the SHA-256 of their bytes. Keyed by (modification time, size) the
# cache returned the old value when a file was rewritten with the same length inside
# one tick of the filesystem clock (a test caught it under load, 2026-09-27); the bytes
# are at most 64 KiB, so reading them per call costs microseconds and parsing stays
# once per content. Only the latest content is kept: a long-lived process that sees the
# operator edit the file many times holds one parse, not one per edit.
_PARSED: dict[str, dict[str, int]] = {}


class SettingsError(ValueError):
    """A settings file or variable names an unknown key or an invalid value."""


def _default_root() -> Path:
    from memory_state import ROOT

    return ROOT


def settings_path(root: Path | None = None) -> Path:
    return Path(_default_root() if root is None else root) / SETTINGS_FILE_NAME


def raise_hint(name: str) -> str:
    """The words a refusal adds so the operator knows which setting lifts it."""
    setting = _BY_NAME[name]
    return f"raise {setting.name} in {SETTINGS_FILE_NAME} or {setting.environment_name}"


def clear_cache() -> None:
    _PARSED.clear()


def _require_value(setting: Setting, raw: object, source: str) -> int:
    """An integer (not a bool) at or above the setting's lower bound."""
    valid_type = isinstance(raw, int) and not isinstance(raw, bool)
    if not valid_type or raw < setting.lower:
        raise SettingsError(
            f"{source}: {setting.name} must be an integer of at least {setting.lower} {setting.unit}"
        )
    return raw


def _section_values(section: str, table: object, source: str) -> dict[str, int]:
    if section not in _SECTIONS or not isinstance(table, Mapping):
        raise SettingsError(f"{source}: unknown section [{section}]")
    values: dict[str, int] = {}
    for key, raw in table.items():
        setting = _BY_NAME.get(f"{section}.{key}")
        if setting is None:
            raise SettingsError(f"{source}: unknown setting {section}.{key}")
        values[setting.name] = _require_value(setting, raw, source)
    return values


def _parsed_bytes(raw: bytes, name: str) -> dict[str, int]:
    try:
        document = tomllib.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as error:
        raise SettingsError(f"{name}: not valid TOML: {error}") from error
    values: dict[str, int] = {}
    for section, table in document.items():
        values.update(_section_values(section, table, name))
    return values


def _file_values(path: Path) -> dict[str, int]:
    """The file's values, parsed once per distinct content; none when it is absent."""
    try:
        raw = read_stable_bytes(path, MAX_SETTINGS_FILE_BYTES, label="settings file")
    except FileNotFoundError:
        return {}
    digest = hashlib.sha256(raw).hexdigest()
    if digest not in _PARSED:
        parsed = _parsed_bytes(raw, path.name)
        _PARSED.clear()
        _PARSED[digest] = parsed
    return _PARSED[digest]


def _environment_value(setting: Setting, environ: Mapping[str, str]) -> int | None:
    raw = environ.get(setting.environment_name)
    if raw is None:
        return None
    try:
        parsed = int(raw.strip())
    except ValueError as error:
        raise SettingsError(f"{setting.environment_name}: {raw!r} is not an integer") from error
    return _require_value(setting, parsed, setting.environment_name)


def _effective(setting: Setting, from_file: Mapping[str, int], environ: Mapping[str, str]) -> Effective:
    from_environment = _environment_value(setting, environ)
    if from_environment is not None:
        return Effective(from_environment, setting.environment_name)
    if setting.name in from_file:
        return Effective(from_file[setting.name], SETTINGS_FILE_NAME)
    return Effective(setting.default, DEFAULT_SOURCE)


def effective(root: Path | None = None, environ: Mapping[str, str] | None = None) -> dict[str, Effective]:
    """Every registered setting with its value and source; raises on an invalid override."""
    from_file = _file_values(settings_path(root))
    environment = os.environ if environ is None else environ
    return {setting.name: _effective(setting, from_file, environment) for setting in REGISTRY}


def setting_value(name: str, root: Path | None = None, environ: Mapping[str, str] | None = None) -> int:
    """The value of one registered setting for the vault at `root` (the installed vault by default)."""
    setting = _BY_NAME[name]
    from_file = _file_values(settings_path(root))
    environment = os.environ if environ is None else environ
    return _effective(setting, from_file, environment).value
