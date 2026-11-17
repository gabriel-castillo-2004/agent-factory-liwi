# PHASE_REPORT_2A — Núcleo offline del constructor (Fase 2a)

## 1. Archivos creados / modificados
- **Puertos:**
  - `src/agent_factory/ports/runner.py`: Definición del protocolo `Runner`, modelo `CommandResult` y excepción `CommandTimeoutError` (INV-02, INV-06).
  - `src/agent_factory/ports/vcs.py`: Definición del protocolo `VCS` y modelo `CIRunResult` (INV-02, INV-15).
  - `src/agent_factory/ports/code_agent.py`: Definición del protocolo `CodeAgent`, modelo `CodeAgentResult`, excepción base `CodeAgentError` y excepción de pausa de cuota `QuotaExhaustedError` (INV-02, INV-09, INV-13, ADR-002).
- **Adaptadores:**
  - `src/agent_factory/adapters/fake_runner.py`: Adaptador de comandos en memoria FIFO para pruebas deterministas offline (INV-02, INV-03).
  - `src/agent_factory/adapters/fake_vcs.py`: Adaptador VCS y CI en memoria FIFO para registrar pushes, consultas de CI y despliegues sin red (INV-02, INV-03, INV-15).
  - `src/agent_factory/adapters/fake_code_agent.py`: Adaptador de agente de código guionado para verificar aislamiento estricto de generación de pruebas e iteraciones de implementación/corrección (INV-02, INV-13, ADR-002).
  - `src/agent_factory/adapters/subprocess_runner.py`: Implementación multiplataforma de `Runner` que ejecuta comandos con timeout estricto y garantiza la terminación del árbol completo de procesos (`taskkill /F /T /PID` en Windows; `start_new_session=True` con `os.killpg` en POSIX) (A2a-2, INV-02, INV-06).
- **Workspace y Aislamiento:**
  - `src/agent_factory/workspace.py`:
    - `WorkspaceManager`: Manejo de directorios `workspaces/<project_id>/` con validación estricta de `project_id` (INV-22), resolución segura de rutas relativas y rechazo riguroso de directory traversal (`..`), rutas absolutas y junctions/symlinks que escapen del workspace (`SandboxViolationError`, INV-18, INV-22).
    - `compute_test_lock`: Cálculo determinista de huellas criptográficas SHA-256 para `tests/` y archivos de configuración del runner (`pyproject.toml`, `pytest.ini`, etc.) con snapshot de contenidos (INV-13, AC-2.2).
    - `verify_and_restore_test_lock`: Verificación determinista contra el snapshot; ante cualquier modificación, borrado de tests o agregado de archivos espurios, revierte automáticamente los archivos al snapshot original y reporta la alteración (INV-13, AC-2.2).
- **Bucle de Construcción y Cola Local:**
  - `src/agent_factory/builder.py`:
    - `LocalJobQueue`: Cola local en memoria para trabajos aprobados en `BUILDING`, soportando estados `QUEUED`, `RUNNING`, `PAUSED_QUOTA`, `COMPLETED` y `FAILED` (AC-2.4).
    - `BuildWorker`: Orquestador del bucle autónomo:
      - Rechaza proyectos no aprobados o fuera de `BUILDING` (`IllegalTransition`, INV-04, INV-05).
      - **Paso A**: Verifica la ausencia de archivos en `src/` antes de la generación de tests (INV-13), invoca `code_agent.generate_tests` pasando únicamente `acceptance_tests` sin el Plan ni código de producción (ADR-002, A2a-5), valida que se generen pruebas no vacías y sella el `TestLockSnapshot` (AC-2.2).
      - **Paso B**: Bucle autónomo escribir -> probar -> corregir. Antes de cada intento valida el presupuesto del circuit breaker (`BUILDING=15`, INV-17). Ante `QuotaExhaustedError` pausa el trabajo en `PAUSED_QUOTA`, ejecuta backoff vía `Clock.sleep` y conserva el estado sin consumir llamadas erróneamente (INV-09, AC-2.4).
      - Verifica y restaura el test-lock antes de cada ejecución de pruebas; si fue alterado, descarta la corrida, no pushea y cuenta el intento como fallido (INV-13).
      - Ejecuta pruebas locales y registra eventos append-only `TESTS_UPDATED` (INV-11) para proyectar avance proporcional (35..90 % en `BUILDING`, INV-12).
      - Empuje mínimo: solo envía a VCS cuando las pruebas locales pasan (INV-15, A2a-3).
      - Realiza verificación remota en CI respetando el tope de 3 rondas (INV-15); al aprobar CI, hace merge/deploy, emite `PROJECT_DELIVERED` y escribe `PROJECT_PROMPT.md` en el workspace. Si se agotan las rondas de CI o el presupuesto de llamadas, transiciona a `FAILED` con `BuildReport`.
- **Decisiones y Trazabilidad:**
  - `docs/decisions/ADR-002-code-agent-port.md`: Justificación arquitectónica del puerto `CodeAgent` y separación de `generate_tests` vs `implement_or_fix` (INV-08, INV-13).
  - `docs/PHASE2A_TEST_MAP.md`: Matriz de trazabilidad de tests de la Fase 2a.

