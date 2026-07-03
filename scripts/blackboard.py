"""Agent coordination blackboard — shared state for parallel agents.

When multiple agents work in the same project simultaneously, they
need a shared "blackboard" to coordinate: claim tasks, signal
completion, leave notes for each other, and detect conflicts.

Pattern: Blackboard Architecture (from AI classical literature).
Each agent reads/writes to a shared space in the vault. No direct
agent-to-agent communication needed — coordination happens through
the shared state.

Files live at: wiki/projects/<slug>/.blackboard/
  - tasks.jsonl     — task queue with claim/complete status
  - signals.jsonl   — inter-agent signals ("I'm working on X")
  - conflicts.jsonl — conflict detection ("agent A and B edited same file")

Usage:
    # Agent A claims a task
    uv run python scripts/blackboard.py claim --project your-project --task "implement JWT" --agent opencode

    # Agent B sees what's taken
    uv run python scripts/blackboard.py status --project your-project

    # Agent A completes
    uv run python scripts/blackboard.py complete --project your-project --task-id <id>

    # Agent B leaves a note
    uv run python scripts/blackboard.py signal --project your-project --from opencode --to codex --message "JWT done, your turn for tests"

    # Check for conflicts (two agents edited same file)
    uv run python scripts/blackboard.py conflicts --project your-project
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from memory_state import ROOT  # noqa: E402

PROJECTS_DIR = ROOT / "wiki" / "projects"


def _bb_dir(project: str) -> Path:
    d = PROJECTS_DIR / project / ".blackboard"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _append_jsonl(path: Path, record: dict) -> None:
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def _read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    records = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return records


def claim_task(project: str, task: str, agent: str) -> str:
    """Agent claims a task. Returns task ID."""
    tasks_file = _bb_dir(project) / "tasks.jsonl"
    task_id = hashlib.sha256(f"{task}{time.time()}".encode()).hexdigest()[:12]
    record = {
        "id": task_id,
        "task": task,
        "agent": agent,
        "status": "claimed",
        "claimed_at": datetime.now().isoformat(timespec="seconds"),
        "completed_at": None,
    }
    _append_jsonl(tasks_file, record)
    return task_id


def complete_task(project: str, task_id: str) -> bool:
    """Mark a task as completed."""
    tasks_file = _bb_dir(project) / "tasks.jsonl"
    tasks = _read_jsonl(tasks_file)
    found = False
    for t in tasks:
        if t["id"] == task_id:
            t["status"] = "completed"
            t["completed_at"] = datetime.now().isoformat(timespec="seconds")
            found = True
    if found:
        tasks_file.write_text(
            "\n".join(json.dumps(t, ensure_ascii=False) for t in tasks) + "\n",
            encoding="utf-8",
        )
    return found


def get_status(project: str) -> dict:
    """Get blackboard status for a project."""
    tasks_file = _bb_dir(project) / "tasks.jsonl"
    signals_file = _bb_dir(project) / "signals.jsonl"
    tasks = _read_jsonl(tasks_file)
    signals = _read_jsonl(signals_file)
    active = [t for t in tasks if t["status"] == "claimed"]
    completed = [t for t in tasks if t["status"] == "completed"]
    # Active agents
    active_agents = list(set(t["agent"] for t in active))
    return {
        "project": project,
        "active_tasks": len(active),
        "completed_tasks": len(completed),
        "active_agents": active_agents,
        "recent_signals": signals[-5:],
        "tasks": active[:10],
    }


def send_signal(project: str, from_agent: str, to_agent: str, message: str) -> None:
    """Leave a signal for another agent."""
    signals_file = _bb_dir(project) / "signals.jsonl"
    record = {
        "from": from_agent,
        "to": to_agent,
        "message": message,
        "at": datetime.now().isoformat(timespec="seconds"),
    }
    _append_jsonl(signals_file, record)


def detect_conflicts(project: str) -> list[dict]:
    """Detect if two agents claimed overlapping tasks."""
    tasks_file = _bb_dir(project) / "tasks.jsonl"
    tasks = [t for t in _read_jsonl(tasks_file) if t["status"] == "claimed"]
    conflicts = []
    for i, a in enumerate(tasks):
        for b in tasks[i + 1:]:
            # Simple word overlap check
            a_words = set(a["task"].lower().split())
            b_words = set(b["task"].lower().split())
            common = a_words & b_words
            stop = {"the", "a", "an", "for", "of", "to", "in", "and", "fix", "add", "update"}
            meaningful = common - stop
            if len(meaningful) >= 2 and a["agent"] != b["agent"]:
                conflicts.append({
                    "agent_a": a["agent"],
                    "task_a": a["task"],
                    "agent_b": b["agent"],
                    "task_b": b["task"],
                    "overlap": list(meaningful),
                })
    return conflicts


def main() -> int:
    p = argparse.ArgumentParser(description="Agent coordination blackboard.")
    sub = p.add_subparsers(dest="command")

    c = sub.add_parser("claim", help="Claim a task")
    c.add_argument("--project", required=True)
    c.add_argument("--task", required=True)
    c.add_argument("--agent", required=True)

    c2 = sub.add_parser("complete", help="Complete a task")
    c2.add_argument("--project", required=True)
    c2.add_argument("--task-id", required=True)

    sub.add_parser("status", help="Show blackboard status").add_argument("--project", required=True)

    s = sub.add_parser("signal", help="Send a signal to another agent")
    s.add_argument("--project", required=True)
    s.add_argument("--from", dest="from_agent", required=True)
    s.add_argument("--to", required=True)
    s.add_argument("--message", required=True)

    cf = sub.add_parser("conflicts", help="Detect task conflicts")
    cf.add_argument("--project", required=True)

    args = p.parse_args()

    if args.command == "claim":
        tid = claim_task(args.project, args.task, args.agent)
        print(f"Claimed: {tid}")
    elif args.command == "complete":
        if complete_task(args.project, args.task_id):
            print(f"Completed: {args.task_id}")
        else:
            print(f"Not found: {args.task_id}")
            return 1
    elif args.command == "status":
        status = get_status(args.project)
        print(json.dumps(status, indent=2, ensure_ascii=False))
    elif args.command == "signal":
        send_signal(args.project, args.from_agent, args.to, args.message)
        print(f"Signal sent: {args.from_agent} → {args.to}: {args.message}")
    elif args.command == "conflicts":
        conflicts = detect_conflicts(args.project)
        if conflicts:
            print(f"Found {len(conflicts)} conflict(s):\n")
            for c in conflicts:
                print(f"  {c['agent_a']}: '{c['task_a']}'")
                print(f"  vs {c['agent_b']}: '{c['task_b']}'")
                print(f"  overlap: {c['overlap']}\n")
        else:
            print("No conflicts detected.")
    else:
        p.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
