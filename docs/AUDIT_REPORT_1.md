# AUDIT_REPORT_1 — Auditoría independiente de la Fase 1

Auditor: independiente (no construyó el código). Rama `phase-1`, HEAD `94edeab`, contra tag `phase-1-tests` (`5a8fdc3`). Referencias: `MASTER_PROMPT.md` v1.2, `AGENTS.md`, `docs/PHASE_REPORT_1.md`.

## Veredicto: **NO APTO**

Los 150 tests pasan y la cobertura nominal cumple, pero la suite no protege los invariantes centrales (INV-04, INV-05, INV-17, INV-11). Se demostró con ejecuciones reales (PoC) y mutaciones que:

- un `ApprovalEvent` con `decision="rejected"` abre `BUILDING`;
- un proyecto en `BUILDING` o `FAILED` vuelve a `WAITING_APPROVAL` llamando `run_debate`;
- `validate_transition` no se usa en ningún lugar de `src/` fuera de su propia definición;
- 3 de 11 mutaciones sobrevivieron, y dos de ellas violan INV-04 e INV-05.

`PHASE_REPORT_1.md` dice "Ningún riesgo o deuda técnica bloqueante" (sección 5). Eso es falso a la luz de lo siguiente.

## 1. Resultados reales de ejecución (una vez cada uno)

| Comando | Resultado real |
|---|---|
| `ruff check .` | `All checks passed!` |
| `pytest --cov=src/agent_factory` | `150 passed, 2 warnings`. Cobertura total 89 %. `orchestrator.py` 77 %, `fake_llm.py` 71 %, `structuralist.py` 80 %. |
| `git diff phase-1-tests HEAD -- tests pyproject.toml config` | Vacío. Los tests y la configuración no se tocaron tras el tag. |
| Scan `skip/xfail/mock/monkeypatch/patch` en `tests/` | Sin coincidencias. |

La cobertura no está forzada en ningún sitio: `ci.yml:35` ejecuta `pytest --cov=src/agent_factory` sin `--cov-fail-under`, y `pyproject.toml` tampoco lo define. AC-1.11 exige ≥ 85 % en `domain/` y `agents/`, pero eso solo es un número del reporte.

## 2. Mutaciones (git worktree temporal en `%TEMP%`, ya eliminado)

Línea base en el worktree: 150 passed.

| # | Mutación | Resultado | ¿Detectada? |
|---|---|---|---|
| M1 | Permitir transición ilegal `IDLE→BUILDING` en `states.py` (tabla) | 150 passed | **NO** |
| M2 | Quitar comparación de `plan_hash` (`orchestrator.py:259`) | 1 failed | Sí |
| M3 | Borrar el tope del circuit breaker (`orchestrator.py:84`) | 4 failed | Sí |
| M4 | Quitar el tope de 3 preguntas (`interviewer.py:56`) | 1 failed | Sí |
| M5 | `agent_invocations += 5` en la proyección (`progress.py`) | 150 passed | **NO** |
| M6 | `compute_hash` devuelve constante (`models.py:103`) | 150 passed | **NO** |
| M7 | Quitar el chequeo de estado `WAITING_APPROVAL` en aprobación (`orchestrator.py:255`) | 150 passed | **NO** |
| M8 | Mediator omite validación de `template_id` | 1 failed | Sí |
| M9 | `Inbox.is_processed` siempre `False` | 2 failed | Sí |
| M10 | Tope de avance BUILDING 90→80 | 3 failed | Sí |
| M11 | Quitar `if approval is None` | 1 failed | Sí |

**4 de 11 sobrevivieron** (M1, M5, M6, M7). M1 y M6 son críticas: tabla de transiciones y hash del plan.

## 3. Hallazgos

Los PoC reproducibles están en `brain/.../scratch/poc.py`. Las salidas citadas son reales.

