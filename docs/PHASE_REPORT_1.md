# PHASE_REPORT_1 — Dominio, pipeline y registro de eventos (Fase 1)

## 1. Archivos creados / modificados
- **Dominio y modelos:**
  - `src/agent_factory/domain/states.py`: Implementación de `ProjectState`, matriz completa de transiciones `ALLOWED_TRANSITIONS`, `can_transition` y `validate_transition` lanzando `IllegalTransition` (INV-04).
  - `src/agent_factory/domain/models.py`: Modelos Pydantic v2 (`Spec`, `AcceptanceTest`, `FileSpec`, `Plan`, `ApprovalEvent`, `ProjectStatus`, `TestCounts`, etc.), checklist determinista de completitud, llenado explícito de supuestos y cálculo determinista de SHA-256 de planes (`Plan.compute_hash`).
  - `src/agent_factory/domain/events.py`: Modelos de eventos inmutables `Event`, enumeración de tipos canónicos `EventType` y fábrica determinista `create_event` (INV-11).
  - `src/agent_factory/domain/progress.py`: Función determinista `calculate_progress` según la tabla de la sección 5 del MASTER_PROMPT (conservando avance en estado `FAILED`) y proyección determinista `project_status_from_events` a partir del registro de eventos (INV-11, INV-12).
- **Puertos:**
  - `src/agent_factory/ports/llm.py`: Protocolo `LLM` y excepción canónica `InvalidLLMResponseError`.
  - `src/agent_factory/ports/store.py`: Protocolo `EventStore` y excepción `DuplicateEventError`.
  - `src/agent_factory/ports/publisher.py`: Protocolo `StatusPublisher`.
  - `src/agent_factory/ports/clock.py`: Protocolo `Clock`.
- **Adaptadores:**
  - `src/agent_factory/adapters/fake_llm.py`: Adaptador offline FIFO para pruebas deterministas sin red (INV-02, INV-03).
  - `src/agent_factory/adapters/memory_store.py`: Adaptador append-only en memoria con verificación de IDs duplicados.
  - `src/agent_factory/adapters/sqlite_store.py`: Adaptador append-only basado en SQLite local para persistencia determinista de eventos.
  - `src/agent_factory/adapters/file_publisher.py`: Publicador local de proyecciones `ProjectStatus` en formato JSON en disco con soporte para reconstrucción idempotente.
- **Agentes:**
  - `src/agent_factory/agents/interviewer.py`: Agente entrevistador que procesa especificaciones, respeta el tope de 3 preguntas por ronda y llena supuestos explícitos al alcanzar el tope (AC-1.2).
  - `src/agent_factory/agents/pragmatist.py`: Primer paso del debate con reintentos acotados (AC-1.3).
  - `src/agent_factory/agents/structuralist.py`: Segundo paso de crítica y salvaguardas con reintentos acotados (AC-1.3).
  - `src/agent_factory/agents/mediator.py`: Tercer paso de síntesis, validación estricta de contratos de archivo, pruebas de aceptación no vacías y plantilla válida, junto con el pipeline completo de debate `run_debate_pipeline` (AC-1.3, AC-1.4).
- **Idempotencia, Circuit Breaker y Orquestador:**
  - `src/agent_factory/inbox.py`: Registro de eventos entrantes procesados antes de cualquier invocación LLM (INV-16).
  - `src/agent_factory/orchestrator.py`: Orquestador de estados, compuerta estricta de aprobación humana contra el `plan_hash` (INV-05), lectura y ejecución del circuit breaker de llamadas por estado según `config/limits.toml` (INV-17).
- **Documentación de trazabilidad:**
  - `docs/PHASE1_TEST_MAP.md`: Mapeo explícito de criterios AC a suites y funciones de prueba.

## 2. Resultado de la suite y cobertura
- **Comando de verificación:** `pytest --cov=src/agent_factory --cov-report=term-missing`
- **Resultados de las pruebas:**
  - Pasadas: 150
  - Falladas: 0
  - Omitidas: 0
  - Tiempo de ejecución: 0.84 segundos
- **Cobertura de código:**
  - `src/agent_factory/domain/`: **98 %** (models: 100%, states: 100%, events: 100%, progress: 92%)
  - `src/agent_factory/agents/`: **94.4 %** (pragmatist: 100%, interviewer: 98%, mediator: 93%, structuralist: 80%)
  - Cobertura global total: **89 %** (superior al umbral requerido del 85 %).
- **Linter:** `ruff check .` -> Pasó sin observaciones (`All checks passed!`).
- **Inmutabilidad de pruebas (INV-13):** `git diff phase-1-tests -- tests/ pyproject.toml` es idénticamente vacío.

## 3. Criterios de Aceptación (AC) cumplidos
- [x] **AC-1.1**: Toda transición válida pasa; toda ilegal lanza `IllegalTransition` (100 casos parametrizados sobre la matriz 10×10 en `test_states.py`).
- [x] **AC-1.2**: `Interviewer` respeta el tope de 3 preguntas por ronda y termina por checklist de `Spec` completo o por tope marcando supuestos explícitos.
- [x] **AC-1.3**: El debate hace exactamente 3 llamadas al LLM en el camino feliz; JSON inválido ejercita hasta 2 reintentos y termina en `FAILED` con causa.
- [x] **AC-1.4**: El `Mediator` rechaza planes sin `acceptance_tests`, con archivos sin `contract` o con `template_id` inexistente.
- [x] **AC-1.5**: Entrar a `BUILDING` sin `ApprovalEvent`, o con `plan_hash` distinto, falla (INV-05).
- [x] **AC-1.6**: `status` (`ProjectStatus`) se reconstruye idéntico a partir del registro de eventos (borrar la proyección y regenerarla).
- [x] **AC-1.7**: El avance sigue la tabla de la sección 5 para cada estado y `FAILED` conserva el último valor.
- [x] **AC-1.8**: Superar el presupuesto de llamadas de un estado (`config/limits.toml`) lleva inmediatamente a `FAILED` (INV-17).
- [x] **AC-1.9**: El mismo ID de evento procesado dos veces genera cero llamadas LLM la segunda vez y se registra antes de invocar al LLM (INV-16).
- [x] **AC-1.10**: Ambos stores (`MemoryEventStore` y `SqliteEventStore`) pasan la misma suite unificada de contrato.
- [x] **AC-1.11**: La suite corre completamente sin red (`pytest-socket`), cobertura >= 85 % en `domain/` y `agents/`, y linter `ruff` limpio.

## 4. Decisiones tomadas y supuestos
- **Trazabilidad estricta**: Los eventos se registran como append-only y el estado `ProjectStatus` nunca se guarda como fuente primaria, sino como proyección recalculable de la secuencia de eventos.
- **Idempotencia (INV-16)**: El `Inbox` registra el ID entrante antes de invocar al manejador; si el LLM falla, el ID sigue registrado como visto, evitando reintentos infinitos que gasten cuota.
- **Circuit Breaker (INV-17)**: Configurado dinámicamente desde `config/limits.toml`. Cuando se alcanza el tope de llamadas para el estado actual, el proyecto transiciona inmediatamente a `FAILED` con la causa detallada.
- **Inmutabilidad de pruebas**: Ninguna prueba ni su configuración en `pyproject.toml` fue alterada durante la fase de implementación.

## 5. Riesgos o deudas detectadas
- Ningún riesgo o deuda técnica bloqueante para la suite de pruebas unitarias y de dominio.
- Para la Fase 2 (Constructor local), se requerirá la implementación de la plantilla base en `templates/static-app/` y el adaptador para invocar al agente de código en un workspace aislado.
