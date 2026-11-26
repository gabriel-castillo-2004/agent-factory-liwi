"""Self-Improvement Idea Radar engine: SQLite persistence, context builder, read-only agy ideation, and optional daemon worker."""

from __future__ import annotations

import json
import logging
import math
import os
import re
import sqlite3
import tempfile
import threading
import time
import unicodedata
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from agent_factory.adapters.agy_cli_adapter import build_sanitized_cli_env
from agent_factory.adapters.subprocess_runner import SubprocessRunner, resolve_executable
from agent_factory.ports.runner import CommandResult, CommandTimeoutError, Runner
from agent_factory.secrets import redact_secrets
from agent_factory.telemetry import (
    DEFAULT_TELEMETRY_DB_PATH,
    _resolve_db_path,
    get_telemetry_summary,
)

logger = logging.getLogger(__name__)

DEFAULT_IDEA_DB_PATH: Path = DEFAULT_TELEMETRY_DB_PATH
IDEA_VALID_STATUSES: frozenset[str] = frozenset({"nueva", "encolada", "descartada"})
IDEA_VALID_SOURCES: frozenset[str] = frozenset({"manual", "auto"})
IDEA_VALID_IMPACTS: frozenset[str] = frozenset({"Alto", "Medio"})
DEFAULT_AUTO_INTERVAL_HOURS: float = 0.0
DEFAULT_IDEATION_COUNT: int = 3
MAX_IDEATION_COUNT: int = 10
DEFAULT_IDEATION_TIMEOUT_SECONDS: float = 180.0
DEFAULT_GIT_LOG_TIMEOUT_SECONDS: float = 10.0
DEFAULT_POLL_INTERVAL_SECONDS: float = 30.0
DEFAULT_LIST_LIMIT: int = 100
MAX_LIST_LIMIT: int = 500

_AUTO_INTERVAL_SETTING_KEY = "auto_interval_hours"
_MAX_INLINE_PROMPT_CHARS = 6000
_FENCED_BLOCK_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.IGNORECASE | re.DOTALL)
_NON_ALNUM_RE = re.compile(r"[^a-z0-9\s]+")
_IDEA_SELECT_COLS = (
    "id, created_at, title, category, impact, simple_description, "
    "technical_prompt, status, source"
)
_HIGH_IMPACT_ALIASES: frozenset[str] = frozenset(
    {"alto", "alta", "high", "critico", "crítico", "critical"}
)


def _default_agy_command() -> tuple[str, ...]:
    return ("agy.EXE",) if os.name == "nt" else ("agy",)


def _default_repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _ensure_idea_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS improvement_ideas (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL,
            title TEXT NOT NULL,
            category TEXT NOT NULL,
            impact TEXT NOT NULL,
            simple_description TEXT NOT NULL,
            technical_prompt TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'nueva'
                CHECK(status IN ('nueva', 'encolada', 'descartada')),
            source TEXT NOT NULL DEFAULT 'manual'
                CHECK(source IN ('manual', 'auto'))
        );
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS idea_radar_settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_improvement_ideas_status ON improvement_ideas(status);"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_improvement_ideas_created_at ON improvement_ideas(created_at DESC);"
    )


def _open_connection(db_path: Path | str | None = None) -> sqlite3.Connection:
    resolved = _resolve_db_path(db_path)
    if str(resolved) != ":memory:":
        resolved.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(resolved), timeout=5.0)
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA busy_timeout=5000;")
        conn.execute("PRAGMA synchronous=NORMAL;")
        _ensure_idea_schema(conn)
        conn.commit()
    except Exception:
        conn.close()
        raise
    return conn


def init_idea_db(db_path: Path | str | None = None) -> bool:
    """Idempotently initialize improvement_ideas and idea_radar_settings tables."""
    conn: sqlite3.Connection | None = None
    try:
        conn = _open_connection(db_path)
        return True
    except (sqlite3.Error, OSError) as exc:
        logger.debug("Idea DB init failed: %s", exc)
        return False
    finally:
        if conn is not None:
            conn.close()


def _validate_status(status: str) -> str:
    clean = str(status or "").strip().lower()
    if clean not in IDEA_VALID_STATUSES:
        raise ValueError(
            f"Estado de idea inválido {status!r}; se esperaba uno de {sorted(IDEA_VALID_STATUSES)}."
        )
    return clean


