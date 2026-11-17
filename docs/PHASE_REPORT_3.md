# Phase Report 3 — Telegram Long Polling, Sanitización de Secretos y Publicación en `registry` (Cierre PASO A'')

**Rama:** `phase-3` (tag `phase-3.2-tests`)  
**Estado:** Implementación y suite de pruebas completadas y verificadas (`305 passed`, 0 failed). Defecto de fixture documentado en [`docs/PHASE3_TEST_ISSUES.md`](PHASE3_TEST_ISSUES.md) resuelto quirúrgicamente en PASO A''.

---

## 1. Resumen de cambios (`A3-01` a `A3-09`) y Ajuste de Fixture (PASO A'')

- **`A3-01` (`src/agent_factory/domain/events.py`, `src/agent_factory/builder.py`, `docs/decisions/ADR-003-telegram-and-registry-ports.md`):**
  - Se agregó `EventType.REGISTRY_PUBLISH_FAILED` al dominio de eventos.
  - En `BuildWorker`, tras `PROJECT_DELIVERED`, el fallo de `registry_publisher.publish_project` no emite `PROJECT_FAILED` (evitando la transición ilegal `DELIVERED -> FAILED` bajo `INV-04`/`INV-11`) y registra `EventType.REGISTRY_PUBLISH_FAILED` en modo *best-effort*, conservando `JobState.COMPLETED` y `ProjectState.DELIVERED`.
- **`A3-02` (`src/agent_factory/builder.py`, `src/agent_factory/orchestrator.py`):**
  - Se extrajeron los helpers `_redact` y `_fail_job` en `BuildWorker` y se aplicó `redact_secrets` de forma uniforme en todas las rutas de fallo (`generate_tests`, `implement_or_fix`, `runner.run`, `vcs.push_branch`, `vcs.get_ci_result`, `vcs.merge_and_deploy`, `max_ci_rounds`, resúmenes de CI).
  - Se aplicó `redact_secrets` en los payloads de fallo de `Orchestrator` (`handle_interview_message`, `run_debate`, `record_state_call`).
- **`A3-03` (`src/agent_factory/adapters/sqlite_store.py`, `src/agent_factory/adapters/memory_store.py`, `src/agent_factory/telegram_bot.py`):**
  - Se añadieron las tablas persistentes `telegram_processed_updates` y `telegram_active_projects` en SQLite (y equivalentes en memoria) con los helpers `record_telegram_update`, `get_processed_telegram_updates`, `set_telegram_active_project` y `get_telegram_active_projects`, sin alterar la tabla `events` ni `list_project_ids()`.
  - `TelegramBotService` hidrata updates procesados y proyectos activos por chat al iniciar (sin avanzar artificialmente `next_offset`, de modo que updates reentregados retornan `ignored_duplicate`).
- **`A3-04` (`src/agent_factory/secrets.py`):**
  - `redact_secrets` enmascara patrones conocidos (`bot<digits>:<token>`, `ghp_...`, `github_pat_...`, `sk-...`) incluso con `env={}` y `extra_secrets=()`.
  - Se acotó la regex de userinfo en URLs a `(https?://[^/\s:@]+:)[^/\s@]+(@)` para no corromper URLs públicas con puerto y `@` en el path (p. ej. `https://example.com:8080/users/@gabriel?tab=1`).
- **`A3-05` (`src/agent_factory/telegram_bot.py`, `src/agent_factory/orchestrator.py`):**
  - `TelegramBotService` valida `plan_hash` en callbacks `adjust:`, verifica guardia antes de `run_debate` si la entrevista pasó a `FAILED`, y aísla errores por update / lote de polling en `handle_update` y `poll_once`.
