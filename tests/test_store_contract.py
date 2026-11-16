"""Unified contract test suite for MemoryEventStore and SqliteEventStore (AC-1.10, INV-11, F-16)."""

from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from agent_factory.adapters.memory_store import MemoryEventStore
from agent_factory.adapters.sqlite_store import SqliteEventStore
from agent_factory.domain.events import Event, EventType
from agent_factory.ports.store import DuplicateEventError, EventStore


@pytest.fixture(params=["memory", "sqlite"])
def event_store(request: pytest.FixtureRequest, tmp_path: Path) -> Iterator[EventStore]:
    """Provide both MemoryEventStore and SqliteEventStore to the same contract suite."""
    if request.param == "memory":
        yield MemoryEventStore()
    else:
        db_file = tmp_path / "contract_events.db"
        store = SqliteEventStore(db_path=db_file)
        yield store
        store.close()


def test_store_append_and_get_events_preserves_order(event_store: EventStore) -> None:
    """AC-1.10: Appending events and reading them back preserves exact chronological append order."""
    t1 = datetime(2026, 11, 16, 10, 0, tzinfo=UTC)
    t2 = datetime(2026, 11, 16, 10, 1, tzinfo=UTC)
    t3 = datetime(2026, 11, 16, 10, 2, tzinfo=UTC)

    e1 = Event(
        id="evt-001",
        project_id="proj-alpha",
        type=EventType.PROJECT_CREATED,
        payload={"name": "Alpha"},
        at=t1,
    )
    e2 = Event(
        id="evt-002",
        project_id="proj-alpha",
        type=EventType.STATE_TRANSITIONED,
        payload={"from": "IDLE", "to": "INTERVIEW"},
        at=t2,
    )
    e3 = Event(
        id="evt-003",
        project_id="proj-alpha",
        type=EventType.SPEC_UPDATED,
        payload={"objective": "Build Alpha"},
        at=t3,
    )

    event_store.append(e1)
    event_store.append(e2)
    event_store.append(e3)

    events = event_store.get_events("proj-alpha")
    assert events == [e1, e2, e3]


def test_store_isolates_events_by_project_id(event_store: EventStore) -> None:
    """AC-1.10: Events for different projects are strictly isolated."""
    now = datetime(2026, 11, 16, 11, 0, tzinfo=UTC)
    e_alpha = Event(
        id="evt-a1",
        project_id="proj-alpha",
        type=EventType.PROJECT_CREATED,
        payload={"name": "Alpha"},
        at=now,
    )
    e_beta = Event(
        id="evt-b1",
        project_id="proj-beta",
        type=EventType.PROJECT_CREATED,
        payload={"name": "Beta"},
        at=now,
    )

    event_store.append(e_alpha)
    event_store.append(e_beta)

    assert event_store.get_events("proj-alpha") == [e_alpha]
    assert event_store.get_events("proj-beta") == [e_beta]


def test_store_rejects_duplicate_event_id(event_store: EventStore) -> None:
    """AC-1.10: Append-only store rejects duplicate event IDs."""
    now = datetime(2026, 11, 16, 11, 30, tzinfo=UTC)
    event = Event(
        id="evt-dup-1",
        project_id="proj-alpha",
        type=EventType.PROJECT_CREATED,
        payload={"name": "Alpha"},
        at=now,
    )
    event_store.append(event)

    with pytest.raises((DuplicateEventError, ValueError)):
        event_store.append(event)

    assert len(event_store.get_events("proj-alpha")) == 1


def test_store_lists_distinct_project_ids(event_store: EventStore) -> None:
    """AC-1.10: list_project_ids returns distinct project IDs in the store."""
    now = datetime(2026, 11, 16, 12, 0, tzinfo=UTC)
    event_store.append(
        Event(
            id="evt-p1-1",
            project_id="proj-1",
            type=EventType.PROJECT_CREATED,
            payload={},
            at=now,
        )
    )
    event_store.append(
        Event(
            id="evt-p2-1",
            project_id="proj-2",
            type=EventType.PROJECT_CREATED,
            payload={},
            at=now,
        )
    )
    event_store.append(
        Event(
            id="evt-p1-2",
            project_id="proj-1",
            type=EventType.SPEC_UPDATED,
            payload={},
            at=now,
        )
    )

    assert set(event_store.list_project_ids()) == {"proj-1", "proj-2"}
    assert len(event_store.list_project_ids()) == 2


def test_store_returns_empty_list_for_unknown_project(event_store: EventStore) -> None:
    """AC-1.10: Requesting events for a nonexistent project returns an empty list."""
    assert event_store.get_events("nonexistent-project-id") == []


def test_store_events_are_immutable_and_defend_against_payload_tampering(
    event_store: EventStore,
) -> None:
    """AC-1.10 / F-16 (INV-11): Event instances are frozen and stored payloads cannot be mutated in-place."""
    now = datetime(2026, 11, 16, 13, 0, tzinfo=UTC)
    original_payload = {"name": "ImmutableApp", "nested": ["v1"]}
    event = Event(
        id="evt-imm-1",
        project_id="proj-immutable",
        type=EventType.PROJECT_CREATED,
        payload=original_payload,
        at=now,
    )

    with pytest.raises((ValidationError, TypeError)):
        event.type = "TAMPERED_TYPE"  # type: ignore[misc]

    event_store.append(event)

    # Attempt mutation of original_payload and event.payload right after append (kills X6a)
    original_payload["name"] = "MUTATED_AFTER_APPEND"
    original_payload["nested"].append("v2")
    event.payload["name"] = "MUTATED_VIA_EVENT_REF"

    # Attempt in-place mutation of retrieved event payload (kills X6b)
    fetched_first = event_store.get_events("proj-immutable")
    assert fetched_first[0].payload == {"name": "ImmutableApp", "nested": ["v1"]}
    fetched_first[0].payload["name"] = "HACKED_NAME"

    fetched_second = event_store.get_events("proj-immutable")
    assert fetched_second[0].payload == {"name": "ImmutableApp", "nested": ["v1"]}
