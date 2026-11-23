"""Interviewer agent for gathering complete project Spec (Section 7.3, AC-1.2)."""

import json
import logging
import re
from collections.abc import Sequence
from typing import Any

from pydantic import BaseModel, Field, model_validator

from agent_factory.domain.models import Spec
from agent_factory.ports.llm import LLM, InvalidLLMResponseError

logger = logging.getLogger(__name__)

MAX_QUESTIONS_PER_ROUND: int = 3

_BULLET_PREFIX_RE = re.compile(r"^\s*(?:[-*\u2022]+|\d+[.)])\s*")
_SPEC_FIELD_ALIASES: dict[str, tuple[str, ...]] = {
    "objective": ("objective", "objetivo", "goal", "summary", "resumen", "title", "titulo"),
    "users": ("users", "usuarios", "target_users", "audience", "publico"),
    "features": ("features", "caracteristicas", "funcionalidades"),
    "business_rules": ("business_rules", "reglas_de_negocio", "reglas"),
    "edge_cases": ("edge_cases", "casos_borde", "casos_limite"),
    "constraints": ("constraints", "restricciones", "limitaciones"),
    "success_criteria": ("success_criteria", "criterios_de_exito", "criterios"),
    "assumptions": ("assumptions", "supuestos", "asunciones"),
}
_QUESTION_KEYS: tuple[str, ...] = (
    "questions",
    "preguntas",
    "clarifying_questions",
    "next_questions",
    "follow_up_questions",
)
_QUESTION_DICT_KEYS: tuple[str, ...] = (
    "question",
    "pregunta",
    "text",
    "texto",
    "label",
    "prompt",
    "content",
    "title",
)
_PROSE_FALLBACK_KEYS: tuple[str, ...] = ("reply", "content", "text", "message", "respuesta")


def _coerce_str_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        parts = re.split(r"[,;\r\n]+", value)
        return [
            _BULLET_PREFIX_RE.sub("", part).strip()
            for part in parts
            if _BULLET_PREFIX_RE.sub("", part).strip()
        ]
    if isinstance(value, (list, tuple, set)):
        items: list[str] = []
        for item in value:
            if isinstance(item, str):
                cleaned = _BULLET_PREFIX_RE.sub("", item).strip()
                if cleaned:
                    items.append(cleaned)
            elif isinstance(item, dict):
                picked = ""
                for key in (*_QUESTION_DICT_KEYS, "name", "description", "value"):
                    raw = item.get(key)
                    if isinstance(raw, str) and raw.strip():
                        picked = raw.strip()
                        break
                if picked:
                    items.append(picked)
            elif item is not None:
                text = str(item).strip()
                if text:
                    items.append(text)
        return items
    text = str(value).strip()
    return [text] if text else []


