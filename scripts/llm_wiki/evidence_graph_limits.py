"""The bounds one evidence-graph generation build is validated under."""

from __future__ import annotations

# Rows one generation build validates per table, refused past it (a runaway build).
# This repository's generation held about 38 000 CALLS assertions (2026-08-29), so a
# million is over 25 times a real repository.
MAX_VALIDATION_ROWS = 1_000_000
