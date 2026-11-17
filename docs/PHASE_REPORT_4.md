# Phase Report 4 — Cierre Operativo: Panel PWA Estático, Entrypoint CLI, Adaptadores Reales y Endurecimiento de Auditoría (`AC-4.1` a `AC-4.6`)

**Rama:** `phase-4`  
**Estado:** Endurecimiento e implementación completadas y verificadas (**`324 passed`**, `0 failed`, `100% offline`). Cierre formal de la Fase 4 según `MASTER_PROMPT.md` (v1.2.1) §7, `AGENTS.md`, `docs/decisions/ADR-004-pwa-panel-and-cli-entrypoint.md` y `docs/AUDIT_REPORT_4.md`.

---

## 1. Resumen de Implementación por Criterio de Aceptación (`AC-4.1` a `AC-4.6`)

### `AC-4.1`: Panel PWA Estático (`panel/`) y Exportación de Bundle
- **Panel PWA 100% estático:** implementado en `panel/` (`index.html`, `styles.css`, `app.js`, `manifest.webmanifest`, `sw.js`, `icons/icon.svg`).
  - **Sin backend runtime (`INV-16`):** funciona en cualquier servidor de archivos estáticos o abriendo directamente como PWA.
  - **Service Worker (`sw.js`):** intercepta únicamente peticiones `GET`, almacena en caché shell estático y consultas a `registry.json` y `projects/<id>.json`.
  - **Seguridad defensiva:** valida `project_id` con regex estricta `^[a-z0-9][a-z0-9-]{0,62}$` (`INV-22`), escapa HTML para prevenir XSS, parsea JSON de forma defensiva con bloques `try/catch` y rechaza esquemas no seguros (`javascript:`, `data:`, protocolo-relativo `//`). Cero referencias a hosts locales (`localhost`, `127.0.0.1`), encabezados `Authorization` o tokens.
- **Exportación y Sanitización (`StaticDeployPublisher`):**
  - `build_sanitized_project_detail()`: genera la vista de detalle para `projects/<id>.json` serializando estado, progreso, enlaces, reporte de construcción y eventos, aplicando `_sanitize_data()` de forma recursiva para eliminar cualquier token o credencial sin romper la estructura JSON ni corromper delimitadores de comillas.
  - `export_panel_bundle()`: copia recursivamente los activos del panel PWA y genera `registry.json` y los archivos individuales en `projects/<id>.json`, saneando todas las propiedades de catálogo (`name`, `objective`, `repo_url`, `deploy_url`).

### `AC-4.2`: Configuración Fail-Closed de CLI (`src/agent_factory/cli.py`)
- **Carga y validación estricta (`load_cli_config`):**
  - Exige obligatoriamente `TELEGRAM_BOT_TOKEN`, `ALLOWED_CHAT_IDS` y `ALLOWED_USER_IDS` (`INV-21`).
  - Parsea listas de IDs separadas por coma a conjuntos inmutables `frozenset[int]`.
  - Valida mediante `math.isfinite()` que `AGENT_FACTORY_POLL_TIMEOUT > 0`, `AGENT_FACTORY_BACKOFF_INITIAL > 0` y `AGENT_FACTORY_BACKOFF_INITIAL <= AGENT_FACTORY_BACKOFF_MAX`, rechazando estrictamente `nan`, `inf`, `-inf`, cero y números negativos.
  - Lanza `CliConfigError` fail-closed ante cualquier parámetro faltante o inválido.
  - **Protección de secretos (`INV-19`):** el valor de `TELEGRAM_BOT_TOKEN` nunca se incluye ni se filtra en mensajes de excepción ni diagnósticos de error de configuración.

