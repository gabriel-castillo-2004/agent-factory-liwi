# AUDIT_REPORT_4_4 — Re-auditoría Final de Cierre de Fase 4 y Proyecto Completo

Auditor independiente (modo solo lectura). Rama `phase-4`, commit `68cf415` auditado contra el tag `phase-4.4-tests` (`c1f2c19`), `docs/AUDIT_REPORT_4_3.md`, `docs/PHASE_REPORT_4.md`, `docs/PHASE4_TEST_MAP.md` y `MASTER_PROMPT.md` (v1.2.1).  
Documentos y código inspeccionados: `MASTER_PROMPT.md` (v1.2.1), `AGENTS.md`, `docs/decisions/ADR-004-pwa-panel-and-cli-entrypoint.md`, `docs/PHASE_REPORT_4.md`, `docs/AUDIT_REPORT_4_3.md`, `docs/PHASE4_TEST_MAP.md`, `docs/DEBT.md`, `git diff phase-4.4-tests...HEAD`, `pyproject.toml`, `config/models.toml`, `.github/workflows/ci.yml`, `templates/static-web/`, `panel/app.js`, `panel/styles.css`, `src/agent_factory/` y `tests/`.

---

## Veredicto: **APTO CON CORRECCIONES** *(cierre de invariantes críticos `INV-17` / `H43-01` y muerte de `M43-11`, `M43-14`, `M43-10b` y 8/9 mutaciones nuevas; permanecen casos borde reproducidos, desfase menor en `PHASE_REPORT_4.md` y 2/3 corridas con timeout de 30 s en Windows)*

### Dictamen objetivo y sin suavizar
El commit `68cf415` resuelve los bloqueos graves que motivaron el rechazo en `AUDIT_REPORT_4_3.md`:
- **Violación `INV-17` eliminada (`H43-01` CERRADO):** `Orchestrator.request_adjustment()` (`src/agent_factory/orchestrator.py:400-423`) ya no reinicia el `CircuitBreaker`. La mutación `M43-11` muere tanto en unitarias (`test_circuit_breaker.py:269`) como en E2E (`test_phase4_e2e_offline.py:591`).
- **`M43-14` CERRADO / KILLED:** `UrllibHttpTransport.post_json()` (`src/agent_factory/adapters/telegram_client.py:24-69`, protocolo `src/agent_factory/ports/notifier.py:52-61`) acepta `extra_secrets` y redacta secretos explícitos junto con valores de cabeceras (`test_cli_entrypoint.py:882`).
- **`M43-10` / parche `\bNaN\b` corregido:** `panel/app.js:28-38` ya no borra `\bNaN\b` de textos legítimos; al reintroducir esa expresión regular (`M43-10b`), `test_panel_pwa.py:535` la detecta y falla.
- **`INV-13` intacto:** `git diff phase-4.4-tests --stat -- tests/ templates/ .github/ pyproject.toml MASTER_PROMPT.md AGENTS.md` es vacío; `ruff check .` pasa sin observaciones (`All checks passed!`); la corrida verde logra **`341 passed`** con **`91 %`** de cobertura global.