def _validate_source(source: str) -> str:
    clean = str(source or "").strip().lower()
    if clean not in IDEA_VALID_SOURCES:
        raise ValueError(
            f"Origen de idea inválido {source!r}; se esperaba uno de {sorted(IDEA_VALID_SOURCES)}."
        )
    return clean


def _validate_idea_id(idea_id: Any) -> int | None:
    if isinstance(idea_id, bool) or not isinstance(idea_id, int) or idea_id <= 0:
        return None
    return idea_id


def normalize_idea_title(title: str) -> str:
    """Normalize an idea title for case-, accent-, emoji-, and whitespace-insensitive deduplication."""
    raw = str(title or "").strip()
    if not raw:
        return ""
    decomposed = unicodedata.normalize("NFKD", raw)
    without_marks = "".join(ch for ch in decomposed if unicodedata.category(ch) != "Mn")
    lowered = without_marks.casefold()
    alnum_only = " ".join(_NON_ALNUM_RE.sub(" ", lowered).split())
    if alnum_only:
        return alnum_only
    return " ".join(lowered.split())


def normalize_impact(raw_impact: Any) -> str:
    """Normalize an impact label strictly to 'Alto' or 'Medio'."""
    clean = str(raw_impact or "").strip()
    if not clean:
        raise ValueError("El campo 'impact' es obligatorio.")
    decomposed = unicodedata.normalize("NFKD", clean)
    without_marks = "".join(ch for ch in decomposed if unicodedata.category(ch) != "Mn")
    lowered = without_marks.casefold().strip()
    if lowered in _HIGH_IMPACT_ALIASES:
        return "Alto"
    return "Medio"


def _row_to_idea(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": int(row["id"]),
        "created_at": str(row["created_at"]),
        "title": str(row["title"]),
        "category": str(row["category"]),
        "impact": str(row["impact"]),
        "simple_description": str(row["simple_description"]),
        "technical_prompt": str(row["technical_prompt"]),
        "status": str(row["status"]),
        "source": str(row["source"]),
    }


def get_auto_interval_hours(db_path: Path | str | None = None) -> float:
    """Return persisted auto_interval_hours (>= 0.0; 0.0 means disabled)."""
    conn: sqlite3.Connection | None = None
    try:
        conn = _open_connection(db_path)
        row = conn.execute(
            "SELECT value FROM idea_radar_settings WHERE key = ?;",
            (_AUTO_INTERVAL_SETTING_KEY,),
        ).fetchone()
        if row is None:
            return DEFAULT_AUTO_INTERVAL_HOURS
        val = float(row["value"])
        if math.isnan(val) or math.isinf(val) or val < 0.0:
            return DEFAULT_AUTO_INTERVAL_HOURS
        return val
    except (sqlite3.Error, OSError, ValueError, TypeError) as exc:
        logger.debug("Fallback get_auto_interval_hours due to error: %s", exc)
        return DEFAULT_AUTO_INTERVAL_HOURS
    finally:
        if conn is not None:
            conn.close()


def set_auto_interval_hours(
    hours: float,
    db_path: Path | str | None = None,
) -> float:
    """Validate and persist auto_interval_hours (>= 0; 0 disables auto ideation)."""
    if isinstance(hours, bool) or not isinstance(hours, (int, float)):
        raise TypeError("auto_interval_hours debe ser un número mayor o igual a 0.")
    val = float(hours)
    if math.isnan(val) or math.isinf(val) or val < 0.0:
        raise ValueError("auto_interval_hours debe ser un número finito >= 0.")

    now_iso = datetime.now(UTC).isoformat()
    conn: sqlite3.Connection | None = None
    try:
        conn = _open_connection(db_path)
        conn.execute(
            """
            INSERT INTO idea_radar_settings (key, value, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET
                value = excluded.value,
                updated_at = excluded.updated_at;
            """,
            (_AUTO_INTERVAL_SETTING_KEY, repr(val), now_iso),
        )
        conn.commit()
        return val
    finally:
        if conn is not None:
            conn.close()


