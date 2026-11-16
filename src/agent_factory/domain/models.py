"""Pydantic v2 data contracts for the Agent Factory domain (Section 5)."""

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, Field

from agent_factory.domain.states import ProjectState

VALID_TEMPLATES: frozenset[str] = frozenset({"static-app"})

SPEC_CHECKLIST_FIELDS: tuple[str, ...] = (
    "objective",
    "users",
    "features",
    "business_rules",
    "edge_cases",
    "constraints",
    "success_criteria",
)


class Spec(BaseModel):
    """Project specification gathered during INTERVIEW (Section 5)."""

    objective: str = ""
    users: list[str] = Field(default_factory=list)
    features: list[str] = Field(default_factory=list)
    business_rules: list[str] = Field(default_factory=list)
    edge_cases: list[str] = Field(default_factory=list)
    constraints: list[str] = Field(default_factory=list)
    success_criteria: list[str] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)

    def missing_fields(self) -> list[str]:
        """Return list of checklist field names that are still empty."""
        raise NotImplementedError

    def is_complete(self) -> bool:
        """Deterministic completeness check across all required Spec fields."""
        raise NotImplementedError

    def fill_gaps_with_assumptions(self) -> "Spec":
        """Fill missing checklist fields with explicit assumptions (Section 7.3)."""
        raise NotImplementedError


class AcceptanceTestType(StrEnum):
    """Allowed types for acceptance tests."""

    UNIT = "unit"
    INTEGRATION = "integration"
    E2E = "e2e"


class AcceptanceTest(BaseModel):
    """Acceptance test specification in a Plan (Section 5)."""

    id: str
    description: str
    type: AcceptanceTestType


class FileSpec(BaseModel):
    """Contract specification for a single file in a Plan (Section 5)."""

    path: str
    purpose: str
    contract: str
    depends_on: list[str] = Field(default_factory=list)


class Plan(BaseModel):
    """Master Plan output by the Mediator (Section 5)."""

    summary: str
    rationale: str
    template_id: str
    files: list[FileSpec] = Field(default_factory=list)
    acceptance_tests: list[AcceptanceTest] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)
    deployment_target: str = "github-pages"

    def compute_hash(self) -> str:
        """Compute deterministic SHA-256 hash of this Plan (INV-05)."""
        raise NotImplementedError


class ApprovalEvent(BaseModel):
    """Human approval record tied to a specific plan_hash (INV-05, Section 5)."""

    plan_hash: str
    timestamp: datetime
    decision: str = "approved"


class TestCounts(BaseModel):
    """Summary counts of acceptance test execution."""

    total: int = 0
    passed: int = 0
    failed: int = 0


class ProjectLinks(BaseModel):
    """External links for repository and live deployment."""

    repo: str | None = None
    deploy: str | None = None


class ProjectStatus(BaseModel):
    """Materialized projection of project state from the event log (INV-11, Section 5)."""

    project_id: str
    name: str = ""
    objective: str = ""
    state: ProjectState = ProjectState.IDLE
    phase: str = "IDLE"
    progress_pct: int = 0
    plan_hash: str | None = None
    tests: TestCounts = Field(default_factory=TestCounts)
    agent_invocations: int = 0
    ci_rounds: int = 0
    links: ProjectLinks = Field(default_factory=ProjectLinks)
    last_event: str | None = None
    updated_at: datetime | None = None
    failure_reason: str | None = None


class BuildReport(BaseModel):
    """Report of build execution and verification (Section 5)."""

    invocations: int = 0
    test_results: dict[str, bool] = Field(default_factory=dict)
    ci_rounds: int = 0
    failure_cause: str | None = None


class ProjectPrompt(BaseModel):
    """Derived living prompt document for a generated project (Section 5)."""

    project_id: str
    plan_hash: str
    content: str
