"""Phase 9 P1 — 타임아웃 정리가 프로세스 트리를 실제로 죽이는지 (RED-first).

⚠ **이 테스트들은 현재 코드에서 실패하도록 작성됐다.** 그게 목적이다.
기존 `test_phase_relay_timeout_kills_process_and_returns_124` 는 이름과 달리 봉투 필드만
검증하고 프로세스 생사를 보지 않아 결함을 못 잡았다(계획 §2 관측 3).

각 테스트는 **외부 watchdog**(`asyncio.wait_for`)으로 감싸 행에 빠지지 않게 하고,
`finally` 에서 **최종 회수**(`reap`)를 돌려 고아를 남기지 않는다.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

from src.engine.phase_relay import PhaseRelay, RelayCommand
from src.engine.static_review import run_subprocess_tool

from tests._process_tree_fixture import reap, survivors_after, tree_command

# 호출이 이 시간 안에 반환하지 않으면 "행"으로 판정한다. 정리 상한(5s)보다 넉넉히 크다.
CALL_WATCHDOG_S = 20.0
CHILD_TIMEOUT_S = 1.0


async def _markers(tmp_path: Path, name: str) -> Path:
    md = tmp_path / name
    md.mkdir(parents=True, exist_ok=True)
    return md


# ── A: PhaseRelay (phase_relay.py:605) ────────────────────────────────
@pytest.mark.parametrize("mode", ["parent", "parent_exit"])
async def test_a_phase_relay_timeout_leaves_no_orphan(tmp_path: Path, mode: str) -> None:
    md = await _markers(tmp_path, "a_" + mode)
    prompt = tmp_path / "prompt.md"
    prompt.write_text("p", encoding="utf-8")
    relay = PhaseRelay(
        prompt_path=prompt,
        commands=[RelayCommand(name="implementer",
                               argv=tree_command(md, mode=mode),
                               timeout_s=CHILD_TIMEOUT_S)],
        output_dir=tmp_path / "runs",
        timeout_s=CALL_WATCHDOG_S,
    )
    try:
        report = await asyncio.wait_for(relay.run(), timeout=CALL_WATCHDOG_S)
        assert report.steps[0].timed_out is True
        assert survivors_after(md) == [], "타임아웃 뒤에도 후손이 살아 있다(고아)"
    except (asyncio.TimeoutError, TimeoutError):
        pytest.fail(f"relay.run() 이 {CALL_WATCHDOG_S}s 안에 반환하지 않았다(무한 대기)")
    finally:
        reap(md)


# ── B: static_review.run_subprocess_tool (static_review.py:262) ───────
@pytest.mark.parametrize("mode", ["parent", "parent_exit"])
async def test_b_static_review_timeout_leaves_no_orphan(tmp_path: Path, mode: str) -> None:
    md = await _markers(tmp_path, "b_" + mode)
    try:
        run = await asyncio.wait_for(
            run_subprocess_tool(tree_command(md, mode=mode),
                                cwd=tmp_path, timeout_s=CHILD_TIMEOUT_S),
            timeout=CALL_WATCHDOG_S,
        )
        assert run.timed_out is True
        assert survivors_after(md) == [], "타임아웃 뒤에도 후손이 살아 있다(고아)"
    except (asyncio.TimeoutError, TimeoutError):
        pytest.fail(f"run_subprocess_tool 이 {CALL_WATCHDOG_S}s 안에 반환하지 않았다(무한 대기)")
    finally:
        reap(md)


# ── 부분 실패 주입 (계획 §6 P1 항목 4) ────────────────────────────────
# supervisor 가 생긴 뒤에야 주입할 대상이 있으므로 P2 에서 붙인다.
# **경계 종료가 성공하면 파이프가 닫혀 drain 상한이 쓰이지 않는다** — 상한이 실제로
# 필요한 건 경계 종료가 실패했을 때뿐이다. mutation 에서 이 공백이 드러났다.
@pytest.mark.parametrize("mode", ["parent", "parent_exit"])
async def test_cleanup_is_bounded_when_boundary_termination_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    """Job 편입/종료가 실패해도 **호출은 제한 시간 안에 반환**하고 열화를 보고한다.

    ⚠ `parent_exit`(직계 선종료)를 함께 돈다 — "직계가 죽었으면 정리할 게 없다"는 가드가
    부활하면 정리는 `finally` 가 대신 하지만 **토큰이 반환값에서 사라진다.** 그 경로를
    테스트가 보지 않으면 가드 부활을 검출하지 못한다(출력 R1 mutation 정정).
    """
    from src.engine import process_supervisor as ps

    md = tmp_path / ("fault_" + mode)
    md.mkdir(parents=True, exist_ok=True)
    # 경계 확보 자체를 실패시킨다(중첩 Job 제약·권한 오류 등을 흉내낸다).
    monkeypatch.setattr(ps, "attach", lambda proc: None)
    monkeypatch.setattr("src.engine.static_review.attach", lambda proc: None)
    try:
        run = await asyncio.wait_for(
            run_subprocess_tool(tree_command(md, mode=mode),
                                cwd=tmp_path, timeout_s=CHILD_TIMEOUT_S),
            timeout=CALL_WATCHDOG_S,
        )
        assert run.timed_out is True
        # 정리 못 했으면 **못 했다고 말해야 한다.** 조용히 통과하지 않는다.
        assert ps.CLEANUP_UNVERIFIED in run.cleanup, run.cleanup
        assert ps.DRAIN_ABANDONED in run.cleanup, run.cleanup
    except (asyncio.TimeoutError, TimeoutError):
        pytest.fail("경계 종료 실패 시 정리가 상한 없이 매달렸다(무한 대기)")
    finally:
        reap(md)


# ── 출력 R1 이 지목한 검출력 공백 2건 ─────────────────────────────────
async def test_cleanup_token_survives_normal_completion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """**타임아웃이 아닌** 정상 종료 경로에서도 정리 열화가 결과에 남아야 한다.

    출력 R1 P1: `finally` 의 토큰을 버리면 "비어 있으면 정리 완료" 계약이 거짓이 된다 —
    편입이 실패했는데 결과가 깨끗해 보인다(silent fallback).
    """
    from src.engine import process_supervisor as ps

    monkeypatch.setattr("src.engine.static_review.attach", lambda proc: None)
    run = await asyncio.wait_for(
        run_subprocess_tool([__import__("sys").executable, "-c", "pass"],
                            cwd=tmp_path, timeout_s=30.0),
        timeout=CALL_WATCHDOG_S,
    )
    assert run.timed_out is False
    assert ps.CLEANUP_UNVERIFIED in run.cleanup, run.cleanup


async def test_deadline_exhausted_reports_drain_abandoned_only(tmp_path: Path) -> None:
    """정리 예산이 소진되면 `drain-abandoned` 만 붙는다.

    출력 R1 정정: `remaining <= 0` 조기 반환을 지우면 음수 timeout 이 예외 경로로 흘러
    **`cleanup-partial` 까지 붙는다** — 경계 종료는 성공했는데 부분 실패라 말하는 건
    의미상 틀리다. 즉 그 분기는 행동 중립이 아니다.
    """
    from src.engine import process_supervisor as ps

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


async def test_a_relay_cleanup_token_survives_normal_completion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A(PhaseRelay)도 정상 종료 경로의 정리 열화를 결과에 남겨야 한다(출력 R1 P1)."""
    from src.engine import process_supervisor as ps
    import sys as _sys

    monkeypatch.setattr("src.engine.phase_relay.attach", lambda proc: None)
    prompt = tmp_path / "p.md"
    prompt.write_text("p", encoding="utf-8")
    relay = PhaseRelay(
        prompt_path=prompt,
        commands=[RelayCommand(name="implementer",
                               argv=[_sys.executable, "-c", "pass"], timeout_s=30.0)],
        output_dir=tmp_path / "runs",
        timeout_s=CALL_WATCHDOG_S,
    )
    report = await asyncio.wait_for(relay.run(), timeout=CALL_WATCHDOG_S)
    assert report.steps[0].timed_out is False
    assert ps.CLEANUP_UNVERIFIED in report.cleanup_tokens(), report.cleanup_tokens()


