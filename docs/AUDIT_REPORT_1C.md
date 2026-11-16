# AUDIT_REPORT_1C — Re-auditoría de cierre de la Fase 1 (segundo endurecimiento)

Auditor independiente (modo solo lectura). Rama `phase-1-hardening`, HEAD `e76a95f`, auditado contra el tag `phase-1.2-tests` (`d8659db`) y contra la línea base `phase-1` (`94edeab`).
Referencias: `MASTER_PROMPT.md` v1.2.1 (con enmienda autorizada en `INV-17`), `AGENTS.md`, `docs/PHASE_REPORT_1.md`, `docs/AUDIT_REPORT_1.md`, `docs/AUDIT_REPORT_1B.md`, `docs/HARDENING_TEST_MAP.md`, `docs/DEBT.md`.

---

## Veredicto: **APTO**

Los tres bloqueantes que motivaron el rechazo en `AUDIT_REPORT_1B.md` (**N-01** excepciones de transporte del LLM, **N-02** reinicio del circuit breaker en `run_debate` y **N-03/N-04** corrupción del log de eventos desde la API pública), así como los hallazgos medios **N-05 a N-10** y **N-12**, han sido cerrados con evidencia verificable en código y pruebas.

Las **11 mutaciones originales (M1–M11)** —incluida **M7**, que había sobrevivido en 1B— y las **mutaciones previamente supervivientes (X1/X1b/X1c, X6a, X7, X9, X16, X17)** ahora **mueren todas** al ejecutar la suite en un `git worktree` temporal aislado. El diff entre `phase-1.2-tests` y `HEAD` sobre `tests/`, `pyproject.toml`, `config/` y `.github/` es vacío (INV-13).

Quedan únicamente observaciones menores no bloqueantes (registradas en §5 y en `docs/DEBT.md`) que no comprometen ningún criterio de aceptación AC-1.1 a AC-1.13.

---

## 1. Resultados reales de ejecución (una sola corrida por comando)

| Comando | Resultado real |
|---|---|
| `ruff check .` | `All checks passed!` (exit code 0) |
| `pytest -q --cov=src/agent_factory --cov-report=term-missing` | `227 passed, 2 warnings in 4.67s` (exit code 0). Cobertura total: **93 %** (821 sentencias, 45 sin cubrir; 174 ramas, 18 parciales). |
| `git diff phase-1.2-tests HEAD -- tests pyproject.toml config .github` | Vacío (0 bytes). |
| Cobertura `src/agent_factory/domain/` | `events.py` **100 %**, `models.py` **100 %**, `progress.py` **93 %**, `states.py` **100 %** (promedio ponderado del paquete: **97 %**, umbral ≥ 85 % cumplido). |
| Cobertura `src/agent_factory/agents/` | `interviewer.py` **98 %**, `mediator.py` **96 %**, `pragmatist.py` **100 %**, `structuralist.py` **100 %** (promedio ponderado del paquete: **98 %**, umbral ≥ 85 % cumplido). |
| Cobertura otros módulos | `inbox.py` **100 %**, `system_clock.py` **100 %**, `memory_store.py` **100 %**, `sqlite_store.py` **96 %**, `file_publisher.py` **92 %**, `orchestrator.py` **87 %**, `fake_llm.py` **77 %**. |

---

## 2. Estado de cierre de hallazgos de `AUDIT_REPORT_1B.md` (N-01 a N-12)

