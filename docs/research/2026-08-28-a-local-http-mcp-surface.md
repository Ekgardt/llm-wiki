# A local HTTP MCP surface, and what makes one safe

Date: 2026-08-28. Backlog item `OPS-01`
(`docs/DEVELOPER-AUDIT-STATUS-2026-08-18.md` §12, "Эксплуатация"):
*"`OPS-01` HTTP MCP (одобрен контрактом) — многоагентный доступ без stdio."*
`CLAUDE.md` names "HTTP MCP" among the approved superset targets. This is the
research that has to exist before a second transport is added to the answer
path.

## The gap this closes, in this vault's own numbers

The owner runs many agents at once on one machine. Under stdio every agent
launches its own MCP subprocess, and every one of them pays the same cold
start. This vault already measured that cost twice, for other reasons:

| number | where it came from | when |
|---|---|---|
| cold encoder 7.99 s, ~1108 MiB resident (23.9 → 1131.6 MiB) | `knowledge/log.md`, cost of the warmup decision | 2026-08-26 |
| owner accepts the warmup so the first `recall` has a dense leg | `mcp_server._start_encoder_warmup` docstring | 2026-08-27 |

The warmup decision was taken per *server*. With one server that is a good
trade. With one server per agent it is the same 1.1 GiB and the same 8 s, once
per agent, for a model whose weights are identical in every copy. Nothing about
the tool surface changes if those agents share a process; only the transport
has to.

## What the transport must be

Not invented here. MCP defines exactly two standard transports, and the
non-stdio one is **Streamable HTTP**. The installed SDK is `mcp` 1.29.0 and
reports `mcp.types.LATEST_PROTOCOL_VERSION == "2025-11-25"`, so **2025-11-25**
is the revision implemented against, and the framing is the SDK's own
`StreamableHTTPSessionManager` rather than a hand-rolled JSON-RPC-over-HTTP.

Read today, first-hand
(`https://modelcontextprotocol.io/specification/2025-11-25/basic/transports`),
the parts this implementation is bound by:

- "The server **MUST** provide a single HTTP endpoint path … that supports both
  POST and GET methods." → one route, `/mcp`, methods `GET`/`POST`/`DELETE`.
- "If the input is a JSON-RPC *request*, the server **MUST** either return
  `Content-Type: text/event-stream` … or `application/json`". The SDK's
  `json_response=True` takes the second branch; both are conformant, and JSON
  is the cheaper one for a tool call that returns a single envelope.
- Session management: the server **MAY** assign an `MCP-Session-Id` at
  initialization, and clients **MUST** then echo it. Kept (SDK default), because
  many agents on one server is exactly the case sessions exist for.
- "If the server receives a request with an invalid or unsupported
  `MCP-Protocol-Version`, it **MUST** respond with `400 Bad Request`." The SDK
  enforces this; nothing here overrides it.

Also read, and deliberately **not** implemented against: the `2026-07-28`
revision, which removes the initialize handshake and the session id and adds
`Mcp-Method`/`Mcp-Name` routing headers. The installed SDK does not speak it,
and a transport that disagrees with its own SDK is worse than one revision
behind.

## The security model, and why loopback is not it

The spec's own Security Warning, verbatim:

> 1. Servers **MUST** validate the `Origin` header on all incoming connections
>    to prevent DNS rebinding attacks. If the `Origin` header is present and
>    invalid, servers **MUST** respond with HTTP 403 Forbidden.
> 2. When running locally, servers **SHOULD** bind only to localhost
>    (127.0.0.1) rather than all network interfaces (0.0.0.0).
> 3. Servers **SHOULD** implement proper authentication for all connections.

These are not hypothetical. Both halves have been shipped wrong by
first-party SDKs inside the last five months:

| advisory | what was missing | impact |
|---|---|---|
| **CVE-2026-42559** (`GHSA-89vp-x53w-74fx`, rmcp / Rust SDK, fixed 1.4.0, 2026-04-09) | no `Host` validation on the Streamable HTTP server transport | CVSS **8.8**; a malicious page could "enumerate and invoke any tool exposed by a locally-running rmcp-based MCP server" |
| **CVE-2026-63118** (Ruby SDK) | Streamable HTTP transport lacked DNS-rebinding (Host/Origin) protection | same shape |

The attack is worth stating plainly, because it is the reason a *loopback*
server needs an `Origin` check at all: a page the owner visits controls a
domain whose DNS it can rebind to `127.0.0.1`. The browser then sends the
page's requests to our port. The port being loopback stops nothing — the
browser *is* on the loopback interface.