### `AC-4.3`: Polling Loop con Backoff Exponencial y Apagado Limpio (`src/agent_factory/cli.py`)
- **Loop de polling acotado (`run_polling_loop`):**
  - Ejecuta `bot_service.poll_once()` en ciclo continuo mientras `stop_event` no esté activo.
  - Ante fallos transitorios de red (`TelegramTransportError`, `OSError`), duplica el tiempo de espera acotado: `delay = min(current_backoff * 2.0, max_backoff)`.
  - Reinicia inmediatamente el backoff a `initial_backoff` tras cada polling exitoso.
  - Notifica a `on_error(exc, delay)` para observabilidad sin abortar el servicio.
- **Manejo de señales de apagado (`install_signal_handlers`):**
  - Registra controladores para `SIGINT` (Ctrl+C) y `SIGTERM` que activan `stop_event.set()`, permitiendo un cierre ordenado e inmediato sin dejar procesos huérfanos.

### `AC-4.4`: Adaptadores de Producción Detrás de Puertos (`src/agent_factory/adapters/`)
- **`GitHubVCSAdapter` (`src/agent_factory/adapters/github_vcs_adapter.py`):**
  - Implementa el puerto `VCS` delegando comandos de `git` y `gh` al puerto `Runner`.
  - `build_clean_env()`: aísla el entorno de subprocess filtrando secretos del host (`TELEGRAM_BOT_TOKEN`, `LLM_API_KEY`, etc.) y pasando únicamente variables esenciales del sistema e inyectando `GITHUB_TOKEN`/`GH_TOKEN` con alcance específico.
  - `get_ci_result()`: consulta `gh run list --repo <owner>/<project> --branch <branch> --limit 1 --json status,conclusion`, parseando de forma robusta arreglos o diccionarios JSON.
  - `merge_and_deploy()`: ejecuta `gh pr merge <branch> --repo <owner>/<project> --merge --auto`.
  - Valida preventivamente `project_id` y nombres de rama (rechaza inyección de flags como `-flag`, espacios o traversals) antes de llamar a Runner (`INV-22`).
  - Limpia `__cause__` y `__context__` en `VcsError` y auto-incluye `github_token` en la lista de secretos a redactar.
- **`StaticDeployPublisher` (`src/agent_factory/adapters/static_deploy_publisher.py`):**
  - Implementa despliegue de sitios estáticos (GitHub Pages / Vercel) mediante `Runner`.
  - `build_clean_env()`: limpia variables de entorno y expone únicamente `VERCEL_TOKEN`/`GITHUB_TOKEN`.
  - Valida directorios de origen y `project_id`; valida la rama de destino rechazando inyecciones de flags (`-force`, `--exec=evil`), espacios, traversals (`..`) y ramas protegidas (`main`, `master`).
  - Limpia `__cause__` y `__context__` en `StaticDeployError` y auto-incluye `deploy_token` en los secretos a redactar.

### `AC-4.5`: Cierre de Observaciones Residuales de Auditoría (`H4-01` a `H4-11`)
- **`H4-01` & `H4-09` (`src/agent_factory/cli.py`):**
  - `build_runtime()` no utiliza `FakeLLM` cuando `llm is None`; exige `LLM_API_KEY`, valida la existencia de `config/models.toml` y rechaza placeholders `"CONFIGURE_ME"`.
  - Cablea `ProductionLLMAdapter` detrás del puerto `LLM` y conecta `limits_config_path` al `CircuitBreaker`.
  - Conecta `StaticDeployPublisher` a `GitHubVCSAdapter` cuando `deploy_target` o `vercel_token` están configurados.
  - `load_cli_config()` valida `math.isfinite` para parámetros numéricos y enmascara `TELEGRAM_BOT_TOKEN` en todas las excepciones `CliConfigError`.
  - `main()` captura `CliConfigError` imprimiendo a `sys.stderr` saneado y garantiza `runtime.store.close()` en bloque `finally`.
- **`H4-02` & `H4-06` (`src/agent_factory/adapters/github_vcs_adapter.py`):**
  - Pasa `<owner>/<project>` en `--repo`, usa `gh run list`, parsea salidas JSON tipo lista o diccionario, usa `clean_env` en todas las invocaciones y limpia `__cause__`/`__context__` en `VcsError`.
