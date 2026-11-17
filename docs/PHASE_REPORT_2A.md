# PHASE_REPORT_2A — Núcleo offline del constructor (Fase 2a y Endurecimiento 2a)

## 1. Archivos creados / modificados
- **Puertos:**
  - `src/agent_factory/ports/runner.py`: Definición del protocolo `Runner`, modelo `CommandResult` y excepción `CommandTimeoutError` (INV-02, INV-06).
  - `src/agent_factory/ports/vcs.py`: Definición del protocolo `VCS` y modelo `CIRunResult` (INV-02, INV-15).
  - `src/agent_factory/ports/code_agent.py`: Definición del protocolo `CodeAgent`, modelo `CodeAgentResult`, excepción base `CodeAgentError` y excepción de pausa de cuota `QuotaExhaustedError` (INV-02, INV-09, INV-13, ADR-002).
- **Adaptadores:**
  - `src/agent_factory/adapters/fake_runner.py`: Adaptador de comandos en memoria FIFO para pruebas deterministas offline (INV-02, INV-03).
  - `src/agent_factory/adapters/fake_vcs.py`: Adaptador VCS y CI en memoria FIFO para registrar pushes, consultas de CI y despliegues sin red (INV-02, INV-03, INV-15).
  - `src/agent_factory/adapters/fake_code_agent.py`: Adaptador de agente de código guionado para verificar aislamiento estricto de generación de pruebas e iteraciones de implementación/corrección (INV-02, INV-13, ADR-002).
  - `src/agent_factory/adapters/subprocess_runner.py`: Implementación multiplataforma de `Runner` que valida el comando (rechazando secuencias vacías o `str`/`bytes` directos, F2A-09), ejecuta con timeout estricto y garantiza la terminación del árbol completo de procesos (`taskkill /F /T /PID` en Windows; `start_new_session=True` con `os.killpg` en POSIX) (A2a-2, INV-02, INV-06).
- **Workspace y Aislamiento:**
  - `src/agent_factory/workspace.py`:
    - `WorkspaceManager`: Manejo de directorios `workspaces/<project_id>/` con validación estricta de `project_id` (INV-22), resolución segura de rutas relativas y rechazo riguroso de directory traversal (`..`), rutas absolutas y junctions/symlinks que escapen del workspace (`SandboxViolationError`, INV-18, INV-22).
    - `compute_test_lock`: Cálculo determinista de huellas criptográficas SHA-256 para `tests/` y archivos de configuración del runner (`pyproject.toml`, `pytest.ini`, `setup.cfg`, `conftest.py`, etc., F2A-06) con snapshot de contenidos (INV-13, AC-2.2).
    - `verify_and_restore_test_lock`: Verificación determinista contra el snapshot; ante cualquier modificación, borrado de tests o agregado de archivos espurios en `tests/` o raíz de configuración, revierte automáticamente los archivos al snapshot original y reporta la alteración (INV-13, AC-2.2, F2A-06).
