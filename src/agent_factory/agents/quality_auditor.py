"""Parent Auditor + 6 Sub-Auditors quality evaluation with adaptive weights (INV-19, INV-22)."""

from __future__ import annotations

import inspect
import logging
import re
import sys
from collections.abc import Mapping, Sequence
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

QUALITY_DIMENSION_META: dict[str, dict[str, str]] = {
    "ux_ui": {
        "label": "UX/UI",
        "focus": (
            "Jerarquía visual, contraste (claro/oscuro), responsividad (360px y 1280px+), "
            "estados de carga/vacío/error y accesibilidad básica (ARIA/foco)."
        ),
    },
    "functional_completeness": {
        "label": "Funcionalidad",
        "focus": (
            "Cruce entre cada requisito del Spec y el código real "
            "(sin botones de adorno ni funciones incompletas)."
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
            "requirements.txt completo y arranque limpio."
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
        out = {
            "summary": str(copied.get("summary") or ""),
            "global_score": (
                _clamp_score(copied["global_score"], default=0.0)
                if isinstance(copied.get("global_score"), (int, float))
                else None
            ),
            "dimensions": norm_dims,
            "raw_payload": copied,
        }
        return out


class SubAuditResult(BaseModel):
    """Evaluation output for a single quality sub-auditor dimension."""

    dimension: str
    label: str
    score: float = Field(ge=0.0, le=100.0)
    weight: float = Field(ge=0.0, le=1.0)
    active: bool = True
    findings: list[str] = Field(default_factory=list)
    improvements: list[str] = Field(default_factory=list)


class QualityAuditReport(BaseModel):
    """Aggregated quality evaluation report produced by ParentAuditor."""

    global_score: float = Field(ge=0.0, le=100.0)
    passed: bool
    has_ui: bool
    min_global_score: float = 85.0
    min_area_score: float = 70.0
    round_number: int = 1
    dimensions: dict[str, SubAuditResult]
    failed_dimensions: list[str] = Field(default_factory=list)
    summary: str = ""
    findings: list[str] = Field(default_factory=list)
    improvements: list[str] = Field(default_factory=list)
    markdown_report: str = ""
    source: str = "llm"

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
        return {
            "global_score": round(self.global_score, 2),
            "passed": self.passed,
            "has_ui": self.has_ui,
            "round_number": self.round_number,
            "min_global_score": self.min_global_score,
            "min_area_score": self.min_area_score,
            "failed_dimensions": list(self.failed_dimensions),
            "source": self.source,
            "summary": redact_secrets(self.summary, extra_secrets=extra_secrets),
            "areas": areas,
        }


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


def _run_deterministic_audit(
    workspace_dir: Path,
    *,
    has_ui: bool,
    plan: Plan | None = None,
    test_results: Mapping[str, bool] | None = None,
    extra_secrets: Sequence[str] = (),
) -> dict[str, dict[str, Any]]:
    ws = Path(workspace_dir)
    src_texts, test_texts = _safe_read_workspace_sources(ws, extra_secrets=extra_secrets)
    ux_s, ux_f, ux_i = _evaluate_ux_ui(has_ui, src_texts)
    sec_s, sec_f, sec_i = _evaluate_security(src_texts)
    tst_s, tst_f, tst_i = _evaluate_testing_runtime(ws, src_texts, test_texts, test_results)
    core = _evaluate_functional_robustness_arch(src_texts, plan)
    return {
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

    return "\n".join(lines).strip()


def format_refinement_feedback(
    report: QualityAuditReport,
    *,
    refinement_round: int,
    max_rounds: int,
    extra_secrets: Sequence[str] = (),
) -> str:
    """Build focused refinement instructions containing only penalized/failing dimensions (<70% or <85%)."""
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
    if not target_subs:
        target_subs = sorted(
            (report.dimensions[dim] for dim in QUALITY_DIMENSION_ORDER if report.dimensions[dim].active),
            key=lambda s: s.score,
        )[:2]

    lines = [
        f"[AUDITORÍA DE CALIDAD · RONDA DE REFINAMIENTO {refinement_round}/{max_rounds}]",
        (
            f"Puntaje global actual: {report.global_score:.1f}% "
            f"(requerido >= {report.min_global_score:.0f}%, mínimo por área >= {report.min_area_score:.0f}%)."
        ),
        "Corrige ÚNICAMENTE las áreas penalizadas manteniendo intactas todas las pruebas actuales:",
    ]
    for sub in target_subs:
        lines.append(f"- {sub.dimension} ({sub.label}): {sub.score:.1f}%")
        for f_txt in sub.findings[:4]:
            lines.append(f"  * Hallazgo: {redact_secrets(f_txt, extra_secrets=extra_secrets)}")
        for imp_txt in sub.improvements[:4]:
            lines.append(f"  * Mejora: {redact_secrets(imp_txt, extra_secrets=extra_secrets)}")
    return "\n".join(lines)


class ParentAuditor:
    """Orchestrates Parent Auditor + 6 Sub-Auditors evaluation with tolerant parsing & fallback."""

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

    def _invoke_llm(self, messages: list[dict[str, str]]) -> Any:
        if self.llm is None:
            raise LLMError("No LLM configured for ParentAuditor")
        gen_fn = getattr(self.llm, "generate", None)
        target_fn = gen_fn if callable(gen_fn) else self.llm.complete
        try:
            sig = inspect.signature(target_fn)
            if "timeout_seconds" in sig.parameters:
                return target_fn(
                    messages,
                    schema=ParentAuditLLMOutput,
                    timeout_seconds=self.timeout_seconds,
                )
        except (TypeError, ValueError):
            pass
        return target_fn(messages, schema=ParentAuditLLMOutput)

    @staticmethod
    def _extract_llm_dimensions(
        raw_resp: Any,
        *,
        extra_secrets: Sequence[str] = (),
    ) -> tuple[dict[str, dict[str, Any]], float | None, str]:
        if isinstance(raw_resp, ParentAuditLLMOutput):
            payload: dict[str, Any] = dict(raw_resp.raw_payload or {})
            if not payload:
                payload = raw_resp.model_dump(mode="python")
        elif isinstance(raw_resp, Mapping):
            payload = dict(raw_resp.get("raw_payload") or raw_resp)
            for k in ("dimensions", "summary", "global_score"):
                if k not in payload and k in raw_resp:
                    payload[k] = raw_resp[k]
        else:
            return {}, None, ""

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
        summary = redact_secrets(str(payload.get("summary") or "").strip(), extra_secrets=extra_secrets)
        return parsed_dims, explicit_global, summary

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
        sys_prompt = (
            "Eres el Auditor Padre coordinando 6 Sub-Auditores de calidad de software en modo ESTRICTAMENTE DE SOLO LECTURA.\n"
            "Evalúa cada dimensión activa de 0 a 100 puntos con hallazgos concretos y acciones de mejora.\n"
            "Responde únicamente en formato JSON válido siguiendo el esquema solicitado."
        )
        user_prompt = redact_secrets(
            f"Proyecto: {project_id}\n"
            f"UI detectada: {'Sí' if has_ui else 'No (API/CLI puro)'}\n"
            f"Pesos adaptativos: {weights_desc}\n"
            f"Resultados de pruebas actuales: {dict(test_results or {})}\n\n"
            f"Criterios por Sub-Auditor:\n" + "\n".join(rubric_lines) + "\n\n"
            f"Especificación (Spec):\n{spec_summary[:4000]}\n\n"
            f"Código del workspace:\n{code_ctx or '(Sin archivos detectados)'}",
            extra_secrets=extra_secrets,
        )
        return [
            {"role": "system", "content": sys_prompt},
            {"role": "user", "content": user_prompt},
        ]

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
        extra_secrets: Sequence[str] = (),
    ) -> QualityAuditReport:
        """Execute Parent Auditor + 6 Sub-Auditors evaluation with adaptive weights and fallback."""
        ws = Path(workspace_dir)
        has_ui = detect_workspace_has_ui(ws, plan=plan)
        weights = compute_area_weights(has_ui)
        deterministic_dims = _run_deterministic_audit(
            ws,
            has_ui=has_ui,
            plan=plan,
            test_results=test_results,
            extra_secrets=extra_secrets,
        )

        llm_dims: dict[str, dict[str, Any]] = {}
        explicit_global: float | None = None
        summary = ""
        source = "llm"

        messages = self._build_audit_messages(
            project_id,
            ws,
            has_ui=has_ui,
            weights=weights,
            spec=spec,
            plan=plan,
            test_results=test_results,
            extra_secrets=extra_secrets,
        )
        try:
            raw_resp = self._invoke_llm(messages)
            llm_dims, explicit_global, summary = self._extract_llm_dimensions(
                raw_resp,
                extra_secrets=extra_secrets,
            )
            if not llm_dims and explicit_global is None:
                source = "deterministic_fallback"
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
                "ParentAuditor falling back to deterministic evaluation for %r: %s",
                project_id,
                redact_secrets(str(exc), extra_secrets=extra_secrets),
            )
            source = "deterministic_fallback"

        active_dims = [dim for dim in QUALITY_DIMENSION_ORDER if weights[dim] > 0.0]
        all_active_supplied = bool(active_dims) and all(dim in llm_dims for dim in active_dims)

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
        if all_active_supplied or explicit_global is None:
            global_score = weighted_global
        else:
            global_score = explicit_global

        failed_dims = [
            dim for dim in active_dims if dimensions[dim].score < self.min_area_score
        ]
        passed = (global_score >= self.min_global_score) and (len(failed_dims) == 0)

        if not summary:
            summary = (
                f"Auditoría {'aprobada' if passed else 'con observaciones'} "
                f"({global_score:.1f}% global)."
            )

        report = QualityAuditReport(
            global_score=global_score,
            passed=passed,
            has_ui=has_ui,
            min_global_score=self.min_global_score,
            min_area_score=self.min_area_score,
            round_number=round_number,
            dimensions=dimensions,
            failed_dimensions=failed_dims,
            summary=summary,
            findings=all_findings,
            improvements=all_improvements,
            source=source,
        )
        report.markdown_report = build_quality_markdown_report(
            report,
            project_name=project_name or project_id,
            version_label=version_label,
            extra_secrets=extra_secrets,
        )
        return report