### Decision 1 — bind literal loopback, and refuse rather than warn

`0.0.0.0` publishes a private vault to the network, so this fails closed:
`require_loopback_host` accepts `127.0.0.1` and `::1` and raises on anything
else, including `localhost`. A name is refused because a name is resolved, and
what resolves it (`/etc/hosts`, a resolver, a search domain) is not ours. This
is the same line `llm_client._is_literal_loopback_endpoint` already draws for
verified local-only Ollama; the vault should not have two definitions of
loopback.

### Decision 2 — no `Origin` is ever accepted

The spec says validate `Origin`; it does not say against what. The usual answer
is an allowlist. Here the correct allowlist is **empty**, and that is stronger
than a populated one: this server returns JSON to MCP clients and serves no
page, so there exists no legitimate browsing context that could produce an
`Origin` for it. A real MCP client (Claude Code, OpenCode, Codex) sends none.
A browser always sends one on a cross-origin request. So "present" is a
sufficient test, and the refusal is 403 exactly as the spec requires.

This is enforced twice on purpose. The SDK's own
`TransportSecurityMiddleware` runs with `allowed_origins=[]` inside the
transport, and this module's `LoopbackGuard` runs in front of it. Measured, by
removing each layer in turn (below): each one alone is sufficient for `Origin`
and `Host`, and neither is sufficient for authentication.

### Decision 3 — a bearer token in a 0600 file, never on a command line

"Loopback is not authentication" is the whole point: on a shared machine every
process of every user can open `127.0.0.1:8765`. The spec's authorization
document says authorization is "**OPTIONAL** for MCP implementations", that
"implementations using an HTTP-based transport **SHOULD** conform to this
specification" — OAuth 2.1, RFC 9728 protected-resource metadata, RFC 8707
resource indicators, PKCE — and that stdio implementations "**SHOULD NOT**
follow this specification, and instead retrieve credentials from the
environment."

Standing up an OAuth 2.1 authorization server for a single-user loopback
process is not proportionate, and it is not what comparable local tools do.
The precedent taken instead is **Jupyter Server's token model**: a token
generated at startup, required on every request, with authentication on by
default. Two things are copied from Jupyter's own security guidance, including
its scars:

- The token is **never** an argument. Jupyter's documented local-exposure
  problem is exactly this: when a token is passed on the command line, other
  users on the system can read it out of the process list — on Linux
  `/proc/<pid>/cmdline` is world-readable, whereas `/proc/<pid>/environ` and a
  0600 file are not. So the token lives in
  `<state root>/run/mcp-http/token`, mode 0600, in a 0700 directory, and the
  server prints the *path*, never the value.
- A token file that other users can read is refused outright, not warned
  about. A secret with mode 0644 is not a secret.

`hmac.compare_digest` does the comparison, so a wrong token costs the same time
as a right one.

