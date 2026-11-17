"""Tests for real GitHubVCSAdapter and StaticDeployPublisher via Runner with clean env and secret redaction (AC-4.4, INV-07, INV-19, INV-22, INV-23)."""

from pathlib import Path

import pytest

from agent_factory.adapters.fake_runner import FakeRunner
from agent_factory.adapters.github_vcs_adapter import GitHubVCSAdapter, VcsError
from agent_factory.adapters.static_deploy_publisher import (
    StaticDeployError,
    StaticDeployPublisher,
)
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
