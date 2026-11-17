# AUDIT_REPORT_2B_1 — Re-auditoría independiente de cierre tras endurecimiento de la Fase 2b

Auditor independiente (en modo solo lectura). Rama `phase-2b`, HEAD `20ecf7e`, comparada contra el tag de pruebas de endurecimiento `phase-2b.1-tests` (`08b049c`), el reporte previo `docs/AUDIT_REPORT_2B.md` (`3548c50`) y `main` (`9f7a07b`). Referencias: `MASTER_PROMPT.md` (v1.2.1), `AGENTS.md`, `docs/decisions/ADR-002-code-agent-port.md`, `docs/PHASE_REPORT_2B.md`, `docs/AUDIT_REPORT_2B.md`, `docs/PHASE2B_TEST_MAP.md`.

---

## Veredicto: **NO APTO**

### Resumen ejecutivo del veredicto
- **Lo que SÍ cerró correctamente:**
  - **F2B-01 (Crítico)** está **CERRADO**: `BuildWorker.run_job` (`src/agent_factory/builder.py:201-221, 319-339`) ahora captura `(CodeAgentError, CommandTimeoutError)` tanto en generación de tests como en implementación/corrección, registra la llamada en `CircuitBreaker` y `AGENT_INVOKED`, emite `PROJECT_FAILED` y transiciona a `FAILED` con `BuildReport`.
  - **F2B-03 (Alto)**, **F2B-04 (Medio)**, **F2B-05 (Medio)** y **F2B-08 (Bajo)** están **CERRADOS** en los términos solicitados: las 3 mutaciones que sobrevivieron en `AUDIT_REPORT_2B.md` (**M1**, **M2** y **M15**) ahora **MUEREN (KILLED)**.
  - **Inmutabilidad de pruebas (`INV-13`) en Paso B' respetada al 100 %:** `git diff phase-2b.1-tests --stat -- tests/ pyproject.toml` es **idénticamente vacío**.
  - `ruff check .` pasa limpio (`All checks passed!`) y la suite completa pasa **287 tests (0 fallos)** en `16.07s` con **91 % de cobertura global**.
- **Por qué NO es APTO todavía (hallazgos abiertos/parciales y regresión):**
  1. **F2B-02 (Alto) — Parche a la medida del test en `agy_cli_adapter.py:201-204` (`PARCIAL`):** En lugar de exigir contexto HTTP/status/cuota para `429` como pedía `AUDIT_REPORT_2B.md`, `_run_cli` mantuvo una coincidencia libre `\b429\b` excluyendo **únicamente la frase literal `"line 429"`** del test (`not re.search(r"\bline\s+429\b", ...)`). Verificado empíricamente sobre `HEAD` sin mutar: fallos fatales (`exit_code=1`) con `"TypeError at src/app.js:429:12 - undefined is not a function"` o `"AssertionError: expected 429 to equal 200"` **siguen lanzando falsamente `QuotaExhaustedError`** en vez de `CodeAgentError` (rompe `INV-09` y `A2b-5`), mientras que la mutación **M19b** (eliminar `:201-204`) **sobrevive a los 287 tests**.
  2. **F2B-09 (Nuevo / Medio) — Regresión de mutación `M5` y `M5b` en `scaffold.py:98-103`:** En `AUDIT_REPORT_2B.md`, la mutación **M5** (omitir la verificación de `REQUIRED_STATIC_WEB_FILES`) **moría**. Tras añadir `pw_file.read_text()` en `scaffold.py:106-107` para F2B-03, el único test de archivo faltante (`test_template_and_scaffold.py:77-88`) solo omite `playwright.config.mjs`, por lo que ahora falla en `:107` antes de necesitar `REQUIRED_STATIC_WEB_FILES`. Como resultado, tanto **M5** (desactivar el bucle entero) como **M5b** (eliminar `"index.html"` y `"styles.css"` de `REQUIRED_STATIC_WEB_FILES`) **SOBREVIVEN a los 287 tests**.
  3. **Inexactitudes documentales en `docs/PHASE_REPORT_2B.md` y código muerto (`F2B-07`):**
     - `PHASE_REPORT_2B.md:66` afirma *"F2B-07 (Bajo): Ramas defensivas... cubiertos al 100 %"*, lo cual es **falso** frente al reporte real de cobertura: `agy_cli_adapter.py` bajó de **92 % a 89 %** (líneas sin cubrir: `91, 229->252, 237, 242-246, 249-250`, dejando todo el fallback JSON por línea `:242-246` sin ejecutar y creando código muerto en `:90-93` porque `:79-82` ya rechaza `ws_path.parent != base_dir`).
     - `scaffold.py` conserva sin cubrir `:62` (que `AUDIT_REPORT_2B.md` pidió eliminar o cubrir), `:154-155` y la nueva línea `:198` (`tests_dir` symlink escape, **M23b** sobrevive — **F2B-06 PARCIAL**).
     - `PHASE_REPORT_2B.md:54` reporta `85 %` de cobertura en `src/agent_factory/ports/`, cuando el cálculo real sobre la corrida ejecutada es `73/88 = 82.95 %` (**83 %**).

