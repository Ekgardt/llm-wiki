# LLM Wiki: статус полного аудита 2026-08-14

Дата среза: 2026-08-14

Последнее обновление candidate: 2026-08-16

Candidate worktree: dirty local `main`

Base HEAD: `2970149b9f1207ac5a3e91e3350b37888e3259ca`

Источник реестра:
`docs/DEVELOPER-AUDIT-HANDOFF-2026-08-12.md` из worktree
`llm-wiki-v340-runtime-recovery`.

Этот документ является status overlay, а не заменой исходного подробного реестра.
Candidate содержит большой набор staged, unstaged и ранее untracked изменений, поэтому
его нельзя обозначать одним commit SHA. Ни один локальный результат ниже не доказывает
deployment, remote CI или release.

## Статусы

| Статус | Значение |
|---|---|
| `CLOSED_CODE` | Полный критерий пункта реализован в candidate и защищён regression tests. Это не deployment/release proof. |
| `PARTIAL` | Существенная часть реализована, но хотя бы одна обязательная ветка критерия отсутствует. |
| `OPEN` | Критерий не реализован либо реализация не достигает его основной цели. |
| `EVIDENCE_PENDING` | Кодовая часть может существовать, но критерий требует live, external, clean-machine, remote CI или model-quality evidence. |
| `PROVEN` | Требуемый внешний evidence artifact существует и проверен. |
| `RETIRED` | Критерий относился к функции, явно удалённой из поддерживаемого продукта утверждённым решением. |

## Итог

| Реестр | Итог |
|---|---|
| `FIX-001..060` | 60/60 остаются исправленными в candidate на уровне кода/regression. Deployment-состояние из среза 2026-08-12 повторно не проверялось. |
| `OPEN-001..043` | `28 CLOSED_CODE`, `12 PARTIAL`, `0 OPEN`, `3 EVIDENCE_PENDING`. |
| `EVID-001..017` | `0 PROVEN`, `14 EVIDENCE_PENDING`, `3 RETIRED`. |
| `OUT-001` | `CLOSED_CODE`: MCP является текущей 12-tool функцией; VS Code Copilot явно остаётся planned и отсутствует в текущем каталоге интеграций. |

## FIX Matrix

`FIX-001..060` сохраняют статус `CODE_PRESENT + REGRESSION`. Исходный handoff содержит
описание и evidence для каждого ID. В текущем candidate не найдено оснований переводить
отдельный FIX обратно в open. Это утверждение ограничено исходниками и тестами:

- установленная копия после accumulated changes не проверялась;
- exact candidate commit отсутствует;
- remote CI и immutable release отсутствуют;
- live-состояния, перечисленные в handoff, относятся к срезу 2026-08-12 и не
  переиспользуются как доказательство нового candidate.

## OPEN Matrix

