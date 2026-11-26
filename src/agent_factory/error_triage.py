"""Background Auto-Triage worker and deterministic error deduplication for Agent Factory."""

from __future__ import annotations

import atexit
import concurrent.futures
import hashlib
import html
import json
import logging
import os
import re
import sqlite3
import tempfile
import threading
from collections.abc import Mapping, Sequence
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from agent_factory.adapters.agy_cli_adapter import build_sanitized_cli_env
from agent_factory.adapters.subprocess_runner import SubprocessRunner, resolve_executable
from agent_factory.ports.runner import CommandTimeoutError, Runner
from agent_factory.recovery_agent import _parse_json_dict_loose
from agent_factory.secrets import redact_secrets
from agent_factory.telemetry import (
    _resolve_db_path,
    update_triage_solution,
    upsert_triage_error,
)
from agent_factory.telemetry import (
    get_triage_solution as _telemetry_get_triage_solution,
)
from agent_factory.telemetry import (
    list_triage_solutions as _telemetry_list_triage_solutions,
)

logger = logging.getLogger(__name__)

DEFAULT_AGY_COMMAND: tuple[str, ...] = ("agy",)
DEFAULT_TRIAGE_TIMEOUT_SECONDS: float = 120.0
_MAX_INLINE_PROMPT_CHARS: int = 6000
_FACTORY_SRC_MARKER = "src/agent_factory/"

_UUID_RE = re.compile(
    r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b",
    re.IGNORECASE,
)
_HEX_ADDR_RE = re.compile(r"\b0x[0-9a-f]+\b", re.IGNORECASE)
_ISO_TS_RE = re.compile(
    r"\b\d{4}-\d{2}-\d{2}(?:[T\s]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?)?\b"
)
_WIN_PATH_RE = re.compile(r"[A-Za-z]:[\\/][^\s\"'`,:;()<>|]+")
_POSIX_PATH_RE = re.compile(r"(?<![A-Za-z0-9_])/(?:[A-Za-z0-9_.-]+/)+[A-Za-z0-9_.-]+")
_LONG_HEX_RE = re.compile(r"\b[0-9a-f]{12,}\b", re.IGNORECASE)
_NUMBER_RE = re.compile(r"\b\d+(?:\.\d+)?\b")
_WHITESPACE_RE = re.compile(r"\s+")

_EXECUTOR_LOCK = threading.Lock()
_EXECUTOR: ThreadPoolExecutor | None = None
_FUTURES_LOCK = threading.Lock()
_PENDING_FUTURES: set[Future[Any]] = set()


def _normalize_path_str(raw_path: str, *, keep_basename: bool) -> str:
    norm = raw_path.replace("\\", "/").strip()
    lowered = norm.lower()
    marker_idx = lowered.find(_FACTORY_SRC_MARKER)
    if marker_idx != -1:
        return lowered[marker_idx:]
    if keep_basename:
        cleaned = lowered.lstrip("./")
        return cleaned or "<unknown>"
    return "<path>"


def _normalize_error_text(error_text: str) -> str:
    text = redact_secrets(str(error_text or ""))
    text = _UUID_RE.sub("<uuid>", text)
    text = _HEX_ADDR_RE.sub("<hex>", text)
    text = _ISO_TS_RE.sub("<ts>", text)
    text = _WIN_PATH_RE.sub(lambda m: _normalize_path_str(m.group(0), keep_basename=False), text)
    text = _POSIX_PATH_RE.sub(lambda m: _normalize_path_str(m.group(0), keep_basename=False), text)
    text = _LONG_HEX_RE.sub("<id>", text)
    text = _NUMBER_RE.sub("<num>", text)
    text = _WHITESPACE_RE.sub(" ", text).strip().lower()
    return text


