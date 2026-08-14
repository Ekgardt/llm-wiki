# LLM Wiki: статус полного аудита 2026-08-14

Дата среза: 2026-08-14

Candidate worktree: `merge/all-accumulated`

Base HEAD: `1996a1b3c953cc752c47ed375d13e8122adce7d4`

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

## Итог

| Реестр | Итог |
|---|---|
| `FIX-001..060` | 60/60 остаются исправленными в candidate на уровне кода/regression. Deployment-состояние из среза 2026-08-12 повторно не проверялось. |
| `OPEN-001..043` | `12 CLOSED_CODE`, `14 PARTIAL`, `14 OPEN`, `3 EVIDENCE_PENDING`. |
| `EVID-001..017` | `0 PROVEN`, `17 EVIDENCE_PENDING`. |
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
| OPEN-001 | OPEN | Private knowledge и `run/` остаются gitignored; integration preimages явно не являются disaster-recovery backup. См. `.gitignore`, `docs/ARCHITECTURE.md`, `knowledge/notes/integration-config-backup-retention-decision.md`. | Зашифрованный full backup/restore на чистую машину с hash/recovery verification. Требуется отдельный архитектурный договор. |
| OPEN-002 | PARTIAL | Есть bounded inputs и best-effort `secret_redact.py`, но `llm_client.py`, compile/advisory/contradiction и `cognee_sync.py` не проходят через один fail-closed outbound DLP adapter. | Единая обязательная sanitization boundary и transport-level secret fixtures. Требуется отдельный архитектурный договор. |
| OPEN-003 | PARTIAL | Queue payload redaction выполняется до digest, но provider-generated `body_markdown` в `compile_memory.py` не имеет финального fail-closed scan до seal/publication. | Scan всех content fields до digest и повторная проверка перед publication с replay/restore tests. Требуется отдельный архитектурный договор. |
| OPEN-004 | PARTIAL | Fresh bootstrap принимает exact 40-char OID и проверяет HEAD (`install.sh`, `install.ps1`, `tests/test_installer_bootstrap.py`), но README не закрепляет опубликованный release OID и нет release file/hash manifest. | Immutable release reference и clean-machine verification exact version/files. |
| OPEN-005 | OPEN | Existing checkout не обновляется; remote bootstrap отвергает существующий destination. | Resumable upgrade transaction с rollback для code, deps, integrations и scheduler. |
| OPEN-006 | PARTIAL | Component preimages и task uninstall существуют, но нет whole-install ownership manifest, uninstall и rollback. | Manifest-bound rollback/uninstall всех owned external mutations. Требуется отдельный архитектурный договор. |
| OPEN-007 | PARTIAL | `export_vault.py` честно экспортирует только tracked `HEAD`, но всё ещё называется vault migration; private importer/exporter отсутствует. | Переименование source-only export и отдельный verified private migration path. |
| OPEN-008 | OPEN | `_verify_archive()` проверяет archive member paths, но не content secrets и custom patterns. | Fail-closed content scanner. Реализация отложена до allowlist/custom-pattern contract. |
| OPEN-009 | OPEN | Документация всё ещё смешивает local storage и cloud-backed processing; enforceable local-only mode отсутствует. | Per-backend disclosure и network-enforced local-only mode. Требуется продуктовый договор. |
| OPEN-010 | OPEN | Cursor/Antigravity всё ещё используют legacy direct append без полного canonical capture envelope и durable receipt. | Supported capture API и regressions для обеих IDE integrations; live cross-agent proof остаётся EVID-001. |
| OPEN-011 | EVIDENCE_PENDING | Unconditional OpenCode provider removed; `llm_client.py` auto-detects Codex/Claude and component tests cover ordering. | Отдельные Codex-only и Claude-only fresh install: capture -> compile -> next-session retrieval. |
| OPEN-012 | OPEN | Failed detached SessionEnd/path-only PreCompact всё ещё удаляет transient evidence; queue-v3 capture-intent primitives не подключены к producers. См. `session_end_capture.py`, `precompact_capture.py`, `integration_adapter.py`, `tests/test_capture_hooks.py`. | Activate create-only capture intents и terminal proof semantics. Approved target не равен implementation; activation требует отдельного согласования. |
| OPEN-013 | PARTIAL | Project checkpoint errors получают redacted log, но prompt/tool parse/state/append failures и `DEVNULL` child failures остаются без полного durable diagnostic/counter contract. | Durable bounded diagnostics для каждой skip branch и поддерживаемый host signal при невозможности диагностики. |
| OPEN-014 | OPEN | `blackboard.py` создаёт независимые random claims без atomic canonical exclusion. | Ровно один owner для concurrent same-resource claim. Требуется общий control-plane/fencing договор. |
| OPEN-015 | PARTIAL | Blackboard append использует transaction boundary, но read snapshot unbounded/unlocked и malformed lines пропускаются. | Stable bounded read, explicit corruption и multiprocess stress. |
| OPEN-016 | OPEN | Blackboard не имеет lease ID, heartbeat, expiry, reclaim и fencing. | Renewable lease и stale-owner fencing. Требуется общий control-plane договор. |
| OPEN-017 | PARTIAL | `query_memory.py` требует exact path/hash/span citations, но не доказывает entailment/relevance claim к span. | Extractive output либо independent support verifier и adversarial irrelevant-citation test. Требуется grounding contract. |
| OPEN-018 | PARTIAL | Legacy FTS имеет unique temp и swap lock; generation catalog имеет CAS. Legacy old/new builder race всё ещё может опубликовать stale build, а index/manifest меняются раздельно. | Predecessor/source-generation CAS и orchestrated old/new reader race. |
| OPEN-019 | OPEN | Unix installer сохраняет env только в shell profile и использует cron; launchd/GUI login path отсутствует. | Supported macOS/Linux GUI env и launchd E2E. Это path/runtime architecture change. |
| OPEN-020 | PARTIAL | Unsafe `clear-failed` удалён; dead tasks сохраняются. Export-first purge покрывает terminal succeeded/cancelled, но нет reviewed failed-task delete/restore path из исходного критерия. | Формально изменить критерий на indefinite retention либо реализовать reviewed manifest + restore. |
| OPEN-021 | OPEN | Blackboard conflict detector сравнивает words после claim и не хранит durable conflict/resolution. | Normalized resource set, pre-mutation exclusion и immutable resolution events. |
| OPEN-022 | CLOSED_CODE | Exact normalized filename продвигается после fusion и повторно после reranker, до final candidate cap. Generation и legacy paths сохраняют rank 1 в BASE/EXACT/HYBRID, включая dense и forced-reranker regressions. См. `retrieval.py`, `test_retrieval_review_round2.py`, `test_search_ranking.py`. | Нет на уровне code criterion. |
| OPEN-023 | CLOSED_CODE | Nightly извлекает wikilinks из одного immutable multi-file knowledge snapshot, partition сохраняет source ownership/dependencies, затем CAS публикует generation. Regression изменяет link A на B и доказывает результат через два fresh Python processes без process-local cache. См. `doctor.py`, `test_generation_maintenance.py`. | Нет на уровне code criterion. |
| OPEN-024 | CLOSED_CODE | QMD удалён из active tiers/docs/installers; `tests/test_quality_guards.py` запрещает текущие QMD claims. | Нет. EVID-003 не требуется, пока QMD остаётся вне продукта. |
| OPEN-025 | CLOSED_CODE | Docs и `archive_stale.py` честно говорят: archived Markdown сохраняется, но исключается из active retrieval; regression в `tests/test_archive_stale.py`. | Нет. |
| OPEN-026 | OPEN | `agent_timeline.py` знает только OpenCode/Codex/Claude, а compiled pages получают attribution `compile`. | Canonical five-agent origin через capture -> compile -> timeline. |
| OPEN-027 | OPEN | `loop_detector.py` считает edit/feedback churn, но не различает single/multi-agent loops и recurring errors. | Три отдельные классификации и tests. |
| OPEN-028 | CLOSED_CODE | Installer success связан с `sync_memory` exit/schema/generation validation; failures/remaining work дают truthful nonzero/partial. См. `sync_memory.py`, installer branches, `tests/test_sync_memory.py`. | Нет на уровне code criterion. |
| OPEN-029 | PARTIAL | Registration failure теперь даёт warning/nonzero и truthful summary. Successful registration не проверяет фактическое task state и не сообщает interactive/logged-on mode; USER-GUIDE обещает sleep execution. | Post-registration state verification и truthful principal/logon documentation. |
| OPEN-030 | OPEN | Unix profile merge не создаёт durable restore point и не сохраняет mode/ownership. | Approved durable profile-backup path и restore command. |
| OPEN-031 | CLOSED_CODE | `integration_config_backup.py` создаёт и проверяет byte-exact Claude/Codex sibling preimage до atomic publish, затем применяет 10-file/90-day/100-MiB retention. Pruning выполняется также на no-op и missing-destination merge; newest/sole restore point защищён. Tests: `tests/test_merge_claude_settings.py`, `tests/test_integration_injection.py`. | Нет на уровне code criterion; owner/ACL/ADS и full-vault recovery явно вне scope. |
| OPEN-032 | CLOSED_CODE | Два `publish_result` принадлежат разным классам и не образуют override. Active `MemoryQueue.publish_result` выполняет bounded validation, create-only publication, fsync, digest verification, lease fencing и связывает artifact с task row; crash/orphan regressions проходят. См. `memory_queue.py`, `test_memory_queue.py`, `test_memory_queue_races.py`. | Нет на уровне code criterion. |
| OPEN-033 | EVIDENCE_PENDING | Third-party actions pinned full SHA; `tests/test_ci_policy.py` enforces allowlist. Candidate dirty и не имеет remote run exact commit. | Successful remote Windows/Linux/macOS run для final SHA; см. EVID-005. |
| OPEN-034 | EVIDENCE_PENDING | Tier parser/tests существуют, но нет labelled real-session corpus и false-negative metrics для decisions/fixes/gotchas. | Pinned model corpus, per-class metrics, threshold и manual sample artifact. |
| OPEN-035 | CLOSED_CODE | README/USER-GUIDE синхронизированы: BM25 always, vector opt-in/when available; `tests/test_readme_i18n.py` guard. | Нет; real embedding остаётся EVID-008. |
| OPEN-036 | PARTIAL | Generation и legacy lexical paths теперь сохраняют pre-rounding score для сортировки, а веса соответствуют documented hierarchy `user > web > ai-derived > inferred`. Generation regression доказывает порядок. Dense/post-fusion policy и benchmark cases пока не покрыты. | Единый measurable trust contract для dense/fusion/rerank и benchmark cases. |
| OPEN-037 | PARTIAL | README/USER-GUIDE различают automatic/manual/viewer-only, но installer summaries объединяют detected agents и затем говорят, что capture automatic. | Per-agent active/manual/conflict/viewer status в обоих installers. |
| OPEN-038 | OPEN | Cognee docs используют `MEMORY_LLM_PROVIDER`, code использует `LLM_PROVIDER` и exits before advertised fallback. | Один env contract, migration warning и local/cloud tests. Это env contract change. |
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
| EVID-002 | EVIDENCE_PENDING | Pinned Cognee/Ollama add -> cognify -> graph query и incompatible-version failure. |
| EVID-003 | EVIDENCE_PENDING | QMD E2E отсутствует; пункт фактически retired, пока QMD удалён из продукта. |
| EVID-004 | EVIDENCE_PENDING | Versioned 500+ page long-term-memory dataset, fixed seed, approved thresholds и publishable report. Текущий benchmark явно `release_evidence: false`. |
| EVID-005 | EVIDENCE_PENDING | Successful remote Windows/Linux/macOS CI, привязанный к final exact SHA. |
| EVID-006 | EVIDENCE_PENDING | Disposable Windows/Ubuntu/macOS install -> capture -> compile -> maintenance -> update -> rollback -> uninstall. |
| EVID-007 | EVIDENCE_PENDING | Реальный Web Clipper import отсутствует; пункт retired, пока Obsidian viewer-only. |
| EVID-008 | EVIDENCE_PENDING | Real embedding model load, cache reuse/corruption rebuild и semantic-only recall на supported ОС. |
| EVID-009 | EVIDENCE_PENDING | Live repeated `/compact` и path-based PreCompact без loss/duplicate. |
| EVID-010 | EVIDENCE_PENDING | Inbox/raw source -> page -> sources/wikilinks -> index/log -> retrieval artifact. |
| EVID-011 | EVIDENCE_PENDING | No-mutation review fixture с orphan/stale/duplicate/missing-source/contradiction и prioritized report. |
| EVID-012 | EVIDENCE_PENDING | Successful-task workflow draft плюс rejection failed/duplicate cases. |
| EVID-013 | EVIDENCE_PENDING | Bridge promotion с reciprocal provenance и idempotent retry. |
| EVID-014 | EVIDENCE_PENDING | Реальный Task Scheduler/cron run, result/report/index и logout/sleep behavior. |
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
- Ruff по затронутым Python files: clean.
- `git diff --check`: clean, кроме информационного LF -> CRLF warning для
  существующего Windows worktree.

Remote CI, deployment, tag и release этим документом не подтверждаются.