| ID | Sev. | Evidencia | Por qué importa | Corrección sugerida (no aplicada) |
|---|---|---|---|---|
| F-01 | **Crítico** | `orchestrator.py:259-275`: solo se compara `plan_hash`; `approval.decision` se ignora y se persiste tal cual. PoC2: `decision="rejected"` con hash válido → estado `BUILDING`. | INV-05 es el único control humano. Un "rechazo" ejecuta el build. | Exigir `decision == "approved"` (enum cerrado `Literal`). Añadir test con `rejected`. |
| F-02 | **Crítico** | `validate_transition`/`can_transition` solo aparecen en `states.py:57,63,65`. Ningún módulo de `src/` los usa. `orchestrator.py:195-202` escribe `STATE_TRANSITIONED INTERVIEW→DEBATE` sin consultar la tabla. | INV-04 ("transiciones ilegales lanzan error") solo se cumple en la función aislada, no en el sistema. | Toda mutación de estado debe pasar por `validate_transition` (orquestador y proyección). |
| F-03 | **Crítico** | `orchestrator.py:193-246`: `run_debate` no comprueba estado actual. PoC3: en `BUILDING`, `run_debate` → `WAITING_APPROVAL` (BUILDING→DEBATE ilegal). PoC11: proyecto `FAILED` → `run_debate` → `WAITING_APPROVAL` (resurrección). PoC4: sin entrevista y con `Spec()` vacío → `WAITING_APPROVAL`. | Se invalida la aprobación humana tras BUILDING y se resucita `FAILED`. Se debate sobre spec vacío. | Precondición de estado `INTERVIEW` (o `WAITING_APPROVAL` para ajuste) y `spec.is_complete()`. |
| F-04 | **Crítico** | `progress.py:71-72, 84-85, 95-96`: la proyección acepta cualquier evento con `to` y salta a ese estado, sin validar transiciones ni aprobación. Cualquiera que escriba un `STATE_TRANSITIONED→BUILDING` en el store muestra `BUILDING` sin `ApprovalEvent`. | INV-05 ("inalcanzable sin ApprovalEvent") e INV-11 ("nunca muestra un estado que no ocurrió") no se garantizan en el dominio. | La proyección debe validar cada transición contra la tabla y exigir aprobación con hash vigente antes de `BUILDING`. |
| F-05 | **Alto** | `FileStatusPublisher._file_path` (`file_publisher.py:17-18`) concatena `project_id` sin sanear. PoC1: `project_id="../escaped"` escribe `escaped.json` fuera de `base_dir`. `delete()` (línea 32-36) también borra fuera. | Path traversal con dato externo (id vendrá de Telegram/Issues en Fase 3). Permite escribir o borrar archivos arbitrarios `*.json`. | Validar `project_id` con regex `^[A-Za-z0-9_-]{1,64}$` en el modelo; comprobar `resolve().is_relative_to(base)`. |
| F-06 | **Alto** | INV-17: `run_debate` nunca llama a `circuit_breaker.record_call` (`orchestrator.py:193-246`). Solo `handle_interview_message` (línea 144) y `record_state_call` (llamado manualmente por el test) lo usan. PoC7: 9 llamadas LLM en `run_debate` con límites DEBATE=3 y MEDIATION=2; contadores del breaker = 0/0. | El presupuesto duro por estado no protege el pipeline real; los reintentos (`max_retries=2` × 3 agentes) lo multiplican por 3. AC-1.8 prueba el contador, no el cableado. | Llamar al breaker antes de cada invocación LLM en todos los agentes; contar reintentos. |
| F-07 | **Alto** | Excepciones LLM no manejadas: `pragmatist.py`/`structuralist.py` capturan solo `InvalidLLMResponseError, ValueError`. `mediator.py:136-143` captura solo una lista. PoC5: `TimeoutError` → se propaga; el estado queda `DEBATE` sin `PROJECT_FAILED`. | INV-09 (timeouts del LLM con reintento y backoff) e INV-06 (agotar → `FAILED` con reporte). El proyecto queda colgado y sin causa. | Capturar la familia de errores del puerto LLM; sin backoff hoy (`pragmatist.py` reintenta inmediatamente). Registrar `FAILED` en cualquier fallo. |
| F-08 | **Alto** | INV-16: `Inbox` es un `set` en memoria (`inbox.py:12-13`), no ligado al store. PoC9: mismo `event_id` tras "reinicio" (nuevo `Orchestrator`, mismo store) → 2 llamadas LLM. | Telegram reentrega updates tras reinicio; el sistema duplicaría debates y gastaría cuota. | Persistir el id procesado en el registro de eventos (verificación vía store). |
| F-09 | **Alto** | Tests con oráculo circular (AC-1.1). `test_states.py:17-28` genera los pares válidos e ilegales desde `ALLOWED_TRANSITIONS`, la misma tabla bajo prueba. M1 sobrevive. | Un cambio de tabla (válido o ilegal) pasa automáticamente. El test no ancla la tabla de la sección 4. Se escribió la tabla ya en el Paso A (`states.py:30-54`), de modo que el "test primero" no probó nada independiente. | Codificar en el test la tabla esperada de la sección 4 de forma literal y compararla. |
| F-10 | **Alto** | AC-1.6 débil: `test_progress_and_projection.py:15-103` solo compara estado reconstruido contra el producido por la misma función; no asserta `state`, `progress_pct`, `plan_hash`, `tests` ni `agent_invocations`. M5 sobrevive. | Una proyección constante (ignora eventos) pasaría. Detección de regresión nula. | Asserts explícitos sobre campos esperados y un test con `AGENT_INVOKED`, `CI_ROUND_COMPLETED`, `TESTS_UPDATED`. |
| F-11 | **Alto** | AC-1.5 no cubre sensibilidad del hash. M6 (`compute_hash` constante) y M7 (sin chequeo de estado, `orchestrator.py:255-256` sin cobertura) sobreviven. `test_enter_building_with_mismatched_plan_hash_fails` (línea 98) usa un hash de ceros. | Aprobación obsoleta tras re-plan (`WAITING_APPROVAL→DEBATE`) o aprobación en `FAILED` quedarían sin cobertura. | Test de hash distinto para planes distintos y de aprobación en estados distintos de `WAITING_APPROVAL`. |
| F-12 | **Medio** | `mediator.py:120,127,144`: `llm_calls` se calcula con `getattr(llm, "calls", [])`; con un LLM real vale 0. Código del dominio depende de un atributo exclusivo del doble de test. | AC-1.3 "exactamente 3 llamadas" solo es medible con `FakeLLM`; fuga de test double a producción (INV-02). | Contar llamadas con un wrapper/contador del propio pipeline. |
| F-13 | **Medio** | `mediator.py:136-143` captura `RuntimeError, KeyError, TypeError, ValueError` y devuelve `FAILED` con `str(err)`. | Oculta errores de programación como "fallo del proyecto". Un `RuntimeError("FakeLLM response queue is empty")` pasa por fallo legítimo. | Separar fallos esperados (LLM/validación) de bugs; dejar propagar los bugs. |
| F-14 | **Medio** | `models.py:50-65` `fill_gaps_with_assumptions`: rellena campos vacíos con cadenas genéricas `"Standard default for {field}"` y marca `is_complete()=True`. Test `test_interviewer.py:85` solo busca subcadenas `"edge_cases"` etc. | La Spec "completa" contiene relleno sin contenido. El Mediator planifica sobre datos inventados. | Mantener el hueco explícito como supuesto con texto significativo; no convertir checklist en `True` con relleno. |
| F-15 | **Medio** | `orchestrator.py:42-50`: si `limits.toml` no existe, devuelve valores hard-coded y sigue en silencio. PoC8: ruta inexistente → DEBATE=3. La ruta por defecto es relativa al CWD (`orchestrator.py:62`). | Cambiar `config/limits.toml` puede no tener efecto según el directorio de ejecución. Error oculto. Duplica la fuente de verdad. | Fallar con error claro si falta el archivo; resolver la ruta desde la raíz del repo. |
| F-16 | **Medio** | `events.py:28-35`: `Event` no es inmutable (`frozen`). PoC6: `get_events(...)[0].payload["name"]="HACKED"` altera el registro y la proyección. `sqlite_store.py` no impide `UPDATE/DELETE`. | INV-11 "append-only" solo es convención. | `model_config = ConfigDict(frozen=True)`, copias defensivas; triggers SQLite contra UPDATE/DELETE. |
| F-17 | **Medio** | `orchestrator.py:112,131,164-167`: `_specs` es un dict en memoria. `handle_interview_message` parte de `Spec()` si no hay entrada (tras reinicio); solo `run_debate` reconstruye desde el log. | El estado de entrevista no deriva del registro (INV-11) y se pierde en reinicio. | Reconstruir siempre `Spec` desde `SPEC_UPDATED`. |
| F-18 | **Medio** | `orchestrator.py:134-191`: `handle_interview_message` nunca avanza a `DEBATE` ni usa `round_number` (siempre 1) ni `terminated_by_limit`; no hay existencia previa del proyecto (PoC10: mensaje a `"ghost"` agrega eventos y devuelve `IDLE`). | La integración entrevista→debate no existe; AC-1.2 solo cubre `Interviewer` aislado. Operaciones sobre proyectos inexistentes. | Orquestar rondas con tope y transición con `validate_transition`; rechazar `project_id` desconocido. |
| F-19 | **Medio** | `run_debate` omite el estado `MEDIATION`: `orchestrator.py:195-245` va `DEBATE→WAITING_APPROVAL` mediante `PLAN_CREATED.to`. El valor de avance 30 (`progress.py:13`) nunca se emite. | Desvío de la sección 4 (`DEBATE→MEDIATION→WAITING_APPROVAL`) y del presupuesto `MEDIATION`. | Emitir transiciones `DEBATE→MEDIATION→WAITING_APPROVAL` validadas. |
| F-20 | **Medio** | INV-08: se agregó `pydantic==2.14.3` (`pyproject.toml`, commit del tag) sin ADR; `docs/decisions/` solo contiene `ADR-000`. Dependencias transitivas sin fijar ni lockfile. | INV-08 exige ADR para toda dependencia nueva y versión exacta. La sección 5 menciona pydantic, pero no exime de ADR. | ADR-001 de pydantic y lockfile (pip-compile/uv). |
| F-21 | **Medio** | AC-1.11 no se refuerza: `ci.yml:35` sin `--cov-fail-under`; `test_suite_offline.py:12-35` verifica que `FakeLLM` devuelve lo que se le guionó (tautología) más un bloqueo de socket ya probado en `test_bootstrap.py:18`. | La condición de cobertura y "CI verde" no se hacen cumplir. | Agregar `--cov-fail-under=85` por paquete o un chequeo explícito. |
| F-22 | **Bajo** | `progress.py:18`: `CHANGE_REQUEST: 10` no está en la tabla de avance (sec. 5). Sin test. | Valor inventado. | Documentar en ADR o retirar. |
| F-23 | **Bajo** | `progress.py:36`: `round(35+55*0.5)=62` por redondeo bancario; el test `test_progress_and_projection.py:106` fija 62. | Dependencia de un detalle de `round`. | Definir regla de redondeo explícita. |
| F-24 | **Bajo** | `orchestrator.py:269-273`: `ApprovalEvent.timestamp` no se guarda; `at` usa el reloj del orquestador. `models.py` no limita `progress_pct` a 0–100. | Contrato sec. 5 (`ApprovalEvent` con timestamp) incompleto. | Persistir `timestamp`; `Field(ge=0, le=100)`. |
| F-25 | **Bajo** | `states.py:30-54`: se añaden transiciones a `FAILED` desde casi todos los estados y `FAILED` es terminal; la sección 4 solo nombra `BUILDING→FAILED`/`VERIFYING→FAILED`. Sin ADR. | Desviación no documentada. Ver también ausencia de estado de pausa (INV-09). | ADR que justifique las aristas añadidas. |
| F-26 | **Bajo** | `domain/events.py:49,53` usa `uuid.uuid4()` y `datetime.now(UTC)` en `domain/`; `orchestrator.py:115` cae a reloj real si no hay `clock`. | La sección 6 define `domain/` como "puro, sin I/O" y el reloj va tras un puerto (INV-02). | Inyectar `Clock` e id-generator. |
| F-27 | **Bajo** | `interviewer.py:102`: `# type: ignore[return-value]`; `conduct_interview` devuelve `None` si `all_messages` fuera vacío. `get_status` publica como efecto lateral de una lectura (`orchestrator.py:309-311`). | Código frágil; consulta con escritura. | Tipado correcto; publicar solo en comandos. |

