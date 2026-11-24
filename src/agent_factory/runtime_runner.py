"""Managed local app execution and HTTP smoke testing on 127.0.0.1 (INV-06, INV-19, INV-22)."""

from __future__ import annotations

import ast
import atexit
import hashlib
import logging
import os
import re
import socket
import subprocess
import sys
import threading
import time
import tomllib
import urllib.error
import urllib.parse
import urllib.request
import weakref
from collections import deque
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

from agent_factory.domain.models import validate_project_id
from agent_factory.secrets import redact_secrets, sanitize_subprocess_env
from agent_factory.workspace import (
    SandboxViolationError,
    WorkspaceManager,
    ensure_workspace_git_excludes,
)

logger = logging.getLogger(__name__)

LOOPBACK_HOST = "127.0.0.1"
DEFAULT_PORT_MIN = 8081
DEFAULT_PORT_MAX = 8199
DEFAULT_READINESS_TIMEOUT = 25.0

_EXTERNAL_OR_SKIP_PREFIXES: tuple[str, ...] = (
    "http://",
    "https://",
    "//",
    "data:",
    "blob:",
    "mailto:",
    "tel:",
    "javascript:",
    "#",
)
_ROOT_PYTHON_NAMES: tuple[str, ...] = (
    "app.py",
    "main.py",
    "server.py",
    "web.py",
    "run.py",
    "cli.py",
)
_PACKAGE_PYTHON_NAMES: tuple[str, ...] = (
    "app.py",
    "main.py",
    "server.py",
    "web.py",
    "run.py",
)
_IGNORED_SCAN_DIRS: frozenset[str] = frozenset(
    {
        ".git",
        "tests",
        "__pycache__",
        ".venv",
        "venv",
        ".agent-factory",
        "node_modules",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
    }
)
_MAIN_GUARD_RE = re.compile(r"""if\s+__name__\s*==\s*["']__main__["']\s*:""")
_WEB_SERVER_HINT_RE = re.compile(
    r"\b(?:HTTPServer|ThreadingHTTPServer|Flask|FastAPI|uvicorn|Bottle|Tornado|http\.server)\b"
)
_ASGI_APP_HINT_RE = re.compile(r"\b(?:FastAPI|Starlette)\s*\(")
_PORT_ENV_OR_FLAG_RE = re.compile(
    r"""(?:environ|getenv|env)\s*[\[(]\s*["']PORT["']|--port"""
)
_STATIC_SUBDIR_NAMES: tuple[str, ...] = ("src", "static", "public", "web")
_MAIN_EMPTY_RE = re.compile(r"<main[^>]*>\s*</main>", re.IGNORECASE)
_SRC_APP_REF_RE = re.compile(r"""src\s*=\s*["']\./src/app\.js["']""", re.IGNORECASE)


class LocalAppError(RuntimeError):
    """Raised when a generated project cannot be started or verified locally (INV-07)."""


class WorkspaceAppInfo(BaseModel):
    """Detected local runtime metadata for a project workspace."""

    app_type: Literal["static", "python_local", "unknown"] = "unknown"
    serve_dir: Path | None = None
    entrypoint: Path | None = None
    module_name: str | None = None
    pythonpath: Path | None = None


class LocalAppStatus(BaseModel):
    """Public status snapshot of a locally managed application process."""

    running: bool = False
    url: str | None = None
    lan_url: str | None = None
    app_type: str = "unknown"
    port: int | None = None

    def to_api_dict(self) -> dict[str, object]:
        """Serialize public API payload expected under local_app."""
        data: dict[str, object] = {
            "running": self.running,
            "url": self.url,
            "app_type": self.app_type,
        }
        if self.lan_url is not None:
            data["lan_url"] = self.lan_url
        return data


class HttpSmokeTestResult(BaseModel):
    """Result of starting an app locally and probing GET / and relative HTML assets."""

    passed: bool
    url: str | None = None
    summary: str = ""
    failed_resources: list[str] = Field(default_factory=list)


@dataclass
class _RunningAppProcess:
    project_id: str
    process: subprocess.Popen[str]
    port: int
    url: str
    lan_url: str | None
    app_type: str
    serve_dir: Path
    stderr_lines: deque[str] = field(default_factory=lambda: deque(maxlen=50))
    stderr_thread: threading.Thread | None = None


