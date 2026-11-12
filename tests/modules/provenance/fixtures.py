"""Original immutable link/prose examples shared by pure and integrated tests."""

RELATED = (
    "## Related\n"
    "- [[knowledge/notes/secret-shape-not-secret-name-decision]] — what the redactor may treat as a secret.\n"
    "- [[solo-operator-superset-product-decision]]\n"
    "- [[v4-reliability-contracts-decision]]\n"
    "- [[derived-evidence-generation-decision]]\n"
)
# Measured on this vault: link density 0.31, under the boilerpipe bound.
ANNOTATED = (
    "## Related\n"
    "- [[knowledge/notes/secret-shape-not-secret-name-decision]] — what the redactor may treat\n"
    "  as a secret: the value decides, not the name beside it.\n"
    "- [[knowledge/notes/self-resolving-health-findings-decision]] — the same principle applied\n"
    "  to health: a rule that can never pass is not a safety rule.\n"
)
PROSE = (
    "## Decision\n"
    "The redactor treats a value as a secret by its shape, not by the name beside it; "
    "see [[secret-shape-not-secret-name-decision]] for the measured misses.\n"
)
