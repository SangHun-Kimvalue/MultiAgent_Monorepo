@echo off
chcp 65001 >nul
setlocal enabledelayedexpansion
rem ===========================================================================
rem  ZRT v2 — Mechanical 검사 런타임 사용 런처 (Windows)
rem  판단은 LLM 세션이, 기계 검사는 ztr가. stdout = 단일 Envelope JSON.
rem  사용법: ztr.bat            (대화형 메뉴)
rem          ztr.bat review --changed   (명령 직접 실행)
rem ===========================================================================

set "ROOT=%~dp0"
set "PY=%ROOT%.venv\Scripts\python.exe"

if not exist "%PY%" goto noenv
cd /d "%ROOT%"

if "%~1"=="" goto menu

rem --- 인자 직접 실행 모드 (자동화/스크립트용) ---
"%PY%" -m src %*
set "RC=!errorlevel!"
echo.
echo [exit code] !RC!   (0=PASS 1=CHANGES_REQUESTED 2=BLOCKED 124=timeout 70=internal)
exit /b !RC!

:noenv
echo [!] venv를 찾을 수 없습니다: %PY%
echo     먼저 아래를 실행하세요 (python 3.12 기준 — mypy 타깃과 일치):
echo         python -m venv .venv
echo         .venv\Scripts\python.exe -m pip install -e .[dev]
echo.
pause
exit /b 1

:menu
cls
echo ===========================================================================
echo   ZRT v2  —  Mechanical 검사 런타임
echo ===========================================================================
echo   [1] review      변경 파일 정적 리뷰   (ruff + mypy + ollama 보조)
echo   [2] invariants  MM/ZRT 규약 사실 검사
echo   [3] verify      사후 검증 (--post-merge --changed)
echo   [4] gate        result JSON 품질 게이트 (파일 경로 입력)
echo   [5] run-phase   헤드리스 CLI 릴레이 (프롬프트 파일 입력)
echo   [6] health      에이전트 헬스체크 (nitpicker / ollama)
echo   ---------------------------------------------------------------------
echo   [7] 대시보드(UI)   http://127.0.0.1:8000   (검증 이력 관측)
echo   [8] list-agents   설정된 에이전트 목록
echo   [9] 전체 게이트   ruff + mypy + pytest
echo   ---------------------------------------------------------------------
echo   [0] 종료
echo ===========================================================================
set "CHOICE="
set /p "CHOICE=선택> "

if "%CHOICE%"=="1" goto do_review
if "%CHOICE%"=="2" goto do_invariants
if "%CHOICE%"=="3" goto do_verify
if "%CHOICE%"=="4" goto do_gate
if "%CHOICE%"=="5" goto do_runphase
if "%CHOICE%"=="6" goto do_health
if "%CHOICE%"=="7" goto do_web
if "%CHOICE%"=="8" goto do_list
if "%CHOICE%"=="9" goto do_gates
if "%CHOICE%"=="0" exit /b 0
echo 잘못된 선택입니다.
timeout /t 1 >nul
goto menu

:do_review
"%PY%" -m src review --changed
goto done

:do_invariants
"%PY%" -m src invariants
goto done

:do_verify
"%PY%" -m src verify --post-merge --changed
goto done

:do_gate
set "GF="
set /p "GF=result JSON 경로> "
"%PY%" -m src gate "!GF!"
goto done

:do_runphase
set "PF="
set /p "PF=프롬프트 파일 경로> "
set "CMDLINE="
set /p "CMDLINE=implementer CLI (예: codex exec --cd . --sandbox read-only --ephemeral -)> "
"%PY%" -m src run-phase --prompt-file "!PF!" --implementer-cmd "!CMDLINE!"
goto done

:do_health
"%PY%" -m src health --agent nitpicker-prefilter
"%PY%" -m src health --agent ollama-local
goto done

:do_web
echo 대시보드 기동 중... 브라우저가 열립니다. 종료하려면 이 창에서 Ctrl+C.
start "" http://127.0.0.1:8000
"%PY%" -m src web
goto done

:do_list
"%PY%" -m src list-agents
goto done

:do_gates
echo === ruff ===
"%PY%" -m ruff check src tests
echo === mypy ===
"%PY%" -m mypy src
echo === pytest ===
"%PY%" -m pytest -q
goto done

:done
echo.
echo [exit code] %errorlevel%   (0=PASS 1=CHANGES_REQUESTED 2=BLOCKED 124=timeout 70=internal)
echo.
pause
goto menu
