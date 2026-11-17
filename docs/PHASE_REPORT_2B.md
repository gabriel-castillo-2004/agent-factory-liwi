# PHASE_REPORT_2B — Adaptador real AgyCliAdapter y plantilla estática base (Fase 2b endurecida)

## 1. Archivos creados / modificados
- **Plantilla base estática (`templates/static-web/`):**
  - `templates/static-web/package.json`: Configuración de app 100 % estática con scripts `test`, `test:unit` (`node --test`) y `test:e2e` (`playwright test`), `dependencies: {}`, sin backend ni dependencias remotas (A2b-1, INV-16, INV-20).
  - `templates/static-web/playwright.config.mjs`: Configuración offline para Playwright (`headless: true`, `offline: true`, viewport estándar, reporter `list`) con guarda estática `attachErrorGuards(page)` que escucha eventos `"console"` (`msg.type() === "error"`) y `"pageerror"`, lanzando error explícito ante cualquier error de consola o página en navegador headless (INV-03, INV-13, AC-2.5, F2B-03).
  - `templates/static-web/index.html`: Esqueleto HTML semántico con contenedor `<main id="app">` y carga de script modular estático `./src/app.js`, sin CDNs ni URLs externas (INV-16).
  - `templates/static-web/styles.css`: Estilos CSS base limpios con variables CSS para temas y tipografía estándar del sistema.
- **Scaffolding e Inspección (`src/agent_factory/scaffold.py`):**
  - `inspect_template`:
    - Valida la presencia de archivos requeridos y que `src/` esté vacío antes de generar pruebas (`A2a-5`, `INV-13`).
    - Parsea `package.json` y valida de forma estricta la ausencia de `dependencies`, `devDependencies` y `peerDependencies` no autorizadas (`A2b-1`, `INV-16`, F2B-04).
    - Valida que `playwright.config.mjs` contenga obligatoriamente los listeners activos para `console.error` y `pageerror` (F2B-03, AC-2.5, INV-13).
    - Escanea archivos de texto y rechaza referencias a URLs y protocolos externos (`http://`, `https://`, `ws://`, `wss://`) (`A2b-1`, `INV-16`, F2B-04).
  - `scaffold`:
    - Valida el aislamiento con `WorkspaceManager`, rechaza `project_id` o `template_id` inválidos (`SandboxViolationError`).
    - Detecta y rechaza si el workspace de destino ya tiene enlaces simbólicos/junctions en `src/` o `tests/` que apunten fuera del sandbox (`A2b-2`, `INV-18`, `INV-22`, F2B-06).
    - Detecta y rechaza si el workspace de destino ya contiene archivos preexistentes en `src/` o `tests/` (F2B-06).
    - Materializa la plantilla asegurando que `src/` quede creado vacío.
- **Adaptador de Agente de Código CLI (`src/agent_factory/adapters/agy_cli_adapter.py`):**
  - `validate_workspace_cwd`: Exige que `workspace_dir` esté confinado como hijo directo dentro de `workspaces/<project_id>/` (rechazando subdirectorios anidados) y corresponda a un `project_id` válido (`A2b-3`, `INV-18`, `INV-22`, F2B-07).
  - `build_clean_env`: Construye un entorno limpio con lista blanca estricta de variables del sistema (`PATH`, `SYSTEMROOT`, `COMSPEC`, `PATHEXT`, `WINDIR`, `HOME`, `USERPROFILE`, `TMP`, `TEMP`, `TMPDIR`, `LANG`, `LC_ALL`) más las variables del CLI (`AGY_HOME`, `AGY_CONFIG_DIR`), filtrando cualquier secreto del host (`TOKEN`, `SECRET`, `API_KEY`, `PASSWORD`, `CREDENTIAL`, etc.) (`A2b-3`, `INV-19`).
  - `build_test_generation_payload`: Construye el payload para `generate_tests` con `acceptance_tests` sin filtrar información del `Plan` ni código de producción (`A2a-5`, `A2b-4`).
  - `build_implementation_payload`: Construye el payload para `implement_or_fix` serializando el `Plan`, `attempt` y el reporte de fallos previo (`A2b-4`).
  - `_run_cli`:
    - Evalúa señales de cuota **únicamente ante códigos de salida no nulos (`exit_code != 0`)** (F2B-02), discriminando con precisión señales reales de cuota/rate limit (`42`, `429`, `"too many requests"`, `"quota exhausted"`) sin clasificar erróneamente ejecuciones exitosas ni referencias en código como `"line 429"` (F2B-02).
    - Parsea la salida estructurada (`files_written`, `summary`) soportando JSON multilínea con formato o líneas individuales en reversa, con degradación segura a texto plano (F2B-07).
- **Construcción y Máquina de Estados (`src/agent_factory/builder.py`):**
  - `BuildWorker.run_job`:
    - Captura `CommandTimeoutError` tanto en el Paso A (`generate_tests`) como en el Paso B (`implement_or_fix`) (F2B-01, INV-06, INV-09, A2b-5, A2b-6).
    - Ante un timeout del CLI: registra la invocación en `CircuitBreaker` y `AGENT_INVOKED`, emite el evento `PROJECT_FAILED`, transiciona el trabajo y el proyecto a `FAILED` y retorna un `BuildReport` con la causa del fallo (evitando que el proyecto quede congelado en `BUILDING`).
