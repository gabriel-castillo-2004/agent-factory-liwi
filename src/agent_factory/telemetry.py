"""Local SQLite WAL telemetry module for execution events, incidents, and summaries."""

from __future__ import annotations

import logging
import os
import re
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_TELEMETRY_DB_PATH = Path("data/telemetry.db")
TELEMETRY_DB_ENV_VAR = "AGENT_FACTORY_TELEMETRY_DB"
CANONICAL_COMPONENTS: tuple[str, ...] = (
    "orchestrator",
    "doctor",
    "self_improver",
    "git",
    "tests",
)
MIN_SUMMARY_LIMIT = 1
MAX_SUMMARY_LIMIT = 100
DEFAULT_SUMMARY_LIMIT = 10
DEFAULT_TRIAGE_LIMIT = 50
MAX_TRIAGE_LIMIT = 200
TRIAGE_VALID_STATUSES: frozenset[str] = frozenset(
    {"analizando", "lista", "encolada", "descartada"}
)
_TELEMETRY_ERRORS = (sqlite3.Error, OSError, ValueError, TypeError, OverflowError, RuntimeError)

_KNOWN_SIGNATURES: frozenset[str] = frozenset(
    {
        "git_timeout",
        "test_timeout",
        "ag_cli_timeout",
        "missing_module",
        "syntax_or_lint",
        "quota_exhausted",
        "test_failure",
        "unknown",
    }
)

_TIMEOUT_RE = re.compile(
    r"\b(timeout|timed[\s_-]?out|time[\s_-]?out|timeoutexpired|commandtimeouterror)\b"
    r"|tiempo\s+(?:de\s+espera\s+)?agotado",
    re.IGNORECASE,
)
_AG_CLI_RE = re.compile(
    r"\b(ag[\s_-]?cli|agy[\s_-]?cli|agy|antigravity|code[\s_-]?agent)\b",
    re.IGNORECASE,
)
_GIT_RE = re.compile(r"\b(git|git_timeout)\b", re.IGNORECASE)
_TEST_RE = re.compile(
    r"\b(pytest|unittest|vitest|jest|mocha|playwright|tests?|test_timeout)\b",
    re.IGNORECASE,
)
_MISSING_MODULE_RE = re.compile(
    r"(modulenotfounderror|no module named|cannot find module|err_module_not_found"
    r"|importerror|cannot import name|module not found|missing_module)",
    re.IGNORECASE,
)
_SYNTAX_OR_LINT_RE = re.compile(
    r"(syntaxerror|indentationerror|taberror|invalid syntax|unexpected indent"
    r"|expected an indented block|unterminated string"
    r"|\bruff\b|\bflake8\b|\beslint\b|\bpylint\b|\blint(?:ing)?\s+errors?\b|\bsyntax_or_lint\b)",
    re.IGNORECASE,
)
_QUOTA_EXHAUSTED_RE = re.compile(
    r"(individual\s+quota\s+reached|resource_exhausted|(?<!\d)429(?!\d))",
    re.IGNORECASE,
)
_TEST_FAILURE_RE = re.compile(
    r"(fall[oó]\s+pytest|failed\s+tests[/\\]|assertionerror)",
    re.IGNORECASE,
)
_AG_CLI_TIMEOUT_CODE_RE = re.compile(
    r"(timeoutexpired|timed[\s_-]?out|\bag_cli_timeout\b)",
    re.IGNORECASE,
)


def _resolve_db_path(db_path: Path | str | None = None) -> Path:
    if db_path is not None:
        return Path(db_path)
    env_val = os.environ.get(TELEMETRY_DB_ENV_VAR, "").strip()
    if env_val:
        return Path(env_val)
    return DEFAULT_TELEMETRY_DB_PATH


def _clamp_limit(limit: int, *, default: int = DEFAULT_SUMMARY_LIMIT) -> int:
    if isinstance(limit, bool) or not isinstance(limit, int):
        return default
    return max(MIN_SUMMARY_LIMIT, min(limit, MAX_SUMMARY_LIMIT))