**Razones estrictas por las que el veredicto es `APTO CON CORRECCIONES` (y no `APTO` limpio):**
1. **Inestabilidad temporal (`H44-01`) bajo `timeout = 30` en Windows:** De 3 ejecuciones completas de `pytest -q --cov=src/agent_factory --cov-report=term-missing` sobre el commit `68cf415`, las 2 primeras expiraron por `Timeout (>30 s)` en pruebas que encadenan decenas de subprocesos `git.exe` reales (`test_git_registry_publisher_recovers_from_active_conflicted_rebase_and_wires_events_and_bot_registry` en corrida 1; `test_phase4_e2e_bot_and_build_worker_stream_state_and_sanitized_events_to_registry:629` en corrida 2 sin procesos concurrentes). La 3.ª corrida pasó en **`190.15s (0:03:10)`** (superando también el umbral operativo de 120 s por comando). El cableado simultáneo de `Orchestrator.publisher` + `TelegramBotService._publish_status_to_registry` + `BuildWorker` multiplica llamadas redundantes de red/git por cada transición.
2. **Heurísticas frágiles en casos borde (`H44-03`, `H44-04`, `H42-01`):**
   - `GitHubVCSAdapter.push_branch()` (`github_vcs_adapter.py:142-158`) falla con `VcsError('Git commit failed: exit code 128')` si `.git/config` contiene solo `user.name` sin `email` o un comentario `# user.name` (`P44-1`); además `:135` ignora `exit_code != 0` de `git add -A` (`P44-2`); y `SubprocessRunner` (`subprocess_runner.py:41`) sufre `UnicodeDecodeError` en `_readerthread` ante mensajes CP1252 de `git` en Windows, perdiendo el texto de `stderr`.
   - `Orchestrator.get_status()` (`orchestrator.py:475-480`) guarda `_last_published_status[project_id] = last_key` **antes** del `try`, impidiendo reintentar ese estado si falla una vez de forma transitoria (`P44-4`).
   - `build_runtime()` acepta `[models]\nfast = ""` y `[models]\nfoo = "bar"` (`P44-3a`), `ProductionLLMAdapter.complete()` sigue enrutando a `strong` si un mensaje `role: "user"` dice `"mediation"` (`P44-3b`), y `StaticDeployPublisher` devuelve `.pages.dev` ante salida multilínea de `vercel` (`P44-5`).
3. **Desfase menor de métricas en `docs/PHASE_REPORT_4.md` (`H44-02`):** El reporte reporta `cli.py` en `85%` (`43` miss, `17` brpart) y total `208` miss / `117` brpart en `~125s`, frente a `cli.py` en **`84%`** (`47` miss, `18` brpart — faltan `512` y `648-650`) y total `212` miss / `118` brpart en **`190.15s`** medidos en esta auditoría.

---

## 1. Tabla de Cierre de Hallazgos (`AUDIT_REPORT_4_3.md` y Nuevos)

