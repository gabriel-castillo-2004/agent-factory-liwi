"""Append-only event log models (INV-11, Section 5)."""

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class EventType(StrEnum):
    """Canonical event types recorded in the append-only log."""

    PROJECT_CREATED = "PROJECT_CREATED"
    STATE_TRANSITIONED = "STATE_TRANSITIONED"
    INTERVIEW_ROUND = "INTERVIEW_ROUND"
    SPEC_UPDATED = "SPEC_UPDATED"
    PRAGMATIST_PROPOSED = "PRAGMATIST_PROPOSED"
    STRUCTURALIST_CRITIQUED = "STRUCTURALIST_CRITIQUED"
    PLAN_CREATED = "PLAN_CREATED"
    APPROVAL_RECORDED = "APPROVAL_RECORDED"
    TESTS_UPDATED = "TESTS_UPDATED"
    AGENT_INVOKED = "AGENT_INVOKED"
    CI_ROUND_COMPLETED = "CI_ROUND_COMPLETED"
    PROJECT_FAILED = "PROJECT_FAILED"
    PROJECT_DELIVERED = "PROJECT_DELIVERED"


class Event(BaseModel):
    """Immutable append-only event record (INV-11, Section 5)."""

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
    raise NotImplementedError
