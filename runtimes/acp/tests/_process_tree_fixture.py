"""프로세스 트리 정리 테스트용 공통 fixture (Phase 9 P1).

**관측 oracle 은 PID 가 아니라 고유 마커 파일**이다. PID 는 재사용되므로 PID 로 생존을
판정하면 관측 자체가 틀린다(P0 실측 근거: `P0_RESULTS.md`).

마커는 **원자적 교체**로 쓴다 — P0 하네스에서 쓰는 중에 읽어 빈 문자열을 받은 사례가 있다.
"""
from __future__ import annotations

import os
import pathlib
import subprocess
import sys
import time

TREE_SCRIPT = r'''
import os, pathlib, subprocess, sys, time

def beat(marker: pathlib.Path) -> None:
    tmp = marker.with_suffix(".tmp")
    tmp.write_text(str(os.getpid()), encoding="utf-8")
    os.replace(tmp, marker)          # 원자적 교체 — 읽는 쪽이 빈 파일을 보지 않는다

marker_dir, tag, mode = sys.argv[1], sys.argv[2], sys.argv[3]
marker = pathlib.Path(marker_dir) / (tag + ".alive")
beat(marker)
print("UP " + tag, flush=True)

if mode in ("parent", "parent_exit"):
    subprocess.Popen(
        [sys.executable, "-c", sys.argv[4], marker_dir, tag + ".gc", "leaf", ""],
        stdout=None, stderr=None,    # 부모 파이프를 상속 -> drain 이 EOF 를 못 받는다
    )
    if mode == "parent_exit":
        time.sleep(0.3)
        raise SystemExit(0)          # 중간 부모 선종료 — P0 에서 taskkill 이 진 시나리오

end = time.monotonic() + 120
while time.monotonic() < end:
    beat(marker)
    time.sleep(0.2)
'''


def tree_command(marker_dir: pathlib.Path, tag: str = "root", mode: str = "parent") -> list[str]:
    """손자를 낳고 파이프를 물려주는 자식 프로세스의 argv."""
    return [sys.executable, "-c", TREE_SCRIPT, str(marker_dir), tag, mode, TREE_SCRIPT]


def alive_tags(marker_dir: pathlib.Path, window: float = 1.5) -> list[str]:
    """`window` 초 안에 갱신된 마커만 살아 있다고 본다. PID 를 보지 않는다."""
    now = time.time()
    found = []
    for path in pathlib.Path(marker_dir).glob("*.alive"):
        try:
            if now - path.stat().st_mtime <= window:
                found.append(path.stem)
        except OSError:
            pass
    return sorted(found)


def survivors_after(marker_dir: pathlib.Path, settle: float = 1.8) -> list[str]:
    """정리 호출 뒤 안정화 시간을 두고 생존 태그를 관측한다."""
    time.sleep(settle)
    return alive_tags(marker_dir)


def reap(marker_dir: pathlib.Path) -> list[str]:
    """**최종 회수 경로** — 테스트가 고아를 남기지 않게 강제 정리한다.

    테스트가 결함을 재현하면 반드시 고아가 생긴다. 회수하지 않으면 테스트 실행 자체가
    머신에 프로세스를 쌓는다.
    """
    leftover = alive_tags(marker_dir)
    for tag in leftover:
        try:
            pid = (pathlib.Path(marker_dir) / (tag + ".alive")).read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if not pid.isdigit():
            continue
        if os.name == "nt":
            subprocess.run(["taskkill", "/T", "/F", "/PID", pid], capture_output=True)
        else:
            try:
                os.kill(int(pid), 9)
            except OSError:
                pass
    return leftover
