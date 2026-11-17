# MCP benchmark output retains Unicode on non-UTF-8 hosts

Date:2026-11-04. Status: locally reproduced and repaired; native verification pending.

Windows full shard3 of run41804515315 at55eb6f95 failed the finite-server Unicode
case: expected answered, got error.4358other cases passed and35were skipped.
Two independent encoding boundaries were reproduced locally with
PYTHONIOENCODING=cp1252. The original finite server failed to emit its Cyrillic
reply, reproducing the native error status. After making the server's protocol
streams explicitly UTF-8, the unchanged benchmark CLI failed printing that valid
answer with UnicodeEncodeError. A separate probe executes the exact Git937f07a8
client source against the repaired server and retains its original source SHA and
traceback. This distinguishes the fixture problem from the real CLI output bug.

MCP framing was already binary UTF-8 and is unchanged. Configure the CLI's stdout
as UTF-8 at the __main__ entrypoint; this covers both JSON results and plain tool listings.
The finite fixture now explicitly configures stdin/stdout, validates a Unicode
request and emits Unicode answers/tool descriptions. New real subprocess cases
force cp1252 defaults and require successful, intact results. No global CI UTF-8
override, weakened assertions, replacement-error handling or skipped Unicode case
is used. Library call_tool/list_tools behavior and signatures stay unchanged.
Only the CLI entrypoint configures stdout; programmatic calls retain caller-owned
streams. Current Python3.14 CLI streams support reconfigure.

Alternatives: ASCII-escaping JSON alone leaves Unicode plain listings broken;
forcing an environment variable only in tests masks the CLI defect; changing the
already-correct binary protocol adds unnecessary risk. Explicit endpoint encoding
matches the existing UTF-8 subprocess reader and preserves decoded values.
The known ignored timeout, unbounded readline and direct-child-only cleanup remain
unresolved; this repair does not certify them or the separate Windows parent exit.

Primary sources checked2026-11-04:

- [MCP2025-06-18 transport](https://modelcontextprotocol.io/specification/2025-06-18/basic/transports)
  requires UTF-8 JSON-RPC messages and newline-delimited stdio frames.
- [CPython3.14 standard streams](https://docs.python.org/3.14/library/sys.html#sys.stdout)
  documents environment/default-dependent encodings; the executable used here is3.14.7.
- [Microsoft code pages](https://learn.microsoft.com/en-us/windows/win32/intl/code-pages)
  explains platform code-page variation and recommends Unicode for new applications.

Evidence: native job126488115631; logs/mcp-stdio-cp1252-red-server.txt,
logs/mcp-stdio-cp1252-red-client.txt, logs/mcp-stdio-utf8-baseline-probe.json,
logs/mcp-stdio-encoding-graph.json and logs/mcp-stdio-utf8-weigh.txt. Native CI must
confirm the repair; a local non-UTF-8 reproduction is not a Windows success claim.
