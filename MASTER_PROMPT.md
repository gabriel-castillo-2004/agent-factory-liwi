# MASTER_PROMPT — Agent Factory (v1.2.1)

> Documento rector del repo. Los agentes de código **NO lo modifican**.
> Cualquier cambio entra solo como enmienda `INV-xx` aprobada por Gabriel.
> Idioma: documentación y reportes en español; código, identificadores, commits y docstrings en inglés.

---

## 1. Misión

Construir **Agent Factory**: un sistema que, a partir de un mensaje desde el celular, haga este ciclo con la mínima intervención humana:

1. **Entrevistar** al usuario hasta tener una especificación completa.
2. **Debatir** la arquitectura en un pipeline fijo de 3 pasos (Pragmatist → Structuralist → Mediator) que produce un Plan Maestro.
3. **Esperar UNA sola aprobación humana** ("luz verde").
4. **Construir** el proyecto en la **PC del usuario** con un agente de código por línea de comandos, en un workspace aislado y partiendo de una **plantilla base ya probada**.
5. **Verificar**: pruebas locales rápidas en cada intento y **CI en GitHub como juez final** en un entorno limpio; autocorrección con topes.
6. **Desplegar gratis** y avisar: "ya está, ábrelo" + link.

El humano actúa como Product Owner: responde preguntas de negocio y aprueba el plan. Nada más.

## 2. Contexto y decisiones de diseño (ya tomadas)

- **Proyecto independiente del "Personal Agent OS".** Bot de Telegram propio, sin estados `CHAT`/`CONSULTING`. Más adelante podrá conectarse como despachador externo (fuera de v1).
- **Local-first.** El "músculo" (orquestador, estado, agente de código) corre en la PC del usuario. GitHub aporta CI, registro de proyectos, panel y hosting estático.
  - *Por qué:* la cuota del plan personal del usuario solo es de uso legítimo en ejecución local e individual (INV-19); además elimina hosting con cold starts, base de datos externa y webhooks.
  - *Costo aceptado:* si la PC está apagada, los trabajos esperan.
- Un solo usuario (Gabriel). Móvil primero. Conversación en español.
- Costo de infraestructura **$0**.
- v1 genera **apps estáticas** sobre una plantilla base. Backends propios quedan fuera.
- **Nombres de modelos nunca van en el código**: se leen de `config/models.toml` por estado (uno fuerte para `MEDIATION`, uno rápido para el resto).

## 3. Invariantes (no negociables)

