# AUDIT_REPORT_4_1 — Re-auditoría Independiente de Cierre de la Fase 4

Auditor independiente (modo solo lectura). Rama `phase-4`, commit `52278d2` auditado contra el tag `phase-4.1-tests` (`35ec849`), `docs/AUDIT_REPORT_4.md` (`6a8f830`), `docs/PHASE4_TEST_MAP.md`, `docs/PHASE_REPORT_4.md` y `main` (`ada4589`).  
Documentos y artefactos revisados: `MASTER_PROMPT.md` (v1.2.1), `AGENTS.md`, `docs/decisions/ADR-004-pwa-panel-and-cli-entrypoint.md`, `docs/PHASE_REPORT_4.md`, `docs/AUDIT_REPORT_4.md`, `docs/AUDIT_REPORT_3_1.md`, `docs/PHASE4_TEST_MAP.md`, `git diff main...HEAD`, `git diff phase-4.1-tests --stat`, `.env.example`, `config/`, `panel/`, `src/agent_factory/` y `tests/`.

---

## Veredicto: **NO APTO**

### Dictamen objetivo y sin concesiones
El endurecimiento del commit `52278d2` logra avances verificables importantes frente a `AUDIT_REPORT_4.md`:
- **Las 7 mutaciones antes supervivientes (`M4-03a`, `M4-15`, `M4-19`, `M4-20`, `M4-21a`, `M4-22`, `M4-23`) ahora mueren (`7/7`).**
- **Se cierran completamente `H4-04`, `H4-05`, `H4-07` y `H4-08`:** la suite ejecuta `panel/app.js` contra payloads XSS reales, sanitiza `registry.json` y `projects/<id>.json` antes de `json.dumps`, cubre rutas de error/JSON de `GitHubVCSAdapter` (`95 %`) y `StaticDeployPublisher` (`98 %`), bloquea `//evil.com`/credenciales en enlaces y renderiza `project_prompt` y `build_report` escapados en el panel PWA.
- **Suite y linter en verde offline:** `ruff check .` limpio, `pytest` pasa **`324 passed, 0 failed`** con `92 %` de cobertura global e `INV-13` intacto respecto a `phase-4.1-tests`.

**Sin embargo, el cierre operativo de la Fase 4 sigue siendo `NO APTO` por 4 fallas graves que siguen abiertas en el código productivo:**
1. **`H4-01` sigue roto en producción — `ProductionLLMAdapter` (`src/agent_factory/cli.py:49-76, 277-282`) es un stub hueco inoperante (`0 %` de cobertura):** en lugar de `FakeLLM()`, `build_runtime(config)` cablea `ProductionLLMAdapter(api_key=..., models_config_path=..., transport=transport)`, pasando **`transport=None` (`:280`)** en `main()`. `ProductionLLMAdapter.complete()` (`:68-76`) nunca lee los modelos de `config/models.toml`, nunca envía peticiones HTTP ni `messages`/`api_key` a ningún `HttpTransport`: si `transport is None` lanza `LLMError("No HttpTransport configured for ProductionLLMAdapter")` convirtiendo **toda entrevista real (`/new`) en `ProjectState.FAILED`** (`PROBE_LLM_INTERVIEW_RESULT`), y aun si se inyecta un `HttpTransport` devuelve `{}` con `0` llamadas (`PROBE_LLM_WITH_TRANSPORT`). Mutar `complete()` a `return {"fake_hollow": True}` (**`M4-N11`**) pasa los **324 tests**. Además, si `build_runtime()` falla en `:257-275`, deja abierto sin cerrar el descriptor `SqliteEventStore` abierto en `:247` (`PROBE_STORE_LEAK: LEAKED_OPEN_FD PermissionError`), `.env.example:2` sigue documentando `TELEGRAM_ALLOWED_USER_ID=` en lugar de `ALLOWED_CHAT_IDS`/`ALLOWED_USER_IDS`, y `__main__.py:3-8` sigue con `0 %` de cobertura en `pytest-cov`.
2. **`H4-02` sigue incompleto en despliegue real — código muerto en `GitHubVCSAdapter` y `StaticDeployPublisher`:** `GitHubVCSAdapter` guarda `self.deploy_publisher` (`github_vcs_adapter.py:89`), pero `merge_and_deploy` (`:226-261`) **jamás lo invoca** ni utiliza `deploy_publisher.deploy_base_url` (`PROBE_VCS_MERGE_WITH_VERCEL_PUB`); y `StaticDeployPublisher.deploy_site` (`static_deploy_publisher.py:84, 226`) ignora `self.target` (`"vercel"`) ejecutando siempre el subcomando inexistente `gh pages deploy` (`PROBE_DEPLOY_TARGET_VERCEL_CMD`).
3. **`H4-06` y `H4-09` dejan fuga viva de `TELEGRAM_BOT_TOKEN` en `err.__context__` dentro de `cli.py` y sleep bloqueante:** `cli.py:140, 179, 200` siguen ejecutando `raise CliConfigError(clean_err) from None` **dentro** de bloques `except (ValueError, CliConfigError)`, reteniendo el token crudo sin redactar en `err.__context__` (`PROBE_CLI_CONTEXT: tok_in_ctx= True` para `ALLOWED_CHAT_IDS`, `AGENT_FACTORY_POLL_TIMEOUT` y `AGENT_FACTORY_BACKOFF_INITIAL`). Asimismo, `run_polling_loop` (`cli.py:428`) sigue usando `time.sleep(delay)` en vez de `event.wait(delay)`, bloqueando `SIGINT`/`SIGTERM` hasta `60 s`.
4. **`H4-10`, `H4-11` y `H4-12` conservan brechas sin cerrar:** `GitRegistryPublisher.publish_project` (`git_registry_publisher.py:245-280`) escribe `name`, `objective`, `failure_reason` y `links` sin `redact_secrets` a la rama `registry` (`PROBE_REG_LEAKS_*: True`) y no implementa el parámetro opcional `events` de `ADR-004:25`; `TelegramBotService.registry_publisher` (`telegram_bot.py:75`) sigue como campo muerto; y `docs/PHASE_REPORT_4.md:121-143` continúa omitiendo selectivamente a `__main__.py` (`0 %`) y `subprocess_runner.py` (`57 %`) en su tabla de cobertura por módulo.

