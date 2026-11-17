"""The compile's shared input records, constants and failure vocabulary."""

from __future__ import annotations

import json
from contextvars import ContextVar
from pathlib import Path

from bounded_io import (  # noqa: E402
    MAX_KNOWLEDGE_PAGE_BYTES,
)
from claims import (
    RELATIONS,
)
from context_budget import (  # noqa: E402
    ContextBudget,  # noqa: E402
)
from evidence_resolver import (  # noqa: E402
    MAX_DAILY_PART_BYTES,  # noqa: F401 - re-exported: callers read the writer's bound here
)
from llm_wiki.claim_ledger import (
    LEDGER_SCHEMA,
)
from llm_wiki.formats.canonical import canonical_json_bytes, sha256_bytes  # noqa: E402

COMPILE_PLAN_SCHEMA = Path(__file__).parents[1] / "schemas" / "compile-plan-v2.json"
# How many times a compile re-reads the notes tree after another writer moved
# it under the assessment. Four, because the window is one model call wide and
# a vault that loses four in a row has a busier problem than a retry. See
# `_published`.
COMPILE_PUBLICATION_ATTEMPTS = 4
COMPILER_VERSION = "2.1.0"
NORMALIZATION_VERSION = "normalize-v6"
# Vault metadata input; numerical basis remains under audit. Knowledge notes
# use MAX_KNOWLEDGE_PAGE_BYTES, shared with their publication boundary.
# Daily readers use the existing archive contract and total compile budget.
MAX_SOURCE_BYTES = 4 * 1024 * 1024
MAX_PROVIDER_RESPONSE_BYTES = 4 * 1024 * 1024
MAX_OPERATIONS = 100
MAX_AFTER_IMAGE_BYTES = MAX_KNOWLEDGE_PAGE_BYTES
# The private vault log's after-image in one transaction; it rotates at half of this
# (docs/research/2026-09-25-the-vault-log-rotates-before-its-cap.md). Live: 0.43 MB.
MAX_LOG_BYTES = 4 * 1024 * 1024
# The log is archived and restarted past half its cap, inside the compile that
# would pass it (docs/research/2026-09-25-the-vault-log-rotates-before-its-cap.md).
LOG_ROTATE_BYTES = MAX_LOG_BYTES // 2
LOG_ARCHIVE_DIRECTORY = "knowledge/log-archive"
CLAIM_RECORD_SCHEMA = json.loads(LEDGER_SCHEMA.read_text(encoding="utf-8"))[
    "properties"
]["claims"]["items"]
# What a language model can actually supply about a claim — the sentence's
# meaning — and nothing else. Every other field of `claim/v1` is a fact about
# bytes this process already holds: the fingerprint is a digest of the canonical
# semantics, the evidence reference is a byte span into an immutable snapshot,
# the literal hash is a digest of the quoted line, the observation instant is the
# entry's own timestamp. Asking a model for those produced fabrications, not
# records: measured against the real `claude` provider on this vault's
# 2026-08-20 daily, it volunteered a claim unasked with
# `"fingerprint": "a1b2c3d4e5f6a1b2..."` and a `block:` naming a hex prefix
# instead of a time — and the whole two-page plan died on it. See
# `docs/research/2026-08-28-who-computes-a-claims-provenance.md`.
CLAIM_CANDIDATE_SCHEMA = {
    "type": "object",
    "required": ["evidence_index", "subject", "relation", "value"],
    "properties": {
        "evidence_index": {
            "type": "integer",
            "minimum": 0,
        },
        "subject": {
            "type": "string", "minLength": 1, "maxLength": 4000,
            "pattern": "^[^\\r\\n]+$",
        },
        "relation": {"enum": sorted(RELATIONS)},
        "value": CLAIM_RECORD_SCHEMA["properties"]["value"],
        "qualifiers": CLAIM_RECORD_SCHEMA["properties"]["qualifiers"],
    },
    "additionalProperties": False,
}
CLAIM_EXTRACTOR_VERSION = "compile-claim/v2"
ALLOWED_CATEGORIES = frozenset(
    {"concepts", "decisions", "patterns", "debugging", "qa"}
)
DRAFT_PROGRAM = (
    "compile-draft/v22: complete-source planning and shared durable-knowledge rubric, with lossless source choices; "
    "with immutable original-entry context and derived-provenance claims"
)
CRITIQUE_PROGRAM = (
    "compile-critique/v11: complete semantic feedback and role-preserving review correction; "
    "one verdict for every operation"
)
DRAFT_SYSTEM = "You are a skeptical memory editor. Return only the requested JSON."
CRITIQUE_SYSTEM = "You are a strict memory-plan critic. Return only the requested JSON."
DURABLE_KNOWLEDGE_POLICY = """DURABLE KNOWLEDGE CRITERIA (the same for drafting and both reviews)
Preserve ALL new evidenced durable project facts, decisions, constraints, risks and commitments across ALL selected sources.
An assignment with an explicit owner and responsibility, a merge dependency, or a concrete change of scope is durable when the source actually states it.
Generic instructions to continue, do the work, check dependencies or follow existing rules add no new project fact by themselves.
Do not infer an ownership decision or a completion claim from those generic instructions.
Keep observations, reported findings, hypotheses, suggestions, requests and completed actions distinct; preserve explicit uncertainty and project scope.
Do not invent a requirement for research, remediation, approval or another next step merely because the source reports an unresolved problem.
Completeness covers supported durable information, not every utterance; a reviewer must not demand an unsupported inference as a missing fact.
Every proposed assertion still requires evidence, and already preserved facts do not require duplicate pages."""
RAW_PLAN_SCHEMA = {
    "type": "object",
    "required": ["operations"],
    "properties": {
        "operations": {
            "type": "array",
            "maxItems": MAX_OPERATIONS,
            "items": {
                "type": "object",
                "required": [
                    "action", "category", "slug", "title", "summary",
                    "body_section", "body_markdown", "evidence", "related"
                ],
                "properties": {
                    "action": {"enum": ["create", "update"]},
                    "category": {"enum": sorted(ALLOWED_CATEGORIES)},
                    "slug": {"type": "string", "minLength": 1, "maxLength": 120, "pattern": "^[a-z0-9]+(?:-[a-z0-9]+)*$"},
                    "title": {"type": "string", "minLength": 1, "maxLength": 200, "pattern": "^[^\\r\\n]+$"},
                    "summary": {"type": "string", "minLength": 1, "maxLength": 500, "pattern": "^[^\\r\\n]+$"},
                    "body_section": {"enum": ["Lesson", "Decision", "Symptom / Cause / Resolution", "Answer"]},
                    "body_markdown": {"type": "string", "minLength": 1, "maxLength": 20000},
                    "evidence": {
                        "type": "array", "minItems": 1,
                        "items": {
                            "type": "object",
                            "required": ["daily_date", "timestamp", "quoted_text", "claim"],
                            "properties": {
                                "daily_date": {"type": "string", "pattern": "^[0-9]{4}-[0-9]{2}-[0-9]{2}$"},
                                "timestamp": {"type": "string", "pattern": "^(?:[01][0-9]|2[0-3]):[0-5][0-9]:[0-5][0-9]$"},
                                "quoted_text": dict(CLAIM_RECORD_SCHEMA["properties"]["evidence"]["properties"]["text"]),
                                "claim": {"type": "string", "minLength": 1, "maxLength": 1000, "pattern": "^[^\\r\\n]+$"}
                            },
                            "additionalProperties": False
                        }
                    },
                    "related": {"type": "array", "items": {"type": "string", "maxLength": 200, "pattern": "^\\[\\[[^\\r\\n]+\\]\\]$"}},
                    "claims": {"type": "array", "items": CLAIM_CANDIDATE_SCHEMA},
                },
                "additionalProperties": False
            }
        },
        "audit": {
            "type": "object",
            "properties": {
                "verified": {"type": "integer", "minimum": 0},
                "dedup": {"type": "integer", "minimum": 0},
                "stubs": {"type": "integer", "minimum": 0},
                "contradictions": {"type": "integer", "minimum": 0},
                "rejected": {"type": "integer", "minimum": 0}
            },
            "additionalProperties": False
        },
    },
    "additionalProperties": False,
}
CRITIQUE_SCHEMA = {
    "type": "object",
    "required": ["reviews"],
    "properties": {
        "reviews": {
            "type": "array", "maxItems": MAX_OPERATIONS,
            "items": {
                "type": "object", "required": ["slug", "verdict", "reason"],
                "properties": {
                    "slug": {"type": "string", "minLength": 1, "maxLength": 120, "pattern": "^[a-z0-9]+(?:-[a-z0-9]+)*$"},
                    "verdict": {"enum": ["pass", "drop"]},
                    "reason": {"type": "string", "minLength": 1, "maxLength": 1000}
                },
                "additionalProperties": False
            }
        },
    },
    "additionalProperties": False,
}
SOURCE_REVIEW_SCHEMA = {
    "type": "object", "required": ["source_review"],
    "properties": {"source_review": {
        "type": "object", "required": ["complete", "reason"],
        "properties": {"complete": {"type": "boolean"},
                       "reason": {"type": "string", "minLength": 1}},
        "additionalProperties": False}},
    "additionalProperties": False,
}
LEGACY_EVIDENCE_SCHEMA = RAW_PLAN_SCHEMA["properties"]["operations"]["items"]["properties"]["evidence"]["items"]
NATIVE_EVIDENCE_SCHEMA = json.loads(canonical_json_bytes(LEGACY_EVIDENCE_SCHEMA))
NATIVE_EVIDENCE_SCHEMA["required"].append("native_event")
NATIVE_EVIDENCE_SCHEMA["properties"]["quoted_text"] = {"type": "string", "minLength": 1, "pattern": "^[^\\r\\n]+$"}
NATIVE_EVIDENCE_SCHEMA["properties"]["native_event"] = {
    "type": "object", "required": ["source_path", "byte_start", "line_index"],
    "properties": {
        "source_path": {"type": "string", "minLength": 1},
        "byte_start": {"type": "integer", "minimum": 0},
        "line_index": {"type": "integer", "minimum": 0},
    }, "additionalProperties": False,
}
RAW_PLAN_SCHEMA["properties"]["operations"]["items"]["properties"]["evidence"]["items"] = {
    "oneOf": [LEGACY_EVIDENCE_SCHEMA, NATIVE_EVIDENCE_SCHEMA],
}


