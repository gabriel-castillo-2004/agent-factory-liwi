# Phase Report 4 — Cierre Operativo: Panel PWA Estático, Entrypoint CLI, Adaptadores Reales y Endurecimiento Final de Auditoría (`AC-4.1` a `AC-4.6`)

**Rama:** `phase-4`  
**Estado:** Endurecimiento e implementación completadas y verificadas (**`341 passed`**, `0 failed`, `100% offline`). Cierre formal de la Fase 4 según `MASTER_PROMPT.md` (v1.2.1) §7, `AGENTS.md`, `docs/decisions/ADR-004-pwa-panel-and-cli-entrypoint.md`, `docs/AUDIT_REPORT_4.md`, `docs/AUDIT_REPORT_4_1.md`, `docs/AUDIT_REPORT_4_2.md` y `docs/AUDIT_REPORT_4_3.md`.

---

## 1. Resumen de Implementación por Criterio de Aceptación (`AC-4.1` a `AC-4.6`)

### `AC-4.1`: Panel PWA Estático (`panel/`) y Exportación de Bundle
- **Panel PWA 100% estático:** implementado en `panel/` (`index.html`, `styles.css`, `app.js`, `manifest.webmanifest`, `sw.js`, `icons/icon.svg`).
  - **Sin backend runtime (`INV-16`):** funciona en cualquier servidor de archivos estáticos o abriendo directamente como PWA.
  - **Service Worker (`sw.js`):** intercepta únicamente peticiones `GET`, almacena en caché shell estático y consultas a `registry.json` y `projects/<id>.json`.
  - **Seguridad defensiva:** valida `project_id` con regex estricta `^[a-z0-9][a-z0-9-]{0,62}$` (`INV-22`), escapa HTML para prevenir XSS, parsea JSON de forma defensiva con bloques `try/catch` y rechaza esquemas no seguros (`javascript:`, `data:`, protocolo-relativo `//`). Cero referencias a hosts locales (`localhost`, `127.0.0.1`), encabezados `Authorization` o tokens.
  - **Alineación de estilos y robustez de renderizado (`styles.css`, `app.js`, `H4-12`, `H41-04`, `H42-05`, `H43-04`):** selector de controles de entrada aplica a `input[type="text"]`, `#registry-base-input`, `input[type="search"]` y `select`. Se añadieron clases `.alert-warning` y `.prompt-box` en `styles.css`. En `app.js`, la función `toFiniteNonNegativeInt()` valida `Number.isFinite()` y `Math.max(0, ...)` en contadores de invocaciones, rondas CI, tests y métricas de build; y `redactDisplayText()` preserva palabras legítimas como `"NaN"` en textos de proyecto sin corromper el contenido.
- **Exportación y Sanitización (`StaticDeployPublisher`):**
  - `build_sanitized_project_detail()`: genera la vista de detalle para `projects/<id>.json` serializando estado, progreso, enlaces, reporte de construcción y eventos, aplicando `_sanitize_data()` de forma recursiva para eliminar cualquier token o credencial sin romper la estructura JSON ni corromper delimitadores de comillas.
  - `export_panel_bundle()`: copia recursivamente los activos del panel PWA y genera `registry.json` y los archivos individuales en `projects/<id>.json`, saneando todas las propiedades de catálogo (`name`, `objective`, `repo_url`, `deploy_url`).