| ID | Severidad | Estado | Evidencia exacta (`archivo:línea` + sonda/mutación) | Dictamen técnico |
|---|---|---|---|---|
| **H43-01** | **crítico** | **CERRADO** | `src/agent_factory/orchestrator.py:400-423`; `tests/test_circuit_breaker.py:237-282`; `tests/test_phase4_e2e_offline.py:412, 591-593, 612-614`; `M43-11` **KILLED** | `self.circuit_breaker.reset(project_id)` eliminado de `request_adjustment()`. Contadores acumulativos preservados (`INTERVIEW=1, DEBATE=2, MEDIATION=1` tras ajuste; 2.ª ronda de debate dispara `FAILED` al superar `DEBATE=3`). |
| **M43-14** | **medio** | **CERRADO** | `src/agent_factory/adapters/telegram_client.py:24-69`; `src/agent_factory/ports/notifier.py:52-61`; `tests/test_cli_entrypoint.py:865-886`; `M43-14` **KILLED** | `UrllibHttpTransport.post_json` acepta `extra_secrets: Sequence[str] = ()` y redacta secretos explícitos y encabezados en `TelegramTransportError`. |
| **H43-02** | **alto** | **PARCIAL** | **Cerrado:** `src/agent_factory/orchestrator.py:477-480`; `tests/test_phase4_audit31_fixes.py:782-825`; `M44-06` **KILLED**. **Abierto:** `orchestrator.py:466-476`, sonda `P44-4` | `get_status()` captura `(RegistryPublishError, OSError, RuntimeError, ValueError)` permitiendo `create_project()` y consultas cuando falla el remoto. Sin embargo `:476` actualiza `_last_published_status` **antes** del `try`, bloqueando reintentos posteriores del mismo estado tras un fallo transitorio (`P44-4`), y omite `tests`/`agent_invocations`/`ci_rounds`. |
| **H42-01** | **crítico** | **PARCIAL** | **Cerrado:** `src/agent_factory/cli.py:82-83, 473-514, 573-577`; `tests/test_cli_entrypoint.py:889-984`; `M44-01/03/04/05` **KILLED**. **Abierto:** `cli.py:122-126, 478-514`; `M44-02` **SURVIVED (equiv.)**; `P44-3a`, `P44-3b` | `models_config_path=None` ya falla con `LLMError` (sin `"model-fast-01"`); `build_runtime` rechaza vacío, sin `[models]`/claves, `[models]` vacío y `[models] CONFIGURE_ME`; Vercel ya no recibe `*.github.io`. Siguen abiertos: `[models]\nfast=""` o `foo="bar"` aceptados en `build_runtime` (`P44-3a`), `role:"user"` con `"mediation"` enruta a `strong` (`P44-3b`), y rama `:510-514` queda redundante respecto a `:504-508`. |
| **H42-03 / H43-03** | **alto** | **PARCIAL** | **Cerrado:** `src/agent_factory/adapters/github_vcs_adapter.py:136-185`; `tests/test_github_vcs_and_deploy_adapter.py:529-642`; `M44-07/08/09` **KILLED**. **Abierto:** `github_vcs_adapter.py:91, 135, 142-158, 291, 296-317`; `static_deploy_publisher.py:234, 251-255`; `P44-1`, `P44-2`, `P44-5` | `push_branch` pasa `-c user.name`/`user.email` cuando no detecta identidad local y valida `commit_res.exit_code != 0` lanzando `VcsError` sanitizado. Siguen abiertos: heurística `has_local_identity` falla si `.git/config` tiene solo `name` sin `email` o un comentario `# user.name` (`P44-1`); `git add -A` ignora `exit_code != 0` (`P44-2`); `gh pr create` silencia errores; `_last_workspace_dir` es volátil en memoria; y salida multilínea de `vercel` retorna `.pages.dev` (`P44-5`). |
| **H43-04** | **medio** | **PARCIAL** | **Cerrado en JS:** `panel/app.js:28-38`; `tests/test_panel_pwa.py:534-536`; `M43-10b` **KILLED**. **Abierto:** `src/agent_factory/telegram_bot.py:137-141`; `panel/app.js:34-37`; `P44-6` | `redactDisplayText("NaN Check")` preserva `"NaN Check"` eliminando el parche `\bNaN\b`. Persisten: acoplamiento concreto de `TelegramBotService._publish_status_to_registry` a `GitRegistryPublisher` y `orchestrator.publisher`, y ausencia de patrones `vercel_`/`vcp_`/`?key=` en `redactDisplayText` JS (`P44-6`). |

### Hallazgos Nuevos Registrados en esta Auditoría

| ID | Severidad | Evidencia exacta (`archivo:línea`) | Descripción |
|---|---|---|---|
| **H44-01** | **alto** | `pyproject.toml:31` (`timeout = 30`); `tests/test_phase4_audit31_fixes.py:595-779`; `tests/test_phase4_e2e_offline.py:548-649`; `cli.py:551-557`; `telegram_bot.py:134-149` | **Flakiness por timeout de 30 s en pruebas con `git.exe` bajo `--cov` en Windows:** 2 de 3 corridas completas expiraron por `pytest-timeout` (corrida 1 en `test_git_registry_publisher_recovers_from_active_conflicted_rebase_and_wires_events_and_bot_registry`, corrida 2 en `test_phase4_e2e_bot_and_build_worker_stream_state_and_sanitized_events_to_registry:629`). La corrida 3 pasó en `190.15s (0:03:10)`. |
| **H44-02** | **bajo** | `docs/PHASE_REPORT_4.md:114, 121, 143` vs salida real `pytest --cov` (`cli.py:512, 648-650`) | **Desfase puntual en métricas de `PHASE_REPORT_4.md`:** Reporta `cli.py` = `85 %` (`43` miss, `17` brpart), total = `208` miss / `117` brpart y `~125s`, frente a los medidos en esta sesión (`cli.py` = `84 %`, `47` miss, `18` brpart, total = `212` miss / `118` brpart en `190.15s`). |
| **H44-03** | **medio** | `src/agent_factory/adapters/github_vcs_adapter.py:135, 142-158`; `src/agent_factory/adapters/subprocess_runner.py:41`; sondas `P44-1`, `P44-2` | **Fallo de `push_branch` ante `.git/config` parcial (`name` sin `email` o comentario `# user.name`), `git add` sin check de `exit_code`, y `UnicodeDecodeError` en `SubprocessRunner`:** `SubprocessRunner(..., text=True)` falla en hilo lector de `subprocess.Popen` ante bytes CP1252 de `git` en español en Windows (visto también como `PytestUnhandledThreadExceptionWarning` en `pytest`), ocultando el mensaje de error real tras `exit code 128`. |
| **H44-04** | **medio** | `src/agent_factory/orchestrator.py:475-480`; sonda `P44-4` | **Cache prematura de `_last_published_status`:** Asignar `self._last_published_status[project_id] = last_key` antes de `self.publisher.publish(status)` bloquea futuros reintentos de publicación del mismo estado si ocurrió un error transitorio. |

