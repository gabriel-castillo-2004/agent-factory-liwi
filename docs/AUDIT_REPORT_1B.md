# AUDIT_REPORT_1B — Re-auditoría independiente de la Fase 1 (endurecimiento)

Auditor independiente (no construyó el código). Rama `phase-1-hardening`, HEAD `33b511e`, contra tag `phase-1.1-tests` (`2ee1b34`). Referencias: `MASTER_PROMPT.md` v1.2.1, `AGENTS.md`, `docs/AUDIT_REPORT_1.md`, `docs/PHASE_REPORT_1.md`, `docs/HARDENING_TEST_MAP.md`.
Los PoC están en `brain/.../scratch/poc2.py` y `mut2.py` (fuera del repo). Todas las salidas citadas son reales.

## Veredicto: **NO APTO** (cierre cercano, con 3 correcciones obligatorias)

Lo bueno, verificado: los 4 críticos (F-01..F-04) están cerrados, 201/201 tests pasan, el diff de `tests/`, `pyproject.toml`, `config/` y `.github/` contra `phase-1.1-tests` es vacío, y 10 de las 11 mutaciones originales ahora se detectan.

Lo que impide aprobar: tres criterios de aceptación del propio MASTER_PROMPT siguen incumplidos con evidencia ejecutable:

1. **AC-1.13 / INV-09** — una excepción del LLM distinta de las 4 listadas (p. ej. `ConnectionError`) deja el proyecto **atascado en `DEBATE`/`INTERVIEW` sin evento de fallo** (N-01). La corrección de F-13 reintrodujo F-07 para el caso general.
2. **AC-1.8 / INV-17** — `run_debate` **reinicia** los contadores del breaker en cada invocación (`orchestrator.py:255-256`). 6 re-debates consumieron **19 llamadas LLM** con topes DEBATE=3 / MEDIATION=2 (N-02).
3. **INV-11 / INV-04** — la API pública permite **envenenar el registro**: `create_project` duplicado o `record_state_call` pasado el tope dejan un evento ilegal persistido y `get_status` lanza `IllegalTransition` para siempre (N-03, N-04).

Además `PHASE_REPORT_1.md` y `DEBT.md` afirman cierres que no son ciertos (N-12).

## 1. Resultados reales de ejecución (una vez cada uno)

| Comando | Resultado |
|---|---|
| `ruff check .` | `All checks passed!` |
| `pytest --cov=src/agent_factory` | `201 passed, 2 warnings in 2.27 s`. Cobertura total 92 %. `domain/`: events 100, models 100, states 100, progress 93. `agents/`: interviewer 98, mediator 95, pragmatist 94, **structuralist 74**. `orchestrator.py` 87 %, `fake_llm.py` 75 %, `clock.py` 75 %. |
| `git diff phase-1.1-tests HEAD -- tests pyproject.toml config .github` | Vacío. |
| Scan `skip/xfail/mock/monkeypatch/patch/assert True` en `tests/` | Sin coincidencias. |
| Scan `subprocess/os.system/eval/exec/requests/urllib/socket` en `src/` | Sin coincidencias. |
| `pip list` vs ADR-003 | `pydantic==2.14.3` coincide con `pyproject.toml` (`dependencies`). Transitivas (`pydantic_core 2.50.4`, `annotated-types 0.8.0`, `typing_extensions 4.16.0`, `typing-inspection 0.4.5`) **sin fijar ni lockfile**. |

## 2. Cierre de hallazgos previos (F-01 a F-19)

