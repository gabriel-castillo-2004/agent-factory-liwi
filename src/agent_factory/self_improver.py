"""Self-improvement Parent Assistant (Liwi) with SQLite memory, safe apply gate, git rollback, and restart signaling (INV-03, INV-18, INV-19, INV-22)."""

from __future__ import annotations

import base64
import binascii
import json
import logging
import os
import re
import sqlite3
import sys
import tempfile
import threading
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any

from agent_factory.adapters.agy_cli_adapter import build_sanitized_cli_env
from agent_factory.adapters.github_vcs_adapter import build_git_push_env
from agent_factory.adapters.subprocess_runner import SubprocessRunner, resolve_executable
from agent_factory.config import build_workspace_pytest_env
from agent_factory.ports.clock import Clock
from agent_factory.ports.runner import CommandTimeoutError, Runner
from agent_factory.recovery_agent import (
    _parse_json_dict_loose,
    compact_pytest_failure_output,
    extract_failed_pytest_nodes,
)
from agent_factory.secrets import redact_secrets
from agent_factory.supervisor import RESTART_EXIT_CODE
from agent_factory.telemetry import (
    TELEMETRY_DB_ENV_VAR,
    record_safe_event,
    record_safe_incident,
)

if TYPE_CHECKING:
    from agent_factory.adapters.sqlite_store import SqliteEventStore

DEFAULT_SELF_IMPROVE_CONSULT_TIMEOUT_SECONDS: float = 180.0
DEFAULT_SELF_IMPROVE_APPLY_TIMEOUT_SECONDS: float = 900.0
DEFAULT_SELF_IMPROVE_REPAIR_TIMEOUT_SECONDS: float = 300.0
DEFAULT_SELF_IMPROVE_VERIFY_TIMEOUT_SECONDS: float = 900.0
FAST_DASHBOARD_PYTEST_K: str = "autonomous or ideas or radar or four_tabs or staging"
DEFAULT_SELF_IMPROVE_TIMEOUT_SECONDS: float = DEFAULT_SELF_IMPROVE_APPLY_TIMEOUT_SECONDS
DEFAULT_RESTART_DELAY_SECONDS: float = 1.0
MAX_UPLOAD_IMAGE_BYTES: int = 5 * 1024 * 1024
MAX_UPLOAD_IMAGES_PER_MESSAGE: int = 4
MAX_AUTO_QUEUED_IDEAS: int = 3
DEFAULT_IDEA_STALE_SECONDS: int = 360
MAX_IDEA_RETRIES: int = 2
_MAX_INLINE_PROMPT_CHARS: int = 6000
_QUEUE_WORKER_LOCK = threading.RLock()
_QUEUE_WORKER: IdeaQueueWorker | None = None
_ACTIVE_APPLY_COUNT: int = 0
_ACTIVE_IDEA_ID: int | None = None
_ACTIVE_CURRENT_STEP: str = ""
_SAFE_RULE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
_SAFE_UPLOAD_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,95}$")
_SELF_IMPROVE_PREFIX_RE = re.compile(r"^chore\(self-improve\)\s*:\s*", flags=re.IGNORECASE)
_SHORTSTAT_FILES_RE = re.compile(r"(\d+)\s+files?\s+changed", flags=re.IGNORECASE)
_FORBIDDEN_SELF_IMPROVE_PREFIXES: tuple[str, ...] = (
    ".env",
    "MASTER_PROMPT.md",
    "AGENTS.md",
    "config/models.toml",
    "data",
    "workspaces",
)


class _WallClock:
    def now(self) -> datetime:
        return datetime.now(UTC)

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)