| ID | Статус | Текущее состояние и проверяемый evidence | Оставшийся критерий |
|---|---|---|---|
| OPEN-001 | PARTIAL | `private_vault_backup.py` реализует exact Restic 0.19.1 CLI, canonical ownership fence, SQLite online backup, source recapture, immutable manifest, exact snapshot/digest receipt, mandatory repository check и clean-target restore с file/link/hash/schema/integrity/ownership validation. Invalid restore очищает plaintext target; success не публикуется поверх installed vault. Tests: `test_private_vault_backup.py`, ownership/runtime-deletion/adoption regressions. | Нужны реальный encrypted external repository, clean-machine restore evidence и manifest-owned publication validated image в installed paths через утверждённый install/recovery control plane. |
| OPEN-002 | CLOSED_CODE | Все first-party model calls проходят через `call_candidate` и `model_dlp.py`: built-in redaction всегда активен, optional absolute policy ограничена literals/exact fingerprints и аутентифицирована digest; policy/scanner failures блокируют transport. Transport-level regressions находятся в `tests/test_llm_descriptors.py`. | Нет на уровне code criterion; live provider evidence остаётся внешним. |
| OPEN-003 | CLOSED_CODE | Provider output блокируется в `call_candidate` до возврата и digest. Compile/contradiction transactions сохраняют `content_guard=model_output`; exact after-image bytes повторно проверяются внутри fenced mutation непосредственно перед publication. Apply и recovery quarantine/rollback сохраняют exact old bytes. См. `model_dlp.py`, `markdown_transaction.py`, `test_markdown_transaction.py`. | Нет на уровне code criterion. |
| OPEN-004 | PARTIAL | Fresh bootstrap принимает exact 40-char OID и проверяет HEAD (`install.sh`, `install.ps1`, `tests/test_installer_bootstrap.py`), но README не закрепляет опубликованный release OID и нет release file/hash manifest. | Immutable release reference и clean-machine verification exact version/files. |
| OPEN-005 | PARTIAL | `install_control.py` v2 resumably reconciles profile/environment/scheduler и managed Cursor/Antigravity hook fragments, сохраняет prior manifest до verified publication и поддерживает latest-update rollback. Existing checkout code и dependencies всё ещё не обновляются, а remote bootstrap отвергает существующий destination. | Расширить transaction boundary на code/dependencies и оставшиеся agent configs; затем получить clean-machine update/rollback evidence. |
| OPEN-006 | PARTIAL | Strict `install-manifest/v2` и `install-transaction/v2` связывают exact release с owned profile/environment/scheduler и Cursor/Antigravity structural fragments, bounded preimages, rollback и uninstall; validated v1 state adopts без guesswork. Active/nonterminal/corrupt install state блокирует runtime deletion. Tests: `test_install_control.py`, `test_integration_hook_config.py`, `test_runtime_deletion_contract.py`, `test_doctor.py`. | Source tree, dependencies, Claude/Codex configs и restored-vault publication пока не являются manifest-owned resources; live uninstall/rollback evidence остаётся EVID-006. |
| OPEN-007 | PARTIAL | `export_vault.py` честно экспортирует только tracked `HEAD`, но всё ещё называется vault migration; private importer/exporter отсутствует. | Переименование source-only export и отдельный verified private migration path. |
| OPEN-008 | CLOSED_CODE | `export_vault.py` создаёт sibling staging archive и публикует final path только после обязательного bounded scan для ZIP/TAR/TAR.GZ. Проверка fail-closed ограничивает compressed size, member count, per-member и aggregate uncompressed bytes; отвергает ambiguous/duplicate/forbidden/non-regular entries; сканирует content и metadata через built-in secrets и аутентифицированные policy literals/exact fingerprints. `--no-verify` больше не обходит security boundary. Regressions находятся в `tests/test_audit_runtime_contracts.py`. | Нет на уровне code criterion; exact-ref inventory и clean extraction остаются EVID-016. |
| OPEN-009 | PARTIAL | README/USER-GUIDE/ARCHITECTURE теперь отделяют локальное хранение от model processing. Strict `MEMORY_LLM_PROVIDER=ollama` + `OLLAMA_NO_CLOUD=1` отключает fallback, принимает только explicit `127.0.0.1`/`::1` и требует local `/api/tags` metadata без `remote_model`/`remote_host`; bypass через `available=True` закрыт regression. Descriptor честно сообщает `external_runtime_unverified`. | Нужен способ доказать, что независимо управляемый Ollama server действительно перезапущен с cloud disabled, либо LLM Wiki должен владеть процессом/network fence; live evidence также отсутствует. |
| OPEN-010 | CLOSED_CODE | Official local Cursor и Antigravity user hooks проходят через `integration_adapter.py`, canonical bounded `EventEnvelope`, redaction-before-identity и существующий transactional occurrence-receipt path. Installer v2 владеет только exact structural fragments в `~/.cursor/hooks.json` и `~/.gemini/config/hooks.json`, сохраняет unrelated config, поддерживает resumable update/latest rollback/uninstall и fail-closed drift. Regressions: `test_ide_hook_integrations.py`, `test_integration_hook_config.py`, `test_install_control.py`, `test_doctor.py`. | Нет на уровне code criterion; Cursor cloud agents не загружают user hooks, а live five-agent и clean-machine lifecycle evidence остаются EVID-001/EVID-006. |
| OPEN-011 | EVIDENCE_PENDING | Unconditional OpenCode provider removed; `llm_client.py` auto-detects Codex/Claude and component tests cover ordering. | Отдельные Codex-only и Claude-only fresh install: capture -> compile -> next-session retrieval. |
| OPEN-012 | CLOSED_CODE | Canonical `integration_adapter.py` синхронно публикует create-only `capture-intent/v1` для bounded SessionEnd/PreCompact transcript evidence до detached wake и сверяет file-first pending/ready publication с active validated Queue/Coordinator v3 pair. Exact replay переиспользует binding/terminal, conflicting bytes блокируются; worker resumably публикует immutable decision/terminal evidence без повторного provider или Markdown side effect. Source cleanup требует terminal proof, а ordinary purge является export-first и crash-resumable с retained task authorization. Regressions: `test_plugin_helpers.py`, `test_capture_terminal.py`, `test_memory_queue.py`, `test_operational_migrations.py`, `test_integration_injection.py`, `test_ide_hook_integrations.py`. | Нет на уровне code criterion; live repeated compact/path capture остаётся EVID-009. |
| OPEN-013 | PARTIAL | Project checkpoint errors получают redacted log, но prompt/tool parse/state/append failures и `DEVNULL` child failures остаются без полного durable diagnostic/counter contract. | Durable bounded diagnostics для каждой skip branch и поддерживаемый host signal при невозможности диагностики. |
| OPEN-014 | CLOSED_CODE | `blackboard.py` нормализует bounded resource set и атомарно резервирует весь набор в fenced `blackboard_claims` внутри существующей coordinator-v3 DB. `BEGIN IMMEDIATE`, primary key `(project, resource)` и rollback дают all-or-none exclusion; multiprocess same-resource regression получает ровно одного winner. | Нет на уровне code criterion; live multi-agent evidence остаётся EVID-001. |
| OPEN-015 | CLOSED_CODE | Blackboard reads используют stable bounded snapshots, malformed JSONL даёт explicit corruption вместо silent skip, а multiprocess regressions покрывают согласованность. См. `blackboard.py`, `tests/test_blackboard.py`. | Нет на уровне code criterion. |
| OPEN-016 | CLOSED_CODE | Blackboard claim содержит 256-bit lease token, heartbeat/expiry и per-resource monotonic fencing epoch. Heartbeat требует exact token+epoch и не возрождает expired lease; reclaim увеличивает epoch, а stale holder получает `BlackboardFenceError`. Live claims теперь также дают `blackboard_claim_retained` в runtime-deletion diagnostics. | Нет на уровне code criterion; длительный live-agent run остаётся внешним evidence. |
| OPEN-017 | PARTIAL | `query_memory.py` требует exact path/hash/span citations, но не доказывает entailment/relevance claim к span. | Extractive output либо independent support verifier и adversarial irrelevant-citation test. Требуется grounding contract. |
| OPEN-018 | CLOSED_CODE | Legacy publication повторно проверяет source digest под publication lock; stale builder не может заменить более новый индекс. Orchestrated regressions покрывают old/new builder и reader race. См. `search_memory.py`, `tests/test_search_ranking.py`. | Нет на уровне code criterion. |
| OPEN-019 | PARTIAL | Unix installer теперь по умолчанию публикует per-user LaunchAgent на macOS и user-systemd units на Linux через resumable install ownership; cron доступен только через explicit `--scheduler cron`. LaunchAgent definitions получают vault/runtime environment. | Нужны реальный macOS/Linux install/login/logout/sleep run и решение для общего GUI environment вне scheduler jobs. |
| OPEN-020 | PARTIAL | Unsafe `clear-failed` удалён; dead tasks сохраняются. Export-first purge покрывает terminal succeeded/cancelled, но нет reviewed failed-task delete/restore path из исходного критерия. | Формально изменить критерий на indefinite retention либо реализовать reviewed manifest + restore. |
| OPEN-021 | CLOSED_CODE | Конфликт определяется по canonical resource до мутации. Busy request публикует immutable bounded `conflict` event с holder identity; operator resolution добавляет отдельный immutable `resolution` event, не переписывая историю. Coherent reads и malformed-stream tests исключают silent skip. | Нет на уровне code criterion. |
| OPEN-022 | CLOSED_CODE | Exact normalized filename продвигается после fusion и повторно после reranker, до final candidate cap. Generation и legacy paths сохраняют rank 1 в BASE/EXACT/HYBRID, включая dense и forced-reranker regressions. См. `retrieval.py`, `test_retrieval_review_round2.py`, `test_search_ranking.py`. | Нет на уровне code criterion. |
| OPEN-023 | CLOSED_CODE | Nightly извлекает wikilinks из одного immutable multi-file knowledge snapshot, partition сохраняет source ownership/dependencies, затем CAS публикует generation. Regression изменяет link A на B и доказывает результат через два fresh Python processes без process-local cache. См. `doctor.py`, `test_generation_maintenance.py`. | Нет на уровне code criterion. |
| OPEN-024 | CLOSED_CODE | QMD удалён из active tiers/docs/installers; `tests/test_quality_guards.py` запрещает текущие QMD claims. | Нет. EVID-003 не требуется, пока QMD остаётся вне продукта. |
| OPEN-025 | CLOSED_CODE | Docs и `archive_stale.py` честно говорят: archived Markdown сохраняется, но исключается из active retrieval; regression в `tests/test_archive_stale.py`. | Нет. |
| OPEN-026 | CLOSED_CODE | Capture envelope использует canonical source ID `opencode`/`codex`/`claude`; Cursor и Antigravity распознаются тем же bounded identity contract. Agent-aware breadcrumbs сохраняют origin, historical breadcrumb format остаётся readable. Для compiled pages `agent_timeline.py` разрешает существующие content-addressed evidence references через `EvidenceResolver` и возвращает исходных авторов вместо synthetic `compile`. Five-agent daily -> compile -> timeline regression находится в `tests/test_compile_transactions.py`; capture/timeline regressions — в `tests/test_audit_runtime_contracts.py` и `tests/test_capture_hooks.py`. | Нет на уровне code criterion; live five-agent proof остаётся EVID-001. |
| OPEN-027 | CLOSED_CODE | `loop_detector.py` разбирает agent-aware и historical breadcrumbs, разделяет `single_agent_churn` и `multi_agent_loop`, а также независимо группирует `recurring_error` по normalized exception family с volatile UUID/hex/number placeholders и distinct-day threshold. Regression tests покрывают все три класса и direct exception lines. | Нет на уровне code criterion. |
| OPEN-028 | CLOSED_CODE | Installer success связан с `sync_memory` exit/schema/generation validation; failures/remaining work дают truthful nonzero/partial. См. `sync_memory.py`, installer branches, `tests/test_sync_memory.py`. | Нет на уровне code criterion. |
| OPEN-029 | CLOSED_CODE | Windows registration проверяется post-registration через `Test-LLMWikiScheduledTasks`; failure остаётся warning/nonzero, а summary и USER-GUIDE сообщают фактический principal/logon mode без обещания неподтверждённого sleep execution. | Нет на уровне code criterion; реальный scheduler run остаётся EVID-014. |
| OPEN-030 | CLOSED_CODE | Profile marker block имеет bounded exact-fragment preimage и восстанавливается через `rollback`/`uninstall` только при exact installed-value match, не затирая concurrent edits. Atomic replacement сохраняет mode и исходные POSIX UID/GID; whole profile с возможными secrets намеренно не копируется. Regression: `test_profile_install_preserves_unrelated_bytes_and_mode_without_copying_secrets`. | Нет на уровне code criterion; live install/rollback/uninstall остаётся EVID-006. |
| OPEN-031 | CLOSED_CODE | `integration_config_backup.py` создаёт и проверяет byte-exact Claude/Codex/Cursor/Antigravity sibling preimage до atomic publish, затем применяет 10-file/90-day/100-MiB retention. Pruning выполняется также на no-op и missing-destination merge; newest/sole restore point защищён. Tests: `tests/test_merge_claude_settings.py`, `tests/test_integration_injection.py`, `tests/test_integration_hook_config.py`. | Нет на уровне code criterion; owner/ACL/ADS и full-vault recovery явно вне scope. |
| OPEN-032 | CLOSED_CODE | Два `publish_result` принадлежат разным классам и не образуют override. Active `MemoryQueue.publish_result` выполняет bounded validation, create-only publication, fsync, digest verification, lease fencing и связывает artifact с task row; crash/orphan regressions проходят. См. `memory_queue.py`, `test_memory_queue.py`, `test_memory_queue_races.py`. | Нет на уровне code criterion. |
| OPEN-033 | EVIDENCE_PENDING | Third-party actions pinned full SHA; `tests/test_ci_policy.py` enforces allowlist. Candidate dirty и не имеет remote run exact commit. | Successful remote Windows/Linux/macOS run для final SHA; см. EVID-005. |
| OPEN-034 | EVIDENCE_PENDING | Tier parser/tests существуют, но нет labelled real-session corpus и false-negative metrics для decisions/fixes/gotchas. | Pinned model corpus, per-class metrics, threshold и manual sample artifact. |
| OPEN-035 | CLOSED_CODE | README/USER-GUIDE синхронизированы: BM25 always, vector opt-in/when available; `tests/test_readme_i18n.py` guard. | Нет; real embedding остаётся EVID-008. |
| OPEN-036 | PARTIAL | Generation и legacy lexical paths теперь сохраняют pre-rounding score для сортировки, а веса соответствуют documented hierarchy `user > web > ai-derived > inferred`. Generation regression доказывает порядок. Dense/post-fusion policy и benchmark cases пока не покрыты. | Единый measurable trust contract для dense/fusion/rerank и benchmark cases. |
| OPEN-037 | CLOSED_CODE | README/USER-GUIDE и оба installer summary показывают OpenCode, Codex, Claude, Cursor и Antigravity отдельно с active/manual/conflict/viewer состояниями; detected больше не означает automatic capture. | Нет на уровне code criterion; live five-agent evidence остаётся EVID-001. |
| OPEN-038 | CLOSED_CODE | Cognee bridge и его conflicting `LLM_PROVIDER` path удалены утверждённым retirement. `MEMORY_LLM_PROVIDER` остаётся единственным first-party provider contract; forced provider не fallback'ит, а local/cloud Ollama negative paths покрыты regressions. | Нет на уровне code criterion. |
| OPEN-039 | CLOSED_CODE | Все три README называют pre-commit opt-in и дают `pre-commit install`; i18n regression закрепляет текст. | Нет. |
| OPEN-040 | PARTIAL | `maintenance_helpers.py` redacts output до отчёта, но capture остаётся unbounded, full protected artifact отсутствует, weekly/error retention не имеет общих age/count/size limits. | Bounded streaming capture, artifact pointer и единая retention policy. |
| OPEN-041 | CLOSED_CODE | `capture_operation.py` резервирует retry-stable occurrence ID до append, повторно использует pending/committed reservation для replay и создаёт новый ID для одинакового later event после rate window. Explicit host `event_id` сохраняется как source identity; prompt/tool completion публикуется только после append success. Crash, replay, contention и later-repeat regressions проходят в `test_capture_hooks.py` и `test_automatic_writer_integration.py`. | Нет на уровне code criterion. |
| OPEN-042 | CLOSED_CODE | Старый `drain` заменён bounded `work`; structured counts и nonzero для failed/dead/remaining covered в `tests/test_memory_queue.py` и `tests/test_memory_queue_cli.py`. | Нет. |
| OPEN-043 | CLOSED_CODE | После одного rebuild/retry повторный SQLite failure использует bounded canonical Markdown fallback; no-match даёт redacted exit 2. Tests: `tests/test_search_ranking.py`. | Нет. |

