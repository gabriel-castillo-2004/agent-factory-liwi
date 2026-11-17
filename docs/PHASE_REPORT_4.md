# Phase Report 4 — Cierre Operativo: Panel PWA Estático, Entrypoint CLI, Adaptadores Reales y Endurecimiento Final de Auditoría (`AC-4.1` a `AC-4.6`)

**Rama:** `phase-4`  
**Estado:** Endurecimiento e implementación completadas y verificadas (**`330 passed`**, `0 failed`, `100% offline`). Cierre formal de la Fase 4 según `MASTER_PROMPT.md` (v1.2.1) §7, `AGENTS.md`, `docs/decisions/ADR-004-pwa-panel-and-cli-entrypoint.md`, `docs/AUDIT_REPORT_4.md` y `docs/AUDIT_REPORT_4_1.md`.

---

## 1. Resumen de Implementación por Criterio de Aceptación (`AC-4.1` a `AC-4.6`)

### `AC-4.1`: Panel PWA Estático (`panel/`) y Exportación de Bundle
- **Panel PWA 100% estático:** implementado en `panel/` (`index.html`, `styles.css`, `app.js`, `manifest.webmanifest`, `sw.js`, `icons/icon.svg`).
  - **Sin backend runtime (`INV-16`):** funciona en cualquier servidor de archivos estáticos o abriendo directamente como PWA.
  - **Service Worker (`sw.js`):** intercepta únicamente peticiones `GET`, almacena en caché shell estático y consultas a `registry.json` y `projects/<id>.json`.
  - **Seguridad defensiva:** valida `project_id` con regex estricta `^[a-z0-9][a-z0-9-]{0,62}$` (`INV-22`), escapa HTML para prevenir XSS, parsea JSON de forma defensiva con bloques `try/catch` y rechaza esquemas no seguros (`javascript:`, `data:`, protocolo-relativo `//`). Cero referencias a hosts locales (`localhost`, `127.0.0.1`), encabezados `Authorization` o tokens.
  - **Alineación de estilos (`styles.css`, `H4-12`, `H41-04`):** el selector de controles de entrada aplica a `input[type="text"]`, `#registry-base-input`, `input[type="search"]` y `select`, eliminando la regla huérfana de `input[type="url"]`.
- **Exportación y Sanitización (`StaticDeployPublisher`):**
  - `build_sanitized_project_detail()`: genera la vista de detalle para `projects/<id>.json` serializando estado, progreso, enlaces, reporte de construcción y eventos, aplicando `_sanitize_data()` de forma recursiva para eliminar cualquier token o credencial sin romper la estructura JSON ni corromper delimitadores de comillas.
  - `export_panel_bundle()`: copia recursivamente los activos del panel PWA y genera `registry.json` y los archivos individuales en `projects/<id>.json`, saneando todas las propiedades de catálogo (`name`, `objective`, `repo_url`, `deploy_url`).

### `AC-4.2`: Configuración Fail-Closed de CLI (`src/agent_factory/cli.py`)
- **Carga y validación estricta (`load_cli_config`):**
  - Exige obligatoriamente `TELEGRAM_BOT_TOKEN`, `ALLOWED_CHAT_IDS` y `ALLOWED_USER_IDS` (`INV-21`).
  - Documentación sincronizada en `.env.example` con `ALLOWED_CHAT_IDS=`, `ALLOWED_USER_IDS=` y `LLM_API_KEY=`, habiendo purgado la variable obsoleta singular.
  - Parsea listas de IDs separadas por coma a conjuntos inmutables `frozenset[int]`.
  - Valida mediante `math.isfinite()` que `AGENT_FACTORY_POLL_TIMEOUT > 0`, `AGENT_FACTORY_BACKOFF_INITIAL > 0` y `AGENT_FACTORY_BACKOFF_INITIAL <= AGENT_FACTORY_BACKOFF_MAX`, rechazando estrictamente `nan`, `inf`, `-inf`, cero y números negativos.
  - Lanza `CliConfigError` fail-closed ante cualquier parámetro faltante o inválido.
  - **Protección de secretos (`INV-07`, `INV-19`, `H4-06`, `H4-09`, `H41-03`):** el valor de `TELEGRAM_BOT_TOKEN` nunca se incluye ni se filtra en mensajes de excepción; se limpian explícitamente `__cause__ = None` y `__context__ = None` en errores de parsing de IDs, timeouts y backoff para evitar filtraciones en el encadenamiento de excepciones de Python.