def compute_error_signature(error_text: str, source_file: str | None = None) -> str:
    """Compute a deterministic SHA-256 signature normalizing spaces, paths, and variable numbers."""
    norm_source = (
        _normalize_path_str(str(source_file), keep_basename=True)
        if source_file is not None and str(source_file).strip()
        else ""
    )
    norm_source = _NUMBER_RE.sub("<num>", _WHITESPACE_RE.sub(" ", norm_source).strip().lower())
    norm_error = _normalize_error_text(error_text)
    canonical = f"{norm_source}|{norm_error}"
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _get_triage_executor() -> ThreadPoolExecutor:
    """Lazily initialize the single-threaded triage ThreadPoolExecutor in a thread-safe manner."""
    global _EXECUTOR
    with _EXECUTOR_LOCK:
        if _EXECUTOR is None or getattr(_EXECUTOR, "_shutdown", False):
            _EXECUTOR = ThreadPoolExecutor(
                max_workers=1,
                thread_name_prefix="agent-factory-triage",
            )
        return _EXECUTOR


def _shutdown_triage_executor() -> None:
    global _EXECUTOR
    with _EXECUTOR_LOCK:
        executor = _EXECUTOR
        _EXECUTOR = None
    if executor is not None:
        executor.shutdown(wait=False, cancel_futures=True)


atexit.register(_shutdown_triage_executor)


def wait_for_triage_tasks(timeout: float = 10.0) -> bool:
    """Wait until all currently enqueued triage background jobs finish; return True if none remain."""
    with _FUTURES_LOCK:
        snapshot = list(_PENDING_FUTURES)
    if not snapshot:
        return True
    _done, not_done = concurrent.futures.wait(snapshot, timeout=timeout)
    return len(not_done) == 0


def _resolve_agy_triage_cmd(
    agy_command: Sequence[str],
    runner: Runner,
) -> list[str] | None:
    cmd = [str(part) for part in agy_command if str(part).strip()]
    if not cmd:
        cmd = list(DEFAULT_AGY_COMMAND)
    is_real_agy = Path(cmd[0]).stem.lower() == "agy"
    is_exact_subprocess = type(runner) is SubprocessRunner
    if is_exact_subprocess and is_real_agy and os.environ.get("AGY_CLI_DISABLED") == "1":
        return None
    resolved = resolve_executable(cmd[0], include_host_fallbacks=is_exact_subprocess)
    if not resolved and is_real_agy and cmd[0].lower() != "agy.exe":
        resolved = resolve_executable("agy.EXE", include_host_fallbacks=is_exact_subprocess)
    if is_exact_subprocess and is_real_agy and not resolved:
        return None
    if resolved and is_exact_subprocess:
        cmd[0] = resolved
    return cmd


