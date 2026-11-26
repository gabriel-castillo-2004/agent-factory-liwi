"""Cumulative background self-improvement staging manager using git worktree (.liwi-staging)."""

from __future__ import annotations

import os
import re
import shutil
import stat
import subprocess
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

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

_MANAGERS_LOCK = threading.Lock()
_MANAGERS: dict[Path, StagingManager] = {}


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

        self._lock = threading.Lock()
        self._cond = threading.Condition(self._lock)
        self._worktree_setup_lock = threading.RLock()
        self._queue: deque[dict[str, Any]] = deque()
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
            name_chk = self._run_git(["config", "--get", "user.name"], cwd=worktree_path)
            email_chk = self._run_git(["config", "--get", "user.email"], cwd=worktree_path)
            commit_prefix = (
                []
                if (
                    name_chk.returncode == 0
                    and name_chk.stdout.strip()
                    and email_chk.returncode == 0
                    and email_chk.stdout.strip()
                )
                else ["-c", "user.name=AgentFactory", "-c", "user.email=agent@factory.local"]
            )
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
    ) -> ParallelRunResult:
        from agent_factory.parallel_improver import ParallelImprover as _ParallelImprover

        active_improver = (
            improver
            if improver is not None
            else _ParallelImprover(
                worktree_path=worktree_path,
                max_workers=max_workers,
                worker_runner=worker_runner,
                ruff_runner=ruff_runner,
                pytest_runner=pytest_runner,
                changed_files_inspector=changed_files_inspector,
            )
        )
        run_result = active_improver.run(plan)
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
        )
        if not result.get("ok"):
            err = str(result.get("error") or "La auto-mejora falló en el worktree de staging.")
            raise RuntimeError(err)
        return result

    def _worker_loop(self) -> None:
        while True:
            with self._cond:
                while (not self._queue or self._is_promoting) and not self._shutdown_requested:
                    self._cond.wait()
                if self._shutdown_requested:
                    self._is_building = False
                    self._current_task = None
                    self._cond.notify_all()
                    return
                if self._is_promoting:
                    continue
                task = self._queue.popleft()
                self._is_building = True
                self._current_task = str(task["title"])
                self._cond.notify_all()

            try:
                worktree_path = self.ensure_worktree()
                if self._executor is not None:
                    self._executor(worktree_path, task)
                else:
                    self._default_execute(worktree_path, task)
                with self._cond:
                    self._last_error = None
            except Exception as exc:  # noqa: BLE001
                err_msg = redact_secrets(str(exc) or type(exc).__name__)
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
            return {
                "pending_count": len(commits),
                "commits": commits,
                "is_building": bool(self._is_building),
                "queue_size": len(self._queue),
                "last_error": self._last_error,
                "current_task": self._current_task,
            }

    def promote_staging_to_main(self, trigger_restart: bool = True) -> dict[str, Any]:
        """Fast-forward merge liwi-staging into main, sync worktree, and optionally trigger restart."""
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
