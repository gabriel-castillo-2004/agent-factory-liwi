"""State machine orchestrator, approval gate, and circuit breaker (INV-05, INV-17)."""

import inspect
import json
import logging
import os
import re
import tempfile
import threading
import time
import tomllib
import unicodedata
import uuid
from collections.abc import Mapping, Sequence
from collections.abc import Set as AbstractSet
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict

from agent_factory.adapters.model_resolver import ACTIVITY_MESSAGES
from agent_factory.adapters.system_clock import SystemClock
from agent_factory.agents.interviewer import Interviewer
from agent_factory.agents.mediator import (
    Mediator,
    PlanValidationError,
    _PipelineLLMWrapper,
    run_debate_pipeline,
)
from agent_factory.agents.pragmatist import PragmatistProposal
from agent_factory.agents.quality_auditor import ParentAuditor, ensure_scorecard_tier
from agent_factory.agents.structuralist import StructuralistCritique
from agent_factory.domain.events import Event, EventType, create_event
from agent_factory.domain.models import (
    VALID_TEMPLATES,
    AcceptanceTest,
    AcceptanceTestType,
    ApprovalEvent,
    Plan,
    ProjectStatus,
    Spec,
    validate_project_id,
)
from agent_factory.domain.progress import project_status_from_events
from agent_factory.domain.states import IllegalTransition, ProjectState, validate_transition
from agent_factory.inbox import Inbox
from agent_factory.ports.clock import Clock
from agent_factory.ports.llm import LLM, LLMError
from agent_factory.ports.publisher import RegistryPublishError, StatusPublisher
from agent_factory.ports.runner import CommandTimeoutError
from agent_factory.ports.store import EventStore
from agent_factory.secrets import redact_secrets
from agent_factory.workspace import (
    DEFAULT_WORKSPACE_PYTEST_INI,
    SandboxViolationError,
    WorkspaceManager,
    collect_workspace_code_context,
    list_workspace_files,
    read_git_head_sha_from_disk,
    read_workspace_git_diff,
    read_workspace_git_head_sha,
    sanitize_target_files,
    validate_commit_sha,
    validate_external_project_path,
    verify_isolated_git_root,
)

logger = logging.getLogger(__name__)

DEFAULT_STATE_LIMITS: dict[str, int] = {
    "INTERVIEW": 6,
    "DEBATE": 6,
    "MEDIATION": 3,
    "BUILDING": 15,
    "VERIFYING": 3,
    "ci_rounds": 3,
}

_CTRL_CHARS_RE = re.compile(r"[\x00-\x1f\x7f]")
_LEADING_FILLER_RE = re.compile(
    r"^(?:"
    r"hola[\s,!:.-]*|"
    r"por\s+favor[\s,!:.-]*|"
    r"please[\s,!:.-]*|"
    r"(?:me\s+gustar[ií]a|quisiera|quiero|necesito|deseo|podr[ií]as|puedes|ay[uú]dame\s+a)\s+(?:que\s+|a\s+)?|"
    r"(?:i\s+want(?:\s+you)?\s+to|i\s+need(?:\s+you)?\s+to|i\s+want|i\s+need|can\s+you|build\s+me)\s+|"
    r"(?:hazme|haz|cr[eé]ame|crea|crear|construye|construir|desarrolla|desarrollar|dise[ñn]a|dise[ñn]ar|genera|generar|arma|armar)\s+|"
    r"(?:create|build|make|develop|design|generate)\s+|"
    r"(?:una?|el|la|los|las|a|an|the)\s+"
    r"(?:aplicaci[oó]n\s+web|app\s+web|aplicaci[oó]n|app|p[aá]gina\s+web|sitio\s+web|web\s+app|web|sistema|herramienta|plataforma|proyecto|programa|software|servicio|bot|chatbot)"
    r"(?:\s+(?:que\s+(?:sea|permita|tenga|haga|sirva\s+para)|para|de|sobre|acerca\s+de|with|for|to|about|that))?\s*|"
    r"(?:una?|el|la|a|an|the|para|de|for|to)\s+"
    r")+",
    re.IGNORECASE,
)


def sanitize_project_title(title: str, max_len: int = 80) -> str:
    """Strip control characters and angle brackets, collapse whitespace, and validate title length."""
    if not isinstance(title, str):
        raise TypeError("El título debe ser texto.")
    cleaned = _CTRL_CHARS_RE.sub(" ", title)
    cleaned = cleaned.replace("<", " ").replace(">", " ")
    cleaned = " ".join(cleaned.split()).strip(" .,-:;")
    if not cleaned:
        raise ValueError("El título no puede estar vacío.")
    if len(cleaned) > max_len:
        raise ValueError(f"El título no puede exceder {max_len} caracteres.")
    return cleaned


def synthesize_project_title(prompt: str, max_len: int = 55) -> str:
    """Synthesize a concise <=max_len project title from a raw user prompt, removing filler openers."""
    collapsed = _CTRL_CHARS_RE.sub(" ", str(prompt or ""))
    collapsed = " ".join(collapsed.split()).strip()
    if not collapsed:
        return "Nuevo proyecto"

    first_sentence = re.split(r"(?<=[.!?])\s+", collapsed, maxsplit=1)[0].strip()
    candidate = first_sentence or collapsed

    stripped = _LEADING_FILLER_RE.sub("", candidate).strip(" ,.:;-")
    if len(stripped) >= 3:
        candidate = stripped
    else:
        stripped_full = _LEADING_FILLER_RE.sub("", collapsed).strip(" ,.:;-")
        if len(stripped_full) >= 3:
            candidate = stripped_full

    if len(candidate) > max_len:
        for sep in (", ", " con ", " que ", " para ", " y ", " using ", " with ", " that "):
            idx = candidate.lower().find(sep, 12)
            if 10 <= idx <= max_len:
                candidate = candidate[:idx].strip(" ,.:;-")
                break

    if len(candidate) > max_len:
        words = candidate.split()
        acc: list[str] = []
        for word in words:
            trial = " ".join([*acc, word])
            if len(trial) > max_len:
                break
            acc.append(word)
        candidate = " ".join(acc).strip(" ,.:;-") if acc else candidate[:max_len].strip()

    candidate = candidate.strip(" ,.:;-") or collapsed[:max_len].strip()
    if candidate and candidate[0].islower():
        candidate = candidate[0].upper() + candidate[1:]
    return candidate[:max_len]


_REPO_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)
_REPO_H1_RE = re.compile(r"<h1[^>]*>(.*?)</h1>", re.IGNORECASE | re.DOTALL)
_REPO_META_DESC_RE = re.compile(
    r"""<meta[^>]+name=["']description["'][^>]+content=["'](.*?)["']""",
    re.IGNORECASE | re.DOTALL,
)
_PY_DOCSTRING_RE = re.compile(
    r'^(?:\s*(?:#[^\n]*\n|from\s+__future__\s+import\s+[^\n]+\n))*\s*(?:"""(.*?)"""|\'\'\'(.*?)\'\'\')',
    re.DOTALL,
)
_GENERIC_APP_TITLES: frozenset[str] = frozenset(
    {
        "static web app",
        "static web application",
        "agent factory",
        "agent-factory",
        "agent-factory-app",
        "agent-factory-static-web-template",
        "base template",
        "app",
        "web app",
        "index",
        "document",
        "home",
        "proyecto",
        "nuevo proyecto",
    }
)
_TEMPLATE_NOISE_PHRASES: tuple[str, ...] = (
    "agent factory",
    "agent-factory",
    "static web application",
    "base template",
    "100% static web",
    "static web app",
    "offline-ready, zero backend",
)
_SPANISH_HINT_RE = re.compile(
    r"[áéíóúñü¿¡]|\b(?:de|del|para|con|una?|el|la|los|las|local|locales|localmente|aplicaci[oó]n|descargar|descargador|m[uú]sica|canciones|archivo|archivos|gestor|temporizador|notas|diario|sin|desde)\b",
    re.IGNORECASE,
)
_TECH_ENGLISH_NOISE_RE = re.compile(
    r"\b(?:application\s+factory|middleware|entrypoint|blueprint|wsgi|asgi|server\s+module|unit\s+test|acceptance\s+test|utility\s+functions|helper\s+module|waitress|flask\s+app(?:lication)?)\b",
    re.IGNORECASE,
)
_TITLE_SKIP_SCAN_DIRS: frozenset[str] = frozenset(
    {".git", "tests", "__pycache__", ".venv", "venv", ".agent-factory", "node_modules"}
)


def _is_template_text(text: str) -> bool:
    """Return True if text is empty, generic placeholder, or base template boilerplate."""
    low = " ".join(str(text or "").lower().split()).strip(" .,:;-_#*`\"'")
    if not low:
        return True
    if low in _GENERIC_APP_TITLES:
        return True
    return any(phrase in low for phrase in _TEMPLATE_NOISE_PHRASES)


def _split_title_pair(raw_text: str) -> tuple[str, str]:
    """Split 'Brand - Description' / 'Brand — Description' / 'Brand | Description' cleanly."""
    cleaned = " ".join(re.sub(r"<[^>]+>", " ", str(raw_text or "")).split()).strip()
    for sep in (" - ", " — ", " – ", " | "):
        if sep in cleaned:
            left, right = cleaned.split(sep, 1)
            return left.strip(), right.strip()
    return cleaned, ""


def _extract_python_doc_hints(py_txt: str) -> list[str]:
    """Extract clean non-template summary lines from leading comments and module docstring."""
    hints: list[str] = []
    for raw_line in py_txt.splitlines()[:12]:
        s = raw_line.strip()
        if not s:
            continue
        if s.startswith("#"):
            comment = s.lstrip("#").strip()
            low_c = comment.lower()
            if (
                comment
                and not s.startswith("#!")
                and not low_c.startswith(("coding:", "-*-", "type:", "noqa", "ruff:"))
                and not _is_template_text(comment)
            ):
                hints.append(comment)
            continue
        break
    m_doc = _PY_DOCSTRING_RE.match(py_txt)
    if m_doc:
        raw_doc = (m_doc.group(1) or m_doc.group(2) or "").strip()
        first_doc_line = raw_doc.splitlines()[0].strip() if raw_doc else ""
        if first_doc_line and not _is_template_text(first_doc_line):
            hints.append(first_doc_line)
    return hints


def _read_contained_text_snippet(
    file_path: Path,
    workspace_dir: Path,
    *,
    max_bytes: int = 8192,
    extra_secrets: Sequence[str] = (),
) -> str:
    """Safely read up to max_bytes UTF-8 from file_path strictly inside workspace_dir (INV-19, INV-22)."""
    ws_resolved = workspace_dir.resolve()
    if not file_path.exists() or file_path.is_symlink():
        return ""
    resolved = file_path.resolve()
    if resolved != ws_resolved and not resolved.is_relative_to(ws_resolved):
        return ""
    if not resolved.is_file():
        return ""
    try:
        raw = resolved.read_bytes()[:max_bytes].decode("utf-8", errors="replace")
    except OSError:
        return ""
    return redact_secrets(raw, extra_secrets=extra_secrets)


def _clean_short_brand(raw_name: str, max_len: int = 22) -> str:
    """Extract a crisp 1-3 word friendly brand name within max_len, filtering template placeholders."""
    if not raw_name or _is_template_text(raw_name):
        return ""
    head, _ = _split_title_pair(raw_name)
    cleaned = _CTRL_CHARS_RE.sub(" ", head)
    cleaned = re.sub(r"<[^>]+>", " ", cleaned)
    cleaned = " ".join(cleaned.split()).strip(" .,:;-_#*`\"'")
    if _is_template_text(cleaned):
        return ""
    cleaned = _LEADING_FILLER_RE.sub("", cleaned).strip(" .,:;-_#*`\"'")
    if _is_template_text(cleaned):
        return ""
    words = [w.strip(",.:;()[]") for w in cleaned.split() if w.strip(",.:;()[]")]
    picked: list[str] = []
    for word in words[:3]:
        trial = " ".join([*picked, word])
        if len(trial) > max_len:
            break
        picked.append(word)
    while len(picked) > 1 and picked[-1].lower() in {
        "de",
        "del",
        "la",
        "el",
        "en",
        "con",
        "para",
        "y",
        "a",
        "the",
        "for",
        "with",
        "of",
        "and",
    }:
        picked.pop()
    out = " ".join(picked) if picked else (words[0][:max_len] if words else "")
    if _is_template_text(out):
        return ""
    if out and out[0].islower():
        out = out[0].upper() + out[1:]
    return out


def _collect_workspace_html_paths(ws: Path) -> list[Path]:
    """Return contained non-symlink HTML files ordered with src/**/templates/*.html before root index.html."""
    candidates: list[tuple[int, str, Path]] = []
    for p in ws.rglob("*.html"):
        if p.is_symlink():
            continue
        try:
            resolved = p.resolve()
        except OSError:
            continue
        if not resolved.is_relative_to(ws) or not resolved.is_file():
            continue
        rel_parts = resolved.relative_to(ws).parts
        if len(rel_parts) > 5 or any(part in _TITLE_SKIP_SCAN_DIRS for part in rel_parts):
            continue
        rel_posix = resolved.relative_to(ws).as_posix()
        low_rel = rel_posix.lower()
        name_low = resolved.name.lower()
        if low_rel.startswith("src/") and "templates/" in low_rel:
            tier = 0 if name_low == "index.html" else 1
        elif low_rel.startswith("src/"):
            tier = 2 if name_low == "index.html" else 3
        elif rel_posix != "index.html" and name_low == "index.html":
            tier = 4
        elif rel_posix != "index.html":
            tier = 5
        else:
            tier = 6
        candidates.append((tier, rel_posix, resolved))
    candidates.sort(key=lambda item: (item[0], len(item[1]), item[1]))
    return [item[2] for item in candidates]


def _humanize_package_dir_name(ws: Path) -> str:
    """Derive a clean readable brand name from src/<package>/ when explicit HTML/README titles are absent."""
    src_dir = ws / "src"
    if not src_dir.is_dir() or src_dir.is_symlink():
        return ""
    acronyms = {"yt", "dl", "ai", "ui", "api", "cli", "pdf", "csv", "mp3", "tv", "db", "sql"}
    for child in sorted(src_dir.iterdir(), key=lambda c: c.name.lower()):
        if (
            not child.is_dir()
            or child.is_symlink()
            or child.name.startswith((".", "_"))
            or child.name in _TITLE_SKIP_SCAN_DIRS
        ):
            continue
        if not (child / "__init__.py").is_file():
            continue
        tokens = [t for t in re.split(r"[-_]+", child.name) if t]
        pretty = " ".join(t.upper() if t.lower() in acronyms else t.capitalize() for t in tokens)
        if pretty and not _is_template_text(pretty):
            return pretty
    return ""


