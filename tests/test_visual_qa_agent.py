"""Unit tests for the Visual QA / Synthetic User Audit agent (visual_qa_agent.py)."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from types import TracebackType
from typing import Any, Self

import pytest

from agent_factory import idea_engine
from agent_factory.ports.runner import CommandResult, CommandTimeoutError


class FakeRunner:
    """Deterministic Runner test double recording commands and returning canned responses."""

    def __init__(self, responses: Sequence[CommandResult | Exception]) -> None:
        self._responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    def run(
        self,
        command: Sequence[str],
        cwd: Path,
        timeout_seconds: float = 30.0,
        env: Mapping[str, str] | None = None,
        input: str | bytes | None = None,
    ) -> CommandResult:
        self.calls.append(
            {
                "command": list(command),
                "cwd": Path(cwd),
                "timeout_seconds": timeout_seconds,
                "env": dict(env) if env is not None else None,
                "input": input,
            }
        )
        if not self._responses:
            raise AssertionError("FakeRunner received more calls than configured responses")
        item = self._responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class FakeConsoleMessage:
    def __init__(self, msg_type: str, text: str) -> None:
        self.type = msg_type
        self.text = text


class FakeRequest:
    def __init__(self, method: str = "GET") -> None:
        self.method = method


class FakeResponse:
    def __init__(self, url: str, status: int, method: str = "GET") -> None:
        self.url = url
        self.status = status
        self.request = FakeRequest(method=method)


class FakeLocator:
    def __init__(
        self,
        page: FakePage,
        selector: str,
        *,
        visible: bool = True,
        count: int = 1,
    ) -> None:
        self._page = page
        self._selector = selector
        self._visible = visible
        self._count = count

    @property
    def first(self) -> FakeLocator:
        return self

    def count(self) -> int:
        return self._count

    def is_visible(self, timeout: int | None = None) -> bool:
        _ = timeout
        return self._visible and self._count > 0

    def click(self, timeout: int | None = None) -> None:
        _ = timeout
        self._page.clicks.append(self._selector)
        if self._selector == "#auto-ideas-btn":
            self._page.active_modal = "auto_ideas"
        elif self._selector == "#improve-factory-btn":
            self._page.active_modal = "liwi"
        elif self._selector == "#close-factory-improve-btn":
            self._page.active_modal = None
        elif self._selector == "#mobile-project-switcher":
            self._page.mobile_sidebar_open = True


class FakeKeyboard:
    def __init__(self, page: FakePage) -> None:
        self._page = page

    def press(self, key: str) -> None:
        self._page.keys_pressed.append(key)
        if key == "Escape":
            self._page.active_modal = None


class FakePage:
    def __init__(
        self,
        viewport: dict[str, int],
        *,
        has_project: bool = True,
        emit_events_on_goto: Callable[[FakePage], None] | None = None,
        geometry_by_viewport: Mapping[str, dict[str, Any]] | None = None,
    ) -> None:
        self.viewport = dict(viewport)
        self.has_project = has_project
        self.emit_events_on_goto = emit_events_on_goto
        self.geometry_by_viewport = dict(geometry_by_viewport or {})
        self.handlers: dict[str, list[Callable[..., Any]]] = {}
        self.clicks: list[str] = []
        self.keys_pressed: list[str] = []
        self.screenshots: list[Path] = []
        self.visited_urls: list[str] = []
        self.evaluated_scripts: list[str] = []
        self.active_modal: str | None = None
        self.mobile_sidebar_open: bool = False
        self.closed: bool = False
        self.keyboard = FakeKeyboard(self)

    def on(self, event_name: str, handler: Callable[..., Any]) -> None:
        self.handlers.setdefault(event_name, []).append(handler)

    def emit(self, event_name: str, payload: Any) -> None:
        for handler in self.handlers.get(event_name, []):
            handler(payload)

    def goto(self, url: str, wait_until: str = "domcontentloaded", timeout: int = 15000) -> None:
        _ = (wait_until, timeout)
        self.visited_urls.append(url)
        if self.emit_events_on_goto is not None:
            self.emit_events_on_goto(self)

    def wait_for_timeout(self, ms: int) -> None:
        _ = ms

    def locator(self, selector: str) -> FakeLocator:
        if selector == ".project-card[data-id]":
            if not self.has_project:
                return FakeLocator(self, selector, visible=False, count=0)
            is_mobile = self.viewport.get("width", 1440) < 1024
            visible = (not is_mobile) or self.mobile_sidebar_open
            return FakeLocator(self, selector, visible=visible, count=1)
        if selector == "#mobile-project-switcher":
            is_mobile = self.viewport.get("width", 1440) < 1024
            return FakeLocator(self, selector, visible=is_mobile, count=1 if is_mobile else 0)
        if selector == "#close-factory-improve-btn":
            return FakeLocator(self, selector, visible=self.active_modal is not None, count=1)
        return FakeLocator(self, selector, visible=True, count=1)

    def screenshot(self, path: str, full_page: bool = False) -> bytes:
        _ = full_page
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        # Write valid minimal PNG header bytes
        png_bytes = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"
        target.write_bytes(png_bytes)
        self.screenshots.append(target)
        return png_bytes

    def evaluate(self, script: str) -> dict[str, Any]:
        self.evaluated_scripts.append(script)
        vp_key = "mobile" if self.viewport.get("width", 1440) < 1024 else "desktop"
        if vp_key in self.geometry_by_viewport:
            return dict(self.geometry_by_viewport[vp_key])
        return {
            "scroll_width": self.viewport["width"],
            "client_width": self.viewport["width"],
            "overflow_px": 0,
            "has_horizontal_overflow": False,
            "overlaps": [],
            "overlap_count": 0,
        }

    def close(self) -> None:
        self.closed = True


class FakeBrowserContext:
    def __init__(
        self,
        viewport: dict[str, int],
        *,
        has_project: bool = True,
        emit_events_on_goto: Callable[[FakePage], None] | None = None,
        geometry_by_viewport: Mapping[str, dict[str, Any]] | None = None,
    ) -> None:
        self.viewport = dict(viewport)
        self.has_project = has_project
        self.emit_events_on_goto = emit_events_on_goto
        self.geometry_by_viewport = geometry_by_viewport
        self.pages: list[FakePage] = []
        self.closed: bool = False

    def new_page(self) -> FakePage:
        page = FakePage(
            self.viewport,
            has_project=self.has_project,
            emit_events_on_goto=self.emit_events_on_goto,
            geometry_by_viewport=self.geometry_by_viewport,
        )
        self.pages.append(page)
        return page

    def close(self) -> None:
        self.closed = True


class FakeBrowser:
    def __init__(
        self,
        *,
        has_project: bool = True,
        emit_events_on_goto: Callable[[FakePage], None] | None = None,
        geometry_by_viewport: Mapping[str, dict[str, Any]] | None = None,
    ) -> None:
        self.has_project = has_project
        self.emit_events_on_goto = emit_events_on_goto
        self.geometry_by_viewport = geometry_by_viewport
        self.contexts: list[FakeBrowserContext] = []
        self.closed: bool = False

    def new_context(self, viewport: dict[str, int] | None = None) -> FakeBrowserContext:
        ctx = FakeBrowserContext(
            viewport or {"width": 1440, "height": 900},
            has_project=self.has_project,
            emit_events_on_goto=self.emit_events_on_goto,
            geometry_by_viewport=self.geometry_by_viewport,
        )
        self.contexts.append(ctx)
        return ctx

    def close(self) -> None:
        self.closed = True


class FakeChromium:
    def __init__(
        self,
        browser: FakeBrowser | None = None,
        *,
        launch_error: Exception | None = None,
    ) -> None:
        self.browser = browser or FakeBrowser()
        self.launch_error = launch_error
        self.launch_calls: list[dict[str, Any]] = []

    def launch(self, *, headless: bool = True, **kwargs: Any) -> FakeBrowser:
        self.launch_calls.append({"headless": headless, **kwargs})
        if self.launch_error is not None:
            raise self.launch_error
        return self.browser


class FakePlaywrightManager:
    def __init__(self, chromium: FakeChromium) -> None:
        self.chromium = chromium
        self.entered = False
        self.exited = False

    def __enter__(self) -> Self:
        self.entered = True
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.exited = True


def _extract_prompt_text_from_agy_cmd(cmd: Sequence[str]) -> str:
    idx = list(cmd).index("-p")
    return str(cmd[idx + 1])


def test_graceful_fallback_when_playwright_is_missing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from agent_factory import visual_qa_agent

    def _raise_import_error() -> Any:
        raise ImportError("No module named 'playwright'")

    monkeypatch.setattr(visual_qa_agent, "_import_sync_playwright", _raise_import_error)

    result = visual_qa_agent.run_synthetic_user_audit(
        base_url="http://127.0.0.1:8000",
        db_path=tmp_path / "telemetry.db",
    )

    assert result["ok"] is False
    assert result["skipped"] is True
    assert result["reason"] == "playwright_unavailable"
    assert "playwright" in result["detail"].lower()
    assert result["ideas_created"] == []
    assert result["console_errors"] == []
    assert result["http_errors"] == []


def test_graceful_fallback_when_browser_launch_fails(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from agent_factory import visual_qa_agent

    chromium = FakeChromium(launch_error=RuntimeError("Chromium executable missing"))
    monkeypatch.setattr(
        visual_qa_agent,
        "_import_sync_playwright",
        lambda: (lambda: FakePlaywrightManager(chromium)),
    )

    result = visual_qa_agent.run_synthetic_user_audit(
        base_url="http://127.0.0.1:8000",
        db_path=tmp_path / "telemetry.db",
    )

    assert result["ok"] is False
    assert result["skipped"] is True
    assert result["reason"] == "browser_launch_failed"
    assert "chromium executable missing" in result["detail"].lower()
    assert chromium.launch_calls == [{"headless": True}]


def test_full_audit_both_viewports_triage_geometry_agy_dedup_and_cleanup(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from agent_factory import visual_qa_agent

    db_path = tmp_path / "telemetry.db"
    # Pre-seed an existing dismissed idea to test deduplication
    seeded = idea_engine.save_ideas(
        [
            {
                "title": "📱 Ajustar Barra Superior Móvil",
                "category": "UX / Visual",
                "impact": "Alto",
                "simple_description": "Evita que los botones se desborden en móvil.",
                "technical_prompt": "Ajusta flex-wrap en header y corre pytest -q --no-cov tests/test_dashboard.py.",
            }
        ],
        source="manual",
        db_path=db_path,
    )
    idea_engine.dismiss_idea(seeded[0]["id"], db_path=db_path)

    def _emit_errors(page: FakePage) -> None:
        if page.viewport["width"] == 1440:
            page.emit("console", FakeConsoleMessage("error", "Uncaught TypeError: Cannot read properties of null"))
            page.emit("console", FakeConsoleMessage("log", "Normal diagnostic log"))
            page.emit("response", FakeResponse("http://127.0.0.1:8000/api/projects", 200))
            page.emit("response", FakeResponse("http://127.0.0.1:8000/api/missing-widget", 404))
        else:
            page.emit("pageerror", RuntimeError("Mobile layout reference error"))
            page.emit("response", FakeResponse("http://127.0.0.1:8000/api/health-check", 500, method="POST"))

    geometry_fixtures = {
        "desktop": {
            "scroll_width": 1440,
            "client_width": 1440,
            "overflow_px": 0,
            "has_horizontal_overflow": False,
            "overlaps": [],
            "overlap_count": 0,
        },
        "mobile": {
            "scroll_width": 442,
            "client_width": 390,
            "overflow_px": 52,
            "has_horizontal_overflow": True,
            "overlaps": [
                {
                    "a": "button#auto-ideas-btn",
                    "b": "button#improve-factory-btn",
                    "overlap_width": 18,
                    "overlap_height": 24,
                }
            ],
            "overlap_count": 1,
        },
    }

    fake_browser = FakeBrowser(
        has_project=True,
        emit_events_on_goto=_emit_errors,
        geometry_by_viewport=geometry_fixtures,
    )
    chromium = FakeChromium(browser=fake_browser)
    monkeypatch.setattr(
        visual_qa_agent,
        "_import_sync_playwright",
        lambda: (lambda: FakePlaywrightManager(chromium)),
    )

    triage_calls: list[dict[str, Any]] = []

    def _fake_report_factory_error(
        error_text: str,
        scope: str = "factory",
        source_file: str | None = None,
        *,
        db_path: Path | str | None = None,
    ) -> dict[str, Any]:
        triage_calls.append(
            {
                "error_text": error_text,
                "scope": scope,
                "source_file": source_file,
                "db_path": db_path,
            }
        )
        return {"id": len(triage_calls), "scope": scope, "raw_error": error_text}

    monkeypatch.setattr(visual_qa_agent, "report_factory_error", _fake_report_factory_error)

    # agy returns 4 ideas: 1 duplicate of dismissed, 3 new -> only 2 new should be saved!
    agy_payload = {
        "ideas": [
            {
                "title": "ajustar barra superior movil",
                "category": "UX / Visual",
                "impact": "Alto",
                "simple_description": "Duplicado de idea ya descartada.",
                "technical_prompt": "No debe insertarse.",
            },
            {
                "title": "📐 Corregir Desbordamiento Horizontal en Móvil",
                "category": "UX / Responsive",
                "impact": "Alto",
                "simple_description": "Alinea los botones del encabezado para que quepan en pantallas de 390px.",
                "technical_prompt": "Ajusta media query <=480px en dashboard.py y corre pytest -q --no-cov tests/test_dashboard.py.",
            },
            {
                "title": "🎯 Separar Botones Solapados del Encabezado",
                "category": "UX / Visual",
                "impact": "Medio",
                "simple_description": "Añade espacio entre Auto-Mejoras y Mejorar Fábrica para evitar clics accidentales.",
                "technical_prompt": "Añade gap en .controls de dashboard.py y corre pytest -q --no-cov tests/test_dashboard.py.",
            },
            {
                "title": "✨ Tercera Idea Excedente",
                "category": "UX / Visual",
                "impact": "Medio",
                "simple_description": "No debe insertarse por el límite de 2 ideas.",
                "technical_prompt": "Límite máximo de 2 ideas.",
            },
        ]
    }
    agy_runner = FakeRunner([CommandResult(exit_code=0, stdout=json.dumps(agy_payload))])

    # Observe screenshots while agy runs so we can confirm they existed during agy call and are deleted afterward
    screenshots_seen_during_agy: list[Path] = []
    orig_run = agy_runner.run

    def _inspecting_run(*args: Any, **kwargs: Any) -> CommandResult:
        for ctx in fake_browser.contexts:
            for p in ctx.pages:
                for shot in p.screenshots:
                    assert shot.exists(), f"Screenshot {shot} should exist while agy runs"
                    screenshots_seen_during_agy.append(shot)
        return orig_run(*args, **kwargs)

    agy_runner.run = _inspecting_run  # type: ignore[method-assign]

    result = visual_qa_agent.run_synthetic_user_audit(
        base_url="http://127.0.0.1:8000",
        db_path=db_path,
        runner=agy_runner,
    )

    assert result["ok"] is True
    assert result["skipped"] is False
    assert chromium.launch_calls == [{"headless": True}]
    assert fake_browser.closed is True

    # Verify both desktop (1440x900) and mobile (390x844) viewports were audited
    viewports_used = [ctx.viewport for ctx in fake_browser.contexts]
    assert viewports_used == [
        {"width": 1440, "height": 900},
        {"width": 390, "height": 844},
    ]
    assert result["viewports_audited"] == ["desktop", "mobile"]

    # Verify safe interactions in both viewports (no destructive/build clicks)
    desktop_page = fake_browser.contexts[0].pages[0]
    mobile_page = fake_browser.contexts[1].pages[0]
    for p in (desktop_page, mobile_page):
        assert ".project-card[data-id]" in p.clicks
        assert "#view-mode-dev" in p.clicks
        assert "#view-mode-simple" in p.clicks
        assert "#auto-ideas-btn" in p.clicks
        assert "#improve-factory-btn" in p.clicks
        assert p.clicks.count("#close-factory-improve-btn") == 2
        assert "#liwi-radar-generate-btn" not in p.clicks
        assert "#factory-chat-send-btn" not in p.clicks
        assert "#submit-new-project-btn" not in p.clicks
        assert p.closed is True
    assert "#mobile-project-switcher" in mobile_page.clicks

    # Verify JS and HTTP errors were captured and reported to error_triage with scope="factory"
    assert len(result["console_errors"]) == 1
    assert len(result["page_errors"]) == 1
    assert len(result["http_errors"]) == 2
    assert len(triage_calls) == 4
    assert all(call["scope"] == "factory" for call in triage_calls)
    assert all(call["db_path"] == db_path for call in triage_calls)
    combined_triage = " | ".join(call["error_text"] for call in triage_calls)
    assert "Uncaught TypeError" in combined_triage
    assert "Mobile layout reference error" in combined_triage
    assert "404" in combined_triage
    assert "500" in combined_triage

    # Verify agy.EXE was invoked in read-only mode with screenshots & geometric report in prompt
    assert len(agy_runner.calls) == 1
    invoked_cmd = agy_runner.calls[0]["command"]
    assert Path(invoked_cmd[0]).stem.lower() == "agy"
    assert "-p" in invoked_cmd
    assert "--dangerously-skip-permissions" not in invoked_cmd
    assert "--mode" not in invoked_cmd

    prompt_arg = _extract_prompt_text_from_agy_cmd(invoked_cmd)
    assert "MODO SOLO LECTURA" in prompt_arg
    assert "scrollWidth" in prompt_arg or "desbordamiento" in prompt_arg.lower()
    assert "442" in prompt_arg
    assert "390" in prompt_arg
    assert "button#auto-ideas-btn" in prompt_arg
    assert "button#improve-factory-btn" in prompt_arg
    assert ".png" in prompt_arg

    # Verify up to 2 deduplicated ideas were saved with source="visual_qa"
    assert len(result["ideas_created"]) == 2
    assert [i["title"] for i in result["ideas_created"]] == [
        "📐 Corregir Desbordamiento Horizontal en Móvil",
        "🎯 Separar Botones Solapados del Encabezado",
    ]
    assert all(i["source"] == "visual_qa" for i in result["ideas_created"])
    assert all(i["status"] == "nueva" for i in result["ideas_created"])

    persisted = idea_engine.list_ideas(statuses=("nueva",), db_path=db_path)
    assert len(persisted) == 2
    assert {i["source"] for i in persisted} == {"visual_qa"}

    # Verify temporary PNGs existed during agy run AND were cleaned up afterward
    assert len(screenshots_seen_during_agy) >= 12
    for shot in screenshots_seen_during_agy:
        assert not shot.exists(), f"Temporary screenshot {shot} was not cleaned up"
        assert not shot.parent.exists(), f"Temporary directory {shot.parent} was not cleaned up"


def test_agy_failure_or_invalid_json_falls_back_gracefully_and_cleans_temp_pngs(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from agent_factory import visual_qa_agent

    db_path = tmp_path / "telemetry.db"
    fake_browser = FakeBrowser(has_project=False)
    chromium = FakeChromium(browser=fake_browser)
    monkeypatch.setattr(
        visual_qa_agent,
        "_import_sync_playwright",
        lambda: (lambda: FakePlaywrightManager(chromium)),
    )

    bad_json_runner = FakeRunner([CommandResult(exit_code=0, stdout="No JSON here")])
    res_bad_json = visual_qa_agent.run_synthetic_user_audit(
        base_url="http://127.0.0.1:8000",
        db_path=db_path,
        runner=bad_json_runner,
    )
    assert res_bad_json["ok"] is True
    assert res_bad_json["agy_invoked"] is True
    assert res_bad_json["ideas_created"] == []
    assert res_bad_json["agy_error"] is not None

    # All screenshots cleaned even on invalid JSON
    for ctx in fake_browser.contexts:
        for p in ctx.pages:
            for shot in p.screenshots:
                assert not shot.exists()

    # Timeout fallback
    fake_browser_2 = FakeBrowser(has_project=False)
    chromium_2 = FakeChromium(browser=fake_browser_2)
    monkeypatch.setattr(
        visual_qa_agent,
        "_import_sync_playwright",
        lambda: (lambda: FakePlaywrightManager(chromium_2)),
    )
    timeout_runner = FakeRunner([CommandTimeoutError("agy timed out")])
    res_timeout = visual_qa_agent.run_synthetic_user_audit(
        base_url="http://127.0.0.1:8000",
        db_path=db_path,
        runner=timeout_runner,
    )
    assert res_timeout["ok"] is True
    assert res_timeout["ideas_created"] == []
    assert "timed out" in str(res_timeout["agy_error"]).lower()
    for ctx in fake_browser_2.contexts:
        for p in ctx.pages:
            for shot in p.screenshots:
                assert not shot.exists()


def test_visual_qa_source_migrates_legacy_improvement_ideas_table(tmp_path: Path) -> None:
    """Existing SQLite DBs with CHECK(source IN ('manual', 'auto')) migrate safely to accept 'visual_qa'."""
    db_path = tmp_path / "legacy_telemetry.db"
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            """
            CREATE TABLE improvement_ideas (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                title TEXT NOT NULL,
                category TEXT NOT NULL,
                impact TEXT NOT NULL,
                simple_description TEXT NOT NULL,
                technical_prompt TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'nueva'
                    CHECK(status IN ('nueva', 'encolada', 'descartada')),
                source TEXT NOT NULL DEFAULT 'manual'
                    CHECK(source IN ('manual', 'auto'))
            );
            """
        )
        conn.execute(
            """
            INSERT INTO improvement_ideas (
                created_at, title, category, impact,
                simple_description, technical_prompt, status, source
            ) VALUES (
                '2026-01-01T00:00:00Z', 'Idea Previa', 'UX', 'Alto',
                'Desc', 'Prompt', 'nueva', 'manual'
            );
            """
        )
        conn.commit()
    finally:
        conn.close()

    saved = idea_engine.save_visual_qa_ideas(
        [
            {
                "title": "Idea Previa",
                "category": "UX",
                "impact": "Alto",
                "simple_description": "Duplicada",
                "technical_prompt": "No debe insertarse",
            },
            {
                "title": "🎨 Mejorar Contraste en Modo Claro",
                "category": "UX / Visual",
                "impact": "Medio",
                "simple_description": "Aumenta el contraste de etiquetas secundarias.",
                "technical_prompt": "Ajusta --muted en dashboard.py y corre pytest -q --no-cov tests/test_dashboard.py.",
            },
        ],
        db_path=db_path,
        max_items=2,
    )

    assert len(saved) == 1
    assert saved[0]["source"] == "visual_qa"
    assert saved[0]["title"] == "🎨 Mejorar Contraste en Modo Claro"

    all_rows = idea_engine.list_ideas(statuses=None, db_path=db_path)
    assert {r["title"] for r in all_rows} == {"Idea Previa", "🎨 Mejorar Contraste en Modo Claro"}


def test_rejects_non_http_base_url(tmp_path: Path) -> None:
    from agent_factory import visual_qa_agent

    with pytest.raises(ValueError, match="http"):
        visual_qa_agent.run_synthetic_user_audit(
            base_url="file:///etc/passwd",
            db_path=tmp_path / "telemetry.db",
        )
