# AUDIT_REPORT_2B_2 — Re-auditoría final de cierre de la Fase 2b (ajuste quirúrgico)

Auditor independiente (en modo solo lectura). Rama `phase-2b`, HEAD `04899fb`, comparada contra el tag de ajuste quirúrgico de pruebas `phase-2b.2-tests` (`0e7ac5a`), el reporte previo `docs/AUDIT_REPORT_2B_1.md` (`6751854`) y `main` (`9f7a07b`). Referencias: `MASTER_PROMPT.md` (v1.2.1), `AGENTS.md`, `docs/decisions/ADR-002-code-agent-port.md`, `docs/PHASE_REPORT_2B.md`, `docs/PHASE2B_TEST_MAP.md`.

---

## Veredicto: **APTO CON CORRECCIONES**

### Resumen ejecutivo del veredicto
- **Lo que cerró correctamente respecto a `AUDIT_REPORT_2B_1.md`:**
  - Los defectos funcionales y regresiones señalados en `AUDIT_REPORT_2B_1.md` quedaron corregidos en código:
    1. **F2B-02 (comportamiento en `HEAD`):** `src/agent_factory/adapters/agy_cli_adapter.py:178-199` eliminó el parche estrecho `not line 429`. La sonda directa sobre `HEAD` confirma que `exit_code=1` con `"TypeError at src/app.js:429:12 - undefined is not a function"` o `"AssertionError: expected 429 to equal 200"` ya lanzan `CodeAgentError` (no `QuotaExhaustedError`), y reintroducir la vieja regla `\b429\b and not line 429` (**M19b_reintro**) ahora **MUERE (KILLED)**.
    2. **F2B-09 (regresión `M5` / `M5b`):** Los nuevos vectores `missing-index-web` y `missing-styles-web` (`tests/test_template_and_scaffold.py:94-118`) ahora ejercen `REQUIRED_STATIC_WEB_FILES` con `playwright.config.mjs` presente; tanto **M5** como **M5b** **MUEREN (KILLED)**.
    3. **F2B-06 (`M23b`):** `tests/test_template_and_scaffold.py:391-398` cubre el escape por symlink/junction en `ws_dir / "tests"`, cubriendo `scaffold.py:198` y matando **M23b** (**KILLED**).
    4. **F2B-07 (código muerto, fallback JSON y métricas):** Se eliminó el chequeo muerto `expected_ws_dir != ws_path` en `agy_cli_adapter.py` (matando ahora **M24**), el fallback JSON por línea y stdout plano quedaron cubiertos en `tests/test_agy_cli_adapter.py:781-848` (`agy_cli_adapter.py` subió a **99 %** con **0** líneas sin cubrir y matando `M_tail_json_off` / `M_non_dict_json_break`), y `docs/PHASE_REPORT_2B.md:48-55, 66` corrigió la cobertura de `ports/` (`83 %`) y eliminó la afirmación falsa de `100 %`.
  - **Inmutabilidad de pruebas (`INV-13`) en Paso B'':** `git diff phase-2b.2-tests --stat -- tests/ templates/ pyproject.toml` es **idénticamente vacío**.
  - **Linter y suite:** `ruff check .` pasa limpio (`All checks passed!`) y `pytest -q --cov=src/agent_factory --cov-report=term-missing` pasa **287/287 tests** en `18.01s` con **91 % de cobertura global**.
- **Correcciones / brechas de mutación que impiden otorgar `APTO` limpio (no suavizadas):**
  1. **Brecha de mutación en detección aislada de cuota (`F2B-02`):** Aunque quitar *ambas* ramas (`M_quota_phrases_AND_regex_off`) o quitar `res.exit_code != 0` (`M_quota_no_exit_check`) muere, **mutar por separado la regex de `429` (`M_quota_regex_off`), las frases de cuota (`M_quota_phrases_empty`) o `res.exit_code in {42, 429}` (`M_quota_exitcode_off`) SOBREVIVE a los 287 tests**. Los 3 tests de cuota acoplan varias señales a la vez (`exit_code=42` + `"Quota exhausted"`, y `exit_code=1` + `"HTTP 429 Too Many Requests"`). Además, la regex `:194` (`\b(?:http|status|error|code)\s*[:=]?\s*429\b`) no detecta `"status_code=429"` ni `"HTTP/1.1 429"` sin texto de cuota adicional.
  2. **Brecha de mutación en subcondición de la guarda de consola (`F2B-03`):** **M20a**, **M20b** y **M_guard_or_to_and** mueren, pero **M_console_msg_type_off** (`scaffold.py:110-114`, omitir la exigencia `type() === "error" or console.error`) **SOBREVIVE a los 287 tests** porque ningún caso negativo prueba un `page.on("console", () => {})` sin filtro de errores.
  3. **Línea defensiva `scaffold.py:62` sin cubrir (`F2B-07`):** Sigue en `Missing: 62` y la mutación **M_tpl_equal_root_off** (`scaffold.py:61`) **SOBREVIVE** porque no se añadió un caso `template_id="."` en Paso A''.

