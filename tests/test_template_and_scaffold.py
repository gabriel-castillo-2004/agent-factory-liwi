"""Tests for templates/static-web/ and TemplateScaffold (A2b-1, A2b-2, INV-13, INV-16, INV-20, INV-22, INV-23)."""

import json
import os
import subprocess
from pathlib import Path

import pytest

from agent_factory.scaffold import DEFAULT_TEMPLATES_ROOT, TemplateScaffold
from agent_factory.workspace import SandboxViolationError, WorkspaceManager


def _create_dir_link(link_path: Path, target_dir: Path) -> None:
    """Create a directory symlink or Windows directory junction without requiring elevation."""
    try:
        os.symlink(target_dir, link_path, target_is_directory=True)
    except OSError:
        subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link_path), str(target_dir)],
            check=True,
            capture_output=True,
            text=True,
        )


def test_static_web_template_exists_has_required_files_and_empty_src(tmp_path: Path) -> None:
    """A2b-1 positive (INV-13, INV-16, INV-20, INV-23): templates/static-web/ has all required static files, valid package.json, zero external/backend refs, and empty src/."""
    ws_mgr = WorkspaceManager(base_dir=tmp_path / "workspaces")
    scaffold = TemplateScaffold(workspace_manager=ws_mgr, templates_root=DEFAULT_TEMPLATES_ROOT)

    inspection = scaffold.inspect_template("static-web")

    assert inspection.template_id == "static-web"
    assert inspection.files == [
        "index.html",
        "package.json",
        "playwright.config.mjs",
        "styles.css",
    ]
    assert inspection.package_name == "agent-factory-static-web-template"
    assert inspection.scripts == {
        "test": "node --test",
        "test:unit": "node --test",
        "test:e2e": "playwright test",
    }
    assert inspection.src_files == []
    assert inspection.external_or_backend_refs == []

    # Direct file verification against hand-written oracle
    tpl_dir = DEFAULT_TEMPLATES_ROOT / "static-web"
    assert tpl_dir.is_dir()
    pkg_data = json.loads((tpl_dir / "package.json").read_text(encoding="utf-8"))
    assert pkg_data["private"] is True
    assert pkg_data["type"] == "module"
    assert pkg_data["dependencies"] == {}

    html_text = (tpl_dir / "index.html").read_text(encoding="utf-8")
    assert 'id="app"' in html_text
    assert 'src="./src/app.js"' in html_text
    assert "http://" not in html_text
    assert "https://" not in html_text

    pw_text = (tpl_dir / "playwright.config.mjs").read_text(encoding="utf-8")
    assert "offline: true" in pw_text
    assert "headless: true" in pw_text