- **Bucle de Construcción y Cola Local:**
  - `src/agent_factory/builder.py`:
    - `LocalJobQueue`: Cola local en memoria para trabajos aprobados en `BUILDING`, soportando estados `QUEUED`, `RUNNING`, `PAUSED_QUOTA`, `COMPLETED` y `FAILED` (AC-2.4).
    - `BuildWorker`: Orquestador del bucle autónomo:
      - Rechaza proyectos no aprobados o fuera de `BUILDING` (`IllegalTransition`, INV-04, INV-05).
      - **Paso A**: Verifica la ausencia de archivos en `src/` exclusivamente durante la inicialización inicial del Paso A (`job.lock_snapshot is None`), permitiendo que el proyecto reanude sin falsos positivos de contaminación tras pausas de cuota previas (F2A-01, F2A-08, INV-13). Invoca `code_agent.generate_tests` pasando únicamente `acceptance_tests` sin el Plan ni código de producción (ADR-002, A2a-5). Ante `QuotaExhaustedError`, pausa el trabajo sin descontar del `CircuitBreaker` ni emitir `AGENT_INVOKED` (F2A-03). Valida que se generen pruebas no vacías y sella el `TestLockSnapshot` (AC-2.2).
      - **Paso B**: Bucle autónomo escribir -> probar -> corregir. Antes de cada intento valida el presupuesto del circuit breaker (`BUILDING=15`, INV-17) sincronizándolo con las llamadas acumuladas centrales (F2A-04). Registra la llamada consumida y emite `AGENT_INVOKED` ante intentos tanto exitosos como fallidos por `CodeAgentError` (F2A-04). Ante `QuotaExhaustedError`, pausa el trabajo en `PAUSED_QUOTA` sin consumir presupuesto erróneamente (INV-09, AC-2.4).
      - Verifica y restaura el test-lock antes de cada ejecución de pruebas; si fue alterado, descarta la corrida, no pushea y cuenta el intento como fallido (INV-13).
      - Ejecuta pruebas locales envolviendo `runner.run` en captura segura de `CommandTimeoutError` y excepciones del sistema operativo/adaptador; si falla por timeout o error de runner, registra el intento fallido o transiciona a `FAILED` con `BuildReport` sin dejar nunca el proyecto atascado (F2A-02, INV-06, INV-09).
      - Valida `local_tests_passed` exigiendo `runner_result.exit_code == 0` y que todos los identificadores de `job.plan.acceptance_tests` figuren y estén en `True` en `runner_result.test_results` (F2A-05, INV-13).
      - Registra eventos append-only `TESTS_UPDATED` (INV-11) para proyectar avance proporcional (35..90 % en `BUILDING`, INV-12).
      - Empuje mínimo: solo envía a VCS cuando las pruebas locales pasan (INV-15, A2a-3).
      - Realiza verificación remota en CI respetando el tope de 3 rondas (INV-15); al aprobar CI, hace merge/deploy, emite `PROJECT_DELIVERED` con `repo_url` y `deploy_url` y escribe `PROJECT_PROMPT.md` en el workspace. Si se agotan las rondas de CI, el presupuesto o el VCS falla catastróficamente, transiciona a `FAILED` con `BuildReport` (F2A-02, INV-15).
      - Eliminación de código inalcanzable redundante en la comprobación de rondas de CI (F2A-10).
- **Dominio y Proyección:**
  - `src/agent_factory/domain/progress.py`: Al procesar el evento `EventType.PROJECT_DELIVERED`, extrae `repo_url` y `deploy_url` del payload del evento y proyecta los enlaces en `status.links.repo` y `status.links.deploy`, manteniéndolos accesibles tanto en memoria como tras reconstruir el estado desde el `EventStore` (F2A-07, INV-11, INV-12).
- **Decisiones y Trazabilidad:**
  - `docs/decisions/ADR-002-code-agent-port.md`: Justificación arquitectónica del puerto `CodeAgent` y separación de `generate_tests` vs `implement_or_fix` (INV-08, INV-13).
  - `docs/PHASE2A_TEST_MAP.md`: Matriz de trazabilidad de tests de la Fase 2a y casos de endurecimiento F2A-01..F2A-07.

## 2. Resultado de la suite y cobertura verificada
- **Comando de verificación:** `pytest --cov=src/agent_factory --cov-report=term-missing`
- **Resultados de las pruebas:**
  - Total: **268** (227 de Fase 1 + 34 de Fase 2a + 7 de endurecimiento Fase 2a)
  - Pasadas: **268**
  - Falladas: **0**
  - Omitidas: **0**
  - Advertencias: 2 (bloqueo de socket por `pytest-socket`, conforme a INV-03)
  - Tiempo de ejecución: **~6.0 segundos**
- **Cobertura real por módulo:**
  - `src/agent_factory/domain/`: **98 %** (`progress.py`: 96 %, `models.py`: 100 %, `events.py`: 100 %, `states.py`: 100 %)
  - `src/agent_factory/ports/`: **85 %**
  - `src/agent_factory/adapters/`: **82 %**
  - `src/agent_factory/workspace.py`: **94 %**
  - `src/agent_factory/builder.py`: **88 %**
  - `src/agent_factory/orchestrator.py`: **90 %**
  - Cobertura global total en `src/agent_factory`: **90 %** (ampliamente superior al umbral requerido del 85 %).