---

## 1. Resultados reales de ejecución (una vez cada uno) y verificación de `docs/PHASE_REPORT_2B.md`

| Comando | Resultado real observado |
|---|---|
| `ruff check .` | `All checks passed!` (exit code `0`) |
| `pytest -q --cov=src/agent_factory --cov-report=term-missing` | `287 passed, 2 warnings in 18.01s` (exit code `0`) · Cobertura global: **91 %** (`1586` stmts, `111` miss, `400` branch, `55` brpart). Desglose real: `agy_cli_adapter.py` **99 %** (`104` stmts, `0` miss, `28` branch, `1` brpart: `237->233`), `scaffold.py` **97 %** (`99` stmts, `3` miss: `62, 154-155`, `46` branch, `1` brpart), `workspace.py` **94 %** (`112` stmts, `3` miss), `builder.py` **88 %** (`237` stmts, `25` miss), `orchestrator.py` **90 %** (`206` stmts, `17` miss), `domain/` **98 %** (`progress.py` 96 %, `models.py`/`events.py`/`states.py` 100 %), `ports/` **83 %** (`73/88` stmts: `clock` 75 %, `code_agent` 89 %, `llm` 88 %, `publisher` 70 %, `runner` 93 %, `store` 73 %, `vcs` 82 %). |
| `git diff phase-2b.2-tests --stat -- tests/ templates/ pyproject.toml` | Vacío (`0` archivos modificados en `tests/`, `templates/` o `pyproject.toml`). |
| `git diff phase-2b.2-tests...HEAD --stat` | 2 archivos modificados: `docs/PHASE_REPORT_2B.md` (`37 +/-`), `src/agent_factory/adapters/agy_cli_adapter.py` (`18 +/-`). |

### Verificación de `docs/PHASE_REPORT_2B.md` frente a la corrida real
- Los totales de pruebas (`287 passed, 0 failed, 2 warnings`, `~19.8s` vs `18.01s` reales en esta máquina) y las cifras de cobertura por módulo (`agy_cli_adapter.py` **99 %**, `scaffold.py` **97 %**, `workspace.py` **94 %**, `builder.py` **88 %**, `orchestrator.py` **90 %**, `domain/` **98 %**, `ports/` **83 %** con su desglose exacto, global **91 %**) **coinciden exactamente** con la ejecución real.
- La línea `:57` cita `git diff phase-2b.2-tests --stat -- tests/ templates/ pyproject.toml` vacío (verificado).
- La línea `:66` ya no afirma falsamente cobertura del `100 %` en ramas defensivas. *(Matiz menor: `:61` y `:66` marcan `[x]` cierre completo de F2B-02 y F2B-07 sin mencionar las 5 mutaciones granulares aún supervivientes ni `scaffold.py:62`.)*

---

## 2. Tabla de cierre de hallazgos (F2B-01 a F2B-09)