---

## 1. Resultados reales de ejecución (una vez cada uno)

| Comando | Resultado real |
|---|---|
| `ruff check .` | `All checks passed!` (exit code `0`) |
| `pytest -q --cov=src/agent_factory --cov-report=term-missing` | `287 passed, 2 warnings in 16.07s` (exit code `0`) · Cobertura total: **91 %** (`1588` stmts, `121` miss, `402` branch, `59` brpart). Desglose relevante: `scaffold.py` **96 %** (`Missing: 62, 154-155, 198`), `workspace.py` **94 %**, `agy_cli_adapter.py` **89 %** (`Missing: 91, 229->252, 237, 242-246, 249-250`), `builder.py` **88 %**, `orchestrator.py` **90 %**, `subprocess_runner.py` **57 %**, `domain/` **98 %** (`progress.py` 96 %, `models.py`/`events.py`/`states.py` 100 %), `ports/` **83 %** (`73/88` stmts). |
| `git diff phase-2b.1-tests --stat -- tests/ pyproject.toml` | Vacío (`0` archivos modificados en `tests/` o `pyproject.toml`). |
| `git diff main...HEAD --stat` | 18 archivos creados/modificados (`2027` inserciones, `6` borrados). |

### Verificación de `docs/PHASE_REPORT_2B.md` frente a la ejecución real
- `PHASE_REPORT_2B.md:41` (`287` tests = `268 + 11 + 8`, `0` fallos, `2` warnings) y coberturas de `scaffold.py` (`96 %`), `workspace.py` (`94 %`), `agy_cli_adapter.py` (`89 %`), `builder.py` (`88 %`), `orchestrator.py` (`90 %`), `domain/` (`98 %`) y global (`91 %`) **coinciden** con la corrida real.
- **Discrepancias detectadas:**
  1. `PHASE_REPORT_2B.md:54` reporta `src/agent_factory/ports/: 85 %`, pero los módulos reales en `ports/` suman `8+19+8+10+15+11+17 = 88` sentencias con `15` omitidas (`73/88 = 82.95 %` ≈ **83 %**).
  2. `PHASE_REPORT_2B.md:66` afirma *"Ramas defensivas de `template_id`, `package.json` corrupto, subdirectorios anidados de cwd y parseo multilínea de stdout cubiertos al 100 %."* En la ejecución real quedan descubiertas las líneas `62, 154-155, 198` en `scaffold.py` y `91, 229->252, 237, 242-246, 249-250` en `agy_cli_adapter.py`.

---

## 2. Tabla de cierre de hallazgos de `AUDIT_REPORT_2B.md` (F2B-01 a F2B-08) y hallazgos nuevos

