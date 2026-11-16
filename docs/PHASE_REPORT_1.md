# PHASE_REPORT_1 — Dominio, pipeline y registro de eventos (Fase 1 Endurecida - Cierre Final)

## 1. Archivos creados / modificados
- **Dominio y modelos:**
  - `src/agent_factory/domain/states.py`: Implementación de `ProjectState`, matriz completa de transiciones `ALLOWED_TRANSITIONS`, `can_transition` y `validate_transition` lanzando `IllegalTransition` (INV-04).
  - `src/agent_factory/domain/models.py`: Modelos Pydantic v2 (`Spec`, `AcceptanceTest`, `FileSpec`, `Plan`, `ApprovalEvent`, `ProjectStatus`, `TestCounts`, etc.), checklist determinista de completitud, validación estricta de `project_id` con regex `^[a-z0-9][a-z0-9-]{0,62}\Z` vía `fullmatch` (INV-22, N-06), llenado explícito de supuestos y cálculo determinista de SHA-256 de planes (`Plan.compute_hash`, INV-05, N-10).
  - `src/agent_factory/domain/events.py`: Modelos de eventos inmutables `Event` con `ConfigDict(frozen=True)`, enumeración de tipos canónicos `EventType` y fábrica determinista `create_event` (INV-11).
  - `src/agent_factory/domain/progress.py`: Función determinista `calculate_progress` según la tabla de la sección 5 del MASTER_PROMPT (conservando avance en estado `FAILED`) y proyección determinista `project_status_from_events` con validación estricta de cada transición vía `validate_transition`, rechazo de eventos previos a `PROJECT_CREATED`, rechazo de `PLAN_CREATED` fuera de `DEBATE`/`MEDIATION`, rechazo de `APPROVAL_RECORDED` con destino distinto a `BUILDING` o decisión distinta a `approved`, y aceptación de la transición legítima de autocorrección `VERIFYING -> BUILDING` (INV-04, INV-05, INV-11, INV-12, N-07).
- **Puertos:**
  - `src/agent_factory/ports/llm.py`: Protocolo `LLM`, excepción base `LLMError` y excepción canónica `InvalidLLMResponseError` (INV-02, N-01).
  - `src/agent_factory/ports/store.py`: Protocolo `EventStore` y excepción `DuplicateEventError`.
  - `src/agent_factory/ports/publisher.py`: Protocolo `StatusPublisher`.
  - `src/agent_factory/ports/clock.py`: Protocolo `Clock` con métodos `now()` y `sleep(seconds)` para backoff determinista (INV-02, INV-09).
- **Adaptadores:**
  - `src/agent_factory/adapters/system_clock.py`: Implementación real de `Clock` con `now()` en UTC y `sleep()` con `time.sleep` (INV-02, INV-09, N-05).
  - `src/agent_factory/adapters/fake_llm.py`: Adaptador offline FIFO para pruebas deterministas sin red (INV-02, INV-03).
  - `src/agent_factory/adapters/memory_store.py`: Adaptador append-only en memoria con verificación de IDs duplicados y aislamiento de mutaciones en `payload` mediante copias profundas defensivas (INV-11, F-16, X6a).
  - `src/agent_factory/adapters/sqlite_store.py`: Adaptador append-only basado en SQLite local para persistencia determinista de eventos.
  - `src/agent_factory/adapters/file_publisher.py`: Publicador local de proyecciones `ProjectStatus` en formato JSON en disco con prevención estricta de path traversal y validación de `project_id` (INV-22, F-05, N-06).
- **Agentes:**
  - `src/agent_factory/agents/interviewer.py`: Agente entrevistador que procesa especificaciones, respeta el tope de 3 preguntas por ronda y llena supuestos explícitos al alcanzar el tope (AC-1.2).
  - `src/agent_factory/agents/pragmatist.py`: Primer paso del debate con reintentos acotados y backoff vía puerto `Clock`, capturando `(LLMError, ValueError, TimeoutError, OSError)` (AC-1.3, INV-09, N-01, N-05).
  - `src/agent_factory/agents/structuralist.py`: Segundo paso de crítica y salvaguardas con reintentos acotados y backoff vía puerto `Clock`, capturando `(LLMError, ValueError, TimeoutError, OSError)` (AC-1.3, INV-09, N-01, N-05).
  - `src/agent_factory/agents/mediator.py`: Tercer paso de síntesis, validación estricta de contratos de archivo, pruebas de aceptación no vacías y plantilla válida, junto con `run_debate_pipeline` con conteo interno de llamadas LLM, soporte de hooks para el circuit breaker y manejo de excepciones de transporte (AC-1.3, AC-1.4, F-06, F-12, N-01, N-05).