def detect_local_lan_ip() -> str | None:
    """Best-effort detection of non-loopback local LAN IPv4 address without contacting external hosts."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("10.255.255.255", 1))
            ip = str(sock.getsockname()[0] or "").strip()
            if ip and not ip.startswith("127.") and ip != "0.0.0.0":
                return ip
    except OSError as exc:
        logger.debug("UDP LAN IP probe fallback: %s", exc)
    try:
        _, _, addrs = socket.gethostbyname_ex(socket.gethostname())
        for addr in addrs:
            cand = str(addr).strip()
            if cand and "." in cand and not cand.startswith("127."):
                return cand
    except OSError as exc:
        logger.debug("Hostname LAN IP lookup fallback: %s", exc)
    return None


def _ensure_contained(path: Path, workspace_dir: Path) -> Path:
    """Validate path is non-symlink (if present) and resolves strictly inside workspace_dir (INV-22)."""
    ws_resolved = workspace_dir.resolve()
    if path.is_symlink():
        raise SandboxViolationError(
            f"Symlink rejected inside workspace '{ws_resolved}': {path}"
        )
    resolved = path.resolve()
    if resolved != ws_resolved and not resolved.is_relative_to(ws_resolved):
        raise SandboxViolationError(
            f"Path '{path}' escapes workspace '{ws_resolved}': {resolved}"
        )
    return resolved


def _is_template_index_html(html_path: Path, workspace_dir: Path) -> bool:
    """Return True if root index.html is still the untouched empty static-web template."""
    try:
        content = html_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False

    has_template_title = "<title>Static Web App</title>" in content
    has_empty_main = bool(_MAIN_EMPTY_RE.search(content))
    if not (has_template_title and has_empty_main):
        return False

    if _SRC_APP_REF_RE.search(content) and not (workspace_dir / "src" / "app.js").is_file():
        return True
    for rel_sub in _STATIC_SUBDIR_NAMES:
        if (workspace_dir / rel_sub / "index.html").is_file():
            return True
    return False


def _scan_workspace_files(
    ws: Path,
    *,
    max_depth: int = 3,
    suffix: str | None = None,
    filename: str | None = None,
) -> list[Path]:
    """Safely walk workspace_dir up to max_depth ignoring test/env/cache dirs (INV-22)."""
    results: list[Path] = []
    ws_resolved = ws.resolve()

    def _walk(current: Path, depth: int) -> None:
        try:
            entries = sorted(current.iterdir(), key=lambda p: (not p.is_file(), p.name.lower()))
        except OSError:
            return
        for entry in entries:
            if entry.is_symlink():
                _ensure_contained(entry, ws_resolved)
            if entry.is_dir():
                if entry.name in _IGNORED_SCAN_DIRS or entry.name.startswith("."):
                    continue
                if depth < max_depth:
                    _ensure_contained(entry, ws_resolved)
                    _walk(entry, depth + 1)
            elif entry.is_file():
                if filename is not None and entry.name != filename:
                    continue
                if suffix is not None and entry.suffix.lower() != suffix.lower():
                    continue
                results.append(_ensure_contained(entry, ws_resolved))

    _walk(ws_resolved, 0)
    return results


def resolve_static_root(workspace_dir: Path) -> Path | None:
    """Return built static directory containing index.html across root/src/static/public/web or subdirs (depth<=3)."""
    ws = Path(workspace_dir).resolve()
    if not ws.is_dir():
        return None

    root_index = ws / "index.html"
    has_root_index = root_index.exists() and _ensure_contained(root_index, ws).is_file()
    root_is_empty_template = has_root_index and _is_template_index_html(root_index, ws)

    subdir_candidates: list[Path] = []
    for rel in _STATIC_SUBDIR_NAMES:
        sub = ws / rel
        sub_idx = sub / "index.html"
        if sub.exists() and _ensure_contained(sub, ws).is_dir() and sub_idx.is_file():
            _ensure_contained(sub_idx, ws)
            subdir_candidates.append(sub.resolve())

    if not subdir_candidates:
        for idx_path in _scan_workspace_files(ws, max_depth=3, filename="index.html"):
            if idx_path.parent != ws:
                subdir_candidates.append(idx_path.parent)

    if subdir_candidates and (not has_root_index or root_is_empty_template):
        return subdir_candidates[0]
    if has_root_index:
        return ws
    if subdir_candidates:
        return subdir_candidates[0]
    return None


def _is_non_test_python(py_file: Path) -> bool:
    name = py_file.name.lower()
    return not (
        name.startswith("test_")
        or name.endswith("_test.py")
        or name in {"conftest.py", "setup.py", "__init__.py"}
    )


def _has_main_web_server(py_file: Path) -> bool:
    try:
        snippet = py_file.read_text(encoding="utf-8", errors="replace")[:16384]
    except OSError:
        return False
    return bool(_MAIN_GUARD_RE.search(snippet) and _WEB_SERVER_HINT_RE.search(snippet))


def _has_asgi_app(py_file: Path) -> bool:
    try:
        snippet = py_file.read_text(encoding="utf-8", errors="replace")[:16384]
    except OSError:
        return False
    return bool(_ASGI_APP_HINT_RE.search(snippet))


def _detect_fastapi_asgi_spec(entrypoint: Path) -> tuple[str | None, bool]:
    """Return (app_attr, launch_via_uvicorn) when entrypoint exposes a FastAPI/ASGI app needing uvicorn."""
    try:
        source = entrypoint.read_text(encoding="utf-8", errors="replace")
        tree = ast.parse(source, filename=str(entrypoint))
    except (OSError, SyntaxError, ValueError):
        return None, False

    uses_fastapi_or_uvicorn = False
    direct_asgi_attr: str | None = None
    fallback_asgi_attr: str | None = None

    for node in tree.body:
        if isinstance(node, ast.Import):
            for alias in node.names:
                top = alias.name.split(".", 1)[0].lower()
                if top in {"fastapi", "starlette", "uvicorn"}:
                    uses_fastapi_or_uvicorn = True
        elif isinstance(node, ast.ImportFrom) and node.module:
            top = node.module.split(".", 1)[0].lower()
            if top in {"fastapi", "starlette", "uvicorn"}:
                uses_fastapi_or_uvicorn = True

        target_name: str | None = None
        rhs: ast.expr | None = None
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            target_name = node.targets[0].id
            rhs = node.value
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.value is not None:
            target_name = node.target.id
            rhs = node.value

        if target_name and isinstance(rhs, ast.Call):
            fn = rhs.func
            fn_name = fn.id if isinstance(fn, ast.Name) else (fn.attr if isinstance(fn, ast.Attribute) else "")
            if fn_name in {"FastAPI", "Starlette"} and direct_asgi_attr is None:
                direct_asgi_attr = target_name
            elif target_name in {"app", "application", "api"} and fallback_asgi_attr is None:
                fallback_asgi_attr = target_name

    app_attr = direct_asgi_attr or (fallback_asgi_attr if uses_fastapi_or_uvicorn else None)
    if app_attr is None:
        return None, False

    has_main = bool(_MAIN_GUARD_RE.search(source))
    reads_port = bool(_PORT_ENV_OR_FLAG_RE.search(source))
    uses_uvicorn_run = "uvicorn.run" in source
    should_use_uvicorn = (not has_main) or (uses_uvicorn_run and not reads_port)
    return app_attr, should_use_uvicorn


def _resolve_asgi_launch_target(
    ws_dir: Path,
    app_info: WorkspaceAppInfo,
    asgi_attr: str,
) -> tuple[str, Path | None]:
    """Compute uvicorn '<module>:<attr>' target and effective PYTHONPATH inside ws_dir."""
    if app_info.module_name:
        return f"{app_info.module_name}:{asgi_attr}", app_info.pythonpath

    assert app_info.entrypoint is not None
    entry = app_info.entrypoint.resolve()
    rel = entry.relative_to(ws_dir)
    if len(rel.parts) == 1:
        return f"{rel.stem}:{asgi_attr}", app_info.pythonpath

    all_packages = True
    cur = ws_dir
    for part in rel.parts[:-1]:
        cur = cur / part
        if not (cur / "__init__.py").is_file():
            all_packages = False
            break
    if all_packages:
        return f"{'.'.join(rel.with_suffix('').parts)}:{asgi_attr}", app_info.pythonpath
    return f"{rel.stem}:{asgi_attr}", entry.parent


def _find_python_entrypoint(ws: Path) -> Path | None:
    """Recursively locate a contained Python server entrypoint up to depth 3 ignoring tests/caches."""
    for name in _ROOT_PYTHON_NAMES:
        cand = ws / name
        if cand.exists() and _ensure_contained(cand, ws).is_file():
            return _ensure_contained(cand, ws)

    priority_subpaths = [
        *(f"src/{name}" for name in _ROOT_PYTHON_NAMES),
        *(f"app/{name}" for name in _PACKAGE_PYTHON_NAMES),
    ]
    for rel in priority_subpaths:
        cand = ws / rel
        if cand.exists() and _ensure_contained(cand, ws).is_file():
            return _ensure_contained(cand, ws)

    scanned_py = _scan_workspace_files(ws, max_depth=3, suffix=".py")
    # 1. Explicit package/subdir server names ordered by shallowest depth
    pkg_matches = [
        p
        for p in scanned_py
        if p.name in _PACKAGE_PYTHON_NAMES and _is_non_test_python(p)
    ]
    if pkg_matches:
        pkg_matches.sort(key=lambda p: (len(p.relative_to(ws).parts), p.as_posix()))
        return pkg_matches[0]

    # 2. Any module with `if __name__ == "__main__":` + known web server framework/stdlib server
    main_matches = [
        p for p in scanned_py if _is_non_test_python(p) and _has_main_web_server(p)
    ]
    if main_matches:
        main_matches.sort(key=lambda p: (len(p.relative_to(ws).parts), p.as_posix()))
        return main_matches[0]

    # 2b. FastAPI/Starlette ASGI module without __main__ guard
    asgi_matches = [p for p in scanned_py if _is_non_test_python(p) and _has_asgi_app(p)]
    if asgi_matches:
        asgi_matches.sort(key=lambda p: (len(p.relative_to(ws).parts), p.as_posix()))
        return asgi_matches[0]

    # 3. Fallback when requirements.txt exists: first executable .py in workspace
    req_file = ws / "requirements.txt"
    if req_file.exists() and _ensure_contained(req_file, ws).is_file():
        non_test = [p for p in scanned_py if _is_non_test_python(p)]
        if non_test:
            non_test.sort(key=lambda p: (len(p.relative_to(ws).parts), p.as_posix()))
            return non_test[0]
    return None


def detect_workspace_app(workspace_dir: Path) -> WorkspaceAppInfo:
    """Detect app_type ('static', 'python_local', or 'unknown'), entrypoint, and package module inside workspace_dir."""
    ws = Path(workspace_dir).resolve()
    if not ws.is_dir():
        return WorkspaceAppInfo(app_type="unknown")

    py_entry = _find_python_entrypoint(ws)
    if py_entry is not None:
        module_name: str | None = None
        pythonpath: Path | None = None
        src_dir = (ws / "src").resolve()
        if src_dir.is_dir() and py_entry.is_relative_to(src_dir):
            rel_src = py_entry.relative_to(src_dir)
            if len(rel_src.parts) >= 2 and (py_entry.parent / "__init__.py").is_file():
                module_name = ".".join(rel_src.with_suffix("").parts)
                pythonpath = src_dir
        return WorkspaceAppInfo(
            app_type="python_local",
            serve_dir=ws,
            entrypoint=py_entry,
            module_name=module_name,
            pythonpath=pythonpath,
        )

    static_root = resolve_static_root(ws)
    if static_root is not None:
        return WorkspaceAppInfo(
            app_type="static",
            serve_dir=static_root,
            entrypoint=static_root / "index.html",
        )

    return WorkspaceAppInfo(app_type="unknown")


def _drain_stderr(proc: subprocess.Popen[str], lines: deque[str]) -> None:
    """Consume child combined stdout/stderr in background so buffers never stall."""
    pipe = proc.stdout if proc.stdout is not None else proc.stderr
    if pipe is None:
        return
    try:
        for line in iter(pipe.readline, ""):
            if not line:
                break
            lines.append(line.rstrip("\r\n"))
    except (OSError, ValueError):
        pass
    finally:
        try:
            pipe.close()
        except OSError:
            pass


def _terminate_process_tree(proc: subprocess.Popen[str]) -> None:
    """Forcefully terminate process tree on Windows (taskkill /T /F) or POSIX (INV-06)."""
    if proc.poll() is None:
        if os.name == "nt":
            try:
                subprocess.run(
                    ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                    capture_output=True,
                    check=False,
                    timeout=5.0,
                )
            except (OSError, subprocess.SubprocessError):
                pass
        else:
            try:
                proc.terminate()
            except OSError:
                pass

        try:
            proc.wait(timeout=3.0)
        except subprocess.TimeoutExpired:
            try:
                proc.kill()
                proc.wait(timeout=2.0)
            except (OSError, subprocess.SubprocessError):
                pass

    for pipe in (proc.stdout, proc.stderr):
        if pipe is not None:
            try:
                pipe.close()
            except OSError:
                pass


def _weak_atexit_cleanup(runner_ref: weakref.ref[LocalAppRunner]) -> None:
    runner = runner_ref()
    if runner is not None:
        runner.stop_all()


def _venv_python_path(venv_dir: Path) -> Path:
    """Return platform-appropriate python executable inside venv_dir."""
    if os.name == "nt":
        return venv_dir / "Scripts" / "python.exe"
    return venv_dir / "bin" / "python"


def _is_pyproject_dependency_manifest(pyproject_path: Path) -> bool:
    """Return True if pyproject.toml defines package metadata/dependencies rather than pytest/tool settings only."""
    try:
        data = tomllib.loads(pyproject_path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, tomllib.TOMLDecodeError):
        return False
    if not isinstance(data, dict):
        return False
    if "project" in data or "build-system" in data or "dependency-groups" in data:
        return True
    tool_tbl = data.get("tool", {})
    if isinstance(tool_tbl, dict):
        return any(k in tool_tbl for k in ("poetry", "pdm", "flit", "hatch"))
    return False


_IMPORT_TO_PYPI_PACKAGE: dict[str, str] = {
    "flask": "flask",
    "yt_dlp": "yt-dlp",
    "fastapi": "fastapi",
    "uvicorn": "uvicorn",
    "requests": "requests",
    "bs4": "beautifulsoup4",
    "pil": "pillow",
    "PIL": "pillow",
    "dotenv": "python-dotenv",
    "yaml": "pyyaml",
    "cv2": "opencv-python",
    "sklearn": "scikit-learn",
    "jwt": "pyjwt",
    "Crypto": "pycryptodome",
    "waitress": "waitress",
    "werkzeug": "werkzeug",
    "jinja2": "jinja2",
    "pydantic": "pydantic",
    "httpx": "httpx",
    "aiohttp": "aiohttp",
    "dateutil": "python-dateutil",
}


def _norm_pypi_name(name: str) -> str:
    """Normalize package names per PEP 503 for deduplication against manifests."""
    return re.sub(r"[-_.]+", "-", name.strip()).lower()


def _detect_workspace_third_party_packages(workspace_dir: Path) -> list[str]:
    """Inspect workspace .py files via AST (excluding tests/venvs/caches) and return sorted PyPI package names."""
    ws = Path(workspace_dir).resolve()
    if not ws.is_dir():
        return []

    py_files = [
        p
        for p in _scan_workspace_files(ws, max_depth=3, suffix=".py")
        if not (
            p.name.lower().startswith("test_")
            or p.name.lower().endswith("_test.py")
            or p.name.lower() in {"conftest.py", "setup.py"}
        )
    ]
    if not py_files:
        return []

    local_modules: set[str] = {"src"}
    for py_path in py_files:
        if py_path.stem != "__init__":
            local_modules.add(py_path.stem)
        rel_parts = py_path.relative_to(ws).parts
        for part in rel_parts[:-1]:
            local_modules.add(part)

    stdlib_names = frozenset(sys.stdlib_module_names) | frozenset(sys.builtin_module_names) | {"__future__"}
    discovered: set[str] = set()

    for py_path in py_files:
        try:
            source = py_path.read_text(encoding="utf-8", errors="replace")
            tree = ast.parse(source, filename=str(py_path))
        except (OSError, SyntaxError, ValueError):
            continue

        for node in ast.walk(tree):
            top_names: list[str] = []
            if isinstance(node, ast.Import):
                for alias in node.names:
                    top = alias.name.split(".", 1)[0].strip()
                    if top:
                        top_names.append(top)
            elif isinstance(node, ast.ImportFrom):
                if node.level and node.level > 0:
                    continue
                if node.module:
                    top = node.module.split(".", 1)[0].strip()
                    if top:
                        top_names.append(top)

            for top in top_names:
                if top in stdlib_names or top in local_modules:
                    continue
                mapped = (
                    _IMPORT_TO_PYPI_PACKAGE.get(top)
                    or _IMPORT_TO_PYPI_PACKAGE.get(top.lower())
                    or top.replace("_", "-").lower()
                )
                if mapped:
                    discovered.add(mapped)

    if "fastapi" in discovered:
        discovered.add("uvicorn")

    return sorted(discovered)


def _extract_declared_manifest_packages(
    req_files: Sequence[Path],
    pyproject_files: Sequence[Path],
) -> set[str]:
    """Extract PEP-503 normalized package names already declared in requirements*.txt / pyproject.toml."""
    declared: set[str] = set()
    for req_file in req_files:
        try:
            lines = req_file.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for raw_line in lines:
            line = raw_line.split("#", 1)[0].strip()
            if not line or line.startswith("-"):
                continue
            pkg = re.split(r"[=<>!~;\[\s@]", line, maxsplit=1)[0].strip()
            if pkg:
                declared.add(_norm_pypi_name(pkg))

    for pyproj in pyproject_files:
        try:
            data = tomllib.loads(pyproj.read_text(encoding="utf-8", errors="replace"))
        except (OSError, tomllib.TOMLDecodeError):
            continue
        if not isinstance(data, dict):
            continue
        proj = data.get("project", {})
        if isinstance(proj, dict):
            deps = list(proj.get("dependencies", []) or [])
            opt = proj.get("optional-dependencies", {})
            if isinstance(opt, dict):
                for vals in opt.values():
                    if isinstance(vals, list):
                        deps.extend(vals)
            for item in deps:
                pkg = re.split(r"[=<>!~;\[\s@]", str(item), maxsplit=1)[0].strip()
                if pkg:
                    declared.add(_norm_pypi_name(pkg))
    return declared


def _find_python_dependency_manifests(ws_dir: Path) -> tuple[list[Path], list[Path]]:
    """Locate contained requirements*.txt and dependency pyproject.toml files up to depth 3."""
    req_files: list[Path] = [
        p
        for p in _scan_workspace_files(ws_dir, max_depth=3, suffix=".txt")
        if p.name.lower().startswith("requirements") and p.name.lower().endswith(".txt")
    ]
    req_files.sort(key=lambda p: p.relative_to(ws_dir).as_posix())

    pyproject_files: list[Path] = [
        p
        for p in _scan_workspace_files(ws_dir, max_depth=3, filename="pyproject.toml")
        if _is_pyproject_dependency_manifest(p)
    ]
    pyproject_files.sort(key=lambda p: (len(p.relative_to(ws_dir).parts), p.relative_to(ws_dir).as_posix()))

    return req_files, pyproject_files


def _compute_deps_hash(
    ws_dir: Path,
    manifests: Sequence[Path],
    ast_packages: Sequence[str] = (),
) -> str:
    """Compute deterministic SHA-256 over manifests and sorted AST-detected packages."""
    hasher = hashlib.sha256()
    for manifest in sorted(manifests, key=lambda p: p.relative_to(ws_dir).as_posix()):
        rel = manifest.relative_to(ws_dir).as_posix().encode("utf-8")
        hasher.update(rel)
        hasher.update(b"\0")
        hasher.update(manifest.read_bytes())
        hasher.update(b"\0")
    if ast_packages:
        hasher.update(b"__ast__\0")
        hasher.update("\n".join(sorted(set(ast_packages))).encode("utf-8"))
        hasher.update(b"\0")
    return hasher.hexdigest()


class LocalAppRunner:
    """Thread-safe manager launching/stopping generated apps on 127.0.0.1 ports 8081..8199."""

    def __init__(
        self,
        workspace_manager: WorkspaceManager | None = None,
        *,
        host: str = LOOPBACK_HOST,
        port_min: int = DEFAULT_PORT_MIN,
        port_max: int = DEFAULT_PORT_MAX,
        secrets_to_redact: Sequence[str] = (),
        dep_command_runner: Callable[..., Any] | None = None,
        lan_mode: bool = False,
        lan_ip_resolver: Callable[[], str | None] | None = None,
    ) -> None:
        if host not in (LOOPBACK_HOST, "0.0.0.0"):
            raise ValueError(f"LocalAppRunner host must be '{LOOPBACK_HOST}' or '0.0.0.0', got '{host}'")
        if port_min < 1024 or port_max > 65535 or port_min > port_max:
            raise ValueError(f"Invalid port range {port_min}..{port_max}")
        self.workspace_manager = workspace_manager
        self.host = LOOPBACK_HOST
        self.lan_mode: bool = bool(lan_mode or host == "0.0.0.0")
        self._lan_ip_resolver: Callable[[], str | None] = (
            lan_ip_resolver if lan_ip_resolver is not None else detect_local_lan_ip
        )
        self.port_min = port_min
        self.port_max = port_max
        self.secrets_to_redact: tuple[str, ...] = tuple(secrets_to_redact)
        self._dep_command_runner: Callable[..., Any] = (
            dep_command_runner if dep_command_runner is not None else subprocess.run
        )
        self._lock = threading.RLock()
        self._dep_locks: dict[str, threading.Lock] = {}
        self._processes: dict[str, _RunningAppProcess] = {}
        atexit.register(_weak_atexit_cleanup, weakref.ref(self))

    def get_lan_mode(self) -> bool:
        """Return whether local apps currently bind to 0.0.0.0 for LAN access."""
        with self._lock:
            return self.lan_mode

    def set_lan_mode(self, enabled: bool) -> dict[str, Any]:
        """Toggle LAN 0.0.0.0 binding for subsequent local app starts."""
        flag = bool(enabled)
        with self._lock:
            self.lan_mode = flag
        lan_ip = self._lan_ip_resolver() if flag else None
        return {"lan_mode": flag, "lan_ip": lan_ip}

    def _resolve_workspace_dir(
        self, project_id: str, workspace_dir: Path | None = None
    ) -> Path:
        validate_project_id(project_id)
        if self.workspace_manager is not None:
            managed = self.workspace_manager.get_workspace_dir(project_id)
            if workspace_dir is not None and Path(workspace_dir).resolve() != managed:
                raise SandboxViolationError(
                    f"Workspace '{workspace_dir}' outside managed directory '{managed}' (INV-22)"
                )
            return managed
        if workspace_dir is None:
            raise ValueError(f"workspace_dir required when workspace_manager is unset ({project_id})")
        return Path(workspace_dir).resolve()

    def _prune_dead_locked(self) -> None:
        dead = [pid for pid, entry in self._processes.items() if entry.process.poll() is not None]
        for pid in dead:
            entry = self._processes.pop(pid)
            _terminate_process_tree(entry.process)

    def _allocate_free_port_locked(self) -> int:
        used_ports = {entry.port for entry in self._processes.values()}
        bind_target = "0.0.0.0" if self.lan_mode else self.host
        for port in range(self.port_min, self.port_max + 1):
            if port in used_ports:
                continue
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
                probe.settimeout(0.05)
                if probe.connect_ex((self.host, port)) == 0:
                    continue
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                if os.name == "nt" and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
                    sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
                elif os.name != "nt":
                    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                try:
                    sock.bind((bind_target, port))
                    return port
                except OSError:
                    continue
        raise LocalAppError(
            f"No free loopback ports available in {self.host}:{self.port_min}-{self.port_max}"
        )

    def _build_env(
        self,
        *,
        port: int,
        app_type: str,
        bind_host: str = LOOPBACK_HOST,
        pythonpath: Path | None = None,
        venv_dir: Path | None = None,
    ) -> dict[str, str]:
        clean = sanitize_subprocess_env()
        if app_type == "python_local":
            clean["PORT"] = str(port)
            clean["HOST"] = bind_host
            clean["PYTHONUNBUFFERED"] = "1"
            if venv_dir is not None:
                venv_bin = str(_venv_python_path(venv_dir).parent)
                existing_path = clean.get("PATH", "").strip()
                clean["PATH"] = (
                    f"{venv_bin}{os.pathsep}{existing_path}" if existing_path else venv_bin
                )
                clean["VIRTUAL_ENV"] = str(venv_dir)
            if pythonpath is not None:
                existing_pp = clean.get("PYTHONPATH", "").strip()
                clean["PYTHONPATH"] = (
                    f"{pythonpath}{os.pathsep}{existing_pp}" if existing_pp else str(pythonpath)
                )
        return clean

    def _redact(self, text: str) -> str:
        return redact_secrets(text, extra_secrets=self.secrets_to_redact)

    def _run_dep_command(
        self,
        cmd: Sequence[str],
        *,
        cwd: Path,
        env: Mapping[str, str],
        timeout_seconds: float,
        context: str,
    ) -> None:
        """Run isolated venv/pip command synchronously with timeout and redact any stderr on error."""
        try:
            res = self._dep_command_runner(
                [str(part) for part in cmd],
                cwd=str(cwd),
                env=dict(env),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout_seconds,
                check=False,
            )
        except subprocess.TimeoutExpired:
            raise LocalAppError(
                f"{context}: tiempo de espera agotado tras {timeout_seconds:.0f}s."
            ) from None
        except (OSError, ValueError) as exc:
            detail = self._redact(str(exc) or type(exc).__name__)
            raise LocalAppError(f"{context}: {detail}") from None

        if int(getattr(res, "exit_code", getattr(res, "returncode", 1))) != 0:
            raw_out = getattr(res, "stderr", "") or getattr(res, "stdout", "") or ""
            tail = " | ".join(line.strip() for line in str(raw_out).splitlines()[-6:] if line.strip())
            detail = self._redact(tail) if tail else "código de salida distinto de cero"
            raise LocalAppError(f"{context}: {detail}")

    def _prepare_python_interpreter(
        self,
        project_id: str,
        ws_dir: Path,
        *,
        install_deps: bool,
        dep_install_timeout: float,
    ) -> tuple[Path, Path | None]:
        """Ensure per-workspace .venv and cached dependency install from manifests + AST imports."""
        req_files, pyproject_files = _find_python_dependency_manifests(ws_dir)
        manifests: list[Path] = [*req_files, *pyproject_files]
        ast_packages = _detect_workspace_third_party_packages(ws_dir)
        raw_venv = ws_dir / ".venv"
        venv_dir = _ensure_contained(raw_venv, ws_dir)
        venv_py = _ensure_contained(_venv_python_path(venv_dir), ws_dir)

        if not manifests and not ast_packages:
            return Path(sys.executable), None

        if not install_deps:
            if venv_py.is_file():
                return venv_py, venv_dir
            return Path(sys.executable), None

        with self._lock:
            dep_lock = self._dep_locks.setdefault(project_id, threading.Lock())

        with dep_lock:
            target_hash = _compute_deps_hash(ws_dir, manifests, ast_packages=ast_packages)
            hash_file = _ensure_contained(venv_dir / ".deps_hash", ws_dir)
            if venv_py.is_file() and hash_file.is_file():
                try:
                    if hash_file.read_text(encoding="utf-8").strip() == target_hash:
                        return venv_py, venv_dir
                except OSError:
                    pass

            ensure_workspace_git_excludes(ws_dir)

            dep_env = sanitize_subprocess_env()
            dep_env["PIP_DISABLE_PIP_VERSION_CHECK"] = "1"
            dep_env["PIP_NO_INPUT"] = "1"
            dep_env["PYTHONUNBUFFERED"] = "1"
            dep_env["VIRTUAL_ENV"] = str(venv_dir)

            if not venv_py.is_file():
                self._run_dep_command(
                    [sys.executable, "-m", "venv", str(venv_dir)],
                    cwd=ws_dir,
                    env=dep_env,
                    timeout_seconds=min(dep_install_timeout, 120.0),
                    context=f"No se pudo crear el entorno virtual en '{project_id}'",
                )
                venv_py = _ensure_contained(_venv_python_path(venv_dir), ws_dir)
                if not venv_py.is_file():
                    raise LocalAppError(
                        f"El entorno virtual '{venv_dir}' no generó el intérprete '{venv_py.name}'"
                    )

            existing_path = dep_env.get("PATH", "").strip()
            venv_bin = str(venv_py.parent)
            dep_env["PATH"] = f"{venv_bin}{os.pathsep}{existing_path}" if existing_path else venv_bin

            for req_file in req_files:
                rel_req = req_file.relative_to(ws_dir).as_posix()
                self._run_dep_command(
                    [str(venv_py), "-m", "pip", "install", "-r", str(req_file)],
                    cwd=ws_dir,
                    env=dep_env,
                    timeout_seconds=dep_install_timeout,
                    context=f"No se pudieron instalar dependencias desde '{rel_req}'",
                )

            if pyproject_files and not req_files:
                for pyproj in pyproject_files:
                    pkg_target = (
                        "."
                        if pyproj.parent == ws_dir
                        else pyproj.parent.relative_to(ws_dir).as_posix()
                    )
                    self._run_dep_command(
                        [str(venv_py), "-m", "pip", "install", "-e", pkg_target],
                        cwd=ws_dir,
                        env=dep_env,
                        timeout_seconds=dep_install_timeout,
                        context="No se pudieron instalar dependencias desde 'pyproject.toml'",
                    )

            declared_norm = _extract_declared_manifest_packages(req_files, pyproject_files)
            missing_ast_pkgs = [
                pkg for pkg in ast_packages if _norm_pypi_name(pkg) not in declared_norm
            ]
            if missing_ast_pkgs:
                self._run_dep_command(
                    [str(venv_py), "-m", "pip", "install", *missing_ast_pkgs],
                    cwd=ws_dir,
                    env=dep_env,
                    timeout_seconds=dep_install_timeout,
                    context="No se pudieron instalar dependencias detectadas por AST",
                )

            hash_file.parent.mkdir(parents=True, exist_ok=True)
            hash_file.write_text(target_hash, encoding="utf-8")
            return venv_py, venv_dir

    def get_status(
        self,
        project_id: str,
        *,
        workspace_dir: Path | None = None,
    ) -> LocalAppStatus:
        """Return current running status, loopback URL, and detected app_type for project_id."""
        ws_dir = self._resolve_workspace_dir(project_id, workspace_dir)
        detected_type = detect_workspace_app(ws_dir).app_type if ws_dir.is_dir() else "unknown"
        with self._lock:
            self._prune_dead_locked()
            entry = self._processes.get(project_id)
            if entry is not None and entry.process.poll() is None:
                return LocalAppStatus(
                    running=True,
                    url=entry.url,
                    lan_url=entry.lan_url,
                    app_type=entry.app_type,
                    port=entry.port,
                )
        return LocalAppStatus(running=False, url=None, lan_url=None, app_type=detected_type, port=None)

    def _collect_process_diagnostic(self, project_id: str, entry: _RunningAppProcess) -> str:
        """Stop process tree to flush output pipes and return redacted trailing stdout/stderr lines."""
        self.stop(project_id)
        if entry.stderr_thread is not None and entry.stderr_thread.is_alive():
            entry.stderr_thread.join(timeout=1.0)
        useful = [line.strip() for line in entry.stderr_lines if line and line.strip()]
        if not useful:
            return ""
        return self._redact(" | ".join(useful[-10:]))

    def start(
        self,
        project_id: str,
        *,
        workspace_dir: Path | None = None,
        readiness_timeout: float = DEFAULT_READINESS_TIMEOUT,
        install_deps: bool = False,
        dep_install_timeout: float = 180.0,
    ) -> LocalAppStatus:
        """Start project_id app on a free 127.0.0.1 (or 0.0.0.0 in lan_mode) port (8081..8199) and wait until ready."""
        ws_dir = self._resolve_workspace_dir(project_id, workspace_dir)
        if not ws_dir.is_dir():
            raise LocalAppError(f"Workspace directory for '{project_id}' does not exist")

        app_info = detect_workspace_app(ws_dir)
        if app_info.app_type == "unknown" or app_info.serve_dir is None:
            raise LocalAppError(
                f"No runnable static index.html or Python entrypoint found in workspace '{project_id}'"
            )

        with self._lock:
            self._prune_dead_locked()
            existing = self._processes.get(project_id)
            if existing is not None and existing.process.poll() is None:
                return LocalAppStatus(
                    running=True,
                    url=existing.url,
                    lan_url=existing.lan_url,
                    app_type=existing.app_type,
                    port=existing.port,
                )

        python_exe = Path(sys.executable)
        venv_dir: Path | None = None
        if app_info.app_type == "python_local":
            python_exe, venv_dir = self._prepare_python_interpreter(
                project_id,
                ws_dir,
                install_deps=install_deps,
                dep_install_timeout=dep_install_timeout,
            )

        with self._lock:
            self._prune_dead_locked()
            existing = self._processes.get(project_id)
            if existing is not None and existing.process.poll() is None:
                return LocalAppStatus(
                    running=True,
                    url=existing.url,
                    lan_url=existing.lan_url,
                    app_type=existing.app_type,
                    port=existing.port,
                )

            port = self._allocate_free_port_locked()
            bind_host = "0.0.0.0" if self.lan_mode else self.host
            url = f"http://{self.host}:{port}"
            lan_ip = self._lan_ip_resolver() if self.lan_mode else None
            lan_url = f"http://{lan_ip}:{port}" if (self.lan_mode and lan_ip) else None

            eff_pythonpath = app_info.pythonpath
            if app_info.app_type == "static":
                cmd = [sys.executable, "-m", "http.server", str(port), "--bind", bind_host]
                cwd = app_info.serve_dir
            else:
                asgi_attr: str | None = None
                use_uvicorn = False
                if app_info.entrypoint is not None:
                    asgi_attr, use_uvicorn = _detect_fastapi_asgi_spec(app_info.entrypoint)
                if use_uvicorn and asgi_attr is not None:
                    asgi_target, resolved_pp = _resolve_asgi_launch_target(
                        ws_dir, app_info, asgi_attr
                    )
                    eff_pythonpath = resolved_pp
                    cmd = [
                        str(python_exe),
                        "-m",
                        "uvicorn",
                        asgi_target,
                        "--host",
                        bind_host,
                        "--port",
                        str(port),
                    ]
                    cwd = ws_dir
                elif app_info.module_name:
                    cmd = [str(python_exe), "-m", app_info.module_name]
                    cwd = ws_dir
                else:
                    assert app_info.entrypoint is not None
                    rel_entry = app_info.entrypoint.resolve().relative_to(ws_dir)
                    cmd = [str(python_exe), str(rel_entry)]
                    cwd = ws_dir

            sub_env = self._build_env(
                port=port,
                app_type=app_info.app_type,
                bind_host=bind_host,
                pythonpath=eff_pythonpath,
                venv_dir=venv_dir,
            )

            try:
                proc = subprocess.Popen(
                    cmd,
                    cwd=str(cwd),
                    env=sub_env,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                )
            except (OSError, ValueError) as exc:
                err_msg = self._redact(str(exc) or type(exc).__name__)
                raise LocalAppError(f"Failed to start local app '{project_id}': {err_msg}") from None

            stderr_lines: deque[str] = deque(maxlen=80)
            reader = threading.Thread(
                target=_drain_stderr,
                args=(proc, stderr_lines),
                name=f"local-app-stderr-{project_id}",
                daemon=True,
            )
            reader.start()
            entry = _RunningAppProcess(
                project_id=project_id,
                process=proc,
                port=port,
                url=url,
                lan_url=lan_url,
                app_type=app_info.app_type,
                serve_dir=cwd,
                stderr_lines=stderr_lines,
                stderr_thread=reader,
            )
            self._processes[project_id] = entry

        deadline = time.monotonic() + max(0.5, readiness_timeout)
        while time.monotonic() < deadline:
            exit_code = proc.poll()
            if exit_code is not None:
                diag = self._collect_process_diagnostic(project_id, entry)
                detail = diag if diag else f"exit code {exit_code}"
                raise LocalAppError(
                    f"La app local '{project_id}' se detuvo al iniciar (código {exit_code}): {detail}"
                )
            try:
                with socket.create_connection((self.host, port), timeout=0.2):
                    return LocalAppStatus(
                        running=True,
                        url=url,
                        lan_url=lan_url,
                        app_type=app_info.app_type,
                        port=port,
                    )
            except OSError:
                time.sleep(0.05)

        diag = self._collect_process_diagnostic(project_id, entry)
        detail = f": {diag}" if diag else " (sin salida en stderr/stdout)"
        raise LocalAppError(
            f"Local app '{project_id}' failed to listen on {url} within {readiness_timeout:.1f}s{detail}"
        )

    def stop(self, project_id: str, *, workspace_dir: Path | None = None) -> LocalAppStatus:
        """Stop running local app process tree for project_id cleanly and release port."""
        validate_project_id(project_id)
        with self._lock:
            entry = self._processes.pop(project_id, None)
        if entry is not None:
            _terminate_process_tree(entry.process)
            if entry.stderr_thread is not None and entry.stderr_thread.is_alive():
                entry.stderr_thread.join(timeout=1.0)

        detected_type = "unknown"
        if self.workspace_manager is not None:
            ws_dir = self.workspace_manager.get_workspace_dir(project_id)
            if ws_dir.is_dir():
                detected_type = detect_workspace_app(ws_dir).app_type
        elif workspace_dir is not None and Path(workspace_dir).is_dir():
            detected_type = detect_workspace_app(Path(workspace_dir)).app_type
        return LocalAppStatus(running=False, url=None, app_type=detected_type, port=None)

    def stop_all(self) -> None:
        """Terminate all running managed local app processes without raising."""
        with self._lock:
            entries = list(self._processes.values())
            self._processes.clear()
        for entry in entries:
            try:
                _terminate_process_tree(entry.process)
                if entry.stderr_thread is not None and entry.stderr_thread.is_alive():
                    entry.stderr_thread.join(timeout=1.0)
            except (OSError, RuntimeError, subprocess.SubprocessError):
                pass


class _RelativeAssetHTMLParser(HTMLParser):
    """Collect relative local script[src] and link[href] URLs from HTML."""

    def __init__(self) -> None:
        super().__init__()
        self.raw_refs: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag_low = tag.lower()
        attr_map = {k.lower(): v for k, v in attrs if v is not None}
        candidate: str | None = None
        if tag_low == "script":
            candidate = attr_map.get("src")
        elif tag_low == "link":
            candidate = attr_map.get("href")
        if candidate is None:
            return
        cleaned = candidate.strip()
        if not cleaned:
            return
        low = cleaned.lower()
        if low.startswith(_EXTERNAL_OR_SKIP_PREFIXES):
            return
        self.raw_refs.append(cleaned)


def _fetch_local_http(url: str, timeout_seconds: float = 3.0) -> tuple[int, str, bytes]:
    """Perform direct loopback HTTP GET bypassing host proxy settings."""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    req = urllib.request.Request(url, method="GET")
    with opener.open(req, timeout=timeout_seconds) as resp:
        status_code = int(resp.status)
        content_type = str(resp.headers.get("Content-Type", "")).lower()
        body = resp.read(1024 * 1024)
        return status_code, content_type, body


def run_http_smoke_test(
    app_runner: LocalAppRunner,
    project_id: str,
    workspace_dir: Path,
    *,
    timeout_seconds: float = DEFAULT_READINESS_TIMEOUT,
    install_deps: bool = False,
) -> HttpSmokeTestResult:
    """Start project_id on 127.0.0.1, probe GET / and relative script/link resources, and stop cleanly."""
    ws_dir = Path(workspace_dir).resolve()
    app_info = detect_workspace_app(ws_dir)
    if app_info.app_type == "unknown":
        return HttpSmokeTestResult(
            passed=True,
            summary="No runnable web/server entrypoint detected; smoke test skipped.",
        )

    started_url: str | None = None
    try:
        try:
            started = app_runner.start(
                project_id,
                workspace_dir=ws_dir,
                readiness_timeout=timeout_seconds,
                install_deps=install_deps,
            )
            started_url = started.url
        except (LocalAppError, SandboxViolationError, ValueError, OSError) as exc:
            clean_err = app_runner._redact(str(exc))
            return HttpSmokeTestResult(
                passed=False,
                url=started_url,
                summary=f"HTTP Smoke Test failed to start app: {clean_err}",
            )

        assert started_url is not None
        root_url = f"{started_url}/"
        try:
            root_code, content_type, root_bytes = _fetch_local_http(root_url)
        except urllib.error.HTTPError as http_err:
            http_err.close()
            return HttpSmokeTestResult(
                passed=False,
                url=started_url,
                summary=f"HTTP Smoke Test failed: GET / returned HTTP {http_err.code}",
                failed_resources=[f"/ (HTTP {http_err.code})"],
            )
        except (urllib.error.URLError, OSError, ValueError) as exc:
            return HttpSmokeTestResult(
                passed=False,
                url=started_url,
                summary=f"HTTP Smoke Test failed requesting GET /: {app_runner._redact(str(exc))}",
            )

        if root_code != 200:
            return HttpSmokeTestResult(
                passed=False,
                url=started_url,
                summary=f"HTTP Smoke Test failed: GET / returned HTTP {root_code}",
                failed_resources=[f"/ (HTTP {root_code})"],
            )

        root_text = root_bytes.decode("utf-8", errors="replace")
        is_html = "text/html" in content_type or root_text.lstrip().startswith("<")
        if not is_html:
            return HttpSmokeTestResult(
                passed=True,
                url=started_url,
                summary=f"HTTP Smoke Test passed: GET {root_url} returned HTTP 200.",
            )

        parser = _RelativeAssetHTMLParser()
        parser.feed(root_text)
        parser.close()

        failed_resources: list[str] = []
        seen_urls: set[str] = set()
        expected_netloc = urllib.parse.urlsplit(root_url).netloc

        for ref in parser.raw_refs:
            resolved = urllib.parse.urljoin(root_url, ref)
            parsed_resolved = urllib.parse.urlsplit(resolved)
            if parsed_resolved.netloc != expected_netloc:
                continue
            if resolved in seen_urls:
                continue
            seen_urls.add(resolved)

            try:
                asset_code, _, _ = _fetch_local_http(resolved)
                if asset_code != 200:
                    failed_resources.append(f"{ref} (HTTP {asset_code})")
            except urllib.error.HTTPError as http_err:
                failed_resources.append(f"{ref} (HTTP {http_err.code})")
                http_err.close()
            except (urllib.error.URLError, OSError, ValueError) as exc:
                failed_resources.append(f"{ref} ({app_runner._redact(str(exc))})")

        if failed_resources:
            joined = ", ".join(failed_resources)
            return HttpSmokeTestResult(
                passed=False,
                url=started_url,
                summary=f"HTTP Smoke Test failed on {root_url}: missing or broken assets: {joined}",
                failed_resources=failed_resources,
            )

        return HttpSmokeTestResult(
            passed=True,
            url=started_url,
            summary=f"HTTP Smoke Test passed: GET / and {len(seen_urls)} local asset(s) returned HTTP 200.",
        )
    finally:
        app_runner.stop(project_id, workspace_dir=ws_dir)