## EVID Matrix

Ни один EVID пункт нельзя закрыть локальным unit/full-suite test без требуемого
внешнего artifact.

| ID | Статус | Недостающее доказательство |
|---|---|---|
| EVID-001 | EVIDENCE_PENDING | Live two-agent, затем five-agent write -> compile -> read с isolation и attribution. |
| EVID-002 | RETIRED | Cognee удалён из поддерживаемого продукта решением 2026-08-15; legacy `cache/cognee/` остаётся disposable и не удаляется автоматически. |
| EVID-003 | RETIRED | QMD удалён из поддерживаемого продукта. |
| EVID-004 | EVIDENCE_PENDING | Versioned 500+ page long-term-memory dataset, fixed seed, approved thresholds и publishable report. Текущий benchmark явно `release_evidence: false`. |
| EVID-005 | EVIDENCE_PENDING | Successful remote Windows/Linux/macOS CI, привязанный к final exact SHA. |
| EVID-006 | EVIDENCE_PENDING | Disposable Windows/Ubuntu/macOS install -> capture -> compile -> maintenance -> update -> rollback -> uninstall. |
| EVID-007 | RETIRED | Obsidian остаётся viewer-only; Web Clipper не входит в поддерживаемый продукт. |
| EVID-008 | EVIDENCE_PENDING | Real embedding model load, cache reuse/corruption rebuild и semantic-only recall на supported ОС. |
| EVID-009 | EVIDENCE_PENDING | Live repeated `/compact` и path-based PreCompact без loss/duplicate. |
| EVID-010 | EVIDENCE_PENDING | Inbox/raw source -> page -> sources/wikilinks -> index/log -> retrieval artifact. |
| EVID-011 | EVIDENCE_PENDING | No-mutation review fixture с orphan/stale/duplicate/missing-source/contradiction и prioritized report. |
| EVID-012 | EVIDENCE_PENDING | Successful-task workflow draft плюс rejection failed/duplicate cases. |
| EVID-013 | EVIDENCE_PENDING | Bridge promotion с reciprocal provenance и idempotent retry. |
| EVID-014 | EVIDENCE_PENDING | Реальный Task Scheduler, LaunchAgent, user-systemd и explicit cron run, result/report/index и login/logout/sleep behavior. |
| EVID-015 | EVIDENCE_PENDING | Intentional OpenCode plugin break -> repair -> restart -> new capture/context. |
| EVID-016 | EVIDENCE_PENDING | Exact-ref/subtree source export с deterministic inventory, content scan и clean extraction. |
| EVID-017 | EVIDENCE_PENDING | Внешний OKF consumer без custom adaptation. |

