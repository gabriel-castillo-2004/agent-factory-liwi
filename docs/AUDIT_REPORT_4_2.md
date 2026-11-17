# AUDIT_REPORT_4_2 — Re-auditoría Final de Cierre de Fase 4 y Proyecto Completo

Auditor independiente (modo solo lectura). Rama `phase-4`, commit `ab5da03` auditado contra el tag `phase-4.2-tests` (`3ca719b`), `docs/AUDIT_REPORT_4_1.md` (`83f61ea`), `docs/PHASE_REPORT_4.md`, `docs/PHASE4_TEST_MAP.md` y `main`.  
Documentos y artefactos revisados: `MASTER_PROMPT.md` (v1.2.1), `AGENTS.md`, `docs/decisions/ADR-004-pwa-panel-and-cli-entrypoint.md`, `docs/PHASE_REPORT_4.md`, `docs/AUDIT_REPORT_4_1.md`, `docs/PHASE4_TEST_MAP.md`, `docs/DEBT.md`, `.github/workflows/ci.yml`, `.env.example`, `pyproject.toml`, `config/models.toml`, `panel/`, `templates/static-web/`, `src/agent_factory/` y `tests/`.

---

## Veredicto: **NO APTO** *(cierre operativo en producción bloqueado hasta corregir 4 brechas reales de integración)*

