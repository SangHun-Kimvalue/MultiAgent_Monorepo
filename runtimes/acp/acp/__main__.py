"""acp/__main__.py — `python -m acp web` 진입점."""
from __future__ import annotations

import asyncio
import logging
import os
import sys
from pathlib import Path

import uvicorn

from acp.collectors.codex import CodexCollector
from acp.collectors.claude import ClaudeSessionCollector
from acp.collectors.cursor import CursorWorkspaceCollector
from acp.collectors.fake import FakeCollector
from acp.collectors.orch_collector import OrchEventCollector
from acp.config import AppConfig
from acp.notify import Notifier
from acp.orch_drivers import (
    ALLOWED_DRIVER_KINDS,
    DRIVER_KIND_MOCK,
    build_driver,
)
from acp.orch_relay_driver import DRIVER_KIND_ZTR_RELAY, ZtrRelayDriver
from acp.orch_runs import OrchRunManager
from acp.poller import Poller
from acp.store import SessionStore
from acp.web.app import EventBroadcaster, app, init_app

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
)
logger = logging.getLogger("acp.main")


# 종료 시 진행 중인 틱을 기다리는 한도. 넘기면 연결을 닫지 않고 나간다(T14 S6 감사 P1).
_SHUTDOWN_WAIT_SECONDS = 15.0

USAGE = (
    "사용법: python -m acp web [--fake] [--host HOST] [--port PORT] "
    "[--db-path PATH] [--events-log PATH] [--poll-interval SECONDS] "
    "[--orch-events-dir PATH] [--orch-driver mock|claude-cli|codex-cli|ztr-relay] "
    "[--ztr-python PATH] [--ztr-runner PATH] [--ztr-cwd PATH] "
    "[--ztr-implementer-cmd CMD] [--ztr-reviewer-cmd CMD] "
    "[--ztr-mechanical-cmd CMD] [--ztr-test-cmd CMD] [--ztr-timeout SECONDS] "
    "[--ztr-process-timeout SECONDS] [--ztr-output-dir PATH] [--no-toast]\n"
    "       python -m acp purge --app <name> [--yes] [--db-path PATH]"
    "  (미리보기가 기본, --yes 없이는 삭제하지 않음)"
)