| ID | Severidad | Estado | Evidencia (`archivo:línea`) | Verificación detallada |
|---|---|---|---|---|
| **F2B-01** | **Crítico** | **CERRADO** | `src/agent_factory/builder.py:187-221`, `:300-339`; `tests/test_agy_cli_adapter.py:588-684` | `BuildWorker.run_job` captura `(CodeAgentError, CommandTimeoutError)` en Paso A y Paso B, registra la invocación en `CircuitBreaker`, emite `PROJECT_FAILED` y retorna `BuildReport` con `JobState.FAILED`. Sin regresiones. |
| **F2B-02** | **Alto** | **PARCIAL** *(código corregido; cobertura mutacional parcial)* | `src/agent_factory/adapters/agy_cli_adapter.py:178-217`; `tests/test_agy_cli_adapter.py:349-372, 503-548, 687-753` | Eliminada la regla `\b429\b and not line 429`. Sonda verificada en `HEAD`: `"TypeError at src/app.js:429:12"` y `"AssertionError: expected 429 to equal 200"` ahora lanzan `CodeAgentError`; `"HTTP 429"`, `"status: 429"`, `"code=429"`, `"rate limit"`, `"rate_limit"`, `"quota exceeded"`, `"RESOURCE_EXHAUSTED"`, `"insufficient quota"` y `exit=42/429` lanzan `QuotaExhaustedError`. **M19b_reintro**, **M_quota_all_off**, **M_quota_no_exit_check** y **M_quota_phrases_AND_regex_off** mueren (**KILLED**). **Por qué sigue PARCIAL:** **M_quota_regex_off**, **M_quota_phrases_empty** y **M_quota_exitcode_off** **sobreviven por separado** porque ningún test aísla cada señal de cuota; además `:194` devuelve `CodeAgentError` ante `"status_code=429"` y `"HTTP/1.1 429"` sin frase explícita. |
| **F2B-03** | **Alto** | **CERRADO** *(con nota mutacional)* | `templates/static-web/playwright.config.mjs:2-21`; `src/agent_factory/scaffold.py:106-121`; `tests/test_template_and_scaffold.py:227-281` | `attachErrorGuards(page)` registra `console` (`msg.type() === "error"`) y `pageerror` y lanza `Error` en `assertNoErrors()`. **M20a**, **M20b** y **M_guard_or_to_and** mueren (**KILLED**). *Nota:* **M_console_msg_type_off** (omitir `:110-114`) sobrevive porque los casos negativos omiten `page.on("console")` completo en vez de definir `page.on("console", () => {})` sin filtro `error`. |
| **F2B-04** | **Medio** | **CERRADO** | `src/agent_factory/scaffold.py:140-160`; `tests/test_template_and_scaffold.py:284-360` | Rechaza de forma aislada `dependencies`, `devDependencies`, `peerDependencies` y URLs `http://`/`https://`/`ws://`/`wss://`. **M1**, **M2**, **M21a**, **M21b** mueren. |
| **F2B-05** | **Medio** | **CERRADO** | `src/agent_factory/workspace.py:14, 19`; `tests/test_test_lock_and_generation.py:417-452` | `compute_test_lock` y `verify_and_restore_test_lock` detectan y restauran tampering/borrado de `playwright.config.mjs` y `package.json`. **M15** muere. |
| **F2B-06** | **Medio** | **CERRADO** | `src/agent_factory/scaffold.py:180-204`; `tests/test_template_and_scaffold.py:363-398` | Añadido el caso 3 (`ws_dir / "tests"` con symlink/junction escapando fuera del workspace en `:391-398`), cubriendo `scaffold.py:198` y matando **M23b** (**KILLED**). |
| **F2B-07** | **Bajo** | **PARCIAL** *(resuelto en `agy_cli_adapter.py` y docs; pendiente `scaffold.py:62`)* | `src/agent_factory/adapters/agy_cli_adapter.py:79-90, 224-245`; `src/agent_factory/scaffold.py:61-62, 154-155`; `tests/test_agy_cli_adapter.py:756-848`; `docs/PHASE_REPORT_2B.md:48-55, 66` | Se eliminó el código muerto `expected_ws_dir != ws_path` (`validate_workspace_cwd`, ahora **M24** muere), se cubrió `:224-245` dejando **0** líneas sin cubrir en `agy_cli_adapter.py` (`237->233` es inalcanzable con `json.loads("{...}")`) y matando `M_tail_json_off`/`M_non_dict_json_break`, y se corrigió `PHASE_REPORT_2B.md`. Queda `scaffold.py:62` sin cubrir (`template_id="."` no probado; **M_tpl_equal_root_off** sobrevive). |
| **F2B-08** | **Bajo** | **CERRADO** | `tests/test_agy_cli_adapter.py:360-389` | Timeouts estabilizados (`5.0s` para cuota/crash y `0.3s` aislado para timeout). |
| **F2B-09** | **Medio** | **CERRADO** | `src/agent_factory/scaffold.py:12-17, 98-103`; `tests/test_template_and_scaffold.py:94-118` | Nuevos vectores `missing-index-web` y `missing-styles-web` con `playwright.config.mjs` presente ejercen `REQUIRED_STATIC_WEB_FILES`; **M5** y **M5b** ahora **MUEREN (KILLED)**. |

