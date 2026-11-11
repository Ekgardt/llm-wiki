# Provider transport keeps the approved destination

Date: 2026-09-29. Status: reproduced; repair qualification pending.

The existing DLP boundary approves a provider descriptor and its normalized
endpoint. Verified local-only mode additionally requires a literal loopback
Ollama endpoint and verifiable cloud disablement. Python's default HTTP opener
can route a request somewhere else after that decision.

## Reproduction and scope

On the installed Python 3.12.3 runtime, two controlled loopback HTTP servers
reproduced both paths through the real `llm_client._opencode_healthy` caller:

- An HTTP 302 from the configured server caused a request to a second origin
  with the synthetic Authorization header. The health caller returned true.
- With `http_proxy` pointing at the other test server and no proxy bypass,
  the configured literal-loopback server received no request. The proxy received
  the synthetic Authorization header and supplied the accepted health answer.

Only non-secret test credentials and a fixed health response were used. These
observations do not establish that a real credential has been exposed. The same
default opener is used for provider calls, discovery and session cleanup, and
`choose_model` also uses it. A redirect can change POST to GET: the reproduction
proves credential forwarding, not forwarding of a POST body on every status.

Source evidence: `scripts/llm_client.py`, `scripts/choose_model.py`, their Codebase
Memory call graph, and the installed `HTTPRedirectHandler.redirect_request`.
Probe records: `/tmp/repair-provider-redirect-observation.json` and
`/tmp/repair-provider-proxy-observation.json` (to be preserved with qualification).

## Current primary research

Checked on 2026-09-29; these are three independent primary sources:

1. [OWASP SSRF prevention](https://cheatsheetseries.owasp.org/cheatsheets/Server_Side_Request_Forgery_Prevention_Cheat_Sheet.html)
   recommends disabling automatic redirects to prevent bypassing destination
   validation. Endpoint validation alone does not constrain subsequent hops.
2. [Python urllib.request](https://docs.python.org/3.12/library/urllib.request.html)
   documents automatic proxy discovery, the redirect handler, and its override
   interface. The documentation currently describes 3.12.14; the deployed 3.12.3
   implementation was inspected separately. The required opener/handler APIs are
   available on the project's Python 3.10 floor.
3. [HTTP Semantics, RFC 9110 §15.4](https://httpwg.org/specs/rfc9110.html#status.3xx)
   describes redirect method changes and removal of origin/resource-specific
   headers, including Authorization, when following redirects. A redirect is
   an application decision, not required transparent behavior for this client.

## Selected repair and alternatives

Use one provider HTTP-opening function in the existing client module. Refuse
automatic redirects before issuing another request. Route literal loopback
addresses directly with an explicit empty proxy map. Preserve environment proxy
behavior for remote endpoints, default TLS verification and caller timeouts.
Use this function for health/model discovery, inference and session cleanup,
including model listing. Do not install a global urllib opener that would change
unrelated download clients. Close a rejected response before raising its named
HTTP error. Existing provider failure reporting and durable retry remain in force.

Stripping Authorization alone does not keep verified-local requests local.
Allowing same-origin redirects still changes the approved operation/path and can
change its method. Following redirects with per-hop reauthorization requires a
new provider contract and is not justified by any supported caller here. Disabling
all environment proxies would unnecessarily break intentionally proxied remote
providers. Raising a hop limit would leave the first unsafe hop possible.

The redirect restriction follows the destination/security contract, not an
arbitrary request-count cap. A configured reverse proxy that relies on a redirect
must expose the final API URL directly; errors must be reported rather than
silently treated as successful provider calls. Revisit this choice only if an
explicit provider contract requires redirects and every hop can be independently
authorized. This is not an OS network sandbox or a DNS-rebinding defense. No new
dependency, database, daemon, runtime path or provider configuration key is added.

## Required qualification

Real loopback-server tests must cover GET/POST redirect statuses, model listing,
no second-origin request or credential delivery, direct successful calls and
literal-loopback proxy bypass. Verify remote proxy configuration is preserved,
existing provider/DLP tests pass, and actual complexity remains within the laws.
The installed nightly pass is running separately; run focused checks from an
isolated source copy to keep its knowledge writes out of pytest's isolation guard.
Do not run the full suite or another heavy index alongside the live compile.