## Product Boundary

| ID | Статус | Основание |
|---|---|---|
| OUT-001 | CLOSED_CODE | `scripts/mcp_server.py` реализует locked 12-tool stdio MCP; `pyproject.toml`, README и USER-GUIDE описывают его как текущую функцию; `integrations/README.md` явно помечает VS Code Copilot как planned/not implemented; `tests/test_mcp_server.py` закрепляет tool contract. Live/release evidence остаётся в EVID-001/005/006. |

## Проверка Candidate

- RED -> GREEN для `OPEN-031`: byte-exact preimage, no-op, changed config,
  missing destination, count/age/aggregate-size bounds, collision rollover и
  preservation newest/sole restore point.
- Focused integration state после всех retention изменений:
  `98 passed, 13 skipped`.
- Full local suite после изменений `OPEN-022`, `OPEN-036` и `OPEN-041`:
  `6204 passed, 258 skipped`, включая `OPEN-023`.
- Latest focused suites: search ranking `115 passed`, capture hooks `37 passed`,
  retrieval review `52 passed`.
- Generation maintenance `53 passed`; knowledge extractor и incremental graph
  `40 passed`, включая edited-link -> nightly -> fresh-process regression.
- Quality/structure/wikilink guards после documentation changes:
  `55 passed`.