---

## 2. Verificación de `INV-13`, `ruff`, `pytest --cov` y Comparativa con `docs/PHASE_REPORT_4.md`

### Resultados literales ejecutados
1. **Inmutabilidad `INV-13`:**
   - Comando: `git diff phase-4.4-tests --stat -- tests/ templates/ .github/ pyproject.toml MASTER_PROMPT.md AGENTS.md`
   - Resultado: salida **completamente vacía**, exit code `0`.
   - Marcadores en `tests/*.py`: `0` marcadores `skip`/`xfail` (`Select-String` mostró exclusivamente `@pytest.mark.parametrize`).
2. **Linter (`ruff`):**
   - Comando: `.\.venv\Scripts\ruff.exe check .`
   - Resultado: `All checks passed!`, exit code `0`.
3. **Suite completa + cobertura (`.\.venv\Scripts\pytest.exe -q --cov=src/agent_factory --cov-report=term-missing`):**
   - **Intento 1** (concurrente con creación de worktree): Timeout (>30 s) en `tests/test_phase4_audit31_fixes.py::test_git_registry_publisher_recovers_from_active_conflicted_rebase_and_wires_events_and_bot_registry` (exit code `1`).
   - **Intento 2** (aislado sin concurrencia): Timeout (>30 s) en `tests/test_phase4_e2e_offline.py::test_phase4_e2e_bot_and_build_worker_stream_state_and_sanitized_events_to_registry:629` durante `git fetch` (exit code `1`).
   - **Intento 3** (aislado sin concurrencia): **`341 passed, 3 warnings in 190.15s (0:03:10)`** (exit code `0`).
   - **Cobertura global (`src/agent_factory`):** **`91%`** (`3055` Stmts, `212` Miss, `870` Branch, `118` BrPart).

### Comparativa exacta contra `docs/PHASE_REPORT_4.md`
- **Coincidencias:** El conteo de tests (`341 passed`), el total de sentencias (`3055`), el total de ramas (`870`), la cobertura global (`91%`) y las cifras exactas de **39 de los 40 módulos** no vacíos en `docs/PHASE_REPORT_4.md:121-161` coinciden exactamente con la corrida real.
- **Discrepancias observadas en la corrida real (`H44-02`):**
  - `docs/PHASE_REPORT_4.md:114`: reporta `~125s` vs **`190.15s (0:03:10)`** real.
  - `docs/PHASE_REPORT_4.md:121`: reporta `208` miss y `117` brpart vs **`212`** miss y **`118`** brpart reales.
  - `docs/PHASE_REPORT_4.md:143` (`src/agent_factory/cli.py`): reporta `380 | 43 | 126 | 17 | 85%` vs **`380 | 47 | 126 | 18 | 84%`** reales (`Missing: 67, 77, 86, 103->106, 113, 147-153, 156, 166-167, 173->176, 219-221, 236-243, 248-260, 451, 467-471, 512, 572->586, 648-650, 659->exit, 688->690, 719-722`).

---

## 3. Resultado de Mutaciones y Sondas en Worktree Temporal (`%TEMP%\af_audit4_4_wt`)

