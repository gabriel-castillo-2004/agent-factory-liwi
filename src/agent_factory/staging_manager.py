"""Cumulative background self-improvement staging manager using git worktree (.liwi-staging)."""

from __future__ import annotations

import logging
import os
import posixpath
import re
import shutil
import stat
import subprocess
import threading
import time
from collections import deque
from collections.abc import Callable, Sequence
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from agent_factory.error_triage import format_agent_error, is_quota_429_error
from agent_factory.secrets import redact_secrets

if TYPE_CHECKING:
    from agent_factory.parallel_improver import (
        ChangedFilesInspector,
        ParallelImprover,
        ParallelPlan,
        ParallelRunResult,
        PytestRunner,
        RuffRunner,
        WorkerRunner,
    )

_SAFE_WORKTREE_NAME_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
_SAFE_BRANCH_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,127}$")
_DEFAULT_GIT_TIMEOUT_SECONDS: float = 30.0

DEFAULT_FORBIDDEN_DIFF_PREFIXES: tuple[str, ...] = (
    ".env",
    "MASTER_PROMPT.md",
    "AGENTS.md",
    "config/models.toml",
    "data/",
    "workspaces/",
)
DEFAULT_MAX_DIFF_FILES: int = 12

_MANAGERS_LOCK = threading.Lock()
_MANAGERS: dict[Path, StagingManager] = {}


def _normalize_diff_path(raw_path: str) -> str:
    cleaned = str(raw_path or "").strip().replace("\\", "/")
    if not cleaned:
        return ""
    while cleaned.startswith("./"):
        cleaned = cleaned[2:]
    cleaned = cleaned.lstrip("/")
    if not cleaned:
        return ""
    normalized = posixpath.normpath(cleaned)
    if normalized == ".":
        return ""
    return normalized


def validate_diff_scope(
    changed_paths: list[str],
    *,
    forbidden_prefixes: tuple[str, ...] = DEFAULT_FORBIDDEN_DIFF_PREFIXES,
    max_files: int = DEFAULT_MAX_DIFF_FILES,
) -> tuple[bool, str]:
    """Validate staging diff scope: reject empty diffs, forbidden paths, and oversized changes."""
    normalized_paths: list[str] = []
    seen: set[str] = set()
    for raw in changed_paths:
        norm = _normalize_diff_path(raw)
        if not norm:
            continue
        if norm == ".." or norm.startswith("../"):
            return False, f"Ruta prohibida fuera del repositorio en diff de staging: '{raw}'"
        key = norm.casefold()
        if key not in seen:
            seen.add(key)
            normalized_paths.append(norm)

    if not normalized_paths:
        return False, "Diff de staging vacío: no se detectaron archivos modificados."

    normalized_prefixes: list[tuple[str, str]] = []
    for raw_prefix in forbidden_prefixes:
        norm_prefix = _normalize_diff_path(raw_prefix)
        if norm_prefix:
            normalized_prefixes.append((raw_prefix, norm_prefix.casefold()))

    for path in normalized_paths:
        path_cf = path.casefold()
        for raw_prefix, prefix_cf in normalized_prefixes:
            if (
                path_cf == prefix_cf
                or path_cf.startswith(f"{prefix_cf}/")
                or (prefix_cf == ".env" and path_cf.startswith(".env."))
            ):
                return (
                    False,
                    f"Ruta prohibida en diff de staging ('{path}' coincide con '{raw_prefix}').",
                )

    if len(normalized_paths) > max_files:
        return (
            False,
            f"El diff de staging modifica {len(normalized_paths)} archivos, excediendo el máximo permitido de {max_files}.",
        )

    return True, ""


def _validate_worktree_name(worktree_name: str, repo_root: Path) -> tuple[str, Path]:
    clean = str(worktree_name or "").strip()
    if (
        not clean
        or clean in {".", ".."}
        or "/" in clean
        or "\\" in clean
        or ".." in clean
        or not _SAFE_WORKTREE_NAME_RE.fullmatch(clean)
    ):
        raise ValueError(f"Nombre de worktree inválido: '{worktree_name}'")
    resolved_root = repo_root.resolve()
    resolved_wt = (resolved_root / clean).resolve()
    if not resolved_wt.is_relative_to(resolved_root) or resolved_wt.parent != resolved_root:
        raise ValueError(f"La ruta del worktree escapa de repo_root (INV-22): '{worktree_name}'")
    return clean, resolved_wt


def _validate_branch_name(branch_name: str, label: str) -> str:
    clean = str(branch_name or "").strip()
    if (
        not clean
        or ".." in clean
        or clean.startswith("-")
        or clean.endswith(("/", ".lock"))
        or not _SAFE_BRANCH_NAME_RE.fullmatch(clean)
    ):
        raise ValueError(f"Nombre de rama {label} inválido: '{branch_name}'")
    return clean


def _handle_remove_readonly(func: Callable[..., Any], path: str, _exc: Any) -> None:
    try:
        os.chmod(path, stat.S_IWRITE)
    except OSError:
        pass
    func(path)


def _safe_resolve(path: Path) -> Path:
    try:
        return path.resolve()
    except OSError:
        return Path(os.path.abspath(str(path)))


def _norm_path(path: Path) -> str:
    return os.path.normcase(str(_safe_resolve(path)))


def _parse_worktree_porcelain(output: str) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    for raw_line in output.splitlines():
        line = raw_line.strip()
        if not line:
            if current is not None:
                entries.append(current)
                current = None
            continue
        if line.startswith("worktree "):
            if current is not None:
                entries.append(current)
            current = {
                "path": line[len("worktree ") :].strip(),
                "head": "",
                "branch": None,
                "detached": False,
                "bare": False,
                "locked": False,
                "prunable": False,
            }
        elif current is not None:
            if line.startswith("HEAD "):
                current["head"] = line[len("HEAD ") :].strip()
            elif line.startswith("branch "):
                ref = line.removeprefix("branch ").strip()
                current["branch"] = ref.removeprefix("refs/heads/")
            elif line == "detached":
                current["detached"] = True
            elif line == "bare":
                current["bare"] = True
            elif line == "locked" or line.startswith("locked "):
                current["locked"] = True
            elif line == "prunable" or line.startswith("prunable "):
                current["prunable"] = True
    if current is not None:
        entries.append(current)
    return entries


