"""Helpers shared by test_policy.py and the test files importing them."""

from datetime import UTC, datetime

NOW = datetime(2026, 10, 3, 12, tzinfo=UTC)


def _pending(number):
    return {'captured_at': NOW.isoformat(), 'pending': {'cwd': '/project', 'transcript_path': f'/sessions/{number}.jsonl', 'turn_id': f'turn-{number}', 'at': NOW.isoformat()}}
