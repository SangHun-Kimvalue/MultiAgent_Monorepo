"""OutputQualityGate - LLM 출력 품질 검증기.

Critic과 Writer의 출력이 실질적으로 유의미한지 자동 검증한다.
"겉만 PASS"인 리뷰와 "빈 코드" Writer를 잡아낸다.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass

logger = logging.getLogger(__name__)

# "잘 구현됨", "잘 작성됨", "모범적" 등 칭찬 패턴
_PRAISE_PATTERNS = [
    r"잘\s*구현",
    r"잘\s*작성",
    r"모범적",
    r"훌륭",
    r"완벽",
    r"우수",
    r"적절히\s*활용",
    r"잘\s*준수",
    r"well\s*implemented",
    r"well\s*written",
    r"excellent",
    r"perfect",
]

_PRAISE_RE = re.compile("|".join(_PRAISE_PATTERNS), re.IGNORECASE)

# 빈 함수 패턴: "def foo(): pass" 또는 "def foo(): ..."
_EMPTY_FUNC_RE = re.compile(
    r"def\s+\w+\s*\([^)]*\)[^:]*:\s*(?:pass|\.\.\.)\s*$",
    re.MULTILINE,
)


@dataclass
class QualityResult:
    """품질 검증 결과."""
    passed: bool
    issues: list[str]
    retry_requested: bool = False  # True면 LLM 재호출 권장


class OutputQualityGate:
    """LLM 출력 품질 검증기."""

    def validate_critic(
        self,
        findings: list[dict[str, str]],
        verdict: str,
    ) -> QualityResult:
        """Critic 출력 품질 검증.

        검증 항목:
            1. findings 중 칭찬만 있으면 → 실질 리뷰 아님
            2. PASS인데 major가 있으면 → verdict 불일치
            3. findings가 0건이면 → 리뷰 부실
        """
        issues: list[str] = []

        # 1. 칭찬만 있는 findings 감지
        if findings:
            praise_count = 0
            for f in findings:
                msg = f.get("message", "")
                rec = f.get("recommendation", "")
                combined = f"{msg} {rec}"
                if _PRAISE_RE.search(combined) and f.get("severity") == "minor":
                    praise_count += 1

            if praise_count == len(findings):
                issues.append(
                    f"findings {len(findings)}건 전부 칭찬 (실질 리뷰 아님)"
                )

        # 2. verdict-findings 불일치
        if verdict == "pass":
            majors = [f for f in findings if f.get("severity") == "major"]
            blockers = [f for f in findings if f.get("severity") == "blocker"]
            if blockers:
                issues.append(
                    f"PASS인데 blocker {len(blockers)}건 (verdict 불일치)"
                )
            if majors:
                issues.append(
                    f"PASS인데 major {len(majors)}건 (verdict 불일치)"
                )

        # 3. findings 0건
        if len(findings) == 0 and verdict in ("pass", "conditional"):
            issues.append("findings 0건 (리뷰 부실)")

        retry = any("칭찬" in i or "부실" in i for i in issues)
        return QualityResult(
            passed=len(issues) == 0,
            issues=issues,
            retry_requested=retry,
        )

    def validate_writer(
        self,
        code: str,
        task: str,
    ) -> QualityResult:
        """Writer 출력 품질 검증.

        검증 항목:
            1. 코드가 비어있거나 너무 짧으면 → 생성 실패
            2. 빈 함수(pass만)만 있으면 → 미구현
            3. import만 있고 로직이 없으면 → 미구현
        """
        issues: list[str] = []
        stripped = code.strip()

        # 1. 너무 짧은 코드
        if len(stripped) < 20:
            issues.append(f"코드가 너무 짧음 ({len(stripped)} chars)")

        # 2. 빈 함수만 있는지
        if stripped:
            funcs = re.findall(r"def\s+\w+", stripped)
            empty_funcs = _EMPTY_FUNC_RE.findall(stripped)
            if funcs and len(empty_funcs) == len(funcs):
                issues.append(
                    f"모든 함수가 빈 구현 (pass/...) — {len(funcs)}개"
                )

        # 3. import만 있고 로직 없음
        if stripped:
            lines = [line.strip() for line in stripped.splitlines() if line.strip()]
            non_import = [
                line for line in lines
                if not line.startswith(("import ", "from ", "#", '"""', "'''"))
            ]
            if len(lines) > 0 and len(non_import) == 0:
                issues.append("import/주석만 있고 로직 없음")

        return QualityResult(
            passed=len(issues) == 0,
            issues=issues,
            retry_requested=len(issues) > 0,
        )