| ID | Severidad | Estado | Evidencia (`archivo:línea`) | Verificación detallada |
|---|---|---|---|---|
| **F2B-01** | **Crítico** | **CERRADO** | `src/agent_factory/builder.py:187-222`, `:300-340`; `tests/test_agy_cli_adapter.py:588-684` | `BuildWorker.run_job` envuelve `generate_tests` (`:201`) e `implement_or_fix` (`:319`) con `except (CodeAgentError, CommandTimeoutError) as err:`, registrando en `CircuitBreaker` (`record_state_call`), emitiendo `PROJECT_FAILED` y retornando `BuildReport` con `JobState.FAILED`. Probado con subprocesos colgados reales en Paso A (`invocations == 1`, `agent_invocations == 2`) y Paso B (`invocations == 2`, `agent_invocations == 3`). Mutaciones **M16** y **M17** **KILLED**. *(Nota menor: si un timeout ocurriera cuando el breaker ya hubiera alcanzado el límite en paralelo, `:205`/`:323` emitiría un segundo `PROJECT_FAILED`.)* |
| **F2B-02** | **Alto** | **PARCIAL** | `src/agent_factory/adapters/agy_cli_adapter.py:182-223`; `tests/test_agy_cli_adapter.py:687-735` | Aunque `:182` ya mueve el chequeo de cuota dentro de `if res.exit_code != 0:` (matando **M18**), las líneas `:201-204` conservan `re.search(r"\b429\b", combined_output) and not re.search(r"\bline\s+429\b", ...)`, parcheando de forma literal la frase del test `:705` en lugar de exigir contexto de cuota/HTTP. Sonda ejecutada sobre `HEAD` (`20ecf7e`) con `exit_code=1`: `"TypeError at src/app.js:429:12 - undefined is not a function"` y `"AssertionError: expected 429 to equal 200"` lanzan **`QuotaExhaustedError`** en vez de `CodeAgentError`. Además, **M19b** (eliminar `:201-204`) sobrevive a los 287 tests; el regex `:196` `\b(?:http\|status\|error\|code)\s*429\b` tampoco acepta `:`/`=` (`status: 429`), y `"rate limit"` se redujo a `"rate limit exceeded"`. |
| **F2B-03** | **Alto** | **CERRADO** *(con nota)* | `templates/static-web/playwright.config.mjs:2-21`; `src/agent_factory/scaffold.py:106-121`; `tests/test_template_and_scaffold.py:197-251` | `playwright.config.mjs` define y exporta `attachErrorGuards(page)` con listeners `page.on("console", ...)` (`msg.type() === "error"`) y `page.on("pageerror", ...)` más `assertNoErrors()` que lanza `Error`. `inspect_template` exige ambos listeners en `playwright.config.mjs` (matando **M20a** y **M20b**), y el test nuevo verifica `static-web` más 3 plantillas negativas. *(Nota técnica: la comprobación en `scaffold.py:108-121` es por subcadenas sin validar `throw`, y Playwright solo activará la guarda si los tests e2e generados importan `attachErrorGuards`.)* |
| **F2B-04** | **Medio** | **CERRADO** | `src/agent_factory/scaffold.py:140-156`; `tests/test_template_and_scaffold.py:254-330` | `inspect_template` valida `dependencies`, `devDependencies` y `peerDependencies` en `package.json`, y URLs `http://`/`https://`/`ws://`/`wss://` en los archivos de plantilla. El nuevo test aísla los 5 vectores (`dep-only-web`, `dev-dep-web`, `peer-dep-web`, `html-https-only-web`, `css-http-only-web`), matando **M1**, **M2**, **M21a** y **M21b**. *(Nota menor: no revisa `optionalDependencies` ni `dependencies` con tipo distinto de `dict`.)* |
| **F2B-05** | **Medio** | **CERRADO** | `src/agent_factory/workspace.py:14, 19`; `tests/test_test_lock_and_generation.py:417-453` | `test_test_lock_protects_playwright_config_mjs_and_package_json_from_tampering` sabotea y borra `playwright.config.mjs` y altera `package.json`, verificando detección (`False`) y restauración exacta. Mutación **M15** (antes superviviente) ahora **KILLED**. |
| **F2B-06** | **Medio** | **PARCIAL** | `src/agent_factory/scaffold.py:180-204`; `tests/test_template_and_scaffold.py:333-359` | `scaffold` comprueba symlink/escape y archivos previos tanto en `ws_dir / "src"` como en `ws_dir / "tests"`, y el test nuevo cubre symlink/junction en `ws/src` vacío y archivo previo en `ws/tests` (matando **M22** y **M23**). Sin embargo, la guarda de symlink/escape en `ws_dir / "tests"` (`:195-200`, línea `198` en `Missing`) carece de prueba y **M23b** **SOBREVIVE**. |
| **F2B-07** | **Bajo** | **PARCIAL** | `src/agent_factory/scaffold.py:44-67, 134-135`; `src/agent_factory/adapters/agy_cli_adapter.py:79-93, 226-252`; `docs/PHASE_REPORT_2B.md:66` | Se cubrieron `template_id` vacío/`\`/inexistente, `package.json` corrupto, subdirectorio anidado, JSON multilínea y stdout texto plano. No obstante: (1) `scaffold.py:62` sigue en el código y sin cobertura; (2) añadir `ws_path.parent != base_dir` (`agy_cli_adapter.py:79-82`) dejó como **código muerto inalcanzable** el chequeo `expected_ws_dir != ws_path` (`:90-93`, línea `91` en `Missing`, **M24** y **M25** sobreviven); (3) la reordenación de `_run_cli` (`:231-250`) dejó sin cobertura todo el bucle de fallback JSON por línea (`:242-246`) y `:237, 249-250` (la cobertura de `agy_cli_adapter.py` bajó de `92 %` a `89 %`); (4) `PHASE_REPORT_2B.md:66` declara falsamente cobertura del `100 %`. |
| **F2B-08** | **Bajo** | **CERRADO** | `tests/test_agy_cli_adapter.py:360-389` | Los intentos 1 y 2 usan `timeout_seconds=5.0`, aislando `timeout_seconds=0.3` exclusivamente en `timeout_adapter` para el intento 3. |
| **F2B-09** *(Nuevo)* | **Medio** | **ABIERTO (Regresión)** | `src/agent_factory/scaffold.py:12-17, 98-107`; `tests/test_template_and_scaffold.py:77-88` | **Regresión de detección en `M5` / `M5b`:** El test `incomplete-web` (`:77-88`) solo omite `playwright.config.mjs`. Al agregarse `pw_file = tpl_dir / "playwright.config.mjs"; pw_text = pw_file.read_text(...)` en `scaffold.py:106-107`, ese test falla ahora en `:107` y ya no ejerce `REQUIRED_STATIC_WEB_FILES` (`:98-103`). Por ello, tanto **M5** (desactivar el bucle completo `REQUIRED_STATIC_WEB_FILES`) como **M5b** (quitar `"index.html"` y `"styles.css"` de `REQUIRED_STATIC_WEB_FILES`) **SOBREVIVEN a los 287 tests**. |

---

## 3. Revisión de los tests de endurecimiento contra criterios (A2b-1 a A2b-6) e `INV-23`

| Test nuevo / reforzado | Criterio / Hallazgo | ¿Oráculo escrito a mano (`INV-23`)? | ¿Sin atajos ni mocks indebidos? | Observaciones de auditoría |
|---|---|---|---|---|
| `test_build_worker_captures_agy_cli_adapter_timeouts_in_test_gen_and_impl_without_getting_stuck` (`test_agy_cli_adapter.py:588-684`) | A2b-5, A2b-6 / F2B-01 | **Sí** (`FAILED`, `invocations == 1`/`2`, `agent_invocations == 2`/`3`, `"timed out"`, `get_calls == 1`/`2`) | **Sí** (usa `SubprocessRunner` real y `MemoryEventStore`) | Verifica timeout en Paso A y en Paso B sin que el proyecto quede en `BUILDING`. |
| `test_agy_cli_adapter_does_not_misclassify_429_substring_on_success_or_non_quota_failure` (`test_agy_cli_adapter.py:687-735`) | A2b-5 / F2B-02 | **Sí** (`files_written == ["src/app.js"]`, `not isinstance(..., QuotaExhaustedError)`, `retry_after_seconds == 4.0`) | **Sí** (usa `SubprocessRunner` real) | Oráculo válido, pero solo prueba el texto literal `"line 429"`, lo que permitió el parche estrecho `not line 429` en `:203`. |
| `test_agy_cli_adapter_rejects_nested_workspace_subdir_and_handles_multiline_or_plain_stdout` (`test_agy_cli_adapter.py:738-780`) | A2b-3 / F2B-07 | **Sí** (`SandboxViolationError`, `["src/app.js"]`, `"Pretty JSON output"`, `files_written == []`) | **Sí** (usa `FakeRunner` detrás de puerto `Runner`) | No ejercita logs previos + última línea JSON (`:239-246`). |
| `test_static_web_template_and_inspect_template_enforce_console_error_and_pageerror_guard` (`test_template_and_scaffold.py:197-251`) | A2b-1, AC-2.5 / F2B-03 | **Sí** (cadenas exactas esperadas y 3 casos `ValueError`) | **Sí** | Validación estática del archivo JS y rechazo en `inspect_template` (sin ejecutar navegador real). |
| `test_inspect_template_separately_rejects_package_dependencies_and_external_urls` (`test_template_and_scaffold.py:254-330`) | A2b-1 / F2B-04 | **Sí** (5 plantillas aisladas, `pytest.raises(ValueError)`) | **Sí** | Mata por separado **M1** (`dependencies`) y **M2** (URL externa), además de `devDependencies` y `peerDependencies`. |
| `test_template_scaffold_rejects_destination_symlink_src_or_prepopulated_tests` (`test_template_and_scaffold.py:333-359`) | A2b-2 / F2B-06 | **Sí** (`pytest.raises(SandboxViolationError)`) | **Sí** | Usa enlace real (`os.symlink` / `mklink /J`). No cubre symlink/junction en `ws/tests` (**M23b**). |
| `test_template_scaffold_defensive_errors_on_invalid_or_missing_template_and_malformed_package_json` (`test_template_and_scaffold.py:362-383`) | A2b-1 / F2B-07 | **Sí** (`SandboxViolationError`, `FileNotFoundError`, `ValueError`) | **Sí** | Cubre `:45, 48, 66, 134-135`; no incluye `template_id="."` (`:62`). |
| `test_test_lock_protects_playwright_config_mjs_and_package_json_from_tampering` (`test_test_lock_and_generation.py:417-453`) | AC-2.2 / F2B-05 | **Sí** (`False`, restauración exacta, `True`) | **Sí** | Mata **M15**. |

---

## 4. Resultado de las 31 mutaciones ejecutadas (en worktree temporal)

Todas las mutaciones se ejecutaron en un worktree temporal fuera del workspace (`$TEMP/af-audit-2b1-wt`) con `PYTHONPATH=$TEMP/af-audit-2b1-wt/src`, verificado mediante importación explícita desde el worktree, y eliminado/podado al finalizar (`git worktree remove --force` + `git worktree prune`; verificado con `git worktree list`).

### 4.1 Repetición de las 15 mutaciones de `AUDIT_REPORT_2B.md` (M1–M15)

| ID | Archivo y líneas mutadas | Descripción de la mutación | Resultado | Evidencia de tests (o motivo de supervivencia) |
|---|---|---|---|---|
| **M1** *(antes sobrevivió)* | `scaffold.py:141` | Omitir `"dependencies"` en el bucle de inspección de `package.json` | **DETECTADA (KILLED)** | `1 failed, 7 passed` — `test_inspect_template_separately_rejects_package_dependencies_and_external_urls` |
| **M2** *(antes sobrevivió)* | `scaffold.py:152` | Ignorar URLs externas (`http://`, `https://`, `ws://`, `wss://`) | **DETECTADA (KILLED)** | `1 failed, 7 passed` — `test_inspect_template_separately_rejects_package_dependencies_and_external_urls` |
| **M3** | `scaffold.py:157` | No lanzar `ValueError` cuando `external_or_backend_refs` no está vacío | **DETECTADA (KILLED)** | `1 failed, 7 passed` — `test_inspect_template_separately_rejects_package_dependencies_and_external_urls` |
| **M4** | `scaffold.py:92` | Permitir archivos preexistentes en `src/` de la plantilla | **DETECTADA (KILLED)** | `1 failed, 7 passed` — `test_inspect_template_rejects_missing_files_external_network_backend_or_populated_src` |
| **M5** *(regresión)* | `scaffold.py:98-103` | Omitir la comprobación de `REQUIRED_STATIC_WEB_FILES` (`for req in ():`) | **SOBREVIVIÓ (SURVIVED)** | `287 passed` en suite completa — el único test de archivo faltante (`test_template_and_scaffold.py:77-88`) omite `playwright.config.mjs`, que ahora falla en `:107` (`pw_file.read_text()`), sin probar la falta de `index.html` o `styles.css` (**F2B-09**). |
| **M6** | `scaffold.py:76-78` | Desactivar la detección de symlinks/junctions que escapan de `tpl_dir` | **DETECTADA (KILLED)** | `1 failed, 7 passed` — `test_template_scaffold_rejects_invalid_project_id_contaminated_workspace_or_symlink_escape` |
| **M7** | `scaffold.py:188` | Desactivar chequeo de archivos preexistentes en `ws_dir / "src"` | **DETECTADA (KILLED)** | `1 failed, 7 passed` — `test_template_scaffold_rejects_invalid_project_id_contaminated_workspace_or_symlink_escape` |
| **M8** | `agy_cli_adapter.py:73` | Desactivar validación de sandbox en `validate_workspace_cwd` | **DETECTADA (KILLED)** | `2 failed, 8 passed` — `test_agy_cli_adapter_rejects_cwd_outside_workspaces_sandbox` y `...rejects_nested_workspace_subdir...` |
| **M9** | `agy_cli_adapter.py:101` | Devolver `dict(source)` sin filtrar secretos del host | **DETECTADA (KILLED)** | `1 failed, 9 passed` — `test_agy_cli_adapter_executes_in_workspace_cwd_and_strips_host_secrets_from_env` |
| **M10** | `subprocess_runner.py:42` | Ignorar `env` (`env=None` en `subprocess.Popen`) | **DETECTADA (KILLED)** | `1 failed, 9 passed` — `test_agy_cli_adapter_executes_in_workspace_cwd_and_strips_host_secrets_from_env` |
| **M11** | `agy_cli_adapter.py:125` | Permitir `build_test_generation_payload` con archivos en `src/` | **DETECTADA (KILLED)** | `1 failed, 9 passed` — `test_agy_cli_adapter_generate_tests_rejects_workspace_with_existing_src_files` |
| **M12** | `agy_cli_adapter.py:131` | Filtrar `"plan"` en `build_test_generation_payload` | **DETECTADA (KILLED)** | `1 failed, 9 passed` — `test_agy_cli_adapter_isolates_test_generation_prompt_from_plan_and_passes_plan_only_in_implement` |
| **M13** | `agy_cli_adapter.py:216` | Lanzar `CodeAgentError` en vez de `QuotaExhaustedError` ante cuota | **DETECTADA (KILLED)** | `3 failed, 7 passed` en `test_agy_cli_adapter.py` |
| **M14** | `agy_cli_adapter.py:182` | Ignorar `res.exit_code != 0` en `_run_cli` | **DETECTADA (KILLED)** | `3 failed, 7 passed` en `test_agy_cli_adapter.py` |
| **M15** *(antes sobrevivió)* | `workspace.py:19` | Eliminar `"playwright.config.mjs"` de `RUNNER_CONFIG_FILENAMES` | **DETECTADA (KILLED)** | `1 failed, 6 passed` — `test_test_lock_protects_playwright_config_mjs_and_package_json_from_tampering` |