def _extract_questions_from_value(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        lines = [_BULLET_PREFIX_RE.sub("", ln).strip() for ln in value.splitlines()]
        q_lines = [ln for ln in lines if ln and ("?" in ln or "¿" in ln)]
        return q_lines[:MAX_QUESTIONS_PER_ROUND]
    if isinstance(value, (list, tuple)):
        questions: list[str] = []
        for item in value:
            if isinstance(item, str):
                cleaned = _BULLET_PREFIX_RE.sub("", item).strip()
                if cleaned:
                    questions.append(cleaned)
            elif isinstance(item, dict):
                for key in _QUESTION_DICT_KEYS:
                    raw = item.get(key)
                    if isinstance(raw, str) and raw.strip():
                        questions.append(raw.strip())
                        break
            if len(questions) >= MAX_QUESTIONS_PER_ROUND:
                break
        return questions[:MAX_QUESTIONS_PER_ROUND]
    return []


def _try_parse_json_payload(raw_text: str) -> Any:
    stripped = raw_text.strip()
    if not stripped:
        return None
    try:
        return json.loads(stripped)
    except (json.JSONDecodeError, ValueError):
        pass
    decoder = json.JSONDecoder()
    for idx, ch in enumerate(stripped):
        if ch not in "{[":
            continue
        try:
            parsed, _ = decoder.raw_decode(stripped[idx:])
            if isinstance(parsed, (dict, list)):
                return parsed
        except (json.JSONDecodeError, ValueError):
            continue
    return None


def _build_fallback_questions(spec: Spec, user_message: str) -> list[str]:
    goal = (spec.objective or user_message or "tu proyecto").strip()
    if len(goal) > 100:
        goal = goal[:97].rstrip() + "..."
    missing = set(spec.missing_fields())
    candidates: list[str] = []
    if "users" in missing or not spec.users:
        candidates.append(
            f"¿Quiénes serán los usuarios principales que utilizarán {goal} y desde qué dispositivos?"
        )
    if "features" in missing or not spec.features:
        candidates.append(
            f"¿Qué secciones o funcionalidades principales debe incluir {goal}?"
        )
    if "constraints" in missing or not spec.constraints:
        candidates.append(
            f"¿Tienes preferencias de diseño, colores, tipografía o restricciones técnicas para {goal}?"
        )
    if "business_rules" in missing or not spec.business_rules:
        candidates.append(
            "¿Existe alguna regla específica o flujo clave que deba cumplirse en el uso diario?"
        )
    if "success_criteria" in missing or not spec.success_criteria:
        candidates.append(
            "¿Qué criterio principal confirmará que el resultado final quedó listo para usarse?"
        )
    if not candidates:
        candidates.append(
            f"¿Qué detalle adicional te gustaría priorizar para completar {goal}?"
        )
    return candidates[:MAX_QUESTIONS_PER_ROUND]


def _extract_normalized_round_fields(
    data: dict[str, Any],
) -> tuple[dict[str, Any], list[str], bool]:
    """Normalize nested/flat/Spanish/prose round payloads into (spec_updates, questions, has_spec_fields)."""
    payload: dict[str, Any] = data
    for wrapper_key in ("data", "result", "output"):
        inner = payload.get(wrapper_key)
        if isinstance(inner, dict) and any(
            k in inner for k in ("spec", "objective", "objetivo", *_QUESTION_KEYS)
        ):
            payload = inner
            break

    raw_spec_obj = payload.get("spec")
    if isinstance(raw_spec_obj, Spec):
        spec_updates = raw_spec_obj.model_dump(mode="python")
        has_explicit_field = True
    else:
        spec_raw = raw_spec_obj if isinstance(raw_spec_obj, dict) else payload
        spec_updates = {}
        has_explicit_field = False
        for target_field, aliases in _SPEC_FIELD_ALIASES.items():
            found = False
            raw_val: Any = None
            for alias in aliases:
                if alias in spec_raw:
                    raw_val = spec_raw[alias]
                    found = True
                    break
            if not found:
                continue
            has_explicit_field = True
            if target_field == "objective":
                if isinstance(raw_val, str):
                    spec_updates["objective"] = raw_val.strip()
                elif raw_val is not None:
                    spec_updates["objective"] = str(raw_val).strip()
            else:
                spec_updates[target_field] = _coerce_str_list(raw_val)

    questions: list[str] = []
    for q_key in _QUESTION_KEYS:
        if q_key in payload:
            questions = _extract_questions_from_value(payload.get(q_key))
            if questions:
                break
    if not questions and isinstance(raw_spec_obj, dict):
        for q_key in _QUESTION_KEYS:
            if q_key in raw_spec_obj:
                questions = _extract_questions_from_value(raw_spec_obj.get(q_key))
                if questions:
                    break
    if not questions:
        for prose_key in _PROSE_FALLBACK_KEYS:
            raw_prose = payload.get(prose_key)
            if isinstance(raw_prose, str) and raw_prose.strip():
                questions = _extract_questions_from_value(raw_prose)
                if questions:
                    break

    return spec_updates, questions[:MAX_QUESTIONS_PER_ROUND], has_explicit_field


def _coerce_interview_payload(
    response: Any,
    *,
    current_spec: Spec,
    user_message: str,
) -> tuple[Spec, list[str]]:
    if isinstance(response, str):
        parsed = _try_parse_json_payload(response)
        if parsed is None:
            questions = _extract_questions_from_value(response)
            obj = current_spec.objective or user_message.strip()
            spec = current_spec.model_copy(update={"objective": obj})
            return spec, questions
        response = parsed

    if isinstance(response, list):
        obj = current_spec.objective or user_message.strip()
        spec = current_spec.model_copy(update={"objective": obj})
        return spec, _extract_questions_from_value(response)

    if isinstance(response, InterviewRoundOutput):
        spec = response.spec
        if not spec.objective and (current_spec.objective or user_message.strip()):
            spec = spec.model_copy(
                update={"objective": current_spec.objective or user_message.strip()}
            )
        questions = [str(q).strip() for q in response.questions if str(q).strip()][
            :MAX_QUESTIONS_PER_ROUND
        ]
        return spec, questions

    if isinstance(response, dict):
        spec_updates, questions, has_explicit_field = _extract_normalized_round_fields(response)
        if has_explicit_field:
            spec = Spec.model_validate(spec_updates)
            if not spec.objective and (current_spec.objective or user_message.strip()):
                spec = spec.model_copy(
                    update={"objective": current_spec.objective or user_message.strip()}
                )
            return spec, questions
        obj = current_spec.objective or user_message.strip()
        spec = current_spec.model_copy(update={"objective": obj}) if not current_spec.objective else current_spec
        return spec, questions

    obj = current_spec.objective or user_message.strip()
    return current_spec.model_copy(update={"objective": obj}), []


class InterviewRoundOutput(BaseModel):
    """Structured response expected from the LLM during an interview round."""

    spec: Spec = Field(default_factory=Spec)
    questions: list[str] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def _unwrap_if_flat_spec(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        if "spec" in data and not isinstance(data.get("spec"), (dict, Spec)):
            return data
        spec_updates, questions, _ = _extract_normalized_round_fields(data)
        return {"spec": spec_updates, "questions": questions}


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

        try:
            response = self.llm.complete(messages=messages, schema=InterviewRoundOutput)
            new_spec, questions = _coerce_interview_payload(
                response,
                current_spec=current_spec,
                user_message=user_message,
            )
        except (InvalidLLMResponseError, ValueError, TypeError) as exc:
            logger.warning(
                "Interviewer falling back to objective-based questions after invalid response format: %s",
                exc,
            )
            fallback_obj = current_spec.objective.strip() or user_message.strip() or "tu proyecto"
            new_spec = current_spec.model_copy(update={"objective": fallback_obj})
            questions = _build_fallback_questions(new_spec, user_message)

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

        if not questions:
            questions = _build_fallback_questions(new_spec, user_message)

        return InterviewRoundResult(
            spec=new_spec,
            questions=questions[:MAX_QUESTIONS_PER_ROUND],
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