- **Idempotencia, Circuit Breaker y Orquestador:**
  - `src/agent_factory/inbox.py`: Registro de eventos entrantes procesados antes de cualquier invocación LLM (INV-16).
  - `src/agent_factory/orchestrator.py`: Orquestador de estados con validación de origen en cada método, compuerta estricta de aprobación humana contra el `plan_hash` y decisión `approved` (INV-05), circuit breaker central acumulativo por proyecto sin reset en re-debates (INV-17, N-02), manejo resiliente de timeouts/excepciones hacia `FAILED` (INV-09, N-01), reconstrucción desacoplada de `Spec` desde el log conservando `objective` (INV-11, N-08), idempotencia previa a la validación de estado en entrevista (INV-16, N-09), rechazo de `create_project` duplicado sin corromper el log (N-03) y rechazo de llamadas de estado cuando ya se está en `FAILED` (N-04).
- **Documentación de decisiones y trazabilidad:**
  - `docs/decisions/ADR-000-pytest-timeout.md`: Justificación de `pytest-timeout==2.4.0` (INV-08).
  - `docs/decisions/ADR-003-pydantic.md`: Justificación de `pydantic==2.14.3` (INV-08).
  - `docs/HARDENING_TEST_MAP.md`: Matriz de trazabilidad de hallazgos F-01..F-19, N-01..N-10, criterios AC-1.1..AC-1.13 y mutaciones M1..M11 y X1..X17.
  - `docs/DEBT.md`: Registro formal honesto de deudas técnicas y estado de hallazgos.

