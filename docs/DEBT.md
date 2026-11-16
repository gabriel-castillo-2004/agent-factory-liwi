# Registro de Deuda Técnica y Estado de Hallazgos tras Paso B' (`DEBT.md`)

Este documento registra los hallazgos de `docs/AUDIT_REPORT_1.md` y su estado tras la finalización del endurecimiento en el Paso B'.

| ID | Severidad | Estado tras Paso B' | Detalle y Resolución |
|---|---|---|---|
| **F-13** | Medio | **Resuelto** | Se restringieron los bloques `except` en `mediator.py` y `orchestrator.py` para capturar exclusivamente excepciones operacionales del LLM (`InvalidLLMResponseError`, `PlanValidationError`, `ValueError`, `TimeoutError`, `CircuitBreakerTrippedError`, `RuntimeError`), permitiendo propagar errores internos de programación. |
| **F-14** | Medio | **Aceptado como diseño** | `Spec.fill_gaps_with_assumptions` rellena campos faltantes con supuestos explícitos en `Spec.assumptions` y marca `is_complete() == True`, cumpliendo contractualmente con AC-1.2. |
| **F-20** | Medio | **Resuelto** | Documentado formalmente en `docs/decisions/ADR-003-pydantic.md` justificando `pydantic==2.14.3` (INV-08). |
| **F-21** | Medio | **Abierto (Fase 2)** | Configuración de CI: `.github/workflows/ci.yml` ejecuta `pytest --cov=src/agent_factory` sin `--cov-fail-under=85`. En Fase 1 no se modifican workflows para preservar la inmutabilidad de configuración del runner (INV-13). Se incluirá al iniciar Fase 2. |
| **F-22** | Bajo | **Aceptado como diseño** | `STATE_BASE_PROGRESS` incluye `CHANGE_REQUEST: 10`, valor base provisional hasta su desarrollo completo en Fase 5. |
| **F-23** | Bajo | **Aceptado como diseño** | Regla de redondeo proporcional en `BUILDING` (`round(35 + 55 * ratio)`). El valor `62` para `2/4` pruebas pasadas está formalmente anclado en la suite de pruebas. |
| **F-24** | Bajo | **Resuelto** | Inclusión de `timestamp` ISO en payload de `APPROVAL_RECORDED` y validación estricta de límites de `progress_pct` (`0 <= progress_pct <= 100`) en `ProjectStatus`. |
| **F-25** | Bajo | **Resuelto** | Aristas hacia `FAILED` desde estados activos formalmente validadas por el oráculo independiente en `tests/test_states.py`. |
| **F-26** | Bajo | **Resuelto** | Inyección del puerto `Clock` en `Orchestrator` y pipeline de debate, con método `sleep(seconds)` para desacoplar esperas operacionales y mantener pruebas deterministas sin retardo. |
| **F-27** | Bajo | **Resuelto** | Tipado estricto en `interviewer.py:conduct_interview` eliminando `# type: ignore[return-value]`. |

