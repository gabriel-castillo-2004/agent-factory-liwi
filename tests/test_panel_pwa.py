"""Tests for static PWA Panel assets, security contract, bundle export, and invalid JSON handling (AC-4.1, INV-16, INV-19, INV-22, INV-23)."""

import json
import re
import shutil
import subprocess
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


def test_panel_pwa_js_behavior_xss_id_links_detail_rendering_and_html_input() -> None:
    """H4-04 / H4-08 / M4-19 / M4-20 (INV-16, INV-22, INV-23): Executes panel/app.js against real XSS/ID/URL/detail payloads and checks relative input support in index.html."""
    js_exe = shutil.which("node") or shutil.which("deno")
    assert js_exe is not None, "A JavaScript runtime (node or deno) is required to execute panel/app.js tests"

    app_js_posix = (PANEL_DIR / "app.js").resolve().as_posix()
    js_script = f"""
const appPath = {json.dumps(app_js_posix)};
const readFile = (p) => typeof Deno !== "undefined" ? Deno.readTextFileSync(p) : require("fs").readFileSync(p, "utf8");
function makeEl(id) {{
  let _html = "";
  const children = [];
  return {{
    id, className: "", tabIndex: 0, textContent: "", value: "", children, listeners: {{}},
    classList: {{ _s: new Set(), add(c){{ this._s.add(c); }}, remove(c){{ this._s.delete(c); }} }},
    setAttribute(){{}},
    addEventListener(t, fn){{ (this.listeners[t] = this.listeners[t] || []).push(fn); }},
    appendChild(ch){{ children.push(ch); }},
    get innerHTML(){{ return _html; }},
    set innerHTML(v){{ _html = String(v); if (_html === "") children.length = 0; }}
  }};
}}
const els = {{}};
for (const id of [
  "error-banner", "registry-base-input", "source-form", "refresh-btn", "search-input",
  "state-filter", "projects-list", "projects-empty", "detail-placeholder", "detail-content",
  "connection-badge", "updated-at-badge", "kpi-total", "kpi-delivered", "kpi-active", "kpi-failed"
]) {{
  els[id] = makeEl(id);
}}
const docListeners = {{}};
globalThis.document = {{
  getElementById: (id) => els[id] || null,
  createElement: (tag) => makeEl(tag),
  addEventListener: (t, fn) => {{ (docListeners[t] = docListeners[t] || []).push(fn); }}
}};
globalThis.window = {{
  location: {{ search: "" }},
  localStorage: {{ getItem: () => null, setItem: () => {{}} }},
  addEventListener: () => {{}}
}};
globalThis.fetch = async (url) => {{
  if (url.endsWith("/registry.json")) {{
    return {{
      ok: true,
      status: 200,
      text: async () => JSON.stringify({{
        version: "1.0",
        projects: {{
          "demo-app": {{
            project_id: "demo-app",
            name: "Demo <script>alert(1)</script>",
            objective: "Obj",
            state: "DELIVERED",
            progress_pct: 100
          }}
        }}
      }})
    }};
  }}
  return {{
    ok: true,
    status: 200,
    text: async () => JSON.stringify({{
      project_id: "demo-app",
      name: "Demo",
      objective: "Obj",
      state: "DELIVERED",
      phase: "DELIVERED",
      progress_pct: 100,
      agent_invocations: 4,
      ci_rounds: 1,
      project_prompt: "# PROJECT_PROMPT_MARKER\\nPrompt <img src=x onerror=alert(2)>",
      build_report: {{ invocations: 4, ci_rounds: 1, failure_cause: "REPORT_CAUSE_MARKER <svg onload=alert(3)>" }},
      events: []
    }})
  }};
}};
(async () => {{
  new Function(readFile(appPath))();
  const api = globalThis.AgentFactoryPanel;
  for (const fn of (docListeners["DOMContentLoaded"] || [])) fn();
  await new Promise((r) => setTimeout(r, 20));
  if (els["projects-list"].children.length > 0 && els["projects-list"].children[0].listeners.click) {{
    els["projects-list"].children[0].listeners.click[0]();
    await new Promise((r) => setTimeout(r, 20));
  }}
  const catalog = api.parseRegistryCatalog({{
    version: "1.0",
    projects: {{
      "demo-app": {{ name: "Demo", state: "DELIVERED", progress_pct: 100 }},
      "../escape": {{ name: "Bad", state: "DELIVERED", progress_pct: 100 }},
      "UPPER": {{ name: "Bad2", state: "DELIVERED", progress_pct: 100 }}
    }}
  }});
  console.log(JSON.stringify({{
    escapedScript: api.escapeHtml("<script>alert(1)</script>"),
    escapedQuotes: api.escapeHtml("'\\"&"),
    validOk: [api.isValidProjectId("demo-app"), api.isValidProjectId("a")],
    validBad: [
      api.isValidProjectId("../escape"),
      api.isValidProjectId("UPPER"),
      api.isValidProjectId("-lead"),
      api.isValidProjectId("x".repeat(64)),
      api.isValidProjectId("")
    ],
    catalogIds: catalog.projects.map((p) => p.project_id),
    links: {{
      ok: api.sanitizeLinkUrl("https://github.com/acme/demo-app"),
      protoRel: api.sanitizeLinkUrl("//evil.com"),
      jsScheme: api.sanitizeLinkUrl("javascript:alert(1)"),
      dataScheme: api.sanitizeLinkUrl("data:text/html,hi"),
      relPath: api.sanitizeLinkUrl("../rel"),
      credUrl: api.sanitizeLinkUrl("https://user:pass@example.com")
    }},
    cardsHtml: els["projects-list"].children.map((c) => c.innerHTML).join(""),
    detailHtml: els["detail-content"].innerHTML
  }}));
}})().catch((err) => {{
  console.error(err);
  if (typeof Deno !== "undefined") Deno.exit(1); else process.exit(1);
}});
"""
    cmd = (
        [js_exe, "-e", js_script]
        if Path(js_exe).stem.lower() == "node"
        else [js_exe, "eval", "--no-remote", js_script]
    )
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=15, check=True)
    out = json.loads(proc.stdout.strip())

    # Hand-crafted oracles killing M4-19 and M4-20
    assert out["escapedScript"] == "&lt;script&gt;alert(1)&lt;/script&gt;"
    assert out["escapedQuotes"] == "&#39;&quot;&amp;"
    assert out["validOk"] == [True, True]
    assert out["validBad"] == [False, False, False, False, False]
    assert out["catalogIds"] == ["demo-app"]
    assert "<script>" not in out["cardsHtml"]
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in out["cardsHtml"]

    # Link URL sanitization (H4-08)
    assert out["links"]["ok"] == "https://github.com/acme/demo-app"
    assert out["links"]["protoRel"] is None
    assert out["links"]["jsScheme"] is None
    assert out["links"]["dataScheme"] is None
    assert out["links"]["relPath"] is None
    assert out["links"]["credUrl"] is None

    # Detail view must render escaped PROJECT_PROMPT.md and BuildReport (H4-08 / MASTER_PROMPT.md §7)
    detail_html = out["detailHtml"]
    assert "PROJECT_PROMPT_MARKER" in detail_html
    assert "<img" not in detail_html
    assert "&lt;img src=x onerror=alert(2)&gt;" in detail_html
    assert "REPORT_CAUSE_MARKER" in detail_html
    assert "<svg" not in detail_html
    assert "&lt;svg onload=alert(3)&gt;" in detail_html

    index_html = (PANEL_DIR / "index.html").read_text(encoding="utf-8")
    assert re.search(r'<input[^>]*id="registry-base-input"[^>]*type="url"', index_html) is None
    assert re.search(r'<input[^>]*type="url"[^>]*id="registry-base-input"', index_html) is None


