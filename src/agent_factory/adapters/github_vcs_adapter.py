"""Real GitHub VCS and CI adapter delegating commands to Runner with clean env (INV-02, INV-07, INV-19, INV-22)."""

from __future__ import annotations

import base64
import json
import os
import re
import shutil
import unicodedata
import urllib.parse
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any

from agent_factory.adapters.subprocess_runner import SubprocessRunner
from agent_factory.domain.models import validate_project_id
from agent_factory.ports.notifier import TelegramTransportError
from agent_factory.ports.runner import CommandResult, CommandTimeoutError, Runner
from agent_factory.ports.vcs import VCS, CIRunResult
from agent_factory.runtime_runner import detect_workspace_app
from agent_factory.secrets import redact_secrets, sanitize_subprocess_env
from agent_factory.workspace import ensure_workspace_git_excludes

if TYPE_CHECKING:
    from agent_factory.adapters.static_deploy_publisher import StaticDeployPublisher


class VcsError(RuntimeError):
    """Raised when a Git/GitHub operation fails, with all secrets redacted (INV-07)."""


def build_git_push_env(
    clean_env: Mapping[str, str],
    token: str,
    repo_base_url: str = "https://github.com",
) -> tuple[dict[str, str], tuple[str, ...]]:
    """Build ephemeral Git auth env using GIT_CONFIG_COUNT without writing tokens to .git/config or argv (INV-19)."""
    push_env = dict(clean_env)
    push_env["GIT_TERMINAL_PROMPT"] = "0"
    cleaned_tok = token.strip()
    base = repo_base_url.rstrip("/")
    if not cleaned_tok or not base.startswith(("http://", "https://")):
        return push_env, ()

    b64_auth = base64.b64encode(f"x-access-token:{cleaned_tok}".encode()).decode("ascii")
    push_env["GIT_CONFIG_COUNT"] = "1"
    push_env["GIT_CONFIG_KEY_0"] = f"http.{base}/.extraheader"
    push_env["GIT_CONFIG_VALUE_0"] = f"Authorization: basic {b64_auth}"
    return push_env, (cleaned_tok, b64_auth)


def is_local_remote_url(url: str) -> bool:
    """Return True if remote URL refers to a local directory or file:// URI rather than a network host."""
    low = url.strip().lower()
    if not low:
        return False
    if low.startswith("file://"):
        return True
    if low.startswith(("http://", "https://", "ssh://", "git://")):
        return False
    return not ("@" in low and ":" in low and not (len(low) >= 2 and low[1] == ":" and low[0].isalpha()))


def _workspace_has_github_workflows(workspace_dir: Path) -> bool:
    """Return True if workspace contains any GitHub Actions workflow YAML definition."""
    wf_dir = workspace_dir / ".github" / "workflows"
    if not wf_dir.is_dir():
        return False
    return any(p.is_file() and p.suffix.lower() in (".yml", ".yaml") for p in wf_dir.iterdir())


def _validate_owner(owner: str) -> str:
    """Validate GitHub owner/org identifier preventing path/URL injection."""
    clean = owner.strip()
    if (
        not clean
        or clean.startswith("-")
        or any(ch.isspace() for ch in clean)
        or any(bad in clean for bad in ("..", "/", "\\", "@", "?", "#"))
    ):
        raise ValueError(f"Invalid repository owner '{owner}'")
    return clean


def _validate_repo_segment(repo: str) -> str:
    """Validate GitHub repository name preventing path traversal or option injection."""
    clean = repo.strip()
    if (
        not clean
        or clean.startswith("-")
        or any(ch.isspace() for ch in clean)
        or any(bad in clean for bad in ("..", "/", "\\", "@", "?", "#", ":"))
    ):
        raise ValueError(f"Invalid repository name '{repo}'")
    return clean


def slugify_repo_name(title: str, max_len: int = 63) -> str:
    """Convert arbitrary title text into a clean lowercase ASCII repository slug without double hyphens."""
    normalized = unicodedata.normalize("NFKD", str(title or "")).encode("ascii", "ignore").decode("ascii")
    slug = re.sub(r"[^a-z0-9]+", "-", normalized.lower()).strip("-")
    if len(slug) > max_len:
        slug = slug[:max_len].strip("-")
    return slug


