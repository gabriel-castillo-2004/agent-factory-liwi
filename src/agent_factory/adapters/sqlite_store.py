"""SQLite append-only EventStore adapter (INV-11, Section 7.5)."""

import json
import sqlite3
from datetime import datetime
from pathlib import Path

from agent_factory.domain.events import Event
from agent_factory.ports.store import DuplicateEventError


class SqliteEventStore:
    """SQLite-backed implementation of EventStore."""

    def __init__(self, db_path: str | Path = ":memory:") -> None:
        self.db_path = str(db_path)
        if self.db_path != ":memory:":
            Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.db_path)
        self._init_db()

    def _init_db(self) -> None:
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

    def append(self, event: Event) -> None:
        """Append an event to the SQLite database."""
        payload_json = json.dumps(event.payload, sort_keys=True)
        at_iso = event.at.isoformat()
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
        cursor = self._conn.cursor()
        cursor.execute("SELECT DISTINCT project_id FROM events ORDER BY rowid ASC")
        rows = cursor.fetchall()
        return [row[0] for row in rows]

    def close(self) -> None:
        """Close any underlying database connection."""
        if self._conn:
            self._conn.close()
