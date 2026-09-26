"""The refusal every repository-index module raises, in a module no one runs as a script.

`repository_index.py` is run by the nightly, which makes it `__main__`; a sibling
that imports `repository_index` gets a second copy of it, and a class defined
there would be two classes. Defined here it is one (audit 2026-09-26 A-5,
docs/research/2026-09-26-a-refusal-class-lives-outside-the-script.md).
"""

from __future__ import annotations

SCHEMA_VERSION = "repository-index/v1"


class RepositoryIndexRefused(ValueError):
    """A named, fail-closed refusal. `reason` is stable; the message explains."""

    def __init__(self, reason: str, message: str, **details: object) -> None:
        super().__init__(message)
        self.reason = reason
        self.details = details

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_version": SCHEMA_VERSION,
            "status": "refused",
            "reason": self.reason,
            "message": str(self),
            **self.details,
        }
