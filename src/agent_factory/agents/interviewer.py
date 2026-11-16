"""Interviewer agent for gathering complete project Spec (Section 7.3, AC-1.2)."""

from collections.abc import Sequence

from pydantic import BaseModel, Field

from agent_factory.domain.models import Spec
from agent_factory.ports.llm import LLM

MAX_QUESTIONS_PER_ROUND: int = 3


class InterviewRoundResult(BaseModel):
    """Output of an interview round or complete interview session."""

    spec: Spec
    questions: list[str] = Field(default_factory=list)
    is_complete: bool = False
    terminated_by_limit: bool = False
    rounds_used: int = 0


class Interviewer:
    """Conducts bounded interview rounds to populate Spec (AC-1.2)."""

    def __init__(self, llm: LLM, max_rounds: int = 6) -> None:
        self.llm = llm
        self.max_rounds = max_rounds

    def run_round(
        self,
        current_spec: Spec,
        user_message: str,
        round_number: int = 1,
    ) -> InterviewRoundResult:
        """Process one user message, update Spec, and return at most 3 questions."""
        raise NotImplementedError

    def conduct_interview(
        self,
        initial_message: str,
        user_replies: Sequence[str] = (),
    ) -> InterviewRoundResult:
        """Run multi-round interview until Spec checklist is complete or max_rounds reached."""
        raise NotImplementedError