def _ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS execution_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            project_id TEXT,
            component TEXT NOT NULL,
            stage TEXT NOT NULL,
            command TEXT,
            duration_ms INTEGER NOT NULL DEFAULT 0,
            exit_code INTEGER,
            timed_out INTEGER NOT NULL DEFAULT 0,
            error_signature TEXT
        );
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS incidents (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            project_id TEXT,
            stage TEXT NOT NULL,
            error_signature TEXT NOT NULL,
            raw_error TEXT NOT NULL,
            root_cause TEXT,
            fix_summary TEXT,
            resolved INTEGER NOT NULL DEFAULT 0
        );
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS triage_solutions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL,
            error_signature TEXT NOT NULL UNIQUE,
            scope TEXT NOT NULL,
            title TEXT NOT NULL DEFAULT '',
            raw_error TEXT NOT NULL,
            root_cause TEXT NOT NULL DEFAULT '',
            proposed_fix TEXT NOT NULL DEFAULT '',
            technical_prompt TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'analizando'
                CHECK(status IN ('analizando', 'lista', 'encolada', 'descartada')),
            occurrences INTEGER NOT NULL DEFAULT 1
        );
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_execution_events_duration ON execution_events(duration_ms DESC);"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_execution_events_component ON execution_events(component);"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_execution_events_error_sig ON execution_events(error_signature);"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_incidents_timestamp ON incidents(timestamp DESC);"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_incidents_error_sig ON incidents(error_signature);"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_triage_solutions_status ON triage_solutions(status);"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_triage_solutions_created_at ON triage_solutions(created_at DESC);"
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
        _ensure_schema(conn)
        conn.commit()
    except Exception:
        conn.close()
        raise
    return conn


def init_telemetry_db(db_path: Path | str | None = None) -> bool:
    """Initialize telemetry SQLite database in WAL mode. Returns False on failure without raising."""
    conn: sqlite3.Connection | None = None
    try:
        conn = _open_connection(db_path)
        return True
    except _TELEMETRY_ERRORS as exc:
        logger.debug("Telemetry DB init failed: %s", exc)
        return False
    finally:
        if conn is not None:
            conn.close()


def classify_error(text: str) -> str:
    """Normalize raw error text into one of 6 canonical telemetry error signatures."""
    if not isinstance(text, str):
        return "unknown"
    stripped = text.strip()
    if not stripped:
        return "unknown"

    lowered = stripped.lower()
    if lowered in _KNOWN_SIGNATURES:
        return lowered

    if _TIMEOUT_RE.search(stripped):
        if _AG_CLI_RE.search(stripped):
            return "ag_cli_timeout"
        if _GIT_RE.search(stripped):
            return "git_timeout"
        if _TEST_RE.search(stripped):
            return "test_timeout"

    if _MISSING_MODULE_RE.search(stripped):
        return "missing_module"

    if _SYNTAX_OR_LINT_RE.search(stripped):
        return "syntax_or_lint"

    return "unknown"


def classify_error_code(message: str, current_code: str | None = None) -> str:
    """Classify an error message when no specific code is set, preserving explicit non-unknown codes."""
    if current_code is not None:
        cleaned_code = str(current_code).strip()
        if cleaned_code and cleaned_code.lower() != "unknown":
            return cleaned_code

    if not isinstance(message, str):
        return "unknown"
    stripped = message.strip()
    if not stripped:
        return "unknown"

    if _QUOTA_EXHAUSTED_RE.search(stripped):
        return "quota_exhausted"
    if _TEST_FAILURE_RE.search(stripped):
        return "test_failure"
    if _AG_CLI_TIMEOUT_CODE_RE.search(stripped):
        return "ag_cli_timeout"
    return "unknown"


def _resolve_error_signature(message: str, current_code: str | None = None) -> str:
    """Resolve an error signature by preserving explicit codes and classifying unknown/empty ones."""
    if current_code is not None:
        cleaned_code = str(current_code).strip()
        if cleaned_code and cleaned_code.lower() != "unknown":
            return classify_error_code(message, current_code=cleaned_code)
    return classify_error_code(message, current_code=classify_error(message))