Todas las mutaciones y sondas corrieron en el `git worktree` temporal aislado en `%TEMP%\af_audit4_4_wt` (commit `68cf415`), verificando importación desde dicho worktree (`IMPORTED_FROM: ...\af_audit4_4_wt\src\agent_factory\__init__.py`), diff no vacío y compilación `py_compile`. Al finalizar se ejecutó `git worktree remove --force` y `git worktree prune`, confirmando `git worktree list` con único workspace principal.

### A. Re-verificación de mutaciones antes supervivientes / en disputa

| ID | Archivo y punto mutado | Descripción | Resultado | Evidencia real observada |
|---|---|---|---|---|
| **M43-11 (unit)** | `src/agent_factory/orchestrator.py:410-411` | Reintroducir `self.circuit_breaker.reset(project_id)` en `request_adjustment` | **KILLED** | `1 failed, 9 passed in 0.96s` — `tests/test_circuit_breaker.py:269` (`assert 0 == 1`) |
| **M43-11 (e2e)** | `src/agent_factory/orchestrator.py:410-411` | Reintroducir `self.circuit_breaker.reset(project_id)` en `request_adjustment` | **KILLED** | `1 failed, 2 deselected in 14.57s` — `tests/test_phase4_e2e_offline.py:591` (`assert 0 == 1`) |
| **M43-14** | `src/agent_factory/adapters/telegram_client.py:62` | Pasar `extra_secrets=()` en lugar de `extra_secrets=extra` en `UrllibHttpTransport.post_json` | **KILLED** | `1 failed, 8 passed in 3.12s` — `tests/test_cli_entrypoint.py:882` (`assert 'CUSTOM_SECRET_XYZ' not in ...`) |
| **M43-10b** *(inversa)* | `panel/app.js:37-38` | Nota: `\bNaN\b` ya fue borrado de `panel/app.js` en `68cf415`. Al reintroducir `.replace(/\bNaN\b/g, "")`, la prueba rechaza la regresión | **KILLED** | `1 failed, 5 passed, 1 warning in 1.64s` — `tests/test_panel_pwa.py:535` (`- NaN Check` / `+  Check`) |

### B. Mutaciones nuevas sobre los ajustes de `68cf415` (`8` KILLED / `1` SURVIVED equivalente)

| ID | Archivo y punto mutado | Descripción | Resultado | Evidencia real observada |
|---|---|---|---|---|
| **M44-01** | `src/agent_factory/cli.py:82-83` | Restaurar fallback `"model-fast-01"` cuando `models_config_path is None` | **KILLED** | `1 failed, 8 passed in 1.56s` — `tests/test_cli_entrypoint.py:914` (`DID NOT RAISE any of (LLMError, ValueError)`) |
| **M44-02** | `src/agent_factory/cli.py:473-476` | Quitar chequeo `or not models_data` en `build_runtime` | **SURVIVED (equivalente)** | `9 passed in 1.20s` — `cli.py:498` (`not any(k.lower() in known_keys for k in models_data)`) igual rechaza `{}` |
| **M44-03** | `src/agent_factory/cli.py:480-481` | Quitar `if not models_section: raise CliConfigError(...)` | **KILLED** | `1 failed, 8 passed in 2.48s` — `tests/test_cli_entrypoint.py:952` (`DID NOT RAISE CliConfigError`) |
| **M44-04** | `src/agent_factory/cli.py:504-508` | Omitir verificación de `CONFIGURE_ME` sobre `models_cfg` (`[models]`) | **KILLED** | `1 failed, 8 passed in 1.28s` — `tests/test_cli_entrypoint.py:952` (`DID NOT RAISE CliConfigError`) |
| **M44-05** | `src/agent_factory/cli.py:575` | Restaurar `or bool(config.github_owner)` para `deploy_base_url` | **KILLED** | `1 failed, 8 passed in 1.23s` — `tests/test_cli_entrypoint.py:982` (`'github.io' is contained here: https://gabriel-vercel-owner.github.io`) |
| **M44-06** | `src/agent_factory/orchestrator.py:477-480` | Quitar `try/except (RegistryPublishError, ...)` en `get_status` | **KILLED** | `1 failed, 7 deselected in 0.74s` — `test_orchestrator_create_project_survives_registry_publish_error_best_effort` (`RegistryPublishError`) |
| **M44-07** | `src/agent_factory/adapters/github_vcs_adapter.py:162-165` | Quitar `-c user.name=AgentFactory` y `-c user.email=...` en `push_branch` | **KILLED** | `1 failed, 6 passed, 1 warning in 6.29s` — `tests/test_github_vcs_and_deploy_adapter.py` |
| **M44-08** | `src/agent_factory/adapters/github_vcs_adapter.py:179-185` | Quitar chequeo `if commit_res.exit_code != 0: raise VcsError` | **KILLED** | `1 failed, 6 passed in 4.72s` — `tests/test_github_vcs_and_deploy_adapter.py:637` (`DID NOT RAISE VcsError`) |
| **M44-09** | `src/agent_factory/adapters/github_vcs_adapter.py:142` | Forzar `has_local_identity = True` siempre en `push_branch` | **KILLED** | `1 failed, 6 passed, 1 warning in 5.14s` — `tests/test_github_vcs_and_deploy_adapter.py` |