async def _main(
    use_fake: bool = False,
    *,
    host: str | None = None,
    port: int | None = None,
    db_path: str | None = None,
    events_log: str | None = None,
    poll_interval: float | None = None,
    orch_events_dir: str | None = None,
    orch_driver: str = DRIVER_KIND_MOCK,
    ztr_python: str | None = None,
    ztr_runner: str | None = None,
    ztr_cwd: str | None = None,
    ztr_implementer_cmd: str | None = None,
    ztr_reviewer_cmd: str | None = None,
    ztr_mechanical_cmd: str | None = None,
    ztr_test_cmd: str | None = None,
    ztr_timeout: float | None = None,
    ztr_process_timeout: float | None = None,
    ztr_output_dir: str | None = None,
    no_toast: bool = False,
) -> None:
    cfg = AppConfig.load("config/paths.yaml")
    if host is not None:
        cfg.host = host
    if port is not None:
        cfg.port = port
    if db_path is not None:
        cfg.db_path = db_path
    elif use_fake:
        # 합성 데이터가 실 DB를 오염시키지 않도록 **경로 자체를 분리**한다(T14 S3 D4).
        # 과거 --fake 실행이 남긴 fake 행이 실데이터 총계에 섞여 있었다.
        fake_db = Path(cfg.db_path).with_name("acp-fake.db")
        cfg.db_path = str(fake_db)
        logger.info("--fake 모드: 별도 DB 사용 %s", cfg.db_path)
    if events_log is not None:
        cfg.events_log = events_log
    if poll_interval is not None:
        cfg.poll_interval = poll_interval
    if orch_events_dir is not None:
        cfg.orch_events_dir = orch_events_dir
    if no_toast:
        cfg.notify.toast_enabled = False
    store = SessionStore(cfg.db_path, cfg.events_log)
    bcast = EventBroadcaster()
    # 기본 driver는 mock — init_app이 MockGateDriver를 만들도록 run_manager=None을 둔다.
    # non-mock(claude-cli/codex-cli)일 때만 build_driver로 probe driver를 주입한다(opt-in).
    run_manager = None
    if orch_driver != DRIVER_KIND_MOCK:
        driver = (
            _build_ztr_relay_driver(
                ztr_python=ztr_python,
                ztr_runner=ztr_runner,
                ztr_cwd=ztr_cwd,
                ztr_implementer_cmd=ztr_implementer_cmd,
                ztr_reviewer_cmd=ztr_reviewer_cmd,
                ztr_mechanical_cmd=ztr_mechanical_cmd,
                ztr_test_cmd=ztr_test_cmd,
                ztr_timeout=ztr_timeout,
                ztr_process_timeout=ztr_process_timeout,
                ztr_output_dir=ztr_output_dir,
            )
            if orch_driver == DRIVER_KIND_ZTR_RELAY
            else build_driver(orch_driver)
        )
        run_manager = OrchRunManager(
            store=store,
            broadcaster=bcast,
            driver=driver,
        )
        logger.info("OrchRunManager driver=%s 주입", orch_driver)
    init_app(store, bcast, poll_interval=cfg.poll_interval, run_manager=run_manager)

    poller = Poller(store, cfg, bcast, notifier=Notifier(cfg.notify))

    if use_fake:
        poller.register(FakeCollector())
        logger.info("FakeCollector 등록 (--fake 모드)")
    else:
        sessions_base = cfg.get_path("codex_sessions")
        processes_path = cfg.get_path("codex_processes")
        poller.register(CodexCollector(sessions_base, processes_path))
        logger.info("CodexCollector 등록: sessions=%s processes=%s", sessions_base, processes_path)
        claude_sessions = cfg.get_path("claude_sessions")
        cursor_workspace = cfg.get_path("cursor_workspace")
        poller.register(
            ClaudeSessionCollector(
                claude_sessions,
                include_archived=cfg.include_archived,
            )
        )
        poller.register(CursorWorkspaceCollector(cursor_workspace))
        logger.info("ClaudeSessionCollector 등록: sessions=%s", claude_sessions)
        logger.info("CursorWorkspaceCollector 등록: workspace=%s", cursor_workspace)

    if cfg.orch_events_dir:
        poller.register_orch_collector(OrchEventCollector(Path(cfg.orch_events_dir)))
        logger.info("OrchEventCollector 등록: events_dir=%s", cfg.orch_events_dir)

    poller_task = asyncio.create_task(poller.run())

    config = uvicorn.Config(app, host=cfg.host, port=cfg.port, log_level="info")
    server = uvicorn.Server(config)

    try:
        await server.serve()
    finally:
        # **취소하지 않는다.** 수집·쓰기는 워커 스레드에서 돌고, 태스크를 취소해도
        # 그 스레드는 멈추지 않는다 — 곧바로 연결을 닫으면 쓰는 중에 닫힌다.
        poller.request_stop()
        try:
            await asyncio.wait_for(poller_task, timeout=_SHUTDOWN_WAIT_SECONDS)
        except asyncio.TimeoutError:
            # 워커가 아직 도는지 확인할 수 없으면 **닫지 않는다**. 프로세스가 끝나면
            # OS가 회수한다 — 확인 못 한 상태에서 닫는 것보다 안전하다.
            logger.warning(
                "폴러가 %.0f초 안에 멈추지 않았다 — 연결을 닫지 않고 종료한다"
                "(워커가 쓰는 중일 수 있다)",
                _SHUTDOWN_WAIT_SECONDS,
            )
            return
        except asyncio.CancelledError:
            logger.warning("종료 대기가 취소됐다 — 연결을 닫지 않고 종료한다")
            raise
        store.close()


