"""Visual QA / Synthetic User Audit agent for Agent Factory dashboard.

Navigates key non-destructive views across desktop (1440x900) and mobile (390x844)
viewports using headless Playwright, captures JS console/page errors and HTTP >= 400
responses for error_triage, runs in-page geometric layout checks (horizontal overflow
and visible overlaps), takes temporary PNG screenshots, and invokes agy.EXE in strict
read-only mode to persist up to 2 deduplicated UX improvement ideas (source='visual_qa').
"""

from __future__ import annotations

import logging
import re
import tempfile
import urllib.parse
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from agent_factory import idea_engine
from agent_factory.adapters.agy_cli_adapter import build_sanitized_cli_env
from agent_factory.adapters.subprocess_runner import SubprocessRunner
from agent_factory.error_triage import report_factory_error
from agent_factory.ports.runner import CommandTimeoutError, Runner
from agent_factory.secrets import redact_secrets

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL: str = "http://127.0.0.1:8000"
DESKTOP_VIEWPORT: dict[str, int] = {"width": 1440, "height": 900}
MOBILE_VIEWPORT: dict[str, int] = {"width": 390, "height": 844}
AUDIT_VIEWPORTS: tuple[tuple[str, dict[str, int]], ...] = (
    ("desktop", DESKTOP_VIEWPORT),
    ("mobile", MOBILE_VIEWPORT),
)
DEFAULT_MAX_UX_IDEAS: int = 2
DEFAULT_AGY_TIMEOUT_SECONDS: float = 180.0
DEFAULT_NAV_TIMEOUT_MS: int = 15000
DEFAULT_STEP_TIMEOUT_MS: int = 3000
DEFAULT_SETTLE_WAIT_MS: int = 250
DEFAULT_SOURCE_FILE: str = "src/agent_factory/dashboard.py"
_MAX_INLINE_PROMPT_CHARS: int = 6000

_GEOMETRY_AUDIT_JS: str = """
() => {
  const docEl = document.documentElement;
  const scrollWidth = Math.round((docEl && docEl.scrollWidth) || 0);
  const clientWidth = Math.round((docEl && docEl.clientWidth) || window.innerWidth || 0);
  const overflowPx = Math.max(0, scrollWidth - clientWidth);
  const hasHorizontalOverflow = scrollWidth > clientWidth;

  const isVisible = (el) => {
    if (!el || typeof el.getBoundingClientRect !== 'function') return false;
    const style = window.getComputedStyle(el);
    if (!style || style.display === 'none' || style.visibility === 'hidden') return false;
    const opacity = parseFloat(style.opacity || '1');
    if (!Number.isNaN(opacity) && opacity <= 0.01) return false;
    const rect = el.getBoundingClientRect();
    return rect.width >= 4 && rect.height >= 4;
  };

  const describe = (el) => {
    const tag = (el.tagName || 'el').toLowerCase();
    const idPart = el.id ? '#' + el.id : '';
    const clsList = typeof el.className === 'string'
      ? el.className.trim().split(/\\s+/).filter(Boolean).slice(0, 2)
      : [];
    const clsPart = clsList.length ? '.' + clsList.join('.') : '';
    return tag + idPart + clsPart;
  };

  const modalRoot = Array.from(document.querySelectorAll('.modal-backdrop')).find(isVisible) || null;
  const scopeRoot = modalRoot || document;
  const selector = 'button, a, input, select, textarea, .panel, .modal-card, header .controls > *';
  const candidates = Array.from(scopeRoot.querySelectorAll(selector))
    .filter(isVisible)
    .slice(0, 80);

  const rects = candidates.map((el) => ({
    el,
    label: describe(el),
    rect: el.getBoundingClientRect(),
  }));

  const overlaps = [];
  for (let i = 0; i < rects.length && overlaps.length < 20; i += 1) {
    for (let j = i + 1; j < rects.length && overlaps.length < 20; j += 1) {
      const a = rects[i];
      const b = rects[j];
      if (a.el.contains(b.el) || b.el.contains(a.el)) continue;
      const ox = Math.min(a.rect.right, b.rect.right) - Math.max(a.rect.left, b.rect.left);
      const oy = Math.min(a.rect.bottom, b.rect.bottom) - Math.max(a.rect.top, b.rect.top);
      if (ox >= 6 && oy >= 6 && (ox * oy) >= 64) {
        overlaps.push({
          a: a.label,
          b: b.label,
          overlap_width: Math.round(ox),
          overlap_height: Math.round(oy),
        });
      }
    }
  }

  return {
    scroll_width: scrollWidth,
    client_width: clientWidth,
    overflow_px: overflowPx,
    has_horizontal_overflow: hasHorizontalOverflow,
    overlaps,
    overlap_count: overlaps.length,
  };
}
"""


