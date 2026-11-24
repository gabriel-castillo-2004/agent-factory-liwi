"""Tests for real GitHubVCSAdapter and StaticDeployPublisher via Runner with clean env and secret redaction (AC-4.4, INV-07, INV-19, INV-22, INV-23)."""

import json
import os
import shutil
import subprocess
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest

from agent_factory.adapters.fake_runner import FakeRunner
from agent_factory.adapters.github_vcs_adapter import GitHubVCSAdapter, VcsError
from agent_factory.adapters.static_deploy_publisher import (
    StaticDeployError,
    StaticDeployPublisher,
)
from agent_factory.adapters.subprocess_runner import SubprocessRunner
from agent_factory.domain.models import ProjectLinks, ProjectStatus
from agent_factory.domain.states import ProjectState
from agent_factory.ports.notifier import HttpResponse
from agent_factory.ports.runner import CommandResult, CommandTimeoutError
from agent_factory.ports.vcs import VCS


def test_github_vcs_and_static_deploy_run_with_clean_env_via_runner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC-4.4 positive (INV-02, INV-19, INV-22, INV-23): Strips unrelated host secrets from Runner env, pushes branch, checks CI, and deploys static artifact."""
    host_tg = "TEST_HOST_TG_SECRET_111"
    host_llm = "TEST_HOST_LLM_SECRET_222"
    gh_tok = "TEST_SCOPED_GH_TOKEN_333"
    vc_tok = "TEST_SCOPED_VC_TOKEN_444"

    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", host_tg)
    monkeypatch.setenv("LLM_API_KEY", host_llm)
    monkeypatch.setenv("OPENAI_API_KEY", host_llm)

    ws_dir = tmp_path / "ws" / "app-vcs-ok"
    ws_dir.mkdir(parents=True)
    (ws_dir / "index.html").write_text("<h1>Hola</h1>\n", encoding="utf-8")

    deploy_runner = FakeRunner(
        results=[CommandResult(exit_code=0, stdout="https://acme-org.github.io/app-vcs-ok\n")]
    )
    deployer = StaticDeployPublisher(
        runner=deploy_runner,
        target="github-pages",
        deploy_base_url="https://acme-org.github.io",
        branch="gh-pages",
        deploy_token=vc_tok,
        secrets=(vc_tok,),
    )

    clean_deploy_env = deployer.build_clean_env()
    assert "TELEGRAM_BOT_TOKEN" not in clean_deploy_env
    assert "LLM_API_KEY" not in clean_deploy_env
    assert "OPENAI_API_KEY" not in clean_deploy_env

    deploy_url = deployer.deploy_site("app-vcs-ok", ws_dir)
    assert deploy_url == "https://acme-org.github.io/app-vcs-ok"
    assert len(deploy_runner.calls) == 1
    assert "TELEGRAM_BOT_TOKEN" not in (deploy_runner.calls[0]["env"] or {})
    assert "LLM_API_KEY" not in (deploy_runner.calls[0]["env"] or {})

    vcs_runner = FakeRunner(
        results=[
            # push_branch calls (e.g., git add/commit/push or git push)
            CommandResult(exit_code=0, stdout="deadbeef1234\n"),
            # get_ci_result passing
            CommandResult(exit_code=0, stdout='{"passed": true, "summary": "CI passed", "failed_checks": []}'),
            # get_ci_result failing
            CommandResult(exit_code=0, stdout='{"passed": false, "summary": "Playwright failed", "failed_checks": ["e2e"]}'),
            # merge_and_deploy git commands
            CommandResult(exit_code=0, stdout="merged\n"),
        ]
    )
    vcs = GitHubVCSAdapter(
        runner=vcs_runner,
        owner="acme-org",
        repo_base_url="https://github.com",
        deploy_base_url="https://acme-org.github.io",
        github_token=gh_tok,
        secrets=(gh_tok, vc_tok),
    )
    assert isinstance(vcs, VCS)

    clean_vcs_env = vcs.build_clean_env()
    assert "TELEGRAM_BOT_TOKEN" not in clean_vcs_env
    assert "LLM_API_KEY" not in clean_vcs_env
    assert "OPENAI_API_KEY" not in clean_vcs_env

    push_ref = vcs.push_branch("app-vcs-ok", ws_dir, branch="work")
    assert isinstance(push_ref, str) and len(push_ref) > 0
    assert "TELEGRAM_BOT_TOKEN" not in (vcs_runner.calls[0]["env"] or {})

    ci_ok = vcs.get_ci_result("app-vcs-ok", branch="work")
    assert ci_ok.passed is True
    assert ci_ok.summary == "CI passed"

    ci_fail = vcs.get_ci_result("app-vcs-ok", branch="work")
    assert ci_fail.passed is False
    assert ci_fail.failed_checks == ["e2e"]

    repo_url, deployed_url = vcs.merge_and_deploy("app-vcs-ok", branch="work")
    assert repo_url == "https://github.com/acme-org/app-vcs-ok"
    assert deployed_url == "https://acme-org.github.io/app-vcs-ok"


def test_github_vcs_and_static_deploy_reject_invalid_ids_and_redact_tokens_on_failure(
    tmp_path: Path,
) -> None:
    """AC-4.4 negative (INV-07, INV-19, INV-22, INV-23): Rejects invalid project_id/branch flags before Runner and redacts GITHUB_TOKEN/VERCEL_TOKEN on errors."""
    gh_tok = "TEST_GH_SECRET_FAIL_555"
    vc_tok = "TEST_VC_SECRET_FAIL_666"
    ws_dir = tmp_path / "ws" / "app-bad"
    ws_dir.mkdir(parents=True)

    guard_runner = FakeRunner()
    vcs_guard = GitHubVCSAdapter(runner=guard_runner, github_token=gh_tok, secrets=(gh_tok,))
    deploy_guard = StaticDeployPublisher(runner=guard_runner, deploy_token=vc_tok, secrets=(vc_tok,))

    for bad_pid in ("../escape", "UPPER", "", "a/b"):
        with pytest.raises(ValueError):
            vcs_guard.push_branch(bad_pid, ws_dir, branch="work")
        with pytest.raises(ValueError):
            vcs_guard.get_ci_result(bad_pid, branch="work")
        with pytest.raises(ValueError):
            vcs_guard.merge_and_deploy(bad_pid, branch="work")
        with pytest.raises(ValueError):
            deploy_guard.deploy_site(bad_pid, ws_dir)

    with pytest.raises(ValueError):
        vcs_guard.push_branch("app-ok", ws_dir, branch="-force")
    with pytest.raises(ValueError):
        deploy_guard.deploy_site("app-ok", tmp_path / "does-not-exist")
    assert len(guard_runner.calls) == 0

    failing_runner = FakeRunner(
        results=[
            CommandResult(
                exit_code=128,
                stderr=f"fatal: https://x-access-token:{gh_tok}@github.com/o/r.git denied GITHUB_TOKEN={gh_tok}",
            ),
            CommandTimeoutError(f"Vercel deploy timed out VERCEL_TOKEN={vc_tok}"),
        ]
    )
    vcs_fail = GitHubVCSAdapter(runner=failing_runner, github_token=gh_tok, secrets=(gh_tok,))
    deploy_fail = StaticDeployPublisher(runner=failing_runner, deploy_token=vc_tok, secrets=(vc_tok,))

    with pytest.raises(VcsError) as vcs_err:
        vcs_fail.push_branch("app-ok", ws_dir, branch="work")
    assert gh_tok not in str(vcs_err.value)
    assert "[REDACTED]" in str(vcs_err.value)

    with pytest.raises(StaticDeployError) as dep_err:
        deploy_fail.deploy_site("app-ok", ws_dir)
    assert vc_tok not in str(dep_err.value)
    assert "[REDACTED]" in str(dep_err.value)


def test_github_vcs_ci_merge_commands_json_variants_and_clean_env_everywhere(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """H4-02 / M4-22 / M4-23 (INV-02, INV-19, INV-22, INV-23): get_ci_result and merge_and_deploy pass owner/project_id, clean env across ALL calls, parse gh list JSON, and redact errors."""
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "HOST_TG_SECRET_111")
    monkeypatch.setenv("LLM_API_KEY", "HOST_LLM_SECRET_222")
    gh_tok = "TEST_GH_CI_TOK_333"

    ws_dir = tmp_path / "ws" / "app-ci-adv"
    ws_dir.mkdir(parents=True)

    runner = FakeRunner(
        results=[
            # push_branch
            CommandResult(exit_code=0, stdout="deadbeef\n"),
            # get_ci_result: gh run list JSON array success
            CommandResult(exit_code=0, stdout=json.dumps([{"status": "completed", "conclusion": "success"}])),
            # get_ci_result: gh run list JSON array failure
            CommandResult(exit_code=0, stdout=json.dumps([{"status": "completed", "conclusion": "failure"}])),
            # get_ci_result: invalid / misleading non-JSON stdout MUST NOT falsely pass
            CommandResult(exit_code=0, stdout="unexpected error bypass"),
            # merge_and_deploy
            CommandResult(exit_code=0, stdout="merged\n"),
            # get_ci_result non-zero error leaking token (kills M4-23)
            CommandResult(exit_code=1, stderr=f"gh auth failed GITHUB_TOKEN={gh_tok}"),
            # get_ci_result timeout leaking token
            CommandTimeoutError(f"gh run timed out {gh_tok}"),
            # merge_and_deploy error leaking token
            CommandResult(exit_code=1, stderr=f"gh pr merge failed {gh_tok}"),
        ]
    )
    vcs = GitHubVCSAdapter(
        runner=runner,
        owner="acme-org",
        github_token=gh_tok,
        secrets=(gh_tok,),
    )

    vcs.push_branch("app-ci-adv", ws_dir, branch="work")
    ci_arr_ok = vcs.get_ci_result("app-ci-adv", branch="work")
    assert ci_arr_ok.passed is True

    ci_arr_fail = vcs.get_ci_result("app-ci-adv", branch="work")
    assert ci_arr_fail.passed is False

    # Invalid / unrecognized non-JSON output must NEVER return passed=True
    try:
        ci_bad = vcs.get_ci_result("app-ci-adv", branch="work")
        assert ci_bad.passed is False
    except VcsError as bad_json_err:
        assert gh_tok not in str(bad_json_err)

    repo_url, deploy_url = vcs.merge_and_deploy("app-ci-adv", branch="work")
    assert repo_url == "https://github.com/acme-org/app-ci-adv"
    assert deploy_url == "https://acme-org.github.io/app-ci-adv"

    # Verify commands reference acme-org/app-ci-adv, avoid invalid `gh run view --branch`, and EVERY call has clean env (kills M4-22)
    for call in runner.calls:
        assert call["env"] is not None
        assert "TELEGRAM_BOT_TOKEN" not in call["env"]
        assert "LLM_API_KEY" not in call["env"]

    ci_cmd = runner.calls[1]["command"]
    assert "acme-org/app-ci-adv" in " ".join(ci_cmd)
    assert not ("view" in ci_cmd and "--branch" in ci_cmd)
    merge_cmd = runner.calls[4]["command"]
    assert "acme-org/app-ci-adv" in " ".join(merge_cmd)

    # Error redaction on get_ci_result (kills M4-23) & merge_and_deploy
    for action in (
        lambda: vcs.get_ci_result("app-ci-adv", branch="work"),
        lambda: vcs.get_ci_result("app-ci-adv", branch="work"),
        lambda: vcs.merge_and_deploy("app-ci-adv", branch="work"),
    ):
        with pytest.raises(VcsError) as exc_info:
            action()
        assert gh_tok not in str(exc_info.value)
        assert exc_info.value.__cause__ is None
        assert exc_info.value.__context__ is None


def test_static_deploy_and_vcs_reject_unsafe_branch_scrub_context_and_own_tokens(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """H4-03 / H4-05 / H4-06 (INV-07, INV-19, INV-22, INV-23): deploy_site validates branch, export_panel_bundle scrubs registry.json, and own tokens + __context__ are redacted."""
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("VERCEL_TOKEN", raising=False)

    ws_dir = tmp_path / "ws" / "app-sec"
    ws_dir.mkdir(parents=True)
    (ws_dir / "index.html").write_text("<h1>OK</h1>", encoding="utf-8")

    # H4-03: Unsafe or protected branch values must fail BEFORE calling Runner
    guard_runner = FakeRunner()
    guard_deployer = StaticDeployPublisher(runner=guard_runner)
    for bad_branch in ("--exec=evil", "-force", "main", "master", "refs/heads/main", "a..b", "bad branch", ""):
        with pytest.raises(ValueError):
            guard_deployer.deploy_site("app-sec", ws_dir, branch=bad_branch)
    assert len(guard_runner.calls) == 0

    # H4-05: export_panel_bundle must scrub tokens from registry.json (name, objective, repo_url)
    sec_tok = "TEST_REG_JSON_SECRET_888"
    status_with_leak = ProjectStatus(
        project_id="app-sec",
        name=f"App {sec_tok}",
        objective=f"https://example.com?token={sec_tok}",
        state=ProjectState.DELIVERED,
        progress_pct=100,
        links=ProjectLinks(
            repo=f"https://github.com/o/r?api_key={sec_tok}",
            deploy="https://o.github.io/app-sec",
        ),
    )
    bundle_dir = StaticDeployPublisher(secrets=(sec_tok,)).export_panel_bundle(
        tmp_path / "panel_out",
        statuses=[status_with_leak],
        panel_source_dir=Path(__file__).resolve().parent.parent / "panel",
    )
    reg_text = (bundle_dir / "registry.json").read_text(encoding="utf-8")
    assert sec_tok not in reg_text

    # H4-06: GitHubVCSAdapter & StaticDeployPublisher redact their own token even without explicit secrets=(), and clear __cause__/__context__
    own_gh = "CUSTOM_OWN_GH_SECRET_901"
    own_vc = "CUSTOM_OWN_VC_SECRET_902"

    vcs_runner = FakeRunner(
        results=[
            CommandResult(exit_code=1, stderr=f"rejected bare {own_gh}"),
            CommandTimeoutError(f"timeout raw {own_gh}"),
        ]
    )
    vcs_own = GitHubVCSAdapter(runner=vcs_runner, github_token=own_gh)
    for _ in range(2):
        with pytest.raises(VcsError) as v_err:
            vcs_own.push_branch("app-sec", ws_dir, branch="work")
        assert own_gh not in str(v_err.value)
        assert v_err.value.__cause__ is None
        assert v_err.value.__context__ is None

    dep_runner = FakeRunner(
        results=[
            CommandResult(exit_code=1, stderr=f"vercel rejected {own_vc}"),
            CommandTimeoutError(f"vercel timeout {own_vc}"),
        ]
    )
    dep_own = StaticDeployPublisher(runner=dep_runner, target="vercel", deploy_token=own_vc)
    for _ in range(2):
        with pytest.raises(StaticDeployError) as d_err:
            dep_own.deploy_site("app-sec", ws_dir)
        assert own_vc not in str(d_err.value)
        assert d_err.value.__cause__ is None
        assert d_err.value.__context__ is None


def test_github_vcs_merge_deploys_via_deploy_publisher_and_static_deploy_routes_target_and_strict_branches(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """H4-02 / H4-03 (INV-02, INV-19, INV-22, INV-23): merge_and_deploy invokes deploy_publisher, deploy_site routes vercel vs gh-pages, and strict branch validation rejects case/remote/separator bypasses."""
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "HOST_TG_SECRET_DEPLOY_1")
    monkeypatch.setenv("LLM_API_KEY", "HOST_LLM_SECRET_DEPLOY_2")

    vc_tok = "TEST_VERCEL_DEPLOY_TOK_333"
    gh_tok = "TEST_GH_MERGE_DEPLOY_TOK_444"
    ws_dir = tmp_path / "ws" / "app-mv"
    ws_dir.mkdir(parents=True)
    (ws_dir / "index.html").write_text("<h1>Live</h1>\n", encoding="utf-8")

    # 1. H4-03: Case/remote/colon/backslash branches rejected BEFORE Runner
    guard_runner = FakeRunner()
    guard_deployer = StaticDeployPublisher(runner=guard_runner)
    for bad_branch in (
        "MAIN",
        "Main",
        "origin/main",
        "refs/heads/MAIN",
        "refs/heads/MASTER",
        "feat:bad",
        "bad\\branch",
        "team/master",
    ):
        with pytest.raises(ValueError):
            guard_deployer.deploy_site("app-mv", ws_dir, branch=bad_branch)
    assert len(guard_runner.calls) == 0

    # 2. StaticDeployPublisher target routing: vercel vs gh-pages / github-pages
    vercel_runner = FakeRunner(results=[CommandResult(exit_code=0, stdout="https://acme-custom.vercel.app/app-mv\n")])
    vercel_pub = StaticDeployPublisher(
        runner=vercel_runner,
        target="vercel",
        deploy_base_url="https://acme-custom.vercel.app",
        deploy_token=vc_tok,
    )
    vercel_url = vercel_pub.deploy_site("app-mv", ws_dir)
    assert vercel_url == "https://acme-custom.vercel.app/app-mv"
    assert len(vercel_runner.calls) == 1
    v_call = vercel_runner.calls[0]
    assert v_call["command"][0].lower() == "vercel"
    assert v_call["env"] is not None
    assert "TELEGRAM_BOT_TOKEN" not in v_call["env"]
    assert "--token" in v_call["command"] or v_call["env"].get("VERCEL_TOKEN") == vc_tok

    for gh_target in ("gh-pages", "github-pages"):
        gh_pages_runner = FakeRunner(results=[CommandResult(exit_code=0, stdout="deployed\n")])
        gh_pages_pub = StaticDeployPublisher(
            runner=gh_pages_runner,
            target=gh_target,
            deploy_base_url="https://acme-org.github.io",
            deploy_token=gh_tok,
        )
        gh_url = gh_pages_pub.deploy_site("app-mv", ws_dir)
        assert gh_url == "https://acme-org.github.io/app-mv"
        assert gh_pages_runner.calls[0]["command"][:3] == ["gh", "pages", "deploy"]

    # 3. H4-02: GitHubVCSAdapter.merge_and_deploy invokes self.deploy_publisher and returns its URL
    shared_runner = FakeRunner(
        results=[
            CommandResult(exit_code=0, stdout="pushed\n"),
            CommandResult(exit_code=0, stdout="merged\n"),
            CommandResult(exit_code=0, stdout="https://acme-custom.vercel.app/app-mv\n"),
        ]
    )
    wired_deployer = StaticDeployPublisher(
        runner=shared_runner,
        target="vercel",
        deploy_base_url="https://acme-custom.vercel.app",
        deploy_token=vc_tok,
    )
    vcs = GitHubVCSAdapter(
        runner=shared_runner,
        owner="acme-org",
        github_token=gh_tok,
        deploy_publisher=wired_deployer,
    )
    vcs.push_branch("app-mv", ws_dir, branch="work")
    repo_url, deploy_url = vcs.merge_and_deploy("app-mv", branch="work")
    assert repo_url == "https://github.com/acme-org/app-mv"
    assert deploy_url == "https://acme-custom.vercel.app/app-mv"
    assert len(shared_runner.calls) == 3
    assert shared_runner.calls[1]["command"][:3] == ["gh", "pr", "merge"]
    assert shared_runner.calls[2]["command"][0].lower() == "vercel"


def test_github_vcs_prepares_dirty_git_repo_ensures_pr_deploys_site_branch_and_keeps_vercel_token_out_of_argv(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """H42-03 (INV-07, INV-19, INV-22, INV-23): push_branch stages/commits dirty git changes and creates PR before merge; merge_and_deploy uses deployer branch (gh-pages, not work); vercel passes token only via env."""
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "HOST_TG_SECRET_H4203")
    monkeypatch.setenv("LLM_API_KEY", "HOST_LLM_SECRET_H4203")

    gh_tok = "TEST_GH_H4203_SECRET_111"
    vc_tok = "TEST_VC_H4203_SECRET_222"

    # Initialize real local bare remote + dirty workspace branch `work`
    bare_remote = tmp_path / "app-remote.git"
    subprocess.run(["git", "init", "--bare", str(bare_remote)], check=True, capture_output=True)
    ws_dir = tmp_path / "ws" / "app-git-flow"
    ws_dir.mkdir(parents=True)
    subprocess.run(["git", "-C", str(ws_dir), "init", "-b", "main"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(ws_dir), "config", "user.name", "AgentFactory"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(ws_dir), "config", "user.email", "af@local"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(ws_dir), "remote", "add", "origin", str(bare_remote)], check=True, capture_output=True)
    (ws_dir / "README.md").write_text("# Init\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(ws_dir), "add", "README.md"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(ws_dir), "commit", "-m", "init"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(ws_dir), "push", "-u", "origin", "main"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(ws_dir), "checkout", "-b", "work"], check=True, capture_output=True)

    # Dirty uncommitted changes in working tree
    (ws_dir / "src").mkdir(parents=True)
    (ws_dir / "src" / "app.js").write_text("export const x = 42;\n", encoding="utf-8")

    real_runner = SubprocessRunner(default_timeout_seconds=15.0)
    calls: list[dict[str, Any]] = []

    class _GitAndMockGhRunner:
        def run(
            self,
            command: Sequence[str],
            cwd: Path,
            timeout_seconds: float = 30.0,
            env: Mapping[str, str] | None = None,
        ) -> CommandResult:
            calls.append(
                {
                    "command": list(command),
                    "cwd": Path(cwd),
                    "env": dict(env) if env is not None else None,
                }
            )
            if command[0] == "git":
                return real_runner.run(command, cwd, timeout_seconds=timeout_seconds, env=env)
            if command[:3] == ["gh", "pr", "list"]:
                return CommandResult(exit_code=0, stdout="[]")
            if command[:3] == ["gh", "pr", "create"]:
                return CommandResult(exit_code=0, stdout="https://github.com/acme-org/app-git-flow/pull/1\n")
            return CommandResult(exit_code=0, stdout="ok\n")

    hybrid_runner = _GitAndMockGhRunner()
    deployer = StaticDeployPublisher(
        runner=hybrid_runner,
        target="github-pages",
        deploy_base_url="https://acme-org.github.io",
        branch="gh-pages",
        deploy_token=gh_tok,
        secrets=(gh_tok,),
    )
    vcs = GitHubVCSAdapter(
        runner=hybrid_runner,
        owner="acme-org",
        github_token=gh_tok,
        secrets=(gh_tok,),
        deploy_publisher=deployer,
    )

    vcs.push_branch("app-git-flow", ws_dir, branch="work")
    # Uncommitted file MUST have been committed and pushed onto bare remote refs/heads/work
    remote_app = subprocess.run(
        ["git", "-C", str(bare_remote), "show", "refs/heads/work:src/app.js"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert remote_app == "export const x = 42;\n"

    repo_url, deploy_url = vcs.merge_and_deploy("app-git-flow", branch="work")
    assert repo_url == "https://github.com/acme-org/app-git-flow"
    assert deploy_url == "https://acme-org.github.io/app-git-flow"

    cmd_strings = [" ".join(c["command"]) for c in calls]
    add_idxs = [i for i, s in enumerate(cmd_strings) if s.startswith("git add")]
    commit_idxs = [i for i, s in enumerate(cmd_strings) if s.startswith("git commit")]
    push_idxs = [i for i, s in enumerate(cmd_strings) if s.startswith("git push")]
    pr_create_idxs = [i for i, c in enumerate(calls) if c["command"][:3] == ["gh", "pr", "create"]]
    pr_merge_idxs = [i for i, c in enumerate(calls) if c["command"][:3] == ["gh", "pr", "merge"]]
    deploy_idxs = [i for i, c in enumerate(calls) if c["command"][:3] == ["gh", "pages", "deploy"]]

    assert add_idxs and commit_idxs and push_idxs
    assert add_idxs[0] < commit_idxs[0] < push_idxs[0]
    assert pr_create_idxs and pr_merge_idxs
    assert pr_create_idxs[0] < pr_merge_idxs[0]
    assert deploy_idxs
    deploy_cmd = calls[deploy_idxs[0]]["command"]
    assert "--branch" in deploy_cmd
    branch_arg = deploy_cmd[deploy_cmd.index("--branch") + 1]
    assert branch_arg == "gh-pages"
    assert "work" not in deploy_cmd

    # StaticDeployPublisher vercel MUST NOT expose deploy_token in CLI argv (--token)
    vc_runner = FakeRunner(results=[CommandResult(exit_code=0, stdout="https://acme-vc.vercel.app/app-vc-sec\n")])
    vc_pub = StaticDeployPublisher(
        runner=vc_runner,
        target="vercel",
        deploy_base_url="https://acme-vc.vercel.app",
        deploy_token=vc_tok,
    )
    vc_pub.deploy_site("app-vc-sec", ws_dir)
    vc_call = vc_runner.calls[0]
    assert vc_call["command"][0].lower() == "vercel"
    assert "--token" not in vc_call["command"]
    assert vc_tok not in " ".join(vc_call["command"])
    assert vc_call["env"] is not None
    assert vc_call["env"].get("VERCEL_TOKEN") == vc_tok


def test_github_vcs_push_branch_passes_git_identity_and_raises_vcs_error_on_commit_failure(
    tmp_path: Path,
) -> None:
    """H43-03 (INV-07, INV-15, INV-19, INV-23): push_branch passes -c user.name=... and -c user.email=... to git commit (working without global gitconfig) and raises sanitized VcsError if git commit fails."""
    gh_tok = "TEST_GH_H4303_SECRET_5566"
    bare_remote = tmp_path / "ident-remote.git"
    subprocess.run(["git", "init", "--bare", str(bare_remote)], check=True, capture_output=True)

    ws_dir = tmp_path / "ws" / "app-git-ident"
    ws_dir.mkdir(parents=True)
    subprocess.run(["git", "-C", str(ws_dir), "init", "-b", "main"], check=True, capture_output=True)
    (ws_dir / "README.md").write_text("# Init\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(ws_dir), "add", "README.md"], check=True, capture_output=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(ws_dir),
            "-c",
            "user.name=Init",
            "-c",
            "user.email=init@local",
            "commit",
            "-m",
            "init",
        ],
        check=True,
        capture_output=True,
    )
    subprocess.run(["git", "-C", str(ws_dir), "remote", "add", "origin", str(bare_remote)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(ws_dir), "checkout", "-b", "work"], check=True, capture_output=True)

    # Dirty file in workspace without any user.name/email configured in repo or isolated HOME
    (ws_dir / "src").mkdir(parents=True)
    (ws_dir / "src" / "main.js").write_text("export const ready = true;\n", encoding="utf-8")
    empty_home = tmp_path / "empty_home"
    empty_home.mkdir()

    real_runner = SubprocessRunner(default_timeout_seconds=15.0)
    recorded_cmds: list[list[str]] = []

    class _IsolatedGitRunner:
        def run(
            self,
            command: Sequence[str],
            cwd: Path,
            timeout_seconds: float = 30.0,
            env: Mapping[str, str] | None = None,
        ) -> CommandResult:
            recorded_cmds.append(list(command))
            isolated_env = dict(env or {})
            isolated_env["HOME"] = str(empty_home)
            isolated_env["USERPROFILE"] = str(empty_home)
            isolated_env["GIT_CONFIG_NOSYSTEM"] = "1"
            isolated_env["GIT_CONFIG_GLOBAL"] = str(empty_home / "nonexistent_gitconfig")
            return real_runner.run(command, cwd, timeout_seconds=timeout_seconds, env=isolated_env)

    vcs_iso = GitHubVCSAdapter(
        runner=_IsolatedGitRunner(),
        owner="acme-org",
        github_token=gh_tok,
        secrets=(gh_tok,),
    )
    vcs_iso.push_branch("app-git-ident", ws_dir, branch="work")

    commit_cmds = [cmd for cmd in recorded_cmds if cmd[:1] == ["git"] and "commit" in cmd]
    assert commit_cmds, f"Expected git commit call, saw: {recorded_cmds}"
    commit_cmd = commit_cmds[0]
    assert "-c" in commit_cmd
    assert any(arg.startswith("user.name=") for arg in commit_cmd)
    assert any(arg.startswith("user.email=") for arg in commit_cmd)

    pushed_files = subprocess.run(
        ["git", "-C", str(bare_remote), "ls-tree", "-r", "refs/heads/work", "--name-only"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert "src/main.js" in pushed_files

    # Part B: Non-zero exit code from git commit MUST raise VcsError (never push silently) and scrub secret
    ws_fail = tmp_path / "ws" / "app-commit-fail"
    (ws_fail / ".git").mkdir(parents=True)
    (ws_fail / "index.html").write_text("<h1>Fail</h1>\n", encoding="utf-8")

    class _CommitFailingRunner:
        def run(
            self,
            command: Sequence[str],
            cwd: Path,
            timeout_seconds: float = 30.0,
            env: Mapping[str, str] | None = None,
        ) -> CommandResult:
            if "diff" in command:
                return CommandResult(exit_code=1, stdout="")
            if "commit" in command:
                return CommandResult(
                    exit_code=1,
                    stderr=f"fatal: hook rejected commit GITHUB_TOKEN={gh_tok}",
                )
            return CommandResult(exit_code=0, stdout="ok")

    vcs_fail = GitHubVCSAdapter(
        runner=_CommitFailingRunner(),
        owner="acme-org",
        github_token=gh_tok,
        secrets=(gh_tok,),
    )
    with pytest.raises(VcsError) as vcs_err:
        vcs_fail.push_branch("app-commit-fail", ws_fail, branch="work")
    assert gh_tok not in str(vcs_err.value)
    assert "[REDACTED]" in str(vcs_err.value)
    assert vcs_err.value.__cause__ is None
    assert vcs_err.value.__context__ is None


def test_github_vcs_isolates_workspace_git_repo_and_auto_creates_repo_via_rest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Real git + REST fallback creates isolated workspace .git, resolves owner via /user, creates dedicated repo, pushes without persisting token, and deploys gh-pages (INV-19, INV-22)."""
    gh_tok = "TEST_EPHEMERAL_GH_TOKEN_909"
    monkeypatch.setenv("GITHUB_TOKEN", gh_tok)

    orig_which = shutil.which

    def _hide_gh(
        name: str,
        mode: int = os.F_OK | os.X_OK,
        path: str | None = None,
    ) -> str | None:
        return None if name == "gh" else orig_which(name, mode=mode, path=path)

    monkeypatch.setattr("agent_factory.adapters.github_vcs_adapter.shutil.which", _hide_gh)
    monkeypatch.setattr("agent_factory.adapters.static_deploy_publisher.shutil.which", _hide_gh)

    parent_repo = tmp_path / "factory_parent"
    parent_repo.mkdir(parents=True)
    subprocess.run(["git", "init", "-b", "main"], cwd=parent_repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "FactoryUser"], cwd=parent_repo, check=True)
    subprocess.run(["git", "config", "user.email", "factory@local.test"], cwd=parent_repo, check=True)
    (parent_repo / "README.md").write_text("# Factory\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=parent_repo, check=True)
    subprocess.run(["git", "commit", "-m", "init parent"], cwd=parent_repo, check=True, capture_output=True)
    parent_head_before = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=parent_repo, check=True, capture_output=True, text=True
    ).stdout.strip()

    ws_dir = parent_repo / "workspaces" / "pomodoro-web"
    ws_dir.mkdir(parents=True)
    (ws_dir / "index.html").write_text("<h1>Pomodoro</h1>\n", encoding="utf-8")
    (ws_dir / "app.js").write_text("console.log('ready');\n", encoding="utf-8")
    pycache_dir = ws_dir / "__pycache__"
    pycache_dir.mkdir()
    (pycache_dir / "app.cpython-312.pyc").write_bytes(b"\xcb\r\r\n")

    bare_remote = tmp_path / "remotes" / "gabriel-user" / "pomodoro-web.git"
    bare_remote.parent.mkdir(parents=True)
    subprocess.run(["git", "init", "--bare", "-b", "main", str(bare_remote)], check=True, capture_output=True)

    empty_home = tmp_path / "empty_home"
    empty_home.mkdir()

    recorded_pushes: list[tuple[list[str], dict[str, str], str]] = []

    class _MirrorSubprocessRunner(SubprocessRunner):
        def run(
            self,
            command: Sequence[str],
            cwd: Path,
            timeout_seconds: float = 30.0,
            env: Mapping[str, str] | None = None,
        ) -> CommandResult:
            iso_env = dict(env if env is not None else os.environ)
            iso_env["HOME"] = str(empty_home)
            iso_env["USERPROFILE"] = str(empty_home)
            iso_env["GIT_CONFIG_NOSYSTEM"] = "1"
            iso_env["GIT_CONFIG_GLOBAL"] = str(empty_home / "none")

            rewritten = list(command)
            if len(rewritten) >= 2 and rewritten[0] == "git" and rewritten[1] in ("push", "ls-remote"):
                if rewritten[1] == "push":
                    cfg_text = (ws_dir / ".git" / "config").read_text(encoding="utf-8")
                    recorded_pushes.append((list(rewritten), dict(iso_env), cfg_text))
                rewritten = [str(bare_remote) if arg == "origin" else arg for arg in rewritten]
            return super().run(rewritten, cwd, timeout_seconds=timeout_seconds, env=iso_env)

    http_calls: list[tuple[str, str, dict[str, Any] | None, dict[str, str] | None]] = []

    class _FakeGitHubHttpTransport:
        def request_json(
            self,
            method: str,
            url: str,
            payload: Mapping[str, Any] | None = None,
            timeout_seconds: float = 30.0,
            headers: Mapping[str, str] | None = None,
            extra_secrets: Sequence[str] = (),
            *,
            raise_on_http_error: bool = False,
        ) -> HttpResponse:
            _ = (timeout_seconds, extra_secrets, raise_on_http_error)
            hdrs = dict(headers) if headers else None
            body_payload = dict(payload) if payload is not None else None
            http_calls.append((method, url, body_payload, hdrs))
            if method == "GET" and url.endswith("/user"):
                return HttpResponse(status_code=200, body=json.dumps({"login": "gabriel-user"}))
            if method == "GET" and url.endswith("/repos/gabriel-user/pomodoro-web"):
                return HttpResponse(status_code=404, body="{}")
            if method == "POST" and url.endswith("/user/repos"):
                return HttpResponse(status_code=201, body=json.dumps({"name": "pomodoro-web"}))
            if method == "GET" and "/actions/runs?" in url:
                return HttpResponse(status_code=200, body=json.dumps({"workflow_runs": []}))
            if method == "POST" and url.endswith("/repos/gabriel-user/pomodoro-web/pages"):
                return HttpResponse(status_code=201, body="{}")
            return HttpResponse(status_code=500, body=f"unexpected {method} {url}")

    runner = _MirrorSubprocessRunner()
    deployer = StaticDeployPublisher(
        runner=runner,
        target="gh-pages",
        deploy_base_url="https://agent-factory.github.io",
        branch="gh-pages",
        deploy_token=gh_tok,
        secrets=(gh_tok,),
    )
    vcs = GitHubVCSAdapter(
        runner=runner,
        owner="agent-factory",
        github_token="",  # Must pick up GITHUB_TOKEN from environment in memory
        deploy_publisher=deployer,
        http_transport=_FakeGitHubHttpTransport(),
    )

    ref = vcs.push_branch("pomodoro-web", ws_dir, branch="work")
    assert "work" in ref
    assert (ws_dir / ".git" / "HEAD").is_file()

    # Workspace git top-level MUST resolve strictly inside ws_dir (never parent_repo)
    ws_top = subprocess.run(
        ["git", "-C", str(ws_dir), "rev-parse", "--show-toplevel"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert Path(ws_top).resolve() == ws_dir.resolve()
    assert Path(ws_top).resolve() != parent_repo.resolve()

    # Parent factory repo HEAD and tracked tree MUST remain untouched
    parent_head_after = subprocess.run(
        ["git", "-C", str(parent_repo), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert parent_head_after == parent_head_before

    # Origin URL is clean HTTPS pointing to dedicated repo and token is NOT stored on disk
    origin_url = subprocess.run(
        ["git", "-C", str(ws_dir), "remote", "get-url", "origin"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert origin_url == "https://github.com/gabriel-user/pomodoro-web.git"
    assert gh_tok not in (ws_dir / ".git" / "config").read_text(encoding="utf-8")

    # CI passes via REST API fallback when workspace has no .github/workflows and runs=[]
    ci_res = vcs.get_ci_result("pomodoro-web", branch="work")
    assert ci_res.passed is True

    # Promote to main + publish gh-pages
    repo_url, deploy_url = vcs.merge_and_deploy("pomodoro-web", branch="work")
    assert repo_url == "https://github.com/gabriel-user/pomodoro-web"
    assert deploy_url == "https://gabriel-user.github.io/pomodoro-web"

    # Verify bare remote has work, main, and gh-pages with expected app files and NO __pycache__
    for branch_ref in ("refs/heads/work", "refs/heads/main", "refs/heads/gh-pages"):
        tree_files = subprocess.run(
            ["git", "-C", str(bare_remote), "ls-tree", "-r", branch_ref, "--name-only"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.splitlines()
        assert "index.html" in tree_files
        assert "app.js" in tree_files
        assert not any("__pycache__" in f or f.endswith(".pyc") for f in tree_files)

    # Every push used ephemeral GIT_CONFIG_COUNT auth without token on argv or .git/config
    assert len(recorded_pushes) >= 3  # work push + main push + gh-pages deploy
    for cmd, push_env, cfg_text in recorded_pushes:
        assert gh_tok not in " ".join(cmd)
        assert gh_tok not in cfg_text
        assert push_env.get("GIT_CONFIG_COUNT") == "1"
        assert push_env.get("GIT_CONFIG_KEY_0") == "http.https://github.com/.extraheader"
        assert str(push_env.get("GIT_CONFIG_VALUE_0", "")).startswith("Authorization: basic ")
        assert push_env.get("GIT_TERMINAL_PROMPT") == "0"

    # REST API sequence: /user -> /repos/gabriel-user/pomodoro-web -> POST /user/repos -> actions/runs -> pages
    called_endpoints = [(m, u.replace("https://api.github.com", "")) for m, u, _, _ in http_calls]
    assert ("GET", "/user") in called_endpoints
    assert ("GET", "/repos/gabriel-user/pomodoro-web") in called_endpoints
    assert ("POST", "/user/repos") in called_endpoints
    assert any(m == "GET" and "/repos/gabriel-user/pomodoro-web/actions/runs" in u for m, u in called_endpoints)
    assert ("POST", "/repos/gabriel-user/pomodoro-web/pages") in called_endpoints
    create_call = next(c for c in http_calls if c[0] == "POST" and c[1].endswith("/user/repos"))
    assert create_call[2] == {"name": "pomodoro-web", "private": False, "auto_init": False}
    assert create_call[3] is not None and create_call[3]["Authorization"] == f"Bearer {gh_tok}"


def test_github_vcs_blocks_parent_git_escape_tolerates_422_and_merges_via_pr_api(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Blocks workspace escaping into parent .git (INV-22), treats HTTP 422 on repo create as existing, redacts tokens on HTTP error, and merges PR via REST when main exists."""
    gh_tok = "TEST_GH_TOKEN_PR_422"
    orig_which = shutil.which

    def _hide_gh(
        name: str,
        mode: int = os.F_OK | os.X_OK,
        path: str | None = None,
    ) -> str | None:
        return None if name == "gh" else orig_which(name, mode=mode, path=path)

    monkeypatch.setattr("agent_factory.adapters.github_vcs_adapter.shutil.which", _hide_gh)

    # Part A: Escape guard when workspace git resolves to parent repo instead of workspace_dir
    parent_repo = tmp_path / "parent_factory"
    parent_repo.mkdir(parents=True)
    subprocess.run(["git", "init", "-b", "main"], cwd=parent_repo, check=True, capture_output=True)
    ws_escape = parent_repo / "workspaces" / "escape-app"
    ws_escape.mkdir(parents=True)
    (ws_escape / "index.html").write_text("<h1>Escape</h1>\n", encoding="utf-8")

    class _NoopInitSubprocessRunner(SubprocessRunner):
        def run(
            self,
            command: Sequence[str],
            cwd: Path,
            timeout_seconds: float = 30.0,
            env: Mapping[str, str] | None = None,
        ) -> CommandResult:
            if len(command) >= 2 and command[0] == "git" and command[1] == "init":
                return CommandResult(exit_code=0, stdout="noop")
            return super().run(command, cwd, timeout_seconds=timeout_seconds, env=env)

    vcs_escape = GitHubVCSAdapter(
        runner=_NoopInitSubprocessRunner(),
        owner="acme-dev",
        github_token=gh_tok,
        secrets=(gh_tok,),
    )
    with pytest.raises(VcsError) as escape_err:
        vcs_escape.push_branch("escape-app", ws_escape, branch="work")
    assert "INV-22" in str(escape_err.value)
    assert "escapes" in str(escape_err.value)

    # Part B: 422 "already exists" + existing remote main triggers PR create & merge REST calls
    ws_pr = tmp_path / "ws" / "pr-app"
    ws_pr.mkdir(parents=True)
    (ws_pr / "index.html").write_text("<h1>PR</h1>\n", encoding="utf-8")
    bare_remote = tmp_path / "bare" / "acme-dev" / "pr-app.git"
    bare_remote.parent.mkdir(parents=True)
    subprocess.run(["git", "init", "--bare", "-b", "main", str(bare_remote)], check=True, capture_output=True)
    seed_dir = tmp_path / "seed"
    seed_dir.mkdir()
    subprocess.run(["git", "init", "-b", "main"], cwd=seed_dir, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Seed"], cwd=seed_dir, check=True)
    subprocess.run(["git", "config", "user.email", "seed@local.test"], cwd=seed_dir, check=True)
    (seed_dir / "README.md").write_text("seed\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=seed_dir, check=True)
    subprocess.run(["git", "commit", "-m", "seed main"], cwd=seed_dir, check=True, capture_output=True)
    subprocess.run(["git", "push", str(bare_remote), "main:main"], cwd=seed_dir, check=True, capture_output=True)

    empty_home = tmp_path / "home2"
    empty_home.mkdir()
    pr_http_calls: list[tuple[str, str]] = []

    class _PrMirrorRunner(SubprocessRunner):
        def run(
            self,
            command: Sequence[str],
            cwd: Path,
            timeout_seconds: float = 30.0,
            env: Mapping[str, str] | None = None,
        ) -> CommandResult:
            iso_env = dict(env if env is not None else os.environ)
            iso_env["HOME"] = str(empty_home)
            iso_env["USERPROFILE"] = str(empty_home)
            iso_env["GIT_CONFIG_NOSYSTEM"] = "1"
            iso_env["GIT_CONFIG_GLOBAL"] = str(empty_home / "none")
            rewritten = (
                [str(bare_remote) if arg == "origin" else arg for arg in command]
                if len(command) >= 2 and command[0] == "git" and command[1] in ("push", "ls-remote")
                else list(command)
            )
            return super().run(rewritten, cwd, timeout_seconds=timeout_seconds, env=iso_env)

    class _PrTransport:
        def __init__(self, create_status: int = 422, create_body: str = '{"message": "already exists"}') -> None:
            self.create_status = create_status
            self.create_body = create_body

        def request_json(
            self,
            method: str,
            url: str,
            payload: Mapping[str, Any] | None = None,
            timeout_seconds: float = 30.0,
            headers: Mapping[str, str] | None = None,
            extra_secrets: Sequence[str] = (),
            *,
            raise_on_http_error: bool = False,
        ) -> HttpResponse:
            _ = (payload, timeout_seconds, headers, extra_secrets, raise_on_http_error)
            path = url.replace("https://api.github.com", "")
            pr_http_calls.append((method, path))
            if method == "GET" and path == "/repos/acme-dev/pr-app":
                return HttpResponse(status_code=404, body="{}")
            if method == "POST" and path == "/user/repos":
                return HttpResponse(status_code=self.create_status, body=self.create_body)
            if method == "POST" and path == "/repos/acme-dev/pr-app/pulls":
                return HttpResponse(status_code=201, body=json.dumps({"number": 7}))
            if method == "PUT" and path == "/repos/acme-dev/pr-app/pulls/7/merge":
                return HttpResponse(status_code=200, body=json.dumps({"merged": True}))
            return HttpResponse(status_code=500, body=f"unexpected {method} {path}")

    vcs_pr = GitHubVCSAdapter(
        runner=_PrMirrorRunner(),
        owner="acme-dev",
        github_token=gh_tok,
        secrets=(gh_tok,),
        http_transport=_PrTransport(),
    )
    assert "work" in vcs_pr.push_branch("pr-app", ws_pr, branch="work")
    repo_url, deploy_url = vcs_pr.merge_and_deploy("pr-app", branch="work")
    assert repo_url == "https://github.com/acme-dev/pr-app"
    assert deploy_url == "https://acme-dev.github.io/pr-app"
    assert ("POST", "/repos/acme-dev/pr-app/pulls") in pr_http_calls
    assert ("PUT", "/repos/acme-dev/pr-app/pulls/7/merge") in pr_http_calls

    # Part C: HTTP error leaking secret is scrubbed without preserving __cause__/__context__
    ws_http_err = tmp_path / "ws" / "err-app"
    ws_http_err.mkdir(parents=True)
    (ws_http_err / "index.html").write_text("<h1>Err</h1>\n", encoding="utf-8")
    vcs_err_http = GitHubVCSAdapter(
        runner=_PrMirrorRunner(),
        owner="acme-dev",
        github_token=gh_tok,
        secrets=(gh_tok,),
        http_transport=_PrTransport(
            create_status=500,
            create_body=f"server error for token {gh_tok}",
        ),
    )
    with pytest.raises(VcsError) as http_err:
        vcs_err_http.push_branch("pr-app", ws_http_err, branch="work")
    assert gh_tok not in str(http_err.value)
    assert "[REDACTED]" in str(http_err.value)
    assert http_err.value.__cause__ is None
    assert http_err.value.__context__ is None


def test_static_deploy_publisher_publishes_src_subdir_not_empty_root_template(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """StaticDeployPublisher detects when index.html in root is the untouched empty template and publishes src/ (both via gh pages deploy and git subtree commit-tree fallback)."""
    gh_tok = "TEST_SRC_DEPLOY_TOK_55"
    ws_dir = tmp_path / "ws" / "pomodoro-src"
    ws_dir.mkdir(parents=True)
    # Untouched root shell from templates/static-web/index.html
    (ws_dir / "index.html").write_text(
        '<!DOCTYPE html>\n<html lang="es">\n  <head>\n    <title>Static Web App</title>\n'
        '    <link rel="stylesheet" href="./styles.css" />\n  </head>\n'
        '  <body>\n    <main id="app" aria-live="polite"></main>\n'
        '    <script type="module" src="./src/app.js"></script>\n  </body>\n</html>\n',
        encoding="utf-8",
    )
    (ws_dir / "styles.css").write_text("/* empty */\n", encoding="utf-8")
    (ws_dir / "src").mkdir()
    (ws_dir / "src" / "index.html").write_text(
        "<!doctype html><html><body><h1>Real Built App</h1><script src='./app.js'></script></body></html>\n",
        encoding="utf-8",
    )
    (ws_dir / "src" / "app.js").write_text("console.log('built');\n", encoding="utf-8")

    # Part A: With FakeRunner (gh CLI mode), deploy target path points strictly to ws_dir/src
    fake_runner = FakeRunner(results=[CommandResult(exit_code=0, stdout="deployed")])
    pub_cli = StaticDeployPublisher(
        runner=fake_runner,
        target="github-pages",
        deploy_base_url="https://acme.github.io",
        deploy_token=gh_tok,
    )
    url = pub_cli.deploy_site("pomodoro-src", ws_dir)
    assert url == "https://acme.github.io/pomodoro-src"
    assert len(fake_runner.calls) == 1
    assert Path(fake_runner.calls[0]["command"][3]).resolve() == (ws_dir / "src").resolve()

    # Part B: Without gh CLI, real git commit-tree publishes ONLY src/ contents to gh-pages branch root
    orig_which = shutil.which

    def _hide_gh(
        name: str,
        mode: int = os.F_OK | os.X_OK,
        path: str | None = None,
    ) -> str | None:
        return None if name == "gh" else orig_which(name, mode=mode, path=path)

    monkeypatch.setattr("agent_factory.adapters.static_deploy_publisher.shutil.which", _hide_gh)

    bare_remote = tmp_path / "bare" / "pomodoro-src.git"
    bare_remote.parent.mkdir(parents=True)
    subprocess.run(["git", "init", "--bare", "-b", "main", str(bare_remote)], check=True, capture_output=True)
    subprocess.run(["git", "init", "-b", "main"], cwd=ws_dir, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Tester"], cwd=ws_dir, check=True)
    subprocess.run(["git", "config", "user.email", "test@local.dev"], cwd=ws_dir, check=True)
    subprocess.run(["git", "add", "-A"], cwd=ws_dir, check=True)
    subprocess.run(["git", "commit", "-m", "initial built workspace"], cwd=ws_dir, check=True, capture_output=True)
    subprocess.run(["git", "remote", "add", "origin", str(bare_remote)], cwd=ws_dir, check=True)

    pub_git = StaticDeployPublisher(
        runner=SubprocessRunner(default_timeout_seconds=15.0),
        target="github-pages",
        deploy_base_url="https://acme.github.io",
        branch="gh-pages",
        deploy_token=gh_tok,
    )
    pub_git.deploy_site("pomodoro-src", ws_dir)

    gh_pages_files = subprocess.run(
        ["git", "-C", str(bare_remote), "ls-tree", "-r", "refs/heads/gh-pages", "--name-only"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.splitlines()
    assert "index.html" in gh_pages_files
    assert "app.js" in gh_pages_files
    assert not any(f.startswith("src/") for f in gh_pages_files)
    deployed_index = subprocess.run(
        ["git", "-C", str(bare_remote), "show", "refs/heads/gh-pages:index.html"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert "Real Built App" in deployed_index
    assert "Static Web App" not in deployed_index


def test_github_vcs_follows_redirects_preserves_method_payload_auth_and_updates_canonical_slug(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Follow 301/302/307/308 redirects (up to 3 hops) preserving method, JSON payload, and Authorization header, reject unsafe cross-origin redirects (INV-19), and update repo_slug/repo_url."""
    gh_tok = "TEST_GH_REDIRECT_SECRET_777"
    orig_which = shutil.which

    def _hide_gh(
        name: str,
        mode: int = os.F_OK | os.X_OK,
        path: str | None = None,
    ) -> str | None:
        return None if name == "gh" else orig_which(name, mode=mode, path=path)

    monkeypatch.setattr("agent_factory.adapters.github_vcs_adapter.shutil.which", _hide_gh)

    # Set up local bare remote + workspace with remote main so _merge_via_github_api_or_git uses PR API
    bare_remote = tmp_path / "bare" / "acme-dev" / "old-app.git"
    bare_remote.parent.mkdir(parents=True)
    subprocess.run(["git", "init", "--bare", "-b", "main", str(bare_remote)], check=True, capture_output=True)
    seed_dir = tmp_path / "seed_redir"
    seed_dir.mkdir()
    subprocess.run(["git", "init", "-b", "main"], cwd=seed_dir, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Seed"], cwd=seed_dir, check=True)
    subprocess.run(["git", "config", "user.email", "seed@local.test"], cwd=seed_dir, check=True)
    (seed_dir / "README.md").write_text("seed\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=seed_dir, check=True)
    subprocess.run(["git", "commit", "-m", "seed"], cwd=seed_dir, check=True, capture_output=True)
    subprocess.run(["git", "push", str(bare_remote), "main:main"], cwd=seed_dir, check=True, capture_output=True)

    ws_dir = tmp_path / "ws" / "old-app"
    ws_dir.mkdir(parents=True)
    (ws_dir / "index.html").write_text("<h1>Renamed</h1>\n", encoding="utf-8")

    empty_home = tmp_path / "home_redir"
    empty_home.mkdir()

    class _RedirGitRunner(SubprocessRunner):
        def run(
            self,
            command: Sequence[str],
            cwd: Path,
            timeout_seconds: float = 30.0,
            env: Mapping[str, str] | None = None,
        ) -> CommandResult:
            iso_env = dict(env if env is not None else os.environ)
            iso_env["HOME"] = str(empty_home)
            iso_env["USERPROFILE"] = str(empty_home)
            iso_env["GIT_CONFIG_NOSYSTEM"] = "1"
            iso_env["GIT_CONFIG_GLOBAL"] = str(empty_home / "none")
            rewritten = (
                [str(bare_remote) if arg == "origin" else arg for arg in command]
                if len(command) >= 2 and command[0] == "git" and command[1] in ("push", "ls-remote")
                else list(command)
            )
            return super().run(rewritten, cwd, timeout_seconds=timeout_seconds, env=iso_env)

    recorded_http: list[dict[str, Any]] = []

    class _RedirectTransport:
        def request_json(
            self,
            method: str,
            url: str,
            payload: Mapping[str, Any] | None = None,
            timeout_seconds: float = 30.0,
            headers: Mapping[str, str] | None = None,
            extra_secrets: Sequence[str] = (),
            *,
            raise_on_http_error: bool = False,
        ) -> HttpResponse:
            _ = (timeout_seconds, extra_secrets, raise_on_http_error)
            hdrs = dict(headers or {})
            body_copy = dict(payload) if payload is not None else None
            recorded_http.append({"method": method, "url": url, "payload": body_copy, "headers": hdrs})

            if method == "GET" and url == "https://api.github.com/repos/acme-dev/old-app":
                return HttpResponse(status_code=200, body=json.dumps({"full_name": "acme-dev/old-app"}))
            if method == "POST" and url == "https://api.github.com/repos/acme-dev/old-app/pulls":
                return HttpResponse(
                    status_code=301,
                    body=json.dumps(
                        {
                            "message": "Moved Permanently",
                            "url": "https://api.github.com/repos/acme-dev/renamed-app/pulls",
                        }
                    ),
                    headers={"Location": "https://api.github.com/repos/acme-dev/renamed-app/pulls"},
                )
            if method == "POST" and url == "https://api.github.com/repos/acme-dev/renamed-app/pulls":
                return HttpResponse(
                    status_code=201,
                    body=json.dumps(
                        {
                            "number": 12,
                            "html_url": "https://github.com/acme-dev/renamed-app/pull/12",
                            "base": {
                                "repo": {
                                    "full_name": "acme-dev/renamed-app",
                                    "html_url": "https://github.com/acme-dev/renamed-app",
                                }
                            },
                        }
                    ),
                )
            if method == "PUT" and url == "https://api.github.com/repos/acme-dev/renamed-app/pulls/12/merge":
                return HttpResponse(
                    status_code=301,
                    body=json.dumps({"message": "Moved Permanently"}),
                    headers={"Location": "https://api.github.com/repositories/777888/pulls/12/merge"},
                )
            if method == "PUT" and url == "https://api.github.com/repositories/777888/pulls/12/merge":
                return HttpResponse(
                    status_code=308,
                    body=json.dumps({"message": "Permanent Redirect"}),
                    headers={"Location": "/repos/acme-dev/renamed-app/pulls/12/merge?final=1"},
                )
            if method == "PUT" and url == "https://api.github.com/repos/acme-dev/renamed-app/pulls/12/merge?final=1":
                return HttpResponse(status_code=200, body=json.dumps({"merged": True}))
            return HttpResponse(status_code=500, body=f"unexpected {method} {url}")

    vcs = GitHubVCSAdapter(
        runner=_RedirGitRunner(),
        owner="acme-dev",
        github_token=gh_tok,
        secrets=(gh_tok,),
        http_transport=_RedirectTransport(),
    )
    vcs.push_branch("old-app", ws_dir, branch="work")
    repo_url, deploy_url = vcs.merge_and_deploy("old-app", branch="work")

    assert repo_url == "https://github.com/acme-dev/renamed-app"
    assert deploy_url == "https://acme-dev.github.io/renamed-app"
    assert vcs.repo_slug == "acme-dev/renamed-app"
    assert vcs.repo_url == "https://github.com/acme-dev/renamed-app"

    # Verify POST redirect preserved method, JSON payload, and Bearer token
    post_hops = [c for c in recorded_http if "/pulls" in c["url"] and "/merge" not in c["url"]]
    assert [c["url"] for c in post_hops] == [
        "https://api.github.com/repos/acme-dev/old-app/pulls",
        "https://api.github.com/repos/acme-dev/renamed-app/pulls",
    ]
    for hop in post_hops:
        assert hop["method"] == "POST"
        assert hop["payload"] == {
            "title": "Merge work",
            "head": "work",
            "base": "main",
            "body": "Automated PR for old-app",
        }
        assert hop["headers"].get("Authorization") == f"Bearer {gh_tok}"

    # Verify PUT merge started on updated canonical slug (NOT old-app) and followed 301 + 308 preserving PUT, payload & token
    put_hops = [c for c in recorded_http if "/merge" in c["url"]]
    assert [c["url"] for c in put_hops] == [
        "https://api.github.com/repos/acme-dev/renamed-app/pulls/12/merge",
        "https://api.github.com/repositories/777888/pulls/12/merge",
        "https://api.github.com/repos/acme-dev/renamed-app/pulls/12/merge?final=1",
    ]
    for hop in put_hops:
        assert hop["method"] == "PUT"
        assert hop["payload"] == {"merge_method": "merge"}
        assert hop["headers"].get("Authorization") == f"Bearer {gh_tok}"

    # Part B: 302 + 307 + 301 up to 3 hops works for PATCH/GET, while > 3 hops stops and unsafe external redirect is blocked (INV-19)
    calls_edge: list[tuple[str, str, dict[str, Any] | None, dict[str, str]]] = []

    class _EdgeRedirectTransport:
        def request_json(
            self,
            method: str,
            url: str,
            payload: Mapping[str, Any] | None = None,
            timeout_seconds: float = 30.0,
            headers: Mapping[str, str] | None = None,
            extra_secrets: Sequence[str] = (),
            *,
            raise_on_http_error: bool = False,
        ) -> HttpResponse:
            _ = (timeout_seconds, extra_secrets, raise_on_http_error)
            calls_edge.append((method, url, dict(payload) if payload else None, dict(headers or {})))
            if url.endswith("/hop0"):
                return HttpResponse(status_code=302, body="{}", headers={"location": "/hop1"})
            if url.endswith("/hop1"):
                return HttpResponse(status_code=307, body="{}", headers={"Location": "https://api.github.com/hop2"})
            if url.endswith("/hop2"):
                return HttpResponse(status_code=308, body="{}", headers={"Location": "/hop3"})
            if url.endswith("/hop3"):
                return HttpResponse(
                    status_code=200,
                    body=json.dumps({"full_name": "acme-dev/three-hops", "html_url": "https://github.com/acme-dev/three-hops"}),
                )
            if url.endswith("/loop0"):
                return HttpResponse(status_code=301, body="{}", headers={"Location": "/loop1"})
            if url.endswith("/loop1"):
                return HttpResponse(status_code=301, body="{}", headers={"Location": "/loop2"})
            if url.endswith("/loop2"):
                return HttpResponse(status_code=301, body="{}", headers={"Location": "/loop3"})
            if url.endswith("/loop3"):
                return HttpResponse(status_code=301, body='{"message":"Still Moving"}', headers={"Location": "/loop4"})
            if url.endswith("/evil-hop"):
                return HttpResponse(status_code=301, body="{}", headers={"Location": "https://evil.attacker.invalid/steal"})
            return HttpResponse(status_code=500, body="unexpected")

    vcs_edge = GitHubVCSAdapter(
        runner=FakeRunner(),
        owner="acme-dev",
        github_token=gh_tok,
        http_transport=_EdgeRedirectTransport(),
    )
    st3, _ = vcs_edge._github_request("PATCH", "/hop0", {"name": "three-hops"})
    assert st3 == 200
    assert vcs_edge.repo_slug == "acme-dev/three-hops"
    assert vcs_edge.repo_url == "https://github.com/acme-dev/three-hops"
    patch_calls = [c for c in calls_edge if "/hop" in c[1]]
    assert len(patch_calls) == 4  # initial + 3 hops
    assert all(c[0] == "PATCH" and c[2] == {"name": "three-hops"} and c[3]["Authorization"] == f"Bearer {gh_tok}" for c in patch_calls)

    st_limit, _ = vcs_edge._github_request("GET", "/loop0")
    assert st_limit == 301
    assert not any(c[1].endswith("/loop4") for c in calls_edge)

    with pytest.raises(VcsError) as unsafe_err:
        vcs_edge._github_request("POST", "/evil-hop", {"title": "PR"})
    assert gh_tok not in str(unsafe_err.value)
    assert not any("evil.attacker.invalid" in c[1] for c in calls_edge)


@pytest.mark.enable_socket
@pytest.mark.allow_hosts(["127.0.0.1"])
def test_github_vcs_real_urllib_transport_preserves_post_and_put_across_301_redirects(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exercise real UrllibHttpTransport against loopback HTTP 301 server confirming urllib does not downgrade POST/PUT to GET or strip JSON body/Bearer header."""
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    from agent_factory.adapters.telegram_client import UrllibHttpTransport

    gh_tok = "TEST_REAL_URLLIB_REDIR_TOKEN_999"
    orig_which = shutil.which

    def _hide_gh(
        name: str,
        mode: int = os.F_OK | os.X_OK,
        path: str | None = None,
    ) -> str | None:
        return None if name == "gh" else orig_which(name, mode=mode, path=path)

    monkeypatch.setattr("agent_factory.adapters.github_vcs_adapter.shutil.which", _hide_gh)

    observed: list[tuple[str, str, str, str]] = []

    class _FakeGitHubRedirectServer(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length).decode("utf-8") if length else ""
            auth = str(self.headers.get("Authorization") or "")
            observed.append(("POST", self.path, auth, raw))
            port = self.server.server_port
            if self.path == "/repos/acme-dev/legacy-slug/pulls":
                self.send_response(301)
                self.send_header("Content-Type", "application/json")
                self.send_header("Location", f"http://127.0.0.1:{port}/repos/acme-dev/canon-slug/pulls")
                body = b'{"message":"Moved Permanently"}'
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            if self.path == "/repos/acme-dev/canon-slug/pulls":
                body = json.dumps({"number": 42, "html_url": "https://github.com/acme-dev/canon-slug/pull/42"}).encode("utf-8")
                self.send_response(201)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            self.send_response(404)
            self.end_headers()

        def do_PUT(self) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length).decode("utf-8") if length else ""
            auth = str(self.headers.get("Authorization") or "")
            observed.append(("PUT", self.path, auth, raw))
            port = self.server.server_port
            if self.path == "/repos/acme-dev/canon-slug/pulls/42/merge":
                self.send_response(301)
                self.send_header("Content-Type", "application/json")
                self.send_header("Location", f"http://127.0.0.1:{port}/repositories/555/pulls/42/merge")
                body = b'{"message":"Moved Permanently"}'
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            if self.path == "/repositories/555/pulls/42/merge":
                body = json.dumps({"merged": True}).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            self.send_response(404)
            self.end_headers()

        def do_GET(self) -> None:
            auth = str(self.headers.get("Authorization") or "")
            observed.append(("GET", self.path, auth, ""))
            self.send_response(405)
            self.end_headers()

        def log_message(self, format: str, *args: object) -> None:
            _ = (format, args)

    srv = HTTPServer(("127.0.0.1", 0), _FakeGitHubRedirectServer)
    srv_thread = threading.Thread(target=srv.serve_forever, daemon=True)
    srv_thread.start()
    try:
        # Repo with remote main so merge_and_deploy executes PR create + merge via REST
        bare_remote = tmp_path / "bare" / "acme-dev" / "legacy-slug.git"
        bare_remote.parent.mkdir(parents=True)
        subprocess.run(["git", "init", "--bare", "-b", "main", str(bare_remote)], check=True, capture_output=True)
        seed = tmp_path / "seed_http"
        seed.mkdir()
        subprocess.run(["git", "init", "-b", "main"], cwd=seed, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "Seed"], cwd=seed, check=True)
        subprocess.run(["git", "config", "user.email", "seed@local.test"], cwd=seed, check=True)
        (seed / "README.md").write_text("seed\n", encoding="utf-8")
        subprocess.run(["git", "add", "README.md"], cwd=seed, check=True)
        subprocess.run(["git", "commit", "-m", "seed"], cwd=seed, check=True, capture_output=True)
        subprocess.run(["git", "push", str(bare_remote), "main:main"], cwd=seed, check=True, capture_output=True)

        ws_dir = tmp_path / "ws" / "legacy-slug"
        ws_dir.mkdir(parents=True)
        (ws_dir / "index.html").write_text("<h1>Live</h1>\n", encoding="utf-8")
        empty_home = tmp_path / "home_http"
        empty_home.mkdir()

        class _LoopbackRunner(SubprocessRunner):
            def run(
                self,
                command: Sequence[str],
                cwd: Path,
                timeout_seconds: float = 30.0,
                env: Mapping[str, str] | None = None,
            ) -> CommandResult:
                iso_env = dict(env if env is not None else os.environ)
                iso_env["HOME"] = str(empty_home)
                iso_env["USERPROFILE"] = str(empty_home)
                iso_env["GIT_CONFIG_NOSYSTEM"] = "1"
                iso_env["GIT_CONFIG_GLOBAL"] = str(empty_home / "none")
                rewritten = (
                    [str(bare_remote) if arg == "origin" else arg for arg in command]
                    if len(command) >= 2 and command[0] == "git" and command[1] in ("push", "ls-remote")
                    else list(command)
                )
                return super().run(rewritten, cwd, timeout_seconds=timeout_seconds, env=iso_env)

        class _LocalNoCreateVCS(GitHubVCSAdapter):
            def _ensure_github_repo(self, project_id: str, *, env: Mapping[str, str]) -> str:
                _ = (project_id, env)
                return "acme-dev"

        vcs = _LocalNoCreateVCS(
            runner=_LoopbackRunner(),
            owner="acme-dev",
            github_token=gh_tok,
            secrets=(gh_tok,),
            api_base_url=f"http://127.0.0.1:{srv.server_port}",
            http_transport=UrllibHttpTransport(),
        )
        vcs.push_branch("legacy-slug", ws_dir, branch="work")
        repo_url, _ = vcs.merge_and_deploy("legacy-slug", branch="work")

        assert repo_url == "https://github.com/acme-dev/canon-slug"
        assert vcs.repo_slug == "acme-dev/canon-slug"
        assert [row[:2] for row in observed] == [
            ("POST", "/repos/acme-dev/legacy-slug/pulls"),
            ("POST", "/repos/acme-dev/canon-slug/pulls"),
            ("PUT", "/repos/acme-dev/canon-slug/pulls/42/merge"),
            ("PUT", "/repositories/555/pulls/42/merge"),
        ]
        assert not any(row[0] == "GET" for row in observed)
        for method, _path, auth, raw_body in observed:
            assert auth == f"Bearer {gh_tok}"
            parsed_json = json.loads(raw_body)
            if method == "POST":
                assert parsed_json["head"] == "work"
            else:
                assert parsed_json == {"merge_method": "merge"}
    finally:
        srv.shutdown()
        srv.server_close()