def test_inspect_template_rejects_missing_files_external_network_backend_or_populated_src(
    tmp_path: Path,
) -> None:
    """A2b-1 negative (INV-13, INV-16, INV-22, INV-23): inspect_template rejects missing required files, external/backend references, or pre-existing files in src/."""
    templates_root = tmp_path / "templates"
    ws_mgr = WorkspaceManager(base_dir=tmp_path / "workspaces")
    scaffold = TemplateScaffold(workspace_manager=ws_mgr, templates_root=templates_root)

    # 1. Missing required file (playwright.config.mjs)
    incomplete_dir = templates_root / "incomplete-web"
    incomplete_dir.mkdir(parents=True, exist_ok=True)
    (incomplete_dir / "package.json").write_text(
        '{"name": "tpl", "scripts": {"test": "node --test"}, "dependencies": {}}',
        encoding="utf-8",
    )
    (incomplete_dir / "index.html").write_text("<html></html>", encoding="utf-8")
    (incomplete_dir / "styles.css").write_text("body {}", encoding="utf-8")

    with pytest.raises((FileNotFoundError, ValueError)):
        scaffold.inspect_template("incomplete-web")

    pw_valid = (DEFAULT_TEMPLATES_ROOT / "static-web" / "playwright.config.mjs").read_text(
        encoding="utf-8"
    )

    # 1b. Missing required index.html when playwright.config.mjs is present (kills M5/M5b)
    missing_index_dir = templates_root / "missing-index-web"
    missing_index_dir.mkdir(parents=True, exist_ok=True)
    (missing_index_dir / "package.json").write_text(
        '{"name": "tpl", "scripts": {"test": "node --test"}, "dependencies": {}}',
        encoding="utf-8",
    )
    (missing_index_dir / "playwright.config.mjs").write_text(pw_valid, encoding="utf-8")
    (missing_index_dir / "styles.css").write_text("body {}", encoding="utf-8")

    with pytest.raises(FileNotFoundError):
        scaffold.inspect_template("missing-index-web")

    # 1c. Missing required styles.css when playwright.config.mjs is present (kills M5/M5b)
    missing_styles_dir = templates_root / "missing-styles-web"
    missing_styles_dir.mkdir(parents=True, exist_ok=True)
    (missing_styles_dir / "package.json").write_text(
        '{"name": "tpl", "scripts": {"test": "node --test"}, "dependencies": {}}',
        encoding="utf-8",
    )
    (missing_styles_dir / "playwright.config.mjs").write_text(pw_valid, encoding="utf-8")
    (missing_styles_dir / "index.html").write_text("<html></html>", encoding="utf-8")

    with pytest.raises(FileNotFoundError):
        scaffold.inspect_template("missing-styles-web")

    # 2. External network URL or backend dependency in template
    net_dir = templates_root / "networked-web"
    net_dir.mkdir(parents=True, exist_ok=True)
    (net_dir / "package.json").write_text(
        '{"name": "tpl", "scripts": {"test": "node --test"}, "dependencies": {"express": "4.19.2"}}',
        encoding="utf-8",
    )
    (net_dir / "playwright.config.mjs").write_text("export default {};", encoding="utf-8")
    (net_dir / "index.html").write_text(
        '<script src="https://cdn.example.com/lib.js"></script>',
        encoding="utf-8",
    )
    (net_dir / "styles.css").write_text("body {}", encoding="utf-8")

    with pytest.raises(ValueError):
        scaffold.inspect_template("networked-web")

    # 3. Pre-existing production file inside src/ (violates A2a-5 / INV-13)
    contaminated_dir = templates_root / "contaminated-src-web"
    (contaminated_dir / "src").mkdir(parents=True, exist_ok=True)
    (contaminated_dir / "package.json").write_text(
        '{"name": "tpl", "scripts": {"test": "node --test"}, "dependencies": {}}',
        encoding="utf-8",
    )
    (contaminated_dir / "playwright.config.mjs").write_text("export default {};", encoding="utf-8")
    (contaminated_dir / "index.html").write_text("<html></html>", encoding="utf-8")
    (contaminated_dir / "styles.css").write_text("body {}", encoding="utf-8")
    (contaminated_dir / "src" / "app.js").write_text("export const x = 1;", encoding="utf-8")

    with pytest.raises(SandboxViolationError):
        scaffold.inspect_template("contaminated-src-web")


def test_template_scaffold_materializes_static_web_into_isolated_workspace(
    tmp_path: Path,
) -> None:
    """A2b-2 positive (INV-20, INV-22, INV-23): TemplateScaffold copies templates/static-web/ into workspaces/<project_id>/ leaving src/ empty."""
    base_dir = tmp_path / "workspaces"
    ws_mgr = WorkspaceManager(base_dir=base_dir)
    scaffold = TemplateScaffold(workspace_manager=ws_mgr, templates_root=DEFAULT_TEMPLATES_ROOT)

    ws_dir = scaffold.scaffold(project_id="proj-static-01", template_id="static-web")

    expected_ws_dir = (base_dir / "proj-static-01").resolve()
    assert ws_dir == expected_ws_dir
    assert (ws_dir / "package.json").is_file()
    assert (ws_dir / "playwright.config.mjs").is_file()
    assert (ws_dir / "index.html").is_file()
    assert (ws_dir / "styles.css").is_file()
    assert (ws_dir / "src").is_dir()
    assert [p for p in (ws_dir / "src").rglob("*") if p.is_file()] == []


