# Defectos detectados en tests de Fase 3 (`INV-13`)

En cumplimiento de `INV-13` y la regla explícita de **PASO B'** (*PROHIBIDO modificar, borrar o agregar archivos en `tests/`, `templates/` o `pyproject.toml`*), **no se modificó** el archivo de pruebas afectado. Este documento registra el defecto observado en el fixture de prueba.

## `ISSUE-3.1`: Fixture de `FakeRunner` incompleto en `tests/test_telegram_polling_and_acl.py`

- **Test afectado**: `tests/test_telegram_polling_and_acl.py::test_telegram_bot_rejects_mismatched_adjust_hash_and_survives_malformed_or_failed_poll_batch`
- **Punto de fallo**: `tests/test_telegram_polling_and_acl.py:730`
  - Esperado por la aserción: `orch.get_status(pid).state == ProjectState.DELIVERED`
  - Observado: `ProjectState.FAILED`
- **Causa raíz exacta**:
  1. La función helper `_complete_plan_payload` (`tests/test_telegram_polling_and_acl.py:82-110`) incluye **dos** pruebas de aceptación (`AT-01` y `AT-02` en `:95-106`), lo cual queda reflejado en `status_wait.tests = TestCounts(total=2, passed=0, failed=0)`.
  2. Sin embargo, al construir `BuildWorker` para este test en `tests/test_telegram_polling_and_acl.py:666-668`, se inyecta:
     ```python
     runner=FakeRunner(results=[CommandResult(exit_code=0, stdout="1 passed", test_results={"AT-01": True})])
     ```
     omitiendo `"AT-02": True`.
  3. Durante el flujo de `approval_processed` (`update_id=702`), `BuildWorker` exige correctamente que todas las pruebas de aceptación del plan (`AT-01` y `AT-02`) reporten resultado en `test_results`. Al faltar `AT-02`, `BuildWorker` reintenta hasta disparar el circuit breaker central del estado `BUILDING` (`"Circuit breaker tripped for project 'app-resiliente' in state BUILDING: 16 calls exceeded limit of 15"`), transicionando el proyecto a `FAILED`.
  4. Como consecuencia, la aserción de `:730` falla y bloquea la ejecución de las aserciones posteriores de ese mismo caso de prueba (observando además que el rechazo previo por mismatch de `plan_hash` en `bad_adj` / `update_id=701` sí se ejecutó y produjo `action='adjustment_rejected'`).

- **Por qué no debe "arreglarse" en `src/`**:
  - Aceptar ejecuciones parciales sin todas las pruebas del plan en `src/agent_factory/builder.py` rompería las garantías de Fase 2 (`AC-2.1` / verificación completa de pruebas de aceptación antes de pasar a `CI_PENDING` / `DELIVERED`) y violaría `INV-13`.

- **Acción recomendada cuando se habilite la corrección de tests**:
  - En `tests/test_telegram_polling_and_acl.py:667`, incluir `"AT-02": True` en `test_results` del `FakeRunner` (por ejemplo, `test_results={"AT-01": True, "AT-02": True}`).
