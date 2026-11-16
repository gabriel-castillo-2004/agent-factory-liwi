"""Pragmatist agent: step 1 of the fixed 3-step debate pipeline (Section 4, Section 7.4)."""

from pydantic import BaseModel, Field

from agent_factory.domain.models import Spec
from agent_factory.ports.llm import LLM


class PragmatistProposal(BaseModel):
    """Simple, direct architecture proposal produced by Pragmatist."""

    approach: str
    components: list[str] = Field(default_factory=list)
    tradeoffs: list[str] = Field(default_factory=list)


class Pragmatist:
    """Proposes the simplest viable architecture for a Spec."""

    def __init__(self, llm: LLM, max_retries: int = 2) -> None:
        self.llm = llm
        self.max_retries = max_retries

    def propose(self, spec: Spec) -> PragmatistProposal:
        """Generate pragmatic proposal with bounded retries on invalid JSON."""
        raise NotImplementedError