| ID | Estado | Evidencia |
|---|---|---|
| F-01 decision ignorada | **CERRADO** | `orchestrator.py:354` (`decision != "approved"`), `progress.py:108-109`. Mutaciones X14 (orq.) y X3 (proyección) detectadas. `decision` sigue siendo `str` libre (`models.py`), no `Literal`; es aceptable porque se compara con igualdad estricta. |
| F-02 `validate_transition` sin uso | **CERRADO (con reservas)** | Usado en `orchestrator.py:249,271,322,362` y `progress.py:64,79,94,114,142,148`. Reserva: `orchestrator.py:146` `validate_transition(IDLE, INTERVIEW)` es una **tautología** (constantes), no valida nada; ver N-03. `record_state_call` (`orchestrator.py:378-404`) no valida estado de origen, contra INV-04 ("cada operación valida su estado de origen"); ver N-04. |
| F-03 `run_debate` sin precondición | **CERRADO** | `orchestrator.py:243-253` (existencia, `validate_transition(estado, DEBATE)`, `spec.is_complete()`). X12, X13 detectadas. |
| F-04 proyección confía en `to` | **PARCIAL** | Cierra lo principal (`progress.py:64-116`). Pero la proyección sigue laxa: N-07 (PoC P8: `PLAN_CREATED` en `VERIFYING` sobrescribe `plan_hash`; `APPROVAL_RECORDED{decision:"rejected", to:"DELIVERED"}` desde `VERIFYING` → `DELIVERED`; eventos sin `PROJECT_CREATED` se aceptan; `VERIFYING→BUILDING` legítimo por la sección 4 se rechaza con `ValueError`, `progress.py:76`). |
| F-05 path traversal | **PARCIAL** | Regex + `is_relative_to` (`models.py:13-22`, `file_publisher.py:18-22`). **Defecto:** `re.match(r"^…$")` acepta `"abc\n"` (PoC P4: `'abc\n'` ACEPTADO). En Linux crearía `abc\n.json`. Falta en la lista de casos inválidos. Además `is_relative_to` es redundante y su mutación sobrevive (X10), es defensa en profundidad sin test propio. Stores (`memory_store`, `sqlite_store`) no validan `project_id`. |
| F-06 breaker no cableado | **PARCIAL** | Cableado en el pipeline vía `_PipelineLLMWrapper` (`mediator.py:119-133`, `orchestrator.py:267-268`); reintentos contados (tests de `test_circuit_breaker.py` pasan, M3 detectada). Pero: (a) **N-02** el `reset` anula el presupuesto; (b) no es un "único punto central": la entrevista cuenta aparte (`orchestrator.py:182`) y llama al LLM crudo (`:205`); `run_debate_pipeline` sin hook no tiene breaker (opt-in); (c) contadores en memoria, se pierden al reiniciar; (d) límite `BUILDING` solo se ejercita llamando `record_state_call` a mano. |
| F-07 excepciones LLM | **PARCIAL / REGRESIÓN** | `TimeoutError` y 4 tipos sí producen `PROJECT_FAILED` (`orchestrator.py:207,290`, tests `test_llm_failures.py`). Pero **N-01**: `ConnectionError`/`OSError`/`KeyError`/cualquier otra se propaga y deja el estado atascado (PoC P3, P3b). Backoff: ver N-05. Cuota agotada → pausa (INV-09): no implementado. |
| F-08 inbox no persistente | **PARCIAL** | Dedupe por `incoming_event_id` en el log (`orchestrator.py:173-174`); el test SQLite reabierto pasa y X2 se detecta. Pero `Inbox` sigue siendo un `set` en memoria (`inbox.py:12-13`, sin cambios) y redundante; un duplicado tras cambiar de estado lanza `IllegalTransition` en vez de ignorarse (PoC P7, N-09); un `event_id` rechazado por estado no se registra. |
| F-09 oráculo circular AC-1.1 | **CERRADO** | `test_states.py` define 19 válidas + 81 ilegales a mano, sin importar `ALLOWED_TRANSITIONS`; M1 detectada. Nota: el oráculo escrito comparte la decisión no documentada de F-25 (aristas a `FAILED`). |
| F-10 AC-1.6 débil | **CERRADO** | Asserts de contenido (`test_progress_and_projection.py:180-192`); M5 detectada. |
| F-11 hash/estado sin cobertura | **PARCIAL** | M6 detectada (3 tests), pero **X1** (`compute_hash` ignora `files`) **sobrevive**: `test_orchestrator_approval.py:171-207` solo prueba que dos hashes difieren por `summary`. `EXPECTED_CONVERTER_PLAN_SHA256` (`:22`) está **declarada y nunca usada**; el nombre del test promete "handwritten digest" y no lo hay. **M7 sobrevive** (ver §5), contradiciendo lo declarado en `HARDENING_TEST_MAP.md:38`. |
| F-12 `llm.calls` en producción | **CERRADO** | Contador propio `mediator.py:127,132,172`. |
| F-13 excepciones amplias | **PARCIAL** | `except Exception` eliminado, pero `ValueError`/`RuntimeError` siguen tragándose como fallo del proyecto (`orchestrator.py:207,290-297`; `mediator.py:174-180`) y el `FakeLLM` agotado sigue siendo `RuntimeError`→`FAILED`. `DEBT.md` lo marca "Resuelto". |
| F-14 relleno genérico | **ABIERTO (aceptado)** | `fill_gaps_with_assumptions` sin cambios. `DEBT.md` lo reclasifica "Aceptado como diseño" porque un test congelado lo exige; es una decisión legítima pero deja el defecto vivo. |
| F-15 `load_limits` silencioso | **CERRADO** | `orchestrator.py:51` lanza `FileNotFoundError`; test lo cubre. Sigue relativo al CWD y un `limits={}` o clave faltante deja el estado sin tope en silencio (menor). |
| F-16 Event inmutable | **PARCIAL** | `frozen=True` (`events.py`) + copias profundas (`memory_store.py:21`). X5, X6b detectadas; **X6a sobrevive** (copia en `append` sin test). `frozen` es superficial (el `dict` payload es mutable). SQLite sigue permitiendo `UPDATE`/`DELETE` (PoC P9) — sin triggers. |
| F-17 `_specs` en memoria | **PARCIAL** | `_get_spec` reconstruye desde `SPEC_UPDATED` (`orchestrator.py:127-136`). Pero la caché `_specs` nunca se invalida: dos `Orchestrator` sobre el mismo store divergen (PoC P5) y el `objective` de `create_project` (`:154`) no está en el log: tras reinicio queda `''` (PoC P12). |
| F-18 integración entrevista | **PARCIAL** | Rechaza proyecto inexistente y estado ≠ INTERVIEW (`:164-170`). `round_number` sigue siempre `1` (`interviewer.py:34`, llamada `orchestrator.py:205`), `terminated_by_limit` jamás se dispara desde el orquestador; el tope INTERVIEW=6 termina en `FAILED`, no en spec con supuestos. |
| F-19 falta `MEDIATION` | **CERRADO** | `mediator.py:160-162` + `orchestrator.py:270-280`; X8 detectada (11 fallos). |