class GitHubVCSAdapter(VCS):
    """Executes isolated workspace Git and GitHub workflows via Runner/API with clean env."""

    def __init__(
        self,
        runner: Runner | None = None,
        *,
        owner: str = "agent-factory",
        repo_base_url: str = "https://github.com",
        deploy_base_url: str = "",
        default_branch: str = "main",
        github_token: str = "",
        secrets: Sequence[str] = (),
        deploy_publisher: StaticDeployPublisher | None = None,
        timeout_seconds: float = 30.0,
        api_base_url: str = "https://api.github.com",
        http_transport: Any | None = None,
    ) -> None:
        self.runner: Runner = (
            runner
            if runner is not None
            else SubprocessRunner(default_timeout_seconds=timeout_seconds)
        )
        self.owner = owner.strip()
        self._owner_is_default = self.owner in ("", "agent-factory")
        self._authenticated_login: str = ""
        self.repo_base_url = repo_base_url.rstrip("/")
        self._auto_deploy_base_url = not bool(deploy_base_url.strip())
        self.deploy_base_url = (
            deploy_base_url.rstrip("/")
            if deploy_base_url.strip()
            else (f"https://{self.owner}.github.io" if self.owner else "")
        )
        self.default_branch = default_branch
        effective_token = (
            github_token.strip()
            or os.environ.get("GITHUB_TOKEN", "").strip()
            or os.environ.get("GH_TOKEN", "").strip()
        )
        self.github_token = effective_token
        combined_secrets = list(secrets)
        if effective_token and effective_token not in combined_secrets:
            combined_secrets.append(effective_token)
        self.secrets: tuple[str, ...] = tuple(combined_secrets)
        self.deploy_publisher = deploy_publisher
        self.timeout_seconds = timeout_seconds
        self.api_base_url = api_base_url.rstrip("/")
        self.http_transport = http_transport
        self._last_workspace_dir: dict[str, Path] = {}
        self._local_only_projects: set[str] = set()

    def build_clean_env(self, host_env: Mapping[str, str] | None = None) -> dict[str, str]:
        """Build sanitized subprocess env keeping system vars and stripping unrelated secrets (INV-19)."""
        clean = sanitize_subprocess_env(host_env=host_env)
        if self.github_token:
            clean["GITHUB_TOKEN"] = self.github_token
            clean["GH_TOKEN"] = self.github_token
        return clean

    def _is_fake_runner(self) -> bool:
        return self.runner.__class__.__name__ == "FakeRunner"

    def _is_real_subprocess_runner(self) -> bool:
        return isinstance(self.runner, SubprocessRunner)

    def _validate_branch(self, branch: str) -> str:
        """Validate branch name preventing flag injection and path traversal."""
        b = branch.strip()
        if (
            not b
            or b.startswith("-")
            or any(ch.isspace() for ch in b)
            or ".." in b
            or ":" in b
            or "\\" in b
        ):
            raise ValueError(f"Invalid branch name '{branch}'")
        return b

    def _raise_vcs_error(
        self,
        prefix: str,
        detail: BaseException | str,
        *,
        extra_secrets: Sequence[str] = (),
    ) -> None:
        all_secrets = (*self.secrets, *extra_secrets)
        clean_err = redact_secrets(str(detail), extra_secrets=all_secrets)
        err_obj = VcsError(f"{prefix}: {clean_err}")
        err_obj.__cause__ = None
        err_obj.__context__ = None
        raise err_obj

    def _run_git(
        self,
        args: Sequence[str],
        *,
        cwd: Path,
        env: Mapping[str, str],
        context: str,
        extra_secrets: Sequence[str] = (),
    ) -> CommandResult:
        cmd = ["git", *args]
        res: CommandResult | None = None
        err_detail: str | None = None
        try:
            res = self.runner.run(cmd, cwd=cwd, env=env)
        except (CommandTimeoutError, OSError, RuntimeError, ValueError) as exc:
            err_detail = str(exc) or type(exc).__name__
        else:
            if res.exit_code != 0:
                err_detail = res.stderr or res.stdout or f"exit code {res.exit_code}"
        if err_detail is not None:
            self._raise_vcs_error(context, err_detail, extra_secrets=extra_secrets)
        assert res is not None
        return res

    def _effective_http_transport(self) -> Any | None:
        if self.http_transport is not None:
            return self.http_transport
        if self._is_real_subprocess_runner() and not is_local_remote_url(self.repo_base_url):
            from agent_factory.adapters.telegram_client import UrllibHttpTransport

            self.http_transport = UrllibHttpTransport()
            return self.http_transport
        return None

    def _github_request(
        self,
        method: str,
        path: str,
        payload: Mapping[str, Any] | None = None,
    ) -> tuple[int, str]:
        transport = self._effective_http_transport()
        if transport is None:
            raise VcsError("GitHub HTTP transport is unavailable")
        if hasattr(transport, "request_json"):
            return self._github_request_with_transport(transport, method, path, payload)
        url = f"{self.api_base_url}/{path.lstrip('/')}"
        headers = {
            "Accept": "application/vnd.github+json",
            "User-Agent": "agent-factory",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if self.github_token:
            headers["Authorization"] = f"Bearer {self.github_token}"

        resp: Any = None
        err_detail: str | None = None
        try:
            if method.upper() == "POST":
                resp = transport.post_json(
                    url,
                    payload or {},
                    timeout_seconds=self.timeout_seconds,
                    headers=headers,
                    extra_secrets=self.secrets,
                )
            else:
                err_detail = f"Unsupported HTTP transport for {method} {path}"
        except (TelegramTransportError, OSError, RuntimeError, ValueError) as exc:
            err_detail = str(exc) or type(exc).__name__
        if err_detail is not None:
            self._raise_vcs_error("GitHub API request failed", err_detail)
        assert resp is not None
        return int(resp.status_code), str(resp.body or "")

    def _sync_owner_urls(self, resolved_owner: str) -> None:
        valid_owner = _validate_owner(resolved_owner)
        self.owner = valid_owner
        self._owner_is_default = False
        if self._auto_deploy_base_url or self.deploy_base_url in ("", "https://agent-factory.github.io"):
            self.deploy_base_url = f"https://{valid_owner}.github.io"
        if (
            self.deploy_publisher is not None
            and self.deploy_publisher.target.lower() in ("gh-pages", "github-pages")
            and self.deploy_publisher.deploy_base_url in ("", "https://agent-factory.github.io")
        ):
            self.deploy_publisher.deploy_base_url = f"https://{valid_owner}.github.io"

    def _resolve_authenticated_owner(self, *, env: Mapping[str, str]) -> str:
        """Resolve GitHub owner login from API or gh CLI if only default placeholder is configured."""
        if not self._owner_is_default and self.owner:
            return _validate_owner(self.owner)

        if self._authenticated_login:
            self._sync_owner_urls(self._authenticated_login)
            return self.owner

        transport = self._effective_http_transport()
        if transport is not None and (self.github_token or self.http_transport is not None):
            status_code, body = self._github_request("GET", "/user")
            if status_code == 200 and body.strip():
                try:
                    data = json.loads(body)
                except json.JSONDecodeError:
                    data = {}
                login = str(data.get("login", "")).strip() if isinstance(data, dict) else ""
                if login:
                    self._authenticated_login = _validate_owner(login)
                    self._sync_owner_urls(self._authenticated_login)
                    return self.owner
        elif self._is_real_subprocess_runner() and shutil.which("gh"):
            res = self.runner.run(["gh", "api", "user", "--jq", ".login"], cwd=Path.cwd(), env=env)
            login = res.stdout.strip() if res.exit_code == 0 else ""
            if login:
                self._authenticated_login = _validate_owner(login)
                self._sync_owner_urls(self._authenticated_login)
                return self.owner

        return _validate_owner(self.owner or "agent-factory")

    def _ensure_github_repo(self, project_id: str, *, env: Mapping[str, str]) -> str:
        """Ensure github.com/<owner>/<project_id> exists via API/gh, creating it if 404 and ignoring 422."""
        owner = self._resolve_authenticated_owner(env=env)
        transport = self._effective_http_transport()
        if transport is not None and (self.github_token or self.http_transport is not None):
            status_code, body = self._github_request("GET", f"/repos/{owner}/{project_id}")
            if status_code == 200:
                return owner
            if status_code == 404:
                if not self.github_token and self.http_transport is None:
                    raise VcsError(
                        f"GitHub repository '{owner}/{project_id}' not found and GITHUB_TOKEN is missing"
                    )
                if not self._authenticated_login and self.github_token:
                    u_status, u_body = self._github_request("GET", "/user")
                    if u_status == 200 and u_body.strip():
                        try:
                            self._authenticated_login = str(json.loads(u_body).get("login", "")).strip()
                        except json.JSONDecodeError:
                            pass
                payload = {"name": project_id, "private": False, "auto_init": False}
                if not self._authenticated_login or self._authenticated_login.lower() == owner.lower():
                    c_status, c_body = self._github_request("POST", "/user/repos", payload)
                else:
                    c_status, c_body = self._github_request("POST", f"/orgs/{owner}/repos", payload)
                    if c_status == 404:
                        c_status, c_body = self._github_request("POST", "/user/repos", payload)
                if c_status in (200, 201, 422):
                    return owner
                self._raise_vcs_error(
                    f"GitHub repo creation failed (HTTP {c_status})",
                    c_body or f"status {c_status}",
                )
            self._raise_vcs_error(
                f"GitHub repo lookup failed (HTTP {status_code})",
                body or f"status {status_code}",
            )

        if self._is_real_subprocess_runner() and shutil.which("gh"):
            view_res = self.runner.run(
                ["gh", "repo", "view", f"{owner}/{project_id}"],
                cwd=Path.cwd(),
                env=env,
            )
            if view_res.exit_code != 0:
                cr = self.runner.run(
                    ["gh", "repo", "create", f"{owner}/{project_id}", "--public"],
                    cwd=Path.cwd(),
                    env=env,
                )
                combined_cr = f"{cr.stderr} {cr.stdout}".lower()
                if cr.exit_code != 0 and "already exists" not in combined_cr:
                    self._raise_vcs_error("GitHub repo creation failed", cr.stderr or cr.stdout or cr.exit_code)
            return owner

        raise VcsError(
            f"GitHub credentials unavailable to verify/create repository '{owner}/{project_id}'"
        )

    def _validate_workspace_git_toplevel(self, workspace_dir: Path, *, env: Mapping[str, str]) -> None:
        """Ensure git resolves its top-level root strictly to workspace_dir (INV-22)."""
        top_res = self.runner.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=workspace_dir,
            env=env,
        )
        if top_res.exit_code != 0:
            err = top_res.stderr or top_res.stdout or f"exit code {top_res.exit_code}"
            self._raise_vcs_error("Git root validation failed", err)
        raw_top = top_res.stdout.strip()
        looks_like_path = (
            os.path.sep in raw_top
            or "/" in raw_top
            or (len(raw_top) >= 2 and raw_top[1] == ":")
        )
        if (
            raw_top
            and ((workspace_dir / ".git" / "HEAD").is_file() or looks_like_path)
            and Path(raw_top).resolve() != workspace_dir.resolve()
        ):
            raise VcsError(
                f"Workspace git repository escapes '{workspace_dir.resolve()}': '{raw_top}' (INV-22)"
            )

    def _ensure_isolated_workspace_git(
        self,
        project_id: str,
        workspace_dir: Path,
        valid_branch: str,
        *,
        env: Mapping[str, str],
    ) -> str:
        """Initialize isolated .git repo inside workspace_dir if missing, set branch & commit, return existing origin URL."""
        git_entry = workspace_dir / ".git"
        if git_entry.is_symlink():
            raise VcsError(f"Workspace .git must not be a symlink: {git_entry} (INV-22)")

        if not git_entry.exists():
            init_res = self.runner.run(
                ["git", "init", "-b", self.default_branch],
                cwd=workspace_dir,
                env=env,
            )
            if init_res.exit_code != 0:
                self._run_git(["init"], cwd=workspace_dir, env=env, context="Git init failed")

        self._validate_workspace_git_toplevel(workspace_dir, env=env)

        ensure_workspace_git_excludes(workspace_dir, write_gitignore=True)

        had_local_identity = False
        git_cfg = workspace_dir / ".git" / "config"
        if git_cfg.is_file():
            try:
                cfg_txt = git_cfg.read_text(encoding="utf-8")
                if "user.name" in cfg_txt or ("[user]" in cfg_txt and "name =" in cfg_txt):
                    had_local_identity = True
            except OSError:
                pass

        if (workspace_dir / ".git" / "HEAD").is_file():
            name_chk = self.runner.run(["git", "config", "--get", "user.name"], cwd=workspace_dir, env=env)
            email_chk = self.runner.run(["git", "config", "--get", "user.email"], cwd=workspace_dir, env=env)
            if name_chk.exit_code != 0 or not name_chk.stdout.strip():
                self.runner.run(["git", "config", "--local", "user.name", "AgentFactory"], cwd=workspace_dir, env=env)
            if email_chk.exit_code != 0 or not email_chk.stdout.strip():
                self.runner.run(
                    ["git", "config", "--local", "user.email", "agent@factory.local"],
                    cwd=workspace_dir,
                    env=env,
                )

        cur_branch_res = self.runner.run(
            ["git", "symbolic-ref", "--short", "HEAD"],
            cwd=workspace_dir,
            env=env,
        )
        cur_branch = cur_branch_res.stdout.strip() if cur_branch_res.exit_code == 0 else ""
        if cur_branch != valid_branch:
            ref_res = self.runner.run(
                ["git", "show-ref", "--verify", f"refs/heads/{valid_branch}"],
                cwd=workspace_dir,
                env=env,
            )
            branch_cmd = (
                ["checkout", valid_branch]
                if ref_res.exit_code == 0
                else ["checkout", "-b", valid_branch]
            )
            self._run_git(branch_cmd, cwd=workspace_dir, env=env, context="Git branch setup failed")

        self._run_git(["add", "-A"], cwd=workspace_dir, env=env, context="Git add failed")
        agent_dir = workspace_dir / ".agent-factory"
        manifest_path = agent_dir / "manifest.json"
        if (
            manifest_path.is_file()
            and not agent_dir.is_symlink()
            and not manifest_path.is_symlink()
        ):
            self._run_git(
                ["add", "-f", ".agent-factory/manifest.json"],
                cwd=workspace_dir,
                env=env,
                context="Git add manifest failed",
            )
        head_res = self.runner.run(["git", "rev-parse", "--verify", "HEAD"], cwd=workspace_dir, env=env)
        diff_res = self.runner.run(["git", "diff", "--cached", "--quiet"], cwd=workspace_dir, env=env)
        if head_res.exit_code != 0 or diff_res.exit_code != 0:
            allow_empty = ["--allow-empty"] if (head_res.exit_code != 0 and diff_res.exit_code == 0) else []
            if had_local_identity:
                commit_args = ["commit", *allow_empty, "-m", f"chore({project_id}): update {valid_branch}"]
            else:
                commit_args = [
                    "-c",
                    "user.name=AgentFactory",
                    "-c",
                    "user.email=agent@factory.local",
                    "commit",
                    *allow_empty,
                    "-m",
                    f"chore({project_id}): update {valid_branch}",
                ]
            self._run_git(commit_args, cwd=workspace_dir, env=env, context="Git commit failed")

        origin_res = self.runner.run(["git", "remote", "get-url", "origin"], cwd=workspace_dir, env=env)
        existing_origin = origin_res.stdout.strip() if origin_res.exit_code == 0 else ""
        if existing_origin == "ok":
            existing_origin = ""
        return existing_origin

    def _legacy_fake_push_branch(
        self,
        project_id: str,
        workspace_dir: Path,
        valid_branch: str,
        clean_env: Mapping[str, str],
    ) -> str:
        """Preserve exact single-call FIFO behavior when tests run with FakeRunner."""
        if (workspace_dir / ".git").is_dir():
            self.runner.run(["git", "add", "-A"], cwd=workspace_dir, env=clean_env)
            diff_res = self.runner.run(
                ["git", "diff", "--cached", "--quiet"],
                cwd=workspace_dir,
                env=clean_env,
            )
            if diff_res.exit_code != 0:
                self._run_git(
                    [
                        "-c",
                        "user.name=AgentFactory",
                        "-c",
                        "user.email=agent@factory.local",
                        "commit",
                        "-m",
                        f"chore({project_id}): update {valid_branch}",
                    ],
                    cwd=workspace_dir,
                    env=clean_env,
                    context="Git commit failed",
                )
        res = self._run_git(
            ["push", "-u", "origin", valid_branch],
            cwd=workspace_dir,
            env=clean_env,
            context="Git push failed",
        )
        return res.stdout.strip() if res.stdout.strip() else valid_branch

    def push_branch(
        self,
        project_id: str,
        workspace_dir: Path,
        branch: str = "work",
    ) -> str:
        """Initialize isolated workspace git repo if needed, configure dedicated origin, and push branch."""
        validate_project_id(project_id)
        valid_branch = self._validate_branch(branch)
        if not workspace_dir.is_dir():
            raise ValueError(f"Workspace directory {workspace_dir} does not exist")

        self._last_workspace_dir[project_id] = workspace_dir
        clean_env = self.build_clean_env()

        if self._is_fake_runner():
            return self._legacy_fake_push_branch(project_id, workspace_dir, valid_branch, clean_env)

        existing_origin = self._ensure_isolated_workspace_git(
            project_id, workspace_dir, valid_branch, env=clean_env
        )

        if is_local_remote_url(existing_origin):
            self._local_only_projects.add(project_id)
            remote_url = existing_origin
        else:
            self._local_only_projects.discard(project_id)
            should_manage_github = (
                not is_local_remote_url(self.repo_base_url)
                and (self.http_transport is not None or self._is_real_subprocess_runner())
            )
            owner = (
                self._ensure_github_repo(project_id, env=clean_env)
                if should_manage_github
                else (self.owner or "agent-factory")
            )
            remote_url = (
                f"{self.repo_base_url}/{owner}/{project_id}.git"
                if owner
                else f"{self.repo_base_url}/{project_id}.git"
            )
            if (workspace_dir / ".git" / "HEAD").is_file():
                if not existing_origin:
                    self._run_git(
                        ["remote", "add", "origin", remote_url],
                        cwd=workspace_dir,
                        env=clean_env,
                        context="Git remote add failed",
                    )
                elif existing_origin != remote_url:
                    self._run_git(
                        ["remote", "set-url", "origin", remote_url],
                        cwd=workspace_dir,
                        env=clean_env,
                        context="Git remote configure failed",
                    )

        push_env, push_secrets = build_git_push_env(
            clean_env,
            self.github_token,
            repo_base_url=self.repo_base_url,
        )
        res = self._run_git(
            ["push", "-u", "origin", valid_branch],
            cwd=workspace_dir,
            env=push_env,
            context="Git push failed",
            extra_secrets=push_secrets,
        )
        return res.stdout.strip() if res.stdout.strip() else valid_branch

    def get_ci_result(
        self,
        project_id: str,
        branch: str = "work",
    ) -> CIRunResult:
        """Query remote CI workflow result for project_id and branch via gh CLI or GitHub API."""
        validate_project_id(project_id)
        valid_branch = self._validate_branch(branch)
        clean_env = self.build_clean_env()
        is_local = project_id in self._local_only_projects or is_local_remote_url(self.repo_base_url)
        owner = (
            self._resolve_authenticated_owner(env=clean_env)
            if (not is_local and (self.http_transport is not None or self._is_real_subprocess_runner()))
            else self.owner
        )
        repo_target = f"{owner}/{project_id}" if owner else project_id
        ws_dir = self._last_workspace_dir.get(project_id)

        if is_local and self._is_real_subprocess_runner() and shutil.which("gh") is None:
            return CIRunResult(passed=True, summary="Local bare repo checks passed.")

        use_rest_api = (
            not is_local
            and self._is_real_subprocess_runner()
            and shutil.which("gh") is None
        )
        if use_rest_api:
            status_code, body = self._github_request(
                "GET",
                f"/repos/{repo_target}/actions/runs?branch={valid_branch}&per_page=1",
            )
            if status_code == 200:
                try:
                    data = json.loads(body) if body.strip() else {}
                except json.JSONDecodeError:
                    data = {}
                runs = data.get("workflow_runs", []) if isinstance(data, dict) else []
                if not runs:
                    if ws_dir is None or not _workspace_has_github_workflows(ws_dir):
                        return CIRunResult(
                            passed=True,
                            summary="Sin workflows de CI configurados; pruebas locales aprobadas.",
                        )
                    return CIRunResult(passed=False, summary="No CI runs found", failed_checks=["ci"])
                latest = runs[0] if isinstance(runs[0], dict) else {}
                conclusion = str(latest.get("conclusion") or "").lower()
                status_str = str(latest.get("status") or "").lower()
                passed = conclusion == "success"
                return CIRunResult(
                    passed=passed,
                    summary=f"CI status: {status_str}, conclusion: {conclusion}",
                    failed_checks=[] if passed else ["ci"],
                )
            if status_code == 404 and (ws_dir is None or not _workspace_has_github_workflows(ws_dir)):
                return CIRunResult(
                    passed=True,
                    summary="Sin workflows de CI configurados; pruebas locales aprobadas.",
                )
            self._raise_vcs_error("CI query failed", body or f"HTTP {status_code}")

        cmd = [
            "gh",
            "run",
            "list",
            "--repo",
            repo_target,
            "--branch",
            valid_branch,
            "--limit",
            "1",
            "--json",
            "status,conclusion",
        ]
        clean_err: str | None = None
        res = None
        try:
            res = self.runner.run(cmd, cwd=Path.cwd(), env=clean_env)
        except (CommandTimeoutError, OSError, RuntimeError, ValueError) as exc:
            clean_err = redact_secrets(str(exc), extra_secrets=self.secrets)
        else:
            if res.exit_code != 0:
                err = res.stderr or res.stdout or f"exit code {res.exit_code}"
                clean_err = redact_secrets(err, extra_secrets=self.secrets)

        if clean_err is not None:
            err_obj = VcsError(f"CI query failed: {clean_err}")
            err_obj.__cause__ = None
            err_obj.__context__ = None
            raise err_obj

        raw_output = res.stdout.strip() if res else ""
        try:
            parsed = json.loads(raw_output)
            if isinstance(parsed, list):
                if not parsed:
                    if (
                        self._is_real_subprocess_runner()
                        and ws_dir is not None
                        and not _workspace_has_github_workflows(ws_dir)
                    ):
                        return CIRunResult(
                            passed=True,
                            summary="Sin workflows de CI configurados; pruebas locales aprobadas.",
                        )
                    return CIRunResult(
                        passed=False,
                        summary="No CI runs found",
                        failed_checks=["ci"],
                    )
                parsed = parsed[0]
            if isinstance(parsed, dict):
                if "passed" in parsed:
                    return CIRunResult(
                        passed=bool(parsed["passed"]),
                        summary=str(parsed.get("summary", "")),
                        failed_checks=list(parsed.get("failed_checks", [])),
                    )
                conclusion = str(parsed.get("conclusion", "")).lower()
                status_str = str(parsed.get("status", "")).lower()
                passed = conclusion == "success"
                summary = f"CI status: {status_str}, conclusion: {conclusion}"
                failed_checks = [] if passed else ["ci"]
                return CIRunResult(passed=passed, summary=summary, failed_checks=failed_checks)
        except json.JSONDecodeError:
            pass

        return CIRunResult(
            passed=False,
            summary=raw_output[:200],
            failed_checks=["ci"],
        )

    def _merge_via_github_api_or_git(
        self,
        project_id: str,
        repo_target: str,
        valid_branch: str,
        ws_dir: Path,
        clean_env: Mapping[str, str],
    ) -> None:
        """Promote verified branch to default_branch via git push (initial repo) or GitHub PR REST API."""
        push_env, push_secrets = build_git_push_env(
            clean_env,
            self.github_token,
            repo_base_url=self.repo_base_url,
        )
        ls_res = self.runner.run(
            ["git", "ls-remote", "--heads", "origin", self.default_branch],
            cwd=ws_dir,
            env=push_env,
        )
        remote_main_exists = ls_res.exit_code == 0 and bool(ls_res.stdout.strip())
        if not remote_main_exists or is_local_remote_url(self.repo_base_url) or project_id in self._local_only_projects:
            self._run_git(
                ["push", "-u", "origin", f"{valid_branch}:{self.default_branch}"],
                cwd=ws_dir,
                env=push_env,
                context="Merge failed",
                extra_secrets=push_secrets,
            )
            return

        pr_payload = {
            "title": f"Merge {valid_branch}",
            "head": valid_branch,
            "base": self.default_branch,
            "body": f"Automated PR for {project_id}",
        }
        pr_status, pr_body = self._github_request("POST", f"/repos/{repo_target}/pulls", pr_payload)
        pr_number: int | None = None
        if pr_status in (200, 201) and pr_body.strip():
            pr_number = int(json.loads(pr_body)["number"])
        elif pr_status == 422:
            if "no commits between" in pr_body.lower():
                return
            owner = repo_target.split("/", 1)[0]
            list_status, list_body = self._github_request(
                "GET",
                f"/repos/{repo_target}/pulls?head={owner}:{valid_branch}&base={self.default_branch}&state=open",
            )
            if list_status == 200 and list_body.strip():
                prs = json.loads(list_body)
                if isinstance(prs, list) and prs:
                    pr_number = int(prs[0]["number"])
            if pr_number is None:
                self._run_git(
                    ["push", "-u", "origin", f"{valid_branch}:{self.default_branch}"],
                    cwd=ws_dir,
                    env=push_env,
                    context="Merge failed",
                    extra_secrets=push_secrets,
                )
                return
        else:
            self._raise_vcs_error("Merge failed", pr_body or f"HTTP {pr_status}")

        if pr_number is not None:
            m_status, m_body = self._github_request(
                "PUT",
                f"/repos/{repo_target}/pulls/{pr_number}/merge",
                {"merge_method": "merge"},
            )
            if m_status not in (200, 201):
                self._raise_vcs_error("Merge failed", m_body or f"HTTP {m_status}")

    def merge_and_deploy(
        self,
        project_id: str,
        branch: str = "work",
    ) -> tuple[str, str]:
        """Merge verified branch into main and deploy static artifact, returning clean (repo_url, deploy_url)."""
        validate_project_id(project_id)
        valid_branch = self._validate_branch(branch)
        clean_env = self.build_clean_env()
        is_local = project_id in self._local_only_projects or is_local_remote_url(self.repo_base_url)
        owner = (
            self._resolve_authenticated_owner(env=clean_env)
            if (not is_local and (self.http_transport is not None or self._is_real_subprocess_runner()))
            else self.owner
        )
        repo_target = f"{owner}/{project_id}" if owner else project_id

        ws_dir = self._last_workspace_dir.get(
            project_id, Path("workspaces") / project_id
        )

        use_rest_merge = (
            self._is_real_subprocess_runner()
            and shutil.which("gh") is None
        )
        if use_rest_merge:
            self._merge_via_github_api_or_git(
                project_id, repo_target, valid_branch, ws_dir, clean_env
            )
        else:
            if (ws_dir / ".git").is_dir():
                if self._is_real_subprocess_runner() and shutil.which("gh") is not None:
                    push_env, push_secrets = build_git_push_env(
                        clean_env, self.github_token, repo_base_url=self.repo_base_url
                    )
                    ls_res = self.runner.run(
                        ["git", "ls-remote", "--heads", "origin", self.default_branch],
                        cwd=ws_dir,
                        env=push_env,
                    )
                    if ls_res.exit_code == 0 and not ls_res.stdout.strip():
                        self._run_git(
                            ["push", "-u", "origin", f"{valid_branch}:{self.default_branch}"],
                            cwd=ws_dir,
                            env=push_env,
                            context="Merge failed",
                            extra_secrets=push_secrets,
                        )
                try:
                    self.runner.run(
                        [
                            "gh",
                            "pr",
                            "create",
                            "--head",
                            valid_branch,
                            "--base",
                            self.default_branch,
                            "--title",
                            f"Merge {valid_branch}",
                            "--body",
                            f"Automated PR for {project_id}",
                            "--repo",
                            repo_target,
                        ],
                        cwd=ws_dir,
                        env=clean_env,
                    )
                except (CommandTimeoutError, OSError, RuntimeError, ValueError) as pr_exc:
                    _ = redact_secrets(str(pr_exc), extra_secrets=self.secrets)

            cmd = ["gh", "pr", "merge", valid_branch, "--repo", repo_target, "--merge", "--auto"]
            clean_err: str | None = None
            res = None
            try:
                res = self.runner.run(cmd, cwd=Path.cwd(), env=clean_env)
            except (CommandTimeoutError, OSError, RuntimeError, ValueError) as exc:
                clean_err = redact_secrets(str(exc), extra_secrets=self.secrets)
            else:
                if res.exit_code != 0:
                    err = res.stderr or res.stdout or f"exit code {res.exit_code}"
                    clean_err = redact_secrets(err, extra_secrets=self.secrets)

            if clean_err is not None:
                err_obj = VcsError(f"Merge failed: {clean_err}")
                err_obj.__cause__ = None
                err_obj.__context__ = None
                raise err_obj

        repo_url = (
            f"{self.repo_base_url}/{owner}/{project_id}"
            if owner
            else f"{self.repo_base_url}/{project_id}"
        )
        is_python_local = ws_dir.is_dir() and detect_workspace_app(ws_dir).app_type == "python_local"
        if is_python_local:
            deploy_url = ""
        elif self.deploy_publisher is not None:
            deploy_url = self.deploy_publisher.deploy_site(project_id, ws_dir)
            if (
                use_rest_merge
                and not is_local
                and self.deploy_publisher.target.lower() in ("gh-pages", "github-pages")
                and (self.github_token or self.http_transport is not None)
            ):
                try:
                    self._github_request(
                        "POST",
                        f"/repos/{repo_target}/pages",
                        {"source": {"branch": self.deploy_publisher.branch, "path": "/"}},
                    )
                except VcsError:
                    pass
        else:
            deploy_url = f"{self.deploy_base_url}/{project_id}" if self.deploy_base_url else repo_url
        return (repo_url, deploy_url)

    def _github_request_with_transport(
        self,
        transport: Any,
        method: str,
        path: str,
        payload: Mapping[str, Any] | None = None,
    ) -> tuple[int, str]:
        """Execute a GitHub REST API call against an explicit transport with secret redaction."""
        url = f"{self.api_base_url}/{path.lstrip('/')}"
        headers = {
            "Accept": "application/vnd.github+json",
            "User-Agent": "agent-factory",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if self.github_token:
            headers["Authorization"] = f"Bearer {self.github_token}"

        resp: Any = None
        err_detail: str | None = None
        try:
            resp = transport.request_json(
                method,
                url,
                payload=payload,
                timeout_seconds=self.timeout_seconds,
                headers=headers,
                extra_secrets=self.secrets,
                raise_on_http_error=False,
            )
        except (TelegramTransportError, OSError, RuntimeError, ValueError) as exc:
            err_detail = str(exc) or type(exc).__name__
        if err_detail is not None:
            self._raise_vcs_error("GitHub API request failed", err_detail)
        assert resp is not None
        return int(resp.status_code), str(resp.body or "")

    def rename_repository(
        self,
        current_repo_url: str,
        new_title: str,
        workspace_dir: Path | None = None,
    ) -> tuple[str, str | None]:
        """Rename GitHub repository via PATCH /repos/{owner}/{old_repo}, update isolated origin remote, and return updated (repo_url, deploy_url)."""
        raw_url = str(current_repo_url or "").strip()
        if not raw_url:
            return raw_url, None

        parsed = urllib.parse.urlsplit(raw_url)
        host = (parsed.hostname or "").lower()
        repo_base_host = (urllib.parse.urlsplit(self.repo_base_url).hostname or "").lower()
        if parsed.scheme.lower() not in ("http", "https") or host not in ("github.com", repo_base_host):
            return raw_url, None

        segments = [seg for seg in parsed.path.strip("/").split("/") if seg]
        if len(segments) != 2:
            return raw_url, None
        owner = _validate_owner(segments[0])
        old_repo = _validate_repo_segment(segments[1].removesuffix(".git"))

        new_slug = slugify_repo_name(new_title)
        if not new_slug:
            raise ValueError("Cannot derive a valid repository slug from the new title")
        new_slug = _validate_repo_segment(new_slug)

        if not self.github_token:
            return raw_url, None

        final_slug = new_slug
        if new_slug != old_repo:
            transport = self._effective_http_transport()
            if transport is None and not self._is_fake_runner():
                from agent_factory.adapters.telegram_client import UrllibHttpTransport

                transport = UrllibHttpTransport()
            if transport is None:
                raise VcsError("GitHub HTTP transport is unavailable to rename repository")

            status_code, body = self._github_request_with_transport(
                transport,
                "PATCH",
                f"/repos/{owner}/{old_repo}",
                {"name": new_slug},
            )
            if status_code in (200, 201):
                if body.strip():
                    try:
                        parsed_body = json.loads(body)
                        if isinstance(parsed_body, dict) and parsed_body.get("name"):
                            final_slug = _validate_repo_segment(str(parsed_body["name"]))
                    except (json.JSONDecodeError, ValueError):
                        pass
            elif status_code == 422 and "already exists on this account" not in body.lower():
                final_slug = new_slug
            else:
                self._raise_vcs_error(
                    f"GitHub repo rename failed (HTTP {status_code})",
                    body or f"status {status_code}",
                )

        repo_base = self.repo_base_url.rstrip("/") if host == repo_base_host and self.repo_base_url else "https://github.com"
        new_repo_url = f"{repo_base}/{owner}/{final_slug}"
        if self.deploy_base_url and owner.lower() == self.owner.lower():
            deploy_base = self.deploy_base_url.rstrip("/")
        else:
            deploy_base = f"https://{owner}.github.io"
        new_deploy_url = f"{deploy_base}/{final_slug}/"

        if workspace_dir is not None:
            ws = Path(workspace_dir)
            if ws.is_dir():
                ws_resolved = ws.resolve()
                git_entry = ws_resolved / ".git"
                if git_entry.is_symlink():
                    raise VcsError(f"Workspace .git must not be a symlink: {git_entry} (INV-22)")
                if (git_entry / "HEAD").is_file():
                    clean_env = self.build_clean_env()
                    self._validate_workspace_git_toplevel(ws_resolved, env=clean_env)
                    remote_git_url = f"{new_repo_url}.git"
                    cur_origin = self.runner.run(
                        ["git", "remote", "get-url", "origin"],
                        cwd=ws_resolved,
                        env=clean_env,
                    )
                    if cur_origin.exit_code == 0 and cur_origin.stdout.strip():
                        self._run_git(
                            ["remote", "set-url", "origin", remote_git_url],
                            cwd=ws_resolved,
                            env=clean_env,
                            context="Git remote set-url failed",
                        )
                    else:
                        self._run_git(
                            ["remote", "add", "origin", remote_git_url],
                            cwd=ws_resolved,
                            env=clean_env,
                            context="Git remote add failed",
                        )

        return (new_repo_url, new_deploy_url)