def suggest_title_from_repo(
    workspace_dir: Path | None,
    *,
    current_name: str = "",
    objective: str = "",
    plan_summary: str = "",
    extra_secrets: Sequence[str] = (),
    max_len: int = 65,
) -> str:
    """Analyze workspace files, objective, and plan to suggest 'Nombre - Descripción breve' <= 65 chars (INV-19)."""
    readme_heading = ""
    readme_desc = ""
    html_title_brand = ""
    html_title_desc = ""
    html_h1_brand = ""
    html_h1_desc = ""
    html_desc = ""
    pkg_name = ""
    pkg_desc = ""
    pyproj_name = ""
    pyproj_desc = ""
    pkg_dir_brand = ""
    py_docs: list[str] = []
    py_combined = ""

    if workspace_dir is not None and workspace_dir.is_dir():
        ws = workspace_dir.resolve()
        for readme_name in ("README.md", "README.txt", "readme.md"):
            txt = _read_contained_text_snippet(
                ws / readme_name, ws, extra_secrets=extra_secrets
            )
            if not txt:
                continue
            for line in txt.splitlines():
                stripped = line.strip()
                if not stripped:
                    continue
                if stripped.startswith("#"):
                    cand_h = stripped.lstrip("#").strip()
                    if cand_h and not _is_template_text(cand_h) and not readme_heading:
                        readme_heading = cand_h
                elif (
                    not stripped.startswith(("#", "!", "[", "```", "|", "---", "==="))
                    and not _is_template_text(stripped)
                    and not readme_desc
                ):
                    readme_desc = stripped
                if readme_heading and readme_desc:
                    break
            break

        pkg_txt = _read_contained_text_snippet(
            ws / "package.json", ws, extra_secrets=extra_secrets
        )
        if pkg_txt:
            try:
                pkg = json.loads(pkg_txt)
                if isinstance(pkg, dict):
                    raw_pn = str(pkg.get("name", "")).strip()
                    pretty_pn = raw_pn.replace("-", " ").replace("_", " ").strip()
                    if raw_pn and not _is_template_text(raw_pn) and not _is_template_text(pretty_pn):
                        pkg_name = pretty_pn
                    raw_pd = str(pkg.get("description", "")).strip()
                    if raw_pd and not _is_template_text(raw_pd):
                        pkg_desc = raw_pd
            except (json.JSONDecodeError, ValueError):
                pass

        pyproj_txt = _read_contained_text_snippet(
            ws / "pyproject.toml", ws, extra_secrets=extra_secrets
        )
        if pyproj_txt:
            try:
                pyproj = tomllib.loads(pyproj_txt)
                proj_meta = pyproj.get("project", {}) if isinstance(pyproj, dict) else {}
                if isinstance(proj_meta, dict):
                    raw_ppn = str(proj_meta.get("name", "")).strip()
                    pretty_ppn = raw_ppn.replace("-", " ").replace("_", " ").strip()
                    if raw_ppn and not _is_template_text(raw_ppn) and not _is_template_text(pretty_ppn):
                        pyproj_name = pretty_ppn
                    raw_ppd = str(proj_meta.get("description", "")).strip()
                    if raw_ppd and not _is_template_text(raw_ppd):
                        pyproj_desc = raw_ppd
            except tomllib.TOMLDecodeError:
                pass

        for html_path in _collect_workspace_html_paths(ws)[:8]:
            html_txt = _read_contained_text_snippet(
                html_path, ws, extra_secrets=extra_secrets
            )
            if not html_txt:
                continue
            m_t = _REPO_TITLE_RE.search(html_txt)
            if m_t:
                t_raw = " ".join(m_t.group(1).split()).strip()
                t_left, t_right = _split_title_pair(t_raw)
                if t_left and not _is_template_text(t_left) and not html_title_brand:
                    html_title_brand = t_left
                if t_right and not _is_template_text(t_right) and not html_title_desc:
                    html_title_desc = t_right
            m_h1 = _REPO_H1_RE.search(html_txt)
            if m_h1:
                h_raw = " ".join(re.sub(r"<[^>]+>", " ", m_h1.group(1)).split()).strip()
                h_left, h_right = _split_title_pair(h_raw)
                if h_left and not _is_template_text(h_left) and not html_h1_brand:
                    html_h1_brand = h_left
                if h_right and not _is_template_text(h_right) and not html_h1_desc:
                    html_h1_desc = h_right
                elif (
                    not h_right
                    and h_raw
                    and not _is_template_text(h_raw)
                    and h_raw.lower() != html_title_brand.lower()
                    and not html_h1_desc
                    and len(h_raw.split()) >= 3
                ):
                    html_h1_desc = h_raw
            m_desc = _REPO_META_DESC_RE.search(html_txt)
            if m_desc and not html_desc:
                md_val = " ".join(m_desc.group(1).split()).strip()
                if md_val and not _is_template_text(md_val):
                    html_desc = md_val

        py_candidates: list[tuple[int, str, Path]] = []
        for py_path in ws.rglob("*.py"):
            if py_path.is_symlink():
                continue
            try:
                resolved_py = py_path.resolve()
            except OSError:
                continue
            if not resolved_py.is_relative_to(ws) or not resolved_py.is_file():
                continue
            rel_parts = resolved_py.relative_to(ws).parts
            if len(rel_parts) > 4 or any(part in _TITLE_SKIP_SCAN_DIRS for part in rel_parts):
                continue
            if resolved_py.name.startswith("test_") or resolved_py.name.endswith("_test.py") or resolved_py.name == "conftest.py":
                continue
            rel_posix = resolved_py.relative_to(ws).as_posix()
            tier = 0 if rel_posix.lower().startswith("src/") else 1
            py_candidates.append((tier, rel_posix, resolved_py))
        py_candidates.sort(key=lambda item: (item[0], len(item[1]), item[1]))

        py_snippets: list[str] = []
        for _, _, py_path in py_candidates[:8]:
            py_txt = _read_contained_text_snippet(
                py_path, ws, max_bytes=4096, extra_secrets=extra_secrets
            )
            if not py_txt:
                continue
            py_snippets.append(py_txt)
            for doc_hint in _extract_python_doc_hints(py_txt):
                if doc_hint not in py_docs:
                    py_docs.append(doc_hint)
        py_combined = "\n".join(py_snippets).lower()
        pkg_dir_brand = _humanize_package_dir_name(ws)

    # 1. Pick short creative brand name ignoring base template strings
    brand = ""
    for candidate_name in (
        html_title_brand,
        html_h1_brand,
        readme_heading,
        pyproj_name,
        pkg_name,
        pkg_dir_brand,
        current_name,
    ):
        brand = _clean_short_brand(candidate_name)
        if brand:
            break

    context_all = " ".join(
        part
        for part in (
            readme_heading,
            readme_desc,
            html_title_brand,
            html_h1_brand,
            html_h1_desc,
            html_desc,
            pkg_desc,
            pyproj_desc,
            *py_docs,
            objective,
            plan_summary,
            current_name,
        )
        if part
    )
    low_ctx = f"{context_all.lower()} {py_combined}"
    if not brand:
        if any(w in low_ctx for w in ("music", "músic", "cancion", "canción", "audio", "mp3", "yt_dlp", "yt-dlp")):
            brand = "Musiquita"
        elif any(w in low_ctx for w in ("pomodoro", "temporizador", "timer")):
            brand = "Pomodoro"
        elif any(w in low_ctx for w in ("nota", "diario", "journal", "markdown")):
            brand = "Bitácora"
        elif any(w in low_ctx for w in ("gasto", "presupuesto", "finanza")):
            brand = "Finanzas"
        else:
            brand = _clean_short_brand(synthesize_project_title(objective or "MiApp", max_len=20)) or "MiApp"

    # 2. Pick concise friendly description matching objective language when possible
    prefer_spanish = bool(_SPANISH_HINT_RE.search(objective or "")) or not (objective or "").strip()
    raw_desc_candidates: list[str] = [
        html_h1_desc,
        html_title_desc,
        html_desc,
        readme_desc,
        pyproj_desc,
        pkg_desc,
        *py_docs,
        objective,
        plan_summary,
    ]
    cleaned_candidates: list[str] = []
    for cand_desc in raw_desc_candidates:
        clean_cand = _CTRL_CHARS_RE.sub(" ", str(cand_desc or "")).strip()
        if not clean_cand or _is_template_text(clean_cand) or _TECH_ENGLISH_NOISE_RE.search(clean_cand):
            continue
        cand_left, cand_right = _split_title_pair(clean_cand)
        if cand_right and brand and cand_left.lower() == brand.lower():
            clean_cand = cand_right
        elif brand and clean_cand.lower().startswith(f"{brand.lower()}:"):
            clean_cand = clean_cand[len(brand) + 1 :].strip()
        if _is_template_text(clean_cand) or _TECH_ENGLISH_NOISE_RE.search(clean_cand):
            continue
        cleaned_candidates.append(clean_cand)

    desc_source = ""
    if prefer_spanish:
        for cand in cleaned_candidates:
            if _SPANISH_HINT_RE.search(cand):
                desc_source = cand
                break
    if not desc_source and cleaned_candidates:
        desc_source = cleaned_candidates[0]

    if not desc_source:
        if "yt_dlp" in low_ctx or "yt-dlp" in low_ctx or "music" in low_ctx or "músic" in low_ctx:
            desc_source = "Descargador local de música"
        elif "pomodoro" in low_ctx or "temporizador" in low_ctx:
            desc_source = "Temporizador minimalista para enfocarse"
        else:
            desc_source = "Aplicación web interactiva local"

    desc = redact_secrets(desc_source, extra_secrets=extra_secrets)
    desc = " ".join(desc.split()).strip(" .,-:;")
    desc = _LEADING_FILLER_RE.sub("", desc).strip(" .,-:;")
    desc = re.sub(
        r"\b(?:local\s+)?en\s+python(?:\s+con\s+interfaz(?:\s+web)?)?\s+para\b",
        "local para",
        desc,
        flags=re.IGNORECASE,
    )
    desc = re.sub(r"^local\s+para\s+", "App local para ", desc, flags=re.IGNORECASE)
    desc = " ".join(desc.split()).strip(" .,-:;")
    # Strip duplicated leading brand from description
    if brand and desc.lower().startswith(brand.lower() + " "):
        trimmed = desc[len(brand) :].strip(" .,-:;")
        if len(trimmed) >= 6:
            desc = trimmed
    if not desc:
        desc = "Aplicación lista para usar"
    if desc[0].islower():
        desc = desc[0].upper() + desc[1:]

    sep = " - "
    max_desc_len = max(8, max_len - len(brand) - len(sep))
    if len(desc) > max_desc_len:
        for clause_sep in (". ", "; ", ", "):
            idx = desc.find(clause_sep, 12)
            if 10 <= idx <= max_desc_len:
                desc = desc[:idx].strip(" .,-:;")
                break
    if len(desc) > max_desc_len:
        words = desc.split()
        acc: list[str] = []
        for w in words:
            trial = " ".join([*acc, w])
            if len(trial) > max_desc_len:
                break
            acc.append(w)
        desc = " ".join(acc).strip(" .,-:;") if acc else desc[:max_desc_len].strip()
        tail_words = desc.split()
        if len(tail_words) >= 3 and tail_words[-1].lower() in {
            "de",
            "del",
            "la",
            "el",
            "en",
            "con",
            "sin",
            "para",
            "por",
            "a",
            "y",
            "e",
            "o",
            "the",
            "for",
            "with",
            "of",
            "to",
            "and",
        }:
            desc = " ".join(tail_words[:-1]).strip(" .,-:;")

    combined = f"{brand}{sep}{desc}"
    redacted = redact_secrets(combined, extra_secrets=extra_secrets)
    if len(redacted) > max_len:
        redacted = redacted[:max_len].rstrip(" .,-:;")
    return redacted


VALID_CHAT_MODES: frozenset[str] = frozenset({"chat", "audit", "document"})
VALID_CHAT_ENTRY_ID_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,63}$")
SERVER_BOOT_ID: str = uuid.uuid4().hex[:12]
CHAT_RUNNING_ORPHAN_SECONDS: float = 260.0
CHAT_STAGE_READ_FILES = "1/3 Leyendo archivos del repositorio"
CHAT_STAGE_LLM_ANALYZE = "2/3 Analizando código con IA"
CHAT_STAGE_SAVE_REPORT = "3/3 Guardando reporte"
CHAT_STAGE_COMPLETED = "Completada"


def validate_chat_entry_id(entry_id: str) -> str:
    """Validate chat entry identifier strictly preventing path traversal (INV-22)."""
    cleaned = str(entry_id or "").strip()
    if not cleaned or ".." in cleaned or not VALID_CHAT_ENTRY_ID_RE.fullmatch(cleaned):
        raise ValueError(f"Invalid chat entry_id '{entry_id}'")
    return cleaned


def _parse_utc_iso_timestamp(raw_iso: object) -> datetime | None:
    """Parse ISO-8601 timestamp string into aware UTC datetime when valid."""
    if not isinstance(raw_iso, str) or not raw_iso.strip():
        return None
    try:
        parsed = datetime.fromisoformat(raw_iso.strip())
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _is_chat_timeout_error(exc: BaseException) -> bool:
    """Classify timeout exceptions vs generic execution errors."""
    if isinstance(exc, (CommandTimeoutError, TimeoutError)):
        return True
    low = str(exc).lower()
    return any(
        tok in low
        for tok in ("timed out", "timeout", "tiempo de espera", "superó el tiempo")
    )


class ProjectChatReply(BaseModel):
    """Schema for structured read-only repository auditor, Q&A, and document generation responses."""

    model_config = ConfigDict(extra="ignore")

    reply: str = ""
    markdown: str = ""
    content: str = ""
    suggested_changes: str = ""
    document_title: str = ""


def _slugify_imported_id(text: str, max_len: int = 50) -> str:
    """Normalize text to an ASCII hyphenated slug safe for project_id (INV-22)."""
    norm = unicodedata.normalize("NFKD", str(text or "")).encode("ascii", "ignore").decode("ascii")
    slug = re.sub(r"[^a-z0-9]+", "-", norm.lower()).strip("-")
    if len(slug) > max_len:
        slug = slug[:max_len].strip("-")
    return slug or "imported-app"


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
    p = Path(path)
    if not p.is_file():
        raise FileNotFoundError(f"Limits config file not found: {p}")
    with p.open("rb") as f:
        data = tomllib.load(f)
    return {str(k): int(v) for k, v in data.items()}