### `AC-4.2`: Configuración Fail-Closed de CLI (`src/agent_factory/cli.py`)
- **Carga y validación estricta (`load_cli_config`, `H42-01`):**
  - Exige obligatoriamente `TELEGRAM_BOT_TOKEN`, `ALLOWED_CHAT_IDS` y `ALLOWED_USER_IDS` (`INV-21`).
  - Documentación sincronizada en `.env.example` con `ALLOWED_CHAT_IDS=`, `ALLOWED_USER_IDS=` y `LLM_API_KEY=`, habiendo purgado la variable obsoleta singular.
  - Parsea listas de IDs separadas por coma a conjuntos inmutables `frozenset[int]`.
  - Valida mediante `math.isfinite()` que `AGENT_FACTORY_POLL_TIMEOUT > 0`, `AGENT_FACTORY_BACKOFF_INITIAL > 0` y `AGENT_FACTORY_BACKOFF_INITIAL <= AGENT_FACTORY_BACKOFF_MAX`, rechazando estrictamente `nan`, `inf`, `-inf`, cero y números negativos.
  - Lanza `CliConfigError` fail-closed ante cualquier parámetro faltante o inválido.
  - Valida exhaustivamente `models.toml` en `build_runtime()`: rechaza archivos vacíos, ausencia de sección `[models]` o claves de modelo válidas, y valores con marcador `"CONFIGURE_ME"` (case-insensitive y stripped).
  - Configura `deploy_base_url` a `https://<github_owner>.github.io` únicamente si el target es `gh-pages` o `github-pages`, evitando asociar URLs de GitHub Pages cuando se despliega hacia Vercel (`H42-01`).
  - **Protección de secretos (`INV-07`, `INV-19`, `H4-06`, `H4-09`, `H41-03`, `H42-05`):** el valor de `TELEGRAM_BOT_TOKEN` nunca se incluye ni se filtra en mensajes de excepción; se limpian explícitamente `__cause__ = None` y `__context__ = None` en errores de parsing de IDs, timeouts y backoff. En `secrets.py`, se ampliaron patrones para sanitizar `x-access-token:`, `?key=`, `LLM_API_KEY=` y tokens Vercel (`vercel_`, `vcp_`).

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
  - `push_branch()` (`H42-03`, `H43-03`): si el workspace contiene `.git`, ejecuta `git add -A` y revisa `git diff --cached --quiet`; si hay cambios, ejecuta commit pasando `-c user.name=AgentFactory -c user.email=agent@factory.local` si no existe identidad local configurada, valida `commit_res.exit_code != 0` lanzando `VcsError` sanitizado ante fallos de commit, y finalmente ejecuta `git push`.
  - `merge_and_deploy()` (`H4-02`, `H42-03`): crea PR con `gh pr create` antes de mergear con `gh pr merge`, y delega el despliegue a `deploy_publisher.deploy_site(project_id, ws_dir)` respetando la rama de publicación configurada en el publisher (`gh-pages`) sin sobreescribirla con la rama de trabajo (`work`).
  - Valida preventivamente `project_id` y nombres de rama (rechaza inyección de flags como `-flag`, espacios, dos puntos, barras invertidas o traversals) antes de llamar a Runner (`INV-22`).
  - Limpia `__cause__` y `__context__` en `VcsError` y auto-incluye `github_token` en la lista de secretos a redactar.
