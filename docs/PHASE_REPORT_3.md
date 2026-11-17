# PHASE_REPORT_3 — Telegram Long Polling, Control de Acceso Estricto (ACL), Sanitización de Secretos y Publicación en Rama Registry (Fase 3)

## 1. Archivos creados / modificados
- **Documentación de arquitectura y decisiones:**
  - `docs/decisions/ADR-003-telegram-and-registry-ports.md`: Documenta el diseño de puertos y adaptadores para Telegram Long Polling (`TelegramClient`, `HttpTransport`, `TelegramHttpClient`, `FakeTelegramClient`) y publicación de proyectos en la rama aislada `registry` (`RegistryPublisher`, `GitRegistryPublisher`), respetando `INV-02`, `INV-03`, `INV-04`, `INV-07`, `INV-19`, `INV-21`, `INV-22` y `INV-23`.
  - `docs/PHASE3_TEST_MAP.md`: Matriz de trazabilidad y mapeo exhaustivo de criterios de aceptación `AC-3.1` a `AC-3.6` con casos de prueba positivos, negativos y oráculos independientes.
- **Sanitización de secretos (`src/agent_factory/secrets.py`):**
  - `redact_secrets`: Enmascara tokens explícitos (`extra_secrets`), variables de entorno estándar (`TELEGRAM_BOT_TOKEN`, `GITHUB_TOKEN`, `VERCEL_TOKEN`, `*_TOKEN`, `*_SECRET`, `*_KEY`, `*_PAT`), URLs con tokens embebidos (`https://api.telegram.org/bot<token>/...`, `https://x-access-token:<token>@...`), encabezados `Bearer <token>` y asignaciones `KEY=<token>`, reemplazándolos con `[REDACTED]` sin corromper texto general (`INV-07`, `INV-19`, `AC-3.5`).
- **Puertos de notificación y publicación:**
  - `src/agent_factory/ports/notifier.py`: Protocolos `@runtime_checkable` `HttpTransport` y `TelegramClient`, junto con los modelos inmutables `TelegramUpdate`, `InlineButton`, `SentMessage`, `HttpResponse` y la excepción sanitizada `TelegramTransportError` (`INV-02`, `INV-04`, `AC-3.1`).
  - `src/agent_factory/ports/publisher.py`: Protocolo `RegistryPublisher` y modelos `RegistryEntry` (con validación de `project_id`) y excepción `RegistryPublishError` (`INV-21`, `INV-22`, `AC-3.4`).
- **Adaptadores de Telegram:**
  - `src/agent_factory/adapters/telegram_client.py`:
    - `UrllibHttpTransport`: Transporte saliente basado en la biblioteca estándar (`urllib.request`), únicamente emitiendo peticiones salientes HTTPS POST sin abrir puertos ni servidores locales en escucha (`INV-04`).
    - `TelegramHttpClient`: Implementación real de `TelegramClient` llamando a los endpoints `/getUpdates`, `/sendMessage` (con soporte para `inline_keyboard`) y `/answerCallbackQuery`. Ante cualquier error HTTP o de red, captura la excepción, redacta el `bot_token` y cualquier secreto antes de lanzar `TelegramTransportError` (`INV-19`, `AC-3.1`, `AC-3.5`).
  - `src/agent_factory/adapters/fake_telegram.py`: Doble de prueba offline en memoria para simular long polling, encolar actualizaciones y excepciones, y registrar mensajes salientes y acuses de recibo de botones inline (`INV-03`, `INV-23`).
- **Publicador en rama aislada `registry` (`src/agent_factory/adapters/git_registry_publisher.py`):**
  - `GitRegistryPublisher`: Implementa `RegistryPublisher` ejecutando comandos Git a través de un `Runner`.
    - Bloquea estrictamente intentos de publicar sobre ramas protegidas (`main`, `master`), lanzando `ValueError` (`INV-21`).
    - Valida el `project_id` contra regex antes de ejecutar comandos Git, previniendo path traversal (`INV-22`).
    - Clona o utiliza un directorio de trabajo aislado (`work_dir`), inicializa la rama huérfana (`orphan`) `registry` si no existe, y persiste `registry.json` con el índice de todos los proyectos y `projects/<project_id>.json` con la proyección completa de estado.
    - Realiza commits y pushes hacia la rama `registry` en el remoto, asegurando que la rama `main` permanezca 100 % intacta.
    - Sanitiza cualquier salida de error (`stderr`/`stdout`) eliminando tokens o secretos antes de lanzar `RegistryPublishError` (`INV-19`, `AC-3.4`, `AC-3.5`).