- **`H4-03` & `H4-05` (`src/agent_factory/adapters/static_deploy_publisher.py` & `src/agent_factory/secrets.py`):**
  - Validación estricta de nombres de rama en `deploy_site()` antes de llamar a Runner.
  - Sanitización recursiva de datos en `export_panel_bundle()` y `build_sanitized_project_detail()` sin corromper delimitadores JSON.
  - En `secrets.py`, exclusión de delimitadores de comillas (`\"'`) en regex de URL de bot de Telegram sin barra final.
- **`H4-04` & `H4-08` (`panel/index.html` & `panel/app.js`):**
  - Origen de registro configurado con `<input type="text">` para aceptar rutas relativas (`.` y `./`).
  - `sanitizeLinkUrl()` valida esquemas `https:`/`http:`, descarta protocolo-relativo (`//`) y URLs con credenciales.
  - `renderDetail()` renderiza `PROJECT_PROMPT.md` y `build_report.failure_cause` con escape HTML estricto contra ataques XSS.
- **`H4-06` & `H4-10` (`src/agent_factory/adapters/git_registry_publisher.py`):**
  - Limpieza de `__cause__` y `__context__` en `RegistryPublishError`.
  - Al abortar rebases divergentes, preserva y restaura entradas de proyectos locales creados offline en `registry.json` y maneja catálogos de proyectos con tipado no diccionario.
- **`H4-11` (`src/agent_factory/telegram_bot.py`):**
  - `/resume` rechaza solicitudes sobre proyectos completados (`JobState.COMPLETED`) con acción `"resume_rejected"`.
  - `poll_once()` aísla excepciones `TelegramTransportError` durante el envío de respuestas de error para garantizar que un fallo en la red no aborte el procesamiento del lote de actualizaciones restantes.

### `AC-4.6`: Pruebas Operativas de Extremo a Extremo Offline (`tests/test_phase4_e2e_offline.py`)
- **Flujo E2E completo verificado offline:**
  - Configuración CLI desde variables de entorno -> ensamblado de runtime en `build_runtime()` -> ejecución en loop de polling -> comando `/new App Clima` -> debate multi-agente -> aprobación vía callback inline -> construcción mediante Agy CLI (`static-web`) -> publicación en rama `registry` -> exportación del bundle PWA estático -> entrega y notificación por Telegram con enlaces de despliegue y repositorio, sin ninguna fuga de tokens.
  - Verificación negativa de configuración inválida y recuperación con backoff exponencial ante fallos de conexión simulados.

---

## 2. Verificación de Invariantes del Sistema

