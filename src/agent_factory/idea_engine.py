"""Self-Improvement Idea Radar engine: SQLite persistence, context builder, read-only agy ideation, and optional daemon worker."""

from __future__ import annotations

import json
import logging
import math
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unicodedata
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

from agent_factory.adapters.agy_cli_adapter import build_sanitized_cli_env
from agent_factory.adapters.subprocess_runner import SubprocessRunner, resolve_executable
from agent_factory.error_triage import (
    format_agent_error,
    is_quota_429_error,
    parse_quota_reset_delay_seconds,
)
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
IDEA_VALID_STATUSES: frozenset[str] = frozenset(
    {"nueva", "encolada", "implementando", "aplicada", "descartada", "failed"}
)
IDEA_FILTER_STATUSES: tuple[str, ...] = (
    "pending",
    "queued",
    "implementing",
    "implemented",
    "dismissed",
    "failed",
)
IDEA_VALID_FILTERS: frozenset[str] = frozenset((*IDEA_FILTER_STATUSES, "all"))
_DEFAULT_ACTIVE_IDEA_STATUSES: tuple[str, ...] = ("nueva", "encolada", "implementando")
_CANONICAL_TO_DB_STATUS: dict[str, str] = {
    "pending": "nueva",
    "queued": "encolada",
    "implementing": "implementando",
    "implemented": "aplicada",
    "dismissed": "descartada",
    "failed": "failed",
}
_DB_TO_CANONICAL_STATUS: dict[str, str] = {
    db_val: canonical for canonical, db_val in _CANONICAL_TO_DB_STATUS.items()
}
_STATUS_ALIAS_TO_DB: dict[str, str] = {
    **_CANONICAL_TO_DB_STATUS,
    **{db_val: db_val for db_val in _DB_TO_CANONICAL_STATUS},
    "applied": "aplicada",
    "implementada": "aplicada",
    "fallida": "failed",
}
DEFAULT_IDEA_STALE_SECONDS: int = 360
MAX_IDEA_RETRIES: int = 2
MAX_AUTO_QUEUED_IDEAS: int = 3
_RUNTIME_STATUS_LOCK = threading.Lock()
_RUNTIME_STATUS_PROVIDER: Callable[[], Mapping[str, Any]] | None = None
_UNSET: Any = object()
IDEA_VALID_SOURCES: frozenset[str] = frozenset({"manual", "auto", "visual_qa"})
IDEA_VALID_IMPACTS: frozenset[str] = frozenset({"Alto", "Medio"})
DEFAULT_AUTO_INTERVAL_HOURS: float = 0.0
DEFAULT_IDEATION_COUNT: int = 3
MAX_IDEATION_COUNT: int = 10
DEFAULT_IDEATION_TIMEOUT_SECONDS: float = 180.0
DEFAULT_GIT_LOG_TIMEOUT_SECONDS: float = 10.0
DEFAULT_RECENT_COMMITS_LIMIT: int = 60
IDEA_DUPLICATE_SIMILARITY_THRESHOLD: float = 0.52
_MAX_PROMPT_HISTORY_IDEAS: int = 80
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
DEFAULT_ESTIMATED_WINDOW_CAPACITY: int = 180000
DEFAULT_QUOTA_WINDOW_SECONDS: int = 5 * 3600
DEFAULT_RESERVE_THRESHOLD_PCT: int = 30
DEFAULT_AVG_TURN_TOKENS: int = 12000
PROJECT_IDEA_VALID_SOURCES: frozenset[str] = frozenset({"manual", "runtime_error", "audit"})
PROJECT_IDEA_VALID_STATUSES: frozenset[str] = frozenset(
    {"nueva", "encolada", "implementando", "aplicada", "descartada"}
)
_PROJECT_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")

_AUTO_INTERVAL_SETTING_KEY = "auto_interval_hours"
_MAX_INLINE_PROMPT_CHARS = 6000
_FENCED_BLOCK_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.IGNORECASE | re.DOTALL)
_NON_ALNUM_RE = re.compile(r"[^a-z0-9\s]+")
_SAFE_COMMIT_HASH_RE = re.compile(r"^[0-9a-fA-F]{4,64}$")
_COMMIT_HASH_PREFIX_RE = re.compile(r"^\s*[0-9a-fA-F]{4,40}\s+")
_CONVENTIONAL_COMMIT_PREFIX_RE = re.compile(
    r"^\s*(?:feat|fix|chore|refactor|perf|docs|test|tests|style|ci|build|revert)"
    r"(?:\(([^)]+)\))?!?:\s*",
    re.IGNORECASE,
)
_SEMANTIC_STOPWORDS: frozenset[str] = frozenset(
    {
        # Spanish functional stopwords
        "a", "al", "algo", "algunas", "algunos", "ante", "antes", "asi", "aun", "aunque",
        "bajo", "bien", "cada", "casi", "como", "con", "contra", "cual", "cuales", "cuando",
        "de", "del", "desde", "donde", "durante", "e", "el", "ella", "ellas", "ello", "ellos",
        "en", "entre", "era", "es", "esa", "esas", "ese", "eso", "esos", "esta", "estaba",
        "estan", "estas", "este", "esto", "estos", "fue", "fueron", "ha", "hace", "hacer",
        "hacia", "han", "hasta", "hay", "la", "las", "le", "les", "lo", "los", "mas", "me",
        "mi", "mis", "mismo", "mucho", "muy", "ni", "no", "nos", "nuestra", "nuestro", "o",
        "otra", "otras", "otro", "otros", "para", "pero", "poco", "por", "porque", "puede",
        "pueden", "que", "quien", "se", "sea", "segun", "ser", "si", "sido", "siempre", "sin",
        "sino", "sobre", "solo", "son", "su", "sus", "tambien", "tan", "te", "tener", "tiene",
        "tienen", "toda", "todas", "todo", "todos", "tu", "tus", "un", "una", "unas", "uno",
        "unos", "ya", "y",
        # English functional stopwords
        "about", "above", "after", "all", "also", "am", "an", "and", "any", "are", "as", "at",
        "be", "because", "been", "before", "being", "between", "both", "but", "by", "can",
        "could", "did", "do", "does", "doing", "done", "down", "each", "few", "for", "from",
        "further", "get", "got", "had", "has", "have", "having", "he", "her", "here", "hers",
        "him", "his", "how", "i", "if", "in", "into", "is", "it", "its", "just", "may",
        "might", "more", "most", "must", "my", "nor", "not", "now", "of", "off", "on",
        "once", "only", "or", "other", "our", "out", "over", "own", "same", "should", "so",
        "some", "such", "than", "that", "the", "their", "them", "then", "there", "these",
        "they", "this", "those", "through", "to", "too", "under", "until", "up", "use",
        "using", "very", "was", "we", "were", "what", "when", "where", "which", "while",
        "who", "why", "will", "with", "would", "you", "your",
        # Generic ideation action boilerplate (prevents false positives on "Implementar soporte de X")
        "agregar", "anadir", "implementar", "crear", "mejorar", "soporte", "sistema",
        "nuevo", "nueva", "nuevos", "nuevas", "permitir", "habilitar", "incorporar", "incluir",
        "add", "implement", "create", "improve", "support", "new", "allow", "enable",
    }
)
_IDEA_SELECT_COLS = (
    "id, created_at, title, category, impact, simple_description, "
    "technical_prompt, status, source"
)
_IDEA_JOIN_SELECT_SQL = (
    "SELECT i.id, i.created_at, i.title, i.category, i.impact, "
    "i.simple_description, i.technical_prompt, i.status, i.source, "
    "i.queued_at, "
    "COALESCE(NULLIF(i.started_at, ''), e.started_at) AS started_at, "
    "i.completed_at, "
    "i.duration_seconds, "
    "COALESCE(NULLIF(i.last_heartbeat_at, ''), e.last_heartbeat_at) AS last_heartbeat_at, "
    "COALESCE(NULLIF(i.current_step, ''), e.current_step, '') AS current_step, "
    "MAX(COALESCE(i.retry_count, 0), COALESCE(e.retry_count, 0)) AS retry_count, "
    "i.diagnostic_snapshot, "
    "e.failure_reason "
    "FROM improvement_ideas AS i "
    "LEFT JOIN idea_execution_state AS e ON e.idea_id = i.id"
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


_CREATE_IMPROVEMENT_IDEAS_SQL = """
CREATE TABLE IF NOT EXISTS improvement_ideas (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    title TEXT NOT NULL,
    category TEXT NOT NULL,
    impact TEXT NOT NULL,
    simple_description TEXT NOT NULL,
    technical_prompt TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'nueva'
        CHECK(status IN ('nueva', 'encolada', 'implementando', 'aplicada', 'descartada', 'failed')),
    source TEXT NOT NULL DEFAULT 'manual'
        CHECK(source IN ('manual', 'auto', 'visual_qa'))
);
"""

_CREATE_IDEA_EXECUTION_STATE_SQL = """
CREATE TABLE IF NOT EXISTS idea_execution_state (
    idea_id INTEGER PRIMARY KEY,
    started_at TEXT,
    last_heartbeat_at TEXT,
    current_step TEXT NOT NULL DEFAULT '',
    retry_count INTEGER NOT NULL DEFAULT 0 CHECK(retry_count >= 0),
    failure_reason TEXT,
    updated_at TEXT NOT NULL
);
"""

_CREATE_PROJECT_IDEAS_SQL = """
CREATE TABLE IF NOT EXISTS project_ideas (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    project_slug TEXT NOT NULL,
    title TEXT NOT NULL,
    category TEXT NOT NULL,
    impact TEXT NOT NULL,
    simple_description TEXT NOT NULL,
    change_prompt TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT 'manual'
        CHECK(source IN ('manual', 'runtime_error', 'audit')),
    status TEXT NOT NULL DEFAULT 'nueva'
        CHECK(status IN ('nueva', 'encolada', 'implementando', 'aplicada', 'descartada'))
);
"""


def _ensure_idea_schema(conn: sqlite3.Connection) -> None:
    existing = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='improvement_ideas';"
    ).fetchone()
    existing_sql = str(existing["sql"] or "") if existing is not None else ""
    if existing is None:
        conn.execute(_CREATE_IMPROVEMENT_IDEAS_SQL)
    elif (
        "visual_qa" not in existing_sql
        or "implementando" not in existing_sql
        or "aplicada" not in existing_sql
        or "'failed'" not in existing_sql
    ):
        conn.execute("ALTER TABLE improvement_ideas RENAME TO _improvement_ideas_legacy;")
        conn.execute(_CREATE_IMPROVEMENT_IDEAS_SQL)
        conn.execute(
            """
            INSERT INTO improvement_ideas (
                id, created_at, title, category, impact,
                simple_description, technical_prompt, status, source
            )
            SELECT
                id, created_at, title, category, impact,
                simple_description, technical_prompt, status, source
            FROM _improvement_ideas_legacy;
            """
        )
        conn.execute("DROP TABLE _improvement_ideas_legacy;")
    conn.execute(_CREATE_IDEA_EXECUTION_STATE_SQL)
    existing_idea_cols = {
        str(r["name"])
        for r in conn.execute("PRAGMA table_info(improvement_ideas);").fetchall()
    }
    idea_col_migrations = (
        ("queued_at", "TEXT"),
        ("started_at", "TEXT"),
        ("completed_at", "TEXT"),
        ("duration_seconds", "REAL"),
        ("current_step", "TEXT NOT NULL DEFAULT ''"),
        ("last_heartbeat_at", "TEXT"),
        ("retry_count", "INTEGER NOT NULL DEFAULT 0"),
        ("diagnostic_snapshot", "TEXT NOT NULL DEFAULT ''"),
    )
    for col_name, col_def in idea_col_migrations:
        if col_name not in existing_idea_cols:
            conn.execute(f"ALTER TABLE improvement_ideas ADD COLUMN {col_name} {col_def};")
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
    now_iso = datetime.now(UTC).isoformat()
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS quota_state (
            id INTEGER PRIMARY KEY CHECK(id = 1),
            window_started_at TEXT NOT NULL,
            window_tokens_used INTEGER NOT NULL DEFAULT 0 CHECK(window_tokens_used >= 0),
            estimated_window_capacity INTEGER NOT NULL DEFAULT 180000
                CHECK(estimated_window_capacity >= 1000),
            quota_paused_until TEXT,
            reserve_threshold_pct INTEGER NOT NULL DEFAULT 30
                CHECK(reserve_threshold_pct >= 0 AND reserve_threshold_pct <= 95),
            turns_used INTEGER NOT NULL DEFAULT 0 CHECK(turns_used >= 0),
            updated_at TEXT NOT NULL
        );
        """
    )
    conn.execute(
        """
        INSERT OR IGNORE INTO quota_state (
            id,
            window_started_at,
            window_tokens_used,
            estimated_window_capacity,
            quota_paused_until,
            reserve_threshold_pct,
            turns_used,
            updated_at
        ) VALUES (1, ?, 0, ?, NULL, ?, 0, ?);
        """,
        (now_iso, DEFAULT_ESTIMATED_WINDOW_CAPACITY, DEFAULT_RESERVE_THRESHOLD_PCT, now_iso),
    )
    existing_proj = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='project_ideas';"
    ).fetchone()
    existing_proj_sql = str(existing_proj["sql"] or "") if existing_proj is not None else ""
    if existing_proj is None:
        conn.execute(_CREATE_PROJECT_IDEAS_SQL)
    elif "encolada" not in existing_proj_sql:
        conn.execute("ALTER TABLE project_ideas RENAME TO _project_ideas_legacy;")
        conn.execute(_CREATE_PROJECT_IDEAS_SQL)
        conn.execute(
            """
            INSERT INTO project_ideas (
                id, created_at, project_slug, title, category, impact,
                simple_description, change_prompt, source, status
            )
            SELECT
                id, created_at, project_slug, title, category, impact,
                simple_description, change_prompt, source, status
            FROM _project_ideas_legacy;
            """
        )
        conn.execute("DROP TABLE _project_ideas_legacy;")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_project_ideas_slug_status "
        "ON project_ideas(project_slug, status);"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_project_ideas_created_at "
        "ON project_ideas(created_at DESC, id DESC);"
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


def _empty_status_counts() -> dict[str, int]:
    return {
        "pending": 0,
        "queued": 0,
        "implementing": 0,
        "implemented": 0,
        "dismissed": 0,
        "all": 0,
    }


class IdeaList(list[dict[str, Any]]):
    """Backward-compatible list of ideas that also carries per-status counts."""

    def __init__(
        self,
        items: Sequence[dict[str, Any]] = (),
        *,
        counts: Mapping[str, int] | None = None,
        status: str | None = None,
    ) -> None:
        super().__init__(items)
        self.counts: dict[str, int] = dict(counts) if counts is not None else _empty_status_counts()
        self.status_counts: dict[str, int] = self.counts
        self.status: str | None = status


def _validate_status(status: Any) -> str:
    if not isinstance(status, str):
        raise TypeError(
            f"Estado de idea inválido {status!r}; se esperaba una cadena entre {sorted(IDEA_VALID_FILTERS)}."
        )
    clean = status.strip().lower()
    mapped = _STATUS_ALIAS_TO_DB.get(clean)
    if mapped is None or mapped not in IDEA_VALID_STATUSES:
        raise ValueError(
            f"Estado de idea inválido {status!r}; se esperaba uno de {sorted(IDEA_VALID_FILTERS)}."
        )
    return mapped


def _validate_status_filter(status: Any) -> tuple[str | None, str]:
    """Validate a single status filter and return (db_status_or_None, canonical_filter_name)."""
    if not isinstance(status, str):
        raise TypeError(
            f"Filtro de estado inválido {status!r}; se esperaba una cadena entre {sorted(IDEA_VALID_FILTERS)}."
        )
    clean = status.strip().lower()
    if clean == "all":
        return None, "all"
    db_status = _validate_status(clean)
    return db_status, _DB_TO_CANONICAL_STATUS[db_status]


def _accumulate_status_counts(rows: Sequence[sqlite3.Row]) -> dict[str, int]:
    counts = _empty_status_counts()
    total = 0
    for row in rows:
        raw_status = str(row["status"] or "").strip().lower()
        cnt = int(row["cnt"] or 0)
        total += cnt
        canonical = _DB_TO_CANONICAL_STATUS.get(raw_status)
        if canonical is not None:
            if canonical in counts:
                counts[canonical] += cnt
            elif cnt > 0:
                counts[canonical] = cnt
    counts["all"] = total
    return counts


def _query_idea_status_counts(conn: sqlite3.Connection) -> dict[str, int]:
    rows = conn.execute(
        "SELECT status, COUNT(*) AS cnt FROM improvement_ideas GROUP BY status;"
    ).fetchall()
    return _accumulate_status_counts(rows)


def _query_project_idea_status_counts(
    conn: sqlite3.Connection,
    project_slug: str,
) -> dict[str, int]:
    rows = conn.execute(
        "SELECT status, COUNT(*) AS cnt FROM project_ideas WHERE project_slug = ? GROUP BY status;",
        (project_slug,),
    ).fetchall()
    return _accumulate_status_counts(rows)


def count_ideas_by_status(db_path: Path | str | None = None) -> dict[str, int]:
    """Return exact counts for every canonical status plus total ('all') in improvement_ideas."""
    conn: sqlite3.Connection | None = None
    try:
        conn = _open_connection(db_path)
        return _query_idea_status_counts(conn)
    finally:
        if conn is not None:
            conn.close()


def count_project_ideas_by_status(
    project_slug: str,
    *,
    db_path: Path | str | None = None,
) -> dict[str, int]:
    """Return exact counts for every canonical status plus total ('all') for a project's ideas."""
    clean_slug = _validate_project_slug(project_slug)
    conn: sqlite3.Connection | None = None
    try:
        conn = _open_connection(db_path)
        return _query_project_idea_status_counts(conn, clean_slug)
    finally:
        if conn is not None:
            conn.close()


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


