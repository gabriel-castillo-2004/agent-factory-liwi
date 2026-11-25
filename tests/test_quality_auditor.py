"""Tests for strict ParentAuditor penalty caps and optional Playwright browser smoke integration."""

from __future__ import annotations

import builtins
import sys
import types
from pathlib import Path
from typing import Any

import pytest

from agent_factory.adapters.fake_llm import FakeLLM
from agent_factory.agents.quality_auditor import AdversarialCritic, ParentAuditor
from agent_factory.domain.models import Spec
from agent_factory.runtime_runner import BrowserSmokeTestResult, run_browser_smoke_test


def _inflated_scores_payload() -> dict[str, Any]:
    return {
        "summary": "Aparentemente todo excelente.",
        "dimensions": {
            "ux_ui": {"score": 95.0, "findings": [], "improvements": []},
            "functional_completeness": {"score": 95.0, "findings": [], "improvements": []},
            "robustness": {"score": 92.0, "findings": [], "improvements": []},
            "architecture_quality": {"score": 92.0, "findings": [], "improvements": []},
            "security_hygiene": {"score": 94.0, "findings": [], "improvements": []},
            "testing_runtime": {"score": 95.0, "findings": [], "improvements": []},
        },
        "objections": [],
    }


def _full_coverage_tests() -> str:
    return (
        "import pytest\n"
        "def test_http_400_and_edge_cases():\n"
        "    with pytest.raises(ValueError):\n"
        "        raise ValueError('bad')\n"
        "    status_code = 400\n"
        "    assert status_code == 400\n"
        "    # Covers missing metadata, exif rotation, CLI FileNotFoundError fallback, cache invalidation\n"
        "    missing_metadata = {'captured_at': None}\n"
        "    assert missing_metadata['captured_at'] is None\n"
        "    exif_transpose_applied = True\n"
        "    assert exif_transpose_applied is True\n"
        "    cli_fallback = 'FileNotFoundError_fallback'\n"
        "    assert 'fallback' in cli_fallback\n"
        "    cache_invalidated = True\n"
        "    assert cache_invalidated is True\n"
    )


def test_ux_ui_capped_at_72_when_async_fetch_lacks_disable_spinner_or_engine_feedback(
    tmp_path: Path,
) -> None:
    """Strict UX/UI rubric caps score <= 72 if async fetch omits button disable, loading indicator, or engine/fallback status."""
    ws_bad = tmp_path / "ws_ux_bad"
    (ws_bad / "src").mkdir(parents=True)
    (ws_bad / "tests").mkdir(parents=True)
    (ws_bad / "index.html").write_text(
        "<!doctype html><html><head><meta name='viewport' content='width=device-width,initial-scale=1'>"
        "<style>@media(max-width:600px){body{padding:8px}}</style></head>"
        "<body><button id='run' aria-label='Ejecutar'>Organizar</button>"
        "<script>document.getElementById('run').onclick = async () => { await fetch('/api/run', {method:'POST'}); };</script>"
        "</body></html>\n",
        encoding="utf-8",
    )
    (ws_bad / "src" / "classifier.py").write_text(
        "def classify():\n    # Uses agy CLI or heuristic fallback\n    return 'Viaje'\n",
        encoding="utf-8",
    )
    (ws_bad / "tests" / "test_all.py").write_text(_full_coverage_tests(), encoding="utf-8")

    spec = Spec(objective="Organizador de fotos", users=["Usuario"], features=["Organizar por fecha"])
    auditor = ParentAuditor(llm=FakeLLM(responses=[_inflated_scores_payload(), _inflated_scores_payload()]))

    bad_report = auditor.audit_workspace("ux-bad", ws_bad, spec=spec)
    assert bad_report.dimensions["ux_ui"].score <= 72.0
    assert bad_report.global_score < 90.0
    ux_blob = " ".join(bad_report.dimensions["ux_ui"].findings).lower()
    assert any(k in ux_blob for k in ("deshabilit", "carga", "spinner", "motor", "fallback"))

    # Clean workspace with disabled button, loading spinner, and motor/fallback communication passes uncapped
    ws_good = tmp_path / "ws_ux_good"
    (ws_good / "src").mkdir(parents=True)
    (ws_good / "tests").mkdir(parents=True)
    (ws_good / "index.html").write_text(
        "<!doctype html><html><head><meta name='viewport' content='width=device-width,initial-scale=1'>"
        "<style>@media(max-width:600px){body{padding:8px}}</style></head>"
        "<body><button id='run' aria-label='Ejecutar'>Organizar</button>"
        "<span id='spinner' hidden>Cargando...</span><span id='engine-badge'></span>"
        "<script>"
        "document.getElementById('run').onclick = async () => {"
        "  const btn = document.getElementById('run'); btn.disabled = true;"
        "  const res = await fetch('/api/run', {method:'POST'}); const data = await res.json();"
        "  document.getElementById('engine-badge').textContent = 'Motor: ' + (data.fallback ? 'fallback local' : data.engine);"
        "  btn.disabled = false;"
        "};</script></body></html>\n",
        encoding="utf-8",
    )
    (ws_good / "src" / "classifier.py").write_text(
        "def classify():\n    return 'Viaje'\n",
        encoding="utf-8",
    )
    (ws_good / "tests" / "test_all.py").write_text(_full_coverage_tests(), encoding="utf-8")

    good_report = auditor.audit_workspace("ux-good", ws_good, spec=spec)
    assert good_report.dimensions["ux_ui"].score == pytest.approx(95.0, abs=0.05)