def _analyze_error_with_agy(
    raw_error: str,
    source_file: str | None = None,
    *,
    runner: Runner | None = None,
    agy_command: Sequence[str] = DEFAULT_AGY_COMMAND,
    timeout_seconds: float = DEFAULT_TRIAGE_TIMEOUT_SECONDS,
) -> dict[str, str] | None:
    """Analyze a factory error using agy.EXE in strict read-only mode and parse JSON output."""
    eff_runner: Runner = (
        runner if runner is not None else SubprocessRunner(default_timeout_seconds=timeout_seconds)
    )
    base_cmd = _resolve_agy_triage_cmd(agy_command, eff_runner)
    if base_cmd is None:
        logger.debug("Auto-triage skipped: agy CLI unavailable in this environment.")
        return None

    clean_error = redact_secrets(str(raw_error or "").strip())[:4000]
    clean_source = redact_secrets(str(source_file or "").strip()) if source_file else "(desconocido)"
    ro_prompt = (
        "MODO SOLO LECTURA (PROHIBIDO editar, crear o borrar archivos y PROHIBIDO mutar el estado).\n"
        "PROHIBIDO leer o modificar .env, MASTER_PROMPT.md, AGENTS.md, config/models.toml, data/ o workspaces/.\n"
        "Eres el analizador de Auto-Triage de Agent Factory. Responde en español técnico claro.\n\n"
        f"Archivo origen: {clean_source}\n"
        f"Error detectado:\n{clean_error}\n\n"
        "Devuelve ÚNICAMENTE un JSON válido con esta estructura exacta:\n"
        '{"title": "<título breve y descriptivo del fallo>", '
        '"root_cause": "<explicación técnica de la causa raíz>", '
        '"proposed_fix": "<solución propuesta concreta>", '
        '"technical_prompt": "<prompt técnico TDD acotado para resolver el error con pruebas dirigidas>"}'
    )

    is_batch = bool(base_cmd and base_cmd[0].lower().endswith((".cmd", ".bat")))
    clean_env = build_sanitized_cli_env()
    tmp_prompt: Path | None = None
    try:
        if len(ro_prompt) <= _MAX_INLINE_PROMPT_CHARS:
            prompt_arg = re.sub(r"[\r\n]+", " ", ro_prompt).strip() if is_batch else ro_prompt
        else:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                suffix=".txt",
                prefix="af_triage_prompt_",
                delete=False,
            ) as tf:
                tf.write(ro_prompt)
                tmp_prompt = Path(tf.name).resolve()
            prompt_arg = f"Lee y sigue exactamente las instrucciones del archivo: {tmp_prompt}"

        cmd = list(base_cmd) + ["-p", prompt_arg]
        with tempfile.TemporaryDirectory(prefix="af_triage_ro_") as ro_cwd:
            res = eff_runner.run(
                cmd,
                cwd=Path(ro_cwd),
                timeout_seconds=timeout_seconds,
                env=clean_env,
            )
    except (CommandTimeoutError, OSError, RuntimeError, ValueError) as exc:
        logger.warning("Auto-triage agy.EXE invocation failed safely: %s", redact_secrets(str(exc)))
        return None
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "Auto-triage unexpected error during agy.EXE call: %s",
            redact_secrets(str(exc)),
        )
        return None
    finally:
        if tmp_prompt is not None:
            tmp_prompt.unlink(missing_ok=True)

    raw_combined = (res.stdout or "") + "\n" + (res.stderr or "")
    try:
        from agent_factory.idea_engine import record_agy_429_pause, record_agy_usage

        if res.exit_code != 0 and is_quota_429_error(raw_combined):
            record_agy_429_pause(raw_combined)
        else:
            record_agy_usage(len(ro_prompt), len(raw_combined))
    except Exception as exc:  # noqa: BLE001
        logger.debug("Quota telemetry skipped in error_triage: %s", exc)

    if res.exit_code != 0:
        logger.warning(
            "Auto-triage agy.EXE returned non-zero exit code %s",
            res.exit_code,
        )
        return None

    parsed = _parse_json_dict_loose(res.stdout or "")
    if not isinstance(parsed, dict):
        logger.debug("Auto-triage agy.EXE output was not parseable JSON.")
        return None

    title = redact_secrets(str(parsed.get("title") or "").strip())[:200]
    root_cause = redact_secrets(str(parsed.get("root_cause") or "").strip())
    proposed_fix = redact_secrets(str(parsed.get("proposed_fix") or "").strip())
    technical_prompt = redact_secrets(str(parsed.get("technical_prompt") or "").strip())

    if not any((title, root_cause, proposed_fix, technical_prompt)):
        return None

    fallback_title = f"Error en {clean_source}" if clean_source != "(desconocido)" else "Error interno de fábrica"
    return {
        "title": title or fallback_title,
        "root_cause": root_cause or clean_error[:500],
        "proposed_fix": proposed_fix or "Revisar el módulo afectado y añadir cobertura TDD para el caso borde.",
        "technical_prompt": (
            technical_prompt
            or f"Reproduce con una prueba TDD dirigida el error en {clean_source}: {clean_error[:300]}."
        ),
    }