Resumen: CERRADO 8 (F-01, F-02*, F-03, F-09, F-10, F-12, F-15, F-19) · PARCIAL 9 · ABIERTO 1 (F-14). (*con reservas)

## 3. Hallazgos nuevos

| ID | Sev. | Evidencia | Por qué importa |
|---|---|---|---|
| N-01 | **Alto** | PoC P3/P3b: `ConnectionError` en `run_debate` → excepción sale, `state=DEBATE`, 0 eventos `PROJECT_FAILED`; en entrevista `state=INTERVIEW`, 0 fallos (el `AGENT_INVOKED` sí quedó). `orchestrator.py:207,290-297` listan 4–6 tipos. | AC-1.13 e INV-09 ("nunca un proyecto atascado") **no se cumplen**. Un cliente HTTP real (Fase 2/3) lanza `OSError`/`httpx` errors. El arreglo de F-13 (quitar `except Exception`) reabrió F-07. Los tests solo usan `TimeoutError`. |
| N-02 | **Alto** | `orchestrator.py:255-256` `circuit_breaker.reset(DEBATE/MEDIATION)` en cada `run_debate`. PoC P6: 6 re-debates = 19 llamadas LLM (tope declarado 1+3+2 por proyecto). X15 (quitar reset) hace fallar 1 test: **el test congelado `test_stale_approval_rejected…` obliga a ese reset** (necesita 2 debates completos = 6 llamadas DEBATE con tope 3). | INV-17 dice "presupuesto duro **por proyecto**"; AC-1.8 "ninguna secuencia de reintentos puede exceder el tope". Un bucle ajuste→debate es ilimitado. Conflicto entre un test (INV-13) y el invariante; el Paso B' lo resolvió debilitando el invariante sin registrarlo en `HARDENING_TEST_ISSUES.md` ni `DEBT.md` (ese archivo no existe). |
| N-03 | **Alto** | `create_project` no verifica existencia (`orchestrator.py:138-155`; la línea 146 es tautológica). PoC P1: segundo `create_project("dup")` persiste `PROJECT_CREATED` y `get_status` lanza `IllegalTransition` permanentemente. | Registro append-only envenenado desde la API pública; el proyecto queda ilegible (INV-11, INV-04). El evento se escribe antes de validar. |
| N-04 | **Alto** | `record_state_call` (`orchestrator.py:378-404`) sin validar origen y sin `FAILED` idempotente. PoC P2: pasada la 7.ª llamada (INTERVIEW=6) la 8.ª agrega otro `PROJECT_FAILED` y `FAILED→FAILED` lanza. `progress.py:142` valida `→FAILED` pero el orquestador no precomprueba. | INV-04 (cada operación valida origen). Mismo envenenamiento que N-03. X9 (proyección sin validar `FAILED`) sobrevive. |
| N-05 | Medio | Backoff: `pragmatist.py:53`, `structuralist.py:57`, `mediator.py:110` solo duermen si `clock is not None`; `Orchestrator(clock=None)` es el valor por defecto (PoC P10: `sleeps=[]`; con reloj P11: `[0.1]`). **No existe ningún adaptador `Clock` con `sleep`**, ni test que lo use (`grep clock tests/` = 0). Mutaciones X7, X16, X17 (eliminar `sleep`) **sobreviven**; `structuralist.py:52-57` sin cobertura. El `Interviewer` no reintenta ni espera. Backoff fijo `0.1·2^n` hard-coded (no configurable). | INV-09 exige reintento acotado **con backoff**; hoy es código no ejecutado ni probado. `PHASE_REPORT_1.md` afirma "esperas… vía `Clock`… pruebas en milisegundos". |
| N-06 | Medio | `models.py:13,18`: `$` acepta salto de línea final (PoC P4). | Falla de INV-22 con dato externo; usar `fullmatch`/`\Z`. Falta caso `"abc\n"` en `INVALID_PROJECT_IDS`. |
| N-07 | Medio | PoC P8 (ver F-04). `progress.py:88-100` (`PLAN_CREATED` sin gating de estado), `:103-117` (`to` libre en `APPROVAL_RECORDED`), eventos `TESTS_UPDATED`/`AGENT_INVOKED` aceptados en cualquier estado, incl. antes de `PROJECT_CREATED`. | INV-11/INV-05: "rechaza eventos inválidos". Quien escriba en el store puede cambiar el `plan_hash` vigente tras la aprobación o saltar a `DELIVERED`. Además `VERIFYING→BUILDING` (sección 4, autocorrección) quedará rechazado en Fase 2 (`progress.py:76`). |
| N-08 | Medio | PoC P5/P12 (F-17). | Estado derivado de memoria; falla con >1 proceso/reinicio. |
| N-09 | Medio | PoC P7: duplicado de `event_id` después de que el proyecto salió de INTERVIEW lanza `IllegalTransition` (`orchestrator.py:166-170` ocurre **antes** del dedupe `:173`). | INV-16: un evento ya procesado "se ignora". Un reintento de Telegram tras `run_debate` produce error en lugar de no-op. |
| N-10 | Medio | `tests/test_orchestrator_approval.py:22-24,171-207` | INV-23: no hay hash escrito a mano; X1 sobrevive. |
| N-11 | Medio | `sqlite_store.py` sin triggers (PoC P9). | INV-11 sigue siendo convención en SQLite. |
| N-12 | Medio | `PHASE_REPORT_1.md` §2-§5 y `DEBT.md`: (a) dice "**11 mutaciones no detectadas**" (el informe anterior reportó 4 de 11); (b) cobertura `agents/` "93 %" y `domain/` ">98 %" no coinciden con la salida real (agents ≈ 94.7 % por sentencias, structuralist 74 %); (c) AC-1.8 "bajo el breaker central" (no es único ni persistente); (d) F-13 "Resuelto"; (e) F-20 "Resuelto" aunque no hay lockfile; (f) F-25 "Resuelto" sin ADR (el oráculo solo repite la tabla); (g) backoff "asegurando que las pruebas corran en milisegundos" sin ningún test; (h) AC-1.9 "persistente" mientras `Inbox` sigue en memoria; (i) no se creó `docs/HARDENING_TEST_ISSUES.md` aunque se tocó una contradicción test↔invariante (N-02). | El informe de fase vuelve a sobre-afirmar, el mismo patrón que motivó la primera auditoría. |
| N-13 | Bajo | `test_suite_offline.py` (tautología FakeLLM) sin cambios; `ci.yml` sin `--cov-fail-under` (F-21, registrado abierto en `DEBT.md`, correcto). | AC-1.11 sigue sin forzarse. |
| N-14 | Bajo | `fake_llm.py` 75 %, `ports/*` 70-86 % (líneas `raise NotImplementedError` de Protocol), sin impacto en el umbral por paquete pero baja la cobertura global real de lógica. | Informativo. |