---

## 3. Resultado de las mutaciones ejecutadas (en worktree temporal)

Todas las mutaciones se ejecutaron en un `git worktree` temporal fuera del workspace (`$TEMP/af-audit-2b2-wt`, `HEAD 04899fb`) con `PYTHONPATH=$TEMP/af-audit-2b2-wt/src` verificado explícitamente (`IMPORT: ...\af-audit-2b2-wt\src\agent_factory\scaffold.py`), restaurando con `git checkout -- .` tras cada corrida. Al finalizar se ejecutó `git worktree remove --force`, `git worktree prune` y se verificó con `git worktree list` que solo existe el workspace principal y el árbol está limpio.

| ID | Archivo y líneas mutadas | Descripción de la mutación | Resultado real | Evidencia de tests (o motivo de supervivencia) |
|---|---|---|---|---|
| **M5** | `scaffold.py:98-103` | Desactivar bucle de archivos requeridos (`for req in ():`) | **DETECTADA (KILLED)** | `1 failed, 7 passed` — `test_inspect_template_rejects_missing_files_external_network_backend_or_populated_src` |
| **M5b** | `scaffold.py:12-17` | Reducir `REQUIRED_STATIC_WEB_FILES` a `("package.json", "playwright.config.mjs")` | **DETECTADA (KILLED)** | `1 failed, 7 passed` — `test_inspect_template_rejects_missing_files_external_network_backend_or_populated_src` |
| **M19b_reintro** | `agy_cli_adapter.py:193-198` | Reintroducir regla ad-hoc `\b429\b and not line 429` | **DETECTADA (KILLED)** | `1 failed, 9 passed` — `test_agy_cli_adapter_does_not_misclassify_429_substring_on_success_or_non_quota_failure` |
| **M23b** | `scaffold.py:195-200` | Desactivar guarda de symlink/junction escapante en `ws_dir / "tests"` (`if False:`) | **DETECTADA (KILLED)** | `1 failed, 7 passed` — `test_template_scaffold_rejects_destination_symlink_src_or_prepopulated_tests` |
| **M24** | `agy_cli_adapter.py:79-82` | Desactivar `if ws_path.parent != base_dir:` en `validate_workspace_cwd` | **DETECTADA (KILLED)** | `1 failed, 9 passed` — `test_agy_cli_adapter_rejects_nested_workspace_subdir_and_handles_multiline_or_plain_stdout` |
| **M20a** | `scaffold.py:108-114` | Forzar `has_console_guard = True` | **DETECTADA (KILLED)** | `1 failed, 7 passed` — `test_static_web_template_and_inspect_template_enforce_console_error_and_pageerror_guard` |
| **M20b** | `scaffold.py:115-117` | Forzar `has_pageerror_guard = True` | **DETECTADA (KILLED)** | `1 failed, 7 passed` — `test_static_web_template_and_inspect_template_enforce_console_error_and_pageerror_guard` |
| **M_guard_or_to_and** | `scaffold.py:118` | Cambiar `if not has_console_guard or not has_pageerror_guard:` por `and` | **DETECTADA (KILLED)** | `1 failed, 7 passed` — `test_static_web_template_and_inspect_template_enforce_console_error_and_pageerror_guard` |
| **M_console_msg_type_off** | `scaffold.py:108-114` | Omitir la segunda condición `type() === "error" or console.error` dejando solo `'page.on("console"'` | **SOBREVIVIÓ (SURVIVED)** | `287 passed` en suite completa — ningún caso negativo define `page.on("console", () => {})` sin filtro de error (**F2B-03**). |
| **M_quota_phrases_empty** | `agy_cli_adapter.py:180-188` | Vaciar `quota_phrases = ()` dejando `exit_code in {42, 429}` y la regex | **SOBREVIVIÓ (SURVIVED)** | `287 passed` en suite completa — ningún test emite una frase de cuota sin `429` ni `exit_code=42` (**F2B-02**). |
| **M_quota_regex_off** | `agy_cli_adapter.py:192-198` | Desactivar la regex `r"\b(?:http\|status\|error\|code)\s*[:=]?\s*429\b"` (`or False`) | **SOBREVIVIÓ (SURVIVED)** | `287 passed` en suite completa — `:708` combina `"HTTP 429"` con `"Too Many Requests"` (atrapado por `quota_phrases`), y `:350`/`506` usan `exit_code=42` (**F2B-02**). |
| **M_quota_exitcode_off** | `agy_cli_adapter.py:190` | Reemplazar `res.exit_code in {42, 429}` por `False` | **SOBREVIVIÓ (SURVIVED)** | `287 passed` en suite completa — los tests con `exit_code=42` (`:350`, `:506`) incluyen además `"Quota exhausted"` en `stderr` (**F2B-02**). |
| **M_quota_phrases_AND_regex_off** | `agy_cli_adapter.py:189-199` | Dejar `is_quota = res.exit_code in {42, 429}` únicamente | **DETECTADA (KILLED)** | `1 failed, 9 passed` — `test_agy_cli_adapter_does_not_misclassify_429_substring_on_success_or_non_quota_failure` |
| **M_quota_no_exit_check** | `agy_cli_adapter.py:178` | Evaluar `is_quota` sin comprobar `if res.exit_code != 0:` (`if True:`) | **DETECTADA (KILLED)** | `7 failed, 3 passed` en `test_agy_cli_adapter.py` |
| **M_quota_all_off** | `agy_cli_adapter.py:201` | Desactivar por completo la detección de cuota (`if False:`) | **DETECTADA (KILLED)** | `3 failed, 7 passed` en `test_agy_cli_adapter.py` |
| **M_tail_json_off** | `agy_cli_adapter.py:233` | Desactivar bucle de JSON en línea final (`for line in []:`) | **DETECTADA (KILLED)** | `1 failed, 9 passed` — `test_agy_cli_adapter_rejects_nested_workspace_subdir_and_handles_multiline_or_plain_stdout` |
| **M_non_dict_json_break** | `agy_cli_adapter.py:227-231` | Eliminar guarda `isinstance(data, dict)` al parsear JSON completo | **DETECTADA (KILLED)** | `1 failed, 9 passed` — `test_agy_cli_adapter_rejects_nested_workspace_subdir_and_handles_multiline_or_plain_stdout` |
| **M_tpl_equal_root_off** | `scaffold.py:61` | Omitir `or tpl_dir == self.templates_root` en `_resolve_template_dir` | **SOBREVIVIÓ (SURVIVED)** | `287 passed` en suite completa — ningún test invoca `inspect_template(".")` (`scaffold.py:62` sigue en `Missing`, **F2B-07**). |