def _run_triage_analysis_job(
    solution_id: int,
    raw_error: str,
    source_file: str | None,
    db_path: Path | str | None,
) -> None:
    """Worker callback executed sequentially inside the single-threaded triage pool."""
    try:
        analysis = _analyze_error_with_agy(raw_error, source_file)
        if not isinstance(analysis, dict):
            return
        update_triage_solution(
            solution_id,
            title=str(analysis.get("title") or ""),
            root_cause=str(analysis.get("root_cause") or ""),
            proposed_fix=str(analysis.get("proposed_fix") or ""),
            technical_prompt=str(analysis.get("technical_prompt") or ""),
            status="lista",
            db_path=db_path,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "Auto-triage worker caught error for solution_id=%s: %s",
            solution_id,
            redact_secrets(str(exc)),
        )


def report_factory_error(
    error_text: str,
    scope: str = "factory",
    source_file: str | None = None,
    *,
    db_path: Path | str | None = None,
) -> dict[str, Any] | None:
    """Record/deduplicate a factory error and enqueue read-only triage analysis for new factory errors.

    Never propagates unhandled exceptions to callers.
    """
    try:
        clean_error = redact_secrets(str(error_text or "").strip())
        if not clean_error:
            return None

        eff_scope = str(scope or "factory").strip().lower() or "factory"
        signature = compute_error_signature(clean_error, source_file=source_file)
        initial_status = "analizando" if eff_scope == "factory" else "descartada"

        record, is_new = upsert_triage_error(
            signature,
            clean_error,
            scope=eff_scope,
            status=initial_status,
            db_path=db_path,
        )
        if record is None:
            return None

        if is_new and eff_scope == "factory":
            executor = _get_triage_executor()
            future = executor.submit(
                _run_triage_analysis_job,
                int(record["id"]),
                clean_error,
                source_file,
                db_path,
            )
            with _FUTURES_LOCK:
                _PENDING_FUTURES.add(future)

            def _cleanup(done_future: Future[Any]) -> None:
                with _FUTURES_LOCK:
                    _PENDING_FUTURES.discard(done_future)

            future.add_done_callback(_cleanup)

        return record
    except Exception as exc:  # noqa: BLE001
        logger.debug("report_factory_error swallowed unexpected error: %s", redact_secrets(str(exc)))
        return None


_QUOTA_429_RE = re.compile(
    r"(?:RESOURCE_EXHAUSTED|\bHTTP\s*429\b|\bstatus(?:\s*code)?\s*[:=]?\s*429\b|\b429\s+Too\s+Many\s+Requests\b|rate[_\s-]?limit|quota\s+exceeded)",
    re.IGNORECASE,
)
_RESET_IN_RE = re.compile(
    r"Resets?\s+in\s+(?:(?P<hours>\d+)\s*h\s*)?(?:(?P<minutes>\d+)\s*m\s*)?(?:(?P<seconds>\d+)\s*s)?",
    re.IGNORECASE,
)


def is_quota_429_error(raw_output: str) -> bool:
    """Return True when raw CLI/API output represents a 429 / RESOURCE_EXHAUSTED quota error."""
    text = str(raw_output or "")
    if not text.strip():
        return False
    return bool(_QUOTA_429_RE.search(text))


def parse_quota_reset_delay_seconds(
    raw_output: str,
    *,
    default_seconds: int = 1800,
) -> int:
    """Extract reset delay in seconds from strings like 'Resets in 14m32s', else return default_seconds (30m)."""
    text = str(raw_output or "")
    match = _RESET_IN_RE.search(text)
    if not match:
        return max(60, int(default_seconds))
    hours = int(match.group("hours") or 0)
    minutes = int(match.group("minutes") or 0)
    seconds = int(match.group("seconds") or 0)
    total = (hours * 3600) + (minutes * 60) + seconds
    if total <= 0:
        return max(60, int(default_seconds))
    return total