def record_execution_event(
    component: str,
    stage: str,
    command: str | None = None,
    duration_ms: float = 0,
    exit_code: int | None = None,
    timed_out: bool = False,
    error_signature: str | None = None,
    project_id: str | None = None,
    error_text: str | None = None,
    timestamp: str | None = None,
    db_path: Path | str | None = None,
) -> int | None:
    """Record a single execution event into telemetry SQLite DB. Never raises."""
    conn: sqlite3.Connection | None = None
    try:
        ts = (
            timestamp.strip()
            if isinstance(timestamp, str) and timestamp.strip()
            else datetime.now(UTC).isoformat()
        )
        comp = str(component).strip().lower() or "orchestrator"
        stg = str(stage).strip() or "unknown"
        cmd = str(command) if command is not None else None
        dur = max(0, round(float(duration_ms)))
        code = int(exit_code) if exit_code is not None else None
        is_timeout = 1 if bool(timed_out) else 0

        eff_sig: str | None
        raw_sig = str(error_signature).strip() if error_signature is not None else ""
        raw_err = str(error_text).strip() if error_text is not None else ""
        if raw_sig and raw_sig.lower() != "unknown":
            eff_sig = classify_error_code(raw_err, current_code=raw_sig)
        elif raw_err:
            eff_sig = _resolve_error_signature(raw_err, current_code=raw_sig or None)
        elif is_timeout:
            eff_sig = _resolve_error_signature(
                f"{comp} {stg} {cmd or ''} timed out",
                current_code=raw_sig or None,
            )
        elif raw_sig:
            eff_sig = classify_error_code(cmd or "", current_code=raw_sig)
        elif code is not None and code != 0:
            eff_sig = classify_error_code(cmd or "", current_code="unknown")
        else:
            eff_sig = None

        conn = _open_connection(db_path)
        cursor = conn.execute(
            """
            INSERT INTO execution_events (
                timestamp,
                project_id,
                component,
                stage,
                command,
                duration_ms,
                exit_code,
                timed_out,
                error_signature
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?);
            """,
            (ts, project_id, comp, stg, cmd, dur, code, is_timeout, eff_sig),
        )
        conn.commit()
        return int(cursor.lastrowid) if cursor.lastrowid is not None else None
    except _TELEMETRY_ERRORS as exc:
        logger.debug("Telemetry record_execution_event skipped due to error: %s", exc)
        return None
    finally:
        if conn is not None:
            conn.close()


def record_incident(
    stage: str,
    raw_error: str,
    project_id: str | None = None,
    error_signature: str | None = None,
    root_cause: str | None = None,
    fix_summary: str | None = None,
    resolved: bool = False,
    timestamp: str | None = None,
    db_path: Path | str | None = None,
) -> int | None:
    """Record a diagnostic incident into telemetry SQLite DB. Never raises."""
    conn: sqlite3.Connection | None = None
    try:
        ts = (
            timestamp.strip()
            if isinstance(timestamp, str) and timestamp.strip()
            else datetime.now(UTC).isoformat()
        )
        stg = str(stage).strip() or "unknown"
        raw = str(raw_error or "")
        eff_sig = _resolve_error_signature(raw, current_code=error_signature)
        is_resolved = 1 if bool(resolved) else 0

        conn = _open_connection(db_path)
        cursor = conn.execute(
            """
            INSERT INTO incidents (
                timestamp,
                project_id,
                stage,
                error_signature,
                raw_error,
                root_cause,
                fix_summary,
                resolved
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?);
            """,
            (ts, project_id, stg, eff_sig, raw, root_cause, fix_summary, is_resolved),
        )
        conn.commit()
        return int(cursor.lastrowid) if cursor.lastrowid is not None else None
    except _TELEMETRY_ERRORS as exc:
        logger.debug("Telemetry record_incident skipped due to error: %s", exc)
        return None
    finally:
        if conn is not None:
            conn.close()