| ID | Regla | Por qué |
|----|-------|---------|
| INV-01 | **TDD**: primero la prueba que falla, luego el código. Ningún módulo sin tests. | Única forma de que el bucle autónomo sea verificable. |
| INV-02 | Todo acceso externo (LLM, GitHub, Telegram, disco compartido, shell, reloj) vive detrás de un **puerto**. El dominio no importa SDKs. | Tests deterministas y cambio de proveedor sin reescribir. |
| INV-03 | Los tests de la suite del orquestador corren **sin red y sin secretos** (red bloqueada). | Reproducibilidad y seguridad. |
| INV-04 | La **máquina de estados es explícita** y tiene un **único punto de paso**: todo cambio de estado, incluido el que se aplica al reproducir eventos, pasa por `validate_transition`. Transiciones ilegales lanzan error. Además, cada operación del orquestador valida su **estado de origen** (p. ej. el debate solo corre desde `INTERVIEW` o `DEBATE`). | Un chat libre pierde el rumbo, y una tabla que nadie consulta no protege nada. |
| INV-05 | `BUILDING` es **inalcanzable** sin un `ApprovalEvent` con `decision = approved`, `plan_hash` igual al del plan vigente y emitido mientras el proyecto está en `WAITING_APPROVAL`. Un rechazo o ajuste nunca abre `BUILDING`. | Garantiza el único punto de control humano. |
| INV-06 | Todo bucle tiene **tope** (intentos, rondas, llamadas, tiempo). Al agotarse: `FAILED` con reporte. | Cuotas finitas, costo cero, sin bucles infinitos. |
| INV-07 | Sin secretos en el repo. `.env` ignorado; solo `.env.example`. Tokens con permisos mínimos. | El agente escribe código solo; el radio de daño debe ser pequeño. |
| INV-08 | Dependencias **mínimas y fijadas** (versión exacta). Agregar una requiere ADR en `docs/decisions/`. | Menos superficie de ataque y de fallos. |
| INV-09 | Fallos del LLM (JSON inválido, timeout, cuota agotada, proceso colgado) se manejan con **reintento acotado y backoff**; agotar cuota **pausa** el trabajo, no lo marca como fallo del proyecto. Toda excepción o timeout del LLM deja un **evento de fallo** y un estado reintentable o `FAILED`; nunca un proyecto atascado. | Los modelos gratuitos o de plan son inestables por naturaleza. |
| INV-10 | Cada fase termina con `docs/PHASE_REPORT_N.md` y **se detiene**. No se empieza otra sin instrucción explícita. | Control de alcance y revisión humana por fase. |
| INV-11 | **Registro de eventos append-only** como fuente de verdad. `status` es una **proyección** regenerable desde el registro; nunca muestra un estado que no ocurrió. Al reproducir, la proyección **valida cada transición y rechaza eventos inválidos**; no confía en el estado destino que declara el evento. | Evita estados huérfanos si algo falla a mitad de una transición. |
| INV-12 | El **porcentaje de avance lo calcula código determinista** (tabla de la sección 5); el LLM tiene prohibido escribirlo. | Un modelo que se autoevalúa sobreestima el progreso. |
| INV-13 | **Inmutabilidad de pruebas.** Los tests se generan al inicio de `BUILDING` a partir de `acceptance_tests`, sin acceso al código de producción. Se registra el SHA-256 de `tests/` **y** de la configuración del runner (config de pruebas y scripts que las ejecutan). Antes de **cada** corrida un script determinista verifica el hash; si cambió: revierte y cuenta el intento como fallido. Mínimo un test por `acceptance_test`. Incluye un e2e en navegador headless que falla ante cualquier `console.error` o `pageerror`. | Impide "arreglar" tests en vez de corregir el bug, y detecta pantallas en blanco que las pruebas unitarias no ven. |
| INV-14 | Los cambios a un proyecto **ya entregado** van en ramas `cr/<id>`, pasan CI y entran por PR con **aprobación humana del merge**. Los commits del agente llevan la marca `[agent-factory-skip]` y **no re-disparan** al agente. | Evita bucles y que el agente pise cambios manuales. |
| INV-15 | **Iteración local, empuje mínimo.** El bucle escribir → probar → corregir ocurre en el disco local. Se hace push solo cuando las pruebas locales pasan (o se agota el tope). Máximo **3 rondas de CI** por proyecto. | Ahorra minutos de CI y mantiene limpio el historial. |
| INV-16 | **Idempotencia de entrada.** Todo evento entrante (ej. `update_id` de Telegram) se registra como procesado de forma **persistente** (sobrevive a reinicios) **antes** de invocar cualquier LLM; si ya existe, se ignora. | Evita debates o builds duplicados por reintentos. |
| INV-17 | **Presupuesto duro de llamadas por proyecto** (circuit breaker) por estado, configurable en `config/limits.toml`. Valores iniciales: `INTERVIEW` 6, `DEBATE` 3, `MEDIATION` 2, `BUILDING` 15 invocaciones del agente de código. Superarlo → `FAILED` inmediato. El breaker se aplica en un **único punto central** que envuelve toda llamada al puerto LLM, de modo que ningún agente pueda saltárselo. | Protege la cuota del plan. |
| INV-18 | **Sandbox y mínimo privilegio** para el agente de código local: trabaja solo dentro de `workspaces/<project_id>/` (idealmente en contenedor o WSL), sin acceso al resto del disco; token de GitHub limitado a los repos generados (idealmente una cuenta u organización dedicada); sin modos "autónomos sin confirmación" fuera del sandbox. | Un agente con shell en la PC del usuario es el mayor riesgo nuevo. |
| INV-19 | **Términos del proveedor.** El adaptador `agy` solo se ejecuta en la máquina del usuario, con el binario oficial sin modificar, iniciado por el sistema tras una aprobación humana; **nunca en CI** ni con tokens personales guardados como secretos en la nube. Si algún día corre en la nube, usará el adaptador de API. | El plan personal está pensado para uso individual guiado por una persona. |
| INV-20 | **Plantilla base obligatoria** para apps estáticas: el agente parte de un esqueleto pre-probado y solo escribe lógica, vistas y tests; no inventa la configuración de build desde cero. | La mayoría de fallos del bucle viene de configuración, no de lógica. |
| INV-21 | **El estado de los proyectos vive fuera de sus repos.** Un archivo por proyecto en la rama `registry` de `agent-factory`; nada de `status.json` en la rama principal de los repos generados. | Evita conflictos de merge con commits manuales. |
| INV-22 | **Toda entrada externa se valida** antes de usarse. `project_id` debe cumplir `^[a-z0-9][a-z0-9-]{0,62}$` y toda ruta derivada se resuelve y debe quedar dentro de su directorio base (sin `..`, rutas absolutas ni enlaces que escapen). | Evita path traversal y escrituras fuera del sandbox. |
| INV-23 | **Pruebas con oráculo independiente.** El resultado esperado de un test nunca se calcula con el código ni con la tabla que se prueba: va escrito a mano en el test. Cada criterio incluye casos negativos. | Un test que reutiliza la lógica que verifica no detecta nada. |