| ID | Sev. | Estado | Evidencia (`archivo:línea`) y análisis |
|---|---|---|---|
| **N-01** Excepciones de transporte LLM (`ConnectionError`, `OSError`, `LLMError`) dejan proyecto atascado | Alto | **CERRADO** | `ports/llm.py:8-13` define `LLMError(Exception)` como clase base de `InvalidLLMResponseError`. `pragmatist.py:50`, `structuralist.py:51` y `mediator.py:107` capturan `(LLMError, ValueError, TimeoutError, OSError)` con reintento acotado y backoff. `mediator.py:174-181` y `orchestrator.py:216-237, 307-325` capturan la familia completa, validan la transición a `FAILED`, persisten `PROJECT_FAILED` y devuelven `ProjectState.FAILED`. Cubierto por 6 casos parametrizados en `tests/test_llm_failures.py:81-162`. |
| **N-02** `run_debate` reiniciaba contadores del `CircuitBreaker` | Alto | **CERRADO** | `MASTER_PROMPT.md:53` (`INV-17`) fue enmendado con autorización del usuario para explicitar que los contadores son acumulativos por proyecto y nunca se reinician. En `orchestrator.py:260-285` se eliminaron las llamadas a `self.circuit_breaker.reset(...)`. `tests/test_circuit_breaker.py:201-235` verifica que un segundo `run_debate` con el tope por defecto (`DEBATE=3`) dispara `FAILED` en la 4.ª llamada acumulada. `tests/test_orchestrator_approval.py:276-278` pasa límites explícitos (`DEBATE=6, MEDIATION=4`) para el escenario de re-debate completo. *Nota menor:* el método `CircuitBreaker.reset` (`orchestrator.py:91-98`) quedó como código sin llamadores en producción. |
| **N-03** `create_project` duplicado corrompía el log de eventos | Alto | **CERRADO** | `orchestrator.py:154-158` consulta `existing_events = self.store.get_events(project_id)` **antes** de construir o agregar `PROJECT_CREATED`. Si existen eventos previos, ejecuta `validate_transition(current_status.state, ProjectState.INTERVIEW)` y lanza `ValueError` como guardia adicional si la transición fuera legal desde otro estado. `tests/test_states.py:302-327` verifica que la segunda llamada falla y `len(store.get_events(project_id)) == 1`. |
| **N-04** `record_state_call` en estado `FAILED` duplicaba `PROJECT_FAILED` y envenenaba el log | Alto | **CERRADO** | `orchestrator.py:404-410` verifica que el proyecto exista y que `current_status.state != ProjectState.FAILED` (lanzando `IllegalTransition(ProjectState.FAILED, state)` antes de tocar el breaker o el store), y en `orchestrator.py:422` ejecuta `validate_transition(current_status.state, ProjectState.FAILED)` antes de persistir `PROJECT_FAILED`. Cubierto en `tests/test_states.py:330-355`. |
| **N-05** Backoff `clock.sleep` sin adaptador real ni cobertura de tests | Medio | **CERRADO** | `adapters/system_clock.py:6-16` implementa `SystemClock` (`datetime.now(UTC)` y `time.sleep(seconds)`). `orchestrator.py:121` asigna `SystemClock()` por defecto cuando `clock is None`. `tests/test_debate_mediator.py:260-327` verifica el backoff `[0.1, 0.2]` en `Pragmatist`, `Structuralist` (incluyendo agotamiento de reintentos) y `Mediator` con `_FakeClock`, elevando la cobertura de `structuralist.py` de 74 % a **100 %**. |
| **N-06** Regex de `project_id` aceptaba salto de línea final (`"abc\n"`) | Medio | **CERRADO** | `domain/models.py:13,18` usa `re.compile(r"^[a-z0-9][a-z0-9-]{0,62}\Z")` y `PROJECT_ID_REGEX.fullmatch(project_id)`. `"abc\n"` fue añadido a `INVALID_PROJECT_IDS` en `tests/test_security_project_id.py:27`. |
| **N-07** Proyección `project_status_from_events` laxa ante eventos fuera de orden y bloqueaba `VERIFYING -> BUILDING` | Medio | **CERRADO** | `domain/progress.py:59-64` rechaza cualquier evento previo a `PROJECT_CREATED`; `:82-85` permite `STATE_TRANSITIONED` hacia `BUILDING` únicamente cuando `current_state == ProjectState.VERIFYING` (autocorrección de Sección 4); `:96-101` rechaza `PLAN_CREATED` fuera de `(DEBATE, MEDIATION)` y exige transición válida hacia `WAITING_APPROVAL`; `:113-129` exige en `APPROVAL_RECORDED` que el estado origen sea `WAITING_APPROVAL`, el destino sea `BUILDING`, `decision == "approved"` y `plan_hash` coincida. Cubierto por 11 casos en `tests/test_progress_and_projection.py:352-512`. |
| **N-08** `_specs` en memoria desincronizado entre instancias y pérdida de `objective` inicial | Medio | **CERRADO** | `orchestrator.py:128-144` reconstruye siempre `Spec` leyendo los eventos del store (`PROJECT_CREATED` para `initial_objective` y `SPEC_UPDATED` para el último `Spec`), eliminando el cortocircuito `if project_id in self._specs:`. Cubierto por `tests/test_interviewer.py:232-310` con dos `Orchestrator` compartiendo un `SqliteEventStore`. |
| **N-09** `event_id` duplicado recibido tras salir de `INTERVIEW` lanzaba `IllegalTransition` | Medio | **CERRADO** | `orchestrator.py:184-191` verifica `self.inbox.is_processed(event_id)` y la presencia de `incoming_event_id == event_id` en el store **antes** de comprobar `current_status.state != ProjectState.INTERVIEW`. Cubierto en `tests/test_inbox.py:165-248` (en la misma instancia y tras reinicio). |
| **N-10** `EXPECTED_CONVERTER_PLAN_SHA256` sin usar y `compute_hash` no probado contra cambios en `files`/`acceptance_tests`/`risks` | Medio | **CERRADO** | `tests/test_orchestrator_approval.py:22-24, 171-213` usa el digest real `07566aa3b1f5145823e388aa12eb4c258b8943d7222c7df4b3ceff73c9a19b7b` y comprueba sensibilidad ante cambios en `summary`, `files`, `acceptance_tests` y `risks` (5 hashes distintos de 64 hex). |
| **N-11** `SqliteEventStore` sin triggers SQL `BEFORE UPDATE`/`BEFORE DELETE` | Medio | **ABIERTO (documentado en `DEBT.md`)** | `sqlite_store.py:22-35` solo expone `append` y `get_events` en su API pública, pero la tabla SQLite no define triggers contra `UPDATE`/`DELETE` directos. Registrado honestamente en `docs/DEBT.md:39` para evaluación en Fase 2. No incumple AC-1.10. |
| **N-12** Sobre-afirmaciones e inexactitudes en `PHASE_REPORT_1.md` y `DEBT.md` | Medio | **CERRADO** | `docs/PHASE_REPORT_1.md:33-63` y `docs/DEBT.md:1-40` fueron actualizados con las cifras reales verificadas (227 tests, cobertura exacta por módulo y paquete) y el estado real de `F-20`, `F-21` y `N-11`. |

