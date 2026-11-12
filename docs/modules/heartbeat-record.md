# Helper for OpenCode plugin: record a no-content heartbeat in state.json

`scripts/heartbeat_record.py` and the tests that exercise them most.

Reads JSON from stdin: {"slug": "...", "projectRoot": "...", "reason": "...", "sessionId": "..."} Updates $LLM_WIKI_STATE_ROOT/run/state.json under `codex_heartbeats` (sharing the key with codex_memory.py — same semantic, different source).

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.
