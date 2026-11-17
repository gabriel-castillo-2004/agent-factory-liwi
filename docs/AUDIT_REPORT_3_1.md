# AUDIT_REPORT_3_1 — Re-auditoría Independiente de Cierre de la Fase 3

Auditor independiente (en modo solo lectura). Rama `phase-3`, commit `24e08e0` / tag `phase-3.2-tests`, auditada contra `docs/AUDIT_REPORT_3.md` (`ac1601c`), tag `phase-3.1-tests` (`6c2612f`) y `main` (`89ba0bc`).  
Referencias leídas: `MASTER_PROMPT.md` (v1.2.1), `AGENTS.md`, `docs/decisions/ADR-003-telegram-and-registry-ports.md`, `docs/PHASE_REPORT_3.md`, `docs/AUDIT_REPORT_3.md`, `docs/PHASE3_TEST_MAP.md`, `docs/PHASE3_TEST_ISSUES.md`, `git diff main...HEAD`.

---

## Veredicto: **APTO CON CORRECCIONES** *(para el alcance offline implementado; la Fase 3 de `MASTER_PROMPT.md` §7 sigue incompleta sin Panel PWA ni despliegue real)*

### Resumen objetivo del dictamen
- **Se cierran las fallas críticas bloqueantes de `AUDIT_REPORT_3.md`:** la corrupción de máquina de estados `DELIVERED -> FAILED` (`A3-01`), la fuga de secretos en `BuildWorker` y `Orchestrator` (`A3-02`), la captura de `http.client.HTTPException` / `"text": null` / limpieza de `__context__` (`A3-07`), el aislamiento de rama `registry` (`A3-09`) y la transparencia numérica/documental de `PHASE_REPORT_3.md` quedan corregidos sin trampas de `INV-13`. De las 10 mutaciones antes supervivientes de `AUDIT_REPORT_3.md`, **9 mueren**.
- **Por qué NO es `APTO` limpio:**
  1. **`M19` SIGUE SOBREVIVIENDO (`305 passed`):** `docs/PHASE3_TEST_MAP.md:22` afirma erróneamente que `test_telegram_bot_rejects_mismatched_adjust_hash_and_survives_malformed_or_failed_poll_batch` mata **M19**. En realidad, mutar `telegram_bot.py:495` para permitir `/resume` fuera de `PAUSED_QUOTA` pasa los 305 tests porque `BuildWorker.run_job` lanza `IllegalTransition`, el `except` de `poll_once()` lo convierte silenciosamente en `action="error"`, y `tests/test_telegram_polling_and_acl.py:790` solo exige `batch_2[3].action != "resumed"`.
  2. **Tres brechas funcionales nuevas/residuales verificadas por sonda y mutación (`N07`/`N07b`, `N16`, Sondas P1–P4):**
     - `poll_once()` tiene `telegram_bot.py:589-600` con **0 % de cobertura** (`N16` sobrevive) y re-crashea si `self.client.send_message` lanza `TelegramTransportError` dentro del `except` (Sonda P4).
     - `TelegramBotService._handle_new` (`telegram_bot.py:428`) sobrescribe `active_project_by_chat` (y SQLite) **antes** de `handle_interview_message` (`:429`), contradiciendo su propio docstring (`:400`) cuando `Interviewer` falla (Sonda P2).
     - `GitRegistryPublisher._ensure_repository_ready` (`git_registry_publisher.py:135`, **0 % de cobertura**) ejecuta `git rebase FETCH_HEAD` ante commits divergentes en `registry.json`, falla por conflicto y deja `work_dir` **permanentemente atascado en rebase en curso** para llamadas futuras (Sonda P3).
     - `secrets.py` aún deja pasar query params `?token=...` y tokens bare `<digits>:<token>` sin prefijo `bot` cuando no hay `extra_secrets`/`env` (Sonda P1).
  3. **Alcance diferido de `MASTER_PROMPT.md:175-181`:** el **Panel PWA de solo lectura** y los **adaptadores reales de VCS/despliegue** siguen sin existir en el código (ahora documentados honestamente en `docs/PHASE_REPORT_3.md:72-79`).