---

## 3. Verificación de Tests vs. Criterios (AC-1.1 a AC-1.13) e INV-23

- **Cumplimiento de INV-13**: `git diff phase-1.2-tests HEAD -- tests/ pyproject.toml` es vacío. No existen usos de `pytest.mark.skip`, `xfail`, `unittest.mock`, `monkeypatch` ni `assert True` en `tests/`.
- **Cumplimiento de INV-23 (oráculo independiente)**:
  - `tests/test_states.py:20-135`: las 19 transiciones válidas y 81 ilegales están escritas a mano sin importar `ALLOWED_TRANSITIONS`.
  - `tests/test_orchestrator_approval.py:22-24, 171-265`: el digest SHA-256 esperado está escrito a mano y verificado contra el plan base; los rechazos de aprobación prueban `INTERVIEW`, `BUILDING` y `FAILED` con el `plan_hash` vigente.
  - `tests/test_progress_and_projection.py:179-193, 269-349`: todos los valores de `ProjectStatus` y porcentajes de avance están escritos a mano.
  - `tests/test_debate_mediator.py:260-314`: los tiempos de backoff esperados `[0.1, 0.2]` están escritos a mano y verificados contra `_FakeClock.sleeps`.

---

## 4. Pruebas de Mutación (`git worktree` temporal fuera del workspace, ya eliminado)

