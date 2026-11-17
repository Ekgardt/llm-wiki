"""The selective-forgetting stand must be trustworthy before its numbers are.

These tests cover the stand's own logic — probe extraction, cohort selection,
rate arithmetic and gate verdicts — not the product's archiving, which the
stand measures in throwaway vaults. Nothing here writes into the live vault.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "benchmark"))

import run_selective_forgetting as runner  # noqa: E402
import selective_forgetting_vault as stand  # noqa: E402

PAGE = (
    "---\ntype: debugging\nstatus: active\nsuperseded_by: \"[[a-newer-page]]\"\n---\n\n"
    "# Title\n\n"
    "One-sentence summary: a short line.\n\n"
    "The longest plain body line in this page states the finding in full words.\n\n"
    "- a bullet that should never be chosen as a probe even when it is long enough\n"
)


def test_probe_phrase_takes_a_verbatim_plain_line():
    phrase = stand.probe_phrase(PAGE)
    assert phrase in PAGE
    assert phrase.startswith("The longest plain body line")


def test_probe_phrase_is_clipped_to_a_fixed_word_count():
    assert len(stand.probe_phrase(PAGE).split()) == stand.PROBE_WORDS


def test_probe_phrase_is_deterministic_across_calls():
    assert stand.probe_phrase(PAGE) == stand.probe_phrase(PAGE)


def test_probe_phrase_skips_headings_bullets_and_links():
    phrase = stand.probe_phrase(PAGE)
    assert not phrase.startswith(("#", "-", "One-sentence"))


def test_probe_phrase_is_empty_when_no_line_qualifies():
    assert stand.probe_phrase("---\ntype: gap\n---\n\n# Short\n\ntiny\n") == ""


def test_frontmatter_fields_are_read_without_quotes():
    frontmatter = stand.frontmatter_of(PAGE)
    assert stand.field(frontmatter, "type") == "debugging"
    assert stand.field(frontmatter, "status") == "active"


def test_successor_slugs_read_the_wikilink_target():
    assert stand.successor_slugs(PAGE) == ["a-newer-page"]


def test_successor_slugs_strip_a_path_qualified_link():
    page = "---\nsuperseded_by: [[knowledge/notes/newer-decision]]\n---\n\nbody\n"
    assert stand.successor_slugs(page) == ["newer-decision"]


def test_successor_slugs_are_empty_without_the_field():
    assert stand.successor_slugs("---\ntype: decision\n---\n\nbody\n") == []


def test_source_pages_skips_editorial_metadata(tmp_path):
    for name in ("index.md", "log.md", "README.md", "real-page.md"):
        (tmp_path / name).write_text("x", encoding="utf-8")
    assert [page.name for page in stand.source_pages(tmp_path)] == ["real-page.md"]


def test_seed_copies_only_the_pages_and_returns_the_copies(tmp_path):
    source = tmp_path / "src"
    source.mkdir()
    (source / "one.md").write_text("body one", encoding="utf-8")
    (source / "index.md").write_text("editorial", encoding="utf-8")
    root = tmp_path / "vault"
    (root / "knowledge/notes").mkdir(parents=True)
    copies = stand.seed(root, source)
    assert [page.name for page in copies] == ["one.md"]
    assert (root / "knowledge/notes/one.md").read_text(encoding="utf-8") == "body one"


def test_rank_of_finds_the_target_by_file_name():
    paths = ["knowledge/notes/other.md", "knowledge/notes/target.md"]
    assert stand._rank_of("target", paths) == 2
    assert stand._rank_of("absent", paths) is None


def test_probe_result_reports_an_unusable_probe_rather_than_a_miss():
    result = stand.probe_result("slug", "")
    assert result["probe_usable"] is False
    assert result["surfaced"] is False


def test_outcome_counts_group_by_the_leading_word():
    counts = stand._outcome_counts(["ARCHIVED: a", "ARCHIVED: b", "WRITE_ERROR: c"])
    assert counts == {"ARCHIVED": 2, "WRITE_ERROR": 1}


# -- the aggregator -------------------------------------------------------


def test_aggregate_fails_the_run_when_a_phase_errored():
    report = runner.aggregate({"legacy": {"error": "boom"}}, 0.0)
    assert report["passed"] is False
    assert report["errors"] == {"legacy": "boom"}


def test_aggregate_passes_a_clean_legacy_phase():
    report = runner.aggregate({"legacy": {"archived_still_collected": []}}, 0.0)
    assert report["passed"] is True


def test_decoded_names_an_unparsable_report_instead_of_raising():
    decoded = runner._decoded("legacy", "treatment", "not json\n")
    assert "unparsable report" in decoded["error"]


def test_decoded_names_an_empty_report():
    assert runner._decoded("legacy", "treatment", "")["error"] == "phase produced no report"


def test_the_control_arm_is_the_only_one_that_forces_pages_active():
    control = runner._command("supersession", Path("/w"), Path("/p"), 1, "control")
    treatment = runner._command("supersession", Path("/w"), Path("/p"), 1, "treatment")
    assert "--keep-active" in control
    assert "--keep-active" not in treatment


@pytest.mark.parametrize("phase", runner.PHASE_ORDER)
def test_every_ordered_phase_is_runnable_by_the_vault_script(phase):
    assert phase in stand.PHASES


def _metric_export(name, value):
    return not name.startswith('__') and (callable(value) or isinstance(value, dict))


def test_runner_retains_identical_metric_and_dispatch_aliases():
    values = vars(runner._metrics)
    names = [name for name, value in values.items()
             if _metric_export(name, value)]
    assert len(names) == 24
    assert all(getattr(runner, name) is values[name] for name in names)


@pytest.mark.parametrize('namespace', [True, False])
def test_forgetting_runner_starts_without_inherited_pythonpath(namespace):
    import subprocess

    script = REPO / 'benchmark/run_selective_forgetting.py'
    snippet = 'import runpy,sys;target=sys.argv[1];sys.argv=[target,"--help"];runpy.run_path(target,run_name="__main__")'
    if namespace:
        snippet = 'import sys;sys.path.insert(0,sys.argv[1]);from benchmark import run_selective_forgetting as r;assert r.parse_args(["--help"])'
        script = REPO
    completed = subprocess.run([sys.executable, '-I', '-c', snippet, str(script)],
                               capture_output=True, text=True, timeout=30, check=True)
    assert '--pages' in completed.stdout and '--phases' in completed.stdout
