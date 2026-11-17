# AUDIT_REPORT_2A_1 — Re-auditoría independiente de cierre tras endurecimiento de la Fase 2a

Auditor independiente (en modo solo lectura). Rama `phase-2a`, HEAD `5172a8d` comparada contra el tag de pruebas de endurecimiento `phase-2a.1-tests` (`48ff6f5`) y contra `main` (`3528e5a`). Referencias: `MASTER_PROMPT.md` (v1.2.1), `AGENTS.md`, `docs/decisions/ADR-002-code-agent-port.md`, `docs/PHASE_REPORT_2A.md`, `docs/AUDIT_REPORT_2A.md`, `docs/PHASE2A_TEST_MAP.md`.

---

## Veredicto: **APTO** (con 2 observaciones menores no bloqueantes en F2A-08 y F2A-09)

### Resumen ejecutivo del veredicto
- **Todos los hallazgos bloqueantes (Críticos y Altos: F2A-01 a F2A-05) y los hallazgos Medios/Bajos F2A-06, F2A-07 y F2A-10 están CERRADOS** con evidencia en código y pruebas deterministas que cumplen `INV-23`.
- **Inmutabilidad de pruebas (`INV-13`) respetada al 100 %:** `git diff phase-2a.1-tests --stat -- tests/ pyproject.toml config/` es **idénticamente vacío**.
- **Suite y linter en verde:** `ruff check .` pasa con `0` errores; `pytest -q --cov=src/agent_factory --cov-report=term-missing` ejecuta **268 tests pasados, 0 fallados** en `~7.1 s` con **90 % de cobertura global** (`builder.py` subió de 82 % a **88 %**, `workspace.py` de 91 % a **94 %**, `progress.py` a **96 %**).
- **Mutaciones:** De las 6 mutaciones que habían sobrevivido en `AUDIT_REPORT_2A.md` (`M8`, `M9a`, `M9b`, `M13`, `M14`, `M15`), **5 ahora mueren (`M8`, `M9b`, `M13`, `M14`, `M15` — KILLED)**. Además, se ejecutaron 6 mutaciones nuevas (`M16` a `M21`) sobre las correcciones de endurecimiento: **5 mueren (`M16`, `M17`, `M18`, `M19`, `M20` — KILLED)**.
- **Las 2 únicas supervivientes (`M9a` y `M21`, severidad baja, no bloqueantes):**
  1. **M9a (F2A-08):** En `builder.py:146-163` se mantuvieron ambas guardas anti-contaminación dentro de `if job.lock_snapshot is None:` (la guarda sobre el dict `template_files` en `:146-152` y la guarda en disco `ws_dir / "src"` en `:159-163`). `M9b` (quitar la guarda en disco) **muere** gracias al nuevo caso 1b de `test_test_lock_and_generation.py:319-339`, pero `M9a` (quitar solo el pre-chequeo de `template_files`) sobrevive porque `create_workspace` escribe el archivo y `:159-163` lanza la misma `SandboxViolationError` 4 líneas después, y el test en `:312-317` no comprueba que el archivo no haya llegado a tocar el disco.
  2. **M21 (F2A-09):** `SubprocessRunner.run` (`subprocess_runner.py:25-26`) ahora valida `if isinstance(command, (str, bytes)) or not command: raise ValueError(...)`, pero la línea `26` figura en `Missing` porque en el Paso A' no se añadió un test unitario en `tests/test_runner_timeout.py` que llame a `SubprocessRunner().run("", ...)` (y en el Paso B' estaba prohibido tocar `tests/` por `INV-13`).

---

## 1. Resultados reales de ejecución (una vez cada uno)

| Comando | Resultado real |
|---|---|
| `ruff check .` | `All checks passed!` (exit code `0`) |
| `pytest -q --cov=src/agent_factory --cov-report=term-missing` | `268 passed, 2 warnings in 7.11s` (exit code `0`) · Cobertura total: **90 %** (`1379` stmts, `108` miss, `324` branch, `53` brpart). Desglose: `builder.py` **88 %**, `workspace.py` **94 %**, `progress.py` **96 %**, `orchestrator.py` **90 %**, `fake_vcs.py` **87 %**, `fake_code_agent.py` **77 %**, `fake_runner.py` **74 %**, `subprocess_runner.py` **57 %**. |
| `git diff phase-2a.1-tests --stat -- tests/ pyproject.toml` | Vacío (`0` archivos modificados en `tests/` o `pyproject.toml`). |
| `git diff main...HEAD --stat` | 18 archivos creados/modificados (`3227` inserciones, `1` borrado). |