def _import_sync_playwright() -> Callable[[], Any]:
    """Import and return playwright.sync_api.sync_playwright (isolated for clean fallback & testing)."""
    from playwright.sync_api import sync_playwright

    return sync_playwright


def _validate_base_url(base_url: str) -> str:
    """Validate that base_url uses http or https with a non-empty host (INV-22)."""
    clean = str(base_url or "").strip()
    parsed = urllib.parse.urlparse(clean)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError(
            f"base_url inválida {base_url!r}; debe comenzar con http:// o https://."
        )
    return clean


def _read_attr_or_call(obj: Any, attr: str, default: Any = "") -> Any:
    val = getattr(obj, attr, default)
    if callable(val):
        try:
            return val()
        except Exception:  # noqa: BLE001
            return default
    return val


def _empty_result(
    *,
    ok: bool,
    skipped: bool,
    reason: str,
    detail: str,
    base_url: str,
) -> dict[str, Any]:
    return {
        "ok": ok,
        "skipped": skipped,
        "reason": reason,
        "detail": redact_secrets(detail),
        "base_url": redact_secrets(base_url),
        "viewports_audited": [],
        "states_visited": [],
        "screenshots_captured": 0,
        "console_errors": [],
        "page_errors": [],
        "http_errors": [],
        "triage_reports": [],
        "geometry_checks": [],
        "ideas_created": [],
        "agy_invoked": False,
        "agy_error": None,
    }


def _safe_is_visible(locator: Any, timeout_ms: int) -> bool:
    try:
        if hasattr(locator, "count") and int(locator.count()) <= 0:
            return False
        target = getattr(locator, "first", locator)
        return bool(target.is_visible(timeout=timeout_ms))
    except Exception:  # noqa: BLE001
        return False


def _safe_click(page: Any, selector: str, timeout_ms: int, settle_ms: int) -> bool:
    """Click a safe, allow-listed UI element only when visible; never raises."""
    try:
        loc = page.locator(selector)
        if not _safe_is_visible(loc, timeout_ms):
            return False
        target = getattr(loc, "first", loc)
        target.click(timeout=timeout_ms)
        if settle_ms > 0 and hasattr(page, "wait_for_timeout"):
            page.wait_for_timeout(settle_ms)
        return True
    except Exception as exc:  # noqa: BLE001
        logger.debug("Visual QA safe click skipped for %s: %s", selector, exc)
        return False


def _close_active_modal(page: Any, timeout_ms: int, settle_ms: int) -> None:
    if _safe_click(page, "#close-factory-improve-btn", timeout_ms, settle_ms):
        return
    try:
        keyboard = getattr(page, "keyboard", None)
        if keyboard is not None and hasattr(keyboard, "press"):
            keyboard.press("Escape")
            if settle_ms > 0 and hasattr(page, "wait_for_timeout"):
                page.wait_for_timeout(settle_ms)
    except Exception as exc:  # noqa: BLE001
        logger.debug("Visual QA Escape fallback failed safely: %s", exc)


def _normalize_geometry(raw: Any, viewport_name: str, state_name: str) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        return {
            "viewport": viewport_name,
            "state": state_name,
            "scroll_width": 0,
            "client_width": 0,
            "overflow_px": 0,
            "has_horizontal_overflow": False,
            "overlaps": [],
            "overlap_count": 0,
        }
    scroll_w = int(raw.get("scroll_width", 0) or 0)
    client_w = int(raw.get("client_width", 0) or 0)
    overflow_px = int(raw.get("overflow_px", max(0, scroll_w - client_w)) or 0)
    has_overflow = bool(raw.get("has_horizontal_overflow", scroll_w > client_w))
    raw_overlaps = raw.get("overlaps")
    overlaps: list[dict[str, Any]] = []
    if isinstance(raw_overlaps, Sequence) and not isinstance(raw_overlaps, (str, bytes)):
        for item in raw_overlaps[:20]:
            if isinstance(item, Mapping):
                overlaps.append(
                    {
                        "a": str(item.get("a") or ""),
                        "b": str(item.get("b") or ""),
                        "overlap_width": int(item.get("overlap_width", 0) or 0),
                        "overlap_height": int(item.get("overlap_height", 0) or 0),
                    }
                )
    return {
        "viewport": viewport_name,
        "state": state_name,
        "scroll_width": scroll_w,
        "client_width": client_w,
        "overflow_px": overflow_px,
        "has_horizontal_overflow": has_overflow,
        "overlaps": overlaps,
        "overlap_count": len(overlaps),
    }