## 4. Matriz AC → test → ¿protege realmente?

| AC | Test (archivo:línea) | ¿Protege? | Motivo |
|---|---|---|---|
| AC-1.1 | `test_states.py:32` y `:40` | **Parcial** | Parametrizado sobre las 100 combinaciones, pero deriva los casos de `ALLOWED_TRANSITIONS`. M1 sobrevive (F-09). |
| AC-1.2 | `test_interviewer.py:8, 48, 85` | **Parcial** | Verifica el tope de 3 (M4 detectada) y fin por checklist/tope, pero el relleno de supuestos es genérico (F-14) y no hay integración con el orquestador (F-18). |
| AC-1.3 | `test_debate_mediator.py:84, 106, 127` | **Parcial** | Camino feliz = 3 llamadas. Los reintentos solo se ejercitan en el primer agente (Pragmatist). `structuralist.py:49-53` y `mediator.py:102-106` sin cobertura. La métrica de llamadas depende de `FakeLLM` (F-12). |
| AC-1.4 | `test_debate_mediator.py:149, 168, 187` | **Sí** | M8 detectada. Cubre las tres causas de rechazo. El modelo `Plan` por sí mismo no valida (solo `validate_plan`). |
| AC-1.5 | `test_orchestrator_approval.py:87, 98, 115` | **Parcial** | M2 detectada, pero ignora `decision` (F-01), hash constante (M6) y estado (M7) sobreviven (F-11). |
| AC-1.6 | `test_progress_and_projection.py:15` | **Parcial** | Round-trip real, pero sin asserts de contenido; M5 sobrevive (F-10). |
| AC-1.7 | `test_progress_and_projection.py:106, 132` | **Sí** (con reservas) | M10 detectada; cubre `FAILED`. No cubre `VERIFYING`/`DELIVERED` desde eventos ni `CHANGE_REQUEST`. |
| AC-1.8 | `test_circuit_breaker.py:18, 37` | **Parcial** | M3 detectada, pero el test llama a `record_state_call` directamente; el pipeline real no consulta el breaker (F-06). |
| AC-1.9 | `test_inbox.py:11, 67` | **Parcial** | M9 detectada. Solo en memoria (F-08). |
| AC-1.10 | `test_store_contract.py:26-140` (fixture parametrizada `memory`/`sqlite`) | **Sí** | Una suite para ambos stores. No prueba inmutabilidad ni persistencia entre conexiones de SQLite. |
| AC-1.11 | `test_suite_offline.py:12`, `test_bootstrap.py:18` | **Parcial** | Red bloqueada verificada. Cobertura y CI no forzadas (F-21); test circular. |