### Verificación de `docs/PHASE_REPORT_2A.md` frente a la ejecución real
- `PHASE_REPORT_2A.md` reporta `268` tests pasados, `0` fallados, `2` advertencias (`pytest-socket`), cobertura global del `90 %` (`builder.py`: `88 %`, `workspace.py`: `94 %`, `progress.py`: `96 %`, `orchestrator.py`: `90 %`, `ports/`: `85 %`, `domain/`: `98 %`).
- Todas las cifras coinciden con la corrida real verificada en esta auditoría.

---

## 2. Tabla de cierre de hallazgos de `AUDIT_REPORT_2A.md` (F2A-01 a F2A-10)

| ID | Severidad original | Estado | Evidencia (`archivo:línea`) | Verificación detallada |
|---|---|---|---|---|
| **F2A-01** | **Crítico** | **CERRADO** | `src/agent_factory/builder.py:144-164`, `:266-269`; `tests/test_builder_loop.py:596-673` | Las comprobaciones anti-contaminación de `src/` ahora están estrictamente dentro de `if job.lock_snapshot is None:` (`:144`). En la reanudación (`else:` en `:266-269`), solo se asegura la existencia del directorio sin re-ejecutar el chequeo de `src/` ni sobreescribir `template_files`. Probado por `test_quota_resume_succeeds_after_prior_attempt_wrote_production_files_in_src` (intento 1 escribe `src/deck.ts` y falla AT-02 -> intento 2 pausa por cuota -> reanudación corrige `src/deck.ts` y entrega). Mutación **M16** detectada (**KILLED**). |
| **F2A-02** | **Crítico** | **CERRADO** | `src/agent_factory/builder.py:367-390`, `:440-463`, `:479-500`; `tests/test_builder_loop.py:675-739` | `runner.run` está envuelto en `try ... except (CommandTimeoutError, OSError, RuntimeError, ValueError) as err:` (`:367-390`) y las llamadas a `vcs.push_branch`, `vcs.get_ci_result` y `vcs.merge_and_deploy` están envueltas en `try ... except (OSError, RuntimeError, ValueError) as err:` (`:440-463`, `:479-500`). En todos los casos registra `EventType.PROJECT_FAILED`, marca `JobState.FAILED` y devuelve `BuildReport` con `failure_cause`. Probado por `test_runner_timeout_and_vcs_errors_transition_project_to_failed_with_report`. Mutación **M17** detectada (**KILLED**). |
| **F2A-03** | **Alto** | **CERRADO** | `src/agent_factory/builder.py:166-227`; `tests/test_builder_loop.py:741-829` | En el Paso A (`generate_tests`), se consulta `circuit_breaker.get_calls` (`:169`) antes de invocar al agente (`:188`), pero `record_state_call` solo se invoca tras éxito (`:224`) o ante `CodeAgentError` (`:203`). Si ocurre `QuotaExhaustedError` (`:189-200`), pausa en `PAUSED_QUOTA`, duerme `1.5s` y deja `get_calls == 0` sin emitir `AGENT_INVOKED`. Probado por `test_test_generation_quota_pause_and_code_agent_error_handling`. Mutación **M8** (antes superviviente) ahora **KILLED**. |
| **F2A-04** | **Alto** | **CERRADO** | `src/agent_factory/builder.py:278-346`; `tests/test_builder_loop.py:831-889` | En el Paso B, el límite se verifica contra el `CircuitBreaker` central acumulativo (`calls = self.orchestrator.circuit_breaker.get_calls(project_id, ProjectState.BUILDING)`, `:280-283`) **antes** de llamar a `implement_or_fix` (`:301`). Además, `CodeAgentError` en `implement_or_fix` (`:319-339`) registra la llamada en el breaker y emite `AGENT_INVOKED` antes de emitir `PROJECT_FAILED`. Probado por `test_building_budget_is_cumulative_with_breaker_and_records_failed_agent_calls`. Mutación **M13** (antes superviviente) ahora **KILLED**. |
| **F2A-05** | **Alto** | **CERRADO** | `src/agent_factory/builder.py:407-418`; `tests/test_builder_loop.py:891-955` | `local_tests_passed` exige `runner_result.exit_code == 0`, que todos los `at.id` de `job.plan.acceptance_tests` estén en `runner_result.test_results` con valor `True` (`has_all_plan_tests`) y que ningún test reportado sea `False` (`all_reported_tests_passed`). Probado en 5 intentos por `test_local_tests_passed_requires_zero_exit_code_and_all_plan_acceptance_tests`. Mutaciones **M14** (antes superviviente) y **M18** ahora **KILLED**. |
| **F2A-06** | **Medio** | **CERRADO** | `src/agent_factory/workspace.py:11-19`; `tests/test_test_lock_and_generation.py:363-415` | `RUNNER_CONFIG_FILENAMES` incluye `"conftest.py"` y `"setup.cfg"` (`:17-18`). Probado en 4 vectores (inyección de `conftest.py` intruso, modificación/borrado de `setup.cfg` y modificación/borrado de `conftest.py` inicial) en `test_test_lock_protects_root_conftest_and_setup_cfg_from_modification_deletion_or_injection`. Mutación **M19** detectada (**KILLED**). |
| **F2A-07** | **Medio** | **CERRADO** | `src/agent_factory/domain/progress.py:166-170`; `tests/test_builder_loop.py:957-1009` | `project_status_from_events` extrae `repo_url` y `deploy_url` en `EventType.PROJECT_DELIVERED` y construye `ProjectLinks(repo=..., deploy=...)`. Probado tanto desde `orchestrator.get_status` como re-proyectando desde `store.get_events` en `test_project_delivered_projects_repo_and_deploy_links_in_status`. Mutaciones **M15** (antes superviviente) y **M20** ahora **KILLED**. |
| **F2A-08** | **Medio** | **PARCIAL (No bloqueante)** | `src/agent_factory/builder.py:146-163`; `tests/test_test_lock_and_generation.py:296-340` | Se añadió el Caso 1b (`:319-339`) con archivo preexistente en disco `ws_dir / "src" / "converter.ts"` sin `template_files`, logrando 100 % de cobertura en `:159-163` y matando la mutación **M9b** (antes superviviente). No obstante, como `:146-152` conserva el pre-chequeo en memoria de `template_files` y el Caso 1 (`:297-317`) no afirma `assert not (ws_dir / "src").exists()`, quitar solo `:146-152` (**M9a**) sigue sobreviviendo porque `:159-163` captura el archivo tras escribirlo en disco. |
| **F2A-09** | **Bajo** | **PARCIAL (No bloqueante)** | `src/agent_factory/adapters/subprocess_runner.py:25-26` | Se añadió la validación `if isinstance(command, (str, bytes)) or not command: raise ValueError(...)` en `SubprocessRunner.run`. Sin embargo, la línea `26` no tiene cobertura (`Missing: 26`) y la mutación **M21** sobrevive porque falta un test unitario en `tests/test_runner_timeout.py` que invoque `SubprocessRunner().run("", ...)`. |
| **F2A-10** | **Bajo** | **CERRADO** | `src/agent_factory/builder.py:427-557` | Eliminado el bloque redundante e inalcanzable `if job.ci_rounds_used >= self.max_ci_rounds:` previo a `push_branch`. La verificación de tope de rondas de CI ocurre en un único lugar (`:531-556`), cubierto al 100 %. |

