# PHASE_REPORT_1 — Dominio, pipeline y registro de eventos (Fase 1 Endurecida)

## 1. Archivos creados / modificados
- **Dominio y modelos:**
  - `src/agent_factory/domain/states.py`: Implementación de `ProjectState`, matriz completa de transiciones `ALLOWED_TRANSITIONS`, `can_transition` y `validate_transition` lanzando `IllegalTransition` (INV-04).
  - `src/agent_factory/domain/models.py`: Modelos Pydantic v2 (`Spec`, `AcceptanceTest`, `FileSpec`, `Plan`, `ApprovalEvent`, `ProjectStatus`, `TestCounts`, etc.), checklist determinista de completitud, validación estricta de `project_id` con regex `^[a-z0-9][a-z0-9-]{0,62}$` (INV-22), llenado explícito de supuestos y cálculo determinista de SHA-256 de planes (`Plan.compute_hash`, INV-05).
  - `src/agent_factory/domain/events.py`: Modelos de eventos inmutables `Event` con `model_config = ConfigDict(frozen=True)`, enumeración de tipos canónicos `EventType` y fábrica determinista `create_event` (INV-11).
  - `src/agent_factory/domain/progress.py`: Función determinista `calculate_progress` según la tabla de la sección 5 del MASTER_PROMPT (conservando avance en estado `FAILED`) y proyección determinista `project_status_from_events` con validación estricta de cada transición vía `validate_transition` y rechazo de `BUILDING` sin aprobación válida coincidente con `plan_hash` (INV-04, INV-05, INV-11, INV-12).
- **Puertos:**
  - `src/agent_factory/ports/llm.py`: Protocolo `LLM` y excepción canónica `InvalidLLMResponseError`.
  - `src/agent_factory/ports/store.py`: Protocolo `EventStore` y excepción `DuplicateEventError`.
  - `src/agent_factory/ports/publisher.py`: Protocolo `StatusPublisher`.
  - `src/agent_factory/ports/clock.py`: Protocolo `Clock` con métodos `now()` y `sleep()` para backoff determinista (INV-02, INV-09).
- **Adaptadores:**
  - `src/agent_factory/adapters/fake_llm.py`: Adaptador offline FIFO para pruebas deterministas sin red (INV-02, INV-03).
  - `src/agent_factory/adapters/memory_store.py`: Adaptador append-only en memoria con verificación de IDs duplicados y aislamiento de mutaciones en `payload` mediante copias profundas defensivas (INV-11, F-16).
  - `src/agent_factory/adapters/sqlite_store.py`: Adaptador append-only basado en SQLite local para persistencia determinista de eventos.
  - `src/agent_factory/adapters/file_publisher.py`: Publicador local de proyecciones `ProjectStatus` en formato JSON en disco con prevención estricta de path traversal y validación de `project_id` (INV-22, F-05).
- **Agentes:**
  - `src/agent_factory/agents/interviewer.py`: Agente entrevistador que procesa especificaciones, respeta el tope de 3 preguntas por ronda y llena supuestos explícitos al alcanzar el tope (AC-1.2).
  - `src/agent_factory/agents/pragmatist.py`: Primer paso del debate con reintentos acotados y backoff vía puerto `Clock` (AC-1.3, INV-09).
  - `src/agent_factory/agents/structuralist.py`: Segundo paso de crítica y salvaguardas con reintentos acotados y backoff vía puerto `Clock` (AC-1.3, INV-09).
  - `src/agent_factory/agents/mediator.py`: Tercer paso de síntesis, validación estricta de contratos de archivo, pruebas de aceptación no vacías y plantilla válida, junto con `run_debate_pipeline` con conteo interno de llamadas LLM y soporte de hooks para el circuit breaker (AC-1.3, AC-1.4, F-06, F-12).
- **Idempotencia, Circuit Breaker y Orquestador:**
  - `src/agent_factory/inbox.py`: Registro de eventos entrantes procesados antes de cualquier invocación LLM (INV-16).
  - `src/agent_factory/orchestrator.py`: Orquestador de estados con validación de origen en cada método, compuerta estricta de aprobación humana contra el `plan_hash` y decisión `approved` (INV-05), circuit breaker central que envuelve toda llamada LLM (INV-17), manejo resiliente de timeouts/excepciones hacia `FAILED` (INV-09), reconstrucción de `Spec` tras reinicios y verificación de existencia del proyecto.
- **Documentación de decisiones y trazabilidad:**
  - `docs/decisions/ADR-000-pytest-timeout.md`: Justificación de `pytest-timeout==2.4.0` (INV-08).
  - `docs/decisions/ADR-003-pydantic.md`: Justificación de `pydantic==2.14.3` (INV-08).
  - `docs/HARDENING_TEST_MAP.md`: Matriz de trazabilidad de hallazgos F-01..F-19, criterios AC-1.1..AC-1.13 y mutaciones M1..M11.
  - `docs/DEBT.md`: Registro formal de deudas técnicas no bloqueantes.

## 2. Resultado de la suite y cobertura
- **Comando de verificación:** `pytest --cov=src/agent_factory --cov-report=term-missing`
- **Resultados de las pruebas:**
  - Pasadas: **201** (152 preexistentes + 49 de endurecimiento)
  - Falladas: **0**
  - Omitidas: **0**
  - Tiempo de ejecución: **~1.0 segundos**
