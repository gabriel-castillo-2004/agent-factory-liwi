"""Parent Auditor + 6 Sub-Auditors quality evaluation with adaptive weights (INV-19, INV-22)."""

from __future__ import annotations

import inspect
import json
import logging
import re
import sys
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from agent_factory.domain.models import Plan, Spec
from agent_factory.ports.llm import LLM, InvalidLLMResponseError, LLMError
from agent_factory.ports.runner import CommandTimeoutError
from agent_factory.secrets import redact_secrets
from agent_factory.workspace import (
    collect_workspace_code_context,
    list_workspace_files,
)

logger = logging.getLogger(__name__)

QUALITY_DIMENSION_ORDER: tuple[str, ...] = (
    "ux_ui",
    "functional_completeness",
    "robustness",
    "architecture_quality",
    "security_hygiene",
    "testing_runtime",
)

STRICT_UX_UI_CAP: float = 72.0
STRICT_FUNCTIONAL_CAP: float = 70.0
STRICT_TESTING_CAP: float = 72.0

STRICT_RUBRIC_PROMPT_BLOCK: str = (
    "Reglas estrictas de penalización (topes obligatorios que NO pueden superarse si se incumplen):\n"
    "a) UX/UI <= 72: si la interfaz web dispara llamadas asíncronas (fetch/acciones) sin deshabilitar "
    "el botón, sin indicador visual de carga (spinner/estado activo) o sin comunicar claramente qué "
    "motor/fallback ejecutó la acción, O si el smoke test de navegador reporta console.error, "
    "pageerror, scrollWidth > innerWidth o imágenes rotas (naturalWidth === 0).\n"
    "b) Funcionalidad <= 70: si el código o los generadores producen nombres o salidas con cadenas "
    "plantilla/técnicas ('__', 'placeholder', 'fallback_', 'sample') o si un botón/acción de "
    "'regenerar' puede reutilizar caché sin invalidarla.\n"
    "c) Pruebas <= 72: si las pruebas solo verifican códigos HTTP 200 o cadenas no vacías sin validar "
    "casos borde reales (metadatos ausentes, rotación EXIF, fallos de CLI externo, invalidación de caché)."
)

QUALITY_DIMENSION_META: dict[str, dict[str, str]] = {
    "ux_ui": {
        "label": "UX/UI",
        "focus": (
            "Jerarquía visual, contraste (claro/oscuro), responsividad (360px y 1280px+), "
            "estados de carga/vacío/error y accesibilidad básica (ARIA/foco). "
            "Regla estricta: tope máximo 72 si una UI dispara llamadas asíncronas (fetch/acciones) sin "
            "deshabilitar el botón, sin indicador visual de carga (spinner/estado activo) o sin comunicar "
            "claramente qué motor/fallback ejecutó la acción."
        ),
    },
    "functional_completeness": {
        "label": "Funcionalidad",
        "focus": (
            "Cruce entre cada requisito del Spec y el código real "
            "(sin botones de adorno ni funciones incompletas). "
            "Regla estricta: tope máximo 70 si código/generadores producen nombres o salidas con cadenas "
            "plantilla/técnicas ('__', 'placeholder', 'fallback_', 'sample') o si una acción 'regenerar' "
            "puede reutilizar caché sin invalidarla."
        ),
    },
    "robustness": {
        "label": "Robustez",
        "focus": (
            "Manejo de entradas vacías o inválidas, timeouts, fallos de subprocesos/red "
            "y mensajes de error claros sin tracebacks crudos."
        ),
    },
    "architecture_quality": {
        "label": "Arquitectura",
        "focus": (
            "Separación de responsabilidades, modularidad, ausencia de código muerto "
            "y cumplimiento de linter (ruff)."
        ),
    },
    "security_hygiene": {
        "label": "Seguridad",
        "focus": (
            "Sin credenciales ni rutas locales hardcodeadas (INV-19), validación de rutas "
            "(.. / path traversal), escape HTML/XSS y ejecución segura de comandos."
        ),
    },
    "testing_runtime": {
        "label": "Pruebas",
        "focus": (
            "Cobertura real de comportamiento en pytest (evitando pruebas vacías), "
            "requirements.txt completo y arranque limpio. "
            "Regla estricta: tope máximo 72 si las pruebas solo verifican HTTP 200 o cadenas no vacías "
            "sin validar casos borde reales (metadatos ausentes, rotación EXIF, fallos de CLI externo, invalidación de caché)."
        ),
    },
}

_DIMENSION_ALIASES: dict[str, str] = {
    "ux_ui": "ux_ui",
    "ux": "ux_ui",
    "ui": "ux_ui",
    "ux/ui": "ux_ui",
    "ux_ui_quality": "ux_ui",
    "functional_completeness": "functional_completeness",
    "functional": "functional_completeness",
    "functionality": "functional_completeness",
    "funcionalidad": "functional_completeness",
    "robustness": "robustness",
    "robustez": "robustness",
    "architecture_quality": "architecture_quality",
    "architecture": "architecture_quality",
    "arquitectura": "architecture_quality",
    "security_hygiene": "security_hygiene",
    "security": "security_hygiene",
    "seguridad": "security_hygiene",
    "testing_runtime": "testing_runtime",
    "testing": "testing_runtime",
    "tests": "testing_runtime",
    "pruebas": "testing_runtime",
}

_UI_EXTENSIONS: frozenset[str] = frozenset(
    {".html", ".htm", ".css", ".scss", ".sass", ".jsx", ".tsx", ".vue", ".svelte"}
)
_UI_PY_MARKERS: tuple[str, ...] = (
    "render_template(",
    "htmlresponse(",
    "text/html",
    "<!doctype html",
    "<html",
)
_UI_JS_MARKERS: tuple[str, ...] = (
    "document.getelementbyid",
    "document.queryselector",
    "createelement(",
    "window.",
    "<div",
    "<button",
)
_SECRET_LITERAL_RE = re.compile(
    r"(?:sk-[A-Za-z0-9]{16,}|ghp_[A-Za-z0-9]{20,}|AIza[0-9A-Za-z\-_]{20,}|-----BEGIN (?:RSA |EC )?PRIVATE KEY-----)"
)
_HOST_PATH_RE = re.compile(r"(?:[A-Za-z]:\\\\Users\\\\|C:/Users/|/Users/[A-Za-z0-9._-]+/|/home/[A-Za-z0-9._-]+/)")
_PYTHON_IMPORT_RE = re.compile(r"^\s*(?:from|import)\s+([a-zA-Z_][a-zA-Z0-9_]*)", re.MULTILINE)


class _SubAuditSchemaItem(BaseModel):
    score: float = 85.0
    findings: list[str] = Field(default_factory=list)
    improvements: list[str] = Field(default_factory=list)


class _AllDimensionsSchema(BaseModel):
    ux_ui: _SubAuditSchemaItem = Field(default_factory=_SubAuditSchemaItem)
    functional_completeness: _SubAuditSchemaItem = Field(default_factory=_SubAuditSchemaItem)
    robustness: _SubAuditSchemaItem = Field(default_factory=_SubAuditSchemaItem)
    architecture_quality: _SubAuditSchemaItem = Field(default_factory=_SubAuditSchemaItem)
    security_hygiene: _SubAuditSchemaItem = Field(default_factory=_SubAuditSchemaItem)
    testing_runtime: _SubAuditSchemaItem = Field(default_factory=_SubAuditSchemaItem)