---

## 3. Revisión de los 7 tests nuevos contra criterios e `INV-23`

| Test nuevo / reforzado | Criterio / Hallazgo | ¿Oráculo escrito a mano (`INV-23`)? | ¿Sin mocks/atajos indebidos? | Verificación |
|---|---|---|---|---|
| `test_quota_resume_succeeds_after_prior_attempt_wrote_production_files_in_src` (`test_builder_loop.py:596`) | AC-2.4 / F2A-01 | **Sí** (`invocations == 2` en pausa, `sleeps == [3.0]`, `invocations == 3` al entregar, `ci_rounds == 1`) | **Sí** | Usa `FakeCodeAgent`, `FakeRunner`, `FakeVCS` y `WorkspaceManager` real en `tmp_path`. |
| `test_runner_timeout_and_vcs_errors_transition_project_to_failed_with_report` (`test_builder_loop.py:675`) | A2a-2 / INV-06 / INV-09 / F2A-02 | **Sí** (`state == FAILED`, `"timed out"` y `"connection reset"` en `failure_reason` y `failure_cause`, `invocations == 2`) | **Sí** | Ejercita tanto `CommandTimeoutError` en `Runner` como `OSError` en `VCS`. |
| `test_test_generation_quota_pause_and_code_agent_error_handling` (`test_builder_loop.py:741`) | AC-2.4 / INV-09 / INV-17 / F2A-03 | **Sí** (`invocations == 0` y `get_calls == 0` en pausa; `invocations == 2` y `agent_invocations == 3` al entregar; `invocations == 1` en `CodeAgentError`) | **Sí** | Cubre pausa por cuota y error fatal durante Paso A (`generate_tests`). |
| `test_building_budget_is_cumulative_with_breaker_and_records_failed_agent_calls` (`test_builder_loop.py:831`) | AC-2.3 / INV-17 / F2A-04 | **Sí** (`14` llamadas previas + `1` `test_gen` = `15`, `len(impl_calls) == 0`; en fallo `impl`, `get_calls == 2`, `agent_invocations == 3`) | **Sí** | Verifica que el `CircuitBreaker` acumulativo impida invocar `implement_or_fix` al alcanzar 15. |
| `test_local_tests_passed_requires_zero_exit_code_and_all_plan_acceptance_tests` (`test_builder_loop.py:891`) | AC-2.1 / INV-13 / F2A-05 | **Sí** (`len(fake_runner.calls) == 5`, `len(fake_vcs.pushes) == 1`, `invocations == 6`, `ci_rounds == 1`) | **Sí** | Cubre 4 combinaciones inválidas de `exit_code` y `test_results` antes de converger en el 5.º intento. |
| `test_project_delivered_projects_repo_and_deploy_links_in_status` (`test_builder_loop.py:957`) | A2a-4 / INV-11 / F2A-07 | **Sí** (`"https://github.com/af-org/proj-links-delivered"` y `"https://af-org.github.io/proj-links-delivered"` escritos a mano) | **Sí** | Comprueba tanto `orchestrator.get_status` como `project_status_from_events`. |
| `test_test_lock_protects_root_conftest_and_setup_cfg_from_modification_deletion_or_injection` (`test_test_lock_and_generation.py:363`) + Caso 1b (`:319`) | AC-2.2 / A2a-5 / INV-13 / F2A-06 / F2A-08 | **Sí** (4 vectores de sabotaje en raíz y 1 vector de contaminación en disco `ws_dir / "src"`) | **Sí** | Verifica reversión exacta de contenido y eliminación de archivos intrusos en disco. |