### C. Sondas directas ejecutadas sobre `68cf415` en el worktree
- **`P44-1` (`github_vcs_adapter.py:142-158` + `subprocess_runner.py:41`):** En repos git sin `.gitconfig` global cuyo `.git/config` tiene solo `user.name = OnlyName` (sin `email`) o el comentario `# user.name not configured`, `push_branch` falla con `VcsError('Git commit failed: exit code 128')` y genera `UnicodeDecodeError` en `_readerthread` de `SubprocessRunner`.
- **`P44-2` (`github_vcs_adapter.py:135`):** Con `git add -A` retornando `exit_code = 128` y `git diff --cached --quiet` retornando `0`, `push_branch` ejecuta `git push` sin error y retorna `'pushed stale'`.
- **`P44-3a` (`cli.py:478-514`):** `build_runtime(cfg)` acepta sin `CliConfigError` archivos `[models]\nfast = ""\nstrong = "s1"` y `[models]\nfoo = "bar"`.
- **`P44-3b` (`cli.py:122-126`):** `ProductionLLMAdapter.complete()` con `{"role": "user", "content": "App about legal mediation"}` y `schema=None` selecciona `"strong-m"`.
- **`P44-4` (`orchestrator.py:475-480`):** Tras fallar `publish(status)` en `create_project`, una segunda llamada `get_status("p-cache")` con el publicador ya recuperado ejecuta `0` llamadas adicionales (`publish calls = [('INTERVIEW', True)]`).
- **`P44-5` (`static_deploy_publisher.py:251-255`):** Con `target="vercel"`, `deploy_base_url=""` y stdout `"Inspect: https://vercel.com/x\nhttps://app-vc.vercel.app\n"`, `deploy_site` devuelve `https://app-vc.pages.dev`.
- **`P44-6` (`panel/app.js:33-38`, `secrets.py:33-38`):** `redactDisplayText("NaN Check")` devuelve `"NaN Check"`, `redactDisplayText("vcp_ABCDEF123456 ?key=MYSECRET")` devuelve la cadena intacta, y `redact_secrets("123456:ABCDEFGH_IJKL", env={})` devuelve la cadena intacta.

---

## 4. Cumplimiento Global de `MASTER_PROMPT.md` (v1.2.1) e Invariantes (`INV-01` a `INV-23`)

### Revisión por secciones del documento rector
- **§2 Decisiones de diseño (`MASTER_PROMPT.md:24-31`):** Local-first, bot long-polling sin servidor propio, costo `$0`, apps estáticas y sin nombres de modelos hardcodeados -> **CUMPLE en diseño y runtime principal** (cerrado el hardcoding `"model-fast-01"` en `cli.py:82`, con observaciones menores en `P44-3a/3b`).
- **§3–§7 Fases 0–4 (`MASTER_PROMPT.md:131-185`):** Toda la columna vertebral de dominio, pipeline de 3 agentes, builder con `TestLock`, bot Telegram, `GitRegistryPublisher`, CLI continuo y PWA estático está implementada y pasa `341 passed` en corrida limpia; persiste sensibilidad de tiempo (`H44-01`) y bordes menores (`H44-03`, `H44-04`). Fase 5 (`CHANGE_REQUEST`) queda diferida conforme a alcance.

