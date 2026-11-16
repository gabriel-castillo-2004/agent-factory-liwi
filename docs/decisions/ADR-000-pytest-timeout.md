# ADR-000: Inclusión de pytest-timeout como dependencia de pruebas

## Estado
Aceptado

## Fecha
2026-11-16

## Contexto
El documento rector `MASTER_PROMPT.md` (v1.2) establece en su sección 3 dos invariantes clave:
- **INV-06**: "Todo bucle tiene tope (intentos, rondas, llamadas, tiempo). Al agotarse: FAILED con reporte."
- **INV-08**: "Dependencias mínimas y fijadas (versión exacta). Agregar una requiere ADR en `docs/decisions/`."

En la sección 7 (Fase 0 — Bootstrap), la lista preliminar de dependencias de desarrollo contempla `ruff`, `pytest`, `pytest-cov` y `pytest-socket`.

Sin un mecanismo de temporización estricto a nivel del ejecutor de pruebas (`pytest`), cualquier prueba que entre en un bucle infinito, condición de carrera o bloqueo de E/S mantendría el proceso de pruebas colgado indefinidamente, agotando el tiempo máximo del runner tanto en el entorno local como en CI.

Asimismo, el entorno de ejecución local en la máquina del usuario es Windows (PowerShell), donde el mecanismo predeterminado de interrupción por señales Unix (`signal` / `SIGALRM`) no está soportado por el sistema operativo, haciendo mandatorio el uso de un mecanismo de interrupción basado en temporizadores de hilos (`timeout_method = "thread"`).

## Decisión
Se incorpora `pytest-timeout` en versión exacta (`pytest-timeout==2.4.0`) como la única dependencia de desarrollo adicional fuera de la enumeración preliminar del `MASTER_PROMPT.md`.

Se configura de manera centralizada en `pyproject.toml` bajo `[tool.pytest.ini_options]`:
- `timeout = 30` (límite máximo de 30 segundos por prueba individual).
- `timeout_method = "thread"` (compatibilidad nativa con Windows y multiplataforma con Linux en CI).

## Consecuencias
- **Positivas:**
  - Garantiza el cumplimiento de INV-06 en la suite de pruebas automatizadas al evitar bloqueos indefinidos.
  - Soporte nativo y consistente tanto en Windows local como en Ubuntu (GitHub Actions).
  - Integración sin fricción con `pytest` y `pytest-socket`.
- **Compromisos (Trade-offs):**
  - Se añade un paquete adicional al árbol de dependencias de desarrollo, fijado a su versión exacta (`2.4.0`) para garantizar reproducibilidad y mitigar riesgos en la cadena de suministro.