- **Cobertura de código:**
  - `src/agent_factory/domain/`: **> 98 %** (models: 100%, states: 100%, events: 100%, progress: 93%)
  - `src/agent_factory/agents/`: **93 %** (pragmatist: 94%, interviewer: 98%, mediator: 95%, structuralist: 74%)
  - Cobertura global total: **91 %** (muy superior al umbral requerido del 85 %).
- **Linter:** `ruff check .` -> Pasó sin observaciones (`All checks passed!`).
- **Inmutabilidad de pruebas (INV-13):** `git diff phase-1.1-tests -- tests/ pyproject.toml` es idénticamente vacío.

## 3. Criterios de Aceptación (AC) cumplidos
- [x] **AC-1.1**: Matriz de transiciones con oráculo escrito a mano (19 válidas, 81 ilegales lanzando `IllegalTransition`). Métodos del orquestador validan estado de origen (INV-04, INV-23).
- [x] **AC-1.2**: `Interviewer` respeta tope de 3 preguntas por ronda y termina por checklist de `Spec` completo o por tope marcando supuestos explícitos.
- [x] **AC-1.3**: Debate ejecuta pipeline de 3 pasos con conteo de llamadas sin depender de atributos del fake; reintentos acotados y transición a `FAILED` ante fallo (INV-09, F-12).
- [x] **AC-1.4**: `Mediator` rechaza planes sin `acceptance_tests`, con archivos sin `contract` o con `template_id` inexistente.
- [x] **AC-1.5**: Entrar a `BUILDING` sin `ApprovalEvent`, con decisión distinta a `approved`, con `plan_hash` diferente o fuera de `WAITING_APPROVAL` falla estrictamente con `ApprovalError` (INV-05, F-01, F-11).
- [x] **AC-1.6**: `status` se proyecta deterministamente desde el log de eventos; transiciones ilegales o entrada a `BUILDING` sin aprobación válida son rechazadas durante la proyección (INV-04, INV-05, INV-11, F-04).
- [x] **AC-1.7**: El avance sigue la tabla de la sección 5 para cada estado y `FAILED` conserva el último valor (INV-12).
- [x] **AC-1.8**: Superar el presupuesto de llamadas de un estado (`config/limits.toml`) lleva inmediatamente a `FAILED`, incluyendo reintentos de debate y mediación bajo el breaker central (INV-17, F-06).
- [x] **AC-1.9**: Idempotencia de entrada persistente que sobrevive a cierre y reapertura de almacenes SQLite (INV-16, F-08).
- [x] **AC-1.10**: Ambos stores (`MemoryEventStore` y `SqliteEventStore`) pasan la misma suite unificada de contrato, defendiendo inmutabilidad de eventos y aislamiento de payloads (INV-11, F-16).
- [x] **AC-1.11**: Suite corre 100% sin red (`pytest-socket`), cobertura >= 85 % en `domain/` y `agents/`, y linter `ruff` limpio.
- [x] **AC-1.12**: Validación estricta de `project_id` (`^[a-z0-9][a-z0-9-]{0,62}$`) y prevención de path traversal en publicador y APIs públicas del orquestador (INV-22, F-05).
- [x] **AC-1.13**: Excepciones y timeouts del LLM dejan evento `PROJECT_FAILED` y estado `FAILED`, nunca un proyecto colgado o atascado (INV-09, F-07).

## 4. Decisiones tomadas y supuestos
- **Unificación de compuertas**: Toda transición de estado pasa obligatoriamente por `validate_transition`, tanto en las operaciones activas del orquestador como durante la proyección de eventos en `project_status_from_events`.
- **Aprobación de doble llave (INV-05)**: Requiere explícitamente `decision == "approved"` Y `plan_hash` coincidente con el plan vigente generado en `WAITING_APPROVAL`.
- **Circuit breaker centralizado (INV-17)**: Envuelve todas las llamadas a los agentes del pipeline (Pragmatist, Structuralist, Mediator) impidiendo llamadas no autorizadas al LLM una vez alcanzado el límite por estado.
- **Backoff desacoplado mediante Clock (INV-09)**: Las esperas entre reintentos operacionales pasan por el método `sleep` del puerto `Clock`, asegurando que las pruebas corran en milisegundos sin ralentizar la ejecución.

## 5. Riesgos o deudas detectadas
- **Corrección de afirmación previa**: La afirmación preliminar de "sin riesgos bloqueantes" emitida tras la corrida inicial de 150 pruebas era **falsa e inexacta**. La auditoría independiente documentada en `docs/AUDIT_REPORT_1.md` identificó 11 hallazgos críticos y altos (F-01 a F-11), así como 11 mutaciones no detectadas.
- **Resolución en endurecimiento**: Los hallazgos testeables F-01 a F-12 y F-15 a F-19 han sido corregidos y anclados mediante 49 pruebas de regresión con oráculos independientes.
- **Deuda técnica no bloqueante**: Los ítems no testeables o de fases futuras (F-13 tipado de excepciones, F-14 contratos de spec, F-20 ADR de dependencias transitivas, F-21 umbral de CI en workflow, F-22 a F-27) quedan debidamente documentados y clasificados en `docs/DEBT.md`.
- **Fase 2 (Constructor local)**: Se requerirá la implementación de la plantilla base en `templates/static-app/` y el adaptador para invocar al agente de código en un workspace aislado (INV-18, INV-20).
