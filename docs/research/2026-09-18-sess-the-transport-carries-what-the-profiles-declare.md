# The transport carries what the profiles declare

Dated 2026-09-18. Finding K-B16 of the third audit (2026-09-17). The research before the fix.
The owner delegated the decision.

Files: scripts/lsp_protocol.py, scripts/lsp_profiles.py, scripts/pyright_session.py,
tests/test_the_typescript_engine_confirms_itself_after_initialize.py,
tests/test_language_server_wiring.py, tests/test_lsp_protocol.py

## What was found

`lsp_protocol.SERVER_NOTIFICATIONS` was a literal set holding Pyright's three vendor
notifications plus the two neutral ones. The registry has four profiles, and three of them
declare notifications that were not on it:

| profile | declares | reached the session |
|---|---|---|
| pyright | `pyright/{begin,end,report}Progress` | yes |
| typescript | `$/typescriptVersion` | no |
| gopls | `window/showMessage`, `window/logMessage` | no |
| rust-analyzer | `window/{showMessage,logMessage}`, `experimental/serverStatus` | no |

`lsp_profiles.server_notification_union()` -- written for exactly this, with a docstring
saying so -- had one caller, a strict-xfail test. `pyright_session._add_identity_handler`
registered a handler for `$/typescriptVersion` and carried a comment saying it was never
reached. `_record_server_identity` wrote the result into `_progress_events`, which no
production code reads. So TypeScript's post-initialize identity confirmation existed as
three connected pieces and confirmed nothing.

## Practice on this date

- The notification is real and is what the check needs: typescript-language-server sends
  `$/typescriptVersion` with `{version, source}` after initialization, where `source` is one
  of `bundled`, `user-setting` or `workspace`, so that a client can tell which TypeScript
  the server actually loaded (typescript-language-server, `docs/configuration.md` and
  `src/tsServer/*`; the repository is <https://github.com/typescript-language-server/typescript-language-server>,
  and the profile in `scripts/lsp_profiles.py` already pins `required_source="user-setting"`
  with a comment explaining why anything else means another engine).
- The protocol says an unknown notification must simply be ignored: "Notification messages
  should be handled ... by the receiver. If a notification is not handled, it should be
  ignored." and, for methods starting with `$/`, "if the receiving end doesn't support the
  method ... it must not fail with an error and simply ignore it." (LSP 3.17, Base Protocol,
  <https://github.com/microsoft/language-server-protocol/blob/gh-pages/_specifications/lsp/3.17/specification.md>.)
  So an allowlist that drops a method is spec-compliant -- it is just not the behaviour this
  product wants, because the dropped method is the one carrying the evidence.

## The decision

1. **The transport's allowlist is derived, not listed.** `SERVER_NOTIFICATIONS` is
   `lsp_profiles.server_notification_union()`. Adding a profile to the registry teaches the
   transport its notifications with it, which is what the function was written for and what
   its one test asserted. The alternative -- a second literal set kept in step by a test --
   duplicates the source of truth for no gain.
2. **Being on the list is still not enough to reach anything.** `_dispatch_known_notification`
   already required a handler registered by the session, and a session registers only its own
   profile's methods (`_progress_methods` plus `_add_identity_handler`). So a Pyright server
   sending `window/showMessage` is admitted by the transport and then dropped for want of a
   handler. The one behaviour lost is the "unknown notification" warning for a method that
   belongs to some other profile; a method no profile declares still warns.
3. **What the confirmation finds is now reported.** A server whose `source` is not the pinned
   one adds `<profile>_server_identity_unconfirmed` to the session's degradation codes.
   Readiness is left alone -- the session still answers -- but the caller is told the answers
   come from an engine we did not verify. Without this the handler would still write only to
   `_progress_events`, which nothing reads, and the work would be unfinished in a new place.
4. **The layering cost is named.** `lsp_protocol` now imports `lsp_profiles`. Nothing in the
   profile chain (`lsp_profiles` -> `pyright_profile` -> `lsp_process_tree`, `bounded_io`,
   `lsp_paths`, `reliable_memory`, `repository_scope`) imports `lsp_protocol`, so there is no
   cycle. The transport still has no per-message knowledge of language: it holds one
   allowlist, as before, and only its contents now come from one place instead of two.

## What this costs

One module import at startup. No per-message cost: the allowlist is a frozenset built once.
