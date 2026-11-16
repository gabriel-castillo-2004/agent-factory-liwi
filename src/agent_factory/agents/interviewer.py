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
        messages = [
            {
                "role": "system",
                "content": (
                    "You are an expert product interviewer. Interview the user to specify a software project. "
                    "Ask at most 3 clear questions per round focusing on missing specification areas."
                ),
            },
            {
                "role": "user",
                "content": (
                    f"Current Spec:\n{current_spec.model_dump_json()}\n\n"
                    f"User Message:\n{user_message}"
                ),
            },
        ]

        response = self.llm.complete(messages=messages, schema=None)
        spec_data = response.get("spec", response)
        questions = list(response.get("questions", []))[:MAX_QUESTIONS_PER_ROUND]
        new_spec = Spec.model_validate(spec_data)

        if new_spec.is_complete():
            return InterviewRoundResult(
                spec=new_spec,
                questions=[],
                is_complete=True,
                terminated_by_limit=False,
                rounds_used=round_number,
            )

        if round_number >= self.max_rounds:
            completed_spec = new_spec.fill_gaps_with_assumptions()
            return InterviewRoundResult(
                spec=completed_spec,
                questions=[],
                is_complete=True,
                terminated_by_limit=True,
                rounds_used=round_number,
            )

        return InterviewRoundResult(
            spec=new_spec,
            questions=questions,
            is_complete=False,
            terminated_by_limit=False,
            rounds_used=round_number,
        )

    def conduct_interview(
        self,
        initial_message: str,
        user_replies: Sequence[str] = (),
    ) -> InterviewRoundResult:
        """Run multi-round interview until Spec checklist is complete or max_rounds reached."""
        current_spec = Spec()
        all_messages = [initial_message, *user_replies]
        last_result: InterviewRoundResult | None = None

        for idx, msg in enumerate(all_messages, start=1):
            last_result = self.run_round(current_spec, msg, round_number=idx)
            current_spec = last_result.spec
            if last_result.is_complete or idx >= self.max_rounds:
                break

        assert last_result is not None
        return last_result