def list_ideas(
    statuses: Sequence[str] | str | None = ("nueva", "encolada"),
    db_path: Path | str | None = None,
    *,
    limit: int = DEFAULT_LIST_LIMIT,
) -> list[dict[str, Any]]:
    """List improvement ideas filtered by status, ordered most recent first."""
    if isinstance(statuses, str):
        status_tuple: tuple[str, ...] | None = (_validate_status(statuses),)
    elif statuses is None:
        status_tuple = None
    else:
        status_tuple = tuple(_validate_status(s) for s in statuses)
        if not status_tuple:
            return []

    if isinstance(limit, bool) or not isinstance(limit, int):
        bounded_limit = DEFAULT_LIST_LIMIT
    else:
        bounded_limit = max(1, min(limit, MAX_LIST_LIMIT))

    params: list[Any] = []
    where_sql = ""
    if status_tuple is not None:
        placeholders = ", ".join("?" for _ in status_tuple)
        where_sql = f"WHERE status IN ({placeholders})"
        params.extend(status_tuple)
    params.append(bounded_limit)

    conn: sqlite3.Connection | None = None
    try:
        conn = _open_connection(db_path)
        rows = conn.execute(
            f"SELECT {_IDEA_SELECT_COLS} FROM improvement_ideas "
            f"{where_sql} ORDER BY created_at DESC, id DESC LIMIT ?;",
            tuple(params),
        ).fetchall()
        return [_row_to_idea(row) for row in rows]
    finally:
        if conn is not None:
            conn.close()


def list_all_idea_titles(db_path: Path | str | None = None) -> list[str]:
    """Return all titles in improvement_ideas (including 'descartada') to prevent repetitions."""
    conn: sqlite3.Connection | None = None
    try:
        conn = _open_connection(db_path)
        rows = conn.execute(
            "SELECT title FROM improvement_ideas ORDER BY id DESC;"
        ).fetchall()
        return [str(row["title"]) for row in rows if str(row["title"]).strip()]
    finally:
        if conn is not None:
            conn.close()


def get_idea(
    idea_id: int,
    db_path: Path | str | None = None,
) -> dict[str, Any] | None:
    """Return a single improvement_ideas record by ID, or None if missing/invalid."""
    valid_id = _validate_idea_id(idea_id)
    if valid_id is None:
        return None
    conn: sqlite3.Connection | None = None
    try:
        conn = _open_connection(db_path)
        row = conn.execute(
            f"SELECT {_IDEA_SELECT_COLS} FROM improvement_ideas WHERE id = ?;",
            (valid_id,),
        ).fetchone()
        return _row_to_idea(row) if row is not None else None
    finally:
        if conn is not None:
            conn.close()


def _normalize_candidate_idea(raw: Any) -> dict[str, str] | None:
    if not isinstance(raw, Mapping):
        return None
    title = redact_secrets(str(raw.get("title") or "").strip())
    category = redact_secrets(str(raw.get("category") or "").strip())
    simple_description = redact_secrets(str(raw.get("simple_description") or "").strip())
    technical_prompt = redact_secrets(str(raw.get("technical_prompt") or "").strip())
    if not (title and category and simple_description and technical_prompt):
        return None
    try:
        impact = normalize_impact(raw.get("impact"))
    except ValueError:
        return None
    return {
        "title": title[:200],
        "category": category[:80],
        "impact": impact,
        "simple_description": simple_description[:1000],
        "technical_prompt": technical_prompt[:8000],
    }


def save_ideas(
    ideas: Sequence[Mapping[str, Any]],
    source: str = "manual",
    db_path: Path | str | None = None,
    *,
    max_items: int | None = None,
) -> list[dict[str, Any]]:
    """Validate, deduplicate against all existing titles (including dismissed), and persist ideas."""
    eff_source = _validate_source(source)
    if max_items is not None and (isinstance(max_items, bool) or max_items <= 0):
        return []

    conn: sqlite3.Connection | None = None
    try:
        conn = _open_connection(db_path)
        existing_rows = conn.execute("SELECT title FROM improvement_ideas;").fetchall()
        seen_normalized: set[str] = {
            norm
            for row in existing_rows
            if (norm := normalize_idea_title(str(row["title"])))
        }

        created: list[dict[str, Any]] = []
        for raw_item in ideas:
            if max_items is not None and len(created) >= max_items:
                break
            normalized = _normalize_candidate_idea(raw_item)
            if normalized is None:
                continue
            norm_title = normalize_idea_title(normalized["title"])
            if not norm_title or norm_title in seen_normalized:
                continue
            seen_normalized.add(norm_title)

            created_at = datetime.now(UTC).isoformat()
            cursor = conn.execute(
                """
                INSERT INTO improvement_ideas (
                    created_at,
                    title,
                    category,
                    impact,
                    simple_description,
                    technical_prompt,
                    status,
                    source
                ) VALUES (?, ?, ?, ?, ?, ?, 'nueva', ?);
                """,
                (
                    created_at,
                    normalized["title"],
                    normalized["category"],
                    normalized["impact"],
                    normalized["simple_description"],
                    normalized["technical_prompt"],
                    eff_source,
                ),
            )
            inserted_id = cursor.lastrowid
            row = conn.execute(
                f"SELECT {_IDEA_SELECT_COLS} FROM improvement_ideas WHERE id = ?;",
                (inserted_id,),
            ).fetchone()
            if row is not None:
                created.append(_row_to_idea(row))

        conn.commit()
        return created
    finally:
        if conn is not None:
            conn.close()