class ParentAuditLLMOutput(BaseModel):
    """Structured schema for ParentAuditor LLM output accepting partial or nested payloads cleanly."""

    model_config = ConfigDict(extra="allow")

    summary: str = ""
    global_score: float | None = None
    dimensions: _AllDimensionsSchema = Field(default_factory=_AllDimensionsSchema)
    objections: list[dict[str, Any]] = Field(default_factory=list)
    resolved_count: int | None = None
    diff_summary: str = ""
    raw_payload: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def _preserve_raw_and_normalize(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        copied = dict(data)
        raw_dims = copied.get("dimensions")
        if not isinstance(raw_dims, dict):
            nested = copied.get("sub_auditors") or copied.get("areas") or copied.get("scores")
            if isinstance(nested, dict):
                raw_dims = nested
            else:
                raw_dims = {}
        norm_dims: dict[str, Any] = {}
        for src in (raw_dims, copied):
            for key, val in src.items():
                canonical = _DIMENSION_ALIASES.get(str(key).strip().lower())
                if not canonical or canonical in norm_dims:
                    continue
                if isinstance(val, (int, float)):
                    norm_dims[canonical] = {"score": float(val), "findings": [], "improvements": []}
                elif isinstance(val, dict):
                    norm_dims[canonical] = {
                        "score": _clamp_score(val.get("score"), default=85.0),
                        "findings": _coerce_str_list(val.get("findings")),
                        "improvements": _coerce_str_list(val.get("improvements")),
                    }
        raw_objs = copied.get("objections")
        norm_objs = [dict(o) for o in raw_objs if isinstance(o, Mapping)] if isinstance(raw_objs, list) else []
        raw_resolved = copied.get("resolved_count")
        resolved_cnt = (
            int(raw_resolved)
            if isinstance(raw_resolved, int) and not isinstance(raw_resolved, bool)
            else None
        )
        out = {
            "summary": str(copied.get("summary") or ""),
            "global_score": (
                _clamp_score(copied["global_score"], default=0.0)
                if isinstance(copied.get("global_score"), (int, float))
                else None
            ),
            "dimensions": norm_dims,
            "objections": norm_objs,
            "resolved_count": resolved_cnt,
            "diff_summary": str(copied.get("diff_summary") or ""),
            "raw_payload": copied,
        }
        return out


class BuilderResponseLLMOutput(BaseModel):
    """Structured schema for BuilderResponder point-by-point debate response."""

    model_config = ConfigDict(extra="allow")

    builder_response: str = ""
    surgical_instructions: str = ""
    fixes: list[dict[str, Any]] = Field(default_factory=list)


_VALID_SEVERITIES: frozenset[str] = frozenset({"high", "medium", "low"})
_SEVERITY_ALIASES: dict[str, str] = {
    "high": "high",
    "alta": "high",
    "critical": "high",
    "critica": "high",
    "crítica": "high",
    "medium": "medium",
    "media": "medium",
    "low": "low",
    "baja": "low",
}


class DebateObjection(BaseModel):
    """Actionable critique objection pointing to a specific dimension and file."""

    dimension: str
    file_path: str = ""
    severity: str = "medium"
    issue: str
    concrete_fix: str = ""
    resolved: bool = False

    @model_validator(mode="before")
    @classmethod
    def _normalize_fields(cls, data: Any) -> Any:
        if not isinstance(data, Mapping):
            return data
        copied = dict(data)
        raw_dim = str(copied.get("dimension") or "").strip().lower()
        copied["dimension"] = _DIMENSION_ALIASES.get(raw_dim, raw_dim or "functional_completeness")
        raw_sev = str(copied.get("severity") or "medium").strip().lower()
        copied["severity"] = _SEVERITY_ALIASES.get(raw_sev, "medium")
        return copied


class DebateRoundRecord(BaseModel):
    """Persistent record of one adversarial Critic vs. Builder debate round."""

    round_number: int = 1
    score_before: float = Field(ge=0.0, le=100.0)
    score_after: float = Field(ge=0.0, le=100.0)
    objections: list[DebateObjection] = Field(default_factory=list)
    builder_response: str = ""
    builder_instructions: str = ""
    resolved_count: int = 0
    kept: bool = True
    diff_summary: str = ""
    discard_reason: str = ""


def summarize_debate_rounds(
    rounds: Sequence[DebateRoundRecord | Mapping[str, Any]],
    *,
    final_global_score: float | None = None,
) -> dict[str, Any] | None:
    """Compute compact summary metrics across adversarial debate rounds."""
    if not rounds:
        return None
    norm: list[dict[str, Any]] = []
    for item in rounds:
        if isinstance(item, DebateRoundRecord):
            norm.append(item.model_dump(mode="python"))
        elif isinstance(item, Mapping):
            norm.append(dict(item))
    if not norm:
        return None
    first_score = float(norm[0].get("score_before") or 0.0)
    kept_list = [r for r in norm if bool(r.get("kept", True))]
    if kept_list:
        after_score = float(kept_list[-1].get("score_after") or first_score)
    elif final_global_score is not None:
        after_score = float(final_global_score)
    else:
        after_score = first_score
    total_objections = sum(len(r.get("objections") or []) for r in norm)
    resolved_total = sum(int(r.get("resolved_count") or 0) for r in kept_list)
    return {
        "rounds": len(norm),
        "kept_rounds": len(kept_list),
        "total_objections": total_objections,
        "resolved_count": resolved_total,
        "score_before": round(first_score, 2),
        "score_after": round(after_score, 2),
        "score_delta": round(after_score - first_score, 2),
    }


class SubAuditResult(BaseModel):
    """Evaluation output for a single quality sub-auditor dimension."""

    dimension: str
    label: str
    score: float = Field(ge=0.0, le=100.0)
    weight: float = Field(ge=0.0, le=1.0)
    active: bool = True
    findings: list[str] = Field(default_factory=list)
    improvements: list[str] = Field(default_factory=list)


def classify_quality_tier(
    global_score: float,
    failed_dimensions: Sequence[str] = (),
    dimensions: Mapping[str, Any] | None = None,
    *,
    min_area_score: float = 70.0,
) -> tuple[str, str]:
    """Classify global_score and active area scores into (tier_id, tier_label)."""
    score = round(float(global_score), 2)
    has_failed_area = bool(failed_dimensions)
    if not has_failed_area and isinstance(dimensions, Mapping):
        for item in dimensions.values():
            if isinstance(item, SubAuditResult):
                active = item.active
                area_score = item.score
            elif isinstance(item, Mapping):
                active = item.get("active", True) is not False
                area_score = _clamp_score(item.get("score"), default=0.0)
            else:
                continue
            if active and area_score < min_area_score:
                has_failed_area = True
                break
    if score < 75.0:
        return "basic", "🥉 Básico"
    if score < 85.0:
        return "standard", "🥈 Estándar"
    if has_failed_area:
        return "standard", "🥈 Estándar (área pendiente)"
    if score < 93.0:
        return "production", "🥇 Producción"
    return "excellence", "💎 Excelencia"


def ensure_scorecard_tier(scorecard: Mapping[str, Any]) -> dict[str, Any]:
    """Return a shallow copy of scorecard guaranteed to include tier_id and tier_label."""
    copy = dict(scorecard)
    raw_tier_id = str(copy.get("tier_id") or "").strip()
    raw_tier_lbl = str(copy.get("tier_label") or "").strip()
    if raw_tier_id and raw_tier_lbl:
        return copy
    min_area = _clamp_score(copy.get("min_area_score"), default=70.0)
    global_score = _clamp_score(copy.get("global_score"), default=0.0)
    failed_dims = list(copy.get("failed_dimensions") or [])
    dims_map: dict[str, Mapping[str, Any]] = {}
    raw_areas = copy.get("areas")
    if isinstance(raw_areas, Sequence) and not isinstance(raw_areas, (str, bytes)):
        for idx, area in enumerate(raw_areas):
            if isinstance(area, Mapping):
                dim_key = str(area.get("dimension") or f"area_{idx}")
                dims_map[dim_key] = area
                if (
                    area.get("active", True) is not False
                    and (
                        area.get("passed") is False
                        or _clamp_score(area.get("score"), default=0.0) < min_area
                    )
                    and dim_key not in failed_dims
                ):
                    failed_dims.append(dim_key)
    tier_id, tier_label = classify_quality_tier(
        global_score,
        failed_dimensions=failed_dims,
        dimensions=dims_map or None,
        min_area_score=min_area,
    )
    copy["tier_id"] = raw_tier_id or tier_id
    copy["tier_label"] = raw_tier_lbl or tier_label
    raw_debate = copy.get("debate_rounds")
    if isinstance(raw_debate, Sequence) and not isinstance(raw_debate, (str, bytes)) and raw_debate and not copy.get("debate_summary"):
        copy["debate_summary"] = summarize_debate_rounds(
            raw_debate, final_global_score=global_score
        )
    return copy


class QualityAuditReport(BaseModel):
    """Aggregated quality evaluation report produced by ParentAuditor."""

    global_score: float = Field(ge=0.0, le=100.0)
    passed: bool
    has_ui: bool
    min_global_score: float = 85.0
    min_area_score: float = 70.0
    round_number: int = 1
    tier_id: str = ""
    tier_label: str = ""
    dimensions: dict[str, SubAuditResult]
    failed_dimensions: list[str] = Field(default_factory=list)
    summary: str = ""
    findings: list[str] = Field(default_factory=list)
    improvements: list[str] = Field(default_factory=list)
    objections: list[DebateObjection] = Field(default_factory=list)
    debate_rounds: list[DebateRoundRecord] = Field(default_factory=list)
    markdown_report: str = ""
    source: str = "llm"

    @model_validator(mode="after")
    def _ensure_tier(self) -> QualityAuditReport:
        if not self.tier_id or not self.tier_label:
            tier_id, tier_label = classify_quality_tier(
                self.global_score,
                failed_dimensions=self.failed_dimensions,
                dimensions=self.dimensions,
                min_area_score=self.min_area_score,
            )
            self.tier_id = self.tier_id or tier_id
            self.tier_label = self.tier_label or tier_label
        return self

    def to_scorecard_dict(self, *, extra_secrets: Sequence[str] = ()) -> dict[str, Any]:
        """Serialize compact, secret-redacted scorecard for PROJECT_DELIVERED and Dashboard."""
        areas: list[dict[str, Any]] = []
        for dim in QUALITY_DIMENSION_ORDER:
            sub = self.dimensions.get(dim)
            if sub is None:
                continue
            areas.append(
                {
                    "dimension": sub.dimension,
                    "label": sub.label,
                    "score": round(sub.score, 2),
                    "weight": round(sub.weight, 4),
                    "active": sub.active,
                    "passed": (sub.score >= self.min_area_score) if sub.active else True,
                    "findings": [
                        redact_secrets(f, extra_secrets=extra_secrets) for f in sub.findings[:5]
                    ],
                    "improvements": [
                        redact_secrets(i, extra_secrets=extra_secrets)
                        for i in sub.improvements[:5]
                    ],
                }
            )
        serialized_rounds: list[dict[str, Any]] = []
        for rnd in self.debate_rounds:
            serialized_rounds.append(
                {
                    "round_number": rnd.round_number,
                    "score_before": round(rnd.score_before, 2),
                    "score_after": round(rnd.score_after, 2),
                    "objections": [
                        {
                            "dimension": obj.dimension,
                            "file_path": redact_secrets(obj.file_path, extra_secrets=extra_secrets),
                            "severity": obj.severity,
                            "issue": redact_secrets(obj.issue, extra_secrets=extra_secrets),
                            "concrete_fix": redact_secrets(obj.concrete_fix, extra_secrets=extra_secrets),
                            "resolved": obj.resolved,
                        }
                        for obj in rnd.objections
                    ],
                    "builder_response": redact_secrets(rnd.builder_response, extra_secrets=extra_secrets),
                    "resolved_count": rnd.resolved_count,
                    "kept": rnd.kept,
                    "diff_summary": redact_secrets(rnd.diff_summary, extra_secrets=extra_secrets),
                    "discard_reason": redact_secrets(rnd.discard_reason, extra_secrets=extra_secrets),
                }
            )
        debate_summary = summarize_debate_rounds(
            serialized_rounds, final_global_score=self.global_score
        )
        out: dict[str, Any] = {
            "global_score": round(self.global_score, 2),
            "passed": self.passed,
            "tier_id": self.tier_id,
            "tier_label": self.tier_label,
            "has_ui": self.has_ui,
            "round_number": self.round_number,
            "min_global_score": self.min_global_score,
            "min_area_score": self.min_area_score,
            "failed_dimensions": list(self.failed_dimensions),
            "source": self.source,
            "summary": redact_secrets(self.summary, extra_secrets=extra_secrets),
            "areas": areas,
            "debate_rounds": serialized_rounds,
        }
        if debate_summary is not None:
            out["debate_summary"] = debate_summary
        return out


def _clamp_score(value: Any, *, default: float = 80.0) -> float:
    if isinstance(value, bool):
        return default
    if isinstance(value, (int, float)):
        return round(max(0.0, min(100.0, float(value))), 2)
    if isinstance(value, str):
        m = re.search(r"-?\d+(?:\.\d+)?", value)
        if m:
            try:
                return round(max(0.0, min(100.0, float(m.group(0)))), 2)
            except ValueError:
                pass
    return default


def _coerce_str_list(value: Any, *, extra_secrets: Sequence[str] = ()) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        cleaned = redact_secrets(value.strip(), extra_secrets=extra_secrets)
        return [cleaned] if cleaned else []
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        out: list[str] = []
        for item in value:
            if isinstance(item, str) and item.strip():
                out.append(redact_secrets(item.strip(), extra_secrets=extra_secrets))
            elif isinstance(item, Mapping):
                txt = str(
                    item.get("message")
                    or item.get("detail")
                    or item.get("description")
                    or item.get("text")
                    or ""
                ).strip()
                if txt:
                    out.append(redact_secrets(txt, extra_secrets=extra_secrets))
        return out
    return []


def detect_workspace_has_ui(workspace_dir: Path, plan: Plan | None = None) -> bool:
    """Detect whether workspace contains a visual frontend UI vs pure CLI/API logic."""
    del plan
    ws = Path(workspace_dir)
    if not ws.is_dir():
        return False
    files = list_workspace_files(ws, max_files=250)
    non_test_files = [
        f
        for f in files
        if not f.startswith(("tests/", "test/", ".agent-factory/"))
        and f not in ("PROJECT_PROMPT.md", "manifest.json")
    ]
    for rel in non_test_files:
        low = rel.lower()
        suffix = Path(low).suffix
        if suffix in _UI_EXTENSIONS:
            return True
        if low.startswith(("templates/", "static/", "frontend/", "public/")):
            return True

    for rel in non_test_files:
        low = rel.lower()
        if not low.endswith((".py", ".js", ".mjs", ".ts")):
            continue
        target = (ws / rel).resolve()
        if not target.is_relative_to(ws.resolve()) or not target.is_file():
            continue
        try:
            text_low = target.read_bytes()[:16384].decode("utf-8", errors="ignore").lower()
        except OSError:
            continue
        markers = _UI_PY_MARKERS if low.endswith(".py") else _UI_JS_MARKERS
        if any(marker in text_low for marker in markers):
            return True
    return False


def compute_area_weights(has_ui: bool) -> dict[str, float]:
    """Return adaptive weights across the 6 dimensions with or without UI."""
    if has_ui:
        return {
            "ux_ui": 0.25,
            "functional_completeness": 0.25,
            "robustness": 0.125,
            "architecture_quality": 0.125,
            "security_hygiene": 0.125,
            "testing_runtime": 0.125,
        }
    redistributed_extra = 0.25 / 3.0
    return {
        "ux_ui": 0.0,
        "functional_completeness": 0.25,
        "robustness": 0.125 + redistributed_extra,
        "architecture_quality": 0.125 + redistributed_extra,
        "security_hygiene": 0.125,
        "testing_runtime": 0.125 + redistributed_extra,
    }


def _safe_read_workspace_sources(
    workspace_dir: Path,
    *,
    extra_secrets: Sequence[str] = (),
) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    """Read contained workspace source and test files safely up to bounded limits."""
    ws = Path(workspace_dir).resolve()
    files = list_workspace_files(ws, max_files=180)
    src_texts: list[tuple[str, str]] = []
    test_texts: list[tuple[str, str]] = []
    for rel in files:
        if rel.startswith(".agent-factory/") or rel in ("PROJECT_PROMPT.md", "manifest.json"):
            continue
        cand = (ws / rel).resolve()
        if not cand.is_relative_to(ws) or not cand.is_file():
            continue
        try:
            content = redact_secrets(
                cand.read_bytes()[:24576].decode("utf-8", errors="replace"),
                extra_secrets=extra_secrets,
            )
        except OSError:
            continue
        if rel.startswith(("tests/", "test/")):
            test_texts.append((rel, content))
        else:
            src_texts.append((rel, content))
    return src_texts, test_texts


def _evaluate_security(src_texts: list[tuple[str, str]]) -> tuple[float, list[str], list[str]]:
    score = 92.0
    findings: list[str] = []
    improvements: list[str] = []
    combined = "\n".join(txt for _, txt in src_texts)
    if _SECRET_LITERAL_RE.search(combined):
        score -= 35.0
        findings.append("Posible credencial literal detectada en el código fuente.")
        improvements.append("Mover credenciales a variables de entorno fuera del repositorio.")
    if _HOST_PATH_RE.search(combined):
        score -= 20.0
        findings.append("Ruta local del equipo hardcodeada en el código fuente.")
        improvements.append("Usar rutas relativas configurables y validaciones dentro del workspace.")
    if "shell=True" in combined:
        score -= 20.0
        findings.append("Uso de shell=True en ejecución de subprocesos.")
        improvements.append("Pasar argumentos como lista con shell=False.")
    if re.search(r"innerHTML\s*=", combined) and "escapeHtml" not in combined and "textContent" not in combined:
        score -= 15.0
        findings.append("Asignación directa a innerHTML sin utilidad visible de escape HTML.")
        improvements.append("Sanitizar valores con escapeHtml o usar textContent.")
    return max(0.0, score), findings, improvements


def _evaluate_testing_runtime(
    ws: Path,
    src_texts: list[tuple[str, str]],
    test_texts: list[tuple[str, str]],
    test_results: Mapping[str, bool] | None,
) -> tuple[float, list[str], list[str]]:
    score = 88.0
    findings: list[str] = []
    improvements: list[str] = []
    if not test_texts:
        score -= 30.0
        findings.append("No se encontraron archivos de prueba en tests/.")
        improvements.append("Agregar pruebas automatizadas reales verificando los flujos principales.")
    else:
        has_assert = any(
            ("assert " in txt or "expect(" in txt or "pytest.raises" in txt)
            for _, txt in test_texts
        )
        if not has_assert:
            score -= 22.0
            findings.append("Las pruebas existentes carecen de aserciones concretas de comportamiento.")
            improvements.append("Añadir aserciones verificando salidas esperadas y casos borde.")
    if test_results and not all(test_results.values()):
        score -= 30.0
        findings.append("Existen pruebas de aceptación en estado fallido.")
        improvements.append("Corregir las fallas hasta lograr 100% de pruebas en verde.")

    stdlib_names = getattr(sys, "stdlib_module_names", frozenset())
    local_roots = {Path(rel).parts[0] for rel, _ in src_texts} | {"src", "app", "main"}
    for rel, _ in src_texts:
        if rel.endswith("src/") or "/" in rel:
            parts = Path(rel).parts
            if parts[0] == "src" and len(parts) > 1:
                local_roots.add(parts[1].removesuffix(".py"))
            local_roots.add(Path(rel).stem)
    third_party_imports: set[str] = set()
    for rel, content in src_texts:
        if not rel.endswith(".py"):
            continue
        for mod in _PYTHON_IMPORT_RE.findall(content):
            if mod not in stdlib_names and mod not in local_roots and mod != "pytest":
                third_party_imports.add(mod.lower())
    req_file = ws / "requirements.txt"
    if third_party_imports and not req_file.is_file():
        score -= 15.0
        findings.append("El proyecto importa paquetes externos pero falta requirements.txt.")
        improvements.append("Declarar dependencias con versión fija en requirements.txt.")
    return max(0.0, score), findings, improvements


def _evaluate_ux_ui(
    has_ui: bool,
    src_texts: list[tuple[str, str]],
) -> tuple[float, list[str], list[str]]:
    if not has_ui:
        return 100.0, [], []
    score = 88.0
    findings: list[str] = []
    improvements: list[str] = []
    ui_blob = "\n".join(
        txt for rel, txt in src_texts if Path(rel).suffix.lower() in _UI_EXTENSIONS or rel.endswith(".js")
    )
    low = ui_blob.lower()
    if "<html" in low and "viewport" not in low:
        score -= 10.0
        findings.append("Falta meta viewport para diseño responsivo en móvil (360px).")
        improvements.append("Incluir <meta name=\"viewport\" content=\"width=device-width, initial-scale=1.0\">.")
    if ui_blob and "@media" not in low and "clamp(" not in low and "minmax(" not in low:
        score -= 8.0
        findings.append("No se detectan reglas CSS responsivas (@media / minmax) para 360px y escritorio.")
        improvements.append("Agregar media queries o rejilla fluida responsiva.")
    if ui_blob and "aria-" not in low and ":focus" not in low and "alt=" not in low:
        score -= 8.0
        findings.append("Poca cobertura de accesibilidad básica (atributos ARIA o foco visible).")
        improvements.append("Añadir atributos aria-label, alt y estilos :focus-visible.")
    return max(0.0, score), findings, improvements


def _evaluate_functional_robustness_arch(
    src_texts: list[tuple[str, str]],
    plan: Plan | None,
) -> dict[str, tuple[float, list[str], list[str]]]:
    func_score = 88.0
    func_findings: list[str] = []
    func_improvements: list[str] = []

    if not src_texts:
        func_score = 45.0
        func_findings.append("No hay archivos de código fuente implementados.")
        func_improvements.append("Implementar los módulos requeridos por la especificación.")
    else:
        combined_src = "\n".join(txt for _, txt in src_texts)
        if "NotImplementedError" in combined_src or "TODO:" in combined_src or "FIXME:" in combined_src:
            func_score -= 18.0
            func_findings.append("Existen marcadores pendientes (TODO/FIXME o NotImplementedError).")
            func_improvements.append("Completar las funciones pendientes sin stubs de adorno.")
        if plan is not None and plan.files:
            existing_set = {rel.lower() for rel, _ in src_texts}
            missing = [
                f.path
                for f in plan.files
                if f.path.lower() not in existing_set
                and not f.path.lower().startswith(("tests/", "test/"))
            ]
            if missing:
                func_score -= min(25.0, 8.0 * len(missing))
                func_findings.append(f"Faltan archivos planificados: {', '.join(missing[:3])}.")
                func_improvements.append("Asegurar la presencia de todos los archivos contratados en el Plan.")

    rob_score = 87.0
    rob_findings: list[str] = []
    rob_improvements: list[str] = []
    combined_all = "\n".join(txt for _, txt in src_texts)
    if combined_all and not any(
        tok in combined_all for tok in ("try", "except", "catch", "if not ", "if (", "raise ")
    ):
        rob_score -= 12.0
        rob_findings.append("Manejo mínimo de validaciones o errores ante entradas inválidas.")
        rob_improvements.append("Validar entradas vacías/nulas y capturar errores esperados limpiamente.")

    arch_score = 88.0
    arch_findings: list[str] = []
    arch_improvements: list[str] = []
    if src_texts and any(len(txt) == 0 for _, txt in src_texts):
        arch_score -= 10.0
        arch_findings.append("Se detectaron archivos fuente vacíos sin propósito claro.")
        arch_improvements.append("Eliminar archivos vacíos o completar su implementación.")

    return {
        "functional_completeness": (max(0.0, func_score), func_findings, func_improvements),
        "robustness": (max(0.0, rob_score), rob_findings, rob_improvements),
        "architecture_quality": (max(0.0, arch_score), arch_findings, arch_improvements),
    }


_STANDARD_PY_DUNDERS: frozenset[str] = frozenset(
    {
        "__main__",
        "__init__",
        "__name__",
        "__file__",
        "__doc__",
        "__all__",
        "__future__",
        "__version__",
        "__pycache__",
        "__slots__",
        "__dict__",
        "__class__",
        "__spec__",
    }
)
_STRING_LITERAL_RE = re.compile(r"""(['"`])((?:\\.|(?!\1).)*?)\1""")
_INLINE_SCRIPT_RE = re.compile(r"<script\b[^>]*>(.*?)</script>", re.IGNORECASE | re.DOTALL)


def _merge_unique_notes(existing: Iterable[str], additions: Iterable[str]) -> list[str]:
    """Append non-empty note entries preserving order and avoiding case-insensitive duplicates."""
    out: list[str] = []
    seen: set[str] = set()
    for item in (*existing, *additions):
        txt = str(item or "").strip()
        if not txt:
            continue
        key = txt.lower()
        if key not in seen:
            seen.add(key)
            out.append(txt)
    return out


def _normalize_browser_findings(
    browser_smoke_findings: Any,
    *,
    extra_secrets: Sequence[str] = (),
) -> list[str]:
    """Extract secret-redacted findings list from BrowserSmokeTestResult, mapping, or sequence."""
    if browser_smoke_findings is None:
        return []
    raw_items: Any = browser_smoke_findings
    if hasattr(browser_smoke_findings, "findings"):
        raw_items = getattr(browser_smoke_findings, "findings", None)
    elif isinstance(browser_smoke_findings, Mapping):
        raw_items = browser_smoke_findings.get("findings")
    if isinstance(raw_items, str):
        items = [raw_items]
    elif isinstance(raw_items, Sequence) and not isinstance(raw_items, (bytes, bytearray)):
        items = list(raw_items)
    else:
        return []
    cleaned = [
        redact_secrets(str(it).strip(), extra_secrets=extra_secrets)
        for it in items
        if str(it or "").strip()
    ]
    return _merge_unique_notes((), cleaned)


def _extract_ui_scripts(src_texts: Sequence[tuple[str, str]]) -> str:
    """Collect frontend JS/TS code and inline HTML <script> contents for async UX inspection."""
    chunks: list[str] = []
    for rel, txt in src_texts:
        low_rel = rel.lower()
        if low_rel.endswith((".js", ".mjs", ".ts", ".jsx", ".tsx")):
            chunks.append(txt)
        elif low_rel.endswith((".html", ".htm")):
            chunks.extend(_INLINE_SCRIPT_RE.findall(txt))
    return "\n".join(chunks)


def _detect_ux_ui_strict_violations(
    has_ui: bool,
    src_texts: Sequence[tuple[str, str]],
    browser_findings: Sequence[str],
) -> tuple[float | None, list[str], list[str]]:
    """Enforce rule (a): cap UX/UI <= 72 on missing async UX safeguards or live browser smoke findings."""
    findings: list[str] = []
    improvements: list[str] = []
    cap: float | None = None

    if browser_findings:
        cap = STRICT_UX_UI_CAP
        findings.extend(browser_findings[:6])
        improvements.append(
            "Corregir errores de consola JS, desbordamiento horizontal e imágenes rotas detectadas en Chromium headless."
        )

    if not has_ui:
        return cap, findings, improvements

    ui_blob = "\n".join(
        txt
        for rel, txt in src_texts
        if Path(rel).suffix.lower() in _UI_EXTENSIONS or rel.lower().endswith((".js", ".mjs", ".ts"))
    )
    ui_low = ui_blob.lower()
    ui_script_blob = _extract_ui_scripts(src_texts)
    if re.search(r"\bfetch\s*\(|XMLHttpRequest|\baxios\.", ui_script_blob):
        has_disable = bool(
            re.search(
                r"\.disabled\s*=|setAttribute\s*\(\s*['\"]disabled['\"]|removeAttribute\s*\(\s*['\"]disabled['\"]",
                ui_script_blob,
            )
        )
        has_loading = bool(
            re.search(
                r"\b(?:spinner|is-loading|loading|cargando|loader|busy)\b|aria-busy|animate-spin|@keyframes\s+spin",
                ui_low,
            )
        )
        combined_all_low = "\n".join(txt.lower() for _, txt in src_texts)
        backend_uses_engine_or_fallback = bool(
            re.search(
                r"\b(?:fallback|heuristic|heurístic|exiftool|pillow|piexif|agy|ffmpeg|ffprobe)\b",
                combined_all_low,
            )
        )
        ui_communicates_engine = bool(
            re.search(
                r"\b(?:fallback|motor|engine|extractor|heurístic|heuristic|usando)\b",
                ui_low,
            )
        )
        missing_parts: list[str] = []
        if not has_disable:
            missing_parts.append("deshabilitar el botón durante la llamada")
        if not has_loading:
            missing_parts.append("mostrar indicador visual de carga (spinner/estado activo)")
        if backend_uses_engine_or_fallback and not ui_communicates_engine:
            missing_parts.append("comunicar qué motor o fallback ejecutó la acción")
        if missing_parts:
            cap = STRICT_UX_UI_CAP
            findings.append(
                f"Acciones asíncronas (fetch) sin resguardo UX completo: falta {', '.join(missing_parts)} (tope <= {STRICT_UX_UI_CAP:.0f})."
            )
            improvements.append(
                "Deshabilitar botones mientras corre fetch, mostrar spinner/estado activo visible e indicar el motor o fallback ejecutado."
            )
    return cap, findings, improvements


def _detect_functional_strict_violations(
    src_texts: Sequence[tuple[str, str]],
) -> tuple[float | None, list[str], list[str]]:
    """Enforce rule (b): cap Funcionalidad <= 70 for technical template literals or uninvalidated regenerate cache."""
    findings: list[str] = []
    improvements: list[str] = []
    cap: float | None = None

    matched_tokens: list[str] = []
    for rel, txt in src_texts:
        ext = Path(rel).suffix.lower()
        if ext not in {".py", ".js", ".mjs", ".ts", ".jsx", ".tsx"}:
            continue
        cleaned_txt = re.sub(r"\bplaceholder\s*=\s*(['\"]).*?\1", "", txt)
        cleaned_txt = re.sub(r"::placeholder\b", "", cleaned_txt)
        for match in _STRING_LITERAL_RE.finditer(cleaned_txt):
            lit = match.group(2).strip()
            if not lit:
                continue
            low_lit = lit.lower()
            if low_lit in _STANDARD_PY_DUNDERS or low_lit.startswith("http"):
                continue
            token_hit: str | None = None
            if "__" in lit:
                token_hit = "__"
            elif "fallback_" in low_lit:
                token_hit = "fallback_"
            elif re.search(r"\bplaceholder\b", low_lit) or "placeholder_" in low_lit:
                token_hit = "placeholder"
            elif re.search(r"\bsample\b", low_lit) or "sample_" in low_lit:
                token_hit = "sample"
            if token_hit and token_hit not in matched_tokens:
                matched_tokens.append(token_hit)

    if matched_tokens:
        cap = STRICT_FUNCTIONAL_CAP
        findings.append(
            f"Se detectaron cadenas plantilla o técnicas en salidas/nombres generados ({', '.join(matched_tokens[:4])}; tope <= {STRICT_FUNCTIONAL_CAP:.0f})."
        )
        improvements.append(
            "Reemplazar etiquetas o nombres con '__', 'placeholder', 'fallback_' o 'sample' por valores semánticos reales para el usuario."
        )

    combined_low = "\n".join(txt.lower() for _, txt in src_texts)
    has_regenerate = bool(re.search(r"regenerar|regenerate", combined_low))
    has_cache = bool(re.search(r"\b[a-z0-9_]*cache[a-z0-9_]*\b", combined_low))
    if has_regenerate and has_cache:
        has_invalidation = bool(
            re.search(
                r"invalidat|bypass|no_cache|skip_cache|use_cache\s*=\s*false|force_refresh|"
                r"force\s*=\s*true|delete\s+from\s+[a-z0-9_]*cache|\.clear\s*\(|\.pop\s*\(",
                combined_low,
            )
        )
        if not has_invalidation:
            cap = STRICT_FUNCTIONAL_CAP
            findings.append(
                f"Acción de regeneración detectada junto a caché sin mecanismo visible de invalidación o bypass (tope <= {STRICT_FUNCTIONAL_CAP:.0f})."
            )
            improvements.append(
                "Invalidar o omitir la caché explícitamente cuando el usuario ejecute 'regenerar' para no devolver resultados obsoletos."
            )

    return cap, findings, improvements


def _detect_testing_strict_violations(
    src_texts: Sequence[tuple[str, str]],
    test_texts: Sequence[tuple[str, str]],
) -> tuple[float | None, list[str], list[str]]:
    """Enforce rule (c): cap Pruebas <= 72 when tests only assert HTTP 200 / non-empty outputs without real edge cases."""
    if not test_texts:
        return None, [], []

    combined_src_low = "\n".join(txt.lower() for _, txt in src_texts)
    combined_tests_low = "\n".join(txt.lower() for _, txt in test_texts)

    has_shallow_checks = bool(
        re.search(
            r"status_code\s*==\s*200|\.status\s*==\s*200|\blen\s*\([^)]+\)\s*>\s*0|"
            r"assert\s+isinstance\s*\([^,]+,\s*str\)|assert\s+[a-z0-9_.]+\.strip\s*\(\s*\)",
            combined_tests_low,
        )
    )
    has_error_status_or_raises = bool(
        re.search(
            r"pytest\.raises|\bstatus(?:_code)?\s*==\s*(?:400|403|404|409|422|500)\b",
            combined_tests_low,
        )
    )
    has_missing_meta_edge = bool(
        re.search(
            r"missing_meta|sin_metadatos|no_meta|undated|captured_at['\"]?\s*[:=]\s*none|"
            r"metadata['\"]?\s*[:=]\s*\{\}|invalid|corrupt|is\s+none",
            combined_tests_low,
        )
    )
    uses_exif_or_image = bool(
        re.search(r"\b(?:pil|pillow|piexif|exif|exiftool|imageops)\b", combined_src_low)
    )
    has_exif_rotation_edge = bool(
        re.search(
            r"exif_transpose|orientation|orientaci|rotate|rotación|rotacion|270|180|90",
            combined_tests_low,
        )
    )
    uses_external_cli = bool(
        re.search(r"subprocess\.(?:run|popen)|shutil\.which", combined_src_low)
    )
    has_cli_failure_edge = bool(
        re.search(
            r"filenotfounderror|timeoutexpired|oserror|subprocesperror|returncode\s*[!=]=\s*[12]|"
            r"missing.*(?:bin|cli)|cli.*fallback",
            combined_tests_low,
        )
    )
    uses_cache_and_regen = bool(
        re.search(r"regenerar|regenerate", combined_src_low)
        and re.search(r"\b[a-z0-9_]*cache[a-z0-9_]*\b", combined_src_low)
    )
    has_cache_invalidation_edge = bool(
        re.search(r"invalidat|no_cache|bypass|clear\(|cache.*regenera|regenera.*cache", combined_tests_low)
    )

    missing_edges: list[str] = []
    if has_shallow_checks and not has_error_status_or_raises and not has_missing_meta_edge:
        missing_edges.append("errores/metadatos ausentes")
    if uses_exif_or_image and not has_exif_rotation_edge:
        missing_edges.append("rotación/orientación EXIF")
    if uses_external_cli and not has_cli_failure_edge:
        missing_edges.append("fallos de CLI externo")
    if uses_cache_and_regen and not has_cache_invalidation_edge:
        missing_edges.append("invalidación de caché")

    if (has_shallow_checks and missing_edges) or (
        (uses_exif_or_image or uses_external_cli or uses_cache_and_regen) and missing_edges
    ):
        return (
            STRICT_TESTING_CAP,
            [
                f"Pruebas superficiales (solo HTTP 200 o salidas no vacías) sin casos borde clave en {', '.join(missing_edges)} (tope <= {STRICT_TESTING_CAP:.0f})."
            ],
            [
                f"Añadir pruebas negativas y de casos borde cubriendo: {', '.join(missing_edges)}."
            ],
        )
    return None, [], []


def _detect_strict_rubric_violations(
    has_ui: bool,
    src_texts: Sequence[tuple[str, str]],
    test_texts: Sequence[tuple[str, str]],
    browser_smoke_findings: Sequence[str] = (),
) -> dict[str, tuple[float, list[str], list[str]]]:
    """Compute hard score caps and concrete findings for UX/UI (<=72), Funcionalidad (<=70), and Pruebas (<=72)."""
    caps: dict[str, tuple[float, list[str], list[str]]] = {}
    ux_cap, ux_f, ux_i = _detect_ux_ui_strict_violations(
        has_ui, src_texts, browser_smoke_findings
    )
    if ux_cap is not None:
        caps["ux_ui"] = (ux_cap, ux_f, ux_i)

    fn_cap, fn_f, fn_i = _detect_functional_strict_violations(src_texts)
    if fn_cap is not None:
        caps["functional_completeness"] = (fn_cap, fn_f, fn_i)

    ts_cap, ts_f, ts_i = _detect_testing_strict_violations(src_texts, test_texts)
    if ts_cap is not None:
        caps["testing_runtime"] = (ts_cap, ts_f, ts_i)
    return caps


def _run_deterministic_audit(
    workspace_dir: Path,
    *,
    has_ui: bool,
    plan: Plan | None = None,
    test_results: Mapping[str, bool] | None = None,
    browser_smoke_findings: Sequence[str] = (),
    extra_secrets: Sequence[str] = (),
) -> dict[str, dict[str, Any]]:
    ws = Path(workspace_dir)
    src_texts, test_texts = _safe_read_workspace_sources(ws, extra_secrets=extra_secrets)
    ux_s, ux_f, ux_i = _evaluate_ux_ui(has_ui, src_texts)
    sec_s, sec_f, sec_i = _evaluate_security(src_texts)
    tst_s, tst_f, tst_i = _evaluate_testing_runtime(ws, src_texts, test_texts, test_results)
    core = _evaluate_functional_robustness_arch(src_texts, plan)
    dims: dict[str, dict[str, Any]] = {
        "ux_ui": {"score": ux_s, "findings": ux_f, "improvements": ux_i},
        "functional_completeness": {
            "score": core["functional_completeness"][0],
            "findings": core["functional_completeness"][1],
            "improvements": core["functional_completeness"][2],
        },
        "robustness": {
            "score": core["robustness"][0],
            "findings": core["robustness"][1],
            "improvements": core["robustness"][2],
        },
        "architecture_quality": {
            "score": core["architecture_quality"][0],
            "findings": core["architecture_quality"][1],
            "improvements": core["architecture_quality"][2],
        },
        "security_hygiene": {"score": sec_s, "findings": sec_f, "improvements": sec_i},
        "testing_runtime": {"score": tst_s, "findings": tst_f, "improvements": tst_i},
    }
    strict_caps = _detect_strict_rubric_violations(
        has_ui,
        src_texts,
        test_texts,
        browser_smoke_findings=browser_smoke_findings,
    )
    for dim, (cap_val, cap_f, cap_i) in strict_caps.items():
        if dim in dims:
            dims[dim]["score"] = min(float(dims[dim]["score"]), cap_val)
            dims[dim]["findings"] = _merge_unique_notes(dims[dim]["findings"], cap_f)
            dims[dim]["improvements"] = _merge_unique_notes(dims[dim]["improvements"], cap_i)
    return dims


def build_quality_markdown_report(
    report: QualityAuditReport,
    *,
    project_name: str,
    version_label: str = "",
    restored_best_checkpoint: bool = False,
    extra_secrets: Sequence[str] = (),
) -> str:
    """Render structured Spanish Markdown quality report with scorecard table and per-area findings."""
    verdict = "Aprobado" if report.passed else "Con observaciones"
    ui_label = "Proyecto con interfaz visual (UI)" if report.has_ui else "API / CLI sin interfaz UI"
    ver_suffix = f" ({version_label})" if version_label else ""
    lines = [
        f"# Auditoría Integral de Calidad — {redact_secrets(project_name, extra_secrets=extra_secrets)}{ver_suffix}",
        "",
        f"- **Calificación Global:** `{report.global_score:.1f}%` ({verdict})",
        f"- **Categoría de Calidad:** {report.tier_label}",
        f"- **Umbrales:** Global `>= {report.min_global_score:.0f}%` · Área activa `>= {report.min_area_score:.0f}%`",
        f"- **Perfil detectado:** {ui_label}",
        f"- **Ronda de evaluación:** {report.round_number}",
    ]
    if restored_best_checkpoint:
        lines.append(
            "- **Seguridad anti-regresión:** Se restauró el mejor checkpoint verificado tras agotar las rondas permitidas."
        )
    if report.summary:
        lines.extend(["", f"**Resumen:** {redact_secrets(report.summary, extra_secrets=extra_secrets)}"])

    lines.extend(
        [
            "",
            "## Boleta de Calificaciones",
            "",
            "| Área | Peso | Calificación | Estado |",
            "| --- | --- | --- | --- |",
        ]
    )
    for dim in QUALITY_DIMENSION_ORDER:
        sub = report.dimensions[dim]
        if not sub.active:
            lines.append(f"| {sub.label} | 0.0% | N/A | No aplica |")
        else:
            status_txt = "Aprobado" if sub.score >= report.min_area_score else "Reprobado"
            lines.append(
                f"| {sub.label} | {sub.weight * 100:.1f}% | {sub.score:.1f}% | {status_txt} |"
            )

    lines.extend(["", "## Hallazgos y Mejoras por Sub-Auditor"])
    for dim in QUALITY_DIMENSION_ORDER:
        sub = report.dimensions[dim]
        if not sub.active:
            continue
        lines.extend(["", f"### {sub.label} ({sub.score:.1f}%)"])
        if sub.findings:
            lines.append("**Hallazgos:**")
            for f_item in sub.findings:
                lines.append(f"- {redact_secrets(f_item, extra_secrets=extra_secrets)}")
        else:
            lines.append("- Sin hallazgos críticos en esta área.")
        if sub.improvements:
            lines.append("**Acciones recomendadas:**")
            for imp_item in sub.improvements:
                lines.append(f"- {redact_secrets(imp_item, extra_secrets=extra_secrets)}")

    if report.debate_rounds:
        first_before = report.debate_rounds[0].score_before
        delta = report.global_score - first_before
        lines.extend(
            [
                "",
                "## ⚔️ Debate de Calidad (Crítico vs. Constructor)",
                "",
                (
                    f"- **Evolución:** `{first_before:.1f}%` → `{report.global_score:.1f}%` "
                    f"(`{delta:+.1f}%` · `{len(report.debate_rounds)}` ronda(s))"
                ),
            ]
        )
        for rnd in report.debate_rounds:
            total_obj = len(rnd.objections)
            if rnd.kept:
                status_lbl = (
                    f"Aplicada · {rnd.resolved_count}/{total_obj} objeciones resueltas"
                    if total_obj
                    else "Aplicada"
                )
            else:
                reason = redact_secrets(
                    rnd.discard_reason or "regresión revertida",
                    extra_secrets=extra_secrets,
                )
                status_lbl = f"Descartada ({reason})"
            lines.extend(
                [
                    "",
                    (
                        f"### Ronda {rnd.round_number} — `{rnd.score_before:.1f}%` → "
                        f"`{rnd.score_after:.1f}%` ({status_lbl})"
                    ),
                ]
            )
            if rnd.objections:
                lines.append("**Objeciones del Crítico Adversario:**")
                for obj in rnd.objections:
                    file_tag = (
                        f" en `{redact_secrets(obj.file_path, extra_secrets=extra_secrets)}`"
                        if obj.file_path
                        else ""
                    )
                    dim_lbl = QUALITY_DIMENSION_META.get(obj.dimension, {}).get(
                        "label", obj.dimension
                    )
                    fix_part = (
                        f" → Fix: {redact_secrets(obj.concrete_fix, extra_secrets=extra_secrets)}"
                        if obj.concrete_fix
                        else ""
                    )
                    lines.append(
                        f"- [{dim_lbl} · {obj.severity}]{file_tag}: "
                        f"{redact_secrets(obj.issue, extra_secrets=extra_secrets)}{fix_part}"
                    )
            if rnd.builder_response:
                lines.append("**Respuesta del Constructor Refinador:**")
                for resp_line in rnd.builder_response.splitlines():
                    cleaned_line = redact_secrets(resp_line.strip(), extra_secrets=extra_secrets)
                    if cleaned_line:
                        lines.append(
                            cleaned_line if cleaned_line.startswith("-") else f"- {cleaned_line}"
                        )
            if rnd.diff_summary:
                lines.append(
                    f"**Inspección de `git diff`:** {redact_secrets(rnd.diff_summary, extra_secrets=extra_secrets)}"
                )

    return "\n".join(lines).strip()


def _select_target_dimensions(
    report: QualityAuditReport,
    *,
    objections: Sequence[DebateObjection] = (),
    challenge_for_polish: bool = False,
) -> list[SubAuditResult]:
    target_subs = [
        report.dimensions[dim]
        for dim in QUALITY_DIMENSION_ORDER
        if report.dimensions[dim].active
        and report.dimensions[dim].score < report.min_area_score
    ]
    if not target_subs:
        target_subs = sorted(
            (
                report.dimensions[dim]
                for dim in QUALITY_DIMENSION_ORDER
                if report.dimensions[dim].active
                and report.dimensions[dim].score < report.min_global_score
            ),
            key=lambda s: s.score,
        )
    if not target_subs and objections:
        obj_dims = {obj.dimension for obj in objections}
        target_subs = [
            report.dimensions[dim]
            for dim in QUALITY_DIMENSION_ORDER
            if dim in obj_dims and report.dimensions[dim].active
        ]
    if not target_subs:
        max_threshold = 100.0 if challenge_for_polish else 101.0
        target_subs = sorted(
            (
                report.dimensions[dim]
                for dim in QUALITY_DIMENSION_ORDER
                if report.dimensions[dim].active and report.dimensions[dim].score < max_threshold
            ),
            key=lambda s: s.score,
        )[:2]
    return target_subs


def format_refinement_feedback(
    report: QualityAuditReport,
    *,
    refinement_round: int,
    max_rounds: int,
    objections: Sequence[DebateObjection] = (),
    challenge_for_polish: bool = False,
    extra_secrets: Sequence[str] = (),
) -> str:
    """Build focused refinement instructions containing only penalized or challenged dimensions."""
    target_subs = _select_target_dimensions(
        report,
        objections=objections,
        challenge_for_polish=challenge_for_polish,
    )
    mode_tag = (
        f"[DEBATE DE CALIDAD · PULIDO {refinement_round}/{max_rounds}]"
        if challenge_for_polish and report.passed
        else f"[AUDITORÍA DE CALIDAD · RONDA DE REFINAMIENTO {refinement_round}/{max_rounds}]"
    )
    lines = [
        mode_tag,
        (
            f"Puntaje global actual: {report.global_score:.1f}% "
            f"(requerido >= {report.min_global_score:.0f}%, mínimo por área >= {report.min_area_score:.0f}%)."
        ),
        "Corrige ÚNICAMENTE las áreas señaladas manteniendo intactas todas las pruebas actuales:",
    ]
    for sub in target_subs:
        lines.append(f"- {sub.dimension} ({sub.label}): {sub.score:.1f}%")
        for f_txt in sub.findings[:4]:
            lines.append(f"  * Hallazgo: {redact_secrets(f_txt, extra_secrets=extra_secrets)}")
        for imp_txt in sub.improvements[:4]:
            lines.append(f"  * Mejora: {redact_secrets(imp_txt, extra_secrets=extra_secrets)}")
    target_dim_set = {sub.dimension for sub in target_subs}
    relevant_objs = [obj for obj in objections if obj.dimension in target_dim_set] or list(objections)
    if relevant_objs:
        lines.append("Objeciones específicas por archivo:")
        for obj in relevant_objs[:8]:
            file_part = (
                f"[{redact_secrets(obj.file_path, extra_secrets=extra_secrets)}] "
                if obj.file_path
                else ""
            )
            fix_part = (
                f" -> {redact_secrets(obj.concrete_fix, extra_secrets=extra_secrets)}"
                if obj.concrete_fix
                else ""
            )
            lines.append(
                f"  * ({obj.dimension} · {obj.severity}) {file_part}"
                f"{redact_secrets(obj.issue, extra_secrets=extra_secrets)}{fix_part}"
            )
    return "\n".join(lines)


def _invoke_structured_llm(
    llm: LLM | None,
    messages: list[dict[str, str]],
    *,
    schema: type[BaseModel],
    timeout_seconds: float,
    agent_name: str,
) -> Any:
    """Invoke LLM with optional timeout_seconds forwarding and unified guard."""
    if llm is None:
        raise LLMError(f"No LLM configured for {agent_name}")
    gen_fn = getattr(llm, "generate", None)
    target_fn = gen_fn if callable(gen_fn) else llm.complete
    try:
        sig = inspect.signature(target_fn)
        if "timeout_seconds" in sig.parameters:
            return target_fn(
                messages,
                schema=schema,
                timeout_seconds=timeout_seconds,
            )
    except (TypeError, ValueError):
        pass
    return target_fn(messages, schema=schema)


def _infer_workspace_file_for_dimension(
    workspace_dir: Path,
    dimension: str,
    hint_text: str = "",
) -> str:
    """Infer a real non-secret file path inside workspace_dir matching text or dimension heuristics."""
    files = [
        f
        for f in list_workspace_files(Path(workspace_dir), max_files=180)
        if not f.startswith(".agent-factory/") and f not in ("PROJECT_PROMPT.md", "manifest.json")
    ]
    if not files:
        return ""
    hint_low = hint_text.lower()
    for rel in files:
        base = Path(rel).name.lower()
        if rel.lower() in hint_low or (base and base in hint_low):
            return rel
    if dimension == "ux_ui":
        for ext in (".html", ".htm", ".css", ".scss", ".jsx", ".tsx", ".vue", ".svelte", ".js"):
            for rel in files:
                if rel.lower().endswith(ext) and not rel.startswith(("tests/", "test/")):
                    return rel
    if dimension == "testing_runtime":
        for rel in files:
            if rel.startswith(("tests/", "test/")) or rel == "requirements.txt":
                return rel
    for rel in files:
        if not rel.startswith(("tests/", "test/")):
            return rel
    return files[0]


class AdversarialCritic:
    """Agent 1: Strict evidence-based quality critic with diff verification against 6 dimensions."""

    def __init__(
        self,
        llm: LLM | None,
        *,
        min_global_score: float = 85.0,
        min_area_score: float = 70.0,
        timeout_seconds: float = 240.0,
    ) -> None:
        self.llm = llm
        self.min_global_score = float(min_global_score)
        self.min_area_score = float(min_area_score)
        self.timeout_seconds = float(timeout_seconds)

    @staticmethod
    def _extract_llm_payload(
        raw_resp: Any,
        *,
        extra_secrets: Sequence[str] = (),
    ) -> tuple[dict[str, dict[str, Any]], float | None, str, list[dict[str, Any]], int | None, str]:
        if isinstance(raw_resp, ParentAuditLLMOutput):
            payload: dict[str, Any] = dict(raw_resp.raw_payload or {})
            if not payload:
                payload = raw_resp.model_dump(mode="python")
        elif isinstance(raw_resp, Mapping):
            payload = dict(raw_resp.get("raw_payload") or raw_resp)
            for k in (
                "dimensions",
                "summary",
                "global_score",
                "objections",
                "resolved_count",
                "diff_summary",
            ):
                if k not in payload and k in raw_resp:
                    payload[k] = raw_resp[k]
        else:
            return {}, None, "", [], None, ""

        parsed_dims: dict[str, dict[str, Any]] = {}
        dims_container = (
            payload.get("dimensions")
            or payload.get("sub_auditors")
            or payload.get("areas")
            or payload.get("scores")
        )
        sources = [dims_container, payload] if isinstance(dims_container, Mapping) else [payload]
        for src in sources:
            for key, val in src.items():
                canonical = _DIMENSION_ALIASES.get(str(key).strip().lower())
                if not canonical or canonical in parsed_dims:
                    continue
                if isinstance(val, (int, float, str)):
                    parsed_dims[canonical] = {
                        "score": _clamp_score(val),
                        "findings": [],
                        "improvements": [],
                    }
                elif isinstance(val, Mapping):
                    parsed_dims[canonical] = {
                        "score": _clamp_score(val.get("score")),
                        "findings": _coerce_str_list(
                            val.get("findings") or val.get("hallazgos"),
                            extra_secrets=extra_secrets,
                        ),
                        "improvements": _coerce_str_list(
                            val.get("improvements") or val.get("mejoras"),
                            extra_secrets=extra_secrets,
                        ),
                    }
        raw_global = payload.get("global_score")
        explicit_global = (
            _clamp_score(raw_global)
            if isinstance(raw_global, (int, float)) and not isinstance(raw_global, bool)
            else None
        )
        summary = redact_secrets(
            str(payload.get("summary") or "").strip(), extra_secrets=extra_secrets
        )
        raw_objs = payload.get("objections") or payload.get("critiques") or []
        parsed_objs: list[dict[str, Any]] = (
            [dict(o) for o in raw_objs if isinstance(o, Mapping)]
            if isinstance(raw_objs, list)
            else []
        )
        raw_resolved = payload.get("resolved_count")
        resolved_cnt = (
            int(raw_resolved)
            if isinstance(raw_resolved, int) and not isinstance(raw_resolved, bool)
            else None
        )
        diff_summary = redact_secrets(
            str(payload.get("diff_summary") or "").strip(), extra_secrets=extra_secrets
        )
        return parsed_dims, explicit_global, summary, parsed_objs, resolved_cnt, diff_summary

    @staticmethod
    def _format_browser_findings_block(browser_findings: Sequence[str]) -> str:
        if not browser_findings:
            return ""
        items = "\n".join(f"- {line}" for line in browser_findings[:8])
        return (
            "\n\nVerificación real en navegador headless (Chromium):\n"
            f"{items}\n"
            "Evalúa estas evidencias bajo UX/UI (tope <= 72 si hay errores de consola, "
            "desbordamiento horizontal scrollWidth > innerWidth o imágenes rotas naturalWidth === 0)."
        )

    def _build_audit_messages(
        self,
        project_id: str,
        workspace_dir: Path,
        *,
        has_ui: bool,
        weights: Mapping[str, float],
        spec: Spec | None,
        plan: Plan | None,
        test_results: Mapping[str, bool] | None,
        challenge_for_polish: bool = False,
        browser_smoke_findings: Sequence[str] = (),
        extra_secrets: Sequence[str],
    ) -> list[dict[str, str]]:
        code_ctx = collect_workspace_code_context(
            workspace_dir,
            max_chars=24000,
            extra_secrets=extra_secrets,
        )
        spec_summary = (
            spec.model_dump_json(indent=2)
            if spec is not None
            else (plan.summary if plan is not None else "N/D")
        )
        weights_desc = ", ".join(
            f"{dim}: {weights[dim] * 100:.1f}%" for dim in QUALITY_DIMENSION_ORDER
        )
        rubric_lines = [
            f"- {dim} ({QUALITY_DIMENSION_META[dim]['label']}): {QUALITY_DIMENSION_META[dim]['focus']}"
            for dim in QUALITY_DIMENSION_ORDER
            if weights[dim] > 0.0
        ]
        polish_hint = (
            "\nModo PULIDO ADVERSARIO activo: incluso si el proyecto supera 85%, desafía cualquier "
            "punto mejorable (<100%) en UX/accesibilidad, robustez, pruebas o seguridad."
            if challenge_for_polish
            else ""
        )
        sys_prompt = (
            "Eres el Agente 1 · Crítico Adversario coordinando 6 Sub-Auditores de calidad de software "
            "en modo ESTRICTAMENTE DE SOLO LECTURA.\n"
            "Evalúa cada dimensión activa de 0 a 100 puntos basándote SOLO en evidencia verificable del código "
            "(nunca infles calificaciones sin respaldo).\n"
            f"{STRICT_RUBRIC_PROMPT_BLOCK}\n"
            "Emite objeciones accionables en `objections` con: `dimension`, `file_path`, `severity`, `issue`, `concrete_fix`."
            + polish_hint
            + "\nResponde únicamente en formato JSON válido siguiendo el esquema solicitado."
        )
        browser_block = self._format_browser_findings_block(browser_smoke_findings)
        user_prompt = redact_secrets(
            f"Proyecto: {project_id}\n"
            f"UI detectada: {'Sí' if has_ui else 'No (API/CLI puro)'}\n"
            f"Pesos adaptativos: {weights_desc}\n"
            f"Resultados de pruebas actuales: {dict(test_results or {})}\n\n"
            f"Criterios por Sub-Auditor:\n" + "\n".join(rubric_lines)
            + browser_block
            + f"\n\nEspecificación (Spec):\n{spec_summary[:4000]}\n\n"
            + f"Código del workspace:\n{code_ctx or '(Sin archivos detectados)'}",
            extra_secrets=extra_secrets,
        )
        return [
            {"role": "system", "content": sys_prompt},
            {"role": "user", "content": user_prompt},
        ]

    def _build_diff_review_messages(
        self,
        project_id: str,
        workspace_dir: Path,
        *,
        previous_report: QualityAuditReport,
        prior_objections: Sequence[DebateObjection],
        diff_text: str,
        has_ui: bool,
        weights: Mapping[str, float],
        spec: Spec | None,
        plan: Plan | None,
        test_results: Mapping[str, bool] | None,
        browser_smoke_findings: Sequence[str] = (),
        extra_secrets: Sequence[str],
    ) -> list[dict[str, str]]:
        code_ctx = collect_workspace_code_context(
            workspace_dir,
            max_chars=18000,
            extra_secrets=extra_secrets,
        )
        obj_lines = [
            (
                f"{idx + 1}. [{obj.dimension} | {obj.file_path or 'workspace'} | {obj.severity}] "
                f"{obj.issue} -> Fix esperado: {obj.concrete_fix}"
            )
            for idx, obj in enumerate(prior_objections)
        ] or ["(Sin objeciones previas registradas)"]
        spec_summary = (
            spec.model_dump_json(indent=2)
            if spec is not None
            else (plan.summary if plan is not None else "N/D")
        )
        weights_desc = ", ".join(
            f"{dim}: {weights[dim] * 100:.1f}%" for dim in QUALITY_DIMENSION_ORDER
        )
        sys_prompt = (
            "Eres el Agente 1 · Crítico Adversario realizando una RE-EVALUACIÓN BASADA EN `git diff`.\n"
            "Inspecciona el diff aplicado tras las correcciones del Constructor, comprueba cuántas objeciones "
            "fueron resueltas (`resolved_count`), resume lo verificado (`diff_summary`) y recalcula las 6 dimensiones "
            "con rigor de evidencia.\n"
            f"{STRICT_RUBRIC_PROMPT_BLOCK}\n"
            "Responde únicamente en JSON válido."
        )
        browser_block = self._format_browser_findings_block(browser_smoke_findings)
        user_prompt = redact_secrets(
            f"Proyecto: {project_id}\n"
            f"Puntaje previo: {previous_report.global_score:.1f}%\n"
            f"UI detectada: {'Sí' if has_ui else 'No (API/CLI puro)'} | Pesos: {weights_desc}\n"
            f"Resultados de pruebas actuales: {dict(test_results or {})}\n\n"
            f"Objeciones planteadas anteriormente ({len(prior_objections)}):\n"
            + "\n".join(obj_lines)
            + browser_block
            + "\n\nGit diff resultante tras la respuesta del Constructor:\n"
            + (diff_text.strip() or "(Sin diff registrado)")
            + f"\n\nSpec (resumen):\n{spec_summary[:2500]}\n\n"
            + f"Estado actual del workspace:\n{code_ctx or '(Sin archivos detectados)'}",
            extra_secrets=extra_secrets,
        )
        return [
            {"role": "system", "content": sys_prompt},
            {"role": "user", "content": user_prompt},
        ]

    @staticmethod
    def _coerce_objections(
        raw_objs: Sequence[Mapping[str, Any]],
        *,
        workspace_dir: Path,
        dimensions: Mapping[str, SubAuditResult],
        extra_secrets: Sequence[str] = (),
    ) -> list[DebateObjection]:
        out: list[DebateObjection] = []
        for item in raw_objs:
            issue = redact_secrets(str(item.get("issue") or item.get("problem") or "").strip(), extra_secrets=extra_secrets)
            if not issue:
                continue
            raw_dim = str(item.get("dimension") or "").strip().lower()
            dim = _DIMENSION_ALIASES.get(raw_dim, raw_dim or "functional_completeness")
            if dim not in dimensions:
                dim = "functional_completeness"
            if not dimensions[dim].active:
                continue
            file_path = redact_secrets(
                str(item.get("file_path") or item.get("file") or "").strip(),
                extra_secrets=extra_secrets,
            ) or _infer_workspace_file_for_dimension(workspace_dir, dim, issue)
            concrete_fix = redact_secrets(
                str(item.get("concrete_fix") or item.get("fix") or item.get("improvement") or "").strip(),
                extra_secrets=extra_secrets,
            )
            out.append(
                DebateObjection(
                    dimension=dim,
                    file_path=file_path,
                    severity=str(item.get("severity") or "medium"),
                    issue=issue,
                    concrete_fix=concrete_fix,
                )
            )
        return out

    def build_objections(
        self,
        workspace_dir: Path,
        report: QualityAuditReport,
        *,
        challenge_for_polish: bool = False,
        extra_secrets: Sequence[str] = (),
    ) -> list[DebateObjection]:
        """Extract or synthesize concrete file-scoped objections from a QualityAuditReport."""
        if report.objections:
            return list(report.objections)
        target_subs = _select_target_dimensions(
            report, challenge_for_polish=challenge_for_polish
        )
        if not target_subs:
            return []
        synthesized: list[DebateObjection] = []
        for sub in target_subs:
            needs_work = (
                sub.score < report.min_area_score
                or report.global_score < report.min_global_score
                or (challenge_for_polish and sub.score < 100.0)
            )
            if not needs_work:
                continue
            if sub.findings:
                for idx, finding in enumerate(sub.findings[:3]):
                    imp = sub.improvements[idx] if idx < len(sub.improvements) else (
                        sub.improvements[0] if sub.improvements else ""
                    )
                    fp = _infer_workspace_file_for_dimension(
                        workspace_dir, sub.dimension, f"{finding} {imp}"
                    )
                    synthesized.append(
                        DebateObjection(
                            dimension=sub.dimension,
                            file_path=fp,
                            severity="high" if sub.score < report.min_area_score else "medium",
                            issue=redact_secrets(finding, extra_secrets=extra_secrets),
                            concrete_fix=redact_secrets(imp, extra_secrets=extra_secrets),
                        )
                    )
            elif sub.improvements:
                for imp in sub.improvements[:2]:
                    fp = _infer_workspace_file_for_dimension(workspace_dir, sub.dimension, imp)
                    synthesized.append(
                        DebateObjection(
                            dimension=sub.dimension,
                            file_path=fp,
                            severity="medium",
                            issue=f"Pulir {sub.label} ({sub.score:.1f}% < 100%)",
                            concrete_fix=redact_secrets(imp, extra_secrets=extra_secrets),
                        )
                    )
            elif challenge_for_polish and sub.score < 100.0 and (not report.passed or report.global_score < 93.0):
                fp = _infer_workspace_file_for_dimension(workspace_dir, sub.dimension)
                synthesized.append(
                    DebateObjection(
                        dimension=sub.dimension,
                        file_path=fp,
                        severity="low",
                        issue=(
                            f"Refinar detalles de {sub.label} ({sub.score:.1f}%) hacia nivel Excelencia"
                        ),
                        concrete_fix=(
                            f"Fortalecer {QUALITY_DIMENSION_META[sub.dimension]['focus']}"
                        ),
                    )
                )
        return synthesized

    def _assemble_report(
        self,
        project_id: str,
        ws: Path,
        *,
        has_ui: bool,
        weights: Mapping[str, float],
        deterministic_dims: Mapping[str, dict[str, Any]],
        llm_dims: Mapping[str, dict[str, Any]],
        explicit_global: float | None,
        summary: str,
        raw_objs: Sequence[Mapping[str, Any]],
        source: str,
        round_number: int,
        project_name: str,
        version_label: str,
        challenge_for_polish: bool,
        browser_smoke_findings: Sequence[str] = (),
        extra_secrets: Sequence[str],
    ) -> QualityAuditReport:
        active_dims = [dim for dim in QUALITY_DIMENSION_ORDER if weights[dim] > 0.0]
        all_active_supplied = bool(active_dims) and all(dim in llm_dims for dim in active_dims)
        src_texts, test_texts = _safe_read_workspace_sources(ws, extra_secrets=extra_secrets)
        strict_caps = _detect_strict_rubric_violations(
            has_ui,
            src_texts,
            test_texts,
            browser_smoke_findings=browser_smoke_findings,
        )

        dimensions: dict[str, SubAuditResult] = {}
        all_findings: list[str] = []
        all_improvements: list[str] = []

        for dim in QUALITY_DIMENSION_ORDER:
            weight = weights[dim]
            active = weight > 0.0
            meta = QUALITY_DIMENSION_META[dim]
            chosen = llm_dims.get(dim) if (active and dim in llm_dims) else deterministic_dims[dim]
            score_val = _clamp_score(chosen.get("score"), default=80.0) if active else 100.0
            f_list = list(chosen.get("findings") or []) if active else []
            i_list = list(chosen.get("improvements") or []) if active else []
            if active and dim in strict_caps:
                cap_score, cap_findings, cap_improvements = strict_caps[dim]
                score_val = min(score_val, cap_score)
                f_list = _merge_unique_notes(f_list, cap_findings)
                i_list = _merge_unique_notes(i_list, cap_improvements)
            dimensions[dim] = SubAuditResult(
                dimension=dim,
                label=meta["label"],
                score=score_val,
                weight=weight,
                active=active,
                findings=f_list,
                improvements=i_list,
            )
            for item in f_list:
                all_findings.append(f"[{meta['label']}] {item}")
            for item in i_list:
                all_improvements.append(f"[{meta['label']}] {item}")

        weighted_global = round(
            sum(dimensions[dim].score * dimensions[dim].weight for dim in active_dims),
            2,
        )
        if strict_caps or all_active_supplied or explicit_global is None:
            global_score = weighted_global
        else:
            global_score = explicit_global

        failed_dims = [
            dim for dim in active_dims if dimensions[dim].score < self.min_area_score
        ]
        passed = (global_score >= self.min_global_score) and (len(failed_dims) == 0)
        tier_id, tier_label = classify_quality_tier(
            global_score,
            failed_dimensions=failed_dims,
            dimensions=dimensions,
            min_area_score=self.min_area_score,
        )

        if not summary:
            summary = (
                f"Auditoría {'aprobada' if passed else 'con observaciones'} "
                f"({tier_label} · {global_score:.1f}%)."
            )

        report = QualityAuditReport(
            global_score=global_score,
            passed=passed,
            has_ui=has_ui,
            min_global_score=self.min_global_score,
            min_area_score=self.min_area_score,
            round_number=round_number,
            tier_id=tier_id,
            tier_label=tier_label,
            dimensions=dimensions,
            failed_dimensions=failed_dims,
            summary=summary,
            findings=all_findings,
            improvements=all_improvements,
            source=source,
        )
        explicit_objs = self._coerce_objections(
            raw_objs,
            workspace_dir=ws,
            dimensions=dimensions,
            extra_secrets=extra_secrets,
        )
        report.objections = (
            explicit_objs
            if explicit_objs
            else self.build_objections(
                ws,
                report,
                challenge_for_polish=challenge_for_polish,
                extra_secrets=extra_secrets,
            )
        )
        report.markdown_report = build_quality_markdown_report(
            report,
            project_name=project_name or project_id,
            version_label=version_label,
            extra_secrets=extra_secrets,
        )
        return report

    def _run_llm_audit_messages(
        self,
        project_id: str,
        messages: list[dict[str, str]],
        *,
        extra_secrets: Sequence[str],
    ) -> tuple[dict[str, dict[str, Any]], float | None, str, list[dict[str, Any]], int | None, str, str]:
        try:
            raw_resp = _invoke_structured_llm(
                self.llm,
                messages,
                schema=ParentAuditLLMOutput,
                timeout_seconds=self.timeout_seconds,
                agent_name="AdversarialCritic",
            )
            llm_dims, explicit_global, summary, raw_objs, resolved_cnt, diff_summary = (
                self._extract_llm_payload(raw_resp, extra_secrets=extra_secrets)
            )
            source = "llm" if (llm_dims or explicit_global is not None) else "deterministic_fallback"
            return llm_dims, explicit_global, summary, raw_objs, resolved_cnt, diff_summary, source
        except (
            LLMError,
            InvalidLLMResponseError,
            CommandTimeoutError,
            OSError,
            RuntimeError,
            ValueError,
            TypeError,
        ) as exc:
            logger.warning(
                "AdversarialCritic falling back to deterministic evaluation for %r: %s",
                project_id,
                redact_secrets(str(exc), extra_secrets=extra_secrets),
            )
            return {}, None, "", [], None, "", "deterministic_fallback"

    def evaluate_workspace(
        self,
        project_id: str,
        workspace_dir: Path,
        *,
        spec: Spec | None = None,
        plan: Plan | None = None,
        test_results: Mapping[str, bool] | None = None,
        round_number: int = 1,
        project_name: str = "",
        version_label: str = "",
        challenge_for_polish: bool = False,
        browser_smoke_findings: Any = None,
        extra_secrets: Sequence[str] = (),
    ) -> QualityAuditReport:
        """Execute strict read-only Critic + 6 Sub-Auditors evaluation with fallback."""
        ws = Path(workspace_dir)
        has_ui = detect_workspace_has_ui(ws, plan=plan)
        weights = compute_area_weights(has_ui)
        norm_browser = _normalize_browser_findings(
            browser_smoke_findings, extra_secrets=extra_secrets
        )
        deterministic_dims = _run_deterministic_audit(
            ws,
            has_ui=has_ui,
            plan=plan,
            test_results=test_results,
            browser_smoke_findings=norm_browser,
            extra_secrets=extra_secrets,
        )
        messages = self._build_audit_messages(
            project_id,
            ws,
            has_ui=has_ui,
            weights=weights,
            spec=spec,
            plan=plan,
            test_results=test_results,
            challenge_for_polish=challenge_for_polish,
            browser_smoke_findings=norm_browser,
            extra_secrets=extra_secrets,
        )
        llm_dims, explicit_global, summary, raw_objs, _, _, source = (
            self._run_llm_audit_messages(project_id, messages, extra_secrets=extra_secrets)
        )
        return self._assemble_report(
            project_id,
            ws,
            has_ui=has_ui,
            weights=weights,
            deterministic_dims=deterministic_dims,
            llm_dims=llm_dims,
            explicit_global=explicit_global,
            summary=summary,
            raw_objs=raw_objs,
            source=source,
            round_number=round_number,
            project_name=project_name,
            version_label=version_label,
            challenge_for_polish=challenge_for_polish,
            browser_smoke_findings=norm_browser,
            extra_secrets=extra_secrets,
        )

    def review_refinement_diff(
        self,
        project_id: str,
        workspace_dir: Path,
        *,
        previous_report: QualityAuditReport,
        prior_objections: Sequence[DebateObjection],
        diff_text: str,
        spec: Spec | None = None,
        plan: Plan | None = None,
        test_results: Mapping[str, bool] | None = None,
        round_number: int = 2,
        project_name: str = "",
        version_label: str = "",
        challenge_for_polish: bool = False,
        browser_smoke_findings: Any = None,
        extra_secrets: Sequence[str] = (),
    ) -> tuple[QualityAuditReport, int, str]:
        """Inspect resulting git diff against prior objections to count fixes and re-score."""
        ws = Path(workspace_dir)
        has_ui = detect_workspace_has_ui(ws, plan=plan)
        weights = compute_area_weights(has_ui)
        norm_browser = _normalize_browser_findings(
            browser_smoke_findings, extra_secrets=extra_secrets
        )
        deterministic_dims = _run_deterministic_audit(
            ws,
            has_ui=has_ui,
            plan=plan,
            test_results=test_results,
            browser_smoke_findings=norm_browser,
            extra_secrets=extra_secrets,
        )
        messages = self._build_diff_review_messages(
            project_id,
            ws,
            previous_report=previous_report,
            prior_objections=prior_objections,
            diff_text=diff_text,
            has_ui=has_ui,
            weights=weights,
            spec=spec,
            plan=plan,
            test_results=test_results,
            browser_smoke_findings=norm_browser,
            extra_secrets=extra_secrets,
        )
        llm_dims, explicit_global, summary, raw_objs, explicit_resolved, diff_summary, source = (
            self._run_llm_audit_messages(project_id, messages, extra_secrets=extra_secrets)
        )
        new_report = self._assemble_report(
            project_id,
            ws,
            has_ui=has_ui,
            weights=weights,
            deterministic_dims=deterministic_dims,
            llm_dims=llm_dims,
            explicit_global=explicit_global,
            summary=summary,
            raw_objs=raw_objs,
            source=source,
            round_number=round_number,
            project_name=project_name,
            version_label=version_label,
            challenge_for_polish=challenge_for_polish,
            browser_smoke_findings=norm_browser,
            extra_secrets=extra_secrets,
        )

        if explicit_resolved is not None:
            resolved_count = max(0, min(len(prior_objections), explicit_resolved))
        elif not prior_objections:
            resolved_count = 0
        else:
            diff_low = diff_text.lower()
            resolved_count = 0
            for obj in prior_objections:
                prev_sub = previous_report.dimensions.get(obj.dimension)
                new_sub = new_report.dimensions.get(obj.dimension)
                score_rose = bool(
                    prev_sub is not None and new_sub is not None and new_sub.score > prev_sub.score
                )
                file_touched = bool(
                    obj.file_path
                    and (
                        obj.file_path.lower() in diff_low
                        or Path(obj.file_path).name.lower() in diff_low
                    )
                )
                if score_rose or (file_touched and new_report.global_score >= previous_report.global_score):
                    resolved_count += 1
        if not diff_summary:
            diff_summary = (
                f"Inspección de diff completada: {resolved_count}/{len(prior_objections)} "
                f"objeción(es) atendida(s); puntaje {previous_report.global_score:.1f}% → {new_report.global_score:.1f}%."
            )
        return new_report, resolved_count, diff_summary


class BuilderResponder:
    """Agent 2: Translates Critic objections into point-by-point responses and surgical code instructions."""

    def __init__(
        self,
        llm: LLM | None = None,
        *,
        timeout_seconds: float = 240.0,
    ) -> None:
        self.llm = llm
        self.timeout_seconds = float(timeout_seconds)

    def _can_invoke_llm(self) -> bool:
        if self.llm is None:
            return False
        if self.llm.__class__.__name__ != "FakeLLM":
            return True
        queued = getattr(self.llm, "_responses", None)
        if not isinstance(queued, list) or not queued:
            return False
        nxt = queued[0]
        if isinstance(nxt, Mapping):
            return any(k in nxt for k in ("builder_response", "surgical_instructions", "fixes"))
        if isinstance(nxt, str):
            try:
                parsed = json.loads(nxt)
                return isinstance(parsed, Mapping) and any(
                    k in parsed for k in ("builder_response", "surgical_instructions", "fixes")
                )
            except (ValueError, TypeError):
                return False
        return False

    @staticmethod
    def _deterministic_response(
        report: QualityAuditReport,
        objections: Sequence[DebateObjection],
        *,
        refinement_round: int,
        max_rounds: int,
        challenge_for_polish: bool,
        extra_secrets: Sequence[str],
    ) -> tuple[str, str]:
        resp_lines: list[str] = []
        if objections:
            for idx, obj in enumerate(objections, start=1):
                target = redact_secrets(obj.file_path or "workspace", extra_secrets=extra_secrets)
                fix_txt = redact_secrets(
                    obj.concrete_fix or "Aplicar corrección alineada con el criterio",
                    extra_secrets=extra_secrets,
                )
                issue_txt = redact_secrets(obj.issue, extra_secrets=extra_secrets)
                resp_lines.append(
                    f"{idx}. `{target}` ({obj.dimension}): ante «{issue_txt}», se aplicará {fix_txt}."
                )
        else:
            for sub in _select_target_dimensions(
                report, objections=objections, challenge_for_polish=challenge_for_polish
            ):
                fix_txt = (
                    redact_secrets(sub.improvements[0], extra_secrets=extra_secrets)
                    if sub.improvements
                    else f"Mejorar {sub.label}"
                )
                resp_lines.append(f"- ({sub.dimension}): {fix_txt}")
        builder_response = "\n".join(resp_lines).strip()
        surgical_instructions = format_refinement_feedback(
            report,
            refinement_round=refinement_round,
            max_rounds=max_rounds,
            objections=objections,
            challenge_for_polish=challenge_for_polish,
            extra_secrets=extra_secrets,
        )
        return builder_response, surgical_instructions

    def respond(
        self,
        project_id: str,
        workspace_dir: Path,
        report: QualityAuditReport,
        objections: Sequence[DebateObjection],
        *,
        refinement_round: int = 1,
        max_rounds: int = 2,
        challenge_for_polish: bool = False,
        extra_secrets: Sequence[str] = (),
    ) -> tuple[str, str]:
        """Generate structured point-by-point response and surgical instructions without touching tests/."""
        base_response, base_instructions = self._deterministic_response(
            report,
            objections,
            refinement_round=refinement_round,
            max_rounds=max_rounds,
            challenge_for_polish=challenge_for_polish,
            extra_secrets=extra_secrets,
        )
        if not self._can_invoke_llm():
            return base_response, base_instructions

        obj_summary = "\n".join(
            f"{idx + 1}. [{obj.dimension} | {obj.file_path or 'workspace'} | {obj.severity}] "
            f"{obj.issue} | Fix: {obj.concrete_fix}"
            for idx, obj in enumerate(objections)
        ) or base_instructions
        sys_prompt = (
            "Eres el Agente 2 · Constructor Refinador en un debate técnico de calidad.\n"
            "Responde punto por punto a las objeciones del Crítico Adversario explicando exactamente qué cambios "
            "aplicarás en cada archivo afectado y genera instrucciones quirúrgicas claras sin modificar `tests/`.\n"
            "Responde únicamente en JSON con campos: `builder_response` y `surgical_instructions`."
        )
        user_prompt = redact_secrets(
            f"Proyecto: {project_id}\n"
            f"Workspace: {Path(workspace_dir).name}\n"
            f"Ronda: {refinement_round}/{max_rounds} | Puntaje actual: {report.global_score:.1f}%\n\n"
            f"Objeciones del Crítico Adversario:\n{obj_summary}",
            extra_secrets=extra_secrets,
        )
        try:
            raw = _invoke_structured_llm(
                self.llm,
                [
                    {"role": "system", "content": sys_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                schema=BuilderResponseLLMOutput,
                timeout_seconds=self.timeout_seconds,
                agent_name="BuilderResponder",
            )
            if isinstance(raw, BuilderResponseLLMOutput):
                raw_dict = raw.model_dump(mode="python")
            elif isinstance(raw, Mapping):
                raw_dict = dict(raw)
            else:
                return base_response, base_instructions
            llm_resp = redact_secrets(
                str(raw_dict.get("builder_response") or "").strip(),
                extra_secrets=extra_secrets,
            )
            llm_instr = redact_secrets(
                str(raw_dict.get("surgical_instructions") or "").strip(),
                extra_secrets=extra_secrets,
            )
            eff_response = llm_resp or base_response
            eff_instructions = (
                f"{base_instructions}\n\nPlan quirúrgico del Constructor:\n{llm_instr}"
                if llm_instr
                else base_instructions
            )
            return eff_response, eff_instructions
        except (
            LLMError,
            InvalidLLMResponseError,
            CommandTimeoutError,
            OSError,
            RuntimeError,
            ValueError,
            TypeError,
        ) as exc:
            logger.warning(
                "BuilderResponder falling back to deterministic plan for %r: %s",
                project_id,
                redact_secrets(str(exc), extra_secrets=extra_secrets),
            )
            return base_response, base_instructions


class ParentAuditor:
    """Orchestrates AdversarialCritic + BuilderResponder + 6 Sub-Auditors evaluation cleanly."""

    def __init__(
        self,
        llm: LLM | None,
        *,
        min_global_score: float = 85.0,
        min_area_score: float = 70.0,
        timeout_seconds: float = 240.0,
        critic: AdversarialCritic | None = None,
        builder_responder: BuilderResponder | None = None,
    ) -> None:
        self.llm = llm
        self.min_global_score = float(min_global_score)
        self.min_area_score = float(min_area_score)
        self.timeout_seconds = float(timeout_seconds)
        self.critic = (
            critic
            if critic is not None
            else AdversarialCritic(
                llm,
                min_global_score=self.min_global_score,
                min_area_score=self.min_area_score,
                timeout_seconds=self.timeout_seconds,
            )
        )
        self.builder_responder = (
            builder_responder
            if builder_responder is not None
            else BuilderResponder(llm, timeout_seconds=self.timeout_seconds)
        )

    def _invoke_llm(self, messages: list[dict[str, str]]) -> Any:
        return _invoke_structured_llm(
            self.llm,
            messages,
            schema=ParentAuditLLMOutput,
            timeout_seconds=self.timeout_seconds,
            agent_name="ParentAuditor",
        )

    @staticmethod
    def _extract_llm_dimensions(
        raw_resp: Any,
        *,
        extra_secrets: Sequence[str] = (),
    ) -> tuple[dict[str, dict[str, Any]], float | None, str]:
        dims, explicit_global, summary, _, _, _ = AdversarialCritic._extract_llm_payload(
            raw_resp, extra_secrets=extra_secrets
        )
        return dims, explicit_global, summary

    def _build_audit_messages(
        self,
        project_id: str,
        workspace_dir: Path,
        *,
        has_ui: bool,
        weights: Mapping[str, float],
        spec: Spec | None,
        plan: Plan | None,
        test_results: Mapping[str, bool] | None,
        browser_smoke_findings: Sequence[str] = (),
        extra_secrets: Sequence[str],
    ) -> list[dict[str, str]]:
        return self.critic._build_audit_messages(
            project_id,
            workspace_dir,
            has_ui=has_ui,
            weights=weights,
            spec=spec,
            plan=plan,
            test_results=test_results,
            browser_smoke_findings=browser_smoke_findings,
            extra_secrets=extra_secrets,
        )

    def audit_workspace(
        self,
        project_id: str,
        workspace_dir: Path,
        *,
        spec: Spec | None = None,
        plan: Plan | None = None,
        test_results: Mapping[str, bool] | None = None,
        round_number: int = 1,
        project_name: str = "",
        version_label: str = "",
        challenge_for_polish: bool = False,
        browser_smoke_findings: Any = None,
        extra_secrets: Sequence[str] = (),
    ) -> QualityAuditReport:
        """Execute AdversarialCritic + 6 Sub-Auditors evaluation with adaptive weights and fallback."""
        return self.critic.evaluate_workspace(
            project_id,
            workspace_dir,
            spec=spec,
            plan=plan,
            test_results=test_results,
            round_number=round_number,
            project_name=project_name,
            version_label=version_label,
            challenge_for_polish=challenge_for_polish,
            browser_smoke_findings=browser_smoke_findings,
            extra_secrets=extra_secrets,
        )

    def review_refinement_diff(
        self,
        project_id: str,
        workspace_dir: Path,
        *,
        previous_report: QualityAuditReport,
        prior_objections: Sequence[DebateObjection],
        diff_text: str,
        spec: Spec | None = None,
        plan: Plan | None = None,
        test_results: Mapping[str, bool] | None = None,
        round_number: int = 2,
        project_name: str = "",
        version_label: str = "",
        challenge_for_polish: bool = False,
        browser_smoke_findings: Any = None,
        extra_secrets: Sequence[str] = (),
    ) -> tuple[QualityAuditReport, int, str]:
        """Delegate git-diff re-evaluation cleanly to AdversarialCritic."""
        return self.critic.review_refinement_diff(
            project_id,
            workspace_dir,
            previous_report=previous_report,
            prior_objections=prior_objections,
            diff_text=diff_text,
            spec=spec,
            plan=plan,
            test_results=test_results,
            round_number=round_number,
            project_name=project_name,
            version_label=version_label,
            challenge_for_polish=challenge_for_polish,
            browser_smoke_findings=browser_smoke_findings,
            extra_secrets=extra_secrets,
        )

    def respond_to_objections(
        self,
        project_id: str,
        workspace_dir: Path,
        report: QualityAuditReport,
        objections: Sequence[DebateObjection],
        *,
        refinement_round: int = 1,
        max_rounds: int = 2,
        challenge_for_polish: bool = False,
        extra_secrets: Sequence[str] = (),
    ) -> tuple[str, str]:
        """Delegate Builder point-by-point debate response cleanly to BuilderResponder."""
        return self.builder_responder.respond(
            project_id,
            workspace_dir,
            report,
            objections,
            refinement_round=refinement_round,
            max_rounds=max_rounds,
            challenge_for_polish=challenge_for_polish,
            extra_secrets=extra_secrets,
        )
