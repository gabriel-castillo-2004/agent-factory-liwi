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