def dismiss_idea(
    idea_id: int,
    db_path: Path | str | None = None,
) -> dict[str, Any] | None:
    """Mark an idea as 'descartada' and return the updated record, or None if not found."""
    valid_id = _validate_idea_id(idea_id)
    if valid_id is None:
        return None
    conn: sqlite3.Connection | None = None
    try:
        conn = _open_connection(db_path)
        cursor = conn.execute(
            "UPDATE improvement_ideas SET status = 'descartada' WHERE id = ?;",
            (valid_id,),
        )
        if cursor.rowcount == 0:
            return None
        conn.commit()
        row = conn.execute(
            f"SELECT {_IDEA_SELECT_COLS} FROM improvement_ideas WHERE id = ?;",
            (valid_id,),
        ).fetchone()
        return _row_to_idea(row) if row is not None else None
    finally:
        if conn is not None:
            conn.close()


def implement_idea(
    idea_id: int,
    staging_manager: Any,
    db_path: Path | str | None = None,
) -> dict[str, Any]:
    """Enqueue an idea's technical_prompt in staging_manager (.liwi-staging) and mark it 'encolada'."""
    idea = get_idea(idea_id, db_path=db_path)
    if idea is None:
        raise KeyError(f"Idea {idea_id} no encontrada.")
    if idea["status"] == "descartada":
        raise ValueError("No se puede implementar una idea descartada.")
    if idea["status"] == "encolada":
        raise ValueError("La idea ya está encolada en staging.")

    staging_status = staging_manager.enqueue_improvement(
        title=idea["title"],
        prompt=idea["technical_prompt"],
        metadata={
            "idea_id": idea["id"],
            "category": idea["category"],
            "impact": idea["impact"],
            "source": idea["source"],
        },
    )

    conn: sqlite3.Connection | None = None
    try:
        conn = _open_connection(db_path)
        conn.execute(
            "UPDATE improvement_ideas SET status = 'encolada' WHERE id = ?;",
            (idea["id"],),
        )
        conn.commit()
        row = conn.execute(
            f"SELECT {_IDEA_SELECT_COLS} FROM improvement_ideas WHERE id = ?;",
            (idea["id"],),
        ).fetchone()
        updated = _row_to_idea(row) if row is not None else {**idea, "status": "encolada"}
    finally:
        if conn is not None:
            conn.close()

    return {
        "ok": True,
        "idea": updated,
        "staging_status": staging_status,
    }


def _list_factory_python_files(repo_root: Path) -> list[str]:
    """Return sorted relative POSIX paths of .py files strictly inside repo_root/src/agent_factory/."""
    resolved_root = repo_root.resolve()
    src_pkg = (resolved_root / "src" / "agent_factory").resolve()
    if not src_pkg.is_relative_to(resolved_root) or not src_pkg.is_dir():
        return []

    files: list[str] = []
    for item in sorted(src_pkg.rglob("*.py")):
        if item.is_symlink() or not item.is_file():
            continue
        if any(part in {"__pycache__", ".pytest_cache", ".git", ".venv"} for part in item.parts):
            continue
        if item.name.startswith(".env"):
            continue
        try:
            resolved_item = item.resolve()
            if not resolved_item.is_relative_to(src_pkg):
                continue
            rel = resolved_item.relative_to(resolved_root).as_posix()
        except (OSError, ValueError):
            continue
        files.append(rel)
        if len(files) >= 200:
            break
    return files