---

## 4. Resultado de las 22 mutaciones ejecutadas (en worktree temporal)

Todas las mutaciones se ejecutaron en un worktree temporal fuera del workspace (`$TEMP/af-audit-2a1-wt`), que fue eliminado y podado al finalizar (`git worktree remove --force` + `git worktree prune`; verificado con `git worktree list`).

| ID | Archivo y líneas mutadas | Descripción de la mutación | Resultado | Tests que la detectaron (o motivo de supervivencia) |
|---|---|---|---|---|
| **M1** | `workspace.py:135-139` | Ignorar archivos de configuración del runner en `compute_test_lock` (`runner_config_hash` fijo en vacío) | **DETECTADA (KILLED)** | 4 fallos en `test_test_lock_and_generation.py` (incluyendo el nuevo test de `conftest.py`/`setup.cfg`) |
| **M2** | `workspace.py:165-168` | Detectar cambio de hash en `verify_and_restore_test_lock` (`return False`) pero **no revertir** los archivos | **DETECTADA (KILLED)** | 3 fallos en `test_test_lock_and_generation.py` |
| **M3** | `workspace.py:83` | Desactivar `if not candidate.is_relative_to(ws_dir)` en `resolve_safe_path` | **DETECTADA (KILLED)** | 5 fallos en `test_workspace_isolation.py` |
| **M4** | `workspace.py:82` | Usar `os.path.abspath` en vez de `Path.resolve()` (permitiendo escape por symlink/junction) | **DETECTADA (KILLED)** | 1 fallo en `test_workspace_isolation.py::test_workspace_manager_rejects_symlink_or_junction_escaping_workspace` |
| **M5** | `subprocess_runner.py:51-55` | Usar `proc.kill()` en lugar de `taskkill /F /T /PID` al vencer el timeout en Windows | **DETECTADA (KILLED)** | 1 fallo en `test_runner_timeout.py::test_subprocess_runner_enforces_timeout_and_kills_parent_and_child_processes` |
| **M6** | `builder.py:420-425` | Hacer `self.vcs.push_branch(...)` también cuando fallan las pruebas locales | **DETECTADA (KILLED)** | 2 fallos en `test_builder_loop.py` (`test_push_occurs_only_when_local_tests_pass...` y `test_local_tests_passed_requires_zero_exit_code...`) |
| **M7** | `builder.py:103` | Permitir 4 rondas de CI (`self.max_ci_rounds = 4`) en lugar del tope de 3 | **DETECTADA (KILLED)** | 1 fallo en `test_builder_loop.py::test_exhausting_three_ci_rounds_transitions_to_failed_with_report` |
| **M8** *(antes sobrevivió)* | `builder.py:189-192` | Descontar presupuesto (`record_state_call`) e incrementar `invocations_used` al capturar `QuotaExhaustedError` en **Paso A (`generate_tests`)** | **DETECTADA (KILLED)** | 1 fallo en `test_builder_loop.py::test_test_generation_quota_pause_and_code_agent_error_handling` |
| **M8b** | `builder.py:307-310` | Descontar presupuesto e incrementar `invocations_used` al capturar `QuotaExhaustedError` en **Paso B (`implement_or_fix`)** | **DETECTADA (KILLED)** | 3 fallos en `test_builder_loop.py` |
| **M9a** *(antes sobrevivió)* | `builder.py:146-152` | Eliminar solo el pre-chequeo de contaminación `src/` sobre el diccionario `template_files` (dejando activo `:159-163`) | **SOBREVIVIÓ (SURVIVED)** | `6 passed` — `create_workspace` escribe `template_files` en disco y 4 líneas después `builder.py:159-163` lanza la misma `SandboxViolationError`. El Caso 1 (`test_test_lock_and_generation.py:312-317`) no comprueba `not (ws_dir / "src").exists()`. |
| **M9b** *(antes sobrevivió)* | `builder.py:159-163` | Eliminar el chequeo en disco de `ws_dir / "src"` antes de `generate_tests` | **DETECTADA (KILLED)** | 1 fallo en `test_test_lock_and_generation.py::test_test_generation_rejects_preexisting_production_code_or_missing_tests` (Caso 1b) |
| **M10** | `builder.py:113, 129` | Eliminar la guarda `if status.state != ProjectState.BUILDING: raise IllegalTransition(...)` | **DETECTADA (KILLED)** | 1 fallo en `test_builder_loop.py::test_build_worker_rejects_unapproved_project_not_in_building_state` |
| **M11** | `progress.py:36` | Devolver `35` fijo en vez del cálculo proporcional `round(35 + (90 - 35) * ratio)` en `BUILDING` | **DETECTADA (KILLED)** | 6 fallos en `test_builder_loop.py` y `test_progress_and_projection.py` |
| **M12** | `builder.py:359` | Desactivar `verify_and_restore_test_lock` en el bucle de `BuildWorker` (`if False:`) | **DETECTADA (KILLED)** | 1 fallo en `test_test_lock_and_generation.py::test_build_worker_detects_test_or_config_tampering_reverts_and_counts_attempt_as_failed` |
| **M13** *(antes sobrevivió)* | `builder.py:280-283` | Verificar el tope de `BUILDING` solo contra el contador local `job.invocations_used >= building_limit` en vez del `CircuitBreaker` central acumulativo | **DETECTADA (KILLED)** | 1 fallo en `test_builder_loop.py::test_building_budget_is_cumulative_with_breaker_and_records_failed_agent_calls` |
| **M14** *(antes sobrevivió)* | `builder.py:415` | Ignorar `runner_result.exit_code == 0` al calcular `local_tests_passed` | **DETECTADA (KILLED)** | 1 fallo en `test_builder_loop.py::test_local_tests_passed_requires_zero_exit_code_and_all_plan_acceptance_tests` |
| **M15** *(antes sobrevivió)* | `builder.py:506` | Omitir `repo_url` y `deploy_url` en el evento `PROJECT_DELIVERED` (`None`) | **DETECTADA (KILLED)** | 1 fallo en `test_builder_loop.py::test_project_delivered_projects_repo_and_deploy_links_in_status` |
| **M16** *(nueva — F2A-01)* | `builder.py:267-269` | Volver a ejecutar el chequeo anti-contaminación de `ws_dir / "src"` fuera de `if job.lock_snapshot is None:` (al reanudar) | **DETECTADA (KILLED)** | 1 fallo en `test_builder_loop.py::test_quota_resume_succeeds_after_prior_attempt_wrote_production_files_in_src` |
| **M17** *(nueva — F2A-02)* | `builder.py:373` | Desactivar la captura de excepciones (`CommandTimeoutError`, `OSError`, etc.) alrededor de `self.runner.run` | **DETECTADA (KILLED)** | 1 fallo en `test_builder_loop.py::test_runner_timeout_and_vcs_errors_transition_project_to_failed_with_report` *(Nota: en Python 3 `CommandTimeoutError(TimeoutError)` es subclase de `OSError`, por lo que `except OSError` también la captura).* |
| **M18** *(nueva — F2A-05)* | `builder.py:416` | Omitir `and has_all_plan_tests` en `local_tests_passed` (aceptando `test_results` incompletos respecto al `Plan`) | **DETECTADA (KILLED)** | 1 fallo en `test_builder_loop.py::test_local_tests_passed_requires_zero_exit_code_and_all_plan_acceptance_tests` |
| **M19** *(nueva — F2A-06)* | `workspace.py:17-18` | Omitir `"conftest.py"` y `"setup.cfg"` de `RUNNER_CONFIG_FILENAMES` | **DETECTADA (KILLED)** | 1 fallo en `test_test_lock_and_generation.py::test_test_lock_protects_root_conftest_and_setup_cfg_from_modification_deletion_or_injection` |
| **M20** *(nueva — F2A-07)* | `progress.py:166-170` | No proyectar `links = ProjectLinks(...)` al procesar `EventType.PROJECT_DELIVERED` | **DETECTADA (KILLED)** | 1 fallo en `test_builder_loop.py::test_project_delivered_projects_repo_and_deploy_links_in_status` |
| **M21** *(nueva — F2A-09)* | `subprocess_runner.py:25-26` | Desactivar la validación `if isinstance(command, (str, bytes)) or not command: raise ValueError(...)` en `SubprocessRunner.run` | **SOBREVIVIÓ (SURVIVED)** | `2 passed` — ningún test en `tests/test_runner_timeout.py` invoca `SubprocessRunner().run` con `command=""` o `command=[]` (`subprocess_runner.py:26` sin cobertura). |

