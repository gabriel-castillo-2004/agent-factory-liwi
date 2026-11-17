"""Static template inspection and isolated workspace scaffolding (Phase 2b, INV-16, INV-20, INV-22, A2b-1, A2b-2)."""

import json
from pathlib import Path, PureWindowsPath

from pydantic import BaseModel, Field

from agent_factory.workspace import SandboxViolationError, WorkspaceManager

DEFAULT_TEMPLATES_ROOT: Path = Path(__file__).resolve().parents[2] / "templates"

REQUIRED_STATIC_WEB_FILES: tuple[str, ...] = (
    "package.json",
    "playwright.config.mjs",
    "index.html",
    "styles.css",
)


class TemplateInspection(BaseModel):
    """Deterministic inspection report for a static application template (A2b-1)."""

    template_id: str
    files: list[str] = Field(default_factory=list)
    package_name: str = ""
    scripts: dict[str, str] = Field(default_factory=dict)
    src_files: list[str] = Field(default_factory=list)
    external_or_backend_refs: list[str] = Field(default_factory=list)


class TemplateScaffold:
    """Validates static templates and scaffolds them into workspaces/<project_id>/ via WorkspaceManager."""

    def __init__(
        self,
        workspace_manager: WorkspaceManager,
        templates_root: Path = DEFAULT_TEMPLATES_ROOT,
    ) -> None:
        self.workspace_manager = workspace_manager
        self.templates_root = Path(templates_root).resolve()

    def _resolve_template_dir(self, template_id: str) -> Path:
        """Resolve and validate template directory within templates_root, rejecting traversal and symlink escapes."""
        if not template_id or not isinstance(template_id, str):
            raise SandboxViolationError(f"Invalid template_id '{template_id}'")

        if "\\" in template_id:
            raise SandboxViolationError(f"Backslash rejected in template_id '{template_id}'")

        win_path = PureWindowsPath(template_id)
        if (
            any(part == ".." for part in win_path.parts)
            or win_path.is_absolute()
            or template_id.startswith(("/", "\\"))
        ):
            raise SandboxViolationError(
                f"Traversal or absolute path rejected in template_id '{template_id}'"
            )

        tpl_dir = (self.templates_root / template_id).resolve()
        if not tpl_dir.is_relative_to(self.templates_root) or tpl_dir == self.templates_root:
            raise SandboxViolationError(
                f"Template '{template_id}' escapes templates root '{self.templates_root}'"
            )
        if not tpl_dir.is_dir():
            raise FileNotFoundError(f"Template directory '{template_id}' not found at '{tpl_dir}'")
        return tpl_dir

    def inspect_template(self, template_id: str = "static-web") -> TemplateInspection:
        """Inspect and validate a template directory for required files, offline rules, and empty src/."""
        tpl_dir = self._resolve_template_dir(template_id)

        # Check for any symlink/junction inside tpl_dir escaping templates_root or tpl_dir
        for p in tpl_dir.rglob("*"):
            resolved = p.resolve()
            if not resolved.is_relative_to(tpl_dir) or not resolved.is_relative_to(
                self.templates_root
            ):
                raise SandboxViolationError(
                    f"Template contains symlink/junction '{p}' escaping template directory '{tpl_dir}': {resolved}"
                )

        # Check for pre-existing files in src/
        src_dir = tpl_dir / "src"
        src_files: list[str] = []
        if src_dir.is_dir():
            src_files = sorted(
                p.relative_to(tpl_dir).as_posix()
                for p in src_dir.rglob("*")
                if p.is_file()
            )
        if src_files:
            raise SandboxViolationError(
                f"Template '{template_id}' contains production files in src/ prior to test generation: {src_files}"
            )

        # Check required files
        for req in REQUIRED_STATIC_WEB_FILES:
            req_path = tpl_dir / req
            if not req_path.is_file():
                raise FileNotFoundError(
                    f"Template '{template_id}' is missing required file '{req}'"
                )

        # Collect all non-src files
        files = sorted(
            p.relative_to(tpl_dir).as_posix()
            for p in tpl_dir.rglob("*")
            if p.is_file() and not p.relative_to(tpl_dir).as_posix().startswith("src/")
        )

        # Parse package.json
        pkg_file = tpl_dir / "package.json"
        try:
            pkg_data = json.loads(pkg_file.read_text(encoding="utf-8"))
        except Exception as exc:
            raise ValueError(f"Invalid package.json in template '{template_id}': {exc}") from exc

        package_name = str(pkg_data.get("name", ""))
        scripts = {str(k): str(v) for k, v in pkg_data.get("scripts", {}).items()}
        dependencies = pkg_data.get("dependencies", {})

        external_or_backend_refs: list[str] = []
        if dependencies:
            for dep in sorted(dependencies.keys()):
                external_or_backend_refs.append(f"package.json dependency: {dep}")

        # Scan text files for external network protocols
        for f_rel in files:
            file_path = tpl_dir / f_rel
            try:
                content = file_path.read_text(encoding="utf-8")
                if any(proto in content for proto in ("http://", "https://", "ws://", "wss://")):
                    external_or_backend_refs.append(f"{f_rel}: external network URL")
            except (UnicodeDecodeError, OSError):
                pass

        if external_or_backend_refs:
            raise ValueError(
                f"Template '{template_id}' contains external network or backend references: {external_or_backend_refs}"
            )

        return TemplateInspection(
            template_id=template_id,
            files=files,
            package_name=package_name,
            scripts=scripts,
            src_files=src_files,
            external_or_backend_refs=external_or_backend_refs,
        )

    def scaffold(
        self,
        project_id: str,
        template_id: str = "static-web",
    ) -> Path:
        """Copy validated template files into workspaces/<project_id>/ via WorkspaceManager."""
        ws_dir = self.workspace_manager.get_workspace_dir(project_id)
        inspection = self.inspect_template(template_id)

        # Check destination workspace for contamination in src/
        src_dir = ws_dir / "src"
        if src_dir.is_dir() and any(p.is_file() for p in src_dir.rglob("*")):
            raise SandboxViolationError(
                f"Destination workspace '{project_id}' already contains files in src/"
            )

        ws_dir.mkdir(parents=True, exist_ok=True)
        tpl_dir = self._resolve_template_dir(template_id)

        for rel_file in inspection.files:
            source_file = tpl_dir / rel_file
            content = source_file.read_text(encoding="utf-8")
            self.workspace_manager.write_file(project_id, rel_file, content)

        # Ensure src/ exists and is empty
        (ws_dir / "src").mkdir(parents=True, exist_ok=True)
        return ws_dir