async def test_cleanup_token_survives_oserror_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`OSError` 반환 경로에서도 정리 열화가 남아야 한다(출력 R2 P1).

    예외 블록에서 곧바로 `return` 하면 반환값이 먼저 만들어져 `finally` 의 토큰이 유실된다.
    ⚠ 주입은 **첫 `wait_for` 한 번만** 한다 — 전역으로 바꾸면 `terminate_tree` 의 bounded
    drain 까지 깨져 정리 경로 자체를 관측할 수 없다.
    """
    from src.engine import process_supervisor as ps
    import sys as _sys

    monkeypatch.setattr("src.engine.static_review.attach", lambda proc: None)
    real_wait_for = asyncio.wait_for
    calls = {"n": 0}

    async def once(awaitable, timeout):          # noqa: ANN001, ANN202
        calls["n"] += 1
        if calls["n"] == 1:
            awaitable.close()
            raise OSError("injected transport failure")
        return await real_wait_for(awaitable, timeout)

    monkeypatch.setattr(asyncio, "wait_for", once)
    # 감시(watchdog)는 **원본** wait_for 로 건다 — 주입본으로 걸면 감시가 첫 호출을 먹는다.
    run = await real_wait_for(
        run_subprocess_tool([_sys.executable, "-c", "pass"],
                            cwd=tmp_path, timeout_s=30.0),
        CALL_WATCHDOG_S,
    )
    assert run.error == "injected transport failure"
    assert ps.CLEANUP_UNVERIFIED in run.cleanup, run.cleanup


@pytest.mark.skipif(sys.platform != "win32", reason="Job Object 는 Windows 전용")
async def test_attach_survives_closehandle_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`CloseHandle(hproc)` 이 던져도 반환이 취소되지 않고, **그 Job 이 실제로 쓸 수 있다.**

    출력 R2 P2: 정리가 `finally` 에서 예외를 내면 `return job` 이 취소되는데
    `handed_over` 는 이미 True 라 Job 도 안 닫혀 **양쪽 다 놓친다.**

    ⚠ 출력 R3 P2: "`None` 이 아니다"까지만 보면 **핵심 계약을 검증하지 못한다.**
    반환받은 **동일한 핸들**로 종료·회수까지 확인해야 한다. 그리고 주입 프록시가 실제
    close 를 건너뛰면 **테스트 자체가 핸들을 누수**한다 — 닫고 나서 던진다.
    """
    from src.engine import process_supervisor as ps

    md = tmp_path / "closehandle"
    md.mkdir(parents=True, exist_ok=True)
    real_kernel32 = ps._kernel32
    ctypes_mod, real_k = real_kernel32()

    class _CloseThenRaise:
        """실제 `CloseHandle` 을 수행한 **뒤** 예외를 던진다(테스트가 누수하지 않게)."""

        def __getattr__(self, name: str):        # noqa: ANN204
            return getattr(real_k, name)

        def CloseHandle(self, handle):           # noqa: N802, ANN001, ANN201
            real_k.CloseHandle(handle)
            raise RuntimeError("injected CloseHandle failure")

    monkeypatch.setattr(ps, "_kernel32", lambda: (ctypes_mod, _CloseThenRaise()))
    proc = await asyncio.create_subprocess_exec(
        *tree_command(md), stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE, **ps.spawn_kwargs(),
    )
    job = None
    try:
        job = ps.attach(proc)
        assert job is not None, "CloseHandle 예외가 attach 의 반환을 삼켰다"
        monkeypatch.setattr(ps, "_kernel32", real_kernel32)
        await asyncio.sleep(1.2)
        # **반환받은 바로 그 job** 으로 트리를 정리할 수 있어야 한다. 새로 편입하지 않는다.
        _out, _err, tokens = await asyncio.wait_for(
            ps.terminate_tree(proc, job), timeout=CALL_WATCHDOG_S)
        job = None   # terminate_tree 가 반납했다
        assert tokens == (), tokens
        assert survivors_after(md) == [], "그 job 으로 트리를 못 죽였다"
    finally:
        monkeypatch.setattr(ps, "_kernel32", real_kernel32)
        if job is not None:                       # 실패 경로에서도 회수한다
            await ps.terminate_tree(proc, job)
        reap(md)
