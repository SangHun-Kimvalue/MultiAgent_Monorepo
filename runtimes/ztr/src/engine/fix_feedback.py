"""휴먼-게이트 fix-resume 프롬프트 빌더 (ㄱ).

mechanical/reviewer가 CHANGES_REQUESTED/BLOCKED를 내면, 그 findings를 구조화 캡처해
다음 implementer **resume 프롬프트에 합친다**. codex는 결함을 스스로 발견하진 못해도
지시받으면 apply_patch로 고친다(LESSON-031). 이 모듈은 그 "지시"를 만든다.

핵심 불변(위반 금지):
- **사람이 명시 트리거**하는 헬퍼다. relay가 findings를 자동으로 되먹여 재실행하는
  무한 루프가 아니다(R5/휴먼게이트). 이 함수는 프롬프트 텍스트만 만들고, 다음 라운드
  실행은 사람이 결과를 확인한 뒤 별도로 발행한다.
- 코드는 findings의 의미를 판정하지 않는다. 실패한 **gating** leg의 구조화 출력
  (name/status/stdout/stderr)을 불투명 사실로 옮길 뿐이다(R5). non-gating autofix leg는
  애초에 verdict가 없으므로 findings 대상이 아니다.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# fix-resume 프롬프트에 박는 구분 마커. 사람이 검토 시 원본/주입 경계를 명확히 본다.
_INJECTION_HEADER = "## 직전 라운드 리뷰 findings (사람 확인 후 주입 — fix-resume)"


@dataclass(frozen=True)
class Finding:
    """실패한 gating leg 하나의 구조화 findings(불투명 사실)."""

    leg: str
    status: str
    text: str


def load_report_payload(path: Path) -> dict[str, Any]:
    """run-phase 결과 JSON을 읽어 report payload(steps 포함 dict)를 반환한다.

    두 형태를 모두 허용한다(R5 경계상 의미 해석 없이 구조만 본다):
    - run-phase가 stdout으로 찍는 **바깥 Envelope**: `stdout` 필드가 report payload JSON
      문자열. (드라이버가 그대로 파일로 리다이렉트한 경우.)
    - report payload 자체(`steps` 키를 가진 dict).
    """
    raw = path.read_text(encoding="utf-8-sig")
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError("run-phase 결과 JSON 최상위 값은 object여야 합니다")
    if "steps" in data:
        return data
    inner = data.get("stdout")
    if isinstance(inner, str):
        parsed = json.loads(inner)
        if isinstance(parsed, dict) and "steps" in parsed:
            return parsed
    raise ValueError(
        "report payload(steps 키)를 찾지 못했습니다 — run-phase Envelope 또는 "
        "report JSON을 넘기세요"
    )


def _step_findings_text(step: dict[str, Any]) -> str:
    """한 step의 findings 본문을 모은다(stdout 파일 우선, 없으면 preview)."""
    parts: list[str] = []
    stdout_text = _read_capture(step.get("stdout_path")) or step.get("stdout_preview", "")
    if stdout_text.strip():
        parts.append(stdout_text.rstrip())
    stderr_text = _read_capture(step.get("stderr_path")) or step.get("stderr_sanitized", "")
    if stderr_text.strip():
        parts.append("[stderr]\n" + stderr_text.rstrip())
    return "\n\n".join(parts)


def _read_capture(path_value: object) -> str:
    """캡처 파일 경로를 읽는다. 경로가 없거나 못 읽으면 빈 문자열(호출부가 preview로 폴백)."""
    if not isinstance(path_value, str) or not path_value:
        return ""
    path = Path(path_value)
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


def extract_findings(report_payload: dict[str, Any]) -> list[Finding]:
    """report에서 **gating + 미skip + non-PASS** step의 findings를 추출한다.

    non-gating autofix leg는 verdict가 없으므로 제외한다. skip된 leg(앞 leg 실패로 안 돈
    것)는 자체 findings가 없으므로 제외한다.
    """
    findings: list[Finding] = []
    for step in report_payload.get("steps", []):
        if not isinstance(step, dict):
            continue
        if not step.get("gating", True):
            continue
        if step.get("skipped", False):
            continue
        status = step.get("status", "")
        if status == "PASS":
            continue
        findings.append(
            Finding(
                leg=str(step.get("name", "?")),
                status=str(status),
                text=_step_findings_text(step),
            )
        )
    return findings


def build_fix_resume_prompt(original_prompt: str, findings: list[Finding]) -> str:
    """원본 프롬프트 + 직전 라운드 findings를 합친 fix-resume 프롬프트를 만든다.

    findings가 비면 ValueError(고칠 게 없으면 fix-resume 자체가 불필요 — 호출부가 게이트).
    """
    if not findings:
        raise ValueError("non-PASS gating leg findings가 없습니다 — fix-resume 불필요")
    blocks = [
        original_prompt.rstrip(),
        "",
        "---",
        _INJECTION_HEADER,
        "아래는 직전 run-phase에서 mechanical/reviewer가 낸 non-PASS findings다. 이 findings를",
        "apply_patch로 **수정**하라(원본 작업 요구는 그대로 유지). 이 주입은 사람이 명시",
        "트리거했다 — 자동 재시도 루프가 아니다.",
        "",
    ]
    for finding in findings:
        blocks.append(f"### [{finding.leg}] {finding.status}")
        blocks.append(finding.text if finding.text.strip() else "(캡처된 findings 본문 없음)")
        blocks.append("")
    return "\n".join(blocks).rstrip() + "\n"
