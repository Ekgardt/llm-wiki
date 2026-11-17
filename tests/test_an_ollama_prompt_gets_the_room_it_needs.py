"""An Ollama call carries a window that holds its prompt, or fails out loud.

Ollama's OpenAI-compatible endpoint cannot set a context size, and a prompt
longer than the loaded window is cut with only a server-side warning. The calls
here go to a stub Ollama on loopback over real HTTP. See
docs/research/2026-09-27-an-ollama-prompt-gets-the-room-it-needs.md.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import llm_client
import pytest

# An answer budget of the size a compile critique asks for; any positive value
# exercises the same arithmetic, this one keeps the window sums readable.
MAX_TOKENS = 500
# ~20 KB of Cyrillic: far past Ollama's default 4 096-token window.
LONG_PROMPT = "\u043a\u043e\u043d\u0442\u0435\u043a\u0441\u0442 " * 1200


class _StubOllama(BaseHTTPRequestHandler):
    """Answers `/api/show` and `/api/chat` as the server object is told to."""

    def do_POST(self):  # noqa: N802 - the stdlib names the handler
        length = int(self.headers["Content-Length"])
        body = json.loads(self.rfile.read(length))
        self.server.requests.append((self.path, body))
        answer = self.server.answers.get(self.path)
        payload = json.dumps(answer if answer is not None else {"error": "not found"})
        self.send_response(200 if answer is not None else 404)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(payload.encode("utf-8"))

    def log_message(self, *args):
        return None


@pytest.fixture
def stub(monkeypatch):
    server = ThreadingHTTPServer(("127.0.0.1", 0), _StubOllama)
    server.requests = []
    server.answers = {}
    # A short poll only so `shutdown` returns quickly; the default 0.5 s per test
    # was most of this file's run time.
    thread = threading.Thread(target=server.serve_forever, args=(0.02,), daemon=True)
    thread.start()
    monkeypatch.setenv("MEMORY_LLM_BASE_URL", f"http://127.0.0.1:{server.server_port}/v1")
    monkeypatch.setenv("MEMORY_LLM_MODEL", "stub-model")
    monkeypatch.delenv("MEMORY_OLLAMA_MAX_CONTEXT", raising=False)
    yield server
    server.shutdown()
    server.server_close()


def _chat_answer(prompt_tokens: int) -> dict:
    return {
        "message": {"role": "assistant", "content": "answer"},
        "prompt_eval_count": prompt_tokens,
        "eval_count": 7,
        "total_duration": 3_000_000,
        "done_reason": "stop",
    }


def _call(prompt: str = LONG_PROMPT):
    descriptor = llm_client.provider_candidates("ollama", max_tokens=MAX_TOKENS)[0]
    return llm_client.call_candidate(
        descriptor, prompt, "system", available=True, max_tokens=MAX_TOKENS
    )


def _chat_request(stub) -> dict:
    bodies = [body for path, body in stub.requests if path == "/api/chat"]
    assert len(bodies) == 1
    return bodies[0]


def test_a_long_prompt_is_sent_with_a_window_that_holds_it(stub):
    stub.answers["/api/show"] = {"model_info": {"qwen3.context_length": 40960}}
    stub.answers["/api/chat"] = _chat_answer(prompt_tokens=2400)

    result = _call()

    request = _chat_request(stub)
    prompt_bytes = len(LONG_PROMPT.encode("utf-8"))
    assert (result.text, result.failure_class) == ("answer", None)
    assert request["options"]["num_ctx"] >= prompt_bytes + MAX_TOKENS
    assert request["options"]["num_predict"] == MAX_TOKENS
    assert (result.usage.input_tokens, result.usage.output_tokens) == (2400, 7)


def test_a_prompt_that_filled_a_capped_window_fails_out_loud(stub):
    stub.answers["/api/show"] = {"model_info": {"qwen3.context_length": 4096}}
    stub.answers["/api/chat"] = _chat_answer(prompt_tokens=4096)

    result = _call()

    assert _chat_request(stub)["options"]["num_ctx"] == 4096
    assert (result.text, result.failure_class) == (None, "context_overflow")


def test_the_operator_ceiling_caps_the_window(stub, monkeypatch):
    monkeypatch.setenv("MEMORY_OLLAMA_MAX_CONTEXT", "8192")
    stub.answers["/api/chat"] = _chat_answer(prompt_tokens=1200)

    result = _call()

    assert _chat_request(stub)["options"]["num_ctx"] == 8192
    assert result.text == "answer"


def test_a_malformed_ceiling_refuses_the_candidate(stub, monkeypatch):
    monkeypatch.setenv("MEMORY_OLLAMA_MAX_CONTEXT", "big")

    descriptor = llm_client.provider_candidates("ollama", max_tokens=MAX_TOKENS)[0]

    assert descriptor.resolution_failure == "invalid_configuration"


def test_a_short_prompt_keeps_the_default_window_and_the_schema_goes_as_format(stub):
    stub.answers["/api/chat"] = _chat_answer(prompt_tokens=20)
    schema = {"type": "object", "properties": {"x": {"type": "string"}}}
    descriptor = llm_client.provider_candidates("ollama", max_tokens=MAX_TOKENS)[0]

    llm_client.call_candidate(
        descriptor, "short", "", available=True, max_tokens=MAX_TOKENS, schema=schema
    )

    request = _chat_request(stub)
    assert (request["options"]["num_ctx"], request["format"]) == (4096, schema)