def test_functional_completeness_capped_at_70_for_template_outputs_and_uninvalidated_cache(
    tmp_path: Path,
) -> None:
    """Strict Funcionalidad rubric caps score <= 70 for '__', 'placeholder', 'fallback_', 'sample' outputs or regenerate without cache invalidation."""
    spec = Spec(objective="Clasificador con regeneración", users=["Usuario"], features=["Regenerar nombres"])

    # Case 1: Template/technical output literals
    ws_tpl = tmp_path / "ws_func_tpl"
    (ws_tpl / "src").mkdir(parents=True)
    (ws_tpl / "tests").mkdir(parents=True)
    (ws_tpl / "src" / "namer.py").write_text(
        "def make_name(idx: int) -> str:\n"
        "    if idx == 0:\n"
        "        return 'placeholder_evento'\n"
        "    return f'__staging_{idx}_fallback_item'\n",
        encoding="utf-8",
    )
    (ws_tpl / "tests" / "test_namer.py").write_text(_full_coverage_tests(), encoding="utf-8")

    # Case 2: Regenerate feature reusing cache without invalidation
    ws_cache = tmp_path / "ws_func_cache"
    (ws_cache / "src").mkdir(parents=True)
    (ws_cache / "tests").mkdir(parents=True)
    (ws_cache / "src" / "app.py").write_text(
        "AI_CACHE = {}\n"
        "def regenerar_nombres(item_id: str) -> str:\n"
        "    if item_id in AI_CACHE:\n"
        "        return AI_CACHE[item_id]\n"
        "    AI_CACHE[item_id] = 'Vacaciones_2025'\n"
        "    return AI_CACHE[item_id]\n",
        encoding="utf-8",
    )
    (ws_cache / "tests" / "test_app.py").write_text(_full_coverage_tests(), encoding="utf-8")

    # Case 3: Clean code (HTML placeholder attr + __name__ == '__main__' must NOT trigger cap)
    ws_clean = tmp_path / "ws_func_clean"
    (ws_clean / "src").mkdir(parents=True)
    (ws_clean / "tests").mkdir(parents=True)
    (ws_clean / "index.html").write_text(
        "<!doctype html><html><head><meta name='viewport' content='width=device-width,initial-scale=1'>"
        "<style>@media(max-width:600px){body{margin:0}}</style></head>"
        "<body><input aria-label='Buscar' placeholder='Buscar evento...'></body></html>\n",
        encoding="utf-8",
    )
    (ws_clean / "src" / "app.py").write_text(
        "AI_CACHE = {}\n"
        "def regenerar_nombres(item_id: str, invalidate_cache: bool = True) -> str:\n"
        "    if invalidate_cache:\n"
        "        AI_CACHE.pop(item_id, None)\n"
        "    return AI_CACHE.setdefault(item_id, 'Cumpleanos_Madrid')\n"
        "if __name__ == '__main__':\n"
        "    print(regenerar_nombres('1'))\n",
        encoding="utf-8",
    )
    (ws_clean / "tests" / "test_app.py").write_text(_full_coverage_tests(), encoding="utf-8")

    auditor = ParentAuditor(
        llm=FakeLLM(
            responses=[
                _inflated_scores_payload(),
                _inflated_scores_payload(),
                _inflated_scores_payload(),
            ]
        )
    )
    rep_tpl = auditor.audit_workspace("func-tpl", ws_tpl, spec=spec)
    assert rep_tpl.dimensions["functional_completeness"].score <= 70.0

    rep_cache = auditor.audit_workspace("func-cache", ws_cache, spec=spec)
    assert rep_cache.dimensions["functional_completeness"].score <= 70.0
    assert any("cach" in f.lower() or "regenera" in f.lower() for f in rep_cache.dimensions["functional_completeness"].findings)

    rep_clean = auditor.audit_workspace("func-clean", ws_clean, spec=spec)
    assert rep_clean.dimensions["functional_completeness"].score == pytest.approx(95.0, abs=0.05)


