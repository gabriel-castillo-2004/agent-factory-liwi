# ADR-002: Diseño del puerto `CodeAgent` y separación física entre generación de pruebas e implementación

## Estado
Aceptado

## Fecha
2026-11-16

## Contexto
El documento rector `MASTER_PROMPT.md` (v1.2.1) establece en sus secciones 3 y 7 las reglas para el constructor local (Fase 2):
- **INV-02**: Todo acceso externo (LLM, GitHub, Telegram, disco compartido, shell, reloj, agente de código) vive detrás de un **puerto**.
- **INV-09**: Fallos del agente/LLM (timeout, cuota agotada, proceso colgado) se manejan con reintento acotado y backoff; **agotar cuota pausa el trabajo, no lo marca como fallo del proyecto**.
- **INV-13**: **Inmutabilidad de pruebas.** Los tests se generan al inicio de `BUILDING` a partir de `acceptance_tests`, **sin acceso al código de producción**. Se registra el SHA-256 de `tests/` y de la configuración del runner. Antes de cada corrida un verificador determinista comprueba el hash; si cambió, revierte y cuenta el intento como fallido.
- **INV-17**: Presupuesto duro de llamadas por estado (`BUILDING = 15` invocaciones acumulativas del agente de código por proyecto).
- **INV-18 / INV-22**: El agente trabaja únicamente dentro del workspace aislado `workspaces/<project_id>/`, rechazando cualquier ruta fuera de su directorio base.

Para que la Fase 2a pueda verificar deterministamente el núcleo del constructor en modo 100% offline antes de integrar binarios CLI reales (`AgyCliAdapter` en Fase 2b), es necesario definir el contrato formal del puerto `CodeAgent` en `src/agent_factory/ports/code_agent.py` y su adaptador falso guionado `FakeCodeAgent` en `src/agent_factory/adapters/fake_code_agent.py`.

## Decisión
Se define el puerto `CodeAgent` (`Protocol` verificable en tiempo de ejecución) con dos operaciones explícitamente separadas para hacer cumplir por diseño **INV-13** y el criterio **A2a-5**:

1. **`generate_tests(workspace_dir: Path, acceptance_tests: Sequence[AcceptanceTest]) -> CodeAgentResult`**:
   - Se invoca una sola vez al inicio de `BUILDING` (Paso A).
   - Su firma recibe **exclusivamente** la ruta del workspace y la secuencia de `AcceptanceTest` aprobada en el `Plan`. No recibe `Plan`, ni `FileSpec`, ni código de producción.
   - Antes de invocar este método, el constructor verifica que no existan archivos de código de producción en `src/` dentro del workspace.
   - Una vez ejecutado, el constructor bloquea criptográficamente el directorio `tests/` y los archivos de configuración del runner mediante `compute_test_lock`.

2. **`implement_or_fix(workspace_dir: Path, plan: Plan, failure_output: str = "", attempt: int = 1) -> CodeAgentResult`**:
   - Se invoca en cada iteración del bucle local escribir → probar → corregir (Paso B).
   - Recibe el `Plan` completo y la salida de fallo (`failure_output`) de la corrida previa (sea de pruebas locales, violación de test-lock revertida o fallo de CI).
   - Antes de ejecutar el runner de pruebas tras esta llamada, el constructor ejecuta `verify_and_restore_test_lock`; si el agente modificó, borró o agregó archivos en `tests/` o en la configuración del runner, los cambios se revierten automáticamente y el intento se contabiliza como fallido sin ejecutar el runner ni publicar a VCS.

3. **Jerarquía de excepciones del puerto**:
   - `CodeAgentError(Exception)`: fallo operacional o de ejecución del agente de código; contabiliza o transiciona el proyecto a `FAILED` con causa registrada.
   - `QuotaExhaustedError(CodeAgentError)`: incluye `retry_after_seconds: float`. Señala agotamiento temporal de cuota (INV-09, AC-2.4): el constructor **pausa** el trabajo en `JobState.PAUSED_QUOTA`, duerme mediante `Clock.sleep`, **no consume presupuesto de invocaciones** y **no marca el proyecto como `FAILED`**, permitiendo reanudar exactamente desde el último estado.

4. **Adaptador falso `FakeCodeAgent`**:
   - Acepta pasos guionados independientes para generación de tests (`test_gen_steps`) e implementación (`impl_steps`), donde cada paso puede ser un diccionario `{ruta_relativa: contenido}`, un callable o una excepción (`QuotaExhaustedError`, `CodeAgentError`).
   - Registra cada llamada en `test_gen_calls` (incluyendo los `acceptance_tests` recibidos y la lista de archivos de producción observables en `src/` en el instante de la invocación) e `impl_calls`.

## Consecuencias
- **Positivas:**
  - Cumplimiento estricto por tipos y por contrato de **INV-13** y **A2a-5**: el paso generador de tests es incapaz de recibir el `Plan` o código de producción.
  - Pruebas unitarias y de integración de Fase 2a deterministas, rápidas y 100% offline.
  - Sustitución transparente por `AgyCliAdapter` y `ApiAdapter` en la Fase 2b sin modificar el orquestador ni el constructor `BuildWorker`.
- **Compromisos (Trade-offs):**
  - El constructor debe gestionar explícitamente la transición entre el Paso A (`generate_tests` + captura de `TestLockSnapshot`) y el Paso B (`implement_or_fix` + verificación de `TestLockSnapshot`), persistiendo el snapshot en el estado del trabajo (`BuildJob`) para soportar pausas y reanudaciones por cuota (`AC-2.4`).
