# AUDIT_REPORT_4_3 — Re-auditoría Final de Cierre de Fase 4 y Proyecto Completo

Auditor independiente (modo solo lectura). Rama `phase-4`, commit `382e91e` auditado contra el tag `phase-4.3-tests` (`2c230ca`), `docs/AUDIT_REPORT_4_2.md`, `docs/PHASE_REPORT_4.md`, `docs/PHASE4_TEST_MAP.md` y `MASTER_PROMPT.md` (v1.2.1).  
Documentos y artefactos revisados: `MASTER_PROMPT.md` (v1.2.1), `AGENTS.md`, `docs/decisions/ADR-004-pwa-panel-and-cli-entrypoint.md`, `docs/PHASE_REPORT_4.md`, `docs/AUDIT_REPORT_4_2.md`, `docs/PHASE4_TEST_MAP.md`, `docs/DEBT.md`, `.github/workflows/ci.yml`, `pyproject.toml`, `config/models.toml`, `panel/`, `src/agent_factory/` y `tests/`.

---

## Veredicto: **NO APTO** *(las 3 mutaciones previas `M42-10`, `M42-11` y `M42-12` mueren, pero `382e91e` introduce una violación directa de `INV-17` y deja brechas abiertas en `H42-01`, `H42-03`, `H42-04` y `H42-05`)*

### Dictamen objetivo y sin concesiones
El commit `382e91e` logra avances verificables frente a `AUDIT_REPORT_4_2.md`:
- **Las 3 mutaciones antes supervivientes (`M42-10`, `M42-11`, `M42-12`) ahora mueren** bajo sus respectivas suites en worktree aislado; `H42-02` queda **CERRADO** (`UrllibHttpTransport.post_json` cubierto en sentencias, envía headers Bearer y sanea errores).
- **`INV-13` intacto y métricas consistentes:** `git diff phase-4.3-tests --stat -- tests/ templates/ pyproject.toml MASTER_PROMPT.md AGENTS.md` es vacío, `ruff check .` pasa sin errores y `pytest -q --cov=src/agent_factory --cov-report=term-missing` ejecuta **`337 passed, 3 warnings in 106.78s (0:01:46)`** con **`91 %`** de cobertura global coincidente con `docs/PHASE_REPORT_4.md`.

