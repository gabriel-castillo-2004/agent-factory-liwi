"""Tests for real GitHubVCSAdapter and StaticDeployPublisher via Runner with clean env and secret redaction (AC-4.4, INV-07, INV-19, INV-22, INV-23)."""

import json
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