def init_factory_self_improver_tables(
    conn: sqlite3.Connection,
    lock: threading.RLock | None = None,
) -> None:
    """Create factory_chat_messages and factory_memory_rules tables idempotently."""

    def _run() -> None:
        with conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS factory_chat_messages (
                    id TEXT PRIMARY KEY,
                    role TEXT NOT NULL,
                    content TEXT NOT NULL,
                    image_paths_json TEXT NOT NULL,
                    proposal_title TEXT,
                    proposal_prompt TEXT,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS factory_memory_rules (
                    id TEXT PRIMARY KEY,
                    rule_text TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
                """
            )

    if lock is not None:
        with lock:
            _run()
    else:
        _run()


class FactoryMemoryStore:
    """Thread-safe SQLite repository for Liwi self-improvement chat history and preference rules."""

    def __init__(
        self,
        *,
        sqlite_store: SqliteEventStore | None = None,
        db_path: str | Path = ":memory:",
        clock: Clock | None = None,
    ) -> None:
        self.clock: Clock = clock if clock is not None else _WallClock()
        if sqlite_store is not None:
            self._owns_conn = False
            self._conn = sqlite_store._conn
            self._lock = sqlite_store._lock
        else:
            self._owns_conn = True
            target = str(db_path)
            if target != ":memory:":
                Path(target).parent.mkdir(parents=True, exist_ok=True)
            self._lock = threading.RLock()
            self._conn = sqlite3.connect(target, check_same_thread=False)
        init_factory_self_improver_tables(self._conn, self._lock)

    @staticmethod
    def _row_to_message(row: tuple[Any, ...]) -> dict[str, Any]:
        raw_paths = str(row[3] or "[]")
        try:
            paths = json.loads(raw_paths)
            if not isinstance(paths, list):
                paths = []
        except json.JSONDecodeError:
            paths = []
        filenames = [Path(str(p)).name for p in paths if str(p).strip()]
        return {
            "id": str(row[0]),
            "role": str(row[1]),
            "content": str(row[2]),
            "image_paths": [str(p) for p in paths],
            "image_urls": [f"/api/factory/assistant/uploads/{fn}" for fn in filenames],
            "proposal_title": str(row[4]) if row[4] is not None else None,
            "proposal_prompt": str(row[5]) if row[5] is not None else None,
            "status": str(row[6]),
            "created_at": str(row[7]),
        }

    def list_messages(self, *, limit: int = 80) -> list[dict[str, Any]]:
        """Return chat messages ordered oldest-first (capped to latest `limit`)."""
        cap = max(1, min(500, int(limit)))
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, role, content, image_paths_json, proposal_title, proposal_prompt, status, created_at "
                "FROM factory_chat_messages ORDER BY rowid DESC LIMIT ?",
                (cap,),
            ).fetchall()
        rows.reverse()
        return [self._row_to_message(r) for r in rows]

    def add_message(
        self,
        *,
        role: str,
        content: str,
        image_paths: Sequence[str] = (),
        proposal_title: str | None = None,
        proposal_prompt: str | None = None,
        status: str = "ok",
    ) -> dict[str, Any]:
        """Insert and return a persisted factory assistant chat message."""
        msg_id = f"fmsg-{uuid.uuid4().hex[:12]}"
        created_at = self.clock.now().isoformat()
        paths_json = json.dumps([str(p) for p in image_paths])
        with self._lock, self._conn:
            self._conn.execute(
                """
                INSERT INTO factory_chat_messages (
                    id, role, content, image_paths_json, proposal_title,
                    proposal_prompt, status, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    msg_id,
                    role,
                    content,
                    paths_json,
                    proposal_title,
                    proposal_prompt,
                    status,
                    created_at,
                ),
            )
            row = self._conn.execute(
                "SELECT id, role, content, image_paths_json, proposal_title, proposal_prompt, status, created_at "
                "FROM factory_chat_messages WHERE id = ?",
                (msg_id,),
            ).fetchone()
        assert row is not None
        return self._row_to_message(row)

    def update_message_status(self, message_id: str, status: str) -> None:
        """Update proposal status for message_id when present."""
        clean_id = str(message_id or "").strip()
        if not clean_id:
            return
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE factory_chat_messages SET status = ? WHERE id = ?",
                (status, clean_id),
            )

    def list_rules(self) -> list[dict[str, Any]]:
        """Return all active persistent memory rules ordered oldest-first."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, rule_text, created_at FROM factory_memory_rules ORDER BY rowid ASC"
            ).fetchall()
        return [
            {"id": str(r[0]), "rule_text": str(r[1]), "created_at": str(r[2])}
            for r in rows
        ]

    def add_rule(self, rule_text: str) -> dict[str, Any] | None:
        """Persist a new preference/memory rule unless an identical rule already exists."""
        clean = " ".join(str(rule_text or "").split()).strip()
        if not clean:
            return None
        norm = clean.lower()
        with self._lock:
            existing = self._conn.execute(
                "SELECT id, rule_text, created_at FROM factory_memory_rules"
            ).fetchall()
            for r in existing:
                if " ".join(str(r[1]).split()).strip().lower() == norm:
                    return None
            rule_id = f"frule-{uuid.uuid4().hex[:12]}"
            created_at = self.clock.now().isoformat()
            with self._conn:
                self._conn.execute(
                    "INSERT INTO factory_memory_rules (id, rule_text, created_at) VALUES (?, ?, ?)",
                    (rule_id, clean[:500], created_at),
                )
        return {"id": rule_id, "rule_text": clean[:500], "created_at": created_at}

    def delete_rule(self, rule_id: str) -> bool:
        """Validate rule_id (INV-22) and delete rule from factory_memory_rules."""
        clean = str(rule_id or "").strip()
        if not _SAFE_RULE_ID_RE.fullmatch(clean):
            raise ValueError(f"Invalid memory rule id '{rule_id}'")
        with self._lock, self._conn:
            cur = self._conn.execute(
                "DELETE FROM factory_memory_rules WHERE id = ?",
                (clean,),
            )
            return cur.rowcount > 0


def _detect_image_ext_and_mime(data: bytes) -> tuple[str, str] | None:
    """Identify safe raster screenshot format strictly via magic bytes."""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png", "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return ".jpg", "image/jpeg"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return ".gif", "image/gif"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp", "image/webp"
    return None


def _list_factory_source_files(repo_root: Path) -> list[str]:
    """Return sorted relative POSIX paths under src/agent_factory/ avoiding secrets/caches."""
    src_pkg = (repo_root / "src" / "agent_factory").resolve()
    if not src_pkg.is_dir():
        return []
    files: list[str] = []
    for item in sorted(src_pkg.rglob("*")):
        if item.is_symlink() or not item.is_file():
            continue
        if any(p in {"__pycache__", ".pytest_cache", ".git", ".venv"} for p in item.parts):
            continue
        if item.name.startswith(".env"):
            continue
        try:
            rel = item.resolve().relative_to(repo_root.resolve()).as_posix()
        except (OSError, ValueError):
            continue
        files.append(rel)
        if len(files) >= 200:
            break
    return files


def _normalize_repo_rel_path(raw: str) -> str:
    """Normalize a repo-relative POSIX path and reject absolute paths or '..' traversal (INV-22)."""
    norm = str(raw or "").strip().replace("\\", "/")
    while norm.startswith("./"):
        norm = norm[2:]
    if not norm or norm.startswith("/") or re.match(r"^[A-Za-z]:", norm):
        return ""
    parts = [part for part in norm.split("/") if part and part != "."]
    if not parts or any(part == ".." for part in parts):
        return ""
    return "/".join(parts)


def _list_available_test_files(repo_root: Path) -> list[str]:
    """Return sorted relative POSIX paths for test_*.py files under tests/."""
    tests_dir = (repo_root / "tests").resolve()
    if not tests_dir.is_dir():
        return []
    repo_resolved = repo_root.resolve()
    files: list[str] = []
    for item in sorted(tests_dir.rglob("test_*.py")):
        if item.is_symlink() or not item.is_file():
            continue
        if any(p in {"__pycache__", ".pytest_cache", ".git", ".venv"} for p in item.parts):
            continue
        try:
            rel = item.resolve().relative_to(repo_resolved).as_posix()
        except (OSError, ValueError):
            continue
        files.append(rel)
    return files


def select_targeted_test_paths(
    changed_files: Sequence[str],
    available_tests: Sequence[str],
) -> list[str]:
    """Pure, deterministic mapper from modified src/tests paths to existing targeted test files."""
    norm_available: list[tuple[str, str, str]] = []
    available_set: set[str] = set()
    for raw_test in available_tests:
        norm_test = _normalize_repo_rel_path(raw_test)
        if not norm_test or norm_test in available_set:
            continue
        test_path = PurePosixPath(norm_test)
        if (
            not norm_test.startswith("tests/")
            or test_path.suffix != ".py"
            or not test_path.name.startswith("test_")
        ):
            continue
        available_set.add(norm_test)
        test_stem = test_path.stem
        test_suffix = test_stem.removeprefix("test_")
        norm_available.append((norm_test, test_stem, test_suffix))

    if not norm_available or not changed_files:
        return []

    selected: set[str] = set()
    for raw_changed in changed_files:
        norm = _normalize_repo_rel_path(raw_changed)
        if not norm:
            continue
        path = PurePosixPath(norm)
        if (
            norm.startswith("tests/")
            and path.suffix == ".py"
            and path.name.startswith("test_")
        ):
            if norm in available_set:
                selected.add(norm)
            continue
        if not norm.startswith("src/agent_factory/") or path.suffix != ".py":
            continue
        stem = path.stem
        if not stem or stem in {"__init__", "__main__"}:
            continue
        subpkg_parts = path.parts[2:-1]
        qualified_stem = "_".join((*subpkg_parts, stem)) if subpkg_parts else ""
        token_pattern = re.compile(rf"(?:^|_){re.escape(stem)}(?:$|_)")
        for test_rel, test_stem, test_suffix in norm_available:
            if (
                test_stem == f"test_{stem}"
                or (qualified_stem and test_stem == f"test_{qualified_stem}")
                or token_pattern.search(test_suffix) is not None
            ):
                selected.add(test_rel)

    return sorted(selected)


class FactorySelfImprover:
    """Orchestrates Liwi parent consult chat, SQLite memory, safe apply+test gate, rollback, and exit 42 restart."""

    def __init__(
        self,
        *,
        sqlite_store: SqliteEventStore | None = None,
        db_path: str | Path = ":memory:",
        repo_root: Path | None = None,
        uploads_dir: Path | None = None,
        runner: Runner | None = None,
        clock: Clock | None = None,
        agy_command: Sequence[str] = ("agy",),
        model: str | None = None,
        secrets_to_redact: Sequence[str] = (),
        consult_timeout_seconds: float = DEFAULT_SELF_IMPROVE_CONSULT_TIMEOUT_SECONDS,
        apply_timeout_seconds: float = DEFAULT_SELF_IMPROVE_APPLY_TIMEOUT_SECONDS,
        verify_timeout_seconds: float = DEFAULT_SELF_IMPROVE_VERIFY_TIMEOUT_SECONDS,
        restart_fn: Callable[[int], Any] | None = None,
        restart_delay_seconds: float = DEFAULT_RESTART_DELAY_SECONDS,
        pre_exit_hook: Callable[[], Any] | None = None,
        telemetry_db_path: Path | str | None = None,
    ) -> None:
        self.clock: Clock = clock if clock is not None else _WallClock()
        self._raw_db_path = (
            getattr(sqlite_store, "db_path", db_path) if sqlite_store is not None else db_path
        )
        self._telemetry_db_path: Path | None = (
            Path(telemetry_db_path) if telemetry_db_path is not None else None
        )
        self.memory_store = FactoryMemoryStore(
            sqlite_store=sqlite_store,
            db_path=db_path,
            clock=self.clock,
        )
        self.repo_root = (
            Path(repo_root).resolve()
            if repo_root is not None
            else Path(__file__).resolve().parents[2]
        )
        if uploads_dir is not None:
            self.uploads_dir = Path(uploads_dir).resolve()
        else:
            raw_db = getattr(sqlite_store, "db_path", db_path) if sqlite_store is not None else db_path
            if raw_db and str(raw_db) != ":memory:":
                db_parent = Path(str(raw_db)).resolve().parent
                repo_data_dir = Path("data").resolve()
                self.uploads_dir = (
                    (repo_data_dir / "factory_chat_uploads").resolve()
                    if db_parent == repo_data_dir
                    else (db_parent / "factory_chat_uploads").resolve()
                )
            else:
                self.uploads_dir = Path("data/factory_chat_uploads").resolve()
        self.runner: Runner = (
            runner
            if runner is not None
            else SubprocessRunner(default_timeout_seconds=consult_timeout_seconds)
        )
        self.agy_command: tuple[str, ...] = tuple(agy_command) if agy_command else ("agy",)
        self.model = model.strip() if isinstance(model, str) and model.strip() else None
        self.secrets_to_redact: tuple[str, ...] = tuple(secrets_to_redact)
        self.consult_timeout_seconds = float(consult_timeout_seconds)
        self.apply_timeout_seconds = float(apply_timeout_seconds)
        self.verify_timeout_seconds = float(verify_timeout_seconds)
        self.restart_fn: Callable[[int], Any] = restart_fn if restart_fn is not None else os._exit
        self.restart_delay_seconds = float(restart_delay_seconds)
        self.pre_exit_hook = pre_exit_hook
        self._apply_lock = threading.Lock()
        self._status_lock = threading.Lock()
        self._active_idea_id: int | None = None
        self._idea_queue_worker: IdeaQueueWorker | None = None
        self._status: dict[str, Any] = {
            "running": False,
            "step": 0,
            "total_steps": 3,
            "stage": "",
            "detail": "",
            "title": "",
            "started_at": None,
            "last_outcome": "",
        }

    def _redact(self, text: str, extra_secrets: Sequence[str] = ()) -> str:
        return redact_secrets(text, extra_secrets=(*self.secrets_to_redact, *extra_secrets))

    def _resolve_telemetry_db_path(self) -> Path:
        """Resolve telemetry SQLite path respecting explicit override, env var, and test isolation."""
        if self._telemetry_db_path is not None:
            return self._telemetry_db_path
        env_val = os.environ.get(TELEMETRY_DB_ENV_VAR, "").strip()
        if env_val:
            return Path(env_val)
        raw_db = self._raw_db_path
        if raw_db and str(raw_db) != ":memory:":
            db_parent = Path(str(raw_db)).resolve().parent
            repo_data_dir = Path("data").resolve()
            if db_parent == repo_data_dir:
                return (repo_data_dir / "telemetry.db").resolve()
            return (db_parent / "telemetry.db").resolve()
        return Path(":memory:")

    def _save_uploaded_images(self, images: Sequence[Mapping[str, Any]]) -> list[str]:
        if not images:
            return []
        if len(images) > MAX_UPLOAD_IMAGES_PER_MESSAGE:
            raise ValueError(f"Máximo {MAX_UPLOAD_IMAGES_PER_MESSAGE} capturas por mensaje.")
        self.uploads_dir.mkdir(parents=True, exist_ok=True)
        base_resolved = self.uploads_dir.resolve()
        saved_paths: list[str] = []

        for idx, item in enumerate(images, start=1):
            if not isinstance(item, Mapping):
                raise TypeError("Cada elemento de 'images' debe ser un objeto.")
            raw_b64 = item.get("data_base64")
            if not isinstance(raw_b64, str) or not raw_b64.strip():
                raise ValueError("Cada captura adjunta requiere 'data_base64'.")
            b64_clean = raw_b64.strip()
            if "base64," in b64_clean[:64]:
                b64_clean = b64_clean.split("base64,", 1)[1].strip()
            try:
                raw_bytes = base64.b64decode(b64_clean, validate=True)
            except (binascii.Error, ValueError) as exc:
                raise ValueError("Codificación base64 inválida en la captura adjunta.") from exc
            if not raw_bytes:
                raise ValueError("La captura adjunta está vacía.")
            if len(raw_bytes) > MAX_UPLOAD_IMAGE_BYTES:
                raise ValueError("Cada captura debe pesar máximo 5 MB.")
            fmt = _detect_image_ext_and_mime(raw_bytes)
            if fmt is None:
                raise ValueError("Formato de captura no permitido (usa PNG, JPG, GIF o WebP).")
            ext, _mime = fmt
            raw_name = str(item.get("filename") or f"clip_{idx}")
            stem = re.sub(r"[^A-Za-z0-9_-]+", "_", Path(raw_name).stem).strip("_-")[:32] or "clip"
            safe_filename = f"{stem}_{uuid.uuid4().hex[:10]}{ext}"
            target = (base_resolved / safe_filename).resolve()
            if not target.is_relative_to(base_resolved) or target.parent != base_resolved:
                raise ValueError("Ruta inválida para captura de pantalla (INV-22).")
            target.write_bytes(raw_bytes)
            saved_paths.append(str(target))
        return saved_paths

    def get_uploaded_image(self, filename: str) -> tuple[str, bytes]:
        """Safely read an uploaded screenshot strictly contained inside self.uploads_dir (INV-22)."""
        clean = str(filename or "").strip()
        if not _SAFE_UPLOAD_NAME_RE.fullmatch(clean):
            raise ValueError(f"Invalid upload filename '{filename}'")
        base_resolved = self.uploads_dir.resolve()
        candidate = base_resolved / clean
        if candidate.is_symlink():
            raise ValueError("Upload symlink rejected (INV-22)")
        resolved = candidate.resolve()
        if not resolved.is_relative_to(base_resolved) or not resolved.is_file():
            raise FileNotFoundError(clean)
        data = resolved.read_bytes()
        fmt = _detect_image_ext_and_mime(data)
        return (fmt[1] if fmt else "application/octet-stream"), data

    def get_history(self) -> dict[str, Any]:
        """Return persisted chat history and memory rules with secrets scrubbed."""
        messages = self.memory_store.list_messages()
        rules = self.memory_store.list_rules()
        return {
            "messages": messages,
            "memory_rules": rules,
            "memory_count": len(rules),
        }

    def _set_apply_status(
        self,
        *,
        running: bool,
        step: int = 0,
        total_steps: int = 3,
        stage: str = "",
        detail: str = "",
        title: str = "",
        started_at: datetime | None = None,
        last_outcome: str | None = None,
    ) -> None:
        with self._status_lock:
            self._status["running"] = bool(running)
            self._status["step"] = int(step)
            self._status["total_steps"] = int(total_steps)
            self._status["stage"] = str(stage)
            self._status["detail"] = str(detail)
            self._status["title"] = str(title)
            self._status["started_at"] = started_at
            if last_outcome is not None:
                self._status["last_outcome"] = str(last_outcome)

    def get_status(self) -> dict[str, Any]:
        """Return thread-safe real-time status of self-improvement apply execution."""
        with self._status_lock:
            running = bool(self._status["running"])
            started_at = self._status.get("started_at")
            elapsed = 0.0
            if running and isinstance(started_at, datetime):
                elapsed = round(max(0.0, (self.clock.now() - started_at).total_seconds()), 2)
            return {
                "running": running,
                "step": int(self._status["step"]),
                "total_steps": int(self._status["total_steps"]),
                "stage": str(self._status["stage"]),
                "detail": str(self._status["detail"]),
                "title": str(self._status["title"]),
                "elapsed_seconds": elapsed,
                "last_outcome": str(self._status.get("last_outcome") or ""),
            }

    def is_worker_alive(self) -> bool:
        """Return True if this improver is actively applying a change or its sequential queue worker is alive."""
        with self._status_lock:
            if bool(self._status.get("running")):
                return True
        worker = self._idea_queue_worker
        return bool(worker is not None and worker.is_alive)

    def is_quota_paused(self, *, now: datetime | None = None) -> bool:
        """Return True when an active 429 quota pause is persisted for this improver's telemetry DB."""
        return is_quota_429_paused(db_path=self._resolve_telemetry_db_path(), now=now)

    def get_worker_diagnostics(self, *, now: datetime | None = None) -> dict[str, Any]:
        """Expose live worker_alive and 429 quota_paused status for queue reconciliation."""
        return {
            "worker_alive": self.is_worker_alive(),
            "quota_paused": self.is_quota_paused(now=now),
            "active_idea_id": self._active_idea_id,
            "current_step": str(self.get_status().get("stage") or ""),
        }

    def _touch_idea_heartbeat(
        self,
        idea_id: int | None,
        current_step: str,
        *,
        db_path: Path | str | None = None,
    ) -> None:
        global _ACTIVE_CURRENT_STEP
        with _QUEUE_WORKER_LOCK:
            _ACTIVE_CURRENT_STEP = str(current_step or "").strip()[:240]
        if idea_id is None:
            return
        try:
            from agent_factory.idea_engine import record_idea_heartbeat

            target_db = db_path if db_path is not None else self._resolve_telemetry_db_path()
            record_idea_heartbeat(
                idea_id,
                current_step=current_step,
                db_path=target_db,
                now=self.clock.now(),
            )
        except Exception as exc:  # noqa: BLE001
            logging.getLogger(__name__).debug("Idea heartbeat update skipped: %s", exc)

    def auto_enqueue_ideas(
        self,
        *,
        max_queued: int = MAX_AUTO_QUEUED_IDEAS,
        generator: Callable[..., Sequence[Mapping[str, Any]]] | None = None,
        now: datetime | None = None,
        source: str = "auto",
    ) -> dict[str, Any]:
        """Auto-enqueue pending or freshly generated ideas while strictly capping queued count at max_queued (3)."""
        return auto_enqueue_ideas(
            db_path=self._resolve_telemetry_db_path(),
            max_queued=max_queued,
            generator=generator,
            now=now if now is not None else self.clock.now(),
            source=source,
        )

    def resume_idea_queue(
        self,
        *,
        stale_seconds: int = DEFAULT_IDEA_STALE_SECONDS,
        executor: Callable[[dict[str, Any]], Any] | None = None,
        now: datetime | None = None,
        quota_paused: bool | None = None,
        poll_interval_seconds: float = 0.5,
    ) -> dict[str, Any]:
        """Reconcile the idea queue and start/wake the sequential queue drainer immediately."""
        return resume_idea_queue(
            db_path=self._resolve_telemetry_db_path(),
            stale_seconds=stale_seconds,
            executor=executor,
            improver=self,
            now=now,
            quota_paused=quota_paused,
            poll_interval_seconds=poll_interval_seconds,
        )

    def get_evolution_history(self, limit: int = 30) -> dict[str, Any]:
        """Return Git evolutionary version history (v1.N) from main (or HEAD fallback) with secrets redacted."""
        empty: dict[str, Any] = {
            "current_version": "v1.0",
            "total_versions": 0,
            "versions": [],
        }
        try:
            cap = max(1, min(100, int(limit)))
        except (TypeError, ValueError):
            cap = 30

        clean_env = build_sanitized_cli_env()
        try:
            main_res = self.runner.run(
                ["git", "rev-parse", "--verify", "main"],
                cwd=self.repo_root,
                timeout_seconds=15.0,
                env=clean_env,
            )
            ref = "main" if main_res.exit_code == 0 else "HEAD"
            count_res = self.runner.run(
                ["git", "rev-list", "--count", ref],
                cwd=self.repo_root,
                timeout_seconds=15.0,
                env=clean_env,
            )
        except (CommandTimeoutError, OSError, RuntimeError, ValueError):
            return empty

        if count_res.exit_code != 0:
            return empty
        try:
            total_versions = int((count_res.stdout or "").strip())
        except ValueError:
            return empty
        if total_versions <= 0:
            return empty

        try:
            log_res = self.runner.run(
                [
                    "git",
                    "log",
                    ref,
                    "-n",
                    str(cap),
                    "--shortstat",
                    "--format=%x1e%h%x1f%H%x1f%aI%x1f%s%x1f%b%x1f",
                ],
                cwd=self.repo_root,
                timeout_seconds=15.0,
                env=clean_env,
            )
        except (CommandTimeoutError, OSError, RuntimeError, ValueError):
            return empty

        if log_res.exit_code != 0:
            return empty

        versions: list[dict[str, Any]] = []
        raw_records = [rec for rec in (log_res.stdout or "").split("\x1e") if rec.strip()]
        for idx, rec in enumerate(raw_records):
            parts = rec.split("\x1f", 5)
            if len(parts) < 5:
                continue
            short_hash = self._redact(parts[0].strip())
            commit_hash = self._redact(parts[1].strip())
            committed_at = self._redact(parts[2].strip())
            raw_subject_plain = parts[3].strip()
            raw_subject = self._redact(raw_subject_plain)
            is_self_improve = "self-improve" in raw_subject_plain.lower()
            stripped_title = _SELF_IMPROVE_PREFIX_RE.sub("", raw_subject).strip()
            title = stripped_title or raw_subject
            raw_body = parts[4].replace("\r\n", "\n").replace("\r", "\n").strip()
            summary = self._redact(raw_body)[:1000].strip() if raw_body else ""
            raw_tail = parts[5] if len(parts) > 5 else ""
            stat_lines = [ln.strip() for ln in raw_tail.splitlines() if ln.strip()]
            stats = self._redact(" ".join(stat_lines)) if stat_lines else ""
            files_match = _SHORTSTAT_FILES_RE.search(stats)
            files_changed = int(files_match.group(1)) if files_match else 0
            version_num = max(1, total_versions - idx)
            versions.append(
                {
                    "version": f"v1.{version_num}",
                    "short_hash": short_hash,
                    "commit_hash": commit_hash,
                    "committed_at": committed_at,
                    "title": title,
                    "raw_subject": raw_subject,
                    "summary": summary,
                    "stats": stats,
                    "files_changed": files_changed,
                    "is_self_improve": is_self_improve,
                    "source": "self_improve" if is_self_improve else "manual",
                    "source_label": "Auto-mejora" if is_self_improve else "Cambio manual",
                }
            )

        return {
            "current_version": f"v1.{total_versions}",
            "total_versions": total_versions,
            "versions": versions,
        }

    def delete_memory_rule(self, rule_id: str) -> dict[str, Any]:
        """Delete a rule from factory_memory_rules or raise KeyError if missing."""
        deleted = self.memory_store.delete_rule(rule_id)
        if not deleted:
            raise KeyError(rule_id)
        rules = self.memory_store.list_rules()
        return {
            "ok": True,
            "deleted": True,
            "rule_id": rule_id,
            "memory_rules": rules,
            "memory_count": len(rules),
        }

    def _resolve_agy_cmd(self) -> tuple[list[str] | None, bool, str]:
        cmd = list(self.agy_command)
        is_real_agy = bool(cmd) and Path(cmd[0]).stem.lower() == "agy"
        is_exact_subprocess = type(self.runner) is SubprocessRunner
        if cmd:
            resolved = resolve_executable(
                cmd[0],
                include_host_fallbacks=is_exact_subprocess,
            )
            if (
                is_exact_subprocess
                and is_real_agy
                and (os.environ.get("AGY_CLI_DISABLED") == "1" or not resolved)
            ):
                return None, True, "CLI 'agy' no disponible en este entorno."
            if resolved and is_exact_subprocess:
                cmd[0] = resolved
        return cmd, is_real_agy, ""

    def _run_agy(
        self,
        *,
        prompt: str,
        cwd: Path,
        allow_writes: bool,
        timeout_seconds: float,
        extra_add_dirs: Sequence[Path] = (),
        extra_secrets: Sequence[str] = (),
    ) -> tuple[bool, str]:
        base_cmd, _is_real_agy, err_reason = self._resolve_agy_cmd()
        if base_cmd is None:
            return False, err_reason
        is_batch = bool(base_cmd and base_cmd[0].lower().endswith((".cmd", ".bat")))
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
                    prefix="af_liwi_",
                    delete=False,
                ) as tf:
                    tf.write(prompt)
                    tmp_prompt = Path(tf.name).resolve()
                prompt_arg = f"Lee y sigue exactamente las instrucciones del archivo: {tmp_prompt}"

            cmd = list(base_cmd) + ["-p", prompt_arg]
            for d in extra_add_dirs:
                cmd.extend(["--add-dir", str(d)])
            if allow_writes:
                cmd.append("--dangerously-skip-permissions")
            if self.model:
                cmd.extend(["--model", self.model])

            res = self.runner.run(
                cmd,
                cwd=cwd,
                timeout_seconds=timeout_seconds,
                env=clean_env,
            )
        except (CommandTimeoutError, OSError, RuntimeError, ValueError) as exc:
            return False, self._redact(str(exc) or type(exc).__name__, extra_secrets)
        finally:
            if tmp_prompt is not None:
                tmp_prompt.unlink(missing_ok=True)

        self._record_quota_telemetry(
            prompt_len=len(prompt),
            output_text=(res.stdout or "") + "\n" + (res.stderr or ""),
            exit_code=res.exit_code,
        )
        if res.exit_code != 0:
            raw = (res.stderr or res.stdout or f"exit code {res.exit_code}").strip()
            return False, self._redact(raw, extra_secrets)
        return True, self._redact(res.stdout.strip(), extra_secrets)

    def _record_quota_telemetry(
        self,
        *,
        prompt_len: int,
        output_text: str,
        exit_code: int,
    ) -> None:
        try:
            from agent_factory.error_triage import is_quota_429_error
            from agent_factory.idea_engine import record_agy_429_pause, record_agy_usage

            db_path = self._resolve_telemetry_db_path()
            if exit_code != 0 and is_quota_429_error(output_text):
                record_agy_429_pause(output_text, db_path=db_path)
            else:
                record_agy_usage(prompt_len, len(output_text or ""), db_path=db_path)
        except Exception as exc:  # noqa: BLE001
            logging.getLogger(__name__).debug("Quota telemetry skipped in self_improver: %s", exc)

    def consult_message(
        self,
        message: str,
        *,
        images: Sequence[Mapping[str, Any]] = (),
    ) -> dict[str, Any]:
        """Consult Liwi assistant in strict read-only mode, persist turns/rules, and extract proposal."""
        raw_msg = str(message or "").strip()
        if not raw_msg and not images:
            raise ValueError("El mensaje o una captura adjunta es obligatorio.")
        eff_msg = raw_msg or "Analiza la captura adjunta y sugiere mejoras."
        saved_images = self._save_uploaded_images(images)
        consult_started = self.clock.now()
        telemetry_db = self._resolve_telemetry_db_path()
        record_safe_event(
            component="self_improver",
            stage="consult_start",
            command="consult_message",
            duration_ms=0,
            db_path=telemetry_db,
        )

        try:
            user_rec = self.memory_store.add_message(
                role="user",
                content=self._redact(eff_msg),
                image_paths=saved_images,
                status="ok",
            )

            src_files = _list_factory_source_files(self.repo_root)
            rules = self.memory_store.list_rules()
            history = self.memory_store.list_messages(limit=14)[:-1]

            rules_block = "\n".join(f"- [{r['id']}] {r['rule_text']}" for r in rules) or "(ninguna)"
            hist_block = (
                "\n".join(f"{m['role']}: {m['content'][:500]}" for m in history[-8:])
                or "(inicio de conversación)"
            )
            files_block = "\n".join(src_files[:120]) or "(sin archivos)"
            img_block = "\n".join(saved_images) if saved_images else "(sin imágenes adjuntas)"

            consult_prompt = (
                "MODO CONSULTA DE SOLO LECTURA (PROHIBIDO editar archivos o ejecutar comandos mutantes).\n"
                "Eres Liwi, el asistente de auto-mejora de Agent Factory. Responde siempre en español claro.\n\n"
                f"Archivos en src/agent_factory/:\n{files_block}\n\n"
                f"Reglas persistentes en Memoria de Liwi:\n{rules_block}\n\n"
                f"Historial reciente:\n{hist_block}\n\n"
                f"Imágenes adjuntas (rutas locales):\n{img_block}\n\n"
                f"Solicitud del usuario:\n{self._redact(eff_msg)}\n\n"
                "IMPORTANTE SOBRE PROPUESTAS ATÓMICAS Y PRUEBAS EN actionable_proposal:\n"
                "- Si el usuario pide múltiples cambios o cambios sobre archivos grandes (como dashboard.py), divide el plan "
                "en pasos pequeños (<3 min cada uno), explica los pasos siguientes en 'reply' y genera 'actionable_proposal' "
                "ÚNICAMENTE para el Paso 1 atómico.\n"
                "- En el campo 'prompt' de actionable_proposal, indica explícitamente que el agente ejecute ÚNICAMENTE las "
                "pruebas dirigidas de los archivos modificados (por ejemplo: `pytest -q --no-cov tests/test_dashboard.py`) "
                "y NO ejecute toda la suite completa de pruebas, ya que la verificación final la ejecuta el gate automático de la fábrica.\n\n"
                "Devuelve ÚNICAMENTE un JSON válido con esta estructura exacta:\n"
                '{"reply": "<respuesta en español claro>", '
                '"new_memory_rules": ["<regla o preferencia detectada, si aplica>"], '
                '"actionable_proposal": {"title": "<título corto>", "prompt": "<instrucciones técnicas detalladas TDD con pruebas dirigidas únicamente>"} | null}'
            )

            with tempfile.TemporaryDirectory(prefix="af_liwi_consult_") as ro_cwd:
                ok, out = self._run_agy(
                    prompt=consult_prompt,
                    cwd=Path(ro_cwd),
                    allow_writes=False,
                    timeout_seconds=self.consult_timeout_seconds,
                )

            reply = ""
            added_rules: list[dict[str, Any]] = []
            proposal_title: str | None = None
            proposal_prompt: str | None = None
            if ok and out:
                parsed = _parse_json_dict_loose(out)
                if parsed is not None:
                    reply = self._redact(str(parsed.get("reply") or "").strip())
                    raw_rules = parsed.get("new_memory_rules")
                    if isinstance(raw_rules, list):
                        for item in raw_rules[:5]:
                            cleaned_rule = self._redact(str(item or "").strip())
                            saved_r = self.memory_store.add_rule(cleaned_rule)
                            if saved_r is not None:
                                added_rules.append(saved_r)
                    raw_prop = parsed.get("actionable_proposal")
                    if isinstance(raw_prop, Mapping):
                        p_title = self._redact(str(raw_prop.get("title") or "").strip())
                        p_prompt = self._redact(str(raw_prop.get("prompt") or "").strip())
                        if p_title and p_prompt:
                            proposal_title = p_title[:160]
                            proposal_prompt = p_prompt
                if not reply:
                    reply = self._redact(out[:3000])
            else:
                from agent_factory.error_triage import format_agent_error, is_quota_429_error

                detail = self._redact(out or "No se pudo consultar a agy.")
                if is_quota_429_error(detail):
                    reply = self._redact(format_agent_error(detail)["message"])
                else:
                    reply = self._redact(
                        f"No pude obtener respuesta del agente en modo lectura ({detail}). "
                        "Puedes revisar la configuración de agy e intentarlo nuevamente."
                    )

            assistant_rec = self.memory_store.add_message(
                role="assistant",
                content=reply,
                proposal_title=proposal_title,
                proposal_prompt=proposal_prompt,
                status="proposal" if proposal_prompt else "ok",
            )
            all_rules = self.memory_store.list_rules()
            duration_ms = max(0.0, (self.clock.now() - consult_started).total_seconds() * 1000.0)
            if ok:
                record_safe_event(
                    component="self_improver",
                    stage="consult_finish",
                    command="consult_message",
                    duration_ms=duration_ms,
                    exit_code=0,
                    db_path=telemetry_db,
                )
            else:
                err_detail = self._redact(out or "No se pudo consultar a agy.")
                record_safe_event(
                    component="self_improver",
                    stage="consult_finish",
                    command="consult_message",
                    duration_ms=duration_ms,
                    exit_code=1,
                    timed_out=("timeout" in err_detail.lower() or "timed out" in err_detail.lower()),
                    error_text=err_detail,
                    db_path=telemetry_db,
                )
                record_safe_incident(
                    stage="consult_message",
                    raw_error=err_detail,
                    db_path=telemetry_db,
                )
            return {
                "ok": True,
                "user_message": user_rec,
                "assistant_message": assistant_rec,
                "reply": reply,
                "new_memory_rules": [r["rule_text"] for r in added_rules],
                "actionable_proposal": (
                    {"title": proposal_title, "prompt": proposal_prompt}
                    if proposal_title and proposal_prompt
                    else None
                ),
                "memory_rules": all_rules,
                "memory_count": len(all_rules),
            }
        except Exception as exc:
            duration_ms = max(0.0, (self.clock.now() - consult_started).total_seconds() * 1000.0)
            redacted_err = self._redact(str(exc) or type(exc).__name__)
            record_safe_incident(
                stage="consult_message",
                raw_error=redacted_err,
                db_path=telemetry_db,
            )
            record_safe_event(
                component="self_improver",
                stage="consult_error",
                command="consult_message",
                duration_ms=duration_ms,
                exit_code=1,
                timed_out=isinstance(exc, CommandTimeoutError),
                error_text=redacted_err,
                db_path=telemetry_db,
            )
            raise

    def _snapshot_forbidden_state(self) -> dict[str, Any]:
        """Record mtime/size and git status for forbidden paths without reading .env contents (INV-19)."""
        stats: dict[str, tuple[int, int] | None] = {}
        for rel in _FORBIDDEN_SELF_IMPROVE_PREFIXES:
            p = self.repo_root / rel
            if p.is_file():
                try:
                    st = p.stat()
                    stats[rel] = (int(st.st_mtime_ns), int(st.st_size))
                except OSError:
                    stats[rel] = None
        porcelain = self._git_porcelain_forbidden()
        return {"stats": stats, "porcelain": porcelain}

    def _git_porcelain_forbidden(self) -> str:
        clean_env = build_sanitized_cli_env()
        try:
            res = self.runner.run(
                ["git", "status", "--porcelain", "--", *_FORBIDDEN_SELF_IMPROVE_PREFIXES],
                cwd=self.repo_root,
                env=clean_env,
            )
            return res.stdout if res.exit_code == 0 else ""
        except (CommandTimeoutError, OSError, RuntimeError, ValueError):
            return ""

    def _find_forbidden_modifications(self, baseline: dict[str, Any]) -> list[str]:
        changed: list[str] = []
        base_stats = baseline.get("stats", {})
        for rel in _FORBIDDEN_SELF_IMPROVE_PREFIXES:
            p = self.repo_root / rel
            if p.is_file():
                try:
                    st = p.stat()
                    cur = (int(st.st_mtime_ns), int(st.st_size))
                except OSError:
                    cur = None
                if (rel in base_stats and cur != base_stats[rel]) or (
                    rel not in base_stats and rel == ".env"
                ):
                    changed.append(rel)
        if self._git_porcelain_forbidden() != baseline.get("porcelain", ""):
            changed.append("forbidden_git_tree")
        return changed

    def _resolve_venv_tool(self, tool_name: str) -> list[str]:
        if isinstance(self.runner, SubprocessRunner):
            ext = ".exe" if os.name == "nt" else ""
            for base_dir in (
                self.repo_root / ".venv" / ("Scripts" if os.name == "nt" else "bin"),
                Path(sys.executable).resolve().parent,
            ):
                cand = base_dir / f"{tool_name}{ext}"
                if cand.is_file():
                    return [str(cand)]
            resolved = resolve_executable(tool_name)
            if resolved:
                return [resolved]
            return [sys.executable, "-m", tool_name]
        return [tool_name]

    def _collect_changed_src_tests(self, env: Mapping[str, str]) -> list[str]:
        """List modified or untracked files under src/ and tests/ relative to repo_root."""
        try:
            res = self.runner.run(
                ["git", "status", "--porcelain", "-uall", "--", "src", "tests"],
                cwd=self.repo_root,
                timeout_seconds=15.0,
                env=env,
            )
        except (CommandTimeoutError, OSError, RuntimeError, ValueError):
            return []
        if res.exit_code != 0 or not res.stdout:
            return []
        changed: list[str] = []
        for line in res.stdout.splitlines():
            if len(line) < 4:
                continue
            raw_path = line[3:].strip()
            if " -> " in raw_path:
                raw_path = raw_path.split(" -> ", 1)[1].strip()
            norm = raw_path.strip('"').replace("\\", "/")
            if norm and norm not in changed:
                changed.append(norm)
        return changed

    def _select_targeted_pytest_targets(self, changed_paths: Sequence[str]) -> tuple[list[str], bool]:
        """Select targeted test files for modified src/tests modules; fall back only if a modified src module has no matching tests."""
        available_tests = _list_available_test_files(self.repo_root)
        if not available_tests:
            return [], False

        available_set = set(available_tests)
        selected: set[str] = set(select_targeted_test_paths(changed_paths, available_tests))
        has_unmatched_src = False

        for rel in changed_paths:
            norm = _normalize_repo_rel_path(rel)
            if not norm:
                continue
            path = PurePosixPath(norm)
            if norm.startswith("src/") and path.suffix == ".py":
                stem = path.stem
                if not stem or stem in {"__init__", "__main__"}:
                    continue
                matched_this_src = bool(select_targeted_test_paths([norm], available_tests))
                if path.name == "cli.py" and "tests/test_dashboard.py" in available_set:
                    selected.add("tests/test_dashboard.py")
                    matched_this_src = True
                if not matched_this_src:
                    has_unmatched_src = True

        if has_unmatched_src or not selected:
            return [], False

        if "tests/test_self_improver.py" in available_set:
            selected.add("tests/test_self_improver.py")

        return sorted(selected), True

    def _resolve_explicit_test_targets(
        self,
        explicit_tests: Sequence[str] | None,
    ) -> list[str]:
        """Validate and deduplicate explicit test paths strictly contained inside tests/ (INV-22)."""
        if not explicit_tests:
            return []
        repo_resolved = self.repo_root.resolve()
        tests_root = (repo_resolved / "tests").resolve()
        if not tests_root.is_dir():
            return []
        resolved_targets: set[str] = set()
        for raw in explicit_tests:
            norm = _normalize_repo_rel_path(raw)
            if not norm or not norm.startswith("tests/") or not norm.endswith(".py"):
                continue
            cand = repo_resolved / norm
            if cand.is_symlink():
                continue
            try:
                cand_resolved = cand.resolve()
            except OSError:
                continue
            if cand_resolved.is_relative_to(tests_root) and cand_resolved.is_file():
                resolved_targets.add(norm)
        return sorted(resolved_targets)

    def _run_verification_gate(
        self,
        *,
        clean_env: Mapping[str, str],
        ws_env: Mapping[str, str],
        clean_title: str,
        started_at: datetime,
        explicit_tests: Sequence[str] | None = None,
        idea_id: int | None = None,
    ) -> tuple[bool, str]:
        """Run ruff check src tests followed by targeted (or full fallback) pytest."""
        self._set_apply_status(
            running=True,
            step=2,
            total_steps=3,
            stage="Verificando calidad y pruebas",
            detail="Ejecutando ruff y la suite completa de seguridad (puede tardar ~4 min)...",
            title=clean_title,
            started_at=started_at,
        )
        telemetry_db = self._resolve_telemetry_db_path()
        self._touch_idea_heartbeat(idea_id, "Verificando calidad (ruff)", db_path=telemetry_db)
        ruff_cmd = [*self._resolve_venv_tool("ruff"), "check", "src", "tests"]
        ruff_t0 = time.monotonic()
        ruff_res = self.runner.run(
            ruff_cmd,
            cwd=self.repo_root,
            timeout_seconds=self.verify_timeout_seconds,
            env=ws_env,
        )
        ruff_ms = max(0.0, (time.monotonic() - ruff_t0) * 1000.0)
        if ruff_res.exit_code != 0:
            tail = (ruff_res.stdout or ruff_res.stderr or "ruff check failed").strip()[-1200:]
            redacted_tail = self._redact(tail)
            record_safe_event(
                component="self_improver",
                stage="verify_ruff",
                command="ruff check src tests",
                duration_ms=ruff_ms,
                exit_code=ruff_res.exit_code,
                error_text=redacted_tail,
                db_path=telemetry_db,
            )
            return False, f"Falló ruff check src tests:\n{redacted_tail}"
        record_safe_event(
            component="self_improver",
            stage="verify_ruff",
            command="ruff check src tests",
            duration_ms=ruff_ms,
            exit_code=0,
            db_path=telemetry_db,
        )

        explicit_targets = self._resolve_explicit_test_targets(explicit_tests)
        if explicit_targets:
            targeted_tests = explicit_targets
            has_targeted = True
            pytest_args = ["-q", "--no-cov", *explicit_targets]
        else:
            changed_paths = self._collect_changed_src_tests(clean_env)
            targeted_tests, has_targeted = self._select_targeted_pytest_targets(changed_paths)
            norm_changed = [p.replace("\\", "/") for p in changed_paths]
            touches_dashboard = "src/agent_factory/dashboard.py" in norm_changed
            other_src_changed = any(
                p.startswith("src/") and p.endswith(".py") and p != "src/agent_factory/dashboard.py"
                for p in norm_changed
            )

            if has_targeted and touches_dashboard and "tests/test_dashboard.py" in targeted_tests:
                if other_src_changed:
                    non_dash_tests = [t for t in targeted_tests if t != "tests/test_dashboard.py"]
                    if non_dash_tests:
                        self._set_apply_status(
                            running=True,
                            step=2,
                            total_steps=3,
                            stage="Verificando calidad y pruebas",
                            detail=f"Ejecutando pruebas dirigidas ({', '.join(non_dash_tests)})...",
                            title=clean_title,
                            started_at=started_at,
                        )
                        pre_res = self.runner.run(
                            [*self._resolve_venv_tool("pytest"), "-q", "--no-cov", *non_dash_tests],
                            cwd=self.repo_root,
                            timeout_seconds=self.verify_timeout_seconds,
                            env=ws_env,
                        )
                        if pre_res.exit_code != 0:
                            raw_pre = (
                                "\n".join(
                                    part for part in (pre_res.stdout, pre_res.stderr) if part and part.strip()
                                ).strip()
                                or "pytest failed"
                            )
                            pre_prefix = f"Falló pytest {' '.join(non_dash_tests)}:\n"
                            pre_compact = compact_pytest_failure_output(
                                self._redact(raw_pre),
                                max_chars=max(200, 2500 - len(pre_prefix)),
                            )
                            return False, f"{pre_prefix}{pre_compact}"
                    pytest_args = [
                        "-q",
                        "--no-cov",
                        "tests/test_dashboard.py",
                        "-k",
                        FAST_DASHBOARD_PYTEST_K,
                    ]
                else:
                    pytest_args = ["-q", "--no-cov", *targeted_tests, "-k", FAST_DASHBOARD_PYTEST_K]
            else:
                pytest_args = ["-q", "--no-cov", *targeted_tests] if has_targeted else ["-q", "--no-cov"]

        if has_targeted:
            self._set_apply_status(
                running=True,
                step=2,
                total_steps=3,
                stage="Verificando calidad y pruebas",
                detail=f"Ejecutando pruebas dirigidas ({', '.join(targeted_tests)})...",
                title=clean_title,
                started_at=started_at,
            )
        self._touch_idea_heartbeat(
            idea_id,
            "Ejecutando pruebas automatizadas",
            db_path=telemetry_db,
        )
        pytest_cmd = [*self._resolve_venv_tool("pytest"), *pytest_args]
        pytest_t0 = time.monotonic()
        pytest_res = self.runner.run(
            pytest_cmd,
            cwd=self.repo_root,
            timeout_seconds=self.verify_timeout_seconds,
            env=ws_env,
        )
        pytest_ms = max(0.0, (time.monotonic() - pytest_t0) * 1000.0)
        pytest_cmd_label = f"pytest {' '.join(pytest_args)}"
        if pytest_res.exit_code != 0:
            raw_pytest = (
                "\n".join(
                    part for part in (pytest_res.stdout, pytest_res.stderr) if part and part.strip()
                ).strip()
                or "pytest failed"
            )
            pytest_prefix = f"Falló pytest {' '.join(pytest_args)}:\n"
            pytest_compact = compact_pytest_failure_output(
                self._redact(raw_pytest),
                max_chars=max(200, 2500 - len(pytest_prefix)),
            )
            record_safe_event(
                component="self_improver",
                stage="verify_pytest",
                command=pytest_cmd_label,
                duration_ms=pytest_ms,
                exit_code=pytest_res.exit_code,
                error_text=pytest_compact,
                db_path=telemetry_db,
            )
            return False, f"{pytest_prefix}{pytest_compact}"
        record_safe_event(
            component="self_improver",
            stage="verify_pytest",
            command=pytest_cmd_label,
            duration_ms=pytest_ms,
            exit_code=0,
            db_path=telemetry_db,
        )
        return True, ""

    def _rollback_savepoint(self, env: Mapping[str, str]) -> None:
        self.runner.run(
            ["git", "reset", "--hard", "pre-self-improve"],
            cwd=self.repo_root,
            timeout_seconds=30.0,
            env=env,
        )
        self.runner.run(
            ["git", "clean", "-fd", "src", "tests"],
            cwd=self.repo_root,
            timeout_seconds=30.0,
            env=env,
        )

    def _schedule_process_restart(self) -> None:
        def _do_restart() -> None:
            self.clock.sleep(self.restart_delay_seconds)
            if self.pre_exit_hook is not None:
                try:
                    self.pre_exit_hook()
                except Exception as exc:  # noqa: BLE001
                    logging.getLogger(__name__).debug("pre_exit_hook failed: %s", exc)
            self.restart_fn(RESTART_EXIT_CODE)

        threading.Thread(
            target=_do_restart,
            name="liwi-self-improve-restart",
            daemon=True,
        ).start()

    def apply_proposal(
        self,
        proposal_prompt: str,
        *,
        proposal_title: str = "",
        message_id: str | None = None,
        github_token: str = "",
        preserve_on_quota_429: bool = False,
        explicit_tests: Sequence[str] | None = None,
        idea_id: int | None = None,
    ) -> dict[str, Any]:
        """Tag HEAD, run agy (900s), gate with ruff + pytest (900s), rollback on failure, commit+push+exit 42 on pass."""
        global _ACTIVE_APPLY_COUNT, _ACTIVE_IDEA_ID, _ACTIVE_CURRENT_STEP

        clean_prompt = str(proposal_prompt or "").strip()
        if not clean_prompt:
            raise ValueError("El campo 'proposal_prompt' es obligatorio.")
        clean_title = self._redact(str(proposal_title or "Auto-mejora Liwi").strip())[:100]

        if not self._apply_lock.acquire(blocking=False):
            raise RuntimeError("Ya hay una auto-mejora en ejecución.")

        clean_env = build_sanitized_cli_env()
        started_at = self.clock.now()
        telemetry_db = self._resolve_telemetry_db_path()
        outcome = "error"
        self._active_idea_id = idea_id
        with _QUEUE_WORKER_LOCK:
            _ACTIVE_APPLY_COUNT += 1
            if idea_id is not None:
                _ACTIVE_IDEA_ID = idea_id
            _ACTIVE_CURRENT_STEP = "Editando código de Liwi con IA"
        self._set_apply_status(
            running=True,
            step=1,
            total_steps=3,
            stage="Editando código de Liwi con IA",
            detail="Analizando archivos y aplicando cambios solicitados...",
            title=clean_title,
            started_at=started_at,
            last_outcome="",
        )
        if idea_id is not None:
            try:
                from agent_factory.idea_engine import start_idea_execution

                start_idea_execution(
                    idea_id,
                    current_step="Preparando entorno y guardando punto de control",
                    db_path=telemetry_db,
                    now=started_at,
                )
            except Exception as exc:  # noqa: BLE001
                logging.getLogger(__name__).debug("start_idea_execution skipped: %s", exc)
        record_safe_event(
            component="self_improver",
            stage="apply_start",
            command=clean_title,
            duration_ms=0,
            db_path=telemetry_db,
        )
        try:
            # 1. Create Git savepoint tag
            tag_res = self.runner.run(
                ["git", "tag", "-f", "pre-self-improve", "HEAD"],
                cwd=self.repo_root,
                timeout_seconds=20.0,
                env=clean_env,
            )
            if tag_res.exit_code != 0:
                detail = self._redact(tag_res.stderr or tag_res.stdout or "git tag failed")
                raise RuntimeError(f"No se pudo crear el punto de guardado Git: {detail}")

            baseline = self._snapshot_forbidden_state()

            # 2. Execute agy inside factory repo with explicit forbidden-file guardrails & targeted tests only
            self._touch_idea_heartbeat(
                idea_id,
                "Editando código de Liwi con IA",
                db_path=telemetry_db,
            )
            apply_prompt = (
                "Implementa la siguiente mejora en Agent Factory usando TDD.\n"
                "REGLA ESTRICTA DE SEGURIDAD: PROHIBIDO leer o modificar .env, MASTER_PROMPT.md, "
                "AGENTS.md, config/models.toml, data/ o workspaces/.\n"
                "REGLA DE VELOCIDAD Y PRUEBAS: Ejecuta ÚNICAMENTE pruebas dirigidas de los archivos que modifiques "
                "(por ejemplo `pytest -q --no-cov tests/test_dashboard.py` o `tests/test_self_improver.py`) y `ruff check`. "
                "NO ejecutes la suite completa de pytest; FactorySelfImprover ejecutará la suite completa en su puerta final.\n\n"
                f"Propuesta ({clean_title}):\n{clean_prompt}"
            )
            agy_ok, agy_out = self._run_agy(
                prompt=apply_prompt,
                cwd=self.repo_root,
                allow_writes=True,
                timeout_seconds=self.apply_timeout_seconds,
                extra_add_dirs=(self.repo_root,),
            )

            forbidden_touched = self._find_forbidden_modifications(baseline)
            failure_reason = ""
            if forbidden_touched:
                failure_reason = (
                    f"Intento bloqueado: se tocaron rutas protegidas ({', '.join(forbidden_touched)})."
                )
            elif not agy_ok:
                failure_reason = f"Falló la ejecución de agy: {agy_out}"
            else:
                # 3. Quality gate: ruff check src tests + targeted/full pytest (with 1 auto-repair round on failure)
                ws_env = build_workspace_pytest_env()
                verify_ok, verify_err = self._run_verification_gate(
                    clean_env=clean_env,
                    ws_env=ws_env,
                    clean_title=clean_title,
                    started_at=started_at,
                    explicit_tests=explicit_tests,
                    idea_id=idea_id,
                )
                if not verify_ok:
                    self._set_apply_status(
                        running=True,
                        step=2,
                        total_steps=3,
                        stage="Auto-reparando fallo detectado...",
                        detail="Ejecutando 1 ronda de auto-reparación con agy sobre el error exacto...",
                        title=clean_title,
                        started_at=started_at,
                    )
                    self._touch_idea_heartbeat(
                        idea_id,
                        "Auto-reparando fallo detectado",
                        db_path=telemetry_db,
                    )
                    compact_verify_err = compact_pytest_failure_output(verify_err, max_chars=2500)
                    failed_nodes = extract_failed_pytest_nodes(verify_err)
                    failed_nodes_block = ""
                    if failed_nodes:
                        top_nodes = failed_nodes[:10]
                        rerun_cmd = "pytest -q --no-cov " + " ".join(top_nodes)
                        nodes_list = "\n".join(f"- {node}" for node in top_nodes)
                        failed_nodes_block = (
                            "Node IDs fallidos detectados (re-ejecuta primero estos tests antes de otros):\n"
                            f"{nodes_list}\n"
                            f"Comando sugerido: `{rerun_cmd}`\n\n"
                        )
                    repair_prompt = (
                        "AUTO-REPARACIÓN OBLIGATORIA (1 ronda): La verificación falló tras tu edición.\n"
                        "REGLA ESTRICTA DE SEGURIDAD: PROHIBIDO leer o modificar .env, MASTER_PROMPT.md, "
                        "AGENTS.md, config/models.toml, data/ o workspaces/.\n"
                        "REGLA DE VELOCIDAD: Corrige únicamente los archivos afectados y verifica solo pruebas dirigidas.\n\n"
                        f"Propuesta original ({clean_title}):\n{clean_prompt}\n\n"
                        f"{failed_nodes_block}"
                        f"Error exacto reportado:\n{compact_verify_err}"
                    )
                    repair_ok, repair_out = self._run_agy(
                        prompt=repair_prompt,
                        cwd=self.repo_root,
                        allow_writes=True,
                        timeout_seconds=min(
                            float(self.apply_timeout_seconds),
                            DEFAULT_SELF_IMPROVE_REPAIR_TIMEOUT_SECONDS,
                        ),
                        extra_add_dirs=(self.repo_root,),
                    )
                    forbidden_after_repair = self._find_forbidden_modifications(baseline)
                    if forbidden_after_repair:
                        failure_reason = (
                            f"Intento bloqueado: se tocaron rutas protegidas ({', '.join(forbidden_after_repair)})."
                        )
                    elif not repair_ok:
                        failure_reason = (
                            f"Falló la auto-reparación de agy: {repair_out}\n\nError previo:\n{verify_err}"
                        )
                    else:
                        verify_ok2, verify_err2 = self._run_verification_gate(
                            clean_env=clean_env,
                            ws_env=ws_env,
                            clean_title=clean_title,
                            started_at=started_at,
                            explicit_tests=explicit_tests,
                            idea_id=idea_id,
                        )
                        if not verify_ok2:
                            failure_reason = verify_err2

            if failure_reason:
                from agent_factory.error_triage import format_agent_error, is_quota_429_error

                raw_failure = self._redact(failure_reason)
                if preserve_on_quota_429 and is_quota_429_error(raw_failure):
                    outcome = "paused_quota"
                    err_info = format_agent_error(raw_failure)
                    duration_ms = max(0.0, (self.clock.now() - started_at).total_seconds() * 1000.0)
                    record_safe_event(
                        component="self_improver",
                        stage="apply_finish",
                        command=clean_title,
                        duration_ms=duration_ms,
                        exit_code=429,
                        timed_out=False,
                        error_text=raw_failure,
                        db_path=telemetry_db,
                    )
                    err_msg = self.memory_store.add_message(
                        role="assistant",
                        content=err_info["message"],
                        status="error",
                    )
                    return {
                        "ok": False,
                        "applied": False,
                        "rolled_back": False,
                        "quota_429": True,
                        "restart_scheduled": False,
                        "error": err_info["message"],
                        "technical_detail": raw_failure,
                        "error_details": err_info,
                        "assistant_message": err_msg,
                        "messages": self.memory_store.list_messages(),
                    }

                outcome = "rolled_back"
                self._rollback_savepoint(clean_env)
                redacted_failure = raw_failure
                duration_ms = max(0.0, (self.clock.now() - started_at).total_seconds() * 1000.0)
                record_safe_incident(
                    stage="apply_proposal",
                    raw_error=redacted_failure,
                    db_path=telemetry_db,
                )
                record_safe_event(
                    component="self_improver",
                    stage="apply_finish",
                    command=clean_title,
                    duration_ms=duration_ms,
                    exit_code=1,
                    timed_out=(
                        "timeout" in redacted_failure.lower()
                        or "timed out" in redacted_failure.lower()
                    ),
                    error_text=redacted_failure,
                    db_path=telemetry_db,
                )
                if message_id:
                    self.memory_store.update_message_status(message_id, "rolled_back")
                err_msg = self.memory_store.add_message(
                    role="assistant",
                    content=self._redact(
                        f"Auto-mejora revertida automáticamente (`pre-self-improve`).\n\n{failure_reason}"
                    ),
                    status="rolled_back",
                )
                return {
                    "ok": False,
                    "applied": False,
                    "rolled_back": True,
                    "restart_scheduled": False,
                    "error": redacted_failure,
                    "assistant_message": err_msg,
                    "messages": self.memory_store.list_messages(),
                }

            # 4. Verification passed: stage src & tests, commit on main, best-effort push, schedule exit 42
            self._set_apply_status(
                running=True,
                step=3,
                total_steps=3,
                stage="Guardando commit y reiniciando",
                detail="Creando commit en main y recargando Liwi automáticamente...",
                title=clean_title,
                started_at=started_at,
            )
            self._touch_idea_heartbeat(
                idea_id,
                "Guardando commit y finalizando",
                db_path=telemetry_db,
            )
            add_res = self.runner.run(
                ["git", "add", "-A", "--", "src", "tests"],
                cwd=self.repo_root,
                timeout_seconds=20.0,
                env=clean_env,
            )
            if add_res.exit_code != 0:
                self._rollback_savepoint(clean_env)
                detail = self._redact(add_res.stderr or add_res.stdout or "git add failed")
                raise RuntimeError(f"Error preparando commit de auto-mejora: {detail}")

            diff_res = self.runner.run(
                ["git", "diff", "--cached", "--quiet"],
                cwd=self.repo_root,
                timeout_seconds=10.0,
                env=clean_env,
            )
            if diff_res.exit_code != 0:
                name_chk = self.runner.run(["git", "config", "--get", "user.name"], cwd=self.repo_root, env=clean_env)
                email_chk = self.runner.run(["git", "config", "--get", "user.email"], cwd=self.repo_root, env=clean_env)
                commit_prefix = (
                    []
                    if (name_chk.exit_code == 0 and name_chk.stdout.strip() and email_chk.exit_code == 0 and email_chk.stdout.strip())
                    else ["-c", "user.name=AgentFactory", "-c", "user.email=agent@factory.local"]
                )
                slug_msg = re.sub(r"[\r\n\"`]+", " ", clean_title).strip() or "apply self-improvement"
                commit_res = self.runner.run(
                    ["git", *commit_prefix, "commit", "-m", f"chore(self-improve): {slug_msg}"],
                    cwd=self.repo_root,
                    timeout_seconds=20.0,
                    env=clean_env,
                )
                if commit_res.exit_code != 0:
                    self._rollback_savepoint(clean_env)
                    detail = self._redact(commit_res.stderr or commit_res.stdout or "git commit failed")
                    raise RuntimeError(f"Error haciendo commit en main: {detail}")

            head_res = self.runner.run(
                ["git", "rev-parse", "HEAD"],
                cwd=self.repo_root,
                timeout_seconds=10.0,
                env=clean_env,
            )
            commit_sha = head_res.stdout.strip() if head_res.exit_code == 0 else ""

            push_env, _push_sec = build_git_push_env(clean_env, github_token)
            try:
                self.runner.run(
                    ["git", "push", "origin", "main"],
                    cwd=self.repo_root,
                    timeout_seconds=20.0,
                    env=push_env,
                )
            except (CommandTimeoutError, OSError, RuntimeError, ValueError) as exc:
                record_safe_incident(
                    stage="apply_git_push",
                    raw_error=self._redact(str(exc) or type(exc).__name__),
                    db_path=telemetry_db,
                )

            if message_id:
                self.memory_store.update_message_status(message_id, "applied")
            ok_msg = self.memory_store.add_message(
                role="assistant",
                content=(
                    f"✓ Mejora aplicada y verificada (`ruff check src tests` + `pytest -q --no-cov` en verde). "
                    f"Commit `{commit_sha[:7]}` en `main`. Reiniciando Liwi..."
                ),
                status="applied",
            )
            outcome = "applied"
            duration_ms = max(0.0, (self.clock.now() - started_at).total_seconds() * 1000.0)
            record_safe_event(
                component="self_improver",
                stage="apply_finish",
                command=clean_title,
                duration_ms=duration_ms,
                exit_code=0,
                db_path=telemetry_db,
            )
            self._schedule_process_restart()
            return {
                "ok": True,
                "applied": True,
                "rolled_back": False,
                "restart_scheduled": True,
                "exit_code": RESTART_EXIT_CODE,
                "commit_sha": commit_sha,
                "assistant_message": ok_msg,
                "messages": self.memory_store.list_messages(),
            }
        except Exception as exc:
            duration_ms = max(0.0, (self.clock.now() - started_at).total_seconds() * 1000.0)
            redacted_err = self._redact(str(exc) or type(exc).__name__)
            record_safe_incident(
                stage="apply_proposal",
                raw_error=redacted_err,
                db_path=telemetry_db,
            )
            record_safe_event(
                component="self_improver",
                stage="apply_error",
                command=clean_title,
                duration_ms=duration_ms,
                exit_code=1,
                timed_out=isinstance(exc, CommandTimeoutError),
                error_text=redacted_err,
                db_path=telemetry_db,
            )
            raise
        finally:
            self._active_idea_id = None
            with _QUEUE_WORKER_LOCK:
                _ACTIVE_APPLY_COUNT = max(0, _ACTIVE_APPLY_COUNT - 1)
                if _ACTIVE_APPLY_COUNT == 0:
                    _ACTIVE_IDEA_ID = None
                    _ACTIVE_CURRENT_STEP = ""
            self._set_apply_status(
                running=False,
                step=0,
                total_steps=3,
                stage="",
                detail="",
                title="",
                started_at=None,
                last_outcome=outcome,
            )
            self._apply_lock.release()