---

## 1. Ejecución verificada, `INV-13` y auditoría de `docs/PHASE_REPORT_3.md`

| Verificación | Comando ejecutado (una vez) | Resultado real observado |
|---|---|---|
| **Linter** | `.\.venv\Scripts\ruff.exe check .` | `All checks passed!` (exit code `0`) |
| **Suite + cobertura** | `.\.venv\Scripts\pytest.exe -q --cov=src/agent_factory --cov-report=term-missing` | **`305 passed, 2 warnings in 41.86s`** (exit code `0`) · Cobertura global: **91 %** (`2271` stmts, `156` miss, `608` branch, `93` brpart). Warnings esperados de `pytest-socket` en `tests/test_bootstrap.py` y `tests/test_suite_offline.py`. |
| **`INV-13` en PASO B' (`86e5ff1`)** | `git diff 6c2612f..86e5ff1 --stat -- tests/ pyproject.toml templates/` | **Vacío** (`0` archivos tocados en `tests/`, `pyproject.toml` o `templates/`). |
| **`INV-13` en PASO A'' (`24e08e0`)** | `git diff phase-3.1-tests..HEAD -- tests/ pyproject.toml templates/` | **Exactamente 1 línea** en `tests/test_telegram_polling_and_acl.py:667` (`test_results={"AT-01": True}` -> `{"AT-01": True, "AT-02": True}`, `stdout="1 passed"` -> `"2 passed"`), exactamente el defecto documentado en `docs/PHASE3_TEST_ISSUES.md:19, 26`. Sin debilitar aserciones ni oráculos (`INV-23`). |
| **Marcadores `skip`/`xfail`** | `Select-String -Path tests/*.py -Pattern "skip\|xfail"` | **0** pruebas saltadas o marcadas `xfail` (solo docstrings descriptivos). |
| **Diff Fase 3 vs `main`** | `git diff main...HEAD --stat` | `21` archivos (`4415` inserciones, `170` borrados). `MASTER_PROMPT.md`, `AGENTS.md`, `templates/` y `pyproject.toml` sin cambios. |

### Auditoría de cifras en `docs/PHASE_REPORT_3.md`
Las cifras reportadas en `docs/PHASE_REPORT_3.md:44-68` coinciden **exactamente** con la ejecución real observada salvo una diferencia mínima y no engañosa de tiempo de reloj (`53.61s` en el reporte vs `41.86s` en esta re-auditoría):
- Global: `2271` stmts, `156` miss, `608` branch, `93` brpart -> **91 %** (coincide).
- Por módulo: `agy_cli_adapter.py` `99%`, `fake_telegram.py` `91%`, `git_registry_publisher.py` `85%`, `memory_store.py` `100%`, `sqlite_store.py` `97%`, `telegram_client.py` `87%`, `builder.py` `93%`, `domain/events.py` `100%`, `domain/models.py` `100%`, `domain/progress.py` `96%`, `domain/states.py` `100%`, `orchestrator.py` `90%`, `secrets.py` `93%`, `telegram_bot.py` `89%` (todos idénticos en `Stmts`, `Miss`, `Branch`, `BrPart` y `Cover`).

---

## 2. Tabla de cierre de hallazgos (`A3-01` a `A3-09`)

