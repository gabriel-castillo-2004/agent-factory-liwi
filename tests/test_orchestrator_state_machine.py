"""Tests for orchestrator state machine enhancements: DEBATE budget, error traceability, retry_project, delete_project, and SQLite thread safety."""

import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from agent_factory.adapters.fake_code_agent import FakeCodeAgent
from agent_factory.adapters.fake_llm import FakeLLM
from agent_factory.adapters.fake_runner import FakeRunner
from agent_factory.adapters.fake_vcs import FakeVCS
from agent_factory.adapters.memory_store import MemoryEventStore
from agent_factory.adapters.sqlite_store import SqliteEventStore
from agent_factory.builder import BuildWorker, LocalJobQueue
from agent_factory.domain.events import Event, EventType, create_event
from agent_factory.domain.models import (
    AcceptanceTest,
    AcceptanceTestType,
    ApprovalEvent,
    FileSpec,
    Plan,
)
from agent_factory.domain.progress import project_status_from_events
from agent_factory.domain.states import IllegalTransition, ProjectState
from agent_factory.orchestrator import Orchestrator
from agent_factory.ports.code_agent import CodeAgentError
from agent_factory.ports.llm import LLMError
from agent_factory.ports.runner import CommandResult
from agent_factory.ports.vcs import CIRunResult
from agent_factory.workspace import WorkspaceManager


def _complete_spec_response() -> dict[str, Any]:
    return {
        "spec": {
            "objective": "Task tracker web app",
            "users": ["Gabriel"],
            "features": ["Add tasks", "Filter tasks"],
            "business_rules": ["Persist in localStorage"],
            "edge_cases": ["Empty input"],
            "constraints": ["Static frontend only"],
            "success_criteria": ["Loads with zero errors"],
            "assumptions": [],
        },
        "questions": [],
    }


def _pragmatist_dict() -> dict[str, Any]:
    return {
        "approach": "Vanilla JS task tracker with localStorage",
        "components": ["src/app.js", "src/store.js"],
        "tradeoffs": ["Single browser storage"],
    }


def _structuralist_dict() -> dict[str, Any]:
    return {
        "critique": ["Ensure safe JSON parse on corrupted storage"],
        "robust_proposal": "Wrap localStorage access with try/catch and DOM textContent escaping",
        "architectural_safeguards": ["Input validation", "XSS-safe rendering"],
    }


def _valid_plan_dict(summary: str = "Task tracker plan") -> dict[str, Any]:
    return Plan(
        summary=summary,
        rationale="Simple vanilla ES modules with defensive storage",
        template_id="static-web",
        files=[
            FileSpec(
                path="src/app.js",
                purpose="Task UI controller",
                contract="export function addTask(title) {}",
                depends_on=[],
            )
        ],
        acceptance_tests=[
            AcceptanceTest(
                id="AT-01",
                description="Adding a task renders it in the DOM list",
                type=AcceptanceTestType.UNIT,
            )
        ],
        risks=["Storage quota full"],
        deployment_target="github-pages",
    ).model_dump(mode="json")


