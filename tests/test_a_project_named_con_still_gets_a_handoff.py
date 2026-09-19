"""The slug a hook mints is one the project journal accepts.

`_sanitize` never applied the journal's own rules — reserved Windows device
names, a trailing dot or space, characters in the Unicode `C` categories — so a
project directory called `aux` or `notes.` produced a slug refused on every
checkpoint. The only trace was a line in `logs/hook-errors.log`, and that project
never had a handoff.

See `docs/research/2026-09-18-a-slug-the-journal-refuses-is-not-a-slug.md`.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import project_journal  # noqa: E402
import session_start_project_state  # noqa: E402

_REFUSED_BEFORE = ("aux", "con", "notes.", "com1.md", "prn ")


@pytest.mark.parametrize("name", _REFUSED_BEFORE)
def test_a_name_the_journal_used_to_refuse_is_minted_as_an_acceptable_slug(
    name: str,
) -> None:
    slug = session_start_project_state._sanitize(name)

    assert project_journal._require_slug(slug) == slug


def test_an_ordinary_name_is_untouched() -> None:
    assert session_start_project_state._sanitize("My Project") == "my-project"


def test_a_name_with_nothing_usable_left_is_still_empty() -> None:
    assert session_start_project_state._sanitize("...") == ""


def test_the_bootstrap_budget_is_the_measured_one_not_a_round_number() -> None:
    """0.12 s measured on this repository; the bound is forty times that."""
    assert session_start_project_state.BOOTSTRAP_BUDGET_SECONDS == 5.0
