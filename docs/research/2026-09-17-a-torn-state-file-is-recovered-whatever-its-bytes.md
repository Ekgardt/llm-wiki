# A torn state file is recovered, whatever its bytes

Dated 2026-09-17. Finding Q-M18 of the third audit (medium, reproduced), with the two small
leftovers of the same module (Q-L6, stale module docstring). The research before the fix.

Files: scripts/memory_state.py, tests/test_a_torn_state_file_is_recovered_whatever_its_bytes.py

## What was found

- `update_state` promises (2026-09-14) that an unreadable `run/state.json` is never replaced
  by an empty one: the writer recovers `state.json.previous` or writes nothing.
- The recovery path `_recovered_previous` first calls `load_state()` to keep the forensic
  copy. `load_state` catches only `json.JSONDecodeError`. A torn write is not polite enough
  to be valid UTF-8: the read raises `UnicodeDecodeError`, which escapes, so there is no
  recovery and no forensic copy, and every writer fails until someone repairs the file by
  hand. Both exceptions are siblings under `ValueError`; the json documentation says of
  `JSONDecodeError` only that it is a "Subclass of ValueError"
  (https://docs.python.org/3/library/json.html#json.JSONDecodeError), and a decoding
  failure of the text is a different subclass raised before the parser runs.
- `load_state` returns whatever the JSON holds. A file containing `[]` hands a list to
  twenty callers that call `.get` on it (`session_start_context`, `mcp_server`,
  `compile_memory`, `flush_memory`, ...). `_parsed_state`, written for the update path,
  already refuses a non-dict; the reader does not.
- Code graph: `load_state` has 17 production callers; `_recovered_previous` is one.

## The decision

- One definition of "readable": valid UTF-8, valid JSON, an object. `load_state` and
  `_parsed_state` both use it. Anything else is corrupt: the reader keeps the forensic copy,
  logs the line and returns `{}` as before; the writer recovers the previous version.
- An `OSError` while reading keeps its meaning in the reader (it still propagates); only
  the content judgement is shared.
- L6 (a dead owner's lock holds writers for 30 s while they give up after 10 s) is left: the
  age is what protects a lock that was just created and whose PID is not written yet, so
  judging the owner earlier needs a different lock file format. It is named in the report.