## 4. Máquina de estados

```
IDLE → INTERVIEW → DEBATE → MEDIATION → WAITING_APPROVAL
WAITING_APPROVAL → INTERVIEW | DEBATE (ajuste) | BUILDING (aprobación válida)
BUILDING → VERIFYING → BUILDING (autocorrección, con tope) | DELIVERED | FAILED
DELIVERED → CHANGE_REQUEST → INTERVIEW (alcance acotado) → … → BUILDING (en rama) → VERIFYING → DELIVERED
```

- **Debate = pipeline fijo de 3 llamadas**: (1) Pragmatist propone la ruta más simple; (2) Structuralist lee la propuesta, la critica y propone una más robusta; (3) Mediator genera el `Plan` en JSON validado. Sin rondas abiertas.
- La tabla de transiciones vive en un solo lugar (`domain/states.py`). Todo lo demás lanza `IllegalTransition`.
- `CHANGE_REQUEST` se declara desde la Fase 1; su lógica llega en la Fase 5.

## 5. Contratos de datos (pydantic v2)

- `Spec`: objetivo, usuarios, funcionalidades, reglas de negocio, casos borde, restricciones, criterios de éxito. Con **checklist de completitud determinista**.
- `AcceptanceTest`: `id`, `description`, `type` (`unit | integration | e2e`).
- `FileSpec`: `path`, `purpose`, `contract` (qué expone y qué garantiza), `depends_on`.
- `Plan` (salida del Mediator), validado:
  - `summary`, `rationale` (por qué este camino y no el descartado)
  - `template_id` (debe existir en el catálogo de plantillas)
  - `files`: lista de `FileSpec` (contratos por archivo, no narrativa)
  - `acceptance_tests`: **no vacía**
  - `risks`, `deployment_target`