**Sin embargo, el cierre operativo de Fase 4 y del proyecto completo es `NO APTO` por los siguientes bloqueos reales:**
1. **Violación crítica de `INV-17` y `INV-06` introducida en `382e91e` (`H43-01`):** `src/agent_factory/orchestrator.py:411` agrega `self.circuit_breaker.reset(project_id)` dentro de `request_adjustment()`. `MASTER_PROMPT.md:53` (`INV-17`) prohíbe expresamente reiniciar contadores tras un ajuste (*"Los contadores son acumulativos por proyecto y nunca se reinician al volver a entrar a un estado (p. ej. tras un ajuste)"*). Verificado en sonda **`P43-1`**: los contadores de `INTERVIEW`, `DEBATE` y `MEDIATION` vuelven a `0` tras `request_adjustment`, permitiendo un bucle ilimitado de reintentos/ajustes. Este atajo se añadió porque `tests/test_phase4_e2e_offline.py:412` configura `DEBATE = 3` exigiendo 4 llamadas `DEBATE` en dos rondas (al eliminar `reset(project_id)` en **`M43-11`**, dicho test falla en `:628`), en lugar de reportar el defecto del test en `docs/PHASE4_TEST_ISSUES.md`.
2. **`H42-01` sigue PARCIAL (`cli.py:156, 464-478, 536-540`):** continúa el literal hardcodeado `chosen_model = "model-fast-01"` cuando `models_config_path is None` (`P43-2a`, violando `MASTER_PROMPT.md:31` y `AGENTS.md` #12); `build_runtime(cfg)` sigue aceptando `models.toml` vacío y `[models]` anidado con `"CONFIGURE_ME"` (`P43-2b`); y `deploy_base_url` asigna `https://<owner>.github.io` incluso cuando `deploy_target == "vercel"` (`P43-2b`).
3. **Publicación obligatoria no protegida en `Orchestrator.get_status` (`H43-02`):** cablear `publisher=effective_registry` (`GitRegistryPublisher`) en `cli.py:519` hace que `Orchestrator.get_status()` (`orchestrator.py:478`) invoque `self.publisher.publish(status)` sin capturar `RegistryPublishError`. Si el remoto `registry` falla, `create_project()` y `/status` colapsan en lugar de degradar como mejor esfuerzo (`P43-3`, violando `MASTER_PROMPT.md:177` / `INV-21`).
4. **Fallo silencioso de `push_branch` sin identidad git (`H42-03` / `H43-03`):** `GitHubVCSAdapter.push_branch` (`github_vcs_adapter.py:134-140`) ejecuta `git commit` sin `-c user.name` / `-c user.email` y sin revisar `exit_code`. En entorno limpio sin `.gitconfig` global (`P43-4a`), `git commit` falla en silencio, se empuja la rama sin los archivos nuevos y el método retorna éxito falso.
5. **Parches cosméticos/frágiles (`H43-04`):** `panel/app.js:38` borra `\bNaN\b` de cualquier cadena mostrada en `redactDisplayText` (`"NaN Check"` -> `" Check"`, sonda `P43-5`, `M43-10`), y `TelegramBotService._publish_status_to_registry` (`telegram_bot.py:137-141`) acopla el servicio a `isinstance(..., GitRegistryPublisher)` y `orchestrator.publisher is not None`.

---

## 1. Tabla de Cierre de `H42-01` a `H42-05`

| ID | Severidad | Estado | Evidencia exacta (`archivo:línea`) | Dictamen técnico |
|---|---|---|---|---|
| **H42-01** | **crítico** | **PARCIAL** | **Cerrado parcial:** `src/agent_factory/cli.py:82-154, 162-175, 218-247, 514-547`. **Abierto:** `src/agent_factory/cli.py:120-124, 155-156, 464-478, 536-540`; `P43-2a`, `P43-2b` | `ProductionLLMAdapter.complete()` lee `[models]` (`fast`/`strong`), enruta `MEDIATION`/`Plan` a `strong` e `INTERVIEW`/`DEBATE` a `fast`, envía `response_format`/`response_schema`, y falla cerrado en TOML vacío/corrupto o respuesta no-JSON (**`M42-10a/b/c` KILLED**); `build_runtime` cablea `Orchestrator.publisher` (**`M43-09` KILLED**) y `deploy_base_url` con `github_owner` (**`M43-08` KILLED**). **Sigue abierto:** `:156` aún hardcodea `"model-fast-01"` cuando `models_config_path is None` (`P43-2a`); `build_runtime` (`:464-478`) no valida `models.toml` vacío ni `"CONFIGURE_ME"` dentro de `[models]` (`P43-2b`); `cli.py:536-540` asigna `https://<owner>.github.io` aunque `deploy_target == "vercel"` (`P43-2b`) y `:120-124` activa modelo fuerte si un mensaje de usuario menciona `"mediation"`. |
| **H42-02** | **crítico** | **CERRADO** | `src/agent_factory/adapters/telegram_client.py:24-68`; `tests/test_cli_entrypoint.py:786-863` | `UrllibHttpTransport.post_json` ejercitado con `urlopen` interceptado offline: envía POST JSON UTF-8, `Content-Type` y headers `Authorization: Bearer` (**`M42-11` KILLED**), y envuelve `HTTPError`/`URLError`/`HTTPException`/`OSError` en `TelegramTransportError` limpiando `__cause__`/`__context__`. *(Nota de precisión: `post_json` tiene `100 %` de sentencias cubiertas pero conserva 2 ramas parciales `48->47` y `65->exit`; además `extra_secrets=extra` en `:61` sobrevive si se omite en `M43-14`.)* |
| **H42-03** | **alto** | **PARCIAL** | **Cerrado parcial:** `github_vcs_adapter.py:134-140, 249-300`; `static_deploy_publisher.py:231-232`. **Abierto:** `github_vcs_adapter.py:132, 134-140, 246, 251-272`; `static_deploy_publisher.py:234`; `P43-4a` | `merge_and_deploy` crea PR con `gh pr create` (**`M43-15` KILLED**), despliega vía `deploy_publisher.deploy_site(project_id, ws_dir)` sin pasar `branch="work"` (**`M43-02` KILLED**), `vercel` ya no expone `--token` en `argv` (**`M43-01` KILLED**) y `push_branch` hace `git add`/`commit` (**`M43-03` KILLED**). **Sigue abierto:** `push_branch` (`:135-140`) omite `-c user.name/email` e ignora `exit_code` de `git add`/`commit`, empujando un repo sin cambios reales en entorno limpio (`P43-4a`); `gh pr create` ignora `exit_code != 0`; `_last_workspace_dir` sigue volátil en memoria; y `gh pages deploy` se mantiene. |
| **H42-04** | **alto** | **PARCIAL** | **Cerrado parcial:** `git_registry_publisher.py:132-139`; `builder.py:458-462`; `telegram_bot.py:134-149`. **Abierto:** `telegram_bot.py:137-141`; `git_registry_publisher.py:200-212`; `orchestrator.py:466-478`; `P43-3` | `GitRegistryPublisher` aborta rebases conflictivos activos (`REBASE_HEAD`) y resetea el índice antes de `checkout` (**`M42-12_omit_active_rebase_abort` KILLED**); `BuildWorker` pasa `events=events` (**`M42-12_omit_worker_events` KILLED**); y `TelegramBotService` publica transiciones intermedias (**`M42-12_bot_registry_none` KILLED**). **Sigue abierto:** `telegram_bot.py:137-141` inspecciona `isinstance(..., GitRegistryPublisher)` y `orchestrator.publisher is not None`, anulando silenciosamente la publicación si se instancia el bot sin `orchestrator.publisher`; y `:200-212` de `git_registry_publisher.py` aún reescribe `registry.json` sin `_scrub_value`. |
| **H42-05** | **bajo** | **PARCIAL** | **Cerrado parcial:** `panel/styles.css:173-190`; `panel/app.js:171-174, 221-238`; `.github/workflows/ci.yml:35`; `secrets.py:47-84`. **Abierto:** `panel/app.js:34, 38`; `secrets.py:33-38`; `docs/DEBT.md:28-29`; `P43-5` | CSS incluye `.alert-warning`/`.prompt-box` (**`M43-05/05b` KILLED**); `app.js` valida contadores con `Number.isFinite` (**`M43-04` KILLED**); CI aplica `--cov-fail-under=85` (**`M43-06` KILLED**); `secrets.py` redacta `x-access-token:`, `?key=`, `vercel_`/`vcp_` y `LLM_API_KEY=` (**`M43-07/13` KILLED**). **Sigue abierto:** `app.js:38` altera texto legítimo quitando `\bNaN\b` (`P43-5`); `redactDisplayText` JS no cubre `vercel_`/`x-access-token:`/`?key=`; `secrets.py` con `env={}` no enmascara `<digits>:<tok>` sin prefijo `bot`; y CI sigue sin lockfile (`F-20`) ni Playwright/JS de plantilla/`panel/`. |

---

## 2. Hallazgos Nuevos / Regresiones Introducidos en `382e91e`

| ID | Severidad | Evidencia (`archivo:línea` + sonda/mutación) | Descripción exacta del problema |
|---|---|---|---|
| **H43-01** | **crítico** | `src/agent_factory/orchestrator.py:411`; `tests/test_phase4_e2e_offline.py:412, 628`; `MASTER_PROMPT.md:53`; `docs/DEBT.md:37`; `P43-1`, `M43-11` | **Violación directa de `INV-17` y `INV-06` (reinicio del circuit breaker tras ajuste):** `request_adjustment()` llama `self.circuit_breaker.reset(project_id)`, borrando los contadores acumulados de **todos** los estados (`INTERVIEW`, `DEBATE`, `MEDIATION`, `BUILDING`). Permite ciclos infinitos de ajuste y oculta el defecto de configuración del test `test_phase4_e2e_offline.py:412` (`DEBATE=3` para 4 llamadas) sin documentarlo en `docs/PHASE4_TEST_ISSUES.md`. `test_circuit_breaker.py:199` no detecta esta regresión al no pasar por `request_adjustment()`. |
| **H43-02** | **alto** | `src/agent_factory/cli.py:519`; `src/agent_factory/orchestrator.py:466-478`; `P43-3` | **`Orchestrator.get_status` rompe publicación best-effort (`INV-21`, `AC-3.6`):** `build_runtime` cablea `GitRegistryPublisher` como `Orchestrator.publisher`, pero `Orchestrator.get_status()` llama `self.publisher.publish(status)` sin capturar `RegistryPublishError`. Un fallo del repositorio remoto crashea `create_project()` (`P43-3`) y `/status`, además de emitir commits extra sin `events=` antes de `BuildWorker`. También `_last_published_status` (`:467-475`) omite `tests`, `agent_invocations` y `ci_rounds`. |
| **H43-03** | **alto** | `src/agent_factory/adapters/github_vcs_adapter.py:134-140`; `src/agent_factory/cli.py:536-540`; `P43-4a`, `P43-2b` | **Fallo silencioso de `git commit` en `push_branch` y `deploy_base_url` erróneo para Vercel:** `push_branch` no configura `-c user.name`/`user.email` ni verifica `exit_code` de `git add`/`git commit`; sin `.gitconfig` falla en silencio y empuja rama sin los archivos generados (`P43-4a`). Además, `build_runtime` asigna `https://<owner>.github.io` como `deploy_base_url` aun cuando `deploy_target == "vercel"` (`P43-2b`). *(Adicionalmente `SubprocessRunner` con `text=True` lanzó `UnicodeDecodeError` en hilo lector ante bytes CP1252 de `git` en `P43-4a`.)* |
| **H43-04** | **medio** | `panel/app.js:38`; `tests/test_panel_pwa.py:448, 528, 530`; `src/agent_factory/telegram_bot.py:137-141`; `P43-5`, `M43-10`, `M43-12` | **Workaround cosmético en `panel/app.js` y acoplamiento concreto en bot:** `redactDisplayText` borra `\bNaN\b` de cualquier cadena de usuario (`"NaN Check"` -> `" Check"`) únicamente porque `test_panel_pwa.py:448` nombra `"NaN Check"` al proyecto mientras afirma `"NaN" not in detailHtml`. Por su parte, `TelegramBotService._publish_status_to_registry` se acopla a `GitRegistryPublisher` y `orchestrator.publisher` para sortear `test_phase3_e2e_offline.py:436`. |

---

## 3. Verificación de `INV-13`, `ruff`, `pytest --cov` y Comparativa contra `docs/PHASE_REPORT_4.md`

### Comandos reales ejecutados (1 corrida cada uno en workspace limpio)
- **Integridad `INV-13` (`git diff phase-4.3-tests --stat -- tests/ templates/ pyproject.toml MASTER_PROMPT.md AGENTS.md`):** salida **vacía**, exit code `0`.
- **Linter (`.\.venv\Scripts\ruff.exe check .`):** `All checks passed!` (exit code `0`).
- **Marcadores `skip`/`xfail` en `tests/*.py`:** `0` marcadores activos en la búsqueda estática (`@pytest.mark.parametrize` únicamente).
- **Suite completa + cobertura (`.\.venv\Scripts\pytest.exe -q --cov=src/agent_factory --cov-report=term-missing`):**
  - Resultado real: **`337 passed, 3 warnings in 106.78s (0:01:46)`** (exit code `0`).
  - Cobertura global (`src/agent_factory`): **`91 %`** (`3011` stmts, `200` miss, `848` branch, `118` brpart).

### Comparativa exacta contra `docs/PHASE_REPORT_4.md`
- **Coincidencia de tabla numérica:** Las cifras globales (`337 passed`, `3011` stmts, `200` miss, `848` branch, `118` brpart -> `91 %`) y las filas de los 40 módulos no vacíos en `docs/PHASE_REPORT_4.md:106-154` coinciden exactamente con la salida real observada (variación normal de reloj: `106.78s` vs `109.49s` reportados).
- **Matiz de precisión en prosa:** `docs/PHASE_REPORT_4.md:60` indica *"UrllibHttpTransport.post_json alcanzado al 100% de cobertura"*; en `pytest-cov` tiene 100 % de **sentencias** (`24-68`), pero conserva 2 **ramas parciales** sin cubrir (`48->47` y `65->exit` en `telegram_client.py`).

### Líneas y ramas críticas sin cobertura observadas en Fase 4
- `src/agent_factory/cli.py` (**`83 %`**): `67`, `77` (`transport is None`), `84` (archivo faltante), `101->104`, `111`, `145-151`, `154-156` (fallback `"model-fast-01"`), `166-167`, `173->176`, `219-221`, `236-243`, `248-260`, `451`, `467-471`, `535->549`, `611-613`, `622->exit`, `651->653`, `682-685`.
- `src/agent_factory/adapters/github_vcs_adapter.py` (**`94 %`**): `101->104`, `130`, `207`, `213->229`, `270-272` (`except` en `gh pr create`), `279-280`.
- `src/agent_factory/adapters/git_registry_publisher.py` (**`89 %`**): `127-130`, `171->179`, `177-178`, `180->exit`, `188->195`, `191->195`, `193-194`, `197-198`, `207-208`, `291->296`, `293-294`, `297`, `300->302`, `343-344`, `354-355`, `370-371`, `390->404`, `393-394`, `395->404`, `405->408`.
- `src/agent_factory/adapters/static_deploy_publisher.py` (**`95 %`**): `160->163`, `253-255`.
- `src/agent_factory/adapters/telegram_client.py` (**`94 %`**): `48->47`, `65->exit`, `149`, `154`, `168->171`, `172->185`, `174->185`, `181->183`, `220->223`.
- `src/agent_factory/orchestrator.py` (**`94 %`**): `95`, `141`, `160`, `199-208`, `270`, `369`, `435`, `487`.
- `src/agent_factory/telegram_bot.py` (**`91 %`**): `84->88`, `88->92`, `97->93`, `104->exit`, `111->exit`, `145-149`, `181`, `215`, `247-248`, `256-257`, `295->298`, `304`, `331-332`, `385-392`, `448-449`, `502`, `511-512`, `520->552`, `538->540`, `540->542`, `595`, `648`.

---

## 4. Mutaciones y Sondas en Worktree Temporal (`%TEMP%\af_audit4_3_wt`)

Todas las mutaciones y sondas corrieron en un `git worktree` aislado (`%TEMP%\af_audit4_3_wt`, commit `382e91e`), verificando importación desde el worktree (`IMPORTED_FROM: ...\af_audit4_3_wt\src\agent_factory\__init__.py` con `PYTHONPATH` al worktree) y compilación `py_compile`. Al finalizar se ejecutó `git worktree remove --force`, `git worktree prune` y se comprobó `git worktree list` con único workspace y `git status --short` vacío.

### A. Mutaciones antes supervivientes de `AUDIT_REPORT_4_2.md` (`7/7` variantes KILLED)

| ID | Archivo y punto mutado | Descripción | Resultado | Evidencia real observada |
|---|---|---|---|---|
| **M42-10a** | `src/agent_factory/cli.py:158` | Forzar `chosen_model = "model-fast-01"` ignorando `models.toml` | **KILLED** | `1 failed, 7 passed in 1.02s` — `test_cli_entrypoint.py:698` (`assert 'model-fast-01' == 'acme-haiku-fast-2026'`) |
| **M42-10b** | `src/agent_factory/cli.py:126` | Desactivar rama `if is_mediation:` (nunca usa modelo `strong`) | **KILLED** | `1 failed, 7 passed in 1.77s` — `test_cli_entrypoint.py:726` (`assert 'acme-haiku-fast-2026' == 'acme-opus-mediation-2026'`) |
| **M42-10c** | `src/agent_factory/cli.py:162` | Omitir bloque `if schema is not None:` en `payload` | **KILLED** | `1 failed, 7 passed in 1.65s` — `test_cli_entrypoint.py:713` |
| **M42-11** | `src/agent_factory/adapters/telegram_client.py:37-38` | Omitir `req_headers.update(headers)` en `UrllibHttpTransport.post_json` | **KILLED** | `1 failed, 7 passed in 0.98s` — `test_cli_entrypoint.py::test_urllib_http_transport_post_json_sends_headers_json_and_wraps_http_url_errors` |
| **M42-12_BOT** | `src/agent_factory/telegram_bot.py:75` | Asignar `self.registry_publisher = None` en `__init__` | **KILLED** | `1 failed, 6 passed in 30.89s` — `test_phase4_audit31_fixes.py:743` |
| **M42-12_REBASE** | `src/agent_factory/adapters/git_registry_publisher.py:133-139` | Omitir `git rebase --abort` + `reset --hard HEAD` ante `REBASE_HEAD`/rebase activo | **KILLED** | `1 failed, 6 passed in 28.13s` — `test_phase4_audit31_fixes.py` (`RegistryPublishError: error: you need to resolve your current index first`) |
| **M42-12_EVENTS** | `src/agent_factory/builder.py:459-462` | Omitir `events=events` al invocar `publish_project(delivered_status)` | **KILLED** | `1 failed, 6 passed in 35.03s` — `test_phase4_audit31_fixes.py:764` (`assert 0 == 14`) |

### B. Mutaciones sobre ajustes de `382e91e` (`14` KILLED / `1` SURVIVED menor)

| ID | Archivo y punto mutado | Descripción de la mutación | Resultado | Evidencia real observada |
|---|---|---|---|---|
| **M43-01** | `static_deploy_publisher.py:232` | Restaurar `if self.deploy_token: cmd.extend(["--token", self.deploy_token])` en Vercel | **KILLED** | `1 failed, 5 passed in 2.39s` — `test_github_vcs_and_deploy_adapter.py:523` |
| **M43-02** | `github_vcs_adapter.py:298-300` | Pasar `branch=valid_branch` (`"work"`) a `deploy_publisher.deploy_site` | **KILLED** | `1 failed, 5 passed in 2.87s` — `test_github_vcs_and_deploy_adapter.py:509` (`assert 'work' == 'gh-pages'`) |
| **M43-03** | `github_vcs_adapter.py:134` | Omitir `git add -A` y `git commit` en `push_branch` | **KILLED** | `1 failed, 5 passed in 2.54s` — `test_github_vcs_and_deploy_adapter.py` |
| **M43-04** | `panel/app.js:171-174` | Reemplazar `toFiniteNonNegativeInt(val)` por `return Number(val || 0);` | **KILLED** | `1 failed, 5 passed in 0.90s` — `test_panel_pwa.py:523` |
| **M43-05** | `panel/styles.css:173` | Renombrar/quitar regla `.alert-warning` | **KILLED** | `1 failed, 5 passed in 0.67s` — `test_panel_pwa.py:393` |
| **M43-05b** | `panel/styles.css:181` | Renombrar/quitar regla `.prompt-box` | **KILLED** | `1 failed, 5 passed in 0.51s` — `test_panel_pwa.py:394` |
| **M43-06** | `.github/workflows/ci.yml:35` | Quitar `--cov-fail-under=85` de CI | **KILLED** | `1 failed, 5 passed in 0.44s` — `test_panel_pwa.py:397` |
| **M43-07** | `src/agent_factory/secrets.py:57` | Quitar `|key` del patrón de parámetros de consulta | **KILLED** | `1 failed, 5 passed in 0.33s` — `test_secret_sanitization.py:524` |
| **M43-08** | `src/agent_factory/cli.py:544` | Pasar `deploy_base_url=""` a `StaticDeployPublisher` | **KILLED** | `1 failed, 7 passed in 0.94s` — `test_cli_entrypoint.py:781` |
| **M43-09** | `src/agent_factory/cli.py:519` | Revertir `publisher=effective_registry` a `publisher=None` en `Orchestrator` | **KILLED** | `1 failed, 7 passed in 0.76s` — `test_cli_entrypoint.py:777` |
| **M43-10** | `panel/app.js:38` | Eliminar `.replace(/\bNaN\b/g, "")` de `redactDisplayText` | **KILLED** | `1 failed, 5 passed in 0.66s` — `test_panel_pwa.py:528` (expone dependencia del test sobre este atajo) |
| **M43-11** | `src/agent_factory/orchestrator.py:411` | Eliminar `self.circuit_breaker.reset(project_id)` para **cumplir `INV-17`** | **KILLED** | `1 failed, 11 passed in 23.03s` — `test_phase4_e2e_offline.py:628` (`AssertionError: assert 'FAILED' == 'DELIVERED'`) |
| **M43-12** | `src/agent_factory/telegram_bot.py:141` | Publicar siempre si `registry_publisher is not None` sin revisar `is_real_git`/`orch_has_pub` | **KILLED** | `1 failed, 9 passed in 46.08s` — `test_phase3_e2e_offline.py:436` (`'refs/heads/registry'` presente en build fallido) |
| **M43-13** | `src/agent_factory/secrets.py:73` | Quitar prefijos `|vercel_...|vcp_...` | **KILLED** | `1 failed, 5 passed in 0.41s` — `test_secret_sanitization.py:524` |
| **M43-15** | `src/agent_factory/adapters/github_vcs_adapter.py:249` | Omitir `gh pr create` en `merge_and_deploy` | **KILLED** | `1 failed, 5 passed in 2.53s` — `test_github_vcs_and_deploy_adapter.py:503` |
| **M43-14** | `src/agent_factory/adapters/telegram_client.py:61` | Pasar `extra_secrets=()` en lugar de `extra_secrets=extra` en `UrllibHttpTransport.post_json` | **SURVIVED** | `8 passed in 0.92s` (`test_cli_entrypoint.py`) — `HTTPError.__str__` no incluye cuerpo `fp`; si falla un header no-Bearer ecoeado en error real, `extra_secrets=extra` no está probado |

### C. Sondas directas ejecutadas sobre `382e91e`
- **`P43-1` (`orchestrator.py:411`):** Tras 1 ronda (`DEBATE=2, MEDIATION=1, INTERVIEW=1`), `orch.request_adjustment(...)` reinicia todos los contadores a `DEBATE=0, MEDIATION=0, INTERVIEW=0`; la 2.ª ronda acumula `4` llamadas `DEBATE` frente a límite `3` y termina en **`WAITING_APPROVAL`** en lugar de **`FAILED`**.
- **`P43-2a` (`cli.py:156`):** `ProductionLLMAdapter(api_key='k', models_config_path=None, transport=tr)` envía `"model": "model-fast-01"`.
- **`P43-2b` (`cli.py:464-478, 536-540`):** `build_runtime(cfg)` pasa sin error tanto con `models.toml` vacío (`""`) como con `[models]\nfast = "CONFIGURE_ME"\nstrong = "CONFIGURE_ME"`. Con `deploy_target="vercel"` y `github_owner="gabriel-owner"`, construye `StaticDeployPublisher` con `deploy_base_url="https://gabriel-owner.github.io"`.
- **`P43-3` (`cli.py:519` + `orchestrator.py:478`):** Con `AGENT_FACTORY_REGISTRY_REPO` inexistente/fallando en `build_runtime`, `orchestrator.create_project(...)` colapsa lanzando `RegistryPublishError`.
- **`P43-4a` (`github_vcs_adapter.py:134-140`):** En workspace git limpio sin `.gitconfig` global, `push_branch` retorna `"branch 'work' set up to track 'origin/work'."` mientras `git ls-tree -r refs/heads/work --name-only` en el remoto devuelve `''` (archivo no commiteado ni empujado).
- **`P43-5` (`panel/app.js:34, 38`):** `redactDisplayText('NaN Check')` devuelve `' Check'`, y `redactDisplayText('vcp_ABCDEF123456 ?key=MYSECRET')` devuelve la cadena sin redactar.

---

## 5. Cumplimiento Global de `MASTER_PROMPT.md` (v1.2.1) e Invariantes (`INV-01` a `INV-23`)

### Cumplimiento por secciones del documento rector
- **§2 Decisiones de diseño (`MASTER_PROMPT.md:31`):** *"Nombres de modelos nunca van en el código: se leen de `config/models.toml` por estado (uno fuerte para `MEDIATION`, uno rápido para el resto)"* -> **PARCIAL / NO CUMPLE totalmente**: `[models]` ya se lee y conmuta entre `MEDIATION` (`strong`) y el resto (`fast`), pero `cli.py:156` mantiene `"model-fast-01"` cuando `models_config_path=None` y `build_runtime` no valida `[models]` contra `"CONFIGURE_ME"` ni archivo vacío. Arquitectura local-first, Telegram long polling sin backend y PWA estática `$0` -> **CUMPLE en diseño**.
- **§3–§7 Fases 0–4:** Fases 0–3 implementaron dominio, máquina de estados, builder y bot offline; Fase 4 cierra transporte HTTP real de Telegram/LLM, recuperación de rebase activo, eventos en `projects/<id>.json` y reglas PWA/CI, pero la regresión **`H43-01` (`INV-17`)** y los pendientes **`H42-01`/`H42-03`/`H43-02`/`H43-03`** bloquean el sello operativo final. Fase 5 (`CHANGE_REQUEST`) diferida conforme al alcance.

| Invariante | Estado | Evidencia / Dictamen estricto |
|---|:---:|---|
| **INV-01** | **parcial** | `337 passed`, pero falta cobertura sobre fallo `git commit` en `push_branch`, `build_runtime` con TOML vacío/`[models] CONFIGURE_ME`, y `tests/test_phase4_e2e_offline.py:412` induce incumplimiento de `INV-17`. |
| **INV-02** | **cumple (offline)** | Puertos `LLM`, `HttpTransport`, `Runner`, `VCS`, `Clock`, `EventStore`, `RegistryPublisher` respetados; `UrllibHttpTransport.post_json` ya ejercitado (`M42-11` KILLED). *(Observación menor de diseño: `telegram_bot.py:137` importa `GitRegistryPublisher`.)* |
| **INV-03** | **cumple** | `--disable-socket` en `pyproject.toml:33`; `337 passed` 100 % offline y sin secretos reales. |
| **INV-04** | **cumple** | `validate_transition` y validación de estado origen mantenidos en `domain/states.py`, `orchestrator.py` y `telegram_bot.py`. |
| **INV-05** | **cumple** | Compuerta `ApprovalEvent(decision="approved")` con `plan_hash` vigente en `WAITING_APPROVAL` verificada en `orchestrator.py:360-398`. |
| **INV-06** | **PARCIAL / ROTO en ajuste** | `builder.py` acota rondas CI/invocaciones y `run_polling_loop` usa backoff con `event.wait`, pero `orchestrator.py:411` (`H43-01`) permite reiniciar el breaker indefinidamente vía `request_adjustment`. |
| **INV-07** | **cumple (repo) / parcial (runtime JS)** | Sin secretos en repo, `secrets.py` cubre `x-access-token:`, `vercel_`, `vcp_`, `LLM_API_KEY=`, `?key=`, y `vercel` ya no expone `--token` en `argv`; sigue abierto `redactDisplayText` en `panel/app.js:34`, `<digits>:<tok>` sin `bot` en `env={}` y `:200-212` de `git_registry_publisher.py`. |
| **INV-08** | **cumple** | `pyproject.toml` intacto con versiones fijadas `==` (deuda `F-20` de lockfile transitivo abierta en `docs/DEBT.md`). |
| **INV-09** | **cumple** | `ProductionLLMAdapter.complete()` ya valida JSON estructurado con `schema`, sanea errores HTTP/TOML y limpia `__cause__`/`__context__`. |
| **INV-10** | **cumple** | `docs/PHASE_REPORT_4.md` actualizado con las métricas reales exactas de `337 passed` y tabla de 40 módulos coincidente (matiz menor en prosa sobre ramas parciales de `post_json`). |
| **INV-11** | **cumple** | `project_status_from_events` reconstruye estado validando cada transición y `BuildWorker` ya persiste `events` sanitizados en `projects/<id>.json`. |
| **INV-12** | **cumple** | `calculate_progress` determinista en `domain/progress.py`. |
| **INV-13** | **cumple (suite) / parcial (CI plantilla)** | Inmutabilidad de `tests/`, `templates/`, `pyproject.toml`, `MASTER_PROMPT.md` y `AGENTS.md` intacta; `.github/workflows/ci.yml` ya exige `--cov-fail-under=85`, aunque sigue sin ejecutar Playwright/JS de `templates/static-web/` ni `panel/`. |
| **INV-14** | **no verificable** | `CHANGE_REQUEST` diferido a Fase 5 (`MASTER_PROMPT.md:186-191`). |
| **INV-15** | **parcial** | Máximo 3 rondas CI cumplido; `push_branch` (`github_vcs_adapter.py:134-140`) ya hace `git add`/`commit` pero falla en silencio sin identidad git (`P43-4a`). |
| **INV-16** | **cumple** | Idempotencia persistente SQLite en Telegram y panel PWA 100 % estático. |
| **INV-17** | **NO CUMPLE** | **Regresión directa en `orchestrator.py:411` (`H43-01` / `P43-1`):** `request_adjustment` invoca `self.circuit_breaker.reset(project_id)`, violando *"nunca se reinician al volver a entrar a un estado (p. ej. tras un ajuste)"* (`MASTER_PROMPT.md:53`). |
| **INV-18** | **cumple** | Aislamiento en `workspaces/<id>/`, entornos limpios y validación de rutas resueltas. |
| **INV-19** | **cumple (local offline) / parcial (E2E real)** | `vercel --token` eliminado de `argv`; ejecución real de `agy`/APIs externas queda fuera de la suite offline. |
| **INV-20** | **cumple** | `templates/static-web/` y `TemplateScaffold` verificados. |
| **INV-21** | **parcial** | `GitRegistryPublisher` recupera rebases conflictivos activos (`REBASE_HEAD`), pero `Orchestrator.get_status` (`cli.py:519` + `orchestrator.py:478`) crashea ante fallo de registro (`P43-3`) y `build_runtime` deja pasar `models.toml` vacío o con `[models] CONFIGURE_ME` (`P43-2b`). |
| **INV-22** | **cumple** | Validación estricta de `project_id`, ramas insensibles a mayúsculas, caracteres especiales y contención de rutas. |
| **INV-23** | **parcial** | Las mutaciones `M42-10`..`M42-12` y 15/16 mutaciones nuevas mueren; sin embargo persisten `test_phase4_e2e_offline.py:412` (presupuesto `DEBATE=3` inconsistente con `INV-17`), `test_panel_pwa.py:448` (`"NaN Check"` anclando `.replace(/\bNaN\b/g, "")` en `app.js:38`) y `M43-14` superviviente. |

---

## 6. Lo que NO se pudo verificar y por qué

1. **Runtime real de navegador y Service Worker (`panel/sw.js`, `panel/index.html`, `panel/styles.css`):**
   - `tests/test_panel_pwa.py` valida `app.js` con mocks de DOM/`fetch` bajo `deno` (`node` no está instalado localmente en este entorno Windows) y revisa CSS por regex; no se ejecutó Chromium/Playwright real contra el PWA ni se probó instalación/caché offline de `sw.js`.
2. **Red real y CLIs externos (`UrllibHttpTransport` hacia TLS real, API LLM, `api.telegram.org`, `gh`, `vercel`, `git push` remoto, `agy`):**
   - `pytest-socket` bloquea todo socket de red (`INV-03`) y no existen credenciales de producción; `UrllibHttpTransport.post_json` se probó interceptando `urllib.request.urlopen`, y `gh`/`vercel`/`agy` mediante `FakeRunner` o scripts locales simulados. En particular, no se comprobó si el subcomando configurado `gh pages deploy` existe/funciona en el CLI `gh` instalado en producción.
3. **Suite JS/Playwright de la plantilla (`templates/static-web/`) y workflow real de GitHub Actions:**
   - `.github/workflows/ci.yml` sigue ejecutando únicamente Python (`ruff` + `pytest --cov --cov-fail-under=85`); tampoco se ejecutó GitHub Actions en la nube ni `npm test` / Playwright sobre `templates/static-web/`.
4. **Demón vivo ante señales reales `SIGINT`/`SIGTERM` del SO en Windows:**
   - En `test_cli_entrypoint.py`, `main()` se probó con `stop_event` ya marcado o fallo de configuración, no como proceso residente recibiendo `CTRL_C_EVENT` real durante espera de red.
5. **Fase 5 (`CHANGE_REQUEST`, `INV-14`):**
   - Fuera del alcance de v1 / Fase 4 según `MASTER_PROMPT.md:72, 186-191`.