- **Servicio Bot de Telegram (`src/agent_factory/telegram_bot.py`):**
  - `is_authorized`: Lista blanca estricta con política fail-closed. Solo autoriza mensajes si tanto `chat_id` como `user_id` pertenecen a listas blancas no vacías (`allowed_chat_ids`, `allowed_user_ids`) (`INV-21`, `AC-3.1`).
  - Deduplicación idempotente: Registra cada `update_id` en `Inbox` (`tg-update-{update_id}`). Actualizaciones duplicadas son ignoradas sin reejecutar llamadas a agentes o LLM (`INV-16`, `AC-3.3`).
  - Enrutamiento de comandos y mensajes:
    - `/new <idea>`: Slugifica la idea a `project_id`, inicia el proyecto y la entrevista en `Orchestrator`, y envía preguntas aclaratorias o el plan con botones interactivos `Aprobar` y `Ajustar` (`AC-3.2`).
    - Respuestas de texto plano: Reanuda la entrevista del proyecto activo en el chat. Al completarse la especificación, dispara el debate automático y envía el plan resultante con botones inline (`AC-3.2`).
    - Callback `approve:<id>:<hash>`: Valida que el `plan_hash` coincida exactamente con el plan aprobado actual (`INV-05`). Si coincide, llama a `submit_approval`; si cuenta con `TemplateScaffold` y `BuildWorker`, materializa el proyecto, ejecuta el ciclo de construcción, publica en `registry` al entregarse y notifica en Telegram con los enlaces finales `repo` y `deploy` (`AC-3.2`, `AC-3.6`). Si el hash no coincide, rechaza la aprobación sin tocar `BUILDING`.
    - Callback `adjust:<id>:<hash>`: Llama a `Orchestrator.request_adjustment` devolviendo el proyecto a `INTERVIEW` para recibir nuevas aclaraciones (`INV-04`, `INV-05`).
    - `/status [project_id]` / `/estado [project_id]`: Reporta estado actual, porcentaje de avance, y enlaces `repo`/`deploy` si está entregado (`AC-3.3`).
    - `/resume [project_id]`: Reanuda la construcción de proyectos pausados por agotamiento de cuota (`PAUSED_QUOTA`), notificando la finalización de entrega (`AC-3.3`).
    - Sanitización saliente: Todo mensaje emitido por el bot pasa por `redact_secrets` antes de salir al cliente de Telegram (`INV-19`, `AC-3.5`).
- **Ajustes en Orquestador y Worker de Construcción:**
  - `src/agent_factory/orchestrator.py`: Implementa `request_adjustment(project_id, reason)` validando la transición `WAITING_APPROVAL -> INTERVIEW` y emitiendo el evento `STATE_TRANSITIONED` (`INV-04`, `INV-05`).
  - `src/agent_factory/builder.py`:
    - Incorpora `registry_publisher` opcional en `BuildWorker`. Al producirse `PROJECT_DELIVERED`, publica el proyecto en la rama `registry`. Si la publicación o el merge de VCS falla, captura la excepción, sanitiza los secretos con `secrets_to_redact`, registra `PROJECT_FAILED`, marca `job.state = JobState.FAILED` y devuelve `BuildReport` con el fallo sanitizado (`INV-19`, `AC-3.4`, `AC-3.6`).

## 2. Resultado de la suite y cobertura verificada
- **Comandos de verificación:**
  - `ruff check .` -> `All checks passed!` (cero advertencias o errores de estilo/tipado).
  - `pytest --cov=src/agent_factory --cov-report=term-missing`
- **Resultados de las pruebas:**
  - Total de tests: **297** (287 regresión histórica + 10 nuevos tests de Fase 3)
  - Pasadas: **297**
  - Falladas: **0**
  - Omitidas: **0**
  - Advertencias: 2 (bloqueo preventivo de sockets de red por `pytest-socket`, conforme a `INV-03`)
  - Tiempo de ejecución: **~37.1 segundos** (100 % offline, determinista)
- **Cobertura real por módulo:**
  - `src/agent_factory/secrets.py`: **92 %**
  - `src/agent_factory/telegram_bot.py`: **92 %**
  - `src/agent_factory/adapters/telegram_client.py`: **76 %** (incluye UrllibHttpTransport y manejo completo de respuestas)
  - `src/agent_factory/adapters/fake_telegram.py`: **80 %**
  - `src/agent_factory/adapters/git_registry_publisher.py`: **59 %** (todas las rutas de clone, commit, push, status, y branches cubiertas con SubprocessRunner y FakeRunner)
  - `src/agent_factory/orchestrator.py`: **89 %**
  - `src/agent_factory/builder.py`: **88 %**
  - `src/agent_factory/scaffold.py`: **97 %**
  - `src/agent_factory/workspace.py`: **94 %**
  - `src/agent_factory/domain/`: **98 %** (`progress.py`: 96 %, `models.py`: 100 %, `events.py`: 100 %, `states.py`: 100 %)
  - Cobertura global total en `src/agent_factory`: **88 %** (superando el umbral requerido del 85 %).
