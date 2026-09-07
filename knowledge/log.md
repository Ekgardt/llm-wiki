# Session Memory Log

- 2026-04-12 — Initialized session-memory subsystem.
- 2026-04-13 — First compile pass over `knowledge/daily/2026-04-13.md`. Created `mirror-existing-pipelines.md`, `audit-current-vs-intended.md`, `flag-inferred-content-as-preliminary.md`, `edit-multiple-matches.md`. Rebuilt `knowledge/index.md`.
- 2026-04-13 — Populated `knowledge/notes/` per session-memory-review findings. Added `editorial-notes-pattern.md`, `provenance-rule-6.md`, `pipeline-mirroring.md` — the three recurring vault principles identified in the daily log but not yet named. Linked `provenance-rule-6` reciprocally with existing `[[Preliminary Flagging]]` (wiki concept). Rebuilt `knowledge/index.md`.
- 2026-04-13 — Upgraded `scripts/rebuild_memory_index.py` to emit full `knowledge/notes/<slug>` wikilinks and a one-line hook per entry (extracted from each page's `One-sentence summary:` line, with an H1-follow fallback). Added an `## Editorial note` footer to the generated index. Regenerated `knowledge/index.md`.
- 2026-04-13 — (collapsed 4 consecutive "Touched: (none reported)" compile-pass entries — pure runtime heartbeat, no knowledge recorded. Bug fixed 2026-04-14: `compile_memory.py` now only writes to this log when a pass actually touched a knowledge page; no-op compiles are recorded in `state.json` only.)
- 2026-04-13 — Automation hardening (Priority-2, coleam00-inspired): (a) expanded `scripts/lint_memory.py` from 6 memory-only checks to 7 vault-wide checks — now covers both `memory/` and `knowledge/notes/` via `--scope`, word-based sparse floor (default 200, configurable via `--sparse-words`), LLM-judged contradictions opt-in via `--contradictions`, with backlink-exempt and broken-link-skip lists to suppress structural false positives; (b) introduced `FLUSH_OK` sentinel in `scripts/flush_memory.py` — summarizer now returns that literal token when nothing is worth persisting, and `flush_memory` skips the daily-log append entirely (empty logs beat noisy logs); (c) added `CLAUDE_INVOKED_BY` recursion guard to `scripts/postcompact_capture.py` (session-end and pre-compact already had it); (d) verified SHA-256 incremental compile and 18:00 auto-compile were already implemented in `compile_memory.py` / `flush_memory.py` — documented in `docs/AGENTS.md`.
- 2026-04-13 — Added first real Q&A entry `inbox-vs-raw-after-compile.md` (post-compile file location). Rebuilt `knowledge/index.md` — Q&A section now populated.
- 2026-04-13 — Hygiene pass: removed 15 hook-testing/empty-transcript session entries (16:04–17:13) from `daily/2026-04-13.md`; added `## Promoted to wiki` marker to `editorial-notes-pattern.md` (promoted to [[knowledge/notes/editorial-notes-pattern|Editorial Notes Pattern]]); dedupe — rewrote `pipeline-mirroring.md` as a redirect stub pointing to `mirror-existing-pipelines.md` (canonical per patterns>concepts tiebreaker). Rebuilt `knowledge/index.md`.
- 2026-04-14 — Content expansion pass on 4 memory pages (stub `pipeline-mirroring` kept as stub but explained; `edit-multiple-matches` got Variants+Prevention; `audit-current-vs-intended` got "why dating matters" + checklist; `mirror-existing-pipelines` got "when NOT to apply" + failure-mode section). Combined with 15 backlink additions, lint now reports 0 findings across all 7 checks.
- 2026-04-14 — Post-audit cleanup: cleared `flush_dedupe` of test/unknown/auto-* keys in `state.json`; bumped `compiled_daily_hashes['2026-04-13.md']` to current hash (hygiene pass removed test entries, no new material to lift). See `knowledge/log.md` for the full 5-point seam-closure pass.
- 2026-04-14 — Priority-4 scale prep: three-tier `/knowledge-lookup` strategy (DIRECT/HYBRID/QMD) with `scripts/lookup_mode.py` helper. QMD freshness check uses index-file mtime rather than `qmd status` CLI — survives the Windows cmd.exe/Git-Bash PATH split.
- 2026-04-14 — Priority-3 feature pass: added two new skills (`knowledge-qa-file-back`, `contradict-check`); rewrote `README.md` with full pipeline diagram; converted `docs/operating-model.md` `memory/↔knowledge/notes/` boundary rule into an explicit two-question table + worked examples. No changes to `knowledge/notes/` pages themselves.
- 2026-04-18 — Recorded decision `no-gitkeep-in-inbox-articles.md`: skip `.gitkeep` in `inbox/articles/` — scripts create the directory on demand at first use. Updated `knowledge/index.md`.
- 2026-04-18 — Fixed `scripts/memory_state.py` to resolve memory paths from a stable root so hooks write to one canonical location regardless of worktree. Cleaned up stale worktrees and added `scripts/cleanup_worktrees.py` for periodic hygiene. Recorded decision `centralized-memory-subsystem.md`. Updated `knowledge/index.md`.
- 2026-04-18 — Manual compile pass over 2026-04-18 daily log (private). No durable content to lift (runtime heartbeat).
- 2026-07-03 — Phase 0-4 build pass (manual). compile_memory.py rewritten with VERIFY-BEFORE-WRITE + COMPILE_AUDIT + rich title/summary snapshot. flush_memory.py upgraded to 3-tier FLUSH_MAJOR/MINOR/OK classifier. codex_memory.py now writes state.json heartbeat instead of polluting daily logs with stub blocks. lint_memory.py gained 5 OKF checks (8-12). session_start_context.py injects metacognitive knowledge-state block. New scripts: migrate_to_okf.py (53 pages migrated to OKF v0.1), user_prompt_capture.py + post_tool_capture.py (non-LLM lifecycle taggers), cognee_sync.py (graceful Cognee bridge). New skill: crystallize-playbook. New bridge: OpenCode plugin. New tests: +33. Test count 37 -> 70. CLAUDE.md rules 11-16 added.
- 2026-07-13 — Recorded the agent-native interface decision: MCP is the common read/action protocol, native integrations are thin lifecycle-event adapters, and health is delivered to agents without a human dashboard. Added the approved design and first-stage implementation plan.
- 2026-07-13 — Recorded the approved Stage 2 reliable-memory architecture after current-practice research: recoverable Markdown transactions, project checkpoint journals, manifest-verified archives, content-addressed compile plans, a SQLite rollback-journal priority queue, and evidence-backed atomic claims.
- 2026-07-15 — Completed Stage 2 reliable-memory implementation and repaired reciprocal links among its public architecture decisions. Clarified that Obsidian remains an optional viewer outside the MCP and lifecycle-adapter boundary.
- 2026-07-17 — Recorded the public decision that evidence retrieval uses disposable immutable cache generations while Markdown, Git, and project journals remain authoritative.
- 2026-07-19 — Recorded the approved product contract: LLM Wiki becomes one local-first memory, code-intelligence, and agent-control system for a single operator managing many agents and sessions, with Graphify and codebase-memory-mcp workflow replacement proven by paired benchmarks rather than feature counts.
- 2026-07-21 — Recorded the approved persistent code-intelligence kernel target while preserving corpus-generation/v2 and Evidence Graph v2 as the current implemented checkpoint until Plan A passes.
- 2026-07-22 — Superseded the one-shot consent/SCIP/publication direction after completed foundation Tasks 1-5. Approved an owned read-only LSP navigation engine, starting with production-quality Python/Pyright while retaining the existing structural Evidence Graph and 12-tool MCP boundary.
- 2026-07-23 — Recorded the bounded mutable LSP live-lease contract: `lease.json` heartbeats every 10 seconds, expires after 30 seconds, remains separate from immutable owner/failure evidence, and is removed after controlled lifecycle cleanup.
- 2026-07-24 — Qualified LSP process containment by platform: Windows Job Objects own assigned trees, POSIX process groups cover pinned Pyright descendants only while they remain in-group, and hostile `setsid()` escape remains unsupported.
- 2026-08-05 — Recorded the proposed exact V4 reliability contract after approval of its direction and implementation base; production implementation remains blocked on final architecture sign-off.
- 2026-08-05 — Activated the reviewed V4 reliability contract after the user delegated exact architecture decisions based on current-source research; implementation may proceed through TDD gates.
- 2026-08-12 — Recorded explicit approval to implement the Reliability V3 runtime database pair and offline adoption backend required by installer repair; Markdown authority, runtime roots, and the 12-tool MCP surface remain unchanged.
- 2026-08-14 — Recorded the approved bounded sibling-preimage contract for Claude and Codex configuration merges: byte-exact verification, exact LLM-Wiki ownership prefixes, simultaneous 10-file/90-day/100-MiB retention, and preservation of the newest or sole restore point.
- 2026-08-15 — Recorded the approved audit-closure architecture: retire the unsupported Cognee bridge, add one fail-closed DLP and local-only boundary, coherent Restic recovery, transactional install state under `run/install/`, native schedulers, and reuse of the existing Reliability v3 databases and MCP surface.
- 2026-08-15 — Recorded explicit approval for fenced blackboard resource claims: two bounded tables in coordinator-v3, all-or-none immediate acquisition, renewable logical leases, expiry/reclaim, exact fencing, and authoritative append-only Markdown events.
- 2026-08-16 — Recorded the exact install ownership control plane: one bounded `run/install` manifest and resumable transaction for profile, user environment, native schedulers, verified owned-fragment preimages, fail-closed recovery, rollback, and uninstall.
- 2026-08-16 — Approved managed local Cursor and Antigravity hooks, structural fragment ownership in the existing install control plane, backward-compatible install v2 updates, and one retained committed-update rollback point.
- 2026-08-16 — Implemented the approved managed IDE hook boundary and install v2 lifecycle; Cursor and Antigravity now use canonical capture adapters, structural ownership, fail-closed drift handling, latest-update rollback, uninstall restoration, and Doctor diagnostics. Audit item `OPEN-010` is closed at code level; live and clean-machine evidence remains pending.
- 2026-08-17 — Rebound the frozen retrieval-v2 baseline to the five packages the benchmark loads instead of the byte digest of the whole `uv.lock`. Commit `350eec8` regenerated the lock when the Cognee bridge was retired; every frozen version was still locked exactly as recorded, but twelve benchmark tests failed on every platform for a condition no one could satisfy without re-measuring offline or editing frozen evidence. Recorded in `baseline-environment-binding-decision.md`.
- 2026-08-16 — Approved durable SessionEnd and PreCompact capture activation: the integration adapter publishes create-only Reliability V3 intent evidence before returning, detached work only wakes recovery, and source deletion requires immutable terminal proof.
- 2026-08-18 — Closed the three silent-loss paths from the developer audit: capture failures now leave a durable trace with a counter surfaced at session start (`OPEN-013`), maintenance step output streams to owner-only artifacts under age/count/size retention (`OPEN-040`), and the injected SessionStart payload is bounded by a hard character ceiling (`NEW-02`). Recorded in `observable-capture-and-bounded-maintenance-decision.md`.
- 2026-08-18 — Replaced the blanket symlink refusal in bounded reads with an ownership rule: an ancestor symlink is accepted only when root owns both it and the directory holding it. macOS reaches every temporary file through root's `/var` symlink, so the old rule refused to read them at all. Current traversal-resistant APIs draw the line at escape rather than at symlinks; recorded in `system-symlink-ancestor-decision.md` with the research in `docs/research/2026-08-18-traversal-resistant-reads.md`.

- 2026-08-19 — Raised the Pyright qualification gate `warm_overhead_p95_ms` from 20 to 30 ms after three consecutive four-vCPU hosted runs measured 22.80, 22.08, and 22.16 ms. The spread is under a millisecond, so the number is the machine class, not noise: the facade pays for its freshness guarantee with an extra workspace-revision walk. The gate still fails closed and now names the slowest supported machine. Recorded in `warm-navigation-overhead-threshold-decision.md`.

- 2026-08-19 — Closed audit item OPEN-036: the typed-provenance weights now live in one table that both the lexical and the hybrid paths import, and the weight multiplies the score that decides the order in fusion and after reranking. Every frozen retrieval-v2 metric and gate is unchanged; the behaviour is proved by unit tests. Recorded in `one-trust-weight-across-retrieval-paths-decision.md`.

- 2026-08-19 — Closed audit item OPEN-020: `purge --include-dead` retires attempts-exhausted tasks through the existing export-first, manifest-verified path, and `restore --export` brings the work back from a verified export as new ready tasks. Retention stays the default because a dead task records work that never happened. Recorded in `dead-task-retirement-and-restore-decision.md`.

- 2026-08-19 — Narrowed audit item OPEN-017: a cited span that shares no content token with the claim now fails the answer, covering unspaced scripts through character bigrams. Entailment is still not verified and is not claimed anywhere. Recorded in `citation-relevance-gate-decision.md`.

- 2026-08-19 — Built the measurement stand audit item OPEN-034 asked for: `benchmark/run_flush_classification.py` scores tier accuracy, durable-content recall, and the false-promotion rate against a labelled corpus, using the product's own classification prompt. The shipped corpus is nine public synthetic cases; the real number still needs an installed vault's sessions. Recorded in `classification-measurement-stand-decision.md`.
- 2026-08-22 — Automated compile completed for snapshot 783fe67f82a3729d3ff12becc8597270e77e3330e04792bb6a0938e982a6d6da, c69195057163e71b04a1b8782e9aa0957c79bd0a0d98f79485eb5b1b5ad27241, ec9934acd2a62c48127d0365fd1bb5ef48fd85397b150040b488994fb094d0a1. Touched: 1 unpublished page(s).
- 2026-08-22 — Automated compile completed for snapshot 4d8e37733171e3be6436220071a9bf9df31f69b6057a0b5ac1472352ffd01252, 7351e04bd497f375571874c15fc821f6c3e7281c20800ed79110dfdf541fe634, 8697a7f70bf53fbd8aa7e41347a6e208d7c62c679bc10fcc6dc13e661220c9f1, d8844d09a86ce9feef3e656eed8c73ce7016dc3d31724d0a4cbdee216201ec4a, e211cae6a6d2e218b0bc5ef18d597e24231b35edcec84ef2b29255ad93502e1e. Touched: none.
- 2026-08-24 — Automated compile completed for snapshot 2418a6e9496dc626189e6af5d5bda667a9ef39c7132c635ef197bdfec970bb53, 6b2268e15bceb07ab6e59af9f1dda4b72d21114af5eea0f6e106465fc4a9d14f, ce728dac4bb804b392167418aed908aaeebee7cf60ad9ae081fa340e641807e9, e35c664dfe0c6de527cfba9664c3a3afd8f806f88b7924cc2ae12f222e9cc142, f0ab6c5978289ffe84671cdd8467829d59906dbee05c798dcc09b5bdf6c242eb. Touched: none.
- 2026-08-24 — Manual compile completed for snapshot 1772e41a6e3b56ddc8d2f151a6def547be3c3101b237452b68a7aaed36383d37, 2b7f56e9e291034b7569bac8b292e61395869f755a696395828ebca68b5a2859, 2be87ea4546e735e07d5635db768f75a8e2aaafc3ed8e1ce305205a921637ec1, 3131a793387b243ffa953535d52ddf3909a5b2418ca68ecbe4bf11bddeed5436, c902db797302edc032a604208949f267489ec523a6ee30ea15f31d3fb30a28ce, d04232f3ce978800e9085ed772147d491a11850bed454504cb782369d5e44171, ffd32941b2010fe3be934693ce8b3210087f4cf1c98460747fe0e1970fdaae0c. Touched: 7 unpublished page(s).
- 2026-08-24 — Manual compile completed for snapshot 1772e41a6e3b56ddc8d2f151a6def547be3c3101b237452b68a7aaed36383d37, 2be87ea4546e735e07d5635db768f75a8e2aaafc3ed8e1ce305205a921637ec1, 65ec8722bcccae194d2c5db7859dc491ecabed42d9efc2948ca8a890674724cf, b212df10817c584acb265497fb1378c5098175eda4b010510cb6e0da33b3b08e, b6ffc5e4c867ccbe9c5a436af922d1da8eb365e5b8476dfafc20bba2e37143f5, c902db797302edc032a604208949f267489ec523a6ee30ea15f31d3fb30a28ce, d04232f3ce978800e9085ed772147d491a11850bed454504cb782369d5e44171, ffd32941b2010fe3be934693ce8b3210087f4cf1c98460747fe0e1970fdaae0c. Touched: 23 unpublished page(s).
- 2026-08-24 — Automated compile completed for snapshot 47881a98da0ef9b84996bf7c9be89058f25c63eb8526010a7879d2cf908c71cd. Touched: none.
- 2026-08-24 — Automated compile completed for snapshot 64bc19b653e9d13ed06f00f8cb19f31d12168fc6ae2a67acda04d5b941fa89d6, 8b47a50f9e326732cd8ff15a2c53632767f9c194e7b8db5e2db8b70b2b4edf1e, dfda08603d5fc7d6c02d8fbee7549b24fe0f557d15decf5b2a392e58b8296548. Touched: none.
- 2026-08-24 — Automated compile completed for snapshot 62f42fc4c292d0015f116d967311c56a9abb48f9b57272b1bed608cdc301a11d, e6789a0f9a12dc44511855beb9bea11b0504fd6fb1e3cf5b797c5567e9f21835. Touched: none.
- 2026-08-25 — Automated compile completed for snapshot 031acf543f18c3d050526e2f5c3d953dc6b01c0c8786bcd526642cdef7d94516, 3e9aecdb3a64c4b1dbef69b29909eef0a2d30fb7550e32e763d40b7b3a8182df, 4d6bd9cdd525e21e70c20a80b73bf0afec8a6d4f7682642498abc50e27416d87, 5b9b597b11ef52c824e0a9002e7e1d878036583555e8c6ec7d65aa7361717272, 6ce181f226d5b0bce1532550661aa63423f4d05d0588596e14e9265e31aa7258, 84faf97a4acf30e871d16cff9b553c5838bbf3847744758615912948b908d4bb. Touched: none.
- 2026-08-25 — Automated compile completed for snapshot 2653a48d07453ce7e95bdd882bc93332f800efbb52ac0e31c5a18098ddfdec5b, a1d6932a9d36055ad61fc02fa1832ee44a98ef4798443a747f3ef7afbb83ecef, f754d1c595350e75ce9a4097bb27f5680f07dd50ba69a5a80902f65b974df5db. Touched: none.
- 2026-08-25 — Automated compile completed for snapshot b4db57160c96e846441bfd95055ef52b638374252301ee78978da32757423b4c. Touched: none.
- 2026-08-25 — Automated compile completed for snapshot 00015eaa77f1eb43f8f1ec118043bbba3f0fdc2cf2c8a3ad43fcaffd98608dd3, 6d63fefb77f9f87f068f5cdde2caa371259fd8abb88fa995978bfe52a86788aa, e8ae7ec50589203d263c9b7b45a310f73377f840076114ae4ce19984940529b6, fc2197cdb90ec43a41871c63152b97fe3f941fa1125be19147564a7afc1c593d, fdd9b23981d415c544c2aa9e5dde16e1fd86778ef9757efc81bf03c1f52f35c6. Touched: none.
- 2026-08-25 — Automated compile completed for snapshot 396158222cb10973fadbf10c7e69a65e6598d200c4b4edcfecdb2f103dd2ad8e, 4f663066183ecf0ee5b29a54cd532776d9154c901612241523579823a6a3e368, 6409ee9665a14b168f6ef3166304a5a124a7ddae080a7d9b7bd6075c981b3d22, 8973e9f8e1cf651f382213c7ac6cdbee2d9697c5971315641d645f08b61d2970, bb72aca69184f38f1bb0aac7b4a78bef356eb72e37ce9ce2e68101b7e21fccc2. Touched: none.

- 2026-08-25 — Владелец делегировал решение по `OPEN-034`/`NEW-62`, и оно принято по правилу «всё должно работать без привлечения пользователя»: хранение сессии перестало быть суждением, а повышение до страницы решает ночная консолидация по всей записи. Классификатор конца сессии оставлен только для строки дневника. Смену выборки на «начало и конец» проверили замером на сорока настоящих сессиях — восемьдесят вызовов провайдера — и отвергли: оба окна дали 24 повышения из 40, две сессии поменяли уровень в разные стороны, а у проигравшей решения стояли в 31 814 знаках от конца, то есть внутри хвоста в 60 000 и вне половины в 30 000. Замер не опровергает `NEW-62`: тот считал захваченное долговременное содержание по размеченному корпусу, этот — уровень повышения. Неустойчивые метки перестали быть препятствием: число, которое они закрывали, больше ничего не решает. Страница — `session-promotion-policy-decision.md`, исследование — `docs/research/2026-08-25-what-the-vault-decides-to-remember.md`.
- 2026-08-26 — Automated compile completed for snapshot 066bffd1b8aec1751ace905c0ca387471d1e6bd29ed5604a60b7c9b4198b62b4, 40c40ef5404431e737ae2c7e9bdea47c8f60b6e580be2a83d2cfb74a771bf429, 4c9eedb7965f6609b6227bd5adc015b70df915788aa5dc609774b402cf28b1ba, 6d23f412e99cdc94066b7e2b710ce008be256e4545965b89c374f56d0d24c585, a858c88b58b1ed2af0c63a69a8121aea4db9472a9ff7b890777791e49b83eee6, acb8b8dc4feb61b38cd0c4926be5bb5561ee48a5d44437e8592fd7074c862c07, b6b86f9bcd11620fe2972d58e03d0d758146ef4d7958b91fc469ac53d55fde2a, e32a511b066d0e6d1deffcc73c9abb94e75863c533ae7db7d6fd9529ac2ee479, e912b858674a3cb766ee7b204b4e492bd9bcac7431d18c3bcce47a0192e7de32, e9516dc87f757f8d72d8a4ca76fcdb02842c5ed3876cd5014943ac9a988bac0f, f804dc018dd8fea36a9cd18604e7151d232f870f19e4ddc11ee47cb003b838ca. Touched: none.

- 2026-08-26 — Снятое накануне закрытие `CROSS-06` восстановлено с оговоркой. Я снял его, увидев падение на macOS и сочтя это повтором; проверка по именам показала другое: оба падения — `test_terminal_intent_lock_respects_deadline_and_cannot_commit_success` и `test_close_from_reader_handler_does_not_deadlock` — лежат в других тестах того же класса, а не в двух, которые пункт называет. Оба закрыты с измеренной причиной, второе — настоящий дефект порядка: `FakeLspServer.start` запускает поток обработчика до возврата, поэтому обработчик мог обратиться к переменной до её присваивания. Воспроизведено 19 раз из 300, после правки 0 из 20 при намеренно расширенном окне. Урок общий: «то же семейство» не значит «тот же пункт», и снимать закрытие нужно по именам, а не по сходству.

- 2026-08-26 — Cursor и Antigravity сняты с поддержки по решению владельца: он ими не пользуется, а платформа, которой никто не пользуется, стоит двух форматов управляемых хуков, двух проверок доктора, двух определений в установщике и двух проекций событий, чьим единственным доказательством были их собственные тесты. Остаются Claude Code, OpenCode и Codex. Одна вещь оставлена намеренно и названа прямо: путь удаления. Контрольная плоскость установки отказывает наглухо, если манифест называет ресурс, которого код больше не даёт (`install_resource_request_mismatch`), — значит удаление писателя целиком оставило бы наши handlers в `~/.cursor/hooks.json` или `~/.gemini/config/hooks.json` указывать на хранилище, которого нет, без всякого способа их забрать. Это тот же дефект, что закрыт для плагина OpenCode 2026-08-23. Поэтому читатели и писатели проекций обоих форматов живут дальше как `retired_cursor_hooks_resource` и `retired_antigravity_hooks_resource`: ни шаблона, ни desired-байтов, ни распознавателя, `write_owned` отказывает с `install_resource_retired`. Проверка — тест ставит фрагмент через историческую форму ресурса и забирает его текущим кодом, байты файла восстанавливаются точно; соседний тест фиксирует сам дефект, ради которого путь оставлен. Решение — `retire-cursor-and-antigravity-decision.md`, оно замещает `managed-ide-hooks-install-update-decision.md`. Заодно `scripts/event_envelope.py` разобран по правилу 5: три функции выше порога, включая построитель конверта сложности 19, приведены к нулю без изменения идентичности события — 24 функции, средняя 2.8, 42 теста зелёные.

- 2026-08-26 — Записано решение, которое приехало внутри починки и было названо починкой: при включённом переранжировщике строки, которые он оценил, идут впереди тех, которых он не читал. Сохранение хвоста пула — это правка дефекта, из-за которого одна страница занимала несколько мест в ответе; а вот куда девать хвост, нигде записано не было. Выбор сделан замером на зафиксированных пулах: слияние хвоста одним проходом убирает семь повторов, но стоит двух случаев на стенде применения, потому что неоценённые строки обгоняют оплаченные переранжировщиком; ярусный порядок убирает те же семь и не теряет ничего. Правило, которое молча решает порядок каждого ответа, должно читаться, а не выводиться из тела функции. Страница — `rerank-tier-ordering-decision.md`.
- 2026-08-26 — Manual compile completed for snapshot 24abb903bf7ac0a555232ed7bf28cf7dce99b13a7d82e392b6ee4a36e7c34185, 544305235ffdbdca2b60f6f82c93b594a8a6c13237e328a1d0a081cf0dbfe621, 7eea32b966591bfa7e0bc3478e483fd8cfb55c9383bfcd6a854a625fc73bebf7, d13de8ee5bf8cd57a4702205f9895eeae54d71267ddd2b4367e0dc3881601804. Touched: none.
- 2026-08-26 — Automated compile completed for snapshot 902e74a07bddd0701a56efebac3cf73acc2d9477b4420d57549ef3ca70963963, bc8b2017207ebbf908ba38a684b71b7aa207b3a6ecaa136857069257468eff9f. Touched: none.

- 2026-08-26 — Три отказа захвата, которые шли прямо сейчас, разобраны и
  исправлены. Первый: `adapter_post_tool_use` — 33 потери за день (счётчик рос
  во время самого разбора). Хост тут ни при чём: Claude Code кладёт в payload
  `PostToolUse` поле `tool_response`, то есть вывод самого инструмента, а адаптер
  из него не читает ничего — `_tool_payload` берёт `tool_name` и один путь или
  команду из `tool_input`. Предел в 64 КиБ отвергал любую правку или Bash с
  выводом крупнее. Предел поднят до 1 МиБ и остаётся пределом — хук не должен
  читать без него, — а отказ теперь называет сломанную границу вместо слова
  «invalid integration event», которое не называло ничего. Второй:
  `adapter_session_start` — `TypeError: can't subtract offset-naive and
  offset-aware datetimes`. `compile_memory._utc_now` пишет `last_compile_at` с
  суффиксом `Z`, то есть осведомлённую метку, а `session_start_context`
  вычитал её из наивного `datetime.now()`; значит каждый старт сессии на
  хранилище, которое хоть раз компилировалось, терял захват и не внедрял
  контекст. Взят тот же нормализатор, что уже стоит в `doctor._parse_utc`, а не
  второй способ: принять `Z`, читать метку без зоны как UTC, всегда отдавать
  осведомлённое значение. Третий записан отдельной страницей —
  `bounded-capture-excerpt-decision.md`. Проверка на живом хранилище парная, на
  настоящих входах: непропатченный адаптер даёт ровно те строки, что лежат в
  `run/state.json`, пропатченный на том же входе пишет строку дневника и
  фиксирует запись дедупликации; на живом транскрипте в 105 258 865 байт старый
  путь отвечает `capture transcript exceeds 921600 bytes`, новый оставляет
  482 287 байт с маркером на 104 776 691 пропущенный байт. Честный остаток:
  после снятия предела по размеру `pre_compact` упирается в
  `legacy_protocol_unquiesced` — отдельный, уже записанный блокиратор, который
  намеренно не запускался.
- 2026-08-26 — Сняты два дефекта, которые стояли между принятым переходом на Reliability V3 и хранилищем, которое после перехода продолжает работать. Живой переход снова не выполнен, и это отказ по измеренной причине, а не незавершённая работа.

  Первый дефект: запись перехода замораживает `sha256(scripts/integration_adapter.py)`, и `_require_adoption_sources` перепроверял её при **каждой** валидации. Через `require_reliability_v3_adopted` эта валидация стоит перед всем путём записи памяти — захват, очередь и любая markdown-транзакция, — поэтому первая же правка адаптера после перехода клала бы память целиком с `reliability_v3_record_invalid` и без объяснения. Ночное самообновление, разрешённое владельцем 2026-08-23, делает это неотвратимым и безнадзорным. Измерено на временном хранилище, принятом настоящей командой перехода: до правки «after producer update: REFUSED -> reliability_v3_record_invalid», после — «STILL VALID». Дайджест умеет говорить «другое», но не «несовместимое»; несовместимость, на которой надо отказывать закрыто, — это форма принятых баз, и она проверяется отдельно (`queue_schema_sha256`, `coordinator_schema_sha256`, `adoption_schema_sha256`) и проверяется по-прежнему. Оставлено связывание там, где оно решает: `_validate_migration_context` по-прежнему отвергает переход, план которого снят с других байтов адаптера, то есть окно между `--plan` и `--apply` закрыто; `_require_adoption_header` по-прежнему требует согласия двух записей. Взамен постоянной проверки байтов введена проверяемая: адаптер обязан существовать. Отвергнут вариант перезаписывать запись перехода после каждого обновления — запись create-only по контракту, а вторая, изменяемая запись с «текущим» дайджестом была бы числом, которое никто ни с чем не сравнивает. Страница — `adoption-digest-is-provenance-decision.md`.

  Второй дефект: после удавшегося перехода доктор объявлял обе базы нечитаемыми и запрещал удаление `run/`. Причина простая — легаси-пути после перехода это JSON-надгробия, а `_queue_check`, `_queue_v2_check`, `_transaction_check` и `_repair_queue_capabilities` открывали их как SQLite. Надгробие само называет замену, поэтому читатели теперь идут по нему: `adopted_database_path` возвращает путь замены, только если надгробие названо для этой базы и называет ровно её легаси- и активный путь; всё остальное — настоящая база v2, недописанная запись, нечитаемый файл — возвращает легаси-путь без изменений, чтобы непринятое или повреждённое хранилище сохранило прежний вердикт. Измерено: до правки `queue_state_unreadable: True`, `transaction_state_unreadable: True`; после — оба False.

  Проверки: `ruff check scripts/ tests/` — чисто; девять наборов, импортирующих затронутые модули, — 432 пройдено, 3 пропущено; `lizard -C 5` на трёх затронутых файлах — «No thresholds exceeded», ноль предупреждений (143 и 572 функции, средняя 2.6 и 3.0); управляемый гейт на `scripts/installed_memory_repair.py` был красным десятью находками `[STRUCTURE]`, не связанными с правкой, — все десять разобраны прежними приёмами (таблица предикатов в `_kind`, реестр отказов в `_require_database_health`, именованные подфункции в остальных), файл даёт ноль. Семь новых тестов в `tests/test_adoption_producer_and_tombstone_readers.py` строят принятое хранилище настоящей командой перехода, а не собранной руками записью.

  Живой переход не выполнен, причина измерена и названа: флаг `--confirm-all-agents-stopped` — утверждение о состоянии машины, и оно ложно. Во время работы в общем checkout `$LLM_WIKI_ROOT` шли три параллельных прогона pytest других агентов (pid 113807, 135393, 146780) плюс ожидающий их цикл. Переход превращает `run/queue.sqlite3` и `run/markdown-transactions.sqlite3` в надгробия; делать это, пока чужие процессы держат или откроют эти файлы, — тот же «наполовину принято», который репетиция уже показала 2026-08-26. Вторая причина: архив `$LLM_WIKI_ROOT-run-archive-2026-08-26/run` устарел — обе базы разошлись с живыми (`queue` 3f1eba58… против dbeca427…, `coordinator` a961e741… против 90c02431…), то есть отката к нему уже нет и перед переходом нужен свежий архив. Обе правки, которых требовал переход, лежат в ветке; сам переход ждёт тихой машины.
- 2026-08-26 — Manual compile completed for snapshot 92073cedcbcf5fe12136d3606e18dd0805b3a2af6641e329f2508e6afd419d07, 9c1ec4115e1d4fe44f851a2c75a2b4a2c3007df41cb20fd020d2c3220bc4059f, bfc3485a34f88e7ab72ae94456113460f49f60fefd93081613f1ea1884fda270. Touched: none.
- 2026-08-27 — Automated compile completed for snapshot 0b1c7a71f705c885e8c90799f1597435d40e4b6cee7f9e000beb9761d17a3bcc, 9765c68dffcfa912dd0f89c92eb0c271ce6812617596d4840df0e5aa9497329c. Touched: none.
- 2026-08-27 — Automated compile completed for snapshot 35aba03ddfdf238ed8b29f0aae297f1f09034b07e9f366d4a59dfda08fe5b00d. Touched: none.
- 2026-08-27 — Automated compile completed for snapshot 8eb195e363f58090294fae47d8f7e298972d16964b1276d5d3566ab928aa0266. Touched: none.
- 2026-08-27 — Automated compile completed for snapshot 0048dd4f0447dd900fdab1be4e936f2418fe7b87a45ce8a8278e647c92a6a7f7, 1508355d80b06af27987279eddc5ce26cd4ec4ec2433c1979c65a85aff079d2d, 796ab5b18297474256092c798e92ab833db232d1dc433246fee26f8d4c0b3a13, de17858042bea11b228b2d0964ab9c7c3700d5cef568c4cdc7471d2548cde57c. Touched: none.
- 2026-08-27 — Automated compile completed for snapshot e58ed24f4fe85c131a52449e59d14a1e7e45ecc160f0aa8b4bae8d705df39295. Touched: none.
- 2026-08-27 — Automated compile completed for snapshot 78fb7198b91aca394aa6a9145bba429a16fcc766c773156a3fbe669b5f6b9272, afed42bf48af66457db130e86a34374d4f81333219617717ce64c098ad0ef87e. Touched: none.
- 2026-08-27 — Automated compile completed for snapshot 93d28f486f439ce64841d82b36e6acfbc1574e765aa04b7e70b6e2af3235a78a. Touched: none.
- 2026-08-27 — Automated compile completed for snapshot 2557b0908c1e8ef746dee4591dc7845637e3af45f498ab8f5c9f48e7c1b0c194, 4bb271b58f5d569a4759ad1b88bd0cd82178e93299655b6920d673c5d4da8eb8, 5d4ef5ad153de4bfe42733fcc924d04db4b83de7289e9d8c0797a4bfff31b322, 9130f7f7b6f6c119eb44d88828ca5977acfe9eb88a43646f02ccefa280d8c150, b0c14188b15b8b243c14414d2344637befe82ae9ea44fc7980b2b04438c49a59, d359cb8b714910ad5070f45a00f0255c458da2980d5d84b95062e55936df79cd, e5e7ef72f7a699a0d573e65cc823ed2e884fb20424ac40121f2eec83cd5f8a23. Touched: none.
- 2026-08-27 — Automated compile completed for snapshot 04ba92b7348c443a0f74b69b09bd59bf54641980aaa39e109ddfe4a8b9650462, 117809b69da796a283d8cf33bcc3070410ed959316d541e60e5e5b8722740c72, 1aa27a23a79b8128f270cce63180bee146f72c687d74a4ff66c1fdb4572e3558, 3a76c8e750e3eea12c2c1ac91f401e46f2d2912578ed6c2293ac7488da9e716e, 4128099c74f16b85792a1e9416852f7ed2a5165696d01ace6fabf4191638b913, 6268616cde928e40ce56b0e8f88ddce6de2bd3a637c48ae65e95637ecaea8f45, 856d8c2de3e7b48453cc308b75819a1f62891049b79b31281010d8d07a72a0f9, b4901625fe6ad4690659b5885a415adbf1de2ccccdbd0457f2642c0046369d4b, bfd48a5b84985a0385c0dfbb87f00cc9bc6b3b7832150d31f610c9cadf7148a7, d7e3dda89a29a5832d2dbad43810288ab222070e81962fcce0a5b2b163c0d9d5, ecfea944209da81b4b7967bb250aca65635875724af4fb2606b040b2a4b59e50, f003ccd9e4826121b2399c6b54bde66a7dad0fdefad66695356865330412ee26. Touched: none.
- 2026-08-28 — Automated compile completed for snapshot 2e000b9638d8082b73e89dbabb800b175ba524f4a86fd3c4da30154217702ccb, 7765d7c7b26a5c57aece3a75cf50c4ada4b9779c4b49764da0d04014129768fd, ddad37a498a09df1b91297b9a08907b916034596347b21e4507086b41d074e28. Touched: none.
- 2026-08-28 — Automated compile completed for snapshot a579bf9181f3d76c6bbed6c7bfea93e9fda64be1fe293e83cc6f252b5774b0dc. Touched: none.
- 2026-08-28 — Manual compile completed for snapshot 364e16b9514d41a78c285be4daec7e6e81fab028fccf9d89e954a16d88c449dd, b532582f3cec31063e7f85efd6d5a426039a9a13da4dbdf45cc84fb1db2e7041, e372f13135861953c44915340cfee367634f5d0622639db1f254aec1e104bb6d. Touched: none.
- 2026-08-29 — Automated compile completed for snapshot 27b4b40d920e0499c13f12d3b25474e39d70f8fa1f4398ee157c68e0b6b2c7ae, 539a208e52e20f15f60549698da4fe6a0850d7e636ca4ef5eb5c5c0d2f2a4e93. Touched: none.
- 2026-08-30 — Automated compile completed for snapshot 0349bafda18976164d3465c75a1f9065b56a4fffe111b2823ed4e5a5b091cf63, 1753b553adb0d8d5458d205aa8c28f0706ef414f83595612fe5fa630fed7da74, 59171debf880009ac191728e7a6e7c2c94aab193ccc782129fc8145fd6ac95a9, f84b059e8d535999fff5335bdad2b74fee6742410efb813f434f12ee0ecdfd12. Touched: none.
- 2026-08-30 — Automated compile completed for snapshot e78b4be5d217af4ac30591c990d61b3848791f6eac3b9d2ac9068e75ce45ab7e. Touched: none.
- 2026-08-31 — Automated compile completed for snapshot 1b4526b82350b5b2527f7933934d29143dd6497fe68306ceadc33c0f5558d122, 4e7e2445a418399b24fa1fbd011b645b533d535c1cbcb96ce0e9147ca714b694, b3bca256856d780512572dc6ee9c014d6cc5d61ed0cb4797636f98c28ba67aac, cffa8604819b75981e70198d43fe1578c707a1d0db199b68f2db97815b685b9a. Touched: none.
- 2026-08-31 — Automated compile completed for snapshot 51f5f9b786284c9821a1de060fd0a5bbcf5ceb1bac86a5e8bf617c769673fe7b, 6fea2bb98150ea254962399e862374523a4187f05d180162b5197fcb1e8fc222, 7bef495b640ae993f00749d9472858b3845bce71390867fe4049fede864461f9, a397cd9ade6c45f7f84732dbe98b3957d4d39d1cef802a24528c21e708e0e56b. Touched: none.
- 2026-09-02 — Automated compile completed for snapshot 45e4be1a30f18cb0e2871bf126f660bd37fba1e307ef42c27f12495caf6440eb, 5dbbb36b987e187201026464c3a6026c1612be7d51ac2271b6531d83d13cb627, a39c3edce32d6abb98bca587858138bfcfdd95e7a07a82d52ac78e7390e8fc48. Touched: none.
- 2026-09-02 — Automated compile completed for snapshot 195d095900ec4a4b62bc5b07eaec6be0c9d827e00887cc93343b64c0301037e3, 21e3cb15c647b9ff57b6422eb67ed1d26d1a45bc7bf7283dba5a61063db65be6, 612384fadbd45829efec4ae56bf28a5c9318917f7ed42b2292a0ddeb9302df1b, d67abead0123afc465647b30afd07fddc6b998a5f287851f8d0f8e6241a76741. Touched: 2 unpublished page(s).
- 2026-09-02 — Automated compile completed for snapshot 22cd1efdce564348c376257f05431a58bb5d9030de9114c0f447f6a72a0047e4, 66fbd33f4cf6eb2742c9c1ca1c503c223d6e451e3930300358346c9e620df14e, 97108e48b62a5a5dbaeb414f6303af024664395b7e9c8fa477833ead08a01549, a3c47bd1f7b81dbec89b17320fae11592f7acaf3f089c52a2356a057a777f511, c4dbfe67b6343a47eaaf7010c14455831c3486b1efe8fd02bd0148cab342c2f8, e75eb97149871c7b3e0efd28dcef65f3476244d3d232e76a26f11a33bc9b2771, f62122574841b56ac1ffda577c1487954f9d3fde7822a46d66929d48b4ea5f49. Touched: none.
- 2026-09-03 — Automated compile completed for snapshot 33f909816016399610280a7bc4204e6a67d03e813a7a1100583fb543a212bfbf. Touched: none.
- 2026-09-03 — Automated compile completed for snapshot 0598760b3beff7689f83d03f40729f2a8946da2f0a893394b14eafd3082eb594, b2b2f8be924f7173f45b35f99db284a1d88607d5a22a539051bedcf6b495f56d. Touched: 4 unpublished page(s).
- 2026-09-03 — Automated compile completed for snapshot 06bc0cdd3d4b8abb5214edc3a422372cafb7abcecd3b0633065f2491a4028b02, 77b5dd1882b8d8f73ee306bd524f668bb531c7cea5d17c1fa094953eceac26c2. Touched: none.
- 2026-09-03 — Automated compile completed for snapshot 0bdfe9ec68c4788a1dea5b3cb4e06883dba5a77f72ad90573439571225990965, 0e9e9d867a55ef941e613a92c48a6e9cb457ae27929bfaa3d9e8eef0bc2a5083, 1675680400d6479442834e6a6559dab8b511d30ba110670ec8f9b617505127de, 1b2a6ddf0341c0b025d7000d91a6ac5a8c7d38e0c0f3af55b6399f7aad559e60, 20f46d066204b481564b5c9e374519ab6d9e9e05e0ac156cc7b602f2007e479f, 7163f3e8608bcab59c8888d189ca906ae2d99df439d1d13107d087565e94795e, 7d6c87cff8fa1c2722b64ee53eab4a313bb1e007ac5cc2c7e782665a501308b6, 93683e94bb6a13069958fbe28544cfc7174938549063bfbb0e934b31469939b5, 997963879f62df7e94918cf865c06863932b7d78301f78d105d2bbee4c16f056, 9e38c59509b5e526632289b112a94f1a536f6bb1b426ccfeed5222df42bc77bf, cf17e09e58e2dc93a31969b636f0bb5e65d72135b123cf2d1aca0476032669ac, d7c4b200227836ccc5bc7b091139c947dd91993c7cdcb4be1889260905ebfdde, dd8c0180bf48a26ed9d3a2a17ec01605c1d94d91dd0e1cec19e675c5760bba6b, e768222ec2a4cc992fb7c3cb84e9298060734f1692743db42ed4f1218439c967, ebee370091a8ce4484d62c099430b3d9d952fd03f350aa7fa97980e3d9f94e40, f32330110a9b397d94bccfb89a8a286c7bed95f084f4aaa55e76b553a211d790. Touched: 5 unpublished page(s).
- 2026-09-04 — Automated compile completed for snapshot a7cd3e20299b53ad0c3e260949bfbcbda2b7b8aeac7be7b18877617562dca72b. Touched: none.
- 2026-09-04 — Automated compile completed for snapshot 004933a023f5df18559dc22c80ecaa875d770a34a19d3f73f784e139d0f2ebe1, 01d9a98d5456ccf65507d0d6793d1994d70aef1f5453560ff28341f3eeebd608, 09e029f8fed74f8a44d57ceb3683788e6d8adeb3cd416f705240cc38f632be5e, 2aa926488a17b4e4eafc150c98495fa8a4dcedbe5475d85901f7d5a858302f57, 3465f71b03c37a0140730972a015c8b3f198cf431ff344a6eb421cf10549a39a, 3723b535de1583e52bfa1ccfa9aa7e76b6580a53a880112bdefce754f9ebc784, 389d243cb19c964ef96575197589d57dd28730444568bdcd258d75325c6f0f2c, 3a1b76deba3252a5f150525d9cc4002f1b029719fdd27a8c30ad689bdae2e9d2, 3ad6a2fe6af8636aba07502d245a6c0a97e7721bed580bd6ce68927ae3d8b563, 44417aa5a1dc39cb654b7b01df703eb814078eb8fd989a47b258698092f68904, 4b6109242e48ce5731b9f12236558f0759691476d83081a7f74b72626f465b63, 4f38872ff5377440e15cabe96b5aadf3e1fe3a5261abc4d39f6ad97ab8b01919, 523f2baf5cf025b4f216aa9d87cf1bc5fb2f2b0405bfdacff00299ad962f72c6, 57321d681e22089be339acc8b31cba855a61491599c8ea0d40e6788d01838554, 8c7d08abe71d02a3330d9ed93ad95afeb668226617c6ff12b8ec911b3fdf8dae, 92fa875f12d867e3beae76091e8e56691c48db06a3a74cea8b684252cf90303a, 966c41cbe2457150572682b63f8e44be8c67e228a805572ee44790b0042ea422, 9a06ecd1e047de98836d6bdd02b4f278a6a3f9aed21ad2cd000b3bc4e0a197c9, 9c663b3cff8b9e2a315ac7fd5ef788886fc792bc939a3b1c7e3f18023f89e83b, 9d2cf3590392eefe379a8dcaadcb59c910e0617b55565d9be8f4184db7005100, 9fe52352833f56e0275e76a283402381f031b0772c9d4b39a0412cecf9f8ed6f, a11818e480540cb28a27d2287ba8c7ddebdcfc7146fdc2d364e4d4e5310e37e8, a38589b30310df66ce7952ff9a5fba626122128ccee6ae896066cc4e1901f8b2, a5cd6f1ba08c98e0b6bd11f1ec885a5fc97fe70cbfd8401a52180b3c38f44a40, bb3faed85faae32d4623a895deea38294421a58303d958ed16e97df5408ab67d, bf0b995dc44ca3f6228074723f3a09ee190b26a43b2edd0ec5340dc689495b46, cf7edf601962af73f44b3895b3ff2993184e61e14171071c221f93bf536d9423, d33b4fc547c5ce851fc8b4ecd0bbc77d31dc4697950bab01135b47f358c0d1df, d8dc0d0b558a7b324a69dbafa314328686af4cb45d7be22ea3d77b29839cdc81, fd7d7476b32c2477da71b00793ecafb766bb59ceca2cf453fce6fd356e144635. Touched: 4 unpublished page(s).
- 2026-09-04 — Automated compile completed for snapshot 8f0367ef387fe23a5dfbc66933962ad055a61c43104655e96d61ae4426fc843f, ea3f230226981e13372c24bc0c70f568023edbe24009060c1584e0461a3a364a. Touched: 1 unpublished page(s).
- 2026-09-05 — Automated compile completed for snapshot 02265be409a2fd0b97116b9cac2a946ad8c2631afd7dd580b5fd44e36d5e9588, 23045846929c55b9dfdab98fde9b164fb6a02e4bdd0177403acd1a39b4003a53, 3891da69252a80f627e9b49a6ffbe1e464b7d2e1f5533e82d581ef42b5a66559, 4867c3c3deb2edecbffaf9ea8360664ca4eecca4c60ff9cf9751938fb5a5b122, 4ed569bec7afffe9758d7e57f0f1ac68ffd3127f419850df48c7e106813769f8, 50919bc1e62d784f084a28cb8d6a5da9b51aaccddfdf82bb3d101454c0be151f, 62f0c85adf4c595831bbdd1208384b8cbda8110439691955414000c77c63fbd3, 655ca4d2e0f09e9993a3ae88a5c5589b024d2f48944f7289c3bea5898fa6af12, 6619070bb634255783ba2a74e096ca31eb3bf527c7ecc0a706d47f03e776b5f6, 7bc7673d8970606a9f3110f9b40367852b5a86522c531a4531f0a46557ac3259, 7ccf2d19045782fa9b53c9863bb98384ff4018425cfe038d723771edb62e19e2, 7ded37feb07c096818a6cc6b031c1887186242b7eda5d58ac413c1d8347f8197, 81d35db0bb0eb7779ae8744c30ed31527cecca5f9ffd9e868906c208b48abd4d, 960c3bdea206e917cf7ab3670e2de8c7a4a180d199a2446dfe154397ae32f47b, 9ddede9a2ab92155a18d21bb9c55bcc270c465136e27c87fb15343699a3b093b, b762c33984ef505eb52c81d9a6eed1a763f3e72429afcb7eac93b4220f0c8ba9, c216872eecd2d8f6dd20aca3ff3044b45a2fd93d4a0cc2cc0ea8fabd01ca17be, c4a653bfa3b42a804bf950df4477ee0f4e5cd94340b184e022e681622221455c, c92869e07062697f62a542369d4f7ab11709d7cb807a718b0d5947754329a46f, d52c227ed97c2aa446b9a41974d9e4ba28122a0f00889f6a8fcf33e43b209d2d, d84a78a955fb19359400ff88bae7a1871a07767f6f2c710eb0601c63430a9356, ea4a61548f148038ff21497b6328b047b6fdd47eca6ec115f212f605a183f7da, eab0cae88171d1162c3e548304dd722f61e0f617d0a16440a7af0b9a7a954280, f1b30640adb93ec4f8c75cb91be619055c157be4e65af255fe8ac5e19e1337f7, f68094b4f37aa2754b20e6ce311e12ef7b1a69e9f19201047fd7847a99df0c73. Touched: 2 unpublished page(s).
- 2026-09-05 — Automated compile completed for snapshot 006e7186dfca58036ddf93b6658098c58e402ca671fc44e57174160ca6ec28da, 007dfd30abe7106e80ed99f0fb72926969d7f94cb0e2d3bc801d7e5b14e69c3c, 03267a0dae908fb15a41bee2a9d07926a02cb50a89d786cbd45a8adf0e0832de, 0b186f703c69ca3a1679f142d20d06eb8fffc116c13427f777a658b0634c6d3f, 26d5d0e2389542a9cb75a1d5358ed63eb8e225dfe7ffff9b9c0cb42b867e3e35, 324691d6b39a963841f741fec00e24cb4edea9120eb418a2747f636d14e4ab79, 3cbacd18d4f18164bc4b09cf35279f2974b474affc753eac6260ac92db610f45, 4e7c7454aafe34846576951b60484bb3c85cb2ddf94d1df6fb23283960b76558, 51109abbac790a4e9676240a259cacdca1d51e38a68fcfc1e57cf22e57349c8e, 536fba11daf6b08dfc4750588b221767a4ffe2015824682c6779271926acc22d, 54aabbcb0d16d9c92a455d2b50b8d0be82fba93cbfe12bc1d154efa75886bdef, 585890e9b51d16495e96f4ba93fb32cc8b3363100a2fe28d58181136667c295c, 590543037ee3cd3cb89959307858304fd4d82f0ce3d0b7734b629a32e6ffd64d, 5b35a22adefc2081f7d43f22470c06d1e7c19df78dd5b10c3a4c4e319ec6681b, 6807983fa12aa4ee89de64dae3fca94267c41e13b91b8cd74bf21c002ea21cee, 6ceab6f68d033145be90cfbd5ea7bd9d728ebbb9e3e8099e26772bbd18e7d689, 71f5ac9e87a002bb8c0ecae26a2f45a3540a954531a6caa8b759275112b9f459, 83b36ae8a26299deb04df7e4fae4529f1a5ac6361d6e232677a993b9cb45eb01, 8ce9b26fa9b10a8227954ec6ad2d71f93f7990e98b2cb2684f872b1671e18f92, 903620eab990916535387c1e269a82811b76c846bf13dc0bb76c7d4d25618250, 932f20816ff42e3c5ec5ab855361a9b927146477c69aea47d2ab723fa121d18d, a291547cca255d566ffe4834af8126ae2c7e7edc2600bd041127ed784d69f23e, a6fe4fd189461731e633262c82f0003ecd03b0dccaadb35c31b84d8c09552147, b32125ed8b03973d2bc1ba37bea9b9d1ce57d67491ec35e9116c6ec3b0449101, b536f6d06eb79033937e4b187ef1e0207bf7f9ba8b88c1dd87b23a67692b5959, b9f8d01dfe33574ec11d1fb3dfa5ba4008e1b2c5ec593049b5e40ce614abd02e, bcd2ad98380d7e12f749318f168d01c28880ba3663636f12f9940b32a5ee55cb, cb141f20ba4338c65cd9c6520ff19bd51e007864c207400d0ac21dd896787e60, cb7c98097c53b90e7635c3e92cc1a244a985499f9d3c47077a62ebd1f37337df, cec078854d4b94a1f85ceae240149bee8c1078cfb32a6da0bc24fbbae186f010, e2aee18e7d9518bd33aabb5cec7b59c20eaa2e9ea113936638011344b55c3383, fe5fa2289cebc783e7061b2a26079df391c28a59b3bb80ad7bd4b114e2f498f3. Touched: none.
- 2026-09-06 — Automated compile completed for snapshot 127550c9b0cf80b84282cef740766618ab3b4000a8ce4385cc1438f7a36d4d21, 219dd94a9824938629d9dd82f890473b8cc1b6327d50a36ca48a433226093f98, af7004ac13ede0a859fb62d4320af85629440204d4c8ed587f401f5ff0694d46, e6e88ac9ba37160302829c2e3f9f44907da8e211544991ec0fc2f06f74322687, ee42869703b2fdcd56968ab1f4e774d393c815ba00c53c0388fb1ecb42dcb5cb. Touched: none.
- 2026-09-06 — Automated compile completed for snapshot 01e8a4e42a8a38975a0027b4a79bdcc5bca3b2b86d222f5647417d2e21160b32, 448a8b9ff6162d8701b8926d5699a05685f31f07da8f982e8c3869fff5361762, 5d735aec2f208778e19242525a2b5a35b03bfde04bbbb6998220f7b5d80ab18e, 6058468e23871f036aaa3ec8bd04d62b0dddc415f07f79857e9cc3f6d1b3ab37, 6f8a5eb7103e470922410d14db0ad1d7122b3558c06fc6d41545c51b1529f49b, ac225956af7389f9a86e95a5c8e237ee4a1afe0127e174cd29c1d0c75b1727e2, dd425233887fa486bcb1b4fb4e386b3a156b33235044ca646cc222fef8f13812. Touched: none.

## Editorial note
This log is vault metadata — an append-only editorial changelog of compile passes and hygiene actions over `knowledge/`, not content derived from `knowledge/raw/` or `knowledge/inbox/`. New entries are appended at the bottom by compile passes and by hand; entries are never rewritten or removed.

> **Note on daily dates:** Only `knowledge/daily/2026-04-13.md` and
> `knowledge/daily/2026-04-19.md` are shipped in the public source.
> Entries dated otherwise (e.g. 2026-04-12, 2026-04-14, 2026-04-18,
> 2026-07-03) reference private daily files that exist only in the
> installed vault (`$LLM_WIKI_ROOT`), not in the public clone.

- 2026-08-21 — Narrowed the citation relevance gate after it was found refusing correct answers in this vault's own configuration. Notes are English by project rule and questions arrive in Russian, so a correct English span cited under a Russian claim shared no token and failed the whole answer. Word overlap now decides only within one script; across scripts the gate looks for tokens that survive translation — figures, versions, counts and kept identifiers — and abstains when the claim carries none. Named entities are not treated as anchors because they are transliterated rather than translated. Recorded in `cross-lingual-citation-relevance-decision.md`; the 2026-08-19 decision stays active for same-script pairs. The four functions in `scripts/query_memory.py` that the complexity gate refused were split in the same pass.

- 2026-08-21 — Named what delimits one entry in a daily log. The evidence binder recognised only a `## [HH:MM:SS]` heading, and the log this runtime wrote today holds 295 entries and no headings, so nothing the lifecycle capture records could be cited by a compiled page. An entry now starts at a heading or at an `<!-- llm-wiki-operation: -->` marker and ends at the next delimiter of either kind; exactly one entry must declare the timestamp evidence names. The quote must still appear once inside that entry and be a complete line. Deliberately not extended to the oversized-day splitter, because part boundaries decide receipt digests. Recorded in `daily-entry-boundary-decision.md`.

- 2026-08-21 — Closed audit item `NEW-39`: the compile pass failed with `no LLM provider produced a validated compile plan` while the `claude` provider probed as available. Reproduced in a hermetic temp vault: with a heading-form daily the provider returned a validated plan, with a marker-form daily it returned `critique:claude:validation_error`, because no evidence could bind. It was a consequence of the entry-boundary defect, not a queueing defect. The same daily now yields a plan.

- 2026-08-21 — Закрыт `CROSS-02`: корень состояния (`LLM_WIKI_STATE_ROOT`) теперь приводится к абсолютному пути в обоих местах, где этого не делалось — на входе записи знаний (`markdown_transaction._default_coordinator`) и в каноническом реестре владения очереди (`memory_queue._state_root`). Остальные читатели делали это всегда, поэтому расхождение давало два имени одного каталога: относительное значение попадало в координатор буквально, а симлинк разводил проверки принадлежности пути. Проверка — `tests/test_state_root_identity.py`, четыре теста, все падали до правки. Правка ждала разбора `scripts/markdown_transaction.py` по правилу 5: файл из 53 функций выше порога приведён к нулю нарушений (539 функций, средняя сложность 2.7) четырнадцатью партиями без изменения поведения.

- 2026-08-21 — `CROSS-06` (мигающие тесты жизненного цикла LSP) укреплён, но не закрыт: воспроизвести падение не удалось — Python 3.10 поставлен локально, двенадцать прогонов названного теста при восьми счётчиках на четырёх ядрах и два полных прогона файла дали ноль падений. Правка сделана по гипотезе и названа так же. В этих тестах три ожидания — барьер, окно удержания замка и короткое ожидание, доказывающее, что работа завершается, пока замок держат; проверяется только третье, но первые два были голыми литералами в одну и две секунды. Тесный барьер давал мигание, а короткое удержание — тихий дефект: если главный поток не получал процессор дольше двух секунд, замок к моменту проверки уже был свободен и проверка проходила по неверной причине. Оба получили имена (`_BARRIER_SECONDS`, `_HOLD_SECONDS`), удержание теперь всегда переживает окно, которое обязано покрыть.

- 2026-08-21 — Полный прогон дал одно падение в `tests/test_lsp_process_tree.py`, и сообщение говорило только «фикстура вышла с кодом 1». Причина: `ProcessTree` даёт каждому потомку трубу `stderr`, которую никто не читает, поэтому трейсбек фикстуры выбрасывался вместе с трубой. Диагностика исправлена — сообщение теперь несёт этот `stderr`, проверено регрессией `test_a_dead_fixture_reports_why_it_died`; сообщение собирается только при провале, поэтому здоровый путь трубу не трогает. Сама причина того падения потеряна: тест проходит в одиночку и не воспроизводится. Заодно четыре функции этого файла, бывшие выше порога правила 5, разложены.

- 2026-08-21 — Записано решение, которое владелец принял, но никто не записал: vault и публичный исходник сведены в один каталог, `$LLM_WIKI_ROOT` и `$LLM_WIKI_STATE_ROOT` указывают сюда. Раздел 2 `CLAUDE.md` и `AGENTS.md` до сих пор описывал два раздельных каталога и называл это разделение тем, что удерживает личное знание вне публичного репозитория. После слияния фраза стала ложной в худшую сторону: агент, следующий ей, прочитал бы «личное знание идёт в `$LLM_WIKI_ROOT`, не сюда» и записал бы личное знание в дерево с GitHub-remote. На деле удерживает `.gitignore` — запрет по умолчанию на `knowledge/daily/*.md` и `knowledge/notes/*` со списком разрешённых страниц, плюс тест `test_the_vault_index_and_log_name_only_published_notes` на индекс и журнал. Контракт переписан под это; страница — `single-directory-vault-decision.md`.

- 2026-08-21 — Следствие из решения об одном каталоге, закрытое в тот же день: генератор `knowledge/index.md` перечислял все найденные страницы, а сам индекс отслеживается git и переписывается рантаймом. То есть каждый ребилд вписывал названия личных страниц в публичный файл, и единственным препятствием был структурный тест на коммите — забор, а не исправление. Теперь генератор читает тот же список разрешённого из `.gitignore` (не спрашивая git: перестройка индекса — автоматический писатель, а набор тестов писателей требует, чтобы они не вызывали git). Заодно удалены `collect_pages`, `extract_hook`, `extract_type` и `rel_link` — мёртвый код, который читается как рабочий путь и в который сначала попала эта правка. Проверка — `tests/test_index_publishes_only_public_pages.py`, четыре теста, два падают при отключённом фильтре.

- 2026-08-21 — Ветка отправлена в CI (PR #2, прогон `32530990216`) — первое доказательство на трёх платформах. 43 задачи из 51 зелёные. Все шарды linux и macOS прошли, включая linux py3.10: значит `CROSS-06` в CI не воспроизводится, а macOS-половина `CROSS-03` закрыта. Падало четыре вещи. Первая — линт: он требовал устаревшую страницу в индексе, откуда генератор её намеренно убирает; два правила были несовместимы, и зелёного состояния у сборки просто не существовало. Вторая — шард 3 на Windows на всех пяти версиях Python, то есть детерминированный дефект: `subprocess.run(input=..., text=True)` на Windows переводит `\n` во входных данных в `\r\n`, `git check-ignore --stdin` получает путь с возвратом каретки, считает его частью имени и возвращает в C-кавычках — каждая страница выглядит неопубликованной. Обе исправлены и проверены. Третья и четвёртая — замок установщика Pyright (Windows отвечает `ERROR_ACCESS_DENIED` на файл, помеченный к удалению; у аренды LSP такая повторная попытка есть, у замка нет) и один тест автономной загрузки LSP — оставлены неисправленными: проверить правку на этой машине нельзя.

- 2026-08-22 — Закрыт дефект замка установщика Pyright, найденный прогоном CI на Windows: `_create_owned_lock` читал как проигранную гонку только `FileExistsError`, а Windows на файл, помеченный другим процессом к удалению, отвечает `ERROR_ACCESS_DENIED` (5), на открытый — 32 и 33. Всё это поднималось до `pyright_install_io_failed` и валило установку, хотя означает ровно то же: жди и пробуй снова. У аренды LSP такая повторная попытка была с самого начала, у замка — нет. Настоящая ошибка прав теперь попадает в то же ожидание и выходит таймаутом вызывающего, а не отказом ввода-вывода; это записано прямо в коде как осознанная цена. Проверка — `tests/test_install_lock_contention.py`. Правка ждала разбора `scripts/install_pyright.py` по правилу 5: 38 замечаний на 104 функциях приведены к нулю шестью партиями (272 функции, средняя сложность 2.9) без изменения поведения — 104 теста установщика проходят.

- 2026-08-22 — Второй прогон CI оставил одно падение, и оно оказалось настоящим дефектом координации: `test_multiprocess_status_reads_remain_coherent_during_claim_and_complete` на Windows/3.14 получал `BlackboardConflictError` на ресурсе, который принадлежит только этому писателю, — то есть конфликтовал сам с собой. `claim_task` делает долговечную запись в SQLite прежде, чем допишет строку журнала; если вторая срывается, `_publish_active_claim` обязан освободить ресурс, иначе вызывающий, так и не узнавший о владении, при повторе встретит собственные строки. Обещание было записано в docstring и покрыто тестом, но само освобождение было best-effort: одна попытка под `contextlib.suppress`. Оно идёт под той же конкуренцией, что сорвала объявление, — то есть срывается вероятнее всего. Теперь у него шесть попыток примерно за две секунды, а наружу по-прежнему выходит исходная ошибка вызывающего. Проверка — `tests/test_blackboard_release_retry.py`; при одной попытке тест падает. Отвергнут вариант с идемпотентным `claim_task` по переданному идентификатору: он потребовал бы предсказуемого токена аренды, а это ломает ограждение.

- 2026-08-22 — Разобрал то, о чём рантайм сообщает сам при старте сессии: «2 capture(s) lost … see `logs/capture-failures.jsonl`», а файла нет. Счётчики и причины лежат в `run/state.json`; след — отдельный best-effort файл, и его в этом хранилище не существует. Исправлены две вещи: `record_capture_failure` обещает никогда не бросать, но вызывала писателя следа вне собственной защиты, а тот ловит только `OSError`; и строка отчёта отсылала к следу, не проверяя его наличие — теперь при отсутствии следа она так и говорит и отправляет к `run/state.json`. Найдено и не исправлено (`NEW-41`): по следу нельзя понять, какой инструмент MCP упал, потому что имя операции передаёт одно место из шести, хотя диспетчеры держат `name` и `uri` в области видимости. Правка — три строки, но `scripts/mcp_server.py` даёт 33 замечания гейта на 93 функциях, и за этой правкой не стоит красный CI: это удобство разбора, а не отказ.

- 2026-08-22 — Разобран второй потерянный захват: `compile_oversized_daily` записан 2026-08-21 в 11:07:30, за 54 минуты до коммита `ab01522`, научившего компилятор резать длинный день на части. Проверено измерением на живом хранилище: день делится на пять частей по ≤16 КБ, самая большая стоит 22 875 токенов при доступных 27 744. Счётчик описывает уже исправленное. Отсюда третий дефект — наблюдаемости: счётчики только растут, и ничто их не снимает, а предупреждение, которое нельзя убрать, перестают читать; заводили же его затем, чтобы потерю заметили. Теперь строка называет момент последней потери, и есть `--clear`, снимающий счётчики намеренно и называющий снятое. Само ничего не очищается: счётчик — свидетельство реальной потери, снимает его человек. Счётчики этого хранилища оставлены на месте.

- 2026-08-22 — Найдено и исправлено (`NEW-42`): блок здоровья при старте сессии почти целиком состоял из выдумки. Измерено на живом хранилище — шестнадцати проверкам доктора нужно 1.77 с, а старт даёт 0.1, и это намеренно закреплено тестом. Семь проверок не выполнялись и объявлялись degraded; две выполнявшиеся обрывались на середине чтения и сообщали отказ, не пометив себя: очередь — «state unreadable», LSP — то же, при том что при настоящем бюджете обе ok, а ложная ошибка очереди делала весь отчёт `error`. То есть каждый сеанс начинался с находок, которые были следствием часов, — так читатель и приучается пропускать этот блок. Правило теперь про прогон, а не про проверку: если бюджет где-то кончился, прогону нельзя верить целиком; блок так и говорит и указывает команду за настоящим состоянием. Строгий бюджет оставлен как закреплено — видна стала его цена.

- 2026-08-22 — CI поймал дефект, который внёс мой же разбор установщика: сброс каталога переехал внутрь помощника создания, где новый дескриптор — локальная переменная. При отказе сброса он не доходил до очистки и оставался открытым; на Windows это нарушение совместного доступа к тому самому каталогу, который нужен повторной попытке, и очистка видела один дескриптор вместо двух. Три шарда Windows назвали это дословно. Сброс возвращён в цикл, где оба дескриптора в области видимости, а `_runtime_child` теперь сообщает, создал ли он каталог, вместо того чтобы делать сброс за вызывающего. Две регрессии живут в наборе установщика и идут на любой платформе; при возвращении дефекта первая падает со словами «the created child was not handed over». Урок общий: при выделении подфункции ресурс, за который отвечает вызывающий, обязан дойти до него раньше, чем что-либо сможет упасть.

- 2026-08-22 — Найдено, не исправлено (`NEW-43`): проверка планировщика считает ночное обслуживание устаревшим, если оно не запускалось «сегодня», а идёт оно в 03:00 — значит каждую ночь с полуночи до трёх планировщик объявляется деградировавшим при здоровом таймере. Проверено в 00:06 UTC: последний запуск 21 час назад, следующий через 2 ч 53 мин, `last_nightly_status: success`, статус degraded. Правка живёт в `scripts/doctor.py` (39 замечаний гейта на 431 функции), а вес находки после правки блока здоровья мал: при закреплённом бюджете 0.1 с прогон всегда усечён и эта ложь до сеанса не доходит.

- 2026-08-22 — Три дефекта записи, найденные по следам собственного рантайма. Первый: компайл падал на коммите с `FileExistsError: knowledge/index.md`, потому что писатель брал предусловие записи из списка источников, сужаемого под окно модели; на большом хранилище индекс и журнал в промпт не влезали, и писатель считал, что их нет на диске. Снимок файлов хранилища отделён от того, что видела модель (`CompileInputs.vault_files`). У той же ошибки было второе дно: выпавший из промпта журнал стал бы перезаписью с потерей истории. Второй: блок начала сеанса называл память свежей в хранилище, где ни один компайл не записал, — свежесть читалась из отметки «завершился», а не «записал». Третий: строка компайла в отслеживаемом `knowledge/log.md` перечисляла приватные страницы поимённо, а квитанции компайла не покрывались ни одним правилом `.gitignore`; фильтр публикации обобщён с заметок на любой путь, квитанции игнорируются. Записано в `docs/DEVELOPER-AUDIT-STATUS-2026-08-18.md` как `NEW-44`, `NEW-45`, `NEW-46`.

- 2026-08-22 — CI уронил один шард на Windows, и причина оказалась не в правках, а в допущении теста: заглушка LSP отсчитывает время жизни от собственного запуска, поэтому обязана пережить старт родителя, а не только проверку. Две секунды не пережили — в журнале задачи `96955875519` виден чистый выход (`returncode: 0`) на 730-й секунде прогона. Порог поднят до восьми секунд и назван, вместе с окном ожидания выхода; цена — файл идёт дольше — записана рядом. Это тот же дефект, что закреплён в этом файле трёхсекундным ожиданием для пути stderr.

- 2026-08-22 — Проверенное состояние ветки зафиксировано под одним коммитом: `47085b7`, прогон CI `32545013435` — 49 задач из 49 зелёные на трёх платформах и пяти версиях Python, локально 6596 тестов пройдено при 249 пропущенных. Пункт `NEW-03` реестра закрыт: он держался на том, что аудируемое состояние жило в рабочем дереве без коммита.

- 2026-08-22 — Найдено, почему память этого хранилища не компилировалась ни разу: в юните `llm-wiki-nightly.service` не задан `PATH`, а пользовательский systemd отдаёт службе только системные каталоги. Единственный установленный провайдер лежит в `$HOME/.local/bin`, поэтому ночной компайл каждый раз падал за секунду с «ни один провайдер не дал проверенного плана», тогда как тот же прогон из оболочки работал. Доказано подменой среды: та же команда под путём планировщика падает, с добавленным пользовательским каталогом — доходит до плана. Генератор юнита теперь кладёт в `PATH` каталог, где лежит сам `uv`. Установленный юнит обновится только явной переустановкой — её должен запустить владелец. Заодно исправлено то, что ночной прогон объявлял «failures=0», не читая исхода компайла, который сам же и породил.

- 2026-08-22 — Разобран ещё один упавший шард CI, и он оказался не тем, чем назывался: `test_close_from_reader_handler_does_not_deadlock` сообщал `assert False` после минуты ожидания, а взаимоблокировки не было. `close()` из обработчика чтения пропускает join своего потока и ждёт писателя одну секунду по умолчанию; не уложившись, он бросает исключение, и строка, выставляющая событие, до выполнения не доходит. Тест теперь ловит исключение, выставляет событие в `finally` — живость проверяется по-прежнему — и называет причину, а срок закрытия задаёт явно. Общее: проверка живости, у которой сигнал стоит после действия, врёт при любом отказе действия.

- 2026-08-22 — Записано, но не исправлено (`NEW-51`): два шарда Windows падают под нагрузкой на путях уборки, и оба раза продукт прав, а тест ждёт беззаботного исхода — внедрённый отказ маскируется сообщением не уложившейся уборки, а задача очереди уходит в `blocked` вместо `ready`, потому что убийство дерева процессов не уложилось в срок. Что это нагрузка, а не код, доказано так: прогон уронил два шарда на коммите, меняющем один файл документации, при том что получасом раньше тот же код дал 49 из 49, а перезапуск тех же задач без правок снова дал 49 из 49. Проверки намеренно не ослаблены: заблокированная задача сама не повторится, а замаскированная причина — то, против чего первый тест и написан.

- 2026-08-22 — Второе из двух падений `NEW-51` повторилось на третьем прогоне подряд, и его пришлось исправлять: два раза из трёх — это не редкость, а красная сборка. Исправлено не ослаблением. Тест назывался «убивает дерево компилятора», но убийство проверял последним утверждением, а падал на предыдущем — то есть до собственного предмета не доходил. Теперь убийство утверждается первым и безусловно, а состояние задачи признаёт оба законных исхода: очередь имеет право заблокировать задачу, если уборка процесса не уложилась в свой срок, и этот контракт уже закреплён отдельным тестом. Утверждать только беззаботный исход значило утверждать неправду.

- 2026-08-22 — После того как ночной компайл получил `PATH` и дошёл до записи, его остановила собственная защита данных: две транзакции ушли в карантин с `dlp_content_blocked`. Причина — ложное срабатывание на слаге страницы этого же репозитория: `dead-task-retirement-and-restore-decision` содержит `sk-` и тридцать символов после, что неотличимо по шаблону от ключа OpenAI. То есть хранилище не могло опубликовать ничего из-за обычного слова «task». Правилам с буквальным префиксом добавлена граница токена — ключ начинает слово, а не продолжает его; знаки препинания границей остаются, поэтому настоящий ключ после `=` или кавычки ловится как прежде. Исследование с источниками — `docs/research/2026-08-22-secret-prefix-boundaries.md`.

- 2026-08-22 — Записано решение о повторе после карантина: ключ идемпотентности по-прежнему нельзя переиспользовать с другим содержимым, но отказ перестал быть тупиком — следующая попытка по тем же входам берёт следующий порядковый номер и называет отвергнутую транзакцию родителем, а сама отвергнутая запись остаётся неизменной уликой. Форма не выдумана: контрольные точки проектов в этом же модуле всегда так и повторялись, а у транзакций поле `parent_transaction_id` было, но его никто не передавал. Стандарт и практика (черновик IETF `Idempotency-Key`, идемпотентность Stripe) требуют именно отказа при другом содержимом, поэтому изменён не отказ, а то, что за ним следует. Страница — `idempotent-retry-after-quarantine-decision.md`, исследование — `docs/research/2026-08-22-idempotency-retry-after-quarantine.md`.

- 2026-08-22 — Первый удавшийся компайл этого хранилища сразу показал мой же регресс: перестроенный индекс лишился пяти опубликованных страниц. Фильтр публикации читает `.gitignore`, потому что автоматическому писателю нельзя звать git, но «опубликована» на деле значит «отслеживается git» — а эти пять отслеживались, не будучи названными в списке. Правило запрета на отслеживаемые файлы уже не действует, поэтому расхождение молчало до первой перестройки. Пять строк дописаны в список, инвариант закреплён структурным тестом: каждая отслеживаемая заметка обязана быть в нём названа.

- 2026-08-22 — Первый компайл дал ещё две находки линта, и обе оказались одним дефектом: линт не знал о публикационной границе. Он требовал, чтобы приватная страница была названа в отслеживаемом индексе, и чтобы опубликованная страница сослалась в ответ на приватную — то есть требовал ровно той утечки, против которой граница и заведена. Обе проверки теперь спрашивают тот же фильтр, что и генератор индекса; обратное направление по-прежнему ловится как битая ссылка. На живом хранилище линт сошёл с трёх находок до одной.

- 2026-08-22 — Записано и не исправлено (`NEW-56`): после решения о повторе после карантина карантинные записи остаются уликами навсегда, а проверка транзакций считает любую из них открытой проблемой — доктор на этом хранилище остаётся красным из-за двух попыток, которые уже перекрыты успешной. Правило должно смотреть на цепочку: попытка, за которой есть зафиксированная, — история. Правка на три строки, но гейт правила 5 отказывает пофайлово, а `scripts/doctor.py` несёт 39 замечаний; чтобы принять три строки, нужно разобрать модуль, сторожащий удаление `run/` и ремонт. Соразмерности нет — решение за владельцем. Два теста написаны и помечены `xfail(strict=True)`.

- 2026-08-22 — `scripts/doctor.py` разобран по правилу 5: 39 замечаний приведены к нулю пятью партиями, поведение не изменилось (527 функций, средняя сложность 3.1, 450 тестов трёх затронутых наборов зелёные). Сам разбор поймал свою же ошибку: имя `_require_real_directory` в этом файле уже было занято, и новое определение перекрывалось поздним — 32 теста назвали это немедленно. После разбора встала правка `NEW-56`: тяжесть проверки транзакций считает не все карантинные записи, а только те, которых не перекрыла зафиксированная попытка той же цепочки. На живом хранилище из двух карантинных перекрыта одна, и доктор красный ровно за вторую — за настоящую, а не за обе.

- 2026-08-22 — Закрыт `NEW-43`: проверка планировщика считала ночное обслуживание устаревшим, если оно не запускалось «сегодня», а идёт оно в 03:00 — каждую ночь с полуночи до трёх здоровый таймер объявлялся деградировавшим. Свежесть расписанной задачи — интервал, а не календарная граница: ночной прогон теперь записывает мгновение, а проверка считает обслуживание текущим, пока ему меньше 26 часов (сутки плюс запас на поздний прогон). Состояние без нового поля живёт по прежнему правилу, поэтому обновлённое хранилище не шумит. Исследование с источниками — `docs/research/2026-08-22-scheduled-job-freshness.md`.

- 2026-08-22 — Закрыт `NEW-41`: по следу отказа нельзя было понять, какой инструмент MCP упал. `_safe_exception_text` принимает имя операции, но из шести мест его передавало одно, поэтому в `run/state.json` оставалась запись вида `mcp: ValueError: ...` — без имени восстановить причину нельзя, и один такой отказ 2026-08-21 так и остался неопознанным. Теперь диспетчер инструментов передаёт `mcp.<имя>`, `_log_decision` — своё имя, диспетчер ресурсов — `uri`. Проверка — `test_a_failed_tool_call_records_which_tool_failed`. Правка ждала разбора `scripts/mcp_server.py` по правилу 5: 33 замечания на 93 функциях (средняя сложность 7.6, отдельные значения 71 и 48) приведены к нулю шестью партиями без изменения поведения — 329 функций, средняя 2.7. По дороге найден и убран мёртвый тернарник в `_build_operation_envelope`: он выбирал потолок «0.6 если stale иначе 0.4» внутри ветки, куда попадают только stale, то есть второе значение было недостижимо. И назван голый литерал в тесте выключения: полсекунды на выход процесса — это мерка машины, а не свойства; свойство в том, что заблокированный демон-поток не задерживает выход вовсе.

- 2026-08-22 — Найдено по собственному состоянию рантайма и исправлено (`NEW-57`): `run/state.json` показывал `last_compile_status: error`, `last_compile_error: ValueError: compile receipt is corrupt` — то есть после успешного компайла в 11:56 хранилище перестало компилироваться вовсе. Сообщение одно на четыре разные причины и не называет ни квитанцию, ни причину; настоящая нашлась воспроизведением через рабочего читателя: `compile receipt has no committed transaction authority`. Корень — во вчерашней правке идемпотентного повтора. Квитанция обязана называть выведённый идентификатор операции: её же читатель пересчитывает его из полей записи и отвергает любой другой. Повтор после карантина берёт следующий ординал (`<id>#2`), значит писала квитанцию строка, чей `operation_id` отличается от названного, а проверка авторитета требовала, чтобы **названная** строка была committed. Названная была карантинной — квитанции стали нечитаемы, и на них падал каждый следующий компайл. Исправлен читатель, а не запись: `MarkdownCoordinator.committed_attempt` разрешает идентификатор в ту попытку, которая зафиксировалась, включая ординалы; доказательство байтов не ослаблено — квитанция по-прежнему авторитетна только если зафиксированная транзакция записала ровно эти байты. Живое хранилище починилось само, без удаления квитанций и правки транзакций: три квитанции снова читаются. Проверка — `test_the_retry_receipt_names_the_transaction_that_committed_it`, воспроизводит отказ на пустом хранилище.

- 2026-08-22 — Владелец назвал правило одной фразой: всё должно работать автоматически, без привлечения пользователя. Под него переписаны две проверки здоровья, которые не умели возвращаться в зелёное сами. Первая: карантинная попытка считалась нерешённой, пока в её же цепочке не зафиксируется повтор, — а обычный путь восстановления другой: отказ чинят, компайл идёт заново и законно берёт другой идентификатор операции, потому что вместе с правкой изменились диспозиции. Теперь достаточно исхода: всё, что отвергнутая попытка собиралась создать, создано зафиксированной транзакцией. Попытка, ничего не создававшая, или та, чьи страницы не написал никто, остаётся находкой — там потеря настоящая. Вторая: счётчик потерянных захватов снимался только руками, и предупреждение висело вечно. Счётчик по-прежнему улика и сам не обнуляется, но находка теперь охватывает последние семь дней: новая потеря делает её истинной сразу, тихая неделя возвращает зелёное. На живом хранилище `transactions` стал ok без единого удаления. Решение — `self-resolving-health-findings-decision.md`, исследование — `docs/research/2026-08-22-health-findings-must-resolve-themselves.md`.

- 2026-08-22 — Найдено по расхождению двух отчётов одного прогона и исправлено (`NEW-59`): линт сообщал `stale_compiled` для `2026-08-21.md`, а компайл в том же прогоне — «нет изменившихся дневников». Измерено: день в 70 063 байта режется на пять частей, у каждой есть квитанция, то есть он скомпилирован целиком; но в `compiled_daily_hashes` лежал дайджест последней части, а не файла. Все дешёвые читатели — линт, `vault_status`, `maybe_compile`, `flush_memory` — сравнивают запись с хешем целого файла, поэтому любой длинный день после разреза объявлялся несжатым навсегда: находка каждую ночь, фантомный backlog и пустой запуск компайла каждый раз. Авторитет — квитанции, и это записано в самом модуле; зеркало просто говорило не то, что от него ждут. Исправлены обе стороны: запись называет целый файл и только когда у каждой части есть квитанция, а каждый прогон компайла приводит зеркало в согласие с квитанциями — иначе уже испорченная запись осталась бы навсегда, ведь к скомпилированному дню компайл не возвращается. На живом хранилище линт сошёл с трёх находок до нуля.

- 2026-08-22 — Закрыт `CROSS-07`, найденный прогоном CI 32579866020: на Python 3.10 доктор объявлял здоровые транзакции испорченными. `datetime.fromisoformat` научился читать суффикс `Z` только в 3.11, а 3.10 у продукта минимальная поддерживаемая версия; рантайм пишет метки именно с `Z`, поэтому на 3.10 `_parse_utc` возвращал `None` для каждой, проверка транзакций давала `transaction_metadata_corrupt` и запрещала удаление `run/`. Проверено на настоящем 3.10: `fromisoformat('2026-08-22T00:00:00Z')` отвергается, `+00:00` читается. Исправлено одним нормализатором на три места разбора; остальные читатели репозитория проверены поимённо — где нужно, нормализация уже была. Нашлось это только потому, что утверждения в тестах стали нести с собой детали: до этого CI три прогона подряд сообщал ровно `assert 'error' == 'ok'`.

- 2026-08-23 — Владелец указал на устаревшее допущение реестра: «на этой машине доказательства получить нельзя, установленного runtime здесь нет». Оно перестало быть верным 2026-08-21, когда vault и исходник свели в один каталог. Проведена репетиция чистой установки прямо здесь — изолированный `HOME`, свежий клон коммита `8d94694`: пройдены предпосылки (Python 3.10.20, git, uv), установка залоченных зависимостей, production-смоук с двенадцатью инструментами MCP и создание рантайм-каталогов. Остановилось на шаге планировщика (`systemd user manager is unavailable; select cron explicitly`), и после отказа не осталось ни строки в профиле, ни каталога `run/install` — транзакция владения закрылась начисто. Изолировать сам шаг планировщика домашним каталогом нельзя: systemd user manager и crontab — состояние пользователя, а не HOME; контейнерного рантайма на машине нет. Реестр доказательств переписан по измеренному.

- 2026-08-23 — Владелец потребовал, чтобы язык вопроса ничего не решал, и проверка этого требования вскрыла `NEW-60`: смысловой поиск здесь написан целиком, покрыт тестами и никогда не запускался. Построитель векторов поколения вызывают только тесты — восемь вызовов и определение, ни одного рабочего; значит каждое опубликованное поколение несёт `vector_state: absent`, а старый путь закрыт с другой стороны, отказом при любом заданном сроке. Половина поиска не работала ни у кого, и ни один тест этого не замечал, потому что каждый звал построитель сам. Заодно заменена английская модель кодирования на многоязычную: на трёх русских вопросах она ставит нужную страницу первой (0.77–0.86), английская давала шум 0.43–0.52 и один неверный выбор. Решение — `nightly-builds-generation-vectors-decision.md`, исследование с обзором достижений на 2026-08-23 — `docs/research/2026-08-23-retrieval-must-not-depend-on-the-language.md`.

- 2026-08-23 — Закрыт `NEW-60`, и он оказался двойным. Векторы поколения не строил никто — построитель звали только тесты; вписал построение в тот же проход обслуживания, который публикует поколение, с отказом в `absent` при отсутствии модели, пустом корпусе или любой ошибке: терять готовое поколение из-за необязательных векторов нельзя. Этого не хватило: путь поиска брал кодировщик из аргумента `generation_embedder`, у которого не было ни значения по умолчанию, ни единого вызывающего, поэтому плотная нога отключалась даже при готовых векторах. Теперь обе точки входа берут кодировщик сами. Заодно: построитель ждёт вызываемый объект, а не модель, и обе стороны сравнения получают свой префикс E5 — страница это passage, вопрос это query. Измерено на живом хранилище: поколение с `vector_state: complete`, 1058 источников, 4097 фрагментов, 251 с; русские вопросы, которых нет в текстах дословно, давали 0 из 4, стали давать по 3. Осталось: ответы однообразные, первым идёт один и тот же большой русский документ по три-четыре фрагмента — нужна дедупликация по странице в слиянии. Ради этой правки разобран `scripts/evidence_graph_builder.py` по правилу 5: 12 замечаний, включая функции сложности 68 и 78, приведены к нулю пятью партиями без изменения поведения — 125 функций, средняя сложность 3.0, 140 тестов сборки зелёные.

- 2026-08-23 — Доделано разнообразие результатов, без которого многоязычный поиск оставался бесполезным: на каждый русский вопрос все места занимал один большой русский документ своими же фрагментами, английские страницы решений не появлялись вовсе. Практика называет это прямо — верхние фрагменты кучкуются вокруг одних и тех же абзацев, и читатель получает один ответ, повторённый трижды, а не втрое больше сигнала. Обычные средства (MMR, семантическая дедупликация) сравнивают кандидатов между собой; здесь это лишняя цена, потому что дублирование структурное — несколько фрагментов одной страницы. Слияние поколения теперь ставит вперёд по одному фрагменту на страницу, остальные идут следом: ничего не выбрасывается, меняется только порядок, лишних вызовов модели нет. Замер сразу после: четыре русских вопроса дают по четыре разные страницы, нужная английская страница решения стоит первой на «как устроен повтор после карантина» и второй на «зачем нужна аренда владельца для языкового сервера».

- 2026-08-23 — Первый шаг по `OPEN-006`: плагин OpenCode переведён во владение установочной транзакции. Раньше его копировали вне транзакции, поэтому удаление установки оставляло файл на месте — он продолжал указывать на хранилище, которого уже нет, а откат не мог вернуть то, что было до. Теперь плагин пишет та же транзакция, что и блок в профиле с таймером, и uninstall его забирает; установщик определяет OpenCode до шага установки, а не после, иначе транзакция о нём не узнала бы. Проверка — `test_the_opencode_plugin_is_written_and_taken_back_by_the_transaction`. Ради правки разобран `scripts/installer_config.py` по правилу 5: девять замечаний, включая сканер комментариев сложности 19 и чтение вывода процесса сложности 24, приведены к нулю тремя партиями — 62 функции, средняя 2.9. Осталось в этом пункте: хуки Codex и настройки Claude сливаются по содержимому и требуют владения фрагментом, как у Cursor и Antigravity.

- 2026-08-23 — Владелец снял запрет на автоматические операции Git одним словом — «обновляй», — и хранилище научилось обновлять собственный код. Ночной проход последним шагом двигает checkout на удалённую ветку, и только при доказательствах, которые считает сам: это git-checkout на ветке с remote, `fetch` уложился в срок, полученная голова — строгий потомок текущей, и ни один путь, который затронет обновление, не изменён локально. Последнее правило заменяет обычное «нужно чистое дерево»: здесь дерево грязное почти всегда и законно, потому что рантайм переписывает отслеживаемые индекс и журнал на каждом компайле, — правило «чистое дерево» было бы выключателем в одежде безопасности. Ничего разрушительного нет: ни `reset`, ни `clean`, ни `stash`, ни разрешения конфликтов, ни касания неотслеживаемых файлов, и никогда никакого push; слияние только `--ff-only`. Шаг идёт последним, чтобы изменённый код вступал в силу со следующего прохода. По дороге найден и исправлен свой же дефект разбора: пути брались срезом от колонки статуса `git status --porcelain`, из-за чего у имени отъедался первый символ и конфликт не находился; теперь пути читаются из `git diff --name-only -z`, где нет ни колонки, ни C-кавычек. Решение — `automatic-code-update-decision.md`, исследование — `docs/research/2026-08-23-self-update-without-losing-work.md`; контракты в `CLAUDE.md` и `AGENTS.md` обновлены.

- 2026-08-23 — Закрыт `NEW-61`: недельное обслуживание дописывало OKF-frontmatter в `knowledge/notes/README.md` и `knowledge/projects/README.md` — два отслеживаемых публичных файла, у которых в первой же строке сказано, что README каталогов от этого требования освобождены. Писатель и проверяющий расходились: линт освобождал все редакционные имена, а мигратор — только те, что лежат в корне хранилища. Теперь оба читают один список (`vault_editorial.EDITORIAL_NAMES`). Измерено, что правка ровно такая, как названа: из 685 файлов области редакционных имён 305, и лишь у двух не было `type:` — у тех самых README; сухой прогон даёт `migrate: 0`. Проверка — `tests/test_migrate_to_okf_exemptions.py`. Правка ждала разбора `scripts/migrate_to_okf.py` по правилу 5 (пять замечаний → ноль, 30 функций, средняя 2.5); разбор сдвинул точку записи с `main` на `_write_page`, и поведенческая матрица писателей названа заново, а её диспетчер на 24 ветки заменён реестром драйверов.

- 2026-08-23 — Настройки Claude переведены во владение установочной транзакции (часть `OPEN-006`). Раньше их сливал отдельный скрипт вне манифеста, поэтому uninstall оставлял наши хуки в `~/.claude/settings.json` указывать на хранилище, которого уже нет. Владеемая проекция — ровно блоки хуков, все команды которых наши, и два ключа среды; всё прочее в файле не трогается. Разрешения намеренно не во владении: они вливаются в списки, которые правит и пользователь, и отличить нашу копию строки от его нельзя, поэтому забирать их при удалении значило бы снимать чужую настройку. Блок, где наша команда стоит рядом с пользовательской, отклоняется по имени, а не переписывается. Проверено на живой установке чтением, без записи: проекция реального файла совпадает с желаемой по всем семи событиям и обоим ключам, `recognizes` истинно. По дороге закрыты две дыры вчерашней правки OpenCode: путь к плагину звал `opencode_global_dir` с именем `sys.platform`, которое та не принимает (флаг уронил бы установщик на любой платформе), и uninstall пересобирал из манифеста только Cursor и Antigravity, то есть всё остальное в удаление не попадало. Проверка — `tests/test_claude_settings_ownership.py`, десять тестов.

- 2026-08-23 — Хуки Codex переведены во владение установочной транзакции — вторая половина `OPEN-006`. Форма та же, что у настроек Claude, и код общий: у обоих хостов хуки лежат как `{событие: [блок]}`, и вопрос владения один — какие блоки целиком наши; различаются только признак «обработчик наш», что ещё в файле наше, и имена отказов. Отдельный вопрос — можно ли вообще писать `hooks.json` при встроенных хуках в `config.toml` — остался прежним, но перестал совмещаться с записью: команда `codex_memory hooks-state` только отвечает, ничего не трогая, и установщик спрашивает её до транзакции; владение включается лишь при ответе `absent`. Codex на этой машине не установлен, поэтому живого доказательства как у Claude нет — проверено лишь, что проба отвечает `absent`. Проверка — `tests/test_codex_hooks_ownership.py`. Правка ждала разбора `scripts/codex_memory.py` по правилу 5: 13 замечаний на 27 функциях (худшая 29) приведены к нулю тремя партиями без изменения поведения.

- 2026-08-23 — CI поймал третью дыру той же правки, и она была в тесте, а не в продукте: пять шардов Windows на всех версиях Python падали на проверке владения плагином OpenCode. Плагин — JavaScript, путь в нём лежит строковым литералом, поэтому на Windows каждый обратный слэш экранирован, а тест сравнивал с сырым путём. Ожидание теперь строится тем же `json.dumps`, что и сама подстановка.

- 2026-08-23 — Закрыт `NEW-05`: тонкие хуки `session_end_capture.py` и `precompact_capture.py` теряли сессию, если не удавалось запустить фоновый процесс. Через адаптер это ничего не значило — долговечное намерение публикуется до фоновой работы, — но при прямом вызове за хуком не стояло ничего, и оставался только печатный `flush_started: false`. Теперь провалившийся запуск сам публикует намерение через ту же машинерию, и очередь доигрывает захват в следующей сессии; след о потере пишется только если и это не удалось, то есть означает настоящую потерю. Порядок важен: намерение забирает текст из файла до удаления эфемерного транскрипта. Граница чтения не ослаблена — путь вне трёх доверенных корней отвергается по-прежнему. Проверка — четыре теста в `tests/test_capture_hooks.py`, включая сквозной на принятом V3-хранилище без единой подмены, кроме падающего запуска.

- 2026-08-23 — `OPEN-034` наконец измерен на настоящих сессиях, и пункт при этом не закрыт — по честной причине. Прежняя запись говорила, что корпуса реальных сессий здесь взять негде; это устарело 2026-08-21, когда vault и исходник свели в один каталог: на машине лежат 65 настоящих транскриптов. Сборщик `benchmark/build_flush_corpus.py` строит из них корпус, а метки ставит отдельная рубрика, которая не видит ни уровней продукта, ни его промпта, — иначе система размечала бы сама себя. Корпус с настоящим текстом сессий не коммитится, публикуются только числа. Метки оказались неустойчивы: те же 40 сессий, изменилась только длина выдержки — и распределение уехало с 20/6/14 на 5/3/32, поэтому «точность 0.45» или «0.825» публиковать как факт нельзя; нужен просмотр человеком, и корпус к этому готов. Зато нашлось то, что от меток не зависит (`NEW-62`): при штатном бюджете продукт сохранил 1 сессию из 40, при втрое меньшей выдержке — 4 из 40, и ни одного ложного повышения. То есть чем больше он видит, тем меньше сохраняет: классификатору отдают хвост транскрипта, а у длинной сессии хвост — это инструментальный шум, решения остались раньше и в окно не попали. Правка не сделана: это политика того, что хранилище помнит, и решение за владельцем.

- 2026-08-23 — Владелец спросил, есть ли решение лучше, и второе исследование это проверило: публичные рекорды систем памяти разобраны аудитом (испорченная разметка, снисходительный судья, корпус, влезающий в одно окно, воспроизведения 92,3% → 38,4% и 84% → 58,4%), а независимая работа на одиннадцати наборах данных говорит, что известные системы не обыгрывают стабильно обычный поиск по тому же материалу. Выдерживает проверку одно: хранить материал и искать по нему. Поэтому сессия теперь сохраняется целиком — редактированная копия в `knowledge/raw/sessions/<дата>/<id>.md`, реплики дословно, вызовы инструментов одной строкой, вывод инструментов не хранится. Запись идёт до классификации и не зависит от уровня; классификатор решает только, делать ли страницу. Причина в числах: на 40 настоящих сессиях он сохранял одну, а сравнение, меняющее только способ хранения, даёт разговору 16–22 пункта над выжимкой. Осталось: записи ещё не входят в поколение корпуса — для этого сборщику нужен вид источника `session`, а `scripts/corpus_snapshot.py` требует разбора по правилу 5. Решение — `session-evidence-retention-decision.md`, исследования — две записки от 2026-08-23.

- 2026-08-23 — Заведена дорожная карта памяти (`MEM-01`…`MEM-09`) по двум исследованиям дня и обзору области, который прислал владелец, с общим правилом: пункт закрыт не когда написан код, а когда измерение на настоящих данных этого хранилища показало выигрыш против простой базы. Сразу закрыт `MEM-01`: записи сессий входят в поколение корпуса — сборщик обходит `knowledge/raw/sessions` наравне с заметками, вид источника распознаётся и при пересборке фрагментов из байтов. Вес решён авторитетом, а не типом: у записи `source_authority: session` весом 0.9, потому что слова владельца в ней настоящие, но неотредактированные, — собранная из них страница обязана стоять выше. Замороженные метрики retrieval-v2 не тронуты: источников этого вида в базовом корпусе нет. Ради правки разобран `scripts/corpus_snapshot.py`, самый критичный модуль: около двадцати пяти замечаний правила 5, включая валидатор сложности 45 и два обходчика каталогов на 28 и 27, приведены к нулю тремя партиями без изменения поведения — 176 функций, средняя 2.9.

- 2026-08-23 — Закрыт `MEM-02`: ночной проход получил шаг, который читает записи сессий за вчерашний день и превращает их в одну запись дневника — дальше её подхватывает обычный компайл со своими квитанциями и транзакциями, второго писателя нет. Повышение проверяется, а не принимается на веру: каждый пункт обязан процитировать строку, которая действительно есть в записи дня, иначе он отбрасывается; это та самая проверка, которую обзор требует перед переводом эпизода в долговременное хранение. Живая проверка на настоящей сессии: транскрипт 16 МБ → запись 209 КБ → шесть durable-пунктов, все с подтверждённой цитатой. По дороге найден и исправлен свой же дефект: у транскрипта одна запись бывает в десятки тысяч знаков, поэтому хвост в 60 000 знаков мог начаться внутри неё и не оставить ни одной целой строки — запись сессии теперь читает файл целиком в пределах своего потолка, а не хвост классификатора. День без находок помечается сделанным, чтобы не перечитывать его каждую ночь.

- 2026-08-23 — Закрыт `MEM-03`: у ночной консолидации появился четвёртый вид — правило. Оно принимается только с условием срабатывания и в повелительной форме (`never`, `always`, `must`…), потому что ровно по этим словам `build_guardrails` собирает страницы-паттерны в блок, который внедряется при старте сессии: правило должно дойти до чтения перед действием, а не находиться поиском после. Правило без условия или без инструкции отбрасывается — это урок и хранится как урок. Срок годности берётся с типа страницы: паттерны стареют за 180 дней, чего и требует обзор, чтобы урок одного случая не применялся вслепую. По дороге измерением найден свой же дефект: на запись сессии отводилось 12 000 знаков, и на настоящей записи в 349 КБ модель видела только начало и честно отвечала «ничего durable». Теперь бюджет один на день и делится между записями, а от длинной записи берутся начало и конец с отметкой о пропуске. После правки те же две настоящие сессии дали три процедурных правила и восемь durable-пунктов вместо нуля и четырёх.

- 2026-08-23 — Перед `MEM-04` измерил, доходит ли до ответа то, что уже построено, и измерение важнее самой правки. Первое (`NEW-63`): у CLI и прямого API смысловая нога была выключена по умолчанию — вопрос «почему systemd таймер, а не cron» давал ноль, с ногой четыре. Поправка к собственному выводу: главный путь, инструмент MCP, включал её всегда, так что «многоязычность не доходила до пользователя» неверно — она не доходила до того, кто зовёт CLI напрямую. Теперь включена по умолчанию с явным `--no-semantic`; бенчмарк передаёт флаг сам, поэтому замороженные метрики не двигаются. Цена: 7.0 с лексически, 10.4 с со смыслом при прогретой модели, 19.4 с на первом вызове. Второе (`NEW-64`, открыто): ранжирование. На двух русских вопросах первым идёт статусный документ, а не страница решения; на английском — тесты и код. Утром те же вопросы давали решение первым, но корпус был 1058 источников против нынешних 1190. Значит дело не в регрессе правки, а в слабом весе типа источника: у статусного документа и у решения он одинаковый. Не исправлено намеренно — сначала стенд `MEM-07`, который меряет применение опыта, потом правка ранга.

- 2026-08-23 — Закрыт `NEW-65`, самая дорогая находка дня: смысловой поиск работал от ночной сборки до первого коммита. Пригодность поколения сверялась сравнением всей области репозитория, а среди её полей есть `git_commit`; на хранилище, которое само себя коммитит, это значит «почти никогда», и ответы молча приходили из старого индекса на 65 английских страниц. Идентичность области от коммита не зависит по замыслу — `repository_id` и `checkout_id` выводятся из путей, — поэтому сверка теперь идёт по пяти полям идентичности, а коммит остался в манифесте происхождением. Проверено на живом хранилище: активное поколение с полными векторами снова пригодно. Правка ждала разбора по правилу 5 трёх файлов (`generation_catalog.py` 27 замечаний, `repository_scope.py` шесть, набор тестов шесть) — поведение не изменено.

- 2026-08-23 — Закрыт `NEW-66`, и он объяснил, почему после `NEW-65` попаданий всё равно не было. Вчерашняя правка «по одному фрагменту на страницу» жила в слиянии поколения, а публичный поиск идёт другим путём со своим слиянием и её не звал: написана, покрыта тестом, не действовала ни для кого. Вторая половина дефекта — размер пула: каждая нога просила ровно столько строк, сколько мест в ответе, а в двадцати пяти фрагментах живого хранилища оказывалось три-семь разных страниц, так что выбирать разнообразию было не из чего. Теперь нога просит `max(40, min(limit × 8, 200))`, а порядок ведут первые фрагменты разных страниц; ничего не выбрасывается. Измерено стендом `MEM-07` на живом хранилище: `hit@5` 0.1 → 0.6, ворота пройдены впервые. `hit@1` остался 0.1 — это `NEW-64`, вес типа источника, и он открыт.

- 2026-08-24 — Закрыт `NEW-67`: принятые решения были невидимы для поиска. Сборщик корпуса оставлял страницу, только если её статус буквально `active`, а на принятых решениях этот vault пишет `accepted` — значит четыре действующих решения не попадали в корпус вовсе, у одного из них в поколении было ноль фрагментов. То же правило стояло ещё в трёх местах на стороне запроса, поэтому починка сборщика в одиночку ничего бы не дала, а линт и генератор индекса при этом держали каждый своё определение отставленной страницы. Теперь определение одно (`scripts/page_status.py`), и оно названо списком отставленных слов, а не списком текущих: неучтённое слово оставляет страницу находимой, а не невидимой — для памяти второй отказ хуже, и случился именно он. Проверено пересборкой: 1190 → 1222 источника, 5992 → 6240 фрагментов. Честный итог: стенд от этой правки не вырос (`hit@5` 0.6, `hit@1` 0.1 → 0.0) — узкое место теперь ровно одно, ранжирование. Исследование — `docs/research/2026-08-24-which-pages-count-as-current.md`.

- 2026-08-24 — Вес, решающий порядок ответа, получил второй множитель: что за страница. Раньше считался только авторитет («кто сказал»), поэтому статусный документ и страница решения, которое он комментирует, входили в ранжирование почти на равных. Теперь `decision` 1.25, `synthesis`/`concept` 1.15, `pattern`/`workflow`/`qa` 1.10, заглушка-пробел 0.8, остальное 1.0 — и множитель применяется один раз на кандидата на всех путях: слияние, переранжировщик, лексический путь поколения, легаси. Ничего, кроме заглушки, не понижено: это хранилище отвечает и на вопросы про код. Честный итог измерения: правка работает ровно как названа (страница решения получает 1.6883 и поднимается с седьмого места на пятое), и этого мало — первым остаётся русский статусный документ с отрывом в 2.5 раза. Разрыв не в весе, а в самом сходстве: вопрос русский, страница английская. Средство под это — кросс-энкодер, а он на этой машине не настроен (`LLMWIKI_RERANKER_MODEL` не задан), и включать его — решение владельца. Исследование — `docs/research/2026-08-24-ranking-by-what-a-page-is.md`.

- 2026-08-24 — Владелец велел включить кросс-энкодер, и включить его было нельзя: загрузчик требовал `onnx/model.onnx`, а единственная одобренная матрицей модель на закреплённой ревизии никакого ONNX не содержит — проверено по API Hub на этот самый sha. Значит рантайм-переранжировщик не мог загрузить свою единственную модель ни на одной машине, и отказ был тихим: несобранный бандл возвращает None, поиск оставляет прежний порядок. Загрузчик переведён на `transformers` — ту библиотеку, которую называет сама запись матрицы и которой всегда пользовался бенчмарк; закреплённая ревизия, только локальные файлы и запрет чужого кода сохранены. Измерено парно на одном поколении: без него `hit@1` 0.0 и `hit@5` 0.6 за 120 с на десять вопросов, с ним — 0.1 и 0.7 за 334 с. Цена — 1.15–1.45 с на пару при 512 токенах на четырёх ядрах; глубина 8 даёт то же качество, поэтому умолчание 20 оставлено. Модель включена для этой машины двумя ключами среды. Записано как `NEW-68`.

- 2026-08-24 — Закрыт `NEW-69`, и нашёлся он падением теста, а не чтением кода: главный путь агента не укладывался в собственный бюджет. Профиль на живом хранилище: лексический запрос 11.37 с, из них 9.75 с — проверка поколения, вызванная пять раз за один запрос, при бюджете операции в 10 с. Проверка обходит все 6240 строк и пересобирает ожидаемые из источников, а источники берутся из самого поколения, и каждый вызов перед этим хеширует все артефакты — значит вердикт есть чистая функция байтов и пять вердиктов об одних и тех же байтах различаться не могут. Вердикт теперь запоминается на процесс, ключом — идентификатор, дайджест манифеста и проверенные дайджесты артефактов; ключ заработан хешированием, а не предположен. Вторая половина: необязательная нога имела право потратить весь остаток бюджета, поэтому холодная загрузка модели уносила с собой готовый лексический ответ; теперь ей достаётся половина остатка. Измерено: лексический запрос 11.4 → 1.26 с, смысловой 35 → 2.2 с, путь MCP из таймаута в 2.4–2.7 с. Осталось: первый запрос в процессе платит загрузку моделей и отвечает без смысла — прогрев при старте сервера решает это, но стоит памяти, и решение за владельцем.

- 2026-08-24 — Закрыт `MEM-05`: ответ начал признавать, на чём он стоит. Цитаты проверялись по байтам давно, а конверт вокруг ответа сообщал «покрытие неизвестно» — одну и ту же строку под каждым ответом, при том что у каждой процитированной страницы в заголовке написано, кто её сказал, насколько уверенно и какого она типа. Теперь покрытие — это доля утверждений, все цитаты которых вернулись проверенными, уверенность — она же, умноженная на собственную заявленную уверенность самой слабой из процитированных страниц, а предупреждения называют страницу и причину: низкая уверенность, выведенный авторитет, возраст больше окна, которое объявляет её собственный тип. Окна типов переехали туда, где и так объявлен единственный источник правды о типах. Чего нет намеренно: проверки того, что цитата влечёт утверждение, — это следование, и оно по-прежнему не проверяется и не заявляется.

- 2026-08-24 — Закрыт `NEW-71`, и это была самая дорогая тишина: записей сессий в хранилище ноль при 234 транскриптах на диске. Граница писателя не содержала `knowledge/raw/sessions`, поэтому каждая запись отвергалась как «путь вне всех разрешённых корней», а писатель по контракту никогда не бросает — терять запись плохо, ломать захват хуже. Значит вся ветка памяти по сессиям стояла на источнике, которого не существовало. Девятнадцать тестов этого не поймали, потому что каждый подменял саму транзакцию: проверялось всё, кроме единственной границы, которая отказывала. Теперь во владение писателя добавлен ровно подкаталог сессий, а не весь `raw/`, и есть тест без подмены — он пишет через настоящую транзакцию и смотрит файл на диске. Проверено сквозным прогоном настоящего пути хука: запись появляется. Прошлые сессии этим не возвращаются — их намерения потреблены; перенос 234 транскриптов остаётся решением владельца.

- 2026-08-24 — Владелец разрешил перенос, и 234 прошлые сессии стали записями: `scripts/backfill_sessions.py` пишет ту же запись, что живой захват, через ту же транзакцию и ту же границу защиты данных, а без `--apply` только печатает цену. 236 транскриптов просмотрено, 234 записи (9.9 МБ) за 29.5 с. Перенос сразу вскрыл два дефекта. Первый: извлекатель знаний брал любой отрывок в обратных кавычках за ссылку на символ без ограничения длины, и одна строка экранированного JSON в 20 000 знаков внутри записи роняла всю ночную сборку поколения; теперь отрывок длиннее 512 знаков символом не считается. Второй: аренда обслуживания помечалась потерянной при любом исключении в биении, а в ту же базу писали рабочие захвата — две пересборки подряд выброшены после пяти минут работы; теперь ограда теряется только когда её действительно забрал другой владелец, а занятая база повторяется.

- 2026-08-24 — Обращён `MEM-01`: записи сессий убраны из корпуса поиска. Пункт закрывали на том, что записи входят в поколение; перенос дал это измерить, и измерение сказало обратное — с 236 записями стенд упал с `hit@5` 0.7 до 0.0, все места заняли транскрипты тех самых разговоров, из которых страницы решений и собраны. Понижение веса вернуло 0.4, порядок «собранные страницы впереди свидетельств» не добавил ничего: страница решения к тому моменту не попадала даже в первые сорок кандидатов. Записи остаются на диске, их читает ночная консолидация и находит `grep`, но в индекс не идут; после исключения стенд вернулся к 0.7, ворота пройдены. Что сделало бы их безопасными для индекса — второй ярус или квота на источник в пуле — не построено, и это записано как есть.

- 2026-08-24 — Прошлое хранилища сведено в знание: у консолидации появился режим `--all-pending`, который догоняет каждый день с записями и без отметки. Он сразу вскрыл дефект — день обрезался первыми двенадцатью записями, а остальные молча помечались консолидированными; на перенесённой истории это 171 сессия против двенадцати прочитанных. Теперь день читается партиями по двенадцать, не больше двадцати партий. Измерено: десять дней, 236 записей, 25 партий, 126 долговременных пунктов, каждый с проверенной дословной цитатой и путём к записи.

- 2026-08-24 — Две находки на пути из дневника в страницы. Первая: компайл падал с «пустым ответом» провайдера, а провайдер работал — вызов не укладывался в девяносто секунд, и таймаут возвращал пустую строку, которую принимали за ответ. Вторая нашлась потому, что диагностика компайла перестала называть стадию вместо причины: настоящая причина — «метка времени свидетельства неоднозначна». Партии консолидации получали один момент на весь день, и двенадцать записей дневника оказались с одной меткой, а запись дневника опознаётся именно меткой. Исправлено там, где дефект: каждая партия получает свой момент. Осталось: уже записанные неоднозначные записи такими и останутся — дневник append-only; разрешать неоднозначность по цитате значит уточнять решение о границах записи, а решения неизменяемы, поэтому нужна отдельная страница решения.

- 2026-08-24 — Записано решение: запись дневника адресуется меткой времени, а доказывается цитатой. Прежнее правило требовало, чтобы метку объявляла ровно одна запись, — то есть считало адрес доказательством; после переноса истории двенадцать записей оказались с одной меткой, а дневник append-only, и 126 долговременных пунктов нельзя было собрать в страницы. Практика разделяет эти роли прямо: в модели веб-аннотаций W3C позиция говорит, где находится выделение, а цитата — что в нём сказано, и durable-якорем считается именно цитата. Теперь метка отбирает записи-кандидаты, а цитата решает, какая из них; случай, где цитата встречается в двух кандидатах, по-прежнему отвергается. Прежняя страница помечена superseded, новая — `daily-entry-quote-anchor-decision.md`, исследование — `docs/research/2026-08-24-the-quote-is-the-anchor.md`.

- 2026-08-24 — Три дефекта на пути из дневника в страницы, найденные подряд. Свидетельство длинного дня не связывалось никогда: поиск дневника требовал ровно одного снимка, а у разрезанного дня их десять с одинаковым путём, поэтому источник оказывался пустым. Квитанция части фильтровала свидетельства по пути, одинаковому у всех частей, и получала чужие — своя же проверка области её отвергала; теперь фильтр сверяет дайджест. И нечитаемая квитанция останавливала каждый следующий компайл всего хранилища: теперь такой источник считается нескомпилированным, называется в stderr и собирается заново — доказательство, которое нельзя прочесть, есть отсутствие доказательства, а не повод остановить работу. Осталось: сам компайл этого дня упирается уже не в контракт, а в ответ модели на очень большом дне.

- 2026-08-24 — Ещё две правки и две границы, которые я не стал двигать. Испорченная квитанция блокировала каждый следующий компайл всего хранилища; сначала я сделал так, чтобы нечитаемая квитанция считалась отсутствующей, и тест назвал это неправым — решение закреплено: порча есть ошибка, а не «не скомпилировано». Контракт восстановлен, а выход сделан явным: `--discard-unusable-receipts` называет каждую нечитаемую квитанцию и снимает её; теряется запись о компайле, а не страницы. Так снято шесть квитанций дефектного писателя. Вторая граница — отказ критики по бюджету: деградацию до плана без критики отверг другой закреплённый тест. Значит дальше не правка, а решение: либо меньший размер части дня, либо меньше операций в плане. Итог дня честный: 126 пунктов лежат в дневнике и уже связываются как свидетельство, страниц из них пока нет.

- 2026-08-24 — Найдено измерением ответа, а не чтением кода: провайдер памяти отвечал голосом оператора. Компайл падал на «не JSON», я снял, что возвращает `claude -p`, и это оказались реплики из этого же разговора в настроенном стиле вывода — «От вас ничего не нужно», — вместо плана по схеме. CLI загружает пользовательские настройки, включая стиль, а системный промпт продукта шёл внутри текста как `<system>…</system>`, то есть как обычный текст, который персона вправе игнорировать. Значит конвейер памяти ломала любая настройка стиля у владельца, и никакой диагностики об этом не было. Вызов изолирован: наш системный промпт передаётся флагом, файлы настроек не загружаются, и оба флага применяются только если CLI их знает. Проверено: на просьбу ответить JSON приходит JSON, а черновик компайла впервые вернул настоящий план на 22 операции.

- 2026-08-24 — Критика длинного дня не влезала в бюджет: черновик 27 143 токена при потолке 27 744, критика тех же шестнадцати операций — 40 543. Заодно измерено, что на русских страницах уходит 1.47 токена на знак, то есть потолок достигается вдвое раньше английского. Практика на такой случай — резать на куски по лимиту и объединять результат; критика теперь идёт партиями, каждая операция рецензируется один раз и целиком, а одна операция, не влезающая сама по себе, по-прежнему отвергается до вызова провайдера. Ради этих двух правок `scripts/llm_client.py` разобран по правилу 5: тринадцать замечаний, включая функцию сложности 41, приведены к нулю (102 функции, средняя 2.9).

- 2026-08-24 — Конвейер замкнулся: после изоляции провайдера компайл прошёл целиком — один черновик и две партии критики, — и в хранилище появилось 23 новые страницы с уроками из перенесённой истории, каждая со свидетельством, привязанным к байтам дневника. Сессия → запись → консолидация → дневник → компайл → страница: впервые всё звено работает на настоящих данных, а не в тестах.

- 2026-08-24 — И сразу измеренная цена: стенд упал с `hit@5` 0.7 до 0.4. В выдаче первым идёт сам реестр аудита — русский документ, который разбирает каждый вопрос подробнее страницы решения, — а следом новые страницы уроков по тем же темам. То есть чем больше система пишет о себе, тем хуже находится первоисточник, и вес типа этого не перекрывает. Правило хранилища «сначала собранные страницы» должно распространяться и на `docs/` при вопросах о знании, но не при вопросах о коде; это условный по намерению вес, и он остаётся в дорожной карте.

- 2026-08-24 — Закрыт `NEW-64`, и правка оказалась не про вес, а про тип. Сборщик корпуса называл `code` всё под корнем кода, включая прозу: исследования, статусные реестры, записки. То есть реестр аудита входил в ранжирование наравне с исходником и обходил страницы решений, о которых сам же и написан. Теперь проза под корнем кода получает тип `doc` с весом ниже нейтрального — это комментарий к решениям, а правило хранилища велит отвечать сначала собранными страницами; настоящий код остаётся нейтральным, поэтому вопросы о коде не задеты. Измерено после пересборки поколения: `hit@1` 0.0 → 0.6, `hit@5` 0.4 → 0.6, ворота стенда пройдены. Нужная страница решения теперь первая в шести случаях из десяти вместо ни одного.

- 2026-08-24 — Побочный эффект той же правки, записан как `NEW-81`: изменение правила типизации обесценивает все опубликованные поколения — их проверка пересобирает ожидаемое по новому правилу и не сходится, каталог не находит ни одного годного кандидата и обнуляет активный указатель. Поиск в этом окне даёт ноль строк; измерено, стенд показал 0.0. Данные не теряются, поколения одноразовы, но вывод для правил такой: менять правило корпуса можно только вместе с пересборкой в том же проходе. И `NEW-82`, открыто: три длинные пересборки из пяти потеряли аренду обслуживания после пяти-семи минут работы, перехватчика поймать не удалось.

- 2026-08-24 — Закрыта половина `NEW-81` — та, из-за которой дефект и остался незамеченным: пустой активный указатель поколения доктор считал нормой. Для молодого хранилища это правда, для хранилища с опубликованными поколениями — потеря основного пути чтения, и ровно в этом состоянии поиск отдавал ноль строк при зелёном отчёте. Проверка теперь считает зарегистрированные поколения: ноль — `ok`, больше нуля при пустом указателе — `degraded` с числом в сообщении, и находка снимается сама при следующей активации. Осталось в пункте автоматическая пересборка при невалидных кандидатах вместо ожидания ночного прохода.

- 2026-08-24 — Закрыт `NEW-83`, найденный собственным линтом сразу после первого удавшегося компайла: 31 страница из 99 проваливала свою же цитату. Измерено, что все 31 дайджеста — это дайджесты частей компайла, а не дня целиком: длинный день режется по границам записей, страница пишется из одной части и называет её, а читатель свидетельства сравнивал только файл целиком. Писатель проверял себя частью в памяти и был прав, читатель проверял файл и был прав, между собой их не проверял никто — и знание хранилища не проходило проверку хранилища с того дня, как дни стали длиннее 16 КиБ, то есть всегда. Читатель теперь ищет выровненный по записям срез, начинающийся там же, где часть, чьи байты дают записанный дайджест; слабее не принимается, правка внутри цитаты по-прежнему валит её. Разрезатель переехал к читателю. Линт на живом хранилище: 42 находки → 11, `invalid_evidence` 31 → 0.

- 2026-08-24 — `NEW-82` проверен и не воспроизведён: одна принудительная пересборка под наблюдателем прошла за 272 с на 1340 источниках, и за 89 снимков строки владения ни токен, ни pid, ни эпоха не изменились. Ночной проход не терял аренду ни 22-го, ни 23-го, ни 24-го. Значит терялись те пересборки, что шли одновременно с другим моим прогоном, — обычный проигрыш гонки. Вместо догадки сделана диагностика: потеря аренды теперь называет проверку, которая её увидела, и содержимое строки — pid, эпоху и отметки, — потому что три разные проверки поднимали одну голую строку и отчёт не мог сказать ничего, кроме факта потери.

- 2026-08-24 — Закрыт `NEW-84`: обратные ссылки перестали ждать человека. Правило взаимной ссылки хранилище требует само, а компайл пишет страницы, которые ссылаются наружу и не правят названные, — значит каждый компайл оставлял находки, снять которые мог только человек. Теперь ночной проход дописывает недостающую ссылку сам: в существующий раздел `## Related` или новым разделом, ничего не удаляя и не переставляя, через ту же транзакционную машинерию, что и остальные автоматические писатели. Границу публикации решает тот же фильтр, что и в линте, поэтому приватная страница не попадёт в опубликованную. На живом хранилище восемь долгов закрыты за прогон: линт 11 находок → 3, после дописанного раздела `## Source` → 2.

- 2026-08-24 — CI на Windows уронил четыре задачи одной проверки границ поколения: ожидаемый отказ происходит, а подмена файла, ради которой тест написан, не выполняется ни разу (`NEW-86`). На Linux все восемь случаев проходят, локальный полный прогон — 6746 пройдено, поэтому воспроизвести здесь нечем. Гипотез две: Windows отказывает в замене файла, который каталог держит открытым, — тогда прав продукт; либо `samestat` на Windows не признаёт дескриптор — тогда не срабатывает крючок. Проверка не ослаблена: падение теперь несёт с собой, сколько раз входили в захват печати, сколько чтений пришлось на нужный файл и какой ошибкой ответила система на подмену. Следующий прогон выберет гипотезу сам.

- 2026-08-24 — Найдено обычным использованием продукта и исправлено (`NEW-87`): на вопрос «как устроен повтор после карантина» нужная страница пришла первой, а места со второго по седьмое заняли журналы проектов вида `project-pytest-603` — артефакты прогонов тестов, лежащие в памяти владельца. Измерено: из 399 записей `knowledge/projects` 384 оказались мусором тестов, у каждой в происхождении путь `/tmp/pytest-of-user/...`; настоящих проектов двенадцать. Причина в том, что `conftest.py` прибивает корень хранилища к этому же checkout, а после слияния каталогов 2026-08-21 этот checkout и есть живое хранилище: два теста адаптера подменяли корень состояния, но не корень хранилища, и каждый прогон добавлял две-три записи. Мусор перемещён в одноразовый `cache/quarantine/`, а не удалён; обоим тестам выдан собственный корень; в conftest появился сторож, который роняет прогон, называя любую новую запись в `knowledge/` этого checkout.

- 2026-08-24 — Закрыт `NEW-86` — и закрыт не догадкой, а тем, что падение научили называть причину. Четыре задачи Windows валились на `assert False` без единой подробности; после правки первый же прогон сообщил: `WinError 5`, отказ в переименовании поверх `artifact-0000.bin`, пока каталог держит его открытым. То есть продукт прав — держать файл открытым и значит не дать его подменить, — а тест на этой платформе не может поставить свой опыт: падали ровно те четыре случая из восьми, где подмена делается переименованием. Утверждение теперь принимает оба исхода явно: подмену поймала печать поколения либо в подмене отказала сама система, что есть та же гарантия слоем ниже; на POSIX требование к печати не изменилось.

- 2026-08-24 — Сторож против записи тестов в живое хранилище пришлось сделать неравномерным, и научил этому единственный оставшийся отказ полного прогона: пока шли тесты, живой сеанс в соседнем проекте дописал журналы `соседнего проекта` и `другого проекта`, и строгий сторож назвал работу владельца течью. Теперь проекты сверяются по именам — утечка теста создаёт новый проект, а живой сеанс дописывает существующий, — заметки пофайлово, потому что туда пишет только ночной компайл, а дневник и записи сессий не наблюдаются вовсе. Полный прогон: 6747 пройдено, 249 пропущено.

- 2026-08-24 — Закрыт `NEW-82`, и причина оказалась своей, а не чужой. Ночной проход воспроизвёл потерю аренды обслуживания, а добавленная накануне диагностика назвала её одним взглядом: проверка `require`, в строке нулевой pid и отметки-сентинелы, эпоха — наша собственная. Это след освобождения, то есть аренду отпустил не перехватчик, а мы сами. Корень: когда хранилище пишут во время чтения корпуса, сборка бросает `CorpusChanged` и продукт перезахватывает корпус ещё раз, но повтор жил за пределами блока аренды, а выход из блока её освобождает — повтор предъявлял уже отпущенную аренду. На хранилище, где капча дописывает непрерывно, это обычный случай, и так провалились три пересборки из пяти и ночной проход, каждый раз после четырёх минут работы. Повтор перенесён внутрь той же аренды, внешний удалён как мёртвый, второй `CorpusChanged` честно откладывает проход. Заодно отказ `error: ValueError` перестал быть словом без причины.

- 2026-08-24 — Закрыт `NEW-90`, найденный тем же способом: отказ научили нести сообщение, и он назвал себя — `publication root does not match generation repository scope` после 268 секунд сборки. Сверка корня публикации сравнивала всю область репозитория целиком, а в ней есть коммит; хранилище коммитит само себя, поэтому любая сборка, пережившая коммит, публиковалась «в другой репозиторий». Это `NEW-65` в другом месте: и там, и здесь вопрос один — тот ли это checkout, — и теперь оба сравнивают идентичность, а коммит остаётся происхождением.

- 2026-08-24 — Живая проверка обеих правок пересборки: аренда больше не теряется — проход прожил два полных захвата подряд под одной арендой, 515 секунд, — и исход стал честным: `deferred: corpus_changed`, потому что хранилище писали и во время повторного захвата. Это записанный контракт, и он верен: при непрерывной записи полная пересборка не может закончиться на устойчивом корпусе, а тихое окно есть у ночного прохода в три часа.

- 2026-08-24 — Остаток `NEW-81` закрыт выводом, а не кодом: пересборка поколения «по требованию», когда каталог не нашёл годных кандидатов, на живом хранилище чаще всего не закончится. Измерено: полная сборка стоит 4.5 минуты, а при непрерывной записи откладывается по `corpus_changed` — так закончились три попытки подряд, включая одну на 515 секунд с двумя полными захватами. Тихое окно есть у ночного прохода в три часа, и он собирает; поэтому правильный ответ здесь — ночь, а не немедленная пересборка в рабочие часы.

- 2026-08-24 — CI поймал то, чего эта машина увидеть не могла: шард 3 падал на всех платформах, потому что тест редактирования секретов запускает адаптер по-настоящему, и журнал проекта уходил в живое хранилище под слагом `[redacted-api-key]`. Здесь такой каталог уже лежал с прошлых прогонов, поэтому сторож по именам молчал, а в чистом клоне имя оказалось новым — сторож сработал как задуман. Тесту выдан собственный корень, включая переменную среды, которую читают дочерние процессы. Вторым упал тест доски задач на Windows с истёкшей арендой заявки: тридцать секунд по умолчанию не переживают загруженный раннер, а тест не про истечение — однопроцессные заявки переведены на названную длинную аренду, как уже сделано в многопроцессном помощнике того же файла.

- 2026-08-25 — Закрыт `MEM-09`: записи сессий стареют из активного дерева. Они не старели никогда — 236 записей, 11 МБ, около мегабайта в день, — и теперь запись старше девяноста дней переезжает в `knowledge/raw/sessions/archive/<YYYY-MM>/<дата>/`: байты те же, имя то же, `grep` находит на каталог глубже, ничего не удаляется и не сжимается. Окно взято из собственного архивного контракта, а не из обзоров наблюдаемости с их тридцатью днями: консолидация читает вчера, а человек тянется на недели назад. Шаг стоит в недельном проходе рядом с архиватором страниц. Заодно закрыт `NEW-89`: нижний вход в поиск тоже берёт смысловую ногу по умолчанию, и ловушка для прямого вызывающего исчезла.

- 2026-08-25 — Закрыт `NEW-85`, и по дороге нашёлся `NEW-92`. Проверки доктора шли в порядке объявления, корпусная стояла первой и съедала весь бюджет — шесть проверок за ней сообщали «не выполнена», то есть находку про часы. Теперь дешёвые идут первыми, корпусная последней и берёт остаток; замер: индекс 0.06 с, остальное меньше сотой, поколение 2.77 с. Второй дефект серьёзнее: `run/state.json` дорос до 257 КиБ при читательском пределе доктора в 256, и проверки планировщика и захвата не читали состояние вовсе — росли карты дедупликации и редьюсеров, у которых был предел по числу записей, но не по байтам. Писатель теперь держит файл под тремя четвертями читательского предела, вытесняя старейшие записи только этих карт. На живом хранилище доктор при умолчании в пять секунд снова отвечает по существу по всем шестнадцати проверкам.

- 2026-08-25 — По `OPEN-002/003/009` расширен список известных форм секретов. Обзор обновлений секрет-сканера GitHub за 2026 год даёт два урока: префикс — это и есть дешёвый сигнал, и префикс не называет вендора (`sk_live_` — Stripe и APIDeck сразу). Добавлены пять остальных префиксов GitHub и `github_pat_`, подчёркнутые ключи `sk_live_/sk_test_/rk_live_/rk_test_`, которых правило `sk-` не видело вовсе, а также `xapp-`, `npm_`, `hf_`, `pypi-`, `GOCSPX-`; замена обобщённая, без заявления о вендоре. Граница токена прежняя, поэтому обычный слаг страницы не трогается. Ограничение записано как есть: это редактор секретов, а не DLP, и полного списка шаблонов не бывает.

- 2026-08-25 — Закрыт `NEW-88`: холодный запрос из CLI стоил 47 секунд. Разложение в одном процессе показало, что платят за загрузку моделей, а не за поиск: только лексика 2.9 с, со смыслом 10.7 с, с переранжированием 30.3 с, второй запрос 2.5 с. Кросс-энкодер стоит около двадцати секунд на холодный процесс и покупает `hit@1` 0.0 → 0.1 и `hit@5` 0.6 → 0.7; резидентный сервер MCP платит это один раз, одноразовый вызов CLI — каждый раз. Поэтому CLI больше не грузит переранжировщик без флага `--rerank`, а путь MCP не изменился. Замер после правки: 13.3 с вместо 47.

- 2026-08-25 — Закрыт `MEM-06`. Половина пункта уже работала и не была записана: страница, которую читают, остаётся живой и после своего окна — возраст только открывает вопрос, обращение на него отвечает. Не хватало обратного хода, и обзоры 2026 года называют его прямо: у памяти три состояния — активное, спящее, списанное, — и переход реактивации возвращает спящее. Архивация здесь и есть спящее состояние, поэтому появился `archive_stale.py --restore <slug>`: страница возвращается в свой каталог и теряет строку `status: archived`. Автоматической реактивации нет намеренно: обращение к архивной странице не должно воскрешать её молча.

- 2026-08-25 — Закрыт `MEM-07`: появился второй стенд, который меряет применение, а не припоминание. Он спрашивает не «есть ли нужная страница в первой пятёрке», а дошёл ли до читателя тот токен, без которого не сделать: флаг, интервал, граница. Семь задач, токены проверены на дословное присутствие в золотой странице, база сравнения та же — `grep` по файлам хранилища. Измерено: продукт 0.857 против `grep` 0.429, выигрыш +0.43, ворота пройдены; единственный промах — страница, написанная сегодня, а поколение с тех пор не пересобиралось. Лист задач исключён из подсчёта: он несёт все ответы, и найти его значило бы измерять измерение. Чего стенд не меряет и не заявляет — применит ли агент прочитанное.

- 2026-08-25 — Закрыт `MEM-04` измерением, а не кодом. Первый замер — добавить к пятёрке соседей по ссылкам — дал ровный ноль: `hit@5` 0.600 и `hit@1` 0.600 с обходом и без, стенд применения 0.857 в обоих случаях; но он был слаб, потому что соседи дописывались в хвост и никого не вытесняли. Второй честнее: достижима ли пропущенная страница за один шаг от выдачи — из четырёх промахов на десяти вопросах достижим один. Значит потолок идеального обхода `hit@5` 0.6 → 0.7, и покупается он разбавлением пятёрки соседями, которые вытеснят настоящие попадания. Цепочка не побеждает top-k на наших вопросах; повторить замер стоит, когда связей между страницами станет больше.

- 2026-08-25 — Вес, решающий порядок ответа, стал условным по намерению вопроса (`NEW-96`). Правило «сначала собранные страницы» теперь распространяется на всё, что живёт с кодом, — но только когда вопрос не про код. Измерение объяснило, почему прежней правки не хватало: тип объявляет сама страница, поэтому спецификация под `docs/` с `type: decision` получала 1.25 и обгоняла страницу решения, которую комментирует, а листы вопросов под `benchmark/*.json` типизированы как `code` и стояли первыми на трёх вопросах стенда из десяти. Теперь предикат по уже вычисленным намерениям выключает приор, если вопрос назвал путь, файл, символ, зависимость, структуру или влияние, а при включённом приоре источник под корнем кода берёт ту же комментарийную величину, которую таблица уже называет для `doc`; таблица одна, точка применения одна — `_weigh_by_trust` внутри `fuse_rrf`, — и корпус не тронут, поэтому пересборка не нужна и обе стороны замера идут на одном поколении. Парно, кросс-энкодер выключен с обеих сторон: `hit@1` 0.30 → 0.50, `hit@5` 0.70 → 0.70, стенд применения 0.857 без изменений, ворота пройдены везде, ни один вопрос не упал. Честная граница: с включённым кросс-энкодером те же три вопроса стояли первыми и до правки — там она не покупает ничего; в стоковой установке он выключен, пока не заданы две переменные среды, и правка адресована именно этой конфигурации. Вопросы о коде не меряет ни один стенд, их неизменность закреплена тестами. Исследование — `docs/research/2026-08-25-intent-conditional-ranking-weights.md`.

- 2026-08-25 — Закрыт `OPEN-004`: выпуск 4.0.0 наконец опубликован. Версия несла 4.0.0 с тех пор, как легла платформенная работа, но тега не было ни разу, и по собственному правилу установщика состояние было непубликуемым: удалённый bootstrap принимает только 40-символьный OID и отвергает имена веток и тегов, а OID нигде не назывался. Тег `v4.0.0` стоит на коммите `6c84811`, где CI дал 49 из 49, а локальный прогон — 6769 пройденных; `scripts/release_manifest.py` печатает этот OID вместе с SHA-256 каждого файла, который bootstrap запускает, и манифест лежит в `docs/RELEASE-v4.0.0.md`. Все три README рассказывают, как проверить выпуск. Слияние PR #3 в `main` осталось за владельцем: команда слияния отклонена политикой окружения.

- 2026-08-25 — Закрыты `OPEN-001` и `OPEN-007`: перенос памяти на новую машину перестал заканчиваться ручным `cp -r`. Это была единственная часть переезда без проверки, без отказа и без записи. Команда `publish` берёт проверенный образ и раскладывает его в установленный vault: образ валидируется заново, поэтому правка между restore и publish отвергается, а каждый путь назначения обязан отсутствовать или совпадать побайтно; первый конфликт останавливает публикацию целиком. Файл создаётся эксклюзивно, поэтому появившееся между проверкой и записью тоже становится отказом, а не перезаписью. Слияния и флага «перезаписать» нет намеренно: замена заполненного хранилища остаётся действием человека.

- 2026-08-25 — Остаток `OPEN-005/006` закрыт не владением, а честным отчётом. Владеть checkout и venv значит обязать uninstall их удалять, а удалить checkout — удалить хранилище владельца; такое решение принимает он, а не установщик. Но молчать о них тоже неправильно: после удаления установки человек либо находит их случайно, либо теряет молча. Теперь `install_control.py status` называет их прямо — вид, путь и есть ли он на диске — как то, что установка создала и никогда не удалит.

- 2026-08-25 — Сужен `OPEN-017`: цитата обязана сойтись с числом утверждения. Проверки следования нет и она не заявляется, но самый опасный вид нерелевантной цитаты — со страницы той же темы, из соседнего предложения, с другим числом — теперь отвергается: если и утверждение, и цитата называют величины (число, версию, флаг), хотя бы одна обязана совпасть; идентификаторы в обратных кавычках величиной намеренно не считаются, потому что страницы этого хранилища набиты путями и именами функций, а ложный отказ для памяти дороже слабой цитаты. Это ровно тот случай, на котором действует оператор: «аренда истекает через 30 секунд», подтверждённая строкой про обновление каждые 10. Там, где цитата не называет величин вовсе, гейт молчит намеренно: число может быть написано словами, и отказ такой паре отверг бы правильные ответы.

- 2026-08-25 — Спросил у продукта то, что он должен уметь, и он не ответил: `query_memory.py "как устроен повтор после карантина"` возвращал пустой манифест, хотя поиск на тот же вопрос отдавал нужные строки. Три дефекта на одном пути, каждый прятал следующий (`NEW-93`). Ответ снимал корпус без единого корня кода, а кандидаты приходили из поколения, построенного по одобренным корням, — все пять кандидатов лежали под `docs/` и ни один не находился в снимке; определение корпуса теперь одно, снимок 597 источников стоит 1.0 с. Дальше весь отбор был обязательным для компилятора, и длинные страницы не влезали в бюджет (4384 против 4330) — теперь слабейший кандидат отбрасывается первым, пока остаток не влезет, а пустой отбор остаётся законным. Дальше срок в 30 с: измерено, что один вызов провайдера на этом промпте стоит 32.5 с, поиск и снимок ещё 6.3 с, то есть ни один вызов из CLI не мог закончиться никогда; умолчание 120 с — это измерение. Заодно промпт называет форму воздержания, потому что провайдер объяснял воздержание утверждениями и проверяющий законно выбрасывал весь ответ. Живой прогон: вопрос доходит до провайдера и возвращает честное воздержание с причиной.

- 2026-08-25 — Пересмотрены пробелы доказательств: четырнадцать пунктов `EVID-*` плюс `CROSS-03`, `CROSS-06` и `NEW-51` получили по одному исходу — доказано здесь, доказуемо только в CI, или не доказуемо ничем доступным. Кода не менялось. Доказано на этой машине в изолированных хранилищах под `/tmp`: цепочка inbox → страница → index/log → поиск (`EVID-010`; `search_memory --rebuild` на двух страницах, запрос `fenced lease epoch` даёт нужную страницу первой за 0.136 с) и три случая кристаллизации плейбука — успех, отказ, дубликат — с ровно одной страницей на выходе (`EVID-012`). Наполовину: линт не мутирует фикстуру (SHA-256 по девяти файлам совпал) и даёт 17 находок, но дубликата и устаревания по окну типа не проверяет никто без мутации (`EVID-011`); взаимная провенанс-разметка промоушена проверена, а идемпотентность повтора в процедуре не записана — шаг 4 буквально велит дописывать (`EVID-013`); живой user-systemd закрыт (`Result=success`, `ExecMainStatus=0`, отчёт `logs/nightly-2026-08-25.md`), а Task Scheduler, LaunchAgent и cron — нет (`EVID-014`); реальная многоязычная модель в активном поколении с `vector_state: complete` закрыта, кеш и не-Linux — нет (`EVID-008`). Закрыты прогоном `32834477702` (49 из 49) `CROSS-03` и `CROSS-06`: обе задачи Pyright на macOS и Windows зелёные, а названные тесты не несут маркеров пропуска, то есть выполнялись. `NEW-51` открыт и повторился на трёх других тестах. Главная находка: `export_vault.py` не может выгрузить собственный исходник — обязательный скан отвергает 75 из 622 членов архива на обычных строках вроде `lease_token: str` и ссылки на gist в `README.md`, поэтому `EVID-016` ждёт правки, а не машины. Записано в `docs/DEVELOPER-AUDIT-STATUS-2026-08-18.md`.
- 2026-08-25 — Хранилище нельзя было выгрузить вовсе: `export_vault.py` отказывал на 75 из 622 отслеживаемых файлов, включая `README.md`, все три перевода, `.github/workflows/tests.yml`, `scripts/blackboard.py` и две опубликованные страницы решений. Три разные причины, и все три — про то, что значит совпадение, а не про то, где оно начинается. Первая: правило `ИМЯ <разделитель> значение` редактировало всё, что стоит за именем вида `token`, — а имя обозначает лишь ячейку, и её можно объявить (`lease_token: str`), вычислить (`token = next(iterator)`) или указать на чужой секрет (`GITHUB_TOKEN: ${{ secrets.GITHUB_TOKEN }}`), ни разу не написав секрет. Теперь решает значение: это литерал без синтаксиса вызова, подстановки и запятой, не число, не короче восьми знаков, а без кавычек — ещё и не слово и не ссылка на символ; разделитель не переходит на другую строку, потому что имя с двоеточием в конце строки открывает блок. Вторая: `/` — и символ base64, и разделитель пути, поэтому энтропия склейки `gist.github.com/karpathy/442a6bf…` выдавала за секрет обычный дайджест, который сам по себе намеренно освобождён. Теперь набор с `/` считается путём, если ни один его кусок не является блобом сам по себе; «не блоб» включает набор из одних букв — измерено, что `CreatingLaunchdJobs` в ссылке на документацию Apple даёт 4.04 при пороге 4.0, а base64 случайных байтов не проходит шестнадцать знаков без цифры. Пороги 4.0 и 40 не тронуты, исключений по пути не добавлено. Третье, помельче: `--strict` спрашивал `git status --porcelain` — вопрос шире флага. Он считает неотслеживаемые файлы, которых `git archive` не несёт, и после слияния каталогов считает отслеживаемые индекс и журнал, которые рантайм переписывает на каждом компайле, — то есть отказывал всегда и не называл ничего. Теперь сравнивается то, что несёт архив: `git diff --name-only -z <ref>`, и отказ называет пути. Это та же линия, что у ночного самообновления: вопрос не «чисто ли дерево», а «трогает ли операция изменённое». Измерено: 75 отказов → 30. Оставшиеся 30 — не ложные срабатывания: 26 файлов в `tests/` намеренно держат фикстуры вида ключей, потому что это тесты этой самой машинерии, и четыре несут настоящий base64 — закреплённую сумму `sha512-` установщика Pyright и полезную нагрузку внедрения из корпуса атак. Выгрузка этого репозитория по-прежнему отказана, и отказана по верной причине; снять её может только `allow_fingerprints` по SHA-256 точного содержимого, и это решение владельца. По дороге пойман собственный регресс, которого не видно в поведении: порядок правил оказался несущим. Правила «имя-значение» были первыми шестью в общем списке, поэтому `token=sk-…` сворачивался в `token=[REDACTED]`, а не в `token=[REDACTED_API_KEY]`; вынос их в отдельный проход молча переставил порядок, и одиннадцать тестов упали на маркере — его сравнивают, хешируют и хранят дальше по конвейеру. Там же закрыт настоящий пробел: `ghp_…`, `github_pat_…`, `npm_…`, `hf_…` по форме — обычные идентификаторы, и правило «ссылка на символ» их проглатывало; теперь префикс вендора старше формы, и это закреплено тестом. Страница — `secret-shape-not-secret-name-decision.md`, исследование — `docs/research/2026-08-26-what-a-keyword-adjacent-match-means.md`.

- 2026-08-25 — Сужен `OPEN-017` и закрыт `NEW-94`. Первое: цитата обязана сойтись с числом утверждения — проверки следования нет и она не заявляется, но пара «со страницы той же темы, из соседнего предложения, с другим числом» теперь отвергается; там, где цитата не называет величин вовсе, гейт молчит, потому что число можно написать словами. Второе: тест таймаута установщика падал в полном прогоне и проходил в одиночку — заглушка держала `sleep` на переднем плане, и обработчик сигнала получал управление только после его возврата, а установщик даёт дереву полсекунды до KILL. Проверка не ослаблена: `sleep` ушёл в фон под `wait`, который прерывается сразу, а падение теперь несёт с собой оставленные потомком метки. Шесть одновременных прогонов на четырёх ядрах — шесть зелёных, до правки один из четырёх падал.
- 2026-08-25 — Закрыт `NEW-95`: проигранная гонка дописывания дневника оставалась находкой навсегда. Доктор показывал `transactions (error)`, и три из пяти карантинных записей сделаны сегодня в 09:29:48, 09:31:10 и 09:33:30 UTC — `precondition_failed`, по одному `replace` файла `knowledge/daily/2026-08-25.md`, то есть несколько сеансов писали один дневник разом. Измерено на живом хранилище: у каждой отвергнутой попытки есть сосед `<id>:cas:1` в состоянии `committed` — содержимое не терялось, повтор перезахватывает байты и дописывает блок. Но `parent_transaction_id` у победителя пуст, а дописывание в существующий файл — это `replace`, поэтому и вторая проверка доктора молчала: она смотрит только операции `create`. Обе двери закрыты — находка не снимается никогда, ровно то, против чего заведено `self-resolving-health-findings-decision.md`. Исправлено то, чего не хватало: повтор теперь называет отвергнутую попытку родителем — линия наследования из `idempotent-retry-after-quarantine-decision.md`, поле для которой существовало с самого начала и которое никто не передавал. Улики по-прежнему неизменны. У дефекта нашлась вторая половина, видимая только на настоящей гонке: цепочка бывает глубже одного шага (`:cas:1` тоже отвергается, коммитит `:cas:2`), а доктор читал один шаг — теперь цепочка проходится до конца. Отвергнут вариант считать проигранную гонку не карантином: тот же код поднимают контрольные точки проектов, где карантин — настоящая улика. Проверка — `tests/test_append_race_lineage.py` на настоящей конкуренции без подмен; инсценировать отказ нельзя, потому что `prepare` отвергает устаревшее предусловие сразу, а восстановление докатывает подготовленную попытку вперёд. До правки 5–7 отказов за прогон и столько же незакрытых, после — восемь прогонов, 43 отказа, ноль незакрытых. Живое хранилище этим не чинится: у тех трёх записей повторы прошли до правки, доктор до и после — три незакрытые записи; правка запрещает появление новых.

- 2026-08-25 — Начат разбор `scripts/markdown_transaction.py` под управляемый гейт правила 5 (`/etc/claude-code/enforcement/gate_complexity.py`), который отказывал в любой правке двух файлов. Важное уточнение измерения: локальный `~/.claude/tools/ccn_gate.py` на обоих файлах даёт ноль — у него три проверки (CCN, вложенность, тернарник), а у управляемого есть четвёртая, `[STRUCTURE]`: больше двух `if` на одном уровне, `if/else` там, где хватило бы раннего возврата, и ветвление внутри тернарника. Именно она и держала правки. Измерено до работы: `markdown_transaction.py` 49 находок, `doctor.py` 46 (45 `[STRUCTURE]` и одна `[COMPLEXITY]` — `_codex_config_state`, radon 6 при потолке 5), всего 95. В `markdown_transaction.py` разобрано 25 функций тремя партиями: цепочки булевых проверок сведены в одно `return ... and ...` (`_valid_project_lease_values`, `_valid_intent_fence_values`, `_valid_capture_binding_values`, `_persisted_shape_matches`, `_abort_receipt_matches`), одинаковые по сообщению отказы слиты в один `if` (`_require_recovery_bound`, `_require_gate_wait`, `_require_wait_seconds`, `_require_before_bound`), диспетчеры по суффиксу и по имени предусловия заменены реестром (`_require_approved_suffix`, `_validated_fence_precondition`, `_validated_precondition`), остальное вынесено в именованные подфункции (`_require_bounded_components`, `_require_prepare_operation_id`, `_require_reservation_binding`, `_require_written_content`, `_deletion_state_blocker`, `_require_committed_state`, `_require_committed_duplicate`, `_require_knowledge_changes`, `_settled_or_missing`, `_require_settle_deadline`, `_require_windows_directory_identity`, `_built_operation_record`, `_require_coordinator_v3_upgrade_schema`). Поведение не менялось: порядок и короткое замыкание проверок сохранены, типы исключений и тексты сообщений те же. Измерено после: находок 49 → 24; lizard 542 → 555 функций, средняя сложность 2.7 → 2.7, предупреждений 0 → 0; `ruff check scripts/ tests/` — чисто; наборы `test_markdown_transaction`, `test_markdown_transaction_recovery`, `test_transaction_abort`, `test_project_journal`, `test_compile_transactions`, `test_automatic_writer_integration` — 461 пройден, 5 пропущено. Честный остаток: 24 функции в `markdown_transaction.py` и все 46 в `doctor.py` не разобраны, поэтому управляемый гейт по-прежнему отказывает в правке обоих файлов; список остатка снимается тем же гейтом.

- 2026-08-25 — Разбор `scripts/markdown_transaction.py` доведён до нуля: оставшиеся 24 функции сняты тремя партиями, управляемый гейт на этом файле даёт `TOTAL 0`. Все 24 находки оказались одним и тем же пунктом `[STRUCTURE]` — больше двух `if` в одном теле; ни `[COMPLEXITY]`, ни тернарников, ни глубокой вложенности в остатке не было. Формы правок: одинаковые по исходу подряд идущие проверки слиты в одно условие с сохранением порядка и короткого замыкания (`_coerce_legacy_append_arguments`, `_verified_owner_acl_line`, `_validated_manifest_document`, `_built_operations`, `_validated_promotion_plan`, `_rollback_one_operation`, `_recover_one`); хвост из одной проверки заменён простым тернарником без логических операторов внутри (`_classify_settled_append`, `_promote_preparing`, `_undo_change`); остальное вынесено в именованные подфункции рядом с местом вызова (`_append_other_failure`, `_require_checkpoint_inputs`, `_require_named_target_boundary`, `_require_applicable_state`, `_survived_promotion`, `_recovered_terminal_state`, `_required_state_hash`, `_collect_applied_undo_image`, `_contract_writer_gate`, `_require_nested_gate_owner`, `_require_change_shape`, `_traversable_target`, `_require_retained_undo_images`, `_reconcile_unapplied_operation`, `_require_safe_model_output`). Ни один контракт не тронут: типы исключений и тексты сообщений те же, порядок вычисления и короткое замыкание сохранены, генераторы `writer_gate` и `_nested_writer_gate` по-прежнему делегируют через `yield from`, так что момент выполнения проверок не сдвинулся. Измерено: управляемый гейт 24 → 0; lizard 556 → 571 функция, средняя сложность 2.7 → 2.6, предупреждений 0 → 0; локальный `ccn_gate.py` — ноль до и после; `ruff check scripts/ tests/` — чисто. Прогнаны все 25 наборов, импортирующих модуль (`grep -rl "import markdown_transaction" tests/`): 1460 пройдено, 107 пропущено, ноль падений; `test_queue_v3_corruption` в том числе зелёный. Честный остаток: `scripts/doctor.py` не тронут — им занят другой агент, и его 46 находок держат управляемый гейт на том файле; ни одна из 24 функций не была отклонена по риску поведения.
- 2026-08-25 — `scripts/doctor.py` разобран под управляемый гейт: 46 замечаний приведены к нулю. Сорок пять из них — одно и то же `[STRUCTURE]`: больше двух `if` на одном уровне; сорок шестое — `[COMPLEXITY]` на `_codex_config_state`, где radon считал 6, а lizard не больше 5. Расхождение объяснилось само: у radon ветвлением считается и тернарник, а он там был; функция переписана так, что оба инструмента согласны. Важно, что гейт отказывал ровно той форме, которую правило 5 велит предпочитать, — цепочке ранних возвратов; поэтому правка не выпрямляла код в `if-else-if`, а применяла четыре приёма: цепочка булевых охранников свёрнута в один `return … and …`, два подряд `raise` с одинаковым текстом слиты в одно условие, три подряд `append` по флагу заменены таблицей пар с одним циклом, а хвост охранников вынесен в названную подфункцию. Порядок вычисления и короткое замыкание сохранены везде; там, где условие переставало быть ленивым, оно всегда было чистым (сравнение, чтение поля). Измерено lizard: 547 функций и средняя сложность 3.1 до, 577 и 3.0 после, предупреждений ноль в обоих случаях; ruff по `scripts/` и `tests/` чист. Восемь наборов, импортирующих модуль, — 915 пройдено, 4 пропущено. Поведение доказано не тестами, а самим доктором на живом хранилище: старый и новый код прогнаны подряд с бюджетом 120 с, 13 проверок из 16 совпали побайтно, три разошлись только счётчиками, которые растут сами, — одна новая зафиксированная транзакция и два `age_seconds`, выросшие на 40 и 41 секунду, ровно на промежуток между прогонами. `overall_status`, счётчики статусов и разрешение на удаление `run/` не сдвинулись. По дороге замечено про сам инструмент: доктор при штатном бюджете в 5 с недетерминирован под нагрузкой — два прогона одного и того же старого кода дали расхождение в десяти проверках, потому что бюджет резал их на разной глубине; сравнивать его выводы можно только по одному прогону на тихой машине.
- 2026-08-25 — `CROSS-06` вернулся после закрытия, и эта поправка важнее самой правки. Пункт «мигающие тесты жизненного цикла LSP» закрыли по доказательству повторных зелёных прогонов, честно назвав предел: зелёные прогоны доказывают невоспроизведение за N раз, а не отсутствие мигания. Через двадцать минут прогон CI `32848186105` на `main` (задача `97802678721`, шард `timing::macos_full::py3.10-s2`) уронил `test_terminal_intent_lock_respects_deadline_and_cannot_commit_success` с `assert False is True`. Это и есть то самое повторение; реестр должен говорить «укреплён», а не «закрыт».

  Причина не в продукте, а в том, что тест мерил машину. Окно `finished.wait(0.2)` открывалось до `closer.start()`, поэтому 200 мс обязаны были покрыть запуск потока, возврат планировщика после истечения срока и разматывание двух кадров — всё это сверх самого срока в 50 мс. Продукт свой срок держит точно: `_acquire_terminal_intent` считает остаток и зовёт `acquire(timeout=remaining)`, а `_shutdown_lsp_process` ограничивает тем же сроком каждый предыдущий шаг. Третий вариант — «сигнал живости выставлен после действия», дефект от 2026-08-22 — здесь исключён: `finished.set()` стоит в `finally` и срабатывает даже когда `shutdown` бросает.

  Воспроизвести на этой машине не удалось, и это сказано прямо: 160 прогонов того теста, по 16 и по 32 одновременные копии на четырёх ядрах при load average 28–44, ни одного падения. Правка сделана по измерению, а не по воспроизведённому падению, — и измерение как раз есть. Инструментированное окно показало худшее наблюдавшееся значение 0.1435 с при потолке 0.2: накладные расходы ОС поверх срока доходили до 93 мс при бюджете 150. На macOS-раннере они больше — в этом же файле рядом уже записано, что `Event().wait()` там возвращался с опозданием больше 150 мс.

  Окно теперь выведено, а не выбрано. `_DEADLINE_SECONDS` (0.05) — срок, который тест отдаёт продукту; `_COMPLETES_WHILE_HELD_SECONDS` (5.0) — окно наблюдения; соотношение `_DEADLINE_SECONDS < _COMPLETES_WHILE_HELD_SECONDS < _HOLD_SECONDS` записано в коде и проверяется на импорте, чтобы следующий читатель не сломал его молча. Свойство не ослаблено: замок держат `_HOLD_SECONDS` = 30 с, поэтому любое окно короче удержания по-прежнему валит тот `shutdown`, который вместо своего срока стал бы ждать замок; расширение окна меняет только то, сколько машине позволено тормозить. Тот же голый литерал исправлен ещё в трёх тестах этого файла с той же формой (`0.2` у освобождения драйвера, `0.5` у фатального обратного вызова, `0.25` у осмотра слива), и заодно снят голый двухсекундный лимит удержания, который под нагрузкой отпускал бы поток раньше времени. Отказ теперь называет причину вместо `assert False is True`: «waited 0.062s of 0.0501s for a 0.05s deadline; lock free=False; holder alive=True; closer alive=True; phase=…» — проверено принудительным сужением окна. Прогоны: `ruff` чист, четыре исправленных теста дали 64 зелёных прогона по 16 копий под нагрузкой, весь файл — 215 пройдено при 44 пропущенных.

- 2026-08-26 — Исправлен настоящий дефект порядка в подготовке теста, а не мигание. PR #6, прогон `32852933106`, задача `97818285097` (`timing::focused::pyright-macos`) уронил `test_close_from_reader_handler_does_not_deadlock` с `NameError("free variable 'protocol' referenced before assignment in enclosing scope")`. Окно называется точно: `FakeLspServer.start` запускает поток обработчика пира (`thread.start()`, `tests/fake_lsp_server.py`) до собственного возврата, а имя `protocol` связывается только оператором `protocol = fake_server.start(...)` в теле теста. Пир слал `$/progress` немедленно, поэтому поток чтения мог вызвать обработчик уведомления, чья замыкающая переменная ещё не существует. macOS просто раньше отдавал процессор потоку чтения. Исправлено не ожиданием и не повтором: протокол теперь стартует вообще без обработчика пира, а уведомление шлётся из самого теста после связывания — сначала связать, потом запускать. Предмет проверки не тронут: `close()` по-прежнему зовётся изнутри обработчика чтения, и тест по-прежнему падает при настоящей взаимоблокировке.

  Воспроизведено здесь, а не только рассуждением: та же фикстура и тот же протокол вне pytest, старый порядок — 19 отказов на 300 попыток (6.3%), все `NameError`. Причинность доказана расширением окна: если между возвратом `start()` и связыванием имени вставить 50 мс, старый порядок даёт 20 отказов из 20, новый — 0 из 20. Проверка: `uv run ruff check scripts/ tests/` — чисто; `tests/test_lsp_protocol.py` трижды подряд — 152 пройдено, 1 пропущен каждый раз; `lizard -C 5` на файле — «No thresholds exceeded», 183 функции, средняя сложность 1.9, ноль предупреждений.

  Найдено и не исправлено. Первое: замыкание на `protocol` из обработчика в этом файле только одно — остальные обработчики захватывают списки и события, созданные до `start`, поэтому окна у них нет. Второе: 28 утверждений этого файла имеют форму `assert <событие>.wait(n)` без сообщения, то есть падают ровно тем `assert False` без причины, против которого заведена запись от 2026-08-22; сообщения несут девять. Третье: управляемый гейт (`/etc/claude-code/enforcement/gate_complexity.py`) отказывает всему файлу за `[COMPLEXITY]` примерно тридцати нетронутых тестов — radon считает ветвлением каждый `assert`, поэтому любая цепочка проверок выходит за потолок; локальный `ccn_gate.py`/lizard на том же файле даёт ноль. Разбирать цепочки утверждений в подфункции значило бы ломать читаемость тестов ради инструмента, который меряет не то; правка внесена, отказ гейта записан здесь как есть.

- 2026-08-26 — Закрыт `NEW-97`: самый критичный путь запроса нельзя было править вовсе. Управляемый гейт отклонял любую правку `scripts/retrieval.py` по правилу «три и более `if` на одном уровне», и обход AST показал, что это ровно одиннадцать функций — весь список, а не выборка. Разобраны все теми же средствами, что уже применены к `markdown_transaction.py` и `doctor.py`: цепочка булевых проверок сворачивается в один `return … and …`, ведущий guard уходит в именованный помощник, три условных `append` становятся таблицей, хвост функции — отдельным шагом. Поведение не менялось, и это проверено прогоном, а не чтением: 382 теста семи наборов ретрива плюс проверки качества и обоснованного ответа, `ruff` чист. Заодно записано, что локальный `ccn_gate.py` правила `[STRUCTURE]` не знает и файл пропускал: «прошло локально» не значит «примет управляемый гейт».

- 2026-08-26 — Закрыт `NEW-98`: одна страница занимала несколько мест одного ответа. `_page_diverse` обещает по одному фрагменту на страницу, но до него доходил урезанный пул — переранжировщик читает ограниченный префикс, а всё, что ниже, выбрасывалось. Измерено на живом хранилище: на вопрос про карантин в порядок приходило двадцать кандидатов с четырёх страниц, шестнадцать из них — один и тот же реестр аудита, поэтому последние видимые места не могли не повторить страницу. Это тот же дефект, что закрывали 2026-08-24 у ног поиска, только по другую сторону переранжировщика. Хвост ниже пула больше не выбрасывается, а порядок разбит на ярусы: сначала то, что переранжировщик оценил, потом то, чего он не читал, и внутри каждого — прежнее правило «собранные страницы впереди свидетельств»; повторы идут следом, не выбрасывается ничего. Ярус выбран измерением, а не вкусом: на одних и тех же пулах простое слияние хвоста тоже убирает дубликаты, но пускает неоценённые страницы впереди тех, за которые кросс-энкодер заплатил, и стоит двух случаев применения (0.857 → 0.714). Принятый порядок: повторов 7 → 0, `hit@5` 0.50 → 0.60, `applied@5` 0.857 без потерь.

- 2026-08-26 — Разобрано, почему на этой машине не захвачена ни одна живая сессия, и почему принятый переход на Reliability V3 сегодня запускать нельзя. Симптом: каждый `session_end` печатает `capture skipped`, а в `run/state.json` лежит `adapter_session_end: ReliabilityV3ValidationError: legacy_protocol_unquiesced` — нет ни `run/reliability-v3-migration.json`, ни `run/reliability-v3-adopted.json`. Репетиция на копии живого `run/` (2.0 ГБ, копия снята побайтно: обе базы совпали по SHA-256) воспроизвела отказ дословно, а потом показала то, чего не видно по коду: **сам переход на этих данных не проходит**. `_require_no_v2_ownership` отвергал миграцию координатора, если в `project_leases`, `writer_owners` или `maintenance_owners` есть хоть одна строка; в живой базе их 57 — 56 аренд проектов, 55 из которых истекли 2026-08-21, и один освобождённый владелец `doctor` с сентинелами `0001-01-01`. Строку аренды удаляет только тот, кто её отпускает, поэтому агент, вышедший без освобождения, оставляет её навсегда: правило «есть строка» делает переход недостижимым на любом хранилище, которое хоть раз брало аренду проекта. Хуже: очередь переводится первой, поэтому упавший переход оставил копию в состоянии «наполовину принято» — `run/queue.sqlite3` уже надгробие-JSON, `queue-v3.sqlite3` опубликована, координатор не тронут, `reliability-v3-adopted.json` не написан. Повтор упирается в ту же стену. То есть живой запуск как есть уничтожил бы путь v2 и не дал бы v3 — строго хуже, чем сейчас. Правило переписано на живость, ровно как у очередной половины того же перехода, где отказывает только задача в состоянии `leased`: истёкшая строка не переносится (её и так не переносил никто — сборщик строк v2 эти таблицы не читает) и больше не останавливает переход, а живая по-прежнему останавливает; сомнение отказывает закрыто — отсутствующая или нечитаемая отметка считается живой. Проверка — `tests/test_coordinator_v2_ownership_liveness.py`, восемь тестов. С правкой репетиция на тех же живых данных прошла целиком: переход `ok/adopted`, `session_end` опубликовал долговечное намерение с настоящим транскриптом, записал строку дневника и поставил задачу в очередь, а запись сессии в 358 041 байт зафиксирована настоящей транзакцией в `knowledge/raw/sessions/2026-08-26/11ce8e5b-….md`.

- 2026-08-26 — Живой переход не выполнен, и это отказ по названным причинам, а не незавершённая работа. Первая: флаг `--confirm-all-agents-stopped` — утверждение о состоянии машины, а оно ложно. `$LLM_WIKI_ROOT/scripts/integration_adapter.py` изменён другим агентом сегодня в 14:54:25 UTC, а в координаторе живёт аренда проекта `llm-wiki` с продлением в 14:57:07. Вторая, и она важнее: запись перехода замораживает SHA-256 именно этого файла, а `require_reliability_v3_adopted` перепроверяет его при **каждом** обращении — и капчи, и очереди, и всех markdown-транзакций. Значит любая правка, смена ветки или ночное самообновление, тронувшее адаптер, после перехода кладёт не только захват, а весь путь записи памяти, с отказом `reliability_v3_record_invalid`. Переход можно делать только на байтах, которые больше не поменяются. Третья: после удавшегося перехода доктор объявляет обе базы нечитаемыми (`transaction_state_unreadable`, `queue_state_unreadable`) и запрещает удаление `run/` — он читает легаси-пути, ставшие надгробиями, и принятой раскладки не понимает. Правка живёт в `scripts/doctor.py` и вне области этой задачи; записано как есть. Живой `run/` заархивирован целиком до всего остального: `$LLM_WIKI_ROOT-run-archive-2026-08-26/run`, 2.0 ГБ, обе базы сверены по SHA-256 с оригиналом.

- 2026-08-26 — Найден и остановлен писатель, оставлявший в живом хранилище фикстуры сессий: `knowledge/raw/sessions/2026-08-2{4,5,6}/session-1.md`, по 231 байту, с телом «durable decision». Источник — два теста в `tests/test_flush_classification.py`, которые подменяли `flush_memory.STATE_ROOT`, но не `ROOT`; запись идёт через `write_session_evidence(ROOT, …)`, а корень хранилища conftest прибивает к этому checkout, который с 2026-08-21 и есть живое хранилище. Писатель по контракту не бросает, поэтому утечка молчала. Обоим тестам выдан собственный vault и собственные переменные среды, и теперь они утверждают, что запись легла именно туда. Сторож conftest расширен на `knowledge/raw/sessions` пофайлово — прежде он смотрел только заметки и проекты, и это прямо записано в его же комментарии как сознательный пропуск. Проверено воспроизведением: при снятой изоляции прогон падает со словами `tests wrote into the live vault: knowledge/raw/sessions/2026-08-26/session-1.md`. Цена расширения записана рядом: настоящий захват, завершившийся во время прогона, тоже поднимет сторожа — отличать их предлагается по имени файла, потому что у настоящей записи имя сессии настоящее. Три фикстуры не удалены, а перенесены в одноразовый `cache/quarantine/session-fixtures-2026-08-26/` — так же, как 2026-08-24 поступили с 384 журналами проектов. Расширенный сторож немедленно поймал второй источник, о котором никто не знал: `tests/test_capture_terminal.py` гоняет настоящий `process_new_capture`, а тот тоже кладёт запись по модульному `ROOT` — за прогон появились `2026-08-16/session-1.md` («debug evidence») и сегодняшняя («status only»). Тесту выдан собственный корень. Правильная правка не в тестах: `_keep_session_record` обязан писать в тот vault, к которому привязан переданный ему координатор, а не в модульную константу — но `scripts/flush_memory.py` несёт семь замечаний `[STRUCTURE]` управляемого гейта, и однострочная правка им отвергается. Разбор файла не делался, следствие записано как есть.

- 2026-08-26 — Найдено при этих замерах и не исправлено: числа обоих стендов гуляют между прогонами одного и того же кода — `hit@5` 0.7, 0.7 и 0.6, а единственный промах стенда применения переезжал с одного случая на другой. На одних и тех же пулах кандидатов порядки совпадают до случая, значит гуляет не порядок, а то, что успевает вернуться в пул: необязательные ноги ограничены сроком и под нагрузкой отваливаются. Практический вывод для чтения парных чисел: разница в один случай из семи-десяти не значит ничего, и сравнивать варианты надо на записанных пулах. Там же записано, что лист задач стенда применения исключён из подсчёта токенов, но не из ранжирования, и занимает одно из пяти видимых мест на своих же вопросах.

- 2026-08-26 — `test_multiprocess_status_reads_remain_coherent_during_claim_and_complete` снова упал в CI (прогон `32971299789`, задача `timing::windows_full::py3.11-s4`, Windows/3.11) с тем же `BlackboardConflictError`, что 2026-08-22. На этой машине гонка не воспроизведена, и это сказано прямо: 48 прогонов названного теста на чистом HEAD в изолированной копии, по 8 одновременных копий на четырёх ядрах при восьми спиннерах, load average 19–24 — ноль падений. Но у той же уборки нашлась вторая половина, и она воспроизводится детерминированно.

  Обработчик `_publish_active_claim` перед решением спрашивал поток задач, не приземлилась ли запись всё-таки: `_active_record_present` → `_read_jsonl` → `coherent_read`, а `coherent_read` берёт тот самый глобальный замок писателя, потеря которого и приводит в этот обработчик. То есть проверка отказывает ровно тогда, когда она нужна. При её отказе наружу уходило её исключение вместо исключения вызывающего, `_release_unannounced_claim` не вызывался ни разу, и строки стояли всю аренду. Дальше симптом CI получается сам: ресурс `worker/W/task/I` запрашивает только один рабочий и только на одном индексе, значит конфликт может прийти только от собственных утёкших строк, а конфликт не считается конкуренцией — `_under_contention` поднимает его при первой же встрече. Замер на пустом хранилище: до правки «release attempts: 0», повтор даёт `BlackboardConflictError`; после — «release attempts: 1», повтор берёт ресурс.

  Правка не в размере бюджета, а в том, что ответ стал трёхзначным: приземлилась, не приземлилась, поток нечитаем. Нечитаемый поток больше не выдаётся за приземлившуюся запись; обе половины уборки повторяются в одном цикле; на последней попытке нечитаемого потока освобождение всё равно происходит, и эта цена записана в коде: оставить строки стоит верной, полной и молчаливой блокировки ровно этого набора ресурсов до конца аренды и требует одного условия, а освободить приземлившуюся заявку стоит одной активации, которую некому завершить, и требует двух условий разом. Когда не удалось ни то ни другое, уборка печатает в stderr заявку, проект, ресурсы, число попыток, затраченное время и момент истечения аренды — записать долговечно нечего, замок писателя это и есть то, что отказало.

  Отдельно исправлено то, из-за чего разбор стоил нескольких кругов: отказ не называл причину. `BlackboardConflictError` говорил ровно «blackboard resources are already claimed» — по этой строке нельзя отличить чужую заявку от собственных утёкших строк; теперь он называет ресурс, держателя и его заявку, а `holders` доступны программно. И повтор в тестах при отказе несёт с собой историю: метку вызова, номер попытки из общего числа, затраченное время и все проглоченные ошибки, потому что конкуренция, породившая утечку, к моменту конфликта уже проглочена.

  У самого повтора чтения нашлась цена, которую пришлось ограничить в той же правке: один запрос к потоку берёт замок писателя и может ждать весь его бюджет — на хосте CI это 120 секунд, — поэтому шесть запросов вместо одного умножили бы худший случай на шесть и вынесли бы уборку за срок вызывающего. Теперь запрос делается, только пока уборка потратила меньше `_SETTLE_READ_SECONDS` (5 с); после этого она не спрашивает, а освобождает. Худший случай остался прежним — одно ожидание замка. Проверка — `test_a_slow_stream_is_asked_once_not_once_per_attempt`.

  Проверки: `uv run ruff check scripts/ tests/` — чисто; шесть наборов, импортирующих модуль, — 332 пройдено; пять новых тестов падают на чистом HEAD ровно с `RuntimeError: gate ownership was lost` и проходят после правки, шестой на HEAD выразить нельзя — там нет ограничителя, который он проверяет; `lizard -C 5` на трёх затронутых файлах — «No thresholds exceeded», ноль предупреждений; оба набора под нагрузкой после правки — три круга по шесть одновременных копий, load average 13–18, 18 прогонов, ноль падений. Управляемый гейт на `scripts/blackboard.py` был красным до правки двумя находками `[STRUCTURE]` в `_fold_task_record` и `_fold_conflict_record` — обе сняты, файл даёт ноль; у `tests/test_blackboard.py` остаются две находки `[COMPLEXITY]`, существовавшие до правки, — radon считает ветвлением каждый `assert` и каждое включение, и это тот же случай, что записан 2026-08-26 про `test_lsp_protocol.py`.

  Не доказано и названо как есть: какой именно из двух путей уборки отказал в том прогоне CI — нечитаемая проверка или исчерпанный бюджет удаления. Оба были одной и той же молчаливой уборкой «по возможности», теперь оба повторяются и оба сообщают о себе. Бюджет в шесть попыток примерно за полторы секунды по-прежнему не измерен на настоящем образе Windows: этой машины здесь нет, и выдумывать число для неё я не стал.

- 2026-08-26 — Закрыт `NEW-51` в той его половине, что названа `test_unix_installer_timeout_stops_tests_and_aborts`, и это оказался настоящий дефект продукта, а не тест, ждущий беззаботного исхода. Воспроизведено намеренно, а не ожиданием: 8 одновременных копий теста при 8 счётчиках на четырёх ядрах — 15 падений из 16, каждое ровно на 62 с при 15 с у прохода. Нагрузка снималась во время прогона, а не после: load average 3.70 в середине круга и 7.67–9.84 к его концу. Число 60 с — это `sleep` фальшивого `uv`, то есть ребёнок доживал до конца собственной работы, и `assert not True` об этом не говорил ничего.

  Причина найдена отметками времени, а не чтением. Обработчик таймаута входит вовремя (+15.06 с при сроке 15 с), но `handle_test_timeout` сначала звал `stop_test_timer`, а тот заканчивается на `wait` по заданию таймера. Замер: `stop_test_timer` держал управление с +15.05 до +60.06 — ровно 45 с, и вышел через 0.04 с после того, как ребёнок сам закончил. Инструментированная проверка в тот же миг показала состояние, которое всё объясняет: `ps` и `kill -0` говорят, что таймер уже мёртв, а `jobs -l` всё ещё называет его Running. Оболочка не сняла его статус, потому что находится в ловушке, прервавшей внешний `wait`; `wait` по такому заданию ждёт, пока сменит состояние хоть какой-нибудь потомок, а единственным другим потомком был тот самый смоук, который обработчик и обязан убить. К моменту `stop_test_child` дерево было мертво, `test_tree_alive` честно отвечал «нет», и эскалация TERM→KILL не выполнялась ни разу.

  Правка — порядок, а не срок: сначала `stop_test_child`, потом `stop_test_timer`. Полсекунды до KILL не тронуты, ни одно утверждение не ослаблено. Парный замер на одной нагрузке, чередуя старый и новый порядок: выживший ребёнок 9 из 10 против 0 из 10, у нового порядка каждый прогон 15.15 с и маркер `child.stopped`. После правки сам тест под нагрузкой — 64 прогона из 64 зелёных тремя заходами при load average до 15.95, то есть выше, чем на воспроизведении.

  Та же форма нашлась в `handle_test_signal` и там тоже кусается: набор сигнальных тестов под нагрузкой давал 7 грязных прогонов из 18 на исходном коде и 6 из 18 после правки таймаута — то есть к моей правке отношения не имел, что и проверено возвратом файла к исходному виду. Симптом другой — `TimeoutExpired` у оркестратора, которому дано 10 с, — а механизм тот же: обработчик реапит таймер до того, как остановит ребёнка. Тот же порядок применён и там, и он снял это: 17 из 18 чистых. Заодно закрыто окно, которое сама перестановка и открывала: `trap - USR1` вернул бы USR1 действие по умолчанию, то есть живой таймер мог убить установщик посреди уборки, поэтому на время уборки USR1 игнорируется, а не сбрасывается.

  Падение научено называть причину. Раньше оно читалось `assert not True`; теперь — «the smoke child ran to completion instead of being killed: installer took 60.0s for a 15s deadline against a 60s child; child markers: {...}», и это проверено принудительным возвратом дефекта. Срок и длина сна перестали быть голыми литералами (`_SMOKE_TIMEOUT_SECONDS`, `_FAKE_UV_SLEEP_SECONDS`): разрыв между ними и есть то, чем выживший ребёнок отличается от убитого, и обе величины теперь стоят в сообщении.

  Проверки: `uv run ruff check scripts/ tests/` — чисто; `tests/test_integration_injection.py` целиком — 84 пройдено, 21 пропущено; группа установщика — 14 пройдено, 19 пропущено; `lizard -C 5` на `tests/test_integration_injection.py` — «No thresholds exceeded», 172 функции, средняя 1.9, ноль предупреждений.

  Не доказано и названо как есть. Первое: Windows. Исходный пункт `NEW-51` заведён по двум шардам Windows, а этой машины здесь нет; `install.sh` там не исполняется вовсе, и на `install.ps1` правка не переносится — у него своя модель ожидания. Второе: остаток семейства не закрыт и к правке не привязан — `test_unix_installer_initial_monitor_mode_cleans_stopped_test_tree` роняет выживший `child.pid` при плотной конкуренции разных тестов. Измерено на одной и той же выборке и нагрузке: 2 падения из 18 с правкой и 2 из 18 при возвращённом порядке, то есть от порядка не зависит; тот же тест в одиночку под нагрузкой — 0 из 18. Это сценарий упрямого дерева, которое намеренно игнорирует TERM, под SIGSTOP, и он не тот, который я чинил. Третье: управляемый гейт отказывает всему `tests/test_integration_injection.py` за `[COMPLEXITY]` примерно сорока нетронутых тестов — radon считает ветвлением каждый `assert`; локальный `ccn_gate.py`/lizard на том же файле даёт ноль, сложность правленого теста как была 8, так и осталась. Это тот же случай, что записан 2026-08-26 про `test_lsp_protocol.py`.

- 2026-08-26 — Paid the rule-2 research debt for three decisions shipped the
  same day without dated current-practice research. Three notes added under
  `docs/research/`; no decision was rewritten or superseded, and each page got a
  short **Later evidence** section pointing at its note.

  `adoption-digest-is-provenance-decision` holds and the sources strengthen it:
  no migration framework or provenance format re-derives the digest of the code
  that performed a migration. Flyway does re-check a checksum on every startup,
  but of the migration *script* — nobody hashes the engine and refuses to start
  after upgrading it — and it ships `repair` precisely because a permanent check
  becomes a dead end. Alembic, Django and Rails store an identifier and no
  checksum at all, relying on the same migration-immutability convention this
  decision cites for refusing to re-record. SLSA compares `builder.id` against a
  preconfigured expectation once, at artifact admission, which is exactly the
  split between `_validate_migration_context` and the removed standing check.
  One qualification, measured: the downgrade barrier is real but incidental — a
  pre-V3 reader opening an adopted `run/queue.sqlite3` gets
  `sqlite3.DatabaseError: file is not a database`, but only on the first
  statement that touches a page (`connect()` and `SELECT 1` both succeed), and
  the error names neither the adoption nor a version. npm writes
  `lockfileVersion`, SQLite offers `user_version`; this vault declares no format
  version for a reader to refuse on. Note —
  `docs/research/2026-08-26-what-a-migration-record-binds.md`.

  `bounded-capture-excerpt-decision` holds, and one sentence in it is backwards.
  It offers 31,814 characters as evidence that *tail-only* missed a session's
  decisions; `docs/research/2026-08-25-what-the-vault-decides-to-remember.md`
  and this log record the opposite — those decisions sat "inside a 60 000 tail,
  outside a 30 000 half", so the 60,000 tail caught them and the symmetric
  head+tail missed them, and that change was reverted. The shape survives on
  other grounds the page did not cite: OpenRouter's `middle-out` keeps half the
  messages from each end by default, on the lost-in-the-middle rationale, and
  OpenTelemetry's baseline is truncation with no marker at all, so naming the
  dropped byte count is ahead of the default rather than behind it. What is not
  supported is the 50/50 split, shipped in both
  `integration_adapter.CAPTURE_EXCERPT_SIDE_BYTES` and
  `episode_consolidation._within_share`: the only measured head+tail ratio in
  the literature is 25% head / 75% tail, and this vault's own halves lost a
  session. Retention and classifier input are different questions, so
  2026-08-25 does not settle this one — it just is not the reason the page
  gives. Note — `docs/research/2026-08-26-a-record-too-large-to-keep-whole.md`.

  `retire-cursor-and-antigravity-decision` holds, and is ahead of practice on
  the half projects usually get wrong: keeping the uninstall path is exactly
  what `apt purge` exists for, and the format knowledge has to outlive the
  feature because subtracting a fragment from shared JSON needs it. What is
  missing is the notice stage. Homebrew retires deprecated → disabled → removed
  with a required reason and a year between; Kubernetes keeps a deprecated API
  working at least a year "but usage will result in a warning being displayed".
  This went from supported to removed in one step, and nothing tells an affected
  machine: `doctor` no longer checks those hosts and `inspect_install_state`
  reports only whether the manifest and transaction files exist, not which
  resource ids the manifest names. The removal path is also all-or-nothing —
  reachable only through `uninstall` and `rollback`, so taking back one dead
  Cursor handler means removing the whole installation. The defence is already
  on the page: no such installation is known to exist, and a clock protects
  users who exist. Note —
  `docs/research/2026-08-26-retiring-a-supported-host.md`.
- 2026-08-26 — Три находки одного прохода по живому хранилищу, и все три об одном: смысловая нога не доходит до ответа.

  Первая: путь MCP не мог воспользоваться смысловым поиском никогда. `OPTIONAL_STAGE_MAX_SECONDS` = 0.5 с стоял поверх доли бюджета, которую `_optional_stage_deadline` и так выдаёт, и молча её перекрывал; в код он попал голым литералом без единого замера. Измерено на этом хранилище: тёплая плотная нога одного запроса стоит 0.99–1.33 с, холодная — 8.85 с, то есть обе всегда выше потолка. Шесть вызовов `recall` в одном сервере дали шесть `optional_stage_timeout` и только лексику, а первый вызов не вернул вообще ничего — `TimeoutError: generation catalog deadline reached` ровно на 10.00 с. У находки нашлась вторая половина: модель грузилась в `search()` до входа в поиск, вне границы отменяемой стадии и вне срока вызывающего, поэтому холодные десять секунд списывались с бюджета раньше, чем начиналась работа. Теперь кодировщик резолвится лениво и грузится внутри плотной ноги — внутри уже существующей отменяемой стадии: вызывающий получает лексический ответ вовремя, демон-страглер догружает модель, следующий вызов застаёт её тёплой. Потолок стадии выведен из измерения, а не выбран: 12 с выше холодных 8.85 с с запасом, а доля в половину остатка по-прежнему связывает короткие бюджеты — десять секунд MCP дают стадии пять. Цена названа: обречённая стадия задерживает ответ до 12 с вместо 0.5 с, и только у вызывающего, давшего не меньше 24 с. Замер до и после, шесть вызовов в одном процессе: было 6 из 6 лексических и первый пустой; стало — первый отвечает за 8.80 с, а с третьего вызова `HYBRID` без fallback.

  И честный остаток первой находки, измеренный после правки: на бюджете MCP в десять секунд смысловая нога по-прежнему проигрывает чаще, чем выигрывает, и потолок тут больше ни при чём. Трассировка шести вызовов подряд в одном процессе называет причину поимённо: у каждого запроса две необязательные стадии, а слотов страглеров два (`MAX_OPTIONAL_STRAGGLERS`), и стадия, не уложившаяся в срок, держит свой слот до конца работы, а не до конца ожидания. Первый вызов: обе стадии «deadline reached» при бюджетах 3.80 и 1.90 с — холодная загрузка в них не влезает, — и оба слота уходят страглерам. Дальше вызовы 2, 4, 5 и 6 получают «capacity exhausted» ровно за 0.00 с, то есть до всякого ожидания; выиграл только третий, где слот освободился и тёплая плотная нога стоила 1.18 с. То есть правка сняла то, что первый вызов не возвращал вообще ничего, и дала смыслу иногда доходить, но устойчивым он на этом бюджете не стал. Правка не сделана намеренно: это изменение семантики параллелизма на самом горячем пути, и делать его последним движением без замера нельзя.

  Заодно измерена цена того средства, которое напрашивается, — прогрева кодировщика при старте сервера: 7.99 с к старту и 1108 МиБ резидентной памяти (23.9 → 1131.6 МиБ), после чего кодирование вопроса стоит 0.02 с. Это плата за каждый сервер MCP, включая те сеансы, где смысловой вопрос не задан ни разу, поэтому решение за владельцем, а не за мной.

  Вторая: `query_memory.py "как устроен повтор после карантина"` возвращал `insufficient_evidence` за 32.3 с. Провайдер был прав — нужной страницы в его материале не было; теряла её ретривальная ступень, и сразу по трём причинам. (1) `_TEMPORAL_RE` считал временным любое `после` и любое `after` — голый предлог последовательности без всякой временной привязки, тогда как все прочие варианты той же группы привязаны к дате. Вопрос уходил в профиль `TEMPORAL`, а `TEMPORAL` не объявляет сигнал `dense`, то есть векторы не спрашивали вовсе. Предлог теперь временной только когда за ним стоит время; все закреплённые случаи («decisions since 2025-01-01», «решения с 2025-01-01», «自 2025-01-01 以来的决策») остались `TEMPORAL`. (2) `retrieve_via_search_memory` принимает `generation_embedder=None` и не резолвит его сам, а `search()` резолвит до делегирования — та же ловушка двух входов, которую 2026-08-25 закрыли для флага `semantic` и не закрыли для кодировщика; вход напрямую, а именно так ходит обоснованный ответ, получал `generation_vectors_unavailable`. (3) `query_memory` передавал размер ответа как `max_candidates`, а это ресурсный потолок на выборку каждой ноги: пул схлопывался до двенадцати строк, а в двенадцати строках этого хранилища оказывается три страницы — ровно тот дефект, против которого заведён `_candidate_pool`. Измерено после каждой правки: 6 кандидатов без нужной страницы → 12 кандидатов, десять из них куски одного статусного документа → нужная страница первой. Замер на итоговом коммите: профиль `HYBRID`, двенадцать кандидатов, двенадцать разных страниц, страница решения первая. Граница честности: это доказательство ретривальной ступени, а не всей команды. Сам `query_memory.py` в конце дня дважды подряд на тихой машине не дошёл до ответа — `claude backend exceeded 90s and was stopped`, — тогда как утром та же команда отвечала за 50.3 с с побайтно проверенной цитатой на ту же страницу. Потолок провайдера в 90 с и его разброс — вещь давняя и не из этой правки, но заявлять «команда отвечает» на основании утреннего прогона было бы неправдой. Заодно замечено и не разобрано: процессы-пробы, наплодившие много страглеров, при выходе интерпретатора печатают `terminate called without an active exception` и роняют дамп; в самой команде продукта это не воспроизвелось.

  Третья — решение, а не только починка: worktree агента больше не заводит проект. Слаг проекта берётся из имени каталога, а каталог у субагента — его собственный worktree под `.claude/worktrees/`, поэтому каждый запуск заводил запись `agent-<хеш>`: измерено 2026-08-26, 46 записей `knowledge/projects` из 61. Они занимают места в ответе — девять из двенадцати кандидатов на вопрос про карантин были журналами проектов, семь из них от worktree агентов. Принято: worktree агента — не проект, а временная копия проекта, и слаг берётся у checkout, который им владеет. Разворачивается ровно эта раскладка. Worktree, сделанный владельцем в другом месте, остаётся отдельным проектом: отсюда нельзя узнать, было ли разделение намеренным, а угадывание молча слило бы журналы, которые владелец разделил. Правка стоит в `_compute_slug` — значит, её получает и `integration_adapter`, которого эта задача не трогала, — и в обоих `_resolve_project_dir`. Проверено: этот самый worktree теперь даёт слаг `llm-wiki` вместо `сеанс соседнего проекта`. Честный остаток: правка останавливает рост, но не убирает 46 уже написанных записей — они несут настоящую работу агентов, и что с ними делать, решает владелец; прецедент 2026-08-24 — перенос в одноразовый `cache/quarantine/`.

  Четвёртая нашлась при проверке первой и оказалась тем же дефектом, что и (3), но на главном пути: `_run_vault_search` в `scripts/mcp_server.py` тоже передавал размер ответа как `max_candidates`. То есть инструмент `recall`, которым ходит агент, схлопывал пул каждой ноги до пяти строк. Измерено парно в одном прогретом процессе на одном вопросе, единственная разница — потолок: с потолком пять строк дают три разные страницы и нужной среди них нет вовсе; без потолка — пять разных страниц, нужная первая. Цена названа и разложена: без переранжировщика правка не стоит ничего (5.66 с → 5.32 с, то есть в пределах шума), с включённым кросс-энкодером — 8.34 с → 18.14 с при load average 10, потому что он теперь оценивает двадцать пар вместо пяти. В стоковой установке кросс-энкодер выключен, а на пути MCP он необязательная стадия с собственным сроком, поэтому при десятисекундном бюджете он отваливается в лексику, а не срывает бюджет.

  Пятая, мелкая, но она выбрасывала первый настоящий ответ: провайдер обрамлял JSON тройными кавычками, и `_parsed_answer` отвергал весь ответ. Снимается только целиком обрамлённый ответ; проза вокруг фрагмента по-прежнему отвергается, потому что там неизвестно, что из этого ответ.

  Шестая находка — чужая, но нашёл её мой же разбор, и она хуже всех остальных, потому что не падает, а висит. Проверка границы продюсеров в `tests/test_context_compiler.py` строит алиасы присваиваний до неподвижной точки: `while changed` по всем `Assign` с одним именем слева. Неподвижной точки не существует, если одно имя в модуле присвоено дважды разными значениями, — каждый проход перекидывает привязку обратно, `changed` навсегда истинно. Мой разобранный `_ancestor_name` содержал ровно это (`current = project_dir`, затем `current = parent`), и весь набор `tests/test_context_compiler.py` перестал заканчиваться: на базовом коммите 0.54 с, у меня — больше 400 с и убит по таймауту. Дефект не мой и не про мой код: восемь строк обычного Python воспроизводят его на базовом коммите (`current = a` в одной функции, `current = a.parent` в другой) — «NEVER SETTLED». Правильная правка — сделать проход монотонным (копить множество целей на имя) и ограничить число раундов длиной цепочки; она не сделана, потому что управляемый гейт отказывает всему файлу за четыре доисторических `[COMPLEXITY]` в тестах, где radon считает ветвлением каждый `assert`, — тот же случай, что записан выше про `test_lsp_protocol.py`. Поэтому убрана форма, а не причина: `_ancestor_name` индексирует `parents` вместо обхода с перепривязкой, и рядом записано, почему это несущее. Проверено, что все пять продюсеров, которые читает проверка, сходятся за один-два раунда. Пока гейт не пустит в тот файл, ни один продюсер не имеет права содержать эту форму — это ловушка для следующего автора, и она записана здесь именно поэтому.

  Седьмая, и она прямое следствие разбора: поведенческая матрица писателей в `tests/test_automatic_writer_integration.py` пришпиливает точку записи, а разбор `main` сдвинул её на `_create_project_state`. Матрица названа заново — ровно тот прецедент, который в этом журнале уже записан 2026-08-23 для `migrate_to_okf.py:_write_page`, и сосед по списку до сих пор так и назван. Драйвер того же теста переведён на прямой вызов писателя с его аргументами, как у `migrate_to_okf`; заодно из него убраны две подмены, которые нужны были только пути через `main`. 133 теста набора зелёные.

  Парный прогон обоих стендов на одном хранилище, кросс-энкодер включён с обеих сторон, дал ровное совпадение до знака: `hit@1` 0.5, `hit@5` 0.6, `applied@5` 0.8571 и до правки, и после; ворота пройдены на обеих сторонах. Это не «правка ничего не дала», а находка про сами стенды: они звали `search()` без срока и без `max_candidates`, а все четыре дефекта требовали либо переданного срока, либо прямого входа, — то есть ни один из них стенды поймать не могли и до сих пор не могут. Дыра в измерительном стенде записана как есть; закрывать её этой правкой я не стал.

  Ради этих правок разобраны по правилу 5 три файла, которые управляемый гейт отказывался принимать целиком: `scripts/search_memory.py` (24 замечания `[STRUCTURE]` → ноль), `scripts/session_start_project_state.py` (5 `[COMPLEXITY]` плюс вложенность и тернарники → ноль) и `scripts/mcp_server.py` (16 `[STRUCTURE]` → ноль). Поведение не менялось: порядок и короткое замыкание проверок сохранены, типы исключений и тексты сообщений те же; доказано прогонами — 1134 пройденных при 13 пропущенных по всем наборам, импортирующим `search_memory`, и 517 пройденных при 7 пропущенных по наборам, импортирующим `mcp_server`.

- 2026-08-28 — Актор владения перестал быть машинной учётной записью. Найдено по
  живым счётчикам: самая свежая запись всего следа потерь захвата —
  `owner_identity_conflict`. `maintenance_owners` объявляет
  `PRIMARY KEY(role, scope)`, но `actor_id` UNIQUE, а личность актора на POSIX
  была `posix-uid:<uid>` — то есть на всю машину существовал ровно один
  владелец чего угодно. Воспроизведено герметично на настоящей схеме v3:
  удерживая `nightly/global`, отказано шести ролям из шести — `queue-worker`,
  `capture`, `markdown-writer`, `project`, `doctor`, `repair` — и
  `acquire_compile_owner` вместе с ними, то есть ночной проход отказывал
  компайлу, который сам же и порождает подпроцессом. Два одновременных сеанса
  одного человека сталкивались так же. Актор теперь выводится из пользователя,
  процесса и пары `(role, scope)`; схема принятой базы не тронута, миграции
  нет, потому что при такой личности `UNIQUE(actor_id)` больше ничего не
  ограничивает сверх первичного ключа. Практика на текущую дату говорит то же:
  владение привязано к сессии или процессу, и один держатель штатно держит
  много замков — lease в etcd, сессия ZooKeeper, advisory lock в PostgreSQL,
  где повторный запрос собственного замка не ждёт никогда. Названо и отдано:
  мёртвый ряд чужой роли больше не переприсваивается попутно — его снимет тот,
  кто следующим попросит его же `(role, scope)`; раньше он блокировал всё,
  теперь только себя. Замер после: шесть из шести берутся, компайл выдаётся,
  исключение цело (`owner_busy` на той же паре, отказ второго `nightly` на
  маркере, `owner_identity_conflict` у названного актора с двумя арендами).
  Страница — `ownership-actor-is-the-agent-decision.md`, исследование —
  `docs/research/2026-08-28-who-is-an-actor-in-a-lock.md`.

  Найдено рядом и не исправлено: очередь копит `flush`-задачи, которые не
  проходят — 11 в `ready`, у девяти по восемь попыток, 72 `processor_failed` в
  истории, ночной проход 2026-08-28 сообщил 14 отказов из 14. Причина не
  записана нигде: код один на все случаи, stderr шага пуст. Одиннадцать сессий
  не стали памятью, и по следу нельзя сказать почему.

- 2026-08-28 — Закрыт `OPS-02`: ответ инструмента MCP называет свою цену. До
  этого правило об экономии токенов утверждалось, а мерил его только стенд
  постфактум. Теперь в конверте есть оценка токенов с названным методом,
  время, бюджет и список отработавших и отказавших необязательных стадий с
  причиной. Оценщик переиспользован, а не написан второй раз. Цена самой
  телеметрии измерена и названа: 23 токена, 51 со строкой стадий; на самом
  дешёвом ответе это 22.8%, поэтому блок прикрепляется только когда стоит не
  больше процента описываемого ответа, а отсутствие читается как «не
  измерено», не как ноль — правило дословно от OpenTelemetry GenAI. В первый
  же день телеметрия нашла то, чего не показывал ни один стенд: три
  одинаковых `recall` в одном процессе стоят 323 148 и 12 525 токенов —
  25.8× разницы на одном вопросе, когда отваливается плотная нога, — и блок
  называет причину. Правка размера запасного пула не сделана.

- 2026-08-28 — Отказавший обработчик очереди перестал молчать. В истории
  живого хранилища 72 записи `processor_failed` и одиннадцать задач `flush`
  по восемь попыток, а причины нет нигде: рабочий гоняет обработчик в
  дочернем процессе, и кадр намеренно сплющивает любое исключение в один
  байт, чтобы через трубу не шёл трейсбек. Контракт верный, потеря причины
  вместе с ним — нет. Теперь ребёнок пишет редактированную строку в тот же
  долговечный след, что и остальные потери захвата, называя вид задачи и её
  идентификатор; провод не изменился, отказ диагностики исхода не меняет.
  Корень тех одиннадцати отказов не установлен и назван неустановленным:
  воспроизвести на копии нельзя, потому что запись принятия v3 связывает базы
  по inode и копия честно отвергается. Первый настоящий отказ после правки
  назовёт себя сам.

- 2026-08-28 — Забывание перестало быть выключенным. Возраст страницы читался
  из времени изменения файла, а это «когда файл писали», не «когда страница
  менялась»; в хранилище, которое одновременно git-checkout и собственный
  рантайм, разница идёт только в одну сторону — любое касание, сохраняющее
  содержание, омолаживает страницу, и часы начинаются заново. Измерено: 53 из
  76 отслеживаемых заметок несли отметку новее последней правки содержания.
  Теперь возраст берётся из времени коммита, записавшего лежащие на диске
  байты, с откатом к файловым часам при сомнении. Политика не тронута — окна
  и отсрочка по обращению остаются как решено. Заодно архивная страница стала
  объявлять себя отставленной (иначе она отвечала из архивного каталога через
  легаси-индекс), а вытесненное архивацией слово статуса теперь
  восстанавливается при возврате. Живая проверка: возраст по коммитам у 78
  страниц, устаревших нет; сегодня архивировать нечего и это верно —
  старейшей архивируемой странице 49.3 дня при окне 60, первое срабатывание
  через 12 дней вместо 49.

- 2026-08-28 — Компайл памяти не проходил на своей же машине, и причина
  оказалась в рабочем каталоге. Ни один вызов провайдера не задавал `cwd`,
  поэтому ребёнок наследовал каталог вызывающего — само хранилище, — а CLI,
  который ищет память проекта от каталога вверх, загружал `CLAUDE.md` этого
  репозитория вместе с индексом и журналом прежде, чем видел промпт. Парный
  замер на тривиальном промпте: из хранилища 62.15 и 64.60 секунды, из `/tmp`
  27.24 и 33.90 — около 33 секунд постоянных расходов при потолке в 90, то
  есть ровно разница между ответом и `provider_timeout`, который стоял в
  состоянии в 16:03:47. Теперь каждый порождающий процесс вызов идёт в пустом
  временном каталоге вне дерева хранилища; внутри `cache/` он не помог бы,
  потому что обнаружение памяти идёт вверх по дереву. После правки, через
  собственный путь продукта: 26.18 и 13.29 секунды. Тот же приём стенд уже
  применял у себя, но умолчание продукта тогда не тронули — и оно осталось
  дефектом на четыре дня.

- 2026-08-28 — Закрыт `MEM-14`, и заявка от этого сузилась, а не выросла.
  Стенд построен на настоящих данных MemoryAgentBench FactConsolidation, 161
  конфликт. Хранилище решает все 161 верно за 0.013 секунды; модель на тех же
  данных — 160 верно в первом прогоне и 161 во втором, то есть единственная
  ошибка не воспроизвелась на побайтно том же промпте. Разница 0.62 пункта,
  точный тест Макнемара даёт единицу, порог собственного шума провайдера в
  14 раз выше. Значит преимущества в точности нет и заявлять его нельзя.
  Держится другое: 80 микросекунд против 12 секунд на конфликт, побайтная
  повторяемость и отсутствие провайдера на пути. Неожиданное: на развёртке
  зернистости хранилище даёт ноль неверных ответов в каждой точке — грубые
  улики стоят ответов, но не правды, превращая неразрешимое в названный
  отказ; тот же выбор по максимуму на тех же часах при 512 наблюдениях
  возвращает устаревшее значение во всех 161 случае. Заодно подтверждено
  заново: подсистема утверждений здесь так и не запускалась — ноль реестров,
  ноль записей, у читателя `index_as_of` ни одного входящего вызова. И
  исправлено название статьи, которое реестр выписал неверно 2026-08-27.

- 2026-08-28 — Ноль научился говорить, какой он ноль. «Кто зовёт» отвечал
  ноль для методов, и это был молчаливо неверный ответ: из 76 044 наблюдений
  46 385 — динамическая диспетчеризация, а из 35 313 разрешённых вызовов лишь
  2619 указывают на метод при 3458 методах. Граф знал, ответ не смотрел.
  Рёбра не выдуманы: список зовущих по-прежнему только доказанные, а рядом
  появились нерешённые вызовы с файлом, строкой и причиной и их точное число.
  Проверено на живом хранилище: у `fuse_rrf` девять зовущих и ноль
  нерешённых, у `recover_expired_leases` ноль и пять, и эти пять называют
  настоящие места вызова. Метод, которого правда никто не зовёт, отвечает
  честным нулём. Заодно сообщества перестали называть хеши: член теперь имя,
  файл и строка, границы выбраны арифметикой — назвать все 4078 сообществ
  стоит 899 071 токена, это 36 потолков ответа, а 30 на 10 — наибольшее, что
  держит общий ответ под потолком. Честный остаток: одна задача стенда
  паритета не чинится никакой границей, потому что нужное сообщество стоит
  729-м из 4078; для неё сделан якорь по символу, но контракт аргументов
  инструмента его пока не пропускает.

- 2026-08-28 — Закрыт однострочный остаток: инструмент научился спрашивать
  «в каком модуле живёт этот символ». Перечень всех сообществ не может быть
  одновременно полным и ограниченным — назвать 4078 стоит 899 071 токена при
  потолке 25 000, — поэтому нужен якорь, ровно как у «кто зовёт». Якорь в
  движке уже был, но контракт аргументов инструмента его отвергал, и вопрос
  нельзя было задать. Теперь можно: ответ на `fuse_rrf` занимает 2401 знак и
  называет `scripts/retrieval.py:1313`, тогда как без якоря нужное сообщество
  стоит 729-м из 4078 и в ответ не попадает никогда. Целый перечень остаётся
  законным вопросом.

- 2026-08-28 — У компайла появился собственный потолок вызова, и это вторая
  половина той же починки. После снятия накладных расходов рабочего каталога
  компайл всё равно падал по сроку в девяносто секунд; тот же дневник прошёл
  при шестистах — 225 секунд на весь проход, включая отвергнутый черновик,
  его повтор и партии критики. Значит один вызов больше девяноста и меньше
  двухсот двадцати пяти. Поднимать умолчание нельзя: его делят захват и
  очередь, где долгое ожидание означает, что о зависшем провайдере услышат
  втрое позже. Поэтому потолок стал свойством вызова, а не процесса: блок
  задаёт срок для того, что внутри него, компайл берёт 300 секунд, переменная
  среды по-прежнему старше блока. Замер по дороге объясняет, почему
  девяносто мало: промпт в четыре тысячи токенов с коротким ответом стоит
  десять-одиннадцать секунд, то есть дело не в размере входа, а в размере
  ответа — план целиком одним ответом.

- 2026-08-28 — Закрыт `MEM-15`: впервые измерено не то, находится ли нужная
  страница, а то, становится ли ответ лучше. На 23 вопросах память даёт
  прирост в 6 случаях, нейтральна в 17 и вредит в 0; парный бутстрап даёт
  интервал от +8.7 до +43.5 пункта. Весь прирост лежит там, где знать могла
  только память: в страте собственных фактов хранилища 6 из 7, а на
  шестнадцати общеизвестных — ровно ноль, при том что по ним в хранилище от
  одного до 281 сталкивающегося документа. Три оговорки названы и не
  снимаются: ноль вреда при таком размере выборки означает верхнюю границу в
  13 процентов, нижняя граница интервала совпадает с порогом шума, а
  единственное расхождение повторной пробы было настоящим дрейфом от памяти —
  ответ соскользнул с режима блокировки на режим журнала вслед за
  извлечёнными страницами. Поэтому честно говорить «вреда не наблюдалось при
  одной выборке на случай», а не «вреда нет». Два дефекта нашлись в самом
  инструменте, а не в продукте: рубрика отвергала четыре верных ответа и ни
  одного неверного, и это исправили пересудом записанных ответов без единого
  нового вызова; и эталон был неверен — пятисекундное ожидание блокировки это
  умолчание Python, а не SQLite. Публикуются только вердикты: тексты ответов
  цитируют приватные страницы и в репозиторий не идут.

- 2026-08-28 — Мёртвый код научился говорить, какой он ноль, — та же правка,
  что у «кто зовёт», но с другой стороны инструмента. Живой ответ давал 868
  кандидатов, и разбор против того же графа показал, что 69 из них методы
  протокола, которые язык зовёт сам и которые анализ по именам увидеть не
  может в принципе, а ещё 384 названы каким-нибудь текстом вызова, то есть до
  них может доходить динамический вызов. Не названы ничем только 415: больше
  половины перечня были не защитимы. Теперь исходов три вместо одного, а
  множество названных имён читается одним ограниченным запросом, а не одним на
  кандидата — на этом репозитории 11 455 различных текстов вызова при потолке
  в 200 000. Отказ закрытый: если чтение не удалось, «никто не называет»
  нельзя утверждать ни про кого, и все кандидаты понижаются до сомнения. Стало
  799 кандидатов, 415 защитимых и 384 с названным сомнением, ноль методов
  протокола.

- 2026-08-28 — Построен ограниченный наблюдатель свежести, и он сразу показал,
  что главное препятствие не в наблюдении. Ворота взяты: днём `stale` исчез —
  в 18:15 отставание в 101 источник, в 18:43 ноль и новое поколение с полными
  векторами. Но взяты один раз и потому, что машина случайно затихла на
  семь-двенадцать минут, тогда как замеренная частота изменений даёт медианную
  паузу около сорока секунд. Цена наблюдения мала и названа: сотые доли
  секунды на пробу, около четверти процента ядра при интервале в полминуты.
  Цена пересборки — 1247 процессорных секунд, и вот здесь нашлось настоящее.
  Пересборка не инкрементальна вообще: при нулевых изменениях она стоит
  столько же, сколько с нуля, а на живом прогоне переиспользовано ноль
  источников из 839. Причина измерена: манифест повторного использования на
  этом корпусе весит 158 мегабайт при потолке в 64, поэтому его выбрасывают, и
  из 33 поколений на диске ни одно его не содержит — переиспользование не
  работало никогда ни у кого. Восемьдесят два процента времени уходит на
  повторное кодирование всех фрагментов. Дневной запуск пересборки намеренно
  не подключён: пока переиспользования нет, это был бы обогреватель, а не
  свежесть.

- 2026-08-28 — Опубликованное намерение захвата без задачи теперь усыновляется,
  и вместе с этим исправлено моё же неверное число. Я насчитал десять
  осиротевших намерений; их ноль — я сравнил с задачами в состоянии «готова» и
  назвал остаток сиротами, тогда как у всех двадцати четырёх задача есть,
  просто десять уже успешно завершились. Неверный диагноз стоил ровно того, о
  чём вторая половина пункта: обе строки следа не называли событие, поэтому
  двадцать два отказа ограды я приписал публикатору, а они принадлежали
  рабочему. След теперь называет событие. Сама дыра настоящая: готовое
  намерение без задачи не ищет никто, и это воспроизведено на неизменённом
  коде. Закрытие доказано стендом долговечности — испытание с убийством
  производителя перешло из «названный отказ» в «доехало»: задача выполнена,
  транзакция зафиксирована, есть и строка дневника, и запись сессии. Десять
  намеренно осиротевших намерений усыновлены до нуля на копии рантайма, второй
  проход ничего не добавил. Оставлено и названо: у ограды намерения нет
  проверки истечения, поэтому умерший публикатор, всё ещё её державший, заблокировал бы
  усыновление.

- 2026-08-28 — Закрыт `OPS-01`: у памяти появилась локальная поверхность HTTP.
  Это транспорт, а не новая возможность — те же двенадцать инструментов, тот
  же конверт, та же проверка аргументов, и единственная правка в сервере это
  выделение одной точки сборки, которую зовут оба транспорта; второй границы
  проверки не появилось. Граница безопасности из четырёх проверок подряд:
  петлевой узел, заголовок узла, происхождение, токен. Привязка только к
  буквальным петлевым адресам, `localhost` отвергается, потому что имя
  разрешает не наш код; список разрешённых происхождений пуст, потому что
  сервер не отдаёт страниц и законного происхождения не бывает; токен лежит в
  файле с правами 0600 и в командную строку не попадает никогда. Проверено на
  живом сервере: без учётных данных 401, с неверными 401, при браузерном
  происхождении 403, при подменённом узле 421, с верным токеном 200. Что это
  покупает: предельный агент стоит гигабайт с четвертью через stdio и
  одну десятую мегабайта через общий сервер, а новая сессия на прогретом
  сервере отвечает за одну сотую секунды против трёх секунд у нового процесса.
  Цена названа: вызов через HTTP медленнее на восемь-двадцать две
  миллисекунды, поэтому одному агенту stdio выгоднее, а от двух и выше — нет.

- 2026-08-28 — Пересборка поколения научилась переиспользовать, и холостой
  проход подешевел со 643 процессорных секунд до 3.9. Дефектов было два.
  Манифест переиспользования ограничивала константа в 64 МиБ, а на этом
  корпусе он весил 158 мегабайт, поэтому его выбрасывали и путь
  переиспользования был недостижим для всех; строки зависимостей заменены
  ограниченным префиксом с точным итогом, а сама константа убрана — чтение
  теперь ограничено размером, который объявляет запечатанный и уже проверенный
  манифест. Почему заменили, а не подняли: новый манифест весит 23.8 мегабайта,
  то есть меньше прежнего потолка, и одна лишь обрезка выглядела бы починкой
  сегодня и сломалась бы снова при трёхкратном росте корпуса. Второй дефект:
  каждый фрагмент кодировался заново, хотя дайджест фрагмента лежит рядом с
  матрицей; кешем стало само родительское поколение, и строка берётся только
  при совпадении фрагмента, модели, её ревизии, размерности и схемы. Третья
  находка выпала из первой: быстрый путь «делать нечего» читал отметку из
  отсутствующего манифеста, поэтому холостой проход каждый раз делал полную
  пересборку. Корректность сравнена построчно: все семь таблиц совпали,
  переиспользованные векторы побитово равны родительским. И отдельно измерено
  то, что снимает вопрос к самому измерению: полная сборка не воспроизводит
  саму себя при другой разбивке на партии — расхождение того же порядка
  возникает между двумя полными сборками, — значит остаточная разница есть шум
  чисел, а не ошибка переиспользования.

- 2026-08-28 — Разобрано, что нужно для точной навигации за пределами Python, и
  главная находка не про установку, а про правду ответа. Языковой сервер
  TypeScript отвечает до загрузки проекта, и ответы при этом неверны, не будучи
  ни пустыми, ни ошибочными: на просьбу показать объявление он возвращает
  привязку импорта вместо самого объявления, и отличить это, глядя на
  результат, нельзя. Парный замер по двенадцать процессов с каждой стороны: без
  ворот готовности ноль верных из двенадцати, с воротами — двенадцать из
  двенадцати, цена ворот меньше секунды. Сигнал готовности при этом приходит
  только клиенту, который объявил соответствующую возможность, а наши
  возможности написаны под Pyright, — поэтому первый проход и решил, что
  сигнала нет вовсе. Заодно выяснилось, что очевидный пин неверен: свежая
  мажорная версия TypeScript это Go-порт, и языкового сервера в ней нет.
  Честная граница названа прямо: шов и профиль написаны, но ни один
  существующий скрипт их не импортирует, то есть рабочий путь их не зовёт и
  TypeScript-навигации у продукта пока нет. Это тот самый класс дефекта, что
  уже записан здесь пять раз: код написан, покрыт тестами, ни разу не выполнен.

- 2026-08-28 — Закрыт `CODE-03`: код-индекс перестал быть индексом одного
  checkout. Три глагола живут на уже существующем инструменте — тринадцатого не
  добавлено, каталог один, второго графа нет, — а чужое поколение регистрируется
  и никогда не активируется, поэтому единственный активный указатель хранилища
  не трогается. Проверено на настоящем втором репозитории этой машины: 436
  источников за две минуты, и вопрос «кто зовёт» приходит из его собственного
  поколения с корнем, указывающим туда, без отката к структурным уликам. Это и
  есть пункт, который держал снятие стороннего код-индекса. Названо и
  перенесено вперёд: набор имён корневых каталогов всё ещё заморожен под наш
  репозиторий, поэтому репозиторий с кодом в `src/` отвергается целиком.
  Отвергается сознательно: частичный индекс сделал бы «нет результата»
  неотличимым от «не проиндексировано», а для памяти второй отказ хуже.

- 2026-08-28 — Публикацию поколения блокировала мёртвая строка ворот писателя,
  и пока её не сняли, переиспользование нельзя было проверить вообще. Я
  сначала списал ошибку на конкуренцию — на тихой машине она воспроизвелась
  дважды подряд, значит списал неверно. Причина оказалась в том, что имя ворот
  это первичный ключ, строка ровно одна, и оставил её умерший писатель проекта:
  аренда истекла, процесс мёртв, а забрать строку некому — реестр
  переприсваивает мёртвого владельца по роли и области, а эта строка не
  ключуется ни тем, ни другим, потому что вложенные ворота записывают ту
  аренду проекта, которая в них вошла. Теперь перед вставкой мёртвая строка
  забирается по той же проверке смерти, что и везде, а недоказанная смерть
  отказывает по имени, которое окно ожидания уже умеет обрабатывать. Побочно
  ушёл второй случай того же: живой вложенный писатель давал конкуренту
  необработанную ошибку целостности вместо отказа. И только после этого проход
  поколения впервые за день дошёл до конца, а следующий, холостой, занял пять
  секунд вместо десяти с лишним минут — на настоящих данных, а не на копии.

- 2026-08-29 — Закрыт `CODE-09`, последний нетронутый пункт дорожной карты:
  трассы выполнения принимаются в отдельное одноразовое хранилище, а не в
  поколение, и отдаются отдельным полем — ребро обязано говорить, откуда оно
  узнано, и трассовое никогда не сливается со статическим. Разделение сборщика
  и приёмника и есть мера безопасности: профиль сериализуется небезопасным
  форматом, поэтому приёмник читает только построчный JSON. Число честное и
  двойное: одна девяностовосьмисекундная трасса покрывает меньше процента всех
  неразрешённых вызовов — половину из них не свяжет никакая трасса, потому что
  это атрибуты библиотек, — но добавляет 578 рёбер, ведущих в метод, к
  имеющимся 2477, то есть рост на 23% там, где статический анализ слеп. Заодно
  автор поправил собственную записку: номер первой строки кадра указывает на
  первый декоратор, а не на `def`, и прежняя проба этого различить не могла —
  из-за чего молча терялась тысяча кадров. `find_dead_code` к трассам намеренно
  не подключён: отсутствие в трассе не значит ничего.

- 2026-08-29 — Два пункта реестра оказались устаревшими, и это важнее самих
  правок. `NEW-111` был исправлен через 67 минут после того, как его завели
  как неразобранный, а строку не сняли; проверено измерением, а не доверием:
  с восстановленным прежним сравнением 30 отказов из 30, с нынешним кодом ноль
  из тридцати. Причина оказалась третьим случаем одной и той же ошибки —
  сравнивали область репозитория целиком, а в ней есть коммит, тогда как
  вопрос был «тот же ли это checkout». Половина `NEW-110` тоже была закрыта
  часом позже записи. Оставшаяся половина закрыта сейчас: брошенная работа
  больше не аллоцирует до конца — вместо 2750 файлов после отказа вызывающего
  разбирается один. И рядом нашлось то, на что запись указывала неверно:
  второй проход обхода не знал правила «не заходить в скрытые каталоги», о
  котором все остальные обходчики хранилища давно договорились, и потому
  разбирал 7686 файлов, из которых 7261 — одноразовые worktree других агентов.
  После правки 425 и ноль под `.claude`. Заодно записан четвёртый случай того
  же сравнения областей: он стоит всего выигрыша переиспользования на первой
  пересборке после любого коммита.

- 2026-08-29 — Шов второго языка подключён к рабочему пути: инструмент выбирает
  сервер по расширению, сессия стала профиль-ориентированной, установка второго
  сервера — отдельное явное действие с проверкой закреплённой суммы. Ворот
  готовности стоит в единственной точке допуска, и это принципиально: без них
  сервер TypeScript отвечает уверенно и неверно. Но верного ответа по
  TypeScript всё ещё нет, и причины измерены, а не предположены: путь запуска
  по форме CommonJS, а серверный пакет — ESM и читает свой `package.json`
  относительно себя, тогда как мы запускаем копию в стороне; и вычислитель
  ревизии рабочей области знает только питоновские расширения, поэтому документ
  отвергается раньше, чем сервер становится важен. Главное же вот что: второй
  язык блокирует не архитектура, а гейт сложности — три из четырёх модулей,
  которые надо тронуть, он отвергает целиком, и однострочная правка в любом
  начинается с разбора модуля, критичного для безопасности. Python при этом не
  тронут: тысяча два теста зелёные, а при неустановленном сервере ответ
  деградирует с названным пределом, без трассировки и без неверного ответа.

- 2026-08-29 — Этот самый журнал перестал входить в преамбулу каждой сессии, и
  повод был жёсткий: три агента подряд умерли на первом же шаге с «промпт
  слишком длинный», не сделав ничего. Причина измерена: контракт втягивал
  журнал целиком, а он вырос до 305 килобайт — около 76 тысяч токенов до
  первого действия, при 86 тысячах всей преамбулы. Рост тоже измерен: на теге
  выпуска журнал весил 147 килобайт, тридцать коммитов назад 259, сейчас 305,
  и большая часть прироста пришлась на один день. Правило об экономии токенов
  запрещает платить столько за собственный changelog, а величина росла линейно
  с работой хранилища и ничем не ограничивалась — то есть вопрос был не
  «сломается ли», а «когда». Журнал никуда не делся: он отслеживается,
  append-only, дописывать его по-прежнему обязательно, и читается он по
  надобности — тем самым поиском, который и есть продукт. Отвергнут вариант
  втягивать хвост: директива берёт файл целиком, значит хвост потребовал бы
  второго генерируемого файла ради контекста, который отдаёт один `grep`.
  Преамбула стала 39.7 килобайта вместо 344 — в 8.7 раза меньше.

- 2026-08-29 — У ограды намерения появилась проверка истечения, и по дороге
  выяснилось, что заклинивает не тот, на кого думали: мёртвый публикатор
  усыновляется и без этого, потому что публикатор и усыновитель делят одного
  владельца, а блокирует ограда мёртвого рабочего. Форма правки та же, что у
  ворот писателя днём раньше: доказуемо мёртвый держатель переприсваивается
  вместе с парной проекцией, недоказанный отказывает по имени, доказательство
  берётся у реестра, а не пишется второй раз. Живое состояние проверено, а не
  предположено: строк ограды ноль, у всех двадцати девяти готовых намерений
  задача есть, сегодня этим не заблокировано ничего.

- 2026-08-29 — У ответа про мёртвый код появилось умолчание бюджета: было 63.5
  тысячи оценочных токенов на обычный вопрос, стало 25 тысяч, и 354 опущенные
  строки теперь названы, а не срезаны молча хостом. Число выбрало измерение:
  25 000 — единственное значение, при котором срез целиком приходится на то,
  чего инструмент не утверждает; при 12 000 он уносит 216 защитимых строк.
  Половиной правки оказался порядок: лестница режет с хвоста, а список был
  отсортирован по имени, поэтому при том же бюджете выбрасывались 97 защитимых
  кандидатов ради 162, которых инструмент мёртвыми не называет. Теперь порядок
  задан причиной. И отдельно разобрано, почему доктор красен вечно: причин две,
  и обе не могут погаснуть сами. Малая — карантинная запись, у которой не
  хватает одной квитанции из восьми; прочитал отвергнутые байты: в ней
  записано «долговременного содержания нет», то есть не потеряно ничего, но имя
  недостающей квитанции выведено из среза, которого в файле давно нет. Большая
  — таблица операций переросла потолок чтения на 1822 строки, и доктор
  сообщает «состояние неизвестно» про строки, которых просто не читал. Это
  неправда, и она постоянна: таблица только растёт.

- 2026-08-29 — `CODE-08` доведён: навигация по TypeScript отвечает верно. На
  фикстуре, поднятой настоящим checkout, определение возвращает объявление, а
  не привязку импорта, за 1.25 секунды, ссылок четыре, провайдер назван
  версией, готовность подтверждена уликами. Оба блокиратора сняты. Первый —
  запуск: дескрипторный путь не просто неудобен, он не может выполнить этот
  сервер, и это измерено тремя способами; взамен профиль объявляет запуск
  пакетом — проверенная копия кладётся с трёхполевым манифестом, дерево
  запечатывается только на чтение и перепроверяется уже через запечатанный
  путь, так что проверяется ровно то, что открывает node. Остаток окна назван
  прямо: процесс под тем же пользователем всё ещё может подменить, раньше это
  окно было закрыто. Второй блокиратор — вычислитель свежести знал только
  питоновские расширения; теперь список берётся из реестра профилей, и правка
  `tsconfig.json` сбрасывает свежесть так же, как `pyproject.toml`. По дороге
  найдены и исправлены два ложных утверждения в собственных полях ответа:
  провенанс называл провайдером pyright на ответах TypeScript, а готовность
  сообщалась снятой до конца загрузки проекта — ждали правильно, сообщали
  неверно.

- 2026-08-29 — Закрыт `NEW-138`, и он поправил мою же формулировку. Оба места
  сравнивали запись области целиком, а в ней есть коммит; теперь оба
  спрашивают идентичность через одно определение, и приватная копия списка
  полей в докторе удалена. Пятого случая нет, и это измерено обходом AST, а не
  предположено: осталось ровно два сравнения целиком, оба намеренные.
  Запретить форму типами нельзя — оба места сравнивали словари, а не объекты,
  — поэтому запрет сделан синтаксическим сторожем, который проверен против
  прежнего кода и называет оба места. Главное же в измерении: выигрыш живёт в
  холостом проходе — 138 секунд против 4, и воротами служит проверка доктора,
  — а возвращённое переиспользование записей на проходе с изменением **не даёт
  измеримого времени**, потому что дорогая половина это переиспользование
  векторов, и оно областью не ограждено. Так и записано, вместо того чтобы
  выдать за победу.


- 2026-08-29 — Закрыта первая половина `NEW-140`, и разбор нашёл не два дефекта, а четыре. Доктор объявлял порчу про строки, которых не читал: таблица `operation` держит 11 826 строк при потолке `MAX_OPERATIONAL_ROWS = 10 000`, и усечение дописывало в коды удаления `transaction_state_unknown` — заявление о порче, — хотя у всех 9 046 строк транзакций состояния верные. Вторая половина крупнее и в реестре записана не была: строки операций за потолком никто не читает, поэтому их транзакции выглядят транзакциями без операций, а это `_transaction_row_corrupt` читает как порчу. Измерено на живом хранилище: полное чтение операций помечает порчей **1** транзакцию, усечённое — **1407**. Третье: потолок один на обе таблицы, а каждая транзакция вне `preparing`/`discarded` обязана владеть хотя бы одной операцией, то есть `operations >= transactions` всегда — усечение `operation` наступает раньше усечения `transaction` **по построению**, и проверка была устроена так, что обязана солгать про порчу прежде, чем сможет правдиво сказать про усечение. Четвёртое: единственная по-настоящему порченая строка порченой не была — `20477c3f5060` это `discarded` с пустым `plan_hash`, а `_promoted_for_recovery` отбрасывает прямо из `preparing`, где `plan_hash` ещё пустая строка; освобождение стояло только для `preparing`.

  Разделены три потребителя, которых код смешивал в один список. Удаление `run/` по-прежнему отказывает — неполное чтение не может доказать, что таблицу не жалко, — но под собственным кодом `transaction_scan_incomplete`, который называет предел чтения, а не порчу. Тяжесть считается по явному факту `state_invalid`, выставляемому теми двумя местами, которые порчу действительно находят, а не сопоставлением строк в списке кодов удаления: именно это позволяло коду ворот молча стать вердиктом здоровья. Неполное чтение воздерживается вместо обвинения — при усечении операций `_transaction_row_corrupt` получает `None` и пропускает тесты по операциям, тогда как прочитанная малформированная строка обвиняет по-прежнему. Форма не выдумана: Nagios десятилетиями держит UNKNOWN (3) отдельно от CRITICAL (2), а XACML 3.0 держит `Indeterminate` отдельно от `Deny` и при этом никогда не выдаёт `Permit` — ровно ворота, которые обязаны отказать по неполному чтению, не записывая доказанного нарушения.

  Живое хранилище до: `error`, коды `transaction_metadata_corrupt`, коды удаления `transaction_state_unknown` + `transaction_state_corrupt`. После: коды порчи исчезли, `state_invalid: False`, остались `transaction_scan_incomplete`, `transaction_quarantined`, `transaction_undo_retained`. Проверка — `tests/test_doctor_bounded_scan_truth.py`, девять тестов, пять падали до правки; ruff чист, управляемый гейт на обоих файлах даёт 0; прогнаны все пятнадцать наборов, импортирующих `doctor` — 979 пройдено при 4 пропущенных.

  Честный остаток, и он важнее правки. Проверка по-прежнему `error`, теперь ровно по одной причине, и причина истинна: `quarantined_unresolved: 1`. Карантинная запись `cb387b96` собиралась создать восемь квитанций, семь создал зафиксированный компайл `76f1199ba76c`, восьмой не создал никто; в отвергнутых байтах у неё `no_durable_content`, `operations: []`, `evidence: []` — не потеряно ничего, но имя выведено из среза в 14 188 байт файла, ныне равного 332 122, поэтому путь недостижим и находка не погаснет никогда. Ослабить `_outcome_was_written` до «существует или доказуемо недостижимо» **отвергнуто и оставлено владельцу**: доказать недостижимость значит пересчитывать идентичности источников компайла внутри проверки здоровья, то есть внести в вердикт доктора семантику и версию разрезателя, а дешёвая замена — освободить всё под `knowledge/daily/receipts/` — это огульное освобождение целого класса улик, тогда как принятое решение говорит прямо: попытка, «whose pages exist nowhere, stays a finding, because there something really was lost». Править эту фразу — не моя подпись. Цена того, что находка остаётся, названа: блок здоровья при старте сессии внедряется каждый сеанс с находкой, которая не может погаснуть, — та самая усталость от предупреждений, против которой заведено `self-resolving-health-findings-decision.md`; в удалении цена нулевая, потому что `transaction_quarantined` дописывается при любой карантинной записи. Решение — `bounded-read-is-not-corruption-decision.md`, исследование — `docs/research/2026-08-29-what-a-bounded-read-may-conclude.md`.

- 2026-08-29 — Снято последнее допущение «это хранилище»: индексируется
  репозиторий любой раскладки, а не только совпадающей с нашей. Корни кода
  берутся из того, что отслеживает git, минус то, что обход и так обрезает.
  Манифест сборки отвергнут измерением: у соседнего репозитория он называет
  один корень из двенадцати, у нашего собственного не называет ничего, то есть
  читатель манифеста проиндексировал бы малую часть и сообщил об успехе —
  ровно тот отказ, который для памяти хуже отсутствия. Отдельно названо
  правило, без которого починка сама стала бы дефектом: каталог, который обход
  обрезает, никогда не бывает корнем кода, потому что правило обрезки
  применяется к детям корня, а не к нему самому. Ворота взяты на настоящем
  чужом репозитории: 943 источника за восемь минут, и вопрос «кто зовёт» про
  функцию в `src/` отвечает верно, тогда как до правки этот каталог был
  неиндексируем вовсе. Доказательство сильнее тестов: корпус самого хранилища
  побайтно тот же на старом и новом коде, четыре прогона подряд.

- 2026-08-29 — Парный стенд против codebase-memory-mcp перезапущен, и он
  опроверг то, что я закоммитил утром. Хорошее: колонка «как зовёт стенд» ушла
  с нуля из тринадцати на восемь, неотвеченных вызовов стало один вместо
  одиннадцати, а на тех же задачах мы подешевели с 3.39 до 2.75 раза против
  cbm. Единственная чистая победа — устойчивость: пять наших прогонов дают
  побайтно одинаковые суммы, у cbm верных то десять, то одиннадцать, и граф
  растёт сам по себе без единого коммита. Плохое перевешивает. Режим
  зависимостей отвечает пустотой на всё и всегда: таблица зависимостей
  активного поколения пуста, и в четырёх предыдущих тоже, при 3934
  утверждениях импорта в том же файле — проверил сам на трёх символах.
  И главное: класс, который я утром назвал защитимым, защитимым не является.
  Имя, загруженное как значение — обработчик потока, запись реестра, —
  не даёт ребра вызова никогда, а таких среди «мёртвых» 300 из 435 по одному
  только каталогу скриптов. Самоопровержение внутри одного прогона: в списке
  мёртвых стоят две функции, которые стенд в том же прогоне выполнил. Заявка о
  безопасности остаётся снятой, и снимать сторонний код-индекс по-прежнему
  рано — но теперь не потому, что мы отказываем, а потому, что то, что мы
  отвечаем, бывает неверным молча.

- 2026-08-29 — Оба молчаливо неверных ответа закрыты. Про мёртвый код корень
  оказался проще и хуже, чем думалось: для ссылки-значением граф не пишет
  ничего — такого типа ребра просто нет, — то есть читатель не спрашивал, а
  извлекатель никогда и не имел. Починено без правки извлечения: поколение уже
  хранит байты источников, и новый читатель разбирает их один раз, поэтому ни
  одно опубликованное поколение не обесценилось. Защитимых кандидатов стало 26
  вместо 461, и у выборки из них ноль упоминаний вне собственного объявления.
  Дешёвый вариант отвергнут измерением: сканирование по токенам оставляет ноль
  выживших, потому что каждое имя встречается в собственной строке
  объявления, — это была бы другая ложь вместо прежней. Про зависимости
  выяснилось, что таблицу не пишет никто: схема без производителя, а
  единственные писатели — пять тестовых фикстур. Ответ теперь строится из
  утверждений, которые есть, и разрешение символа умеет путь, а не только имя.
  Парный стенд на одном поколении: верных восемь стало десять, уверенно-неверных
  четыре стало два, ошибок инструмента ноль, и ничего не опустилось.

- 2026-08-29 — Инвалидация при пересборке была не просто широкой, а неверной, и
  это важнее выигрыша во времени. Сравнение полной и инкрементальной сборки по
  всем семи таблицам показало: при старом правиле добавление страницы знаний
  пересобирало только её саму, а три висячие викиссылки, которые эта страница
  закрывала, навсегда оставались записанными как неразрешённые — три таблицы
  расходились с полной сборкой. Причина в том, что добавленный источник никогда
  не считался семантическим изменением. Теперь источник инвалидируется, только
  когда сдвинулась его собственная вселенная извлечения, и все четыре проверенных
  случая совпадают с полной сборкой. Заодно разложение прохода поправило прежнюю
  запись: дорогая половина — не векторы (кодирование это один процент, 3400
  фрагментов из 3401 берутся у родителя), а запись всей базы графа и её
  перепроверка, сорок пять и двадцать один процент, и обе масштабируются
  размером корпуса, а не размером правки. Выигрыш поэтому есть только там, где
  правка не трогает вселенную кода: правка страницы знаний подешевела на
  девятнадцать процентов, правка файла кода — ни на что. И найдено, что все пять
  отпечатков инвалидации сегодня вычисляются из хеша содержимого, то есть вся
  их машинерия не покупает ничего; не исправлено.

- 2026-08-29 — Поколения научились удаляться, и заодно снят живой блокиратор,
  из-за которого хранилище с четырёх утра не могло записать ничего. У продукта
  не было никакого способа убрать поколение: единственный поддерживаемый путь
  отказывает всякому активированному, что проверено отказом на всех тридцати
  трёх. Политика теперь такая: активное поколение плюс один предок, и на вопрос
  «зачем второй по старшинству» ответ измерен и отрицателен — цепочке
  переиспользования нужно ровно одно, а удерживаемый предок нужен как первая
  альтернатива при отказе активного дерева. Стало два поколения вместо
  тридцати шести, 518 мегабайт вместо 6.6 гигабайта, и цепочка выжила: холостой
  проход отвечает «текущее» за четыре с половиной секунды, а сборка перед ним
  переиспользовала 487 источников из 910. Блокиратор оказался залипшим
  журналом SQLite от 03:58, который никто не держал: любое чтение базы падало,
  и наружу это выходило как неверная запись принятия — а эта проверка стоит
  перед всем путём записи памяти. Снято штатно: журнал отложен в сторону, база
  открыта на запись, SQLite откатил сам; целостность `ok`, 9063 строки на
  месте. Дефект наблюдаемости записан: восстановимое состояние выдавалось за
  порчу записи, и по сообщению нельзя было понять, что чинить.

- 2026-08-29 — Уборщик worktree спрашивал не про ту ветку: он проверял, влита
  ли ветка в `main`, а работа агентов приземляется в `work`, и владелец сливает
  пачками — с PR 12 в `main` не попадало ничего, поэтому вопрос не мог стать
  истинным неделями. Измерено при чистке диска: он оставил тринадцать
  каталогов, у которых все коммиты уже лежали в `work`, на 743 мегабайта.
  Теперь ветка считается влитой, если её содержит любая из двух, и прежний
  вопрос продолжает работать — это закреплено отдельным тестом. Ради двух строк
  файл пришлось разобрать целиком: он был красным и до правки, с функцией
  сложности 32 и вложенностью пять.

- 2026-08-29 — Разрыв по токенам против стороннего код-индекса сократился
  вдвое, и решило не сжатие, а вопрос «о чём спросили». Две трети сводки
  архитектуры оказались дословной копией того, что отдаёт отдельный режим
  сообществ; треть ответа про мёртвый код — константы и общий префикс путей;
  а у режима зависимостей нашёлся сегодняшний аналог прежних непрозрачных
  хешей, проскочивший правило только потому, что заканчивается читаемым
  текстом. Решающее нашлось в графе: собственный комментарий кода уже
  записывал, что нужное сообщество стоит 729-м по размеру, значит неякорный
  перечень на этот вопрос не ответит никогда. Живые числа: сводка с двадцати
  тысяч токенов до шести, два других ответа с двадцати пяти тысяч до трёхсот.
  По стенду целиком с 87 до 41 тысячи, отношение к сопернику с 36 до 19 раз.
  Правильность при этом выросла: верных восемь стало одиннадцать,
  уверенно-неверных четыре стало один. И отдельно найдено то, что не про цену:
  ответ про мёртвый код сообщал об усечении, но срезал 307 названных
  кандидатов, для которых отсутствие в списке неотличимо от наличия вызывающих.

- 2026-08-29 — Отпечатки инвалидации разобраны, и результат отрицательный, но
  измеренный. Из пяти ключей настоящее определение имеет ровно один: источник
  виден другому только через определения, которые он отдаёт, — импорты и
  псевдонимы в общий индекс не попадают, сигнатура уже внутри личности узла, а
  журналы проектов извлекаются в одиночку. Ожидаемая посылка «почти всякая
  правка меняет экспорт» оказалась ложной: невидимы для зависимых 27.9% правок
  содержимого. Но файл не та единица — целиком невидимы лишь 6.2% коммитов. И
  главное: на парных плечах настоящий отпечаток экспортов схлопывает набор
  пересборки в 423 раза — с 423 источников до одного, — а проход при этом
  становится на две секунды **медленнее**, потому что извлечение кода берёт
  партией все 671 источник, как только пересобирается хоть один. Поэтому
  отгружен не механизм, а названный выбор: двадцать строк объяснения без
  изменения поведения и четыре теста, закрепляющих контракт. Заодно найдена
  латентная дыра: узел из `__tablename__` внутри тела класса попадает в
  зависимости, сегодня их ноль, но отпечаток такой формы разошёлся бы в день,
  когда в проекте появится ORM.

- 2026-08-29 — Разобрано, куда уходят десять секунд главного вопроса к памяти.
  Дефект оказался в семантике пула, а не в бюджете: в падавших вызовах почти
  половина времени уходила двум необязательным стадиям, не вернувшим ничего,
  после чего уже вычисленный лексический ответ выбрасывался, — и под нагрузкой
  восемнадцать вызовов из тридцати шести поднимали исключение вместо
  деградированного ответа. Теперь ожидание прекращается за два с половиной
  секунды до срока, а стадия, чей прошлый прогон не уложился в предлагаемое
  окно, не ожидается вовсе; сама стадия при этом стартует по-прежнему — автор
  сначала сделал наоборот и это поймал существующий тест. Медиана упала с 8.77
  до 6.62 секунды, но ворота одним бюджетированием не берутся, и это сказано
  прямо: остаток — холодная обязательная работа первого вызова, до которой
  правило бюджета не дотягивается. Её берёт синхронный прогрев, добавленный
  только в общий HTTP-сервер: ноль превышений из двадцати четырёх против шести,
  ценой тридцати секунд старта и двух с половиной гигабайт — у stdio сервер на
  каждого агента, там эта цена была бы за каждого. Рекомендацию автора дать
  запасному пути собственное окно я проверил и отменил: её отвергли два теста,
  и один прямо называет контракт — ответ после срока вызывающего приходит тому,
  кто уже сдался. Отдельно записано найденное попутно: оба стенда качества
  сейчас ниже собственных порогов и на HEAD тоже, регресс предшествует правке.

- 2026-08-29 — Качество поиска вернулось, и причиной оказалась собственная
  продуктивность хранилища. Числа воспроизвелись на тихой машине с нулевым
  разбросом, а первой настоящей уликой было то, что упала и базовая линия
  `grep`: она не трогает ни поиск, ни поколение, значит изменились файлы, а не
  код. И правда: каталог `docs` несёт теперь 1409 фрагментов против 620 у
  скомпилированных страниц, причём 49 записок из 87 написаны за последние два
  дня — то есть моими же агентами. Вопросы русские, страницы решений
  английские, комментарий русский, а многоязычная модель вознаграждает
  совпадение языка, поэтому комментарий забрал все верхние места: лучший
  фрагмент нужной страницы стоял на 117–310-м месте из трёх с половиной тысяч и
  до ответа не доезжал вовсе. Правило «сначала собранные страницы» у хранилища
  было, но применялось после слияния — оно упорядочивало то, что уже попало в
  пул, и не решало, кто в пул войдёт. Асимметрия лежала в коде: лексическая
  нога умножала ранг на вес доверия, плотная перезаписывала его сырым
  косинусом. Одна нога правилу подчинялась, вторая нет. После одного умножения
  эталон переезжает на первые места, а стенд применения возвращается с 0.2857
  на 0.8571. Отдельно: один эталон был устаревшим и не мог пройти никогда —
  он называл страницу, отставленную пять дней назад, а корпус такие исключает.

- 2026-08-29 — Три пробела индексации чужого репозитория закрыты: два правкой,
  один отчётом. Файл в корне теперь может быть корнем кода — оказалось, что
  сборщик умел это с самого начала, а на директориях настаивал только отбор;
  у соседнего репозитория стало восемнадцать корней вместо одиннадцати и на
  семь источников больше. Набор пропускаемых каталогов перестал быть словарём
  этого хранилища, который ездит по чужим: имена вроде `gaps` и `raw-sources`
  теперь режут только обход заметок, а `__pycache__` режет везде, потому что
  это факт о репозиториях, а не наша привычка. Сегодня не выигрывается и не
  теряется ничего — измерено, таких каталогов у соседа нет, — меняется форма
  отказа: раньше чужой `gaps` пропал бы молча. Третий пробел закрыт как
  дефект отчётности, а не обхода: перевод обхода на список git убрал бы из
  нашего же корпуса три настоящих источника и добавил вызов git внутрь
  запечатанного прохода, а на единственном доступном чужом репозитории не
  изменил бы ничего — поэтому квитанция теперь просто говорит, чем она
  смотрела и сколько неотслеживаемых файлов взяла. По дороге закрыта ловушка,
  которая иначе выстрелила бы: отслеживаемая символическая ссылка в корне
  заставила бы наивную правку отказать во всём репозитории целиком.

- 2026-08-29 — Печать поколения перестала считаться заново по четыре-пять раз
  за запрос: теперь каждый артефакт хешируется один раз на процесс, и медиана
  обоих главных инструментов упала примерно на секунду в каждом из трёх парных
  раундов. Ключ прежнего похожего кеша здесь не подошёл, и причина структурная:
  там кешировали дорогой вердикт о дайджестах, за которые уже заплатили, а
  здесь дайджест и есть работа — ключевать кеш хеша тем хешем, который он
  собирается посчитать, значит ходить по кругу. Взята идентичность файла,
  которую кортеж печати и так несёт, а остаточный риск подпёрт тем, что метку
  изменения инода не выставляет ни один системный вызов; единственная дыра —
  вторая запись того же размера в тот же тик часов, и под неё введён отстойник
  в двадцать миллисекунд. Первая попытка эту дыру упустила, и поймал её новый
  тест. Отдельно: запасной путь для второго инструмента отказан с измерением, а
  не приделан. Он входит, когда денег уже нет — замерено минус четырнадцать
  миллисекунд остатка, — а резерв, достаточный для лексического прохода, не
  влезает рядом с основным. Настоящая правка выше: выносить уже завершившиеся
  ноги из слияния, а не считать их заново. И правило для чтения стендов: тот же
  код дал 0.43 под нагрузкой и 0.857 дважды на тихой машине, поэтому число,
  снятое во время чужой работы, доказательством не является.

- 2026-08-29 — Стенд действительно находил собственный лист ответов, но
  загрязнение шло в обратную сторону, и это важнее самой правки. Попадание
  требует эталонной страницы, поэтому найденный лист только отнимает место, а
  косвенного канала от листа к эталону нет — проверено, ноль строк
  зависимостей. Значит ни одно записанное этим хранилищем число листом не
  раздувалось ни на какую дату; листы делали продукт хуже. Весь эффект — один
  случай, где лист пришёл первым, а нужная страница стояла шестой: это стоило
  одной десятой и не купило ничего. Отсюда два верных числа, и публикуются оба:
  0.7 получит оператор на хранилище, где лист лежит, 0.8 стоит поиск без
  артефакта измерения. Набор листов теперь выводится с диска каждый прогон, а
  не задан списком: прежние литералы устарели в обе стороны — стенд применения
  выбрасывал чужой лист и оставлял свой. И самое важное для чтения любых чисел:
  при нагрузке 17–19 путь MCP дал ноль применённых случаев из семи, потому что
  за десять секунд не возвращалось ни строки, тогда как тот же код и то же
  поколение по прямому пути дали 0.857.

- 2026-08-29 — Частичное извлечение оказалось доказуемо корректным и
  неокупаемым, и это тоже результат. Граница между фазами точная: первая делает
  все записи в общий индекс разрешения, вторая не пишет туда ничего и не зависит
  ни от порядка, ни от источника. Одно частичная партия делает неверно — узел,
  на который никто не ссылается, отдаётся источнику с наименьшим
  идентификатором, а это знание глобальное; воспроизведено на пустом файле.
  Корректность доказана сравнением всех семи таблиц, включая случай с
  добавленным источником, из-за которого правило инвалидации меняли утром. Но
  арифметика против: половина стоимости извлечения приходится на фазу, которую
  сузить нельзя, поэтому на широком наборе пересборки быстрее не становится
  никогда, а узкие наборы случаются на шести процентах коммитов — ожидание
  выходит плюс полсекунды на проход, то есть проигрыш. Отгружены измерения,
  объяснение выбора в коде и три теста, падающие на испорченном извлекателе.

- 2026-08-29 — Поиск, у которого вышел срок, перестал выбрасывать то, что уже
  посчитал: успевшие ноги возвращаются помеченными, а не заменяются отказом. Но
  замер вышел отрицательным — за двенадцать парных прогонов под нагрузкой точка
  спасения не достигалась ни разу, потому что отказ рождается раньше начала
  работы. Зато поиск причины нашёл настоящий дефект: под нагрузкой вызов не
  падает, он возвращает одну строку из сорока запрошенных, потому что поиск по
  словам ничего не находит на русских вопросах против англоязычного корпуса.
  Всё качество живёт в смысловой ноге, а она объявлена необязательной и
  отбрасывается по бюджету. Проверил сам на живом хранилище при тихой машине:
  смысловая нога отбрасывается и без нагрузки — её съедает первая загрузка
  модели, — а русский вопрос вернул ноль строк. Дефект записан, не исправлен:
  чинить его значит менять политику бюджета, а это отдельное решение.

- 2026-08-29 — Мультиязычный поиск оказался цел, а опаздывал допуск. Указатель
  построен многоязычной моделью и содержит три с половиной тысячи фрагментов;
  русский вопрос не находил ответа потому, что смысловую ногу к нему не
  пускали. Пускают её, сравнивая стоимость последнего завершившегося прогона с
  окном вызывающего, а записать стоимость может только прогон, который шёл, —
  прогрев же грузил модель и ни одной ноги не запускал. Стоимость оставалась
  неизвестной, окно бюджета ниже потолка для неизвестной, и нога молчала до
  четвёртого вызова под нагрузкой. Теперь прогрев проходит весь путь дважды:
  первый проход платит загрузку, второй идёт по тёплому и записывает
  установившуюся стоимость. Парно, два круга: было — первый вызов только по
  словам, стало — смысловая нога во всех вызовах. Плата — фоновый прогрев
  вырос с восьми секунд до двадцати восьми, обслуживание начинается сразу.

- 2026-08-29 — Подозрение про общий сервер подтвердилось: он тоже грелся одним
  проходом, а один проход записывает стоимость холодной загрузки, то есть
  цифру «никогда не влезает». Первый вопрос к нему всё равно отвечал без
  смысла. Прежний замер там мерил задержку, а не состав ответа, — потому дыра
  и не всплыла. Теперь прогрев один на оба вида сервера, и заодно убрана
  гонка: после вчерашней правки фоновый прогрев стал полным, и два полных
  прогрева шли наперегонки на четырёх ядрах. Парно, два круга: было — первый
  вызов только по словам, стало — со смыслом; плата около трёх секунд.

- 2026-08-29 — Мигавший тест линейности мерил настенным временем, а оно на
  занятой машине считает и то время, что процесс простоял снятым с ядра.
  Переведён на процессорное время и минимум из пяти попыток. Прибор проверен
  напрямую: старый даёт отношение от 1,19 до 2,47 при запасе 3,25, новый — от
  1,98 до 2,03, то есть ровно двойку, истинный ответ для удвоения входа.
  Разброс упал в двадцать пять раз. Само свойство теста не ослаблено, граница
  осталась прежней.

- 2026-08-29 — Второй мигавший тест оказался не хрупким тестом, а настоящим
  дефектом уборки. Убивая группу процессов, код слал ей SIGTERM, а следом
  SIGKILL, и если группа к тому моменту уже умерла — то есть первый сигнал
  сработал, — второй получал «нет такого процесса», и это записывалось как
  неудача. Чистое истечение срока становилось отказом. Найдено трассировкой:
  уборка была подтверждена, а вердикт всё равно отрицательный. Первая
  гипотеза — про потомка, не успевшего отделиться в свою группу, — оказалась
  неверной, правка по ней ничего не изменила и снята целиком. Ослабление
  наглухо отказывавшего пути сделано сознательно: вопрос о группе и так
  проверяется прямым обходом `/proc`, а не выводом из номера ошибки сигнала.

- 2026-08-29 — Разрыв по токенам против cbm разобран по задачам, и оказалось,
  что три четверти его дают две штуки, а причина не в том, как ответ
  закодирован, а в том, сколько его. Режим зависимостей всегда обходил всё
  достижимое множество, и попросить прямые зависимости было нельзя: вопрос
  «какие модули использует этот файл» стоил триста сорок шесть строк вместо
  семи, то есть в шестьдесят один раз дороже. Проверил заодно, что вынос
  постоянных столбцов тут не помогает — столбцы не постоянны, — и что усечение
  по бюджету не срабатывает, потому что ответ в бюджет умещается. Глубина
  стала аргументом, отсутствие которого ничего не меняет. Менять ли значение по
  умолчанию — вопрос владельцу, это смена контракта.

- 2026-08-29 — Умолчание у вопроса о зависимостях сменено с полного обхода на
  один шаг, и это решено исследованием: один шаг — уже собственная конвенция
  продукта в соседнем инструменте, и так же устроены иерархии вызовов в средах
  разработки и в протоколе языковых серверов. Полный обход по умолчанию был
  исключением, доставшимся по недосмотру. Ответ теперь говорит, насколько
  далеко зашёл и упёрся ли в край, — без этого ограниченный ответ был бы
  неотличим от полного, и только это и оправдывало прежнее поведение. Стенд:
  сорок тысяч восемьсот токенов стали одиннадцатью тысячами четырьмястами,
  время втрое меньше, оценки те же. Отставание от конкурента по расходу с
  девятнадцати раз до пяти с половиной.

- 2026-08-29 — В сводке архитектуры два списка росли без предела и молчали об
  этом: девяносто семь точек входа отдавались целиком, без счёта и без
  признака обрезки, тогда как все соседние списки свой предел объявляют.
  Теперь оба ограничены общим для сводки числом и несут счёт. Сводка
  подешевела на пятую часть, стенд целиком — до отношения четыре и восемь
  десятых против конкурента. Попутно записаны две находки, которые не тронуты:
  сводка вообще не перечисляет модули, хотя вопрос ровно про них, и рейтинг
  горячих точек занимает четыре пятых оставшегося ответа — но резать его тем
  же приёмом нельзя, потому что вернуть полноту нечем.

- 2026-08-29 — Попытка урезать рейтинг в сводке сломала оценку, и это оказалось
  полезнее самой экономии: вскрылось, что сводка отвечала лишь на половину
  своего вопроса. Спрашивают про основные модули и точки входа, а списка
  модулей в ответе не было вовсе, и главный модуль хранилища попадал туда
  только потому, что одна его функция случайно стояла в четвёртом десятке
  рейтинга вызовов. Добавлен рейтинг модулей по числу импортирующих — тем же
  запросом, что и рейтинг вызовов, только по другому виду рёбер. Сводка стала
  вдвое с лишним дешевле и снова верна, а расход на стенде за день упал с сорока
  тысяч восьмисот до восьми тысяч ста при тех же оценках.

- 2026-08-29 — Весь путь записи памяти был отгорожен брошенным журналом
  SQLite, и сообщение об этом называло недействительной запись о переходе на
  новую надёжность — ни файла, ни причины. Рухнувший писатель оставляет журнал,
  проиграть его можно только открытием на запись, а проверяющие открывают
  только на чтение и потому отказывают. За сутки скопилось четыре с половиной
  тысячи несведённых контрольных точек, а файл состояния вырос до десяти
  мегабайт. Теперь брошенный журнал проигрывается один раз и чтение
  повторяется, а живой писатель по-прежнему получает прежний отказ. Попутно
  выяснилось, что предел очереди в сорок событий решает лишь, когда сброс
  назрел, но не сколько их может накопиться, — записано отдельно.

- 2026-08-29 — Пять суток система отказывала каждые десять секунд, и здоровье
  об этом молчало: след отказов писался в файл, который не читала ни одна
  проверка. Пять тысяч шестьсот восемьдесят две записи с двадцать пятого
  августа. Именно это, а не сам дефект, растянуло аварию: то, что видит только
  человек с grep, сигналом здоровья не является. Теперь есть проверка, читающая
  хвост следа ограниченным окном и жалующаяся, только если отказ свежее часа.
  Собственный тест сразу нашёл в ней ошибку — отбрасывалась первая строка даже
  тогда, когда окно начиналось с начала файла. Попутно проверено правило 2,
  пропущенное накануне: восстановление брошенного журнала — документированная
  обязанность клиента SQLite, а не самодеятельность, но закрыта пока только
  общая точка входа.

- 2026-08-29 — Журнал проекта заклинило: он был полон, дописать нельзя,
  ротации не было вовсе, и за отказом ждали почти пять тысяч событий. Теперь
  полный журнал запечатывается в неизменяемый отрезок, а новый открывается
  событием, повторяющим свёртку всего запечатанного, — проекция от этого не
  меняется, и это закреплено тестом. Но главное нашлось рядом: все тысяча
  событий пусты, потому что поле, из которого берётся содержимое контрольной
  точки, не заполняет никто — оно только читается, а пишется лишь в тестах.
  Значит состояние проекта было пустым всегда. Что должна записывать
  контрольная точка — вопрос владельцу. И собственная ошибка: правил живой
  модуль по частям, хук выстрелил между шагами и повернул журнал без отрезка;
  историю вернул из хранилища транзакций целиком, тысячу событий, непрерывность
  проверил.

- 2026-08-29 — Исследование о том, что должна записывать контрольная точка
  проекта и кто это заявляет. Наука отвечает резче, чем подсказывает чутьё:
  абляция в работе о холодном старте разделяет вклад выполненных задач и вклад
  собственных слов агента и показывает, что несёт сигнал первое, а второе почти
  ничего не даёт. Работы о надёжности памяти считают самоотчёт путём отравления
  и требуют отделять выведенное от заявленного. А главный способ провала, по
  измерениям Momento, — не отсутствие состояния, а его несвежесть: агент берёт
  прошлую сессию за нынешний контекст вместо того, чтобы перепроверить. Отсюда
  решение: выводить из сделанного, заявленное помечать отдельно, каждому пункту
  ставить дату. Схема при этом уже подходит поле в поле — не хватает только
  того, кто заполняет. И живая правда рядом: три тысячи восемьсот записей
  «использован инструмент» не несут ни имени инструмента, ни цели, хотя ровно
  это хранилище пишет в другом месте построчно.

- 2026-08-29 — Контрольная точка проекта перестала быть записью о ничём.
  Содержимое выводится из того, что было сделано: изменённый файл ключуется
  своим путём, команда своим текстом, отказ инструмента открывает препятствие
  по тому, что упало, а текущая задача — один идентификатор, который всегда
  замещается, поэтому тысячи событий оставляют одну строку. Каждому пункту
  ставится дата, потому что главный способ провала перенесённого состояния —
  несвежесть, а не пустота. Заявленное агентом по-прежнему побеждает
  выведенное. Цель и фаза остаются пустыми намеренно: из действий их не
  вывести. Проверено сквозняком — состояние называет задачу, файлы и команду,
  где раньше по всем девяти полям стояло «None».

- 2026-08-29 — Качество внесено в цели наравне с расходом: нужно выше, а не
  вровень. Положение честное: пять прогонов стенда дали у нас 11/1/1 без
  единого отклонения, у конкурента 11/1/1 дважды и 9/2/2 трижды. Но разница в
  два задания из тринадцати целиком лежит внутри его собственного разброса,
  поэтому превосходством её называть нельзя. Наше измеренное преимущество
  сейчас другое — устойчивость, и это отдельная величина. Чтобы утверждать
  превосходство по качеству, нужен корпус, на котором разница в один-два ответа
  не тонет, повторные прогоны обоих плеч и правило решения, названное до
  прогона.

- 2026-08-29 — Исследование состояния отрасли и бэклог до цели. Главное число
  неприятное: наш LongMemEval — три десятых, рекорд отрасли — девяносто четыре
  сотых. Токенов мы тратим меньше рекорда, но это не экономия, а воздержание:
  на двадцати шести вопросах из пятидесяти система отказывается отвечать, а
  когда отвечает, права в семи случаях из девяти. Значит связывает нас отказ, а
  не ошибка, и это отдельная, обучаемая величина с двумя направлениями ошибки.
  Два слабейших разряда — межсессионный и временной — ровно те, где отрасль
  показала наибольший прирост, и для временного техника известна: факты со
  сроком действия и раздельное время мира и время системы. По расходу планка
  выше, чем казалось: конкурент публикует десятикратное сокращение, а мы
  отстаём в три и восемь. И ни LoCoMo, ни BEAM у нас не прогонялись ни разу.

- 2026-08-29 — Ядро долговечности приведено под закон пятый. Файл отвергался
  гейтами целиком, двадцать пять находок, и пока он такой, закон невыполним для
  всякого, кто его тронет. Разбор чисто структурный: валидатор схемы со
  сложностью сорок девять, две функции публикации по двадцать пять и двадцать
  шесть — везде выделены подфункции и охранные возвраты, поведение не менялось.
  Стало сто тридцать семь функций со средней сложностью два и девять и ноль
  предупреждений. Проверено тремястами пятьюдесятью двумя тестами. Попутно
  широкий прогон нашёл мою же поломку: заглушка в тесте извлекателя ждала
  старую сигнатуру, изменённую днём раньше, а тот набор я тогда не прогнал.

- 2026-08-29 — Прибор, поставленный утром, ответил на главный вопрос: отказ
  системы это калибровка, а не извлечение. Из двадцати шести воздержаний
  девятнадцать случились при том, что нужная сессия была найдена и лежала перед
  отвечающим; извлечение нашло ответ в тридцати восьми вопросах из пятидесяти.
  Пятнадцать из девятнадцати приходятся на временные рассуждения и
  межсессионные вопросы. Порядок выигрыша: если бы отказы с доказательством
  отвечались с уже измеренной точностью при ответе, общая точность выросла бы с
  трети до шести десятых. Значит чинить надо готовность отвечать, и это меняет
  порядок бэклога.

- 2026-08-30 — Пункт про компактные схемы инструментов закрыт нулём, и это
  честный результат. Отрасль обещает сорок процентов экономии от снятия
  отступов и лишних полей, но у нас ни того, ни другого нет: весь объём схемы
  это описания, по которым вызывающий выбирает режим. Вся поверхность стоит две
  с половиной тысячи токенов один раз за сессию, половину дают два инструмента,
  и внутри них резать можно только смысл. Записано, чтобы на это не тратили
  усилия второй раз.

- 2026-08-30 — Гипотеза о причине переотказа не подтвердилась. Парный прогон на
  тех же пятидесяти вопросах: воздержаний было двадцать шесть из сорока восьми,
  стало двадцать восемь из пятидесяти, верных ответов семнадцать против
  шестнадцати. Разница в один вопрос, то есть внутри шума. Значит отвечающий
  отказывается не потому, что ему не сказали цену неверного отказа. Правка
  оставлена как нейтральная — прежний текст был просто неверен, — но за
  улучшение не выдаётся. Следующая догадка: отказ может быть дешевле по форме,
  а не по смыслу, потому что ответ обязан нести атомарные утверждения с точным
  использованием цитат и гибнет на шлюзе целиком, а воздержание требует только
  статуса и причины.

- 2026-08-30 — Мой собственный вывод опровергнут, и опроверг его отвечающий
  своими же словами. Я объявил, что отказ это калибровка, а не извлечение,
  опираясь на то, что нужная сессия была найдена. Но причины отказов называют
  ровно то, что система видела: на вопрос с ответом «двадцать» она пишет, что
  единственная цифра в доказательстве это тридцать дюжин яиц из другого
  разговора. Значит сессия дошла, а нужное предложение нет, и мой признак мерил
  попадание сессии, а не отрывка. Плюс три отказа из двадцати одного были
  верными: там золотой ответ и есть отказ. На шлюзах цитирования гибнут всего
  четыре ответа из пятидесяти, так что форма тоже ни при чём. Добавлен признак
  уровня отрывка, намеренно слабый и читаемый только в паре с сессионным.

- 2026-08-30 — Третий прогон закрыл ещё одну гипотезу и разоблачил мой
  собственный прибор. Нужная сессия стоит первой у тридцати семи вопросов из
  пятидесяти, и пятнадцать из двадцати четырёх обоснованных отказов приходятся
  именно на неё, так что отбрасывание лишнего с хвоста исключено: нужное стоит в
  начале. А признак уровня отрывка оказался истинным всего у двух вопросов из
  пятидесяти и ни у одного из тех семнадцати, где система ответила, — он читает
  сводку кандидата, а не переданный текст, и не мерит ничего. Осталось две
  возможности, и различить их можно только чтением самого запроса к модели;
  такой признак поставлен.

- 2026-08-30 — Найдено, где теряется качество, и это не то, что я думал трижды
  подряд. Нужная сессия выбирается первой у тридцати семи вопросов из
  пятидесяти, а золотой текст доходит до модели лишь у четырнадцати из этих
  тридцати семи. Из двадцати четырёх отказов только пять случились при ответе в
  запросе — значит система не переотказывает, она отказывается честно, потому
  что ответа ей не дали. Дефект между выбором источника и упаковкой
  доказательства: из записи сессии в десять килобайт до модели доходит кусок без
  нужного предложения. Хуже всего там же, где и точность: межсессионные два из
  двенадцати, временные четыре из тринадцати. Прежние объяснения — калибровка,
  формулировка, отбрасывание по хвосту — опровергнуты замерами.

- 2026-08-30 — Воронка наконец измерена по следу самого компилятора: из
  двенадцати извлечённых кандидатов до него доходят в среднем два, разместить
  он не смог ни одного лишнего — ноль пропущенных, — а всё сужение происходит
  раньше, при подгонке под бюджет. Запись сессии около десяти килобайт, бюджет
  двадцать восемь, помещаются две. Моё прежнее исключение этой причины было
  верно наполовину: первый отрывок переживает обрезку, поэтому односессионные
  вопросы отвечаются, а межсессионным и временным нужно больше двух — и это
  ровно те разряды, где точность худшая. Заодно выяснилось, что и признак
  вхождения золотого текста для них негоден: там ответ выводится, а не
  цитируется.


- 2026-08-30 — Правило упаковки сменено на покрытие: когда места не хватает,
  первым уходит отрывок со страницы, которая уже представлена, и лишь потом —
  единственный отрывок другой страницы. Парный прогон на пятидесяти вопросах
  дал общую точность выше всех четырёх прежних, но разброс прежних прогонов
  сам по себе больше прироста, так что по общей цифре это в пределах шума.
  Не похоже на шум одно: межсессионный разряд сдвинулся с величины, которая
  была одинаковой в четырёх прогонах подряд. Одного прогона мало, чтобы это
  утверждать.

- 2026-08-30 — Смотрел не на код, а на диск, и нашёл, что запись состояния
  проектов стоит уже двое суток. В очереди лежало две с половиной тысячи
  чекпойнтов, файл состояния разбух до шести с половиной мегабайт, и в журнале
  ошибок полторы тысячи записей о неудачной блокировке — ни одна проверка
  здоровья об этом не говорила. Причин оказалось три, и все они одной породы:
  имя пережило то, что им называлось. Цикл разбора брал всю очередь целиком,
  поэтому чем больше затор, тем вернее он не разбирался. Пачка событий
  называлась именем своего последнего события, поэтому две разные пачки
  дрались за одно имя, и вторая получала вечный отказ. Резервирование
  переживало свою отменённую транзакцию и выдавало себя за уже записанное.
  Всё три починены, на каждую написаны тесты.

- 2026-08-30 — Появился ночной шаг восстановления. Хук разбирает очередь
  торопливо, потому что его ждёт человек, и потому затор его переживает;
  ночной проход не ждёт никого и может ждать по-настоящему, но ограничен по
  времени и разбирает проекты порознь, чтобы один сломанный не останавливал
  остальные. Он же подметает брошенные временные файлы: их накопилось тридцать
  девять на двести семьдесят два мегабайта, все целые, самый старый
  четырёхдневный. На живом хранилище удалено тридцать семь, двести семьдесят
  восемь мегабайт. Сначала я хотел удалять заготовку сразу при неудачной
  записи — тест напомнил, что она там нарочно: это целая копия того, что не
  удалось записать, и она нужна для восстановления. Не хватало не удаления, а
  того, чтобы её потом кто-нибудь забрал.

- 2026-08-30 — Две строки живое хранилище само не разберёт, и одну из них
  создал я сам, когда гонял принудительный разбор одновременно с живыми
  хуками. Распорядиться ими — значит распорядиться карантинной записью, а
  решение от двадцать второго августа прямо говорит, что это не делается
  автоматически. Оставил как есть и вынес наружу: `doctor` теперь сообщает о
  проекте, чья последовательность чекпойнтов стоит дольше часа, с номером
  строки и глубиной очереди.

- 2026-08-30 — Затор разобран до конца, и одну поломку в нём я внёс сам.
  Сначала проверил тестами, как карантин снимается на самом деле: исходный
  запрос приходит снова под тем же именем — так задумано и так описано семью
  тестами. Сломало это моё же переименование пачек: три строки остались с
  именами, которых больше никто не породит. Два решения общего вида я написал
  и откатил — одно снимало карантин само и ломало контракт, второе закрывало
  резервирование без записи и опиралось на то, чего схема не обещает. Второе
  успело навредить: одна последовательность оказалась помечена записанной, не
  будучи записанной, и журнал разошёлся с базой. Обе беды закрыты разовыми
  починками, на каждую по девять тестов.

- 2026-08-30 — Живое хранилище впервые за двое суток пусто по очереди:
  две с половиной тысячи отложенных чекпойнтов записаны, файл состояния сжался
  с семи мегабайт до девятнадцати килобайт, незавершённых последовательностей
  не осталось, новых ошибок в журнале хуков нет. Осталась известная плата: на
  каждую дозапись журнала транзакция кладёт копию до и копию после, поэтому
  каталог операций вырос до четырёх с лишним гигабайт. Это в пределах
  тридцатидневного окна отката, но по сути квадратично и заслуживает
  отдельного решения.

- 2026-08-30 — CI поймал два падения, которых не видел мой локальный прогон,
  и это ровно то, ради чего он есть. Первое старше сегодняшнего дня: разбор
  времени в Python до одиннадцатой версии принимал только три или шесть знаков
  дробной секунды, а проект поддерживает десятую, и обычные полсекунды там
  давали отказ. Дополняю дробь до шести знаков перед разбором — миг тот же,
  вывод тот же. Второе моё: тест из этой сессии трогал сигнал, которого нет на
  Windows; теперь он пропускается там, где сигнала нет, и условие названо
  через сам сигнал, а не через имя платформы. Полный прогон на десятой версии
  зелёный.

- 2026-08-30 — Windows повалил восемь заданий, и это оказалось полезнее, чем
  выглядело. Четыре причины были в тестах: журнал переписывался текстом, а не
  байтами, и перевод строк ломал заголовок; пути вида `/srv/vault` выдавались
  за абсолютные, хотя на Windows абсолютный путь требует диска; тест группы
  процессов гонялся там, где групп нет. Пятая причина была не в тесте: кэш
  печатей ловил подделанное время изменения через `ctime`, а на Windows это
  время создания, которое переживает перезапись, — то есть подделку там никто
  бы не поймал. Кэш выключен там, где эта проверка не существует. Шестая — шов
  запуска языкового сервера склеивал аргумент текстом с прямым слешем, и на
  Windows выходил смешанный разделитель, которого прежний код не печатал.

- 2026-08-30 — Полный прогон CI показал то, чего не показывали отменённые:
  падали все пятнадцать шардов Windows. Две причины были в рабочем коде.
  Первая: корень репозитория сравнивался в двух написаниях, и на Windows
  совпадения не было никогда — индексация репозиториев там была сломана
  целиком, и никто этого не видел. Вторая: стенд долговечности вообще ничего
  не убивал, потому что звал сигнал, которого на Windows нет; теперь там
  выход без финализаторов, и в обоих файлах написано, что это доказательство
  слабее, чем настоящий сигнал, и выдавать одно за другое нельзя.

- 2026-08-31 — Последним упал один тест на Windows под Python 3.10, и это
  оказалась не платформа, а моё допущение: тест верил, что сон никогда не
  вернётся раньше срока. Там разрешение таймера грубее, сон возвращался
  раньше, бюджет ещё не был истрачен, и вторая нога поиска честно шла
  работать. Теперь ожидание идёт по часам, а не по сну. Путь по прогонам:
  пятнадцать упавших шардов, пять, один, ноль.

- 2026-08-31 — PR 13 стал зелёным целиком: прогон `33346978034` на коммите
  `af5e402`, пятьдесят проверок, ни одной упавшей. Дорога заняла пять кругов:
  пятнадцать упавших шардов Windows, потом пять, потом один, потом ноль. Две
  причины из шести были в рабочем коде, а не в тестах, и обе жили незамеченными
  с двадцать восьмого августа, потому что последний зелёный прогон был раньше
  них: индексация репозиториев на Windows отклоняла вообще всё, а стенд
  долговечности там ничего не убивал и честно об этом сообщал, только никто не
  смотрел.

- 2026-08-31 — Взялся мерить стоимость ответа в токенах и обнаружил, что
  измерение считало половину: системная часть промпта записывалась отдельно и
  в стоимость не входила, поэтому все прежние цифры стенда были занижены.
  Исправил и посчитал заново: около четырёх тысяч девятисот оценочных токенов
  на вопрос против почти семи тысяч у Mem0. То есть по стоимости мы уже
  дешевле сильнейшей публичной системы, а по точности отстаём втрое. Это
  уточняет цель: экономить токены дальше сейчас незачем, вся дистанция в
  качестве.

- 2026-08-31 — Появился стенд, который отличает улучшение от шума, и правило к
  нему записано до прогона, а не после. Рука выигрывает разряд только если
  превосходит базовую больше, чем на собственный разброс базовой; падение
  больше того же разброса — проигрыш, и он блокирует изменение, что бы ни
  делало общее среднее; всё прочее — «разницы не измерено», а это не «разницы
  нет». Проверил на настоящих отчётах: вчерашний прирост общей точности
  оказался ровно внутри разброса базовой, то есть не доказан, — как я и
  говорил вчера. Заодно стенд сам сообщает, когда руку прогнали слишком мало
  раз, чтобы вердикт считался измерением.

- 2026-08-31 — Базовая рука наконец измерена как следует: три прогона по
  двести вопросов. Разброс общей точности упал с семи сотых при пятидесяти
  вопросах до полутора сотых при двухстах — теперь изменение в две сотых
  доказуемо. Сама точность оказалась ниже прежних чисел, и это не ухудшение:
  в большой выборке больше тяжёлых разрядов, а прежние цифры были оптимистичны
  из-за состава выборки. Заодно вылезло то, чего не было видно: разряд
  предпочтений в одной сессии даёт ноль во всех трёх прогонах на двенадцати
  вопросах. Это не шум, это разряд, который не работает совсем, и в бэклоге он
  не назван.

- 2026-09-01 — Вчерашнее моё «разряд предпочтений не работает совсем» оказалось
  неверным, и это важнее самого разряда. Точность в отчёте считалась вхождением
  подстроки, а эталон там — не значение, а описание того, что ответ должен
  учесть; совпасть оно не могло никогда. Судья на тех же строках даёт по этому
  разряду четверть, а не ноль. Все числа, с которыми мы себя сравниваем, —
  судейские, а мы сравнивали их со счётом подстрок. Третья поломка измерения за
  два дня, той же породы, что и половина промпта в стоимости токенов. Теперь
  судья судит этот разряд по его рубрике, а отчёт несёт судейскую колонку.

- 2026-09-01 — Правку промпта про вопросы-советы отклонило собственное правило:
  общая точность вышла проигрышем, а число отвеченных вопросов не сдвинулось
  вовсе. Значит причина не в формулировке. Снял правку и оставил запись о том,
  что пробовал и почему отвергнуто, — чтобы никто, включая меня, не пробовал
  это второй раз как свежую мысль.

- 2026-09-02 — След отката оказался не страховкой, а привычкой: на каждую
  дописанную строчку система сохраняла файл целиком дважды, окно хранения
  лежало числом «тридцать» в четырёх местах, а чистку не вызывал никто —
  механизм был написан и ни разу не запускался. Пять гигабайт превратились в
  триста мегабайт, как только чистка впервые прошла. Окно теперь двое суток и
  одно число в одном месте, а сама чистка встроена в ночной проход.

- 2026-09-02 — И главное, чего не было вовсе: у памяти появилась вторая копия.
  Она лежит за пределами хранилища, чтобы пережить его удаление, у её
  репозитория нет и не будет адреса для отправки, и она хранит историю, так что
  можно посмотреть, как страница выглядела вчера. Взял git, а не Restic, хотя
  исследование советовало Restic: его преимущество — шифрование при отправке
  наружу, а наружу владелец отправлять отказался, и остаётся пароль, потеря
  которого уничтожает копию. Размен назван прямо: точечный откат любой записи за
  месяц обменян на восстановление на день назад. Копия на том же диске, поэтому
  от смерти диска она не спасает — эта дыра остаётся открытой.

- 2026-09-02 — Разобрал журнал потерянных захватов: из 452 записей живой
  оказалась одна причина, остальные прекратились ещё двадцать девятого вместе с
  починкой чекпойнтов. Живая была такая: ветка выбиралась по тому, что путь к
  записи сессии задан, а не по тому, что файл существует. Сессии, начатые вне
  проекта, кладут запись в отдельный каталог, и к концу сессии её там иногда уже
  нет — тогда захват шёл читать и падал. Ничего этим не спасалось: если файла
  нет, его содержимого нет. Теперь такая сессия идёт по ветке, написанной ровно
  для этого случая. Заодно выяснилось, что три теста задавали путь вида
  `C:/tmp/session.jsonl`, которого нет ни на одной машине, где они гоняются, —
  то есть проверяли ветку по наличию ключа, а не по наличию файла.

- 2026-09-02 — Ворота цитирования перестали убивать ответ целиком. Проверки были
  устроены по утверждению, а решение принималось по ответу: одна фраза с
  неточной ссылкой уничтожала весь ответ вместе с хорошими фразами. Когда я
  наконец сохранил выброшенное и посмотрел, оказалось, что семь из одиннадцати
  уничтоженных содержали верный ответ. Это не послабление: каждая дошедшая до
  читателя фраза по-прежнему обязана иметь цитату, которая находится, пересекается
  с фразой и сходится с ней по числам. Изменилось одно — неудачная фраза теперь
  выбрасывается сама, а не уводит с собой соседей. Если не уцелела ни одна, это
  отказ, а не ошибка. Два вида отказа остались ответными и намеренно: ссылка на
  то, чего системе не давали, и отказ, к которому пристёгнуты утверждения.

## 2026-09-02 — a failing claim no longer destroys the answer

The grounding gates were applied per claim and enforced per answer: one claim
whose citation failed took every other claim with it, and one unresolvable entry
in the citation list took the whole document. Measured over 200 questions, seven
of eleven destroyed answers had been correct, and 18 more died at the document
level. Both gates now drop instead of raising; when nothing survives the result
is an abstention rather than an error. The guarantee is unchanged — every
published claim still cites a span that resolves, touches it, and agrees on
figures. See `knowledge/notes/a-failing-claim-does-not-destroy-the-answer-decision.md`.

## 2026-09-03 — the search required every word of the question

FTS5 puts an implicit AND between bare terms, so a chunk had to contain every
word of a query. "What day of the week do I take a cocktail-making class?"
retrieved zero candidates from a vault where "cocktail class" retrieved three,
and three of fifty stand questions reached the model with an empty evidence
manifest. Terms are now joined with OR and function words dropped; the same
question retrieves forty. See
`knowledge/notes/a-question-is-not-a-conjunction-decision.md`.

Separately, a grounded reply no longer has to transcribe nine fields of the
evidence manifest byte for byte — it names the citation and we supply the
locator, which is stronger because the model's word about a locator is never
read. That transcription requirement had destroyed 18 answers in 200. See
`knowledge/notes/the-model-names-the-evidence-we-locate-it-decision.md`.

## 2026-09-03 — a fact is stored with its date

Relative dates the user states are now resolved at write time against the
entry's own day and written into the entry as a dated calendar; a question is
expanded with the dates it only implies, anchored on the day it is asked. Four
of nine substantive refusals on the stand were this. Nothing below a day is
resolved, nor months or years, and only the user's own turns are read — a model
writes "last night" for an hour ago. See
`knowledge/notes/a-fact-is-stored-with-its-date-decision.md`.

## 2026-09-05 — the index had been missing for five days

An outside reviewer found search degraded to lexical only. Confirmed: no active
generation since 2026-08-30, complete vectors unreachable on disk. Three causes —
a span cut inside a UTF-8 character, a capture that surrendered on its first lost
race, and a publication fence demanding the vault hold still for the length of a
build. All three addressed; a generation is active again and queries report
HYBRID with a dense leg. See `knowledge/notes/an-index-may-lag-decision.md`.

Separately, the project journal had 3713 events with nothing in any delta,
because a flag about agent narration was read as if it meant "carries content".
Fixed and verified live. Red `main` fixed: a Windows timing assertion was
comparing four clock ticks with one.