def main() -> None:
    args = sys.argv[1:]
    if _wants_help(args):
        print(USAGE)
        return
    if not args or args[0] == "web":
        use_fake = "--fake" in args
        host = _arg_value(args, "--host")
        port_value = _arg_value(args, "--port")
        poll_value = _arg_value(args, "--poll-interval")
        ztr_timeout_value = _arg_value(args, "--ztr-timeout")
        ztr_process_timeout_value = _arg_value(args, "--ztr-process-timeout")
        orch_driver = (_arg_value(args, "--orch-driver") or DRIVER_KIND_MOCK).strip().lower()
        allowed_driver_kinds = {*ALLOWED_DRIVER_KINDS, DRIVER_KIND_ZTR_RELAY}
        if orch_driver not in allowed_driver_kinds:
            print(
                "--orch-driver 값은 mock|claude-cli|codex-cli 여야 합니다"
                f"(또는 ztr-relay): {orch_driver}",
                file=sys.stderr,
            )
            sys.exit(1)
        asyncio.run(_main(
            use_fake=use_fake,
            host=host,
            port=int(port_value) if port_value else None,
            db_path=_arg_value(args, "--db-path"),
            events_log=_arg_value(args, "--events-log"),
            poll_interval=float(poll_value) if poll_value else None,
            orch_events_dir=_arg_value(args, "--orch-events-dir"),
            orch_driver=orch_driver,
            ztr_python=_arg_value(args, "--ztr-python"),
            ztr_runner=_arg_value(args, "--ztr-runner"),
            ztr_cwd=_arg_value(args, "--ztr-cwd"),
            ztr_implementer_cmd=_arg_value(args, "--ztr-implementer-cmd"),
            ztr_reviewer_cmd=_arg_value(args, "--ztr-reviewer-cmd"),
            ztr_mechanical_cmd=_arg_value(args, "--ztr-mechanical-cmd"),
            ztr_test_cmd=_arg_value(args, "--ztr-test-cmd"),
            ztr_timeout=float(ztr_timeout_value) if ztr_timeout_value else None,
            ztr_process_timeout=(
                float(ztr_process_timeout_value) if ztr_process_timeout_value else None
            ),
            ztr_output_dir=_arg_value(args, "--ztr-output-dir"),
            no_toast="--no-toast" in args,
        ))
    elif args[0] == "purge":
        sys.exit(_run_purge(args[1:]))
    else:
        print(
            "알 수 없는 명령: "
            f"{args[0]}. {USAGE}",
            file=sys.stderr,
        )
        sys.exit(1)


# 삭제가 **지속적 정리**를 뜻하지 않는다는 사실을 CLI가 말한다(T14 S5 D3). 수집기는
# 시간 컷오프 없이 전량을 재스캔하므로, 원본 세션 파일이 남아 있으면 같은 행이 다음
# 수집 주기에 다시 만들어진다. 그 사실을 숨기면 사용자는 하지 않은 정리를 했다고 믿는다.
_PURGE_REGENERATION_NOTICE = (
    "\n주의: 원본 세션 파일이 남아 있으면 다음 수집 주기에 같은 행이 다시 생성됩니다"
    "(수집기는 시간 컷오프 없이 전량을 재스캔합니다)."
)


def _run_purge(args: list[str]) -> int:
    """앱 행 일회성 정리(T14 S3 D4). **사용자 승인 없이는 삭제하지 않는다.**

    안전 계약: ① 대상 미리보기 ② `app` 정확 일치로만 한정 ③ `--yes` 없이는 실행 금지
    ④ 삭제 건수를 감사 이벤트로 기록.
    """
    app = _arg_value(args, "--app")
    if not app:
        print("purge: --app <name> 필요(예: --app fake)", file=sys.stderr)
        return 2

    cfg = AppConfig.load("config/paths.yaml")
    db_path = _arg_value(args, "--db-path") or cfg.db_path
    store = SessionStore(db_path, cfg.events_log)
    try:
        preview = store.preview_app_rows(app)
        print(
            f"대상: app='{app}' {preview['count']}행 "
            f"(updated_at {preview['oldest']} ~ {preview['newest']}) · DB={db_path}"
            + _PURGE_REGENERATION_NOTICE
        )
        if preview["count"] == 0:
            print("삭제할 행이 없습니다.")
            return 0
        if "--yes" not in args:
            # 비가역 작업이므로 명시적 승인 없이는 여기서 멈춘다.
            print("미리보기만 수행했습니다. 실제 삭제하려면 --yes 를 붙이세요.")
            return 0
        deleted = store.purge_app_rows(app, confirmed=True)
        print(f"삭제 완료: {deleted}행 (감사 이벤트 rows_purged 기록됨)")
        return 0
    finally:
        store.close()