| Invariante | Estado | Evidencia y dictamen estricto |
|---|:---:|---|
| **INV-01** | **cumple (con deuda `H44-01`/`H44-03`)** | TDD respetado en tag `phase-4.4-tests`; `341 passed` en corrida 3 (tras 2 timeouts de 30 s bajo Windows en `H44-01`). Faltan pruebas para los casos borde `P44-1..P44-5`. |
| **INV-02** | **cumple (con deuda menor)** | Todos los puertos (`LLM`, `HttpTransport`, `Runner`, `VCS`, `Clock`, `EventStore`, `StatusPublisher`, `CodeAgent`) desacoplan I/O; `UrllibHttpTransport.post_json` probado offline. Observación menor abierta: `telegram_bot.py:137` importa `GitRegistryPublisher`. |
| **INV-03** | **cumple** | `--disable-socket` activo en `pyproject.toml:33`; 341 pruebas corren 100 % offline sin credenciales. |
| **INV-04** | **cumple** | `ALLOWED_TRANSITIONS` y `validate_transition` en `domain/states.py:30-67` como único paso en orquestador y proyección (`domain/progress.py`). |
| **INV-05** | **cumple** | `Orchestrator.submit_approval` (`orchestrator.py:360-398`) y `project_status_from_events` (`domain/progress.py:114-138`) exigen `WAITING_APPROVAL`, `decision="approved"` y `plan_hash` vigente. |
| **INV-06** | **cumple** | Circuit breaker acumulativo, topes de rondas CI (`max_ci_rounds=3`), `SubprocessRunner` con terminación de árbol de procesos y `run_polling_loop` con backoff acotado y `event.wait`. |
| **INV-07** | **cumple (repo) / parcial (bordes)** | Sin secretos en el repo; `secrets.py` redacta tokens de Telegram, GitHub, Vercel, `LLM_API_KEY=` y `?key=`; `vercel` no pasa `--token` en `argv`. Siguen abiertos `P44-6` (`redactDisplayText` JS para `vcp_`/`?key=`, `<digits>:<tok>` sin `bot` con `env={}`) y `:200-212` de `git_registry_publisher.py`. |
| **INV-08** | **cumple** | `pyproject.toml:11-22` intacto con versiones `==` (deuda `F-20` de lockfile transitivo documentada en `docs/DEBT.md:28`). |
| **INV-09** | **cumple** | `ProductionLLMAdapter.complete()` rechaza `models_config_path=None`, TOML inválido, no-JSON cuando hay `schema` y limpia `__cause__`/`__context__`. |
| **INV-10** | **parcial (`H44-02`)** | `docs/PHASE_REPORT_4.md` sigue las reglas de reporte de cierre de fase y detuvo el trabajo; presenta desfase menor en `cli.py` (`85%` reportado vs `84%` real en `task-5409` y `~125s` vs `190.15s`). |
| **INV-11** | **cumple** | Registro append-only como fuente de verdad (`domain/progress.py:40-187`), valida transiciones en replay y `BuildWorker` publica `events` sanitizados en `projects/<id>.json`. |
| **INV-12** | **cumple** | `calculate_progress` determinista en `domain/progress.py:22-37`. |
| **INV-13** | **cumple (suite) / parcial (CI plantilla)** | Rutas protegidas (`tests/`, `templates/`, `.github/`, `pyproject.toml`, `MASTER_PROMPT.md`, `AGENTS.md`) 100 % inmutables; `workspace.py` verifica SHA-256 de tests/configs antes de cada ejecución; `.github/workflows/ci.yml:35` exige `--cov-fail-under=85` pero aún no ejecuta JS/Playwright de plantilla/`panel/`. |
| **INV-14** | **no verificable** | `CHANGE_REQUEST` diferido a Fase 5 según `MASTER_PROMPT.md:72, 186-191`. |
| **INV-15** | **parcial (`H44-03`)** | Iteración local y tope de 3 rondas CI cumplidos; `push_branch` ya pasa `-c user.name`/`user.email` en ausencia de `.git/config`, pero falla ante `.git/config` parcial (`P44-1`) o fallo en `git add -A` (`P44-2`). |
| **INV-16** | **cumple** | Idempotencia persistente SQLite en Telegram y panel PWA en `panel/` 100 % estático sin backend. |
| **INV-17** | **CUMPLE** | Regresión `H43-01` cerrada (`orchestrator.py:400-423`); contadores acumulados en `CircuitBreaker` ya no se reinician tras `request_adjustment()`. |
| **INV-18** | **cumple** | Aislamiento en `workspaces/<id>/`, `WorkspaceManager.resolve_safe_path`, y entornos limpios en `AgyCliAdapter`, `GitHubVCSAdapter` y `StaticDeployPublisher`. |
| **INV-19** | **cumple (offline) / parcial (real)** | Sin tokens en `argv` ni ejecución en nube de `agy`; validación real con CLIs/APIs en producción queda fuera del entorno offline. |
| **INV-20** | **cumple** | `templates/static-web/` y `TemplateScaffold.inspect_template` validados con `console.error`/`pageerror` guards y `src/` vacío inicial. |
| **INV-21** | **cumple (con observación `H44-04`)** | `Orchestrator.get_status()` ya no rompe `create_project()` ante fallos de `GitRegistryPublisher`; `GitRegistryPublisher` recupera `REBASE_HEAD`; `build_runtime` valida `models.toml`. Pendiente cache prematura en `P44-4`. |
| **INV-22** | **cumple** | Validación estricta con regex de `project_id`, ramas insensibles a mayúsculas/minúsculas y contención de rutas resueltas. |
| **INV-23** | **cumple** | Oráculos escritos a mano en las pruebas revisadas; las mutaciones críticas (`M43-11`, `M43-14`, `M43-10b`, `M44-01`, `M44-03`..`M44-09`) mueren; única excepción equivalente `M44-02` y casos borde no cubiertos en `P44-1..P44-5`. |

