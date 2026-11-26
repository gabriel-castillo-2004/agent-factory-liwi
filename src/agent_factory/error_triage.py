"""Background Auto-Triage worker and deterministic error deduplication for Agent Factory."""

from __future__ import annotations

import atexit
import concurrent.futures
import hashlib
import logging
import os
import re
import tempfile
import threading
from collections.abc import Sequence
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from typing import Any

from agent_factory.adapters.agy_cli_adapter import build_sanitized_cli_env
from agent_factory.adapters.subprocess_runner import SubprocessRunner, resolve_executable
from agent_factory.ports.runner import CommandTimeoutError, Runner
from agent_factory.recovery_agent import _parse_json_dict_loose
from agent_factory.secrets import redact_secrets
from agent_factory.telemetry import update_triage_solution, upsert_triage_error

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
