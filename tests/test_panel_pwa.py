"""Tests for static PWA Panel assets, security contract, bundle export, and invalid JSON handling (AC-4.1, INV-16, INV-19, INV-22, INV-23)."""

import json
import re
from datetime import UTC, datetime
from pathlib import Path

from agent_factory.adapters.static_deploy_publisher import StaticDeployPublisher
from agent_factory.domain.events import Event, EventType
from agent_factory.domain.models import (
    BuildReport,
    ProjectLinks,
    ProjectStatus,
    TestCounts,
)
from agent_factory.domain.states import ProjectState

PANEL_DIR = Path(__file__).resolve().parent.parent / "panel"


def test_panel_pwa_static_assets_manifest_sw_and_security_contract() -> None:
    """AC-4.1 positive & negative (INV-16, INV-19, INV-22, INV-23): Static PWA assets, valid manifest, offline SW, zero backend/secrets, XSS & JSON guards."""
    required_files = (
        "index.html",
        "app.js",
        "styles.css",
        "manifest.webmanifest",
        "sw.js",
        "icons/icon.svg",
    )
    for rel_path in required_files:
        asset = PANEL_DIR / rel_path
        assert asset.is_file(), f"Missing PWA asset: {asset}"
        assert asset.read_text(encoding="utf-8").strip() != ""

    manifest_raw = (PANEL_DIR / "manifest.webmanifest").read_text(encoding="utf-8")
    manifest = json.loads(manifest_raw)
    assert manifest["name"] == "Agent Factory Registry Panel"
    assert manifest["short_name"] == "AgentFactory"
    assert manifest["start_url"] == "./index.html"
    assert manifest["display"] == "standalone"
    assert isinstance(manifest.get("icons"), list) and len(manifest["icons"]) >= 1
    for icon in manifest["icons"]:
        assert "src" in icon and "sizes" in icon and "type" in icon
        icon_path = (PANEL_DIR / icon["src"]).resolve()
        assert icon_path.is_file() and icon_path.is_relative_to(PANEL_DIR.resolve())

    index_html = (PANEL_DIR / "index.html").read_text(encoding="utf-8")
    assert 'rel="manifest"' in index_html
    assert "./manifest.webmanifest" in index_html
    assert "./styles.css" in index_html
    assert "./app.js" in index_html
    for required_id in (
        'id="registry-base-input"',
        'id="error-banner"',
        'id="search-input"',
        'id="state-filter"',
        'id="projects-list"',
        'id="detail-pane"',
    ):
        assert required_id in index_html

    sw_js = (PANEL_DIR / "sw.js").read_text(encoding="utf-8")
    assert 'event.request.method !== "GET"' in sw_js
    assert "./index.html" in sw_js
    assert "./manifest.webmanifest" in sw_js
    assert "registry.json" in sw_js
    assert "caches.open" in sw_js

    app_js = (PANEL_DIR / "app.js").read_text(encoding="utf-8")
    # Strict project_id validation matching INV-22, XSS escaping, defensive JSON.parse, URL scheme guard
    assert "^[a-z0-9][a-z0-9-]{0,62}$" in app_js
    assert "escapeHtml" in app_js
    assert "JSON.parse" in app_js
    assert "try {" in app_js
    assert "catch" in app_js
    assert "javascript" in app_js and "data" in app_js
    assert "serviceWorker" in app_js

    # Negative security check: ZERO backend hosts, mutating methods, Authorization headers, or secrets
    forbidden_patterns = (
        r"localhost:\d+",
        r"127\.0\.0\.1",
        r"Authorization",
        r'method:\s*["\'](?:POST|PUT|DELETE|PATCH)["\']',
        r"\b(?:ghp_|github_pat_|sk-)[A-Za-z0-9_-]{10,}",
        r"GITHUB_TOKEN\s*=\s*\S+",
        r"TELEGRAM_BOT_TOKEN\s*=\s*\S+",
        r"VERCEL_TOKEN\s*=\s*\S+",
    )
    for rel_path in required_files:
        text = (PANEL_DIR / rel_path).read_text(encoding="utf-8")
        for pat in forbidden_patterns:
            assert re.search(pat, text) is None, f"Forbidden pattern {pat} found in {rel_path}"


