# Processor error codes must obey the stored byte ceiling

Date: 2026-11-03. Reproduced on cf3709e8 before queue-worker extraction.
This is a pre-existing defect, not a claimed benefit of moving definitions.

## Reproduction and cause

processor_error_code uses a Python string slice of 64 characters. The real v3
attempt_history CHECK measures length(CAST(error_code AS BLOB)), i.e. UTF-8 bytes.
An exception type named 'Ошибка' repeated twenty times yields a 64-character,
111-byte code. The real candidate v3 queue accepts/leases an ordinary query but
queue.fail raises IntegrityError and leaves it leased. Evidence:
logs/queue-error-unicode-baseline.json (isolated SQLite constraint),
logs/queue-error-unicode-real-queue.json (actual candidate queue API).

## Intended separate correction

After the exact-body extraction is qualified, pass the existing character-bounded
generic code to the shared strict _utf8_prefix helper extracted from the renderer.
The initial character bound avoids an unbounded encoded copy; the helper then
clips to the same existing byte ceiling at a complete Unicode scalar boundary.
All ordinary ASCII codes remain identical. Explicit QueueOperationError codes
keep their separate existing contract and are not silently rewritten.

The schema ceiling, retry decisions and queue state transitions do not change.
Regression must first fail on the old implementation, then verify byte-bound,
valid UTF-8, unchanged ASCII/security cases and the real v3 fail transition from
leased to dead with a stored error. Include two/three/four-byte code points.
CPython rejects surrogate-containing names at type construction, so no hypothetical
backslash-replacement policy is added. Strict invalid-Unicode rejection remains.
Recheck capture/worker consumers and actual complexity.

## Current primary sources / alternatives

Checked 2026-11-03, three independent publishers:
- IETF RFC 3629 defines variable-length UTF-8 and character boundaries. A Python
  code-point count is not an encoded byte count.
  https://www.rfc-editor.org/rfc/rfc3629.txt
- Python codecs documents strict decoding and encoding errors. The existing
  helper encodes valid text first and removes only an incomplete trailing scalar
  from the bounded prefix, keeping the original strict input policy.
  https://docs.python.org/3/library/codecs.html#codecs.IncrementalDecoder.decode
- SQLite CREATE TABLE documents CHECK enforcement; the existing project schema
  explicitly casts to BLOB for its bound. Increasing it would change a database
  contract and leave the underlying mismatch elsewhere.
  https://www.sqlite.org/lang_createtable.html#check_constraints

Rejected: catch IntegrityError and report success (loses failure settlement),
raise the schema limit (changes contract), truncate UTF-8 blindly (invalid text),
replace every Unicode name with a generic reason (unnecessary loss of diagnostic
identity). Correction is pending; neither transfer nor documentation fixes it.

## Shared primitive and related-case evidence

code_navigation_renderer._utf8_prefix already performs strict boundary clipping.
Its exact-body extraction keeps the renderer alias and strict input policy; reuse
avoids a duplicate incremental-decoder algorithm or reverse renderer import.
Related normal type names produced 111, 158 and 205 bytes for two-, three- and
four-byte characters. A surrogate name failed at type construction before the
converter ran. Evidence: logs/queue-error-unicode-related-baseline.json.
The correction is pending and is not claimed as part of the exact-body transfer.
