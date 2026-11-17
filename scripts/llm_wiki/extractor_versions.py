"""The versions naming which extractor rules built a generation."""

# The name of the rule that cuts a source into chunks. Change where anything is cut
# and this moves with it: deep validation, the doctor and the nightly all ask whether a
# generation was made by today's rule by reading this string. v4 (2026-09-17) names the
# 2026-09-16 rule — a user turn begins its own chunk — which had shipped under v3.
# `tests/test_a_chunker_that_changes_changes_its_version.py` holds the pin. See
# `docs/research/2026-09-17-a-chunker-that-changes-changes-its-version.md`.
CORPUS_EXTRACTOR_VERSION = "markdown-heading-extractor/v6"

CODE_EXTRACTOR_VERSION = "code-extractor/v19"  # v18: finite re-export walks and complete literal route matching