## 5. Invariantes

| INV | Estado | Evidencia |
|---|---|---|
| INV-01 TDD | Parcial | Flujo con tag respetado; `Orchestrator.get_plan` (`orchestrator.py:316-319`) y la ruta `FAILED` de `run_debate` (`220-228`) sin test. |
| INV-02 Puertos | Parcial | Puertos existen; `domain/events.py:49,53` y `orchestrator.py:115` usan reloj/uuid reales (F-26); `mediator.py:120` depende de `FakeLLM.calls`. |
| INV-03 Red bloqueada | Cumple | `pyproject.toml` `addopts="--disable-socket"`; `test_bootstrap.py:18`. CI no verificable. |
| INV-04 Máquina explícita | **No cumple** | F-02, F-03. |
| INV-05 BUILDING con aprobación | **No cumple** | F-01, F-04, M7. |
| INV-06 Topes | Parcial | Topes de reintento y de breaker existen; F-06, F-07. |
| INV-07 Sin secretos | Cumple | `.env.example:1-4` solo nombres; sin secretos en `git ls-files`. `.env` ignorado. |
| INV-08 Dependencias | **No cumple** | F-20. |
| INV-09 Fallos del LLM | **No cumple** | F-07 (sin backoff ni manejo de timeout; sin pausa por cuota). |
| INV-10 Reporte y alto | Cumple (forma) | `docs/PHASE_REPORT_1.md` existe, pero su sección de riesgos es incorrecta. |
| INV-11 Registro append-only | **No cumple** | F-04, F-16, F-17. |
| INV-12 Avance por código | Cumple | `calculate_progress` determinista; el LLM no escribe `progress_pct`. Valor extra F-22. |
| INV-13 Inmutabilidad de pruebas | Cumple (proceso) | Diff vacío contra el tag. Pero el oráculo de AC-1.1 es circular (F-09). |
| INV-14 / 18 / 19 / 20 / 21 | No aplica a Fase 1 | — |
| INV-15 Rondas de CI | No verificable | Solo existe `ci_rounds` en `config/limits.toml`; sin lógica en esta fase. |
| INV-16 Idempotencia | **No cumple** (parcial) | Orden correcto (marca antes del LLM), pero sin persistencia (F-08). |
| INV-17 Presupuesto | **No cumple** | F-06, F-15. Contadores solo en memoria (`orchestrator.py:69`). |

