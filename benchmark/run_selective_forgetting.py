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

if not __package__ and str(BENCHMARK) not in sys.path:
    sys.path.insert(0, str(BENCHMARK))
if __package__:
    from . import selective_forgetting_metrics as _metrics
else:
    import selective_forgetting_metrics as _metrics

_rate = _metrics._rate
_counted = _metrics._counted
_usable = _metrics._usable
cohort_summary = _metrics.cohort_summary
_supersession_metrics = _metrics._supersession_metrics
_paired = _metrics._paired
_ageing_metrics = _metrics._ageing_metrics
_restore_metrics = _metrics._restore_metrics
_legacy_metrics = _metrics._legacy_metrics
_sessions_metrics = _metrics._sessions_metrics
_phase_metrics = _metrics._phase_metrics
_gate_rows = _metrics._gate_rows
_rate_gate = _metrics._rate_gate
_session_gates = _metrics._session_gates
_forgetting_gates = _metrics._forgetting_gates
_reprieve_gate = _metrics._reprieve_gate
_restore_gates = _metrics._restore_gates
_legacy_gates = _metrics._legacy_gates
_gates_for = _metrics._gates_for
_all_metrics = _metrics._all_metrics
_gate_records = _metrics._gate_records
_errored = _metrics._errored
_METRIC_BUILDERS = _metrics._METRIC_BUILDERS
_GATE_BUILDERS = _metrics._GATE_BUILDERS


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