def test_template_scaffold_rejects_invalid_project_id_contaminated_workspace_or_symlink_escape(
    tmp_path: Path,
) -> None:
    """A2b-2 negative (INV-13, INV-18, INV-22, INV-23): TemplateScaffold rejects invalid project_ids, contaminated destination workspaces, and symlink escapes."""
    base_dir = tmp_path / "workspaces"
    ws_mgr = WorkspaceManager(base_dir=base_dir)
    scaffold = TemplateScaffold(workspace_manager=ws_mgr, templates_root=DEFAULT_TEMPLATES_ROOT)

    # 1. Invalid project_id
    for bad_id in ("../escape", "..\\escape", "UPPERCASE", ""):
        with pytest.raises(SandboxViolationError):
            scaffold.scaffold(project_id=bad_id, template_id="static-web")

    # 2. Invalid/traversal template_id
    with pytest.raises((SandboxViolationError, ValueError)):
        scaffold.scaffold(project_id="proj-valid-01", template_id="../outside")

    # 3. Destination workspace already contains files in src/
    ws_contaminated = ws_mgr.create_workspace("proj-dirty-dest")
    (ws_contaminated / "src").mkdir(parents=True, exist_ok=True)
    (ws_contaminated / "src" / "existing.js").write_text("export const dirty = true;", encoding="utf-8")

    with pytest.raises(SandboxViolationError):
        scaffold.scaffold(project_id="proj-dirty-dest", template_id="static-web")

    # 4. Template containing a directory link escaping templates_root
    custom_templates = tmp_path / "custom_templates"
    evil_tpl = custom_templates / "evil-web"
    evil_tpl.mkdir(parents=True, exist_ok=True)
    for name in ("package.json", "playwright.config.mjs", "index.html", "styles.css"):
        (evil_tpl / name).write_text(
            (DEFAULT_TEMPLATES_ROOT / "static-web" / name).read_text(encoding="utf-8"),
            encoding="utf-8",
        )
    outside_target = tmp_path / "outside_secret_dir"
    outside_target.mkdir(parents=True, exist_ok=True)
    (outside_target / "secret.txt").write_text("TOP_SECRET", encoding="utf-8")
    _create_dir_link(evil_tpl / "linked_dir", outside_target)

    evil_scaffold = TemplateScaffold(workspace_manager=ws_mgr, templates_root=custom_templates)
    with pytest.raises(SandboxViolationError):
        evil_scaffold.scaffold(project_id="proj-evil-link", template_id="evil-web")


def _write_valid_template_base(tpl_dir: Path) -> None:
    """Populate tpl_dir with clean static-web files copied directly from DEFAULT_TEMPLATES_ROOT."""
    tpl_dir.mkdir(parents=True, exist_ok=True)
    for name in ("package.json", "playwright.config.mjs", "index.html", "styles.css"):
        (tpl_dir / name).write_text(
            (DEFAULT_TEMPLATES_ROOT / "static-web" / name).read_text(encoding="utf-8"),
            encoding="utf-8",
        )


def test_static_web_template_and_inspect_template_enforce_console_error_and_pageerror_guard(
    tmp_path: Path,
) -> None:
    """F2B-03 (AC-2.5, INV-13, INV-23): templates/static-web/ enforces console.error and pageerror failure guards, and inspect_template rejects templates missing them."""
    ws_mgr = WorkspaceManager(base_dir=tmp_path / "workspaces")
    default_scaffold = TemplateScaffold(
        workspace_manager=ws_mgr, templates_root=DEFAULT_TEMPLATES_ROOT
    )

    # Positive check against templates/static-web/
    inspection = default_scaffold.inspect_template("static-web")
    assert inspection.template_id == "static-web"
    pw_text = (DEFAULT_TEMPLATES_ROOT / "static-web" / "playwright.config.mjs").read_text(
        encoding="utf-8"
    )
    assert 'page.on("console"' in pw_text
    assert 'msg.type() === "error"' in pw_text
    assert 'page.on("pageerror"' in pw_text
    assert "throw new Error" in pw_text

    # Negative checks against custom templates missing browser error guards
    templates_root = tmp_path / "templates"
    custom_scaffold = TemplateScaffold(workspace_manager=ws_mgr, templates_root=templates_root)

    # Case 1: Missing both console error and pageerror listeners
    no_guard_dir = templates_root / "no-error-guard-web"
    _write_valid_template_base(no_guard_dir)
    (no_guard_dir / "playwright.config.mjs").write_text(
        'export default { testDir: "./tests", use: { headless: true, offline: true } };\n',
        encoding="utf-8",
    )
    with pytest.raises(ValueError):
        custom_scaffold.inspect_template("no-error-guard-web")

    # Case 2: Only console error listener without pageerror listener
    no_pageerror_dir = templates_root / "no-pageerror-web"
    _write_valid_template_base(no_pageerror_dir)
    (no_pageerror_dir / "playwright.config.mjs").write_text(
        'export function guard(page) { page.on("console", (m) => { if (m.type() === "error") throw new Error(m.text()); }); }\n'
        'export default { testDir: "./tests", use: { headless: true, offline: true } };\n',
        encoding="utf-8",
    )
    with pytest.raises(ValueError):
        custom_scaffold.inspect_template("no-pageerror-web")

    # Case 3: Only pageerror listener without console.error listener
    no_console_dir = templates_root / "no-console-guard-web"
    _write_valid_template_base(no_console_dir)
    (no_console_dir / "playwright.config.mjs").write_text(
        'export function guard(page) { page.on("pageerror", (e) => { throw e; }); }\n'
        'export default { testDir: "./tests", use: { headless: true, offline: true } };\n',
        encoding="utf-8",
    )
    with pytest.raises(ValueError):
        custom_scaffold.inspect_template("no-console-guard-web")