def _capture_state(
    page: Any,
    *,
    viewport_name: str,
    state_name: str,
    step_index: int,
    screenshots_dir: Path,
    screenshot_records: list[dict[str, Any]],
    geometry_checks: list[dict[str, Any]],
    states_visited: list[str],
) -> None:
    states_visited.append(f"{viewport_name}:{state_name}")

    filename = f"{viewport_name}_{step_index:02d}_{state_name}.png"
    target = (screenshots_dir / filename).resolve()
    if target.is_relative_to(screenshots_dir.resolve()):
        try:
            page.screenshot(path=str(target), full_page=False)
            if target.exists():
                screenshot_records.append(
                    {
                        "viewport": viewport_name,
                        "state": state_name,
                        "path": str(target),
                    }
                )
        except Exception as exc:  # noqa: BLE001
            logger.debug(
                "Visual QA screenshot failed for %s/%s: %s",
                viewport_name,
                state_name,
                exc,
            )

    try:
        raw_geom = page.evaluate(_GEOMETRY_AUDIT_JS)
    except Exception as exc:  # noqa: BLE001
        logger.debug("Visual QA geometry evaluate failed for %s/%s: %s", viewport_name, state_name, exc)
        raw_geom = None
    geometry_checks.append(_normalize_geometry(raw_geom, viewport_name, state_name))