def _run_port_command(
    runner: Any,
    command: Sequence[str],
    *,
    cwd: Path,
    timeout_seconds: float,
    env: Mapping[str, str] | None,
) -> CommandResult:
    if hasattr(runner, "run"):
        result = runner.run(
            command,
            cwd=cwd,
            timeout_seconds=timeout_seconds,
            env=env,
        )
        if isinstance(result, CommandResult):
            return result
        return CommandResult(
            exit_code=int(getattr(result, "exit_code", 0)),
            stdout=str(getattr(result, "stdout", "") or ""),
            stderr=str(getattr(result, "stderr", "") or ""),
            timed_out=bool(getattr(result, "timed_out", False)),
        )
    if callable(runner):
        raw = runner(list(command), cwd, timeout_seconds, env)
        if isinstance(raw, CommandResult):
            return raw
        return CommandResult(exit_code=0, stdout=str(raw or ""), stderr="")
    raise TypeError("runner debe implementar .run(...) o ser invocable.")


def _fetch_recent_git_commits(
    repo_root: Path,
    *,
    commit_limit: int = 15,
    runner: Runner | None = None,
) -> list[str]:
    cap = max(1, min(50, int(commit_limit)))
    active_runner: Any = (
        runner
        if runner is not None
        else SubprocessRunner(default_timeout_seconds=DEFAULT_GIT_LOG_TIMEOUT_SECONDS)
    )
    clean_env = build_sanitized_cli_env()
    try:
        res = _run_port_command(
            active_runner,
            ["git", "log", "--oneline", "-n", str(cap)],
            cwd=repo_root,
            timeout_seconds=DEFAULT_GIT_LOG_TIMEOUT_SECONDS,
            env=clean_env,
        )
    except (CommandTimeoutError, OSError, RuntimeError, ValueError):
        return []
    if res.exit_code != 0 or not res.stdout.strip():
        return []
    return [redact_secrets(line.strip()) for line in res.stdout.splitlines() if line.strip()][:cap]


def build_ideation_context(
    db_path: Path | str | None = None,
    repo_root: Path | str | None = None,
    *,
    runner: Runner | None = None,
    commit_limit: int = 15,
    telemetry_limit: int = 5,
) -> str:
    """Build a compact context digest combining telemetry, recent commits, .py files, and all existing titles."""
    resolved_root = Path(repo_root).resolve() if repo_root is not None else _default_repo_root()
    summary = get_telemetry_summary(limit=telemetry_limit, db_path=db_path)
    commits = _fetch_recent_git_commits(
        resolved_root,
        commit_limit=commit_limit,
        runner=runner,
    )
    py_files = _list_factory_python_files(resolved_root)
    existing_titles = list_all_idea_titles(db_path=db_path)

    lines: list[str] = ["=== CONTEXTO DEL RADAR DE AUTO-MEJORAS ==="]

    totals = summary.get("totals", {})
    lines.append(
        "1. Telemetría local: "
        f"eventos={totals.get('execution_events', 0)}, "
        f"incidentes={totals.get('incidents', 0)}, "
        f"sin_resolver={totals.get('unresolved_incidents', 0)}"
    )

    recurring = summary.get("recurring_errors") or []
    if recurring:
        err_items = [f"{item['error_signature']} ({item['count']}x)" for item in recurring]
        lines.append("   - Errores recurrentes: " + ", ".join(err_items))

    slowest = summary.get("slowest_executions") or []
    if slowest:
        slow_items = [
            f"{item['component']}/{item['stage']} ({item['duration_ms']}ms)"
            for item in slowest
        ]
        lines.append("   - Etapas más lentas: " + ", ".join(slow_items))

    rates = summary.get("component_success_rates") or {}
    active_rates = [
        f"{comp}: {round(float(stats['success_rate']) * 100, 1)}% ({stats['succeeded']}/{stats['total']})"
        for comp, stats in sorted(rates.items())
        if isinstance(stats, Mapping) and int(stats.get("total", 0)) > 0 and stats.get("success_rate") is not None
    ]
    if active_rates:
        lines.append("   - Tasas de éxito por componente: " + ", ".join(active_rates))

    incidents = summary.get("recent_incidents") or []
    if incidents:
        lines.append("   - Incidentes recientes:")
        for inc in incidents:
            state = "resuelto" if inc.get("resolved") else "abierto"
            raw_err = redact_secrets(str(inc.get("raw_error") or ""))[:140]
            lines.append(f"     * [{state}] {inc.get('stage')} ({inc.get('error_signature')}): {raw_err}")

    lines.append("\n2. Últimos commits (git log --oneline -n 15):")
    if commits:
        lines.extend(f"   - {commit}" for commit in commits)
    else:
        lines.append("   - (git no disponible o sin historial)")

    lines.append("\n3. Archivos Python en src/agent_factory/:")
    if py_files:
        lines.extend(f"   - {rel_path}" for rel_path in py_files)
    else:
        lines.append("   - (sin archivos .py detectados)")

    lines.append(
        "\n4. Títulos de ideas ya existentes o descartadas (PROHIBIDO repetir o parafrasear):"
    )
    if existing_titles:
        lines.extend(f"   - {redact_secrets(title)}" for title in existing_titles)
    else:
        lines.append("   - (ninguna idea previa registrada)")

    return redact_secrets("\n".join(lines))


