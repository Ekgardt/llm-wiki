"""Helpers shared by test_grounded_qa.py and the test files importing them."""
from __future__ import annotations

import json
from pathlib import Path

import pytest


def _write_page(vault: Path, name: str, body: str, *, authority: str = "user") -> Path:
    path = vault / "knowledge" / "notes" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "---\n"
        "type: concept\n"
        f"source_authority: {authority}\n"
        "confidence: high\n"
        "---\n\n"
        f"# {name.removesuffix('.md').title()}\n\n"
        f"{body}\n",
        encoding="utf-8",
        newline="",
    )
    return path


@pytest.fixture
def vault(tmp_path: Path) -> Path:
    root = tmp_path / "vault"
    (root / "knowledge" / "notes").mkdir(parents=True)
    (root / "knowledge" / "projects").mkdir(parents=True)
    (root / "knowledge" / "daily").mkdir(parents=True)
    return root


def _answer_for_prompt(prompt: str, *, status: str = "answered") -> str:
    marker = "<evidence_manifest>\n"
    manifest_text = prompt.split(marker, 1)[1].split("\n</evidence_manifest>", 1)[0]
    evidence = json.loads(manifest_text)[0]
    if status != "answered":
        return json.dumps(
            {
                "schema_version": "grounded-answer/v1",
                "status": status,
                "claims": [],
                "citations": [],
                "reason": "The supplied evidence does not support an answer.",
            }
        )
    citation = {key: evidence[key] for key in evidence if key != "text"}
    return json.dumps(
        {
            "schema_version": "grounded-answer/v1",
            "status": "answered",
            "claims": [{"text": "Alpha is enabled.", "citation_ids": [evidence["citation_id"]]}],
            "citations": [citation],
            "reason": None,
        }
    )