| ID | Estado | Evidencia exacta (`archivo:línea`) | Dictamen técnico |
|---|---|---|---|
| **A3-01** | **CERRADO** | `src/agent_factory/domain/events.py:27`; `src/agent_factory/builder.py:436-486`; `src/agent_factory/domain/progress.py:172`; `tests/test_registry_publisher.py:311-416`; `tests/test_phase3_e2e_offline.py:439-558` | Tras `PROJECT_DELIVERED`, el fallo de `registry_publisher` ya no emite `PROJECT_FAILED`: registra `EventType.REGISTRY_PUBLISH_FAILED` redactado, mantiene `JobState.COMPLETED` y preserva `ProjectState.DELIVERED` al reconstruir `EventStore`. Mata **M16**, **M17** y **N01**. |
| **A3-02** | **CERRADO** | `src/agent_factory/builder.py:111-142, 215, 241, 250, 262, 290, 323, 332, 355, 412, 415, 434, 465, 490`; `src/agent_factory/orchestrator.py:203, 234, 323, 334, 453`; `tests/test_secret_sanitization.py:296-476` | `_redact` y `_fail_job` sanean todas las ramas de error de `BuildWorker`; `Orchestrator` aplica `redact_secrets` en entrevista, debate y `record_state_call`. Mata **M13**, **N02** y **N03**. |
| **A3-03** | **PARCIAL** | **Cerrado:** `sqlite_store.py:35-49, 92-118`; `memory_store.py:15-32`; `telegram_bot.py:76-107, 287, 292-300`; `test_telegram_polling_and_acl.py:556-633`. **Abierto:** `telegram_bot.py:72, 183-199, 400, 428-432, 589-600` | Persistencia de `telegram_processed_updates` y `telegram_active_projects` ante reinicios funciona y mata **N04**/**N05**; validación de `plan_hash` en `adjust:` funciona y mata **N06**. **Pendiente:** (1) `next_offset` no se persiste en SQLite; (2) `_handle_new` (`:428`) sobrescribe `active_project_by_chat` antes de `handle_interview_message` contradiciendo su docstring (Sonda P2); (3) `poll_once` `except` (`:589-600`) tiene `0 %` cobertura (**N16** sobrevive) y falla si `send_message` lanza `TelegramTransportError` (Sonda P4); (4) **N07/N07b** sobreviven. |
| **A3-04** | **PARCIAL** | **Cerrado:** `src/agent_factory/secrets.py:27-68`; `tests/test_secret_sanitization.py:232-271`. **Abierto:** Sonda P1 sobre `secrets.py:34-68` | `bot<digits>:<token>`, `ghp_`/`github_pat_`/`sk-`, `Bearer`, userinfo URL sin cruzar `/` y `GITHUB_TOKEN=/VERCEL_TOKEN=/TELEGRAM_BOT_TOKEN=` están cubiertos con `env={}` y `extra_secrets=()`; URLs públicas con puerto y `@` no se corrompen; pruebas desacopladas matan **M21**, **M22**, **N08**, **N09**, **N10**. **Pendiente:** query params genéricos (`?token=SECRET_XYZ`) y `<digits>:<token>` sin prefijo `bot` no se redactan cuando `env={}` y `extra_secrets=()`. |
| **A3-05** | **PARCIAL** | **Cerrado (`M20`, `M25`, `M26`, `M28`):** `test_telegram_polling_and_acl.py:690-691, 734, 737-738, 784, 800, 857-860`. **Abierto (`M19`):** `telegram_bot.py:495`; `test_telegram_polling_and_acl.py:790` | 4 de 5 mutaciones mueren. **`M19` SIGUE VIVA (`305 passed`)** porque `test_telegram_polling_and_acl.py:790` solo afirma `batch_2[3].action != "resumed"` dentro de `poll_once()`, cuyo `except` oculta el `IllegalTransition` disparado en `BuildWorker.run_job`. |
| **A3-06** | **PARCIAL** | **Cerrado:** `git_registry_publisher.py:44-59, 132-133, 159-177`; `tests/test_registry_publisher.py:418-562`. **Abierto:** `git_registry_publisher.py:82, 135`; Sonda P3 | Bloquea `refs/heads/main|master`, `MAIN` y flags `-` (**N11** muere); reintenta `push` con `updated_at` determinista (**N12** muere); recupera con `reset --hard FETCH_HEAD` (**N13** muere); cubre CRUD y sube cobertura de `59 %` a `85 %`. **Pendiente:** línea `:135` (`git rebase FETCH_HEAD`) tiene `0 %` de cobertura y, ante commits divergentes que tocan `registry.json`, **bloquea permanentemente `work_dir` en mitad de rebase** (Sonda P3). `:82` (`_resolve_work_dir`, `hash()` no determinista) también tiene `0 %`. |
| **A3-07** | **CERRADO** | `src/agent_factory/adapters/telegram_client.py:72-83, 92-102, 147-162`; `tests/test_secret_sanitization.py:272-294`; `tests/test_telegram_polling_and_acl.py:803-864` | Captura `http.client.HTTPException`, lanza `TelegramTransportError` fuera de `except` con `__cause__=None` y `__context__=None`, y tolera `"text": null` y dicts nulos. Mata **N14**, **N15a**, **N15b**. (`UrllibHttpTransport` `:31-47` sigue al `0 %` offline). |
| **A3-08** | **PARCIAL** | `docs/PHASE_REPORT_3.md:50-79`; `src/agent_factory/telegram_bot.py:70` | `PHASE_REPORT_3.md` corrige métricas y declara el alcance diferido de Panel PWA y adaptadores VCS reales frente a `MASTER_PROMPT.md:175-181`. **Pendiente:** `TelegramBotService.registry_publisher` (`telegram_bot.py:70`) continúa como campo muerto nunca invocado. |
| **A3-09** | **CERRADO** | `tests/test_registry_publisher.py:490-497` | Se verifica con `git ls-tree -r --name-only refs/heads/registry` que `README.md` de `main` no existe en `registry`. |

---

## 3. Hallazgos nuevos y sondas directas verificadas

1. **H31-01 (Alto — Falsos negativos en tests nuevos de `TelegramBotService`: `M19`, `N07b`, `N16` sobreviven):**
   - `tests/test_telegram_polling_and_acl.py:790` (`!= "resumed"`) y `:793` (`!= "plan_ready_for_approval"`) permiten que **M19** (permitir `/resume` sobre job `COMPLETED`) y **N07b** (saltar las guardas `:183-199` antes de `run_debate`) sobrevivan porque ambas mutaciones provocan `IllegalTransition` río abajo (`BuildWorker.run_job` y `Orchestrator.run_debate`), el cual es atrapado y disfrazado de `action="error"`. A la vez, `poll_once` `except` (`telegram_bot.py:589-600`) tiene **0 % de cobertura** y **N16** sobrevive.
2. **H31-02 (Medio — Bug de atascamiento en `GitRegistryPublisher` ante rebase conflictivo, `git_registry_publisher.py:135`):**
   - Sonda directa P3: cuando `wt1` posee un commit local sin hacer push que modificó `registry.json` y `origin/registry` recibió otro commit que modificó `registry.json`, `_ensure_repository_ready` ejecuta `git rebase FETCH_HEAD` (`:135`), falla con `RegistryPublishError` por conflicto en `registry.json` y **deja `work_dir` bloqueado en rebase en curso** (`P3_AFTER_REBASE_CONFLICT: WEDGED -> RegistryPublishError Git command failed (checkout registry): error: you need to resolve your current index first`), impidiendo toda publicación futura en ese `work_dir`.
3. **H31-03 (Medio — `_handle_new` sobrescribe el proyecto activo en fallo de `Interviewer`, `telegram_bot.py:400, 428-432` y parsing por prefijo `:550-555`):**
   - Sonda P2: pese a que el docstring `:400` declara *"without overwriting active project on errors"*, `:428` registra `self._set_active_project(update.chat_id, pid)` antes de `handle_interview_message`, cambiando el proyecto activo del chat de `"good-app"` al proyecto roto `"bad-app"` (`FAILED`). Adicionalmente, `:550, 552, 554` usan `startswith` sin delimitador de espacio (`"/newsletter"` dispararía `_handle_new`).
4. **H31-04 (Medio — `poll_once` no aísla `TelegramTransportError` lanzado por `_send_reply`, `telegram_bot.py:589-599`):**
   - Sonda P4: `TelegramTransportError` hereda de `RuntimeError`; si `send_message` falla dentro de `handle_update`, `:595` lo captura y vuelve a llamar `self._error_outcome` (`:598`) -> `send_message`, abortando `poll_once()` con excepción no manejada tras avanzar `next_offset`.
5. **H31-05 (Bajo — Brechas residuales menores en `secrets.py` y código muerto en `telegram_bot.py:70`):**
   - Sonda P1: `?token=SECRET_XYZ` en URL y `<digits>:<token>` sin prefijo `"bot"` no son redactados cuando `extra_secrets=()` y `env={}`. `TelegramBotService.registry_publisher` (`:70`) sigue sin usarse.

---

## 4. Resultado de mutaciones ejecutadas en worktree temporal

Todas las mutaciones se ejecutaron en un `git worktree` separado fuera del workspace (`%TEMP%\af_audit3_1_wt`, commit `24e08e0`), verificando importación desde `%TEMP%\af_audit3_1_wt\src\agent_factory\__init__.py` y ejecutando `pytest` con `--disable-socket` (y fallback a la suite completa de 305 tests ante cualquier supervivencia). Al terminar, se eliminó y podó el worktree verificando con `git worktree list` y `git status --short` el workspace limpio.

### A. Mutaciones antes supervivientes de `AUDIT_REPORT_3.md` (`9/10` detectadas, `1` aún superviviente)

| ID | Archivo y punto mutado | Resultado | Evidencia real observada |
|---|---|---|---|
| **M00-sanity** | `telegram_bot.py:109` (`is_authorized` -> `True`) | **KILLED** | `1 failed, 1 passed in 0.42s` — `test_telegram_bot_enforces_strict_whitelist_on_chat_id_and_user_id` |
| **M13** | `telegram_bot.py:125` (`_send_reply` sin `redact_secrets`) | **KILLED** | `1 failed, 3 passed in 1.57s` — `test_secret_sanitization_across_build_worker_paths_orchestrator_and_bot_egress` |
| **M16** | `builder.py:469-476` (omitir evento `REGISTRY_PUBLISH_FAILED`) | **KILLED** | `1 failed, 2 passed in 10.23s` — `test_build_worker_registry_failure_preserves_delivered_state_without_illegal_transition` |
| **M17** | `builder.py:465-468` (anular `registry_warning = None`) | **KILLED** | `1 failed, 2 passed in 9.49s` — `test_build_worker_registry_failure_preserves_delivered_state_without_illegal_transition` |
| **M19** | `telegram_bot.py:495` (`if job is not None:` ignorando `PAUSED_QUOTA`) | **SURVIVED** | **`305 passed, 2 warnings in 34.97s`** — `poll_once` atrapa `IllegalTransition` de `BuildWorker.run_job` y `test_telegram_polling_and_acl.py:790` solo afirma `!= "resumed"`. |
| **M20** | `telegram_bot.py:271-272` (omitir `answer_callback_query`) | **KILLED** | `1 failed, 5 passed in 0.50s` — `test_telegram_bot_rejects_mismatched_adjust_hash_and_survives_malformed_or_failed_poll_batch` |
| **M21** | `secrets.py:27-68` (desactivar todas las regex de patrones) | **KILLED** | `1 failed, 2 passed in 0.36s` — `test_redact_secrets_patterns_env_only_non_corruption_and_http_error_chain` |
| **M22** | `secrets.py:71` (`effective_env = {} if env is None else env`) | **KILLED** | `1 failed, 2 passed in 0.73s` — `test_redact_secrets_patterns_env_only_non_corruption_and_http_error_chain` |
| **M25** | `telegram_client.py:104` (ignorar `not data.get("ok")` en HTTP 200) | **KILLED** | `1 failed, 6 passed in 0.50s` — `test_telegram_http_client_handles_null_text_ok_false_invalid_json_and_answer_callback` |
| **M26** | `telegram_bot.py:582` (pasar `offset=None` en `poll_once`) | **KILLED** | `1 failed, 5 passed in 0.50s` — `test_telegram_bot_rejects_mismatched_adjust_hash_and_survives_malformed_or_failed_poll_batch` |
| **M28** | `orchestrator.py:407-409` (omitir guarda `WAITING_APPROVAL` en `request_adjustment`) | **KILLED** | `1 failed, 5 passed in 0.48s` — `test_telegram_bot_rejects_mismatched_adjust_hash_and_survives_malformed_or_failed_poll_batch` |

### B. Mutaciones nuevas sobre las correcciones de PASO B' (`15/18` detectadas, `3` supervivientes)

| ID | Archivo y punto mutado | Descripción de la mutación | Resultado | Evidencia real observada |
|---|---|---|---|---|
| **N01** | `builder.py:472` | Emitir `EventType.PROJECT_FAILED` ilegal tras `PROJECT_DELIVERED` | **KILLED** | `1 failed, 2 passed in 8.18s` — `test_build_worker_registry_failure_preserves_delivered_state_without_illegal_transition` |
| **N02** | `builder.py:411-412` | Omitir `_fail_job`/`_redact` en error de `vcs.push_branch`/`get_ci_result` | **KILLED** | `1 failed, 3 passed in 0.67s` — `test_secret_sanitization_across_build_worker_paths_orchestrator_and_bot_egress` |
| **N03** | `orchestrator.py:234` | Omitir `redact_secrets` en `PROJECT_FAILED` tras `LLMError` en entrevista | **KILLED** | `1 failed, 3 passed in 0.33s` — `test_secret_sanitization_across_build_worker_paths_orchestrator_and_bot_egress` |
| **N04** | `telegram_bot.py:79-81` | Desactivar hidratación de `telegram_processed_updates` al reiniciar | **KILLED** | `1 failed, 4 passed in 0.53s` — `test_telegram_bot_persists_dedup_and_active_project_across_restart` |
| **N05** | `telegram_bot.py:83-85` | Desactivar hidratación de `telegram_active_projects` al reiniciar | **KILLED** | `1 failed, 4 passed in 0.41s` — `test_telegram_bot_persists_dedup_and_active_project_across_restart` |
| **N06** | `telegram_bot.py:292` | Verificar `plan_hash` solo en `approve:`, omitiéndolo en `adjust:` | **KILLED** | `1 failed, 5 passed in 0.59s` — `test_telegram_bot_rejects_mismatched_adjust_hash_and_survives_malformed_or_failed_poll_batch` |
| **N07** | `telegram_bot.py:183-190` | Omitir `if status.state == ProjectState.FAILED:` antes de `run_debate` | **SURVIVED** | **`305 passed`** — cae a la guarda `:192` (`!= INTERVIEW`) y `:793` solo comprueba `!= "plan_ready_for_approval"`. |
| **N07b** | `telegram_bot.py:183-199` | Omitir ambas guardas (`== FAILED` y `!= INTERVIEW`) antes de `run_debate` | **SURVIVED** | **`305 passed in 38.95s`** — `Orchestrator.run_debate` lanza `IllegalTransition` capturado en `:224`; `test_telegram_polling_and_acl.py:793` no distingue `"error"` de un rechazo limpio previo. |
| **N08** | `secrets.py:34-38` | Eliminar regex `\bbot\d+:[A-Za-z0-9_-]{8,}` | **KILLED** | `1 failed, 2 passed in 0.43s` — `test_redact_secrets_patterns_env_only_non_corruption_and_http_error_chain` |
| **N09** | `secrets.py:56-60` | Eliminar regex `ghp_` / `github_pat_` / `sk-` | **KILLED** | `1 failed, 2 passed in 0.38s` — `test_redact_secrets_patterns_env_only_non_corruption_and_http_error_chain` |
| **N10** | `secrets.py:42` | Revertir regex URL a versión codiciosa que cruza `/` (`[^@\s]+`) | **KILLED** | `1 failed, 2 passed in 0.70s` — `test_redact_secrets_patterns_env_only_non_corruption_and_http_error_chain` |
| **N11** | `git_registry_publisher.py:55-56` | Permitir `refs/heads/main`, `refs/heads/master` y variantes con `/` | **KILLED** | `1 failed, 3 passed in 9.69s` — `test_git_registry_publisher_retry_after_push_fail_rejects_refs_main_and_crud` |
| **N12** | `git_registry_publisher.py:159-177` | Volver a condicionar `git push` a `diff_check.exit_code != 0` | **KILLED** | `1 failed, 3 passed in 9.98s` — `test_git_registry_publisher_retry_after_push_fail_rejects_refs_main_and_crud` |
| **N13** | `git_registry_publisher.py:133` | Revertir `reset --hard FETCH_HEAD` a `merge --ff-only FETCH_HEAD` | **KILLED** | `1 failed, 3 passed in 16.66s` — `test_git_registry_publisher_retry_after_push_fail_rejects_refs_main_and_crud` |
| **N14** | `telegram_client.py:162` | Pasar `msg.get("text", "")` crudo (`None` cuando `"text": null`) | **KILLED** | `1 failed, 6 passed in 0.48s` — `test_telegram_http_client_handles_null_text_ok_false_invalid_json_and_answer_callback` |
| **N15a** | `telegram_client.py:76` | Quitar `http.client.HTTPException` del `except` de `_post` | **KILLED** | `1 failed, 2 passed in 0.44s` — `test_redact_secrets_patterns_env_only_non_corruption_and_http_error_chain` |
| **N15b** | `telegram_client.py:78-83` | Lanzar `TelegramTransportError(...) from None` dentro del `except` (conservando `__context__` con token) | **KILLED** | `1 failed, 2 passed in 0.51s` — `test_redact_secrets_patterns_env_only_non_corruption_and_http_error_chain` |
| **N16** | `telegram_bot.py:587-605` | Eliminar `try/except` exterior en `poll_once()` | **SURVIVED** | **`305 passed in 36.22s`** — líneas `589-600` tienen `0 %` de cobertura. |

*(Balance global sin `M00-sanity`: `24` detectadas / `28` = `85.7 %`, con `4` supervivientes: `M19`, `N07`, `N07b`, `N16`.)*

---

## 5. Lo que NO se pudo verificar y por qué

1. **Red real de Telegram (`UrllibHttpTransport`) y repositorios/despliegues reales de GitHub/Vercel:**
   - La suite bloquea toda red mediante `pytest-socket` (`--disable-socket`, `INV-03`) y no existen credenciales reales en el repositorio (`INV-07`). En cobertura real, `UrllibHttpTransport.post_json` (`telegram_client.py:31-47`) sigue al **0 % de ejecución** incluso offline.
2. **Panel PWA de solo lectura y adaptador VCS/Pages productivo (`MASTER_PROMPT.md:175-181`):**
   - No existen archivos HTML/CSS/JS del Panel PWA ni implementación real de GitHub API/Vercel en esta rama, por lo que no hay artefactos auditables ni pruebas end-to-end contra dicho panel.
3. **Resistencia de `GitRegistryPublisher` con `work_dir=None` entre múltiples procesos Python:**
   - `_resolve_work_dir()` (`git_registry_publisher.py:82`, `0 %` cobertura) usa `hash(str(self.repo_path))`, cuyo seed cambia entre procesos; no se ejecutó una prueba multi-proceso con `work_dir=None`.