Order matters and is deliberate: `Origin` is judged **before** the token, so an
unauthenticated browser gets 403 ("you are a browser") rather than 401 ("try a
credential"). Telling a rebinding attacker that a credential would help is
free help.

## What was deliberately not built

- **OAuth 2.1 / RFC 9728 / dynamic client registration.** Disproportionate for
  one user on one machine, and it would add a discovery surface, a metadata
  endpoint and a token lifecycle — more attack surface than it removes. If this
  server ever leaves loopback, that decision has to be revisited *first*; the
  bind refusal is what makes that a deliberate act rather than a flag.
- **A daemon, a scheduler entry, an installer resource.** Nothing here is
  installed. `CLAUDE.md` is explicit that the product has no persistent daemon,
  and `run/install` ownership is a separate contract. This is a command the
  owner runs.
- **TLS.** On loopback it protects against nothing the token does not, and a
  self-signed certificate would train every client to skip verification.
- **Any new tool, argument, or write path.** The surface is the same twelve
  tools reached through the same `mcp_server.build_server()`. HTTP does not add
  a capability that stdio lacks — in particular it adds no network-reachable
  write path, because `log_decision` and `compile` were already on the stdio
  surface and are reached through the same validated dispatch.
- **Rate limiting and concurrency admission.** The existing
  `MCP_WORKER_SLOTS = 4` bound and the per-operation deadline apply unchanged,
  because they live in `_execute_tool_call`, not in the transport. What is *not*
  measured is what happens when many agents contend for those four slots at
  once; see "what remains".

## The one structural risk, and how it is answered

A second transport is a second chance to build a second dispatch path. This
vault has already paid for that once: commit `4494d8c` was a verification that
bypassed the boundary it was supposed to check.

The answer is that there is no second path. `mcp_server.build_server()` is the
single construction point; `run_server()` (stdio) and `mcp_http.build_app()`
both call it and neither registers a handler of its own. Every tool call over
HTTP therefore lands in the same `_register_tools` callback → `_tool_call_data`
→ `_validate_tool_arguments` → `_dispatch_tool` → `_build_operation_envelope`,
under the same `_tool_operation_seconds` deadline.

That claim is not left to inspection. `tests/test_mcp_http.py` takes the
envelope for the same call over both transports and asserts they are equal
except for the fields that are a clock (`generated_at`, `answer_cost`), and
asserts that a schema-invalid argument is refused over HTTP by
`_validate_tool_arguments`'s own message rather than by the SDK's validator.

## Measurements

Taken on this machine (4 vCPU, 16 GiB, load average 5-7 from other agents),
with `LLM_WIKI_STATE_ROOT` pointed at a temporary directory, so nothing in
this section touched the live `run/`. Harness: `/tmp/ops01_measure.py`. "ready" is time until
`initialize` is answered; "warm" is time until resident memory stops growing,
which is when the encoder is loaded and the first semantic question would not
fall back to lexical.

### Resident memory: the whole point

| agents | N stdio servers | one HTTP server |
|---|---|---|
| 1 | 1230.2 MiB | 1219.7 MiB |
| 2 | 2449.1 MiB | 1219.4 MiB |
| 3 | 3670.7 MiB | 1219.9 MiB |
| 6 | not run - it would need ~7.3 GiB, and this machine has 16 | 1219.8 MiB |

The marginal agent costs **1220.3 MiB** under stdio and **0.1 MiB** over HTTP.
That is not a percentage saving, it is a different shape: stdio is linear in
agents, HTTP is flat. On a 16 GiB machine stdio runs out of memory at about
twelve agents; the shared server does not.

### Cold start: what the second agent waits for

The number an agent actually feels is *launch to first answered tool call*,
three rounds each:

| | measured |
|---|---|
| stdio, brand-new server process | 2.942, 2.481, 1.276 s - and the encoder is still loading behind it |
| HTTP, new session on a warm server | 0.013, 0.011, 0.010 s |
| HTTP server warmup, paid **once** | 11.73 s |

So the shared server pays 11.73 s once, and every agent after that joins in
about **11 ms** - two orders of magnitude faster than launching a process, and
without the 8-14 s during which a freshly launched stdio server answers
`recall` lexically because its encoder is not resident yet.

Time until resident memory stops growing (the encoder is loaded), for
completeness: stdio 10.89 / 11.98 / 13.57 s for N = 1 / 2 / 3, each agent
paying its own; HTTP 14.24 / 12.41 / 11.17 / 18.08 s for 1 / 2 / 3 / 6
sessions - one payment regardless of how many agents arrive.

### Per-call latency: the honest cost

Twenty warm `wiki_overview` calls, median / p95 / min in ms, on a machine with
load average 5-7 from other agents:

| | stdio | HTTP |
|---|---|---|
| 1 agent | 7.0 / 12.6 / 6.6 | 29.1 / 35.7 / 14.3 |
| 2 agents | 13.8 / 19.8 / 8.0 | 15.6 / 18.2 / 7.6 |
| 3 agents | 14.3 / 18.9 / 6.9 | 16.0 / 44.2 / 14.1 |
| 6 agents | - | 17.0 / 25.0 / 14.1 |

**HTTP costs roughly 8-22 ms more per call than stdio.** Said plainly: a pipe
write is cheaper than an HTTP round trip through an ASGI stack, and no
transport choice makes that untrue. Whether it matters depends entirely on
what the call does - it is 1-3× the cost of `wiki_overview`, which is the
cheapest tool there is, and under 1% of a `recall` with a dense leg, which
this vault has measured at 1.26-2.7 s and, cold, at 8.8 s. The spread between
the rows is larger than the effect being measured, so these medians are worth
one significant figure and no more.

The trade is therefore explicit: **HTTP trades ~15 ms per call for ~1.2 GiB
and ~11 s per agent.** For one agent, stdio wins. From two agents up, it is
not close.

Method note, because it bit: `subprocess.kill()` on `uv run` kills the wrapper
and leaves the server. The first measurement run leaked six servers, consumed
7 GiB, and produced numbers distorted by its own leak (one 401 from a stale
listener, an RSS reading of 111 MiB taken mid-load). The harness now starts
each server in its own session and kills the process group; the numbers above
are from the clean run. The earlier, discarded run is named here rather than
quietly dropped.

### Live, on the real vault: the two transports paired

The question that matters is not "does HTTP work" but "does it answer the same
as stdio". Three rounds, three tools, both transports interleaved so neither
gets the quiet half of the machine, against the live vault after 45 s of
warmup (load average 6-8):

| round | tool | stdio | HTTP |
|---|---|---|---|
| 1 | `wiki_overview` | 0.08 s, ok | 0.07 s, ok |
| 1 | `get_decisions` | 10.30 s, `operation_timeout` | 12.04 s, `operation_timeout` |
| 1 | `recall` | 8.46 s, ok | 9.94 s, ok |
| 2 | `wiki_overview` | 0.12 s, ok | 0.17 s, ok |
| 2 | `get_decisions` | 9.09 s, `list[5]` | 7.96 s, `list[5]` |
| 2 | `recall` | 10.22 s, `operation_timeout` | 10.01 s, `operation_timeout` |
| 3 | `wiki_overview` | 0.21 s, ok | 0.30 s, ok |
| 3 | `get_decisions` | 8.43 s, `list[5]` | 8.88 s, `list[5]` |
| 3 | `recall` | 10.10 s, `operation_timeout` | 8.46 s, ok |

Two things are worth saying plainly. First, the data keys agree everywhere
(`_meta, results, retrieval_trace` for `recall`, `list[5]` for
`get_decisions`), so the envelope is the envelope. Second, `recall` and
`get_decisions` sit right on this vault's `MCP_OPERATION_SECONDS = 10` ceiling
on a loaded machine, and **they time out over stdio too** - round 3 has stdio
timing out where HTTP succeeded. A first pass measured only the HTTP side and
looked like the transport had broken `recall`; running the same call over the
unchanged stdio path is what showed it had not. That the ceiling is this tight
on real questions is a pre-existing finding, not this change's, and it is not
fixed here.

One side effect of running these live calls, named because it is a write:
the timeouts above incremented the product's own `capture_failures.mcp_tool`
counter in `run/state.json` (`mcp.recall: TimeoutError: retrieval deadline
exceeded`). No knowledge page, transaction or queue entry was touched, and the
counter increments identically whichever transport the call arrives on - but
it is a write to live runtime state and the owner should know it happened.
By `self-resolving-health-findings-decision` it falls out of the seven-day
window on its own; `--clear` removes it deliberately.

## What remains

- **Not documented for a user.** The agent-configuration snippet lives only in
  the module docstring. `README.md`, `README.ru.md`, `README.zh-CN.md` and
  `docs/` say nothing about an HTTP transport, and this change deliberately did
  not touch them - they are outside the files this task was scoped to, and the
  three READMEs must be edited together by contract.
- **Not proven against a real agent host.** The protocol is driven here by
  `httpx` and by the SDK's own session manager, conforming to 2025-11-25. That
  Claude Code, OpenCode or Codex will connect to it with this configuration is
  *inferred from the spec, not observed*. That is the first thing to check
  before the owner relies on it.
- **Not measured: contention.** `MCP_WORKER_SLOTS = 4` and the per-operation
  deadline are unchanged and shared, so six agents asking for `recall` at once
  now contend inside one process where before they had a process each. Nothing
  here measures what that does to p95. It is the one place where the shared
  server can be *worse* than stdio, and it is unmeasured.
- **Not implemented: session cleanup.** `StreamableHTTPSessionManager` accepts
  a `session_idle_timeout`; this passes none, so it keeps the SDK default. An
  agent that dies without sending `DELETE` leaves its session behind.
- **Windows.** `_private_mode_is_enforceable()` returns False off POSIX, the
  0600 check is skipped and the server says so on startup. On Windows the token
  file is only as private as its directory. Untested there - this machine is
  Linux.
- **`run/mcp-http/token` is new state under `run/`.** It is not evidence and
  blocks nothing in the `run/` deletion contract, which is correct; the
  consequence is that deleting `run/` invalidates every agent's configured
  token and a new one is minted on next start.
- **No installer, no scheduler entry, no daemon** - by design, so starting the
  server is the owner's explicit act. It also means nothing restarts it.