def is_quota_429_paused(
    *,
    db_path: Path | str | None = None,
    now: datetime | None = None,
) -> bool:
    """Return True if an active 429 quota pause is persisted in SQLite quota_state."""
    try:
        from agent_factory.idea_engine import get_quota_status

        status = get_quota_status(db_path=db_path, now=now)
        return bool(status.get("is_paused", False))
    except Exception as exc:  # noqa: BLE001
        logging.getLogger(__name__).debug("is_quota_429_paused fallback False: %s", exc)
        return False


def is_idea_worker_alive(
    *,
    improver: FactorySelfImprover | None = None,
    staging_manager: Any | None = None,
) -> bool:
    """Return True when the sequential idea queue worker or an active self-improvement apply is alive."""
    with _QUEUE_WORKER_LOCK:
        if _QUEUE_WORKER is not None and _QUEUE_WORKER.is_alive:
            return True
        if _ACTIVE_APPLY_COUNT > 0:
            return True
    if improver is not None and improver.is_worker_alive():
        return True
    if staging_manager is not None:
        worker_thread = getattr(staging_manager, "_worker", None)
        if isinstance(worker_thread, threading.Thread) and worker_thread.is_alive():
            return True
    return False


def get_idea_worker_runtime_status(
    *,
    db_path: Path | str | None = None,
    now: datetime | None = None,
    improver: FactorySelfImprover | None = None,
    staging_manager: Any | None = None,
) -> dict[str, Any]:
    """Return live worker_alive and 429 quota_paused diagnostics without circular imports."""
    with _QUEUE_WORKER_LOCK:
        worker = _QUEUE_WORKER
        active_id = worker.active_idea_id if (worker is not None and worker.active_idea_id is not None) else _ACTIVE_IDEA_ID
        step = worker.current_step if (worker is not None and worker.current_step) else _ACTIVE_CURRENT_STEP
    return {
        "worker_alive": is_idea_worker_alive(improver=improver, staging_manager=staging_manager),
        "quota_paused": is_quota_429_paused(db_path=db_path, now=now),
        "active_idea_id": active_id,
        "current_step": step,
    }


