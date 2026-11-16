# ADR-003: Inclusión de Pydantic v2 para contratos de datos tipados y validación determinista

## Estado
Aceptado

## Fecha
2026-11-16

## Contexto
El documento rector `MASTER_PROMPT.md` (v1.2.1) establece en sus secciones 3 y 5:
- **INV-05**: "BUILDING es inalcanzable sin un `ApprovalEvent` con `decision = approved`, `plan_hash` igual al del plan vigente y emitido mientras el proyecto está en `WAITING_APPROVAL`."
- **INV-08**: "Dependencias mínimas y fijadas (versión exacta). Agregar una requiere ADR en `docs/decisions/`."
- **INV-11**: "Registro de eventos append-only como fuente de verdad. `status` es una proyección regenerable desde el registro... Al reproducir, la proyección valida cada transición y rechaza eventos inválidos."
- **INV-22**: "Validación estricta de toda entrada externa: `project_id` con regex `^[a-z0-9][a-z0-9-]{0,62}$` y rutas resueltas dentro de su base."

En la sección 5 del `MASTER_PROMPT.md` se definen formalmente los contratos de datos del sistema (`Spec`, `AcceptanceTest`, `FileSpec`, `Plan`, `ApprovalEvent`, `ProjectStatus`, `BuildReport`, `ProjectPrompt`). Modelar estos contratos mediante diccionarios no tipados o clases planas sin validación expondría al orquestador a datos corruptos, evasiones de compuertas de seguridad y dificultades para serializar y deserializar JSON canónico para el cálculo de `plan_hash`.

## Decisión
Se incorpora `pydantic` en su versión 2 con fijación exacta (`pydantic==2.14.3`) en `pyproject.toml` como dependencia de producción del paquete `agent_factory`.

Se utiliza centralizadamente para:
1. **Contratos de datos del dominio**: Implementación de los modelos de la sección 5 con validación en tiempo de construcción.
2. **Inmutabilidad de eventos (INV-11)**: Configuración `model_config = ConfigDict(frozen=True)` en la clase `Event` para impedir mutaciones en memoria tras la instanciación.
3. **Validación estricta de identificadores (INV-22)**: Validación declarativa de `project_id` mediante `@field_validator` y expresión regular `^[a-z0-9][a-z0-9-]{0,62}$`.
4. **Serialización canónica para hashing (INV-05)**: Método `Plan.compute_hash()` utilizando `model_dump(mode="json")` y serialización canónica con ordenamiento de claves para SHA-256 determinista.
5. **Esquemas estructurados para agentes**: Validación tipada de las salidas del LLM en `Interviewer`, `Pragmatist`, `Structuralist` y `Mediator`.

## Consecuencias
- **Positivas:**
  - Garantiza cumplimiento determinista de INV-05, INV-11 y INV-22.
  - Validación de alto rendimiento implementada en el motor subyacente `pydantic-core`.
  - Excelente ergonomía para tipado estático con `ruff` y herramientas de análisis estático en Python 3.12.
- **Compromisos (Trade-offs):**
  - Se añade `pydantic==2.14.3` y sus dependencias directas (`pydantic-core`, `annotated-types`, `typing-extensions`), debidamente auditadas y fijadas para garantizar reproducibilidad en el entorno virtual.