---

## 1. Tabla de Cierre de Hallazgos (`H4-01` a `H4-12`)

| ID | Severidad | Estado | Evidencia exacta (`archivo:línea`) | Dictamen técnico |
|---|---|---|---|---|
| **H4-01** | **crítico** | **PARCIAL** | **Cerrado parcial:** `src/agent_factory/cli.py:202-203, 254-284, 304-322, 440-468`. **Abierto:** `src/agent_factory/cli.py:49-76, 247, 280`; `.env.example:1-4`; `src/agent_factory/__main__.py:3-8` | Se eliminó el fallback a `FakeLLM()`, `load_cli_config` lee `AGENT_FACTORY_MODELS_CONFIG`/`LIMITS_CONFIG`, `build_runtime` exige `LLM_API_KEY`, rechaza `CONFIGURE_ME`, conecta `CircuitBreaker(config_path=...)` y `StaticDeployPublisher`, y `main()` cierra `runtime.store.close()` en `finally`. **Falla bloqueante:** `ProductionLLMAdapter.complete()` (`:68-76`, `0 %` cov) es un stub hueco (`transport=None` en `:280` -> `FAILED` en el primer `/new`; con `transport` devuelve `{}` y `0` llamadas; `M4-N11` **SURVIVED**). `build_runtime` filtra FD de `SqliteEventStore` (`:247`) si falla después; `.env.example:2` sigue mal; `__main__.py` sigue al `0 %`. |
| **H4-02** | **crítico** | **PARCIAL** | **Cerrado parcial:** `src/agent_factory/adapters/github_vcs_adapter.py:160-224, 234-237`; `tests/test_github_vcs_and_deploy_adapter.py:157-237`. **Abierto:** `github_vcs_adapter.py:89, 132, 226-261`; `static_deploy_publisher.py:84, 226` | `get_ci_result` envía `--repo owner/project_id`, usa `gh run list --limit 1 --json status,conclusion`, soporta JSON array sin falsos positivos y usa `clean_env` en todas las llamadas (`M4-22` KILLED); `merge_and_deploy` pasa `--repo owner/project_id`. **Pendiente:** `self.deploy_publisher` nunca se invoca en `merge_and_deploy` (`PROBE_VCS_MERGE_WITH_VERCEL_PUB`), `StaticDeployPublisher.target` (`"vercel"`) sigue sin usarse (`gh pages deploy` fijo en `:226`, `PROBE_DEPLOY_TARGET_VERCEL_CMD`), y `push_branch` (`:132`) no ejecuta `git add/commit`. |
| **H4-03** | **alto** | **PARCIAL** | **Cerrado parcial:** `src/agent_factory/adapters/static_deploy_publisher.py:209-223`; `tests/test_github_vcs_and_deploy_adapter.py:251-257`. **Abierto:** `static_deploy_publisher.py:221` | `deploy_site` resuelve `source_dir.resolve()` y rechaza `""`, prefijo `-` (`--exec=evil`, `-force`), espacios, `..` y `main`/`master`/`refs/heads/main`/`master` antes de llamar a `Runner` (`M4-N03` KILLED). **Brecha menor:** la validación no normaliza `.lower()` ni bloquea `:`, `\` ni sufijos `/main` (`/master`), aceptando `MAIN`, `origin/main`, `feat:bad` y `bad\branch` (`PROBE_DEPLOY_BRANCH_ACCEPTED`). |
| **H4-04** | **alto** | **CERRADO** | `tests/test_panel_pwa.py:199-358`; `panel/app.js:15-26` | Ejecución real de `panel/app.js` vía `deno eval --no-remote` / `node -e` verificando vectores XSS reales, `isValidProjectId`, `parseRegistryCatalog` y `sanitizeLinkUrl`. Mata **`M4-19`** y **`M4-20`**. |
| **H4-05** | **alto** | **CERRADO** | `src/agent_factory/adapters/static_deploy_publisher.py:50-58, 145, 182, 195`; `src/agent_factory/secrets.py:28`; `tests/test_github_vcs_and_deploy_adapter.py:259-278`; `tests/test_panel_pwa.py:361-379` | `_sanitize_data` redacta recursivamente diccionarios Python antes de `json.dumps` tanto en `build_sanitized_project_detail` como en `export_panel_bundle` (`registry.json`, `M4-N04c` KILLED). `secrets.py:28` excluye comillas `\"'`, pero como nota de cobertura **`M4-N09` sobrevive** porque el test pasa por `_sanitize_data` por campo y no prueba `redact_secrets` directo sobre una URL Telegram sin `/` dentro de comillas JSON. |
| **H4-06** | **alto** | **PARCIAL** | **Cerrado en adaptadores:** `github_vcs_adapter.py:85-88, 144-148, 187-191, 249-253`; `static_deploy_publisher.py:88-91, 237-241`; `git_registry_publisher.py:78-82`. **Abierto en CLI:** `src/agent_factory/cli.py:135-140, 174-179, 195-200, 268-269` | Los 3 adaptadores auto-incluyen `github_token`/`deploy_token` en `self.secrets` (`M4-N05c` KILLED) y limpian `__cause__` y `__context__` lanzando fuera del `except` (`M4-N05a`, `M4-N05d` KILLED). **Abierto:** `cli.py:140, 179, 200` siguen usando `raise CliConfigError(...) from None` dentro del `except`, conservando el secreto crudo `TELEGRAM_BOT_TOKEN` dentro de `err.__context__` (`PROBE_CLI_CONTEXT: tok_in_ctx= True`). |
| **H4-07** | **alto** | **CERRADO** | `src/agent_factory/adapters/github_vcs_adapter.py:152-261`; `tests/test_github_vcs_and_deploy_adapter.py:157-237, 298-310` | Cubiertas rutas positivas, negativas y errores/timeouts de `get_ci_result` (`95 %` cov en `github_vcs_adapter.py`) y `merge_and_deploy`, además de errores de `deploy_site` (`98 %` cov, `0` sentencias sin cubrir en `static_deploy_publisher.py`). Mata **`M4-22`** y **`M4-23`**. |
| **H4-08** | **alto** | **CERRADO** | `panel/app.js:40-63, 381-386, 413-414`; `panel/index.html:37`; `tests/test_panel_pwa.py:339-358` | `renderDetail` renderiza `PROJECT_PROMPT.md` y `build_report.failure_cause` escapados con `escapeHtml` (`M4-N07` KILLED); `sanitizeLinkUrl` exige `^https?://` y rechaza `//evil.com`, `../rel` y credenciales (`M4-N06` KILLED); `index.html:37` usa `type="text"` aceptando `.` y `./`. *(Solo queda deuda cosmética en `panel/styles.css:132` al mantener `input[type="url"]` sin selector `type="text"`, ni estilos para `.alert-warning`/`.prompt-box`).* |
| **H4-09** | **medio** | **PARCIAL** | **Cerrado parcial:** `src/agent_factory/cli.py:174-200, 440-468`. **Abierto:** `src/agent_factory/cli.py:140, 179, 200, 247, 428` | `load_cli_config` exige `math.isfinite()` (`M4-N02` KILLED) y enmascara `str(err)` en timeout/backoff y whitelists (`M4-03a` KILLED); `main()` cierra `runtime.store.close()` en éxito. **Pendiente:** (1) `run_polling_loop` (`:428`) sigue bloqueando con `time.sleep(delay)` en vez de `event.wait(delay)`; (2) `err.__context__` con token en `:140, 179, 200`; (3) leak de `store` si `build_runtime` falla tras `:247`. |
| **H4-10** | **medio** | **PARCIAL** | **Cerrado parcial:** `git_registry_publisher.py:78-82, 175-178`; `tests/test_phase4_audit31_fixes.py:247-330`. **Abierto:** `git_registry_publisher.py:88-90, 120, 236-285` | La recuperación de rebase preserva otros proyectos offline (`proj-three` al publicar `proj-four`, **`M4-15` KILLED**), valida `isinstance(projs, dict)` y limpia `__context__`. **Pendiente:** (1) `publish_project` (`:245-280`) no aplica `redact_secrets` sobre `registry.json` ni `projects/<id>.json` (`PROBE_REG_LEAKS_*: True`); (2) `:120` falla en `git checkout branch` si un crash previo dejó rebase huérfano; (3) no soporta parámetro opcional `events` de `ADR-004:25`. |
| **H4-11** | **medio** | **PARCIAL** | **Cerrado parcial:** `src/agent_factory/telegram_bot.py:502-509, 613-626`; `tests/test_phase4_audit31_fixes.py:332-468`. **Abierto:** `src/agent_factory/telegram_bot.py:75`; `tests/test_telegram_polling_and_acl.py:793` | `/resume` en jobs `COMPLETED` responde `action="resume_rejected"` sin tocar `BuildWorker` (**`M4-21a`/`M19` KILLED**); `poll_once` aísla fallos de `send_message` en `_error_outcome` redactando la respuesta (**`M4-N08` KILLED**, cobertura `91 %`). **Pendiente:** `TelegramBotService.registry_publisher` (`:75`) sigue sin invocarse nunca en toda la clase (`MASTER_PROMPT.md:180`), y persiste la debilidad de aserción en `test_telegram_polling_and_acl.py:793` (`!= "plan_ready_for_approval"`). |
| **H4-12** | **bajo** | **ABIERTO** | `docs/PHASE_REPORT_4.md:121-143`; `src/agent_factory/secrets.py:28, 34-38, 49, 72`; `panel/app.js:215-221, 230-231`; `panel/styles.css:132` | (1) `PHASE_REPORT_4.md:121-143` sigue omitiendo `__main__.py` (`0 %`) y `subprocess_runner.py` (`57 %`), además de `ports/*`, `fake_*`, `agents/*`, `file_publisher.py` y `system_clock.py` de su tabla por módulo mientras lista la fila `TOTAL` global (`92 %`). (2) `secrets.py` con `env={}` no redacta `<digits>:<token>` bare, `LLM_API_KEY=` ni `?key=` (`PROBE_SECRET_*`). (3) `panel/app.js:215-221` no valida `Number.isFinite` y `panel/styles.css:132` quedó con `input[type="url"]`. |