## 4. Matriz AC → test → ¿protege realmente?

| AC | Test (archivo) | ¿Protege? | Motivo |
|---|---|---|---|
| AC-1.1 | `test_states.py` (19 válidas + 81 ilegales a mano; 4 tests de origen en orquestador) | **Sí** | M1 detectada. Origen de `run_debate`/`handle_interview_message` cubierto; **`record_state_call` y `create_project` no** (N-03, N-04). |
| AC-1.2 | `test_interviewer.py` | **Parcial** | Tope de 3 (M4 detectada). Relleno genérico (F-14), `round_number` fijo (F-18). |
| AC-1.3 | `test_debate_mediator.py` | **Parcial** | 3 llamadas contadas internamente. `structuralist` reintentos y backoff sin cobertura (74 %). |
| AC-1.4 | `test_debate_mediator.py` | **Sí** | M8 detectada. |
| AC-1.5 | `test_orchestrator_approval.py` | **Parcial** | rejected/adjust/pending/"" cubiertos (X14 detectada), hash distinto cubierto; **M7 sobrevive** y X1 sobrevive (hash no sensible a `files`). |
| AC-1.6 | `test_progress_and_projection.py` | **Parcial** | Valores a mano (M5 detectada) y rechazo de transición ilegal/BUILDING sin aprobación (X3, X4 detectadas). No cubre N-07 (`PLAN_CREATED` tardío, `APPROVAL` con `to` arbitrario). |
| AC-1.7 | `test_progress_and_projection.py` | **Sí** | M10 detectada; valores a mano. |
| AC-1.8 | `test_circuit_breaker.py` | **Parcial** | Debate y mediación acotados con FakeLLM (M3 detectada) pero un test congelado obliga a resetear contadores (N-02). BUILDING solo contador directo. |
| AC-1.9 | `test_inbox.py` | **Parcial** | Persistencia SQLite real (X2 detectada). No cubre duplicado tras cambio de estado (N-09) ni `Inbox` en sí. |
| AC-1.10 | `test_store_contract.py` | **Sí** | Una suite, dos stores, inmutabilidad (X5/X6b). X6a sobrevive; no verifica UPDATE/DELETE en SQLite. |
| AC-1.11 | `test_suite_offline.py`, `test_bootstrap.py` | **Parcial** | Red bloqueada sí; cobertura/CI no forzadas; test tautológico. |
| AC-1.12 | `test_security_project_id.py` | **Parcial** | 12 ids × (publisher + 5 APIs), X11 detectada (19 fallos). No incluye `"abc\n"` (N-06); X10 sobrevive. |
| AC-1.13 | `test_llm_failures.py` | **Parcial** | Solo `TimeoutError` en debate/entrevista. Falla con cualquier otra excepción (N-01). Sin caso de excepción en `MEDIATION`/`WAITING_APPROVAL`. |

