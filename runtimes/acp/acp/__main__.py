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


USAGE = (
    "사용법: python -m acp web [--fake] [--host HOST] [--port PORT] "
    "[--db-path PATH] [--events-log PATH] [--poll-interval SECONDS] "
    "[--orch-events-dir PATH] [--orch-driver mock|claude-cli|codex-cli|ztr-relay] "
    "[--ztr-python PATH] [--ztr-runner PATH] [--ztr-cwd PATH] "
    "[--ztr-implementer-cmd CMD] [--ztr-reviewer-cmd CMD] "
    "[--ztr-mechanical-cmd CMD] [--ztr-test-cmd CMD] [--ztr-timeout SECONDS] "
    "[--ztr-process-timeout SECONDS] [--ztr-output-dir PATH] [--no-toast]"
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
        poller.register(ClaudeSessionCollector(claude_sessions))
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
        poller_task.cancel()
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
    else:
        print(
            "알 수 없는 명령: "
            f"{args[0]}. {USAGE}",
            file=sys.stderr,
        )
        sys.exit(1)


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