---

## 5. Lo que NO se pudo verificar y por qué

1. **Integración real de red y APIs externas (`api.telegram.org`, API LLM OpenAI-compatible, `git push` a GitHub remoto, CLI `gh` incluyendo `gh pages deploy`, `vercel`, binario oficial `agy`):**
   - `pytest-socket` bloquea todo socket de red (`INV-03`) y no hay credenciales reales en el entorno; se probó vía `UrllibHttpTransport` interceptando `urllib.request.urlopen`, `FakeRunner`, scripts Python simulados y repositorios `git --bare` locales.
2. **Navegador real Chromium/Playwright y Service Worker (`panel/sw.js`, `templates/static-web/`):**
   - `tests/test_panel_pwa.py` evalúa `panel/app.js` con mocks de DOM/`fetch` bajo `deno` (`node` no está instalado en este Windows) y valida `panel/styles.css` por regex; tampoco se ejecutó `npm test` ni `playwright test` real sobre `templates/static-web/`, ni se probó `sw.js` en navegador real.
3. **Ejecución en Linux / GitHub Actions real (`.github/workflows/ci.yml`):**
   - No se ejecutó en runner Ubuntu de GitHub Actions; las corridas de esta auditoría ocurrieron en Windows 11 con Python `3.12.10` (donde además los `git.exe` múltiples mostraron sensibilidad al timeout de `30 s` bajo cobertura).
4. **Señales reales del SO en demonio residente (`SIGINT`/`SIGTERM`):**
   - En `test_cli_entrypoint.py`, `main()` se probó con `stop_event` pre-marcado, no como proceso bloqueado en espera de red al recibir una señal real de sistema operativo.
5. **Fase 5 (`CHANGE_REQUEST`, `INV-14`):**
   - Fuera del alcance de la Fase 4 según `MASTER_PROMPT.md:72, 186-191`.