- `ApprovalEvent`: `plan_hash`, timestamp, decisión. Vale solo para ese hash.
- `Event`: registro append-only (`id`, `project_id`, `type`, `payload`, `at`).
- `ProjectStatus` (proyección, INV-11): `project_id`, `name`, `objective`, `state`, `phase`, `progress_pct`, `plan_hash`, `tests` (`total`, `passed`, `failed`), `agent_invocations`, `ci_rounds`, `links` (`repo`, `deploy`), `last_event`, `updated_at`.
- `BuildReport`: invocaciones, resultado de cada prueba, rondas de CI, causa de fallo.
- `ProjectPrompt`: prompt maestro propio de cada proyecto derivado del `Plan`; se guarda como `PROJECT_PROMPT.md` en su repo. **Documento vivo, no fuente de verdad: la fuente de verdad es el código real.**
- **Tabla de avance** (INV-12): `INTERVIEW` 10 · `DEBATE` 25 · `MEDIATION` 30 · `WAITING_APPROVAL` 35 · `BUILDING` de 35 a 90 proporcional a `acceptance_tests` pasadas · `VERIFYING` 95 · `DELIVERED` 100 · `FAILED` conserva el último valor. Indicador aproximado, no medida exacta.

## 6. Dónde vive cada cosa

| En la PC del usuario (worker) | En GitHub |
|---|---|
| Orquestador y máquina de estados | Repo `agent-factory` (código y CI) |
| Registro de eventos (SQLite local) | Rama `registry` con un JSON por proyecto |
| Telegram por *long polling* (saliente, sin puertos abiertos) | Panel PWA de solo lectura en GitHub Pages |
| Agente de código por CLI en `workspaces/<id>/` | Repos generados (uno por proyecto) |
| Pruebas locales rápidas | CI = juez final en entorno limpio |
| | Hosting estático de las apps generadas |

**Repo `agent-factory`:**

```
agent-factory/
├── AGENTS.md
├── MASTER_PROMPT.md           # solo lectura para agentes
├── pyproject.toml
├── .env.example
├── config/                    # models.toml, limits.toml
├── .github/workflows/         # ci.yml (ruff + pytest + cobertura)
├── src/agent_factory/
│   ├── domain/                # states.py, models.py, events.py, progress.py (puro, sin I/O)
│   ├── ports/                 # llm.py, store.py, vcs.py, runner.py, notifier.py, publisher.py, clock.py
│   ├── adapters/              # fake_llm.py, memory_store.py, sqlite_store.py, file_publisher.py, ...
│   ├── agents/                # interviewer.py, pragmatist.py, structuralist.py, mediator.py
│   ├── inbox.py               # idempotencia de entrada (INV-16)
│   └── orchestrator.py
├── templates/                 # plantillas base de apps (Fase 2)
├── tests/
└── docs/
    ├── decisions/             # ADRs
    └── PHASE_REPORT_N.md
```

## 7. Fases

Cada fase = **una tarea = un PR**. Al terminar: reporte y alto (INV-10).

### Fase 0 — Bootstrap
- `pyproject.toml` (Python ≥3.11), `ruff`, `pytest`, `pytest-cov`, `pytest-socket`, estructura, `ci.yml`, `.gitignore`, `.env.example`, `config/`.
- **Criterio:** CI verde con un test trivial; la red está bloqueada en la suite.

