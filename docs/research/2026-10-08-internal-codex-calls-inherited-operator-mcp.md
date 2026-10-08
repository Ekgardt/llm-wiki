# Internal Codex calls inherited the operator's MCP servers

Research date: 2026-10-08. Native qualification: Codex CLI 0.160.0.

The automatic memory compiler started a Codex child that started another LLM
Wiki MCP server. The additional server used approximately 2.1 GiB of resident
memory. Disabling lifecycle hooks and using a neutral working directory did
not prevent MCP startup. Passing an empty `mcp_servers` object did not help:
Codex merges configuration overrides with the existing configuration.

Internal memory calls already receive their evidence in a protected packet.
They now disable every configured MCP server for that call. Model discovery
reads the effective configuration before starting its ephemeral thread and
passes explicit `enabled = false` overrides. The retained planning basis owns
the server names along with its existing executable, model and configuration
evidence. Calls without a planning basis obtain names through the native
`mcp list --json` command. Invalid discovery fails rather than inventing an
empty list. Discovery and model execution share the existing call deadline.
Internal execution is ephemeral and disables hooks, apps and plugins. The
operator's configuration, interactive tools and evidence packet are preserved.
This change does not claim that every built-in Codex tool is disabled.

Alternatives considered: empty-map overrides are ineffective; changing the
operator's configuration would affect normal sessions; a second `CODEX_HOME`
would add unnecessary credentials and configuration ownership; a prompt asking
the model not to use tools does not stop server startup. Per-call overrides
address the observed cause without adding a runtime root or MCP tool.

Independent primary sources:

- [OpenAI Codex MCP configuration](https://learn.chatgpt.com/docs/extend/mcp?surface=cli)
  and [configuration reference](https://learn.chatgpt.com/docs/config-file/config-reference):
  per-server enablement and configuration overrides.
- [MCP tools specification, 2025-11-25](https://modelcontextprotocol.io/specification/2025-11-25/server/tools):
  client-controlled tool capabilities.
- [OWASP LLM prompt injection prevention](https://cheatsheetseries.owasp.org/cheatsheets/LLM_Prompt_Injection_Prevention_Cheat_Sheet.html):
  least privilege and separating untrusted content processing from tool access.

Regression evidence includes a test of the configuration passed before native
thread startup and a real CLI test with two independently named MCP servers.
The CLI test failed on the original dispatch because both servers remained
enabled; it passes after the change and checks that the configuration file
remains byte-for-byte unchanged. Malformed metadata is also rejected.
The final related local set passed 1,066 tests across 58 provider-related files.
Timeout tests cover both discovery and model execution, including an exhausted
discovery deadline stopping the provider chain. Actual AST and Lizard analysis
passed all 33 changed or new callables with maximum complexity 4.

An actual short protected model call returned the expected answer. That is
transport evidence only. Full compilation quality, total token efficiency,
installed generation completion and the overall audit remain unqualified.