### `AC-4.3`: Polling Loop con Backoff Exponencial y Apagado Limpio (`src/agent_factory/cli.py`)
- **Loop de polling acotado e interactivo (`run_polling_loop`, `H41-03`):**
  - Ejecuta `bot_service.poll_once()` en ciclo continuo mientras `stop_event` no esté activo.
  - Ante fallos transitorios de red (`TelegramTransportError`, `OSError`), duplica el tiempo de espera acotado: `delay = min(current_backoff * 2.0, max_backoff)`.
  - Reinicia inmediatamente el backoff a `initial_backoff` tras cada polling exitoso.
  - Notifica a `on_error(exc, delay)` para observabilidad sin abortar el servicio.
  - Con `clock=None`, utiliza `stop_event.wait(delay)` en lugar de `time.sleep(delay)`, permitiendo que la señal de parada despierte el bucle de inmediato sin esperar el tiempo remanente de backoff.
- **Manejo de señales de apagado (`install_signal_handlers`):**
  - Registra controladores para `SIGINT` (Ctrl+C) y `SIGTERM` que activan `stop_event.set()`, permitiendo un cierre ordenado e inmediato sin dejar procesos huérfanos.

### `AC-4.4`: Adaptadores de Producción Detrás de Puertos (`src/agent_factory/adapters/`)
- **`GitHubVCSAdapter` (`src/agent_factory/adapters/github_vcs_adapter.py`):**
  - Implementa el puerto `VCS` delegando comandos de `git` y `gh` al puerto `Runner`.
  - `build_clean_env()`: aísla el entorno de subprocess filtrando secretos del host (`TELEGRAM_BOT_TOKEN`, `LLM_API_KEY`, etc.) y pasando únicamente variables esenciales del sistema e inyectando `GITHUB_TOKEN`/`GH_TOKEN` con alcance específico.
  - `get_ci_result()`: consulta `gh run list --repo <owner>/<project> --branch <branch> --limit 1 --json status,conclusion`, parseando de forma robusta arreglos o diccionarios JSON.
  - `merge_and_deploy()` (`H4-02`): ejecuta `gh pr merge <branch> --repo <owner>/<project> --merge --auto`, delega el despliegue a `deploy_publisher.deploy_site()` usando el directorio de workspace recordado en `push_branch` y retorna la tupla `(repo_url, deploy_url)`.
  - Valida preventivamente `project_id` y nombres de rama (rechaza inyección de flags como `-flag`, espacios, dos puntos, barras invertidas o traversals) antes de llamar a Runner (`INV-22`).
  - Limpia `__cause__` y `__context__` en `VcsError` y auto-incluye `github_token` en la lista de secretos a redactar.