### Fase 1 — Dominio, pipeline y registro de eventos (todo offline)
1. `domain/states.py`: estados, tabla de transiciones, `IllegalTransition`.
2. `ports/llm.py` (`LLM.complete(messages, schema) -> dict`) y `fake_llm.py` con respuestas **guionadas** por prueba.
3. `agents/interviewer.py`: rondas de **máximo 3 preguntas**; termina por checklist de `Spec` completo o por tope, marcando los huecos como supuestos explícitos.
4. `agents/pragmatist.py`, `structuralist.py`, `mediator.py`: pipeline fijo de 3 llamadas. JSON inválido → hasta 2 reintentos → `FAILED` con causa. El Mediator **rechaza** un plan con `acceptance_tests` vacío, archivos sin `contract` o `template_id` inexistente.
5. `ports/store.py` + `memory_store.py` + `sqlite_store.py`: **registro de eventos append-only**; ambos adaptadores pasan la misma suite de contrato.
6. `domain/progress.py` y `ProjectStatus` como **proyección** del registro (INV-11, INV-12), publicada mediante el puerto `publisher.py` (adaptador de archivos local en esta fase).
7. `inbox.py`: idempotencia por id de evento (INV-16).
8. Circuit breaker de llamadas por estado (INV-17), leyendo `config/limits.toml`.
9. `orchestrator.py`: conduce los estados, compuerta de aprobación (INV-05).
10. **Criterios de aceptación**
    - AC-1.1 La matriz de transiciones esperada (estado × estado) está **escrita a mano en el test**, sin importar la tabla del código. Toda transición válida pasa y toda ilegal lanza `IllegalTransition`. Además, cada método del orquestador rechaza ser invocado desde un estado de origen inválido.
    - AC-1.2 Interviewer respeta el tope de 3 preguntas por ronda y termina por checklist o por tope.
    - AC-1.3 El debate hace **exactamente 3 llamadas** al LLM en el camino feliz; JSON inválido ejercita reintentos y termina en `FAILED` con causa.
    - AC-1.4 El Mediator rechaza planes sin `acceptance_tests`, con archivos sin `contract` o con `template_id` inexistente.
    - AC-1.5 Entrar a `BUILDING` falla si no hay aprobación, si el `plan_hash` difiere, si la decisión no es `approved` o si el proyecto no estaba en `WAITING_APPROVAL`.
    - AC-1.6 `status` se reconstruye idéntico a partir del registro de eventos (borrar la proyección y regenerarla) y su contenido (estado, avance, hash) coincide con valores esperados escritos a mano. Un evento con transición inválida se rechaza al reproducir.
    - AC-1.7 El avance sigue la tabla de la sección 5 para cada estado y `FAILED` conserva el último valor.
    - AC-1.8 Superar el presupuesto de llamadas de un estado lleva a `FAILED`, incluido el debate: ninguna secuencia de reintentos puede exceder el tope.
    - AC-1.9 El mismo id de evento procesado dos veces genera **cero** llamadas LLM la segunda vez, incluso tras reabrir el store (simula un reinicio).
    - AC-1.10 Ambos stores pasan la misma suite de contrato.
    - AC-1.11 La suite corre sin red; cobertura ≥ 85 % en `domain/` y `agents/`; `ruff` limpio; CI verde.
    - AC-1.12 Un `project_id` inválido (`../x`, ruta absoluta, vacío, demasiado largo) se rechaza en toda API pública y en `file_publisher`; ninguna escritura sale del directorio base.
    - AC-1.13 Una excepción o timeout del LLM en cualquier estado produce un evento de fallo y un estado reintentable o `FAILED`; el proyecto nunca queda atascado.

### Fase 2 — Constructor local (el corazón del proyecto)
- **Plantilla base** en `templates/static-app/` con build, linter, pruebas unitarias, e2e en navegador headless, workflow de CI y de despliegue. Stack decidido en `ADR-001` (preferencia: Vite + TypeScript sin framework + Vitest + Playwright).
- **Worker local**: toma trabajos aprobados de la cola local, crea `workspaces/<id>/` aislado (INV-18), clona la plantilla.
- **Adaptador `AgyCliAdapter`** (INV-19): usa el modo no interactivo documentado con salida estructurada, tiene *timeouts*, detecta y mata procesos colgados, y trata errores de cuota como señal de pausa con backoff (INV-09). Adaptador `ApiAdapter` como segunda opción, desactivado por defecto.
- **Paso A:** generador de tests a partir de `acceptance_tests` + registro de hashes (INV-13).
- **Paso B:** bucle local escribir → probar → corregir con verificación de hash antes de cada corrida, topes de INV-17.
- **Juez final:** push a una rama de trabajo, esperar el resultado de CI por el puerto `vcs.py`, leer fallos y, si hace falta, otra ronda (máx. 3, INV-15). Si pasa: merge y despliegue.
- Todo lo anterior se desarrolla y se prueba con **adaptadores falsos**. La prueba real contra `agy` es un test marcado `manual`, omitido por defecto, que documenta el procedimiento.
- **Criterios:**
  - AC-2.1 Con un escenario guionado, el bucle converge y produce un `BuildReport` correcto.
  - AC-2.2 Si el agente falso modifica un test o su configuración, se detecta, se revierte y el intento cuenta como fallido.
  - AC-2.3 Agotar el tope de invocaciones o de rondas de CI lleva a `FAILED` con reporte.
  - AC-2.4 Una cuota agotada pausa el trabajo y lo reanuda sin perder estado.
  - AC-2.5 El e2e de la plantilla falla ante un `console.error`.

