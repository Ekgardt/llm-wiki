# A context answer sends its text once

Date: 2026-09-26. Audit 2026-09-26 B-18.

## Facts

- `mcp_server._context_result` returned the packed `text` and then every item
  again, with its text, in `pages`/`symbols`, again in `decisions`/`incidents`/
  `active_task`, and again in `evidence`: about four times the answer the budget
  was computed for.
- `context_budget._partition` sorted mandatory items by `(priority class,
  item_id)`; `item_id` is a hash, so the sections of one page came out scrambled.
- The token budget is counted as UTF-8 bytes when no tokenizer adapter is
  configured (`_byte_estimate`, source `estimated`). That over-counts, never under-
  counts, so the answer always fits; it is kept and now stated in the guide.
- Anthropic's context-window documentation
  (https://platform.claude.com/docs/en/build-with-claude/context-windows, fetched
  2026-09-26): "Everything in the request counts toward the context window: the
  system prompt, every message in `messages` (including tool results …)", and "As
  token count grows, accuracy and recall degrade". A tool result that repeats its
  text is paid for, and read, each time.

## Decision

- The packed text is sent once, in `text`; every list names its items without
  their `text` field.
- Within a priority class mandatory items keep the caller's order.
- `docs/USER-GUIDE.md` describes `get_context` and `read_page` as they now behave.

## Files

- `scripts/mcp_server.py`
- `scripts/context_budget.py`
- `docs/USER-GUIDE.md`
- `tests/test_a_context_answer_sends_its_text_once.py`
- `CHANGELOG.md`