### Calidad INV-23 de los tests nuevos
- Oráculos independientes y correctos: tabla de transiciones, avance por estado, `FAILED` conserva, valores de `ProjectStatus`.
- **Circulares/débiles:** `test_plan_compute_hash_sensitivity_and_handwritten_digest` (sin digest; la constante esperada no se usa); `test_suite_offline.py`; tests de `reset` implícitos.
- Tests que fijan comportamiento cuestionable: `test_stale_approval…` (obliga a N-02).

## 5. Mutaciones M1–M11 (git worktree temporal en `%TEMP%\wt-1b`, ya eliminado)

Línea base: 201 passed.

| # | Mutación | Resultado | ¿Detectada? |
|---|---|---|---|
| M1 | `IDLE→BUILDING` permitido en la tabla | 1 failed | Sí |
| M2 | Sin comparar `plan_hash` en `submit_approval` | 2 failed | Sí |
| M3 | Sin tope en `CircuitBreaker.record_call` | 7 failed | Sí |
| M4 | Sin tope de 3 preguntas | 1 failed | Sí |
| M5 | `agent_invocations += 5` | 1 failed | Sí |
| M6 | `compute_hash` constante | 3 failed | Sí |
| **M7** | **Sin chequeo `WAITING_APPROVAL` en `submit_approval`** | **201 passed** | **NO** |
| M8 | Mediator sin validar `template_id` | 1 failed | Sí |
| M9 | `Inbox.is_processed` siempre `False` | 2 failed | Sí |
| M10 | Tope BUILDING 90→80 | 4 failed | Sí |
| M11 | Sin `if approval is None` | 1 failed | Sí |