---

## 2. Hallazgos Nuevos / Consolidados en Esta Re-auditoría (`H41-01` a `H41-04`)

| ID | Severidad | Evidencia (`archivo:línea` + sonda) | Descripción del problema |
|---|---|---|---|
| **H41-01** | **crítico** | `src/agent_factory/cli.py:49-76, 277-282` + `M4-N11` + `PROBE_LLM_NO_TRANSPORT` + `PROBE_LLM_WITH_TRANSPORT` | **`ProductionLLMAdapter` es un placeholder no funcional (`0 %` de cobertura):** `build_runtime(cfg)` instancia `ProductionLLMAdapter(..., transport=transport)` donde `transport=None` desde `main()` (`:280, 453`). Su método `complete()` (`:68-76`) nunca invoca ningún `HttpTransport`, ni consulta el modelo por estado de `models.toml`, ni envía `api_key`/`messages`: falla con `LLMError("No HttpTransport configured for ProductionLLMAdapter")` en toda entrevista real, y si se le pasa `transport` retorna `{}` sin ejecutar ninguna llamada HTTP (`transport_calls=0`). |
| **H41-02** | **alto** | `src/agent_factory/cli.py:247, 257-275` + `PROBE_STORE_LEAK` | **Fuga de descriptor `SqliteEventStore` ante fallo en `build_runtime()`:** `store = SqliteEventStore(db_path=config.db_path)` se abre en `:247` antes de validar `LLM_API_KEY` (`:257`) o `models.toml` (`:260-275`). Al lanzar `CliConfigError`, la conexión SQLite no se cierra, bloqueando el archivo `.db` (`PermissionError: [WinError 32]`). |
| **H41-03** | **alto** | `src/agent_factory/cli.py:135-140, 174-179, 195-200` + `PROBE_CLI_CONTEXT` | **Retención del token en `err.__context__` en `load_cli_config`:** `_parse_id_whitelist` y las validaciones numéricas usan `raise CliConfigError(clean_err) from None` dentro del `except ValueError:`, conservando `ValueError("...bad_<TOKEN>")` sin redactar en `err.__context__`. |
| **H41-04** | **bajo** | `panel/styles.css:132`; `panel/app.js:381-386` | **Regresión visual tras `H4-04`/`H4-08`:** `panel/index.html:37` cambió `#registry-base-input` a `type="text"`, pero `panel/styles.css:132` conserva únicamente `input[type="url"]` y carece de reglas para las nuevas clases `.alert-warning` y `.prompt-box` añadidas en `panel/app.js:382-385`. |