def test_testing_runtime_capped_at_72_for_shallow_http_200_tests_and_rubric_prompt_included(
    tmp_path: Path,
) -> None:
    """Strict Pruebas rubric caps score <= 72 when tests only assert HTTP 200 / non-empty strings without edge cases, andCritic prompt states all caps."""
    ws_shallow = tmp_path / "ws_tests_shallow"
    (ws_shallow / "src").mkdir(parents=True)
    (ws_shallow / "tests").mkdir(parents=True)
    (ws_shallow / "src" / "server.py").write_text(
        "import subprocess\n"
        "from PIL import Image, ImageOps\n"
        "def classify_with_cli():\n"
        "    return subprocess.run(['agy', '--version'], check=False)\n"
        "def make_thumb(p):\n"
        "    return ImageOps.exif_transpose(Image.open(p))\n",
        encoding="utf-8",
    )
    (ws_shallow / "tests" / "test_shallow.py").write_text(
        "def test_health(client):\n"
        "    resp = client.get('/api/health')\n"
        "    assert resp.status_code == 200\n"
        "    assert len(resp.text) > 0\n",
        encoding="utf-8",
    )

    spec = Spec(objective="App de fotos", users=["Usuario"], features=["Miniaturas y CLI"])
    spy_llm = FakeLLM(responses=[_inflated_scores_payload()])
    critic = AdversarialCritic(llm=spy_llm)
    auditor = ParentAuditor(llm=spy_llm, critic=critic)

    rep = auditor.audit_workspace("shallow-tests", ws_shallow, spec=spec)
    assert rep.dimensions["testing_runtime"].score <= 72.0
    assert any(
        "200" in f or "borde" in f.lower() or "exif" in f.lower() or "cli" in f.lower()
        for f in rep.dimensions["testing_runtime"].findings
    )

    # Verify prompt sent to AdversarialCritic explicitly includes all three strict cap instructions
    prompt_blob = str(spy_llm.calls[0]["messages"])
    assert "72" in prompt_blob
    assert "70" in prompt_blob
    assert "scrollWidth" in prompt_blob or "fetch" in prompt_blob.lower()
    assert "fallback_" in prompt_blob
    assert "placeholder" in prompt_blob