M7 sobrevive porque `test_submit_approval_rejected_when_project_not_in_waiting_approval` (`test_orchestrator_approval.py:210-232`, citado en `HARDENING_TEST_MAP.md:38`) solo prueba `INTERVIEW`, donde `current_status.plan_hash` es `None` y la rama `if not current_status.plan_hash or approval.plan_hash != current_status.plan_hash:` (`orchestrator.py:356`) lanza `ApprovalError` igual aunque se elimine el chequeo `current_status.state != ProjectState.WAITING_APPROVAL` (`:350`). Si se invocara en `BUILDING` con el `plan_hash` vigente y el chequeo `:350` mutado, `validate_transition(BUILDING, BUILDING)` (`:362`) lanzaría `IllegalTransition` en vez de `ApprovalError`, caso que ningún test ejercita.

Mutaciones adicionales (17 más): **sobreviven** X1 (`compute_hash` ignora `files`), X6a (sin copia defensiva en `append`), X7/X16/X17 (se elimina el `sleep` de backoff en los tres agentes), X9 (proyección sin validar `→FAILED`), X10 (`is_relative_to` eliminado). Detectadas: X2, X3, X4, X5, X6b, X8, X11, X12, X13, X14, X15.

## 6. Invariantes

| INV | Estado | Evidencia |
|---|---|---|
| INV-01 TDD | Cumple (proceso) | Tags `phase-1-tests`, `phase-1.1-tests`; `get_plan` sigue sin test (`orchestrator.py:415-421`). |
| INV-02 Puertos | Parcial | `Clock` sin adaptador real; `domain/events.py` sigue usando `uuid`/`datetime.now`; `orchestrator.py:125` reloj real por defecto. |
| INV-03 Sin red | Cumple | `addopts=--disable-socket`; sin imports de red. CI no verificable. |
| INV-04 Máquina explícita | **Parcial** | N-03, N-04; el resto cumple. |
| INV-05 BUILDING con aprobación | **Cumple** (aplicación) / Parcial (proyección laxa, N-07) | `orchestrator.py:344-362`, `progress.py:103-117`. |
| INV-06 Topes | Parcial | N-02. |
| INV-07 Sin secretos | Cumple | `.env.example` solo nombres; `.env` no versionado. |
| INV-08 Dependencias | Parcial | ADR-003 existe y coincide con `pyproject.toml`; transitivas sin fijar ni lockfile. |
| INV-09 Fallos del LLM | **No cumple** | N-01, N-05; sin pausa por cuota. |
| INV-10 Reporte y alto | Cumple (forma) / reporte inexacto (N-12) | |
| INV-11 Append-only | **Parcial** | N-03, N-07, N-11, F-17. |
| INV-12 Avance por código | Cumple | `progress.py`. |
| INV-13 Inmutabilidad de tests | Cumple | diff vacío; pero ver N-02 (un test fija un comportamiento que debilita INV-17). |
| INV-15 Rondas de CI | No verificable | Sin lógica en Fase 1. |
| INV-16 Idempotencia | Parcial | F-08, N-09. |
| INV-17 Presupuesto | **No cumple** | N-02 y no es punto único ni persistente. |
| INV-14, 18–21 | No aplica a Fase 1 | |
| INV-22 `project_id`/rutas | Parcial | N-06. |
| INV-23 Oráculo independiente | Parcial | N-10. |

