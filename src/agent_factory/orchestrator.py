"""State machine orchestrator, approval gate, and circuit breaker (INV-05, INV-17)."""

import tomllib
from collections.abc import Mapping
from collections.abc import Set as AbstractSet
from datetime import UTC, datetime
from pathlib import Path

from agent_factory.agents.interviewer import Interviewer
from agent_factory.agents.mediator import PlanValidationError, run_debate_pipeline
from agent_factory.domain.events import EventType, create_event
from agent_factory.domain.models import (
    VALID_TEMPLATES,
    ApprovalEvent,
    Plan,
    ProjectStatus,
    Spec,
    validate_project_id,
)
from agent_factory.domain.progress import project_status_from_events
from agent_factory.domain.states import IllegalTransition, ProjectState, validate_transition
from agent_factory.inbox import Inbox
from agent_factory.ports.clock import Clock
from agent_factory.ports.llm import LLM, InvalidLLMResponseError
from agent_factory.ports.publisher import StatusPublisher
from agent_factory.ports.store import EventStore


class ApprovalError(Exception):
    """Raised when attempting to enter BUILDING without a valid ApprovalEvent (INV-05)."""


class CircuitBreakerTrippedError(Exception):
    """Raised when a project exceeds the per-state call budget in config/limits.toml (INV-17)."""

    def __init__(self, project_id: str, state: ProjectState, calls: int, limit: int) -> None:
        self.project_id = project_id
        self.state = state
        self.calls = calls
        self.limit = limit
        super().__init__(
            f"Circuit breaker tripped for project '{project_id}' in state {state}: "
            f"{calls} calls exceeded limit of {limit}"
        )


def load_limits(path: Path | str = Path("config/limits.toml")) -> dict[str, int]:
    """Load per-state call limits and CI round limits from config/limits.toml (INV-17)."""
    p = Path(path)
    if not p.is_file():
        raise FileNotFoundError(f"Limits config file not found: {p}")
    with p.open("rb") as f:
        data = tomllib.load(f)
    return {str(k): int(v) for k, v in data.items()}


class CircuitBreaker:
    """Enforces hard per-project, per-state call budgets (INV-17, Section 7.8)."""

    def __init__(
        self,
        limits: Mapping[str, int] | None = None,
        config_path: Path | str = Path("config/limits.toml"),
    ) -> None:
        self.config_path = Path(config_path)
        if limits is not None:
            self._limits = dict(limits)
        else:
            self._limits = load_limits(self.config_path)
        self._counts: dict[tuple[str, ProjectState], int] = {}

    def get_limit(self, state: ProjectState) -> int | None:
        """Return configured call limit for state, or None if unbounded."""
        key = state.value if isinstance(state, ProjectState) else str(state)
        return self._limits.get(key)

    def get_calls(self, project_id: str, state: ProjectState) -> int:
        """Return number of recorded calls for project_id in state."""
        return self._counts.get((project_id, state), 0)

    def record_call(self, project_id: str, state: ProjectState) -> int:
        """Increment call count and raise CircuitBreakerTrippedError if budget exceeded."""
        limit = self.get_limit(state)
        current = self.get_calls(project_id, state)
        if limit is not None and current >= limit:
            raise CircuitBreakerTrippedError(project_id, state, current + 1, limit)
        self._counts[(project_id, state)] = current + 1
        return current + 1

    def reset(self, project_id: str, state: ProjectState | None = None) -> None:
        """Reset call count for a specific state, or all states of project_id."""
        if state is not None:
            self._counts.pop((project_id, state), None)
        else:
            to_remove = [k for k in self._counts if k[0] == project_id]
            for k in to_remove:
                del self._counts[k]


