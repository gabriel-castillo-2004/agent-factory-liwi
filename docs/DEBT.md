# Registro de Deuda Técnica y Estado de Hallazgos tras Paso B'' (`DEBT.md`)

Este documento registra los hallazgos de `docs/AUDIT_REPORT_1.md` y `docs/AUDIT_REPORT_1B.md`, su estado tras la finalización del endurecimiento en el Paso B'', y las deudas técnicas remanentes.

## 1. Estado de Hallazgos de Auditoría (F-01..F-27 y N-01..N-12)

| ID | Severidad | Estado tras Paso B'' | Detalle y Resolución |
|---|---|---|---|
| **F-01** | Crítico | **CERRADO** | `submit_approval` y `progress.py` exigen `decision == "approved"`. Probado con 5 casos negativos y mutaciones X14/X3. |
| **F-02** | Crítico | **CERRADO** | `validate_transition` es el único punto de paso para cambios de estado en orquestador y proyección. `record_state_call` y `create_project` validan estado de origen. |
| **F-03** | Crítico | **CERRADO** | `run_debate` valida existencia, origen (`INTERVIEW` o `DEBATE`) y completitud de `Spec` antes de ejecutar. |
| **F-04** | Crítico | **CERRADO** | `project_status_from_events` no confía en `payload["to"]`; valida cada transición, rechaza planes creados fuera de debate/mediación y rechaza approvals con targets ilegales. |
| **F-05** | Alto | **CERRADO** | Validación estricta con regex y `is_relative_to` en publicador y orquestador. |
| **F-06** | Alto | **CERRADO** | Circuit breaker centralizado envuelve toda llamada LLM en entrevista y debate. Contadores estrictamente acumulativos por proyecto. |
| **F-07** | Alto | **CERRADO** | Timeouts y fallos de LLM registran `PROJECT_FAILED` y llevan a `FAILED`, nunca dejan el proyecto atascado. |
| **F-08** | Alto | **CERRADO** | Deduplicación de `incoming_event_id` en el log de eventos SQLite sobrevive a reinicios y cierres de base de datos. |
| **F-09** | Alto | **CERRADO** | Matriz 10×10 completa escrita a mano en tests (19 válidas, 81 ilegales) con oráculo independiente (INV-23). |
| **F-10** | Alto | **CERRADO** | Reconstrucción de `ProjectStatus` verificada contra valores literales escritos a mano (INV-23). |
| **F-11** | Alto | **CERRADO** | `compute_hash` verificado contra digest SHA-256 escrito a mano y sensible a todos los campos. |
| **F-12** | Medio | **CERRADO** | Conteo de llamadas LLM en `_PipelineLLMWrapper` sin depender de atributos del fake. |
| **F-13** | Medio | **CERRADO** | Excepciones restringidas a la familia `LLMError`, `TimeoutError`, `OSError`, `ValueError`, `RuntimeError`. |
| **F-14** | Medio | **Aceptado como diseño** | `Spec.fill_gaps_with_assumptions` rellena campos faltantes con supuestos explícitos en `Spec.assumptions` al alcanzar el límite de rondas. |
| **F-15** | Medio | **CERRADO** | `load_limits` lanza `FileNotFoundError` si falta `config/limits.toml`. |
| **F-16** | Medio | **CERRADO** | `Event` es `frozen=True` y los almacenes (`MemoryEventStore` y `SqliteEventStore`) aplican copias profundas defensivas. |
| **F-17** | Medio | **CERRADO** | `_get_spec` reconstruye `Spec` desde el registro de eventos (`PROJECT_CREATED` + `SPEC_UPDATED`) sin depender de memoria. |
| **F-18** | Medio | **CERRADO** | `handle_interview_message` rechaza proyectos inexistentes antes de invocar al LLM. |
| **F-19** | Medio | **CERRADO** | Transición `DEBATE -> MEDIATION` explícitamente registrada en el log de eventos. |
| **F-20** | Medio | **Abierto (Fase 2)** | Dependencias directas fijadas con versión exacta (`==`) en `pyproject.toml` (ADR-003). Dependencias transitivas (`pydantic_core`, etc.) sin lockfile en Fase 1; se adoptará lockfile formal (ej. `pip-compile` / `uv`) en Fase 2. |
| **F-21** | Medio | **Abierto (Fase 2)** | `.github/workflows/ci.yml` ejecuta `pytest --cov=src/agent_factory` sin `--cov-fail-under=85`. Para cumplir INV-13 en Fase 1 no se modifican workflows; se incluirá al inicio de Fase 2. |
| **F-22** | Bajo | **Aceptado como diseño** | `STATE_BASE_PROGRESS` incluye `CHANGE_REQUEST: 10`, valor base provisional hasta su desarrollo en Fase 5. |
| **F-23** | Bajo | **Aceptado como diseño** | Regla de redondeo proporcional en `BUILDING` (`round(35 + 55 * ratio)`). El valor `62` para `2/4` pruebas pasadas está anclado en la suite de pruebas. |
| **F-24** | Bajo | **CERRADO** | Inclusión de `timestamp` ISO en payload de `APPROVAL_RECORDED` y validación estricta de límites de `progress_pct` (`0 <= progress_pct <= 100`) en `ProjectStatus`. |
| **F-25** | Bajo | **CERRADO** | Aristas hacia `FAILED` desde estados activos formalmente validadas por el oráculo independiente en `tests/test_states.py`. |
| **F-26** | Bajo | **CERRADO** | Inyección del puerto `Clock` en `Orchestrator` y pipeline de debate, con método `sleep(seconds)` para desacoplar esperas operacionales y mantener pruebas deterministas sin retardo. |
| **F-27** | Bajo | **CERRADO** | Tipado estricto en `interviewer.py:conduct_interview` eliminando `# type: ignore[return-value]`. |
| **N-01** | Alto | **CERRADO** | Captura completa de `LLMError`, `TimeoutError`, `ConnectionError`, `OSError` durante entrevista, debate y mediación, registrando `PROJECT_FAILED` y transición a `FAILED`. |
| **N-02** | Alto | **CERRADO** | Eliminado `circuit_breaker.reset` de `run_debate`. Presupuestos estrictamente acumulativos por proyecto (INV-17). |
| **N-03** | Alto | **CERRADO** | `create_project` comprueba eventos previos antes de persistir `PROJECT_CREATED`, impidiendo la corrupción del log. |
| **N-04** | Alto | **CERRADO** | `record_state_call` valida estado de origen y rechaza llamadas cuando el proyecto ya está en `FAILED` lanzando `IllegalTransition` sin duplicar `PROJECT_FAILED`. |
| **N-05** | Medio | **CERRADO** | Implementado `SystemClock` (`now()` en UTC y `sleep()` con `time.sleep`) como reloj por defecto. Pruebas de backoff exponencial verificado con `_FakeClock` para los tres agentes del debate. |
| **N-06** | Medio | **CERRADO** | `PROJECT_ID_REGEX` usa `\Z` y `fullmatch` en `validate_project_id`, rechazando `"abc\n"`. |
| **N-07** | Medio | **CERRADO** | `project_status_from_events` rechaza eventos previos a `PROJECT_CREATED`, rechaza `PLAN_CREATED` fuera de `DEBATE`/`MEDIATION`, rechaza `APPROVAL_RECORDED` con destino no `BUILDING` y admite la transición legítima `VERIFYING -> BUILDING`. |
| **N-08** | Medio | **CERRADO** | `Spec` reconstruido desde `PROJECT_CREATED` (preservando `objective`) y `SPEC_UPDATED` sin depender de caché en memoria desincronizada entre instancias. |
| **N-09** | Medio | **CERRADO** | `handle_interview_message` comprueba idempotencia del `event_id` en el log antes de validar el estado del proyecto, ignorando duplicados sin lanzar `IllegalTransition`. |
| **N-10** | Medio | **CERRADO** | `EXPECTED_CONVERTER_PLAN_SHA256` verificado contra digest SHA-256 real calculado a mano y sensibilidad probada ante cambios en `files`, `acceptance_tests` y `risks`. |
| **N-11** | Medio | **Aceptado como diseño** | SQLite local sin triggers de esquema para inmutabilidad; la invariante append-only se garantiza a través de la API pública de `SqliteEventStore`. Se evaluará agregar triggers en Fase 2. |
| **N-12** | Medio | **CERRADO** | Reportes `docs/PHASE_REPORT_1.md` y `docs/DEBT.md` actualizados con cifras exactas verificadas de la ejecución real. |