## 2. Resultado de la suite y cobertura verificada
- **Comando de verificación:** `pytest -q --cov=src/agent_factory --cov-report=term-missing`
- **Resultados de las pruebas:**
  - Total: **227** (152 originales + 49 de Paso A' + 26 de Paso A'')
  - Pasadas: **227**
  - Falladas: **0**
  - Omitidas: **0**
  - Advertencias: 2 (pytest-socket bloqueando socket.socket, esperado en INV-03)
  - Tiempo de ejecución: **~5.3 segundos**
- **Cobertura real por paquete:**
  - `src/agent_factory/domain/`: **97 %**
    - `events.py`: 100 %
    - `models.py`: 100 %
    - `progress.py`: 93 %
    - `states.py`: 100 %
  - `src/agent_factory/agents/`: **98 %**
    - `interviewer.py`: 98 %
    - `mediator.py`: 96 %
    - `pragmatist.py`: 100 %
    - `structuralist.py`: 100 %
  - `src/agent_factory/adapters/`: **93 %**
    - `system_clock.py`: 100 %
    - `memory_store.py`: 100 %
    - `sqlite_store.py`: 96 %
    - `file_publisher.py`: 92 %
    - `fake_llm.py`: 77 %
  - `src/agent_factory/orchestrator.py`: **87 %**
  - `src/agent_factory/inbox.py`: **100 %**
  - Cobertura global total en `src/agent_factory`: **93 %** (muy superior al umbral requerido del 85 %).
- **Linter:** `ruff check .` -> Pasó sin observaciones (`All checks passed!`).
- **Inmutabilidad de pruebas (INV-13):** `git diff phase-1.2-tests -- tests/ pyproject.toml` es idénticamente vacío.

## 3. Criterios de Aceptación (AC) cumplidos
- [x] **AC-1.1**: Matriz de transiciones completa (10×10, 19 válidas y 81 ilegales) con oráculo escrito a mano en el test. Métodos del orquestador validan estado de origen y rechazan estados terminales o no permitidos (INV-04, INV-23).
- [x] **AC-1.2**: `Interviewer` respeta tope de 3 preguntas por ronda y termina por checklist de `Spec` completo o por tope marcando supuestos explícitos.
- [x] **AC-1.3**: Debate ejecuta pipeline de 3 pasos con conteo de llamadas sin depender de atributos del fake; reintentos acotados con backoff vía `Clock` y transición a `FAILED` ante fallo (INV-09, F-12, N-05).
- [x] **AC-1.4**: `Mediator` rechaza planes sin `acceptance_tests`, con archivos sin `contract` o con `template_id` inexistente.
- [x] **AC-1.5**: Entrar a `BUILDING` sin `ApprovalEvent`, con decisión distinta a `approved`, con `plan_hash` diferente o fuera de `WAITING_APPROVAL` (incluyendo llamadas en `BUILDING` y `FAILED`) falla con `ApprovalError` (INV-05, F-01, F-11, M7).
- [x] **AC-1.6**: `status` se proyecta deterministamente desde el log de eventos; transiciones ilegales, planes fuera de debate/mediación o aprobación hacia destinos ilegales son rechazadas durante la proyección (INV-04, INV-05, INV-11, F-04, N-07).
- [x] **AC-1.7**: El avance sigue la tabla de la sección 5 para cada estado y `FAILED` conserva el último valor (INV-12).
- [x] **AC-1.8**: Superar el presupuesto de llamadas de un estado (`config/limits.toml`) lleva inmediatamente a `FAILED`, con contadores estrictamente acumulativos por proyecto que nunca se reinician en re-debates (INV-17, F-06, N-02).
- [x] **AC-1.9**: Idempotencia de entrada persistente que sobrevive a cierre y reapertura de almacenes SQLite, ignorando eventos duplicados antes de validar estado (INV-16, F-08, N-09).
- [x] **AC-1.10**: Ambos stores (`MemoryEventStore` y `SqliteEventStore`) pasan la misma suite unificada de contrato, defendiendo inmutabilidad de eventos y aislamiento de payloads (INV-11, F-16, X6a).
- [x] **AC-1.11**: Suite corre 100% sin red (`pytest-socket`), cobertura >= 85 % en `domain/` y `agents/`, y linter `ruff` limpio.
- [x] **AC-1.12**: Validación estricta de `project_id` con regex estricto `\Z` (`^[a-z0-9][a-z0-9-]{0,62}\Z`) rechazando `"abc\n"` y prevención de path traversal en publicador y APIs públicas del orquestador (INV-22, F-05, N-06).
- [x] **AC-1.13**: Excepciones de red (`ConnectionError`, `OSError`), excepciones de proveedor (`LLMError`) y timeouts del LLM dejan evento `PROJECT_FAILED` y estado `FAILED`, nunca un proyecto colgado o atascado (INV-09, F-07, N-01).

## 4. Decisiones tomadas y supuestos
- **Presupuestos acumulativos por proyecto (INV-17)**: Conforme a la clarificación de Gabriel en `MASTER_PROMPT.md` v1.2.1, los presupuestos del circuit breaker son acumulativos por proyecto y no se reinician en re-debates tras ajustes. Para escenarios que requieren múltiples rondas de debate se deben configurar límites explícitos más amplios.
- **Backoff con `SystemClock` (INV-09, INV-02)**: Se implementó el adaptador `SystemClock` como implementación por defecto de `Clock`, permitiendo que en producción se duerma con `time.sleep` y en tests se inyecte un `_FakeClock` para ejecución determinista sin pausas reales.
- **Idempotencia de entrada anterior a validación de estado (INV-16)**: Un mensaje con un `event_id` ya procesado se reconoce y descarta antes de verificar el estado actual del proyecto, evitando que reintentos de red o de long polling en estados posteriores lancen `IllegalTransition`.
- **Integridad del log append-only (INV-11)**: `create_project` comprueba eventos existentes antes de persistir `PROJECT_CREATED`, impidiendo la corrupción de la proyección por inicializaciones duplicadas.
- **Transición legítima de autocorrección (Sección 4)**: La proyección en `progress.py` permite la transición `VERIFYING -> BUILDING` de la autocorrección con tope de la Fase 2, bloqueando únicamente transiciones no autorizadas hacia `BUILDING` desde otros estados.

## 5. Estado de deudas y riesgos
- **Hallazgos N-01 a N-10 resueltos**: Todos los hallazgos de severidad crítica, alta y media derivados de `AUDIT_REPORT_1B.md` cuentan con pruebas específicas con oráculos independientes y han sido completamente resueltos en el código de producción.
- **Deuda técnica registrada en `docs/DEBT.md`**:
  - `F-20`: Dependencias transitivas de `pydantic` sin lockfile (se resolverá con herramienta de gestión de dependencias en Fase 2).
  - `F-21`: En `.github/workflows/ci.yml`, agregar `--cov-fail-under=85` al iniciar Fase 2 (preservando INV-13 en Fase 1).
  - `N-11`: Ausencia de triggers de base de datos en SQLite para forzar append-only a nivel de esquema (se confía en la API pública de `SqliteEventStore`).