def _extract_candidate_list(decoded: Any) -> list[Any] | None:
    if isinstance(decoded, list):
        return decoded
    if isinstance(decoded, Mapping):
        raw_ideas = decoded.get("ideas")
        if isinstance(raw_ideas, list):
            return raw_ideas
    return None


def parse_ideas_payload(raw_text: str) -> list[dict[str, str]]:
    """Parse JSON ideas (list or {'ideas': [...]}) tolerating prose or ```json``` blocks."""
    text = str(raw_text or "").strip()
    if not text:
        return []

    candidate_list: list[Any] | None = None

    try:
        candidate_list = _extract_candidate_list(json.loads(text))
    except json.JSONDecodeError:
        candidate_list = None

    if candidate_list is None:
        for match in _FENCED_BLOCK_RE.finditer(text):
            block = match.group(1).strip()
            if not block:
                continue
            try:
                extracted = _extract_candidate_list(json.loads(block))
            except json.JSONDecodeError:
                continue
            if extracted is not None:
                candidate_list = extracted
                break

    if candidate_list is None:
        decoder = json.JSONDecoder()
        for idx, ch in enumerate(text):
            if ch not in "[{":
                continue
            try:
                decoded, _end = decoder.raw_decode(text, idx)
            except json.JSONDecodeError:
                continue
            extracted = _extract_candidate_list(decoded)
            if extracted is not None:
                candidate_list = extracted
                break

    if not candidate_list:
        return []

    valid_ideas: list[dict[str, str]] = []
    for item in candidate_list:
        normalized = _normalize_candidate_idea(item)
        if normalized is not None:
            valid_ideas.append(normalized)
    return valid_ideas


def _resolve_agy_readonly_command(
    agy_command: Sequence[str] | None,
    runner: Any,
) -> tuple[list[str] | None, str]:
    cmd = list(agy_command) if agy_command else list(_default_agy_command())
    if not cmd:
        return None, "Comando agy vacío."
    is_real_agy = Path(cmd[0]).stem.lower() == "agy"
    is_exact_subprocess = type(runner) is SubprocessRunner
    if is_exact_subprocess and is_real_agy and os.environ.get("AGY_CLI_DISABLED") == "1":
        return None, "CLI 'agy' deshabilitado en este entorno."

    if is_exact_subprocess:
        resolved = resolve_executable(cmd[0], include_host_fallbacks=True)
        if not resolved and is_real_agy and cmd[0].lower() != "agy":
            resolved = resolve_executable("agy", include_host_fallbacks=True)
        if not resolved and is_real_agy:
            return None, "CLI 'agy' no disponible en este entorno."
        if resolved:
            cmd[0] = resolved
    return cmd, ""


