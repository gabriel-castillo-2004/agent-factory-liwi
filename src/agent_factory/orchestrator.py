"""State machine orchestrator, approval gate, and circuit breaker (INV-05, INV-17)."""

import threading
import tomllib
from collections.abc import Mapping, Sequence
from collections.abc import Set as AbstractSet
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from agent_factory.adapters.system_clock import SystemClock
from agent_factory.agents.interviewer import Interviewer
from agent_factory.agents.mediator import (
    Mediator,
    PlanValidationError,
    _PipelineLLMWrapper,
    run_debate_pipeline,
)
from agent_factory.agents.pragmatist import PragmatistProposal
from agent_factory.agents.structuralist import StructuralistCritique
from agent_factory.domain.events import Event, EventType, create_event
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
from agent_factory.ports.llm import LLM, LLMError
from agent_factory.ports.publisher import RegistryPublishError, StatusPublisher
from agent_factory.ports.store import EventStore
from agent_factory.secrets import redact_secrets

DEFAULT_STATE_LIMITS: dict[str, int] = {
    "INTERVIEW": 6,
    "DEBATE": 6,
    "MEDIATION": 3,
    "BUILDING": 15,
    "VERIFYING": 3,
    "ci_rounds": 3,
}


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
        config_path: Path | str | None = None,
    ) -> None:
        self._lock = threading.RLock()
        self.config_path = Path(config_path) if config_path is not None else Path("config/limits.toml")
        if limits is not None:
            self._limits = dict(limits)
        elif config_path is not None:
            self._limits = load_limits(self.config_path)
        else:
            base = (
                load_limits(self.config_path)
                if self.config_path.is_file()
                else dict(DEFAULT_STATE_LIMITS)
            )
            base["DEBATE"] = max(int(base.get("DEBATE", 6)), 6)
            base["MEDIATION"] = max(int(base.get("MEDIATION", 3)), 3)
            self._limits = base
        self._counts: dict[tuple[str, ProjectState], int] = {}

    def get_limit(self, state: ProjectState) -> int | None:
        """Return configured call limit for state, or None if unbounded."""
        key = state.value if isinstance(state, ProjectState) else str(state)
        with self._lock:
            return self._limits.get(key)

    def get_calls(self, project_id: str, state: ProjectState) -> int:
        """Return number of recorded calls for project_id in state."""
        with self._lock:
            return self._counts.get((project_id, state), 0)

    def record_call(self, project_id: str, state: ProjectState) -> int:
        """Increment call count and raise CircuitBreakerTrippedError if budget exceeded."""
        with self._lock:
            limit = self.get_limit(state)
            current = self.get_calls(project_id, state)
            if limit is not None and current >= limit:
                raise CircuitBreakerTrippedError(project_id, state, current + 1, limit)
            self._counts[(project_id, state)] = current + 1
            return current + 1

    def reset(self, project_id: str, state: ProjectState | None = None) -> None:
        """Reset call count for a specific state, or all states of project_id."""
        with self._lock:
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
        self.clock = clock if clock is not None else SystemClock()
        self.valid_templates = valid_templates
        self._lock = threading.RLock()
        self._project_locks: dict[str, threading.RLock] = {}
        self._specs: dict[str, Spec] = {}
        self._last_published_status: dict[str, tuple[object, ...]] = {}

    def _project_lock(self, project_id: str) -> threading.RLock:
        with self._lock:
            lock = self._project_locks.get(project_id)
            if lock is None:
                lock = threading.RLock()
                self._project_locks[project_id] = lock
            return lock

    def _now(self) -> datetime:
        return self.clock.now() if self.clock else datetime.now(UTC)

    def _get_spec(self, project_id: str) -> Spec:
        events = self.store.get_events(project_id)
        spec: Spec | None = None
        initial_objective = ""
        for e in events:
            if e.type == EventType.PROJECT_CREATED:
                initial_objective = e.payload.get("objective", "")
            elif e.type == EventType.SPEC_UPDATED and "spec" in e.payload:
                spec = Spec.model_validate(e.payload["spec"])
        if spec is not None:
            if not spec.objective and initial_objective:
                spec.objective = initial_objective
            with self._lock:
                self._specs[project_id] = spec
            return spec
        fallback = Spec(objective=initial_objective)
        with self._lock:
            self._specs[project_id] = fallback
        return fallback

    def get_spec(self, project_id: str) -> Spec:
        """Retrieve the current Spec reconstructed from the event log."""
        validate_project_id(project_id)
        return self._get_spec(project_id)

    def record_phase_failure(
        self,
        project_id: str,
        *,
        failed_state: ProjectState,
        error: BaseException | str,
        agent: str = "",
        extra_secrets: Sequence[str] = (),
    ) -> ProjectStatus:
        """Record redacted PHASE_ERROR detail event and transition project cleanly to FAILED."""
        validate_project_id(project_id)
        err_type = type(error).__name__ if isinstance(error, BaseException) else "Error"
        raw_msg = str(error).strip() or err_type
        clean_msg = redact_secrets(raw_msg, extra_secrets=extra_secrets)

        self.store.append(
            create_event(
                project_id=project_id,
                event_type=EventType.PHASE_ERROR,
                payload={
                    "state": failed_state.value if isinstance(failed_state, ProjectState) else str(failed_state),
                    "agent": agent,
                    "error_type": err_type,
                    "detail": clean_msg,
                },
                at=self._now(),
            )
        )

        current_status = self.get_status(project_id)
        if current_status.state != ProjectState.FAILED:
            validate_transition(current_status.state, ProjectState.FAILED)
            self.store.append(
                create_event(
                    project_id=project_id,
                    event_type=EventType.PROJECT_FAILED,
                    payload={
                        "reason": clean_msg,
                        "failed_from": current_status.state.value,
                        "error_type": err_type,
                    },
                    at=self._now(),
                )
            )
        return self.get_status(project_id)

    def create_project(
        self,
        project_id: str,
        name: str,
        objective: str = "",
    ) -> ProjectStatus:
        """Create a new project in INTERVIEW state and append initial events."""
        validate_project_id(project_id)
        with self._project_lock(project_id):
            existing_events = self.store.get_events(project_id)
            if existing_events:
                current_status = self.get_status(project_id)
                validate_transition(current_status.state, ProjectState.INTERVIEW)
                raise ValueError(f"Project '{project_id}' already exists")
            validate_transition(ProjectState.IDLE, ProjectState.INTERVIEW)
            event = create_event(
                project_id=project_id,
                event_type=EventType.PROJECT_CREATED,
                payload={"name": name, "objective": objective, "state": ProjectState.INTERVIEW},
                at=self._now(),
            )
            self.store.append(event)
            with self._lock:
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
        with self._project_lock(project_id):
            events = self.store.get_events(project_id)
            if not events:
                raise ValueError(f"Project '{project_id}' does not exist")

            current_status = self.get_status(project_id)

            if self.inbox.is_processed(event_id) or any(
                e.payload.get("incoming_event_id") == event_id for e in events
            ):
                self.inbox.mark_processed(event_id)
                return current_status

            if current_status.state != ProjectState.INTERVIEW:
                raise IllegalTransition(current_status.state, ProjectState.INTERVIEW)

            self.inbox.mark_processed(event_id)

            try:
                self.circuit_breaker.record_call(project_id, ProjectState.INTERVIEW)
            except CircuitBreakerTrippedError as err:
                return self.record_phase_failure(
                    project_id,
                    failed_state=current_status.state,
                    error=err,
                    agent="circuit_breaker",
                )

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
                with self._lock:
                    self._specs[project_id] = round_result.spec
            except (
                LLMError,
                ValueError,
                TypeError,
                LookupError,
                TimeoutError,
                OSError,
                RuntimeError,
            ) as err:
                return self.record_phase_failure(
                    project_id,
                    failed_state=current_status.state,
                    error=err,
                    agent="interviewer",
                )

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

    def _latest_debate_artifacts(
        self, events: Sequence[Event]
    ) -> tuple[PragmatistProposal | None, StructuralistCritique | None]:
        proposal: PragmatistProposal | None = None
        critique: StructuralistCritique | None = None
        for ev in events:
            if ev.type == EventType.PRAGMATIST_PROPOSED and "proposal" in ev.payload:
                try:
                    proposal = PragmatistProposal.model_validate(ev.payload["proposal"])
                except ValueError:
                    proposal = None
            elif ev.type == EventType.STRUCTURALIST_CRITIQUED and "critique" in ev.payload:
                try:
                    critique = StructuralistCritique.model_validate(ev.payload["critique"])
                except ValueError:
                    critique = None
        if proposal is not None and critique is not None:
            return proposal, critique
        return None, None

    def run_debate(self, project_id: str) -> ProjectStatus:
        """Run Pragmatist -> Structuralist -> Mediator pipeline and advance to WAITING_APPROVAL."""
        validate_project_id(project_id)
        with self._project_lock(project_id):
            events = self.store.get_events(project_id)
            if not events:
                raise ValueError(f"Project '{project_id}' does not exist")

            current_status = self.get_status(project_id)
            resuming_debate = current_status.state == ProjectState.DEBATE
            resuming_mediation = current_status.state == ProjectState.MEDIATION

            if not (resuming_debate or resuming_mediation):
                validate_transition(current_status.state, ProjectState.DEBATE)

            current_spec = self._get_spec(project_id)
            if not current_spec.is_complete():
                raise ValueError("Spec checklist is incomplete")

            if not (resuming_debate or resuming_mediation):
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

            def _on_debate_complete(
                proposal: PragmatistProposal,
                critique: StructuralistCritique,
            ) -> None:
                self.store.append(
                    create_event(
                        project_id=project_id,
                        event_type=EventType.PRAGMATIST_PROPOSED,
                        payload={"proposal": proposal.model_dump(mode="json")},
                        at=self._now(),
                    )
                )
                self.store.append(
                    create_event(
                        project_id=project_id,
                        event_type=EventType.STRUCTURALIST_CRITIQUED,
                        payload={"critique": critique.model_dump(mode="json")},
                        at=self._now(),
                    )
                )

            plan: Plan | None = None
            try:
                if resuming_mediation:
                    stored_proposal, stored_critique = self._latest_debate_artifacts(events)
                    if stored_proposal is not None and stored_critique is not None:
                        med_wrapper = _PipelineLLMWrapper(inner=self.llm, on_call=_on_llm_call)
                        med_wrapper.current_state = ProjectState.MEDIATION
                        mediator = Mediator(
                            llm=med_wrapper,
                            valid_templates=self.valid_templates,
                            clock=self.clock,
                        )
                        plan = mediator.mediate(current_spec, stored_proposal, stored_critique)
                    else:
                        result = run_debate_pipeline(
                            spec=current_spec,
                            llm=self.llm,
                            valid_templates=self.valid_templates,
                            clock=self.clock,
                            on_llm_call=_on_llm_call,
                            on_transition=None,
                            on_debate_complete=_on_debate_complete,
                        )
                        if result.state == ProjectState.FAILED:
                            return self.record_phase_failure(
                                project_id,
                                failed_state=ProjectState.MEDIATION,
                                error=result.failure_cause or "Debate pipeline failed",
                                agent="mediator",
                            )
                        plan = result.plan
                else:
                    result = run_debate_pipeline(
                        spec=current_spec,
                        llm=self.llm,
                        valid_templates=self.valid_templates,
                        clock=self.clock,
                        on_llm_call=_on_llm_call,
                        on_transition=_on_transition,
                        on_debate_complete=_on_debate_complete,
                    )
                    if result.state == ProjectState.FAILED:
                        cur_state = self.get_status(project_id).state
                        failed_in = (
                            cur_state
                            if cur_state in (ProjectState.DEBATE, ProjectState.MEDIATION)
                            else ProjectState.DEBATE
                        )
                        return self.record_phase_failure(
                            project_id,
                            failed_state=failed_in,
                            error=result.failure_cause or "Debate pipeline failed",
                            agent="mediator" if failed_in == ProjectState.MEDIATION else "debate",
                        )
                    plan = result.plan
            except (
                LLMError,
                PlanValidationError,
                ValueError,
                TypeError,
                LookupError,
                TimeoutError,
                CircuitBreakerTrippedError,
                IllegalTransition,
                OSError,
                RuntimeError,
            ) as err:
                cur_state = self.get_status(project_id).state
                failed_in = (
                    cur_state
                    if cur_state in (ProjectState.DEBATE, ProjectState.MEDIATION)
                    else ProjectState.DEBATE
                )
                return self.record_phase_failure(
                    project_id,
                    failed_state=failed_in,
                    error=err,
                    agent="circuit_breaker"
                    if isinstance(err, CircuitBreakerTrippedError)
                    else ("mediator" if failed_in == ProjectState.MEDIATION else "debate"),
                )

            assert plan is not None
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
        with self._project_lock(project_id):
            events = self.store.get_events(project_id)
            if not events:
                raise ValueError(f"Project '{project_id}' does not exist")

            current_status = self.get_status(project_id)
            if current_status.state != ProjectState.WAITING_APPROVAL:
                raise ApprovalError(f"Cannot submit approval in state {current_status.state}")
            if approval is None:
                raise ApprovalError("ApprovalEvent is required to transition to BUILDING (INV-05)")
            if approval.decision != "approved":
                raise ApprovalError(
                    f"Approval rejected or non-approved decision: '{approval.decision}'"
                )
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

    def request_adjustment(
        self,
        project_id: str,
        reason: str = "",
    ) -> ProjectStatus:
        """Transition a project in WAITING_APPROVAL back to INTERVIEW for plan adjustment (INV-04, INV-05)."""
        validate_project_id(project_id)
        with self._project_lock(project_id):
            current_status = self.get_status(project_id)
            if current_status.state != ProjectState.WAITING_APPROVAL:
                raise IllegalTransition(current_status.state, ProjectState.INTERVIEW)
            validate_transition(current_status.state, ProjectState.INTERVIEW)
            self.store.append(
                create_event(
                    project_id=project_id,
                    event_type=EventType.STATE_TRANSITIONED,
                    payload={
                        "from": current_status.state.value,
                        "to": ProjectState.INTERVIEW.value,
                        "reason": reason or "Adjustment requested by user",
                    },
                    at=self._now(),
                )
            )
            return self.get_status(project_id)

    def retry_project(self, project_id: str) -> ProjectStatus:
        """Reset failure counters for a FAILED project and return it to its recoverable active state."""
        validate_project_id(project_id)
        with self._project_lock(project_id):
            events = self.store.get_events(project_id)
            if not events:
                raise ValueError(f"Project '{project_id}' does not exist")

            current_status = self.get_status(project_id)
            if current_status.state != ProjectState.FAILED:
                raise ValueError(
                    f"Cannot retry project '{project_id}' in state {current_status.state.value}: expected FAILED"
                )

            tracked = ProjectState.IDLE
            last_active_state = ProjectState.INTERVIEW
            latest_plan: Plan | None = None
            adjusted_after_plan = False

            for ev in events:
                pl: Mapping[str, Any] = ev.payload or {}
                if ev.type == EventType.PROJECT_CREATED:
                    tracked = ProjectState(pl.get("state", ProjectState.INTERVIEW))
                    last_active_state = tracked
                elif ev.type == EventType.STATE_TRANSITIONED:
                    nxt = ProjectState(pl["to"])
                    if nxt == ProjectState.INTERVIEW and tracked == ProjectState.WAITING_APPROVAL:
                        adjusted_after_plan = True
                    tracked = nxt
                    if tracked != ProjectState.FAILED:
                        last_active_state = tracked
                elif ev.type == EventType.PLAN_CREATED:
                    if "plan" in pl:
                        try:
                            latest_plan = Plan.model_validate(pl["plan"])
                            adjusted_after_plan = False
                        except ValueError:
                            pass
                    tracked = ProjectState(pl.get("to", ProjectState.WAITING_APPROVAL))
                    last_active_state = tracked
                elif ev.type == EventType.APPROVAL_RECORDED:
                    tracked = ProjectState(pl.get("to", ProjectState.BUILDING))
                    last_active_state = tracked
                elif ev.type == EventType.PROJECT_RETRIED:
                    nxt = ProjectState(pl["to"])
                    tracked = nxt
                    last_active_state = tracked
                elif ev.type == EventType.PHASE_ERROR and "state" in pl:
                    try:
                        candidate = ProjectState(pl["state"])
                        if candidate != ProjectState.FAILED:
                            last_active_state = candidate
                    except ValueError:
                        pass
                elif ev.type == EventType.PROJECT_FAILED:
                    if "failed_from" in pl:
                        try:
                            candidate = ProjectState(pl["failed_from"])
                            if candidate != ProjectState.FAILED:
                                last_active_state = candidate
                        except ValueError:
                            pass
                    tracked = ProjectState.FAILED
                elif ev.type == EventType.PROJECT_DELIVERED:
                    tracked = ProjectState.DELIVERED

            has_valid_plan = (latest_plan is not None and not adjusted_after_plan) or (
                current_status.plan_hash is not None
                and last_active_state
                in {ProjectState.WAITING_APPROVAL, ProjectState.BUILDING, ProjectState.VERIFYING}
            )
            target_state = ProjectState.WAITING_APPROVAL if has_valid_plan else last_active_state

            self.circuit_breaker.reset(project_id, last_active_state)
            if target_state != last_active_state:
                self.circuit_breaker.reset(project_id, target_state)
            if last_active_state in {ProjectState.BUILDING, ProjectState.VERIFYING}:
                self.circuit_breaker.reset(project_id, ProjectState.BUILDING)
                self.circuit_breaker.reset(project_id, ProjectState.VERIFYING)

            validate_transition(current_status.state, target_state, allow_retry=True)
            self.store.append(
                create_event(
                    project_id=project_id,
                    event_type=EventType.PROJECT_RETRIED,
                    payload={
                        "from": ProjectState.FAILED.value,
                        "to": target_state.value,
                        "failed_from": last_active_state.value,
                    },
                    at=self._now(),
                )
            )
            return self.get_status(project_id)

    def delete_project(self, project_id: str) -> bool:
        """Delete project events and cached orchestrator state from the local store."""
        validate_project_id(project_id)
        with self._project_lock(project_id):
            with self._lock:
                self._specs.pop(project_id, None)
                self._last_published_status.pop(project_id, None)
            self.circuit_breaker.reset(project_id)
            return self.store.delete_project(project_id)

    def record_state_call(
        self,
        project_id: str,
        state: ProjectState,
    ) -> ProjectStatus:
        """Record a call in the given state or transition to FAILED if budget exceeded (INV-17)."""
        validate_project_id(project_id)
        with self._project_lock(project_id):
            events = self.store.get_events(project_id)
            if not events:
                raise ValueError(f"Project '{project_id}' does not exist")
            current_status = self.get_status(project_id)
            if current_status.state == ProjectState.FAILED:
                raise IllegalTransition(ProjectState.FAILED, state)
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
                return self.record_phase_failure(
                    project_id,
                    failed_state=current_status.state,
                    error=err,
                    agent="circuit_breaker",
                )
            return self.get_status(project_id)

    def get_status(self, project_id: str) -> ProjectStatus:
        """Project current ProjectStatus from the append-only event log (INV-11)."""
        validate_project_id(project_id)
        events = self.store.get_events(project_id)
        status = project_status_from_events(project_id, events)
        if self.publisher is not None:
            last_key = (
                project_id,
                status.state,
                status.progress_pct,
                status.plan_hash,
                status.failure_reason,
                status.links.repo if status.links else "",
                status.links.deploy if status.links else "",
            )
            should_publish = False
            with self._lock:
                if self._last_published_status.get(project_id) != last_key:
                    self._last_published_status[project_id] = last_key
                    should_publish = True
            if should_publish:
                try:
                    self.publisher.publish(status)
                except (RegistryPublishError, OSError, RuntimeError, ValueError):
                    pass
        return status

    def get_plan(self, project_id: str) -> Plan | None:
        """Retrieve the latest validated Plan for project_id from the event log."""
        validate_project_id(project_id)
        for event in reversed(self.store.get_events(project_id)):
            if event.type == EventType.PLAN_CREATED and "plan" in event.payload:
                return Plan.model_validate(event.payload["plan"])
        return None