| Invariante | Descripción | Estado | Evidencia |
|---|---|:---:|---|
| **`INV-01`** | Dependencias mínimas y con versión fija | **CUMPLE** | Sin cambios en `pyproject.toml`; dependencias intactas. |
| **`INV-02`** | Todo acceso externo detrás de un puerto | **CUMPLE** | `GitHubVCSAdapter` y `StaticDeployPublisher` delegan en `Runner`; Telegram opera por `HttpTransport`. |
| **`INV-03`** | Tests 100% offline y sin secretos | **CUMPLE** | `pytest-socket` activo (`--disable-socket`); 324 pruebas pasan sin socket de red. |
| **`INV-04`** | Transiciones de estado deterministas | **CUMPLE** | Todas las transiciones validadas mediante `validate_transition()`. |
| **`INV-06`** | Rate limits y circuit breaker | **CUMPLE** | Circuit breaker central protege todas las invocaciones LLM respetando `config/limits.toml`. |
| **`INV-07` / `INV-19`** | Cero filtración de secretos en errores/logs | **CUMPLE** | Tokens enmascarados con `[REDACTED]` en excepciones, eventos, bundle PWA, Telegram y `__cause__`/`__context__`. |
| **`INV-11`** | Estado derivado de eventos | **CUMPLE** | `project_status_from_events` reconstruye el estado del proyecto sin mutaciones ad-hoc. |
| **`INV-13`** | Prohibido alterar tests o configuración | **CUMPLE** | `git diff phase-4.1-tests --stat -- tests/ templates/ pyproject.toml` es **vacío**. |
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
git diff phase-4.1-tests --stat -- tests/ templates/ pyproject.toml
(salida vacía - 0 líneas modificadas en tests, templates o pyproject.toml)
```

### Ejecución Completa de Pruebas y Cobertura (`pytest`)
- **Total de pruebas:** **324 pasadas** (316 anteriores + 8 nuevas de endurecimiento Fase 4), 0 falladas, 0 omitidas.
- **Tiempo de ejecución:** 103.69s (100% offline).
- **Cobertura global (`src/agent_factory`):** **92%**

#### Desglose de Cobertura por Módulo

| Módulo | Sentencias | Fallas | Ramas | Ramas Parciales | Cobertura |
|---|---:|---:|---:|---:|---:|
| **TOTAL (`src/agent_factory`)** | **2778** | **174** | **736** | **104** | **92%** |
| `adapters/agy_cli_adapter.py` | 104 | 0 | 28 | 1 | **99%** |
| `adapters/fake_telegram.py` | 35 | 2 | 10 | 2 | **91%** |
| `adapters/git_registry_publisher.py` | 212 | 22 | 68 | 12 | **87%** |
| `adapters/github_vcs_adapter.py` | 128 | 4 | 32 | 4 | **95%** |
| `adapters/memory_store.py` | 32 | 0 | 6 | 0 | **100%** |
| `adapters/sqlite_store.py` | 56 | 0 | 6 | 2 | **97%** |
| `adapters/static_deploy_publisher.py` | 100 | 0 | 28 | 2 | **98%** |
| `adapters/telegram_client.py` | 114 | 11 | 34 | 7 | **88%** |
| `builder.py` | 221 | 10 | 56 | 10 | **93%** |
| `cli.py` | 219 | 11 | 42 | 5 | **93%** |
| `domain/events.py` | 29 | 0 | 0 | 0 | **100%** |
| `domain/models.py` | 106 | 0 | 8 | 0 | **100%** |
| `domain/progress.py` | 97 | 2 | 46 | 4 | **96%** |
| `domain/states.py` | 25 | 0 | 2 | 0 | **100%** |
| `inbox.py` | 18 | 0 | 4 | 0 | **100%** |
| `orchestrator.py` | 215 | 17 | 56 | 6 | **90%** |
| `scaffold.py` | 99 | 3 | 46 | 1 | **97%** |
| `secrets.py` | 29 | 1 | 14 | 2 | **93%** |
| `telegram_bot.py` | 272 | 18 | 100 | 16 | **91%** |
| `workspace.py` | 112 | 3 | 52 | 7 | **94%** |

---

## 4. Supuestos y Decisiones Técnicas Adoptadas
1. **PWA estático desacoplado:** El panel no requiere ningún proceso de servidor propio; consume el catálogo `registry.json` y los detalles por proyecto directamente desde la URL de la rama `registry` o desde un clon local montado como recurso estático.
2. **Entorno limpio para adaptadores de producción:** `GitHubVCSAdapter` y `StaticDeployPublisher` construyen de forma determinista un entorno subprocess filtrando cualquier secreto no autorizado del host (`INV-19`), protegiendo credenciales del sistema central ante herramientas CLI externas.
3. **Resiliencia de publicación en Git Registry:** La recuperación automática de rebases divergentes garantiza que actualizaciones concurrentes o ejecuciones offline previas no bloqueen el catálogo de publicación.
4. **Protección multinivel contra fuga de credenciales:** Todos los adaptadores limpian activamente `__cause__` y `__context__` en las excepciones lanzadas para evitar que el traceback estándar de Python exponga cadenas con tokens que pudieran existir en excepciones causales subyacentes.