def _build_ideation_prompt(context_text: str, count: int) -> str:
    return (
        "MODO IDEACIÓN DE SOLO LECTURA (PROHIBIDO editar archivos o ejecutar comandos mutantes).\n"
        "Eres el Radar de Auto-Mejoras de Agent Factory.\n"
        f"Propón exactamente {count} mejoras atómicas, útiles y no repetidas basadas en el contexto real.\n\n"
        f"{context_text}\n\n"
        "REGLAS ESTRICTAS DE SALIDA:\n"
        "- Devuelve ÚNICAMENTE un JSON válido (sin comentarios ni explicaciones fuera del JSON).\n"
        '- Formato esperado: {"ideas": [{"title": "<emoji + nombre llamativo>", '
        '"category": "<categoría corta>", "impact": "Alto" | "Medio", '
        '"simple_description": "<explicación clara en español cotidiano>", '
        '"technical_prompt": "<instrucciones técnicas TDD acotadas, indicando correr ÚNICAMENTE pruebas dirigidas del módulo modificado y ruff check>"}]}\n'
        "- NO repitas ni parafrasees ningún título listado en la sección 4.\n"
        "- Cada technical_prompt debe ser atómico (<3 min), con pruebas dirigidas (`pytest -q --no-cov tests/test_<modulo>.py`) y sin modificar archivos prohibidos (.env, MASTER_PROMPT.md, AGENTS.md, config/models.toml, data/, workspaces/).\n"
    )


def generate_new_ideas(
    source: str = "manual",
    count: int = DEFAULT_IDEATION_COUNT,
    db_path: Path | str | None = None,
    runner: Runner | None = None,
    *,
    repo_root: Path | str | None = None,
    git_runner: Runner | None = None,
    agy_command: Sequence[str] | None = None,
    model: str | None = None,
    timeout_seconds: float = DEFAULT_IDEATION_TIMEOUT_SECONDS,
) -> list[dict[str, Any]]:
    """Invoke agy.EXE in read-only mode to propose new ideas, validate, deduplicate, and persist them."""
    eff_source = _validate_source(source)
    if isinstance(count, bool) or not isinstance(count, int) or count <= 0:
        raise ValueError("count debe ser un entero positivo.")
    bounded_count = min(count, MAX_IDEATION_COUNT)

    context_text = build_ideation_context(
        db_path=db_path,
        repo_root=repo_root,
        runner=git_runner,
    )
    prompt = _build_ideation_prompt(context_text, bounded_count)

    active_runner: Any = (
        runner
        if runner is not None
        else SubprocessRunner(default_timeout_seconds=timeout_seconds)
    )
    base_cmd, err_reason = _resolve_agy_readonly_command(agy_command, active_runner)
    if base_cmd is None:
        logger.warning("Ideation skipped: %s", err_reason)
        return []

    is_batch = base_cmd[0].lower().endswith((".cmd", ".bat"))
    clean_env = build_sanitized_cli_env()
    tmp_prompt: Path | None = None

    try:
        if len(prompt) <= _MAX_INLINE_PROMPT_CHARS:
            prompt_arg = re.sub(r"[\r\n]+", " ", prompt).strip() if is_batch else prompt
        else:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                suffix=".txt",
                prefix="af_idea_prompt_",
                delete=False,
            ) as tf:
                tf.write(prompt)
                tmp_prompt = Path(tf.name).resolve()
            prompt_arg = f"Lee y sigue exactamente las instrucciones del archivo: {tmp_prompt}"

        cmd = [*base_cmd, "-p", prompt_arg]
        if isinstance(model, str) and model.strip():
            cmd.extend(["--model", model.strip()])

        with tempfile.TemporaryDirectory(prefix="af_idea_radar_") as ro_cwd:
            res = _run_port_command(
                active_runner,
                cmd,
                cwd=Path(ro_cwd),
                timeout_seconds=float(timeout_seconds),
                env=clean_env,
            )
    except (CommandTimeoutError, OSError, RuntimeError, ValueError) as exc:
        logger.warning("Idea generation failed: %s", redact_secrets(str(exc)))
        return []
    finally:
        if tmp_prompt is not None:
            tmp_prompt.unlink(missing_ok=True)

    if res.exit_code != 0 or not res.stdout.strip():
        detail = redact_secrets((res.stderr or res.stdout or f"exit code {res.exit_code}").strip())
        logger.warning("Idea generation CLI returned non-zero/empty output: %s", detail)
        return []

    parsed = parse_ideas_payload(res.stdout)
    if not parsed:
        return []

    return save_ideas(
        parsed,
        source=eff_source,
        db_path=db_path,
        max_items=bounded_count,
    )