class IdeaQueueWorker:
    """Sequential daemon worker that drains 'queued' improvement ideas independently of the 24/7 generator."""

    def __init__(
        self,
        *,
        db_path: Path | str | None = None,
        executor: Callable[[dict[str, Any]], Any] | None = None,
        improver: FactorySelfImprover | None = None,
        stale_seconds: int = DEFAULT_IDEA_STALE_SECONDS,
        max_retries: int = MAX_IDEA_RETRIES,
        poll_interval_seconds: float = 0.5,
        clock: Clock | None = None,
    ) -> None:
        self.db_path = db_path
        self._executor = executor
        self._improver = improver
        self.stale_seconds = int(stale_seconds)
        self.max_retries = max(0, int(max_retries))
        self.poll_interval_seconds = max(0.01, float(poll_interval_seconds))
        self.clock: Clock = clock if clock is not None else _WallClock()

        self._lock = threading.RLock()
        self._cond = threading.Condition(self._lock)
        self._stop_event = threading.Event()
        self._wakeup_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._busy: bool = False
        self._active_idea_id: int | None = None
        self._current_step: str = ""
        self._last_result: dict[str, Any] | None = None

    @property
    def is_alive(self) -> bool:
        with self._lock:
            return self._thread is not None and self._thread.is_alive()

    @property
    def active_idea_id(self) -> int | None:
        with self._lock:
            return self._active_idea_id

    @property
    def current_step(self) -> str:
        with self._lock:
            return self._current_step

    def _set_active_step(self, idea_id: int | None, step: str) -> None:
        with self._lock:
            self._active_idea_id = idea_id
            self._current_step = str(step or "")

    def _default_execute_idea(self, idea: dict[str, Any]) -> dict[str, Any]:
        target_improver = self._improver
        if target_improver is None:
            target_improver = FactorySelfImprover(
                telemetry_db_path=self.db_path,
                clock=self.clock,
                restart_fn=lambda _code: None,
                restart_delay_seconds=0.0,
            )
        return target_improver.apply_proposal(
            str(idea.get("technical_prompt") or ""),
            proposal_title=str(idea.get("title") or "Auto-mejora"),
            preserve_on_quota_429=True,
            idea_id=int(idea["id"]),
        )

    def drain_once(self) -> dict[str, Any]:
        """Execute at most one queued idea sequentially, updating heartbeat and handling 429/retries."""
        from agent_factory.error_triage import is_quota_429_error
        from agent_factory.idea_engine import (
            finish_idea_execution,
            record_agy_429_pause,
            record_idea_heartbeat,
            select_next_queued_idea,
            start_idea_execution,
        )

        ref_now = self.clock.now()
        if is_quota_429_paused(db_path=self.db_path, now=ref_now):
            return {"status": "quota_pause", "processed": False}

        idea = select_next_queued_idea(db_path=self.db_path)
        if idea is None:
            return {"status": "empty", "processed": False}

        idea_id = int(idea["id"])
        with self._cond:
            self._busy = True
            self._active_idea_id = idea_id
            self._current_step = "Iniciando ejecución en cola"
            self._cond.notify_all()

        try:
            start_idea_execution(
                idea_id,
                current_step="Iniciando ejecución en cola",
                db_path=self.db_path,
                now=ref_now,
            )
            exec_fn = self._executor if self._executor is not None else self._default_execute_idea
            raw_res = exec_fn(idea)

            if isinstance(raw_res, Mapping):
                if raw_res.get("quota_429") or (
                    not raw_res.get("ok", True)
                    and is_quota_429_error(
                        str(raw_res.get("technical_detail") or raw_res.get("error") or "")
                    )
                ):
                    err_text = str(
                        raw_res.get("technical_detail")
                        or raw_res.get("error")
                        or "RESOURCE_EXHAUSTED 429"
                    )
                    if not is_quota_429_paused(db_path=self.db_path, now=self.clock.now()):
                        record_agy_429_pause(err_text, db_path=self.db_path, now=self.clock.now())
                    finish_idea_execution(
                        idea_id,
                        status="queued",
                        current_step="Pausada por cuota 429",
                        increment_retry=False,
                        clear_heartbeat=True,
                        db_path=self.db_path,
                        now=self.clock.now(),
                    )
                    return {"status": "quota_pause", "processed": True, "idea_id": idea_id}
                ok = bool(raw_res.get("ok", raw_res.get("applied", True)))
                err_detail = str(raw_res.get("error") or "Error ejecutando idea en cola.")
            elif isinstance(raw_res, bool):
                ok = raw_res
                err_detail = "Error ejecutando idea en cola."
            else:
                ok = True
                err_detail = ""

            if ok:
                record_idea_heartbeat(
                    idea_id,
                    current_step="Finalizando implementación",
                    db_path=self.db_path,
                    now=self.clock.now(),
                )
                finish_idea_execution(
                    idea_id,
                    status="implemented",
                    current_step="Completada",
                    failure_reason=None,
                    increment_retry=False,
                    clear_heartbeat=True,
                    db_path=self.db_path,
                    now=self.clock.now(),
                )
                return {"status": "implemented", "processed": True, "idea_id": idea_id}

            next_retry = int(idea.get("retry_count") or 0) + 1
            if next_retry > self.max_retries:
                finish_idea_execution(
                    idea_id,
                    status="failed",
                    current_step="",
                    failure_reason=redact_secrets(err_detail),
                    increment_retry=True,
                    clear_heartbeat=True,
                    db_path=self.db_path,
                    now=self.clock.now(),
                )
                return {"status": "failed", "processed": True, "idea_id": idea_id}

            finish_idea_execution(
                idea_id,
                status="queued",
                current_step="",
                failure_reason=None,
                increment_retry=True,
                clear_heartbeat=True,
                db_path=self.db_path,
                now=self.clock.now(),
            )
            return {"status": "requeued", "processed": True, "idea_id": idea_id}
        except Exception as exc:  # noqa: BLE001
            err_msg = redact_secrets(str(exc) or type(exc).__name__)
            if is_quota_429_error(err_msg):
                if not is_quota_429_paused(db_path=self.db_path, now=self.clock.now()):
                    record_agy_429_pause(err_msg, db_path=self.db_path, now=self.clock.now())
                finish_idea_execution(
                    idea_id,
                    status="queued",
                    current_step="Pausada por cuota 429",
                    increment_retry=False,
                    clear_heartbeat=True,
                    db_path=self.db_path,
                    now=self.clock.now(),
                )
                return {"status": "quota_pause", "processed": True, "idea_id": idea_id}
            next_retry = int(idea.get("retry_count") or 0) + 1
            target_status = "failed" if next_retry > self.max_retries else "queued"
            finish_idea_execution(
                idea_id,
                status=target_status,
                current_step="",
                failure_reason=err_msg if target_status == "failed" else None,
                increment_retry=True,
                clear_heartbeat=True,
                db_path=self.db_path,
                now=self.clock.now(),
            )
            return {"status": target_status, "processed": True, "idea_id": idea_id, "error": err_msg}
        finally:
            with self._cond:
                self._busy = False
                self._active_idea_id = None
                self._current_step = ""
                self._cond.notify_all()

    def _worker_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                result = self.drain_once()
                with self._cond:
                    self._last_result = result
                    self._cond.notify_all()
                if result.get("processed") and result.get("status") not in {"quota_pause", "empty"}:
                    continue
            except Exception as exc:  # noqa: BLE001
                logging.getLogger(__name__).warning(
                    "IdeaQueueWorker cycle error: %s",
                    redact_secrets(str(exc)),
                )
            if self._stop_event.is_set():
                break
            self._wakeup_event.clear()
            self._wakeup_event.wait(timeout=self.poll_interval_seconds)

    def start(self) -> IdeaQueueWorker:
        """Start the sequential queue worker thread idempotently."""
        with self._cond:
            if self._thread is not None and self._thread.is_alive():
                self._wakeup_event.set()
                return self
            self._stop_event.clear()
            self._wakeup_event.set()
            self._thread = threading.Thread(
                target=self._worker_loop,
                name="liwi-idea-queue-worker",
                daemon=True,
            )
            self._thread.start()
            self._cond.notify_all()
        return self

    def wake(self) -> None:
        """Wake the worker immediately (starting it if not alive and not stopped)."""
        with self._cond:
            if self._stop_event.is_set():
                return
            if self._thread is None or not self._thread.is_alive():
                self.start()
                return
            self._wakeup_event.set()
            self._cond.notify_all()

    def wait_until_idle(self, timeout: float = 10.0) -> bool:
        """Wait until the worker is not busy and either no queued/implementing ideas remain or quota is paused."""
        from agent_factory.idea_engine import count_ideas_by_status

        deadline = time.monotonic() + max(0.0, float(timeout))
        while True:
            with self._cond:
                busy = self._busy
            if not busy:
                if is_quota_429_paused(db_path=self.db_path, now=self.clock.now()):
                    with self._cond:
                        if not self._busy:
                            return True
                else:
                    counts = count_ideas_by_status(db_path=self.db_path)
                    queued_left = int(counts.get("queued", 0))
                    impl_left = int(counts.get("implementing", 0))
                    if queued_left == 0 and impl_left == 0:
                        with self._cond:
                            if not self._busy:
                                return True
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            with self._cond:
                self._cond.wait(timeout=min(0.05, remaining))

    def stop(self, timeout: float = 5.0) -> None:
        """Signal the sequential queue worker thread to stop and join cleanly."""
        with self._cond:
            self._stop_event.set()
            self._wakeup_event.set()
            thread = self._thread
            self._cond.notify_all()
        if thread is not None and thread.is_alive():
            thread.join(timeout=max(0.0, float(timeout)))
        with self._cond:
            if self._thread is not None and not self._thread.is_alive():
                self._thread = None


