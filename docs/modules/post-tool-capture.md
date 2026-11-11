# PostToolUse compatibility entrypoint for complete, durable, non-LLM capture

`scripts/post_tool_capture.py`, `scripts/user_prompt_capture.py` and the tests that exercise them most.

The common adapter persists the redacted occurrence before asynchronous journal delivery. No direct append, text truncation, tool filter or time suppression lives here. Historical --background invocations keep using this same ingress.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.
