"""State machine orchestrator, approval gate, and circuit breaker (INV-05, INV-17)."""

from collections.abc import Mapping
from collections.abc import Set as AbstractSet
from pathlib import Path

from agent_factory.domain.models import VALID_TEMPLATES, ApprovalEvent, Plan, ProjectStatus
from agent_factory.domain.states import ProjectState
from agent_factory.inbox import Inbox
from agent_factory.ports.clock import Clock
from agent_factory.ports.llm import LLM
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
    raise NotImplementedError


class CircuitBreaker:
    """Enforces hard per-project, per-state call budgets (INV-17, Section 7.8)."""

    def __init__(
        self,
        limits: Mapping[str, int] | None = None,
        config_path: Path | str = Path("config/limits.toml"),
    ) -> None:
        self._limits = dict(limits) if limits is not None else None
        self.config_path = Path(config_path)
        self._counts: dict[tuple[str, ProjectState], int] = {}

    def get_limit(self, state: ProjectState) -> int | None:
        """Return configured call limit for state, or None if unbounded."""
        raise NotImplementedError

    def get_calls(self, project_id: str, state: ProjectState) -> int:
        """Return number of recorded calls for project_id in state."""
        raise NotImplementedError

    def record_call(self, project_id: str, state: ProjectState) -> int:
        """Increment call count and raise CircuitBreakerTrippedError if budget exceeded."""
        raise NotImplementedError


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

    def create_project(
        self,
        project_id: str,
        name: str,
        objective: str = "",
    ) -> ProjectStatus:
        """Create a new project in INTERVIEW state and append initial events."""
        raise NotImplementedError

    def handle_interview_message(
        self,
        project_id: str,
        event_id: str,
        user_message: str,
    ) -> ProjectStatus:
        """Process an incoming user message idempotently during INTERVIEW (INV-16, INV-17)."""
        raise NotImplementedError

    def run_debate(self, project_id: str) -> ProjectStatus:
        """Run Pragmatist -> Structuralist -> Mediator pipeline and advance to WAITING_APPROVAL."""
        raise NotImplementedError

    def submit_approval(
        self,
        project_id: str,
        approval: ApprovalEvent | None,
    ) -> ProjectStatus:
        """Validate ApprovalEvent and plan_hash before transitioning to BUILDING (INV-05)."""
        raise NotImplementedError

    def record_state_call(
        self,
        project_id: str,
        state: ProjectState,
    ) -> ProjectStatus:
        """Record a call in the given state or transition to FAILED if budget exceeded (INV-17)."""
        raise NotImplementedError

    def get_status(self, project_id: str) -> ProjectStatus:
        """Project current ProjectStatus from the append-only event log (INV-11)."""
        raise NotImplementedError

    def get_plan(self, project_id: str) -> Plan | None:
        """Retrieve the latest validated Plan for project_id from the event log."""
        raise NotImplementedError
