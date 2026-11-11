# `tests/test_doctor.py`


The doctor report, its command line, read-only runs, filesystem health, repair and the
maintenance owner. The other doctor scenarios live in `tests/test_doctor_*.py` files with their
own guides.

It also owns `tests/doctor_case_helpers.py`, the helpers those scenario files share.

Providers outside the module appear in the packet either whole or as their exact
derived description; the `-context.json` manifest next to this guide lists the
complete maintenance context and the tests that qualify each description.

Run `uv run pytest tests/test_doctor.py -q`.