### Fase 3 — Interfaz, panel y despliegue
- **Telegram por *long polling*** desde la PC; aprobación con botones (Aprobar / Ajustar) sobre el resumen del plan; comando `/estado`.
- **Publicador** hacia la rama `registry` (un archivo por proyecto, INV-21); mejor esfuerzo e idempotente: si falla, la verdad sigue siendo el registro local.
- **Panel PWA de solo lectura** en GitHub Pages: lista de proyectos con estado, % de avance, fase, objetivo y links; detalle con `PROJECT_PROMPT.md` y último `BuildReport`. Lee de la rama `registry`. Nunca guarda tokens con permiso de escritura en el navegador.
- Despliegue de la app generada a hosting estático gratuito y aviso con link.
- **Criterio:** de un mensaje de Telegram a un link funcional con una sola aprobación, y el panel refleja cada cambio de estado con el avance correcto.

### Fase 4 — Observabilidad (sin auto-modificación)
- Costo y cuota por proyecto, historial de eventos e intentos en el panel.
- **Queda fuera de alcance** que el sistema modifique su propio orquestador.

### Fase 5 — Mantenimiento de proyectos entregados (post-v1)
- **"Chat por repo" sin backend:** las peticiones de cambio entran por Telegram (`/cambio <proyecto> <texto>`) o por un Issue de GitHub con etiqueta `change-request` que el worker local consulta. El panel sigue siendo de solo lectura.
- **Plan de cambio basado en el código real** (mapa del repo actual), **no** en `PROJECT_PROMPT.md`, para evitar *spec drift*. El `PROJECT_PROMPT.md` se actualiza en cada PR de cambio.
- **Flujo:** mini-entrevista de alcance → plan delta → una aprobación ligera → build en rama `cr/<id>` → CI → URL de preview → **merge solo por orden del usuario** (INV-14).
- **Cambios manuales del usuario:** una Action en cada push corre la suite, actualiza el registro y **solo avisa** si algo se rompe, ofreciendo abrir un PR de arreglo. **Nunca** repara por su cuenta.
- **Criterio:** desde Telegram, pedir un cambio pequeño a un proyecto entregado produce un PR con tests verdes y preview, sin tocar `main` directamente.

## 8. Reglas de trabajo para el agente de código

1. Implementa **únicamente la fase indicada** en la tarea.
2. No hagas preguntas a mitad de fase. Ante ambigüedad, elige la opción más conservadora y documéntala en `docs/decisions/ADR-NNN.md`.
3. Ciclo por cada unidad: prueba que falla → implementación mínima → prueba verde → refactor.
4. No agregues dependencias fuera de las listadas sin ADR (INV-08).
5. No toques `MASTER_PROMPT.md` ni `AGENTS.md`.
6. Commits pequeños, en inglés, formato `type(scope): message`.
7. Nunca escribas credenciales, ni siquiera de ejemplo con apariencia real.
8. El estado se deriva del registro de eventos; el avance lo calcula el código, nunca tú (INV-11, INV-12).
9. No cambies tests ni su configuración para hacer pasar una corrida (INV-13).

## 9. Definición de terminado por fase

`docs/PHASE_REPORT_N.md` con:
- Archivos creados/modificados (lista).
- Resultado de la suite (pasadas/falladas/omitidas) y cobertura.
- Criterios AC cumplidos y **cuáles no**, con la razón.
- Decisiones tomadas (enlaces a ADRs) y supuestos.
- Riesgos o deudas detectadas.

Después del reporte, **detente**.
