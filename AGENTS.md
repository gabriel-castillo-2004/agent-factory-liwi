# AGENTS.md

Reglas cortas para cualquier agente de código en este repo. La fuente de verdad es `MASTER_PROMPT.md` (v1.2.1).

1. Lee `MASTER_PROMPT.md` completo antes de escribir código.
2. No modifiques `MASTER_PROMPT.md` ni este archivo.
3. Implementa solo la fase que indique la tarea. Al terminar: `docs/PHASE_REPORT_N.md` y detente.
4. TDD siempre: prueba que falla → código mínimo → prueba verde → refactor.
5. Todo acceso externo (LLM, GitHub, Telegram, shell, reloj) va detrás de un puerto. Tests sin red y sin secretos.
6. Dependencias mínimas y con versión fija. Una nueva requiere ADR en `docs/decisions/`.
7. Sin secretos en el repo, ni siquiera de ejemplo con apariencia real.
8. No hagas preguntas a mitad de fase: elige la opción conservadora y documéntala.
9. Commits en inglés, formato `type(scope): message`.
10. El estado se deriva del registro de eventos y el avance lo calcula el código, nunca tú (INV-11, INV-12).
11. Nunca cambies tests ni su configuración para hacer pasar una corrida (INV-13).
12. No pongas nombres de modelos en el código: van en `config/models.toml`.
13. En proyectos ya entregados trabaja solo en ramas y entra por PR; tus commits llevan `[agent-factory-skip]` (INV-14).
14. Valida toda entrada externa: `project_id` con regex y rutas resueltas dentro de su base (INV-22).
15. En los tests, el resultado esperado se escribe a mano; nunca lo calcules con el código que pruebas (INV-23).
16. Todo cambio de estado pasa por `validate_transition`; todo llamado al LLM pasa por el breaker central (INV-04, INV-17).

Comandos de verificación (deben pasar antes de cerrar una tarea):

```
ruff check .
pytest --cov=src/agent_factory
```