---

## 3. Verificación de `INV-13`, `ruff`, `pytest` y Auditoría de `docs/PHASE_REPORT_4.md`

### Comandos reales ejecutados (1 corrida cada uno en workspace limpio)
- **Integridad `INV-13` (`git diff phase-4.1-tests --stat -- tests/ templates/ pyproject.toml MASTER_PROMPT.md AGENTS.md`):** salida **vacía**, `DIFF_EXIT=0` (`0` líneas tocadas en tests, templates, `pyproject.toml`, `MASTER_PROMPT.md` o `AGENTS.md`).
- **Linter (`.\.venv\Scripts\ruff.exe check .`):** `All checks passed!` (exit code `0`).
- **Marcadores `skip`/`xfail` en `tests/*.py`:** `0` ocurrencias.
- **Suite completa + cobertura (`.\.venv\Scripts\pytest.exe -q --cov=src/agent_factory --cov-report=term-missing`):**
  - Resultado real: **`324 passed, 3 warnings in 99.59s (0:01:39)`** (exit code `0`).
  - Cobertura global (`src/agent_factory`): **`92 %`** (`2778` stmts, `174` miss, `736` branch, `104` brpart).

### Comparativa exacta contra `docs/PHASE_REPORT_4.md`
- **Cifras coincidentes:** El conteo de tests (`324 passed`), el `TOTAL` global (`2778` stmts, `174` miss, `736` branch, `104` brpart -> `92 %`) y todos los valores de los `20` módulos listados en `docs/PHASE_REPORT_4.md:121-143` coinciden exactamente con la ejecución real observada (salvo la variación normal de reloj `103.69s` vs `99.59s`).
- **Omisión de transparencia (`H4-12` abierto):** La tabla `Desglose de Cobertura por Módulo` (`PHASE_REPORT_4.md:121-143`) continúa omitiendo a **`src/agent_factory/__main__.py` (`0 %`, líneas `3-8`)**, **`src/agent_factory/adapters/subprocess_runner.py` (`57 %`, `40` stmts, `17` miss)**, `adapters/fake_*.py` (`74 %`–`91 %`), `adapters/file_publisher.py` (`92 %`), `adapters/system_clock.py` (`100 %`), `agents/*` (`96 %`–`100 %`) y `ports/*.py` (`73 %`–`93 %`).

