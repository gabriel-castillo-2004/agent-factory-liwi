"""Tests for Interviewer agent (AC-1.2, Section 7.3)."""

from agent_factory.adapters.fake_llm import FakeLLM
from agent_factory.agents.interviewer import MAX_QUESTIONS_PER_ROUND, Interviewer
from agent_factory.domain.models import Spec


def test_interviewer_caps_questions_at_three_per_round() -> None:
    """AC-1.2: Interviewer enforces the maximum of 3 questions per round."""
    fake_llm = FakeLLM(
        responses=[
            {
                "spec": {
                    "objective": "Personal expense tracker PWA",
                    "users": ["Gabriel"],
                    "features": ["Add expense"],
                    "business_rules": [],
                    "edge_cases": [],
                    "constraints": [],
                    "success_criteria": [],
                    "assumptions": [],
                },
                "questions": [
                    "What categories do you need?",
                    "Should expenses support multiple currencies?",
                    "What happens when offline?",
                    "Are there budget alerts?",
                    "How do you measure success?",
                ],
            }
        ]
    )
    interviewer = Interviewer(llm=fake_llm, max_rounds=6)

    result = interviewer.run_round(
        current_spec=Spec(),
        user_message="Quiero una app para registrar mis gastos diarios.",
        round_number=1,
    )

    assert len(result.questions) == MAX_QUESTIONS_PER_ROUND
    assert len(result.questions) <= 3
    assert result.is_complete is False
    assert result.terminated_by_limit is False
    assert result.spec.objective == "Personal expense tracker PWA"


def test_interviewer_finishes_when_spec_checklist_complete() -> None:
    """AC-1.2: Interviewer finishes immediately when Spec checklist is complete."""
    complete_spec_payload = {
        "spec": {
            "objective": "Pomodoro timer static app",
            "users": ["Individual remote worker"],
            "features": ["25-minute work timer", "5-minute break timer", "Session counter"],
            "business_rules": ["Break unlocks only after work timer completes or resets"],
            "edge_cases": ["Tab backgrounded or browser refreshed mid-session"],
            "constraints": ["Static frontend only, zero backend cost"],
            "success_criteria": ["Timer accuracy within 1 second over 25 minutes"],
            "assumptions": [],
        },
        "questions": ["Optional extra question that should be ignored when complete"],
    }
    fake_llm = FakeLLM(
        responses=[
            complete_spec_payload,
            complete_spec_payload,
        ]
    )
    interviewer = Interviewer(llm=fake_llm, max_rounds=6)

    result = interviewer.conduct_interview(
        initial_message="Quiero un reloj Pomodoro completo.",
        user_replies=["Respuesta extra que no debería usarse"],
    )

    assert result.is_complete is True
    assert result.terminated_by_limit is False
    assert result.questions == []
    assert result.rounds_used == 1
    assert len(fake_llm.calls) == 1
    assert result.spec.is_complete() is True
    assert result.spec.missing_fields() == []


def test_interviewer_finishes_on_round_limit_marking_explicit_assumptions() -> None:
    """AC-1.2: Interviewer stops at max_rounds and marks missing Spec fields as assumptions."""
    fake_llm = FakeLLM(
        responses=[
            {
                "spec": {
                    "objective": "Habit tracker",
                    "users": ["Single user"],
                    "features": ["Daily check-in"],
                    "business_rules": [],
                    "edge_cases": [],
                    "constraints": [],
                    "success_criteria": [],
                    "assumptions": [],
                },
                "questions": ["What are the business rules?", "What are the edge cases?"],
            },
            {
                "spec": {
                    "objective": "Habit tracker",
                    "users": ["Single user"],
                    "features": ["Daily check-in"],
                    "business_rules": ["One check-in per habit per calendar day"],
                    "edge_cases": [],
                    "constraints": [],
                    "success_criteria": [],
                    "assumptions": [],
                },
                "questions": ["What are the edge cases?", "What are the constraints?"],
            },
        ]
    )
    interviewer = Interviewer(llm=fake_llm, max_rounds=2)

    result = interviewer.conduct_interview(
        initial_message="Quiero un tracker de hábitos.",
        user_replies=["Solo un check-in por día por hábito."],
    )

    assert result.is_complete is True
    assert result.terminated_by_limit is True
    assert result.rounds_used == 2
    assert result.questions == []
    assert len(result.spec.assumptions) >= 3
    assert any("edge_cases" in assumption for assumption in result.spec.assumptions)
    assert any("constraints" in assumption for assumption in result.spec.assumptions)
    assert any("success_criteria" in assumption for assumption in result.spec.assumptions)
    assert result.spec.is_complete() is True