def record_safe_event(
    component: str,
    stage: str,
    command: str | None = None,
    duration_ms: float = 0,
    exit_code: int | None = None,
    timed_out: bool = False,
    error_signature: str | None = None,
    project_id: str | None = None,
    error_text: str | None = None,
    timestamp: str | None = None,
    db_path: Path | str | None = None,
) -> int | None:
    """Fail-safe wrapper around record_execution_event that silences SQLite/IO/runtime errors."""
    try:
        return record_execution_event(
            component=component,
            stage=stage,
            command=command,
            duration_ms=duration_ms,
            exit_code=exit_code,
            timed_out=timed_out,
            error_signature=error_signature,
            project_id=project_id,
            error_text=error_text,
            timestamp=timestamp,
            db_path=db_path,
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug("Telemetry record_safe_event silenced error: %s", exc)
        return None


def record_safe_incident(
    stage: str,
    raw_error: str,
    project_id: str | None = None,
    error_signature: str | None = None,
    root_cause: str | None = None,
    fix_summary: str | None = None,
    resolved: bool = False,
    timestamp: str | None = None,
    db_path: Path | str | None = None,
) -> int | None:
    """Fail-safe wrapper around record_incident that silences SQLite/IO/runtime errors."""
    try:
        return record_incident(
            stage=stage,
            raw_error=raw_error,
            project_id=project_id,
            error_signature=error_signature,
            root_cause=root_cause,
            fix_summary=fix_summary,
            resolved=resolved,
            timestamp=timestamp,
            db_path=db_path,
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug("Telemetry record_safe_incident silenced error: %s", exc)
        return None


def _empty_summary(limit: int) -> dict[str, Any]:
    return {
        "limit": limit,
        "totals": {
            "execution_events": 0,
            "incidents": 0,
            "unresolved_incidents": 0,
        },
        "slowest_executions": [],
        "recurring_errors": [],
        "component_success_rates": {
            comp: {
                "total": 0,
                "succeeded": 0,
                "failed": 0,
                "timed_out": 0,
                "success_rate": None,
            }
            for comp in CANONICAL_COMPONENTS
        },
        "recent_incidents": [],
    }


def get_telemetry_summary(
    limit: int = DEFAULT_SUMMARY_LIMIT,
    db_path: Path | str | None = None,
) -> dict[str, Any]:
    """Return a JSON-serializable telemetry summary with bottlenecks, errors, rates, and incidents."""
    bounded_limit = _clamp_limit(limit)
    summary = _empty_summary(bounded_limit)
    conn: sqlite3.Connection | None = None
    try:
        conn = _open_connection(db_path)

        total_events = int(conn.execute("SELECT COUNT(*) FROM execution_events;").fetchone()[0])
        inc_counts = conn.execute(
            """
            SELECT
                COUNT(*) AS total_incidents,
                COALESCE(SUM(CASE WHEN resolved = 0 THEN 1 ELSE 0 END), 0) AS unresolved_incidents
            FROM incidents;
            """
        ).fetchone()
        summary["totals"] = {
            "execution_events": total_events,
            "incidents": int(inc_counts["total_incidents"]),
            "unresolved_incidents": int(inc_counts["unresolved_incidents"]),
        }

        slow_rows = conn.execute(
            """
            SELECT
                id,
                timestamp,
                project_id,
                component,
                stage,
                command,
                duration_ms,
                exit_code,
                timed_out,
                error_signature
            FROM execution_events
            ORDER BY duration_ms DESC, id DESC
            LIMIT ?;
            """,
            (bounded_limit,),
        ).fetchall()
        summary["slowest_executions"] = [
            {
                "id": int(row["id"]),
                "timestamp": str(row["timestamp"]),
                "project_id": row["project_id"],
                "component": str(row["component"]),
                "stage": str(row["stage"]),
                "command": row["command"],
                "duration_ms": int(row["duration_ms"]),
                "exit_code": int(row["exit_code"]) if row["exit_code"] is not None else None,
                "timed_out": bool(row["timed_out"]),
                "error_signature": row["error_signature"],
            }
            for row in slow_rows
        ]

        sig_stats: dict[str, dict[str, Any]] = {}
        for row in conn.execute(
            """
            SELECT
                error_signature,
                COUNT(*) AS cnt,
                MAX(timestamp) AS last_seen
            FROM execution_events
            WHERE error_signature IS NOT NULL AND TRIM(error_signature) != ''
            GROUP BY error_signature;
            """
        ).fetchall():
            sig = str(row["error_signature"]).strip()
            entry = sig_stats.setdefault(
                sig,
                {
                    "error_signature": sig,
                    "count": 0,
                    "execution_events": 0,
                    "incidents": 0,
                    "last_seen": None,
                },
            )
            cnt = int(row["cnt"])
            entry["execution_events"] += cnt
            entry["count"] += cnt
            last_seen = str(row["last_seen"]) if row["last_seen"] is not None else None
            if last_seen and (entry["last_seen"] is None or last_seen > entry["last_seen"]):
                entry["last_seen"] = last_seen

        for row in conn.execute(
            """
            SELECT
                error_signature,
                COUNT(*) AS cnt,
                MAX(timestamp) AS last_seen
            FROM incidents
            WHERE error_signature IS NOT NULL AND TRIM(error_signature) != ''
            GROUP BY error_signature;
            """
        ).fetchall():
            sig = str(row["error_signature"]).strip()
            entry = sig_stats.setdefault(
                sig,
                {
                    "error_signature": sig,
                    "count": 0,
                    "execution_events": 0,
                    "incidents": 0,
                    "last_seen": None,
                },
            )
            cnt = int(row["cnt"])
            entry["incidents"] += cnt
            entry["count"] += cnt
            last_seen = str(row["last_seen"]) if row["last_seen"] is not None else None
            if last_seen and (entry["last_seen"] is None or last_seen > entry["last_seen"]):
                entry["last_seen"] = last_seen

        summary["recurring_errors"] = sorted(
            sig_stats.values(),
            key=lambda item: (-int(item["count"]), str(item["error_signature"])),
        )[:bounded_limit]

        rates: dict[str, dict[str, Any]] = summary["component_success_rates"]
        for row in conn.execute(
            "SELECT component, exit_code, timed_out, error_signature FROM execution_events;"
        ).fetchall():
            comp = str(row["component"]).strip().lower() or "orchestrator"
            bucket = rates.setdefault(
                comp,
                {
                    "total": 0,
                    "succeeded": 0,
                    "failed": 0,
                    "timed_out": 0,
                    "success_rate": None,
                },
            )
            bucket["total"] += 1
            timed_out_flag = bool(row["timed_out"])
            if timed_out_flag:
                bucket["timed_out"] += 1
            exit_code_val = row["exit_code"]
            has_err_sig = bool(row["error_signature"] and str(row["error_signature"]).strip())
            succeeded = (
                (not timed_out_flag)
                and (exit_code_val is None or int(exit_code_val) == 0)
                and (not has_err_sig)
            )
            if succeeded:
                bucket["succeeded"] += 1
            else:
                bucket["failed"] += 1

        for bucket in rates.values():
            total = int(bucket["total"])
            bucket["success_rate"] = round(int(bucket["succeeded"]) / total, 4) if total > 0 else None

        inc_rows = conn.execute(
            """
            SELECT
                id,
                timestamp,
                project_id,
                stage,
                error_signature,
                raw_error,
                root_cause,
                fix_summary,
                resolved
            FROM incidents
            ORDER BY timestamp DESC, id DESC
            LIMIT ?;
            """,
            (bounded_limit,),
        ).fetchall()
        summary["recent_incidents"] = [
            {
                "id": int(row["id"]),
                "timestamp": str(row["timestamp"]),
                "project_id": row["project_id"],
                "stage": str(row["stage"]),
                "error_signature": str(row["error_signature"]),
                "raw_error": str(row["raw_error"]),
                "root_cause": row["root_cause"],
                "fix_summary": row["fix_summary"],
                "resolved": bool(row["resolved"]),
            }
            for row in inc_rows
        ]
        return summary
    except _TELEMETRY_ERRORS as exc:
        logger.debug("Telemetry get_telemetry_summary fallback due to error: %s", exc)
        return _empty_summary(bounded_limit)
    finally:
        if conn is not None:
            conn.close()


def format_telemetry_context(
    limit: int = 5,
    db_path: Path | str | None = None,
) -> str:
    """Format a concise plain-text telemetry digest for prompt injection, or '' when empty."""
    summary = get_telemetry_summary(limit=limit, db_path=db_path)
    recurring = summary["recurring_errors"]
    slowest = summary["slowest_executions"]
    incidents = summary["recent_incidents"]
    if not recurring and not slowest and not incidents:
        return ""

    lines: list[str] = ["Local Telemetry Context:"]
    if recurring:
        err_parts = [f"{item['error_signature']} ({item['count']}x)" for item in recurring]
        lines.append("- Recurring errors: " + ", ".join(err_parts))
    if slowest:
        slow_parts = [
            f"{item['component']}/{item['stage']} ({item['duration_ms']}ms)"
            for item in slowest
        ]
        lines.append("- Slowest stages: " + ", ".join(slow_parts))
    if incidents:
        lines.append("- Recent incidents:")
        for inc in incidents:
            status_lbl = "resolved" if inc["resolved"] else "open"
            lines.append(
                f"  * [{status_lbl}] {inc['stage']} ({inc['error_signature']}): "
                f"{inc['raw_error'][:160]}"
            )
    return "\n".join(lines)


_TRIAGE_SELECT_COLS = (
    "id, created_at, error_signature, scope, title, raw_error, "
    "root_cause, proposed_fix, technical_prompt, status, occurrences"
)


def _validate_triage_status(status: str) -> str:
    clean = str(status or "").strip().lower()
    if clean not in TRIAGE_VALID_STATUSES:
        raise ValueError(
            f"Invalid triage status {status!r}; expected one of {sorted(TRIAGE_VALID_STATUSES)}"
        )
    return clean


def _row_to_triage_solution(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": int(row["id"]),
        "created_at": str(row["created_at"]),
        "error_signature": str(row["error_signature"]),
        "scope": str(row["scope"]),
        "title": str(row["title"]),
        "raw_error": str(row["raw_error"]),
        "root_cause": str(row["root_cause"]),
        "proposed_fix": str(row["proposed_fix"]),
        "technical_prompt": str(row["technical_prompt"]),
        "status": str(row["status"]),
        "occurrences": int(row["occurrences"]),
    }


def upsert_triage_error(
    error_signature: str,
    raw_error: str,
    *,
    scope: str = "factory",
    title: str = "",
    status: str = "analizando",
    created_at: str | None = None,
    db_path: Path | str | None = None,
) -> tuple[dict[str, Any] | None, bool]:
    """Insert a new triage error or atomically increment occurrences for an existing signature."""
    sig = str(error_signature or "").strip()
    if not sig:
        raise ValueError("error_signature must be a non-empty string")
    eff_status = _validate_triage_status(status)
    eff_scope = str(scope or "factory").strip().lower() or "factory"
    eff_title = str(title or "").strip()
    raw = str(raw_error or "")
    ts = (
        created_at.strip()
        if isinstance(created_at, str) and created_at.strip()
        else datetime.now(UTC).isoformat()
    )

    conn: sqlite3.Connection | None = None
    try:
        conn = _open_connection(db_path)
        cursor = conn.execute(
            """
            INSERT OR IGNORE INTO triage_solutions (
                created_at,
                error_signature,
                scope,
                title,
                raw_error,
                root_cause,
                proposed_fix,
                technical_prompt,
                status,
                occurrences
            ) VALUES (?, ?, ?, ?, ?, '', '', '', ?, 1);
            """,
            (ts, sig, eff_scope, eff_title, raw, eff_status),
        )
        is_new = cursor.rowcount == 1
        if not is_new:
            conn.execute(
                "UPDATE triage_solutions SET occurrences = occurrences + 1 WHERE error_signature = ?;",
                (sig,),
            )
        row = conn.execute(
            f"SELECT {_TRIAGE_SELECT_COLS} FROM triage_solutions WHERE error_signature = ?;",
            (sig,),
        ).fetchone()
        conn.commit()
        return (_row_to_triage_solution(row) if row is not None else None), is_new
    except (sqlite3.Error, OSError) as exc:
        logger.debug("Telemetry upsert_triage_error skipped due to error: %s", exc)
        return None, False
    finally:
        if conn is not None:
            conn.close()


def update_triage_solution(
    solution_id: int,
    *,
    title: str | None = None,
    root_cause: str | None = None,
    proposed_fix: str | None = None,
    technical_prompt: str | None = None,
    status: str | None = None,
    db_path: Path | str | None = None,
) -> dict[str, Any] | None:
    """Update analysis fields and/or status of a triage_solutions record by ID."""
    clean_status = _validate_triage_status(status) if status is not None else None
    if isinstance(solution_id, bool) or not isinstance(solution_id, int) or solution_id <= 0:
        return None

    updates: list[str] = []
    params: list[Any] = []
    if title is not None:
        updates.append("title = ?")
        params.append(str(title))
    if root_cause is not None:
        updates.append("root_cause = ?")
        params.append(str(root_cause))
    if proposed_fix is not None:
        updates.append("proposed_fix = ?")
        params.append(str(proposed_fix))
    if technical_prompt is not None:
        updates.append("technical_prompt = ?")
        params.append(str(technical_prompt))
    if clean_status is not None:
        updates.append("status = ?")
        params.append(clean_status)

    conn: sqlite3.Connection | None = None
    try:
        conn = _open_connection(db_path)
        if updates:
            params.append(solution_id)
            conn.execute(
                f"UPDATE triage_solutions SET {', '.join(updates)} WHERE id = ?;",
                tuple(params),
            )
            conn.commit()
        row = conn.execute(
            f"SELECT {_TRIAGE_SELECT_COLS} FROM triage_solutions WHERE id = ?;",
            (solution_id,),
        ).fetchone()
        return _row_to_triage_solution(row) if row is not None else None
    except (sqlite3.Error, OSError) as exc:
        logger.debug("Telemetry update_triage_solution skipped due to error: %s", exc)
        return None
    finally:
        if conn is not None:
            conn.close()


def list_triage_solutions(
    *,
    status: str | None = None,
    scope: str | None = None,
    limit: int = DEFAULT_TRIAGE_LIMIT,
    db_path: Path | str | None = None,
) -> list[dict[str, Any]]:
    """List triage_solutions ordered by most recent first, with optional status/scope filters."""
    clean_status = _validate_triage_status(status) if status is not None else None
    clean_scope = str(scope).strip().lower() if scope is not None and str(scope).strip() else None
    if isinstance(limit, bool) or not isinstance(limit, int):
        bounded_limit = DEFAULT_TRIAGE_LIMIT
    else:
        bounded_limit = max(1, min(limit, MAX_TRIAGE_LIMIT))

    clauses: list[str] = []
    params: list[Any] = []
    if clean_status is not None:
        clauses.append("status = ?")
        params.append(clean_status)
    if clean_scope is not None:
        clauses.append("scope = ?")
        params.append(clean_scope)

    where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    params.append(bounded_limit)

    conn: sqlite3.Connection | None = None
    try:
        conn = _open_connection(db_path)
        rows = conn.execute(
            f"SELECT {_TRIAGE_SELECT_COLS} FROM triage_solutions "
            f"{where_sql} ORDER BY created_at DESC, id DESC LIMIT ?;",
            tuple(params),
        ).fetchall()
        return [_row_to_triage_solution(row) for row in rows]
    except (sqlite3.Error, OSError) as exc:
        logger.debug("Telemetry list_triage_solutions fallback due to error: %s", exc)
        return []
    finally:
        if conn is not None:
            conn.close()


def get_triage_solution(
    solution_id: int,
    db_path: Path | str | None = None,
) -> dict[str, Any] | None:
    """Fetch a single triage_solutions record by ID, or None if not found."""
    if isinstance(solution_id, bool) or not isinstance(solution_id, int) or solution_id <= 0:
        return None
    conn: sqlite3.Connection | None = None
    try:
        conn = _open_connection(db_path)
        row = conn.execute(
            f"SELECT {_TRIAGE_SELECT_COLS} FROM triage_solutions WHERE id = ?;",
            (solution_id,),
        ).fetchone()
        return _row_to_triage_solution(row) if row is not None else None
    except (sqlite3.Error, OSError) as exc:
        logger.debug("Telemetry get_triage_solution fallback due to error: %s", exc)
        return None
    finally:
        if conn is not None:
            conn.close()