def _audit_viewport(
    browser: Any,
    *,
    base_url: str,
    viewport_name: str,
    viewport_size: dict[str, int],
    screenshots_dir: Path,
    nav_timeout_ms: int,
    step_timeout_ms: int,
    settle_wait_ms: int,
    console_errors: list[dict[str, Any]],
    page_errors: list[dict[str, Any]],
    http_errors: list[dict[str, Any]],
    screenshot_records: list[dict[str, Any]],
    geometry_checks: list[dict[str, Any]],
    states_visited: list[str],
) -> bool:
    """Run safe non-destructive navigation across key dashboard states for one viewport."""
    context = browser.new_context(viewport=dict(viewport_size))
    page = None
    current_state = ["home"]

    try:
        page = context.new_page()

        def _on_console(msg: Any) -> None:
            msg_type = str(_read_attr_or_call(msg, "type", "")).strip().lower()
            if msg_type != "error":
                return
            text = redact_secrets(str(_read_attr_or_call(msg, "text", "")).strip())
            if text:
                console_errors.append(
                    {
                        "viewport": viewport_name,
                        "state": current_state[0],
                        "type": "console.error",
                        "message": text,
                    }
                )

        def _on_pageerror(err: Any) -> None:
            text = redact_secrets(str(err or "").strip())
            if text:
                page_errors.append(
                    {
                        "viewport": viewport_name,
                        "state": current_state[0],
                        "type": "pageerror",
                        "message": text,
                    }
                )

        def _on_response(resp: Any) -> None:
            raw_status = _read_attr_or_call(resp, "status", 0)
            try:
                status_code = int(raw_status)
            except (TypeError, ValueError):
                return
            if status_code < 400:
                return
            url = redact_secrets(str(_read_attr_or_call(resp, "url", "")).strip())
            req = getattr(resp, "request", None)
            method = str(_read_attr_or_call(req, "method", "GET") if req is not None else "GET").upper()
            http_errors.append(
                {
                    "viewport": viewport_name,
                    "state": current_state[0],
                    "status": status_code,
                    "method": method,
                    "url": url,
                }
            )

        page.on("console", _on_console)
        page.on("pageerror", _on_pageerror)
        page.on("response", _on_response)

        try:
            page.goto(base_url, wait_until="domcontentloaded", timeout=nav_timeout_ms)
            if settle_wait_ms > 0 and hasattr(page, "wait_for_timeout"):
                page.wait_for_timeout(settle_wait_ms)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Visual QA navigation failed on %s (%s): %s",
                viewport_name,
                redact_secrets(base_url),
                redact_secrets(str(exc)),
            )
            return False

        step = 1
        current_state[0] = "home"
        _capture_state(
            page,
            viewport_name=viewport_name,
            state_name="home",
            step_index=step,
            screenshots_dir=screenshots_dir,
            screenshot_records=screenshot_records,
            geometry_checks=geometry_checks,
            states_visited=states_visited,
        )

        # Safe project selection (if at least one project card exists)
        project_card = page.locator(".project-card[data-id]")
        has_cards = False
        try:
            has_cards = int(project_card.count()) > 0
        except Exception:  # noqa: BLE001
            has_cards = False

        if has_cards:
            if viewport_size["width"] < 1024 and not _safe_is_visible(project_card, step_timeout_ms):
                _safe_click(page, "#mobile-project-switcher", step_timeout_ms, settle_wait_ms)
            if _safe_click(page, ".project-card[data-id]", step_timeout_ms, settle_wait_ms):
                step += 1
                current_state[0] = "project_detail"
                _capture_state(
                    page,
                    viewport_name=viewport_name,
                    state_name="project_detail",
                    step_index=step,
                    screenshots_dir=screenshots_dir,
                    screenshot_records=screenshot_records,
                    geometry_checks=geometry_checks,
                    states_visited=states_visited,
                )

        # Toggle between Desarrollador and Simple modes
        if _safe_click(page, "#view-mode-dev", step_timeout_ms, settle_wait_ms):
            step += 1
            current_state[0] = "mode_dev"
            _capture_state(
                page,
                viewport_name=viewport_name,
                state_name="mode_dev",
                step_index=step,
                screenshots_dir=screenshots_dir,
                screenshot_records=screenshot_records,
                geometry_checks=geometry_checks,
                states_visited=states_visited,
            )

        if _safe_click(page, "#view-mode-simple", step_timeout_ms, settle_wait_ms):
            step += 1
            current_state[0] = "mode_simple"
            _capture_state(
                page,
                viewport_name=viewport_name,
                state_name="mode_simple",
                step_index=step,
                screenshots_dir=screenshots_dir,
                screenshot_records=screenshot_records,
                geometry_checks=geometry_checks,
                states_visited=states_visited,
            )

        # Open and safely close Auto-Mejoras modal (never click generate/implement)
        if _safe_click(page, "#auto-ideas-btn", step_timeout_ms, settle_wait_ms):
            step += 1
            current_state[0] = "modal_auto_ideas"
            _capture_state(
                page,
                viewport_name=viewport_name,
                state_name="modal_auto_ideas",
                step_index=step,
                screenshots_dir=screenshots_dir,
                screenshot_records=screenshot_records,
                geometry_checks=geometry_checks,
                states_visited=states_visited,
            )
            _close_active_modal(page, step_timeout_ms, settle_wait_ms)

        # Open and safely close Liwi modal (never submit messages or apply actions)
        if _safe_click(page, "#improve-factory-btn", step_timeout_ms, settle_wait_ms):
            step += 1
            current_state[0] = "modal_liwi"
            _capture_state(
                page,
                viewport_name=viewport_name,
                state_name="modal_liwi",
                step_index=step,
                screenshots_dir=screenshots_dir,
                screenshot_records=screenshot_records,
                geometry_checks=geometry_checks,
                states_visited=states_visited,
            )
            _close_active_modal(page, step_timeout_ms, settle_wait_ms)

        return True
    finally:
        if page is not None:
            try:
                page.close()
            except Exception as exc:  # noqa: BLE001
                logger.debug("Visual QA page close error ignored: %s", exc)
        try:
            context.close()
        except Exception as exc:  # noqa: BLE001
            logger.debug("Visual QA context close error ignored: %s", exc)


