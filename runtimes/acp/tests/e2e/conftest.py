"""Playwright e2e fixtures for the ACP dashboard."""
from __future__ import annotations

import os
from pathlib import Path
import socket
import subprocess
import sys
import time
from urllib.request import urlopen

import pytest

playwright_api = pytest.importorskip("playwright.sync_api")


ROOT = Path(__file__).resolve().parents[2]


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@pytest.fixture(scope="session")
def acp_base_url(tmp_path_factory):
    proc, base_url = _start_acp_server(tmp_path_factory, "acp-e2e")
    yield base_url
    _stop_acp_server(proc)


@pytest.fixture(scope="session")
def acp_transition_base_url(tmp_path_factory):
    proc, base_url = _start_acp_server(
        tmp_path_factory,
        "acp-e2e-transition",
        extra_env={
            "ACP_FAKE_TRANSITION_AFTER": "6",
            "ACP_FAKE_TRANSITION_AGE_SECONDS": "1200",
        },
    )
    yield base_url
    _stop_acp_server(proc)


def _start_acp_server(tmp_path_factory, prefix: str, extra_env: dict[str, str] | None = None):
    port = _free_port()
    runtime_dir = tmp_path_factory.mktemp(prefix)
    db_path = runtime_dir / "acp.db"
    events_log = runtime_dir / "events.jsonl"
    env = os.environ.copy()
    env["PYTHONUTF8"] = "1"
    if extra_env:
        env.update(extra_env)
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "acp",
            "web",
            "--fake",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--db-path",
            str(db_path),
            "--events-log",
            str(events_log),
            "--poll-interval",
            "1",
            "--no-toast",
        ],
        cwd=str(ROOT),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    base_url = f"http://127.0.0.1:{port}"
    deadline = time.time() + 20
    last_error: Exception | None = None
    while time.time() < deadline:
        if proc.poll() is not None:
            output = proc.stdout.read() if proc.stdout else ""
            raise RuntimeError(f"ACP e2e server exited early:\n{output}")
        try:
            # 헬스체크는 서버 기본 창 계약을 그대로 쓴다(limit 하드코딩 우회 금지 — LESSON-002).
            with urlopen(f"{base_url}/api/sessions", timeout=1) as response:
                if response.status == 200:
                    break
        except Exception as exc:
            last_error = exc
            time.sleep(0.25)
    else:
        proc.terminate()
        raise RuntimeError(f"ACP e2e server did not start: {last_error}")

    return proc, base_url


def _stop_acp_server(proc: subprocess.Popen[str]) -> None:
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()


@pytest.fixture
def page():
    with playwright_api.sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        try:
            yield page
        finally:
            browser.close()