def test_default_circuit_breaker_allows_six_debate_calls_before_tripping() -> None:
    """Default Orchestrator DEBATE limit is >= 6 so multi-round/retried debate succeeds without false trip at 4."""
    store = MemoryEventStore()
    # 1 interview + 2 retries + 1 valid Pragmatist (3 DEBATE) + 2 retries + 1 valid Structuralist (6 DEBATE) + 1 Mediator
    llm = FakeLLM(
        responses=[
            _complete_spec_response(),
            "bad-pragmatist-1",
            "bad-pragmatist-2",
            _pragmatist_dict(),
            "bad-structuralist-1",
            "bad-structuralist-2",
            _structuralist_dict(),
            _valid_plan_dict(),
        ]
    )
    orch = Orchestrator(store=store, llm=llm)
    assert (orch.circuit_breaker.get_limit(ProjectState.DEBATE) or 0) >= 6
    assert orch.circuit_breaker.get_limit(ProjectState.MEDIATION) == 3

    pid = "proj-debate-six-calls"
    orch.create_project(pid, "Debate Six", "Test 6 DEBATE calls")
    orch.handle_interview_message(pid, "evt-int-1", "Build task tracker")

    status = orch.run_debate(pid)
    assert status.state == ProjectState.WAITING_APPROVAL
    assert orch.circuit_breaker.get_calls(pid, ProjectState.DEBATE) == 6
    assert orch.circuit_breaker.get_calls(pid, ProjectState.MEDIATION) == 1

    # Exceeding 6 DEBATE calls on a subsequent re-debate trips CircuitBreaker at call 7 -> FAILED
    assert isinstance(orch.llm, FakeLLM)
    orch.llm.enqueue(_pragmatist_dict())
    failed = orch.run_debate(pid)
    assert failed.state == ProjectState.FAILED
    assert failed.failure_reason is not None
    assert "DEBATE" in failed.failure_reason

    events = store.get_events(pid)
    phase_errors = [e for e in events if e.type == EventType.PHASE_ERROR]
    failed_events = [e for e in events if e.type == EventType.PROJECT_FAILED]
    assert len(phase_errors) == 1
    assert len(failed_events) == 1
    assert events.index(phase_errors[0]) < events.index(failed_events[0])