def test_run_browser_smoke_test_optional_missing_and_reports_dom_errors_to_critic(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """run_browser_smoke_test returns available=False gracefully if Playwright is missing, captures console.error/pageerror/overflow/broken images when present, and forwards findings to AdversarialCritic."""
    # 1. Missing playwright -> graceful no-op pass
    real_import = builtins.__import__

    def _missing_playwright(name: str, *args: Any, **kwargs: Any) -> Any:
        if name == "playwright" or name.startswith("playwright."):
            raise ImportError("No module named 'playwright'")
        return real_import(name, *args, **kwargs)

    with monkeypatch.context() as mp:
        mp.setattr(builtins, "__import__", _missing_playwright)
        res_missing = run_browser_smoke_test("http://127.0.0.1:8123")
        assert isinstance(res_missing, BrowserSmokeTestResult)
        assert res_missing.available is False
        assert res_missing.passed is True
        assert res_missing.findings == []

    # 2. Installed playwright (mocked) -> captures console.error, pageerror, horizontal overflow, broken image
    class _FakeConsoleMsg:
        def __init__(self, msg_type: str, text: str) -> None:
            self.type = msg_type
            self.text = text

    class _FakePage:
        def __init__(self) -> None:
            self.handlers: dict[str, list[Any]] = {}

        def on(self, event: str, fn: Any) -> None:
            self.handlers.setdefault(event, []).append(fn)

        def goto(self, _url: str, **_kwargs: Any) -> None:
            for fn in self.handlers.get("console", []):
                fn(_FakeConsoleMsg("error", "TypeError: cannot read properties of undefined"))
            for fn in self.handlers.get("pageerror", []):
                fn(RuntimeError("Uncaught ReferenceError: initApp is not defined"))

        def wait_for_timeout(self, _ms: int) -> None:
            return None

        def evaluate(self, script: str) -> Any:
            if "scrollWidth" in script:
                return {"scrollWidth": 1520, "innerWidth": 1280}
            return ["/static/broken-photo.jpg"]

    class _FakeBrowser:
        def __init__(self) -> None:
            self.closed = False

        def new_page(self, **_kwargs: Any) -> _FakePage:
            return _FakePage()

        def close(self) -> None:
            self.closed = True

    fake_browser = _FakeBrowser()

    class _FakePWManager:
        def __enter__(self) -> Any:
            return types.SimpleNamespace(
                chromium=types.SimpleNamespace(launch=lambda **_kw: fake_browser)
            )

        def __exit__(self, *_args: object) -> None:
            return None

    fake_pw_pkg = types.ModuleType("playwright")
    fake_pw_sync = types.ModuleType("playwright.sync_api")
    fake_pw_sync.sync_playwright = lambda: _FakePWManager()  # type: ignore[attr-defined]
    fake_pw_pkg.sync_api = fake_pw_sync  # type: ignore[attr-defined]

    with monkeypatch.context() as mp:
        mp.setitem(sys.modules, "playwright", fake_pw_pkg)
        mp.setitem(sys.modules, "playwright.sync_api", fake_pw_sync)
        res_live = run_browser_smoke_test("http://127.0.0.1:8123")

    assert fake_browser.closed is True
    assert res_live.available is True
    assert res_live.passed is False
    assert res_live.horizontal_overflow is True
    assert res_live.scroll_width == 1520
    assert res_live.inner_width == 1280
    assert res_live.broken_images == ["/static/broken-photo.jpg"]
    assert any("cannot read properties" in e for e in res_live.console_errors)
    assert any("initApp" in e for e in res_live.page_errors)
    assert len(res_live.findings) >= 4

    # 3. Findings delivered to AdversarialCritic -> included in critic prompt and cap UX/UI <= 72
    ws_ui = tmp_path / "ws_browser_critic"
    (ws_ui / "tests").mkdir(parents=True)
    (ws_ui / "index.html").write_text(
        "<!doctype html><html><head><meta name='viewport' content='width=device-width,initial-scale=1'>"
        "<style>@media(max-width:600px){body{margin:0}}</style></head>"
        "<body><h1>Panel</h1><img aria-label='Foto' src='/static/broken-photo.jpg'></body></html>\n",
        encoding="utf-8",
    )
    (ws_ui / "tests" / "test_ui.py").write_text(_full_coverage_tests(), encoding="utf-8")

    spy_llm = FakeLLM(responses=[_inflated_scores_payload()])
    auditor = ParentAuditor(llm=spy_llm)
    report = auditor.audit_workspace(
        "browser-audit",
        ws_ui,
        spec=Spec(objective="Galería web", users=["Usuario"], features=["Ver fotos"]),
        browser_smoke_findings=res_live,
    )
    assert report.dimensions["ux_ui"].score <= 72.0
    critic_prompt = str(spy_llm.calls[0]["messages"])
    assert "1520" in critic_prompt and "1280" in critic_prompt
    assert "broken-photo.jpg" in critic_prompt
    assert "initApp" in critic_prompt
    assert any("broken-photo.jpg" in f for f in report.dimensions["ux_ui"].findings)