def get_idea_queue_worker() -> IdeaQueueWorker | None:
    """Return the active module-level IdeaQueueWorker instance, if initialized."""
    with _QUEUE_WORKER_LOCK:
        return _QUEUE_WORKER


def stop_idea_queue_worker(timeout: float = 5.0) -> None:
    """Stop and clear the module-level IdeaQueueWorker cleanly."""
    global _QUEUE_WORKER
    with _QUEUE_WORKER_LOCK:
        worker = _QUEUE_WORKER
        _QUEUE_WORKER = None
    if worker is not None:
        worker.stop(timeout=timeout)


def auto_enqueue_ideas(
    *,
    db_path: Path | str | None = None,
    max_queued: int = MAX_AUTO_QUEUED_IDEAS,
    generator: Callable[..., Sequence[Mapping[str, Any]]] | None = None,
    now: datetime | None = None,
    source: str = "auto",
) -> dict[str, Any]:
    """Automatically enqueue pending or newly generated ideas, strictly capping queued count at max_queued (default 3)."""
    from agent_factory.idea_engine import (
        count_ideas_by_status,
        save_ideas,
        select_next_autonomous_idea,
        update_idea_status,
    )

    if isinstance(max_queued, bool) or not isinstance(max_queued, int) or max_queued <= 0:
        raise ValueError("max_queued debe ser un entero positivo.")

    if is_quota_429_paused(db_path=db_path, now=now):
        current_queued = int(count_ideas_by_status(db_path=db_path).get("queued", 0))
        return {
            "ok": True,
            "skipped": True,
            "reason": "quota_pause",
            "queued_count": current_queued,
            "max_queued": max_queued,
            "enqueued": [],
            "generated_count": 0,
        }

    current_queued = int(count_ideas_by_status(db_path=db_path).get("queued", 0))
    available_slots = max(0, max_queued - current_queued)
    if available_slots <= 0:
        return {
            "ok": True,
            "skipped": True,
            "reason": "queue_limit_reached",
            "queued_count": current_queued,
            "max_queued": max_queued,
            "enqueued": [],
            "generated_count": 0,
        }

    enqueued: list[dict[str, Any]] = []
    while len(enqueued) < available_slots:
        next_pending = select_next_autonomous_idea(db_path=db_path)
        if next_pending is None:
            break
        updated = update_idea_status(int(next_pending["id"]), "queued", db_path=db_path)
        enqueued.append(updated)

    generated_count = 0
    remaining_slots = available_slots - len(enqueued)
    if remaining_slots > 0 and generator is not None:
        try:
            raw_generated = generator(remaining_slots)
        except TypeError:
            raw_generated = generator()
        raw_list = list(raw_generated) if raw_generated is not None else []
        unsaved = [item for item in raw_list if isinstance(item, Mapping) and "id" not in item]
        if unsaved:
            saved_new = save_ideas(
                unsaved,
                source=source,
                db_path=db_path,
                max_items=remaining_slots,
            )
            generated_count = len(saved_new)
        else:
            generated_count = len(raw_list)

        while len(enqueued) < available_slots:
            next_pending = select_next_autonomous_idea(db_path=db_path)
            if next_pending is None:
                break
            updated = update_idea_status(int(next_pending["id"]), "queued", db_path=db_path)
            enqueued.append(updated)

    final_queued = int(count_ideas_by_status(db_path=db_path).get("queued", 0))
    return {
        "ok": True,
        "skipped": False,
        "reason": "ok",
        "queued_count": final_queued,
        "max_queued": max_queued,
        "enqueued": enqueued,
        "generated_count": generated_count,
    }


