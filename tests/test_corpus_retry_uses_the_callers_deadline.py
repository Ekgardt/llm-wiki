"""A moving source may settle after four passes; no torn view is accepted."""
import corpus_snapshot
import pytest


def test_a_source_that_settles_after_four_passes_is_captured(tmp_path, monkeypatch):
    root = tmp_path / "vault"
    page = root / "knowledge/notes/page.md"
    page.parent.mkdir(parents=True)
    page.write_text("# Original\n", encoding="utf-8")
    original = corpus_snapshot._Capture.add
    edits = []

    def edit_after_capture(capture, candidate):
        original(capture, candidate)
        if len(edits) < 4:
            edits.append(len(edits))
            page.write_text(f"# Revision {len(edits)}\n", encoding="utf-8")

    monkeypatch.setattr(corpus_snapshot._Capture, "add", edit_after_capture)
    snapshot = corpus_snapshot.collect_corpus(root, deadline_seconds=5)
    assert len(edits) == 4
    assert len(snapshot.sources) == 1
    assert snapshot.sources[0].content == page.read_bytes()


@pytest.mark.parametrize("option", ["deadline", "deadline_seconds"])
@pytest.mark.parametrize("value", [float("nan"), float("inf"), -float("inf")])
def test_a_nonfinite_deadline_cannot_allow_unbounded_retries(tmp_path, option, value):
    with pytest.raises(ValueError):
        corpus_snapshot.collect_corpus(tmp_path, **{option: value})


def test_a_continuously_moving_source_stops_at_the_same_deadline(tmp_path, monkeypatch):
    now = [0.0]
    calls = []

    def changing_capture(*args):
        calls.append(args[2])
        now[0] += 1.0
        raise corpus_snapshot.CorpusChanged("source moved")

    monkeypatch.setattr(corpus_snapshot.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(corpus_snapshot, "_capture", changing_capture)
    with pytest.raises(TimeoutError, match="deadline"):
        corpus_snapshot._captured_after_retries(tmp_path, None, 6.0, None)
    assert calls == [6.0] * 6


def test_a_cancelled_retry_stops_before_another_capture(tmp_path, monkeypatch):
    calls = []

    def changing_capture(*args):
        calls.append(args[2])
        raise corpus_snapshot.CorpusChanged("source moved")

    monkeypatch.setattr(corpus_snapshot, "_capture", changing_capture)
    with pytest.raises(TimeoutError, match="cancelled"):
        corpus_snapshot._captured_after_retries(
            tmp_path, None, corpus_snapshot.time.monotonic() + 5, lambda: bool(calls)
        )
    assert len(calls) == 1