## 7. Seguridad
- Secretos: ninguno. Sin ejecución de comandos ni red en `src/`.
- SQL parametrizado (`sqlite_store.py`); sin concatenación.
- Rutas: F-05/N-06 (`"abc\n"`). `FileStatusPublisher` aislado correctamente para `../`, absolutas, vacío, largo.
- Errores que ocultan fallos: `RuntimeError`/`ValueError` del LLM → `FAILED` genérico; mensajes `str(err)` se guardan tal cual en el log (`failure_reason`); sin sanitizar (si un proveedor incluyera credenciales en el mensaje, quedarían persistidas).
- Integridad: la API pública permite corromper el log (N-03, N-04).

## 8. Correcciones mínimas antes de aprobar
1. N-01: capturar la familia completa de errores del puerto LLM (excepción base propia en `ports/llm.py`) y registrar `PROJECT_FAILED` ante cualquier excepción inesperada sin ocultar bugs; test con `ConnectionError`/`OSError`.
2. N-02: no reiniciar contadores; si el ajuste debe permitir nuevos debates, definir un presupuesto por proyecto (no por ejecución) en MASTER_PROMPT/limits y documentar el conflicto con `test_stale_approval…` en `HARDENING_TEST_ISSUES.md`.
3. N-03/N-04: `create_project` debe fallar si existen eventos; `record_state_call` debe validar origen y no emitir `FAILED` si ya está `FAILED`; validar **antes** de persistir.
4. N-06: `fullmatch`/`\Z` + caso `"abc\n"`.
5. N-05: adaptador `Clock` real con `sleep`, `clock` obligatorio o por defecto, tests que cuenten `sleep` para los tres agentes.
6. N-07/N-09/F-17: endurecer proyección y dedupe previo a validación de estado; invalidar caché `_specs`; persistir `objective`.
7. N-10: usar el digest escrito a mano; test de sensibilidad por `files`/`acceptance_tests`/`risks`.
8. N-12: corregir `PHASE_REPORT_1.md` y `DEBT.md`; lockfile para transitivas.

## 9. Lo que NO pude verificar
- **CI de GitHub** (`ci.yml` solo leído; Ubuntu no ejecutado). Solo se ejecutó localmente en Windows (Python 3.12.10).
- **Concurrencia/multiproceso** en SQLite e `Inbox`; solo demostré la divergencia de caché en P5 con dos instancias de `Orchestrator` en el mismo proceso.
- **Comportamiento con un LLM real** (solo `FakeLLM`): los tipos de excepción reales de proveedores o CLI (N-01) y la calidad semántica de los prompts no son medibles sin adaptador real.
- **Evaluación exhaustiva de mutaciones**: se probaron 28 mutaciones dirigidas (M1–M11 + X1–X17); no es mutation testing exhaustivo de todo el AST.