def resume_idea_queue(
    *,
    db_path: Path | str | None = None,
    stale_seconds: int = DEFAULT_IDEA_STALE_SECONDS,
    worker: IdeaQueueWorker | None = None,
    executor: Callable[[dict[str, Any]], Any] | None = None,
    improver: FactorySelfImprover | None = None,
    now: datetime | None = None,
    quota_paused: bool | None = None,
    poll_interval_seconds: float = 0.5,
) -> dict[str, Any]:
    """Reconcile the idea queue and immediately start/wake the sequential worker to drain queued ideas (independent of 24/7 toggle, respecting only 429 quota pause)."""
    global _QUEUE_WORKER
    from agent_factory.idea_engine import reconcile_idea_queue

    eff_quota_paused = (
        bool(quota_paused)
        if quota_paused is not None
        else is_quota_429_paused(db_path=db_path, now=now)
    )
    current_worker_alive = (
        worker.is_alive
        if worker is not None
        else is_idea_worker_alive(improver=improver)
    )

    reconciliation = reconcile_idea_queue(
        stale_seconds=stale_seconds,
        db_path=db_path,
        worker_alive=current_worker_alive,
        quota_paused=eff_quota_paused,
        now=now,
    )

    if eff_quota_paused:
        return {
            "ok": True,
            "resumed": False,
            "worker_started": False,
            "worker_alive": current_worker_alive,
            "pause_reason": "quota_pause",
            "reconciliation": reconciliation,
            "counts": reconciliation["counts"],
        }

    queued_count = int(reconciliation["counts"].get("queued", 0))
    has_active = reconciliation["active_idea"] is not None
    if queued_count <= 0 and not has_active:
        return {
            "ok": True,
            "resumed": False,
            "worker_started": False,
            "worker_alive": current_worker_alive,
            "pause_reason": "empty",
            "reconciliation": reconciliation,
            "counts": reconciliation["counts"],
        }

    old_worker_to_stop: IdeaQueueWorker | None = None
    if worker is not None:
        target_worker = worker
    else:
        with _QUEUE_WORKER_LOCK:
            existing = _QUEUE_WORKER
            same_db = existing is not None and str(existing.db_path) == str(db_path)
            same_exec = existing is not None and (executor is None or existing._executor is executor)
            if existing is not None and same_db and same_exec:
                target_worker = existing
            else:
                old_worker_to_stop = existing
                target_worker = IdeaQueueWorker(
                    db_path=db_path,
                    executor=executor,
                    improver=improver,
                    stale_seconds=stale_seconds,
                    poll_interval_seconds=poll_interval_seconds,
                )
                _QUEUE_WORKER = target_worker
        if old_worker_to_stop is not None:
            old_worker_to_stop.stop(timeout=2.0)

    if improver is not None:
        improver._idea_queue_worker = target_worker

    was_alive = target_worker.is_alive
    if was_alive:
        target_worker.wake()
        worker_started = False
    else:
        target_worker.start()
        worker_started = True

    live_now = target_worker.is_alive
    return {
        "ok": True,
        "resumed": True,
        "worker_started": worker_started,
        "worker_alive": live_now,
        "pause_reason": "ok" if live_now else "no_worker",
        "reconciliation": reconciliation,
        "counts": reconciliation["counts"],
    }