### 4.2 Mutaciones nuevas sobre las correcciones de endurecimiento (M5b, M16–M26)

| ID | Archivo y líneas mutadas | Descripción de la mutación | Resultado | Evidencia de tests (o motivo de supervivencia) |
|---|---|---|---|---|
| **M5b** | `scaffold.py:12-17` | Dejar `REQUIRED_STATIC_WEB_FILES` solo con `("package.json", "playwright.config.mjs")` (sin `index.html` ni `styles.css`) | **SOBREVIVIÓ (SURVIVED)** | `287 passed` en suite completa — ningún test prueba una plantilla sin `index.html` o `styles.css` (**F2B-09**). |
| **M16** | `builder.py:201` | Capturar solo `CodeAgentError` (omitir `CommandTimeoutError`) en Paso A (`generate_tests`) | **DETECTADA (KILLED)** | `1 failed, 9 passed` — `test_build_worker_captures_agy_cli_adapter_timeouts_in_test_gen_and_impl_without_getting_stuck` |
| **M17** | `builder.py:319` | Capturar solo `CodeAgentError` (omitir `CommandTimeoutError`) en Paso B (`implement_or_fix`) | **DETECTADA (KILLED)** | `1 failed, 9 passed` — `test_build_worker_captures_agy_cli_adapter_timeouts_in_test_gen_and_impl_without_getting_stuck` |
| **M18** | `agy_cli_adapter.py:182` | Evaluar cuota antes de verificar `res.exit_code != 0` | **DETECTADA (KILLED)** | `1 failed, 9 passed` — `test_agy_cli_adapter_does_not_misclassify_429_substring_on_success_or_non_quota_failure` |
| **M19** | `agy_cli_adapter.py:201-204` | Usar `"429" in combined_output` sin exclusión de `"line 429"` cuando `exit_code != 0` | **DETECTADA (KILLED)** | `1 failed, 9 passed` — `test_agy_cli_adapter_does_not_misclassify_429_substring_on_success_or_non_quota_failure` |
| **M19b** *(sonda)* | `agy_cli_adapter.py:201-204` | Eliminar por completo la rama libre `re.search(r"\b429\b", ...) and not line 429` | **SOBREVIVIÓ (SURVIVED)** | `287 passed` en suite completa — demuestra que ningún test legítimo de cuota depende de `:201-204`; esa rama solo sirve de atajo alrededor de `"line 429"` mientras deja expuestos otros fallos con `429` (**F2B-02**). |
| **M20a** | `scaffold.py:108-114` | Omitir la comprobación `has_console_guard` en `inspect_template` | **DETECTADA (KILLED)** | `1 failed, 7 passed` — `test_static_web_template_and_inspect_template_enforce_console_error_and_pageerror_guard` |
| **M20b** | `scaffold.py:115-117` | Omitir la comprobación `has_pageerror_guard` en `inspect_template` | **DETECTADA (KILLED)** | `1 failed, 7 passed` — `test_static_web_template_and_inspect_template_enforce_console_error_and_pageerror_guard` |
| **M21a** | `scaffold.py:141` | Ignorar `devDependencies` en `inspect_template` | **DETECTADA (KILLED)** | `1 failed, 7 passed` — `test_inspect_template_separately_rejects_package_dependencies_and_external_urls` |
| **M21b** | `scaffold.py:141` | Ignorar `peerDependencies` en `inspect_template` | **DETECTADA (KILLED)** | `1 failed, 7 passed` — `test_inspect_template_separately_rejects_package_dependencies_and_external_urls` |
| **M22** | `scaffold.py:182-187` | Omitir detección de symlink/junction en destino `ws_dir / "src"` | **DETECTADA (KILLED)** | `1 failed, 7 passed` — `test_template_scaffold_rejects_destination_symlink_src_or_prepopulated_tests` |
| **M23** | `scaffold.py:201-204` | Permitir archivos preexistentes en destino `ws_dir / "tests"` | **DETECTADA (KILLED)** | `1 failed, 7 passed` — `test_template_scaffold_rejects_destination_symlink_src_or_prepopulated_tests` |
| **M23b** | `scaffold.py:195-200` | Omitir detección de symlink/junction escapante en destino `ws_dir / "tests"` | **SOBREVIVIÓ (SURVIVED)** | `287 passed` en suite completa — ningún test crea symlink/junction en `ws/tests` (línea `198` sin cobertura, **F2B-06**). |
| **M24** | `agy_cli_adapter.py:79-82` | Omitir `if ws_path.parent != base_dir:` en `validate_workspace_cwd` | **SOBREVIVIÓ (SURVIVED)** | `287 passed` en suite completa — `expected_ws_dir != ws_path` (`:90-93`) ya interceptaba el subdirectorio anidado; `:79-82` duplicó esa verificación y dejó `:90-93` como código muerto (**F2B-07**). |
| **M25** | `agy_cli_adapter.py:90-93` | Eliminar `if expected_ws_dir != ws_path:` en `validate_workspace_cwd` | **SOBREVIVIÓ (SURVIVED)** | `287 passed` en suite completa — `:75-89` ya garantizan `expected_ws_dir == ws_path`, confirmando que `:90-93` es inalcanzable (**F2B-07**). |
| **M26** | `agy_cli_adapter.py:231-238` | Saltar `json.loads(output_text)` completo antes del escaneo línea por línea | **DETECTADA (KILLED)** | `1 failed, 9 passed` — `test_agy_cli_adapter_rejects_nested_workspace_subdir_and_handles_multiline_or_plain_stdout` |

