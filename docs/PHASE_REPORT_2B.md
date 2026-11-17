# PHASE_REPORT_2B — Adaptador real AgyCliAdapter y plantilla estática base (Fase 2b)

## 1. Archivos creados / modificados
- **Plantilla base estática (`templates/static-web/`):**
  - `templates/static-web/package.json`: Configuración de app 100 % estática con scripts `test`, `test:unit` (`node --test`) y `test:e2e` (`playwright test`), `dependencies: {}`, sin backend ni dependencias remotas (A2b-1, INV-16, INV-20).
  - `templates/static-web/playwright.config.mjs`: Configuración offline para Playwright (`headless: true`, `offline: true`, viewport estándar, reporter `list`) (INV-03, INV-13, AC-2.5).
  - `templates/static-web/index.html`: Esqueleto HTML semántico con contenedor `<main id="app">` y carga de script modular estático `./src/app.js`, sin CDNs ni URLs externas (INV-16).
  - `templates/static-web/styles.css`: Estilos CSS base limpios con variables CSS para temas y tipografía estándar del sistema.
- **Scaffolding e Inspección:**
  - `src/agent_factory/scaffold.py`: Implementación de `TemplateScaffold` y modelo `TemplateInspection`:
    - `inspect_template`: Valida la presencia de archivos requeridos, que `src/` esté vacío antes de generar pruebas (`A2a-5`, `INV-13`), parsea `package.json` y rechaza dependencias de backend, bases de datos o URLs externas (`http://`, `https://`, etc.) (`A2b-1`, `INV-16`).
    - `scaffold`: Valida el aislamiento con `WorkspaceManager`, rechaza `project_id` o `template_id` inválidos (`SandboxViolationError`), detecta workspaces ya contaminados en `src/`, bloquea enlaces simbólicos o junctions que apunten fuera de `templates_root` y materializa la plantilla en `workspaces/<project_id>/` asegurando que `src/` quede creado vacío (`A2b-2`, `INV-18`, `INV-22`).
- **Adaptador de Agente de Código CLI:**
  - `src/agent_factory/adapters/agy_cli_adapter.py`: Implementación del puerto `CodeAgent` (`AgyCliAdapter`):
    - `validate_workspace_cwd`: Exige que el directorio de trabajo esté contenido dentro de `workspaces/<project_id>/` y corresponda a un `project_id` válido antes de invocar cualquier comando (`A2b-3`, `INV-18`, `INV-22`).
    - `build_clean_env`: Construye un entorno limpio con una lista blanca estricta de variables del sistema (`PATH`, `SYSTEMROOT`, `COMSPEC`, `PATHEXT`, `WINDIR`, `HOME`, `USERPROFILE`, `TMP`, `TEMP`, `TMPDIR`, `LANG`, `LC_ALL`) más las variables permitidas por configuración (`AGY_HOME`, `AGY_CONFIG_DIR`), filtrando cualquier secreto del host (`TOKEN`, `SECRET`, `API_KEY`, `PASSWORD`, `CREDENTIAL`, etc.) (`A2b-3`, `INV-19`).
    - `build_test_generation_payload`: Construye el payload para `generate_tests` conteniendo única y exclusivamente `acceptance_tests` y la ruta del workspace, rechazando si `src/` ya tiene código de producción (`A2a-5`, `A2b-4`).
    - `build_implementation_payload`: Construye el payload para `implement_or_fix` serializando el `Plan`, `attempt` y el reporte de fallos previo (`A2b-4`).
    - `_run_cli`: Ejecuta el CLI mediante el puerto `Runner` (`SubprocessRunner`) pasando `--mode` y `--payload`, heredando timeouts estrictos y terminación del árbol de subprocesos (`INV-06`). Traduce señales de cuota (`429`, exit codes 42/429 o menciones en salida) a `QuotaExhaustedError` con `retry_after_seconds` (`INV-09`), errores fatales a `CodeAgentError` y parsea el resultado estructurado `files_written` / `summary` (`A2b-5`).
- **Puertos, Adaptadores y Workspace actualizados:**
  - `src/agent_factory/ports/runner.py`: Parámetro opcional `env: Mapping[str, str] | None = None` añadido a la firma de `Runner.run`.
  - `src/agent_factory/adapters/subprocess_runner.py`: `SubprocessRunner.run` propaga el diccionario `env` explícito a `subprocess.Popen` sin heredar el entorno del host (`INV-19`).
  - `src/agent_factory/adapters/fake_runner.py`: Registra `"env"` en su historial `calls` para verificación en tests offline.
  - `src/agent_factory/domain/models.py`: Registro de `"static-web"` en `VALID_TEMPLATES`.
  - `src/agent_factory/workspace.py`: Incorporación de `"playwright.config.mjs"`, `"playwright.config.js"` y `"vitest.config.js"` a `RUNNER_CONFIG_FILENAMES`.
- **Trazabilidad:**
  - `docs/PHASE2B_TEST_MAP.md`: Matriz de trazabilidad de tests de la Fase 2b para criterios A2b-1 a A2b-6.