Se ejecutaron **27 mutaciones** en `%TEMP%\wt-1c` (eliminado al finalizar; verificado con `git worktree list` y `exists=False`):

| ID | Mutación aplicada | Resultado | ¿Detectada? |
|---|---|---|---|
| **M1** | Permitir `IDLE -> BUILDING` en `states.py` | `1 failed` (`test_states.py`) | **Sí (KILLED)** |
| **M2** | Omitir comparación de `plan_hash` en `submit_approval` | `2 failed` (`test_orchestrator_approval.py`) | **Sí (KILLED)** |
| **M3** | Desactivar tope en `CircuitBreaker.record_call` | `7 failed` (`test_circuit_breaker.py`) | **Sí (KILLED)** |
| **M4** | Quitar tope `[:MAX_QUESTIONS_PER_ROUND]` en `interviewer.py` | `1 failed` (`test_interviewer.py`) | **Sí (KILLED)** |
| **M5** | `agent_invocations += 5` en `progress.py` | `1 failed` (`test_progress_and_projection.py`) | **Sí (KILLED)** |
| **M6** | `Plan.compute_hash` devuelve constante `"a" * 64` | `3 failed` (`test_orchestrator_approval.py`) | **Sí (KILLED)** |
| **M7** | Eliminar chequeo `if current_status.state != ProjectState.WAITING_APPROVAL:` en `submit_approval` | `1 failed` (`test_orchestrator_approval.py`) | **Sí (KILLED)** |
| **M8** | Omitir validación de `template_id` en `validate_plan` | `1 failed` (`test_debate_mediator.py`) | **Sí (KILLED)** |
| **M9** | `Inbox.is_processed` devuelve siempre `False` | `2 failed` (`test_inbox.py`) | **Sí (KILLED)** |
| **M10** | Cambiar tope de avance `BUILDING` de `90` a `80` en `progress.py` | `5 failed` (`test_progress_and_projection.py`) | **Sí (KILLED)** |
| **M11** | No lanzar `ApprovalError` cuando `approval is None` | `1 failed` (`test_orchestrator_approval.py`) | **Sí (KILLED)** |
| **X1** | `Plan.compute_hash` ignora `files` | `2 failed` (`test_orchestrator_approval.py`) | **Sí (KILLED)** |
| **X1b** | `Plan.compute_hash` ignora `acceptance_tests` | `2 failed` (`test_orchestrator_approval.py`) | **Sí (KILLED)** |
| **X1c** | `Plan.compute_hash` ignora `risks` | `2 failed` (`test_orchestrator_approval.py`) | **Sí (KILLED)** |
| **X6a** | Eliminar `copy.deepcopy` en `MemoryEventStore.append` | `1 failed` (`test_store_contract.py`) | **Sí (KILLED)** |
| **X7** | Eliminar `self.clock.sleep(...)` en `Pragmatist.propose` | `1 failed` (`test_debate_mediator.py`) | **Sí (KILLED)** |
| **X16** | Eliminar `self.clock.sleep(...)` en `Structuralist.critique` | `1 failed` (`test_debate_mediator.py`) | **Sí (KILLED)** |
| **X17** | Eliminar `self.clock.sleep(...)` en `Mediator.mediate` | `1 failed` (`test_debate_mediator.py`) | **Sí (KILLED)** |
| **X9** | Eliminar `validate_transition(current_state, ProjectState.FAILED)` en `progress.py` | `5 failed` (`test_progress_and_projection.py`) | **Sí (KILLED)** |
| **C1** | Quitar `LLMError` y `OSError` del `except` en `handle_interview_message` | `3 failed` (`test_llm_failures.py`) | **Sí (KILLED)** |
| **C2** | Reintroducir `circuit_breaker.reset(DEBATE)` en `run_debate` | `1 failed` (`test_circuit_breaker.py`) | **Sí (KILLED)** |
| **C3** | Eliminar guardia `if existing_events:` en `create_project` | `1 failed` (`test_states.py`) | **Sí (KILLED)** |
| **C4** | Eliminar guardia `if current_status.state == ProjectState.FAILED:` en `record_state_call` | `1 failed` (`test_states.py`) | **Sí (KILLED)** |
| **C5** | Cambiar solo `\Z` por `$` manteniendo `.fullmatch()` en `models.py` | `26 passed` (defensa en profundidad: `.fullmatch()` también rechaza `"abc\n"`; al mutar ambos a `.match(...$)` fallan 2 tests) | **Defensa doble** |
| **C6** | Quitar solo `if current_state not in (DEBATE, MEDIATION):` manteniendo `validate_transition(current_state, WAITING_APPROVAL)` en `PLAN_CREATED` | `32 passed` (defensa en profundidad: `validate_transition` en la línea siguiente rechaza `INTERVIEW`/`BUILDING`/`VERIFYING` con `IllegalTransition`) | **Defensa doble** |
| **C7** | Reintroducir cortocircuito `if project_id in self._specs:` en `_get_spec` | `1 failed` (`test_interviewer.py`) | **Sí (KILLED)** |
| **C8** | Mover chequeo `state != INTERVIEW` antes de deduplicación en `handle_interview_message` | `1 failed` (`test_inbox.py`) | **Sí (KILLED)** |

