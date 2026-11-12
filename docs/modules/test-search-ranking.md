# `tests/test_search_ranking.py`

Search over the vault: title and summary extraction, the bounded page collector, limits, impressions,
telemetry and the status report.

Generation artifacts, fallbacks and publication live in `tests/test_search_ranking_*.py`.

Providers outside the module appear in the packet either whole or as their exact
derived description; the `-context.json` manifest next to this guide lists the
complete maintenance context and the tests that qualify each description.

Run `uv run pytest tests/test_search_ranking.py -q`.