- Audit-closure DLP/provider/transaction/compile suites после изменений 2026-08-15:
  `247 passed, 4 skipped`; дополнительные LLM-dependent component suites:
  `273 passed, 2 skipped`.
- Current quality/structure/README/CI guards: `82 passed`; automatic-writer integration:
  `131 passed, 1 skipped`; security invariants: `54 passed`.
- Structural memory lint с explicit public-source roots: `7 findings` — только
  `3 orphan_daily_logs` и `4 missing_backlinks`; новых broken/untracked decision links нет.
- Private-vault backup/restore: `15 passed`; canonical ownership: `40 passed`;
  runtime deletion: `68 passed`; Reliability-v3 adoption: `12 passed`; transaction:
  `87 passed, 3 skipped`; doctor: `160 passed, 9 skipped`.
- Install ownership/Doctor/deletion focused verification after the 2026-08-16 integration:
  `267 passed, 9 skipped`; standalone install-control suite: `31 passed`.
- Blackboard/coordinator verification: blackboard `8 passed`; coordinator migrations
  `33 passed`; canonical ownership `40 passed`; transaction recovery `89 passed`;
  markdown transactions `87 passed, 3 skipped`; automatic writers `131 passed, 1 skipped`.
- Attribution/loop, touched lifecycle/compile и quality/structure regression slice:
  `341 passed, 14 skipped`.
- `OPEN-008` RED -> GREEN: `12 passed` в focused export slice; полный
  `tests/test_audit_runtime_contracts.py`: `22 passed`.
- `OPEN-010` acceptance slice по hooks/install/Doctor/sync/capture/docs/structure:
  `549 passed, 37 skipped`; полные integration-injection + automatic-writer suites:
  `222 passed, 14 skipped`; security invariants: `54 passed`.
- Дополнительный loaded cross-boundary run достиг `759 passed, 38 skipped` и выявил
  один intermittent pre-existing writer-heartbeat timeout; точный race-test после этого
  прошёл изолированно. Это не считается full-suite proof.
- Ruff по install-control, Doctor, installed-memory repair и их changed tests: clean;
  `py_compile`: clean; Ruff C901 with maximum 5 reports no changed-function finding.
- Ruff по затронутым Python files: clean.
- `git diff --check`: clean, кроме информационного LF -> CRLF warning для
  существующего Windows worktree.

Remote CI, deployment, tag и release этим документом не подтверждаются.