class StagingManager:
    """Manages an isolated git worktree (.liwi-staging) for queued background self-improvements."""

    def __init__(
        self,
        repo_root: Path | str,
        worktree_name: str = ".liwi-staging",
        staging_branch: str = "liwi-staging",
        base_branch: str = "main",
        executor: Callable[[Path, dict[str, Any]], Any] | None = None,
        on_restart: Callable[[], None] | None = None,
        db_path: Path | str | None = None,
        forbidden_prefixes: tuple[str, ...] = DEFAULT_FORBIDDEN_DIFF_PREFIXES,
        max_diff_files: int = DEFAULT_MAX_DIFF_FILES,
    ) -> None:
        self.repo_root = Path(repo_root).resolve()
        self.worktree_name, self.worktree_path = _validate_worktree_name(
            worktree_name,
            self.repo_root,
        )
        self.staging_branch = _validate_branch_name(staging_branch, "staging")
        self.base_branch = _validate_branch_name(base_branch, "base")
        self._executor = executor
        self._on_restart = on_restart
        self._db_path = db_path
        self.forbidden_prefixes: tuple[str, ...] = tuple(forbidden_prefixes)
        self.max_diff_files: int = int(max_diff_files)

        self._lock = threading.Lock()
        self._cond = threading.Condition(self._lock)
        self._worktree_setup_lock = threading.RLock()
        self._queue: deque[dict[str, Any]] = deque()
        self._paused_tasks: deque[dict[str, Any]] = deque()
        self._worker: threading.Thread | None = None
        self._is_building: bool = False
        self._is_promoting: bool = False
        self._shutdown_requested: bool = False
        self._last_error: str | None = None
        self._current_task: str | None = None

    def _run_git(
        self,
        args: list[str],
        *,
        cwd: Path | None = None,
        timeout: float = _DEFAULT_GIT_TIMEOUT_SECONDS,
    ) -> subprocess.CompletedProcess[str]:
        target_cwd = cwd if cwd is not None else self.repo_root
        return subprocess.run(
            ["git", *args],
            cwd=target_cwd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=timeout,
        )

    def _ensure_git_info_exclude(self) -> None:
        git_dir_res = self._run_git(["rev-parse", "--git-common-dir"], cwd=self.repo_root)
        if git_dir_res.returncode != 0 or not git_dir_res.stdout.strip():
            return
        raw_git_dir = Path(git_dir_res.stdout.strip())
        git_dir = raw_git_dir if raw_git_dir.is_absolute() else (self.repo_root / raw_git_dir)
        exclude_file = git_dir.resolve() / "info" / "exclude"
        exclude_file.parent.mkdir(parents=True, exist_ok=True)

        entry = f"{self.worktree_name.rstrip('/')}/"
        existing_text = ""
        if exclude_file.is_file():
            existing_text = exclude_file.read_text(encoding="utf-8", errors="replace")
        existing_lines = {line.strip() for line in existing_text.splitlines()}
        if entry in existing_lines or self.worktree_name in existing_lines:
            return

        prefix = "\n" if existing_text and not existing_text.endswith("\n") else ""
        with exclude_file.open("a", encoding="utf-8") as fh:
            fh.write(f"{prefix}{entry}\n")

    def _is_valid_worktree(self) -> bool:
        if not self.worktree_path.is_dir():
            return False
        if not (self.worktree_path / ".git").exists():
            return False
        inside = self._run_git(["rev-parse", "--is-inside-work-tree"], cwd=self.worktree_path)
        if inside.returncode != 0 or inside.stdout.strip() != "true":
            return False
        top = self._run_git(["rev-parse", "--show-toplevel"], cwd=self.worktree_path)
        if top.returncode != 0 or not top.stdout.strip():
            return False
        try:
            if Path(top.stdout.strip()).resolve() != self.worktree_path:
                return False
        except OSError:
            return False
        branch = self._run_git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=self.worktree_path)
        return branch.returncode == 0 and branch.stdout.strip() == self.staging_branch

    def ensure_worktree(self) -> Path:
        """Ensure staging branch, .git/info/exclude entry, and .liwi-staging git worktree exist."""
        with self._worktree_setup_lock:
            self._ensure_git_info_exclude()

            branch_chk = self._run_git(
                ["rev-parse", "--verify", f"refs/heads/{self.staging_branch}"],
                cwd=self.repo_root,
            )
            if branch_chk.returncode != 0:
                create_branch = self._run_git(
                    ["branch", self.staging_branch, self.base_branch],
                    cwd=self.repo_root,
                )
                if create_branch.returncode != 0:
                    detail = redact_secrets(
                        (create_branch.stderr or create_branch.stdout or "git branch failed").strip()
                    )
                    raise RuntimeError(
                        f"No se pudo crear la rama '{self.staging_branch}' desde '{self.base_branch}': {detail}"
                    )

            if self._is_valid_worktree():
                return self.worktree_path

            self._run_git(["worktree", "prune"], cwd=self.repo_root)

            if self.worktree_path.exists():
                self._run_git(
                    ["worktree", "remove", "--force", str(self.worktree_path)],
                    cwd=self.repo_root,
                )
                if self.worktree_path.exists():
                    shutil.rmtree(self.worktree_path, onerror=_handle_remove_readonly)
                self._run_git(["worktree", "prune"], cwd=self.repo_root)

            add_res = self._run_git(
                ["worktree", "add", str(self.worktree_path), self.staging_branch],
                cwd=self.repo_root,
            )
            if add_res.returncode != 0:
                detail = redact_secrets(
                    (add_res.stderr or add_res.stdout or "git worktree add failed").strip()
                )
                raise RuntimeError(
                    f"No se pudo crear el worktree '{self.worktree_name}' en '{self.staging_branch}': {detail}"
                )

            return self.worktree_path

    def _cleanup_worktree(self) -> None:
        if not self.worktree_path.is_dir() or not (self.worktree_path / ".git").exists():
            return
        self._run_git(["reset", "--hard", "HEAD"], cwd=self.worktree_path)
        self._run_git(["clean", "-fd"], cwd=self.worktree_path)

    def _run_git_safe(
        self,
        args: list[str],
        *,
        errors: list[str],
        cwd: Path | None = None,
        timeout: float = _DEFAULT_GIT_TIMEOUT_SECONDS,
        record_nonzero: bool = True,
    ) -> subprocess.CompletedProcess[str] | None:
        try:
            res = self._run_git(args, cwd=cwd, timeout=timeout)
        except (subprocess.SubprocessError, OSError) as exc:
            errors.append(redact_secrets(f"git {' '.join(args)}: {exc}"))
            return None
        if record_nonzero and res.returncode != 0:
            detail = (res.stderr or res.stdout or f"exit code {res.returncode}").strip()
            errors.append(redact_secrets(f"git {' '.join(args)}: {detail}"))
        return res

    def _is_temp_staging_worktree_name(self, path: Path) -> bool:
        name = path.name
        if not name or name == self.worktree_name:
            return False
        prefixes = (
            f"{self.worktree_name}-",
            f"{self.worktree_name}_",
            f"{self.worktree_name}.",
            ".liwi-staging-",
            ".liwi-staging_",
            ".liwi-staging.",
            "staging-tmp-",
            ".staging-tmp-",
        )
        if any(name.startswith(prefix) for prefix in prefixes):
            return True
        return path.parent.name.startswith("af_wt_") and name.startswith("agent-")

    def _is_temp_staging_branch(self, branch_name: str) -> bool:
        clean = str(branch_name or "").strip()
        if not clean or clean in {"main", "master", self.base_branch, self.staging_branch}:
            return False
        prefixes = (
            f"{self.staging_branch}-",
            f"{self.staging_branch}/",
            f"{self.staging_branch}_",
            "liwi-staging-",
            "liwi-staging/",
            "liwi-staging_",
            "staging-tmp-",
            "staging/tmp-",
            "af-par-",
        )
        return any(clean.startswith(prefix) for prefix in prefixes)

    def prune_stale_worktrees(self, max_age_seconds: int = 3600) -> dict[str, list[str]]:
        """Safely prune orphaned/expired temporary staging worktrees and branches without touching main or active worktrees."""
        pruned_worktrees: list[str] = []
        pruned_branches: list[str] = []
        skipped: list[str] = []
        errors: list[str] = []
        summary: dict[str, list[str]] = {
            "pruned_worktrees": pruned_worktrees,
            "pruned_branches": pruned_branches,
            "skipped": skipped,
            "errors": errors,
        }

        max_age = max(0.0, float(max_age_seconds))
        now_ts = time.time()

        with self._worktree_setup_lock:
            with self._lock:
                manager_busy = bool(self._is_building or self._is_promoting)

            repo_root_norm = _norm_path(self.repo_root)
            active_wt_norm = _norm_path(self.worktree_path)
            protected_branches: set[str] = {
                "main",
                "master",
                self.base_branch,
                self.staging_branch,
            }

            head_res = self._run_git_safe(
                ["rev-parse", "--abbrev-ref", "HEAD"],
                errors=errors,
                cwd=self.repo_root,
                record_nonzero=False,
            )
            if head_res is not None and head_res.returncode == 0:
                current_head = head_res.stdout.strip()
                if current_head and current_head != "HEAD":
                    protected_branches.add(current_head)

            list_res = self._run_git_safe(
                ["worktree", "list", "--porcelain"],
                errors=errors,
                cwd=self.repo_root,
            )
            worktree_list_ok = list_res is not None and list_res.returncode == 0
            entries = (
                _parse_worktree_porcelain(list_res.stdout)
                if worktree_list_ok and list_res is not None
                else []
            )

            prunable_entries: list[dict[str, Any]] = []
            registered_paths_norm: set[str] = set()
            for entry in entries:
                wt_path = _safe_resolve(Path(entry["path"]))
                wt_norm = _norm_path(wt_path)
                registered_paths_norm.add(wt_norm)
                if entry.get("prunable") and wt_norm not in {repo_root_norm, active_wt_norm}:
                    prunable_entries.append(entry)

            prune_res = self._run_git_safe(
                ["worktree", "prune"],
                errors=errors,
                cwd=self.repo_root,
            )
            prune_succeeded = prune_res is not None and prune_res.returncode == 0
            if prune_succeeded:
                for entry in prunable_entries:
                    pruned_worktrees.append(str(_safe_resolve(Path(entry["path"]))))

            if not worktree_list_ok:
                return summary

            active_branches: set[str] = set(protected_branches)
            if not prune_succeeded:
                for entry in prunable_entries:
                    branch = entry.get("branch")
                    if branch:
                        active_branches.add(str(branch))

            for entry in entries:
                wt_path = _safe_resolve(Path(entry["path"]))
                wt_norm = _norm_path(wt_path)
                wt_branch = str(entry["branch"]) if entry.get("branch") else None

                if entry.get("prunable") and wt_norm not in {repo_root_norm, active_wt_norm}:
                    continue

                if (
                    wt_norm == repo_root_norm
                    or wt_norm == active_wt_norm
                    or (wt_branch is not None and wt_branch in protected_branches)
                    or bool(entry.get("locked"))
                ):
                    skipped.append(str(wt_path))
                    if wt_branch:
                        active_branches.add(wt_branch)
                    continue

                is_temp_wt = self._is_temp_staging_worktree_name(wt_path) or (
                    wt_branch is not None and self._is_temp_staging_branch(wt_branch)
                )
                if not is_temp_wt or manager_busy:
                    skipped.append(str(wt_path))
                    if wt_branch:
                        active_branches.add(wt_branch)
                    continue

                if wt_path.exists():
                    try:
                        mtime = wt_path.stat().st_mtime
                    except OSError as exc:
                        errors.append(redact_secrets(f"stat {wt_path}: {exc}"))
                        if wt_branch:
                            active_branches.add(wt_branch)
                        continue
                    if (now_ts - mtime) < max_age:
                        skipped.append(str(wt_path))
                        if wt_branch:
                            active_branches.add(wt_branch)
                        continue

                rem_res = self._run_git_safe(
                    ["worktree", "remove", "--force", str(wt_path)],
                    errors=errors,
                    cwd=self.repo_root,
                )
                if rem_res is not None and rem_res.returncode == 0:
                    pruned_worktrees.append(str(wt_path))
                elif wt_branch:
                    active_branches.add(wt_branch)

            if not manager_busy and self.repo_root.is_dir():
                try:
                    children = list(self.repo_root.iterdir())
                except OSError as exc:
                    errors.append(redact_secrets(f"iterdir {self.repo_root}: {exc}"))
                    children = []

                for child in children:
                    if child.is_symlink() or not child.is_dir():
                        continue
                    child_resolved = _safe_resolve(child)
                    child_norm = _norm_path(child_resolved)
                    if child_norm in registered_paths_norm or child_norm in {
                        repo_root_norm,
                        active_wt_norm,
                    }:
                        continue
                    if (
                        not child_resolved.is_relative_to(self.repo_root)
                        or child_resolved.parent != self.repo_root
                    ):
                        continue
                    if not self._is_temp_staging_worktree_name(child_resolved):
                        continue
                    try:
                        child_mtime = child_resolved.stat().st_mtime
                    except OSError as exc:
                        errors.append(redact_secrets(f"stat {child_resolved}: {exc}"))
                        continue
                    if (now_ts - child_mtime) < max_age:
                        skipped.append(str(child_resolved))
                        continue
                    try:
                        shutil.rmtree(child_resolved, onerror=_handle_remove_readonly)
                        pruned_worktrees.append(str(child_resolved))
                    except OSError as exc:
                        errors.append(redact_secrets(f"rmtree {child_resolved}: {exc}"))

            if not manager_busy:
                refs_res = self._run_git_safe(
                    [
                        "for-each-ref",
                        "--format=%(refname:short)\t%(committerdate:unix)",
                        "refs/heads",
                    ],
                    errors=errors,
                    cwd=self.repo_root,
                )
                if refs_res is not None and refs_res.returncode == 0 and refs_res.stdout:
                    for raw_line in refs_res.stdout.splitlines():
                        line = raw_line.strip()
                        if not line:
                            continue
                        parts = line.split("\t", 1)
                        if len(parts) != 2:
                            continue
                        branch_name = parts[0].strip()
                        ts_raw = parts[1].strip()
                        if not branch_name:
                            continue
                        if branch_name in protected_branches:
                            skipped.append(f"branch:{branch_name}")
                            continue
                        if not self._is_temp_staging_branch(branch_name):
                            continue
                        if branch_name in active_branches:
                            skipped.append(f"branch:{branch_name}")
                            continue
                        try:
                            branch_ts = float(ts_raw)
                        except ValueError:
                            skipped.append(f"branch:{branch_name}")
                            continue
                        if (now_ts - branch_ts) < max_age:
                            skipped.append(f"branch:{branch_name}")
                            continue
                        try:
                            validated_branch = _validate_branch_name(branch_name, "temporal")
                        except ValueError:
                            skipped.append(f"branch:{branch_name}")
                            continue
                        del_res = self._run_git_safe(
                            ["branch", "-D", validated_branch],
                            errors=errors,
                            cwd=self.repo_root,
                        )
                        if del_res is not None and del_res.returncode == 0:
                            pruned_branches.append(validated_branch)

        return summary

    def _git_identity_prefix(self, cwd: Path) -> list[str]:
        name_chk = self._run_git(["config", "--get", "user.name"], cwd=cwd)
        email_chk = self._run_git(["config", "--get", "user.email"], cwd=cwd)
        if (
            name_chk.returncode == 0
            and name_chk.stdout.strip()
            and email_chk.returncode == 0
            and email_chk.stdout.strip()
        ):
            return []
        return ["-c", "user.name=AgentFactory", "-c", "user.email=agent@factory.local"]

    def _sync_empty_staging_to_base(self, worktree_path: Path) -> None:
        """Reset .liwi-staging to main HEAD before building when staging has zero pending commits."""
        if not worktree_path.is_dir() or not (worktree_path / ".git").exists():
            return
        if self._list_pending_commits():
            return
        base_head_res = self._run_git(["rev-parse", self.base_branch], cwd=self.repo_root)
        target_ref = (
            base_head_res.stdout.strip()
            if base_head_res.returncode == 0 and base_head_res.stdout.strip()
            else self.base_branch
        )
        self._run_git(["reset", "--hard", target_ref], cwd=worktree_path)
        self._run_git(["clean", "-fd"], cwd=worktree_path)

    def _commit_parallel_changes(
        self,
        worktree_path: Path,
        plan: ParallelPlan,
        *,
        commit_message: str | None = None,
    ) -> tuple[bool, str | None, str]:
        branch_res = self._run_git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=worktree_path)
        current_branch = branch_res.stdout.strip() if branch_res.returncode == 0 else ""
        if current_branch != self.staging_branch:
            return (
                False,
                None,
                f"El worktree está en '{current_branch or 'desconocido'}' en lugar de '{self.staging_branch}'.",
            )

        assigned_files = [f for st in plan.subtasks for f in st.assigned_files]
        add_args = ["add", "-A", "--", *assigned_files] if assigned_files else ["add", "-A"]
        add_res = self._run_git(add_args, cwd=worktree_path)
        if add_res.returncode != 0:
            detail = redact_secrets((add_res.stderr or add_res.stdout or "git add failed").strip())
            return False, None, f"Error preparando commit paralelo en staging: {detail}"

        diff_res = self._run_git(["diff", "--cached", "--quiet"], cwd=worktree_path)
        if diff_res.returncode != 0:
            commit_prefix = self._git_identity_prefix(worktree_path)
            slug = re.sub(r"[\r\n\"`]+", " ", plan.title).strip() or "apply parallel improvement"
            msg = commit_message.strip() if isinstance(commit_message, str) and commit_message.strip() else f"chore(self-improve): {slug}"
            commit_res = self._run_git(
                [*commit_prefix, "commit", "-m", msg],
                cwd=worktree_path,
            )
            if commit_res.returncode != 0:
                detail = redact_secrets(
                    (commit_res.stderr or commit_res.stdout or "git commit failed").strip()
                )
                return False, None, f"Error creando commit paralelo en '{self.staging_branch}': {detail}"

        head_res = self._run_git(["rev-parse", "HEAD"], cwd=worktree_path)
        commit_sha = head_res.stdout.strip() if head_res.returncode == 0 else None
        return True, commit_sha, ""

    def _get_worktree_changed_files(self, worktree_path: Path) -> list[str]:
        res = self._run_git(
            ["-c", "core.quotepath=false", "status", "--porcelain", "-uall"],
            cwd=worktree_path,
        )
        if res.returncode != 0 or not res.stdout:
            return []
        paths: list[str] = []
        for raw_line in res.stdout.splitlines():
            if len(raw_line) < 4:
                continue
            entry = raw_line[3:].strip()
            if not entry:
                continue
            if " -> " in entry:
                old_p, new_p = entry.split(" -> ", 1)
                for part in (old_p, new_p):
                    clean_part = part.strip().strip('"')
                    if clean_part:
                        paths.append(clean_part)
            else:
                clean_entry = entry.strip('"')
                if clean_entry:
                    paths.append(clean_entry)
        return paths

    def _get_staging_branch_changed_files(self) -> list[str]:
        diff_res = self._run_git(
            [
                "-c",
                "core.quotepath=false",
                "diff",
                "--name-only",
                f"{self.base_branch}...{self.staging_branch}",
            ],
            cwd=self.repo_root,
        )
        if diff_res.returncode != 0:
            diff_res = self._run_git(
                [
                    "-c",
                    "core.quotepath=false",
                    "diff",
                    "--name-only",
                    f"{self.base_branch}..{self.staging_branch}",
                ],
                cwd=self.repo_root,
            )
        if diff_res.returncode != 0 or not diff_res.stdout:
            return []
        return [
            line.strip().strip('"')
            for line in diff_res.stdout.splitlines()
            if line.strip()
        ]

    def _run_parallel_plan_in_worktree(
        self,
        worktree_path: Path,
        plan: ParallelPlan,
        *,
        commit_message: str | None = None,
        improver: ParallelImprover | None = None,
        worker_runner: WorkerRunner | None = None,
        ruff_runner: RuffRunner | None = None,
        pytest_runner: PytestRunner | None = None,
        changed_files_inspector: ChangedFilesInspector | None = None,
        max_workers: int = 3,
        cleanup_on_failure: bool = True,
        forbidden_prefixes: tuple[str, ...] | None = None,
        max_diff_files: int | None = None,
    ) -> ParallelRunResult:
        from agent_factory.parallel_improver import (
            ParallelImprover as _ParallelImprover,
        )
        from agent_factory.parallel_improver import (
            ParallelRunResult as _ParallelRunResult,
        )

        effective_forbidden = (
            tuple(forbidden_prefixes) if forbidden_prefixes is not None else self.forbidden_prefixes
        )
        effective_max_files = (
            int(max_diff_files) if max_diff_files is not None else self.max_diff_files
        )

        planned_files = [f for st in plan.subtasks for f in st.assigned_files]
        ok_plan_scope, plan_scope_err = validate_diff_scope(
            planned_files,
            forbidden_prefixes=effective_forbidden,
            max_files=effective_max_files,
        )
        if not ok_plan_scope:
            if cleanup_on_failure:
                self._cleanup_worktree()
            err_msg = redact_secrets(plan_scope_err)
            with self._lock:
                self._last_error = err_msg
            return _ParallelRunResult(
                success=False,
                plan=plan,
                worker_results=(),
                pytest_ran=False,
                pytest_ok=False,
                pytest_output="",
                target_tests=plan.target_tests,
                out_of_partition_files=(),
                error=err_msg,
                commit_allowed=False,
                commit_sha=None,
            )

        scope_blocked_errors: list[str] = []
        active_improver: ParallelImprover

        def _guarded_pytest_runner(tests: Sequence[str], wt_path: Path) -> tuple[bool, str]:
            inspector = (
                changed_files_inspector
                if changed_files_inspector is not None
                else self._get_worktree_changed_files
            )
            changed_paths = list(inspector(wt_path))
            ok_scope, scope_err = validate_diff_scope(
                changed_paths,
                forbidden_prefixes=effective_forbidden,
                max_files=effective_max_files,
            )
            if not ok_scope:
                scope_blocked_errors.append(scope_err)
                return False, scope_err
            if pytest_runner is not None:
                return pytest_runner(tests, wt_path)
            return active_improver._default_pytest_runner(tests, wt_path)

        active_improver = (
            improver
            if improver is not None
            else _ParallelImprover(
                worktree_path=worktree_path,
                max_workers=max_workers,
                worker_runner=worker_runner,
                ruff_runner=ruff_runner,
                pytest_runner=_guarded_pytest_runner,
                changed_files_inspector=changed_files_inspector,
            )
        )
        run_result = active_improver.run(plan)
        if scope_blocked_errors:
            if cleanup_on_failure:
                self._cleanup_worktree()
            err_msg = redact_secrets(scope_blocked_errors[0])
            with self._lock:
                self._last_error = err_msg
            return replace(
                run_result,
                success=False,
                pytest_ran=False,
                pytest_ok=False,
                pytest_output="",
                commit_allowed=False,
                commit_sha=None,
                error=err_msg,
            )
        if not run_result.success or not run_result.commit_allowed:
            if cleanup_on_failure:
                self._cleanup_worktree()
            err_msg = redact_secrets(run_result.error or "La ejecución paralela en staging falló.")
            with self._lock:
                self._last_error = err_msg
            return replace(run_result, success=False, commit_allowed=False, commit_sha=None, error=err_msg)

        ok_commit, commit_sha, commit_err = self._commit_parallel_changes(
            worktree_path,
            run_result.plan,
            commit_message=commit_message,
        )
        if not ok_commit:
            if cleanup_on_failure:
                self._cleanup_worktree()
            err_msg = redact_secrets(commit_err)
            with self._lock:
                self._last_error = err_msg
            return replace(
                run_result,
                success=False,
                commit_allowed=False,
                commit_sha=None,
                error=err_msg,
            )

        with self._lock:
            self._last_error = None
        return replace(run_result, commit_sha=commit_sha)

    def apply_parallel_plan(
        self,
        plan: ParallelPlan,
        *,
        commit_message: str | None = None,
        improver: ParallelImprover | None = None,
        worker_runner: WorkerRunner | None = None,
        ruff_runner: RuffRunner | None = None,
        pytest_runner: PytestRunner | None = None,
        changed_files_inspector: ChangedFilesInspector | None = None,
        max_workers: int = 3,
        cleanup_on_failure: bool = True,
    ) -> ParallelRunResult:
        """Execute a disjoint-file ParallelPlan inside .liwi-staging and commit on liwi-staging only if all checks pass."""
        with self._cond:
            if self._is_promoting:
                raise RuntimeError("No se puede ejecutar un plan paralelo mientras staging se está promoviendo.")
            if self._is_building:
                raise RuntimeError("Ya hay una mejora en construcción en staging.")
            self._is_building = True
            self._current_task = plan.title
            self._cond.notify_all()

        try:
            worktree_path = self.ensure_worktree()
            self._sync_empty_staging_to_base(worktree_path)
            return self._run_parallel_plan_in_worktree(
                worktree_path,
                plan,
                commit_message=commit_message,
                improver=improver,
                worker_runner=worker_runner,
                ruff_runner=ruff_runner,
                pytest_runner=pytest_runner,
                changed_files_inspector=changed_files_inspector,
                max_workers=max_workers,
                cleanup_on_failure=cleanup_on_failure,
            )
        finally:
            with self._cond:
                self._is_building = False
                self._current_task = None
                self._cond.notify_all()

    def _default_execute(self, worktree_path: Path, task: dict[str, Any]) -> dict[str, Any]:
        meta = task.get("metadata") if isinstance(task.get("metadata"), dict) else {}
        if meta.get("parallel_plan") is not None:
            parallel_result = self._run_parallel_plan_in_worktree(
                worktree_path,
                meta["parallel_plan"],
                commit_message=meta.get("commit_message"),
                improver=meta.get("parallel_improver"),
                worker_runner=meta.get("worker_runner"),
                ruff_runner=meta.get("ruff_runner"),
                pytest_runner=meta.get("pytest_runner"),
                changed_files_inspector=meta.get("changed_files_inspector"),
                max_workers=int(meta.get("max_workers", 3)),
            )
            if not parallel_result.success:
                raise RuntimeError(
                    parallel_result.error or "La auto-mejora paralela falló en el worktree de staging."
                )
            return {
                "ok": True,
                "commit_sha": parallel_result.commit_sha,
                "parallel_result": parallel_result,
            }

        from agent_factory.self_improver import FactorySelfImprover

        improver_kwargs: dict[str, Any] = {
            "repo_root": worktree_path,
            "restart_fn": lambda _code: None,
            "restart_delay_seconds": 0.0,
        }
        for key in (
            "sqlite_store",
            "db_path",
            "uploads_dir",
            "runner",
            "clock",
            "agy_command",
            "model",
            "secrets_to_redact",
            "apply_timeout_seconds",
            "verify_timeout_seconds",
        ):
            if key in meta and meta[key] is not None:
                improver_kwargs[key] = meta[key]

        improver = FactorySelfImprover(**improver_kwargs)
        result = improver.apply_proposal(
            str(task["prompt"]),
            proposal_title=str(task["title"]),
            message_id=str(meta["message_id"]) if meta.get("message_id") else None,
            github_token="",
            preserve_on_quota_429=True,
        )
        if not result.get("ok"):
            if result.get("quota_429") or is_quota_429_error(
                str(result.get("technical_detail") or result.get("error") or "")
            ):
                raise RuntimeError(
                    str(result.get("technical_detail") or result.get("error") or "RESOURCE_EXHAUSTED 429")
                )
            err = str(result.get("error") or "La auto-mejora falló en el worktree de staging.")
            raise RuntimeError(err)
        return result

    def _preserve_429_partial_work(
        self,
        worktree_path: Path | None,
        task: dict[str, Any],
        raw_error: str,
    ) -> None:
        """Preserve dirty .liwi-staging edits as a wip commit on 429 and schedule auto-resume."""
        if worktree_path is not None and worktree_path.is_dir():
            self._run_git(["add", "-A"], cwd=worktree_path)
            cached = self._run_git(["diff", "--cached", "--quiet"], cwd=worktree_path)
            if cached.returncode != 0:
                prefix = self._git_identity_prefix(worktree_path)
                self._run_git(
                    [*prefix, "commit", "-m", "wip(quota-pause): avance parcial"],
                    cwd=worktree_path,
                )
            diff_stat = self._run_git(
                ["diff", f"{self.base_branch}..HEAD", "--stat"],
                cwd=worktree_path,
            ).stdout.strip()
            diff_patch = self._run_git(
                ["diff", f"{self.base_branch}..HEAD"],
                cwd=worktree_path,
            ).stdout[:1800].strip()
            previous_diff = "\n".join(part for part in (diff_stat, diff_patch) if part)
        else:
            previous_diff = ""

        err_info = format_agent_error(raw_error)
        try:
            from agent_factory.idea_engine import record_agy_429_pause

            meta = task.get("metadata") if isinstance(task.get("metadata"), dict) else {}
            db_override = meta.get("db_path") if meta.get("db_path") is not None else self._db_path
            record_agy_429_pause(raw_error, db_path=db_override)
        except Exception as exc:  # noqa: BLE001
            logging.getLogger(__name__).debug("Quota 429 pause record skipped: %s", exc)

        paused_until_iso = str(err_info.get("quota_paused_until") or "")
        try:
            resume_dt = datetime.fromisoformat(paused_until_iso)
        except ValueError:
            resume_dt = datetime.now().astimezone()

        paused_entry = {
            "task": dict(task),
            "resume_at_dt": resume_dt,
            "quota_paused_until": resume_dt.isoformat(),
            "previous_diff": previous_diff,
            "friendly_error": str(err_info.get("message") or raw_error),
        }
        with self._cond:
            self._paused_tasks.append(paused_entry)
            self._last_error = str(err_info.get("message") or raw_error)
            self._cond.notify_all()

    def _worker_loop(self) -> None:
        while True:
            task: dict[str, Any] | None = None
            with self._cond:
                while not self._shutdown_requested:
                    if self._is_promoting:
                        self._cond.wait()
                        continue
                    if self._queue:
                        task = self._queue.popleft()
                        break
                    if self._paused_tasks:
                        front = self._paused_tasks[0]
                        now_dt = datetime.now().astimezone()
                        resume_dt = front["resume_at_dt"]
                        if resume_dt.tzinfo is None:
                            resume_dt = resume_dt.replace(tzinfo=now_dt.tzinfo)
                        if now_dt >= resume_dt:
                            paused = self._paused_tasks.popleft()
                            task = dict(paused["task"])
                            prev_diff = str(paused.get("previous_diff") or "").strip()
                            if prev_diff:
                                task["prompt"] = (
                                    "[REANUDACIÓN AUTOMÁTICA TRAS PAUSA DE CUOTA 429 — "
                                    "Avance previo ya guardado en commit wip(quota-pause) en .liwi-staging]\n"
                                    f"Resumen/diff acumulado:\n{prev_diff}\n\n"
                                    f"Continúa y completa la tarea original:\n{task['prompt']}"
                                )
                            break
                        wait_sec = min(2.0, max(0.05, (resume_dt - now_dt).total_seconds()))
                        self._cond.wait(timeout=wait_sec)
                        continue
                    self._cond.wait()

                if self._shutdown_requested:
                    self._is_building = False
                    self._current_task = None
                    self._cond.notify_all()
                    return

                assert task is not None
                self._is_building = True
                self._current_task = str(task["title"])
                self._cond.notify_all()

            worktree_path: Path | None = None
            try:
                worktree_path = self.ensure_worktree()
                self._sync_empty_staging_to_base(worktree_path)
                if self._executor is not None:
                    self._executor(worktree_path, task)
                else:
                    self._default_execute(worktree_path, task)
                with self._cond:
                    self._last_error = None
            except Exception as exc:  # noqa: BLE001
                raw_exc = str(exc) or type(exc).__name__
                err_msg = redact_secrets(raw_exc)
                if is_quota_429_error(err_msg):
                    self._preserve_429_partial_work(worktree_path, task, raw_exc)
                else:
                    self._cleanup_worktree()
                    with self._cond:
                        self._last_error = err_msg
            finally:
                with self._cond:
                    self._is_building = False
                    self._current_task = None
                    self._cond.notify_all()

    def enqueue_improvement(
        self,
        title: str,
        prompt: str,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Enqueue a self-improvement task for sequential execution inside .liwi-staging."""
        clean_title = str(title or "").strip()
        clean_prompt = str(prompt or "").strip()
        if not clean_title:
            raise ValueError("El título de la mejora ('title') es obligatorio.")
        if not clean_prompt:
            raise ValueError("El prompt de la mejora ('prompt') es obligatorio.")

        task: dict[str, Any] = {
            "title": clean_title,
            "prompt": clean_prompt,
            "metadata": dict(metadata) if metadata is not None else {},
        }

        with self._cond:
            if self._shutdown_requested:
                raise RuntimeError("StagingManager está detenido y no acepta nuevas mejoras.")
            self._queue.append(task)
            if self._worker is None or not self._worker.is_alive():
                self._worker = threading.Thread(
                    target=self._worker_loop,
                    name="liwi-staging-worker",
                    daemon=True,
                )
                self._worker.start()
            self._cond.notify_all()

        return self.get_staging_status()

    def wait_until_idle(self, timeout: float = 30.0) -> bool:
        """Wait until the queue is empty and no build is running. Returns False on timeout."""
        deadline = time.monotonic() + max(0.0, float(timeout))
        with self._cond:
            while self._is_building or len(self._queue) > 0:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._cond.wait(timeout=remaining)
            return True

    def shutdown(self, timeout: float = 5.0) -> None:
        """Stop the background worker cleanly without leaking threads."""
        with self._cond:
            self._shutdown_requested = True
            self._cond.notify_all()
            worker = self._worker
        if worker is not None and worker.is_alive():
            worker.join(timeout=max(0.0, float(timeout)))

    def _list_pending_commits(self) -> list[dict[str, str]]:
        log_res = self._run_git(
            ["log", "--format=%h%x1f%s", f"{self.base_branch}..{self.staging_branch}"],
            cwd=self.repo_root,
        )
        if log_res.returncode != 0 or not log_res.stdout:
            return []
        commits: list[dict[str, str]] = []
        for line in log_res.stdout.splitlines():
            if not line.strip():
                continue
            parts = line.split("\x1f", 1)
            if len(parts) != 2:
                continue
            commits.append(
                {
                    "hash": redact_secrets(parts[0].strip()),
                    "title": redact_secrets(parts[1].strip()),
                }
            )
        return commits

    def get_staging_status(self) -> dict[str, Any]:
        """Return JSON-serializable staging status including pending commits and worker state."""
        commits = self._list_pending_commits()
        with self._lock:
            paused = self._paused_tasks[0] if self._paused_tasks else None
            return {
                "pending_count": len(commits),
                "commits": commits,
                "is_building": bool(self._is_building),
                "queue_size": len(self._queue),
                "paused_count": len(self._paused_tasks),
                "paused_until": paused["quota_paused_until"] if paused else None,
                "quota_paused_message": paused["friendly_error"] if paused else None,
                "last_error": self._last_error,
                "current_task": self._current_task,
            }

    def promote(
        self,
        trigger_restart: bool = True,
        *,
        rebase_on_diverge: bool = True,
    ) -> dict[str, Any]:
        """Promote liwi-staging commits to main, rebasing diverged commits cleanly before ff-only merge."""
        return self.promote_staging_to_main(
            trigger_restart=trigger_restart,
            rebase_on_diverge=rebase_on_diverge,
        )

    def promote_staging_to_main(
        self,
        trigger_restart: bool = True,
        *,
        rebase_on_diverge: bool = False,
    ) -> dict[str, Any]:
        """Fast-forward merge liwi-staging into main (rebasing if requested on diverge), sync worktree, and optionally restart."""
        with self._cond:
            if self._is_building or len(self._queue) > 0:
                raise RuntimeError(
                    "No se puede promover staging a main mientras hay mejoras en construcción o en cola."
                )
            if self._is_promoting:
                raise RuntimeError("Ya hay una promoción de staging a main en ejecución.")
            self._is_promoting = True

        should_restart = False
        result: dict[str, Any] = {"promoted": False, "merged_commits": 0, "commits": []}

        try:
            commits = self._list_pending_commits()
            if not commits:
                return result

            changed_paths = self._get_staging_branch_changed_files()
            scope_ok, scope_err = validate_diff_scope(
                changed_paths,
                forbidden_prefixes=self.forbidden_prefixes,
                max_files=self.max_diff_files,
            )
            if not scope_ok:
                err_msg = redact_secrets(
                    f"Promoción bloqueada por guardián de alcance del diff: {scope_err}"
                )
                with self._lock:
                    self._last_error = err_msg
                raise RuntimeError(err_msg)

            branch_res = self._run_git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=self.repo_root)
            current_branch = branch_res.stdout.strip() if branch_res.returncode == 0 else ""
            if current_branch != self.base_branch:
                err_msg = (
                    f"El repositorio principal está en '{current_branch or 'desconocido'}' "
                    f"en lugar de '{self.base_branch}'."
                )
                with self._lock:
                    self._last_error = err_msg
                raise RuntimeError(err_msg)

            merge_res = self._run_git(
                ["merge", "--ff-only", self.staging_branch],
                cwd=self.repo_root,
            )
            if merge_res.returncode != 0 and rebase_on_diverge:
                ancestor_chk = self._run_git(
                    ["merge-base", "--is-ancestor", self.base_branch, self.staging_branch],
                    cwd=self.repo_root,
                )
                if ancestor_chk.returncode != 0:
                    worktree_path = self.ensure_worktree()
                    self._cleanup_worktree()
                    rebase_prefix = self._git_identity_prefix(worktree_path)
                    rebase_res = self._run_git(
                        [*rebase_prefix, "rebase", self.base_branch],
                        cwd=worktree_path,
                    )
                    if rebase_res.returncode != 0:
                        self._run_git(["rebase", "--abort"], cwd=worktree_path)
                        detail = redact_secrets(
                            (rebase_res.stderr or rebase_res.stdout or "git rebase failed").strip()
                        )
                        err_msg = (
                            f"Conflicto real al hacer rebase de '{self.staging_branch}' "
                            f"sobre '{self.base_branch}' (rebase abortado): {detail}"
                        )
                        with self._lock:
                            self._last_error = err_msg
                        raise RuntimeError(err_msg)

                    commits = self._list_pending_commits()
                    if not commits:
                        with self._lock:
                            self._last_error = None
                        return result
                    merge_res = self._run_git(
                        ["merge", "--ff-only", self.staging_branch],
                        cwd=self.repo_root,
                    )

            if merge_res.returncode != 0:
                detail = redact_secrets(
                    (merge_res.stderr or merge_res.stdout or "git merge --ff-only failed").strip()
                )
                err_msg = (
                    f"Falló el merge fast-forward de '{self.staging_branch}' "
                    f"en '{self.base_branch}': {detail}"
                )
                with self._lock:
                    self._last_error = err_msg
                raise RuntimeError(err_msg)

            head_res = self._run_git(["rev-parse", "HEAD"], cwd=self.repo_root)
            new_head = head_res.stdout.strip() if head_res.returncode == 0 else ""

            if self._is_valid_worktree() and new_head:
                self._run_git(["reset", "--hard", new_head], cwd=self.worktree_path)
                self._run_git(["clean", "-fd"], cwd=self.worktree_path)
            else:
                self.ensure_worktree()

            with self._lock:
                self._last_error = None

            result = {
                "promoted": True,
                "merged_commits": len(commits),
                "commits": commits,
            }
            should_restart = bool(trigger_restart and self._on_restart is not None)
        finally:
            with self._cond:
                self._is_promoting = False
                self._cond.notify_all()

        if should_restart and self._on_restart is not None:
            self._on_restart()

        return result


def get_staging_manager(
    repo_root: Path | str | None = None,
    *,
    worktree_name: str = ".liwi-staging",
    staging_branch: str = "liwi-staging",
    base_branch: str = "main",
    executor: Callable[[Path, dict[str, Any]], Any] | None = None,
    on_restart: Callable[[], None] | None = None,
    reset: bool = False,
) -> StagingManager:
    """Return a thread-safe singleton StagingManager per resolved repo_root."""
    resolved = (
        Path(repo_root).resolve()
        if repo_root is not None
        else Path(__file__).resolve().parents[2]
    )
    old_manager: StagingManager | None = None
    with _MANAGERS_LOCK:
        if reset and resolved in _MANAGERS:
            old_manager = _MANAGERS.pop(resolved)
        manager = _MANAGERS.get(resolved)
        if manager is None:
            manager = StagingManager(
                repo_root=resolved,
                worktree_name=worktree_name,
                staging_branch=staging_branch,
                base_branch=base_branch,
                executor=executor,
                on_restart=on_restart,
            )
            _MANAGERS[resolved] = manager
    if old_manager is not None:
        old_manager.shutdown()
    return manager
