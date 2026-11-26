"""Self-Improvement Idea Radar engine: SQLite persistence, context builder, read-only agy ideation, and optional daemon worker."""

from __future__ import annotations

import json
import logging
import math
import os
import re
import sqlite3
import sys
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
    list_triage_solutions,
    update_triage_solution,
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
DEFAULT_AUTONOMOUS_ENABLED: bool = False
DEFAULT_AUTONOMOUS_COOLDOWN_MINUTES: int = 25
MIN_AUTONOMOUS_COOLDOWN_MINUTES: int = 1
MAX_AUTONOMOUS_COOLDOWN_MINUTES: int = 1440
AUTONOMOUS_MAX_CONSECUTIVE_FAILURES: int = 2
AUTONOMOUS_HISTORY_VALID_STATUSES: frozenset[str] = frozenset({"activa", "revertida"})
DEFAULT_AUTONOMOUS_WAIT_TIMEOUT_SECONDS: float = 1800.0
DEFAULT_AUTONOMOUS_VERIFY_TIMEOUT_SECONDS: float = 300.0

_AUTO_INTERVAL_SETTING_KEY = "auto_interval_hours"
_MAX_INLINE_PROMPT_CHARS = 6000
_FENCED_BLOCK_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.IGNORECASE | re.DOTALL)
_NON_ALNUM_RE = re.compile(r"[^a-z0-9\s]+")
_SAFE_COMMIT_HASH_RE = re.compile(r"^[0-9a-fA-F]{4,64}$")
_IDEA_SELECT_COLS = (
    "id, created_at, title, category, impact, simple_description, "
    "technical_prompt, status, source"
)
_AUTONOMOUS_HISTORY_SELECT_COLS = (
    "id, applied_at, idea_id, title, simple_description, commit_hash, status"
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
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS autonomous_state (
            id INTEGER PRIMARY KEY CHECK(id = 1),
            autonomous_enabled INTEGER NOT NULL DEFAULT 0 CHECK(autonomous_enabled IN (0, 1)),
            cooldown_minutes INTEGER NOT NULL DEFAULT 25 CHECK(cooldown_minutes >= 1),
            consecutive_failures INTEGER NOT NULL DEFAULT 0 CHECK(consecutive_failures >= 0),
            updated_at TEXT NOT NULL
        );
        """
    )
    conn.execute(
        """
        INSERT OR IGNORE INTO autonomous_state (
            id,
            autonomous_enabled,
            cooldown_minutes,
            consecutive_failures,
            updated_at
        ) VALUES (1, 0, ?, 0, ?);
        """,
        (DEFAULT_AUTONOMOUS_COOLDOWN_MINUTES, datetime.now(UTC).isoformat()),
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS autonomous_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            applied_at TEXT NOT NULL,
            idea_id INTEGER,
            title TEXT NOT NULL,
            simple_description TEXT NOT NULL DEFAULT '',
            commit_hash TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'activa'
                CHECK(status IN ('activa', 'revertida'))
        );
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_autonomous_history_applied_at "
        "ON autonomous_history(applied_at DESC, id DESC);"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_autonomous_history_status "
        "ON autonomous_history(status);"
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


# ---------------------------------------------------------------------------
# 24/7 Autonomous Mode: SQLite persistence and AutonomousEngine
# ---------------------------------------------------------------------------


def _validate_autonomous_history_status(status: str) -> str:
    clean = str(status or "").strip().lower()
    if clean not in AUTONOMOUS_HISTORY_VALID_STATUSES:
        raise ValueError(
            f"Estado de historial autónomo inválido {status!r}; "
            f"se esperaba uno de {sorted(AUTONOMOUS_HISTORY_VALID_STATUSES)}."
        )
    return clean


def _validate_commit_hash(commit_hash: str) -> str:
    clean = str(commit_hash or "").strip()
    if not clean or not _SAFE_COMMIT_HASH_RE.fullmatch(clean):
        raise ValueError(f"commit_hash inválido: {commit_hash!r}")
    return clean


def _row_to_autonomous_state(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "autonomous_enabled": bool(row["autonomous_enabled"]),
        "cooldown_minutes": int(row["cooldown_minutes"]),
        "consecutive_failures": int(row["consecutive_failures"]),
        "updated_at": str(row["updated_at"]),
    }


def _row_to_autonomous_history(row: sqlite3.Row) -> dict[str, Any]:
    raw_idea_id = row["idea_id"]
    idea_id: int | str | None
    if raw_idea_id is None:
        idea_id = None
    elif isinstance(raw_idea_id, int) and not isinstance(raw_idea_id, bool):
        idea_id = raw_idea_id
    else:
        text_id = str(raw_idea_id).strip()
        idea_id = int(text_id) if text_id.isdigit() else (text_id or None)
    return {
        "id": int(row["id"]),
        "applied_at": str(row["applied_at"]),
        "idea_id": idea_id,
        "title": str(row["title"]),
        "simple_description": str(row["simple_description"]),
        "commit_hash": str(row["commit_hash"]),
        "status": str(row["status"]),
    }


def get_autonomous_state(db_path: Path | str | None = None) -> dict[str, Any]:
    """Return persisted 24/7 autonomous mode state (singleton row id=1)."""
    conn: sqlite3.Connection | None = None
    try:
        conn = _open_connection(db_path)
        row = conn.execute(
            "SELECT autonomous_enabled, cooldown_minutes, consecutive_failures, updated_at "
            "FROM autonomous_state WHERE id = 1;"
        ).fetchone()
        if row is None:
            return {
                "autonomous_enabled": DEFAULT_AUTONOMOUS_ENABLED,
                "cooldown_minutes": DEFAULT_AUTONOMOUS_COOLDOWN_MINUTES,
                "consecutive_failures": 0,
                "updated_at": datetime.now(UTC).isoformat(),
            }
        return _row_to_autonomous_state(row)
    finally:
        if conn is not None:
            conn.close()


def set_autonomous_config(
    autonomous_enabled: bool | None = None,
    cooldown_minutes: int | None = None,
    consecutive_failures: int | None = None,
    db_path: Path | str | None = None,
) -> dict[str, Any]:
    """Validate and update autonomous_state fields atomically in SQLite."""
    updates: list[str] = []
    params: list[Any] = []

    if autonomous_enabled is not None:
        if not isinstance(autonomous_enabled, bool):
            raise TypeError("autonomous_enabled debe ser un booleano (True o False).")
        updates.append("autonomous_enabled = ?")
        params.append(1 if autonomous_enabled else 0)

    if cooldown_minutes is not None:
        if isinstance(cooldown_minutes, bool) or not isinstance(cooldown_minutes, int):
            raise TypeError("cooldown_minutes debe ser un entero positivo.")
        if (
            cooldown_minutes < MIN_AUTONOMOUS_COOLDOWN_MINUTES
            or cooldown_minutes > MAX_AUTONOMOUS_COOLDOWN_MINUTES
        ):
            raise ValueError(
                f"cooldown_minutes debe estar entre {MIN_AUTONOMOUS_COOLDOWN_MINUTES} "
                f"y {MAX_AUTONOMOUS_COOLDOWN_MINUTES}."
            )
        updates.append("cooldown_minutes = ?")
        params.append(cooldown_minutes)

    if consecutive_failures is not None:
        if isinstance(consecutive_failures, bool) or not isinstance(consecutive_failures, int):
            raise TypeError("consecutive_failures debe ser un entero >= 0.")
        if consecutive_failures < 0:
            raise ValueError("consecutive_failures debe ser >= 0.")
        updates.append("consecutive_failures = ?")
        params.append(consecutive_failures)

    conn: sqlite3.Connection | None = None
    try:
        conn = _open_connection(db_path)
        if updates:
            updates.append("updated_at = ?")
            params.append(datetime.now(UTC).isoformat())
            conn.execute(
                f"UPDATE autonomous_state SET {', '.join(updates)} WHERE id = 1;",
                tuple(params),
            )
            conn.commit()
        row = conn.execute(
            "SELECT autonomous_enabled, cooldown_minutes, consecutive_failures, updated_at "
            "FROM autonomous_state WHERE id = 1;"
        ).fetchone()
        if row is None:
            raise RuntimeError("No se encontró el registro singleton autonomous_state (id=1).")
        return _row_to_autonomous_state(row)
    finally:
        if conn is not None:
            conn.close()


def increment_autonomous_failures(
    db_path: Path | str | None = None,
    *,
    max_failures: int = AUTONOMOUS_MAX_CONSECUTIVE_FAILURES,
) -> dict[str, Any]:
    """Atomically increment consecutive_failures and trip circuit breaker at max_failures."""
    limit = max(1, int(max_failures))
    now_iso = datetime.now(UTC).isoformat()
    conn: sqlite3.Connection | None = None
    try:
        conn = _open_connection(db_path)
        conn.execute(
            """
            UPDATE autonomous_state
            SET consecutive_failures = consecutive_failures + 1,
                autonomous_enabled = CASE
                    WHEN consecutive_failures + 1 >= ? THEN 0
                    ELSE autonomous_enabled
                END,
                updated_at = ?
            WHERE id = 1;
            """,
            (limit, now_iso),
        )
        conn.commit()
        row = conn.execute(
            "SELECT autonomous_enabled, cooldown_minutes, consecutive_failures, updated_at "
            "FROM autonomous_state WHERE id = 1;"
        ).fetchone()
        if row is None:
            raise RuntimeError("No se encontró el registro singleton autonomous_state (id=1).")
        return _row_to_autonomous_state(row)
    finally:
        if conn is not None:
            conn.close()


def record_autonomous_application(
    title: str,
    commit_hash: str,
    simple_description: str = "",
    idea_id: int | str | None = None,
    *,
    status: str = "activa",
    applied_at: str | None = None,
    db_path: Path | str | None = None,
) -> dict[str, Any]:
    """Persist an applied autonomous improvement record with its commit_hash."""
    clean_title = redact_secrets(str(title or "").strip())[:200]
    if not clean_title:
        raise ValueError("El título ('title') de la mejora autónoma es obligatorio.")
    clean_hash = _validate_commit_hash(commit_hash)
    clean_desc = redact_secrets(str(simple_description or "").strip())[:1000]
    eff_status = _validate_autonomous_history_status(status)
    eff_applied_at = (
        applied_at.strip()
        if isinstance(applied_at, str) and applied_at.strip()
        else datetime.now(UTC).isoformat()
    )

    stored_idea_id: int | str | None
    if idea_id is None:
        stored_idea_id = None
    elif isinstance(idea_id, bool):
        raise TypeError("idea_id no puede ser booleano.")
    elif isinstance(idea_id, int):
        stored_idea_id = idea_id
    else:
        text_id = str(idea_id).strip()
        stored_idea_id = int(text_id) if text_id.isdigit() else (text_id or None)

    conn: sqlite3.Connection | None = None
    try:
        conn = _open_connection(db_path)
        cursor = conn.execute(
            """
            INSERT INTO autonomous_history (
                applied_at,
                idea_id,
                title,
                simple_description,
                commit_hash,
                status
            ) VALUES (?, ?, ?, ?, ?, ?);
            """,
            (
                eff_applied_at,
                stored_idea_id,
                clean_title,
                clean_desc,
                clean_hash,
                eff_status,
            ),
        )
        inserted_id = cursor.lastrowid
        conn.commit()
        row = conn.execute(
            f"SELECT {_AUTONOMOUS_HISTORY_SELECT_COLS} FROM autonomous_history WHERE id = ?;",
            (inserted_id,),
        ).fetchone()
        if row is None:
            raise RuntimeError("No se pudo recuperar la entrada recién registrada en autonomous_history.")
        return _row_to_autonomous_history(row)
    finally:
        if conn is not None:
            conn.close()


def get_autonomous_history_entry(
    entry_id: int,
    db_path: Path | str | None = None,
) -> dict[str, Any] | None:
    """Return a single autonomous_history record by ID, or None if missing/invalid."""
    valid_id = _validate_idea_id(entry_id)
    if valid_id is None:
        return None
    conn: sqlite3.Connection | None = None
    try:
        conn = _open_connection(db_path)
        row = conn.execute(
            f"SELECT {_AUTONOMOUS_HISTORY_SELECT_COLS} FROM autonomous_history WHERE id = ?;",
            (valid_id,),
        ).fetchone()
        return _row_to_autonomous_history(row) if row is not None else None
    finally:
        if conn is not None:
            conn.close()


def list_autonomous_history(
    status: str | None = None,
    db_path: Path | str | None = None,
    *,
    limit: int = DEFAULT_LIST_LIMIT,
) -> list[dict[str, Any]]:
    """List autonomous_history records ordered most recent first, optionally filtered by status."""
    clean_status = _validate_autonomous_history_status(status) if status is not None else None
    if isinstance(limit, bool) or not isinstance(limit, int):
        bounded_limit = DEFAULT_LIST_LIMIT
    else:
        bounded_limit = max(1, min(limit, MAX_LIST_LIMIT))

    params: list[Any] = []
    where_sql = ""
    if clean_status is not None:
        where_sql = "WHERE status = ?"
        params.append(clean_status)
    params.append(bounded_limit)

    conn: sqlite3.Connection | None = None
    try:
        conn = _open_connection(db_path)
        rows = conn.execute(
            f"SELECT {_AUTONOMOUS_HISTORY_SELECT_COLS} FROM autonomous_history "
            f"{where_sql} ORDER BY applied_at DESC, id DESC LIMIT ?;",
            tuple(params),
        ).fetchall()
        return [_row_to_autonomous_history(row) for row in rows]
    finally:
        if conn is not None:
            conn.close()


def mark_autonomous_reverted(
    entry_id: int,
    db_path: Path | str | None = None,
) -> dict[str, Any] | None:
    """Mark an autonomous_history entry as 'revertida' and discard its linked idea/triage item."""
    valid_id = _validate_idea_id(entry_id)
    if valid_id is None:
        return None

    conn: sqlite3.Connection | None = None
    try:
        conn = _open_connection(db_path)
        row = conn.execute(
            f"SELECT {_AUTONOMOUS_HISTORY_SELECT_COLS} FROM autonomous_history WHERE id = ?;",
            (valid_id,),
        ).fetchone()
        if row is None:
            return None

        entry = _row_to_autonomous_history(row)
        conn.execute(
            "UPDATE autonomous_history SET status = 'revertida' WHERE id = ?;",
            (valid_id,),
        )

        linked_id = entry["idea_id"]
        if isinstance(linked_id, int) and linked_id > 0:
            conn.execute(
                "UPDATE improvement_ideas SET status = 'descartada' WHERE id = ?;",
                (linked_id,),
            )
        elif isinstance(linked_id, str) and linked_id.startswith("triage:"):
            suffix = linked_id.split(":", 1)[1].strip()
            if suffix.isdigit():
                triage_table = conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name='triage_solutions';"
                ).fetchone()
                if triage_table is not None:
                    conn.execute(
                        "UPDATE triage_solutions SET status = 'descartada' WHERE id = ?;",
                        (int(suffix),),
                    )

        conn.commit()
        updated_row = conn.execute(
            f"SELECT {_AUTONOMOUS_HISTORY_SELECT_COLS} FROM autonomous_history WHERE id = ?;",
            (valid_id,),
        ).fetchone()
        return _row_to_autonomous_history(updated_row) if updated_row is not None else None
    finally:
        if conn is not None:
            conn.close()


def select_next_triage_solution(
    db_path: Path | str | None = None,
) -> dict[str, Any] | None:
    """Return the highest-priority ready ('lista') factory error_triage solution, or None."""
    solutions = list_triage_solutions(
        status="lista",
        scope="factory",
        limit=50,
        db_path=db_path,
    )
    if not solutions:
        return None
    ranked = sorted(
        solutions,
        key=lambda item: (
            0 if str(item.get("technical_prompt") or "").strip() else 1,
            -int(item.get("occurrences", 1)),
            int(item.get("id", 0)),
        ),
    )
    return ranked[0]


def select_next_autonomous_idea(
    db_path: Path | str | None = None,
) -> dict[str, Any] | None:
    """Return the highest-impact pending ('nueva') idea from improvement_ideas, or None."""
    conn: sqlite3.Connection | None = None
    try:
        conn = _open_connection(db_path)
        row = conn.execute(
            f"""
            SELECT {_IDEA_SELECT_COLS}
            FROM improvement_ideas
            WHERE status = 'nueva'
            ORDER BY
                CASE WHEN impact = 'Alto' THEN 0 ELSE 1 END ASC,
                created_at ASC,
                id ASC
            LIMIT 1;
            """
        ).fetchone()
        return _row_to_idea(row) if row is not None else None
    finally:
        if conn is not None:
            conn.close()


def _interpret_action_result(result: Any, default_error: str) -> tuple[bool, str]:
    if isinstance(result, CommandResult):
        if result.exit_code == 0 and not result.timed_out:
            return True, ""
        detail = redact_secrets((result.stderr or result.stdout or default_error).strip())
        return False, detail or default_error
    if isinstance(result, Mapping):
        if "ok" in result:
            ok = bool(result["ok"])
        elif "exit_code" in result:
            ok = int(result["exit_code"]) == 0 and not bool(result.get("timed_out", False))
        else:
            ok = True
        if ok:
            return True, ""
        detail = redact_secrets(
            str(result.get("error") or result.get("stderr") or default_error).strip()
        )
        return False, detail or default_error
    if isinstance(result, bool):
        return (True, "") if result else (False, default_error)
    return True, ""


def _extract_promoted_commit_hash(result: Any) -> tuple[bool, str | None, str]:
    if isinstance(result, str):
        clean = result.strip()
        if clean and _SAFE_COMMIT_HASH_RE.fullmatch(clean):
            return True, clean, ""
        return False, None, f"commit_hash inválido devuelto por el promotor: {result!r}"
    if isinstance(result, Mapping):
        if result.get("ok") is False or result.get("promoted") is False:
            detail = redact_secrets(
                str(result.get("error") or "La promoción a main no se completó.").strip()
            )
            return False, None, detail
        raw_hash = result.get("commit_hash") or result.get("commit_sha") or result.get("hash")
        if not raw_hash and isinstance(result.get("commits"), Sequence) and result["commits"]:
            first = result["commits"][0]
            if isinstance(first, Mapping):
                raw_hash = first.get("hash")
        clean = str(raw_hash or "").strip()
        if clean and _SAFE_COMMIT_HASH_RE.fullmatch(clean):
            return True, clean, ""
        return False, None, "El promotor a main no devolvió un commit_hash válido."
    return False, None, "El promotor a main no devolvió un commit_hash válido."


class AutonomousEngine:
    """24/7 Autonomous self-improvement daemon engine with 3 safety brakes."""

    def __init__(
        self,
        *,
        db_path: Path | str | None = None,
        repo_root: Path | str | None = None,
        staging_manager: Any | None = None,
        base_branch: str = "main",
        activity_checker: Callable[[], str | None] | None = None,
        has_active_builds: Callable[[], bool] | None = None,
        has_running_local_apps: Callable[[], bool] | None = None,
        triage_provider: Callable[[], dict[str, Any] | None] | None = None,
        idea_selector: Callable[[], dict[str, Any] | None] | None = None,
        staging_executor: Callable[[dict[str, Any]], Any] | None = None,
        main_promoter: Callable[[dict[str, Any]], Any] | None = None,
        revert_runner: Callable[[str], Any] | None = None,
        verify_runner: Callable[[str], Any] | None = None,
        command_runner: Runner | None = None,
        max_consecutive_failures: int = AUTONOMOUS_MAX_CONSECUTIVE_FAILURES,
        cooldown_unit_seconds: float = 60.0,
        idle_poll_seconds: float = 1.0,
        staging_wait_timeout_seconds: float = DEFAULT_AUTONOMOUS_WAIT_TIMEOUT_SECONDS,
        verify_timeout_seconds: float = DEFAULT_AUTONOMOUS_VERIFY_TIMEOUT_SECONDS,
    ) -> None:
        self.db_path = db_path
        self.repo_root = (
            Path(repo_root).resolve() if repo_root is not None else _default_repo_root()
        )
        self._staging_manager = staging_manager
        self.base_branch = str(base_branch or "main").strip() or "main"
        self._activity_checker = activity_checker
        self._has_active_builds = has_active_builds
        self._has_running_local_apps = has_running_local_apps
        self._triage_provider = triage_provider
        self._idea_selector = idea_selector
        self._staging_executor = staging_executor
        self._main_promoter = main_promoter
        self._revert_runner = revert_runner
        self._verify_runner = verify_runner
        self._command_runner = command_runner
        self._max_consecutive_failures = max(1, int(max_consecutive_failures))
        self._cooldown_unit_seconds = max(0.001, float(cooldown_unit_seconds))
        self._idle_poll_seconds = max(0.01, float(idle_poll_seconds))
        self._staging_wait_timeout_seconds = max(1.0, float(staging_wait_timeout_seconds))
        self._verify_timeout_seconds = max(5.0, float(verify_timeout_seconds))

        self._lock = threading.Lock()
        self._cycle_lock = threading.Lock()
        self._stop_event = threading.Event()
        self._wakeup_event = threading.Event()
        self._config_generation: int = 0
        self._thread: threading.Thread | None = None
        self._last_cycle_result: dict[str, Any] | None = None

    @property
    def is_running(self) -> bool:
        with self._lock:
            return self._thread is not None and self._thread.is_alive()

    @property
    def last_cycle_result(self) -> dict[str, Any] | None:
        with self._lock:
            return dict(self._last_cycle_result) if self._last_cycle_result is not None else None

    def _set_last_cycle_result(self, result: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            self._last_cycle_result = dict(result)
        return result

    def notify_config_changed(self) -> None:
        """Wake the daemon loop immediately when configuration or enabled state changes."""
        with self._lock:
            self._config_generation += 1
            self._wakeup_event.set()

    def get_state(self) -> dict[str, Any]:
        """Return persisted autonomous state combined with live worker and busy status."""
        state = get_autonomous_state(db_path=self.db_path)
        return {
            **state,
            "is_running": self.is_running,
            "busy_reason": self.get_busy_reason(),
            "last_cycle_result": self.last_cycle_result,
        }

    def update_config(
        self,
        *,
        autonomous_enabled: bool | None = None,
        cooldown_minutes: int | None = None,
        consecutive_failures: int | None = None,
    ) -> dict[str, Any]:
        """Update autonomous configuration in SQLite and wake the daemon loop immediately."""
        eff_failures = consecutive_failures
        if autonomous_enabled is True and eff_failures is None:
            current = get_autonomous_state(db_path=self.db_path)
            if int(current["consecutive_failures"]) >= self._max_consecutive_failures:
                eff_failures = 0

        updated = set_autonomous_config(
            autonomous_enabled=autonomous_enabled,
            cooldown_minutes=cooldown_minutes,
            consecutive_failures=eff_failures,
            db_path=self.db_path,
        )
        self.notify_config_changed()
        return updated

    def _resolve_staging_manager(self) -> Any:
        if self._staging_manager is not None:
            return self._staging_manager
        from agent_factory.staging_manager import get_staging_manager

        return get_staging_manager(self.repo_root)

    def _active_command_runner(self) -> Any:
        if self._command_runner is not None:
            return self._command_runner
        return SubprocessRunner(default_timeout_seconds=self._verify_timeout_seconds)

    def get_busy_reason(self) -> str | None:
        """Brake 1: return a non-empty reason if user builds, local apps, or staging are active."""
        if self._activity_checker is not None:
            try:
                reason = self._activity_checker()
            except Exception as exc:  # noqa: BLE001
                logger.debug("Autonomous activity_checker failed (treating as busy): %s", exc)
                return "error verificando actividad de la fábrica"
            if isinstance(reason, str):
                if reason.strip():
                    return reason.strip()
            elif reason:
                return str(reason).strip() or "actividad en curso"

        if self._has_active_builds is not None:
            try:
                if bool(self._has_active_builds()):
                    return "build de proyecto en curso"
            except Exception as exc:  # noqa: BLE001
                logger.debug("Autonomous has_active_builds failed (treating as busy): %s", exc)
                return "error verificando builds activos"

        if self._has_running_local_apps is not None:
            try:
                if bool(self._has_running_local_apps()):
                    return "app local encendida"
            except Exception as exc:  # noqa: BLE001
                logger.debug("Autonomous has_running_local_apps failed (treating as busy): %s", exc)
                return "error verificando apps locales"

        if self._staging_manager is not None:
            try:
                status = self._staging_manager.get_staging_status()
            except Exception as exc:  # noqa: BLE001
                logger.debug("Autonomous staging status check failed (treating as busy): %s", exc)
                return "error verificando staging_manager"
            if isinstance(status, Mapping):
                if status.get("is_building"):
                    return "mejora en construcción en staging_manager"
                if int(status.get("queue_size", 0)) > 0:
                    return "tareas pendientes en cola de staging_manager"
                if int(status.get("pending_count", 0)) > 0:
                    return "commits pendientes por promover en staging_manager"

        return None

    def _select_next_work_item(self) -> dict[str, Any] | None:
        triage_item: dict[str, Any] | None
        if self._triage_provider is not None:
            triage_item = self._triage_provider()
        else:
            triage_item = select_next_triage_solution(db_path=self.db_path)

        if isinstance(triage_item, Mapping):
            triage_id = triage_item.get("id")
            title = redact_secrets(str(triage_item.get("title") or "Corrección de Auto-Triage").strip())
            desc = redact_secrets(
                str(
                    triage_item.get("proposed_fix")
                    or triage_item.get("root_cause")
                    or triage_item.get("raw_error")
                    or ""
                ).strip()
            )
            prompt = redact_secrets(
                str(triage_item.get("technical_prompt") or triage_item.get("proposed_fix") or "").strip()
            )
            return {
                "source": "error_triage",
                "triage_id": triage_id if isinstance(triage_id, int) and not isinstance(triage_id, bool) else None,
                "idea_id": f"triage:{triage_id}" if triage_id is not None else None,
                "title": title,
                "simple_description": desc,
                "technical_prompt": prompt,
                "raw_item": dict(triage_item),
            }

        idea_item: dict[str, Any] | None
        if self._idea_selector is not None:
            idea_item = self._idea_selector()
        else:
            idea_item = select_next_autonomous_idea(db_path=self.db_path)

        if isinstance(idea_item, Mapping):
            raw_id = idea_item.get("id")
            idea_id = raw_id if isinstance(raw_id, int) and not isinstance(raw_id, bool) else None
            return {
                "source": "idea_engine",
                "triage_id": None,
                "idea_id": idea_id,
                "title": redact_secrets(str(idea_item.get("title") or "Mejora autónoma").strip()),
                "simple_description": redact_secrets(str(idea_item.get("simple_description") or "").strip()),
                "technical_prompt": redact_secrets(str(idea_item.get("technical_prompt") or "").strip()),
                "raw_item": dict(idea_item),
            }

        return None

    def _default_staging_execute(self, work_item: dict[str, Any]) -> dict[str, Any]:
        manager = self._resolve_staging_manager()
        manager.enqueue_improvement(
            title=work_item["title"],
            prompt=work_item["technical_prompt"],
            metadata={
                "autonomous": True,
                "source": work_item["source"],
                "idea_id": work_item["idea_id"],
                "triage_id": work_item["triage_id"],
            },
        )
        finished = manager.wait_until_idle(timeout=self._staging_wait_timeout_seconds)
        if not finished:
            return {"ok": False, "error": "Tiempo de espera agotado construyendo en .liwi-staging."}
        status = manager.get_staging_status()
        if isinstance(status, Mapping):
            if status.get("last_error"):
                return {"ok": False, "error": str(status["last_error"])}
            if int(status.get("pending_count", 0)) <= 0:
                return {"ok": False, "error": "No se produjeron commits verificados en .liwi-staging."}
        return {"ok": True, "staging_status": status}

    def _default_promote_to_main(self, _work_item: dict[str, Any]) -> dict[str, Any]:
        manager = self._resolve_staging_manager()
        promote_result = manager.promote(trigger_restart=False, rebase_on_diverge=True)
        if not isinstance(promote_result, Mapping) or not promote_result.get("promoted"):
            return {"ok": False, "error": "No había commits listos para promover a main."}
        commit_hash = ""
        commits = promote_result.get("commits")
        if isinstance(commits, Sequence) and commits and isinstance(commits[0], Mapping):
            commit_hash = str(commits[0].get("hash") or "").strip()
        if not commit_hash:
            res = _run_port_command(
                self._active_command_runner(),
                ["git", "rev-parse", "HEAD"],
                cwd=self.repo_root,
                timeout_seconds=DEFAULT_GIT_LOG_TIMEOUT_SECONDS,
                env=build_sanitized_cli_env(),
            )
            if res.exit_code == 0:
                commit_hash = res.stdout.strip()
        return {"ok": bool(commit_hash), "promoted": True, "commit_hash": commit_hash}

    def _mark_work_item_applied(self, work_item: dict[str, Any]) -> None:
        if work_item.get("source") == "idea_engine":
            idea_id = _validate_idea_id(work_item.get("idea_id"))
            if idea_id is not None:
                conn: sqlite3.Connection | None = None
                try:
                    conn = _open_connection(self.db_path)
                    conn.execute(
                        "UPDATE improvement_ideas SET status = 'encolada' "
                        "WHERE id = ? AND status = 'nueva';",
                        (idea_id,),
                    )
                    conn.commit()
                finally:
                    if conn is not None:
                        conn.close()
        elif work_item.get("source") == "error_triage":
            triage_id = _validate_idea_id(work_item.get("triage_id"))
            if triage_id is not None:
                update_triage_solution(
                    triage_id,
                    status="encolada",
                    db_path=self.db_path,
                )

    def _record_cycle_failure(self, error_msg: str, work_item: dict[str, Any]) -> dict[str, Any]:
        new_state = increment_autonomous_failures(
            db_path=self.db_path,
            max_failures=self._max_consecutive_failures,
        )
        tripped = not bool(new_state["autonomous_enabled"])
        if tripped:
            self.notify_config_changed()
        return self._set_last_cycle_result(
            {
                "status": "failed",
                "executed": True,
                "promoted": False,
                "error": redact_secrets(error_msg),
                "consecutive_failures": int(new_state["consecutive_failures"]),
                "circuit_breaker_tripped": tripped,
                "work_item": work_item,
            }
        )

    def run_cycle(self) -> dict[str, Any]:
        """Execute a single autonomous cycle enforcing all 3 safety brakes."""
        with self._cycle_lock:
            state = get_autonomous_state(db_path=self.db_path)
            if not state["autonomous_enabled"] or self._stop_event.is_set():
                return self._set_last_cycle_result(
                    {"status": "disabled", "executed": False, "promoted": False}
                )

            # Brake 1: absolute user priority
            busy_reason = self.get_busy_reason()
            if busy_reason is not None:
                return self._set_last_cycle_result(
                    {
                        "status": "skipped_busy",
                        "executed": False,
                        "promoted": False,
                        "busy_reason": busy_reason,
                    }
                )

            work_item = self._select_next_work_item()
            if work_item is None:
                return self._set_last_cycle_result(
                    {"status": "idle_no_work", "executed": False, "promoted": False}
                )

            # Re-verify Brake 2 immediately before staging execution
            if not get_autonomous_state(db_path=self.db_path)["autonomous_enabled"] or self._stop_event.is_set():
                return self._set_last_cycle_result(
                    {"status": "disabled", "executed": False, "promoted": False}
                )

            # Build and verify inside isolated .liwi-staging worktree
            try:
                if self._staging_executor is not None:
                    exec_raw = self._staging_executor(work_item)
                else:
                    exec_raw = self._default_staging_execute(work_item)
                exec_ok, exec_err = _interpret_action_result(
                    exec_raw,
                    "La ejecución o verificación en .liwi-staging falló.",
                )
            except Exception as exc:  # noqa: BLE001
                exec_ok = False
                exec_err = redact_secrets(str(exc) or type(exc).__name__)

            if not exec_ok:
                return self._record_cycle_failure(exec_err, work_item)

            # Brake 2: immediate shutoff during build parks result in .liwi-staging without promoting
            mid_state = get_autonomous_state(db_path=self.db_path)
            if not mid_state["autonomous_enabled"] or self._stop_event.is_set():
                return self._set_last_cycle_result(
                    {
                        "status": "parked_in_staging",
                        "executed": True,
                        "promoted": False,
                        "work_item": work_item,
                    }
                )

            # Promote verified .liwi-staging commit to main
            try:
                if self._main_promoter is not None:
                    promote_raw = self._main_promoter(work_item)
                else:
                    promote_raw = self._default_promote_to_main(work_item)
                prom_ok, commit_hash, prom_err = _extract_promoted_commit_hash(promote_raw)
            except Exception as exc:  # noqa: BLE001
                prom_ok = False
                commit_hash = None
                prom_err = redact_secrets(str(exc) or type(exc).__name__)

            if not prom_ok or not commit_hash:
                return self._record_cycle_failure(
                    prom_err or "Error promoviendo mejora a main.",
                    work_item,
                )

            history_entry = record_autonomous_application(
                title=work_item["title"],
                commit_hash=commit_hash,
                simple_description=work_item["simple_description"],
                idea_id=work_item["idea_id"],
                db_path=self.db_path,
            )
            set_autonomous_config(consecutive_failures=0, db_path=self.db_path)
            self._mark_work_item_applied(work_item)

            return self._set_last_cycle_result(
                {
                    "status": "applied",
                    "executed": True,
                    "promoted": True,
                    "commit_hash": commit_hash,
                    "history_entry": history_entry,
                    "work_item": work_item,
                }
            )

    def _worker_loop(self) -> None:
        while not self._stop_event.is_set():
            with self._lock:
                seen_gen = self._config_generation

            state = get_autonomous_state(db_path=self.db_path)
            if not state["autonomous_enabled"]:
                with self._lock:
                    if self._stop_event.is_set():
                        break
                    if self._config_generation != seen_gen:
                        continue
                    self._wakeup_event.clear()
                self._wakeup_event.wait(timeout=self._idle_poll_seconds)
                continue

            try:
                self.run_cycle()
            except Exception as exc:  # noqa: BLE001
                logger.warning("AutonomousEngine cycle error: %s", redact_secrets(str(exc)))

            if self._stop_event.is_set():
                break

            post_state = get_autonomous_state(db_path=self.db_path)
            if not post_state["autonomous_enabled"]:
                continue

            wait_seconds = max(
                0.001,
                float(post_state["cooldown_minutes"]) * self._cooldown_unit_seconds,
            )
            with self._lock:
                if self._stop_event.is_set():
                    break
                if self._config_generation != seen_gen:
                    continue
                self._wakeup_event.clear()
            self._wakeup_event.wait(timeout=wait_seconds)

    def start(self) -> AutonomousEngine:
        """Start the 24/7 autonomous daemon thread idempotently."""
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return self
            self._stop_event.clear()
            self._wakeup_event.clear()
            self._thread = threading.Thread(
                target=self._worker_loop,
                name="liwi-autonomous-worker",
                daemon=True,
            )
            self._thread.start()
        return self

    def stop(self, timeout: float = 5.0) -> None:
        """Signal the daemon thread to stop immediately and join cleanly."""
        with self._lock:
            self._stop_event.set()
            self._config_generation += 1
            self._wakeup_event.set()
            thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=max(0.0, float(timeout)))
        with self._lock:
            if self._thread is not None and not self._thread.is_alive():
                self._thread = None

    def _safe_repo_relative_paths(self, raw_lines: Sequence[str]) -> list[str]:
        safe: list[str] = []
        for raw in raw_lines:
            rel = str(raw or "").strip().replace("\\", "/")
            if not rel or rel.startswith("-") or ".." in rel.split("/"):
                continue
            try:
                resolved = (self.repo_root / rel).resolve()
                if not resolved.is_relative_to(self.repo_root):
                    continue
            except OSError:
                continue
            if rel not in safe:
                safe.append(rel)
        return safe

    def _default_git_revert(self, commit_hash: str) -> list[str]:
        runner = self._active_command_runner()
        clean_env = build_sanitized_cli_env()

        branch_res = _run_port_command(
            runner,
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=self.repo_root,
            timeout_seconds=DEFAULT_GIT_LOG_TIMEOUT_SECONDS,
            env=clean_env,
        )
        current_branch = branch_res.stdout.strip() if branch_res.exit_code == 0 else ""
        if current_branch != self.base_branch:
            raise RuntimeError(
                f"No se puede revertir en '{current_branch or 'desconocido'}'; "
                f"se esperaba la rama '{self.base_branch}'."
            )

        changed_res = _run_port_command(
            runner,
            ["git", "show", "--pretty=", "--name-only", commit_hash],
            cwd=self.repo_root,
            timeout_seconds=DEFAULT_GIT_LOG_TIMEOUT_SECONDS,
            env=clean_env,
        )
        changed_files = (
            self._safe_repo_relative_paths(changed_res.stdout.splitlines())
            if changed_res.exit_code == 0 and changed_res.stdout.strip()
            else []
        )

        revert_res = _run_port_command(
            runner,
            ["git", "revert", "--no-edit", commit_hash],
            cwd=self.repo_root,
            timeout_seconds=DEFAULT_GIT_LOG_TIMEOUT_SECONDS * 3,
            env=clean_env,
        )
        if revert_res.exit_code != 0 or revert_res.timed_out:
            try:
                _run_port_command(
                    runner,
                    ["git", "revert", "--abort"],
                    cwd=self.repo_root,
                    timeout_seconds=DEFAULT_GIT_LOG_TIMEOUT_SECONDS,
                    env=clean_env,
                )
            except Exception as abort_exc:  # noqa: BLE001
                logger.debug("git revert --abort failed safely: %s", redact_secrets(str(abort_exc)))
            detail = redact_secrets(
                (revert_res.stderr or revert_res.stdout or "git revert failed").strip()
            )
            raise RuntimeError(f"Error ejecutando git revert --no-edit {commit_hash}: {detail}")

        return changed_files

    def _default_verify_after_revert(self, changed_files: Sequence[str]) -> None:
        runner = self._active_command_runner()
        clean_env = build_sanitized_cli_env()

        py_targets = [p for p in changed_files if p.endswith(".py")]
        if not py_targets:
            py_targets = ["src/agent_factory/idea_engine.py"]

        test_targets = [
            p for p in changed_files if p.startswith("tests/") and Path(p).name.startswith("test_") and p.endswith(".py")
        ]
        if not test_targets:
            for p in py_targets:
                if p.startswith("src/agent_factory/"):
                    candidate = f"tests/test_{Path(p).stem}.py"
                    if (self.repo_root / candidate).is_file() and candidate not in test_targets:
                        test_targets.append(candidate)
        if not test_targets:
            test_targets = ["tests/test_autonomous_mode.py"]

        ruff_res = _run_port_command(
            runner,
            ["ruff", "check", *py_targets],
            cwd=self.repo_root,
            timeout_seconds=self._verify_timeout_seconds,
            env=clean_env,
        )
        if ruff_res.exit_code != 0 or ruff_res.timed_out:
            detail = redact_secrets((ruff_res.stderr or ruff_res.stdout or "ruff check failed").strip())
            raise RuntimeError(f"Verificación ruff falló tras revertir: {detail}")

        pytest_res = _run_port_command(
            runner,
            [sys.executable, "-m", "pytest", "-q", "--no-cov", *test_targets],
            cwd=self.repo_root,
            timeout_seconds=self._verify_timeout_seconds,
            env=clean_env,
        )
        if pytest_res.exit_code != 0 or pytest_res.timed_out:
            detail = redact_secrets((pytest_res.stderr or pytest_res.stdout or "pytest failed").strip())
            raise RuntimeError(f"Verificación pytest dirigida falló tras revertir: {detail}")

    def revert_autonomous_improvement(self, entry_id: int) -> dict[str, Any]:
        """Revert an applied improvement on main, run verification, mark 'revertida', and discard linked idea."""
        entry = get_autonomous_history_entry(entry_id, db_path=self.db_path)
        if entry is None:
            raise KeyError(f"Entrada de historial autónomo {entry_id} no encontrada.")
        if entry["status"] == "revertida":
            raise ValueError(f"La mejora autónoma {entry_id} ya fue revertida.")

        commit_hash = _validate_commit_hash(entry["commit_hash"])
        changed_files: list[str] = []

        if self._revert_runner is not None:
            rev_raw = self._revert_runner(commit_hash)
            rev_ok, rev_err = _interpret_action_result(
                rev_raw,
                f"Error ejecutando git revert --no-edit {commit_hash}.",
            )
            if not rev_ok:
                raise RuntimeError(rev_err)
        else:
            changed_files = self._default_git_revert(commit_hash)

        if self._verify_runner is not None:
            ver_raw = self._verify_runner(commit_hash)
            ver_ok, ver_err = _interpret_action_result(
                ver_raw,
                f"Falló la verificación tras revertir {commit_hash}.",
            )
            if not ver_ok:
                raise RuntimeError(ver_err)
        else:
            self._default_verify_after_revert(changed_files)

        updated = mark_autonomous_reverted(entry["id"], db_path=self.db_path)
        return {
            "ok": True,
            "entry": updated,
            "commit_hash": commit_hash,
            "discarded_idea_id": updated["idea_id"] if updated is not None else entry["idea_id"],
        }


def start_autonomous_engine(
    *,
    db_path: Path | str | None = None,
    repo_root: Path | str | None = None,
    staging_manager: Any | None = None,
    base_branch: str = "main",
    activity_checker: Callable[[], str | None] | None = None,
    has_active_builds: Callable[[], bool] | None = None,
    has_running_local_apps: Callable[[], bool] | None = None,
    triage_provider: Callable[[], dict[str, Any] | None] | None = None,
    idea_selector: Callable[[], dict[str, Any] | None] | None = None,
    staging_executor: Callable[[dict[str, Any]], Any] | None = None,
    main_promoter: Callable[[dict[str, Any]], Any] | None = None,
    revert_runner: Callable[[str], Any] | None = None,
    verify_runner: Callable[[str], Any] | None = None,
    command_runner: Runner | None = None,
    max_consecutive_failures: int = AUTONOMOUS_MAX_CONSECUTIVE_FAILURES,
    cooldown_unit_seconds: float = 60.0,
    idle_poll_seconds: float = 1.0,
) -> AutonomousEngine:
    """Create, start, and return a daemon AutonomousEngine."""
    engine = AutonomousEngine(
        db_path=db_path,
        repo_root=repo_root,
        staging_manager=staging_manager,
        base_branch=base_branch,
        activity_checker=activity_checker,
        has_active_builds=has_active_builds,
        has_running_local_apps=has_running_local_apps,
        triage_provider=triage_provider,
        idea_selector=idea_selector,
        staging_executor=staging_executor,
        main_promoter=main_promoter,
        revert_runner=revert_runner,
        verify_runner=verify_runner,
        command_runner=command_runner,
        max_consecutive_failures=max_consecutive_failures,
        cooldown_unit_seconds=cooldown_unit_seconds,
        idle_poll_seconds=idle_poll_seconds,
    )
    return engine.start()