### Dictamen objetivo y sin concesiones
El commit `ab5da03` logra avances sólidos sobre `AUDIT_REPORT_4_1.md` en seguridad local, ciclo de vida del CLI y observabilidad de reportes:
- **Se cierran completamente `H4-03`, `H4-05`, `H4-06`, `H4-09`, `H41-02` y `H41-03`:** `build_runtime` valida `limits.toml`, `LLM_API_KEY` y `models.toml` antes de abrir `SqliteEventStore` y cierra el descriptor ante fallos; `load_cli_config` limpia `__cause__`/`__context__` fuera de `except`; `run_polling_loop` espera con `event.wait(delay)`; `deploy_site` valida insensible a mayúsculas, `:`, `\` y sufijos `/main|/master`; `ProductionLLMAdapter.complete` ya invoca `transport.post_json` (matando **`M4-N11`**); `M4-N09` ya muere; y `docs/PHASE_REPORT_4.md` ahora coincide módulo por módulo con `pytest --cov`.
- **Suite y linter en verde offline con `INV-13` intacto:** `ruff check .` pasa sin errores y `pytest` ejecuta **`330 passed, 0 failed`** con **`91 %`** de cobertura global y `git diff phase-4.2-tests --stat -- tests/ templates/ pyproject.toml` vacío.

**Sin embargo, el cierre operativo global y de Fase 4 sigue siendo `NO APTO` para puesta en producción sin correcciones por quedar bloqueado en los siguientes puntos concretos:**
1. **Bloqueo LLM y violación directa de `MASTER_PROMPT.md:31` / `AGENTS.md` #12 (`H42-01`, `M42-10` SURVIVED):** `src/agent_factory/cli.py:80, 92, 96` hardcodea el nombre de modelo `"model-fast-01"` en el código fuente, `ProductionLLMAdapter.complete()` (`cli.py:70-101`) lee siempre `INTERVIEW`/`IDLE`/`FAST` ignorando el estado que lo invoca (nunca usa el modelo fuerte para `MEDIATION`, verificado en sonda `P42-1a`), ignora el parámetro `schema` (`:73`), silencia excepciones de lectura/TOML cayendo de nuevo a `"model-fast-01"` (`:94-96`), y `build_runtime` pasa en abierto si `models.toml` está vacío (`P42-1b`). Mutar `cli.py` para ignorar `models.toml` y fijar `chosen_model = "model-fast-01"` (**`M42-10`**) sobrevive **330/330** pruebas (ancorado por `tests/test_cli_entrypoint.py:474`, que incluye `"model-fast-01"` en su conjunto aceptado).
2. **Transporte HTTP real `UrllibHttpTransport.post_json` al `0 %` de cobertura (`H42-02`, `M42-11` SURVIVED):** `src/agent_factory/adapters/telegram_client.py:32-51` no se ejecuta en ningún test de la suite; mutar `:37-38` para omitir `req_headers.update(headers)` —dejando sin cabecera `Authorization: Bearer` todas las llamadas LLM en producción— (**`M42-11`**) sobrevive **330/330** pruebas.
3. **Flujo VCS -> Deploy roto en parámetros reales (`H42-03`):** `GitHubVCSAdapter.merge_and_deploy` (`github_vcs_adapter.py:266-268`) invoca `deploy_publisher.deploy_site(project_id, ws_dir, branch=valid_branch)` pasando la rama de trabajo (`"work"`) y sobrescribiendo la rama de publicación configurada en `StaticDeployPublisher` (`"gh-pages"`, verificado en sonda `P42-2b`: ejecuta `gh pages deploy ... --branch work`); `cli.py:434-439` no pasa `deploy_base_url=f"https://{config.github_owner}.github.io"` (`P42-2a`); `cli.py:413-418` instancia `Orchestrator(publisher=None)`; `push_branch` (`github_vcs_adapter.py:134`) no hace `git add`/`commit` ni crea PR antes de `gh pr merge` (`:239`); y la rama Vercel expone `self.deploy_token` como argumento CLI `--token` (`static_deploy_publisher.py:234`).
4. **Recuperación incompleta ante conflicto real de rebase y código muerto en bot/events (`H42-04`, `M42-12` SURVIVED):** `GitRegistryPublisher._ensure_repository_ready` (`git_registry_publisher.py:132`) ejecuta `git checkout branch` antes de limpiar un rebase abortado con índice en conflicto, fallando en producción (`P42-3b`: `FAILED RegistryPublishError Git command failed (checkout registry): error: you need to resolve your current index first`); `BuildWorker` (`builder.py:458`) sigue omitiendo `events=` al publicar el cierre en el registro; y `TelegramBotService.registry_publisher` (`telegram_bot.py:75`) sigue siendo un atributo muerto nunca llamado (**`M42-12` SOBREVIVE 330/330**).

---

## 1. Tabla de Cierre de Hallazgos (`H4-01` a `H4-12` y `H41-01` a `H41-04`)

| ID | Severidad | Estado | Evidencia exacta (`archivo:línea`) | Dictamen técnico |
|---|---|---|---|---|
| **H4-01** | **crítico** | **PARCIAL** | **Cerrado parcial:** `src/agent_factory/cli.py:70-171, 361-405`; `src/agent_factory/__main__.py:1-8`; `.env.example:1-5`. **Abierto:** `cli.py:73, 80, 88-96, 384-388, 413-418, 434-439`; `telegram_client.py:32-51` | `build_runtime` cablea `UrllibHttpTransport` (`:394`, **`M42-02` KILLED**), `ProductionLLMAdapter.complete()` ejecuta HTTP POST real con Bearer y sanea errores (**`M4-N11`**, **`M42-03a`**, **`M42-03b` KILLED**), `__main__.py` alcanza `100 %` y `.env.example` documenta `ALLOWED_CHAT_IDS`/`ALLOWED_USER_IDS`/`LLM_API_KEY`. **Bloqueo abierto:** hardcodea `"model-fast-01"` (`:80, 92, 96`), ignora estado (`MEDIATION` nunca usa modelo fuerte, `P42-1a`) y `schema` (`:73`), acepta `models.toml` vacío (`P42-1b`), **`M42-10` SURVIVED**, **`UrllibHttpTransport.post_json` tiene `0 %` cov (`M42-11` SURVIVED)**, `Orchestrator` queda con `publisher=None` (`:413-418`) y `StaticDeployPublisher` omite `deploy_base_url` de `github_owner` (`P42-2a`). |
| **H4-02** | **crítico** | **PARCIAL** | **Cerrado parcial:** `github_vcs_adapter.py:262-268`; `static_deploy_publisher.py:230-236`. **Abierto:** `github_vcs_adapter.py:134, 239, 263-268`; `static_deploy_publisher.py:233-236`; `cli.py:434-439` | `merge_and_deploy` invoca `self.deploy_publisher.deploy_site(...)` (**`M42-06` KILLED**) y `StaticDeployPublisher` enruta `"vercel"` a `vercel deploy <dir> --yes` (**`M42-07b` KILLED**). **Abierto:** `merge_and_deploy` (`:267`) sobrescribe `deploy_publisher.branch` (`"gh-pages"`) con `branch=valid_branch` (`"work"`, sonda `P42-2b`); `cli.py:434` no pasa `deploy_base_url` (`P42-2a`); `push_branch` no hace `git add/commit` ni abre PR previo a `gh pr merge`; Vercel expone `--token` en argv (`:234`); y `gh pages deploy` no es subcomando oficial estándar de `gh`. |
| **H4-03** | **alto** | **CERRADO** | `src/agent_factory/adapters/static_deploy_publisher.py:214-227` | Normaliza `.lower()`, rechaza `:`, `\`, `origin/main`, `origin/master`, y sufijos `/main`, `/master` antes de invocar `Runner`. Mata **`M42-07a`**. |
| **H4-04** | **alto** | **CERRADO** | `tests/test_panel_pwa.py:199-358`; `panel/app.js:15-26` | Cerrado desde `AUDIT_REPORT_4_1`; verificado en corrida verde (`330 passed`). |
| **H4-05** | **alto** | **CERRADO** | `src/agent_factory/secrets.py:27-31`; `tests/test_secret_sanitization.py` | Regex URL Telegram excluye `\"'` (`:28`) y la suite ahora prueba `redact_secrets` directo sobre JSON con comillas. **`M4-N09` KILLED** (`0.50s`). |
| **H4-06** | **alto** | **CERRADO** | `src/agent_factory/cli.py:124-127, 136-139, 166-169, 239-242, 285-288, 314-317, 378-382` | Todas las elevaciones sanitizadas en `cli.py` (parsing IDs, timeout, backoff, `models.toml`, `ProductionLLMAdapter`) y en los 3 adaptadores asignan y limpian `__cause__ = None` y `__context__ = None` **fuera** del bloque `except`. **`M42-03b`** y **`M42-05` KILLED**. *(Nota menor: `main()` en `:586` usa solo `(token,)` al redactar `CliConfigError` de `build_runtime`).* |
| **H4-07** | **alto** | **CERRADO** | `github_vcs_adapter.py:154-271`; `static_deploy_publisher.py:206-257` | Cerrado desde `AUDIT_REPORT_4_1` (`95 %` y `94 %` cov). |
| **H4-08** | **alto** | **CERRADO** | `panel/app.js:40-63, 381-386`; `panel/index.html:37` | XSS y validación de enlaces cerrados desde `AUDIT_REPORT_4_1`. *(Regresión de CSS restante consolidada en `H41-04`).* |
| **H4-09** | **medio** | **CERRADO** | `src/agent_factory/cli.py:239-317, 361-410, 514-516, 559` | `run_polling_loop` usa `event.wait(delay)` cuando `clock is None` (**`M42-04` KILLED**); `load_cli_config` limpia `__context__` (**`M42-05` KILLED**); `build_runtime` abre `SqliteEventStore` tras validar config y lo cierra en `except` (**`M42-01` KILLED**). |
| **H4-10** | **medio** | **PARCIAL** | **Cerrado parcial:** `git_registry_publisher.py:88-96, 248-318`. **Abierto:** `git_registry_publisher.py:132, 194-204`; `builder.py:458` | `publish_project` acepta `events: Sequence[Event] = ()` y sanea recursivamente con `_scrub_value` + `redact_secrets` (`registry.json`, `projects/<id>.json` y `events`; **`M42-08_FULL_OMIT` KILLED**). **Abierto:** (1) `_ensure_repository_ready` (`:132`) falla antes de abortar rebase si un crash dejó conflicto activo en el índice (sonda `P42-3b`: `FAILED RegistryPublishError`); (2) restauración post-conflicto (`:194-204`) reescribe `registry.json` sin `_scrub_value`; (3) `BuildWorker` (`builder.py:458`) sigue sin pasar `events=...`. |
| **H4-11** | **medio** | **PARCIAL** | `src/agent_factory/telegram_bot.py:75`; `tests/test_telegram_polling_and_acl.py:786, 790, 793` | `TelegramBotService.registry_publisher` (`:75`) nunca se invoca en toda la clase (**`M42-12` SURVIVED 330/330**). Persisten aserciones débiles negativas (`!= "plan_ready_for_approval"` en `:793`, aunque acompañadas de verificación de estado en `:791, :794`). |
| **H4-12** | **bajo** | **PARCIAL** | **Cerrado:** `docs/PHASE_REPORT_4.md:127-169`; `panel/styles.css:132-135`. **Abierto:** `secrets.py:34-76`; `panel/app.js:215-221, 230-231` | `PHASE_REPORT_4.md:127-169` lista ahora los `40` módulos con métricas exactas y `panel/styles.css:132-135` corrige selector de input. **Abierto:** `secrets.py` con `env={}` sigue dejando sin redactar `<digits>:<token>` bare, `LLM_API_KEY=` y `?key=` (`P42-5`); `app.js:215-221, 230-231` usa `Number(...)` sin `Number.isFinite(...)`. |
| **H41-01** | **crítico** | **PARCIAL** | **Cerrado:** `cli.py:98-171, 394-404`. **Abierto:** `cli.py:73, 80, 88-96, 384-388`; `telegram_client.py:32-51` | `ProductionLLMAdapter.complete()` ya no es un stub vacío: llama `transport.post_json` con Bearer y limpia errores (**`M4-N11` KILLED**). **Abierto:** hardcodea `"model-fast-01"` (`M42-10` SURVIVED), ignora `MEDIATION`/`schema`, falla en abierto con `models.toml` vacío (`P42-1a`, `P42-1b`), y `UrllibHttpTransport.post_json` sigue al `0 %` (`M42-11` SURVIVED). |
| **H41-02** | **alto** | **CERRADO** | `src/agent_factory/cli.py:361-410, 514-516` | Valida límites, `LLM_API_KEY` y `models.toml` antes de abrir `SqliteEventStore` (`:409`) y cierra `store.close()` en `except Exception`. **`M42-01` KILLED**. |
| **H41-03** | **alto** | **CERRADO** | `src/agent_factory/cli.py:232-243, 279-289, 308-318, 559` | `__cause__`/`__context__ = None` aplicado fuera de `except` en whitelists, timeout y backoff (**`M42-05` KILLED**); backoff de loop usa `event.wait(delay)` (**`M42-04` KILLED**). |
| **H41-04** | **bajo** | **PARCIAL** | **Cerrado:** `panel/styles.css:132-135`. **Abierto:** `panel/styles.css:165-171`; `panel/app.js:382, 385` | `styles.css:132-133` ahora estiliza `input[type="text"]` y `#registry-base-input` (**`M42-09` KILLED**), pero sigue sin reglas CSS para `.alert-warning` ni `.prompt-box` renderizados en `app.js:382, 385`. |