def test_phase_exceptions_record_redacted_phase_error_before_failed_in_all_states(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exceptions during INTERVIEW, DEBATE, MEDIATION, BUILDING, and VERIFYING record redacted PHASE_ERROR prior to PROJECT_FAILED."""
    secret = "TEST_PHASE_ERR_SECRET_9911"
    monkeypatch.setenv("GITHUB_TOKEN", secret)

    # 1. INTERVIEW error
    db_path = tmp_path / "phase_errors.db"
    store = SqliteEventStore(db_path=db_path)
    try:
        orch_int = Orchestrator(
            store=store,
            llm=FakeLLM(
                responses=[
                    LLMError(f"Interviewer API error GITHUB_TOKEN={secret}"),
                    LLMError(f"Interviewer API error GITHUB_TOKEN={secret}"),
                    LLMError(f"Interviewer API error GITHUB_TOKEN={secret}"),
                ]
            ),
        )
        orch_int.create_project("err-interview", "Err Interview", "Obj")
        st_int = orch_int.handle_interview_message("err-interview", "e1", "hello")
        assert st_int.state == ProjectState.FAILED
        assert secret not in (st_int.failure_reason or "")
        ev_int = store.get_events("err-interview")
        idx_pe = next(i for i, e in enumerate(ev_int) if e.type == EventType.PHASE_ERROR)
        idx_pf = next(i for i, e in enumerate(ev_int) if e.type == EventType.PROJECT_FAILED)
        assert idx_pe < idx_pf
        assert ev_int[idx_pe].payload["state"] == ProjectState.INTERVIEW.value
        assert secret not in ev_int[idx_pe].model_dump_json()

        # 2. DEBATE error
        orch_deb = Orchestrator(
            store=store,
            llm=FakeLLM(
                responses=[
                    _complete_spec_response(),
                    ValueError(f"Debate validation boom GITHUB_TOKEN={secret}"),
                    ValueError(f"Debate validation boom GITHUB_TOKEN={secret}"),
                    ValueError(f"Debate validation boom GITHUB_TOKEN={secret}"),
                ]
            ),
        )
        orch_deb.create_project("err-debate", "Err Debate", "Obj")
        orch_deb.handle_interview_message("err-debate", "e1", "spec")
        st_deb = orch_deb.run_debate("err-debate")
        assert st_deb.state == ProjectState.FAILED
        ev_deb = store.get_events("err-debate")
        pe_deb = next(e for e in ev_deb if e.type == EventType.PHASE_ERROR)
        pf_deb = next(e for e in ev_deb if e.type == EventType.PROJECT_FAILED)
        assert ev_deb.index(pe_deb) < ev_deb.index(pf_deb)
        assert pe_deb.payload["state"] == ProjectState.DEBATE.value
        assert secret not in pe_deb.model_dump_json()

        # 3. MEDIATION error (e.g. bad template_id PlanValidationError)
        bad_plan = _valid_plan_dict()
        bad_plan["template_id"] = f"missing-tmpl-{secret}"
        orch_med = Orchestrator(
            store=store,
            llm=FakeLLM(
                responses=[
                    _complete_spec_response(),
                    _pragmatist_dict(),
                    _structuralist_dict(),
                    bad_plan,
                ]
            ),
        )
        orch_med.create_project("err-mediation", "Err Mediation", "Obj")
        orch_med.handle_interview_message("err-mediation", "e1", "spec")
        st_med = orch_med.run_debate("err-mediation")
        assert st_med.state == ProjectState.FAILED
        ev_med = store.get_events("err-mediation")
        pe_med = next(e for e in ev_med if e.type == EventType.PHASE_ERROR)
        pf_med = next(e for e in ev_med if e.type == EventType.PROJECT_FAILED)
        assert ev_med.index(pe_med) < ev_med.index(pf_med)
        assert pe_med.payload["state"] == ProjectState.MEDIATION.value
        assert secret not in pe_med.model_dump_json()

        # 4. BUILDING error
        class _FailCodeAgent(FakeCodeAgent):
            def implement_or_fix(
                self,
                workspace_dir: Path,
                plan: Plan,
                failure_output: str = "",
                attempt: int = 1,
            ) -> Any:
                raise CodeAgentError(f"Builder crash GITHUB_TOKEN={secret}")

        orch_bld = Orchestrator(
            store=store,
            llm=FakeLLM(
                responses=[
                    _complete_spec_response(),
                    _pragmatist_dict(),
                    _structuralist_dict(),
                    _valid_plan_dict(),
                ]
            ),
        )
        orch_bld.create_project("err-building", "Err Build", "Obj")
        orch_bld.handle_interview_message("err-building", "e1", "spec")
        wait_bld = orch_bld.run_debate("err-building")
        assert wait_bld.plan_hash is not None
        orch_bld.submit_approval(
            "err-building",
            ApprovalEvent(
                plan_hash=wait_bld.plan_hash,
                timestamp=datetime(2026, 11, 18, 12, 0, tzinfo=UTC),
                decision="approved",
            ),
        )
        worker_bld = BuildWorker(
            orchestrator=orch_bld,
            workspace_manager=WorkspaceManager(base_dir=tmp_path / "ws_bld"),
            code_agent=_FailCodeAgent(
                test_gen_steps=[{"tests/test_app.py": "def test_ok(): assert True\n"}]
            ),
            runner=FakeRunner(),
            vcs=FakeVCS(),
            secrets_to_redact=(secret,),
        )
        worker_bld.run_job("err-building")
        assert orch_bld.get_status("err-building").state == ProjectState.FAILED
        ev_bld = store.get_events("err-building")
        pe_bld = next(e for e in ev_bld if e.type == EventType.PHASE_ERROR)
        pf_bld = next(e for e in ev_bld if e.type == EventType.PROJECT_FAILED)
        assert ev_bld.index(pe_bld) < ev_bld.index(pf_bld)
        assert pe_bld.payload["state"] == ProjectState.BUILDING.value
        assert secret not in pe_bld.model_dump_json()

        # 5. VERIFYING error
        class _FailVerifyVCS(FakeVCS):
            def get_ci_result(self, project_id: str, branch: str = "work") -> CIRunResult:
                raise RuntimeError(f"CI API down GITHUB_TOKEN={secret}")

        orch_ver = Orchestrator(
            store=store,
            llm=FakeLLM(
                responses=[
                    _complete_spec_response(),
                    _pragmatist_dict(),
                    _structuralist_dict(),
                    _valid_plan_dict(),
                ]
            ),
        )
        orch_ver.create_project("err-verifying", "Err Verify", "Obj")
        orch_ver.handle_interview_message("err-verifying", "e1", "spec")
        wait_ver = orch_ver.run_debate("err-verifying")
        assert wait_ver.plan_hash is not None
        orch_ver.submit_approval(
            "err-verifying",
            ApprovalEvent(
                plan_hash=wait_ver.plan_hash,
                timestamp=datetime(2026, 11, 18, 12, 0, tzinfo=UTC),
                decision="approved",
            ),
        )
        worker_ver = BuildWorker(
            orchestrator=orch_ver,
            workspace_manager=WorkspaceManager(base_dir=tmp_path / "ws_ver"),
            code_agent=FakeCodeAgent(
                test_gen_steps=[{"tests/test_app.py": "def test_ok(): assert True\n"}],
                impl_steps=[{"src/app.js": "export const ok = 1;\n"}],
            ),
            runner=FakeRunner(
                results=[CommandResult(exit_code=0, stdout="1 passed", test_results={"AT-01": True})]
            ),
            vcs=_FailVerifyVCS(),
            secrets_to_redact=(secret,),
        )
        worker_ver.run_job("err-verifying")
        assert orch_ver.get_status("err-verifying").state == ProjectState.FAILED
        ev_ver = store.get_events("err-verifying")
        pe_ver = next(e for e in ev_ver if e.type == EventType.PHASE_ERROR)
        pf_ver = next(e for e in ev_ver if e.type == EventType.PROJECT_FAILED)
        assert ev_ver.index(pe_ver) < ev_ver.index(pf_ver)
        assert pe_ver.payload["state"] == ProjectState.VERIFYING.value
        assert secret not in pe_ver.model_dump_json()
    finally:
        store.close()


def test_retry_project_resumes_debate_with_spec_intact_and_waiting_approval_with_plan(
    tmp_path: Path,
) -> None:
    """retry_project resets breaker and resumes DEBATE with Spec intact, MEDIATION cleanly, or WAITING_APPROVAL if Plan exists."""
    store = SqliteEventStore(db_path=tmp_path / "retry.db")
    try:
        # Case A: Fail in DEBATE -> retry_project -> DEBATE with Spec intact -> run_debate succeeds
        llm_a = FakeLLM(
            responses=[
                _complete_spec_response(),
                LLMError("Debate attempt 1"),
                LLMError("Debate attempt 2"),
                LLMError("Debate attempt 3"),
                _pragmatist_dict(),
                _structuralist_dict(),
                _valid_plan_dict(),
            ]
        )
        orch_a = Orchestrator(store=store, llm=llm_a)
        pid_a = "retry-debate"
        orch_a.create_project(pid_a, "Retry Debate", "Obj")
        orch_a.handle_interview_message(pid_a, "evt-a1", "Spec complete")
        failed_a = orch_a.run_debate(pid_a)
        assert failed_a.state == ProjectState.FAILED
        assert failed_a.failure_reason is not None

        resumed_a = orch_a.retry_project(pid_a)
        assert resumed_a.state == ProjectState.DEBATE
        assert resumed_a.progress_pct == 25
        assert resumed_a.failure_reason is None
        assert orch_a.circuit_breaker.get_calls(pid_a, ProjectState.DEBATE) == 0
        assert orch_a.get_spec(pid_a).objective == "Task tracker web app"

        waiting_a = orch_a.run_debate(pid_a)
        assert waiting_a.state == ProjectState.WAITING_APPROVAL
        assert waiting_a.plan_hash is not None
        # Verify full replay consistency
        assert (
            project_status_from_events(pid_a, store.get_events(pid_a)).state
            == ProjectState.WAITING_APPROVAL
        )

        # Case B: Fail in MEDIATION -> retry_project -> MEDIATION (reuses stored debate artifacts) -> WAITING_APPROVAL
        bad_plan = _valid_plan_dict()
        bad_plan["acceptance_tests"] = []
        llm_b = FakeLLM(
            responses=[
                _complete_spec_response(),
                _pragmatist_dict(),
                _structuralist_dict(),
                bad_plan,
                _valid_plan_dict(summary="Recovered mediator plan"),
            ]
        )
        orch_b = Orchestrator(store=store, llm=llm_b)
        pid_b = "retry-mediation"
        orch_b.create_project(pid_b, "Retry Mediation", "Obj")
        orch_b.handle_interview_message(pid_b, "evt-b1", "Spec complete")
        failed_b = orch_b.run_debate(pid_b)
        assert failed_b.state == ProjectState.FAILED

        resumed_b = orch_b.retry_project(pid_b)
        assert resumed_b.state == ProjectState.MEDIATION
        assert resumed_b.progress_pct == 30
        assert orch_b.circuit_breaker.get_calls(pid_b, ProjectState.MEDIATION) == 0
        calls_before_med = len(llm_b.calls)
        waiting_b = orch_b.run_debate(pid_b)
        assert waiting_b.state == ProjectState.WAITING_APPROVAL
        # Only 1 Mediator call needed because Pragmatist & Structuralist artifacts were persisted
        assert len(llm_b.calls) == calls_before_med + 1

        # Case C: Fail in BUILDING when Plan already exists -> retry_project -> WAITING_APPROVAL with Plan intact -> approve & deliver
        llm_c = FakeLLM(
            responses=[
                _complete_spec_response(),
                _pragmatist_dict(),
                _structuralist_dict(),
                _valid_plan_dict(summary="Persisted build plan"),
            ]
        )
        orch_c = Orchestrator(store=store, llm=llm_c)
        pid_c = "retry-build"
        orch_c.create_project(pid_c, "Retry Build", "Obj")
        orch_c.handle_interview_message(pid_c, "evt-c1", "Spec")
        wait_c = orch_c.run_debate(pid_c)
        assert wait_c.plan_hash is not None
        saved_hash = wait_c.plan_hash

        orch_c.submit_approval(
            pid_c,
            ApprovalEvent(
                plan_hash=saved_hash,
                timestamp=datetime(2026, 11, 18, 12, 30, tzinfo=UTC),
                decision="approved",
            ),
        )
        ws_mgr = WorkspaceManager(base_dir=tmp_path / "ws_c")
        queue = LocalJobQueue()
        failing_worker = BuildWorker(
            orchestrator=orch_c,
            workspace_manager=ws_mgr,
            code_agent=FakeCodeAgent(
                test_gen_steps=[{"tests/test_app.py": "def test_ok(): assert True\n"}],
                impl_steps=[CodeAgentError("Transient build CLI failure")],
            ),
            runner=FakeRunner(),
            vcs=FakeVCS(),
            queue=queue,
        )
        failing_worker.run_job(pid_c)
        assert orch_c.get_status(pid_c).state == ProjectState.FAILED

        resumed_c = orch_c.retry_project(pid_c)
        assert resumed_c.state == ProjectState.WAITING_APPROVAL
        assert resumed_c.plan_hash == saved_hash
        assert resumed_c.failure_reason is None
        assert resumed_c.progress_pct == 35
        assert orch_c.get_plan(pid_c) is not None
        assert orch_c.circuit_breaker.get_calls(pid_c, ProjectState.BUILDING) == 0

        # Cannot retry again once active
        with pytest.raises(ValueError):
            orch_c.retry_project(pid_c)
        with pytest.raises(ValueError):
            orch_c.retry_project("missing-project-id")

        # Re-approve and build cleanly to DELIVERED
        orch_c.submit_approval(
            pid_c,
            ApprovalEvent(
                plan_hash=saved_hash,
                timestamp=datetime(2026, 11, 18, 12, 35, tzinfo=UTC),
                decision="approved",
            ),
        )
        # Clear previous test contamination from workspace before fresh run
        ws_dir = ws_mgr.get_workspace_dir(pid_c)
        for p in (ws_dir / "src").rglob("*"):
            if p.is_file():
                p.unlink()
        for p in (ws_dir / "tests").rglob("*"):
            if p.is_file():
                p.unlink()

        ok_worker = BuildWorker(
            orchestrator=orch_c,
            workspace_manager=ws_mgr,
            code_agent=FakeCodeAgent(
                test_gen_steps=[{"tests/test_app.py": "def test_ok(): assert True\n"}],
                impl_steps=[{"src/app.js": "export const ok = 1;\n"}],
            ),
            runner=FakeRunner(
                results=[CommandResult(exit_code=0, stdout="1 passed", test_results={"AT-01": True})]
            ),
            vcs=FakeVCS(ci_results=[CIRunResult(passed=True, summary="CI green")]),
            queue=queue,
        )
        ok_worker.run_job(pid_c)
        assert orch_c.get_status(pid_c).state == ProjectState.DELIVERED

        # Replaying invalid PROJECT_RETRIED when not FAILED raises IllegalTransition
        bad_events = store.get_events(pid_c) + [
            Event(
                id="evt-bad-retry",
                project_id=pid_c,
                type=EventType.PROJECT_RETRIED,
                payload={"to": ProjectState.DEBATE.value},
                at=datetime(2026, 11, 18, 12, 40, tzinfo=UTC),
            )
        ]
        with pytest.raises(IllegalTransition):
            project_status_from_events(pid_c, bad_events)
    finally:
        store.close()


def test_sqlite_thread_safety_and_delete_project(tmp_path: Path) -> None:
    """SqliteEventStore supports concurrent threads safely and delete_project deletes events and chat links."""
    store = SqliteEventStore(db_path=tmp_path / "threads.db")
    try:
        orch = Orchestrator(store=store, llm=FakeLLM())
        errors: list[Exception] = []

        def _worker(idx: int) -> None:
            pid = f"thread-proj-{idx}"
            try:
                orch.create_project(pid, f"Thread {idx}", "Concurrent test")
                store.set_telegram_active_project(1000 + idx, pid)
                events = store.get_events(pid)
                assert len(events) == 1
                assert pid in store.list_project_ids()
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=_worker, args=(i,)) for i in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5.0)
        assert errors == []

        assert store.get_telegram_active_projects()[1002] == "thread-proj-2"
        assert orch.delete_project("thread-proj-2") is True
        assert orch.delete_project("thread-proj-2") is False
        assert store.get_events("thread-proj-2") == []
        assert 1002 not in store.get_telegram_active_projects()
        assert "thread-proj-2" not in store.list_project_ids()
        assert "thread-proj-1" in store.list_project_ids()

        with pytest.raises(ValueError):
            orch.delete_project("../escape")
    finally:
        store.close()


def test_retry_orphaned_project_in_building_debate_and_mediation(tmp_path: Path) -> None:
    """retry_project recovers orphaned projects in BUILDING (to WAITING_APPROVAL), DEBATE, and MEDIATION."""
    store = SqliteEventStore(db_path=tmp_path / "orphaned_retry.db")
    try:
        now = datetime(2026, 11, 18, 12, 0, tzinfo=UTC)

        # 1. Orphaned in BUILDING: has plan, was approved, server restarted -> retry leaves it in WAITING_APPROVAL
        pid_bld = "orphan-bld"
        plan = Plan(
            summary="Building Plan",
            rationale="Testing",
            template_id="static-web",
            deployment_target="static",
            files=[FileSpec(path="index.html", purpose="UI", contract="Contract")],
            acceptance_tests=[AcceptanceTest(id="AT-01", type=AcceptanceTestType.UNIT, description="Pass")],
        )
        plan_dict = plan.model_dump(mode="json")
        plan_hash = plan.compute_hash()
        store.append(create_event(pid_bld, EventType.PROJECT_CREATED, {"project_id": pid_bld, "name": "Bld App", "objective": "Obj"}, at=now))
        store.append(create_event(pid_bld, EventType.STATE_TRANSITIONED, {"from": "INTERVIEW", "to": "DEBATE"}, at=now))
        store.append(create_event(pid_bld, EventType.STATE_TRANSITIONED, {"from": "DEBATE", "to": "MEDIATION"}, at=now))
        store.append(create_event(pid_bld, EventType.PLAN_CREATED, {"plan": plan_dict, "plan_hash": plan_hash, "from": "MEDIATION", "to": "WAITING_APPROVAL"}, at=now))
        store.append(create_event(pid_bld, EventType.APPROVAL_RECORDED, {"plan_hash": plan_hash, "decision": "approved", "from": "WAITING_APPROVAL", "to": "BUILDING"}, at=now))

        orch = Orchestrator(store=store, llm=FakeLLM())
        assert orch.get_status(pid_bld).state == ProjectState.BUILDING
        # Simulate circuit breaker having had invocations
        orch.circuit_breaker.record_call(pid_bld, ProjectState.BUILDING)
        assert orch.circuit_breaker.get_calls(pid_bld, ProjectState.BUILDING) == 1

        resumed_bld = orch.retry_project(pid_bld)
        assert resumed_bld.state == ProjectState.WAITING_APPROVAL
        assert resumed_bld.plan_hash == plan_hash
        assert resumed_bld.progress_pct == 35
        assert orch.circuit_breaker.get_calls(pid_bld, ProjectState.BUILDING) == 0

        # 2. Orphaned in DEBATE: server restarted during debate -> retry resets breaker and keeps DEBATE
        pid_deb = "orphan-deb"
        store.append(create_event(pid_deb, EventType.PROJECT_CREATED, {"project_id": pid_deb, "name": "Deb App", "objective": "Obj"}, at=now))
        store.append(create_event(pid_deb, EventType.STATE_TRANSITIONED, {"from": "INTERVIEW", "to": "DEBATE"}, at=now))
        assert orch.get_status(pid_deb).state == ProjectState.DEBATE
        orch.circuit_breaker.record_call(pid_deb, ProjectState.DEBATE)
        assert orch.circuit_breaker.get_calls(pid_deb, ProjectState.DEBATE) == 1

        resumed_deb = orch.retry_project(pid_deb)
        assert resumed_deb.state == ProjectState.DEBATE
        assert resumed_deb.progress_pct == 25
        assert orch.circuit_breaker.get_calls(pid_deb, ProjectState.DEBATE) == 0

        # 3. Orphaned in MEDIATION: server restarted during mediation -> retry resets breaker and keeps MEDIATION
        pid_med = "orphan-med"
        store.append(create_event(pid_med, EventType.PROJECT_CREATED, {"project_id": pid_med, "name": "Med App", "objective": "Obj"}, at=now))
        store.append(create_event(pid_med, EventType.STATE_TRANSITIONED, {"from": "INTERVIEW", "to": "DEBATE"}, at=now))
        store.append(create_event(pid_med, EventType.STATE_TRANSITIONED, {"from": "DEBATE", "to": "MEDIATION"}, at=now))
        assert orch.get_status(pid_med).state == ProjectState.MEDIATION
        orch.circuit_breaker.record_call(pid_med, ProjectState.MEDIATION)
        assert orch.circuit_breaker.get_calls(pid_med, ProjectState.MEDIATION) == 1

        resumed_med = orch.retry_project(pid_med)
        assert resumed_med.state == ProjectState.MEDIATION
        assert resumed_med.progress_pct == 30
        assert orch.circuit_breaker.get_calls(pid_med, ProjectState.MEDIATION) == 0

        # 4. Attempting to retry a project in an invalid state (e.g. WAITING_APPROVAL or DELIVERED) raises ValueError
        with pytest.raises(ValueError, match="expected one of"):
            orch.retry_project(pid_bld)  # now in WAITING_APPROVAL
    finally:
        store.close()
