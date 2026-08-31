"""Selective forgetting as a measured property (MEM-12).

Usage:
    uv run python benchmark/run_selective_forgetting.py
    uv run python benchmark/run_selective_forgetting.py --report out.json
    uv run python benchmark/run_selective_forgetting.py --phases supersession

Memory Agent Bench scores selective forgetting alongside accurate retrieval,
test-time learning and long-range understanding. This stand asks the same
question of this vault, on this vault's own pages, and answers it with five
paired phases run in separate processes by `selective_forgetting_vault.py`:

  supersession  live pages carrying `status: superseded`, measured against a
                control arm where the same pages are forced active, so a miss
                is attributable to the status and not to a weak probe;
  ageing        live pages whose mtime is pushed past their own type window
                (the ages are synthetic, the pages are real), archived through
                `archive_stale.py --apply`, asked before and after, in three
                cohorts: aged and unread, aged but read once (which must earn
                a reprieve), and the never-archive types;
  restore       every archived page brought back with `--restore`, compared
                byte for byte against the page as it was before archiving;
  legacy        the same archive read by the lexical index used when no corpus
                generation is usable;
  sessions      the second forgetting mechanism, on raw session records.

Every gate is a rate over named cohorts, not a score: a forgotten page that
still answers, or a retained page that stops answering, fails the run.
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

BENCHMARK = Path(__file__).resolve().parent
REPO = BENCHMARK.parent
VAULT_SCRIPT = BENCHMARK / "selective_forgetting_vault.py"
LIVE_NOTES = REPO / "knowledge" / "notes"

PHASE_ORDER = ("supersession", "ageing", "restore", "legacy", "sessions")
PHASE_TIMEOUT_SECONDS = 3600.0


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--pages", type=Path, default=LIVE_NOTES,
                        help="notes copied into every trial vault (read-only)")
    parser.add_argument("--phases", nargs="+", choices=PHASE_ORDER, default=list(PHASE_ORDER))
    parser.add_argument("--sample", type=int, default=200, help="probes per cohort")
    parser.add_argument("--base", type=Path, default=None, help="trial scratch directory")
    parser.add_argument("--keep", action="store_true", help="keep every trial vault")
    parser.add_argument("--report", type=Path, default=None, help="write the JSON report here")
    return parser.parse_args(argv)


def _command(phase: str, work: Path, pages: Path, sample: int, arm: str) -> list[str]:
    command = [
        sys.executable, str(VAULT_SCRIPT),
        "--phase", phase,
        "--work", str(work),
        "--pages", str(pages),
        "--sample", str(sample),
    ]
    return command + (["--keep-active"] if arm == "control" else [])


def run_phase(phase: str, work: Path, pages: Path, sample: int, arm: str = "treatment") -> dict:
    """One phase in its own process; the last stdout line is its JSON report."""
    work.mkdir(parents=True, exist_ok=True)
    completed = subprocess.run(
        _command(phase, work, pages, sample, arm),
        capture_output=True, text=True, cwd=str(work),
        timeout=PHASE_TIMEOUT_SECONDS, check=False,
    )
    if completed.returncode != 0:
        return {"phase": phase, "arm": arm, "error": completed.stderr[-2000:]}
    return _decoded(phase, arm, completed.stdout)


def _decoded(phase: str, arm: str, stdout: str) -> dict:
    lines = [line for line in stdout.splitlines() if line.strip()]
    if not lines:
        return {"phase": phase, "arm": arm, "error": "phase produced no report"}
    try:
        return json.loads(lines[-1])
    except json.JSONDecodeError:
        return {"phase": phase, "arm": arm, "error": f"unparsable report: {lines[-1][:200]}"}


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
    return {
        "n": len(targets),
        "probes_usable": usable,
        "surfaced": surfaced,
        "in_corpus": _counted(targets, "in_corpus"),
        "surfaced_rate": _rate(surfaced, usable),
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


def aggregate(raw: dict, started: float) -> dict:
    """The report: raw phase output, derived metrics, and the gate verdicts."""
    errors = dict(_errored(raw))
    metrics = _all_metrics(raw, errors)
    gates = _gate_rows(metrics)
    return {
        "stand": "selective-forgetting-v1",
        "seconds": round(time.monotonic() - started, 2),
        "errors": errors,
        "metrics": metrics,
        "gates": _gate_records(gates),
        "passed": all(ok for _, ok, _ in gates) and not errors,
        "raw": raw,
    }


def _errored(raw: dict) -> list[tuple[str, str]]:
    return [(phase, item["error"]) for phase, item in raw.items() if "error" in item]


def _run_supersession_arms(base: Path, pages: Path, sample: int) -> dict:
    return {
        arm: run_phase("supersession", base / f"supersession-{arm}", pages, sample, arm)
        for arm in ("control", "treatment")
    }


def _merged_supersession(arms: dict) -> dict:
    if any("error" in arm for arm in arms.values()):
        return {"error": "; ".join(arm["error"] for arm in arms.values() if "error" in arm)}
    return {"control": arms["control"], "treatment": arms["treatment"]}


def run_all(phases: list[str], base: Path, pages: Path, sample: int) -> dict:
    raw: dict[str, dict] = {}
    for phase in [item for item in PHASE_ORDER if item in phases]:
        print(f"  running phase {phase} ...", flush=True)
        raw[phase] = _run_one(phase, base, pages, sample)
    return raw


def _run_one(phase: str, base: Path, pages: Path, sample: int) -> dict:
    if phase == "supersession":
        return _merged_supersession(_run_supersession_arms(base, pages, sample))
    return run_phase(phase, base / phase, pages, sample)


def _print_phase(phase: str, entry: dict) -> None:
    print(f"\n{phase}:")
    for key, value in entry.items():
        print(f"  {key}: {json.dumps(value)}")


def _print_gates(gates: list[dict]) -> None:
    print("\ngates:")
    for gate in gates:
        print(f"  {'PASS' if gate['passed'] else 'FAIL'}  {gate['name']}: {gate['detail']}")


def print_report(report: dict) -> None:
    print(f"\nstand: {report['stand']}  ({report['seconds']}s)")
    for phase, entry in report["metrics"].items():
        _print_phase(phase, entry)
    for phase, message in report["errors"].items():
        print(f"\n{phase}: ERROR {message}")
    _print_gates(report["gates"])


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    base = args.base or Path(tempfile.mkdtemp(prefix="llmwiki-forgetting-"))
    base.mkdir(parents=True, exist_ok=True)
    print(f"trial scratch: {base}")
    started = time.monotonic()
    report = aggregate(run_all(args.phases, base, args.pages.resolve(), args.sample), started)
    print_report(report)
    _write(report, args.report)
    _cleanup(base, args.keep)
    print(f"\nverdict: {'PASS' if report['passed'] else 'FAIL'}")
    return 0 if report["passed"] else 1


def _write(report: dict, destination: Path | None) -> None:
    if destination is None:
        return
    destination.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"report written: {destination}")


def _cleanup(base: Path, keep: bool) -> None:
    if keep:
        return
    shutil.rmtree(base, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