---

## 5. Acciones concretas requeridas para alcanzar **APTO**

1. **Corregir de raíz `F2B-02` (`src/agent_factory/adapters/agy_cli_adapter.py:194-205`):** Eliminar la rama libre `\b429\b` / `not line 429` (`:201-204`), ampliar `:196` para aceptar separadores (`\b(?:http|status|error|code)[:=\s]+429\b`) o `"429"` acompañado explícitamente de indicadores de límite/cuota (`too many requests`, `quota`, `rate[- ]?limit`), y añadir casos negativos reales (`src/app.js:429:12`, `expected 429`) en `tests/test_agy_cli_adapter.py`.
2. **Cerrar `F2B-09` (`M5` / `M5b`) y brechas de cobertura (`F2B-06` / `F2B-07`):**
   - En `tests/test_template_and_scaffold.py`, agregar casos negativos para plantilla sin `index.html` y sin `styles.css` (con `playwright.config.mjs` presente) para matar **M5** y **M5b**, más `ws_dir / "tests"` symlink escapante para matar **M23b** y `template_id="."` (o eliminar `:62` si no se requiere).
   - En `src/agent_factory/adapters/agy_cli_adapter.py`, eliminar la guarda duplicada/inalcanzable (`:90-93`) y probar en `tests/test_agy_cli_adapter.py` stdout con líneas de log previas + una línea JSON final para cubrir `:242-246`.
3. **Corregir `docs/PHASE_REPORT_2B.md`:** Actualizar la cobertura real de `ports/` (`83 %`) y eliminar la afirmación falsa de cobertura del `100 %` en ramas defensivas (`:66`).

---

## 6. Lo que NO se pudo verificar y por qué

1. **Ejecución real de `templates/static-web/` con Node (`node --test`), Playwright y navegador headless Chromium (`AC-2.5`):**
   - La suite del orquestador corre bajo `pytest-socket` (`--disable-socket`, `INV-03`) y solo valida estáticamente la estructura y presencia de `attachErrorGuards` en `playwright.config.mjs`; no se ejecutaron `node` ni Chromium contra `index.html` dentro de esta auditoría.
2. **Binario oficial `agy` autenticado y rama POSIX de `SubprocessRunner` (`src/agent_factory/adapters/subprocess_runner.py:66-95`):**
   - El entorno de auditoría es Windows (`win32`, `os.name == "nt"`), por lo que la rama POSIX (`start_new_session=True` + `os.killpg`) solo se ejecuta en Linux CI, y las llamadas al CLI en Fase 2b usan scripts Python locales deterministas bajo `SubprocessRunner` (`INV-03`, `INV-19`).