## 2. Resultado de la suite y cobertura verificada
- **Comando de verificación:** `pytest --cov=src/agent_factory --cov-report=term-missing`
- **Resultados de las pruebas:**
  - Total: **279** (268 anteriores + 11 nuevos de Fase 2b)
  - Pasadas: **279**
  - Falladas: **0**
  - Omitidas: **0**
  - Advertencias: 2 (bloqueo de socket por `pytest-socket`, conforme a INV-03)
  - Tiempo de ejecución: **~13.9 segundos**
- **Cobertura real por módulo:**
  - `src/agent_factory/domain/`: **98 %** (`progress.py`: 96 %, `models.py`: 100 %, `events.py`: 100 %, `states.py`: 100 %)
  - `src/agent_factory/ports/`: **85 %**
  - `src/agent_factory/adapters/`: **85 %** (`agy_cli_adapter.py`: 92 %, `fake_vcs.py`: 87 %, `file_publisher.py`: 92 %, `sqlite_store.py`: 96 %, `memory_store.py`: 100 %, `subprocess_runner.py`: 57 %)
  - `src/agent_factory/scaffold.py`: **90 %**
  - `src/agent_factory/workspace.py`: **94 %**
  - `src/agent_factory/builder.py`: **88 %**
  - `src/agent_factory/orchestrator.py`: **90 %**
  - Cobertura global total en `src/agent_factory`: **90 %** (superior al umbral requerido del 85 %).
- **Linter:** `ruff check .` -> Pasó sin observaciones (`All checks passed!`).
- **Inmutabilidad de pruebas (INV-13):** `git diff phase-2b-tests --stat -- tests/ pyproject.toml` es idénticamente vacío (cero modificaciones).

## 3. Criterios de Aceptación (AC) y requerimientos cumplidos
- [x] **A2b-1**: La plantilla `templates/static-web/` existe, contiene todos los archivos estáticos requeridos (`package.json`, `playwright.config.mjs`, `index.html`, `styles.css`), valida la ausencia de dependencias remotas/backend o URLs externas, y mantiene `src/` vacío.
- [x] **A2b-2**: `TemplateScaffold` materializa la plantilla dentro de `workspaces/<project_id>/` apoyándose en `WorkspaceManager` (INV-22), valida que `src/` quede creado vacío y rechaza `project_id` inválidos, `template_id` con traversal, destinos ya contaminados o symlinks que apunten fuera de `templates_root`.
- [x] **A2b-3**: `AgyCliAdapter` valida que `workspace_dir` esté confinado dentro de `workspaces/<project_id>/` (INV-18), ejecuta el comando con `cwd=workspace_dir` y filtra secretos del host manteniendo solo la lista blanca del sistema y del CLI (INV-19).
- [x] **A2b-4**: `AgyCliAdapter.generate_tests` construye su carga útil únicamente con `acceptance_tests` sin filtrar información del `Plan` ni código de `src/` (A2a-5, INV-13), mientras que `implement_or_fix` provee el `Plan` y la salida de fallo previa.
- [x] **A2b-5**: `AgyCliAdapter` mapea señales de cuota agotada a `QuotaExhaustedError` con `retry_after_seconds` (INV-09), errores de ejecución a `CodeAgentError`, y respeta los timeouts del `Runner` (INV-06).
- [x] **A2b-6**: Integración de extremo a extremo offline: `TemplateScaffold` + `WorkspaceManager` + `AgyCliAdapter` (contra CLI simulado local) + `BuildWorker` convergen y entregan el proyecto con `BuildReport` exacto, soportando pausas por cuota y detención ante fallos irrecuperables.

## 4. Decisiones tomadas y supuestos
- **Protocolo de comunicación CLI estructurado**: `AgyCliAdapter` utiliza argumentos estándar `--mode <mode>` y `--payload <json>` para pasar entradas estructuradas al binario CLI y parsea la salida JSON (`files_written`, `summary`) de la última línea de stdout, permitiendo compatibilidad directa con herramientas CLI de agentes locales sin depender de stdin interactivo.
- **Aislamiento de entorno en SubprocessRunner**: Para cumplir con INV-19, cuando se proporciona un diccionario `env`, `SubprocessRunner.run` lo pasa directamente a `subprocess.Popen(..., env=dict(env))`, asegurando que el proceso hijo no herede variables de entorno no autorizadas del proceso padre.
- **Protección de archivos de configuración en TestLock**: Se incorporaron `"playwright.config.mjs"`, `"playwright.config.js"` y `"vitest.config.js"` a `RUNNER_CONFIG_FILENAMES` en `workspace.py` para asegurar que las configuraciones de test de la plantilla `static-web` queden bloqueadas criptográficamente contra alteraciones durante el bucle de construcción (INV-13).

## 5. Riesgos o deudas detectadas
- La ejecución del CLI real depende de la disponibilidad del binario en la máquina local del usuario; el adaptador utiliza `sys.executable` o el comando configurado por el usuario con timeouts garantizados por el runner.
- En la Fase 3 se integrará la interfaz de Telegram por long polling y la publicación hacia la rama `registry`.