- **Trazabilidad y Pruebas Reforzadas:**
  - `tests/test_agy_cli_adapter.py`: Tests añadidos para timeouts en `BuildWorker` (F2B-01), precisión en detección de 429 y `line 429` (F2B-02), subdirectorios anidados y parsing de stdout multilínea (F2B-07), y estabilización de timeouts en Windows (F2B-08).
  - `tests/test_template_and_scaffold.py`: Tests para verificación de guardas `console.error`/`pageerror` (F2B-03), rechazo aislado de `dependencies`, `devDependencies`, `peerDependencies` y URLs externas (F2B-04), rechazo de symlinks en destino y `tests/` pre-poblado (F2B-06), y validaciones defensivas de plantilla y JSON malformado (F2B-07).
  - `tests/test_test_lock_and_generation.py`: Verificación de protección criptográfica de `playwright.config.mjs` y `package.json` contra manipulación o borrado (F2B-05).
  - `docs/PHASE2B_TEST_MAP.md`: Matriz de trazabilidad actualizada con las pruebas de endurecimiento.

## 2. Resultado de la suite y cobertura verificada
- **Comando de verificación:** `pytest -q --cov=src/agent_factory --cov-report=term-missing`
- **Resultados de las pruebas:**
  - Total: **287** (268 anteriores + 11 iniciales 2b + 8 de endurecimiento 2b.1)
  - Pasadas: **287**
  - Falladas: **0**
  - Omitidas: **0**
  - Advertencias: 2 (bloqueo de socket por `pytest-socket`, conforme a INV-03)
  - Tiempo de ejecución: **~17.4 segundos**
- **Cobertura real por módulo:**
  - `src/agent_factory/scaffold.py`: **96 %**
  - `src/agent_factory/workspace.py`: **94 %**
  - `src/agent_factory/adapters/agy_cli_adapter.py`: **89 %**
  - `src/agent_factory/builder.py`: **88 %**
  - `src/agent_factory/orchestrator.py`: **90 %**
  - `src/agent_factory/domain/`: **98 %** (`progress.py`: 96 %, `models.py`: 100 %, `events.py`: 100 %, `states.py`: 100 %)
  - `src/agent_factory/ports/`: **85 %**
  - Cobertura global total en `src/agent_factory`: **91 %** (superando holgadamente el umbral del 85 %).
- **Linter:** `ruff check .` -> Pasó sin observaciones (`All checks passed!`).
- **Inmutabilidad de pruebas (INV-13):** `git diff phase-2b.1-tests --stat -- tests/ pyproject.toml` es idénticamente vacío (cero modificaciones en `tests/` o `pyproject.toml` durante el Paso B').

## 3. Cierre de hallazgos de auditoría (F2B-01 a F2B-08)
- [x] **F2B-01 (Crítico)**: `BuildWorker.run_job` captura `(CodeAgentError, CommandTimeoutError)` en test generation y en implement/fix, contabilizando la llamada en el circuit breaker y registrando `PROJECT_FAILED`.
- [x] **F2B-02 (Alto)**: `AgyCliAdapter._run_cli` evalúa cuota solo si `res.exit_code != 0` y no clasifica subcadenas espurias como `"line 429"` o ejecuciones con éxito como cuota agotada.
- [x] **F2B-03 (Alto)**: `templates/static-web/playwright.config.mjs` incorpora la guarda e2e `attachErrorGuards(page)` para `console.error` y `pageerror`, e `inspect_template` valida su presencia.
- [x] **F2B-04 (Medio)**: `inspect_template` rechaza de forma aislada dependencias runtime, `devDependencies`, `peerDependencies` y URLs externas.
- [x] **F2B-05 (Medio)**: `compute_test_lock` y `verify_and_restore_test_lock` protegen `playwright.config.mjs` y `package.json` contra sabotaje o borrado.
- [x] **F2B-06 (Medio)**: `TemplateScaffold.scaffold` valida enlaces simbólicos/junctions en `src/` y `tests/` y rechaza destinos con archivos previos en cualquiera de ambas carpetas.
- [x] **F2B-07 (Bajo)**: Ramas defensivas de `template_id`, `package.json` corrupto, subdirectorios anidados de cwd y parseo multilínea de stdout cubiertos al 100 %.
- [x] **F2B-08 (Bajo)**: Pruebas de timeout en Windows estabilizadas con tiempos holgados para invocaciones no relacionadas.

## 4. Decisiones tomadas y supuestos
- **Propagación y captura de CommandTimeoutError**: `AgyCliAdapter` permite la propagación natural de `CommandTimeoutError` desde `SubprocessRunner`, mientras que `BuildWorker.run_job` captura la tupla `(CodeAgentError, CommandTimeoutError)` asegurando el registro en `CircuitBreaker` y la transición a `JobState.FAILED` con reporte explícito.
- **Protección completa de dependencias**: La inspección de plantillas estáticas bloquea tanto `dependencies` como `devDependencies` y `peerDependencies`, garantizando que ninguna plantilla estática introduzca servidores o herramientas que requieran conexión de red en tiempo de ejecución.
- **Aislamiento preventivo en destino**: Antes de escribir cualquier archivo de la plantilla en el workspace destino, `TemplateScaffold` comprueba no solo que `src/` y `tests/` no contengan archivos sino también que no sean enlaces simbólicos que redirijan fuera del sandbox asignado.

## 5. Riesgos o deudas detectadas
- La integración con el binario oficial `agy` en producción usará las mismas firmas probadas con los adaptadores offline.
- En la Fase 3 se integrará la interfaz de Telegram con canal interactivo y publicación de proyectos hacia la rama `registry`.
