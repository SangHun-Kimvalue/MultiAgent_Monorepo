"""Phase 9 P1 — 타임아웃 정리가 프로세스 트리를 실제로 죽이는지 (RED-first).

⚠ **현재 코드에서 실패하도록 작성됐다.** 그게 목적이다.

C(`orch_drivers.run_subprocess_tool`)는 D1·D2 둘 다 미조치,
D(`orch_relay_driver.run_subprocess_tool`)는 **D2만 조치**돼 있어 고아(D1)로 RED 가 난다.
D 는 bounded `communicate()` 뒤 **`finally` 에서 다시 무제한 `proc.kill(); await proc.wait()`**
가 돌므로 부분 실패 시 거기서 다시 행이 될 수 있다 — watchdog 이 그것도 잡는다.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

from acp.orch_drivers import run_subprocess_tool as drivers_run
from acp.orch_relay_driver import run_subprocess_tool as relay_run

from tests._process_tree_fixture import reap, survivors_after, tree_command

CALL_WATCHDOG_S = 20.0
CHILD_TIMEOUT_S = 1.0


async def _run_case(runner, md: Path, mode: str, cwd: Path) -> None:
    try:
        run = await asyncio.wait_for(
            runner(tree_command(md, mode=mode), cwd=cwd, timeout_s=CHILD_TIMEOUT_S),
            timeout=CALL_WATCHDOG_S,
        )
        assert run.timed_out is True
        assert survivors_after(md) == [], "타임아웃 뒤에도 후손이 살아 있다(고아)"
    except (asyncio.TimeoutError, TimeoutError):
        pytest.fail(f"{runner.__module__} 가 {CALL_WATCHDOG_S}s 안에 반환하지 않았다(무한 대기)")
    finally:
        reap(md)


# ── C: orch_drivers.run_subprocess_tool (orch_drivers.py:101) ─────────
@pytest.mark.parametrize("mode", ["parent", "parent_exit"])
async def test_c_orch_drivers_timeout_leaves_no_orphan(tmp_path: Path, mode: str) -> None:
    md = tmp_path / ("c_" + mode)
    md.mkdir(parents=True, exist_ok=True)
    await _run_case(drivers_run, md, mode, tmp_path)


# ── D: orch_relay_driver.run_subprocess_tool (orch_relay_driver.py:79) ─
@pytest.mark.parametrize("mode", ["parent", "parent_exit"])
async def test_d_orch_relay_driver_timeout_leaves_no_orphan(tmp_path: Path, mode: str) -> None:
    md = tmp_path / ("d_" + mode)
    md.mkdir(parents=True, exist_ok=True)
    await _run_case(relay_run, md, mode, tmp_path)


# ── 결함 주입 (ztr P2 에서 mutation 이 드러낸 공백을 그대로 이식) ────────
RUNNERS = {"orch_drivers": drivers_run, "orch_relay_driver": relay_run}


@pytest.mark.parametrize("runner_name", sorted(RUNNERS))
@pytest.mark.parametrize("mode", ["parent", "parent_exit"])
async def test_cleanup_is_bounded_when_boundary_termination_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, runner_name: str, mode: str
) -> None:
    """경계 확보가 실패해도 **제한 시간 안에 반환**하고 열화를 보고한다.

    `parent_exit` 를 함께 돈다 — "직계가 죽었으면 정리할 게 없다"는 가드가 부활하면
    정리는 `finally` 가 대신 하지만 **토큰이 반환값에서 사라진다.**
    """
    from acp import process_supervisor as ps

    md = tmp_path / f"fault_{runner_name}_{mode}"
    md.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(f"acp.{runner_name}.attach", lambda proc: None)
    try:
        run = await asyncio.wait_for(
            RUNNERS[runner_name](tree_command(md, mode=mode),
                                 cwd=tmp_path, timeout_s=CHILD_TIMEOUT_S),
            timeout=CALL_WATCHDOG_S,
        )
        assert run.timed_out is True
        assert ps.CLEANUP_UNVERIFIED in run.cleanup, run.cleanup
        assert ps.DRAIN_ABANDONED in run.cleanup, run.cleanup
    except (asyncio.TimeoutError, TimeoutError):
        pytest.fail("경계 종료 실패 시 정리가 상한 없이 매달렸다(무한 대기)")
    finally:
        reap(md)


@pytest.mark.parametrize("runner_name", sorted(RUNNERS))
async def test_cleanup_token_survives_normal_completion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, runner_name: str
) -> None:
    """**타임아웃이 아닌** 정상 종료 경로에서도 정리 열화가 결과에 남아야 한다."""
    from acp import process_supervisor as ps

    monkeypatch.setattr(f"acp.{runner_name}.attach", lambda proc: None)
    run = await asyncio.wait_for(
        RUNNERS[runner_name]([sys.executable, "-c", "pass"],
                             cwd=tmp_path, timeout_s=30.0),
        timeout=CALL_WATCHDOG_S,
    )
    assert run.timed_out is False
    assert ps.CLEANUP_UNVERIFIED in run.cleanup, run.cleanup


async def test_deadline_exhausted_reports_drain_abandoned_only(tmp_path: Path) -> None:
    """정리 예산이 소진되면 `drain-abandoned` 만 붙는다(`cleanup-partial` 아님)."""
    from acp import process_supervisor as ps

    md = tmp_path / "deadline"
    md.mkdir(parents=True, exist_ok=True)
    proc = await asyncio.create_subprocess_exec(
        *tree_command(md), stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE, **ps.spawn_kwargs(),
    )
    job = ps.attach(proc)
    await asyncio.sleep(1.2)
    try:
        _out, _err, tokens = await asyncio.wait_for(
            ps.terminate_tree(proc, job, deadline_s=0.0), timeout=CALL_WATCHDOG_S)
        assert tokens == (ps.DRAIN_ABANDONED,), tokens
    finally:
        reap(md)


# ── 소비자 경계까지 도달하는가 (출력 R4 major) ────────────────────────
def test_cleanup_reaches_segment_result(tmp_path: Path) -> None:
    """정리 열화가 `SegmentResult.cleanup` 으로 **소비자에게 도달**해야 한다.

    출력 R4 major: `ToolRun.cleanup` 에만 있으면 상위 결과는 종전과 같아 보인다 —
    "세그먼트는 끝났지만 프로세스 트리는 통제하지 못했다"를 호출자가 알 수 없다.
    """
    from acp.orch_relay_driver import ToolRun, ZtrRelayDriver
    from acp.orch_runs import SegmentStatus
    from acp.process_supervisor import CLEANUP_UNVERIFIED

    async def fake_runner(command, *, cwd, timeout_s):   # noqa: ANN001, ANN202
        return ToolRun(command=tuple(command), exit_code=None, timed_out=True,
                       error="timeout", duration_s=0.1,
                       cleanup=(CLEANUP_UNVERIFIED,))

    driver = ZtrRelayDriver(
        python="python",
        runner_script=tmp_path / "runner.py",
        cwd=tmp_path,
        implementer_cmd='["python","impl.py"]',
        reviewer_cmd='["python","review.py"]',
        mechanical_cmd='["python","nit.py"]',
        test_cmd='["python","test.py"]',
        output_dir=".ztr/test-relay",
        leg_timeout_s=17.0,
        process_timeout_s=23.0,
        runner=fake_runner,
    )
    result = asyncio.run(
        driver.run_segment(prompt="x", project_id="p", phase_id="ph",
                           run_id="r", resume_token=None)
    )
    assert result.status is SegmentStatus.BLOCKED
    assert CLEANUP_UNVERIFIED in result.cleanup, result.cleanup
