"""Native user data retains its complete physical JSON citation."""
import hashlib
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import fact_keys
from breadcrumb_decision import _journal_block
from corpus_snapshot import collect_corpus
from event_envelope import native_user_text

from tests.test_breadcrumb_evidence import _source


def _frame(prompt, **changes):
    event = dict(schema_version="1.0", event_type="user_prompt", payload={"prompt": prompt},
                 agent="codex", session="s", project=None, worktree=None,
                 severity=None, parent_event_id=None, source_event_id=None)
    event.update(changes)
    return json.dumps(event, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _captured_vault(root, frame, *, daily=True):
    import breadcrumb_evidence

    head, documents = _source(frame.encode())
    _, anchor = breadcrumb_evidence._read_head(documents[head])
    path = "knowledge/daily/2026-09-29.md"
    if daily:
        documents[path] = ("# Daily\n" + _journal_block(json.loads(anchor), head, frame.encode())).encode()
    for relative, content in documents.items():
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
    return collect_corpus(root, daily_paths=[path] if daily else [])


def test_complete_native_user_frame_is_one_physical_turn(tmp_path):
    prompt = 'Я владею велосипедом. ' * 400 + '\nХвост: машина только тестовая.'
    frame = _frame(prompt)
    snapshot = _captured_vault(tmp_path, frame)
    path = "knowledge/daily/2026-09-29.md"
    turns = fact_keys.user_turns(snapshot.chunks, sources=snapshot.sources)
    assert len(turns) == 1
    turn = turns[0]
    assert turn.source_path == path
    assert turn.text == prompt
    raw = (tmp_path / path).read_bytes()
    physical = raw[turn.byte_start:turn.byte_end]
    assert frame.encode() in physical
    assert len(physical) > 4096
    assert hashlib.sha256(physical).hexdigest() == turn.span_sha256


def test_linked_only_native_part_retains_its_physical_container(tmp_path):
    snapshot = _captured_vault(tmp_path, _frame("Мой велосипед синий."), daily=False)
    turns = fact_keys.user_turns(snapshot.chunks, sources=snapshot.sources)
    assert len(turns) == 1
    turn = turns[0]
    assert turn.source_path.endswith(".breadcrumb-part.md")
    assert turn.text == "Мой велосипед синий."
    raw = (tmp_path / turn.source_path).read_bytes()[turn.byte_start:turn.byte_end]
    assert hashlib.sha256(raw).hexdigest() == turn.span_sha256
    assert json.loads(raw)["text"] == _frame(turn.text)


def test_chunks_without_source_proof_grant_no_native_fact(tmp_path):
    snapshot = _captured_vault(tmp_path, _frame("Мой велосипед синий."))
    assert fact_keys.user_turns(snapshot.chunks) == []


def test_main_style_daily_only_snapshot_resolves_only_its_link(tmp_path):
    _captured_vault(tmp_path, _frame("Мой велосипед синий."))
    snapshot = collect_corpus(tmp_path, daily_paths=["knowledge/daily/2026-09-29.md"],
                              pruned_directories=("knowledge/raw/sessions",))
    assert all(source.record.relative_path.startswith("knowledge/daily/") for source in snapshot.sources)
    turns = fact_keys.user_turns(snapshot.chunks, sources=snapshot.sources, vault=tmp_path)
    assert len(turns) == 1


@pytest.mark.parametrize("changes", [{"schema_version": "future"}, {"payload": {"prompt": 4}}])
def test_unsupported_or_invalid_native_frame_is_a_visible_refusal(changes):
    with pytest.raises(ValueError):
        native_user_text("    " + _frame("user", **changes))


def test_tool_text_is_not_a_user_fact(tmp_path):
    snapshot = _captured_vault(tmp_path, _frame("", event_type="post_tool_use",
                                             payload={"tool_name": "read", "target": "file"}))
    assert fact_keys.user_turns(snapshot.chunks, sources=snapshot.sources) == []


def test_source_authority_expiry_does_not_report_successful_zero(tmp_path):
    snapshot = _captured_vault(tmp_path, _frame("Мой велосипед синий."))
    with pytest.raises(TimeoutError, match="deadline"):
        fact_keys.user_turns(snapshot.chunks, sources=snapshot.sources, deadline=0)


def test_default_capacity_admits_existing_daily_family_but_explicit_override_refuses(tmp_path):
    from bounded_io import MAX_KNOWLEDGE_PAGE_BYTES

    path = "knowledge/daily/2026-09-29.md"
    target = tmp_path / path
    target.parent.mkdir(parents=True)
    target.write_bytes(b"# Daily\n" + b" " * MAX_KNOWLEDGE_PAGE_BYTES)
    assert collect_corpus(tmp_path, daily_paths=[path]).sources
    with pytest.raises(ValueError, match="exceeds"):
        collect_corpus(tmp_path, daily_paths=[path], max_file_bytes=MAX_KNOWLEDGE_PAGE_BYTES)


@pytest.mark.parametrize("replacement", ["## [01:00:00] Captured event"])
def test_forged_journal_framing_grants_no_daily_fact(tmp_path, replacement):
    _captured_vault(tmp_path, _frame("Мой велосипед синий."))
    path = "knowledge/daily/2026-09-29.md"
    target = tmp_path / path
    target.write_text(target.read_text().replace("## [00:00:00] Captured event", replacement))
    snapshot = collect_corpus(tmp_path, daily_paths=[path], pruned_directories=("knowledge/raw/sessions",))
    assert fact_keys.user_turns(snapshot.chunks, sources=snapshot.sources, vault=tmp_path) == []


def test_native_multipart_collection_stays_valid_and_fact_work_stays_pending(tmp_path):
    snapshot = _captured_vault(tmp_path, _frame("Мой велосипед. " * 180000), daily=False)
    assert snapshot.sources and snapshot.chunks
    with pytest.raises(ValueError, match="physical citation; remains pending"):
        fact_keys.user_turns(snapshot.chunks, sources=snapshot.sources)


def test_native_tool_multipart_does_not_break_collection_or_manufacture_user_facts(tmp_path):
    snapshot = _captured_vault(tmp_path, _frame("", event_type="post_tool_use",
                                             payload={"tool_name": "read", "target": "x" * 3000000}), daily=False)
    assert snapshot.sources
    assert fact_keys.user_turns(snapshot.chunks, sources=snapshot.sources) == []


def test_native_part_fact_and_undated_ledger_record_reach_physical_fts_hit(tmp_path):
    import ledger

    from tests.test_the_keys_are_indexed_beside_the_turn import _built, _searched

    snapshot = _captured_vault(tmp_path, _frame("Мой велосипед синий."), daily=False)
    turn = fact_keys.user_turns(snapshot.chunks, sources=snapshot.sources)[0]
    records = ledger.records_of(turn, {"records": [{"kind": "bike", "thing": "blue bike"}]})
    assert len(records) == 1
    assert records[0].day == "2026-09-29" and records[0].dated is False
    store = fact_keys.KeyStore(tmp_path / "keys.sqlite3")
    store.add(turn, ["I own a velocipede"])
    ledger.post(store.connection, records)
    store.close()
    artifact = _built(tmp_path, snapshot, fact_keys.keys_by_span(tmp_path / "keys.sqlite3"))
    hits = _searched(artifact, "velocipede")
    assert len(hits) == 1
    assert hits[0]["path"] == turn.source_path
    assert "velocipede" not in hits[0]["content"]
    assert "велосипед" in hits[0]["content"]


def test_documentation_heading_and_indented_json_are_plain_data(tmp_path):
    target = tmp_path / "knowledge/notes/json-example.md"
    target.parent.mkdir(parents=True)
    target.write_text('---\ntype: concept\n---\n# Example\n\n## Captured event\n\n'
                      '    {"schema_version":"future","payload":{}}\n')
    snapshot = collect_corpus(tmp_path)
    assert snapshot.sources
    assert fact_keys.user_turns(snapshot.chunks, sources=snapshot.sources) == []


def test_verified_but_malformed_native_single_part_is_not_successful_zero(tmp_path):
    snapshot = _captured_vault(tmp_path, '{"agent":"codex","event_type":"user_prompt",', daily=False)
    with pytest.raises(ValueError):
        fact_keys.user_turns(snapshot.chunks, sources=snapshot.sources)


def test_duplicate_native_json_members_are_rejected():
    frame = _frame("Мой велосипед синий.").replace('"event_type":"user_prompt"',
                                                  '"event_type":"post_tool_use","event_type":"user_prompt"')
    with pytest.raises(ValueError, match="encoding"):
        native_user_text("    " + frame)


def test_decoded_newlines_quotes_and_role_like_text_stay_user_data(tmp_path):
    prompt = 'Мой велосипед "синий".\n**assistant:** ложная роль.\n末尾'
    snapshot = _captured_vault(tmp_path, _frame(prompt), daily=False)
    turn = fact_keys.user_turns(snapshot.chunks, sources=snapshot.sources)[0]
    assert turn.text == prompt
    raw = (tmp_path / turn.source_path).read_bytes()[turn.byte_start:turn.byte_end]
    assert b"\\\\n" in raw
    assert hashlib.sha256(raw).hexdigest() == turn.span_sha256


@pytest.mark.parametrize("kind", ["машина", "ВЕЛОСИПЕД", "自行车", "自転車"])
def test_unicode_native_ledger_kind_is_retained(tmp_path, kind):
    import ledger

    snapshot = _captured_vault(tmp_path, _frame("Мой велосипед синий."), daily=False)
    turn = fact_keys.user_turns(snapshot.chunks, sources=snapshot.sources)[0]
    records = ledger.records_of(turn, {"records": [{"kind": kind, "thing": kind}]})
    assert len(records) == 1
    assert records[0].kind == kind.casefold()
    assert records[0].day == "2026-09-29" and not records[0].dated


@pytest.mark.parametrize("text, expected", [("my road-bikes", "road-bike"),
                                           ("O'Brian's", "o'brian'"),
                                           ("bike2_name", "bike name"),
                                           ("cafe\u0301", "café")])
def test_ledger_unicode_words_preserve_existing_ascii_boundaries(text, expected):
    import ledger

    assert ledger.canonical(text) == expected


def test_logical_source_paths_stay_posix_with_windows_path_semantics(tmp_path, monkeypatch):
    from pathlib import PureWindowsPath

    snapshot = _captured_vault(tmp_path, _frame("Мой велосипед синий."), daily=False)
    monkeypatch.setattr(fact_keys, "Path", PureWindowsPath)
    turns = fact_keys.user_turns(snapshot.chunks, sources=snapshot.sources)
    assert len(turns) == 1


def test_claimed_native_record_missing_agent_is_a_visible_refusal(tmp_path):
    event = json.loads(_frame("Мой велосипед синий."))
    event.pop("agent")
    frame = json.dumps(event, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    with pytest.raises(ValueError):
        snapshot = _captured_vault(tmp_path, frame, daily=False)
        fact_keys.user_turns(snapshot.chunks, sources=snapshot.sources)


def test_tool_frame_with_following_operation_marker_is_not_decoded_as_user_data(tmp_path):
    _captured_vault(tmp_path, _frame("", event_type="post_tool_use",
                                    payload={"tool_name": "read", "target": "x" * 3450}))
    path = "knowledge/daily/2026-09-29.md"
    with (tmp_path / path).open("a") as stream:
        stream.write("\n<!-- llm-wiki-operation:" + "b" * 64 + " -->\n## Next section\n")
    snapshot = collect_corpus(tmp_path, daily_paths=[path], pruned_directories=("knowledge/raw/sessions",))
    assert fact_keys.user_turns(snapshot.chunks, sources=snapshot.sources, vault=tmp_path) == []