## 2. Resultado de la suite y cobertura verificada
- **Comando de verificación:** `pytest --cov=src/agent_factory --cov-report=term-missing`
- **Resultados de las pruebas:**
  - Total: **261** (227 de Fase 1 + 34 de Fase 2a)
  - Pasadas: **261**
  - Falladas: **0**
  - Omitidas: **0**
  - Advertencias: 2 (bloqueo de socket por `pytest-socket`, conforme a INV-03)
  - Tiempo de ejecución: **~5.3 segundos**
- **Cobertura real por módulo:**
  - `src/agent_factory/domain/`: **98 %**
  - `src/agent_factory/ports/`: **85 %**
  - `src/agent_factory/adapters/`: **80 %**
  - `src/agent_factory/workspace.py`: **91 %**
  - `src/agent_factory/builder.py`: **82 %**
  - `src/agent_factory/orchestrator.py`: **90 %**
  - Cobertura global total en `src/agent_factory`: **89 %** (superior al umbral requerido del 85 %).
- **Linter:** `ruff check .` -> Pasó sin observaciones (`All checks passed!`).
- **Inmutabilidad de pruebas (INV-13):** `git diff phase-2a-tests --stat -- tests/ pyproject.toml` es idénticamente vacío (cero modificaciones).

## 3. Criterios de Aceptación (AC) y requerimientos cumplidos
- [x] **AC-2.1**: Bucle autónomo escribir -> probar -> corregir ejecutado localmente, converge ante escenario guionado, respeta aprobaciones previas en `BUILDING` y emite `BuildReport` exacto.
- [x] **AC-2.2**: Test-lock criptográfico por SHA-256 de `tests/` y configuración del runner (`pyproject.toml`, etc.); detección y restauración automática ante manipulación contando el intento como fallido sin ejecutar runner ni pushear a VCS (INV-13, INV-23).
- [x] **AC-2.3**: Presupuestos duros respetados: `BUILDING=15` invocaciones del agente de código acumulativas sin reseteo (INV-17) y máximo 3 rondas de CI (INV-15), derivando a `FAILED` con reporte descriptivo si se agotan.
- [x] **AC-2.4**: Manejo resiliente de cuota: `QuotaExhaustedError` pausa el trabajo en `PAUSED_QUOTA` sin marcar `FAILED`, ejecuta backoff vía `Clock.sleep` y reanuda sin pérdida de estado ni consumo erróneo de presupuesto (INV-09). Errores fatales `CodeAgentError` derivan a `FAILED` inmediatamente.
- [x] **A2a-1**: Aislamiento estricto de workspace (`workspaces/<project_id>/`) validando `project_id` con regex del dominio y previniendo escapes mediante traversals (`..`), rutas absolutas y enlaces simbólicos o junctions de Windows (INV-18, INV-22).
- [x] **A2a-2**: Ejecutor de comandos con timeout multiplataforma que mata tanto el proceso padre como todos sus subprocesos hijos, sin colgarse (INV-02, INV-06).
- [x] **A2a-3**: Empuje mínimo a VCS: solo se hace push al pasar las pruebas locales o agotarse el presupuesto; nunca en fallos locales intermedios (INV-15).
- [x] **A2a-4**: Registro de eventos append-only (`AGENT_INVOKED`, `TESTS_UPDATED`, `CI_ROUND_COMPLETED`, etc.) permitiendo a `project_status_from_events` proyectar el progreso porcentual proporcional (35..90 % en `BUILDING`) y generar `BuildReport` (INV-11, INV-12).
- [x] **A2a-5**: Aislamiento estricto en generación de tests: `generate_tests` solo recibe `acceptance_tests`, no tiene acceso al código de producción ni al Plan, y rechaza contaminación previa de `src/` (INV-13).

## 4. Decisiones tomadas y supuestos
- **ADR-002**: Diseño del puerto `CodeAgent` separando formalmente la generación aislada de pruebas de aceptación (`generate_tests`) de la implementación y corrección del código (`implement_or_fix`).
- **Limpieza de procesos en Windows vs POSIX**: Se implementó una terminación limpia garantizada del árbol de procesos mediante `taskkill /F /T /PID` en Windows y grupos de procesos con `os.killpg` en POSIX para evitar procesos huérfanos o bloqueos tras timeout.
- **Formato determinista del Test-Lock**: Las entradas de archivos se ordenan alfabéticamente por ruta POSIX relativa y se delimitan con bytes nulos (`\0`), garantizando hashes independientes de la plataforma y del orden de recorrido en disco.

## 5. Riesgos o deudas detectadas
- Ninguna deuda técnica bloqueante para el alcance de la Fase 2a.
- En la Fase 2b se implementarán los adaptadores reales con red y CLI (`AgyCliAdapter`), las plantillas en `templates/` y las pruebas e2e en navegador headless con Playwright.