### Líneas críticas sin cobertura observadas en Fase 4
- `src/agent_factory/__main__.py` (**`0 %`**): `3-8`.
- `src/agent_factory/cli.py` (**`93 %`**): `65`, **`74-76` (`ProductionLLMAdapter.complete`)**, `268-269` (error TOML), `305->313`, `394->exit`, `423->425`, **`428` (`time.sleep(delay)` real)**, `454-457` (`CliConfigError` de `build_runtime` en `main()`).
- `src/agent_factory/adapters/github_vcs_adapter.py` (**`95 %`**): `100->103`, `129`, `198`, `204->220`, `242-243`.
- `src/agent_factory/adapters/static_deploy_publisher.py` (**`98 %`**): `102->105`, `160->163`.
- `src/agent_factory/adapters/git_registry_publisher.py` (**`87 %`**): `115-118`, `151->159`, `157-158`, `160->exit`, `168->175`, `171->175`, `173-174`, `177-178`, `187-188`, `262->267`, `264-265`, `268`, `271->273`, `297-298`, `308-309`, `324-325`, `344->358`, `347-348`, `349->358`, `359->362`.
- `src/agent_factory/telegram_bot.py` (**`91 %`**): `84->88`, `88->92`, `97->93`, `104->exit`, `111->exit`, `164`, `198`, `229-230`, `238-239`, `276->279`, `285`, `312-313`, `365-372`, `428-429`, `482`, `491-492`, `500->532`, `518->520`, `520->522`, `575`, **`628`** (`poll_once` ruta no autorizada).

---

## 4. Cierre Global de Invariantes (`INV-01` a `INV-23`)

