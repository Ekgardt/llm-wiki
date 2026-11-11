"""Helpers shared by test_module_contract_docs.py and the test files importing them."""

import re

from tests.module_context import _contained


def _context_owners(root, files, ownership, record_names):
    names = {p.relative_to(root).as_posix() for p in files} | set(record_names)
    return {ownership[name] for name in names if name in ownership}


def _required_guides(root, files, ownership, *, record_names=()):
    owners = _context_owners(root, files, ownership, record_names)
    pending = {root / 'docs/modules' / f'{owner}.md' for owner in owners}
    return _guide_closure(root, pending)


def _shared_targets(root, guide):
    lines = [line for line in guide.read_text(encoding='utf-8').splitlines() if line.startswith('Shared canonical contract:')]
    matches = [re.fullmatch(r'Shared canonical contract: \[.*?\]\(([^)]+)\)\.', line) for line in lines]
    assert all(matches), f'Malformed shared contract reference: {guide}'
    return {_contained(root, str(guide.parent.relative_to(root) / match.group(1))) for match in matches}


def _guide_closure(root, pending):
    seen = set()
    while pending:
        guide = pending.pop()
        if guide in seen:
            continue
        seen.add(guide)
        pending.update(_shared_targets(root, guide) - seen)
    return seen