class Orchestrator:
    """Drives project state transitions, debate pipeline, approval gate, and projections."""

    def __init__(
        self,
        store: EventStore,
        llm: LLM,
        publisher: StatusPublisher | None = None,
        inbox: Inbox | None = None,
        circuit_breaker: CircuitBreaker | None = None,
        clock: Clock | None = None,
        valid_templates: AbstractSet[str] = VALID_TEMPLATES,
    ) -> None:
        self.store = store
        self.llm = llm
        self.publisher = publisher
        self.inbox = inbox if inbox is not None else Inbox()
        self.circuit_breaker = (
            circuit_breaker if circuit_breaker is not None else CircuitBreaker()
        )
        self.clock = clock
        self.valid_templates = valid_templates
        self._specs: dict[str, Spec] = {}

    def _now(self) -> datetime:
        return self.clock.now() if self.clock else datetime.now(UTC)

    def _get_spec(self, project_id: str) -> Spec:
        if project_id in self._specs:
            return self._specs[project_id]
        events = self.store.get_events(project_id)
        for e in reversed(events):
            if e.type == EventType.SPEC_UPDATED and "spec" in e.payload:
                spec = Spec.model_validate(e.payload["spec"])
                self._specs[project_id] = spec
                return spec
        return Spec()

    def create_project(
        self,
        project_id: str,
        name: str,
        objective: str = "",
    ) -> ProjectStatus:
        """Create a new project in INTERVIEW state and append initial events."""
        validate_project_id(project_id)
        validate_transition(ProjectState.IDLE, ProjectState.INTERVIEW)
        event = create_event(
            project_id=project_id,
            event_type=EventType.PROJECT_CREATED,
            payload={"name": name, "objective": objective, "state": ProjectState.INTERVIEW},
            at=self._now(),
        )
        self.store.append(event)
        self._specs[project_id] = Spec(objective=objective)
        return self.get_status(project_id)

    def handle_interview_message(
        self,
        project_id: str,
        event_id: str,
        user_message: str,
    ) -> ProjectStatus:
        """Process an incoming user message idempotently during INTERVIEW (INV-16, INV-17)."""
        validate_project_id(project_id)
        events = self.store.get_events(project_id)
        if not events:
            raise ValueError(f"Project '{project_id}' does not exist")

        current_status = self.get_status(project_id)
        if current_status.state != ProjectState.INTERVIEW:
            raise IllegalTransition(current_status.state, ProjectState.INTERVIEW)

        if self.inbox.is_processed(event_id) or any(
            e.payload.get("incoming_event_id") == event_id for e in events
        ):
            self.inbox.mark_processed(event_id)
            return current_status

        self.inbox.mark_processed(event_id)

        try:
            self.circuit_breaker.record_call(project_id, ProjectState.INTERVIEW)
        except CircuitBreakerTrippedError as err:
            fail_evt = create_event(
                project_id=project_id,
                event_type=EventType.PROJECT_FAILED,
                payload={"reason": str(err)},
                at=self._now(),
            )
            self.store.append(fail_evt)
            return self.get_status(project_id)

        try:
            self.store.append(
                create_event(
                    project_id=project_id,
                    event_type=EventType.AGENT_INVOKED,
                    payload={"agent": "interviewer", "incoming_event_id": event_id},
                    at=self._now(),
                )
            )

            current_spec = self._get_spec(project_id)
            interviewer = Interviewer(llm=self.llm)
            round_result = interviewer.run_round(current_spec, user_message)
            self._specs[project_id] = round_result.spec
        except (InvalidLLMResponseError, ValueError, TimeoutError, RuntimeError) as err:
            fail_evt = create_event(
                project_id=project_id,
                event_type=EventType.PROJECT_FAILED,
                payload={"reason": str(err)},
                at=self._now(),
            )
            self.store.append(fail_evt)
            return self.get_status(project_id)

        self.store.append(
            create_event(
                project_id=project_id,
                event_type=EventType.INTERVIEW_ROUND,
                payload={
                    "incoming_event_id": event_id,
                    "questions": round_result.questions,
                    "is_complete": round_result.is_complete,
                    "terminated_by_limit": round_result.terminated_by_limit,
                },
                at=self._now(),
            )
        )
        self.store.append(
            create_event(
                project_id=project_id,
                event_type=EventType.SPEC_UPDATED,
                payload={"spec": round_result.spec.model_dump(mode="json")},
                at=self._now(),
            )
        )

        return self.get_status(project_id)

    def run_debate(self, project_id: str) -> ProjectStatus:
        """Run Pragmatist -> Structuralist -> Mediator pipeline and advance to WAITING_APPROVAL."""
        validate_project_id(project_id)
        events = self.store.get_events(project_id)
        if not events:
            raise ValueError(f"Project '{project_id}' does not exist")

        current_status = self.get_status(project_id)
        validate_transition(current_status.state, ProjectState.DEBATE)

        current_spec = self._get_spec(project_id)
        if not current_spec.is_complete():
            raise ValueError("Spec checklist is incomplete")

        self.circuit_breaker.reset(project_id, ProjectState.DEBATE)
        self.circuit_breaker.reset(project_id, ProjectState.MEDIATION)

        self.store.append(
            create_event(
                project_id=project_id,
                event_type=EventType.STATE_TRANSITIONED,
                payload={"from": current_status.state, "to": ProjectState.DEBATE},
                at=self._now(),
            )
        )

        def _on_llm_call(state: ProjectState) -> None:
            self.circuit_breaker.record_call(project_id, state)

        def _on_transition(from_state: ProjectState, to_state: ProjectState) -> None:
            validate_transition(from_state, to_state)
            self.store.append(
                create_event(
                    project_id=project_id,
                    event_type=EventType.STATE_TRANSITIONED,
                    payload={"from": from_state, "to": to_state},
                    at=self._now(),
                )
            )

        try:
            result = run_debate_pipeline(
                spec=current_spec,
                llm=self.llm,
                valid_templates=self.valid_templates,
                clock=self.clock,
                on_llm_call=_on_llm_call,
                on_transition=_on_transition,
            )
        except (
            InvalidLLMResponseError,
            PlanValidationError,
            ValueError,
            TimeoutError,
            CircuitBreakerTrippedError,
            RuntimeError,
        ) as err:
            self.store.append(
                create_event(
                    project_id=project_id,
                    event_type=EventType.PROJECT_FAILED,
                    payload={"reason": str(err)},
                    at=self._now(),
                )
            )
            return self.get_status(project_id)

        if result.state == ProjectState.FAILED:
            self.store.append(
                create_event(
                    project_id=project_id,
                    event_type=EventType.PROJECT_FAILED,
                    payload={"reason": result.failure_cause or "Debate pipeline failed"},
                    at=self._now(),
                )
            )
            return self.get_status(project_id)

        assert result.plan is not None
        plan = result.plan
        plan_hash = plan.compute_hash()
        validate_transition(ProjectState.MEDIATION, ProjectState.WAITING_APPROVAL)
        self.store.append(
            create_event(
                project_id=project_id,
                event_type=EventType.PLAN_CREATED,
                payload={
                    "plan": plan.model_dump(mode="json"),
                    "plan_hash": plan_hash,
                    "total_tests": len(plan.acceptance_tests),
                    "to": ProjectState.WAITING_APPROVAL,
                },
                at=self._now(),
            )
        )
        return self.get_status(project_id)

    def submit_approval(
        self,
        project_id: str,
        approval: ApprovalEvent | None,
    ) -> ProjectStatus:
        """Validate ApprovalEvent and plan_hash before transitioning to BUILDING (INV-05)."""
        validate_project_id(project_id)
        events = self.store.get_events(project_id)
        if not events:
            raise ValueError(f"Project '{project_id}' does not exist")

        current_status = self.get_status(project_id)
        if current_status.state != ProjectState.WAITING_APPROVAL:
            raise ApprovalError(f"Cannot submit approval in state {current_status.state}")
        if approval is None:
            raise ApprovalError("ApprovalEvent is required to transition to BUILDING (INV-05)")
        if approval.decision != "approved":
            raise ApprovalError(f"Approval rejected or non-approved decision: '{approval.decision}'")
        if not current_status.plan_hash or approval.plan_hash != current_status.plan_hash:
            raise ApprovalError(
                f"Approval plan_hash '{approval.plan_hash}' does not match "
                f"current plan_hash '{current_status.plan_hash}' (INV-05)"
            )

        validate_transition(current_status.state, ProjectState.BUILDING)
        self.store.append(
            create_event(
                project_id=project_id,
                event_type=EventType.APPROVAL_RECORDED,
                payload={
                    "plan_hash": approval.plan_hash,
                    "decision": "approved",
                    "timestamp": approval.timestamp.isoformat(),
                    "to": ProjectState.BUILDING,
                },
                at=self._now(),
            )
        )
        return self.get_status(project_id)

    def record_state_call(
        self,
        project_id: str,
        state: ProjectState,
    ) -> ProjectStatus:
        """Record a call in the given state or transition to FAILED if budget exceeded (INV-17)."""
        validate_project_id(project_id)
        try:
            self.circuit_breaker.record_call(project_id, state)
            self.store.append(
                create_event(
                    project_id=project_id,
                    event_type=EventType.AGENT_INVOKED,
                    payload={"state": state.value},
                    at=self._now(),
                )
            )
        except CircuitBreakerTrippedError as err:
            self.store.append(
                create_event(
                    project_id=project_id,
                    event_type=EventType.PROJECT_FAILED,
                    payload={"reason": str(err)},
                    at=self._now(),
                )
            )
        return self.get_status(project_id)

    def get_status(self, project_id: str) -> ProjectStatus:
        """Project current ProjectStatus from the append-only event log (INV-11)."""
        validate_project_id(project_id)
        events = self.store.get_events(project_id)
        status = project_status_from_events(project_id, events)
        if self.publisher is not None:
            self.publisher.publish(status)
        return status

    def get_plan(self, project_id: str) -> Plan | None:
        """Retrieve the latest validated Plan for project_id from the event log."""
        validate_project_id(project_id)
        for event in reversed(self.store.get_events(project_id)):
            if event.type == EventType.PLAN_CREATED and "plan" in event.payload:
                return Plan.model_validate(event.payload["plan"])
        return None
