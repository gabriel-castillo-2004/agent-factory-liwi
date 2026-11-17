# Defectos detectados en tests de Fase 3 (`INV-13`)

En cumplimiento de `INV-13`, durante el **PASO B'** se preservó intacto el fixture de pruebas para no modificar archivos en `tests/` y se documentó el defecto observado.
Posteriormente, en el **PASO A''**, se aplicó el ajuste quirúrgico del fixture autorizado en `tests/test_telegram_polling_and_acl.py:667` sin alterar aserciones ni el oráculo de prueba.

---

## `ISSUE-3.1`: Fixture de `FakeRunner` incompleto en `tests/test_telegram_polling_and_acl.py` [RESUELTO]

- **Estado:** RESUELTO en **PASO A''**.
- **Test afectado**: `tests/test_telegram_polling_and_acl.py::test_telegram_bot_rejects_mismatched_adjust_hash_and_survives_malformed_or_failed_poll_batch`
- **Punto de fallo previo**: `tests/test_telegram_polling_and_acl.py:730`
  - Esperado por la aserción: `orch.get_status(pid).state == ProjectState.DELIVERED`
  - Observado previamente en PASO B': `ProjectState.FAILED`
- **Causa raíz exacta**:
  1. La función helper `_complete_plan_payload` (`tests/test_telegram_polling_and_acl.py:82-110`) incluye **dos** pruebas de aceptación (`AT-01` y `AT-02` en `:95-106`), lo cual queda reflejado en `status_wait.tests = TestCounts(total=2, passed=0, failed=0)`.
  2. Sin embargo, al construir `BuildWorker` para este test en `tests/test_telegram_polling_and_acl.py:666-668`, se inyectaba:
     ```python
     runner=FakeRunner(results=[CommandResult(exit_code=0, stdout="1 passed", test_results={"AT-01": True})])
     ```
     omitiendo `"AT-02": True`.
  3. Durante el flujo de `approval_processed` (`update_id=702`), `BuildWorker` exige correctamente que todas las pruebas de aceptación del plan (`AT-01` y `AT-02`) reporten resultado en `test_results`. Al faltar `AT-02`, `BuildWorker` reintentaba hasta disparar el circuit breaker central del estado `BUILDING` (`"Circuit breaker tripped for project 'app-resiliente' in state BUILDING: 16 calls exceeded limit of 15"`), transicionando el proyecto a `FAILED`.
  4. Como consecuencia, la aserción de `:730` fallaba y bloqueaba la ejecución de las aserciones posteriores de ese mismo caso de prueba.

- **Resolución aplicada en PASO A''**:
  - En `tests/test_telegram_polling_and_acl.py:667`, se completó el fixture de `FakeRunner` con `test_results={"AT-01": True, "AT-02": True}`, manteniendo intactas todas las aserciones, oráculos y validaciones de `test_telegram_bot_rejects_mismatched_adjust_hash_and_survives_malformed_or_failed_poll_batch`.
  - El test ahora pasa exitosamente en verde junto con la totalidad de la suite.
