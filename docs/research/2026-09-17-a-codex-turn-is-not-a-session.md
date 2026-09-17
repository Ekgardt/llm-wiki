# A Codex turn is not a session

Dated 2026-09-17. Finding C-F2 of the third audit (medium, reproduced). The research before
the fix.

## What was found

- `codex_memory.py` maps the Codex `Stop` hook to the shared `session_end` event, with the
  turn id as the event id. Codex fires `Stop` when a turn ends, so a session of thirty turns
  is "ended" thirty times.
- Each of those ends publishes a capture intent of up to 900 KiB holding the whole session so
  far, asks the classifier about it, rewrites the session record, and writes a `session-end`
  line in the daily log. Two `Stop` payloads of one session with turn ids `t1` and `t2` and
  the same transcript give two different intents (reproduced by the audit; the identity
  includes the event id, so nothing can fold them).
- Thirty classifier calls over overlapping text for one session is the opposite of the token
  economy the owner's rule 4 asks for, and the daily log fills with duplicates.
- A failure inside the Codex hook prints `hook skipped` and leaves no line in the
  capture-failure trail, unlike the adapter's own entry point.

## Practice on this date

- The host's own description of the two events
  ([Codex hooks](https://learn.chatgpt.com/docs/hooks), fetched 2026-09-17): `Stop` carries
  a `turn_id` and a `stop_hook_active` field described as "Whether this turn was already
  continued by `Stop`", and
  `SessionEnd` "runs for the main thread when you archive or delete a conversation that's
  still open, when Codex closes normally, or after a conversation has been idle and isn't
  open in any connected client for 30 minutes". So the host itself treats thirty idle
  minutes as the end of a session, and treats `Stop` as the end of a turn.
- Claude Code draws the same line: `Stop` fires "When Claude finishes responding",
  `SessionEnd` "When a session terminates"
  ([Claude Code hooks reference](https://code.claude.com/docs/en/hooks), fetched 2026-09-17).
  Our Claude configuration already sends `Stop` to the shared `stop` event, which captures
  nothing.
- The same page bounds that event: "`SessionEnd` and `Interrupt` use 1 second by default and
  support up to 3 seconds". Our capture starts a Python process through `uv`, reads a
  transcript and publishes a durable record; the shipped budget for it is 15 seconds. Three
  seconds is not a budget a capture can be promised in, so `SessionEnd` cannot simply replace
  `Stop` as the capture signal.
- `SessionEnd` is also newer than the Codex CLI this integration was reviewed against (0.153.4,
  `docs/research/2026-09-09-native-codex-hooks-and-handshake.md`), and no Codex is installed
  on the machine this fix was written on. Registering an event an installed host may not
  know, in a file the installer and doctor own, is not something to do unverified. It is left
  to the owner as a follow-up; the fix below does not depend on it and stays correct once it
  is registered.
- The usual shape for "many signals, one action" without a daemon is a debounce with a
  leading edge and a trailing edge: act on the first signal, stay quiet for a window, and act
  once more when the signals stop. With no timer of our own the trailing edge is taken by the
  next hook that runs after the window.

## The decision

- A `Stop` is a capture only when this session has not been captured in the last 30 minutes —
  the host's own idle figure. The first turn of a session is captured at once, so a
  one-question session is not delayed.
- Otherwise the `Stop` is the shared `stop` event, exactly as from Claude, and the session is
  remembered as having an uncaptured tail: session id, transcript path, working directory,
  turn id and the time.
- Every Codex hook, of any session, picks up at most one tail that has been quiet for 30
  minutes and captures it. So the end of a session is captured by the next Codex activity
  rather than never.
- A `PreCompact` capture counts as a capture of its session.
- The bookkeeping is one bounded map in `run/state.json` (`codex_turn_end_captures`, the 64
  most recent sessions). If the state lock cannot be taken the turn is captured, as before: a
  duplicate is cheaper than a loss. If the capture itself fails the claim is put back.
- A failure of the Codex hook is written to the capture-failure trail as `codex_hook`.

Files: `scripts/codex_memory.py`, `tests/test_a_codex_turn_is_not_a_session.py`
