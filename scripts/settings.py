"""The vault settings reader and registry of limits with their declared basis.

Law 9: a limit that depends on operating conditions is stated with its basis and,
where it can be, set by the operator rather than hidden in the code. Every such
limit is declared here once, with its default, unit, lower bound and one line of
reason, and code reads it only through `setting_value`.

An operator overrides a default in an optional `llm-wiki.toml` at the vault root
(gitignored, absent by default) or, for one run, in `LLM_WIKI_<SECTION>_<KEY>`:

    [corpus]
    max_files = 20000

For limits, precedence is default < file < environment, as in uv.
The separate [llm] section is validated by model_selection and is authoritative
for model selection; it is not a numeric limit or an environment projection. An unknown section or key, a
value that is not an integer, or one below its bound stops the caller with the
key's name and its source; a limit never falls back silently. With no file and no
variable every value is its registry default. Compile/index aggregate quotas accept
zero to disable the quota; positive values remain explicit operator policy. See
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


# One existing discovery entry budget also bounds the default number of sources.
# Retained from corpus_snapshot: no additional numeric value is selected here.
# Its independent numerical basis remains under review; source defaults must not
# introduce a smaller second ceiling than this bounded walk already enforces.
MAX_CORPUS_INSPECTED_ENTRIES = 50_000


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
    Setting("provider", "catalog_max_bytes", MIB, "bytes",
            "provider metadata admission budget: 561046-byte Codex catalog parsed at "
            "2182523-byte peak on 2026-10-19; roughly twice current catalog input; "
            "raise after measuring parser memory when catalogs grow; not an entitlement limit"),
    Setting("index", "max_pages", 0, "pages",
            "optional operator quota on index source volume; 0 disables it; rendering "
            "streams bodies and retains navigation metadata; review positive quotas "
            "against measured work volume and metadata memory", lower=0),
    Setting("index", "max_total_bytes", 0, "bytes",
            "optional operator quota on index input bytes; 0 disables it; rendering "
            "streams bodies; review positive quotas against measured I/O and latency", lower=0),
    Setting("compile", "max_sources", 0, "sources",
            "optional operator quota on total admitted work; 0 disables it; streamed "
            "bodies do not justify an aggregate memory ceiling; choose/review a positive "
            "quota against measured work volume, disk and full-cycle cost", lower=0),
    Setting("compile", "max_total_source_bytes", 0, "bytes",
            "optional operator quota on total admitted source bytes; 0 disables it; "
            "streamed bodies do not justify an aggregate memory ceiling; choose/review "
            "a positive quota against disk capacity and full-cycle cost", lower=0),
    Setting("compile", "validation_retries", 2, "correcting passes per provider",
            "cost/quality choice: initial attempt plus format and semantic corrections; "
            "not a measured optimum; reassess complete-cycle facts, tokens and latency "
            "after model/prompt/workload changes (approved 2026-10-18)", lower=0),
    Setting("corpus", "max_files", MAX_CORPUS_INSPECTED_ENTRIES, "files", "shares the existing corpus discovery entry budget; " + _HELD_IN_MEMORY),
    Setting("corpus", "max_total_bytes", 64 * MIB, "bytes", "the corpus a search generation is built from; " + _HELD_IN_MEMORY),
    Setting("claims", "max_pages", 10_000, "pages", "notes and journals the claim tree hashes; " + _HELD_IN_MEMORY),
    Setting("claims", "max_total_bytes", 32 * MIB, "bytes", "the pages the claim tree hashes; " + _HELD_IN_MEMORY),
    Setting("extraction", "max_sources", MAX_CORPUS_INSPECTED_ENTRIES, "sources", "shares the existing corpus discovery entry budget; " + _HELD_IN_MEMORY),
    Setting("search", "max_pages", 10_000, "pages", "notes a search without a generation walks; " + _HELD_IN_MEMORY),
    Setting("impact", "max_note_files", 2_000, "files", "notes one impact request scans; " + _HELD_IN_MEMORY),
    Setting("impact", "max_total_note_bytes", 32 * MIB, "bytes", "the notes one impact request scans; " + _HELD_IN_MEMORY),
    Setting("retention", "report_days", 30, "days", _REPORT_REASON),
    Setting("retention", "report_files", 60, "files", _REPORT_REASON + "; two nightly reports a day for 30 days"),
    Setting("retention", "report_bytes", 32 * MIB, "bytes", _REPORT_REASON + "; the only bound on the scheduler's own log"),
    Setting("retention", "telemetry_days", 90, "days", "retrieval telemetry is kept for the archive's 90 hot days"),
    Setting("retention", "benchmark_run_days", 30, "days", "a benchmark run directory is evidence for the report written from it"),
    Setting("retention", "config_backup_days", 90, "days", "agent-config backups and displaced owned files undo an installer rewrite; the archive's 90 hot days"),
    Setting("generation", "nightly_seconds", 2131, "seconds", "107490-source measured full build exceeded 1812 s; plus 53 s validation scaled by corpus growth and 260 s collection variation; remeasure after growth (2026-10-08)"),
    Setting("generation", "post_compile_seconds", 2131, "seconds", "same complete generation builder and measured corpus as nightly; a 120 s window could not finish its 260 s collection (2026-10-08)"),
    Setting("doctor", "nightly_seconds", 60, "seconds", "legacy bounded nightly inspection profile; insufficient for large retained history (507-1618 s measured 2026-10-17); configure and requalify against full checks, load and scheduler cost"),
    Setting("provider", "draft_ceiling_seconds", 600, "seconds", _DRAFT_CEILING_REASON),
    Setting("mcp", "retrieval_seconds", 14, "seconds", _RETRIEVAL_BUDGET_REASON, lower=5),
)
_BY_NAME = {setting.name: setting for setting in REGISTRY}
_SECTIONS = frozenset(setting.section for setting in REGISTRY)

# Parsed files by the SHA-256 of their bytes. Keyed by (modification time, size) the
# cache returned the old value when a file was rewritten with the same length inside
# one tick of the filesystem clock (a test caught it under load, 2026-09-27); the bytes
# are at most 64 KiB, so reading them per call costs microseconds and parsing stays
# once per content. Only the latest content is kept: a long-lived process that sees the
# operator edit the file many times holds one parse, not one per edit.
_PARSED: dict[str, dict[str, int | str]] = {}


class SettingsError(ValueError):
    """A settings file or variable names an unknown key or an invalid value."""


def _default_root() -> Path:
    from memory_state import ROOT
    from model_selection import settings_root

    return settings_root(ROOT)


def settings_path(root: Path | None = None) -> Path:
    return Path(_default_root() if root is None else root) / SETTINGS_FILE_NAME


def raise_hint(name: str) -> str:
    """Name the controls to review, without treating an increase as qualified."""
    setting = _BY_NAME[name]
    return (
        f"measure resource capacity before changing {setting.name} "
        f"in {SETTINGS_FILE_NAME} or {setting.environment_name}"
    )


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


def _section_values(section: str, table: object, source: str) -> dict[str, int | str]:
    if section == "llm":
        from model_selection import table_values

        return table_values(table, source)
    return _limit_section_values(section, table, source)


def _limit_section_values(section: str, table: object, source: str) -> dict[str, int]:
    if section not in _SECTIONS or not isinstance(table, Mapping):
        raise SettingsError(f"{source}: unknown section [{section}]")
    values: dict[str, int] = {}
    for key, raw in table.items():
        setting = _BY_NAME.get(f"{section}.{key}")
        if setting is None:
            raise SettingsError(f"{source}: unknown setting {section}.{key}")
        values[setting.name] = _require_value(setting, raw, source)
    return values


def _parsed_bytes(raw: bytes, name: str) -> dict[str, int | str]:
    try:
        document = tomllib.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as error:
        raise SettingsError(f"{name}: not valid TOML: {error}") from error
    values: dict[str, int | str] = {}
    for section, table in document.items():
        values.update(_section_values(section, table, name))
    return values


def _file_values(path: Path) -> dict[str, int | str]:
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
