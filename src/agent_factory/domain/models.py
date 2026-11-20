"""Pydantic v2 data contracts for the Agent Factory domain (Section 5)."""

import hashlib
import json
import re
from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, Field, field_validator

from agent_factory.domain.states import ProjectState

PROJECT_ID_REGEX: re.Pattern[str] = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}\Z")


def validate_project_id(project_id: str) -> str:
    """Validate project_id strictly according to INV-22 (AC-1.12, F-05)."""
    if not isinstance(project_id, str) or not PROJECT_ID_REGEX.fullmatch(project_id):
        raise ValueError(
            f"Invalid project_id '{project_id}': must match ^[a-z0-9][a-z0-9-]{{0,62}}$ "
            "(lowercase alphanumeric and hyphens, 1-63 chars, start with alphanumeric)"
        )
    return project_id

VALID_TEMPLATES: frozenset[str] = frozenset({"static-app", "static-web"})

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
        missing: list[str] = []
        for field in SPEC_CHECKLIST_FIELDS:
            val = getattr(self, field)
            if (isinstance(val, str) and not val.strip()) or (isinstance(val, list) and len(val) == 0):
                missing.append(field)
        return missing

    def is_complete(self) -> bool:
        """Deterministic completeness check across all required Spec fields."""
        return len(self.missing_fields()) == 0

    def fill_gaps_with_assumptions(self) -> "Spec":
        """Fill missing checklist fields with explicit assumptions (Section 7.3)."""
        updated_data = self.model_dump()
        assumptions = list(self.assumptions)
        missing = self.missing_fields()

        for field in missing:
            assumption_text = f"Explicit assumption for {field}: standard defaults applied."
            assumptions.append(assumption_text)
            updated_data[field] = (
                f"Standard default for {field}" if field == "objective" else [f"Standard default for {field}"]
            )

        updated_data["assumptions"] = assumptions
        return Spec.model_validate(updated_data)


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
        data = self.model_dump(mode="json")
        canonical_json = json.dumps(data, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()


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
    progress_pct: int = Field(default=0, ge=0, le=100)
    plan_hash: str | None = None
    tests: TestCounts = Field(default_factory=TestCounts)
    agent_invocations: int = 0
    ci_rounds: int = 0
    links: ProjectLinks = Field(default_factory=ProjectLinks)
    workspace_path: str | None = None
    last_event: str | None = None
    updated_at: datetime | None = None
    failure_reason: str | None = None

    @field_validator("project_id")
    @classmethod
    def _validate_project_id(cls, v: str) -> str:
        return validate_project_id(v)


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
