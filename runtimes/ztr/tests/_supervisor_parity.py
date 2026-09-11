"""`process_supervisor` 두 벌의 **동등성 검사** (supervisor-shared-package-20260821 P1).

왜 복제가 남아 있는가: P0 가 ADR-0001 원문을 확인한 결과, 공용 패키지(O2)의 **AD-3 적합성이
미확정**이고 그 해석은 결정권자 사안이다. 그래서 이번 단계는 **"검증된 동기 복제 유지"(O1)**
를 잠정 선택했다. 근거·재검토 트리거는
`methodology/docs/discovery/supervisor-shared-package-20260821/P0_RESULTS.md`.

⚠ **이 검사가 없으면 O1 은 성립하지 않는다.** Phase 9 는 "같은 결함이 네 곳에 있는데 한 곳만
고쳐져 있던" 상태에서 출발했다 — 주석으로 동기화를 강제하려던 시도가 실패한 실증이다.
"""
from __future__ import annotations

import ast
import pathlib

#: 모노레포 루트임을 나타내는 표식. **전부** 있어야 루트로 인정한다.
MONOREPO_MARKERS = (
    pathlib.Path("docs") / "decisions" / "ADR-0001-kit-to-suite.md",
    pathlib.Path("runtimes") / "README.md",
)

#: 동기 복제 쌍. 저장소 루트 기준 상대경로. **각 쌍의 본문은 항상 같아야 한다.**
#: ⚠ 두 번째 쌍은 **이 검사 헬퍼 자신**이다 — 헬퍼도 두 벌 복제이므로(런타임 간 import 불가)
#: 그것이 갈라지면 검사 자체가 조용히 달라진다. 자기 자신을 검사 대상에 넣어 막는다.
SYNCED_PAIRS = (
    (
        pathlib.Path("runtimes") / "ztr" / "src" / "engine" / "process_supervisor.py",
        pathlib.Path("runtimes") / "acp" / "acp" / "process_supervisor.py",
    ),
    (
        pathlib.Path("runtimes") / "ztr" / "tests" / "_supervisor_parity.py",
        pathlib.Path("runtimes") / "acp" / "tests" / "_supervisor_parity.py",
    ),
    # ⚠ **게이트 진입점 자신**(출력 R1 P1). 한쪽 진입점을 지우거나 이름을 바꾸면
    # 그 런타임 스위트에서 게이트가 **통째로 사라지는데** 아무도 못 잡는다.
    (
        pathlib.Path("runtimes") / "ztr" / "tests" / "test_supervisor_parity.py",
        pathlib.Path("runtimes") / "acp" / "tests" / "test_supervisor_parity.py",
    ),
)


def find_monorepo_root(start: pathlib.Path) -> pathlib.Path | None:
    """모노레포 루트를 찾는다. **표식이 전부 있는 조상**만 인정한다."""
    for candidate in (start, *start.parents):
        if all((candidate / marker).is_file() for marker in MONOREPO_MARKERS):
            return candidate
    return None


def module_body(path: pathlib.Path) -> str:
    """**모듈 docstring 구간만** 빼고 반환한다. docstring 이 없으면 파일 전체.

    각 사본의 헤더 docstring 은 자기 맥락을 적으므로 의도적으로 다르다. 그 외에는
    shebang·인코딩 선언·선행 주석까지 **전부 비교 대상**이다 — docstring 앞을 통째로
    버리면 한쪽에만 shebang 을 넣어도 같다고 판정된다(출력 R2).

    판정은 `ast` 로 한다. 문자열 검색은 docstring 부재·`'''`·중첩 따옴표에서 틀린다.
    파싱 실패는 예외로 두어 FAIL 이 되게 한다.
    """
    text = path.read_text(encoding="utf-8")
    tree = ast.parse(text)
    first = tree.body[0] if tree.body else None
    if not (
        isinstance(first, ast.Expr)
        and isinstance(first.value, ast.Constant)
        and isinstance(first.value.value, str)
        and first.end_lineno is not None
    ):
        return text
    # docstring 이 줄을 **단독 점유**하지 않으면(`\"\"\"doc\"\"\"; X = 1`) 그 줄을 통째로
    # 지우면서 뒤따르는 코드까지 비교에서 사라진다. 판정 불가이므로 fail-closed 로 닫는다.
    following = tree.body[1] if len(tree.body) > 1 else None
    if following is not None and following.lineno <= first.end_lineno:
        raise ValueError(
            f"{path}: 모듈 docstring 과 같은 줄에 코드가 있어 본문 경계를 판정할 수 없다"
        )
    lines = text.splitlines()
    return chr(10).join(lines[: first.lineno - 1] + lines[first.end_lineno :])


def assert_parity(caller_file: str) -> None:
    """동등성을 강제한다. **fail-closed — 판정할 수 없으면 실패한다.**

    skip 분기를 두지 않는다: 탐색이 깨지면 조용히 skip 되고, 헬퍼도 복제라 같은 버그가
    양쪽에 동시에 들어가면 검사가 전멸한다(mutation 실증). 단독 배포 요구는 확정된 바
    없으므로(ADR 에 split 요구 없음) split 이 실제로 생기면 **이 실패가 재검토 신호**다.
    """
    root = find_monorepo_root(pathlib.Path(caller_file).resolve().parent)
    assert root is not None, (
        "모노레포 루트를 찾지 못했다. 표식: "
        f"{[str(m) for m in MONOREPO_MARKERS]}. "
        "단독 배포로 분리됐다면 이 실패가 재검토 신호다 — skip 으로 덮지 마라."
    )

    every = [rel for pair in SYNCED_PAIRS for rel in pair]
    missing = [str(rel) for rel in every if not (root / rel).is_file()]
    assert not missing, f"모노레포인데 동기 복제 대상이 없다: {missing} (root={root})"

    for first, second in SYNCED_PAIRS:
        assert module_body(root / first) == module_body(root / second), (
            "동기 복제 쌍이 갈라졌다: "
            f"{first} vs {second}. "
            "한쪽만 고쳤다면 다른 쪽도 고쳐라. 이 복제는 O1(검증된 동기 복제 유지)"
            " 결정의 결과이며, 이 검사가 그 결정의 유일한 강제 수단이다."
        )
