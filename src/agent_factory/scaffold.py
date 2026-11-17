"""Static template inspection and isolated workspace scaffolding (Phase 2b, INV-16, INV-20, INV-22, A2b-1, A2b-2)."""

from pathlib import Path

from pydantic import BaseModel, Field

from agent_factory.workspace import WorkspaceManager

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

    def inspect_template(self, template_id: str = "static-web") -> TemplateInspection:
        """Inspect and validate a template directory for required files, offline rules, and empty src/."""
        raise NotImplementedError

    def scaffold(
        self,
        project_id: str,
        template_id: str = "static-web",
    ) -> Path:
        """Copy validated template files into workspaces/<project_id>/ via WorkspaceManager."""
        raise NotImplementedError