def _report_captured_errors_to_triage(
    *,
    console_errors: Sequence[Mapping[str, Any]],
    page_errors: Sequence[Mapping[str, Any]],
    http_errors: Sequence[Mapping[str, Any]],
    db_path: Path | str | None,
    source_file: str,
) -> list[dict[str, Any]]:
    """Send every captured JS console/page error and HTTP >= 400 error to error_triage."""
    reports: list[dict[str, Any]] = []

    for err in console_errors:
        msg = (
            f"[Visual QA][{err['viewport']}/{err['state']}] JS console.error: {err['message']}"
        )
        rec = report_factory_error(
            msg,
            scope="factory",
            source_file=source_file,
            db_path=db_path,
        )
        if rec is not None:
            reports.append(rec)

    for err in page_errors:
        msg = f"[Visual QA][{err['viewport']}/{err['state']}] JS pageerror: {err['message']}"
        rec = report_factory_error(
            msg,
            scope="factory",
            source_file=source_file,
            db_path=db_path,
        )
        if rec is not None:
            reports.append(rec)

    for err in http_errors:
        msg = (
            f"[Visual QA][{err['viewport']}/{err['state']}] HTTP {err['status']} "
            f"{err['method']} {err['url']}"
        )
        rec = report_factory_error(
            msg,
            scope="factory",
            source_file=source_file,
            db_path=db_path,
        )
        if rec is not None:
            reports.append(rec)

    return reports


def _build_visual_qa_prompt(
    *,
    screenshot_records: Sequence[Mapping[str, Any]],
    geometry_checks: Sequence[Mapping[str, Any]],
    console_errors: Sequence[Mapping[str, Any]],
    page_errors: Sequence[Mapping[str, Any]],
    http_errors: Sequence[Mapping[str, Any]],
    existing_titles: Sequence[str],
    max_ideas: int,
) -> str:
    lines: list[str] = [
        "MODO SOLO LECTURA (PROHIBIDO editar, crear o borrar archivos y PROHIBIDO mutar estado).",
        "PROHIBIDO leer o modificar .env, MASTER_PROMPT.md, AGENTS.md, config/models.toml, data/ o workspaces/.",
        "Eres el Agente de QA Visual / Usuario Sintético de Agent Factory.",
        f"Analiza las capturas temporales y el reporte de auditoría y propón hasta {max_ideas} mejoras de UX/UI concretas y no repetidas.",
        "",
        "1. Capturas PNG temporales (escritorio 1440x900 y móvil 390x844):",
    ]

    if screenshot_records:
        for item in screenshot_records:
            lines.append(f"   - [{item['viewport']}/{item['state']}] {item['path']}")
    else:
        lines.append("   - (sin capturas disponibles)")

    lines.append("")
    lines.append("2. Reporte geométrico en página (scrollWidth vs clientWidth y solapamientos visibles):")
    if geometry_checks:
        for check in geometry_checks:
            status_parts: list[str] = [
                f"scrollWidth={check['scroll_width']}",
                f"clientWidth={check['client_width']}",
                f"desbordamiento_horizontal={check['has_horizontal_overflow']} (+{check['overflow_px']}px)",
                f"solapamientos={check['overlap_count']}",
            ]
            lines.append(f"   - [{check['viewport']}/{check['state']}] " + ", ".join(status_parts))
            for ov in check.get("overlaps", [])[:5]:
                lines.append(
                    f"     * Solapamiento: {ov['a']} <-> {ov['b']} "
                    f"({ov['overlap_width']}x{ov['overlap_height']}px)"
                )
    else:
        lines.append("   - (sin chequeos geométricos registrados)")

    lines.append("")
    lines.append("3. Errores de consola JS y pageerror detectados:")
    all_js = [*console_errors, *page_errors]
    if all_js:
        for err in all_js[:20]:
            lines.append(
                f"   - [{err['viewport']}/{err['state']}] {err['type']}: {err['message'][:240]}"
            )
    else:
        lines.append("   - (ningún error JS detectado)")

    lines.append("")
    lines.append("4. Fallos HTTP (status >= 400) detectados:")
    if http_errors:
        for err in http_errors[:20]:
            lines.append(
                f"   - [{err['viewport']}/{err['state']}] HTTP {err['status']} {err['method']} {err['url'][:200]}"
            )
    else:
        lines.append("   - (ningún fallo HTTP >= 400 detectado)")

    lines.append("")
    lines.append("5. Títulos de ideas ya existentes o descartadas (PROHIBIDO repetir o parafrasear):")
    if existing_titles:
        for title in existing_titles[:100]:
            lines.append(f"   - {redact_secrets(title)}")
    else:
        lines.append("   - (ninguna idea previa registrada)")

    lines.extend(
        [
            "",
            "REGLAS ESTRICTAS DE SALIDA:",
            "- Devuelve ÚNICAMENTE un JSON válido (sin texto adicional fuera del JSON).",
            f'- Propón como máximo {max_ideas} mejoras UX con este formato exacto:',
            (
                '  {"ideas": [{"title": "<emoji + nombre llamativo>", '
                '"category": "UX / Visual", "impact": "Alto" | "Medio", '
                '"simple_description": "<explicación clara en español cotidiano>", '
                '"technical_prompt": "<instrucciones técnicas TDD acotadas, indicando ejecutar únicamente pruebas dirigidas y ruff check>"}]}'
            ),
        ]
    )
    return redact_secrets("\n".join(lines))


