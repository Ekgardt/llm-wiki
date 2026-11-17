"""Deterministic selective-forgetting metrics; no process, clock or file IO."""
from __future__ import annotations


def _rate(hits: int, total: int) -> float | None:
    return round(hits / total, 4) if total else None

def _counted(targets: list[dict], key: str) -> int:
    return sum(1 for item in targets if item.get(key))

def _usable(targets: list[dict]) -> list[dict]:
    """Only a probe that could be asked can have missed; the rest is unmeasured."""
    return [item for item in targets if item.get("probe_usable")]

def cohort_summary(targets: list[dict]) -> dict:
    """How a cohort answered: how many probes worked, how many surfaced."""
    usable = len(_usable(targets))
    surfaced = _counted(_usable(targets), "surfaced")
    visible = _counted(_usable(targets), "visible")
    return {
        "n": len(targets),
        "probes_usable": usable,
        "surfaced": surfaced,
        "in_corpus": _counted(targets, "in_corpus"),
        "surfaced_rate": _rate(surfaced, usable),
        # Reported, never gated: how many of them keep a place in the first ten
        # rows. Archiving 59 pages reorders the edge of a narrow window, and a
        # page one place past it is not a page forgotten — see
        # docs/research/2026-09-13-one-argument-one-slot.md.
        "visible": visible,
        "visible_rate": _rate(visible, usable),
    }

def _supersession_metrics(arms: dict) -> dict:
    control = arms.get("control", {})
    treatment = arms.get("treatment", {})
    forgotten = cohort_summary(treatment.get("forget_targets", []))
    proven = cohort_summary(control.get("forget_targets", []))
    retained = cohort_summary(treatment.get("retain_targets", []))
    return {
        "control_forget_cohort": proven,
        "treatment_forget_cohort": forgotten,
        "treatment_retain_cohort": retained,
        "forget_rate": _rate(proven["surfaced"] - forgotten["surfaced"], proven["surfaced"]),
        "retain_rate": retained["surfaced_rate"],
    }

def _paired(report: dict, cohort: str) -> tuple[dict, dict]:
    key = f"{cohort}_targets"
    before = cohort_summary(report.get("before", {}).get(key, []))
    after = cohort_summary(report.get("after", {}).get(key, []))
    return before, after

def _ageing_metrics(report: dict) -> dict:
    proven, forgotten = _paired(report, "forget")
    retained_before, retained_after = _paired(report, "retain")
    read_before, read_after = _paired(report, "reprieve")
    return {
        "before_forget_cohort": proven,
        "after_forget_cohort": forgotten,
        "before_retain_cohort": retained_before,
        "after_retain_cohort": retained_after,
        "before_reprieve_cohort": read_before,
        "after_reprieve_cohort": read_after,
        "forget_rate": _rate(proven["surfaced"] - forgotten["surfaced"], proven["surfaced"]),
        "retain_rate": _rate(retained_after["surfaced"], retained_before["surfaced"]),
        "reprieve_rate": _rate(read_after["surfaced"], read_before["surfaced"]),
        "archived_files": report.get("still_on_disk", {}),
    }

def _restore_metrics(report: dict) -> dict:
    results = report.get("results", [])
    synthetic = report.get("synthetic", [])
    return {
        "live_pages": len(results),
        "live_byte_identical": report.get("byte_identical", 0),
        "live_fidelity": _rate(report.get("byte_identical", 0), len(results)),
        "synthetic_cases": [
            {
                "slug": case["slug"],
                "identical": case["identical"],
                "archived_declares_retired": case.get("archived_declares_retired"),
            }
            for case in synthetic
        ],
    }

def _legacy_metrics(report: dict) -> dict:
    leaked = report.get("archived_still_collected", [])
    return {
        "archived_files": report.get("archived_files", 0),
        "legacy_collected_pages": report.get("legacy_collected_pages", 0),
        "archived_still_collected": leaked,
        "leaks": len(leaked),
        "leaked_probes": report.get("leaked_probes", []),
    }

def _sessions_metrics(report: dict) -> dict:
    return {
        "retention_days": report.get("retention_days"),
        "records_written": report.get("records_written", 0),
        "records_in_corpus": report.get("records_in_corpus"),
        "aged_record_moved": report.get("aged_record_moved"),
        "recent_record_kept": report.get("recent_record_kept"),
        "archived_bytes_identical": report.get("archived_bytes_identical"),
        "restore_command_exists": report.get("restore_command_exists"),
    }

_METRIC_BUILDERS = {
    "ageing": _ageing_metrics,
    "restore": _restore_metrics,
    "legacy": _legacy_metrics,
    "sessions": _sessions_metrics,
}

def _phase_metrics(phase: str, payload: dict) -> dict:
    if phase == "supersession":
        return _supersession_metrics(payload)
    return _METRIC_BUILDERS[phase](payload)

def _gate_rows(metrics: dict) -> list[tuple[str, bool, str]]:
    """Every gate this run can decide, as (name, passed, detail)."""
    rows: list[tuple[str, bool, str]] = []
    for phase, entry in metrics.items():
        rows.extend(_gates_for(phase, entry))
    return rows

def _rate_gate(name: str, value: float | None) -> tuple[str, bool, str]:
    return name, value == 1.0, f"{value}"

def _session_gates(entry: dict) -> list[tuple[str, bool, str]]:
    moved = bool(entry.get("aged_record_moved")) and bool(entry.get("recent_record_kept"))
    intact = bool(entry.get("archived_bytes_identical"))
    return [
        ("sessions.window_applied", moved, f"aged moved={entry.get('aged_record_moved')}"),
        ("sessions.bytes_unchanged", intact, f"{intact}"),
    ]

def _forgetting_gates(phase: str, entry: dict) -> list[tuple[str, bool, str]]:
    rows = [
        _rate_gate(f"{phase}.forget_rate", entry.get("forget_rate")),
        _rate_gate(f"{phase}.retain_rate", entry.get("retain_rate")),
    ]
    return rows + _reprieve_gate(phase, entry)

def _reprieve_gate(phase: str, entry: dict) -> list[tuple[str, bool, str]]:
    """Only the ageing phase can reprieve; supersession has no access term."""
    if "reprieve_rate" not in entry:
        return []
    return [_rate_gate(f"{phase}.reprieve_rate", entry["reprieve_rate"])]

def _restore_gates(_: str, entry: dict) -> list[tuple[str, bool, str]]:
    return [_rate_gate("restore.live_fidelity", entry.get("live_fidelity"))]

def _legacy_gates(_: str, entry: dict) -> list[tuple[str, bool, str]]:
    leaks = entry.get("leaks", 0)
    return [("legacy.no_archive_leak", leaks == 0, f"{leaks} leaked")]

_GATE_BUILDERS = {
    "supersession": _forgetting_gates,
    "ageing": _forgetting_gates,
    "restore": _restore_gates,
    "legacy": _legacy_gates,
    "sessions": lambda _, entry: _session_gates(entry),
}

def _gates_for(phase: str, entry: dict) -> list[tuple[str, bool, str]]:
    return _GATE_BUILDERS[phase](phase, entry)

def _all_metrics(raw: dict, errors: dict) -> dict:
    usable = [(phase, payload) for phase, payload in raw.items() if phase not in errors]
    return {phase: _phase_metrics(phase, payload) for phase, payload in usable}

def _gate_records(gates: list[tuple[str, bool, str]]) -> list[dict]:
    return [{"name": name, "passed": ok, "detail": detail} for name, ok, detail in gates]

def _errored(raw: dict) -> list[tuple[str, str]]:
    return [(phase, item["error"]) for phase, item in raw.items() if "error" in item]
