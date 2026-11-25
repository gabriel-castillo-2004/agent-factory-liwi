"""SQLite append-only EventStore adapter (INV-11, Section 7.5)."""

import json
import sqlite3
import threading
from datetime import datetime
from pathlib import Path

from agent_factory.domain.events import Event
from agent_factory.ports.store import DuplicateEventError


class SqliteEventStore:
    """Thread-safe SQLite-backed implementation of EventStore."""

    def __init__(self, db_path: str | Path = ":memory:") -> None:
        self.db_path = str(db_path)
        if self.db_path != ":memory:":
            Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._init_db()

    def _init_db(self) -> None:
        with self._lock:
            self._conn.execute("PRAGMA busy_timeout=5000")
            with self._conn:
                self._conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS events (
                        id TEXT PRIMARY KEY,
                        project_id TEXT NOT NULL,
                        type TEXT NOT NULL,
                        payload TEXT NOT NULL,
                        at TEXT NOT NULL
                    )
                    """
                )
                self._conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS telegram_processed_updates (
                        update_id INTEGER PRIMARY KEY
                    )
                    """
                )
                self._conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS telegram_active_projects (
                        chat_id INTEGER PRIMARY KEY,
                        project_id TEXT NOT NULL
                    )
                    """
                )
        from agent_factory.recovery_agent import init_error_lessons_table
        from agent_factory.self_improver import init_factory_self_improver_tables

        init_error_lessons_table(self._conn, self._lock)
        init_factory_self_improver_tables(self._conn, self._lock)

    def append(self, event: Event) -> None:
        """Append an event to the SQLite database."""
        payload_json = json.dumps(event.payload, sort_keys=True)
        at_iso = event.at.isoformat()
        with self._lock:
            try:
                with self._conn:
                    self._conn.execute(
                        "INSERT INTO events (id, project_id, type, payload, at) VALUES (?, ?, ?, ?, ?)",
                        (event.id, event.project_id, event.type, payload_json, at_iso),
                    )
            except sqlite3.IntegrityError as err:
                raise DuplicateEventError(f"Duplicate event ID: {event.id}") from err

    def get_events(self, project_id: str) -> list[Event]:
        """Return all events for project_id in append order."""
        with self._lock:
            cursor = self._conn.cursor()
            cursor.execute(
                "SELECT id, project_id, type, payload, at FROM events WHERE project_id = ? ORDER BY rowid ASC",
                (project_id,),
            )
            rows = cursor.fetchall()
        events: list[Event] = []
        for row in rows:
            events.append(
                Event(
                    id=row[0],
                    project_id=row[1],
                    type=row[2],
                    payload=json.loads(row[3]),
                    at=datetime.fromisoformat(row[4]),
                )
            )
        return events

    def list_project_ids(self) -> list[str]:
        """Return distinct project IDs stored in the database."""
        with self._lock:
            cursor = self._conn.cursor()
            cursor.execute("SELECT DISTINCT project_id FROM events ORDER BY rowid ASC")
            rows = cursor.fetchall()
        return [row[0] for row in rows]

    def delete_project(self, project_id: str) -> bool:
        """Delete all events and active Telegram chat mappings for project_id."""
        with self._lock, self._conn:
            cursor = self._conn.execute(
                "DELETE FROM events WHERE project_id = ?",
                (project_id,),
            )
            self._conn.execute(
                "DELETE FROM telegram_active_projects WHERE project_id = ?",
                (project_id,),
            )
            return cursor.rowcount > 0

    def record_telegram_update(self, update_id: int) -> None:
        """Persist a processed Telegram update_id across restarts (INV-16)."""
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT OR IGNORE INTO telegram_processed_updates (update_id) VALUES (?)",
                (int(update_id),),
            )

    def get_processed_telegram_updates(self) -> set[int]:
        """Load all persistently recorded Telegram update_ids."""
        with self._lock:
            cursor = self._conn.cursor()
            cursor.execute("SELECT update_id FROM telegram_processed_updates")
            return {int(row[0]) for row in cursor.fetchall()}

    def set_telegram_active_project(self, chat_id: int, project_id: str) -> None:
        """Persist the active project_id associated with chat_id across restarts."""
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT OR REPLACE INTO telegram_active_projects (chat_id, project_id) VALUES (?, ?)",
                (int(chat_id), project_id),
            )

    def get_telegram_active_projects(self) -> dict[int, str]:
        """Load persisted chat_id -> project_id routing map."""
        with self._lock:
            cursor = self._conn.cursor()
            cursor.execute("SELECT chat_id, project_id FROM telegram_active_projects")
            return {int(row[0]): str(row[1]) for row in cursor.fetchall()}

    def close(self) -> None:
        """Close any underlying database connection."""
        with self._lock:
            if self._conn:
                self._conn.close()