def _analyze_with_agy_readonly(
    prompt: str,
    *,
    runner: Runner | None,
    agy_command: Sequence[str] | None,
    model: str | None,
    timeout_seconds: float,
) -> tuple[bool, list[dict[str, str]], str | None]:
    """Invoke agy.EXE in strict read-only mode and parse candidate UX ideas."""
    active_runner: Any = (
        runner
        if runner is not None
        else SubprocessRunner(default_timeout_seconds=timeout_seconds)
    )
    base_cmd, err_reason = idea_engine._resolve_agy_readonly_command(agy_command, active_runner)
    if base_cmd is None:
        return False, [], err_reason or "CLI 'agy' no disponible."

    is_batch = base_cmd[0].lower().endswith((".cmd", ".bat"))
    clean_env = build_sanitized_cli_env()
    tmp_prompt: Path | None = None

    try:
        if len(prompt) <= _MAX_INLINE_PROMPT_CHARS:
            prompt_arg = re.sub(r"[\r\n]+", " ", prompt).strip() if is_batch else prompt
        else:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                suffix=".txt",
                prefix="af_visual_qa_prompt_",
                delete=False,
            ) as tf:
                tf.write(prompt)
                tmp_prompt = Path(tf.name).resolve()
            prompt_arg = f"Lee y sigue exactamente las instrucciones del archivo: {tmp_prompt}"

        cmd = [*base_cmd, "-p", prompt_arg]
        if isinstance(model, str) and model.strip():
            cmd.extend(["--model", model.strip()])

        with tempfile.TemporaryDirectory(prefix="af_visual_qa_ro_") as ro_cwd:
            res = idea_engine._run_port_command(
                active_runner,
                cmd,
                cwd=Path(ro_cwd),
                timeout_seconds=float(timeout_seconds),
                env=clean_env,
            )
    except (CommandTimeoutError, OSError, RuntimeError, ValueError) as exc:
        return True, [], redact_secrets(str(exc) or type(exc).__name__)
    except Exception as exc:  # noqa: BLE001
        return True, [], redact_secrets(str(exc) or type(exc).__name__)
    finally:
        if tmp_prompt is not None:
            tmp_prompt.unlink(missing_ok=True)

    if res.exit_code != 0 or not res.stdout.strip():
        detail = redact_secrets((res.stderr or res.stdout or f"exit code {res.exit_code}").strip())
        return True, [], detail

    parsed = idea_engine.parse_ideas_payload(res.stdout)
    if not parsed:
        return True, [], "La salida de agy.EXE no contenía un JSON válido de ideas UX."
    return True, parsed, None