class AutoIdeaScheduler:
    """Lightweight daemon scheduler that generates auto ideas when interval elapses and factory/staging are idle."""

    def __init__(
        self,
        *,
        db_path: Path | str | None = None,
        repo_root: Path | str | None = None,
        is_factory_idle: Callable[[], bool] | None = None,
        is_staging_idle: Callable[[], bool] | None = None,
        generator: Callable[..., list[dict[str, Any]]] | None = None,
        runner: Runner | None = None,
        count: int = DEFAULT_IDEATION_COUNT,
        poll_interval_seconds: float = DEFAULT_POLL_INTERVAL_SECONDS,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self.db_path = db_path
        self.repo_root = repo_root
        self._is_factory_idle = is_factory_idle if is_factory_idle is not None else (lambda: True)
        self._is_staging_idle = is_staging_idle if is_staging_idle is not None else (lambda: True)
        self._generator = generator
        self._runner = runner
        self.count = max(1, min(int(count), MAX_IDEATION_COUNT))
        self.poll_interval_seconds = max(0.005, float(poll_interval_seconds))
        self._clock: Callable[[], float] = clock if clock is not None else time.monotonic

        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_run_at: float = self._clock()

    @property
    def is_running(self) -> bool:
        with self._lock:
            return self._thread is not None and self._thread.is_alive()

    def _safe_check_idle(self, callback: Callable[[], bool], label: str) -> bool:
        try:
            return bool(callback())
        except Exception as exc:  # noqa: BLE001
            logger.debug("Idle check '%s' failed (treating as busy): %s", label, exc)
            return False

    def _invoke_generator(self) -> list[dict[str, Any]]:
        if self._generator is not None:
            return list(
                self._generator(
                    source="auto",
                    count=self.count,
                    db_path=self.db_path,
                )
            )
        return generate_new_ideas(
            source="auto",
            count=self.count,
            db_path=self.db_path,
            runner=self._runner,
            repo_root=self.repo_root,
        )

    def run_once_if_due(self, *, force: bool = False) -> list[dict[str, Any]]:
        """Run one auto-ideation cycle if enabled, due, and both factory and staging are idle."""
        interval_hours = get_auto_interval_hours(db_path=self.db_path)
        if interval_hours <= 0.0:
            return []

        now = self._clock()
        interval_seconds = interval_hours * 3600.0
        with self._lock:
            elapsed = now - self._last_run_at
            if not force and elapsed < interval_seconds:
                return []

        if not self._safe_check_idle(self._is_factory_idle, "factory"):
            return []
        if not self._safe_check_idle(self._is_staging_idle, "staging"):
            return []

        created = self._invoke_generator()
        with self._lock:
            self._last_run_at = self._clock()
        return created

    def _worker_loop(self) -> None:
        while not self._stop_event.wait(timeout=self.poll_interval_seconds):
            try:
                self.run_once_if_due()
            except Exception as exc:  # noqa: BLE001
                logger.warning("AutoIdeaScheduler cycle error: %s", redact_secrets(str(exc)))

    def start(self) -> AutoIdeaScheduler:
        """Start the daemon scheduler thread idempotently."""
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return self
            self._stop_event.clear()
            self._last_run_at = self._clock()
            self._thread = threading.Thread(
                target=self._worker_loop,
                name="liwi-auto-idea-worker",
                daemon=True,
            )
            self._thread.start()
        return self

    def stop(self, timeout: float = 5.0) -> None:
        """Signal the daemon worker to stop and wait cleanly until it exits."""
        with self._lock:
            self._stop_event.set()
            thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=max(0.0, float(timeout)))
        with self._lock:
            if self._thread is not None and not self._thread.is_alive():
                self._thread = None


def start_auto_idea_worker(
    *,
    db_path: Path | str | None = None,
    repo_root: Path | str | None = None,
    is_factory_idle: Callable[[], bool] | None = None,
    is_staging_idle: Callable[[], bool] | None = None,
    generator: Callable[..., list[dict[str, Any]]] | None = None,
    runner: Runner | None = None,
    count: int = DEFAULT_IDEATION_COUNT,
    poll_interval_seconds: float = DEFAULT_POLL_INTERVAL_SECONDS,
    clock: Callable[[], float] | None = None,
) -> AutoIdeaScheduler:
    """Create, start, and return a lightweight daemon AutoIdeaScheduler."""
    scheduler = AutoIdeaScheduler(
        db_path=db_path,
        repo_root=repo_root,
        is_factory_idle=is_factory_idle,
        is_staging_idle=is_staging_idle,
        generator=generator,
        runner=runner,
        count=count,
        poll_interval_seconds=poll_interval_seconds,
        clock=clock,
    )
    return scheduler.start()