---

## 5. Observaciones menores residuales (no bloqueantes)

1. **`CircuitBreaker.reset` sin uso (`orchestrator.py:91-98`)**: tras eliminar el reinicio de contadores en `run_debate` (N-02), el método `reset()` ya no tiene llamadores en producción ni en tests (6 líneas sin cubrir en `orchestrator.py`). Puede eliminarse en limpieza futura.
2. **Retardo real de ~2.4 s en tests de fallo que no inyectan `_FakeClock`**: como `Orchestrator` ahora usa `SystemClock()` por defecto (N-05), los tests de `test_llm_failures.py` y `test_circuit_breaker.py` que instan `Orchestrator(store=..., llm=...)` sin pasar un reloj falso ejecutan `time.sleep(0.1)` + `time.sleep(0.2)` reales en los reintentos, subiendo el tiempo total de la suite de ~2.1 s a ~4.7 s (dentro del límite de 30 s por prueba).
3. **Contadores del `CircuitBreaker` en memoria (`orchestrator.py:71`)**: los contadores por estado viven en `CircuitBreaker._counts` en memoria de la instancia. En la Fase 2/3, cuando el worker local pueda reiniciarse a mitad de un proyecto, convendrá reconstruir o respaldar el conteo desde los eventos `AGENT_INVOKED` del `EventStore`.
4. **Deudas de Fase 2 ya registradas en `docs/DEBT.md`**: lockfile de dependencias transitivas (`F-20`), flag `--cov-fail-under=85` en `.github/workflows/ci.yml` (`F-21`) y triggers SQL append-only opcionales en SQLite (`N-11`).

---

## 6. Lo que NO pude verificar y por qué

1. **Ejecución real del workflow de GitHub Actions (`ci.yml` en Ubuntu)**: la auditoría se ejecutó localmente en Windows (`Python 3.12.10`) sin acceso a red ni push al remoto.
2. **Integración con proveedores LLM reales / CLI `agy`**: corresponde a la Fase 2; en Fase 1 toda interacción usa `FakeLLM` y `_MinimalStatelessLLM` offline.
3. **Concurrencia multiproceso real sobre SQLite**: se verificó la sincronización entre dos instancias de `Orchestrator` sobre el mismo archivo `.db` en un proceso (`test_two_orchestrators_sharing_sqlite_store_preserve_objective_and_sync_spec`), pero no contención de bloqueos (`SQLITE_BUSY`) entre procesos simultáneos del sistema operativo.