- **`StaticDeployPublisher` (`src/agent_factory/adapters/static_deploy_publisher.py`):**
  - Implementa despliegue de sitios estáticos (GitHub Pages / Vercel) mediante `Runner`.
  - `build_clean_env()`: limpia variables de entorno y expone únicamente `VERCEL_TOKEN`/`GITHUB_TOKEN`.
  - `deploy_site()` (`H4-03`, `H42-03`): valida preventivamente ramas protegidas insensible a mayúsculas/minúsculas (`MAIN`, `master`), ramas remotas (`origin/main`), referencias (`refs/heads/MAIN`), caracteres no válidos (`:`, `\`) y sufijos (`/main`, `/master`). Enruta según `target`: ejecuta `vercel deploy <dir> --yes` pasando el token estrictamente por `VERCEL_TOKEN` en el entorno (sin flag `--token` en argv) o `gh pages deploy <dir> --branch <branch>` para GitHub Pages.
  - Limpia `__cause__` y `__context__` en `StaticDeployError` y auto-incluye `deploy_token` en los secretos a redactar.
- **`GitRegistryPublisher` (`src/agent_factory/adapters/git_registry_publisher.py`):**
  - `_ensure_repository_ready()` (`H42-04`): detecta rebases conflictivos previos (`.git/rebase-merge`, `.git/rebase-apply`, `REBASE_HEAD`) y ejecuta `git rebase --abort` y `git reset --hard HEAD` **antes** del checkout para evitar bloqueos por conflictos pendientes en el índice.
  - `publish_project()`: soporta parámetro `events: Sequence[Event] = ()` serializando el historial saneado en `projects/<id>.json`.
- **`UrllibHttpTransport` y `ProductionLLMAdapter` (`src/agent_factory/adapters/telegram_client.py` y `cli.py`):**
  - `UrllibHttpTransport.post_json()` (`H42-02`, `M42-11`, `M43-14`): transmite encabezados HTTP (`Authorization: Bearer`), acepta parámetro opcional `extra_secrets: Sequence[str] = ()`, sanea errores de transporte `urllib`/`http`/`OSError` como `TelegramTransportError` y limpia `__cause__` y `__context__`.
  - `ProductionLLMAdapter.complete()` (`H42-01`, `M42-10`): lee nombres de modelos directamente desde `[models]` en `config/models.toml` (sin hardcoding ni fallback a `"model-fast-01"` cuando `models_config_path is None`), enruta al modelo fuerte para `MEDIATION`/`Plan` y rápido para `INTERVIEW`/`DEBATE`, incluye instrucciones de esquema JSON cuando se solicita y falla de forma fail-closed con `LLMError` si la configuración es corrupta o si la respuesta no es JSON válido.
- **`Orchestrator` (`src/agent_factory/orchestrator.py`):**
  - `request_adjustment()` (`H43-01`, `INV-17`): nunca reinicia los contadores acumulados del `CircuitBreaker` al solicitar ajustes de plan desde `WAITING_APPROVAL` hacia `INTERVIEW`.
  - `get_status()` (`H43-02`, `INV-21`): publicación en registro remoto es de mejor esfuerzo (envuelta en bloque try/except capturando `RegistryPublishError`, `OSError`, `RuntimeError`, `ValueError`), garantizando que la creación y consulta de estado nunca fallen ante caídas del servidor de registro git remoto.

### `AC-4.5`: Cierre de Observaciones Residuales de Auditoría (`H4-01` a `H4-12`, `H41-01` a `H41-04`, `H42-01` a `H42-05`, `H43-01` a `H43-04`, `M43-14`)
- **`H43-01` (`INV-17`):** Eliminado `circuit_breaker.reset()` en `request_adjustment()`. Los presupuestos de invocación LLM son acumulativos a lo largo de todo el ciclo de vida del proyecto.
- **`H42-01` y `M42-10` KILLED:** `ProductionLLMAdapter` exige `models_config_path` no nulo, lee dinámicamente `config/models.toml`, realiza routing según el estado del contexto (`MEDIATION` usa `strong`), exige JSON estructurado y falla si el TOML está corrupto o vacío. `build_runtime` valida `models.toml` exhaustivamente y pasa `config.github_owner` a `StaticDeployPublisher` como `deploy_base_url` únicamente en despliegues GitHub Pages.
- **`H43-02`:** `Orchestrator.get_status()` captura errores de publicación en registro remoto protegiendo `create_project()` y consultas de estado.
- **`H43-03`:** `GitHubVCSAdapter.push_branch()` pasa identidad git defensiva (`-c user.name=AgentFactory -c user.email=agent@factory.local`) en entornos limpios y valida fallos de `git commit` elevando `VcsError` sanitizado.
- **`M43-14` KILLED:** `UrllibHttpTransport.post_json()` soporta y redacta `extra_secrets` junto con tokens de cabecera en `TelegramTransportError`.
- **`H43-04`:** `panel/app.js` mantiene la palabra `"NaN"` en textos no numéricos mientras garantiza normalización a cero de métricas no finitas mediante `toFiniteNonNegativeInt()`.

### `AC-4.6`: Pruebas Operativas de Extremo a Extremo Offline (`tests/test_phase4_e2e_offline.py`)
- **Flujo E2E completo verificado offline:**
  - Configuración CLI desde variables de entorno -> ensamblado de runtime en `build_runtime()` -> ejecución en loop de polling -> comando `/new Clima Viva` -> debate multi-agente -> publicación intermedia en registro -> aprobación vía callback inline -> construcción mediante Agy CLI (`static-web`) -> publicación final en rama `registry` con array completo de eventos saneados -> entrega y notificación por Telegram con enlaces de despliegue y repositorio, sin ninguna fuga de tokens.
  - Verificación negativa de configuración inválida, recuperación de rebases y backoff exponencial ante fallos transitorios de red.

---

## 2. Verificación de Invariantes del Sistema

| Invariante | Descripción | Estado | Evidencia |
|---|---|:---:|---|
| **`INV-01`** | Dependencias mínimas y con versión fija | **CUMPLE** | Sin cambios en `pyproject.toml`; dependencias fijas e intactas. |
| **`INV-02`** | Todo acceso externo detrás de un puerto | **CUMPLE** | `GitHubVCSAdapter` y `StaticDeployPublisher` delegan en `Runner`; Telegram y LLM operan por `HttpTransport`. |
| **`INV-03`** | Tests 100% offline y sin secretos | **CUMPLE** | `pytest-socket` activo (`--disable-socket`); 341 pruebas pasan sin socket de red. |
| **`INV-04`** | Transiciones de estado deterministas | **CUMPLE** | Todas las transiciones validadas mediante `validate_transition()`. |
| **`INV-06`** | Rate limits y circuit breaker | **CUMPLE** | Circuit breaker central protege todas las invocaciones LLM respetando `config/limits.toml` sin reinicio en reentradas (`H43-01`). |
| **`INV-07` / `INV-19`** | Cero filtración de secretos en errores/logs | **CUMPLE** | Tokens enmascarados con `[REDACTED]` en excepciones, eventos, bundle PWA, Telegram y `__cause__`/`__context__`. |
| **`INV-11`** | Estado derivado de eventos | **CUMPLE** | `project_status_from_events` reconstruye el estado del proyecto sin mutaciones ad-hoc. |
| **`INV-13`** | Prohibido alterar tests o configuración | **CUMPLE** | `git diff phase-4.4-tests --stat -- tests/ templates/ .github/ pyproject.toml` es **completamente vacío**. |
| **`INV-16`** | Panel PWA sin backend | **CUMPLE** | Panel en `panel/` 100% estático (HTML/CSS/JS + Service Worker) consumiendo `registry.json`. |
| **`INV-17`** | Presupuestos acumulativos inviolables | **CUMPLE** | `request_adjustment()` no reinicia contadores; breaker acumula llamadas a través de múltiples rondas de debate/ajuste. |
| **`INV-21`** | Configuración fail-closed | **CUMPLE** | `load_cli_config()` y `build_runtime()` abortan limpiamente ante whitelists vacías, números inválidos (`nan`/`inf`), `models.toml` corrupto/vacío/`CONFIGURE_ME` o tokens ausentes. |
| **`INV-22`** | Validación estricta de entradas externas | **CUMPLE** | Validación regex de `project_id` y ramas en CLI, adaptadores VCS, deploy y PWA. |
| **`INV-23`** | Oráculo de prueba independiente | **CUMPLE** | Todos los valores esperados en pruebas están declarados a mano sin depender del código bajo prueba; todas las mutaciones analizadas resultan eliminadas. |

---

## 3. Métricas de Verificación

### Análisis Estático de Código (`ruff`)
```
.\.venv\Scripts\ruff.exe check .
All checks passed!
```

### Integridad de Rutas Protegidas (`INV-13`)
```
git diff phase-4.4-tests --stat -- tests/ templates/ .github/ pyproject.toml
(salida vacía - 0 líneas modificadas en tests, templates, .github o pyproject.toml)
```

### Ejecución Completa de Pruebas y Cobertura (`pytest`)
- **Total de pruebas:** **341 pasadas** (334 previas + 7 nuevos tests quirúrgicos), 0 falladas, 0 omitidas.
- **Tiempo de ejecución:** ~125s (100% offline).
- **Cobertura global (`src/agent_factory`):** **91%**

#### Desglose de Cobertura por Módulo

| Módulo | Sentencias | Fallas | Ramas | Ramas Parciales | Cobertura |
|---|---:|---:|---:|---:|---:|
| **TOTAL (`src/agent_factory`)** | **3055** | **208** | **870** | **117** | **91%** |
| `__main__.py` | 4 | 0 | 2 | 0 | **100%** |
| `adapters/agy_cli_adapter.py` | 104 | 0 | 28 | 1 | **99%** |
| `adapters/fake_code_agent.py` | 61 | 10 | 26 | 8 | **79%** |
| `adapters/fake_llm.py` | 36 | 7 | 12 | 2 | **77%** |
| `adapters/fake_runner.py` | 25 | 5 | 10 | 4 | **74%** |
| `adapters/fake_telegram.py` | 35 | 2 | 10 | 2 | **91%** |
| `adapters/fake_vcs.py` | 33 | 3 | 6 | 2 | **87%** |
| `adapters/file_publisher.py` | 32 | 1 | 6 | 2 | **92%** |
| `adapters/git_registry_publisher.py` | 237 | 22 | 78 | 12 | **89%** |
| `adapters/github_vcs_adapter.py` | 170 | 15 | 48 | 5 | **91%** |
| `adapters/memory_store.py` | 32 | 0 | 6 | 0 | **100%** |
| `adapters/sqlite_store.py` | 56 | 0 | 6 | 2 | **97%** |
| `adapters/static_deploy_publisher.py` | 108 | 3 | 34 | 2 | **95%** |
| `adapters/subprocess_runner.py` | 40 | 17 | 4 | 2 | **57%** |
| `adapters/system_clock.py` | 7 | 0 | 0 | 0 | **100%** |
| `adapters/telegram_client.py` | 129 | 2 | 46 | 9 | **94%** |
| `agents/interviewer.py` | 38 | 0 | 8 | 1 | **98%** |
| `agents/mediator.py` | 77 | 2 | 16 | 2 | **96%** |
| `agents/pragmatist.py` | 28 | 0 | 4 | 0 | **100%** |
| `agents/structuralist.py` | 27 | 0 | 4 | 0 | **100%** |
| `builder.py` | 222 | 10 | 56 | 10 | **93%** |
| `cli.py` | 380 | 43 | 126 | 17 | **85%** |
| `domain/events.py` | 29 | 0 | 0 | 0 | **100%** |
| `domain/models.py` | 106 | 0 | 8 | 0 | **100%** |
| `domain/progress.py` | 97 | 2 | 46 | 4 | **96%** |
| `domain/states.py` | 25 | 0 | 2 | 0 | **100%** |
| `inbox.py` | 18 | 0 | 4 | 0 | **100%** |
| `orchestrator.py` | 222 | 16 | 58 | 5 | **91%** |
| `ports/clock.py` | 8 | 2 | 0 | 0 | **75%** |
| `ports/code_agent.py` | 19 | 2 | 0 | 0 | **89%** |
| `ports/llm.py` | 8 | 1 | 0 | 0 | **88%** |
| `ports/notifier.py` | 35 | 4 | 0 | 0 | **89%** |
| `ports/publisher.py` | 36 | 5 | 0 | 0 | **86%** |
| `ports/runner.py` | 15 | 1 | 0 | 0 | **93%** |
| `ports/store.py` | 11 | 3 | 0 | 0 | **73%** |
| `ports/vcs.py` | 17 | 3 | 0 | 0 | **82%** |
| `scaffold.py` | 99 | 3 | 46 | 1 | **97%** |
| `secrets.py` | 30 | 0 | 14 | 1 | **98%** |
| `telegram_bot.py` | 287 | 21 | 104 | 16 | **91%** |
| `workspace.py` | 112 | 3 | 52 | 7 | **94%** |

---

## 4. Supuestos y Decisiones Técnicas Adoptadas
1. **PWA estático desacoplado:** El panel no requiere ningún proceso de servidor propio; consume el catálogo `registry.json` y los detalles por proyecto directamente desde la URL de la rama `registry` o desde un clon local montado como recurso estático.
2. **Entorno limpio para adaptadores de producción:** `GitHubVCSAdapter` y `StaticDeployPublisher` construyen de forma determinista un entorno subprocess filtrando cualquier secreto no autorizado del host (`INV-19`), protegiendo credenciales del sistema central ante herramientas CLI externas.
3. **Resiliencia de publicación en Git Registry:** La recuperación automática de rebases conflictivos previos y divergentes garantiza que actualizaciones concurrentes o ejecuciones offline previas no bloqueen el catálogo de publicación, limpiando de raíz el índice antes de realizar `checkout` y saneando de forma recursiva todos los datos de proyecto e historial de eventos.
4. **Protección multinivel contra fuga de credenciales:** Todos los adaptadores y puntos de configuración limpian activamente `__cause__` y `__context__` en las excepciones lanzadas para evitar que el traceback estándar de Python exponga cadenas con tokens que pudieran existir en excepciones causales subyacentes.
5. **Cero hardcoding de modelos y fail-closed:** `ProductionLLMAdapter` obtiene los nombres de modelo estrictamente desde `config/models.toml` y conmuta dinámicamente entre modelos fuertes y rápidos según el contexto de la fase (`MEDIATION` vs `INTERVIEW`/`DEBATE`), rechazando de raíz configuraciones vacías o respuestas que no respeten el esquema estructurado JSON.
6. **Presupuesto acumulativo e inviolable (`INV-17`):** El circuit breaker no reinicia contadores tras solicitar ajustes (`request_adjustment`), garantizando que bucles infinitos de ajuste/debate no consuman presupuesto de tokens descontroladamente.
