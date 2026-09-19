# The evidence fits its record, and the record says what was dropped

Dated 2026-09-17. Findings C-F3 and C-F4 of the third audit (medium and low-medium, both
reproduced again before the fix). The research before the fix.

## What was found

- The transcript a capture carries is bounded at 900 KiB of raw text
  (`MAX_CAPTURE_EVIDENCE_BYTES`). The capture intent that holds it is bounded at 1 MiB *after*
  it is encoded as JSON. The headroom is 13.8 %.
- A transcript is JSONL. Put inside a JSON string, every `"`, every `\` and every line break
  in it becomes two bytes. The audit measured a growth of up to 1.23 on real transcripts of
  this machine; code-heavy text goes higher. Reproduced here: 861 600 bytes of ordinary
  assistant text with quotes and backslashes raise
  `ValueError: capture intent exceeds its byte limit`, and the whole capture is recorded as
  lost. These are the long sessions `PreCompact` exists for.
- When a transcript is over the bound the adapter keeps its head and its tail and writes
  between them a sentence saying how many bytes were not captured. That sentence is plain
  text. `session_evidence.render_transcript` drops every line that is not JSON once any line
  decoded, so the sentence never reaches the stored record, which then reads as if it were
  whole. Reproduced: the marker is in the excerpt and not in the rendered record.
- The classifier prompt then tells the model "the stored record is complete".

## Practice on this date

- The growth is the format's, not ours: "All Unicode characters may be placed within the
  quotation marks, except for the characters that MUST be escaped: quotation mark, reverse
  solidus, and the control characters (U+0000 through U+001F)"
  ([RFC 8259, section 7](https://www.rfc-editor.org/rfc/rfc8259#section-7)). A bound on raw
  text is therefore never a bound on the encoded record; only measuring the encoded record
  is.
- The standard remedy for "fit a payload into an envelope whose overhead depends on the
  payload" is to measure and retry with a smaller payload, scaled by the measured ratio, a
  bounded number of times. The intent's canonical encoding writes non-ASCII text as UTF-8
  (`ensure_ascii=False`), so the ratio stays below 2 for text and a few attempts are enough.
- A marker that must survive a parser should be written in the parser's own format. A JSONL
  reader skips what is not JSON; a JSONL line it knows is kept.

## The decision

- The adapter builds the record, measures its encoded size, and when it is over the limit
  reads the transcript again with a smaller evidence bound — the old bound scaled by the
  measured growth, less a tenth — up to four times. The excerpt is still head and tail on
  whole lines. Only after that does it raise the old error.
- The "not captured" marker becomes one JSONL line, `{"type": "capture_gap", …}`, that
  carries the same sentence. `session_evidence` owns its format and renders it as that
  sentence, in a JSONL transcript and in a plain-text one alike.
- The classifier prompt no longer claims the stored record is complete.

Files: `scripts/integration_adapter.py`, `scripts/session_evidence.py`,
`scripts/flush_memory.py`, `tests/test_the_evidence_fits_its_record.py`