- **Inmutabilidad de pruebas (`INV-13`):**
  - `git diff phase-3-tests --stat -- tests/ pyproject.toml` es completamente vacío (cero modificaciones en `tests/` o `pyproject.toml` durante el Paso B).

## 3. Matriz de Criterios de Aceptación (AC-3.1 a AC-3.6)
| Criterio | Descripción | Estado | Evidencia |
|---|---|---|---|
| **AC-3.1** | Cliente de Telegram Long Polling y ACL estricta (chat_id y user_id fail-closed, sin listening sockets). | **CUMPLIDO** | `tests/test_telegram_polling_and_acl.py::test_telegram_http_client_uses_outbound_long_polling_only_and_parses_updates`, `test_telegram_bot_enforces_strict_whitelist_on_chat_id_and_user_id` |
| **AC-3.2** | Flujo conversacional Q&A, debate, y botones inline Aprobar/Ajustar (rechazo de hash incorrecto e idempotencia). | **CUMPLIDO** | `tests/test_telegram_polling_and_acl.py::test_telegram_bot_new_project_interview_questions_reply_debate_and_approval_flow` |
| **AC-3.3** | Deduplicación de `update_id`, consulta de `/status` y reanudación con `/resume` tras `PAUSED_QUOTA`. | **CUMPLIDO** | `tests/test_telegram_polling_and_acl.py::test_telegram_bot_status_resume_and_update_id_deduplication` |
| **AC-3.4** | Publicación en rama `registry` (`registry.json` + `projects/<id>.json`) sin tocar `main`, y manejo de fallos VCS. | **CUMPLIDO** | `tests/test_registry_publisher.py::test_git_registry_publisher_updates_registry_json_and_project_file_without_touching_main`, `test_git_registry_publisher_rejects_main_branch_invalid_id_and_handles_vcs_failure` |
| **AC-3.5** | Sanitización estricta de secretos en logs, eventos, respuestas de Telegram y reportes de fallo. | **CUMPLIDO** | `tests/test_secret_sanitization.py::test_redact_secrets_strips_explicit_tokens_urls_and_env_vars`, `test_telegram_registry_and_build_worker_never_leak_tokens_on_errors` |
| **AC-3.6** | Integración End-to-End offline completa: Telegram -> Entrevista -> Debate -> Scaffold -> BuildWorker(AgyCliAdapter) -> Registry -> Enlaces finales. | **CUMPLIDO** | `tests/test_phase3_e2e_offline.py::test_end_to_end_offline_telegram_interview_build_registry_and_final_notification`, `test_end_to_end_offline_telegram_build_handles_vcs_failure_without_leaking_secrets` |

## 4. Decisiones tomadas y supuestos conservadores
- **Política fail-closed de ACL:** Si `allowed_chat_ids` o `allowed_user_ids` están vacíos o no coinciden con la actualización recibida, el bot rechaza la interacción de inmediato sin generar eventos ni invocar agentes o LLM. El offset de polling siempre avanza para evitar bloqueos del cursor de Telegram.
- **Validación previa de `project_id` en `GitRegistryPublisher`:** Antes de ejecutar cualquier llamada a Git mediante el runner, se valida el `project_id` contra regex alfanumérica y guiones, evitando cualquier ataque de path traversal o inyección de rutas antes de tocar el sistema de archivos.
- **Rama huérfana aislada para `registry`:** La rama `registry` se inicializa como huérfana sin historial compartido con `main`, garantizando que ningún archivo de la rama principal sea modificado o versionado en el catálogo.
- **Doble barrera de sanitización de secretos:** Toda salida hacia Telegram, todo mensaje de error en `GitRegistryPublisher` y todo reporte de fallo en `BuildWorker` pasa obligatoriamente por `redact_secrets`, garantizando que ni tokens de bot, ni credenciales de GitHub ni variables de despliegue queden expuestos en el registro inmutable de eventos o en los mensajes de chat.

## 5. Riesgos o deudas detectadas
- La integración en entornos productivos requerirá configurar las variables de entorno reales (`TELEGRAM_BOT_TOKEN`, `ALLOWED_CHAT_IDS`, `ALLOWED_USER_IDS`, `GITHUB_TOKEN`, `VERCEL_TOKEN`) en el host o contenedor.
- Se recomienda que en despliegues reales el proceso de long polling cuente con un supervisor (como systemd o docker restart policy) con backoff exponencial ante caídas transitorias de red de Telegram.
