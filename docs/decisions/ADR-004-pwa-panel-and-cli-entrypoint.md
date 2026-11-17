# ADR-004: Panel PWA estático sin backend, Entrypoint CLI continuo y adaptadores VCS/Despliegue

## Estado
Aceptado

## Fecha
2026-11-17

## Contexto
La Fase 4 (Cierre Operativo) completa los componentes pendientes identificados en `MASTER_PROMPT.md` (v1.2.1, §6, §7 Fases 3–4) y cierra las observaciones residuales registradas en `docs/AUDIT_REPORT_3_1.md`:
- **Panel PWA de solo lectura (`MASTER_PROMPT.md:178, 183`):** visualización móvil-primero en GitHub Pages del catálogo `registry.json` y detalle `projects/<id>.json` (incluyendo estado, `%` de avance, invocaciones/rondas CI, `PROJECT_PROMPT.md` / `BuildReport` e historial de eventos sanitizados) sin backend (`INV-16`) ni tokens de escritura en el navegador (`INV-19`).
- **Entrypoint continuo `python -m agent_factory` (`INV-02`, `INV-04`, `INV-06`, `INV-19`, `INV-21`):** carga de configuración *fail-closed* de `TELEGRAM_BOT_TOKEN`, `ALLOWED_CHAT_IDS` y `ALLOWED_USER_IDS`, ensamblaje de dependencias reales (`SqliteEventStore`, `TelegramHttpClient`, `TelegramBotService`, `GitRegistryPublisher`, `BuildWorker`, `AgyCliAdapter`, adaptadores VCS/Despliegue) y ciclo de *long polling* con backoff exponencial acotado ante fallos transitorios y apagado limpio ante `SIGINT`/`SIGTERM`.
- **Adaptadores reales detrás de puertos (`INV-02`, `INV-07`, `INV-19`, `INV-22`):** implementación de `GitHubVCSAdapter` (sobre el puerto `VCS`) y `StaticDeployPublisher` (publicación/despliegue estático vía `Runner`), ejecutando subprocesos únicamente con entorno limpio y redacción estricta de secretos (`GITHUB_TOKEN`, `VERCEL_TOKEN`).
- **Dependencias mínimas (`INV-08`):** sin agregar librerías nuevas en `pyproject.toml`; la suite continúa corriendo `100 %` offline (`pytest-socket`, `INV-03`). Dado que el entorno CI de Python (`.github/workflows/ci.yml`) no instala Node.js de forma predeterminada, el Panel PWA se diseña en HTML/CSS/JS estándar sin paso de compilación y con validación estructural/negativa portátil desde `pytest` (y compatible con `node --test` cuando esté disponible).

---

## Decisión

### 1. Panel PWA estático (`panel/`)
Se crea una aplicación web progresiva 100 % estática bajo `panel/`:
- **`panel/index.html`, `panel/styles.css`, `panel/app.js`, `panel/manifest.webmanifest`, `panel/sw.js`, `panel/icons/icon.svg`.**
- **Lectura segura y configurable:** `app.js` acepta una ruta/URL configurable de origen de datos (`?registryBase=...` o almacenamiento local de preferencia de lectura), restringida a rutas relativas o URLs HTTP(S) de solo lectura (`GET`), sin aceptar esquemas `javascript:` ni almacenar tokens o headers de autorización (`INV-19`).
- **Validación y sanitización defensiva:** valida cada `project_id` contra `^[a-z0-9][a-z0-9-]{0,62}$` (`INV-22`), escapa HTML antes de renderizar, tolera JSON inválido o incompleto con banner de error controlado y aplica un filtro defensivo de secretos en texto visible.
- **Historial de eventos sanitizados en `projects/<id>.json`:** `GitRegistryPublisher.publish_project` admitirá opcionalmente `events` (secuencia de `Event`) serializados en forma de resumen sanitizado (`type`, `at`, `summary`), manteniendo total compatibilidad hacia atrás con el esquema de `ProjectStatus`.
- **Modo offline:** `sw.js` precachea la cáscara de la aplicación y aplica *network-first with cache fallback* exclusivamente para peticiones `GET` de `registry.json` y `projects/*.json`.