---

## 5. Observaciones residuales para la Fase 2b (No bloqueantes para Fase 2a)

1. **Integración de `SubprocessRunner` con `has_all_plan_tests` (Fase 2b):** `BuildWorker` exige en `builder.py:407-418` que `runner_result.test_results` contenga `True` para cada `AcceptanceTest.id` del `Plan`. Actualmente `SubprocessRunner.run` (`subprocess_runner.py:44-49`) devuelve `CommandResult` con `test_results={}` por defecto. En la Fase 2b, al conectar el runner real con Vitest/Playwright, el adaptador o el worker deberá poblar `test_results` a partir del reporte de pruebas para que `has_all_plan_tests` pueda evaluarse en `True`.
2. **Test unitario de argumento inválido en `SubprocessRunner.run` (F2A-09 / M21):** Añadir en Fase 2b un test de 3 líneas en `tests/test_runner_timeout.py` que verifique `pytest.raises(ValueError)` ante `runner.run("", cwd=tmp_path)` y `runner.run([], cwd=tmp_path)` para cubrir `subprocess_runner.py:26`.
3. **Cobertura de ramas defensivas en `builder.py` (`173-185`, `230-238`, `348-356`, `483-500`):** Las líneas `230-238` y `348-356` son guardas defensivas post-`record_state_call` que no se alcanzan porque `:172` y `:283` ya interceptan el límite antes de invocar al agente; y `:483-500` es el bloque `except` de `vcs.merge_and_deploy` (idéntico al de `get_ci_result` probado en `:446-463`).

---

## 6. Lo que NO se pudo verificar y por qué

1. **Rama POSIX de `SubprocessRunner` (`src/agent_factory/adapters/subprocess_runner.py:63-92`, `start_new_session=True` + `os.killpg`):**
   - El entorno de ejecución local es Windows (`win32`, `os.name == "nt"`), donde solo se ejecuta la rama Windows (`taskkill /F /T /PID`). La rama POSIX se ejercitará en el runner Ubuntu de GitHub Actions.
2. **Plantilla `templates/static-app/`, `AgyCliAdapter` real y pruebas e2e con Playwright (`AC-2.5`, `INV-19`, `INV-20`):**
   - Excluidos por diseño del alcance de la Fase 2a; corresponden a la Fase 2b.