def run_synthetic_user_audit(
    base_url: str = DEFAULT_BASE_URL,
    *,
    db_path: Path | str | None = None,
    runner: Runner | None = None,
    agy_command: Sequence[str] | None = None,
    model: str | None = None,
    max_ideas: int = DEFAULT_MAX_UX_IDEAS,
    agy_timeout_seconds: float = DEFAULT_AGY_TIMEOUT_SECONDS,
    nav_timeout_ms: int = DEFAULT_NAV_TIMEOUT_MS,
    step_timeout_ms: int = DEFAULT_STEP_TIMEOUT_MS,
    settle_wait_ms: int = DEFAULT_SETTLE_WAIT_MS,
    source_file: str = DEFAULT_SOURCE_FILE,
) -> dict[str, Any]:
    """Run a non-destructive synthetic user visual QA audit across desktop and mobile viewports."""
    safe_base_url = _validate_base_url(base_url)
    bounded_max_ideas = (
        DEFAULT_MAX_UX_IDEAS
        if isinstance(max_ideas, bool) or not isinstance(max_ideas, int)
        else max(0, min(max_ideas, DEFAULT_MAX_UX_IDEAS))
    )

    try:
        sync_playwright_fn = _import_sync_playwright()
    except ImportError as exc:
        logger.info("Visual QA audit skipped (Playwright unavailable): %s", exc)
        return _empty_result(
            ok=False,
            skipped=True,
            reason="playwright_unavailable",
            detail="Playwright no está instalado (ImportError). Instala playwright para habilitar la auditoría visual.",
            base_url=safe_base_url,
        )

    console_errors: list[dict[str, Any]] = []
    page_errors: list[dict[str, Any]] = []
    http_errors: list[dict[str, Any]] = []
    screenshot_records: list[dict[str, Any]] = []
    geometry_checks: list[dict[str, Any]] = []
    states_visited: list[str] = []
    viewports_audited: list[str] = []

    with tempfile.TemporaryDirectory(prefix="af_visual_qa_shots_") as tmp_dir_str:
        screenshots_dir = Path(tmp_dir_str).resolve()

        try:
            with sync_playwright_fn() as playwright:
                try:
                    browser = playwright.chromium.launch(headless=True)
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "Visual QA browser launch failed safely: %s",
                        redact_secrets(str(exc)),
                    )
                    return _empty_result(
                        ok=False,
                        skipped=True,
                        reason="browser_launch_failed",
                        detail=f"No se pudo lanzar Chromium headless: {exc}",
                        base_url=safe_base_url,
                    )

                try:
                    for viewport_name, viewport_size in AUDIT_VIEWPORTS:
                        audited = _audit_viewport(
                            browser,
                            base_url=safe_base_url,
                            viewport_name=viewport_name,
                            viewport_size=viewport_size,
                            screenshots_dir=screenshots_dir,
                            nav_timeout_ms=nav_timeout_ms,
                            step_timeout_ms=step_timeout_ms,
                            settle_wait_ms=settle_wait_ms,
                            console_errors=console_errors,
                            page_errors=page_errors,
                            http_errors=http_errors,
                            screenshot_records=screenshot_records,
                            geometry_checks=geometry_checks,
                            states_visited=states_visited,
                        )
                        if audited:
                            viewports_audited.append(viewport_name)
                finally:
                    try:
                        browser.close()
                    except Exception as exc:  # noqa: BLE001
                        logger.debug("Visual QA browser close error ignored: %s", exc)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Visual QA Playwright session error: %s", redact_secrets(str(exc)))
            return _empty_result(
                ok=False,
                skipped=True,
                reason="playwright_session_failed",
                detail=f"Error ejecutando la sesión de Playwright: {exc}",
                base_url=safe_base_url,
            )

        triage_reports = _report_captured_errors_to_triage(
            console_errors=console_errors,
            page_errors=page_errors,
            http_errors=http_errors,
            db_path=db_path,
            source_file=source_file,
        )

        ideas_created: list[dict[str, Any]] = []
        agy_invoked = False
        agy_error: str | None = None

        if bounded_max_ideas > 0 and viewports_audited:
            existing_titles = idea_engine.list_all_idea_titles(db_path=db_path)
            prompt = _build_visual_qa_prompt(
                screenshot_records=screenshot_records,
                geometry_checks=geometry_checks,
                console_errors=console_errors,
                page_errors=page_errors,
                http_errors=http_errors,
                existing_titles=existing_titles,
                max_ideas=bounded_max_ideas,
            )
            agy_invoked, parsed_ideas, agy_error = _analyze_with_agy_readonly(
                prompt,
                runner=runner,
                agy_command=agy_command,
                model=model,
                timeout_seconds=agy_timeout_seconds,
            )
            if parsed_ideas:
                ideas_created = idea_engine.save_visual_qa_ideas(
                    parsed_ideas,
                    db_path=db_path,
                    max_items=bounded_max_ideas,
                )

    audit_ok = bool(viewports_audited)
    return {
        "ok": audit_ok,
        "skipped": False,
        "reason": "completed" if audit_ok else "navigation_failed",
        "detail": (
            f"Auditoría visual completada en {len(viewports_audited)} viewport(s)."
            if audit_ok
            else "No se pudo cargar la URL del dashboard en ningún viewport."
        ),
        "base_url": redact_secrets(safe_base_url),
        "viewports_audited": viewports_audited,
        "states_visited": states_visited,
        "screenshots_captured": len(screenshot_records),
        "console_errors": console_errors,
        "page_errors": page_errors,
        "http_errors": http_errors,
        "triage_reports": triage_reports,
        "geometry_checks": geometry_checks,
        "ideas_created": ideas_created,
        "agy_invoked": agy_invoked,
        "agy_error": agy_error,
    }