### 2. Punto de entrada CLI (`src/agent_factory/__main__.py` y `src/agent_factory/cli.py`)
- **`CliConfig` y `load_cli_config(env)`:** validan de forma estricta (`CliConfigError`, subclase de `ValueError`) que existan `TELEGRAM_BOT_TOKEN` no vacío, `ALLOWED_CHAT_IDS` y `ALLOWED_USER_IDS` no vacíos ni malformados, rechazando listas blancas vacías (*fail-closed*, `INV-21`). Ningún mensaje de excepción incluye el valor de tokens (`INV-19`).
- **`build_runtime(config, ...)`:** ensambla `SqliteEventStore`, `Orchestrator`, `WorkspaceManager`, `TemplateScaffold`, `AgyCliAdapter`, `GitHubVCSAdapter` / `StaticDeployPublisher`, `GitRegistryPublisher`, `BuildWorker`, `TelegramHttpClient` y `TelegramBotService`, aceptando inyección opcional de puertos para pruebas offline (`INV-02`, `INV-03`). No fija nombres de modelos en código (`AGENTS.md` #12; lee `config/models.toml` y `config/limits.toml`).
- **`run_polling_loop(...)` y `install_signal_handlers(stop_event)`:** iteran `bot_service.poll_once()` hasta que `stop_event` se activa (`SIGINT`/`SIGTERM`) o se alcanza `max_iterations`. Ante `TelegramTransportError`, aplican backoff exponencial acotado (`min(initial * 2**(failures - 1), max_backoff)`) mediante `clock.sleep(...)` y reinician el backoff tras un poll exitoso.

### 3. Adaptadores reales de VCS y Despliegue (`src/agent_factory/adapters/`)
- **`GitHubVCSAdapter` (`github_vcs_adapter.py`):** implementa el protocolo `VCS` (`push_branch`, `get_ci_result`, `merge_and_deploy`) delegando comandos `git` / `gh` al puerto `Runner`. Valida `project_id` (`INV-22`), nombres de rama seguros (sin `-`, espacios ni `..`) y contención del workspace (`INV-18`), construye un entorno limpio sin filtrar secretos ajenos del host (`TELEGRAM_BOT_TOKEN`, `LLM_API_KEY`, `INV-19`), y envuelve cualquier fallo en `VcsError` (subclase de `RuntimeError`) con `redact_secrets`.
- **`StaticDeployPublisher` (`static_deploy_publisher.py`):** implementa `deploy_site(project_id, source_dir, branch=...)` delegando el despliegue estático (GitHub Pages / Vercel CLI) al puerto `Runner` con entorno limpio (`INV-19`), validación de `project_id` y rutas (`INV-22`), y redacción de secretos en `StaticDeployError` (`RuntimeError`).

### 4. Cierre de observaciones de `docs/AUDIT_REPORT_3_1.md` (`AC-4.5`)
- `GitRegistryPublisher._ensure_repository_ready`: cuando `git rebase FETCH_HEAD` falla por conflicto, ejecuta `git rebase --abort` antes de propagar/recuperar vía `git reset --hard FETCH_HEAD`, evitando dejar `work_dir` atascado permanentemente en rebase en curso (`H31-02`).
- `TelegramBotService._handle_new`: registra el proyecto activo del chat únicamente después de ejecutar `handle_interview_message` sin error terminal, preservando el proyecto activo anterior si `Interviewer` falla (`H31-03`), además de exigir coincidencia de palabra completa para `/new`, `/status`, `/estado` y `/resume`.
- `secrets.redact_secrets`: añade redacción automática para parámetros de consulta sensibles (`?token=...`, `?access_token=...`, `?api_key=...`, `?secret=...`, `?password=...`) aun sin `env` ni `extra_secrets` (`H31-05`).

---

## Consecuencias
- **Positivas:** Operación continua real desacoplada detrás de puertos, visibilidad web estática instalable sin costos ni servidores (`$0`), cierre completo de hallazgos de la Fase 3 y pruebas `100 %` portátiles sin Node.js ni red.
- **Compromisos:** La integración real contra `api.github.com` / `vercel` y `api.telegram.org` sigue fuera de la suite automática (`INV-03`), quedando cubierta mediante contratos deterministas sobre `Runner` y `HttpTransport`.