DRAFT_PROGRAM_HASH = sha256_bytes(
    canonical_json_bytes(
        {"program": DRAFT_PROGRAM, "system": DRAFT_SYSTEM, "schema": RAW_PLAN_SCHEMA,
         "knowledge_policy": DURABLE_KNOWLEDGE_POLICY}
    )
)
CRITIQUE_PROGRAM_HASH = sha256_bytes(
    canonical_json_bytes(
        {
            "program": CRITIQUE_PROGRAM,
            "system": CRITIQUE_SYSTEM,
            "schema": CRITIQUE_SCHEMA,
            "source_review_schema": SOURCE_REVIEW_SCHEMA,
            "knowledge_policy": DURABLE_KNOWLEDGE_POLICY,
        }
    )
)


# Singular form per category — used for OKF `type:` frontmatter. Avoids the
# `rstrip('s')` footgun (would mangle entities→entitie, syntheses→synthese).
CATEGORY_SINGULAR = {
    "concepts": "concept",
    "decisions": "decision",
    "patterns": "pattern",
    "debugging": "debugging",
    "qa": "qa",
}


SOURCE_CHOICE_RESOLVER = ContextVar("source_choice_resolver", default=None)
SOURCE_CHOICE_PARSING = ContextVar("source_choice_parsing", default=None)
SOURCE_CHOICE_ORIGINAL = ContextVar("source_choice_original", default=None)
SOURCE_CHOICE_BINDINGS = ContextVar("source_choice_bindings", default=None)


# One compile budget: a 32k window, 4k reserved for the answer, 1k of slack.
# Written once, read by batching and by the schema fit check (audit L6).
COMPILE_CONTEXT_WINDOW_TOKENS = 32_768
COMPILE_ANSWER_RESERVE_TOKENS = 4_000
COMPILE_SLACK_TOKENS = 1_024


def compile_budget(model: str | None) -> ContextBudget:
    return ContextBudget(
        model, COMPILE_CONTEXT_WINDOW_TOKENS, COMPILE_ANSWER_RESERVE_TOKENS, COMPILE_SLACK_TOKENS
    )