def test_panel_pwa_bundle_export_sanitized_history_and_negative_cases(tmp_path: Path) -> None:
    """AC-4.1 integration & negative (INV-07, INV-19, INV-22, INV-23): StaticDeployPublisher builds sanitized panel bundle & rejects bad project_id."""
    secret_gh = "TEST_PANEL_GH_SECRET_1234"
    secret_tg = "bot991122:TEST_PANEL_TG_SECRET_XYZ"
    ts = datetime(2026, 11, 17, 9, 0, tzinfo=UTC)

    status = ProjectStatus(
        project_id="app-panel-ok",
        name="Panel Demo App",
        objective="Mostrar catálogo offline",
        state=ProjectState.DELIVERED,
        phase="DELIVERED",
        progress_pct=100,
        plan_hash="hash-panel-01",
        tests=TestCounts(total=2, passed=2, failed=0),
        agent_invocations=4,
        ci_rounds=1,
        links=ProjectLinks(
            repo="https://github.com/acme/app-panel-ok",
            deploy="https://acme.github.io/app-panel-ok",
        ),
        updated_at=ts,
    )
    report = BuildReport(
        invocations=4,
        test_results={"AT-01": True, "AT-02": True},
        ci_rounds=1,
        failure_cause=f"Aviso previo con GITHUB_TOKEN={secret_gh}",
    )
    events = [
        Event(
            id="evt-1",
            project_id="app-panel-ok",
            type=EventType.PROJECT_CREATED,
            payload={"name": "Panel Demo App"},
            at=ts,
        ),
        Event(
            id="evt-2",
            project_id="app-panel-ok",
            type=EventType.REGISTRY_PUBLISH_FAILED,
            payload={"reason": f"Retry error {secret_tg}"},
            at=ts,
        ),
    ]

    publisher = StaticDeployPublisher()
    detail = publisher.build_sanitized_project_detail(
        status,
        events=events,
        build_report=report,
        project_prompt="# Prompt limpio",
    )

    assert detail["project_id"] == "app-panel-ok"
    assert detail["state"] == "DELIVERED"
    assert detail["progress_pct"] == 100
    assert len(detail["events"]) == 2
    assert detail["events"][0]["type"] == "PROJECT_CREATED"
    assert detail["events"][1]["type"] == "REGISTRY_PUBLISH_FAILED"

    detail_json = json.dumps(detail)
    assert secret_gh not in detail_json
    assert secret_tg not in detail_json
    assert "[REDACTED]" in detail_json

    out_dir = tmp_path / "dist_panel"
    bundle_dir = publisher.export_panel_bundle(
        out_dir,
        statuses=[status],
        events_by_project={"app-panel-ok": events},
        reports_by_project={"app-panel-ok": report},
        panel_source_dir=PANEL_DIR,
    )
    assert (bundle_dir / "index.html").is_file()
    assert (bundle_dir / "manifest.webmanifest").is_file()
    assert (bundle_dir / "sw.js").is_file()

    reg_data = json.loads((bundle_dir / "registry.json").read_text(encoding="utf-8"))
    assert list(reg_data["projects"].keys()) == ["app-panel-ok"]
    assert reg_data["projects"]["app-panel-ok"]["state"] == "DELIVERED"
    assert reg_data["projects"]["app-panel-ok"]["progress_pct"] == 100

    proj_json_text = (bundle_dir / "projects" / "app-panel-ok.json").read_text(encoding="utf-8")
    assert secret_gh not in proj_json_text
    assert secret_tg not in proj_json_text

    # Negative: invalid project_id must fail validation without writing files
    bad_status = ProjectStatus.model_construct(
        project_id="../escape",
        state=ProjectState.DELIVERED,
        progress_pct=100,
    )
    try:
        publisher.build_sanitized_project_detail(bad_status)
        raise AssertionError("Expected ValueError for invalid project_id")
    except ValueError:
        pass