| Invariante | Estado | Evidencia / Justificación estricta |
|---|:---:|---|
| **INV-01** | **no cumple** | `cli.py:68-76` (`ProductionLLMAdapter.complete`) y `__main__.py:3-8` siguen al `0 %` de cobertura (`M4-N11` sobrevive 324/324); tampoco hay tests de ejecución real para `sw.js`. *(Nota: `panel/app.js` ahora sí tiene tests ejecutables vía `deno`/`node`).* |
| **INV-02** | **no cumple (en producción LLM)** | Aunque el dominio y puertos mantienen aislamiento, en producción `ProductionLLMAdapter` (`cli.py:280`) se instancia con `transport=None` y nunca ejecuta `HttpTransport.post_json` (`H41-01`). Los adaptadores VCS/Deploy/Telegram sí operan detrás de `Runner`/`HttpTransport`. |
| **INV-03** | **cumple** | `pyproject.toml:33` (`--disable-socket`), `324 passed` sin red ni secretos reales. |
| **INV-04** | **cumple** | `domain/states.py:30-67`, `domain/progress.py:72, 87, 102`, `orchestrator.py` y `telegram_bot.py:502-509` validan estado de origen y `validate_transition`. |
| **INV-05** | **cumple** | `orchestrator.py:360-397` y `domain/progress.py:83-86, 114-129` bloquean `BUILDING` sin `ApprovalEvent(approved)` con `plan_hash` vigente en `WAITING_APPROVAL`. |
| **INV-06** | **cumple** | Topes en `CircuitBreaker` (ahora cableado con `limits_config_path` en `cli.py:284`), `max_ci_rounds` (`builder.py:107`), `mediator.py:98`, backoff acotado (`cli.py:422`) y kill de árboles de procesos (`subprocess_runner.py:53-92`). |
| **INV-07** | **no cumple** | No hay credenciales reales en el repo y `.env` está en `.gitignore:20`, pero persisten fugas de secretos en tiempo de ejecución: `cli.py:140, 179, 200` retiene el token en `err.__context__` (`PROBE_CLI_CONTEXT`), `GitRegistryPublisher.publish_project` escribe secretos sin redactar en `registry` (`PROBE_REG_LEAKS_*`), y `.env.example:2` sigue incorrecto. |
| **INV-08** | **cumple** | `pyproject.toml:11-22` intacto, dependencias fijadas, `0` dependencias nuevas. |
| **INV-09** | **cumple** | Manejo de errores LLM/cuota verificado en `orchestrator.py:223-238, 310-338`, `mediator.py:98-113` y `builder.py:225-241, 307-323`. |
| **INV-10** | **cumple** | Cierre formal con `docs/PHASE_REPORT_4.md`. |
| **INV-11** | **cumple** | `SqliteEventStore` / `MemoryEventStore` append-only y proyección determinista `project_status_from_events` (`domain/progress.py:40-187`). |
| **INV-12** | **cumple** | `calculate_progress` (`domain/progress.py:22-37`) calcula el `%` de forma determinista. |
| **INV-13** | **cumple** | Verificado en `workspace.py:117-205`, `templates/static-web/playwright.config.mjs:2-21` y en este repo mediante `git diff phase-4.1-tests --stat` vacío. |
| **INV-14** | **no verificable** | `CHANGE_REQUEST` declarado en `domain/states.py:18, 51-52`, lógica diferida a Fase 5 (`MASTER_PROMPT.md:72, 186-191`). |
| **INV-15** | **cumple** | Iteración local y tope de `max_ci_rounds=3` en `builder.py:385-414, 489-494`. |
| **INV-16** | **cumple** | Persistencia de `telegram_processed_updates` y deduplicación previa a cualquier LLM en `orchestrator.py:185-195` y `telegram_bot.py:84-105, 551-559`. |
| **INV-17** | **cumple** | `cli.py:284-290` ahora pasa `config.limits_config_path` al `CircuitBreaker` que envuelve todas las llamadas del orquestador y `BuildWorker`. |
| **INV-18** | **cumple** | Aislamiento en `workspaces/<project_id>/` (`workspace.py`, `agy_cli_adapter.py:71-90`) y `deploy_site` resuelve `source_dir.resolve()`. |
| **INV-19** | **no cumple** | Aunque `agy`, `GitHubVCSAdapter`, `StaticDeployPublisher` y `TelegramBotService` sanean entornos y errores propios, **`cli.py:140, 179, 200` filtra `TELEGRAM_BOT_TOKEN` en `err.__context__`**, **`GitRegistryPublisher.publish_project` no redacta datos antes de commitear a `registry`**, y **`ProductionLLMAdapter` es un stub sin implementación real**. *(Ejecución viva con `agy`/APIs reales además `no verificable` offline).* |
| **INV-20** | **cumple** | `templates/static-web/` y `TemplateScaffold` verificados. |
| **INV-21** | **cumple** | `GitRegistryPublisher` aísla rama `registry` (`git_registry_publisher.py:44-59`) y `cli.py` falla cerrado ante whitelists/tokens faltantes o `NaN`/`Inf`. |
| **INV-22** | **parcial** | `validate_project_id` cubre adaptadores, CLI y `panel/app.js`; `deploy_site` bloquea `-...`, espacios, `..` y `main`/`master` minúsculas, pero aún deja pasar `MAIN`, `origin/main`, `:` y `\` (`PROBE_DEPLOY_BRANCH_ACCEPTED`). |
| **INV-23** | **parcial** | Las pruebas nuevas tienen oráculos independientes escritos a mano (`7/7` mutaciones previas mueren), pero `M4-N11` (`ProductionLLMAdapter`) y `M4-N09` (`secrets.py:28`) sobreviven a los 324 tests. |

---

## 5. Resultado de Mutaciones y Sondas en Worktree Temporal

Todas las pruebas se ejecutaron en un `git worktree` temporal separado (`%TEMP%\af_audit4_1_wt`, commit `52278d2`), verificando `IMPORTED_FROM: ...\af_audit4_1_wt\src\agent_factory\__init__.py` y ejecutando tanto Python como `tests/test_panel_pwa.py` desde dentro del worktree para mutar también `panel/app.js`. Al finalizar se ejecutó `git worktree remove --force` + `git worktree prune`, comprobando `git worktree list` con único worktree y `git status --short` vacío.

### A. Repetición de las 7 mutaciones antes supervivientes de `AUDIT_REPORT_4.md` (`7/7 KILLED`)

| ID | Archivo y punto mutado | Resultado | Evidencia real observada |
|---|---|---|---|
| **M4-03a** | `cli.py:136-140` (omitir `redact_secrets` en `_parse_id_whitelist` al fallar `int(part)`) | **KILLED** | `1 failed, 3 passed in 1.56s` — `test_cli_entrypoint.py` |
| **M4-15** | `git_registry_publisher.py:149-192` (dejar `rebase --abort` + `reset --hard FETCH_HEAD` sin restaurar `projects/*.json` locales) | **KILLED** | `1 failed, 4 passed in 31.81s` — `test_phase4_audit31_fixes.py` |
| **M4-19** | `panel/app.js:19-26` (`escapeHtml` devuelve string sin escapar) | **KILLED** | `1 failed, 3 passed, 1 warning in 1.73s` — `test_panel_pwa.py` |
| **M4-20** | `panel/app.js:15-17` (`isValidProjectId` devuelve siempre `true`) | **KILLED** | `1 failed, 3 passed, 1 warning in 0.78s` — `test_panel_pwa.py` |
| **M4-21a** | `telegram_bot.py:502-510` (quitar guarda `COMPLETED` y `PAUSED_QUOTA` en `_handle_resume`) | **KILLED** | `1 failed, 4 passed in 30.92s` — `test_phase4_audit31_fixes.py` |
| **M4-22** | `github_vcs_adapter.py:179` (pasar `env=dict(os.environ)` en `get_ci_result`) | **KILLED** | `1 failed, 3 passed in 0.58s` — `test_github_vcs_and_deploy_adapter.py` |
| **M4-23** | `github_vcs_adapter.py:181-185` (omitir `redact_secrets` en fallos de `get_ci_result`) | **KILLED** | `1 failed, 3 passed in 0.45s` — `test_github_vcs_and_deploy_adapter.py` |

### B. Mutaciones sobre las correcciones nuevas de PASO B' (`11` no equivalentes: `9` detectadas / `2` supervivientes + `3` redundantes/equivalentes)

| ID | Archivo y punto mutado | Descripción de la mutación | Resultado | Evidencia real observada |
|---|---|---|---|---|
| **M4-N01** | `cli.py:254-258` | Restaurar `FakeLLM()` en `build_runtime` cuando `llm is None` | **KILLED** | `1 failed, 3 passed in 0.83s` — `test_cli_entrypoint.py` |
| **M4-N02** | `cli.py:186-189` | Omitir `math.isfinite()` en backoff dentro de `load_cli_config` | **KILLED** | `1 failed, 3 passed in 1.25s` — `test_cli_entrypoint.py` |
| **M4-N03** | `static_deploy_publisher.py:216-223` | Eliminar validación de `branch` (`-...`, `main`/`master`) en `deploy_site` | **KILLED** | `1 failed, 3 passed in 0.38s` — `test_github_vcs_and_deploy_adapter.py` |
| **M4-N04c** | `static_deploy_publisher.py:182, 195` | Eliminar ambas llamadas `_sanitize_data` sobre `registry.json` en `export_panel_bundle` | **KILLED** | `1 failed, 3 passed in 0.43s` — `test_github_vcs_and_deploy_adapter.py` |
| **M4-N05a** | `github_vcs_adapter.py:137-148` | Relanzar `raise VcsError(...) from None` dentro del `except` de `push_branch` | **KILLED** | `1 failed, 3 passed in 1.61s` — `test_github_vcs_and_deploy_adapter.py` |
| **M4-N05c** | `static_deploy_publisher.py:88-91` | No auto-incluir `deploy_token` en `self.secrets` | **KILLED** | `1 failed, 3 passed in 0.46s` — `test_github_vcs_and_deploy_adapter.py` |
| **M4-N05d** | `static_deploy_publisher.py:230-231` | Relanzar `raise StaticDeployError(...) from None` dentro del `except` de `deploy_site` | **KILLED** | `1 failed, 3 passed in 1.48s` — `test_github_vcs_and_deploy_adapter.py` |
| **M4-N06** | `panel/app.js:48-55` | Quitar rechazo de `//` y credenciales en `sanitizeLinkUrl` (restaurar base `.invalid`) | **KILLED** | `1 failed, 3 passed, 1 warning in 0.89s` — `test_panel_pwa.py` |
| **M4-N07** | `panel/app.js:382, 385` | Quitar `escapeHtml()` al renderizar `build_report.failure_cause` y `project_prompt` | **KILLED** | `1 failed, 3 passed, 1 warning in 0.77s` — `test_panel_pwa.py` |
| **M4-N08** | `telegram_bot.py:614-626` | Quitar `try/except (TelegramTransportError, ...)` alrededor de `_error_outcome` en `poll_once` | **KILLED** | `1 failed, 4 passed in 30.67s` — `test_phase4_audit31_fixes.py` |
| **M4-N09** | `secrets.py:28` | Revertir regex URL Telegram a `[^/\s]+` (sin excluir comillas `\"'`) | **SURVIVED** | **`324 passed, 3 warnings in 94.96s`** — `_sanitize_data` ahora sanea campo a campo antes de `json.dumps` y ningún test unitario pasa JSON crudo con comillas directo a `redact_secrets` |
| **M4-N10** | `cli.py:284-290` | No pasar `circuit_breaker=CircuitBreaker(config_path=config.limits_config_path)` al `Orchestrator` | **KILLED** | `1 failed, 3 passed in 0.93s` — `test_cli_entrypoint.py` |
| **M4-N11** | `cli.py:74-76` | Hacer que `ProductionLLMAdapter.complete()` devuelva `{"fake_hollow": True}` sin error ni red | **SURVIVED** | **`324 passed, 3 warnings in 93.43s`** — ningún test ejercita `ProductionLLMAdapter.complete()` (`0 %` de cobertura) |
| *Nota (redundantes / equiv.)* | `static_deploy_publisher.py:182` (`M4-N04`), `:195` (`M4-N04b`), `:237-241` (`M4-N05b`) | Quitar solo una de las dos llamadas `_sanitize_data` o solo `__cause__/__context__ = None` ya fuera de `except` | *SURVIVED (equiv./redund.)* | `324 passed` en los 3 casos (`96.68s`, `95.54s`, `97.56s`): `:182` y `:195` duplican sanitización y `:237-241` ya corre fuera de `except` donde Python limpió la excepción activa |

### C. Sondas directas ejecutadas sobre commit `52278d2`
- `PROBE_STORE_LEAK`: `build_runtime` fallando por `LLM_API_KEY` faltante deja `leak.db` bloqueado (`LEAKED_OPEN_FD PermissionError`).
- `PROBE_LLM_NO_TRANSPORT` / `PROBE_LLM_INTERVIEW_RESULT`: `build_runtime(cfg)` deja `transport=None`; al primer mensaje de entrevista el proyecto pasa a `FAILED` con `No HttpTransport configured for ProductionLLMAdapter`.
- `PROBE_LLM_WITH_TRANSPORT`: incluso pasando un `HttpTransport` doble, `rt2.llm.complete(...)` devuelve `{}` con `transport_calls= 0`.
- `PROBE_VCS_MERGE_WITH_VERCEL_PUB`: `GitHubVCSAdapter(deploy_publisher=StaticDeployPublisher(target="vercel", ...)).merge_and_deploy("app-1")` solo ejecuta `['gh', 'pr', 'merge', 'work', '--repo', 'acme/app-1', '--merge', '--auto']` y devuelve `https://acme.github.io/app-1` sin llamar a `deploy_publisher`.
- `PROBE_DEPLOY_TARGET_VERCEL_CMD`: `StaticDeployPublisher(target="vercel").deploy_site(...)` ignora `target` y ejecuta `['gh', 'pages', 'deploy', ..., '--branch', 'gh-pages']`.
- `PROBE_DEPLOY_BRANCH_ACCEPTED`: `deploy_site` acepta `MAIN`, `origin/main`, `feat:bad`, `bad\branch`.
- `PROBE_CLI_CONTEXT`: `load_cli_config` retiene `ValueError("...bad_CLI_TOK_LEAK_999")` en `err.__context__` para `ALLOWED_CHAT_IDS`, `AGENT_FACTORY_POLL_TIMEOUT` y `AGENT_FACTORY_BACKOFF_INITIAL`.
- `PROBE_REG_LEAKS_IN_PROJ_JSON` / `PROBE_REG_LEAKS_IN_IDX_JSON`: `GitRegistryPublisher.publish_project` escribe `REG_SEC_123`, `?token=URL_SEC_456` y `?api_key=REPO_SEC_789` sin redactar en `registry.json` y `projects/sec-app.json`.
- `PROBE_SECRET_BARE_TG` / `PROBE_SECRET_LLM_EQ` / `PROBE_SECRET_QKEY`: con `env={}` y `extra_secrets=()`, `redact_secrets` deja intactos `123456:ABCDEFGH_12345`, `LLM_API_KEY=MY_RAW_LLM_999` y `https://x.test/?key=MY_QKEY_111`.

---

## 6. Lo que NO se pudo verificar y por qué

1. **Comportamiento real en navegador / Service Worker del Panel PWA (`panel/sw.js` y renderizado CSS en vivo):**
   - Aunque `panel/app.js` ahora se ejecuta en `deno.exe` (Node.js no está instalado en este equipo), la prueba corre sobre dobles simulados en memoria de `document`, `window` y `fetch` (`tests/test_panel_pwa.py:208-276`), sin Playwright/Chromium real sobre `panel/`. Por lo tanto no se verificó en navegador real la instalación de `sw.js`, su caché offline ni la regresión visual CSS (`panel/styles.css:132`).
2. **Integración real contra APIs y CLIs externos (`api.telegram.org`, `gh`, `git push` remoto HTTPS, proveedores LLM, Vercel/GitHub Pages, `agy`):**
   - Toda la red está bloqueada por `pytest-socket` (`--disable-socket`, `INV-03`) y no existen credenciales reales en el repo (`INV-07`). En particular, `UrllibHttpTransport.post_json` (`telegram_client.py:31-47`) sigue al `0 %` de ejecución y el CLI externo `gh`/`vercel` solo se probó con `FakeRunner`.
3. **Proceso `python -m agent_factory` vivo de larga duración con señales POSIX `SIGTERM`:**
   - En Windows se ejecutó un subproceso real fallido (`test_cli_entrypoint.py:278-288`, salida temprana en `load_cli_config`); no se ejecutó el bucle continuo como daemon vivo recibiendo `SIGINT`/`SIGTERM` del sistema operativo en caliente (por lo cual tampoco se ejercitó en vivo el bloqueo de `time.sleep(delay)` hasta `60 s` de `cli.py:428`), y `__main__.py:3-8` sigue con `0 %` en `pytest-cov`.
4. **Flujo de Fase 5 (`CHANGE_REQUEST`, `INV-14`):**
   - Diferido explícitamente fuera del alcance de Fase 4 según `MASTER_PROMPT.md:186-191`.