def _extract_semantic_tokens(text: str) -> list[str]:
    """Normalize ES/EN text (lowercase, NFKD without diacritics/emojis/punctuation) into unique keywords."""
    raw = str(text or "").strip()
    if not raw:
        return []
    decomposed = unicodedata.normalize("NFKD", raw)
    without_marks = "".join(ch for ch in decomposed if unicodedata.category(ch) != "Mn")
    lowered = without_marks.casefold()
    alnum_tokens = [tok for tok in _NON_ALNUM_RE.sub(" ", lowered).split() if len(tok) >= 2]
    if not alnum_tokens:
        return []
    filtered = [tok for tok in alnum_tokens if tok not in _SEMANTIC_STOPWORDS]
    source_tokens = filtered if filtered else alnum_tokens
    unique_tokens: list[str] = []
    seen: set[str] = set()
    for tok in source_tokens:
        if tok not in seen:
            seen.add(tok)
            unique_tokens.append(tok)
    return unique_tokens


def _strip_commit_prefix(commit_line: str) -> str:
    """Remove leading git hash and conventional commit type (preserving optional scope) from a log line."""
    clean = str(commit_line or "").strip()
    if not clean:
        return ""
    without_hash = _COMMIT_HASH_PREFIX_RE.sub("", clean)
    without_type = _CONVENTIONAL_COMMIT_PREFIX_RE.sub(r"\1 ", without_hash)
    return " ".join(without_type.split())


def _semantic_tokens_match(token_a: str, token_b: str) -> bool:
    if token_a == token_b:
        return True
    min_len = min(len(token_a), len(token_b))
    if min_len >= 3 and (token_a + "s" == token_b or token_b + "s" == token_a):
        return True
    if min_len < 4:
        return False
    prefix_len = 0
    for ch_a, ch_b in zip(token_a, token_b):
        if ch_a != ch_b:
            break
        prefix_len += 1
    if prefix_len < 4:
        return False
    max_len = max(len(token_a), len(token_b))
    if prefix_len == min_len and min_len >= 5:
        return True
    if prefix_len >= 6 and (prefix_len / max_len) >= 0.60:
        return True
    return SequenceMatcher(None, token_a, token_b).ratio() >= 0.78


def _semantic_similarity(text_a: str, text_b: str) -> float:
    """Compute lightweight semantic similarity in [0.0, 1.0] combining keyword overlap and SequenceMatcher."""
    tokens_a = _extract_semantic_tokens(text_a)
    tokens_b = _extract_semantic_tokens(text_b)
    if not tokens_a or not tokens_b:
        return 0.0

    matched_a = sum(1 for tok_a in tokens_a if any(_semantic_tokens_match(tok_a, tok_b) for tok_b in tokens_b))
    matched_b = sum(1 for tok_b in tokens_b if any(_semantic_tokens_match(tok_b, tok_a) for tok_a in tokens_a))
    min_matched = min(matched_a, matched_b)
    min_len = min(len(tokens_a), len(tokens_b))
    max_len = max(len(tokens_a), len(tokens_b))

    joined_a = " ".join(tokens_a)
    joined_b = " ".join(tokens_b)
    if min_matched == 0:
        return 0.4 * SequenceMatcher(None, joined_a, joined_b).ratio()

    if min_matched == 1 and max_len > 1:
        jaccard = 1.0 / (len(tokens_a) + len(tokens_b) - 1)
        seq = SequenceMatcher(None, joined_a, joined_b).ratio()
        return min(0.45, 0.55 * jaccard + 0.35 * seq)

    dice = (matched_a + matched_b) / (len(tokens_a) + len(tokens_b))
    containment = min_matched / min_len
    overlap = 0.6 * dice + 0.4 * containment

    sorted_a = " ".join(sorted(tokens_a))
    sorted_b = " ".join(sorted(tokens_b))
    seq_score = max(
        SequenceMatcher(None, joined_a, joined_b).ratio(),
        SequenceMatcher(None, sorted_a, sorted_b).ratio(),
    )
    return 0.6 * overlap + 0.4 * seq_score


def _extract_existing_item_fields(item: Any) -> tuple[str, str, str]:
    if isinstance(item, str):
        return item.strip(), "", ""
    if isinstance(item, Mapping):
        title = str(item.get("title") or "").strip()
        desc = str(item.get("simple_description") or item.get("description") or "").strip()
        status = str(item.get("status") or "").strip()
        return title, desc, status
    title = str(getattr(item, "title", "") or "").strip()
    desc = str(getattr(item, "simple_description", "") or getattr(item, "description", "") or "").strip()
    status = str(getattr(item, "status", "") or "").strip()
    return title, desc, status


def is_duplicate_idea(
    candidate_title: str,
    candidate_desc: str,
    existing_items: Sequence[Any],
    recent_commits: Sequence[str],
) -> tuple[bool, str]:
    """Detect duplicate/paraphrased ideas against historical items (any status) and recent git commits."""
    cand_title = str(candidate_title or "").strip()
    cand_desc = str(candidate_desc or "").strip()
    if not cand_title and not cand_desc:
        return False, ""

    cand_norm_title = normalize_idea_title(cand_title)
    cand_combined = f"{cand_title} {cand_desc}".strip()

    best_score = 0.0
    best_reason = ""

    for item in existing_items:
        ref_title, ref_desc, ref_status = _extract_existing_item_fields(item)
        if not ref_title and not ref_desc:
            continue

        ref_norm_title = normalize_idea_title(ref_title)
        if cand_norm_title and ref_norm_title and cand_norm_title == ref_norm_title:
            score = 1.0
        else:
            title_score = (
                _semantic_similarity(cand_title, ref_title)
                if cand_title and ref_title
                else 0.0
            )
            ref_combined = f"{ref_title} {ref_desc}".strip()
            combined_score = (
                _semantic_similarity(cand_combined, ref_combined)
                if cand_combined and ref_combined
                else 0.0
            )
            score = max(title_score, combined_score)

        if score >= IDEA_DUPLICATE_SIMILARITY_THRESHOLD and score > best_score:
            best_score = score
            label = ref_title or ref_desc[:80]
            status_part = f" [{ref_status}]" if ref_status else ""
            best_reason = f"Similitud {score:.2f} con idea existente{status_part}: {label}"

    for raw_commit in recent_commits:
        commit_line = str(raw_commit or "").strip()
        commit_msg = _strip_commit_prefix(commit_line)
        if not commit_msg:
            continue

        title_score = _semantic_similarity(cand_title, commit_msg) if cand_title else 0.0
        combined_score = _semantic_similarity(cand_combined, commit_msg) if cand_combined else 0.0
        score = max(title_score, combined_score)

        if score >= IDEA_DUPLICATE_SIMILARITY_THRESHOLD and score > best_score:
            best_score = score
            best_reason = f"Similitud {score:.2f} con commit reciente: {commit_line}"

    if best_score >= IDEA_DUPLICATE_SIMILARITY_THRESHOLD:
        return True, best_reason
    return False, ""


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
    keys = set(row.keys())
    raw_queued = row["queued_at"] if "queued_at" in keys else None
    raw_started = row["started_at"] if "started_at" in keys else None
    raw_completed = row["completed_at"] if "completed_at" in keys else None
    raw_duration = row["duration_seconds"] if "duration_seconds" in keys else None
    raw_hb = row["last_heartbeat_at"] if "last_heartbeat_at" in keys else None
    raw_step = row["current_step"] if "current_step" in keys else ""
    raw_retry = row["retry_count"] if "retry_count" in keys else 0
    raw_diag = row["diagnostic_snapshot"] if "diagnostic_snapshot" in keys else ""
    raw_failure = row["failure_reason"] if "failure_reason" in keys else None
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
        "queued_at": str(raw_queued) if raw_queued else None,
        "started_at": str(raw_started) if raw_started else None,
        "completed_at": str(raw_completed) if raw_completed else None,
        "duration_seconds": float(raw_duration) if raw_duration is not None else None,
        "last_heartbeat_at": str(raw_hb) if raw_hb else None,
        "current_step": str(raw_step or ""),
        "retry_count": max(0, int(raw_retry or 0)),
        "diagnostic_snapshot": str(raw_diag or ""),
        "failure_reason": str(raw_failure) if raw_failure else None,
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
    statuses: Sequence[str] | str | None = _UNSET,
    db_path: Path | str | None = None,
    *,
    status: str | None = None,
    limit: int = DEFAULT_LIST_LIMIT,
    include_counts: bool = False,
) -> IdeaList | dict[str, Any]:
    """List improvement ideas filtered by status, ordered most recent first, with per-status counts."""
    if statuses is not _UNSET and status is not None:
        raise ValueError("No se puede especificar 'statuses' y 'status' al mismo tiempo.")

    status_tuple: tuple[str, ...] | None
    filter_label: str
    if status is not None:
        db_status, filter_label = _validate_status_filter(status)
        status_tuple = None if db_status is None else (db_status,)
    elif statuses is _UNSET:
        status_tuple = _DEFAULT_ACTIVE_IDEA_STATUSES
        filter_label = "active"
    elif statuses is None:
        status_tuple = None
        filter_label = "all"
    elif isinstance(statuses, str):
        db_status, filter_label = _validate_status_filter(statuses)
        status_tuple = None if db_status is None else (db_status,)
    else:
        parsed_statuses: list[str] = []
        has_all = False
        for raw_item in statuses:
            db_status, _canonical = _validate_status_filter(raw_item)
            if db_status is None:
                has_all = True
            else:
                parsed_statuses.append(db_status)
        if has_all:
            status_tuple = None
            filter_label = "all"
        else:
            status_tuple = tuple(parsed_statuses)
            if not status_tuple:
                filter_label = ""
            elif len(status_tuple) == 1:
                filter_label = _DB_TO_CANONICAL_STATUS[status_tuple[0]]
            else:
                filter_label = ",".join(_DB_TO_CANONICAL_STATUS[s] for s in status_tuple)

    if isinstance(limit, bool) or not isinstance(limit, int):
        bounded_limit = DEFAULT_LIST_LIMIT
    else:
        bounded_limit = max(1, min(limit, MAX_LIST_LIMIT))

    conn: sqlite3.Connection | None = None
    try:
        conn = _open_connection(db_path)
        counts = _query_idea_status_counts(conn)
        if status_tuple == ():
            items: list[dict[str, Any]] = []
        else:
            params: list[Any] = []
            where_sql = ""
            if status_tuple is not None:
                placeholders = ", ".join("?" for _ in status_tuple)
                where_sql = f"WHERE i.status IN ({placeholders})"
                params.extend(status_tuple)
            params.append(bounded_limit)
            rows = conn.execute(
                f"{_IDEA_JOIN_SELECT_SQL} "
                f"{where_sql} ORDER BY i.created_at DESC, i.id DESC LIMIT ?;",
                tuple(params),
            ).fetchall()
            items = [_row_to_idea(row) for row in rows]
        if include_counts:
            return {
                "ideas": items,
                "counts": counts,
                "status": filter_label,
            }
        return IdeaList(items, counts=counts, status=filter_label)
    finally:
        if conn is not None:
            conn.close()