class CircuitBreaker:
    """Enforces hard per-project, per-state call budgets (INV-17, Section 7.8)."""

    def __init__(
        self,
        limits: Mapping[str, int] | None = None,
        config_path: Path | str | None = None,
    ) -> None:
        self._lock = threading.RLock()
        self.config_path = Path(config_path) if config_path is not None else Path("config/limits.toml")
        if limits is not None:
            self._limits = dict(limits)
        elif config_path is not None:
            self._limits = load_limits(self.config_path)
        else:
            base = (
                load_limits(self.config_path)
                if self.config_path.is_file()
                else dict(DEFAULT_STATE_LIMITS)
            )
            base["DEBATE"] = max(int(base.get("DEBATE", 6)), 6)
            base["MEDIATION"] = max(int(base.get("MEDIATION", 3)), 3)
            self._limits = base
        self._counts: dict[tuple[str, ProjectState], int] = {}

    def get_limit(self, state: ProjectState) -> int | None:
        """Return configured call limit for state, or None if unbounded."""
        key = state.value if isinstance(state, ProjectState) else str(state)
        with self._lock:
            return self._limits.get(key)

    def get_calls(self, project_id: str, state: ProjectState) -> int:
        """Return number of recorded calls for project_id in state."""
        with self._lock:
            return self._counts.get((project_id, state), 0)

    def record_call(self, project_id: str, state: ProjectState) -> int:
        """Increment call count and raise CircuitBreakerTrippedError if budget exceeded."""
        with self._lock:
            limit = self.get_limit(state)
            current = self.get_calls(project_id, state)
            if limit is not None and current >= limit:
                raise CircuitBreakerTrippedError(project_id, state, current + 1, limit)
            self._counts[(project_id, state)] = current + 1
            return current + 1

    def reset(self, project_id: str, state: ProjectState | None = None) -> None:
        """Reset call count for a specific state, or all states of project_id."""
        with self._lock:
            if state is not None:
                self._counts.pop((project_id, state), None)
            else:
                to_remove = [k for k in self._counts if k[0] == project_id]
                for k in to_remove:
                    del self._counts[k]


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
        workspace_manager: WorkspaceManager | None = None,
        vcs: Any | None = None,
    ) -> None:
        self.store = store
        self.llm = llm
        self.publisher = publisher
        self.inbox = inbox if inbox is not None else Inbox()
        self.circuit_breaker = (
            circuit_breaker if circuit_breaker is not None else CircuitBreaker()
        )
        self.clock = clock if clock is not None else SystemClock()
        self.valid_templates = valid_templates
        self.workspace_manager = workspace_manager
        self.vcs = vcs
        self._lock = threading.RLock()
        self._chat_io_lock = threading.RLock()
        self._server_boot_id = SERVER_BOOT_ID
        self._project_locks: dict[str, threading.RLock] = {}
        self._specs: dict[str, Spec] = {}
        self._last_published_status: dict[str, tuple[object, ...]] = {}
        self.build_worker: Any | None = None

    def _project_lock(self, project_id: str) -> threading.RLock:
        with self._lock:
            lock = self._project_locks.get(project_id)
            if lock is None:
                lock = threading.RLock()
                self._project_locks[project_id] = lock
            return lock

    def _now(self) -> datetime:
        return self.clock.now() if self.clock else datetime.now(UTC)

    def _report_activity(
        self,
        activity_summary: str,
        *,
        agents_count: int = 1,
        stage: str | None = None,
    ) -> None:
        resolver = getattr(self.llm, "model_resolver", None)
        recorder = getattr(resolver, "record_activity", None) if resolver is not None else None
        if callable(recorder):
            recorder(activity_summary, agents_count=agents_count, stage=stage)

    def _get_spec(self, project_id: str) -> Spec:
        events = self.store.get_events(project_id)
        spec: Spec | None = None
        initial_objective = ""
        for e in events:
            if e.type == EventType.PROJECT_CREATED:
                initial_objective = e.payload.get("objective", "")
            elif e.type == EventType.SPEC_UPDATED and "spec" in e.payload:
                spec = Spec.model_validate(e.payload["spec"])
        if spec is not None:
            if not spec.objective and initial_objective:
                spec.objective = initial_objective
            with self._lock:
                self._specs[project_id] = spec
            return spec
        fallback = Spec(objective=initial_objective)
        with self._lock:
            self._specs[project_id] = fallback
        return fallback

    def get_spec(self, project_id: str) -> Spec:
        """Retrieve the current Spec reconstructed from the event log."""
        validate_project_id(project_id)
        return self._get_spec(project_id)

    def record_phase_failure(
        self,
        project_id: str,
        *,
        failed_state: ProjectState,
        error: BaseException | str,
        agent: str = "",
        extra_secrets: Sequence[str] = (),
    ) -> ProjectStatus:
        """Record redacted PHASE_ERROR detail event and transition project cleanly to FAILED."""
        validate_project_id(project_id)
        err_type = type(error).__name__ if isinstance(error, BaseException) else "Error"
        raw_msg = str(error).strip() or err_type
        clean_msg = redact_secrets(raw_msg, extra_secrets=extra_secrets)

        self.store.append(
            create_event(
                project_id=project_id,
                event_type=EventType.PHASE_ERROR,
                payload={
                    "state": failed_state.value if isinstance(failed_state, ProjectState) else str(failed_state),
                    "agent": agent,
                    "error_type": err_type,
                    "detail": clean_msg,
                },
                at=self._now(),
            )
        )

        current_status = self.get_status(project_id)
        if current_status.state != ProjectState.FAILED:
            validate_transition(current_status.state, ProjectState.FAILED)
            self.store.append(
                create_event(
                    project_id=project_id,
                    event_type=EventType.PROJECT_FAILED,
                    payload={
                        "reason": clean_msg,
                        "failed_from": current_status.state.value,
                        "error_type": err_type,
                    },
                    at=self._now(),
                )
            )
        return self.get_status(project_id)

    def create_project(
        self,
        project_id: str,
        name: str,
        objective: str = "",
    ) -> ProjectStatus:
        """Create a new project in INTERVIEW state and append initial events."""
        validate_project_id(project_id)
        raw_name = str(name or "").strip()
        raw_obj = str(objective or "").strip()
        if not raw_name and raw_obj:
            effective_name = synthesize_project_title(raw_obj)
        elif raw_name and (
            _LEADING_FILLER_RE.match(raw_name)
            or (len(raw_name) > 55 and raw_name == raw_obj)
        ):
            effective_name = synthesize_project_title(raw_name)
        else:
            effective_name = raw_name or project_id

        with self._project_lock(project_id):
            existing_events = self.store.get_events(project_id)
            if existing_events:
                current_status = self.get_status(project_id)
                validate_transition(current_status.state, ProjectState.INTERVIEW)
                raise ValueError(f"Project '{project_id}' already exists")
            validate_transition(ProjectState.IDLE, ProjectState.INTERVIEW)
            event = create_event(
                project_id=project_id,
                event_type=EventType.PROJECT_CREATED,
                payload={"name": effective_name, "objective": objective, "state": ProjectState.INTERVIEW},
                at=self._now(),
            )
            self.store.append(event)
            with self._lock:
                self._specs[project_id] = Spec(objective=objective)
            return self.get_status(project_id)

    def rename_project(
        self,
        project_id: str,
        title: str,
        *,
        rename_github_repo: bool | None = None,
        vcs: Any | None = None,
    ) -> ProjectStatus:
        """Append PROJECT_RENAMED event validating/sanitizing title and optionally renaming GitHub repository (INV-11)."""
        validate_project_id(project_id)
        clean_title = sanitize_project_title(title)
        eff_vcs = vcs if vcs is not None else self.vcs
        with self._project_lock(project_id):
            events = self.store.get_events(project_id)
            if not events:
                raise ValueError(f"Project '{project_id}' does not exist")
            current_status = self.get_status(project_id)
            payload: dict[str, Any] = {
                "name": clean_title,
                "previous_name": current_status.name,
            }

            current_repo_url = (current_status.links.repo or "").strip()
            has_github_repo = "github.com/" in current_repo_url.lower()
            should_rename_repo = (
                has_github_repo
                if rename_github_repo is None
                else (bool(rename_github_repo) and bool(current_repo_url))
            )
            if should_rename_repo and eff_vcs is not None and hasattr(eff_vcs, "rename_repository"):
                ws_dir: Path | None = None
                if self.workspace_manager is not None:
                    cand_ws = self.workspace_manager.get_workspace_dir(project_id)
                    if cand_ws.is_dir():
                        ws_dir = cand_ws
                new_repo_url, new_deploy_url = eff_vcs.rename_repository(
                    current_repo_url,
                    clean_title,
                    workspace_dir=ws_dir,
                )
                if new_repo_url and new_repo_url != current_repo_url:
                    payload["repo_url"] = new_repo_url
                    if new_deploy_url and (current_status.links.deploy or "").strip():
                        payload["deploy_url"] = new_deploy_url

            self.store.append(
                create_event(
                    project_id=project_id,
                    event_type=EventType.PROJECT_RENAMED,
                    payload=payload,
                    at=self._now(),
                )
            )
            return self.get_status(project_id)

    def suggest_title(
        self,
        project_id: str,
        *,
        workspace_dir: Path | None = None,
        extra_secrets: Sequence[str] = (),
    ) -> str:
        """Analyze workspace repo files, objective, and plan to suggest 'Nombre - Descripción breve'."""
        validate_project_id(project_id)
        events = self.store.get_events(project_id)
        if not events:
            raise ValueError(f"Project '{project_id}' does not exist")
        status = self.get_status(project_id)
        plan = self.get_plan(project_id)
        eff_ws = self._resolve_project_workspace_dir(
            project_id,
            status=status,
            workspace_dir=workspace_dir,
        )
        return suggest_title_from_repo(
            eff_ws,
            current_name=status.name,
            objective=status.objective,
            plan_summary=plan.summary if plan is not None else "",
            extra_secrets=extra_secrets,
        )

    def _resolve_project_workspace_dir(
        self,
        project_id: str,
        *,
        status: ProjectStatus | None = None,
        workspace_dir: Path | None = None,
        workspace_manager: WorkspaceManager | None = None,
    ) -> Path | None:
        """Resolve authorized internal or external workspace directory safely (INV-22)."""
        if workspace_dir is not None:
            resolved = Path(workspace_dir).resolve()
            return resolved if resolved.is_dir() else None
        eff_status = status if status is not None else self.get_status(project_id)
        eff_mgr = workspace_manager if workspace_manager is not None else self.workspace_manager
        if eff_status.workspace_path:
            validated_ext = validate_external_project_path(eff_status.workspace_path)
            if eff_mgr is not None and not eff_mgr.is_external_workspace(project_id):
                eff_mgr.register_external_workspace(project_id, validated_ext)
            return validated_ext
        if eff_mgr is not None:
            cand = eff_mgr.get_workspace_dir(project_id)
            if cand.is_dir():
                return cand
        return None

    def _resolve_chats_dir(self, chats_dir: Path | None = None) -> Path:
        if chats_dir is not None:
            return Path(chats_dir).resolve()
        db_path = getattr(self.store, "db_path", None)
        if db_path and str(db_path) != ":memory:":
            db_parent = Path(db_path).resolve().parent
            repo_data_dir = Path("data").resolve()
            return (
                (repo_data_dir / "chats").resolve()
                if db_parent == repo_data_dir
                else (db_parent / "chats").resolve()
            )
        return Path("data/chats").resolve()

    def _chat_file_path(self, project_id: str, chats_dir: Path) -> Path:
        validate_project_id(project_id)
        target = (chats_dir / f"{project_id}.json").resolve()
        if not target.is_relative_to(chats_dir):
            raise ValueError(f"Chat file escapes chats_dir: {target}")
        if (chats_dir / f"{project_id}.json").is_symlink():
            raise ValueError(f"Chat file symlink rejected: {target}")
        return target

    def _read_raw_chat_entries(
        self,
        project_id: str,
        chats_dir: Path,
    ) -> tuple[list[dict[str, Any]], Path]:
        chat_file = self._chat_file_path(project_id, chats_dir)
        if not chat_file.is_file():
            return [], chat_file
        try:
            raw = json.loads(chat_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, ValueError):
            return [], chat_file
        if not isinstance(raw, list):
            return [], chat_file
        return [dict(e) for e in raw if isinstance(e, dict)], chat_file

    def _write_chat_entries(
        self,
        project_id: str,
        entries: Sequence[Mapping[str, Any]],
        chats_dir: Path,
    ) -> Path:
        with self._chat_io_lock:
            chats_dir.mkdir(parents=True, exist_ok=True)
            chat_file = self._chat_file_path(project_id, chats_dir)
            trimmed = [dict(e) for e in entries][-50:]
            payload_text = json.dumps(trimmed, indent=2, ensure_ascii=False) + "\n"
            tmp_path: Path | None = None
            try:
                with tempfile.NamedTemporaryFile(
                    mode="w",
                    encoding="utf-8",
                    dir=chats_dir,
                    prefix=f".{project_id}_",
                    suffix=".tmp",
                    delete=False,
                ) as tmp_file:
                    tmp_file.write(payload_text)
                    tmp_path = Path(tmp_file.name)
                os.replace(tmp_path, chat_file)
                tmp_path = None
            finally:
                if tmp_path is not None:
                    tmp_path.unlink(missing_ok=True)
            return chat_file

    def _heal_orphaned_running_entries(self, entries: list[dict[str, Any]]) -> bool:
        now_dt = self._now()
        if now_dt.tzinfo is None:
            now_dt = now_dt.replace(tzinfo=UTC)
        changed = False
        for entry in entries:
            if entry.get("status") != "running":
                continue
            ref_dt = _parse_utc_iso_timestamp(entry.get("started_at") or entry.get("created_at"))
            elapsed_secs: float | None = (
                (now_dt - ref_dt).total_seconds() if ref_dt is not None else None
            )
            stale_boot = str(entry.get("server_boot_id") or "") != self._server_boot_id
            expired = elapsed_secs is not None and elapsed_secs > CHAT_RUNNING_ORPHAN_SECONDS
            if stale_boot or expired:
                stage = str(entry.get("stage_label") or CHAT_STAGE_READ_FILES).strip()
                if not stage:
                    stage = CHAT_STAGE_READ_FILES
                entry["status"] = "interrupted"
                entry["stage_label"] = stage
                entry["error_detail"] = (
                    f"La ejecución se interrumpió o superó el tiempo límite en la etapa: {stage}"
                )
                if entry.get("duration_seconds") is None and elapsed_secs is not None:
                    entry["duration_seconds"] = round(max(0.0, elapsed_secs), 2)
                changed = True
        return changed

    def _update_chat_entry(
        self,
        project_id: str,
        chats_dir: Path,
        entry_id: str,
        fields: Mapping[str, Any],
    ) -> list[dict[str, Any]]:
        with self._chat_io_lock:
            raw_entries, _ = self._read_raw_chat_entries(project_id, chats_dir)
            updated = False
            for entry in raw_entries:
                if entry.get("id") == entry_id:
                    entry.update(fields)
                    updated = True
                    break
            if updated:
                self._write_chat_entries(project_id, raw_entries, chats_dir)
            return raw_entries

    @staticmethod
    def _scrub_chat_value(value: Any, extra_secrets: Sequence[str]) -> Any:
        if isinstance(value, str):
            return redact_secrets(value, extra_secrets=extra_secrets)
        if isinstance(value, list):
            return [Orchestrator._scrub_chat_value(v, extra_secrets) for v in value]
        if isinstance(value, dict):
            return {
                str(k): Orchestrator._scrub_chat_value(v, extra_secrets)
                for k, v in value.items()
            }
        return value

    def record_automated_quality_audit(
        self,
        project_id: str,
        *,
        report: Any,
        commit_sha: str = "",
        version_label: str = "",
        duration_seconds: float | None = None,
        chats_dir: Path | None = None,
        prompt_label: str | None = None,
        extra_secrets: Sequence[str] = (),
    ) -> dict[str, Any]:
        """Persist automatic ParentAuditor Markdown report and scorecard into project audit history."""
        validate_project_id(project_id)
        if not self.store.get_events(project_id):
            raise ValueError(f"Project '{project_id}' does not exist")
        eff_dir = self._resolve_chats_dir(chats_dir)
        clean_sha = ""
        if isinstance(commit_sha, str) and commit_sha.strip():
            try:
                clean_sha = validate_commit_sha(commit_sha.strip())
            except ValueError:
                clean_sha = ""
        clean_ver = redact_secrets(str(version_label or "").strip(), extra_secrets=extra_secrets)
        scorecard = (
            report.to_scorecard_dict(extra_secrets=extra_secrets)
            if hasattr(report, "to_scorecard_dict")
            else (
                self._scrub_chat_value(dict(report), extra_secrets)
                if isinstance(report, Mapping)
                else {}
            )
        )
        if isinstance(scorecard, Mapping) and scorecard:
            scorecard = ensure_scorecard_tier(scorecard)
        raw_markdown = (
            str(getattr(report, "markdown_report", "") or "")
            if not isinstance(report, Mapping)
            else str(report.get("markdown_report") or report.get("content") or "")
        )
        clean_markdown = redact_secrets(raw_markdown.strip(), extra_secrets=extra_secrets)
        raw_improvements = (
            getattr(report, "improvements", None)
            if not isinstance(report, Mapping)
            else report.get("improvements")
        ) or []
        suggested = "\n".join(
            f"- {redact_secrets(str(imp).strip(), extra_secrets=extra_secrets)}"
            for imp in raw_improvements
            if str(imp).strip()
        )
        ver_suffix = f"-{clean_ver}" if clean_ver else ""
        doc_title = f"{project_id}-auditoria{ver_suffix}.md"
        now_iso = self._now().isoformat()
        entry_id = f"qa-{uuid.uuid4().hex[:10]}"
        eff_prompt = (
            redact_secrets(prompt_label.strip(), extra_secrets=extra_secrets)
            if isinstance(prompt_label, str) and prompt_label.strip()
            else f"Auditoría integral automática ({clean_ver or 'entrega'})"
        )
        entry: dict[str, Any] = {
            "id": entry_id,
            "role": "assistant",
            "mode": "audit",
            "prompt": eff_prompt,
            "content": clean_markdown,
            "suggested_changes": suggested,
            "document_title": doc_title,
            "status": "completed",
            "stage_label": CHAT_STAGE_COMPLETED,
            "error_detail": "",
            "duration_seconds": (
                round(max(0.0, float(duration_seconds)), 2)
                if duration_seconds is not None
                else None
            ),
            "server_boot_id": self._server_boot_id,
            "commit_sha": clean_sha,
            "version_label": clean_ver,
            "quality_scorecard": scorecard,
            "created_at": now_iso,
            "completed_at": now_iso,
        }
        with self._chat_io_lock:
            raw, _ = self._read_raw_chat_entries(project_id, eff_dir)
            raw.append(entry)
            self._write_chat_entries(project_id, raw, eff_dir)
        return entry

    def get_project_chat_history(
        self,
        project_id: str,
        *,
        chats_dir: Path | None = None,
        extra_secrets: Sequence[str] = (),
    ) -> list[dict[str, Any]]:
        """Return persisted read-only audit/Q&A chat history for project_id, healing orphans and redacting secrets."""
        validate_project_id(project_id)
        if not self.store.get_events(project_id):
            raise ValueError(f"Project '{project_id}' does not exist")
        eff_dir = self._resolve_chats_dir(chats_dir)
        with self._chat_io_lock:
            raw, _ = self._read_raw_chat_entries(project_id, eff_dir)
            if self._heal_orphaned_running_entries(raw):
                self._write_chat_entries(project_id, raw, eff_dir)

        cleaned: list[dict[str, Any]] = []
        for entry in raw:
            item: dict[str, Any] = {}
            for k, v in entry.items():
                item[str(k)] = self._scrub_chat_value(v, extra_secrets)
            item.setdefault("commit_sha", "")
            item.setdefault("version_label", "")
            eff_status = str(item.get("status") or "completed")
            item["status"] = eff_status
            if "stage_label" not in item or item.get("stage_label") is None:
                item["stage_label"] = CHAT_STAGE_COMPLETED if eff_status == "completed" else ""
            item.setdefault("error_detail", "")
            item.setdefault("duration_seconds", None)
            item.setdefault("server_boot_id", "")
            cleaned.append(item)
        return cleaned

    def delete_project_chat_entry(
        self,
        project_id: str,
        entry_id: str,
        *,
        chats_dir: Path | None = None,
    ) -> bool:
        """Delete a single persisted audit/document/chat entry by validated entry_id (INV-22)."""
        validate_project_id(project_id)
        clean_entry_id = validate_chat_entry_id(entry_id)
        if not self.store.get_events(project_id):
            raise ValueError(f"Project '{project_id}' does not exist")
        eff_dir = self._resolve_chats_dir(chats_dir)
        with self._chat_io_lock:
            raw, _ = self._read_raw_chat_entries(project_id, eff_dir)
            if not raw:
                return False
            remaining = [entry for entry in raw if entry.get("id") != clean_entry_id]
            if len(remaining) == len(raw):
                return False
            self._write_chat_entries(project_id, remaining, eff_dir)
            return True

    def delete_project_chat_history(
        self,
        project_id: str,
        *,
        chats_dir: Path | None = None,
    ) -> None:
        """Remove persisted chat log file for project_id if present."""
        validate_project_id(project_id)
        eff_dir = self._resolve_chats_dir(chats_dir)
        with self._chat_io_lock:
            raw_file = eff_dir / f"{project_id}.json"
            if raw_file.is_symlink():
                return
            resolved = raw_file.resolve()
            if resolved.is_relative_to(eff_dir) and resolved.is_file():
                resolved.unlink()

    def _invoke_chat_llm(
        self,
        llm_messages: list[dict[str, str]],
        *,
        timeout_seconds: float = 240.0,
    ) -> Any:
        """Invoke LLM generate() or complete() passing timeout_seconds when supported."""
        gen_fn = getattr(self.llm, "generate", None)
        target_fn = gen_fn if callable(gen_fn) else self.llm.complete
        try:
            sig = inspect.signature(target_fn)
            if "timeout_seconds" in sig.parameters:
                return target_fn(
                    llm_messages,
                    schema=ProjectChatReply,
                    timeout_seconds=timeout_seconds,
                )
        except (TypeError, ValueError):
            pass
        return target_fn(llm_messages, schema=ProjectChatReply)

    @staticmethod
    def _default_prompt_for_chat_mode(mode: str) -> str:
        if mode == "audit":
            return (
                "Audita el proyecto completo: identifica posibles bugs, rutas o recursos rotos, "
                "dependencias faltantes, riesgos de seguridad y mejoras claras de UX."
            )
        if mode == "document":
            return (
                "Genera un documento técnico y guía de uso completa del proyecto en Markdown "
                "listo para descargar."
            )
        raise ValueError("El mensaje de consulta no puede estar vacío.")

    @staticmethod
    def _resolve_retry_prompt(
        entries: Sequence[Mapping[str, Any]],
        target_idx: int,
        mode: str,
    ) -> str:
        target = entries[target_idx]
        stored_prompt = str(target.get("prompt") or "").strip()
        if stored_prompt:
            return stored_prompt
        if target.get("role") == "user":
            content_prompt = str(target.get("content") or "").strip()
            if content_prompt:
                return content_prompt
        for idx in range(target_idx - 1, -1, -1):
            prev = entries[idx]
            if prev.get("role") == "user" and str(prev.get("mode") or "") == mode:
                cand = str(prev.get("content") or "").strip()
                if cand:
                    return cand
        return (
            Orchestrator._default_prompt_for_chat_mode(mode)
            if mode in ("audit", "document")
            else "Explica el funcionamiento actual de este proyecto."
        )

    def _run_project_chat_execution(
        self,
        *,
        project_id: str,
        clean_mode: str,
        clean_msg: str,
        workspace_dir: Path | None,
        chats_dir: Path | None,
        workspace_manager: WorkspaceManager | None,
        extra_secrets: Sequence[str],
        retry_entry_id: str | None = None,
    ) -> dict[str, Any]:
        status = self.get_status(project_id)
        plan = self.get_plan(project_id)
        spec = self._get_spec(project_id)
        eff_chats_dir = self._resolve_chats_dir(chats_dir)

        checkpoints = self.list_project_checkpoints(
            project_id, chats_dir=eff_chats_dir, extra_secrets=extra_secrets
        )
        current_sha = str(checkpoints[0].get("commit_sha") or "") if checkpoints else ""
        current_version = str(checkpoints[0].get("version_label") or "") if checkpoints else ""

        default_doc = (
            f"{project_id}-auditoria.md"
            if clean_mode == "audit"
            else (
                f"{project_id}-documentacion.md"
                if clean_mode == "document"
                else f"{project_id}-consulta.md"
            )
        )
        now_iso = self._now().isoformat()
        mono_start = time.monotonic()

        with self._chat_io_lock:
            raw_entries, _ = self._read_raw_chat_entries(project_id, eff_chats_dir)
            self._heal_orphaned_running_entries(raw_entries)
            if retry_entry_id is not None:
                entry_id = retry_entry_id
                found = False
                for item in raw_entries:
                    if item.get("id") == entry_id:
                        item.update(
                            {
                                "mode": clean_mode,
                                "prompt": clean_msg,
                                "status": "running",
                                "stage_label": CHAT_STAGE_READ_FILES,
                                "error_detail": "",
                                "duration_seconds": None,
                                "server_boot_id": self._server_boot_id,
                                "commit_sha": current_sha,
                                "version_label": current_version,
                                "started_at": now_iso,
                            }
                        )
                        found = True
                        break
                if not found:
                    raise LookupError(f"Chat entry '{retry_entry_id}' not found")
            else:
                entry_id = f"a-{uuid.uuid4().hex[:10]}"
                raw_entries.append(
                    {
                        "id": f"u-{uuid.uuid4().hex[:10]}",
                        "role": "user",
                        "mode": clean_mode,
                        "content": clean_msg,
                        "status": "completed",
                        "stage_label": CHAT_STAGE_COMPLETED,
                        "error_detail": "",
                        "duration_seconds": 0.0,
                        "server_boot_id": self._server_boot_id,
                        "commit_sha": current_sha,
                        "version_label": current_version,
                        "created_at": now_iso,
                    }
                )
                raw_entries.append(
                    {
                        "id": entry_id,
                        "role": "assistant",
                        "mode": clean_mode,
                        "prompt": clean_msg,
                        "content": "",
                        "suggested_changes": "",
                        "document_title": default_doc,
                        "status": "running",
                        "stage_label": CHAT_STAGE_READ_FILES,
                        "error_detail": "",
                        "duration_seconds": None,
                        "server_boot_id": self._server_boot_id,
                        "commit_sha": current_sha,
                        "version_label": current_version,
                        "started_at": now_iso,
                        "created_at": now_iso,
                    }
                )
            self._write_chat_entries(project_id, raw_entries, eff_chats_dir)

        prior_history = [
            e
            for e in raw_entries
            if e.get("id") != entry_id and str(e.get("status") or "completed") == "completed"
        ]
        current_stage = CHAT_STAGE_READ_FILES
        try:
            eff_ws = self._resolve_project_workspace_dir(
                project_id,
                status=status,
                workspace_dir=workspace_dir,
                workspace_manager=workspace_manager,
            )
            code_ctx = (
                collect_workspace_code_context(
                    eff_ws,
                    max_chars=12000,
                    extra_secrets=extra_secrets,
                )
                if eff_ws is not None
                else ""
            )

            if clean_mode == "audit":
                mode_guidance = (
                    "MODO AUDITORÍA (SOLO LECTURA): Analiza bugs, casos borde, rutas HTTP o enlaces rotos, "
                    "selectores DOM, dependencias o ejecución local, seguridad (validación de entradas, rutas, "
                    "secretos) y mejoras de UX. Responde en Markdown estructurado en 'reply' y redacta en "
                    "'suggested_changes' instrucciones concretas y accionables para corregir los hallazgos."
                )
            elif clean_mode == "document":
                mode_guidance = (
                    "MODO GENERADOR DE DOCUMENTOS (SOLO LECTURA): Genera documentación técnica completa y guía de "
                    "uso en Markdown limpio en 'reply' (resumen, arquitectura/archivos, dependencias y arranque "
                    "local, endpoints/flujos y notas). Define un nombre sugerido .md en 'document_title'."
                )
            else:
                mode_guidance = (
                    "MODO CONSULTA (SOLO LECTURA): Responde con claridad y precisión basándote únicamente en el código "
                    "y metadatos observados del proyecto. Si detectas alguna mejora útil, opcionalmente inclúyela en "
                    "'suggested_changes'."
                )

            system_prompt = (
                "Eres un Auditor de Software y Redactor Técnico Senior trabajando en modo ESTRICTAMENTE DE SOLO LECTURA.\n"
                "No modifiques archivos ni propongas ejecutar cambios automáticamente.\n"
                "Responde siempre en español claro usando formato Markdown dentro del campo JSON 'reply'.\n"
                f"{mode_guidance}"
            )
            meta_block = (
                f"Proyecto: {status.name or project_id} (ID: {project_id}, estado: {status.state.value})\n"
                f"Objetivo: {redact_secrets(status.objective or spec.objective or 'N/D', extra_secrets=extra_secrets)}\n"
                f"Plan: {redact_secrets(plan.summary if plan is not None else 'N/D', extra_secrets=extra_secrets)}\n"
            )
            context_block = code_ctx or "(Sin archivos de código disponibles en este workspace.)"

            llm_messages: list[dict[str, str]] = [{"role": "system", "content": system_prompt}]
            for prev in prior_history[-8:]:
                p_role = "assistant" if prev.get("role") == "assistant" else "user"
                p_text = str(prev.get("content", "")).strip()
                if p_text:
                    llm_messages.append({"role": p_role, "content": p_text[:2000]})
            llm_messages.append(
                {
                    "role": "user",
                    "content": redact_secrets(
                        f"{meta_block}\nCódigo real del repositorio:\n{context_block}\n\n"
                        f"Solicitud del usuario ({clean_mode}):\n{clean_msg}",
                        extra_secrets=extra_secrets,
                    ),
                }
            )

            current_stage = CHAT_STAGE_LLM_ANALYZE
            self._update_chat_entry(
                project_id,
                eff_chats_dir,
                entry_id,
                {"stage_label": CHAT_STAGE_LLM_ANALYZE},
            )
            raw_resp = self._invoke_chat_llm(llm_messages, timeout_seconds=240.0)

            current_stage = CHAT_STAGE_SAVE_REPORT
            self._update_chat_entry(
                project_id,
                eff_chats_dir,
                entry_id,
                {"stage_label": CHAT_STAGE_SAVE_REPORT},
            )

            raw_reply = ""
            raw_suggested = ""
            raw_doc_title = ""
            if isinstance(raw_resp, Mapping):
                raw_reply = str(
                    raw_resp.get("reply")
                    or raw_resp.get("markdown")
                    or raw_resp.get("content")
                    or ""
                ).strip()
                raw_suggested = str(raw_resp.get("suggested_changes") or "").strip()
                raw_doc_title = str(raw_resp.get("document_title") or "").strip()

            clean_reply = redact_secrets(
                raw_reply or "No se recibió contenido del modelo para esta consulta.",
                extra_secrets=extra_secrets,
            )
            clean_suggested = redact_secrets(raw_suggested, extra_secrets=extra_secrets)
            clean_doc_title = (
                redact_secrets(raw_doc_title, extra_secrets=extra_secrets) or default_doc
            )
            if not clean_doc_title.lower().endswith(".md"):
                clean_doc_title = f"{clean_doc_title}.md"

            duration_secs = round(max(0.0, time.monotonic() - mono_start), 2)
            self._update_chat_entry(
                project_id,
                eff_chats_dir,
                entry_id,
                {
                    "content": clean_reply,
                    "suggested_changes": clean_suggested,
                    "document_title": clean_doc_title,
                    "status": "completed",
                    "stage_label": CHAT_STAGE_COMPLETED,
                    "error_detail": "",
                    "duration_seconds": duration_secs,
                    "commit_sha": current_sha,
                    "version_label": current_version,
                    "completed_at": self._now().isoformat(),
                },
            )
        except Exception as exc:
            fail_status = "timed_out" if _is_chat_timeout_error(exc) else "failed"
            clean_err = redact_secrets(str(exc), extra_secrets=extra_secrets)
            duration_secs = round(max(0.0, time.monotonic() - mono_start), 2)
            self._update_chat_entry(
                project_id,
                eff_chats_dir,
                entry_id,
                {
                    "status": fail_status,
                    "stage_label": current_stage,
                    "error_detail": clean_err,
                    "duration_seconds": duration_secs,
                },
            )
            raise

        updated_history = self.get_project_chat_history(
            project_id,
            chats_dir=eff_chats_dir,
            extra_secrets=extra_secrets,
        )
        return {
            "ok": True,
            "project_id": project_id,
            "entry_id": entry_id,
            "mode": clean_mode,
            "status": "completed",
            "stage_label": CHAT_STAGE_COMPLETED,
            "duration_seconds": duration_secs,
            "reply": clean_reply,
            "suggested_changes": clean_suggested,
            "document_title": clean_doc_title,
            "commit_sha": current_sha,
            "version_label": current_version,
            "context_chars": len(code_ctx),
            "history": updated_history,
        }

    def chat_with_project(
        self,
        project_id: str,
        message: str,
        mode: str = "chat",
        workspace_dir: Path | None = None,
        *,
        chats_dir: Path | None = None,
        workspace_manager: WorkspaceManager | None = None,
        extra_secrets: Sequence[str] = (),
    ) -> dict[str, Any]:
        """Execute strictly read-only Q&A, audit, or Markdown doc generation with persistent lifecycle (INV-19)."""
        validate_project_id(project_id)
        clean_mode = str(mode or "chat").strip().lower()
        if clean_mode not in VALID_CHAT_MODES:
            raise ValueError(
                f"Modo de chat inválido '{mode}': usa chat, audit o document."
            )

        clean_msg = redact_secrets(str(message or "").strip(), extra_secrets=extra_secrets)
        if not clean_msg:
            clean_msg = self._default_prompt_for_chat_mode(clean_mode)

        events = self.store.get_events(project_id)
        if not events:
            raise ValueError(f"Project '{project_id}' does not exist")

        return self._run_project_chat_execution(
            project_id=project_id,
            clean_mode=clean_mode,
            clean_msg=clean_msg,
            workspace_dir=workspace_dir,
            chats_dir=chats_dir,
            workspace_manager=workspace_manager,
            extra_secrets=extra_secrets,
            retry_entry_id=None,
        )

    def retry_project_chat_entry(
        self,
        project_id: str,
        entry_id: str,
        workspace_dir: Path | None = None,
        *,
        chats_dir: Path | None = None,
        workspace_manager: WorkspaceManager | None = None,
        extra_secrets: Sequence[str] = (),
    ) -> dict[str, Any]:
        """Re-execute an existing audit, document, or chat entry reusing original mode/prompt (INV-19, INV-22)."""
        validate_project_id(project_id)
        clean_entry_id = validate_chat_entry_id(entry_id)
        events = self.store.get_events(project_id)
        if not events:
            raise ValueError(f"Project '{project_id}' does not exist")

        eff_chats_dir = self._resolve_chats_dir(chats_dir)
        history = self.get_project_chat_history(
            project_id,
            chats_dir=eff_chats_dir,
            extra_secrets=extra_secrets,
        )
        target_idx = -1
        for idx, item in enumerate(history):
            if item.get("id") == clean_entry_id:
                target_idx = idx
                break
        if target_idx < 0:
            raise LookupError(f"Chat entry '{clean_entry_id}' not found")

        target_item = history[target_idx]
        if target_item.get("status") == "running":
            raise RuntimeError(f"Chat entry '{clean_entry_id}' is already running")

        clean_mode = str(target_item.get("mode") or "audit").strip().lower()
        if clean_mode not in VALID_CHAT_MODES:
            clean_mode = "audit"
        raw_prompt = self._resolve_retry_prompt(history, target_idx, clean_mode)
        clean_msg = redact_secrets(raw_prompt, extra_secrets=extra_secrets)

        return self._run_project_chat_execution(
            project_id=project_id,
            clean_mode=clean_mode,
            clean_msg=clean_msg,
            workspace_dir=workspace_dir,
            chats_dir=eff_chats_dir,
            workspace_manager=workspace_manager,
            extra_secrets=extra_secrets,
            retry_entry_id=clean_entry_id,
        )

    def import_project(
        self,
        local_path: str | Path,
        *,
        title: str = "",
        project_id: str | None = None,
        workspace_manager: WorkspaceManager | None = None,
        factory_root: Path | None = None,
        extra_secrets: Sequence[str] = (),
    ) -> tuple[ProjectStatus, dict[str, Any]]:
        """Validate external folder (INV-22), load optional manifest, and register project cleanly as DELIVERED."""
        from agent_factory.runtime_runner import detect_workspace_app

        validated_dir = validate_external_project_path(local_path, factory_root=factory_root)
        manifest_path = validated_dir / ".agent-factory" / "manifest.json"
        manifest: dict[str, Any] = {}
        manifest_loaded = False
        raw_manifest = _read_contained_text_snippet(
            manifest_path,
            validated_dir,
            max_bytes=32768,
            extra_secrets=extra_secrets,
        )
        if raw_manifest:
            try:
                parsed = json.loads(raw_manifest)
                if isinstance(parsed, dict):
                    manifest = parsed
                    manifest_loaded = True
            except (json.JSONDecodeError, ValueError):
                pass

        app_info = detect_workspace_app(validated_dir)
        manifest_title = str(manifest.get("title") or "").strip()
        user_title = str(title or "").strip()
        if user_title:
            effective_title = sanitize_project_title(user_title)
        elif manifest_title:
            effective_title = sanitize_project_title(manifest_title)
        else:
            suggested = suggest_title_from_repo(
                validated_dir,
                current_name=validated_dir.name.replace("-", " ").replace("_", " "),
                extra_secrets=extra_secrets,
            )
            effective_title = sanitize_project_title(suggested or validated_dir.name)

        raw_obj = redact_secrets(
            str(manifest.get("objective") or effective_title).strip() or effective_title,
            extra_secrets=extra_secrets,
        )
        plan_summary = redact_secrets(
            str(
                manifest.get("plan_summary")
                or f"Proyecto local importado ({app_info.app_type}) desde carpeta existente."
            ).strip(),
            extra_secrets=extra_secrets,
        )
        repo_url = redact_secrets(str(manifest.get("repo_url") or "").strip(), extra_secrets=extra_secrets)
        deploy_url = redact_secrets(str(manifest.get("deploy_url") or "").strip(), extra_secrets=extra_secrets)

        candidate_pid = str(project_id or manifest.get("project_id") or "").strip()
        try:
            base_slug = validate_project_id(candidate_pid) if candidate_pid else _slugify_imported_id(effective_title)
        except ValueError:
            base_slug = _slugify_imported_id(effective_title)

        with self._lock:
            final_pid = base_slug
            suffix = 2
            while self.store.get_events(final_pid):
                tail = f"-{suffix}"
                final_pid = f"{base_slug[: 63 - len(tail)]}{tail}"
                suffix += 1
            validate_project_id(final_pid)

        now = self._now()
        spec = Spec(
            objective=raw_obj,
            users=["Usuario local"],
            features=[effective_title],
            business_rules=["Proyecto importado desde carpeta local autorizada"],
            edge_cases=["Validación estricta de rutas e invariants"],
            constraints=[f"Tipo detectado: {app_info.app_type}"],
            success_criteria=["Ejecución e inspección disponibles desde Agent Factory"],
        )
        plan = Plan(
            summary=plan_summary,
            rationale="Expediente inicializado al importar proyecto existente.",
            template_id="static-web",
            files=[],
            acceptance_tests=[
                AcceptanceTest(
                    id="AT-IMPORT-01",
                    description="Proyecto local accesible y verificable",
                    type=AcceptanceTestType.UNIT,
                )
            ],
            risks=[],
            deployment_target="github-pages" if repo_url else "local",
        )
        plan_hash = plan.compute_hash()

        with self._project_lock(final_pid):
            validate_transition(ProjectState.IDLE, ProjectState.INTERVIEW)
            self.store.append(
                create_event(
                    final_pid,
                    EventType.PROJECT_CREATED,
                    {
                        "name": effective_title,
                        "objective": raw_obj,
                        "state": ProjectState.INTERVIEW.value,
                        "imported": True,
                        "external_workspace_path": str(validated_dir),
                    },
                    at=now,
                )
            )
            self.store.append(
                create_event(
                    final_pid,
                    EventType.SPEC_UPDATED,
                    {"spec": spec.model_dump(mode="json")},
                    at=now,
                )
            )
            validate_transition(ProjectState.INTERVIEW, ProjectState.DEBATE)
            self.store.append(
                create_event(
                    final_pid,
                    EventType.STATE_TRANSITIONED,
                    {"from": ProjectState.INTERVIEW.value, "to": ProjectState.DEBATE.value},
                    at=now,
                )
            )
            validate_transition(ProjectState.DEBATE, ProjectState.MEDIATION)
            self.store.append(
                create_event(
                    final_pid,
                    EventType.STATE_TRANSITIONED,
                    {"from": ProjectState.DEBATE.value, "to": ProjectState.MEDIATION.value},
                    at=now,
                )
            )
            validate_transition(ProjectState.MEDIATION, ProjectState.WAITING_APPROVAL)
            self.store.append(
                create_event(
                    final_pid,
                    EventType.PLAN_CREATED,
                    {
                        "to": ProjectState.WAITING_APPROVAL.value,
                        "plan_hash": plan_hash,
                        "total_tests": len(plan.acceptance_tests),
                        "plan": plan.model_dump(mode="json"),
                    },
                    at=now,
                )
            )
            validate_transition(ProjectState.WAITING_APPROVAL, ProjectState.BUILDING)
            self.store.append(
                create_event(
                    final_pid,
                    EventType.APPROVAL_RECORDED,
                    {
                        "to": ProjectState.BUILDING.value,
                        "decision": "approved",
                        "plan_hash": plan_hash,
                    },
                    at=now,
                )
            )
            validate_transition(ProjectState.BUILDING, ProjectState.VERIFYING)
            self.store.append(
                create_event(
                    final_pid,
                    EventType.STATE_TRANSITIONED,
                    {"from": ProjectState.BUILDING.value, "to": ProjectState.VERIFYING.value},
                    at=now,
                )
            )
            validate_transition(ProjectState.VERIFYING, ProjectState.DELIVERED)
            init_sha = read_workspace_git_head_sha(validated_dir)
            delivered_pl: dict[str, Any] = {
                "repo_url": repo_url or None,
                "deploy_url": deploy_url or None,
                "imported": True,
                "version_label": "v1",
            }
            if init_sha:
                delivered_pl["commit_sha"] = init_sha
            self.store.append(
                create_event(
                    final_pid,
                    EventType.PROJECT_DELIVERED,
                    delivered_pl,
                    at=now,
                )
            )
            with self._lock:
                self._specs[final_pid] = spec

        eff_mgr = workspace_manager if workspace_manager is not None else self.workspace_manager
        if eff_mgr is not None:
            eff_mgr.register_external_workspace(final_pid, validated_dir, factory_root=factory_root)

        final_status = self.get_status(final_pid)
        return final_status, {
            "app_type": app_info.app_type,
            "manifest_loaded": manifest_loaded,
            "external_workspace_path": str(validated_dir),
        }

    def list_project_files(
        self,
        project_id: str,
        *,
        workspace_manager: WorkspaceManager | None = None,
    ) -> list[str]:
        """List workspace files safely for Vista Desarrollador without exposing .env or symlinks (INV-19, INV-22)."""
        validate_project_id(project_id)
        events = self.store.get_events(project_id)
        if not events:
            raise ValueError(f"Project '{project_id}' does not exist")
        status = self.get_status(project_id)
        ws_dir = self._resolve_project_workspace_dir(
            project_id, status=status, workspace_manager=workspace_manager
        )
        if ws_dir is None:
            return []
        return list_workspace_files(ws_dir)

    def get_project_diff(
        self,
        project_id: str,
        *,
        workspace_manager: WorkspaceManager | None = None,
        runner: Any | None = None,
        extra_secrets: Sequence[str] = (),
    ) -> dict[str, Any]:
        """Return redacted Git diff for isolated project workspace (INV-19, INV-22)."""
        from agent_factory.adapters.subprocess_runner import SubprocessRunner

        validate_project_id(project_id)
        events = self.store.get_events(project_id)
        if not events:
            raise ValueError(f"Project '{project_id}' does not exist")
        status = self.get_status(project_id)
        ws_dir = self._resolve_project_workspace_dir(
            project_id, status=status, workspace_manager=workspace_manager
        )
        if ws_dir is None:
            return {"has_git": False, "commit_sha": None, "diff": ""}
        eff_runner = runner if runner is not None else SubprocessRunner(default_timeout_seconds=15.0)
        has_git, head_sha, diff_text = read_workspace_git_diff(
            ws_dir, eff_runner, extra_secrets=extra_secrets
        )
        return {
            "has_git": has_git,
            "commit_sha": head_sha,
            "diff": diff_text,
        }

    def _load_audit_scorecards_index(
        self,
        project_id: str,
        *,
        chats_dir: Path | None = None,
        extra_secrets: Sequence[str] = (),
    ) -> tuple[dict[str, tuple[str, dict[str, Any]]], dict[str, tuple[str, dict[str, Any]]]]:
        """Read latest completed ParentAuditor scorecards indexed by version_label and short commit SHA without creating files."""
        eff_dir = self._resolve_chats_dir(chats_dir)
        chat_file = self._chat_file_path(project_id, eff_dir)
        if not chat_file.is_file():
            return {}, {}
        with self._chat_io_lock:
            raw_entries, _ = self._read_raw_chat_entries(project_id, eff_dir)
        by_ver: dict[str, tuple[str, dict[str, Any]]] = {}
        by_sha: dict[str, tuple[str, dict[str, Any]]] = {}
        for entry in raw_entries:
            if entry.get("role") != "assistant" or entry.get("mode") != "audit":
                continue
            if str(entry.get("status") or "completed") != "completed":
                continue
            raw_sc = entry.get("quality_scorecard")
            if not isinstance(raw_sc, Mapping) or not isinstance(raw_sc.get("areas"), Sequence):
                continue
            sc_clean = ensure_scorecard_tier(self._scrub_chat_value(raw_sc, extra_secrets))
            ts = str(entry.get("completed_at") or entry.get("created_at") or "")
            ver = str(entry.get("version_label") or "").strip().lower()
            sha_short = str(entry.get("commit_sha") or "").strip().lower()[:7]
            if ver:
                prev = by_ver.get(ver)
                if prev is None or ts >= prev[0]:
                    by_ver[ver] = (ts, sc_clean)
            if sha_short:
                prev_s = by_sha.get(sha_short)
                if prev_s is None or ts >= prev_s[0]:
                    by_sha[sha_short] = (ts, sc_clean)
        return by_ver, by_sha

    @staticmethod
    def _clean_checkpoint_summary(text: str, *, extra_secrets: Sequence[str] = (), limit: int = 200) -> str:
        collapsed = " ".join(str(text or "").split()).strip()
        if not collapsed:
            return ""
        redacted = redact_secrets(collapsed, extra_secrets=extra_secrets)
        return redacted if len(redacted) <= limit else f"{redacted[: limit - 3]}..."

    def list_project_checkpoints(
        self,
        project_id: str,
        *,
        chats_dir: Path | None = None,
        extra_secrets: Sequence[str] = (),
    ) -> list[dict[str, Any]]:
        """List delivered checkpoints in reverse chronological order enriched with summary & quality_scorecard (INV-11, INV-19)."""
        validate_project_id(project_id)
        events = self.store.get_events(project_id)
        if not events:
            raise ValueError(f"Project '{project_id}' does not exist")

        audit_by_ver, audit_by_sha = self._load_audit_scorecards_index(
            project_id, chats_dir=chats_dir, extra_secrets=extra_secrets
        )

        checkpoints: list[dict[str, Any]] = []
        idx = 0
        initial_objective = ""
        pending_change_reason = ""
        for ev in events:
            pl = ev.payload or {}
            if ev.type == EventType.PROJECT_CREATED:
                initial_objective = str(pl.get("objective") or pl.get("name") or initial_objective).strip()
            elif ev.type == EventType.STATE_TRANSITIONED:
                if str(pl.get("to") or "") == ProjectState.CHANGE_REQUEST.value:
                    cand = str(pl.get("instructions") or pl.get("reason") or "").strip()
                    if cand:
                        pending_change_reason = cand
            elif ev.type == EventType.SPEC_UPDATED:
                chg = str(pl.get("change_instructions") or "").strip()
                if chg:
                    pending_change_reason = chg
                elif not initial_objective and isinstance(pl.get("spec"), Mapping):
                    initial_objective = str(pl["spec"].get("objective") or "").strip()

            if ev.type != EventType.PROJECT_DELIVERED:
                continue

            idx += 1
            raw_sha = str(pl.get("commit_sha") or "").strip()
            clean_sha = ""
            if raw_sha:
                try:
                    clean_sha = validate_commit_sha(raw_sha)
                except ValueError:
                    clean_sha = ""
            label = redact_secrets(
                str(pl.get("version_label") or f"v{idx}").strip(),
                extra_secrets=extra_secrets,
            )
            rollback_from = str(pl.get("rollback_from") or "").strip() or None

            explicit_summary = str(pl.get("summary") or "").strip()
            if explicit_summary:
                raw_summary = explicit_summary
            elif rollback_from:
                raw_summary = f"Restaurado desde {rollback_from[:7]}"
            elif idx > 1 and pending_change_reason:
                raw_summary = pending_change_reason
            elif initial_objective:
                raw_summary = initial_objective
            else:
                raw_summary = f"Entrega inicial ({label})"
            pending_change_reason = ""

            raw_scorecard = pl.get("quality_scorecard")
            scorecard: dict[str, Any] | None = (
                ensure_scorecard_tier(self._scrub_chat_value(raw_scorecard, extra_secrets))
                if isinstance(raw_scorecard, Mapping)
                else None
            )
            audit_cand = audit_by_ver.get(label.lower())
            if audit_cand is None and clean_sha:
                audit_cand = audit_by_sha.get(clean_sha[:7].lower())
            if audit_cand is not None:
                audit_ts, audit_sc = audit_cand
                ev_ts = ev.at.isoformat()
                if scorecard is None or audit_ts >= ev_ts:
                    scorecard = audit_sc

            cp_entry: dict[str, Any] = {
                "version_label": label,
                "commit_sha": clean_sha,
                "short_sha": clean_sha[:7] if clean_sha else "",
                "delivered_at": ev.at.isoformat(),
                "repo_url": redact_secrets(str(pl.get("repo_url") or ""), extra_secrets=extra_secrets),
                "deploy_url": redact_secrets(str(pl.get("deploy_url") or ""), extra_secrets=extra_secrets),
                "rollback_from": rollback_from,
                "summary": self._clean_checkpoint_summary(raw_summary, extra_secrets=extra_secrets),
                "can_rollback": bool(clean_sha),
                "is_current": False,
                "quality_scorecard": scorecard,
            }
            if isinstance(scorecard, Mapping) and isinstance(scorecard.get("debate_summary"), Mapping):
                cp_entry["debate_summary"] = dict(scorecard["debate_summary"])
            checkpoints.append(cp_entry)
        if checkpoints:
            checkpoints[-1]["is_current"] = True
        return list(reversed(checkpoints))

    def evaluate_project_quality(
        self,
        project_id: str,
        *,
        auditor: Any | None = None,
        workspace_dir: Path | None = None,
        workspace_manager: WorkspaceManager | None = None,
        chats_dir: Path | None = None,
        extra_secrets: Sequence[str] = (),
    ) -> dict[str, Any]:
        """Execute ParentAuditor in read-only mode on current workspace, update current scorecard, and persist report."""
        validate_project_id(project_id)
        with self._project_lock(project_id):
            events = self.store.get_events(project_id)
            if not events:
                raise ValueError(f"Project '{project_id}' does not exist")
            status = self.get_status(project_id)
            if status.state != ProjectState.DELIVERED:
                raise IllegalTransition(status.state, ProjectState.CHANGE_REQUEST)

            ws_dir = self._resolve_project_workspace_dir(
                project_id,
                status=status,
                workspace_dir=workspace_dir,
                workspace_manager=workspace_manager,
            )
            if ws_dir is None or not ws_dir.is_dir():
                raise ValueError(f"El workspace del proyecto '{project_id}' no existe.")

            eff_chats_dir = self._resolve_chats_dir(chats_dir)
            checkpoints = self.list_project_checkpoints(
                project_id, chats_dir=eff_chats_dir, extra_secrets=extra_secrets
            )
            cur_cp = next((c for c in checkpoints if c.get("is_current")), checkpoints[0] if checkpoints else None)
            prior_deliv = sum(1 for ev in events if ev.type == EventType.PROJECT_DELIVERED)
            current_version = (cur_cp.get("version_label") if cur_cp else "") or f"v{max(1, prior_deliv)}"
            commit_sha = (cur_cp.get("commit_sha") if cur_cp else "") or (
                read_git_head_sha_from_disk(ws_dir) or ""
            )

            eff_auditor = auditor if auditor is not None else ParentAuditor(self.llm)
            test_results: dict[str, bool] | None = (
                {"suite": status.tests.failed == 0} if status.tests.total > 0 else None
            )
            t0 = time.monotonic()
            report = eff_auditor.audit_workspace(
                project_id,
                ws_dir,
                spec=self.get_spec(project_id),
                plan=self.get_plan(project_id),
                test_results=test_results,
                round_number=1,
                project_name=status.name or project_id,
                version_label=current_version,
                extra_secrets=extra_secrets,
            )
            dur = round(max(0.0, time.monotonic() - t0), 2)

            entry = self.record_automated_quality_audit(
                project_id,
                report=report,
                commit_sha=commit_sha,
                version_label=current_version,
                duration_seconds=dur,
                chats_dir=eff_chats_dir,
                prompt_label=f"Re-evaluación de calidad ({current_version})",
                extra_secrets=extra_secrets,
            )
            scorecard = ensure_scorecard_tier(
                report.to_scorecard_dict(extra_secrets=extra_secrets)
            )
            updated_checkpoints = self.list_project_checkpoints(
                project_id, chats_dir=eff_chats_dir, extra_secrets=extra_secrets
            )
            return {
                "ok": True,
                "project_id": project_id,
                "version_label": current_version,
                "commit_sha": commit_sha,
                "scorecard": scorecard,
                "quality_scorecard": scorecard,
                "report": report,
                "chat_entry": self._scrub_chat_value(entry, extra_secrets),
                "checkpoints": updated_checkpoints,
            }

    def transition_for_quality_improvement(
        self,
        project_id: str,
        *,
        plan: Plan,
        reason: str,
    ) -> str:
        """Transition a DELIVERED project canonically via CHANGE_REQUEST to BUILDING for quality refinement (INV-04, INV-11)."""
        validate_project_id(project_id)
        with self._project_lock(project_id):
            events = self.store.get_events(project_id)
            if not events:
                raise ValueError(f"Project '{project_id}' does not exist")
            current_status = self.get_status(project_id)
            if current_status.state != ProjectState.DELIVERED:
                raise IllegalTransition(current_status.state, ProjectState.CHANGE_REQUEST)

            now = self._now()
            for src_state, dst_state, extra_pl in (
                (
                    ProjectState.DELIVERED,
                    ProjectState.CHANGE_REQUEST,
                    {"reason": reason, "mode": "quality_improve"},
                ),
                (ProjectState.CHANGE_REQUEST, ProjectState.INTERVIEW, {}),
                (ProjectState.INTERVIEW, ProjectState.DEBATE, {}),
                (ProjectState.DEBATE, ProjectState.MEDIATION, {}),
            ):
                validate_transition(src_state, dst_state)
                self.store.append(
                    create_event(
                        project_id=project_id,
                        event_type=EventType.STATE_TRANSITIONED,
                        payload={"from": src_state.value, "to": dst_state.value, **extra_pl},
                        at=now,
                    )
                )

            eff_plan_hash = current_status.plan_hash
            if eff_plan_hash:
                validate_transition(ProjectState.MEDIATION, ProjectState.WAITING_APPROVAL)
                self.store.append(
                    create_event(
                        project_id=project_id,
                        event_type=EventType.STATE_TRANSITIONED,
                        payload={
                            "from": ProjectState.MEDIATION.value,
                            "to": ProjectState.WAITING_APPROVAL.value,
                        },
                        at=now,
                    )
                )
            else:
                eff_plan_hash = plan.compute_hash()
                validate_transition(ProjectState.MEDIATION, ProjectState.WAITING_APPROVAL)
                self.store.append(
                    create_event(
                        project_id=project_id,
                        event_type=EventType.PLAN_CREATED,
                        payload={
                            "plan": plan.model_dump(mode="json"),
                            "plan_hash": eff_plan_hash,
                            "total_tests": len(plan.acceptance_tests),
                            "from": ProjectState.MEDIATION.value,
                            "to": ProjectState.WAITING_APPROVAL.value,
                        },
                        at=now,
                    )
                )

            self.circuit_breaker.reset(project_id, ProjectState.BUILDING)
            self.circuit_breaker.reset(project_id, ProjectState.VERIFYING)

            validate_transition(ProjectState.WAITING_APPROVAL, ProjectState.BUILDING)
            self.store.append(
                create_event(
                    project_id=project_id,
                    event_type=EventType.APPROVAL_RECORDED,
                    payload={
                        "plan_hash": eff_plan_hash,
                        "decision": "approved",
                        "timestamp": now.isoformat(),
                        "from": ProjectState.WAITING_APPROVAL.value,
                        "to": ProjectState.BUILDING.value,
                        "mode": "quality_improve",
                        "instructions": reason,
                    },
                    at=now,
                )
            )
            return eff_plan_hash

    def improve_project_quality(
        self,
        project_id: str,
        *,
        build_worker: Any | None = None,
        auditor: Any | None = None,
        chats_dir: Path | None = None,
        force_polish: bool = False,
        extra_secrets: Sequence[str] = (),
    ) -> dict[str, Any]:
        """Audit project quality and run bounded 2-agent debate via BuildWorker when needed or under force_polish."""
        validate_project_id(project_id)
        eff_worker = build_worker if build_worker is not None else self.build_worker
        if eff_worker is not None:
            return eff_worker.improve_project_quality(
                project_id,
                auditor=auditor,
                chats_dir=chats_dir,
                force_polish=force_polish,
            )
        eval_res = self.evaluate_project_quality(
            project_id,
            auditor=auditor,
            chats_dir=chats_dir,
            extra_secrets=extra_secrets,
        )
        if not force_polish and eval_res["scorecard"].get("passed"):
            return {
                **eval_res,
                "already_passed": True,
                "improved": False,
            }
        raise RuntimeError(
            "No hay un BuildWorker configurado para ejecutar el refinamiento de calidad."
        )

    @staticmethod
    def _run_rollback_git(
        runner: Any,
        args: Sequence[str],
        ws_dir: Path,
        *,
        env: Mapping[str, str] | None = None,
        context: str,
        extra_secrets: Sequence[str] = (),
    ) -> str:
        res = runner.run(["git", *args], cwd=ws_dir, timeout_seconds=20.0, env=env)
        if res.exit_code != 0:
            detail = redact_secrets(
                (res.stderr or res.stdout or f"exit code {res.exit_code}").strip(),
                extra_secrets=extra_secrets,
            )
            raise RuntimeError(f"{context}: {detail}")
        return res.stdout.strip()

    @staticmethod
    def _run_rollback_pytest(runner: Any, ws_dir: Path, extra_secrets: Sequence[str]) -> None:
        import shutil
        import sys

        from agent_factory.workspace import is_python_cache_path

        tests_dir = ws_dir / "tests"
        has_tests = (
            tests_dir.is_dir()
            and any(
                p.is_file() and not is_python_cache_path(p.relative_to(ws_dir))
                for p in tests_dir.rglob("*")
            )
        ) or any(ws_dir.glob("test_*.py")) or any(ws_dir.glob("*_test.py"))
        if not has_tests:
            return

        pytest_bin: list[str] = (
            ["pytest"]
            if runner.__class__.__name__ == "FakeRunner" or shutil.which("pytest")
            else [sys.executable, "-m", "pytest"]
        )
        ws_ini = ws_dir / "pytest.ini"
        tmp_ini: Path | None = None
        try:
            if ws_ini.is_file():
                ini_arg = ws_ini
            else:
                with tempfile.NamedTemporaryFile(
                    mode="w", encoding="utf-8", suffix=".ini", prefix="af_rb_", delete=False
                ) as tf:
                    tf.write(DEFAULT_WORKSPACE_PYTEST_INI)
                    tmp_ini = Path(tf.name)
                ini_arg = tmp_ini
            res = runner.run(
                [
                    *pytest_bin,
                    "-q",
                    "--tb=short",
                    "-p",
                    "no:cov",
                    "-p",
                    "no:cacheprovider",
                    "-o",
                    "addopts=",
                    "-c",
                    str(ini_arg),
                    f"--rootdir={ws_dir}",
                ],
                cwd=ws_dir,
                timeout_seconds=30.0,
            )
        finally:
            if tmp_ini is not None:
                tmp_ini.unlink(missing_ok=True)

        if res.exit_code not in (0, 5):
            raw_out = "\n".join(
                line for line in f"{res.stdout}\n{res.stderr}".splitlines() if line.strip()
            )
            tail = redact_secrets(raw_out[-500:], extra_secrets=extra_secrets)
            raise RuntimeError(f"Las pruebas fallaron tras restaurar la versión: {tail}")

    def rollback_project(
        self,
        project_id: str,
        commit_sha: str,
        *,
        workspace_manager: WorkspaceManager | None = None,
        runner: Any | None = None,
        local_app_runner: Any | None = None,
        vcs: Any | None = None,
        extra_secrets: Sequence[str] = (),
    ) -> tuple[ProjectStatus, dict[str, Any]]:
        """Restore isolated repo to commit_sha via clean commit, verify pytest + smoke test, push if origin exists, and record PROJECT_DELIVERED (INV-04, INV-11, INV-22)."""
        from agent_factory.adapters.agy_cli_adapter import build_sanitized_cli_env
        from agent_factory.adapters.github_vcs_adapter import build_git_push_env
        from agent_factory.adapters.subprocess_runner import SubprocessRunner
        from agent_factory.runtime_runner import LocalAppRunner, run_http_smoke_test

        validate_project_id(project_id)
        target_sha = validate_commit_sha(commit_sha)

        with self._project_lock(project_id):
            events = self.store.get_events(project_id)
            if not events:
                raise ValueError(f"Project '{project_id}' does not exist")
            current_status = self.get_status(project_id)
            if current_status.state != ProjectState.DELIVERED:
                raise IllegalTransition(current_status.state, ProjectState.CHANGE_REQUEST)

            ws_dir = self._resolve_project_workspace_dir(
                project_id, status=current_status, workspace_manager=workspace_manager
            )
            if ws_dir is None:
                raise ValueError(f"El workspace del proyecto '{project_id}' no existe.")

            eff_runner = runner if runner is not None else SubprocessRunner(default_timeout_seconds=25.0)
            ws_resolved = verify_isolated_git_root(ws_dir, eff_runner)
            clean_env = build_sanitized_cli_env()

            rev_res = eff_runner.run(
                ["git", "rev-parse", "--verify", f"{target_sha}^{{commit}}"],
                cwd=ws_resolved,
                timeout_seconds=10.0,
                env=clean_env,
            )
            full_target_sha = rev_res.stdout.strip().lower()
            if rev_res.exit_code != 0 or not re.fullmatch(r"[0-9a-f]{40}", full_target_sha):
                raise SandboxViolationError(
                    f"El commit '{target_sha}' no existe en el historial aislado de '{project_id}' (INV-22)."
                )

            anc_res = eff_runner.run(
                ["git", "merge-base", "--is-ancestor", full_target_sha, "HEAD"],
                cwd=ws_resolved,
                timeout_seconds=10.0,
                env=clean_env,
            )
            if anc_res.exit_code != 0:
                raise SandboxViolationError(
                    f"El commit '{target_sha}' no pertenece al historial actual de '{project_id}' (INV-22)."
                )

            pre_sha = read_git_head_sha_from_disk(ws_resolved) or self._run_rollback_git(
                eff_runner,
                ["rev-parse", "--verify", "HEAD"],
                ws_resolved,
                env=clean_env,
                context="No se pudo leer el commit actual",
                extra_secrets=extra_secrets,
            )

            if local_app_runner is not None:
                local_app_runner.stop(project_id, workspace_dir=ws_resolved)

            self._run_rollback_git(
                eff_runner,
                ["checkout", full_target_sha, "--", "."],
                ws_resolved,
                env=clean_env,
                context="No se pudo restaurar el árbol de trabajo",
                extra_secrets=extra_secrets,
            )
            diff_cached = eff_runner.run(
                ["git", "diff", "--cached", "--quiet"],
                cwd=ws_resolved,
                timeout_seconds=10.0,
                env=clean_env,
            )
            allow_empty = ["--allow-empty"] if diff_cached.exit_code == 0 else []
            self._run_rollback_git(
                eff_runner,
                [
                    "-c",
                    "user.name=AgentFactory",
                    "-c",
                    "user.email=agent@factory.local",
                    "commit",
                    *allow_empty,
                    "-m",
                    f"chore({project_id}): rollback to {full_target_sha[:7]}",
                ],
                ws_resolved,
                env=clean_env,
                context="No se pudo crear el commit de rollback",
                extra_secrets=extra_secrets,
            )

            try:
                self._run_rollback_pytest(eff_runner, ws_resolved, extra_secrets)
                smoke_runner = (
                    local_app_runner
                    if local_app_runner is not None
                    else LocalAppRunner(
                        workspace_manager=workspace_manager or self.workspace_manager,
                        secrets_to_redact=extra_secrets,
                    )
                )
                smoke_res = run_http_smoke_test(smoke_runner, project_id, ws_resolved)
                if not smoke_res.passed:
                    clean_smoke = redact_secrets(smoke_res.summary, extra_secrets=extra_secrets)
                    raise RuntimeError(f"Smoke Test HTTP falló tras el rollback: {clean_smoke}")

                git_cfg_file = ws_resolved / ".git" / "config"
                has_origin_cfg = False
                if git_cfg_file.is_file():
                    try:
                        has_origin_cfg = '[remote "origin"]' in git_cfg_file.read_text(
                            encoding="utf-8", errors="ignore"
                        )
                    except OSError:
                        has_origin_cfg = True
                if has_origin_cfg:
                    head_ref_file = ws_resolved / ".git" / "HEAD"
                    cur_branch = "main"
                    if head_ref_file.is_file():
                        try:
                            htxt = head_ref_file.read_text(encoding="utf-8", errors="ignore").strip()
                            if htxt.startswith("ref: refs/heads/"):
                                cur_branch = htxt[len("ref: refs/heads/") :].strip() or "main"
                        except OSError:
                            pass
                    gh_tok = str(getattr(vcs, "github_token", "") or "")
                    repo_base = str(getattr(vcs, "repo_base_url", "") or "https://github.com")
                    push_env, push_secrets = build_git_push_env(
                        clean_env, gh_tok, repo_base_url=repo_base
                    )
                    self._run_rollback_git(
                        eff_runner,
                        ["push", "-u", "origin", cur_branch],
                        ws_resolved,
                        env=push_env,
                        context="No se pudo hacer push del rollback a origin",
                        extra_secrets=(*extra_secrets, *push_secrets),
                    )
            except Exception:
                eff_runner.run(
                    ["git", "reset", "--hard", pre_sha],
                    cwd=ws_resolved,
                    timeout_seconds=15.0,
                    env=clean_env,
                )
                raise

            new_sha = read_git_head_sha_from_disk(ws_resolved) or self._run_rollback_git(
                eff_runner,
                ["rev-parse", "--verify", "HEAD"],
                ws_resolved,
                env=clean_env,
                context="No se pudo verificar el commit de rollback",
                extra_secrets=extra_secrets,
            )

            now = self._now()
            prior_delivered = sum(1 for ev in events if ev.type == EventType.PROJECT_DELIVERED)
            new_label = f"v{prior_delivered + 1}"
            target_cp_label = next(
                (
                    str((ev.payload or {}).get("version_label") or "")
                    for ev in events
                    if ev.type == EventType.PROJECT_DELIVERED
                    and str((ev.payload or {}).get("commit_sha") or "").lower().startswith(full_target_sha[:7])
                ),
                full_target_sha[:7],
            ) or full_target_sha[:7]

            eff_plan_hash = current_status.plan_hash
            if not eff_plan_hash:
                fallback_plan = Plan(
                    summary=f"Rollback {project_id}",
                    rationale="Restauración de checkpoint estable.",
                    template_id="static-web",
                    files=[],
                    acceptance_tests=[
                        AcceptanceTest(id="AT-RB-01", description="Rollback verified", type=AcceptanceTestType.UNIT)
                    ],
                    risks=[],
                    deployment_target="local",
                )
                eff_plan_hash = fallback_plan.compute_hash()

            for src_state, dst_state, extra_pl in (
                (
                    ProjectState.DELIVERED,
                    ProjectState.CHANGE_REQUEST,
                    {"reason": f"Rollback a {target_cp_label}", "rollback_to": full_target_sha},
                ),
                (ProjectState.CHANGE_REQUEST, ProjectState.INTERVIEW, {}),
                (ProjectState.INTERVIEW, ProjectState.DEBATE, {}),
                (ProjectState.DEBATE, ProjectState.MEDIATION, {}),
            ):
                validate_transition(src_state, dst_state)
                self.store.append(
                    create_event(
                        project_id,
                        EventType.STATE_TRANSITIONED,
                        {"from": src_state.value, "to": dst_state.value, **extra_pl},
                        at=now,
                    )
                )

            if current_status.plan_hash:
                validate_transition(ProjectState.MEDIATION, ProjectState.WAITING_APPROVAL)
                self.store.append(
                    create_event(
                        project_id,
                        EventType.STATE_TRANSITIONED,
                        {"from": ProjectState.MEDIATION.value, "to": ProjectState.WAITING_APPROVAL.value},
                        at=now,
                    )
                )
            else:
                validate_transition(ProjectState.MEDIATION, ProjectState.WAITING_APPROVAL)
                self.store.append(
                    create_event(
                        project_id,
                        EventType.PLAN_CREATED,
                        {"to": ProjectState.WAITING_APPROVAL.value, "plan_hash": eff_plan_hash, "total_tests": 1},
                        at=now,
                    )
                )

            validate_transition(ProjectState.WAITING_APPROVAL, ProjectState.BUILDING)
            self.store.append(
                create_event(
                    project_id,
                    EventType.APPROVAL_RECORDED,
                    {
                        "plan_hash": eff_plan_hash,
                        "decision": "approved",
                        "to": ProjectState.BUILDING.value,
                        "mode": "rollback",
                        "rollback_to": full_target_sha,
                    },
                    at=now,
                )
            )
            validate_transition(ProjectState.BUILDING, ProjectState.VERIFYING)
            self.store.append(
                create_event(
                    project_id,
                    EventType.STATE_TRANSITIONED,
                    {"from": ProjectState.BUILDING.value, "to": ProjectState.VERIFYING.value},
                    at=now,
                )
            )
            validate_transition(ProjectState.VERIFYING, ProjectState.DELIVERED)
            delivered_pl: dict[str, Any] = {
                "repo_url": current_status.links.repo if current_status.links else None,
                "deploy_url": current_status.links.deploy if current_status.links else None,
                "commit_sha": new_sha,
                "version_label": new_label,
                "rollback_from": full_target_sha,
                "summary": f"Restaurado a {target_cp_label} ({full_target_sha[:7]})",
            }
            self.store.append(
                create_event(project_id, EventType.PROJECT_DELIVERED, delivered_pl, at=now)
            )

            final_status = self.get_status(project_id)
            checkpoints = self.list_project_checkpoints(project_id, extra_secrets=extra_secrets)
            return final_status, (checkpoints[0] if checkpoints else {"commit_sha": new_sha, "version_label": new_label})

    def modify_project(
        self,
        project_id: str,
        instructions: str,
        *,
        target_files: Sequence[str] | None = None,
        workspace_manager: WorkspaceManager | None = None,
    ) -> ProjectStatus:
        """Transition a DELIVERED project legally via CHANGE_REQUEST to BUILDING preserving workspace (INV-04, INV-05)."""
        validate_project_id(project_id)
        clean_instr = _CTRL_CHARS_RE.sub(" ", str(instructions or "")).strip()
        if not clean_instr:
            raise ValueError("Las instrucciones de modificación son obligatorias.")
        with self._project_lock(project_id):
            events = self.store.get_events(project_id)
            if not events:
                raise ValueError(f"Project '{project_id}' does not exist")
            current_status = self.get_status(project_id)
            if current_status.state != ProjectState.DELIVERED:
                raise IllegalTransition(current_status.state, ProjectState.CHANGE_REQUEST)
            base_plan = self.get_plan(project_id)
            if base_plan is None or not current_status.plan_hash:
                raise ValueError(f"Project '{project_id}' has no approved plan to modify")

            ws_dir = self._resolve_project_workspace_dir(
                project_id, status=current_status, workspace_manager=workspace_manager
            )
            clean_targets = sanitize_target_files(ws_dir, target_files)

            # INV-04 canonical sequence: DELIVERED -> CHANGE_REQUEST -> INTERVIEW -> DEBATE -> MEDIATION -> WAITING_APPROVAL -> BUILDING
            cr_extra: dict[str, Any] = {"reason": clean_instr, "instructions": clean_instr}
            if clean_targets:
                cr_extra["target_files"] = clean_targets
            for src_state, dst_state, extra_payload in (
                (
                    ProjectState.DELIVERED,
                    ProjectState.CHANGE_REQUEST,
                    cr_extra,
                ),
                (ProjectState.CHANGE_REQUEST, ProjectState.INTERVIEW, {}),
            ):
                validate_transition(src_state, dst_state)
                self.store.append(
                    create_event(
                        project_id=project_id,
                        event_type=EventType.STATE_TRANSITIONED,
                        payload={"from": src_state.value, "to": dst_state.value, **extra_payload},
                        at=self._now(),
                    )
                )

            spec = self._get_spec(project_id)
            feature_entry = f"Cambio solicitado: {clean_instr}"
            updated_features = list(spec.features)
            if feature_entry not in updated_features:
                updated_features.append(feature_entry)
            updated_spec = spec.model_copy(update={"features": updated_features})
            with self._lock:
                self._specs[project_id] = updated_spec
            spec_pl: dict[str, Any] = {
                "spec": updated_spec.model_dump(mode="json"),
                "change_instructions": clean_instr,
            }
            if clean_targets:
                spec_pl["target_files"] = clean_targets
            self.store.append(
                create_event(
                    project_id=project_id,
                    event_type=EventType.SPEC_UPDATED,
                    payload=spec_pl,
                    at=self._now(),
                )
            )

            for src_state, dst_state in (
                (ProjectState.INTERVIEW, ProjectState.DEBATE),
                (ProjectState.DEBATE, ProjectState.MEDIATION),
            ):
                validate_transition(src_state, dst_state)
                self.store.append(
                    create_event(
                        project_id=project_id,
                        event_type=EventType.STATE_TRANSITIONED,
                        payload={"from": src_state.value, "to": dst_state.value},
                        at=self._now(),
                    )
                )

            mod_idx = len(base_plan.acceptance_tests) + 1
            updated_tests = [
                *base_plan.acceptance_tests,
                AcceptanceTest(
                    id=f"MOD-{mod_idx:02d}",
                    description=clean_instr,
                    type=AcceptanceTestType.E2E,
                ),
            ]
            updated_plan = base_plan.model_copy(update={"acceptance_tests": updated_tests})
            new_plan_hash = updated_plan.compute_hash()
            validate_transition(ProjectState.MEDIATION, ProjectState.WAITING_APPROVAL)
            self.store.append(
                create_event(
                    project_id=project_id,
                    event_type=EventType.PLAN_CREATED,
                    payload={
                        "plan": updated_plan.model_dump(mode="json"),
                        "plan_hash": new_plan_hash,
                        "total_tests": len(updated_plan.acceptance_tests),
                        "from": ProjectState.MEDIATION.value,
                        "to": ProjectState.WAITING_APPROVAL.value,
                        "instructions": clean_instr,
                    },
                    at=self._now(),
                )
            )

            self.circuit_breaker.reset(project_id, ProjectState.BUILDING)
            self.circuit_breaker.reset(project_id, ProjectState.VERIFYING)

            validate_transition(ProjectState.WAITING_APPROVAL, ProjectState.BUILDING)
            app_pl: dict[str, Any] = {
                "plan_hash": new_plan_hash,
                "decision": "approved",
                "timestamp": self._now().isoformat(),
                "from": ProjectState.WAITING_APPROVAL.value,
                "to": ProjectState.BUILDING.value,
                "mode": "modify",
                "instructions": clean_instr,
            }
            if clean_targets:
                app_pl["target_files"] = clean_targets
            self.store.append(
                create_event(
                    project_id=project_id,
                    event_type=EventType.APPROVAL_RECORDED,
                    payload=app_pl,
                    at=self._now(),
                )
            )
            return self.get_status(project_id)

    def handle_interview_message(
        self,
        project_id: str,
        event_id: str,
        user_message: str,
    ) -> ProjectStatus:
        """Process an incoming user message idempotently during INTERVIEW (INV-16, INV-17)."""
        validate_project_id(project_id)
        with self._project_lock(project_id):
            events = self.store.get_events(project_id)
            if not events:
                raise ValueError(f"Project '{project_id}' does not exist")

            current_status = self.get_status(project_id)

            if self.inbox.is_processed(event_id) or any(
                e.payload.get("incoming_event_id") == event_id for e in events
            ):
                self.inbox.mark_processed(event_id)
                return current_status

            if current_status.state != ProjectState.INTERVIEW:
                raise IllegalTransition(current_status.state, ProjectState.INTERVIEW)

            self.inbox.mark_processed(event_id)

            try:
                self.circuit_breaker.record_call(project_id, ProjectState.INTERVIEW)
            except CircuitBreakerTrippedError as err:
                return self.record_phase_failure(
                    project_id,
                    failed_state=current_status.state,
                    error=err,
                    agent="circuit_breaker",
                )

            interview_t0 = time.monotonic()
            try:
                self._report_activity(
                    ACTIVITY_MESSAGES["INTERVIEW"],
                    agents_count=1,
                    stage="INTERVIEW",
                )
                interviewer_payload: dict[str, Any] = {
                    "agent": "interviewer",
                    "incoming_event_id": event_id,
                }
                interviewer_model = self._resolve_llm_model_hint("INTERVIEW")
                if interviewer_model:
                    interviewer_payload["model"] = interviewer_model
                self.store.append(
                    create_event(
                        project_id=project_id,
                        event_type=EventType.AGENT_INVOKED,
                        payload=interviewer_payload,
                        at=self._now(),
                    )
                )

                current_spec = self._get_spec(project_id)
                interviewer = Interviewer(llm=self.llm)
                round_result = interviewer.run_round(current_spec, user_message)
                with self._lock:
                    self._specs[project_id] = round_result.spec
            except (
                LLMError,
                ValueError,
                TypeError,
                LookupError,
                TimeoutError,
                OSError,
                RuntimeError,
            ) as err:
                return self.record_phase_failure(
                    project_id,
                    failed_state=current_status.state,
                    error=err,
                    agent="interviewer",
                )

            interview_duration = round(max(0.0, time.monotonic() - interview_t0), 3)
            self.store.append(
                create_event(
                    project_id=project_id,
                    event_type=EventType.INTERVIEW_ROUND,
                    payload={
                        "incoming_event_id": event_id,
                        "questions": round_result.questions,
                        "is_complete": round_result.is_complete,
                        "terminated_by_limit": round_result.terminated_by_limit,
                        "duration_seconds": interview_duration,
                    },
                    at=self._now(),
                )
            )
            self.store.append(
                create_event(
                    project_id=project_id,
                    event_type=EventType.SPEC_UPDATED,
                    payload={"spec": round_result.spec.model_dump(mode="json")},
                    at=self._now(),
                )
            )

            return self.get_status(project_id)

    def _latest_debate_artifacts(
        self, events: Sequence[Event]
    ) -> tuple[PragmatistProposal | None, StructuralistCritique | None]:
        proposal: PragmatistProposal | None = None
        critique: StructuralistCritique | None = None
        for ev in events:
            if ev.type == EventType.PRAGMATIST_PROPOSED and "proposal" in ev.payload:
                try:
                    proposal = PragmatistProposal.model_validate(ev.payload["proposal"])
                except ValueError:
                    proposal = None
            elif ev.type == EventType.STRUCTURALIST_CRITIQUED and "critique" in ev.payload:
                try:
                    critique = StructuralistCritique.model_validate(ev.payload["critique"])
                except ValueError:
                    critique = None
        if proposal is not None and critique is not None:
            return proposal, critique
        return None, None

    def run_debate(self, project_id: str) -> ProjectStatus:
        """Run Pragmatist -> Structuralist -> Mediator pipeline and advance to WAITING_APPROVAL."""
        validate_project_id(project_id)
        with self._project_lock(project_id):
            events = self.store.get_events(project_id)
            if not events:
                raise ValueError(f"Project '{project_id}' does not exist")

            current_status = self.get_status(project_id)
            resuming_debate = current_status.state == ProjectState.DEBATE
            resuming_mediation = current_status.state == ProjectState.MEDIATION

            if not (resuming_debate or resuming_mediation):
                validate_transition(current_status.state, ProjectState.DEBATE)

            current_spec = self._get_spec(project_id)
            if not current_spec.is_complete():
                raise ValueError("Spec checklist is incomplete")

            if not (resuming_debate or resuming_mediation):
                self.store.append(
                    create_event(
                        project_id=project_id,
                        event_type=EventType.STATE_TRANSITIONED,
                        payload={"from": current_status.state, "to": ProjectState.DEBATE},
                        at=self._now(),
                    )
                )

            debate_t0 = time.monotonic()
            mediation_t0: float | None = None
            if resuming_mediation:
                mediation_t0 = time.monotonic()
                self._report_activity(
                    ACTIVITY_MESSAGES["MEDIATION"],
                    agents_count=1,
                    stage="MEDIATION",
                )
            else:
                self._report_activity(
                    ACTIVITY_MESSAGES["DEBATE"],
                    agents_count=2,
                    stage="DEBATE",
                )

            def _on_llm_call(state: ProjectState) -> None:
                self.circuit_breaker.record_call(project_id, state)

            def _on_transition(from_state: ProjectState, to_state: ProjectState) -> None:
                nonlocal mediation_t0
                validate_transition(from_state, to_state)
                if to_state == ProjectState.MEDIATION:
                    mediation_t0 = time.monotonic()
                    self._report_activity(
                        ACTIVITY_MESSAGES["MEDIATION"],
                        agents_count=1,
                        stage="MEDIATION",
                    )
                self.store.append(
                    create_event(
                        project_id=project_id,
                        event_type=EventType.STATE_TRANSITIONED,
                        payload={"from": from_state, "to": to_state},
                        at=self._now(),
                    )
                )

            def _on_debate_complete(
                proposal: PragmatistProposal,
                critique: StructuralistCritique,
            ) -> None:
                debate_duration = round(max(0.0, time.monotonic() - debate_t0), 3)
                self.store.append(
                    create_event(
                        project_id=project_id,
                        event_type=EventType.PRAGMATIST_PROPOSED,
                        payload={
                            "proposal": proposal.model_dump(mode="json"),
                            "duration_seconds": debate_duration,
                        },
                        at=self._now(),
                    )
                )
                self.store.append(
                    create_event(
                        project_id=project_id,
                        event_type=EventType.STRUCTURALIST_CRITIQUED,
                        payload={
                            "critique": critique.model_dump(mode="json"),
                            "duration_seconds": debate_duration,
                        },
                        at=self._now(),
                    )
                )

            plan: Plan | None = None
            try:
                if resuming_mediation:
                    stored_proposal, stored_critique = self._latest_debate_artifacts(events)
                    if stored_proposal is not None and stored_critique is not None:
                        med_wrapper = _PipelineLLMWrapper(inner=self.llm, on_call=_on_llm_call)
                        med_wrapper.current_state = ProjectState.MEDIATION
                        mediator = Mediator(
                            llm=med_wrapper,
                            valid_templates=self.valid_templates,
                            clock=self.clock,
                        )
                        plan = mediator.mediate(current_spec, stored_proposal, stored_critique)
                    else:
                        result = run_debate_pipeline(
                            spec=current_spec,
                            llm=self.llm,
                            valid_templates=self.valid_templates,
                            clock=self.clock,
                            on_llm_call=_on_llm_call,
                            on_transition=None,
                            on_debate_complete=_on_debate_complete,
                        )
                        if result.state == ProjectState.FAILED:
                            return self.record_phase_failure(
                                project_id,
                                failed_state=ProjectState.MEDIATION,
                                error=result.failure_cause or "Debate pipeline failed",
                                agent="mediator",
                            )
                        plan = result.plan
                else:
                    result = run_debate_pipeline(
                        spec=current_spec,
                        llm=self.llm,
                        valid_templates=self.valid_templates,
                        clock=self.clock,
                        on_llm_call=_on_llm_call,
                        on_transition=_on_transition,
                        on_debate_complete=_on_debate_complete,
                    )
                    if result.state == ProjectState.FAILED:
                        cur_state = self.get_status(project_id).state
                        failed_in = (
                            cur_state
                            if cur_state in (ProjectState.DEBATE, ProjectState.MEDIATION)
                            else ProjectState.DEBATE
                        )
                        return self.record_phase_failure(
                            project_id,
                            failed_state=failed_in,
                            error=result.failure_cause or "Debate pipeline failed",
                            agent="mediator" if failed_in == ProjectState.MEDIATION else "debate",
                        )
                    plan = result.plan
            except (
                LLMError,
                PlanValidationError,
                ValueError,
                TypeError,
                LookupError,
                TimeoutError,
                CircuitBreakerTrippedError,
                IllegalTransition,
                OSError,
                RuntimeError,
            ) as err:
                cur_state = self.get_status(project_id).state
                failed_in = (
                    cur_state
                    if cur_state in (ProjectState.DEBATE, ProjectState.MEDIATION)
                    else ProjectState.DEBATE
                )
                return self.record_phase_failure(
                    project_id,
                    failed_state=failed_in,
                    error=err,
                    agent="circuit_breaker"
                    if isinstance(err, CircuitBreakerTrippedError)
                    else ("mediator" if failed_in == ProjectState.MEDIATION else "debate"),
                )

            assert plan is not None
            mediation_duration = round(
                max(0.0, time.monotonic() - (mediation_t0 if mediation_t0 is not None else debate_t0)),
                3,
            )
            plan_hash = plan.compute_hash()
            validate_transition(ProjectState.MEDIATION, ProjectState.WAITING_APPROVAL)
            self.store.append(
                create_event(
                    project_id=project_id,
                    event_type=EventType.PLAN_CREATED,
                    payload={
                        "plan": plan.model_dump(mode="json"),
                        "plan_hash": plan_hash,
                        "total_tests": len(plan.acceptance_tests),
                        "duration_seconds": mediation_duration,
                        "to": ProjectState.WAITING_APPROVAL,
                    },
                    at=self._now(),
                )
            )
            return self.get_status(project_id)

    def submit_approval(
        self,
        project_id: str,
        approval: ApprovalEvent | None,
    ) -> ProjectStatus:
        """Validate ApprovalEvent and plan_hash before transitioning to BUILDING (INV-05)."""
        validate_project_id(project_id)
        with self._project_lock(project_id):
            events = self.store.get_events(project_id)
            if not events:
                raise ValueError(f"Project '{project_id}' does not exist")

            current_status = self.get_status(project_id)
            if current_status.state != ProjectState.WAITING_APPROVAL:
                raise ApprovalError(f"Cannot submit approval in state {current_status.state}")
            if approval is None:
                raise ApprovalError("ApprovalEvent is required to transition to BUILDING (INV-05)")
            if approval.decision != "approved":
                raise ApprovalError(
                    f"Approval rejected or non-approved decision: '{approval.decision}'"
                )
            if not current_status.plan_hash or approval.plan_hash != current_status.plan_hash:
                raise ApprovalError(
                    f"Approval plan_hash '{approval.plan_hash}' does not match "
                    f"current plan_hash '{current_status.plan_hash}' (INV-05)"
                )

            validate_transition(current_status.state, ProjectState.BUILDING)
            self.store.append(
                create_event(
                    project_id=project_id,
                    event_type=EventType.APPROVAL_RECORDED,
                    payload={
                        "plan_hash": approval.plan_hash,
                        "decision": "approved",
                        "timestamp": approval.timestamp.isoformat(),
                        "to": ProjectState.BUILDING,
                    },
                    at=self._now(),
                )
            )
            return self.get_status(project_id)

    def request_adjustment(
        self,
        project_id: str,
        reason: str = "",
    ) -> ProjectStatus:
        """Transition a project in WAITING_APPROVAL back to INTERVIEW for plan adjustment (INV-04, INV-05)."""
        validate_project_id(project_id)
        with self._project_lock(project_id):
            current_status = self.get_status(project_id)
            if current_status.state != ProjectState.WAITING_APPROVAL:
                raise IllegalTransition(current_status.state, ProjectState.INTERVIEW)
            validate_transition(current_status.state, ProjectState.INTERVIEW)
            self.store.append(
                create_event(
                    project_id=project_id,
                    event_type=EventType.STATE_TRANSITIONED,
                    payload={
                        "from": current_status.state.value,
                        "to": ProjectState.INTERVIEW.value,
                        "reason": reason or "Adjustment requested by user",
                    },
                    at=self._now(),
                )
            )
            return self.get_status(project_id)

    @staticmethod
    def _extract_active_interview_questions(events: Sequence[Event]) -> list[str]:
        """Extract cleaned non-empty questions from the latest active INTERVIEW_ROUND."""
        for ev in reversed(events):
            if ev.type == EventType.INTERVIEW_ROUND:
                pl = ev.payload or {}
                raw_questions = pl.get("questions") if isinstance(pl, Mapping) else None
                if isinstance(raw_questions, Sequence) and not isinstance(
                    raw_questions, (str, bytes)
                ):
                    return [
                        q.strip()
                        for q in raw_questions
                        if isinstance(q, str) and q.strip()
                    ]
                return []
            if ev.type in {EventType.STATE_TRANSITIONED, EventType.PROJECT_RETRIED}:
                pl = ev.payload or {}
                if pl.get("to") == ProjectState.INTERVIEW.value:
                    return []
            elif ev.type == EventType.PROJECT_CREATED:
                return []
        return []

    @classmethod
    def _has_active_interview_questions(cls, events: Sequence[Event]) -> bool:
        """Return True if the latest active INTERVIEW phase already produced non-empty questions."""
        return bool(cls._extract_active_interview_questions(events))

    @staticmethod
    def _resolve_interview_retry_prompt(
        events: Sequence[Event],
        status: ProjectStatus,
    ) -> str:
        """Pick the best user prompt/objective for regenerating INTERVIEW questions on retry."""
        for ev in reversed(events):
            if ev.type in {"ADJUSTMENT_REQUESTED", "CHANGE_REQUEST", EventType.STATE_TRANSITIONED}:
                pl = ev.payload or {}
                if (
                    ev.type == EventType.STATE_TRANSITIONED
                    and pl.get("to") != ProjectState.INTERVIEW.value
                ):
                    continue
                for key in ("reason", "change_description", "prompt", "message"):
                    val = pl.get(key)
                    if isinstance(val, str) and val.strip():
                        return val.strip()
        return (
            (status.objective or "").strip()
            or (status.name or "").strip()
            or "Genera preguntas iniciales para definir el proyecto."
        )

    def retry_project(
        self,
        project_id: str,
        *,
        workspace_manager: WorkspaceManager | None = None,
        clean_workspace: bool = False,
        regenerate_interview: bool = True,
    ) -> ProjectStatus:
        """Reset failure counters for a FAILED or orphaned active project, optionally clean workspace, and return to recoverable state."""
        validate_project_id(project_id)
        with self._project_lock(project_id):
            events = self.store.get_events(project_id)
            if not events:
                raise ValueError(f"Project '{project_id}' does not exist")

            current_status = self.get_status(project_id)
            allowed_retry_states = {
                ProjectState.FAILED,
                ProjectState.INTERVIEW,
                ProjectState.BUILDING,
                ProjectState.VERIFYING,
                ProjectState.DEBATE,
                ProjectState.MEDIATION,
            }
            if current_status.state not in allowed_retry_states:
                raise ValueError(
                    f"Cannot retry project '{project_id}' in state {current_status.state.value}: "
                    f"expected one of {[s.value for s in allowed_retry_states]}"
                )

            had_active_questions = self._has_active_interview_questions(events)

            tracked = ProjectState.IDLE
            last_active_state = ProjectState.INTERVIEW
            latest_plan: Plan | None = None
            adjusted_after_plan = False

            for ev in events:
                pl: Mapping[str, Any] = ev.payload or {}
                if ev.type == EventType.PROJECT_CREATED:
                    tracked = ProjectState(pl.get("state", ProjectState.INTERVIEW))
                    last_active_state = tracked
                elif ev.type == EventType.STATE_TRANSITIONED:
                    nxt = ProjectState(pl["to"])
                    if nxt == ProjectState.INTERVIEW and tracked == ProjectState.WAITING_APPROVAL:
                        adjusted_after_plan = True
                    tracked = nxt
                    if tracked != ProjectState.FAILED:
                        last_active_state = tracked
                elif ev.type == EventType.PLAN_CREATED:
                    if "plan" in pl:
                        try:
                            latest_plan = Plan.model_validate(pl["plan"])
                            adjusted_after_plan = False
                        except ValueError:
                            pass
                    tracked = ProjectState(pl.get("to", ProjectState.WAITING_APPROVAL))
                    last_active_state = tracked
                elif ev.type == EventType.APPROVAL_RECORDED:
                    tracked = ProjectState(pl.get("to", ProjectState.BUILDING))
                    last_active_state = tracked
                elif ev.type == EventType.PROJECT_RETRIED:
                    nxt = ProjectState(pl["to"])
                    tracked = nxt
                    last_active_state = tracked
                elif ev.type == EventType.PHASE_ERROR and "state" in pl:
                    try:
                        candidate = ProjectState(pl["state"])
                        if candidate != ProjectState.FAILED:
                            last_active_state = candidate
                    except ValueError:
                        pass
                elif ev.type == EventType.PROJECT_FAILED:
                    if "failed_from" in pl:
                        try:
                            candidate = ProjectState(pl["failed_from"])
                            if candidate != ProjectState.FAILED:
                                last_active_state = candidate
                        except ValueError:
                            pass
                    tracked = ProjectState.FAILED
                elif ev.type == EventType.PROJECT_DELIVERED:
                    tracked = ProjectState.DELIVERED

            has_valid_plan = (latest_plan is not None and not adjusted_after_plan) or (
                current_status.plan_hash is not None
                and last_active_state
                in {ProjectState.WAITING_APPROVAL, ProjectState.BUILDING, ProjectState.VERIFYING}
            )
            target_state = ProjectState.WAITING_APPROVAL if has_valid_plan else last_active_state

            self.circuit_breaker.reset(project_id, last_active_state)
            if target_state != last_active_state:
                self.circuit_breaker.reset(project_id, target_state)
            if target_state == ProjectState.INTERVIEW:
                self.circuit_breaker.reset(project_id, ProjectState.INTERVIEW)
            if last_active_state in {ProjectState.BUILDING, ProjectState.VERIFYING}:
                self.circuit_breaker.reset(project_id, ProjectState.BUILDING)
                self.circuit_breaker.reset(project_id, ProjectState.VERIFYING)

            validate_transition(current_status.state, target_state, allow_retry=True)

            workspace_cleaned = False
            effective_ws_mgr = (
                workspace_manager if workspace_manager is not None else self.workspace_manager
            )
            if (
                clean_workspace
                and current_status.state != ProjectState.DELIVERED
                and target_state != ProjectState.DELIVERED
                and effective_ws_mgr is not None
            ):
                workspace_cleaned = effective_ws_mgr.remove_workspace(project_id)
                if workspace_cleaned:
                    logger.info(
                        "Cleaned partial workspace before retrying non-delivered project %r",
                        project_id,
                    )

            self.store.append(
                create_event(
                    project_id=project_id,
                    event_type=EventType.PROJECT_RETRIED,
                    payload={
                        "from": current_status.state.value,
                        "to": target_state.value,
                        "failed_from": last_active_state.value,
                        "workspace_cleaned": workspace_cleaned,
                    },
                    at=self._now(),
                )
            )
            if regenerate_interview and target_state == ProjectState.INTERVIEW and (
                current_status.state == ProjectState.INTERVIEW or not had_active_questions
            ):
                retry_prompt = self._resolve_interview_retry_prompt(events, current_status)
                return self.handle_interview_message(
                    project_id,
                    f"retry-int-{uuid.uuid4().hex[:12]}",
                    retry_prompt,
                )
            return self.get_status(project_id)

    def delete_project(self, project_id: str) -> bool:
        """Delete project events and cached orchestrator state from the local store."""
        validate_project_id(project_id)
        with self._project_lock(project_id):
            with self._lock:
                self._specs.pop(project_id, None)
                self._last_published_status.pop(project_id, None)
            self.circuit_breaker.reset(project_id)
            return self.store.delete_project(project_id)

    def _resolve_llm_model_hint(self, stage: str) -> str | None:
        """Resolve configured/discovered model hint when LLM exposes a ModelResolver."""
        cfg_fn = getattr(self.llm, "_configured_model_for_stage", None)
        resolver = getattr(self.llm, "model_resolver", None)
        if not callable(cfg_fn) or resolver is None:
            return None
        configured = cfg_fn(stage)
        return resolver.resolve_model_for_role(stage, configured)

    def record_state_call(
        self,
        project_id: str,
        state: ProjectState,
        *,
        model: str | None = None,
        agent: str | None = None,
        duration_seconds: float | None = None,
        round_num: int | None = None,
        max_rounds: int | None = None,
        files_written: Sequence[str] | None = None,
        summary_short: str | None = None,
        failing_tests: Sequence[str] | None = None,
        outcome: str | None = None,
        extra_secrets: Sequence[str] = (),
    ) -> ProjectStatus:
        """Record a call in the given state or transition to FAILED if budget exceeded (INV-17)."""
        validate_project_id(project_id)
        with self._project_lock(project_id):
            events = self.store.get_events(project_id)
            if not events:
                raise ValueError(f"Project '{project_id}' does not exist")
            current_status = self.get_status(project_id)
            if current_status.state == ProjectState.FAILED:
                raise IllegalTransition(ProjectState.FAILED, state)
            try:
                self.circuit_breaker.record_call(project_id, state)
                payload: dict[str, Any] = {"state": state.value}
                if agent:
                    payload["agent"] = agent
                if model:
                    payload["model"] = redact_secrets(str(model), extra_secrets=extra_secrets)
                if duration_seconds is not None:
                    payload["duration_seconds"] = round(max(0.0, float(duration_seconds)), 3)
                if round_num is not None:
                    payload["round"] = int(round_num)
                if max_rounds is not None:
                    payload["max_rounds"] = int(max_rounds)
                if files_written is not None:
                    cleaned_files: list[str] = []
                    for item in list(files_written)[:10]:
                        norm_item = redact_secrets(
                            str(item).strip().replace("\\", "/"),
                            extra_secrets=extra_secrets,
                        )[:120]
                        if norm_item and norm_item not in cleaned_files:
                            cleaned_files.append(norm_item)
                    payload["files_written"] = cleaned_files
                if summary_short is not None:
                    collapsed = " ".join(str(summary_short).split())
                    redacted_sum = redact_secrets(collapsed, extra_secrets=extra_secrets)
                    if len(redacted_sum) > 140:
                        redacted_sum = redacted_sum[:137].rstrip() + "..."
                    if redacted_sum:
                        payload["summary_short"] = redacted_sum
                if failing_tests is not None:
                    payload["failing_tests"] = [
                        redact_secrets(str(t).strip(), extra_secrets=extra_secrets)[:40]
                        for t in list(failing_tests)[:10]
                        if str(t).strip()
                    ]
                if outcome:
                    payload["outcome"] = str(outcome)
                self.store.append(
                    create_event(
                        project_id=project_id,
                        event_type=EventType.AGENT_INVOKED,
                        payload=payload,
                        at=self._now(),
                    )
                )
            except CircuitBreakerTrippedError as err:
                return self.record_phase_failure(
                    project_id,
                    failed_state=current_status.state,
                    error=err,
                    agent="circuit_breaker",
                )
            return self.get_status(project_id)

    @staticmethod
    def _status_publish_key(status: ProjectStatus) -> tuple[Any, ...]:
        return (
            status.project_id,
            status.state,
            status.progress_pct,
            status.plan_hash,
            status.failure_reason,
            status.links.repo if status.links else "",
            status.links.deploy if status.links else "",
        )

    def mark_status_published(self, status: ProjectStatus) -> None:
        """Record status as already published so get_status does not republish redundantly."""
        with self._lock:
            self._last_published_status[status.project_id] = self._status_publish_key(status)

    def get_status(self, project_id: str, *, notify_publisher: bool = True) -> ProjectStatus:
        """Project current ProjectStatus from the append-only event log (INV-11)."""
        validate_project_id(project_id)
        events = self.store.get_events(project_id)
        status = project_status_from_events(project_id, events)
        if notify_publisher and self.publisher is not None:
            last_key = self._status_publish_key(status)
            should_publish = False
            with self._lock:
                if self._last_published_status.get(project_id) != last_key:
                    self._last_published_status[project_id] = last_key
                    should_publish = True
            if should_publish:
                try:
                    self.publisher.publish(status)
                except (RegistryPublishError, OSError, RuntimeError, ValueError):
                    pass
        return status

    def get_plan(self, project_id: str) -> Plan | None:
        """Retrieve the latest validated Plan for project_id from the event log."""
        validate_project_id(project_id)
        for event in reversed(self.store.get_events(project_id)):
            if event.type == EventType.PLAN_CREATED and "plan" in event.payload:
                return Plan.model_validate(event.payload["plan"])
        return None

    def get_project_detail(self, project_id: str) -> dict[str, Any]:
        """Return status, Spec, Plan, cleaned active interview questions, and events for project_id."""
        validate_project_id(project_id)
        events = self.store.get_events(project_id)
        if not events:
            raise ValueError(f"Project '{project_id}' does not exist")

        status = self.get_status(project_id)
        spec = self.get_spec(project_id)
        plan = self.get_plan(project_id)
        raw_questions = (
            self._extract_active_interview_questions(events)
            if status.state == ProjectState.INTERVIEW
            else []
        )
        questions = [q.strip() for q in raw_questions if isinstance(q, str) and q.strip()]
        return {
            "project_id": project_id,
            "status": status,
            "spec": spec,
            "plan": plan,
            "questions": questions,
            "events": list(events),
        }