def test_panel_pwa_detail_redaction_tolerates_telegram_url_without_trailing_slash() -> None:
    """H4-05 (INV-07, INV-19, INV-23): build_sanitized_project_detail does not break JSON when Telegram bot URL has no trailing slash."""
    status = ProjectStatus(
        project_id="app-tg-noslash",
        name="TG NoSlash",
        objective="Test",
        state=ProjectState.FAILED,
        progress_pct=35,
    )
    report = BuildReport(
        invocations=1,
        ci_rounds=0,
        test_results={},
        failure_cause="Failed contacting https://api.telegram.org/bot123456:SECRET_NO_SLASH_999",
    )
    detail = StaticDeployPublisher().build_sanitized_project_detail(status, build_report=report)
    rendered = json.dumps(detail)
    assert "SECRET_NO_SLASH_999" not in rendered
    assert "[REDACTED]" in rendered


def test_panel_styles_css_targets_text_input_not_removed_url_input() -> None:
    """H4-12 / H41-04 (INV-16, INV-23): styles.css must style #registry-base-input / input[type="text"] instead of relying on removed input[type="url"]."""
    styles_css = (PANEL_DIR / "styles.css").read_text(encoding="utf-8")
    assert re.search(r'input\[type=["\']url["\']\]', styles_css) is None
    assert re.search(r'input\[type=["\']text["\']\]|#registry-base-input', styles_css) is not None
