"""In-memory append-only EventStore adapter (INV-11, Section 7.5)."""

import copy
import threading

from agent_factory.domain.events import Event
from agent_factory.ports.store import DuplicateEventError


class MemoryEventStore:
    """Thread-safe in-memory implementation of EventStore."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._events: list[Event] = []
        self._seen_ids: set[str] = set()
        self._tg_updates: set[int] = set()
        self._tg_active_projects: dict[int, str] = {}

    def record_telegram_update(self, update_id: int) -> None:
        """Record a processed Telegram update_id in memory."""
        with self._lock:
            self._tg_updates.add(int(update_id))

    def get_processed_telegram_updates(self) -> set[int]:
        """Return processed Telegram update_ids."""
        with self._lock:
            return set(self._tg_updates)

    def set_telegram_active_project(self, chat_id: int, project_id: str) -> None:
        """Store chat_id -> active project_id mapping."""
        with self._lock:
            self._tg_active_projects[int(chat_id)] = project_id

    def get_telegram_active_projects(self) -> dict[int, str]:
        """Return chat_id -> active project_id mapping."""
        with self._lock:
            return dict(self._tg_active_projects)

    def append(self, event: Event) -> None:
        """Append an event to memory store."""
        with self._lock:
            if event.id in self._seen_ids:
                raise DuplicateEventError(f"Duplicate event ID: {event.id}")
            self._seen_ids.add(event.id)
            self._events.append(
                event.model_copy(update={"payload": copy.deepcopy(event.payload)}, deep=True)
            )

    def get_events(self, project_id: str) -> list[Event]:
        """Return all events for project_id in append order."""
        with self._lock:
            return [
                e.model_copy(update={"payload": copy.deepcopy(e.payload)}, deep=True)
                for e in self._events
                if e.project_id == project_id
            ]

    def list_project_ids(self) -> list[str]:
        """Return distinct project IDs in insertion order."""
        with self._lock:
            seen: set[str] = set()
            res: list[str] = []
            for e in self._events:
                if e.project_id not in seen:
                    seen.add(e.project_id)
                    res.append(e.project_id)
            return res

    def delete_project(self, project_id: str) -> bool:
        """Remove all stored events and Telegram routing entries for project_id."""
        with self._lock:
            before = len(self._events)
            removed_ids = {e.id for e in self._events if e.project_id == project_id}
            self._events = [e for e in self._events if e.project_id != project_id]
            self._seen_ids -= removed_ids
            for cid in [k for k, v in self._tg_active_projects.items() if v == project_id]:
                del self._tg_active_projects[cid]
            return len(self._events) < before