def update_idea_status(
    idea_id: int,
    status: str,
    *,
    db_path: Path | str | None = None,
) -> dict[str, Any]:
    """Update status of a factory improvement idea, accepting canonical or DB status names."""
    valid_id = _validate_idea_id(idea_id)
    if valid_id is None:
        raise ValueError(f"idea_id inválido: {idea_id!r}")
    clean_status = _validate_status(status)

    conn: sqlite3.Connection | None = None
    try:
        conn = _open_connection(db_path)
        cursor = conn.execute(
            "UPDATE improvement_ideas SET status = ? WHERE id = ?;",
            (clean_status, valid_id),
        )
        conn.commit()
        if cursor.rowcount <= 0:
            raise KeyError(f"Idea {valid_id} no encontrada.")
        row = conn.execute(
            f"{_IDEA_JOIN_SELECT_SQL} WHERE i.id = ?;",
            (valid_id,),
        ).fetchone()
        assert row is not None
        return _row_to_idea(row)
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
            f"{_IDEA_JOIN_SELECT_SQL} WHERE i.id = ?;",
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
    technical_prompt = redact_secrets(
        str(raw.get("technical_prompt") or raw.get("change_prompt") or "").strip()
    )
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
                f"{_IDEA_JOIN_SELECT_SQL} WHERE i.id = ?;",
                (inserted_id,),
            ).fetchone()
            if row is not None:
                created.append(_row_to_idea(row))

        conn.commit()
        return created
    finally:
        if conn is not None:
            conn.close()


def save_visual_qa_ideas(
    ideas: Sequence[Mapping[str, Any]],
    db_path: Path | str | None = None,
    *,
    max_items: int | None = 2,
) -> list[dict[str, Any]]:
    """Persist deduplicated UX improvement ideas originating from visual QA audits."""
    return save_ideas(
        ideas,
        source="visual_qa",
        db_path=db_path,
        max_items=max_items,
    )


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
            f"{_IDEA_JOIN_SELECT_SQL} WHERE i.id = ?;",
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
        now_iso = datetime.now(UTC).isoformat()
        conn.execute(
            "UPDATE improvement_ideas SET status = 'encolada', "
            "queued_at = COALESCE(queued_at, ?) WHERE id = ?;",
            (now_iso, idea["id"]),
        )
        conn.commit()
        row = conn.execute(
            f"{_IDEA_JOIN_SELECT_SQL} WHERE i.id = ?;",
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


def _list_all_historical_ideas(db_path: Path | str | None = None) -> list[dict[str, Any]]:
    """Return all improvement_ideas records across every status (pending/queued/implementing/implemented/dismissed/failed)."""
    conn: sqlite3.Connection | None = None
    try:
        conn = _open_connection(db_path)
        rows = conn.execute(
            f"{_IDEA_JOIN_SELECT_SQL} ORDER BY i.id DESC;"
        ).fetchall()
        return [_row_to_idea(row) for row in rows]
    finally:
        if conn is not None:
            conn.close()


def _get_recent_commits(
    repo_path: Path | str,
    limit: int = DEFAULT_RECENT_COMMITS_LIMIT,
    *,
    runner: Runner | None = None,
) -> list[str]:
    """Safely read recent git commits (`git log --oneline -n <limit>`) without ever raising."""
    try:
        resolved_path = Path(repo_path).resolve()
    except (OSError, RuntimeError, TypeError, ValueError):
        return []
    if not resolved_path.is_dir():
        return []
    if runner is None and not (resolved_path / ".git").exists():
        return []
    try:
        cap = max(1, min(200, int(limit)))
    except (TypeError, ValueError):
        cap = DEFAULT_RECENT_COMMITS_LIMIT

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
            cwd=resolved_path,
            timeout_seconds=DEFAULT_GIT_LOG_TIMEOUT_SECONDS,
            env=clean_env,
        )
    except (CommandTimeoutError, OSError, RuntimeError, ValueError):
        return []
    if res.exit_code != 0 or not res.stdout.strip():
        return []
    return [redact_secrets(line.strip()) for line in res.stdout.splitlines() if line.strip()][:cap]


def _call_get_recent_commits(
    repo_path: Path | str,
    *,
    limit: int = DEFAULT_RECENT_COMMITS_LIMIT,
    runner: Runner | None = None,
) -> list[str]:
    if runner is None:
        return _get_recent_commits(repo_path, limit=limit)
    return _get_recent_commits(repo_path, limit=limit, runner=runner)


def _fetch_recent_git_commits(
    repo_root: Path,
    *,
    commit_limit: int = 15,
    runner: Runner | None = None,
) -> list[str]:
    return _call_get_recent_commits(repo_root, limit=commit_limit, runner=runner)


def _format_negative_history_lines(
    historical_ideas: Sequence[Mapping[str, Any]],
    *,
    max_items: int = _MAX_PROMPT_HISTORY_IDEAS,
) -> list[str]:
    formatted: list[str] = []
    for item in list(historical_ideas)[:max_items]:
        title = redact_secrets(str(item.get("title") or "").strip())
        if not title:
            continue
        raw_status = str(item.get("status") or "").strip().lower()
        canonical = _DB_TO_CANONICAL_STATUS.get(raw_status, raw_status or "unknown")
        desc = redact_secrets(str(item.get("simple_description") or "").strip())
        desc_excerpt = f" — {desc[:90]}" if desc else ""
        formatted.append(f"   - [{canonical}] {title}{desc_excerpt}")
    return formatted


def build_ideation_context(
    db_path: Path | str | None = None,
    repo_root: Path | str | None = None,
    *,
    runner: Runner | None = None,
    commit_limit: int = 15,
    telemetry_limit: int = 5,
    recent_commits: Sequence[str] | None = None,
    historical_ideas: Sequence[Mapping[str, Any]] | None = None,
) -> str:
    """Build a compact context digest combining telemetry, recent commits, .py files, and all historical ideas."""
    resolved_root = Path(repo_root).resolve() if repo_root is not None else _default_repo_root()
    summary = get_telemetry_summary(limit=telemetry_limit, db_path=db_path)
    commits = (
        list(recent_commits)
        if recent_commits is not None
        else _fetch_recent_git_commits(
            resolved_root,
            commit_limit=commit_limit,
            runner=runner,
        )
    )
    py_files = _list_factory_python_files(resolved_root)
    all_ideas = (
        list(historical_ideas)
        if historical_ideas is not None
        else _list_all_historical_ideas(db_path=db_path)
    )

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

    commit_heading_count = len(commits) if recent_commits is not None and commits else commit_limit
    lines.append(
        f"\n2. Últimos commits ya implementados (git log --oneline -n {commit_heading_count}) "
        "— PROHIBIDO proponer mejoras ya cubiertas aquí:"
    )
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
        "\n4. Historial completo de ideas previas en CUALQUIER estado "
        "(pending, queued, implementing, implemented/applied, dismissed) "
        "— PROHIBIDO repetir, parafrasear o proponer equivalentes aunque cambie la redacción:"
    )
    history_lines = _format_negative_history_lines(all_ideas)
    if history_lines:
        lines.extend(history_lines)
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


def _build_ideation_prompt(
    context_text: str,
    count: int,
    *,
    excluded_topics: Sequence[str] = (),
    accepted_titles: Sequence[str] = (),
) -> str:
    extra_block = ""
    if excluded_topics or accepted_titles:
        extra_lines: list[str] = [
            "\nRONDA COMPLEMENTARIA (compleción de cupo — propone temas 100% NUEVOS y distintos):"
        ]
        if accepted_titles:
            extra_lines.append("Ideas ya aceptadas en esta corrida (NO repetir ni parafrasear):")
            extra_lines.extend(f"  - {redact_secrets(t)}" for t in accepted_titles)
        if excluded_topics:
            extra_lines.append(
                "Temas RECHAZADOS por duplicidad con el historial o commits (STRICTAMENTE EXCLUIDOS):"
            )
            extra_lines.extend(f"  - {redact_secrets(topic)}" for topic in excluded_topics)
        extra_block = "\n".join(extra_lines) + "\n"

    return (
        "MODO IDEACIÓN DE SOLO LECTURA (PROHIBIDO editar archivos o ejecutar comandos mutantes).\n"
        "Eres el Radar de Auto-Mejoras de Agent Factory.\n"
        f"Propón exactamente {count} mejoras atómicas, útiles y no repetidas basadas en el contexto real.\n\n"
        f"{context_text}\n"
        f"{extra_block}\n"
        "REGLAS ESTRICTAS DE SALIDA:\n"
        "- Devuelve ÚNICAMENTE un JSON válido (sin comentarios ni explicaciones fuera del JSON).\n"
        '- Formato esperado: {"ideas": [{"title": "<emoji + nombre llamativo>", '
        '"category": "<categoría corta>", "impact": "Alto" | "Medio", '
        '"simple_description": "<explicación clara en español cotidiano>", '
        '"technical_prompt": "<instrucciones técnicas TDD acotadas, indicando correr ÚNICAMENTE pruebas dirigidas del módulo modificado y ruff check>"}]}\n'
        "- PROHIBIDO proponer mejoras ya cubiertas en commits recientes (sección 2) o ya registradas en cualquier estado (sección 4: pending, queued, implementing, implemented/applied, dismissed), ni equivalentes aunque cambie la redacción.\n"
        "- Cada technical_prompt debe ser atómico (<3 min), con pruebas dirigidas (`pytest -q --no-cov tests/test_<modulo>.py`) y sin modificar archivos prohibidos (.env, MASTER_PROMPT.md, AGENTS.md, config/models.toml, data/, workspaces/).\n"
    )


def _filter_unique_candidates(
    candidates: Sequence[dict[str, str]],
    existing_items: Sequence[Mapping[str, Any]],
    recent_commits: Sequence[str],
    *,
    already_accepted: Sequence[dict[str, str]] = (),
    max_items: int | None = None,
) -> tuple[list[dict[str, str]], list[tuple[str, str]]]:
    """Filter candidate ideas with is_duplicate_idea against history, commits, and same-batch items."""
    accepted: list[dict[str, str]] = []
    rejected: list[tuple[str, str]] = []
    comparison_pool: list[Mapping[str, Any]] = [*existing_items, *already_accepted]

    for cand in candidates:
        if max_items is not None and len(accepted) >= max_items:
            break
        is_dup, reason = is_duplicate_idea(
            cand["title"],
            cand["simple_description"],
            comparison_pool,
            recent_commits,
        )
        if is_dup:
            rejected.append((cand["title"], reason))
            continue
        accepted.append(cand)
        comparison_pool.append(cand)

    return accepted, rejected