---

## 4. Acciones menores recomendadas (para cerrar al 100 % las brechas restantes)

1. **Aislar vectores de cuota en `tests/test_agy_cli_adapter.py`** (para matar **M_quota_regex_off**, **M_quota_phrases_empty** y **M_quota_exitcode_off**): agregar 3 casos mínimos con `exit_code=1` + `"status: 429"` (sin `"too many requests"`), `exit_code=1` + `"rate limit exceeded"` (sin `429`), y `exit_code=429` con `stderr=""`. Opcionalmente ampliar `:194` en `agy_cli_adapter.py` para aceptar `"status_code=429"` y `"HTTP/1.1 429"`.
2. **Aislar guarda de consola en `tests/test_template_and_scaffold.py`** (para matar **M_console_msg_type_off**) y **cubrir `template_id="."`** (para matar **M_tpl_equal_root_off** y cubrir `scaffold.py:62`).

---

## 5. Lo que NO se pudo verificar y por qué

1. **Ejecución en vivo de `templates/static-web/` con Node (`node --test`), Playwright y navegador headless Chromium (`AC-2.5`):**
   - La suite automatizada corre bajo `pytest-socket` (`--disable-socket`, `INV-03`) y solo valida estáticamente el archivo `playwright.config.mjs` y la existencia/contrato de `attachErrorGuards`; no se ejecutaron `node` ni Chromium contra `index.html` dentro de esta auditoría.
2. **Binario oficial `agy` autenticado (`INV-19`) y rama POSIX de `SubprocessRunner` (`src/agent_factory/adapters/subprocess_runner.py:66-95`):**
   - El entorno de auditoría es Windows (`win32`), por lo que la rama POSIX (`start_new_session=True` + `os.killpg`) solo se ejecuta en Linux CI, y las pruebas de `AgyCliAdapter` utilizan scripts Python locales deterministas bajo `SubprocessRunner` (`INV-03`, `INV-19`).