- **`StaticDeployPublisher` (`src/agent_factory/adapters/static_deploy_publisher.py`):**
  - Implementa despliegue de sitios estáticos (GitHub Pages / Vercel) mediante `Runner`.
  - `build_clean_env()`: limpia variables de entorno y expone únicamente `VERCEL_TOKEN`/`GITHUB_TOKEN`.
  - `deploy_site()` (`H4-03`): valida preventivamente ramas protegidas insensible a mayúsculas/minúsculas (`MAIN`, `master`), ramas remotas (`origin/main`), referencias (`refs/heads/MAIN`), caracteres no válidos (`:`, `\`) y sufijos (`/main`, `/master`). Enruta según `target`: ejecuta `vercel deploy <dir> --yes` (con `--token` o `VERCEL_TOKEN`) para Vercel o `gh pages deploy <dir> --branch <branch>` para GitHub Pages.
  - Limpia `__cause__` y `__context__` en `StaticDeployError` y auto-incluye `deploy_token` en los secretos a redactar.

### `AC-4.5`: Cierre de Observaciones Residuales de Auditoría (`H4-01` a `H4-12` y `H41-01` a `H41-04`)
- **`H4-01`, `H41-01` & `H41-02` (`src/agent_factory/cli.py` & `adapters/telegram_client.py`):**
  - `build_runtime()` valida tempranamente la existencia y contenido de `config/limits.toml`, `config/models.toml` (sin `"CONFIGURE_ME"`) y `LLM_API_KEY` **antes** de abrir `SqliteEventStore`, previniendo bloqueos de archivos SQLite en Windows ante configuraciones defectuosas. Además, protege con bloque `try...except` y `store.close()` cualquier fallo posterior.
  - `ProductionLLMAdapter` adopta `UrllibHttpTransport` como transporte por defecto cuando `transport is None`.
  - `UrllibHttpTransport.post_json` soporta parámetro opcional `headers` y transmite encabezados HTTP adicionales.
  - `ProductionLLMAdapter.complete()` lee modelo y endpoint configurados en `models.toml` (ej. `endpoint = "..."` e `INTERVIEW`), envía petición HTTP con autenticación `Bearer`, parsea respuestas JSON defensivamente y sanea cualquier mensaje de error con `redact_secrets` limpiando `__cause__` y `__context__`.
- **`H4-02` & `H4-06` (`src/agent_factory/adapters/github_vcs_adapter.py`):**
  - `merge_and_deploy` invoca `deploy_publisher.deploy_site()` y retorna la URL del despliegue efectivo.
  - Limpieza de `__cause__` y `__context__` en `VcsError`.
- **`H4-03` & `H4-05` (`src/agent_factory/adapters/static_deploy_publisher.py` & `src/agent_factory/secrets.py`):**
  - Validación estricta insensible a mayúsculas/minúsculas de nombres de rama en `deploy_site()` antes de llamar a Runner.
  - Sanitización recursiva de datos en `export_panel_bundle()` y `build_sanitized_project_detail()` sin corromper delimitadores JSON.
  - En `secrets.py`, exclusión de delimitadores de comillas (`\"'`) en regex de URL de bot de Telegram sin barra final (`M4-N09`).
- **`H4-04` & `H4-08` (`panel/index.html` & `panel/app.js`):**
  - Origen de registro configurado con `<input type="text">` para aceptar rutas relativas (`.` y `./`).
  - `sanitizeLinkUrl()` valida esquemas `https:`/`http:`, descarta protocolo-relativo (`//`) y URLs con credenciales.
  - `renderDetail()` renderiza `PROJECT_PROMPT.md` y `build_report.failure_cause` con escape HTML estricto contra ataques XSS.
- **`H4-06`, `H4-10` (`src/agent_factory/adapters/git_registry_publisher.py`):**
  - `publish_project()` acepta opcionalmente `events: Sequence[Event] = ()` y serializa el historial saneado en `projects/<id>.json`.
  - Sanitización exhaustiva y recursiva de campos (`name`, `objective`, `repo_url`, `deploy_url`, `failure_reason`, eventos) en `registry.json` y `projects/<id>.json`.
  - Al abortar rebases divergentes, preserva y restaura entradas de proyectos locales creados offline en `registry.json`.
- **`H4-11` (`src/agent_factory/telegram_bot.py`):**
  - `/resume` rechaza solicitudes sobre proyectos completados (`JobState.COMPLETED`) con acción `"resume_rejected"` sin invocar a `BuildWorker`.
  - `poll_once()` aísla excepciones `TelegramTransportError` durante el envío de respuestas para garantizar que un fallo en la red no aborte el procesamiento del lote.
- **`H4-12` & `H41-04` (`panel/styles.css`):**
  - Selector de estilos corregido para apuntar a `input[type="text"]` y `#registry-base-input`, descartando el selector huérfano de `input[type="url"]`.

### `AC-4.6`: Pruebas Operativas de Extremo a Extremo Offline (`tests/test_phase4_e2e_offline.py`)
- **Flujo E2E completo verificado offline:**
  - Configuración CLI desde variables de entorno -> ensamblado de runtime en `build_runtime()` -> ejecución en loop de polling -> comando `/new App Clima` -> debate multi-agente -> aprobación vía callback inline -> construcción mediante Agy CLI (`static-web`) -> publicación en rama `registry` -> exportación del bundle PWA estático -> entrega y notificación por Telegram con enlaces de despliegue y repositorio, sin ninguna fuga de tokens.
  - Verificación negativa de configuración inválida y recuperación con backoff exponencial ante fallos de conexión simulados.

---

## 2. Verificación de Invariantes del Sistema

| Invariante | Descripción | Estado | Evidencia |
|---|---|:---:|---|
| **`INV-01`** | Dependencias mínimas y con versión fija | **CUMPLE** | Sin cambios en `pyproject.toml`; dependencias intactas. |
| **`INV-02`** | Todo acceso externo detrás de un puerto | **CUMPLE** | `GitHubVCSAdapter` y `StaticDeployPublisher` delegan en `Runner`; Telegram y LLM operan por `HttpTransport`. |
| **`INV-03`** | Tests 100% offline y sin secretos | **CUMPLE** | `pytest-socket` activo (`--disable-socket`); 330 pruebas pasan sin socket de red. |
| **`INV-04`** | Transiciones de estado deterministas | **CUMPLE** | Todas las transiciones validadas mediante `validate_transition()`. |
| **`INV-06`** | Rate limits y circuit breaker | **CUMPLE** | Circuit breaker central protege todas las invocaciones LLM respetando `config/limits.toml`. |
| **`INV-07` / `INV-19`** | Cero filtración de secretos en errores/logs | **CUMPLE** | Tokens enmascarados con `[REDACTED]` en excepciones, eventos, bundle PWA, Telegram y `__cause__`/`__context__`. |
| **`INV-11`** | Estado derivado de eventos | **CUMPLE** | `project_status_from_events` reconstruye el estado del proyecto sin mutaciones ad-hoc. |
| **`INV-13`** | Prohibido alterar tests o configuración | **CUMPLE** | `git diff phase-4.2-tests --stat -- tests/ templates/ pyproject.toml` es **vacío**. |
| **`INV-16`** | Panel PWA sin backend | **CUMPLE** | Panel en `panel/` 100% estático (HTML/CSS/JS + Service Worker) consumiendo `registry.json`. |
| **`INV-21`** | Configuración fail-closed | **CUMPLE** | `load_cli_config()` aborta limpiamente ante whitelists, números inválidos (`nan`/`inf`) o tokens ausentes. |
| **`INV-22`** | Validación estricta de entradas externas | **CUMPLE** | Validación regex de `project_id` y ramas en CLI, adaptadores VCS, deploy y PWA. |
| **`INV-23`** | Oráculo de prueba independiente | **CUMPLE** | Todos los valores esperados en pruebas están declarados a mano sin depender del código bajo prueba. |

---

## 3. Métricas de Verificación

### Análisis Estático de Código (`ruff`)
```
.\.venv\Scripts\ruff.exe check .
All checks passed!
```

### Integridad de Rutas Protegidas (`INV-13`)
```
git diff phase-4.2-tests --stat -- tests/ templates/ pyproject.toml
(salida vacía - 0 líneas modificadas en tests, templates o pyproject.toml)
```

### Ejecución Completa de Pruebas y Cobertura (`pytest`)
- **Total de pruebas:** **330 pasadas** (316 iniciales + 8 endurecimiento A' + 6 endurecimiento final A''), 0 falladas, 0 omitidas.
- **Tiempo de ejecución:** 147.84s (100% offline).
- **Cobertura global (`src/agent_factory`):** **91%**

#### Desglose de Cobertura por Módulo

| Módulo | Sentencias | Fallas | Ramas | Ramas Parciales | Cobertura |
|---|---:|---:|---:|---:|---:|
| **TOTAL (`src/agent_factory`)** | **2908** | **198** | **784** | **112** | **91%** |
| `__main__.py` | 4 | 0 | 2 | 0 | **100%** |
| `adapters/agy_cli_adapter.py` | 104 | 0 | 28 | 1 | **99%** |
| `adapters/fake_code_agent.py` | 61 | 10 | 26 | 8 | **79%** |
| `adapters/fake_llm.py` | 36 | 7 | 12 | 2 | **77%** |
| `adapters/fake_runner.py` | 25 | 5 | 10 | 4 | **74%** |
| `adapters/fake_telegram.py` | 35 | 2 | 10 | 2 | **91%** |
| `adapters/fake_vcs.py` | 33 | 3 | 6 | 2 | **87%** |
| `adapters/file_publisher.py` | 32 | 1 | 6 | 2 | **92%** |
| `adapters/git_registry_publisher.py` | 233 | 23 | 76 | 13 | **88%** |
| `adapters/github_vcs_adapter.py` | 133 | 4 | 34 | 4 | **95%** |
| `adapters/memory_store.py` | 32 | 0 | 6 | 0 | **100%** |
| `adapters/sqlite_store.py` | 56 | 0 | 6 | 2 | **97%** |
| `adapters/static_deploy_publisher.py` | 110 | 3 | 36 | 4 | **94%** |
| `adapters/subprocess_runner.py` | 40 | 17 | 4 | 2 | **57%** |
| `adapters/system_clock.py` | 7 | 0 | 0 | 0 | **100%** |
| `adapters/telegram_client.py` | 117 | 14 | 36 | 7 | **85%** |
| `agents/interviewer.py` | 38 | 0 | 8 | 1 | **98%** |
| `agents/mediator.py` | 77 | 2 | 16 | 2 | **96%** |
| `agents/pragmatist.py` | 28 | 0 | 4 | 0 | **100%** |
| `agents/structuralist.py` | 27 | 0 | 4 | 0 | **100%** |
| `builder.py` | 221 | 10 | 56 | 10 | **93%** |
| `cli.py` | 308 | 33 | 70 | 11 | **87%** |
| `domain/events.py` | 29 | 0 | 0 | 0 | **100%** |
| `domain/models.py` | 106 | 0 | 8 | 0 | **100%** |
| `domain/progress.py` | 97 | 2 | 46 | 4 | **96%** |
| `domain/states.py` | 25 | 0 | 2 | 0 | **100%** |
| `inbox.py` | 18 | 0 | 4 | 0 | **100%** |
| `orchestrator.py` | 215 | 17 | 56 | 6 | **90%** |
| `ports/clock.py` | 8 | 2 | 0 | 0 | **75%** |
| `ports/code_agent.py` | 19 | 2 | 0 | 0 | **89%** |
| `ports/llm.py` | 8 | 1 | 0 | 0 | **88%** |
| `ports/notifier.py` | 35 | 4 | 0 | 0 | **89%** |
| `ports/publisher.py` | 36 | 5 | 0 | 0 | **86%** |
| `ports/runner.py` | 15 | 1 | 0 | 0 | **93%** |
| `ports/store.py` | 11 | 3 | 0 | 0 | **73%** |
| `ports/vcs.py` | 17 | 3 | 0 | 0 | **82%** |
| `scaffold.py` | 99 | 3 | 46 | 1 | **97%** |
| `secrets.py` | 29 | 0 | 14 | 1 | **98%** |
| `telegram_bot.py` | 272 | 18 | 100 | 16 | **91%** |
| `workspace.py` | 112 | 3 | 52 | 7 | **94%** |

---

## 4. Supuestos y Decisiones Técnicas Adoptadas
1. **PWA estático desacoplado:** El panel no requiere ningún proceso de servidor propio; consume el catálogo `registry.json` y los detalles por proyecto directamente desde la URL de la rama `registry` o desde un clon local montado como recurso estático.
2. **Entorno limpio para adaptadores de producción:** `GitHubVCSAdapter` y `StaticDeployPublisher` construyen de forma determinista un entorno subprocess filtrando cualquier secreto no autorizado del host (`INV-19`), protegiendo credenciales del sistema central ante herramientas CLI externas.
3. **Resiliencia de publicación en Git Registry:** La recuperación automática de rebases divergentes garantiza que actualizaciones concurrentes o ejecuciones offline previas no bloqueen el catálogo de publicación, y se sanean de forma recursiva todos los datos de proyecto e historial de eventos.
4. **Protección multinivel contra fuga de credenciales:** Todos los adaptadores y puntos de configuración limpian activamente `__cause__` y `__context__` en las excepciones lanzadas para evitar que el traceback estándar de Python exponga cadenas con tokens que pudieran existir en excepciones causales subyacentes.
5. **Prevención temprana de bloqueo de recursos en Windows:** `build_runtime` valida tokens, modelos y límites antes de abrir conexiones a la base de datos SQLite, y garantiza el cierre seguro de descriptores de archivo ante cualquier contingencia.