def _invoke_readonly_agy(
    prompt: str,
    *,
    base_cmd: Sequence[str],
    active_runner: Any,
    timeout_seconds: float,
    model: str | None = None,
    tmp_prefix: str = "af_idea_prompt_",
    cwd_prefix: str = "af_idea_radar_",
    db_path: Path | str | None = None,
    log_label: str = "Idea generation",
    best_effort: bool = False,
) -> tuple[bool, list[dict[str, str]], bool]:
    """Invoke agy in read-only mode, record quota/usage, and parse candidate ideas."""
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
                prefix=tmp_prefix,
                delete=False,
            ) as tf:
                tf.write(prompt)
                tmp_prompt = Path(tf.name).resolve()
            prompt_arg = f"Lee y sigue exactamente las instrucciones del archivo: {tmp_prompt}"

        cmd = [*base_cmd, "-p", prompt_arg]
        if isinstance(model, str) and model.strip():
            cmd.extend(["--model", model.strip()])

        with tempfile.TemporaryDirectory(prefix=cwd_prefix) as ro_cwd:
            res = _run_port_command(
                active_runner,
                cmd,
                cwd=Path(ro_cwd),
                timeout_seconds=float(timeout_seconds),
                env=clean_env,
            )
    except Exception as exc:
        if not best_effort and not isinstance(exc, (CommandTimeoutError, OSError, RuntimeError, ValueError)):
            raise
        logger.warning("%s failed: %s", log_label, redact_secrets(str(exc)))
        return False, [], False
    finally:
        if tmp_prompt is not None:
            tmp_prompt.unlink(missing_ok=True)

    if res.exit_code != 0 or not res.stdout.strip():
        detail = redact_secrets((res.stderr or res.stdout or f"exit code {res.exit_code}").strip())
        if is_quota_429_error(detail):
            record_agy_429_pause(detail, db_path=db_path)
            logger.warning("%s CLI hit quota 429: %s", log_label, detail)
            return False, [], True
        record_agy_usage(
            len(prompt),
            len(res.stdout or "") + len(res.stderr or ""),
            db_path=db_path,
        )
        logger.warning("%s CLI returned non-zero/empty output: %s", log_label, detail)
        return False, [], False

    record_agy_usage(
        len(prompt),
        len(res.stdout or "") + len(res.stderr or ""),
        db_path=db_path,
    )
    return True, parse_ideas_payload(res.stdout), False


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
    """Invoke agy.EXE in read-only mode to propose new ideas, filter semantic duplicates, and persist them."""
    eff_source = _validate_source(source)
    if isinstance(count, bool) or not isinstance(count, int) or count <= 0:
        raise ValueError("count debe ser un entero positivo.")
    bounded_count = min(count, MAX_IDEATION_COUNT)

    resolved_root = Path(repo_root).resolve() if repo_root is not None else _default_repo_root()
    historical_ideas = _list_all_historical_ideas(db_path=db_path)
    recent_commits = _call_get_recent_commits(
        resolved_root,
        limit=DEFAULT_RECENT_COMMITS_LIMIT,
        runner=git_runner,
    )
    context_text = build_ideation_context(
        db_path=db_path,
        repo_root=resolved_root,
        commit_limit=DEFAULT_RECENT_COMMITS_LIMIT,
        recent_commits=recent_commits,
        historical_ideas=historical_ideas,
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

    ok, parsed, hit_429 = _invoke_readonly_agy(
        prompt,
        base_cmd=base_cmd,
        active_runner=active_runner,
        timeout_seconds=timeout_seconds,
        model=model,
        tmp_prefix="af_idea_prompt_",
        cwd_prefix="af_idea_radar_",
        db_path=db_path,
        log_label="Idea generation",
        best_effort=False,
    )
    if not ok or hit_429 or not parsed:
        return []

    accepted, rejected = _filter_unique_candidates(
        parsed,
        historical_ideas,
        recent_commits,
        max_items=bounded_count,
    )

    if len(accepted) < bounded_count and rejected:
        missing = bounded_count - len(accepted)
        complementary_prompt = _build_ideation_prompt(
            context_text,
            missing,
            excluded_topics=[f"{title} ({reason})" for title, reason in rejected],
            accepted_titles=[item["title"] for item in accepted],
        )
        ok_retry, parsed_retry, _retry_429 = _invoke_readonly_agy(
            complementary_prompt,
            base_cmd=base_cmd,
            active_runner=active_runner,
            timeout_seconds=timeout_seconds,
            model=model,
            tmp_prefix="af_idea_prompt_",
            cwd_prefix="af_idea_radar_",
            db_path=db_path,
            log_label="Idea generation complementary round",
            best_effort=True,
        )
        if ok_retry and parsed_retry:
            extra, _extra_rejected = _filter_unique_candidates(
                parsed_retry,
                historical_ideas,
                recent_commits,
                already_accepted=accepted,
                max_items=missing,
            )
            accepted.extend(extra)

    if not accepted:
        return []

    return save_ideas(
        accepted,
        source=eff_source,
        db_path=db_path,
        max_items=bounded_count,
    )


generate_ideas = generate_new_ideas


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
        if count_ideas_by_status(db_path=self.db_path).get("queued", 0) >= MAX_AUTO_QUEUED_IDEAS:
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
            {_IDEA_JOIN_SELECT_SQL}
            WHERE i.status = 'nueva'
            ORDER BY
                CASE WHEN i.impact = 'Alto' THEN 0 ELSE 1 END ASC,
                i.created_at ASC,
                i.id ASC
            LIMIT 1;
            """
        ).fetchone()
        return _row_to_idea(row) if row is not None else None
    finally:
        if conn is not None:
            conn.close()


def select_next_queued_idea(
    db_path: Path | str | None = None,
) -> dict[str, Any] | None:
    """Return the highest-priority queued ('encolada') idea ready for sequential execution, or None."""
    conn: sqlite3.Connection | None = None
    try:
        conn = _open_connection(db_path)
        row = conn.execute(
            f"""
            {_IDEA_JOIN_SELECT_SQL}
            WHERE i.status = 'encolada'
            ORDER BY
                CASE WHEN i.impact = 'Alto' THEN 0 ELSE 1 END ASC,
                i.created_at ASC,
                i.id ASC
            LIMIT 1;
            """
        ).fetchone()
        return _row_to_idea(row) if row is not None else None
    finally:
        if conn is not None:
            conn.close()


def _normalize_utc_now(now: datetime | Callable[[], datetime] | None = None) -> datetime:
    ref = now() if callable(now) else (now if now is not None else datetime.now(UTC))
    if ref.tzinfo is None:
        return ref.replace(tzinfo=UTC)
    return ref


def register_runtime_status_provider(
    provider: Callable[[], Mapping[str, Any]] | None,
) -> None:
    """Register an optional runtime worker/quota provider without creating circular imports."""
    global _RUNTIME_STATUS_PROVIDER
    with _RUNTIME_STATUS_LOCK:
        _RUNTIME_STATUS_PROVIDER = provider


def _resolve_runtime_worker_and_quota(
    *,
    db_path: Path | str | None,
    worker_alive: bool | Callable[[], bool] | None,
    quota_paused: bool | Callable[[], bool] | None,
    ref_now: datetime,
) -> tuple[bool, bool]:
    resolved_worker: bool | None = (
        bool(worker_alive())
        if callable(worker_alive)
        else (bool(worker_alive) if worker_alive is not None else None)
    )
    resolved_quota: bool | None = (
        bool(quota_paused())
        if callable(quota_paused)
        else (bool(quota_paused) if quota_paused is not None else None)
    )

    if resolved_worker is None or resolved_quota is None:
        provider_payload: Mapping[str, Any] | None = None
        with _RUNTIME_STATUS_LOCK:
            provider = _RUNTIME_STATUS_PROVIDER
        if provider is not None:
            try:
                raw = provider()
                if isinstance(raw, Mapping):
                    provider_payload = raw
            except Exception as exc:  # noqa: BLE001
                logger.debug("Runtime status provider failed: %s", exc)
        if provider_payload is None:
            try:
                from agent_factory.self_improver import get_idea_worker_runtime_status

                provider_payload = get_idea_worker_runtime_status(db_path=db_path, now=ref_now)
            except Exception as exc:  # noqa: BLE001
                logger.debug("Lazy self_improver runtime status skipped: %s", exc)
        if isinstance(provider_payload, Mapping):
            if resolved_worker is None and "worker_alive" in provider_payload:
                resolved_worker = bool(provider_payload["worker_alive"])
            if resolved_quota is None and "quota_paused" in provider_payload:
                resolved_quota = bool(provider_payload["quota_paused"])

    if resolved_worker is None:
        resolved_worker = False
    if resolved_quota is None:
        try:
            resolved_quota = bool(get_quota_status(db_path=db_path, now=ref_now).get("is_paused"))
        except Exception as exc:  # noqa: BLE001
            logger.debug("Fallback quota_paused check failed: %s", exc)
            resolved_quota = False

    return resolved_worker, resolved_quota


def start_idea_execution(
    idea_id: int,
    *,
    current_step: str = "Iniciando implementación",
    db_path: Path | str | None = None,
    now: datetime | Callable[[], datetime] | None = None,
) -> dict[str, Any]:
    """Mark an idea as 'implementando' and initialize started_at, last_heartbeat_at, and current_step."""
    valid_id = _validate_idea_id(idea_id)
    if valid_id is None:
        raise ValueError(f"idea_id inválido: {idea_id!r}")
    ref_now = _normalize_utc_now(now)
    now_iso = ref_now.isoformat()
    clean_step = redact_secrets(str(current_step or "").strip())[:240]

    conn: sqlite3.Connection | None = None
    try:
        conn = _open_connection(db_path)
        cur = conn.execute(
            "UPDATE improvement_ideas "
            "SET status = 'implementando', started_at = ?, last_heartbeat_at = ?, current_step = ? "
            "WHERE id = ?;",
            (now_iso, now_iso, clean_step, valid_id),
        )
        if cur.rowcount <= 0:
            raise KeyError(f"Idea {valid_id} no encontrada.")
        conn.execute(
            """
            INSERT INTO idea_execution_state (
                idea_id, started_at, last_heartbeat_at, current_step,
                retry_count, failure_reason, updated_at
            ) VALUES (?, ?, ?, ?, 0, NULL, ?)
            ON CONFLICT(idea_id) DO UPDATE SET
                started_at = excluded.started_at,
                last_heartbeat_at = excluded.last_heartbeat_at,
                current_step = excluded.current_step,
                failure_reason = NULL,
                updated_at = excluded.updated_at;
            """,
            (valid_id, now_iso, now_iso, clean_step, now_iso),
        )
        conn.commit()
        row = conn.execute(
            f"{_IDEA_JOIN_SELECT_SQL} WHERE i.id = ?;",
            (valid_id,),
        ).fetchone()
        assert row is not None
        return _row_to_idea(row)
    finally:
        if conn is not None:
            conn.close()


def record_idea_heartbeat(
    idea_id: int,
    *,
    current_step: str | None = None,
    db_path: Path | str | None = None,
    now: datetime | Callable[[], datetime] | None = None,
) -> dict[str, Any]:
    """Refresh last_heartbeat_at (and optional current_step) for an idea in execution."""
    valid_id = _validate_idea_id(idea_id)
    if valid_id is None:
        raise ValueError(f"idea_id inválido: {idea_id!r}")
    ref_now = _normalize_utc_now(now)
    now_iso = ref_now.isoformat()
    clean_step = (
        redact_secrets(str(current_step).strip())[:240]
        if current_step is not None
        else None
    )

    conn: sqlite3.Connection | None = None
    try:
        conn = _open_connection(db_path)
        exists = conn.execute(
            "SELECT id FROM improvement_ideas WHERE id = ?;",
            (valid_id,),
        ).fetchone()
        if exists is None:
            raise KeyError(f"Idea {valid_id} no encontrada.")
        if clean_step is not None:
            conn.execute(
                "UPDATE improvement_ideas "
                "SET started_at = COALESCE(started_at, ?), last_heartbeat_at = ?, current_step = ? "
                "WHERE id = ?;",
                (now_iso, now_iso, clean_step, valid_id),
            )
            conn.execute(
                """
                INSERT INTO idea_execution_state (
                    idea_id, started_at, last_heartbeat_at, current_step,
                    retry_count, failure_reason, updated_at
                ) VALUES (?, ?, ?, ?, 0, NULL, ?)
                ON CONFLICT(idea_id) DO UPDATE SET
                    started_at = COALESCE(idea_execution_state.started_at, excluded.started_at),
                    last_heartbeat_at = excluded.last_heartbeat_at,
                    current_step = excluded.current_step,
                    updated_at = excluded.updated_at;
                """,
                (valid_id, now_iso, now_iso, clean_step, now_iso),
            )
        else:
            conn.execute(
                "UPDATE improvement_ideas "
                "SET started_at = COALESCE(started_at, ?), last_heartbeat_at = ? "
                "WHERE id = ?;",
                (now_iso, now_iso, valid_id),
            )
            conn.execute(
                """
                INSERT INTO idea_execution_state (
                    idea_id, started_at, last_heartbeat_at, current_step,
                    retry_count, failure_reason, updated_at
                ) VALUES (?, ?, ?, '', 0, NULL, ?)
                ON CONFLICT(idea_id) DO UPDATE SET
                    started_at = COALESCE(idea_execution_state.started_at, excluded.started_at),
                    last_heartbeat_at = excluded.last_heartbeat_at,
                    updated_at = excluded.updated_at;
                """,
                (valid_id, now_iso, now_iso, now_iso),
            )
        conn.commit()
        row = conn.execute(
            f"{_IDEA_JOIN_SELECT_SQL} WHERE i.id = ?;",
            (valid_id,),
        ).fetchone()
        assert row is not None
        return _row_to_idea(row)
    finally:
        if conn is not None:
            conn.close()


update_idea_heartbeat = record_idea_heartbeat


def finish_idea_execution(
    idea_id: int,
    *,
    status: str = "implemented",
    current_step: str = "",
    failure_reason: str | None = None,
    increment_retry: bool = False,
    clear_heartbeat: bool = True,
    db_path: Path | str | None = None,
    now: datetime | Callable[[], datetime] | None = None,
) -> dict[str, Any]:
    """Transition an executing idea to its target status and update execution metadata cleanly."""
    valid_id = _validate_idea_id(idea_id)
    if valid_id is None:
        raise ValueError(f"idea_id inválido: {idea_id!r}")
    db_status = _validate_status(status)
    ref_now = _normalize_utc_now(now)
    now_iso = ref_now.isoformat()
    clean_step = redact_secrets(str(current_step or "").strip())[:240]
    clean_reason = (
        redact_secrets(str(failure_reason).strip())[:500]
        if failure_reason is not None and str(failure_reason).strip()
        else None
    )

    conn: sqlite3.Connection | None = None
    try:
        conn = _open_connection(db_path)
        existing_row = conn.execute(
            "SELECT queued_at, started_at FROM improvement_ideas WHERE id = ?;",
            (valid_id,),
        ).fetchone()
        if existing_row is None:
            raise KeyError(f"Idea {valid_id} no encontrada.")
        existing_exec = conn.execute(
            "SELECT retry_count, started_at, last_heartbeat_at FROM idea_execution_state WHERE idea_id = ?;",
            (valid_id,),
        ).fetchone()
        prev_retry = int(existing_exec["retry_count"]) if existing_exec is not None else 0
        next_retry = prev_retry + 1 if increment_retry else prev_retry
        next_started = None if clear_heartbeat else (
            existing_exec["started_at"] if existing_exec is not None else existing_row["started_at"]
        )
        next_hb = None if clear_heartbeat else (
            existing_exec["last_heartbeat_at"] if existing_exec is not None else now_iso
        )
        is_terminal = db_status in {"aplicada", "failed"}
        completed_val: str | None = now_iso if is_terminal else None
        duration_val: float | None = None
        if is_terminal:
            ref_start_dt = (
                _parse_iso_datetime(
                    (existing_exec["started_at"] if existing_exec is not None else None)
                    or existing_row["started_at"]
                    or existing_row["queued_at"]
                )
            )
            if ref_start_dt is not None:
                duration_val = round(max(0.0, (ref_now - ref_start_dt).total_seconds()), 2)
        conn.execute(
            """
            UPDATE improvement_ideas
            SET status = ?,
                completed_at = COALESCE(?, completed_at),
                duration_seconds = COALESCE(?, duration_seconds),
                current_step = ?,
                last_heartbeat_at = ?,
                retry_count = ?
            WHERE id = ?;
            """,
            (db_status, completed_val, duration_val, clean_step, next_hb, next_retry, valid_id),
        )
        conn.execute(
            """
            INSERT INTO idea_execution_state (
                idea_id, started_at, last_heartbeat_at, current_step,
                retry_count, failure_reason, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(idea_id) DO UPDATE SET
                started_at = excluded.started_at,
                last_heartbeat_at = excluded.last_heartbeat_at,
                current_step = excluded.current_step,
                retry_count = excluded.retry_count,
                failure_reason = excluded.failure_reason,
                updated_at = excluded.updated_at;
            """,
            (valid_id, next_started, next_hb, clean_step, next_retry, clean_reason, now_iso),
        )
        conn.commit()
        row = conn.execute(
            f"{_IDEA_JOIN_SELECT_SQL} WHERE i.id = ?;",
            (valid_id,),
        ).fetchone()
        assert row is not None
        return _row_to_idea(row)
    finally:
        if conn is not None:
            conn.close()


def reconcile_idea_queue(
    stale_seconds: int = DEFAULT_IDEA_STALE_SECONDS,
    *,
    db_path: Path | str | None = None,
    worker_alive: bool | Callable[[], bool] | None = None,
    quota_paused: bool | Callable[[], bool] | None = None,
    now: datetime | Callable[[], datetime] | None = None,
    max_retries: int = MAX_IDEA_RETRIES,
) -> dict[str, Any]:
    """Reconcile improvement_ideas execution states, enforce max 1 active idea, and return queue diagnostics."""
    if isinstance(stale_seconds, bool) or not isinstance(stale_seconds, (int, float)):
        raise TypeError("stale_seconds debe ser un número >= 0.")
    eff_stale_seconds = float(stale_seconds)
    if math.isnan(eff_stale_seconds) or math.isinf(eff_stale_seconds) or eff_stale_seconds < 0.0:
        raise ValueError("stale_seconds debe ser un número finito >= 0.")

    if isinstance(max_retries, bool) or not isinstance(max_retries, int) or max_retries < 0:
        raise ValueError("max_retries debe ser un entero >= 0.")

    ref_now = _normalize_utc_now(now)
    now_iso = ref_now.isoformat()
    eff_worker_alive, eff_quota_paused = _resolve_runtime_worker_and_quota(
        db_path=db_path,
        worker_alive=worker_alive,
        quota_paused=quota_paused,
        ref_now=ref_now,
    )

    requeued_ids: list[int] = []
    failed_ids: list[int] = []
    reconciled_actions: list[dict[str, Any]] = []
    active_idea: dict[str, Any] | None = None

    conn: sqlite3.Connection | None = None
    try:
        conn = _open_connection(db_path)
        impl_rows = conn.execute(
            f"{_IDEA_JOIN_SELECT_SQL} WHERE i.status = 'implementando' ORDER BY i.id ASC;"
        ).fetchall()
        implementing = [_row_to_idea(row) for row in impl_rows]

        valid_candidates: list[tuple[datetime, datetime, int, dict[str, Any]]] = []
        stale_map: dict[int, bool] = {}

        for item in implementing:
            idea_id = int(item["id"])
            hb_dt = _parse_iso_datetime(item["last_heartbeat_at"])
            started_dt = _parse_iso_datetime(item["started_at"])
            is_stale = hb_dt is None or (ref_now - hb_dt).total_seconds() > eff_stale_seconds
            stale_map[idea_id] = is_stale
            if eff_worker_alive and not is_stale and hb_dt is not None:
                valid_candidates.append((hb_dt, started_dt or hb_dt, idea_id, item))

        keeper: dict[str, Any] | None = None
        if valid_candidates:
            valid_candidates.sort(key=lambda entry: (entry[0], entry[1], entry[2]), reverse=True)
            keeper = valid_candidates[0][3]

        keeper_id = int(keeper["id"]) if keeper is not None else None

        for item in implementing:
            idea_id = int(item["id"])
            if keeper_id is not None and idea_id == keeper_id:
                continue

            current_retry = int(item["retry_count"] or 0)
            is_stale = stale_map.get(idea_id, True)
            is_concurrent_dup = eff_worker_alive and not is_stale and keeper_id is not None

            if is_concurrent_dup:
                conn.execute(
                    "UPDATE improvement_ideas SET status = 'encolada' WHERE id = ?;",
                    (idea_id,),
                )
                conn.execute(
                    """
                    INSERT INTO idea_execution_state (
                        idea_id, started_at, last_heartbeat_at, current_step,
                        retry_count, failure_reason, updated_at
                    ) VALUES (?, NULL, NULL, '', ?, NULL, ?)
                    ON CONFLICT(idea_id) DO UPDATE SET
                        started_at = NULL,
                        last_heartbeat_at = NULL,
                        current_step = '',
                        retry_count = excluded.retry_count,
                        failure_reason = NULL,
                        updated_at = excluded.updated_at;
                    """,
                    (idea_id, current_retry, now_iso),
                )
                requeued_ids.append(idea_id)
                reconciled_actions.append(
                    {
                        "idea_id": idea_id,
                        "title": item["title"],
                        "action": "requeued",
                        "reason": "duplicate_implementing",
                        "retry_count": current_retry,
                    }
                )
                continue

            next_retry = current_retry + 1
            cause_detail = (
                "worker inactivo"
                if not eff_worker_alive
                else f"latido expirado (> {int(eff_stale_seconds)}s)"
            )
            if next_retry > max_retries:
                failure_msg = (
                    f"Superó el máximo de {max_retries} reintentos tras quedar sin progreso "
                    f"({cause_detail})."
                )
                ref_start_dt = (
                    _parse_iso_datetime(item.get("started_at"))
                    or _parse_iso_datetime(item.get("queued_at"))
                )
                dur_val = (
                    round(max(0.0, (ref_now - ref_start_dt).total_seconds()), 2)
                    if ref_start_dt is not None
                    else None
                )
                conn.execute(
                    """
                    UPDATE improvement_ideas
                    SET status = 'failed',
                        completed_at = COALESCE(completed_at, ?),
                        duration_seconds = COALESCE(duration_seconds, ?),
                        started_at = NULL,
                        last_heartbeat_at = NULL,
                        current_step = '',
                        retry_count = ?
                    WHERE id = ?;
                    """,
                    (now_iso, dur_val, next_retry, idea_id),
                )
                conn.execute(
                    """
                    INSERT INTO idea_execution_state (
                        idea_id, started_at, last_heartbeat_at, current_step,
                        retry_count, failure_reason, updated_at
                    ) VALUES (?, NULL, NULL, '', ?, ?, ?)
                    ON CONFLICT(idea_id) DO UPDATE SET
                        started_at = NULL,
                        last_heartbeat_at = NULL,
                        current_step = '',
                        retry_count = excluded.retry_count,
                        failure_reason = excluded.failure_reason,
                        updated_at = excluded.updated_at;
                    """,
                    (idea_id, next_retry, failure_msg, now_iso),
                )
                failed_ids.append(idea_id)
                reconciled_actions.append(
                    {
                        "idea_id": idea_id,
                        "title": item["title"],
                        "action": "failed",
                        "reason": failure_msg,
                        "retry_count": next_retry,
                    }
                )
            else:
                conn.execute(
                    """
                    UPDATE improvement_ideas
                    SET status = 'encolada',
                        started_at = NULL,
                        last_heartbeat_at = NULL,
                        current_step = '',
                        retry_count = ?
                    WHERE id = ?;
                    """,
                    (next_retry, idea_id),
                )
                conn.execute(
                    """
                    INSERT INTO idea_execution_state (
                        idea_id, started_at, last_heartbeat_at, current_step,
                        retry_count, failure_reason, updated_at
                    ) VALUES (?, NULL, NULL, '', ?, NULL, ?)
                    ON CONFLICT(idea_id) DO UPDATE SET
                        started_at = NULL,
                        last_heartbeat_at = NULL,
                        current_step = '',
                        retry_count = excluded.retry_count,
                        failure_reason = NULL,
                        updated_at = excluded.updated_at;
                    """,
                    (idea_id, next_retry, now_iso),
                )
                requeued_ids.append(idea_id)
                reconciled_actions.append(
                    {
                        "idea_id": idea_id,
                        "title": item["title"],
                        "action": "requeued",
                        "reason": cause_detail,
                        "retry_count": next_retry,
                    }
                )

        if reconciled_actions:
            conn.commit()

        raw_counts = _query_idea_status_counts(conn)
        if keeper is not None:
            ref_start = (
                _parse_iso_datetime(keeper["started_at"])
                or _parse_iso_datetime(keeper["last_heartbeat_at"])
                or ref_now
            )
            elapsed_seconds = round(max(0.0, (ref_now - ref_start).total_seconds()), 2)
            active_idea = {
                "id": int(keeper["id"]),
                "title": str(keeper["title"]),
                "elapsed_seconds": elapsed_seconds,
                "current_step": str(keeper["current_step"] or ""),
                "started_at": keeper["started_at"],
                "last_heartbeat_at": keeper["last_heartbeat_at"],
                "retry_count": int(keeper["retry_count"] or 0),
            }

        counts: dict[str, int] = {
            "pending": int(raw_counts.get("pending", 0)),
            "queued": int(raw_counts.get("queued", 0)),
            "implementing": int(raw_counts.get("implementing", 0)),
            "implemented": int(raw_counts.get("implemented", 0)),
            "dismissed": int(raw_counts.get("dismissed", 0)),
            "failed": int(raw_counts.get("failed", 0)),
            "all": int(raw_counts.get("all", 0)),
            "requeued": len(requeued_ids),
            "marked_failed": len(failed_ids),
            "reconciled": len(reconciled_actions),
        }

        if eff_quota_paused:
            pause_reason = "quota_pause"
        elif active_idea is not None:
            pause_reason = "ok"
        elif counts["queued"] > 0:
            pause_reason = "ok" if eff_worker_alive else "no_worker"
        else:
            pause_reason = "empty"

        return {
            "worker_alive": eff_worker_alive,
            "quota_paused": eff_quota_paused,
            "pause_reason": pause_reason,
            "active_idea": active_idea,
            "counts": counts,
            "reconciled": {
                "requeued_ids": requeued_ids,
                "failed_ids": failed_ids,
                "actions": reconciled_actions,
            },
            "reconciled_count": len(reconciled_actions),
            "stale_seconds": eff_stale_seconds,
        }
    finally:
        if conn is not None:
            conn.close()


def _collect_queue_diagnostic_snapshot(
    *,
    repo_root: Path | None = None,
    db_path: Path | str | None = None,
) -> dict[str, Any]:
    """Capture a black-box diagnostic snapshot of autonomous state, quota, threads, and .liwi-staging git."""
    eff_root = Path(repo_root).resolve() if repo_root is not None else _default_repo_root()
    staging_dir = eff_root / ".liwi-staging"
    lock_paths = [
        staging_dir / ".git" / "index.lock",
        eff_root / ".git" / "worktrees" / ".liwi-staging" / "index.lock",
        eff_root / ".git" / "worktrees" / "liwi-staging" / "index.lock",
    ]
    found_locks = [str(p) for p in lock_paths if p.exists()]

    git_status = ""
    git_log = ""
    if staging_dir.is_dir():
        try:
            st_proc = subprocess.run(
                ["git", "-C", str(staging_dir), "status", "--short"],
                capture_output=True,
                text=True,
                timeout=5.0,
                check=False,
            )
            git_status = redact_secrets((st_proc.stdout or "").strip())[:1000]
            log_proc = subprocess.run(
                ["git", "-C", str(staging_dir), "log", "-n", "5", "--oneline"],
                capture_output=True,
                text=True,
                timeout=5.0,
                check=False,
            )
            git_log = redact_secrets((log_proc.stdout or "").strip())[:1000]
        except (OSError, subprocess.SubprocessError) as exc:
            git_status = f"(error inspeccionando git: {exc})"

    return {
        "captured_at": datetime.now(UTC).isoformat(),
        "autonomous_state": get_autonomous_state(db_path=db_path),
        "quota_status": get_quota_status(db_path=db_path),
        "active_threads": [t.name for t in threading.enumerate() if t.is_alive()],
        "git_index_locks": found_locks,
        "staging_git_status": git_status,
        "staging_recent_commits": git_log,
    }


def _cleanup_orphan_staging_locks(repo_root: Path | None = None) -> list[str]:
    eff_root = Path(repo_root).resolve() if repo_root is not None else _default_repo_root()
    candidates = (
        eff_root / ".liwi-staging" / ".git" / "index.lock",
        eff_root / ".git" / "worktrees" / ".liwi-staging" / "index.lock",
        eff_root / ".git" / "worktrees" / "liwi-staging" / "index.lock",
    )
    removed: list[str] = []
    for lock_path in candidates:
        if lock_path.is_file():
            try:
                lock_path.unlink()
                removed.append(str(lock_path))
            except OSError:
                pass
    return removed


def reconcile_and_investigate_queue(
    stale_exec_seconds: int = 300,
    stale_wait_seconds: int = 900,
    *,
    db_path: Path | str | None = None,
    repo_root: Path | str | None = None,
    worker_alive: bool | Callable[[], bool] | None = None,
    quota_paused: bool | Callable[[], bool] | None = None,
    staging_manager: Any | None = None,
    now: datetime | Callable[[], datetime] | None = None,
) -> dict[str, Any]:
    """Detect stalled implementing/queued ideas, capture black-box snapshot, log triage ticket, and recover."""
    ref_now = _normalize_utc_now(now)
    now_iso = ref_now.isoformat()
    eff_root = Path(repo_root).resolve() if repo_root is not None else _default_repo_root()

    base_report = reconcile_idea_queue(
        stale_seconds=stale_exec_seconds,
        db_path=db_path,
        worker_alive=worker_alive,
        quota_paused=quota_paused,
        now=ref_now,
        max_retries=2,
    )
    reconciled_actions = list(base_report["reconciled"]["actions"])
    requeued_ids = list(base_report["reconciled"]["requeued_ids"])
    failed_ids = list(base_report["reconciled"]["failed_ids"])

    # Check if Automejora is ON (without quota_pause), worker is inactive, and first queued idea waited > stale_wait_seconds
    stale_wait_ids: list[int] = []
    auto_st = get_autonomous_state(db_path=db_path)
    if (
        auto_st.get("autonomous_enabled")
        and not base_report["quota_paused"]
        and not base_report["worker_alive"]
    ):
        conn: sqlite3.Connection | None = None
        try:
            conn = _open_connection(db_path)
            first_queued = conn.execute(
                f"{_IDEA_JOIN_SELECT_SQL} WHERE i.status = 'encolada' "
                "ORDER BY COALESCE(i.queued_at, i.created_at) ASC, i.id ASC LIMIT 1;"
            ).fetchone()
            if first_queued is not None:
                q_item = _row_to_idea(first_queued)
                wait_ref = _parse_iso_datetime(q_item["queued_at"]) or _parse_iso_datetime(q_item["created_at"])
                if wait_ref is not None and (ref_now - wait_ref).total_seconds() > float(stale_wait_seconds):
                    qid = int(q_item["id"])
                    next_retry = int(q_item["retry_count"] or 0) + 1
                    stale_wait_ids.append(qid)
                    if next_retry > 2:
                        fail_msg = (
                            f"Superó 2 reintentos tras permanecer más de {int(stale_wait_seconds)}s "
                            "en cola sin avance del worker."
                        )
                        finish_idea_execution(
                            qid,
                            status="failed",
                            failure_reason=fail_msg,
                            increment_retry=True,
                            clear_heartbeat=True,
                            db_path=db_path,
                            now=ref_now,
                        )
                        failed_ids.append(qid)
                        reconciled_actions.append(
                            {
                                "idea_id": qid,
                                "title": q_item["title"],
                                "action": "failed",
                                "reason": fail_msg,
                                "retry_count": next_retry,
                            }
                        )
                    else:
                        conn.execute(
                            "UPDATE improvement_ideas SET queued_at = ?, retry_count = ? WHERE id = ?;",
                            (now_iso, next_retry, qid),
                        )
                        conn.execute(
                            """
                            INSERT INTO idea_execution_state (
                                idea_id, started_at, last_heartbeat_at, current_step,
                                retry_count, failure_reason, updated_at
                            ) VALUES (?, NULL, NULL, '', ?, NULL, ?)
                            ON CONFLICT(idea_id) DO UPDATE SET
                                retry_count = excluded.retry_count,
                                updated_at = excluded.updated_at;
                            """,
                            (qid, next_retry, now_iso),
                        )
                        conn.commit()
                        requeued_ids.append(qid)
                        reconciled_actions.append(
                            {
                                "idea_id": qid,
                                "title": q_item["title"],
                                "action": "requeued_stale_wait",
                                "reason": f"espera > {int(stale_wait_seconds)}s en cola",
                                "retry_count": next_retry,
                            }
                        )
        finally:
            if conn is not None:
                conn.close()

    stall_detected = bool(reconciled_actions)
    diagnostic_snapshot: dict[str, Any] | None = None
    ticket: dict[str, Any] | None = None
    locks_removed: list[str] = []

    if stall_detected:
        diagnostic_snapshot = _collect_queue_diagnostic_snapshot(
            repo_root=eff_root,
            db_path=db_path,
        )
        locks_removed = _cleanup_orphan_staging_locks(eff_root)
        diagnostic_snapshot["locks_removed"] = locks_removed
        snap_json = redact_secrets(json.dumps(diagnostic_snapshot, ensure_ascii=False, default=str))[:4000]

        affected_ids = sorted({int(a["idea_id"]) for a in reconciled_actions if "idea_id" in a})
        if affected_ids:
            conn_u: sqlite3.Connection | None = None
            try:
                conn_u = _open_connection(db_path)
                for aid in affected_ids:
                    conn_u.execute(
                        "UPDATE improvement_ideas SET diagnostic_snapshot = ? WHERE id = ?;",
                        (snap_json, aid),
                    )
                conn_u.commit()
            finally:
                if conn_u is not None:
                    conn_u.close()

        summary_lines = [
            f"- Idea #{a['idea_id']} ({a['title']}): {a['action']} ({a['reason']})"
            for a in reconciled_actions
        ]
        err_text = (
            "Atasco detectado en la cola de auto-mejoras (.liwi-staging):\n"
            + "\n".join(summary_lines)
            + f"\nLocks limpiados: {locks_removed or 'ninguno'}"
        )
        try:
            from agent_factory.error_triage import build_actionable_fix_ticket

            ticket = build_actionable_fix_ticket(
                "queue_watchdog",
                err_text,
                context=diagnostic_snapshot,
                db_path=db_path,
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("queue_watchdog ticket creation skipped: %s", exc)

        if staging_manager is not None and requeued_ids:
            try:
                if hasattr(staging_manager, "wake_worker"):
                    staging_manager.wake_worker()
            except Exception as exc:  # noqa: BLE001
                logger.debug("staging_manager wake skipped: %s", exc)

    return {
        "ok": True,
        "investigated": stall_detected,
        "stall_detected": stall_detected,
        "diagnostic_snapshot": diagnostic_snapshot,
        "locks_removed": locks_removed,
        "ticket": ticket,
        "stale_wait_ids": stale_wait_ids,
        "requeued_ids": requeued_ids,
        "failed_ids": failed_ids,
        "actions": reconciled_actions,
        **base_report,
    }


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


def _should_skip_default_db_write(db_path: Path | str | None) -> bool:
    return (
        db_path is None
        and "PYTEST_CURRENT_TEST" in os.environ
        and not os.environ.get("AGENT_FACTORY_TELEMETRY_DB")
    )


def _parse_iso_datetime(value: str | None) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt
    except ValueError:
        return None


def _compute_quota_payload(
    row: sqlite3.Row,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    ref_now = now if now is not None else datetime.now(UTC)
    if ref_now.tzinfo is None:
        ref_now = ref_now.replace(tzinfo=UTC)

    capacity = max(1000, int(row["estimated_window_capacity"]))
    used = max(0, int(row["window_tokens_used"]))
    turns_used = max(0, int(row["turns_used"]))
    reserve_pct = max(0, min(95, int(row["reserve_threshold_pct"])))
    paused_until_raw = row["quota_paused_until"]
    paused_dt = _parse_iso_datetime(paused_until_raw)
    is_paused = bool(paused_dt is not None and ref_now < paused_dt)

    if is_paused and paused_dt is not None:
        remaining_pct = 0
        estimated_turns_left = 0
        resume_hhmm = paused_dt.astimezone().strftime("%H:%M")
    else:
        remaining_capacity = max(0, capacity - used)
        remaining_pct = max(0, min(100, round((remaining_capacity / capacity) * 100)))
        avg_turn = max(4000, used // turns_used) if turns_used > 0 else DEFAULT_AVG_TURN_TOKENS
        estimated_turns_left = max(0, remaining_capacity // avg_turn)
        resume_hhmm = None

    reserve_reached = (not is_paused) and (remaining_pct <= reserve_pct)
    return {
        "window_started_at": str(row["window_started_at"]),
        "window_tokens_used": used,
        "estimated_window_capacity": capacity,
        "quota_paused_until": str(paused_until_raw) if is_paused and paused_until_raw else None,
        "reserve_threshold_pct": reserve_pct,
        "turns_used": turns_used,
        "quota_remaining_pct": remaining_pct,
        "estimated_turns_left": estimated_turns_left,
        "is_paused": is_paused,
        "reserve_reached": reserve_reached,
        "resume_hhmm": resume_hhmm,
        "updated_at": str(row["updated_at"]),
    }


def get_quota_status(
    db_path: Path | str | None = None,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Return persisted quota meter telemetry, expiring finished 429 pauses or 5h windows cleanly."""
    ref_now = now if now is not None else datetime.now(UTC)
    if ref_now.tzinfo is None:
        ref_now = ref_now.replace(tzinfo=UTC)
    conn: sqlite3.Connection | None = None
    try:
        conn = _open_connection(db_path)
        row = conn.execute(
            "SELECT window_started_at, window_tokens_used, estimated_window_capacity, "
            "quota_paused_until, reserve_threshold_pct, turns_used, updated_at "
            "FROM quota_state WHERE id = 1;"
        ).fetchone()
        if row is None:
            now_iso = ref_now.isoformat()
            return {
                "window_started_at": now_iso,
                "window_tokens_used": 0,
                "estimated_window_capacity": DEFAULT_ESTIMATED_WINDOW_CAPACITY,
                "quota_paused_until": None,
                "reserve_threshold_pct": DEFAULT_RESERVE_THRESHOLD_PCT,
                "turns_used": 0,
                "quota_remaining_pct": 100,
                "estimated_turns_left": DEFAULT_ESTIMATED_WINDOW_CAPACITY // DEFAULT_AVG_TURN_TOKENS,
                "is_paused": False,
                "reserve_reached": False,
                "resume_hhmm": None,
                "updated_at": now_iso,
            }

        paused_dt = _parse_iso_datetime(row["quota_paused_until"])
        window_dt = _parse_iso_datetime(row["window_started_at"])
        reset_needed = (paused_dt is not None and ref_now >= paused_dt) or (
            paused_dt is None
            and window_dt is not None
            and (ref_now - window_dt).total_seconds() >= DEFAULT_QUOTA_WINDOW_SECONDS
        )

        if reset_needed:
            now_iso = ref_now.isoformat()
            conn.execute(
                """
                UPDATE quota_state
                SET window_started_at = ?,
                    window_tokens_used = 0,
                    quota_paused_until = NULL,
                    turns_used = 0,
                    updated_at = ?
                WHERE id = 1;
                """,
                (now_iso, now_iso),
            )
            conn.commit()
            row = conn.execute(
                "SELECT window_started_at, window_tokens_used, estimated_window_capacity, "
                "quota_paused_until, reserve_threshold_pct, turns_used, updated_at "
                "FROM quota_state WHERE id = 1;"
            ).fetchone()

        assert row is not None
        return _compute_quota_payload(row, now=ref_now)
    finally:
        if conn is not None:
            conn.close()


def set_quota_config(
    *,
    reserve_threshold_pct: int | None = None,
    estimated_window_capacity: int | None = None,
    window_tokens_used: int | None = None,
    quota_paused_until: str | None = None,
    clear_pause: bool = False,
    turns_used: int | None = None,
    db_path: Path | str | None = None,
) -> dict[str, Any]:
    """Validate and update quota_state configuration atomically."""
    updates: list[str] = []
    params: list[Any] = []

    if reserve_threshold_pct is not None:
        if isinstance(reserve_threshold_pct, bool) or not isinstance(reserve_threshold_pct, int):
            raise TypeError("reserve_threshold_pct debe ser un entero entre 0 y 95.")
        if reserve_threshold_pct < 0 or reserve_threshold_pct > 95:
            raise ValueError("reserve_threshold_pct debe estar entre 0 y 95.")
        updates.append("reserve_threshold_pct = ?")
        params.append(reserve_threshold_pct)

    if estimated_window_capacity is not None:
        if isinstance(estimated_window_capacity, bool) or not isinstance(estimated_window_capacity, int):
            raise TypeError("estimated_window_capacity debe ser un entero >= 1000.")
        if estimated_window_capacity < 1000:
            raise ValueError("estimated_window_capacity debe ser >= 1000.")
        updates.append("estimated_window_capacity = ?")
        params.append(estimated_window_capacity)

    if window_tokens_used is not None:
        if isinstance(window_tokens_used, bool) or not isinstance(window_tokens_used, int) or window_tokens_used < 0:
            raise ValueError("window_tokens_used debe ser un entero >= 0.")
        updates.append("window_tokens_used = ?")
        params.append(window_tokens_used)

    if turns_used is not None:
        if isinstance(turns_used, bool) or not isinstance(turns_used, int) or turns_used < 0:
            raise ValueError("turns_used debe ser un entero >= 0.")
        updates.append("turns_used = ?")
        params.append(turns_used)

    if clear_pause:
        updates.append("quota_paused_until = NULL")
    elif quota_paused_until is not None:
        parsed = _parse_iso_datetime(quota_paused_until)
        if parsed is None:
            raise ValueError(f"quota_paused_until inválido: {quota_paused_until!r}")
        updates.append("quota_paused_until = ?")
        params.append(parsed.isoformat())

    conn: sqlite3.Connection | None = None
    try:
        conn = _open_connection(db_path)
        if updates:
            updates.append("updated_at = ?")
            params.append(datetime.now(UTC).isoformat())
            conn.execute(
                f"UPDATE quota_state SET {', '.join(updates)} WHERE id = 1;",
                tuple(params),
            )
            conn.commit()
        row = conn.execute(
            "SELECT window_started_at, window_tokens_used, estimated_window_capacity, "
            "quota_paused_until, reserve_threshold_pct, turns_used, updated_at "
            "FROM quota_state WHERE id = 1;"
        ).fetchone()
        if row is None:
            raise RuntimeError("No se encontró el registro singleton quota_state (id=1).")
        return _compute_quota_payload(row)
    finally:
        if conn is not None:
            conn.close()


def record_agy_usage(
    input_chars: int = 0,
    output_chars: int = 0,
    *,
    db_path: Path | str | None = None,
    now: datetime | None = None,
) -> dict[str, Any] | None:
    """Estimate and accumulate quota consumption from an agy invocation."""
    if _should_skip_default_db_write(db_path):
        return None
    ref_now = now if now is not None else datetime.now(UTC)
    if ref_now.tzinfo is None:
        ref_now = ref_now.replace(tzinfo=UTC)
    delta = max(0, int(input_chars)) + max(0, int(output_chars))
    if delta <= 0:
        delta = 1
    conn: sqlite3.Connection | None = None
    try:
        # Ensure expired pause/window is rolled over cleanly first
        get_quota_status(db_path=db_path, now=ref_now)
        conn = _open_connection(db_path)
        conn.execute(
            """
            UPDATE quota_state
            SET window_tokens_used = window_tokens_used + ?,
                turns_used = turns_used + 1,
                updated_at = ?
            WHERE id = 1;
            """,
            (delta, ref_now.isoformat()),
        )
        conn.commit()
        row = conn.execute(
            "SELECT window_started_at, window_tokens_used, estimated_window_capacity, "
            "quota_paused_until, reserve_threshold_pct, turns_used, updated_at "
            "FROM quota_state WHERE id = 1;"
        ).fetchone()
        assert row is not None
        return _compute_quota_payload(row, now=ref_now)
    finally:
        if conn is not None:
            conn.close()


def record_agy_429_pause(
    raw_output: str = "",
    *,
    reset_seconds: int | None = None,
    db_path: Path | str | None = None,
    now: datetime | None = None,
) -> dict[str, Any] | None:
    """Record a 429 pause timestamp and auto-calibrate estimated_window_capacity without tripping failures."""
    if _should_skip_default_db_write(db_path):
        return None
    ref_now = now if now is not None else datetime.now(UTC)
    if ref_now.tzinfo is None:
        ref_now = ref_now.replace(tzinfo=UTC)
    delay = (
        max(60, int(reset_seconds))
        if reset_seconds is not None
        else parse_quota_reset_delay_seconds(raw_output, default_seconds=1800)
    )
    paused_until = ref_now + timedelta(seconds=delay)
    conn: sqlite3.Connection | None = None
    try:
        conn = _open_connection(db_path)
        row = conn.execute(
            "SELECT window_tokens_used, estimated_window_capacity FROM quota_state WHERE id = 1;"
        ).fetchone()
        used = int(row["window_tokens_used"]) if row is not None else 0
        current_cap = int(row["estimated_window_capacity"]) if row is not None else DEFAULT_ESTIMATED_WINDOW_CAPACITY
        calibrated_cap = used if used >= 5000 else current_cap
        conn.execute(
            """
            UPDATE quota_state
            SET estimated_window_capacity = ?,
                window_tokens_used = ?,
                quota_paused_until = ?,
                updated_at = ?
            WHERE id = 1;
            """,
            (calibrated_cap, calibrated_cap, paused_until.isoformat(), ref_now.isoformat()),
        )
        conn.commit()
        updated_row = conn.execute(
            "SELECT window_started_at, window_tokens_used, estimated_window_capacity, "
            "quota_paused_until, reserve_threshold_pct, turns_used, updated_at "
            "FROM quota_state WHERE id = 1;"
        ).fetchone()
        assert updated_row is not None
        return _compute_quota_payload(updated_row, now=ref_now)
    finally:
        if conn is not None:
            conn.close()


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
        idea_generator: Callable[[], Sequence[Mapping[str, Any]]] | None = None,
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
        self._idea_generator = idea_generator
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
        self._building_task_title: str | None = None
        self._last_error_details: dict[str, Any] | None = None

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
        """Return persisted autonomous state combined with live worker, quota, and busy status."""
        state = get_autonomous_state(db_path=self.db_path)
        quota = get_quota_status(db_path=self.db_path)
        busy_reason = self.get_busy_reason()
        with self._lock:
            building_title = self._building_task_title
            last_error_details = dict(self._last_error_details) if self._last_error_details else None
            last_result = dict(self._last_cycle_result) if self._last_cycle_result else None

        pause_reason: str | None = None
        if quota["is_paused"]:
            hhmm = quota["resume_hhmm"] or "pronto"
            pause_reason = f"Sin cuota hasta las {hhmm}"
        elif quota["reserve_reached"]:
            pause_reason = (
                f"Cuota en reserva ({quota['quota_remaining_pct']}% <= "
                f"{quota['reserve_threshold_pct']}%)"
            )

        if not state["autonomous_enabled"]:
            live_state = "apagado"
        elif building_title:
            live_state = "construyendo"
        elif pause_reason is not None:
            live_state = "pausado_cuota"
        elif busy_reason is not None:
            live_state = "cediendo_paso"
        else:
            live_state = "activo"

        return {
            **state,
            "state": live_state,
            "current_task": building_title,
            "is_running": self.is_running,
            "busy_reason": busy_reason,
            "pause_reason": pause_reason,
            "last_error": last_error_details,
            "last_cycle_result": last_result,
            "quota": quota,
        }

    def get_status_snapshot(self, *, history_limit: int = 20) -> dict[str, Any]:
        """Return full status payload (engine state + quota telemetry + autonomous history)."""
        base = self.get_state()
        base["history"] = list_autonomous_history(limit=history_limit, db_path=self.db_path)
        return base

    def update_config(
        self,
        *,
        autonomous_enabled: bool | None = None,
        cooldown_minutes: int | None = None,
        consecutive_failures: int | None = None,
        reserve_threshold_pct: int | None = None,
    ) -> dict[str, Any]:
        """Update autonomous & quota configuration in SQLite and wake the daemon loop immediately."""
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
        if reserve_threshold_pct is not None:
            quota = set_quota_config(
                reserve_threshold_pct=reserve_threshold_pct,
                db_path=self.db_path,
            )
            updated["quota"] = quota
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
            if int(status.get("paused_count", 0)) > 0 or (
                status.get("last_error") and is_quota_429_error(str(status["last_error"]))
            ):
                return {
                    "ok": False,
                    "quota_429": True,
                    "error": str(
                        status.get("quota_paused_message")
                        or status.get("last_error")
                        or "RESOURCE_EXHAUSTED 429"
                    ),
                }
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

    def _mark_work_item_applied(self, work_item: dict[str, Any], *, commit_hash: str | None = None) -> None:
        if work_item.get("source") == "idea_engine":
            idea_id = _validate_idea_id(work_item.get("idea_id"))
            if idea_id is not None:
                finish_idea_execution(
                    idea_id,
                    status="implemented",
                    current_step="Integrando",
                    commit_sha=commit_hash,
                    db_path=self.db_path,
                )
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
        """Execute a single autonomous cycle enforcing quota reserve and all 3 safety brakes."""
        with self._cycle_lock:
            state = get_autonomous_state(db_path=self.db_path)
            if not state["autonomous_enabled"] or self._stop_event.is_set():
                return self._set_last_cycle_result(
                    {"status": "disabled", "executed": False, "promoted": False}
                )

            # Quota Brake: yield when 429 pause is active or remaining quota <= reserve threshold
            quota = get_quota_status(db_path=self.db_path)
            if quota["is_paused"] or quota["reserve_reached"]:
                pause_reason = (
                    f"Sin cuota hasta las {quota['resume_hhmm']}"
                    if quota["is_paused"] and quota["resume_hhmm"]
                    else (
                        f"Cuota en reserva ({quota['quota_remaining_pct']}% <= "
                        f"{quota['reserve_threshold_pct']}%)"
                    )
                )
                return self._set_last_cycle_result(
                    {
                        "status": "paused_quota",
                        "executed": False,
                        "promoted": False,
                        "pause_reason": pause_reason,
                        "quota": quota,
                    }
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
                queued_now = int(count_ideas_by_status(db_path=self.db_path).get("queued", 0))
                if queued_now >= MAX_AUTO_QUEUED_IDEAS:
                    return self._set_last_cycle_result(
                        {
                            "status": "queue_full",
                            "executed": False,
                            "promoted": False,
                            "queued_count": queued_now,
                            "max_queued": MAX_AUTO_QUEUED_IDEAS,
                        }
                    )
                # Generate fresh backlog ideas automatically if permitted and quota is healthy
                if self._idea_generator is not None:
                    try:
                        self._idea_generator()
                    except Exception as exc:  # noqa: BLE001
                        logger.warning("Autonomous idea_generator failed: %s", redact_secrets(str(exc)))
                    work_item = self._select_next_work_item()
                elif self._idea_selector is None:
                    try:
                        generate_new_ideas(
                            count=3,
                            source="auto",
                            db_path=self.db_path,
                            repo_root=self.repo_root,
                            runner=self._command_runner,
                        )
                    except Exception as exc:  # noqa: BLE001
                        logger.warning("Autonomous generate_new_ideas failed: %s", redact_secrets(str(exc)))
                    work_item = self._select_next_work_item()

            if work_item is None:
                post_quota = get_quota_status(db_path=self.db_path)
                if post_quota["is_paused"]:
                    return self._set_last_cycle_result(
                        {
                            "status": "paused_quota",
                            "executed": False,
                            "promoted": False,
                            "pause_reason": f"Sin cuota hasta las {post_quota['resume_hhmm'] or 'pronto'}",
                            "quota": post_quota,
                        }
                    )
                return self._set_last_cycle_result(
                    {"status": "idle_no_work", "executed": False, "promoted": False}
                )

            # Re-verify Brake 2 immediately before staging execution
            if not get_autonomous_state(db_path=self.db_path)["autonomous_enabled"] or self._stop_event.is_set():
                return self._set_last_cycle_result(
                    {"status": "disabled", "executed": False, "promoted": False}
                )

            # Build and verify inside isolated .liwi-staging worktree
            cycle_idea_id = (
                _validate_idea_id(work_item.get("idea_id"))
                if work_item.get("source") == "idea_engine"
                else None
            )
            if cycle_idea_id is not None:
                start_idea_execution(
                    cycle_idea_id,
                    current_step="Analizando",
                    db_path=self.db_path,
                )
            with self._lock:
                self._building_task_title = work_item["title"]
            try:
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
            finally:
                with self._lock:
                    self._building_task_title = None

            if not exec_ok:
                if is_quota_429_error(exec_err):
                    if cycle_idea_id is not None:
                        finish_idea_execution(
                            cycle_idea_id,
                            status="queued",
                            current_step="",
                            db_path=self.db_path,
                        )
                    err_info = format_agent_error(exec_err)
                    current_quota = get_quota_status(db_path=self.db_path)
                    if not current_quota["is_paused"]:
                        current_quota = (
                            record_agy_429_pause(exec_err, db_path=self.db_path) or current_quota
                        )
                    with self._lock:
                        self._last_error_details = err_info
                    return self._set_last_cycle_result(
                        {
                            "status": "paused_quota",
                            "executed": True,
                            "promoted": False,
                            "error": err_info["message"],
                            "error_details": err_info,
                            "quota": current_quota,
                            "work_item": work_item,
                        }
                    )
                if cycle_idea_id is not None:
                    finish_idea_execution(
                        cycle_idea_id,
                        status="failed",
                        current_step="",
                        failure_reason=exec_err,
                        db_path=self.db_path,
                    )
                err_info = format_agent_error(exec_err)
                with self._lock:
                    self._last_error_details = err_info
                return self._record_cycle_failure(exec_err, work_item)

            # Brake 2: immediate shutoff during build parks result in .liwi-staging without promoting
            mid_state = get_autonomous_state(db_path=self.db_path)
            if not mid_state["autonomous_enabled"] or self._stop_event.is_set():
                if cycle_idea_id is not None:
                    finish_idea_execution(
                        cycle_idea_id,
                        status="queued",
                        current_step="",
                        db_path=self.db_path,
                    )
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
            self._mark_work_item_applied(work_item, commit_hash=commit_hash)
            with self._lock:
                self._last_error_details = None

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
        last_watchdog_mono = 0.0
        while not self._stop_event.is_set():
            with self._lock:
                seen_gen = self._config_generation

            now_mono = time.monotonic()
            if now_mono - last_watchdog_mono >= 30.0:
                last_watchdog_mono = now_mono
                try:
                    reconcile_and_investigate_queue(
                        db_path=self.db_path,
                        repo_root=self.repo_root,
                        staging_manager=self._staging_manager,
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.debug("Autonomous queue watchdog error: %s", redact_secrets(str(exc)))

            state = get_autonomous_state(db_path=self.db_path)
            if not state["autonomous_enabled"]:
                with self._lock:
                    if self._stop_event.is_set():
                        break
                    if self._config_generation != seen_gen:
                        continue
                    self._wakeup_event.clear()
                self._wakeup_event.wait(timeout=min(self._idle_poll_seconds, 30.0))
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
            deadline_mono = time.monotonic() + wait_seconds
            while not self._stop_event.is_set():
                remaining = deadline_mono - time.monotonic()
                if remaining <= 0:
                    break
                with self._lock:
                    if self._config_generation != seen_gen:
                        break
                    self._wakeup_event.clear()
                self._wakeup_event.wait(timeout=min(remaining, 30.0))
                if time.monotonic() - last_watchdog_mono >= 30.0 and not self._stop_event.is_set():
                    last_watchdog_mono = time.monotonic()
                    try:
                        reconcile_and_investigate_queue(
                            db_path=self.db_path,
                            repo_root=self.repo_root,
                            staging_manager=self._staging_manager,
                        )
                    except Exception as exc:  # noqa: BLE001
                        logger.debug("Autonomous queue watchdog error: %s", redact_secrets(str(exc)))
                with self._lock:
                    if self._config_generation != seen_gen:
                        break

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


# ---------------------------------------------------------------------------
# Per-Project Auto-Improvement Ideas (project_ideas)
# ---------------------------------------------------------------------------


def _validate_project_slug(project_slug: str) -> str:
    clean = str(project_slug or "").strip()
    if not clean or not _PROJECT_SLUG_RE.fullmatch(clean):
        raise ValueError(f"project_slug inválido: {project_slug!r}")
    return clean


def _validate_project_idea_source(source: str) -> str:
    clean = str(source or "manual").strip().lower() or "manual"
    if clean not in PROJECT_IDEA_VALID_SOURCES:
        raise ValueError(
            f"source de project_idea inválido {source!r}; "
            f"se esperaba uno de {sorted(PROJECT_IDEA_VALID_SOURCES)}."
        )
    return clean


def _validate_project_idea_status(status: Any) -> str:
    if not isinstance(status, str):
        raise TypeError(
            f"status de project_idea inválido {status!r}; "
            f"se esperaba una cadena entre {sorted(IDEA_VALID_FILTERS)}."
        )
    clean = status.strip().lower()
    mapped = _STATUS_ALIAS_TO_DB.get(clean)
    if mapped is None or mapped not in PROJECT_IDEA_VALID_STATUSES:
        raise ValueError(
            f"status de project_idea inválido {status!r}; "
            f"se esperaba uno de {sorted(IDEA_VALID_FILTERS)}."
        )
    return mapped


def _row_to_project_idea(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": int(row["id"]),
        "created_at": str(row["created_at"]),
        "project_slug": str(row["project_slug"]),
        "title": str(row["title"]),
        "category": str(row["category"]),
        "impact": str(row["impact"]),
        "simple_description": str(row["simple_description"]),
        "change_prompt": str(row["change_prompt"]),
        "source": str(row["source"]),
        "status": str(row["status"]),
    }


def list_project_ideas(
    project_slug: str,
    *,
    status: str | None = None,
    limit: int = DEFAULT_LIST_LIMIT,
    db_path: Path | str | None = None,
    include_counts: bool = False,
) -> IdeaList | dict[str, Any]:
    """List improvement ideas for a specific generated project ordered newest first, with status counts."""
    clean_slug = _validate_project_slug(project_slug)
    if isinstance(limit, bool) or not isinstance(limit, int):
        bounded_limit = DEFAULT_LIST_LIMIT
    else:
        bounded_limit = max(1, min(limit, MAX_LIST_LIMIT))

    if status is None:
        clean_status: str | None = None
        filter_label = "all"
    else:
        clean_status, filter_label = _validate_status_filter(status)

    conn: sqlite3.Connection | None = None
    try:
        conn = _open_connection(db_path)
        counts = _query_project_idea_status_counts(conn, clean_slug)
        if clean_status is not None:
            rows = conn.execute(
                "SELECT id, created_at, project_slug, title, category, impact, "
                "simple_description, change_prompt, source, status "
                "FROM project_ideas WHERE project_slug = ? AND status = ? "
                "ORDER BY id DESC LIMIT ?;",
                (clean_slug, clean_status, bounded_limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT id, created_at, project_slug, title, category, impact, "
                "simple_description, change_prompt, source, status "
                "FROM project_ideas WHERE project_slug = ? "
                "ORDER BY id DESC LIMIT ?;",
                (clean_slug, bounded_limit),
            ).fetchall()
        items = [_row_to_project_idea(row) for row in rows]
        if include_counts:
            return {
                "ideas": items,
                "counts": counts,
                "status": filter_label,
            }
        return IdeaList(items, counts=counts, status=filter_label)
    finally:
        if conn is not None:
            conn.close()


def get_project_idea(
    idea_id: int,
    *,
    db_path: Path | str | None = None,
) -> dict[str, Any] | None:
    """Return a single project_idea row by id, or None."""
    validated_id = _validate_idea_id(idea_id)
    if validated_id is None:
        raise ValueError(f"idea_id inválido: {idea_id!r}")
    conn: sqlite3.Connection | None = None
    try:
        conn = _open_connection(db_path)
        row = conn.execute(
            "SELECT id, created_at, project_slug, title, category, impact, "
            "simple_description, change_prompt, source, status "
            "FROM project_ideas WHERE id = ?;",
            (validated_id,),
        ).fetchone()
        return _row_to_project_idea(row) if row is not None else None
    finally:
        if conn is not None:
            conn.close()


def update_project_idea_status(
    idea_id: int,
    status: str,
    *,
    project_slug: str | None = None,
    db_path: Path | str | None = None,
) -> dict[str, Any]:
    """Update status of a project idea, verifying ownership when project_slug is provided."""
    validated_id = _validate_idea_id(idea_id)
    if validated_id is None:
        raise ValueError(f"idea_id inválido: {idea_id!r}")
    clean_status = _validate_project_idea_status(status)
    clean_slug = _validate_project_slug(project_slug) if project_slug is not None else None

    conn: sqlite3.Connection | None = None
    try:
        conn = _open_connection(db_path)
        if clean_slug is not None:
            cur = conn.execute(
                "UPDATE project_ideas SET status = ? WHERE id = ? AND project_slug = ?;",
                (clean_status, validated_id, clean_slug),
            )
        else:
            cur = conn.execute(
                "UPDATE project_ideas SET status = ? WHERE id = ?;",
                (clean_status, validated_id),
            )
        conn.commit()
        if cur.rowcount <= 0:
            raise KeyError(f"Idea de proyecto {validated_id} no encontrada.")
        row = conn.execute(
            "SELECT id, created_at, project_slug, title, category, impact, "
            "simple_description, change_prompt, source, status "
            "FROM project_ideas WHERE id = ?;",
            (validated_id,),
        ).fetchone()
        assert row is not None
        return _row_to_project_idea(row)
    finally:
        if conn is not None:
            conn.close()


def create_project_error_idea(
    project_slug: str,
    error_text: str,
    *,
    title: str | None = None,
    db_path: Path | str | None = None,
) -> dict[str, Any] | None:
    """Persist an actionable runtime_error idea for a generated project (deduplicating open identical issues)."""
    clean_slug = _validate_project_slug(project_slug)
    clean_error = redact_secrets(str(error_text or "").strip())
    if not clean_error:
        return None
    if _should_skip_default_db_write(db_path):
        return None

    short_summary = clean_error.splitlines()[0].strip()[:140]
    eff_title = redact_secrets(str(title or f"🩹 Corregir error al iniciar: {short_summary[:60]}").strip())[:140]
    simple_desc = f"Se detectó un fallo en tiempo de ejecución: {short_summary}"
    change_prompt = (
        f"Diagnostica y corrige el siguiente error ocurrido al ejecutar el proyecto '{clean_slug}'.\n"
        f"Error reportado:\n{clean_error[:1200]}\n\n"
        "Asegura una prueba automatizada que cubra el fallo antes de terminar."
    )

    conn: sqlite3.Connection | None = None
    try:
        conn = _open_connection(db_path)
        existing = conn.execute(
            "SELECT id, created_at, project_slug, title, category, impact, "
            "simple_description, change_prompt, source, status "
            "FROM project_ideas "
            "WHERE project_slug = ? AND source = 'runtime_error' AND status IN ('nueva', 'implementando') "
            "ORDER BY id DESC LIMIT 10;",
            (clean_slug,),
        ).fetchall()
        for row in existing:
            if normalize_idea_title(str(row["title"])) == normalize_idea_title(eff_title) or (
                short_summary[:60].lower() in str(row["simple_description"]).lower()
            ):
                return _row_to_project_idea(row)

        now_iso = datetime.now(UTC).isoformat()
        cur = conn.execute(
            """
            INSERT INTO project_ideas (
                created_at, project_slug, title, category, impact,
                simple_description, change_prompt, source, status
            ) VALUES (?, ?, ?, 'Estabilidad', 'Alto', ?, ?, 'runtime_error', 'nueva');
            """,
            (now_iso, clean_slug, eff_title, simple_desc, change_prompt),
        )
        conn.commit()
        row = conn.execute(
            "SELECT id, created_at, project_slug, title, category, impact, "
            "simple_description, change_prompt, source, status "
            "FROM project_ideas WHERE id = ?;",
            (int(cur.lastrowid),),
        ).fetchone()
        assert row is not None
        return _row_to_project_idea(row)
    finally:
        if conn is not None:
            conn.close()


def save_project_ideas(
    project_slug: str,
    ideas: Sequence[Mapping[str, Any]],
    *,
    source: str = "manual",
    db_path: Path | str | None = None,
    max_items: int = MAX_IDEATION_COUNT,
) -> list[dict[str, Any]]:
    """Persist validated, deduplicated per-project ideas to project_ideas."""
    clean_slug = _validate_project_slug(project_slug)
    eff_source = _validate_project_idea_source(source)
    conn: sqlite3.Connection | None = None
    inserted: list[dict[str, Any]] = []
    try:
        conn = _open_connection(db_path)
        existing_rows = conn.execute(
            "SELECT title FROM project_ideas WHERE project_slug = ?;",
            (clean_slug,),
        ).fetchall()
        seen_keys = {normalize_idea_title(str(r["title"])) for r in existing_rows}

        for item in ideas:
            if len(inserted) >= max_items:
                break
            title = redact_secrets(str(item.get("title") or "").strip())
            simple_desc = redact_secrets(str(item.get("simple_description") or "").strip())
            change_prompt = redact_secrets(
                str(item.get("change_prompt") or item.get("technical_prompt") or "").strip()
            )
            if not title or not simple_desc or not change_prompt:
                continue
            key = normalize_idea_title(title)
            if not key or key in seen_keys:
                continue
            seen_keys.add(key)

            category = redact_secrets(str(item.get("category") or "UX").strip()) or "UX"
            raw_impact = str(item.get("impact") or "Medio").strip().lower()
            impact = "Alto" if raw_impact in _HIGH_IMPACT_ALIASES else "Medio"
            now_iso = datetime.now(UTC).isoformat()

            cur = conn.execute(
                """
                INSERT INTO project_ideas (
                    created_at, project_slug, title, category, impact,
                    simple_description, change_prompt, source, status
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'nueva');
                """,
                (
                    now_iso,
                    clean_slug,
                    title[:140],
                    category[:40],
                    impact,
                    simple_desc[:400],
                    change_prompt[:2500],
                    eff_source,
                ),
            )
            row = conn.execute(
                "SELECT id, created_at, project_slug, title, category, impact, "
                "simple_description, change_prompt, source, status "
                "FROM project_ideas WHERE id = ?;",
                (int(cur.lastrowid),),
            ).fetchone()
            if row is not None:
                inserted.append(_row_to_project_idea(row))
        conn.commit()
        return inserted
    finally:
        if conn is not None:
            conn.close()


def _summarize_project_workspace(workspace_dir: Path) -> str:
    """Collect a compact, read-only summary of files inside a generated project's workspace."""
    root = Path(workspace_dir).resolve()
    if not root.is_dir():
        return "(Workspace no encontrado)"
    ignored = {".git", ".venv", "venv", "node_modules", "__pycache__", ".pytest_cache", "dist", "build"}
    entries: list[str] = []
    for path in sorted(root.rglob("*")):
        if len(entries) >= 80:
            break
        parts = path.relative_to(root).parts
        if any(p in ignored or p.startswith(".") for p in parts[:-1]):
            continue
        if path.is_file():
            rel = "/".join(parts)
            entries.append(rel)
    readme_excerpt = ""
    for name in ("README.md", "package.json", "pyproject.toml"):
        candidate = root / name
        if candidate.is_file():
            try:
                text = redact_secrets(candidate.read_text(encoding="utf-8", errors="ignore"))[:800]
                readme_excerpt += f"\n--- {name} ---\n{text}"
            except OSError:
                pass
    listing = "\n".join(f"- {e}" for e in entries) or "(Sin archivos relevantes)"
    return f"Archivos del proyecto:\n{listing}{readme_excerpt}"


def _list_all_historical_project_ideas(
    project_slug: str,
    *,
    db_path: Path | str | None = None,
) -> list[dict[str, Any]]:
    """Return all project_ideas records for project_slug across every status."""
    clean_slug = _validate_project_slug(project_slug)
    conn: sqlite3.Connection | None = None
    try:
        conn = _open_connection(db_path)
        rows = conn.execute(
            "SELECT id, created_at, project_slug, title, category, impact, "
            "simple_description, change_prompt, source, status "
            "FROM project_ideas WHERE project_slug = ? ORDER BY id DESC;",
            (clean_slug,),
        ).fetchall()
        return [_row_to_project_idea(row) for row in rows]
    finally:
        if conn is not None:
            conn.close()


def _build_project_ideation_prompt(
    project_slug: str,
    summary: str,
    count: int,
    *,
    historical_ideas: Sequence[Mapping[str, Any]] = (),
    recent_commits: Sequence[str] = (),
    excluded_topics: Sequence[str] = (),
    accepted_titles: Sequence[str] = (),
) -> str:
    commits_lines = (
        [f"  - {redact_secrets(c)}" for c in recent_commits]
        if recent_commits
        else ["  - (sin historial git disponible)"]
    )
    history_lines = _format_negative_history_lines(historical_ideas) or [
        "  - (ninguna idea previa registrada)"
    ]

    extra_block = ""
    if excluded_topics or accepted_titles:
        extra_lines: list[str] = [
            "\nRONDA COMPLEMENTARIA (compleción de cupo — propone temas 100% NUEVOS y distintos):"
        ]
        if accepted_titles:
            extra_lines.append("Ideas ya aceptadas en esta corrida (NO repetir ni parafrasear):")
            extra_lines.extend(f"  - {redact_secrets(t)}" for t in accepted_titles)
        if excluded_topics:
            extra_lines.append(
                "Temas RECHAZADOS por duplicidad con el historial o commits (STRICTAMENTE EXCLUIDOS):"
            )
            extra_lines.extend(f"  - {redact_secrets(topic)}" for topic in excluded_topics)
        extra_block = "\n".join(extra_lines) + "\n"

    return (
        f"Eres un Arquitecto de Producto revisando el proyecto '{project_slug}' de Agent Factory.\n"
        "REGLAS: MODO SOLO LECTURA. PROHIBIDO modificar archivos o leer .env/secretos.\n"
        f"Devuelve ÚNICAMENTE un arreglo JSON válido con exactamente {count} objetos.\n"
        "Cada objeto debe tener exactamente: title (español con emoji), category "
        "(Rendimiento|Seguridad|UX|Estabilidad|Producto), impact (Alto|Medio), "
        "simple_description (máx 2 frases en español simple) y "
        "change_prompt (instrucción concreta y realizable para implementar en el workspace).\n"
        "PROHIBIDO proponer mejoras ya cubiertas en commits recientes, pendientes, encoladas, "
        "en implementación, aplicadas/implementadas o descartadas, ni equivalentes aunque cambie la redacción.\n\n"
        f"Contexto del proyecto '{project_slug}':\n{summary}\n\n"
        "Últimos commits ya implementados en el proyecto (PROHIBIDO repetir):\n"
        + "\n".join(commits_lines)
        + "\n\nHistorial completo de ideas previas del proyecto en CUALQUIER estado "
        "(pending, queued, implementing, implemented/applied, dismissed) — PROHIBIDO repetir o parafrasear:\n"
        + "\n".join(history_lines)
        + f"\n{extra_block}"
    )


def generate_project_ideas(
    project_slug: str,
    workspace_dir: Path | str,
    *,
    count: int = DEFAULT_IDEATION_COUNT,
    source: str = "manual",
    db_path: Path | str | None = None,
    runner: Runner | None = None,
    git_runner: Runner | None = None,
    agy_command: Sequence[str] | None = None,
    model: str | None = None,
    timeout_seconds: float = DEFAULT_IDEATION_TIMEOUT_SECONDS,
) -> list[dict[str, Any]]:
    """Inspect a generated project's workspace in read-only mode via agy.EXE, filter duplicates, and store ideas."""
    clean_slug = _validate_project_slug(project_slug)
    eff_source = _validate_project_idea_source(source)
    if isinstance(count, bool) or not isinstance(count, int) or count <= 0:
        raise ValueError("count debe ser un entero positivo.")
    bounded_count = min(count, MAX_IDEATION_COUNT)

    ws_path = Path(workspace_dir).resolve()
    summary = _summarize_project_workspace(ws_path)
    historical_ideas = _list_all_historical_project_ideas(clean_slug, db_path=db_path)
    recent_commits = _call_get_recent_commits(
        ws_path,
        limit=DEFAULT_RECENT_COMMITS_LIMIT,
        runner=git_runner,
    )
    prompt = _build_project_ideation_prompt(
        clean_slug,
        summary,
        bounded_count,
        historical_ideas=historical_ideas,
        recent_commits=recent_commits,
    )

    active_runner: Any = (
        runner if runner is not None else SubprocessRunner(default_timeout_seconds=timeout_seconds)
    )
    base_cmd, err_reason = _resolve_agy_readonly_command(agy_command, active_runner)
    if base_cmd is None:
        logger.warning("Project ideation skipped for %s: %s", clean_slug, err_reason)
        return []

    ok, parsed, hit_429 = _invoke_readonly_agy(
        prompt,
        base_cmd=base_cmd,
        active_runner=active_runner,
        timeout_seconds=timeout_seconds,
        model=model,
        tmp_prefix="af_proj_idea_",
        cwd_prefix="af_proj_radar_",
        db_path=db_path,
        log_label=f"Project ideation ({clean_slug})",
        best_effort=False,
    )
    if not ok or hit_429 or not parsed:
        return []

    accepted, rejected = _filter_unique_candidates(
        parsed,
        historical_ideas,
        recent_commits,
        max_items=bounded_count,
    )

    if len(accepted) < bounded_count and rejected:
        missing = bounded_count - len(accepted)
        complementary_prompt = _build_project_ideation_prompt(
            clean_slug,
            summary,
            missing,
            historical_ideas=historical_ideas,
            recent_commits=recent_commits,
            excluded_topics=[f"{title} ({reason})" for title, reason in rejected],
            accepted_titles=[item["title"] for item in accepted],
        )
        ok_retry, parsed_retry, _retry_429 = _invoke_readonly_agy(
            complementary_prompt,
            base_cmd=base_cmd,
            active_runner=active_runner,
            timeout_seconds=timeout_seconds,
            model=model,
            tmp_prefix="af_proj_idea_",
            cwd_prefix="af_proj_radar_",
            db_path=db_path,
            log_label=f"Project ideation complementary round ({clean_slug})",
            best_effort=True,
        )
        if ok_retry and parsed_retry:
            extra, _extra_rejected = _filter_unique_candidates(
                parsed_retry,
                historical_ideas,
                recent_commits,
                already_accepted=accepted,
                max_items=missing,
            )
            accepted.extend(extra)

    if not accepted:
        return []

    return save_project_ideas(
        clean_slug,
        accepted,
        source=eff_source,
        db_path=db_path,
        max_items=bounded_count,
    )