- **`A3-06` (`src/agent_factory/adapters/git_registry_publisher.py`):**
  - `_validate_branch` rechaza ramas protegidas (`main`, `master`, sufijos `/main` y `/master` con `norm.endswith(("/main", "/master"))`) y refs inseguras (`-`, espacios, `..`, `:`, `\`).
  - `_ensure_repository_ready` recupera con `git reset --hard FETCH_HEAD` cuando `HEAD` es ancestro estricto de `FETCH_HEAD` (tolerando `registry.json` corrupto en disco local) y usa `git rebase FETCH_HEAD` en ramas divergentes tras push fallido.
- **`A3-07` (`src/agent_factory/adapters/telegram_client.py`):**
  - `_post` captura `http.client.HTTPException` además de errores de `urllib`/`OSError` y relanza `TelegramTransportError` sanitizado fuera de bloques `except` (`__cause__=None`, `__context__=None`).
  - `get_updates` tolera `"text": None` y subobjetos nulos sin lanzar `AttributeError`/`TypeError`.
- **`A3-08` y `A3-09` (Transparencia de métricas y alcance):**
  - Métricas exactas observadas en la corrida final de la suite y delimitación del alcance pendiente respecto a `MASTER_PROMPT.md` §7.
- **Ajuste Quirúrgico PASO A'' (`tests/test_telegram_polling_and_acl.py:667`):**
  - Se completó el fixture de `FakeRunner` inyectando `test_results={"AT-01": True, "AT-02": True}` para coincidir con las 2 pruebas de aceptación definidas por `_complete_plan_payload`, resolviendo `ISSUE-3.1` sin debilitar aserciones ni el oráculo de prueba.

---

## 2. Verificación ejecutada

### Linter e integridad de código (`src/`, `templates/`, `pyproject.toml`)

- `git diff --stat -- src/ templates/ pyproject.toml`: **salida vacía** (cero cambios en código de producción, plantillas ni dependencias en este paso).
- `.\.venv\Scripts\ruff.exe check .`: **`All checks passed!`** (código de salida `0`).

### Suite de pruebas y cobertura (`pytest -q --cov=src/agent_factory --cov-report=term-missing`)

- **Resultado global observado:** **`305 passed, 2 warnings in 53.61s`** (código de salida `0`).
- **Tests de registro (`tests/test_registry_publisher.py`):** `4 passed`.
- **Tests de Telegram y ACL (`tests/test_telegram_polling_and_acl.py`):** `7 passed`.

#### Cobertura medida (`src/agent_factory`)

| Módulo / Total | Stmts | Miss | Branch | BrPart | Cobertura |
|---|---:|---:|---:|---:|---:|
| `TOTAL` (`src/agent_factory`) | 2271 | 156 | 608 | 93 | **91%** |
| `adapters/agy_cli_adapter.py` | 104 | 0 | 28 | 1 | **99%** |
| `adapters/fake_telegram.py` | 35 | 2 | 10 | 2 | **91%** |
| `adapters/git_registry_publisher.py` | 166 | 19 | 48 | 11 | **85%** |
| `adapters/memory_store.py` | 32 | 0 | 6 | 0 | **100%** |
| `adapters/sqlite_store.py` | 56 | 0 | 6 | 2 | **97%** |
| `adapters/telegram_client.py` | 114 | 11 | 34 | 8 | **87%** |
| `builder.py` | 221 | 10 | 56 | 10 | **93%** |
| `domain/events.py` | 29 | 0 | 0 | 0 | **100%** |
| `domain/models.py` | 106 | 0 | 8 | 0 | **100%** |
| `domain/progress.py` | 97 | 2 | 46 | 4 | **96%** |
| `domain/states.py` | 25 | 0 | 2 | 0 | **100%** |
| `orchestrator.py` | 215 | 17 | 56 | 6 | **90%** |
| `secrets.py` | 28 | 1 | 14 | 2 | **93%** |
| `telegram_bot.py` | 263 | 22 | 96 | 16 | **89%** |

---

## 3. Alcance cubierto vs. pendiente de Fase 3 (`MASTER_PROMPT.md` §7)

- **Cubierto en `phase-3` (100% offline / detrás de puertos):**
  - Interfaz Telegram por *long polling* con ACL de chat/usuario, deduplicación persistente y sanitización de secretos.
  - Publicación en rama `registry` (`registry.json` y metadatos por proyecto) con tolerancia *best-effort* ante fallos de publicación sin corromper `DELIVERED`.
- **Pendiente / diferido fuera de este paso (requerirá fase o alcance específico):**
  - **Panel PWA** estático (HTML/CSS/JS) para visualización del catálogo `registry.json` y detalle de proyectos.
  - **Adaptadores de red real / despliegue real** en GitHub API / Vercel u otros proveedores productivos (actualmente cubiertos únicamente detrás de los puertos `VCSPort` y `RegistryPublisherPort` con fakes/repositorios locales offline).
