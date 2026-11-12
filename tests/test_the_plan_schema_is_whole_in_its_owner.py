"""The compile plan schema and its program hash do not depend on which module was imported first.

The native evidence branch used to be added by the `compile_pipeline` facade at import time,
after `compile_pipeline_base` had already hashed the schema without it: a process that loaded
only the owner saw another schema, and every process saw a draft program hash that no longer
named the schema the model was given.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

_PROBE = """
import json, sys
import llm_wiki.compile_pipeline_base as base
if sys.argv[1] == "facade":
    import compile_pipeline  # noqa: F401
from llm_wiki.formats.canonical import canonical_json_bytes, sha256_bytes
schema = base.RAW_PLAN_SCHEMA
print(json.dumps({
    "evidence": schema["properties"]["operations"]["items"]["properties"]["evidence"]["items"],
    "hash": base.DRAFT_PROGRAM_HASH,
    "rehashed": sha256_bytes(canonical_json_bytes({
        "program": base.DRAFT_PROGRAM, "system": base.DRAFT_SYSTEM, "schema": schema,
        "knowledge_policy": base.DURABLE_KNOWLEDGE_POLICY,
    })),
}))
"""


def _probe(first_import: str) -> dict:
    environment = {**os.environ, "PYTHONPATH": os.pathsep.join([str(ROOT / "scripts"), str(ROOT)])}
    completed = subprocess.run(
        [sys.executable, "-c", _PROBE, first_import],
        capture_output=True, text=True, check=True, env=environment, timeout=120,
    )
    return json.loads(completed.stdout)


def test_the_owner_alone_offers_the_native_evidence_branch():
    evidence = _probe("owner")["evidence"]
    assert [sorted(branch["required"]) for branch in evidence["oneOf"]][1].count("native_event") == 1


def test_the_draft_program_hash_names_the_schema_the_model_is_given():
    owner, facade = _probe("owner"), _probe("facade")
    assert owner == facade
    assert owner["hash"] == owner["rehashed"]