def test_inspect_template_separately_rejects_package_dependencies_and_external_urls(
    tmp_path: Path,
) -> None:
    """F2B-04 (A2b-1, INV-16, INV-23): inspect_template rejects package dependencies/devDependencies/peerDependencies alone (kills M1) and external http(s) URLs alone (kills M2)."""
    templates_root = tmp_path / "templates"
    ws_mgr = WorkspaceManager(base_dir=tmp_path / "workspaces")
    scaffold = TemplateScaffold(workspace_manager=ws_mgr, templates_root=templates_root)

    # 1. Runtime dependency alone (clean HTML/CSS/Playwright) -> kills M1
    dep_dir = templates_root / "dep-only-web"
    _write_valid_template_base(dep_dir)
    (dep_dir / "package.json").write_text(
        json.dumps(
            {
                "name": "dep-only",
                "scripts": {"test": "node --test"},
                "dependencies": {"express": "4.19.2"},
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError):
        scaffold.inspect_template("dep-only-web")

    # 2. devDependencies alone -> fails today
    dev_dep_dir = templates_root / "dev-dep-web"
    _write_valid_template_base(dev_dep_dir)
    (dev_dep_dir / "package.json").write_text(
        json.dumps(
            {
                "name": "dev-dep-only",
                "scripts": {"test": "node --test"},
                "dependencies": {},
                "devDependencies": {"fastify": "4.28.0"},
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError):
        scaffold.inspect_template("dev-dep-web")

    # 3. peerDependencies alone -> fails today
    peer_dep_dir = templates_root / "peer-dep-web"
    _write_valid_template_base(peer_dep_dir)
    (peer_dep_dir / "package.json").write_text(
        json.dumps(
            {
                "name": "peer-dep-only",
                "scripts": {"test": "node --test"},
                "dependencies": {},
                "peerDependencies": {"pg": "8.11.0"},
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError):
        scaffold.inspect_template("peer-dep-web")

    # 4. External https:// URL alone in index.html (clean package.json) -> kills M2
    html_url_dir = templates_root / "html-https-only-web"
    _write_valid_template_base(html_url_dir)
    (html_url_dir / "index.html").write_text(
        '<!DOCTYPE html><html><body><script src="https://cdn.example.com/lib.js"></script></body></html>\n',
        encoding="utf-8",
    )
    with pytest.raises(ValueError):
        scaffold.inspect_template("html-https-only-web")

    # 5. External http:// URL alone in styles.css (clean package.json and index.html)
    css_url_dir = templates_root / "css-http-only-web"
    _write_valid_template_base(css_url_dir)
    (css_url_dir / "styles.css").write_text(
        '@import url("http://fonts.example.com/font.css");\n',
        encoding="utf-8",
    )
    with pytest.raises(ValueError):
        scaffold.inspect_template("css-http-only-web")


def test_template_scaffold_rejects_destination_symlink_src_or_prepopulated_tests(
    tmp_path: Path,
) -> None:
    """F2B-06 (A2b-2, INV-13, INV-18, INV-22): TemplateScaffold.scaffold rejects escaping symlink/junction at ws/src and rejects pre-existing files in ws/tests."""
    base_dir = tmp_path / "workspaces"
    ws_mgr = WorkspaceManager(base_dir=base_dir)
    scaffold = TemplateScaffold(workspace_manager=ws_mgr, templates_root=DEFAULT_TEMPLATES_ROOT)

    # 1. Destination ws_dir/src is a directory link pointing outside workspace (even when target is empty)
    empty_outside_dir = tmp_path / "empty_outside_src"
    empty_outside_dir.mkdir(parents=True, exist_ok=True)
    ws_src_link = ws_mgr.create_workspace("proj-dest-src-link")
    _create_dir_link(ws_src_link / "src", empty_outside_dir)

    with pytest.raises(SandboxViolationError):
        scaffold.scaffold(project_id="proj-dest-src-link", template_id="static-web")

    # 2. Destination ws_dir/tests already has pre-existing files
    ws_dirty_tests = ws_mgr.create_workspace("proj-dest-dirty-tests")
    (ws_dirty_tests / "tests").mkdir(parents=True, exist_ok=True)
    (ws_dirty_tests / "tests" / "preexisting.test.mjs").write_text(
        "// preexisting rogue test\n",
        encoding="utf-8",
    )

    with pytest.raises(SandboxViolationError):
        scaffold.scaffold(project_id="proj-dest-dirty-tests", template_id="static-web")

    # 3. Destination ws_dir/tests is a directory link pointing outside workspace (even when target is empty)
    empty_outside_tests = tmp_path / "empty_outside_tests"
    empty_outside_tests.mkdir(parents=True, exist_ok=True)
    ws_tests_link = ws_mgr.create_workspace("proj-dest-tests-link")
    _create_dir_link(ws_tests_link / "tests", empty_outside_tests)

    with pytest.raises(SandboxViolationError):
        scaffold.scaffold(project_id="proj-dest-tests-link", template_id="static-web")


def test_template_scaffold_defensive_errors_on_invalid_or_missing_template_and_malformed_package_json(
    tmp_path: Path,
) -> None:
    """F2B-07 (A2b-1, INV-22): Covers empty/backslash/missing template_id and malformed package.json branches."""
    templates_root = tmp_path / "templates"
    ws_mgr = WorkspaceManager(base_dir=tmp_path / "workspaces")
    scaffold = TemplateScaffold(workspace_manager=ws_mgr, templates_root=templates_root)

    with pytest.raises(SandboxViolationError):
        scaffold.inspect_template("")

    with pytest.raises(SandboxViolationError):
        scaffold.inspect_template("static\\web")

    with pytest.raises(FileNotFoundError):
        scaffold.inspect_template("does-not-exist")

    bad_json_dir = templates_root / "bad-json-web"
    _write_valid_template_base(bad_json_dir)
    (bad_json_dir / "package.json").write_text("{not valid json", encoding="utf-8")
    with pytest.raises(ValueError):
        scaffold.inspect_template("bad-json-web")


def test_template_scaffold_resolves_static_app_to_static_web_without_error(
    tmp_path: Path,
) -> None:
    """A2b-2 positive (INV-16, INV-20, INV-22, INV-23): TemplateScaffold resolves template_id='static-app' to 'static-web' when 'static-app' does not exist on disk."""
    base_dir = tmp_path / "workspaces"
    ws_mgr = WorkspaceManager(base_dir=base_dir)
    scaffold = TemplateScaffold(workspace_manager=ws_mgr, templates_root=DEFAULT_TEMPLATES_ROOT)

    # 1. inspect_template with "static-app" succeeds without FileNotFoundError
    inspection = scaffold.inspect_template("static-app")
    assert inspection.template_id == "static-app"
    assert "index.html" in inspection.files
    assert "package.json" in inspection.files
    assert "playwright.config.mjs" in inspection.files
    assert "styles.css" in inspection.files

    # 2. scaffold with "static-app" copies files from static-web into workspace without error
    ws_dir = scaffold.scaffold(project_id="proj-legacy-app", template_id="static-app")
    assert ws_dir == (base_dir / "proj-legacy-app").resolve()
    assert (ws_dir / "package.json").is_file()
    assert (ws_dir / "playwright.config.mjs").is_file()
    assert (ws_dir / "index.html").is_file()
    assert (ws_dir / "styles.css").is_file()
    assert (ws_dir / "src").is_dir()
    assert [p for p in (ws_dir / "src").rglob("*") if p.is_file()] == []

    # 3. If a distinct static-app directory actually exists, it uses it directly
    custom_templates = tmp_path / "custom_templates"
    custom_static_app = custom_templates / "static-app"
    _write_valid_template_base(custom_static_app)
    custom_scaffold = TemplateScaffold(workspace_manager=ws_mgr, templates_root=custom_templates)
    custom_inspection = custom_scaffold.inspect_template("static-app")
    assert custom_inspection.template_id == "static-app"
    ws_custom = custom_scaffold.scaffold(project_id="proj-custom-app", template_id="static-app")
    assert (ws_custom / "package.json").is_file()
