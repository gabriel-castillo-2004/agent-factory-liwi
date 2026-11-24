"""Append-only event log models (INV-11, Section 5)."""

import uuid
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class EventType(StrEnum):
    """Canonical event types recorded in the append-only log."""

    PROJECT_CREATED = "PROJECT_CREATED"
    PROJECT_RENAMED = "PROJECT_RENAMED"
    STATE_TRANSITIONED = "STATE_TRANSITIONED"
    INTERVIEW_ROUND = "INTERVIEW_ROUND"
    SPEC_UPDATED = "SPEC_UPDATED"
    PRAGMATIST_PROPOSED = "PRAGMATIST_PROPOSED"
    STRUCTURALIST_CRITIQUED = "STRUCTURALIST_CRITIQUED"
    PLAN_CREATED = "PLAN_CREATED"
    APPROVAL_RECORDED = "APPROVAL_RECORDED"
    TESTS_REUSED = "TESTS_REUSED"
    TESTS_UPDATED = "TESTS_UPDATED"
    AGENT_INVOKED = "AGENT_INVOKED"
    CI_ROUND_COMPLETED = "CI_ROUND_COMPLETED"
    PROJECT_FAILED = "PROJECT_FAILED"
    PROJECT_RETRIED = "PROJECT_RETRIED"
    PHASE_ERROR = "PHASE_ERROR"
    PROJECT_DELIVERED = "PROJECT_DELIVERED"
    REGISTRY_PUBLISH_FAILED = "REGISTRY_PUBLISH_FAILED"
    BACKGROUND_OP_STARTED = "BACKGROUND_OP_STARTED"
    BACKGROUND_OP_FINISHED = "BACKGROUND_OP_FINISHED"


class Event(BaseModel):
    """Immutable append-only event record (INV-11, Section 5)."""

    model_config = ConfigDict(frozen=True)

    id: str
    project_id: str
    type: str
    payload: dict[str, Any] = Field(default_factory=dict)
    at: datetime


def create_event(
    project_id: str,
    event_type: str,
    payload: dict[str, Any] | None = None,
    *,
    event_id: str | None = None,
    at: datetime | None = None,
) -> Event:
    """Create a new Event instance."""
    return Event(
        id=event_id or f"evt-{uuid.uuid4().hex[:12]}",
        project_id=project_id,
        type=event_type,
        payload=payload or {},
        at=at or datetime.now(UTC),
    )