def format_agent_error(
    raw_output: str,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Translate raw agent/CLI output into a friendly Spanish status payload with collapsible technical details."""
    clean_detail = redact_secrets(str(raw_output or "").strip())
    escaped_detail = html.escape(clean_detail)
    details_block = (
        f"<details><summary>Ver detalle técnico</summary><pre>{escaped_detail}</pre></details>"
        if clean_detail
        else ""
    )

    if is_quota_429_error(clean_detail):
        delay_seconds = parse_quota_reset_delay_seconds(clean_detail, default_seconds=1800)
        ref_now = now if now is not None else datetime.now().astimezone()
        resume_dt = ref_now + timedelta(seconds=delay_seconds)
        resume_hhmm = resume_dt.strftime("%H:%M")
        friendly = (
            "⏳ Sin cuota disponible por ahora. "
            f"Tu avance quedó guardado a salvo y continuará automáticamente a las {resume_hhmm}."
        )
        summary_html = (
            f"{html.escape(friendly)}\n{details_block}"
            if details_block
            else html.escape(friendly)
        )
        return {
            "is_quota_429": True,
            "message": friendly,
            "user_message": friendly,
            "resume_hhmm": resume_hhmm,
            "reset_seconds": delay_seconds,
            "quota_paused_until": resume_dt.isoformat(),
            "technical_detail": clean_detail,
            "details_html": summary_html,
        }

    first_line = clean_detail.splitlines()[0].strip() if clean_detail else "Error inesperado del agente."
    if len(first_line) > 220:
        first_line = first_line[:217] + "..."
    friendly = f"⚠️ Ocurrió un inconveniente con el agente: {first_line}"
    summary_html = (
        f"{html.escape(friendly)}\n{details_block}"
        if details_block
        else html.escape(friendly)
    )
    return {
        "is_quota_429": False,
        "message": friendly,
        "user_message": friendly,
        "resume_hhmm": None,
        "reset_seconds": None,
        "quota_paused_until": None,
        "technical_detail": clean_detail,
        "details_html": summary_html,
    }


TICKET_VALID_STATUSES: frozenset[str] = frozenset(
    {"pendiente", "reparando", "resuelto", "descartado"}
)
_SRC_FACTORY_FILE_RE = re.compile(
    r"(src[\\/]agent_factory[\\/][A-Za-z0-9_./\\-]+\.py)(?:[\"']?\s*,?\s*line\s*(\d+)|:(\d+))?",
    re.IGNORECASE,
)
_JS_LINE_RE = re.compile(r"(?::(\d+)(?::(\d+))?|\bline\s+(\d+))", re.IGNORECASE)


def _ensure_triage_tickets_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS triage_tickets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            error_signature TEXT NOT NULL,
            source TEXT NOT NULL,
            error_text TEXT NOT NULL,
            affected_file TEXT NOT NULL DEFAULT '',
            affected_lines TEXT NOT NULL DEFAULT '',
            simple_summary TEXT NOT NULL,
            repair_prompt TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pendiente'
                CHECK(status IN ('pendiente', 'reparando', 'resuelto', 'descartado')),
            occurrences INTEGER NOT NULL DEFAULT 1 CHECK(occurrences >= 1)
        );
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_triage_tickets_status "
        "ON triage_tickets(status, updated_at DESC);"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_triage_tickets_sig "
        "ON triage_tickets(error_signature);"
    )


def _open_tickets_conn(db_path: Path | str | None = None) -> sqlite3.Connection:
    resolved = _resolve_db_path(db_path)
    if str(resolved) != ":memory:":
        resolved.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(resolved), timeout=5.0)
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA busy_timeout=5000;")
        _ensure_triage_tickets_schema(conn)
        conn.commit()
    except Exception:
        conn.close()
        raise
    return conn


def _row_to_ticket(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": int(row["id"]),
        "created_at": str(row["created_at"]),
        "updated_at": str(row["updated_at"]),
        "error_signature": str(row["error_signature"]),
        "source": str(row["source"]),
        "error_text": str(row["error_text"]),
        "affected_file": str(row["affected_file"]),
        "affected_lines": str(row["affected_lines"]),
        "simple_summary": str(row["simple_summary"]),
        "repair_prompt": str(row["repair_prompt"]),
        "status": str(row["status"]),
        "occurrences": int(row["occurrences"]),
    }


def _locate_suspect_file_and_lines(
    source: str,
    error_text: str,
) -> tuple[str, str]:
    matches = _SRC_FACTORY_FILE_RE.findall(error_text)
    if matches:
        last_file, py_line, colon_line = matches[-1]
        norm_file = last_file.replace("\\", "/")
        line_str = py_line or colon_line or ""
        return norm_file, line_str

    src_lower = source.lower()
    if any(tok in src_lower for tok in ("browser", "client", "js", "fetch", "ui")):
        line_m = _JS_LINE_RE.search(error_text)
        line_str = (line_m.group(1) or line_m.group(3) or "") if line_m else ""
        return "src/agent_factory/dashboard.py", line_str
    if "boot" in src_lower or "startup" in src_lower or "8000" in src_lower:
        return "src/agent_factory/dashboard.py", ""
    return "src/agent_factory/", ""


def _targeted_test_for_file(affected_file: str) -> str:
    norm = affected_file.replace("\\", "/")
    if norm == "src/agent_factory/dashboard.py":
        return 'tests/test_dashboard.py -k "triage or autonomous or ideas"'
    if norm.startswith("src/agent_factory/") and norm.endswith(".py"):
        stem = Path(norm).stem
        return f"tests/test_{stem}.py"
    return "tests/test_error_triage.py"


def build_actionable_fix_ticket(
    source: str,
    error_text: str,
    context: Mapping[str, Any] | str | None = None,
    *,
    db_path: Path | str | None = None,
) -> dict[str, Any]:
    """Analyze a runtime/JS/HTTP500/startup error, build simple_summary + repair_prompt, and persist/deduplicate ticket."""
    clean_source = redact_secrets(str(source or "server").strip())[:80] or "server"
    clean_error = redact_secrets(str(error_text or "").strip())[:4000]
    if not clean_error:
        raise ValueError("error_text es obligatorio para generar un ticket de diagnóstico.")

    if isinstance(context, Mapping):
        clean_ctx = redact_secrets(json.dumps(dict(context), ensure_ascii=False, default=str))[:1200]
    elif context is not None:
        clean_ctx = redact_secrets(str(context).strip())[:1200]
    else:
        clean_ctx = ""

    affected_file, affected_lines = _locate_suspect_file_and_lines(clean_source, clean_error)
    sig = compute_error_signature(clean_error, source_file=affected_file or clean_source)

    first_line = clean_error.splitlines()[0].strip()[:180]
    loc_label = f"{affected_file}:{affected_lines}" if affected_lines else (affected_file or clean_source)

    src_lower = clean_source.lower()
    if any(tok in src_lower for tok in ("browser", "client", "js", "window.onerror", "unhandledrejection")):
        simple_summary = f"Ocurrió un error en la interfaz del navegador ({loc_label}): {first_line}"
    elif "500" in src_lower or "fetch" in src_lower:
        simple_summary = f"El servidor respondió con error interno (HTTP 500) en {loc_label}: {first_line}"
    elif "startup" in src_lower or "boot" in src_lower or "watchdog" in src_lower:
        simple_summary = f"El servidor en el puerto 8000 falló al arrancar y fue restaurado automáticamente ({loc_label}): {first_line}"
    else:
        simple_summary = f"Se detectó un fallo técnico en {loc_label}: {first_line}"

    target_test = _targeted_test_for_file(affected_file)
    ruff_target = affected_file if affected_file.endswith(".py") else "src/agent_factory/"
    ctx_section = f"\nContexto adicional:\n{clean_ctx}\n" if clean_ctx else ""

    repair_prompt = (
        "Diagnostica y corrige el siguiente error real capturado en Agent Factory (Liwi) siguiendo TDD.\n"
        "REGLAS: NO modifiques .env, MASTER_PROMPT.md, AGENTS.md, config/models.toml, data/ ni workspaces/.\n"
        f"Origen del fallo: {clean_source}\n"
        f"Archivo sospechoso: {loc_label}\n"
        f"{ctx_section}\n"
        f"Error / Stacktrace exacto:\n{clean_error}\n\n"
        "Pasos requeridos:\n"
        "1. Escribe o actualiza primero una prueba unitaria dirigida que reproduzca el fallo.\n"
        "2. Aplica la corrección mínima y limpia en el archivo afectado.\n"
        f"3. Verifica con:\n   ruff check {ruff_target}\n   pytest -q --no-cov {target_test}\n"
    )

    now_iso = datetime.now(UTC).isoformat()
    conn = _open_tickets_conn(db_path)
    try:
        existing = conn.execute(
            "SELECT * FROM triage_tickets "
            "WHERE error_signature = ? AND status IN ('pendiente', 'reparando') "
            "ORDER BY id DESC LIMIT 1;",
            (sig,),
        ).fetchone()
        if existing is not None:
            conn.execute(
                "UPDATE triage_tickets SET occurrences = occurrences + 1, updated_at = ? WHERE id = ?;",
                (now_iso, int(existing["id"])),
            )
            conn.commit()
            refreshed = conn.execute(
                "SELECT * FROM triage_tickets WHERE id = ?;",
                (int(existing["id"]),),
            ).fetchone()
            assert refreshed is not None
            payload = _row_to_ticket(refreshed)
            payload["deduplicated"] = True
            return payload

        cur = conn.execute(
            """
            INSERT INTO triage_tickets (
                created_at, updated_at, error_signature, source, error_text,
                affected_file, affected_lines, simple_summary, repair_prompt, status, occurrences
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pendiente', 1);
            """,
            (
                now_iso,
                now_iso,
                sig,
                clean_source,
                clean_error,
                affected_file,
                affected_lines,
                simple_summary[:500],
                repair_prompt[:4000],
            ),
        )
        conn.commit()
        created = conn.execute(
            "SELECT * FROM triage_tickets WHERE id = ?;",
            (int(cur.lastrowid),),
        ).fetchone()
        assert created is not None
        payload = _row_to_ticket(created)
        payload["deduplicated"] = False
        return payload
    finally:
        conn.close()


def list_triage_tickets(
    *,
    status: str | None = "pendiente",
    limit: int = 20,
    db_path: Path | str | None = None,
) -> list[dict[str, Any]]:
    """List actionable fix tickets ordered newest/most-recent first."""
    bounded = max(1, min(int(limit), 200))
    conn = _open_tickets_conn(db_path)
    try:
        if status is not None:
            clean_st = str(status).strip().lower()
            if clean_st not in TICKET_VALID_STATUSES:
                raise ValueError(f"Estado de ticket inválido: {status!r}")
            rows = conn.execute(
                "SELECT * FROM triage_tickets WHERE status = ? ORDER BY updated_at DESC, id DESC LIMIT ?;",
                (clean_st, bounded),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM triage_tickets ORDER BY updated_at DESC, id DESC LIMIT ?;",
                (bounded,),
            ).fetchall()
        return [_row_to_ticket(r) for r in rows]
    finally:
        conn.close()


def get_triage_ticket(
    ticket_id: int,
    *,
    db_path: Path | str | None = None,
) -> dict[str, Any] | None:
    """Return a single triage ticket by id, or None."""
    if isinstance(ticket_id, bool) or not isinstance(ticket_id, int) or ticket_id <= 0:
        raise ValueError(f"ticket_id inválido: {ticket_id!r}")
    conn = _open_tickets_conn(db_path)
    try:
        row = conn.execute("SELECT * FROM triage_tickets WHERE id = ?;", (ticket_id,)).fetchone()
        return _row_to_ticket(row) if row is not None else None
    finally:
        conn.close()


def update_triage_ticket_status(
    ticket_id: int,
    status: str,
    *,
    db_path: Path | str | None = None,
) -> dict[str, Any]:
    """Update ticket status cleanly and return updated row."""
    if isinstance(ticket_id, bool) or not isinstance(ticket_id, int) or ticket_id <= 0:
        raise ValueError(f"ticket_id inválido: {ticket_id!r}")
    clean_st = str(status or "").strip().lower()
    if clean_st not in TICKET_VALID_STATUSES:
        raise ValueError(f"Estado de ticket inválido: {status!r}")
    conn = _open_tickets_conn(db_path)
    try:
        cur = conn.execute(
            "UPDATE triage_tickets SET status = ?, updated_at = ? WHERE id = ?;",
            (clean_st, datetime.now(UTC).isoformat(), ticket_id),
        )
        conn.commit()
        if cur.rowcount <= 0:
            raise KeyError(f"Ticket de diagnóstico {ticket_id} no encontrado.")
        row = conn.execute("SELECT * FROM triage_tickets WHERE id = ?;", (ticket_id,)).fetchone()
        assert row is not None
        return _row_to_ticket(row)
    finally:
        conn.close()


TRIAGE_SOLUTION_DECISION_STATUS_MAP: dict[str, str] = {
    "aprobada": "encolada",
    "aprobar": "encolada",
    "approved": "encolada",
    "approve": "encolada",
    "encolada": "encolada",
    "descartada": "descartada",
    "descartar": "descartada",
    "dismissed": "descartada",
    "dismiss": "descartada",
    "discard": "descartada",
    "rejected": "descartada",
}


def list_triage_solutions(
    *,
    status: str | None = None,
    scope: str | None = None,
    limit: int = 50,
    db_path: Path | str | None = None,
) -> list[dict[str, Any]]:
    """List Auto-Triage solutions ordered most-recent first with optional status/scope filter."""
    return _telemetry_list_triage_solutions(
        status=status,
        scope=scope,
        limit=limit,
        db_path=db_path,
    )


def get_triage_solution(
    solution_id: int,
    *,
    db_path: Path | str | None = None,
) -> dict[str, Any] | None:
    """Return a single Auto-Triage solution by ID, or None if not found."""
    if isinstance(solution_id, bool) or not isinstance(solution_id, int) or solution_id <= 0:
        raise ValueError(f"solution_id inválido: {solution_id!r}")
    return _telemetry_get_triage_solution(solution_id, db_path=db_path)


def decide_triage_solution(
    solution_id: int,
    decision: str,
    *,
    db_path: Path | str | None = None,
) -> dict[str, Any]:
    """Mark an Auto-Triage solution as approved ('encolada') or dismissed ('descartada')."""
    if isinstance(solution_id, bool) or not isinstance(solution_id, int) or solution_id <= 0:
        raise ValueError(f"solution_id inválido: {solution_id!r}")
    clean_decision = str(decision or "").strip().lower()
    target_status = TRIAGE_SOLUTION_DECISION_STATUS_MAP.get(clean_decision)
    if target_status is None:
        raise ValueError(
            f"Decisión de Auto-Triage inválida: {decision!r}. "
            "Usa 'aprobada' o 'descartada'."
        )
    existing = _telemetry_get_triage_solution(solution_id, db_path=db_path)
    if existing is None:
        raise KeyError(f"Solución de Auto-Triage '{solution_id}' no encontrada.")
    updated = update_triage_solution(
        solution_id,
        status=target_status,
        db_path=db_path,
    )
    if updated is None:
        raise KeyError(f"Solución de Auto-Triage '{solution_id}' no encontrada.")
    return updated