## 6. Seguridad

- **Secretos:** ninguno encontrado.
- **Red y ejecución de comandos:** sin `subprocess`, `os.system`, `eval`, `exec`, `socket` ni clientes HTTP en `src/`.
- **SQL:** parametrizado (`sqlite_store.py:43-44, 53-54, 73`). Sin inyección.
- **Path traversal:** F-05, probado.
- **Errores que ocultan fallos:** F-13, F-15, y `FakeLLM` en `fake_llm.py:35` lanza `RuntimeError` de cola vacía que `mediator.py` convierte en `FAILED`.
- **Recursos:** `SqliteEventStore` abre `sqlite3.connect` en el constructor (`sqlite_store.py:20`) y los tests nunca llaman `close()`; sin gestor de contexto.

## 7. Lo que NO pude verificar

- **CI en GitHub:** no hay ejecución remota; `ci.yml` solo se leyó. No sé si Ubuntu/Python 3.12 pasa.
- **Concurrencia / múltiples procesos:** no se probó acceso concurrente a SQLite ni al `Inbox`.
- **Reintentos con un LLM real:** solo `FakeLLM` guionado; los mensajes (prompts) no se evaluaron contra ningún modelo.
- **Cobertura por rama real de `domain/` y `agents/` combinada:** el reporte da cifras por sentencias; con ramas es algo menor. Con los números reales de la corrida sigue por encima del 85 %.
- **Persistencia de SQLite entre procesos/archivos:** no se probó reabrir la BD.
- **Windows vs Linux:** solo se ejecutó en Windows (Python 3.12.10).
- **Las mutaciones** se limitaron a 11 puntos; no es una evaluación exhaustiva.

## 8. Correcciones mínimas antes de aprobar

1. F-01, F-02, F-03, F-04: enforcement real de transiciones y aprobación (con tests que fallen hoy).
2. F-05: sanear `project_id`.
3. F-06, F-07, F-08: cablear el breaker en el pipeline, manejar excepciones del LLM, persistir idempotencia.
4. F-09, F-10, F-11: reforzar los tests (oráculo independiente de la tabla, asserts de contenido, sensibilidad del hash).
5. F-20, F-21: ADR de pydantic y umbral de cobertura en CI.
6. Corregir la sección de riesgos de `PHASE_REPORT_1.md`.
