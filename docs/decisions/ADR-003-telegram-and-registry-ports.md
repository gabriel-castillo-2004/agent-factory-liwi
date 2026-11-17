# ADR-003: Puertos y adaptadores para Telegram Long Polling y publicación en rama `registry`

> Nota de numeración: El repositorio ya contiene `ADR-003-pydantic.md` de la Fase 0; este documento conserva el identificador solicitado para la Fase 3 (`ADR-003-telegram-and-registry-ports.md`).

## Estado
Aceptado

## Fecha
2026-11-16

## Contexto
La Fase 3 de `MASTER_PROMPT.md` (v1.2.1, §6 y §7) incorpora la interfaz conversacional móvil con Gabriel y el registro público de proyectos entregados sin infraestructura de servidor ni costos:
- **INV-02 / INV-03:** Toda comunicación con Telegram, GitHub/Git remoto y despliegue estático vive detrás de puertos explícitos y se prueba 100 % offline (`pytest-socket` activo).
- **INV-04 (Transporte y máquina de estados):** Telegram opera exclusivamente mediante *long polling* saliente (`getUpdates` / `sendMessage`), sin webhooks, servidores HTTP entrantes ni puertos abiertos; toda transición de estado pasa por `validate_transition`.
- **INV-05:** La transición a `BUILDING` exige aprobación humana válida vinculada al `plan_hash` vigente (vía botón inline `Aprobar` / confirmación explícita); `Ajustar` nunca abre `BUILDING`.
- **INV-07 / INV-19:** Higiene estricta de secretos; `TELEGRAM_BOT_TOKEN`, `GITHUB_TOKEN`, `VERCEL_TOKEN` y credenciales nunca aparecen en mensajes de Telegram, eventos del `EventStore`, `BuildReport` ni excepciones.
- **INV-16 / INV-21:** Control de acceso estricto por lista blanca (`allowed_chat_ids` y `allowed_user_ids` fail-closed), deduplicación por `offset` (`last_update_id + 1`) + `Inbox`, y publicación del estado/registro fuera de `main`, en archivos por proyecto e índice `registry.json` sobre la rama `registry`.
- **INV-22:** Validación estricta de `project_id` y rutas ante entradas provenientes de comandos o callbacks de Telegram.

## Decisión

### 1. Puerto de Telegram y transporte HTTP inyectable (`src/agent_factory/ports/notifier.py`)
Se definen modelos Pydantic (`InlineButton`, `TelegramUpdate`, `SentMessage`, `HttpResponse`), la excepción `TelegramTransportError` y dos protocolos:
- **`HttpTransport`:** abstracción mínima de transporte saliente (`post_json(url, payload, timeout_seconds) -> HttpResponse`), implementada en producción con librería estándar (`urllib.request` en `UrllibHttpTransport`) sin agregar dependencias externas (`INV-08`).
- **`TelegramClient`:** protocolo de alto nivel con `get_updates(offset, timeout)`, `send_message(chat_id, text, buttons)` y `answer_callback_query(callback_query_id, text)`.
- **Adaptadores:**
  - `TelegramHttpClient` (`src/agent_factory/adapters/telegram_client.py`): consume únicamente `/getUpdates`, `/sendMessage` y `/answerCallbackQuery` sobre `HttpTransport`; nunca abre sockets de escucha y sanitiza `bot_token` ante cualquier error de transporte.
  - `FakeTelegramClient` (`src/agent_factory/adapters/fake_telegram.py`): doble determinista en memoria para pruebas offline.

### 2. Controlador conversacional (`src/agent_factory/telegram_bot.py`)
`TelegramBotService` coordina `TelegramClient`, `Orchestrator`, `Inbox`, y opcionalmente `TemplateScaffold`, `BuildWorker` y `RegistryPublisher`:
- **Lista blanca fail-closed (`is_authorized`):** exige coincidencia de `chat_id` en `allowed_chat_ids` y `user_id` en `allowed_user_ids` (listas vacías rechazan todo). Mensajes no autorizados se ignoran/rechazan sin tocar `EventStore`, `Orchestrator`, `LLM` ni `BuildWorker`, aunque avanzan `next_offset` para no bloquear el poller.
- **Deduplicación (`INV-16`):** combina avance de `offset = max_update_id + 1` con registro previo en `Inbox` (`tg-update-<update_id>`).
- **Comandos y callbacks:**
  - `/new <idea>` (o texto libre sin pregunta pendiente): deriva un `project_id` válido (`INV-22`), crea el proyecto, inicia `INTERVIEW` y envía las preguntas pendientes o avanza al debate.
  - Respuesta libre cuando el proyecto activo espera aclaración en `INTERVIEW`: enruta a `handle_interview_message`; al completarse `Spec`, ejecuta `run_debate` y envía el resumen del `Plan` con botones inline `Aprobar` (`approve:<id>:<hash>`) y `Ajustar` (`adjust:<id>:<hash>`).
  - Callback `Aprobar` (y `/approve`): llama `submit_approval`; si hay `TemplateScaffold`/`BuildWorker`, ejecuta construcción local, publica en `registry` tras entrega y notifica enlaces `repo` y `deploy`. Callback `Ajustar`: ejecuta `Orchestrator.request_adjustment` (`WAITING_APPROVAL -> INTERVIEW`).
  - `/status [project_id]` (`/estado [project_id]`): consulta `Orchestrator.get_status` y formatea estado, avance y enlaces.
  - `/resume [project_id]`: reanuda trabajos en `JobState.PAUSED_QUOTA` en `BuildWorker`.

### 3. Publicación en rama `registry` (`src/agent_factory/ports/publisher.py` y `src/agent_factory/adapters/git_registry_publisher.py`)
Se añade `RegistryEntry`, `RegistryPublishError` y el protocolo `RegistryPublisher` en `ports/publisher.py` manteniendo intacto `StatusPublisher`:
- `GitRegistryPublisher` opera a través de `Runner` (`git` en worktree/clon aislado, rechazando explícitamente escribir en `main`/`master`) actualizando `projects/<project_id>.json` (`INV-21`) y el índice consolidado `registry.json` en la rama `registry`.
- Es idempotente y de mejor esfuerzo: ante fallos de VCS/red en `registry`, levanta `RegistryPublishError` con error sanitizado sin corromper la verdad local ni alterar `main`. Los fallos previos de `VCS` durante `VERIFYING` transicionan el proyecto a `PROJECT_FAILED` en `BuildWorker` con causa sanitizada.

### 4. Sanitización central de secretos (`src/agent_factory/secrets.py`)
`redact_secrets(text, extra_secrets, env)` sustituye tokens explícitos (`TELEGRAM_BOT_TOKEN`, `GITHUB_TOKEN`, `VERCEL_TOKEN`) y patrones de credenciales (`bot<id>:<token>`, `Bearer ...`, `x-access-token:...`) por `[REDACTED]` antes de registrar eventos, `BuildReport`, excepciones o respuestas de Telegram.

## Consecuencias
- **Positivas:** Cero puertos abiertos en la PC del usuario, cero dependencias nuevas en `pyproject.toml`, aislamiento completo entre `main` y `registry`, e higiene transversal de tokens en fallos de red/API.
- **Compromisos:** La interfaz `git` de `GitRegistryPublisher` se prueba offline contra repositorios bare locales reales en `tmp_path` mediante `SubprocessRunner` y fallos simulados con `FakeRunner`, dejando las credenciales reales fuera de la suite automatizada (`INV-03`).