def _wants_help(args: list[str]) -> bool:
    if args in (["-h"], ["--help"]):
        return True
    if args and args[0] == "web" and any(arg in ("-h", "--help") for arg in args[1:]):
        return True
    return False


def _arg_value(args: list[str], name: str) -> str | None:
    if name not in args:
        return None
    index = args.index(name)
    if index + 1 >= len(args):
        raise SystemExit(f"{name} 값이 필요합니다")
    return args[index + 1]


def _build_ztr_relay_driver(
    *,
    ztr_python: str | None,
    ztr_runner: str | None,
    ztr_cwd: str | None,
    ztr_implementer_cmd: str | None,
    ztr_reviewer_cmd: str | None,
    ztr_mechanical_cmd: str | None,
    ztr_test_cmd: str | None,
    ztr_timeout: float | None,
    ztr_process_timeout: float | None,
    ztr_output_dir: str | None,
) -> ZtrRelayDriver:
    ztr_root = Path(__file__).resolve().parents[2] / "ztr"
    runner_default = ztr_root / "src" / "runner.py"
    implementer_cmd = _setting(ztr_implementer_cmd, "ACP_ZTR_IMPLEMENTER_CMD")
    if not implementer_cmd:
        raise SystemExit("--ztr-implementer-cmd 또는 ACP_ZTR_IMPLEMENTER_CMD 값이 필요합니다")
    # 독립 리뷰 P2: runner.py는 `src` 패키지가 보이는 ztr venv python에서만 동작 —
    # sys.executable(ACP venv) 기본값은 무조건 즉사(DOA). ztr venv를 탐지, 없으면 fail-loud.
    python = _setting(ztr_python, "ACP_ZTR_PYTHON")
    if not python:
        ztr_venv_python = ztr_root / ".venv" / "Scripts" / "python.exe"
        if not ztr_venv_python.exists():
            raise SystemExit(
                "--ztr-python 또는 ACP_ZTR_PYTHON 값이 필요합니다 "
                f"(자동탐지 실패: {ztr_venv_python} 없음 — runner는 ztr venv python 전용)"
            )
        python = str(ztr_venv_python)
    return ZtrRelayDriver(
        python=python,
        runner_script=_setting(ztr_runner, "ACP_ZTR_RUNNER") or runner_default,
        cwd=_setting(ztr_cwd, "ACP_ZTR_CWD") or Path.cwd(),
        implementer_cmd=implementer_cmd,
        reviewer_cmd=_setting(ztr_reviewer_cmd, "ACP_ZTR_REVIEWER_CMD") or "",
        mechanical_cmd=_setting(ztr_mechanical_cmd, "ACP_ZTR_MECHANICAL_CMD") or "",
        test_cmd=_setting(ztr_test_cmd, "ACP_ZTR_TEST_CMD") or "",
        leg_timeout_s=(
            ztr_timeout if ztr_timeout is not None else float(os.environ.get("ACP_ZTR_TIMEOUT", "600"))
        ),
        process_timeout_s=(
            ztr_process_timeout
            if ztr_process_timeout is not None
            else _optional_float_env("ACP_ZTR_PROCESS_TIMEOUT")
        ),
        output_dir=_setting(ztr_output_dir, "ACP_ZTR_OUTPUT_DIR") or ".ztr/acp-relay",
    )


def _setting(value: str | None, env_name: str) -> str | None:
    return value if value is not None else os.environ.get(env_name)


def _optional_float_env(env_name: str) -> float | None:
    raw = os.environ.get(env_name)
    return float(raw) if raw else None


if __name__ == "__main__":
    main()