- **Linter:** `ruff check .` -> Pasó sin observaciones (`All checks passed!`).
- **Inmutabilidad de pruebas (INV-13):** `git diff phase-2a.1-tests --stat -- tests/ pyproject.toml` es idénticamente vacío (cero modificaciones en tests o configuración durante el Paso B').

## 3. Cierre de hallazgos de auditoría (F2A-01 a F2A-10)
- [x] **F2A-01 (Crítico — AC-2.4 / INV-09)**: Se trasladó la validación anti-contaminación de `src/` estrictamente dentro del bloque inicial del Paso A (`if job.lock_snapshot is None:`). Las reanudaciones tras `PAUSED_QUOTA` que ya tienen código previo en `src/` ya no son interceptadas erróneamente por `SandboxViolationError`. Cubierto con test `test_quota_resume_succeeds_after_prior_attempt_wrote_production_files_in_src`.
- [x] **F2A-02 (Crítico — A2a-2 / INV-06 / INV-09)**: `runner.run` y las llamadas a `vcs` están protegidas contra `CommandTimeoutError`, `OSError`, `RuntimeError` y `ValueError`. Se registran como fallo de intento o transicionan inmediatamente a `FAILED` emitiendo `PROJECT_FAILED` con `BuildReport`, garantizando que el proyecto nunca quede atascado en `BUILDING`. Cubierto con test `test_runner_timeout_and_vcs_errors_transition_project_to_failed_with_report`.
- [x] **F2A-03 (Alto — AC-2.4 / INV-09 / INV-17 / ADR-002)**: En el Paso A (`generate_tests`), se invoca al agente de código antes de descontar presupuesto en el `CircuitBreaker`. Si se lanza `QuotaExhaustedError`, el trabajo se pausa en `PAUSED_QUOTA` sin consumir presupuesto ni registrar `AGENT_INVOKED`. Si se lanza `CodeAgentError`, se registra la llamada y transiciona a `FAILED`. Mata mutación M8 y cubierto con test `test_test_generation_quota_pause_and_code_agent_error_handling`.
- [x] **F2A-04 (Alto — INV-17)**: El tope de `BUILDING` en Paso B se sincroniza con el `CircuitBreaker` central acumulativo antes de invocar a `implement_or_fix`. Al ejecutarse la llamada (o fallar con `CodeAgentError`), se registra la llamada en el breaker y se emite `AGENT_INVOKED`. Mata mutación M13 y cubierto con test `test_building_budget_is_cumulative_with_breaker_and_records_failed_agent_calls`.
- [x] **F2A-05 (Alto — AC-2.1 / INV-13)**: `local_tests_passed` exige de forma conjunta que `runner_result.exit_code == 0` y que todos los `acceptance_tests` del `Plan` estén presentes y con valor `True` en `runner_result.test_results`. Mata mutación M14 y cubierto con test `test_local_tests_passed_requires_zero_exit_code_and_all_plan_acceptance_tests`.
- [x] **F2A-06 (Medio — AC-2.2 / INV-13)**: Se agregaron `conftest.py` y `setup.cfg` a `RUNNER_CONFIG_FILENAMES` en `src/agent_factory/workspace.py`, protegiendo la raíz del workspace contra inyección, alteración o borrado de archivos que alteren la ejecución de tests. Cubierto con test `test_test_lock_protects_root_conftest_and_setup_cfg_from_modification_deletion_or_injection`.
- [x] **F2A-07 (Medio — A2a-4 / INV-11)**: En `src/agent_factory/domain/progress.py`, `project_status_from_events` extrae `repo_url` y `deploy_url` al procesar `PROJECT_DELIVERED`, asignando `ProjectLinks(repo=..., deploy=...)` en `status.links`. Mata mutación M15 y cubierto con test `test_project_delivered_projects_repo_and_deploy_links_in_status`.
- [x] **F2A-08 (Medio — A2a-5)**: Se unificó la comprobación de contaminación previa de `src/` evaluando tanto las rutas de `template_files` como el directorio `ws_dir / "src"` directamente en disco dentro del Paso A. Cubierto con prueba de 3 casos en `test_test_generation_rejects_preexisting_production_code_or_missing_tests`.
- [x] **F2A-09 (Bajo — A2a-2 / INV-22)**: Se agregó validación estricta de `command` en `SubprocessRunner.run`, rechazando secuencias vacías o tipos directos `str`/`bytes` para evitar invocaciones erróneas de subprocess.
- [x] **F2A-10 (Bajo)**: Se eliminó el bloque redundante e inalcanzable de chequeo de rondas de CI previo a push, consolidando la gestión de rondas en el bloque de verificación remota.

## 4. Criterios de Aceptación (AC) y requerimientos cumplidos
- [x] **AC-2.1**: Bucle autónomo escribir -> probar -> corregir ejecutado localmente, converge ante escenario guionado, respeta aprobaciones previas en `BUILDING` y emite `BuildReport` exacto.
- [x] **AC-2.2**: Test-lock criptográfico por SHA-256 de `tests/` y configuración del runner (`pyproject.toml`, `conftest.py`, `setup.cfg`, etc.); detección y restauración automática ante manipulación contando el intento como fallido sin ejecutar runner ni pushear a VCS (INV-13, INV-23).
- [x] **AC-2.3**: Presupuestos duros respetados: `BUILDING=15` invocaciones del agente de código acumulativas sin reseteo (INV-17) y máximo 3 rondas de CI (INV-15), derivando a `FAILED` con reporte descriptivo si se agotan.
- [x] **AC-2.4**: Manejo resiliente de cuota: `QuotaExhaustedError` pausa el trabajo en `PAUSED_QUOTA` sin marcar `FAILED`, ejecuta backoff vía `Clock.sleep` y reanuda sin pérdida de estado ni consumo erróneo de presupuesto (INV-09). Errores fatales `CodeAgentError` derivan a `FAILED` inmediatamente. Reanudación robusta aun habiendo código en `src/`.
- [x] **A2a-1**: Aislamiento estricto de workspace (`workspaces/<project_id>/`) validando `project_id` con regex del dominio y previniendo escapes mediante traversals (`..`), rutas absolutas y enlaces simbólicos o junctions de Windows (INV-18, INV-22).
- [x] **A2a-2**: Ejecutor de comandos con timeout multiplataforma que mata tanto el proceso padre como todos sus subprocesos hijos, capturando `CommandTimeoutError` para evitar atascos en `BUILDING` (INV-02, INV-06).
- [x] **A2a-3**: Empuje mínimo a VCS: solo se hace push al pasar las pruebas locales o agotarse el presupuesto; nunca en fallos locales intermedios (INV-15).
- [x] **A2a-4**: Registro de eventos append-only (`AGENT_INVOKED`, `TESTS_UPDATED`, `CI_ROUND_COMPLETED`, etc.) permitiendo a `project_status_from_events` proyectar el progreso porcentual proporcional (35..90 % en `BUILDING`), `status.links` y generar `BuildReport` (INV-11, INV-12).
- [x] **A2a-5**: Aislamiento estricto en generación de tests: `generate_tests` solo recibe `acceptance_tests`, no tiene acceso al código de producción ni al Plan, y rechaza contaminación previa de `src/` (INV-13).

## 5. Decisiones tomadas y supuestos
- **ADR-002**: Diseño del puerto `CodeAgent` separando formalmente la generación aislada de pruebas de aceptación (`generate_tests`) de la implementación y corrección del código (`implement_or_fix`).
- **Limpieza de procesos en Windows vs POSIX**: Se implementó una terminación limpia garantizada del árbol de procesos mediante `taskkill /F /T /PID` en Windows y grupos de procesos con `os.killpg` en POSIX para evitar procesos huérfanos o bloqueos tras timeout.
- **Formato determinista del Test-Lock**: Las entradas de archivos se ordenan alfabéticamente por ruta POSIX relativa y se delimitan con bytes nulos (`\0`), garantizando hashes independientes de la plataforma y del orden de recorrido en disco.

## 6. Riesgos o deudas detectadas
- Ninguna deuda técnica bloqueante para el alcance de la Fase 2a.
- En la Fase 2b se implementarán los adaptadores reales con red y CLI (`AgyCliAdapter`), las plantillas en `templates/` y las pruebas e2e en navegador headless con Playwright.