---

## 2. Hallazgos Nuevos / Consolidados tras `ab5da03` (`H42-01` a `H42-05`)

| ID | Severidad | Evidencia (`archivo:línea` + mutación/sonda) | Descripción exacta del problema |
|---|---|---|---|
| **H42-01** | **crítico** | `src/agent_factory/cli.py:70-101, 384-388, 413-418, 434-439`; `tests/test_cli_entrypoint.py:474`; `M42-10`; `P42-1a`, `P42-1b`, `P42-2a` | **Hardcoding de modelo, omisión de `MEDIATION`/`schema` y `deploy_base_url` fijo:** `cli.py:80, 92, 96` hardcodea `"model-fast-01"` (violación directa de `MASTER_PROMPT.md:31` y `AGENTS.md` #12); `complete()` siempre consulta `INTERVIEW or IDLE or FAST`, ignora el estado (`MEDIATION` recibe modelo rápido en `P42-1a`), descarta `schema` (`has_schema_in_payload: False`), silencia errores TOML (`:94-96`), y `build_runtime` no exige claves mínimas en `models.toml` (`P42-1b`). **`M42-10` sobrevive 330/330**. Además `Orchestrator` queda con `publisher=None` (`:413-418`) y `StaticDeployPublisher` usa `"https://agent-factory.github.io"` ignorando `config.github_owner` (`P42-2a`). |
| **H42-02** | **crítico** | `src/agent_factory/adapters/telegram_client.py:24-51`; `M42-11` | **`UrllibHttpTransport.post_json` tiene `0 %` de cobertura y `M42-11` sobrevive:** Las líneas `32-51` no se ejercitan en ninguna prueba. Mutar `:37-38` para descartar los encabezados HTTP (`Authorization: Bearer` de LLM) pasa los **330 tests** sin fallos. |
| **H42-03** | **alto** | `github_vcs_adapter.py:134, 239, 263-268`; `static_deploy_publisher.py:231-236`; `P42-2b` | **Despliegue sobreescribe la rama `gh-pages` con `"work"`, sin commit/PR y `--token` en argv:** `merge_and_deploy` pasa `branch=valid_branch` (`"work"`) a `deploy_publisher.deploy_site` (`P42-2b`: ejecuta `gh pages deploy ... --branch work`); `push_branch` no hace `git add/commit` ni abre PR previo a `gh pr merge`; `_last_workspace_dir` es solo en memoria; y `vercel` expone `self.deploy_token` en argv (`--token`) pese a ya estar en `env`. |
| **H42-04** | **alto** | `git_registry_publisher.py:132, 194-204`; `builder.py:458`; `telegram_bot.py:75`; `P42-3b`, `M42-12` | **Fallo ante conflicto real de rebase y cableado faltante de eventos/`registry_publisher`:** Si existe `.git/rebase-merge` con índice conflictivo (`P42-3b`), `git checkout branch` (`:132`) falla antes de `rebase --abort` (`RegistryPublishError: you need to resolve your current index first`). La restauración en `:194-204` no invoca `_scrub_value`; `builder.py:458` no pasa `events=` a `publish_project`; y `TelegramBotService.registry_publisher` (`:75`) sigue sin usarse (**`M42-12` sobrevive 330/330**). |
| **H42-05** | **bajo** | `panel/styles.css:165-171`; `panel/app.js:215-221, 382-385`; `secrets.py:34-76`; `.github/workflows/ci.yml:35`; `docs/DEBT.md:28-29`; `P42-5` | **Deuda menor y endurecimiento CI pendiente:** Faltan reglas CSS `.alert-warning` y `.prompt-box`; `app.js` no valida `Number.isFinite` en contadores; `secrets.py` con `env={}` no redacta `<digits>:<token>` bare, `LLM_API_KEY=` ni `?key=` (`P42-5`); `.github/workflows/ci.yml:35` no aplica `--cov-fail-under=85` (`F-21`) ni lockfile transitivo (`F-20`) ni corre `templates/static-web` o Playwright. |

---

## 3. Verificación de `INV-13`, `ruff`, `pytest --cov` y Comparativa contra `docs/PHASE_REPORT_4.md`

### Comandos reales ejecutados (1 corrida cada uno en workspace limpio)
- **Integridad `INV-13` (`git diff phase-4.2-tests --stat -- tests/ templates/ pyproject.toml MASTER_PROMPT.md AGENTS.md`):** salida **vacía**, exit code `0` (`0` archivos/líneas alterados en rutas protegidas).
- **Linter (`.\.venv\Scripts\ruff.exe check .`):** `All checks passed!` (exit code `0`).
- **Marcadores `skip`/`xfail` en `tests/*.py`:** `0` marcadores activos (solo 2 menciones en docstrings).
- **Suite completa + cobertura (`.\.venv\Scripts\pytest.exe -q --cov=src/agent_factory --cov-report=term-missing`):**
  - Resultado real: **`330 passed, 3 warnings in 67.32s (0:01:07)`** (exit code `0`).
  - Cobertura global (`src/agent_factory`): **`91 %`** (`2908` stmts, `198` miss, `784` branch, `112` brpart).

### Comparativa exacta contra `docs/PHASE_REPORT_4.md`
- **Cifras coincidentes:** El total de tests (`330 passed`), el `TOTAL` global (`2908` stmts, `198` miss, `784` branch, `112` brpart -> `91 %`) y los **40 módulos no vacíos** reportados en `docs/PHASE_REPORT_4.md:127-169` (incluyendo ahora `__main__.py` al `100 %` y `subprocess_runner.py` al `57 %`) coinciden exactamente con la salida real de `pytest-cov` (únicamente se omiten los cinco `__init__.py` con `0` sentencias, y el reloj difiere de forma normal `147.84s` vs `67.32s`).

### Líneas y ramas críticas sin cobertura observadas en código de Fase 4
- `src/agent_factory/adapters/telegram_client.py` (**`85 %`**): **`32-51` (`UrllibHttpTransport.post_json` completamente al `0 %`)**, `132`, `137`, `151->154`, `155->168`, `157->168`, `164->166`, `203->206`.
- `src/agent_factory/cli.py` (**`87 %`**): `67`, `77` (`transport is None`), `82->98`, `86->88`, **`94-96` (fallback silencioso a `"model-fast-01"`)**, `145`, `153-171` (parseo de `content` dict/no-str y error JSON), `362`, `378-382`, `433->441`, `514-516`, `525->exit`, `554->556`, **`585-588` (`CliConfigError` de `build_runtime` en `main()`)**.
- `src/agent_factory/adapters/git_registry_publisher.py` (**`88 %`**): `95`, `127-130`, `163->171`, `169-170`, `172->exit`, `180->187`, `183->187`, `185-186`, `189-190`, `199-200`, `283->288`, `285-286`, `289`, `292->294`, `335-336`, `346-347`, `362-363`, `382->396`, `385-386`, `387->396`, `397->400`.
- `src/agent_factory/adapters/github_vcs_adapter.py` (**`95 %`**): `101->104`, `130`, `200`, `206->222`, `244-245`.
- `src/agent_factory/adapters/static_deploy_publisher.py` (**`94 %`**): `102->105`, `160->163`, `233->237`, `255-257`.
- `src/agent_factory/adapters/subprocess_runner.py` (**`57 %`**): `27`, `60-61`, `67-93`.
- `src/agent_factory/telegram_bot.py` (**`91 %`**): `84->88`, `88->92`, `97->93`, `104->exit`, `111->exit`, `164`, `198`, `229-230`, `238-239`, `276->279`, `285`, `312-313`, `365-372`, `428-429`, `482`, `491-492`, `500->532`, `518->520`, `520->522`, `575`, `628`.

---

## 4. Mutaciones y Sondas en Worktree Temporal (`%TEMP%\af_audit4_2_wt`)

Todas las mutaciones y sondas corrieron en un `git worktree` aislado (`%TEMP%\af_audit4_2_wt`, `ab5da03`), verificando importación desde el worktree (`IMPORTED_FROM: ...\af_audit4_2_wt\src\agent_factory\__init__.py`) y validando compilación con `py_compile` cuando aplicó. Al concluir se ejecutó `git worktree remove --force`, `git worktree prune` y se comprobó `git worktree list` con único workspace y `git status --short` vacío.

### A. Repetición de las 2 mutaciones supervivientes de `AUDIT_REPORT_4_1.md` (`2/2 KILLED`)

| ID | Archivo y punto mutado | Resultado | Evidencia real observada |
|---|---|---|---|
| **M4-N09** | `src/agent_factory/secrets.py:28` (revertir regex URL Telegram a `[^/\s]+` sin excluir comillas `\"'`) | **KILLED** | `1 failed, 4 passed in 0.50s` — `test_secret_sanitization.py::test_redact_secrets_telegram_url_without_slash_preserves_quotes_and_kills_m4_n09` |
| **M4-N11** | `src/agent_factory/cli.py:76` (reemplazar `ProductionLLMAdapter.complete()` con stub `return {"fake_hollow": True}`) | **KILLED** | `1 failed, 5 passed in 1.71s` — `test_cli_entrypoint.py::test_cli_production_llm_adapter_uses_urllib_transport_calls_http_and_releases_store_on_error` |

### B. Nuevas mutaciones sobre correcciones de `ab5da03` (`11` KILLED / `3` SURVIVED + `1` redundante)

| ID | Archivo y punto mutado | Descripción de la mutación | Resultado | Evidencia real observada |
|---|---|---|---|---|
| **M42-01** | `cli.py:361-409` | Mover `SqliteEventStore(db_path=config.db_path)` antes de validar `limits.toml`/`LLM_API_KEY`/`models.toml` | **KILLED** | `1 failed, 5 passed in 1.30s` — `test_cli_entrypoint.py` |
| **M42-02** | `cli.py:394` | Cambiar `effective_transport` a `transport` (dejando `transport=None`) | **KILLED** | `1 failed, 5 passed in 0.75s` — `test_cli_entrypoint.py` |
| **M42-03a** | `cli.py:103` | Omitir encabezado `"Authorization": f"Bearer {self.api_key}"` en `ProductionLLMAdapter.complete` | **KILLED** | `1 failed, 5 passed in 0.74s` — `test_cli_entrypoint.py` |
| **M42-03b** | `cli.py:117-122` | Omitir `redact_secrets` y lanzar `LLMError(f"LLM transport error: {exc}")` directo en `except` | **KILLED** | `1 failed, 5 passed in 0.79s` — `test_cli_entrypoint.py` |
| **M42-04** | `cli.py:559` | Reemplazar `event.wait(delay)` por `time.sleep(delay)` en `run_polling_loop` | **KILLED** | `1 failed, 5 passed in 5.78s` — `test_cli_entrypoint.py::test_cli_config_scrubs_exception_context_and_polling_loop_wakes_immediately_on_stop_event` |
| **M42-05** | `cli.py:232-237` | Lanzar `raise CliConfigError(clean_err) from None` dentro del `except ValueError` de `_parse_id_whitelist` | **KILLED** | `1 failed, 5 passed in 0.70s` — `test_cli_entrypoint.py` |
| **M42-06** | `github_vcs_adapter.py:262-270` | Omitir llamada `self.deploy_publisher.deploy_site(...)` en `merge_and_deploy` | **KILLED** | `1 failed, 4 passed in 0.26s` — `test_github_vcs_and_deploy_adapter.py` |
| **M42-07a** | `static_deploy_publisher.py:216-226` | Revertir validación de rama a `effective_branch in {"main", "master", ...}` sin `.lower()`, `:` ni `\` | **KILLED** | `1 failed, 4 passed in 0.30s` — `test_github_vcs_and_deploy_adapter.py` |
| **M42-07b** | `static_deploy_publisher.py:230-236` | Eliminar rama `target_lower == "vercel"` dejando siempre `gh pages deploy` | **KILLED** | `1 failed, 4 passed in 0.27s` — `test_github_vcs_and_deploy_adapter.py` |
| **M42-08_FULL_OMIT** | `git_registry_publisher.py:88-96, 296-317` | Desactivar `_scrub_value` y eliminar ambos `redact_secrets(json.dumps(...))` en `publish_project` | **KILLED** | `1 failed, 5 passed in 20.86s` — `test_phase4_audit31_fixes.py::test_git_registry_publisher_scrubs_secrets_in_registry_index_project_json_and_events` |
| **M42-09** | `panel/styles.css:132-133` | Revertir `input[type="text"], #registry-base-input,` a `input[type="url"],` | **KILLED** | `1 failed, 4 passed, 1 warning in 0.54s` — `test_panel_pwa.py::test_panel_styles_css_targets_text_input_not_removed_url_input` |
| **M42-10** | `cli.py:88-93` | Ignorar `models.toml` y fijar `chosen_model = "model-fast-01"` en `ProductionLLMAdapter.complete()` | **SURVIVED** | **`330 passed, 3 warnings in 60.09s (0:01:00)`** — `test_cli_entrypoint.py:474` acepta `"model-fast-01"` |
| **M42-11** | `telegram_client.py:37-38` | Omitir `req_headers.update(headers)` en `UrllibHttpTransport.post_json` | **SURVIVED** | **`330 passed, 3 warnings in 60.73s (0:01:00)`** — `UrllibHttpTransport.post_json` tiene `0 %` de cobertura |
| **M42-12** | `telegram_bot.py:75` | Asignar `self.registry_publisher = None` en `TelegramBotService.__init__` | **SURVIVED** | **`330 passed, 3 warnings in 61.39s (0:01:01)`** — atributo nunca usado en `TelegramBotService` |
| *Nota (`M42-08`)* | `git_registry_publisher.py:88-96` | Hacer que `_scrub_value` devuelva `value` manteniendo `redact_secrets(json.dumps(...))` al serializar | *SURVIVED (redundante)* | `330 passed in 62.59s`: el paso externo `redact_secrets(json.dumps(...))` continúa sanitizando cadenas sin comillas |

### C. Sondas directas ejecutadas sobre `ab5da03`
- `P42-1a`: Con `models.toml` definiendo `MEDIATION = "strong-med-01"` e `INTERVIEW = "fast-interview"`, llamar a `ProductionLLMAdapter.complete()` envía `model = "fast-interview"` y **`has_schema_in_payload: False`**.
- `P42-1b`: `build_runtime(cfg)` con un `models.toml` **vacío** pasa la validación y termina llamando con `chosen_model = "model-fast-01"`.
- `P42-2a` / `P42-2b`: `build_runtime(cfg_with_github_owner)` crea `StaticDeployPublisher` con `deploy_base_url = "https://agent-factory.github.io"`; y `merge_and_deploy("app-one", branch="work")` invoca `['gh', 'pages', 'deploy', '...\\app-one', '--branch', 'work']` (sobrescribiendo `gh-pages`).
- `P42-3` vs `P42-3b`: Un directorio `.git/rebase-merge` vacío sin conflicto se recupera (`RECOVERED`), pero un `.git/rebase-merge` procedente de un **conflicto real de rebase en el índice** falla (`P42-3b_PUBLISH_RESULT: FAILED RegistryPublishError Git command failed (checkout registry): error: you need to resolve your current index first`).
- `P42-5`: Con `env={}`, `redact_secrets` deja intactos `123456:ABCDEFGH_12345`, `LLM_API_KEY=RAW_LLM_999` y `https://x.test/?key=MY_QKEY_111`.

---

## 5. Revisión Global contra `MASTER_PROMPT.md` (v1.2.1) e Invariantes (`INV-01` a `INV-23`)

### Cumplimiento por secciones del documento rector
- **§2 Decisiones de diseño:** Regla *"Nombres de modelos nunca van en el código: se leen de `config/models.toml` por estado (uno fuerte para `MEDIATION`, uno rápido para el resto)"* (`MASTER_PROMPT.md:31`) -> **NO CUMPLE** (`cli.py:80, 92, 96`). Arquitectura *local-first*, bot Telegram propio sin backend externo, hosting estático de costo `$0` -> **CUMPLE en diseño**.
- **§7 Fases 0 a 4:** Fases 0, 1, 2 y 3 implementaron la base offline completa con pruebas deterministas, máquina de estados, builder y bot; Fase 4 cerró PWA estática, sanitización JSON, bloqueo de path/branch en `deploy_site` y lifecycle de CLI/store, pero deja abiertas las brechas operativas `H42-01`..`H42-05`. Fase 5 (`CHANGE_REQUEST`) queda correctamente fuera de alcance v1 (`INV-14`).

| Invariante | Estado | Evidencia / Justificación estricta |
|---|:---:|---|
| **INV-01** | **parcial** | `cli.py:94-96, 585-588` y sobre todo **`UrllibHttpTransport.post_json` (`telegram_client.py:32-51`) al `0 %`** permiten que sobrevivan `M42-10` y `M42-11`; tampoco hay ejecución real de `panel/sw.js` ni Playwright template en CI. |
| **INV-02** | **cumple (adaptadores locales) / parcial (E2E real)** | El dominio y los puertos no importan SDKs externos; `ProductionLLMAdapter`, `TelegramHttpClient`, `GitHubVCSAdapter` y `StaticDeployPublisher` pasan detrás de `HttpTransport` y `Runner`, aunque `UrllibHttpTransport` no está ejercitado en tests (`0 %`). |
| **INV-03** | **cumple** | `pyproject.toml:33` (`--disable-socket`), **`330 passed`** offline sin red ni secretos reales. |
| **INV-04** | **cumple** | `domain/states.py`, `domain/progress.py`, `orchestrator.py` y `telegram_bot.py` validan estado origen y pasan únicamente por `validate_transition`. |
| **INV-05** | **cumple** | `orchestrator.py` y `domain/progress.py` exigen `ApprovalEvent(approved)` con `plan_hash` vigente en `WAITING_APPROVAL` antes de `BUILDING`. |
| **INV-06** | **cumple** | Circuit breaker central (`cli.py:412`), `max_ci_rounds=3`, tope de mediación, backoff acotado con `event.wait` (`cli.py:559`) y kill de árbol de procesos (`subprocess_runner.py`). |
| **INV-07** | **cumple (repo) / parcial (runtime edge)** | Cero secretos en repo, `.env` ignorado, `.env.example` corregido, CLI y `publish_project` ya sanean errores/archivos y `__context__`; persisten brechas menores (`secrets.py` con `env={}` en `P42-5`, `vercel --token` en argv `:234`, restauración rebase `:194-204`). |
| **INV-08** | **cumple** | `pyproject.toml:11-22` intacto y fijado con `==` (deuda `F-20` abierta en `DEBT.md` por falta de lockfile transitivo). |
| **INV-09** | **cumple** | Manejo acotado de fallos LLM/cuota verificado en `orchestrator.py`, `mediator.py`, `builder.py` y ahora `ProductionLLMAdapter` (`cli.py:117-169`). |
| **INV-10** | **cumple** | Reportes `docs/PHASE_REPORT_0..4.md` emitidos al cierre de cada fase con datos exactos en `PHASE_REPORT_4.md`. |
| **INV-11** | **cumple** | Registro append-only y proyección determinista `project_status_from_events` validando cada transición al reproducir. |
| **INV-12** | **cumple** | `calculate_progress` (`domain/progress.py:22-37`) determinista según tabla de §5. |
| **INV-13** | **cumple (suite) / parcial (CI template)** | Inmutabilidad estricta verificada (`git diff phase-4.2-tests --stat` vacío, `workspace.py` revierte cambios de tests); sin embargo `.github/workflows/ci.yml` no ejecuta el Playwright e2e (`console.error`/`pageerror`) de `templates/static-web/` ni `panel/`. |
| **INV-14** | **no verificable** | `CHANGE_REQUEST` declarado en `domain/states.py`; lógica diferida a Fase 5 (`MASTER_PROMPT.md:186-191`). |
| **INV-15** | **cumple (local) / parcial (push VCS)** | Tope de 3 rondas CI en `builder.py`; en `GitHubVCSAdapter.push_branch` (`:134`) falta `git add/commit` y apertura de PR previa a `gh pr merge`. |
| **INV-16** | **cumple** | Deduplicación persistente `telegram_processed_updates` previa al LLM y panel 100 % estático sin backend. |
| **INV-17** | **cumple** | `CircuitBreaker(config_path=config.limits_config_path)` cableado en `cli.py:412, 417`. |
| **INV-18** | **cumple** | Aislamiento en `workspaces/<project_id>/`, `clean_env` en adaptadores, `source_dir.resolve()` en deploy. |
| **INV-19** | **parcial** | `AgyCliAdapter` y `clean_env` sanean entornos y `__context__` se limpia en CLI y adaptadores, pero `StaticDeployPublisher` (`vercel`) expone `deploy_token` en CLI `--token` (`:234`) y la ejecución real con `agy`/APIs queda fuera de verificación offline. |
| **INV-20** | **cumple** | Plantilla `templates/static-web/` con `TemplateScaffold` verificados. |
| **INV-21** | **cumple (salvo conflicto rebase real)** | `GitRegistryPublisher` aísla rama `registry` y CLI falla cerrado ante config inválida (aunque `models.toml` vacío pasa `P42-1b` y rebase con conflicto real falla en `P42-3b`). |
| **INV-22** | **cumple** | `validate_project_id` y `StaticDeployPublisher.deploy_site` validan rutas, ramas insensibles a mayúsculas, `:`, `\` y `/main|/master` (`M42-07a` KILLED). |
| **INV-23** | **parcial** | Oráculos escritos a mano en general, pero **`M42-10`**, **`M42-11`** y **`M42-12`** sobreviven a los 330 tests (`test_cli_entrypoint.py:474` admite el literal `"model-fast-01"` hardcodeado). |

---

## 6. Lo que NO se pudo verificar y por qué

1. **Instalación, caché offline y runtime real del Service Worker (`panel/sw.js`) y renderizado CSS en navegador:**
   - Las pruebas de `panel/app.js` corren en `deno` con mocks en memoria; no se levantó Playwright/Chromium real contra `panel/`, por lo que `panel/sw.js` (`install`/`activate`/`fetch`), el comportamiento offline real del PWA y la apariencia visual (incluidas las clases faltantes `.alert-warning` y `.prompt-box` en `panel/styles.css`) no fueron comprobados en navegador.
2. **Tráfico HTTP real y CLIs externos (`UrllibHttpTransport`, API LLM, `api.telegram.org`, `gh`, `git push`, `vercel`, `agy`):**
   - `pytest-socket` bloquea toda red (`--disable-socket`) y el repositorio no contiene credenciales reales. En particular, `UrllibHttpTransport.post_json` (`telegram_client.py:32-51`) tiene **`0 %` de ejecución** en la suite, y todos los comandos `gh`/`git`/`vercel`/`agy` hacia remotos reales se simularon con `FakeRunner` o repos locales temporales (excepto `git` local en `GitRegistryPublisher`).
3. **Ejecución E2E Playwright/Node de `templates/static-web/` dentro de `.github/workflows/ci.yml`:**
   - `.github/workflows/ci.yml` instala y ejecuta únicamente Python (`ruff check .` y `pytest --cov=src/agent_factory` sin `--cov-fail-under=85`); tampoco se ejecutaron las pruebas JS `npm test` / `playwright test` de `templates/static-web/package.json`.
4. **Proceso vivo `python -m agent_factory` con señales `SIGINT`/`SIGTERM` reales del sistema operativo:**
   - En pruebas se ejercitó `main()` con `threading.Event` ya activado (`pre_stopped.set()`) o salida por error de configuración; no se mantuvo un demonio vivo en Windows recibiendo `CTRL_C_EVENT`/`SIGTERM` asíncrono del SO durante backoff real.
5. **Flujo completo de mantenimiento Fase 5 (`CHANGE_REQUEST`, `INV-14`):**
   - Explícitamente diferido fuera de v1/Fase 4 por `MASTER_PROMPT.md:72, 186-191`.
