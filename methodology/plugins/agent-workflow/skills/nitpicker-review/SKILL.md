---
name: nitpicker-review
description: >
  Codex, Claude Code 또는 다른 CLI 세션에서 코드 변경 후 로컬 Nitpicker wrapper를 실행해 diff를 비판적으로 리뷰한다.
  프로젝트의 `nitpicker/run_nit.py`를 우선 사용하고, Ollama 로컬 LLM 또는 mock provider로 `--changed`,
  `--staged`, 파일 단위 리뷰를 수행한다. raw `git diff`를 shell 인자로 직접 넘기지 않고,
  ALL PASS / CHANGES_REQUESTED / BLOCKED 결과를 정리할 때 사용한다.
---

# Nitpicker Review

이 스킬은 구현 스킬이 아니라 **기계 리뷰 실행 어댑터**다. diff 운반과 provider 호출은 프로젝트의 `nitpicker/run_nit.py`에 맡긴다.

## Rules

- `nitpicker/run_nit.py`가 있으면 반드시 그 wrapper를 우선 사용한다.
- shell에서 raw `git diff` 문자열을 LLM CLI 인자로 넘기지 않는다.
- 기본 provider는 Ollama다. 설치 확인이나 dry run은 `--provider mock`을 쓴다.
- docs-only diff는 사용자가 명시하지 않으면 리뷰 범위를 source/config 중심으로 둔다.
- 결과는 `ALL PASS`, `CHANGES_REQUESTED`, `BLOCKED` 중 하나로 보고한다.

## Commands

Changed files:

```bash
python3 nitpicker/run_nit.py --changed
```

Staged files:

```bash
python3 nitpicker/run_nit.py --staged
```

One or more files:

```bash
python3 nitpicker/run_nit.py path/to/file.cpp path/to/file.py
```

Dry run without LLM:

```bash
python3 nitpicker/run_nit.py --provider mock --changed
```

Install smoke test:

```bash
python3 nitpicker/run_nit.py --self-test
```

## Report

Summarize:

```text
Nitpicker: ALL PASS | CHANGES_REQUESTED | BLOCKED
Provider:
Scope:
Files:
Findings:
Command:
Notes:
```

If the wrapper is missing, report `BLOCKED` and suggest installing the beta package. Do not fabricate a review from memory.
