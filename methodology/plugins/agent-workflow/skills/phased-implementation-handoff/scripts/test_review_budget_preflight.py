from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import threading
from pathlib import Path

import pytest

SKILL_ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location(
    "review_budget_preflight", SKILL_ROOT / "scripts" / "review_budget_preflight.py"
)
assert _spec and _spec.loader
preflight = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(preflight)


def _doc(tmp_path: Path, *, grade: str = "L2", consumed: int = 0, doc_consumed: int = 0,
         slice_id: str = "slice-a", blocks: int = 1) -> Path:
    block = (
        "```review-budget\n"
        f"slice_id: {slice_id}\n"
        f"work_grade: {grade}\n"
        f"rounds_consumed: {consumed}\n"
        f"doc_rounds_consumed: {doc_consumed}\n"
        "```\n"
    )
    doc = tmp_path / "PLAN.md"
    doc.write_text("# 진행 문서\n\n" + block * blocks + "\n본문\n", encoding="utf-8")
    return doc


def _run(doc: Path, *extra: str) -> tuple[int, dict]:
    argv = ["--doc", str(doc), *extra]
    from io import StringIO

    buffer, original = StringIO(), sys.stdout
    sys.stdout = buffer
    try:
        code = preflight.main(argv)
    finally:
        sys.stdout = original
    return code, json.loads(buffer.getvalue().strip().splitlines()[-1])


# --- R2-1 : 예산 소진 시 Reviewer 가 호출되지 않는다 -------------------------


def test_exhausted_does_not_launch_reviewer(tmp_path: Path) -> None:
    """4/4 상태에서 stub Reviewer 호출 횟수 = 0."""
    marker = tmp_path / "reviewer_ran.txt"
    doc = _doc(tmp_path, grade="L2", consumed=4)
    code, payload = _run(
        doc, "--", sys.executable, "-c", f"open(r'{marker}','w').write('ran')"
    )
    assert code == preflight.EXIT_EXHAUSTED
    assert payload["status"] == "BLOCKED"
    assert "review budget exhausted" in payload["reason"]
    assert not marker.exists(), "예산 소진인데 Reviewer 가 실행됐다"


def test_boundary_round_launches(tmp_path: Path) -> None:
    """3/4 → next_round=4 는 허용된다(경계는 통과)."""
    marker = tmp_path / "ran.txt"
    doc = _doc(tmp_path, grade="L2", consumed=3)
    code, payload = _run(doc, "--", sys.executable, "-c", f"open(r'{marker}','w').write('x')")
    assert code == 0 and payload["status"] == "LAUNCHED"
    assert payload["rounds_consumed"] == 4
    assert marker.exists()


def test_reserve_persists_increment(tmp_path: Path) -> None:
    doc = _doc(tmp_path, consumed=1)
    code, payload = _run(doc)
    assert code == 0 and payload["reserved_round"] == 2
    assert "rounds_consumed: 2" in doc.read_text(encoding="utf-8")


# --- R2-2 : 동시 호출에서 카운터가 유실되지 않는다 --------------------------


def test_concurrent_reserve_is_serialized(tmp_path: Path) -> None:
    """3/4 에서 두 스레드가 동시에 예약해도 성공은 정확히 1회."""
    doc = _doc(tmp_path, grade="L2", consumed=3)
    barrier = threading.Barrier(2)
    results: list[int] = []
    lock = threading.Lock()

    def worker() -> None:
        barrier.wait()
        try:
            preflight.reserve(doc, None, timeout_s=5.0)
            outcome = 0
        except preflight.ContractError as exc:
            outcome = exc.code
        with lock:
            results.append(outcome)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sorted(results) == [0, preflight.EXIT_EXHAUSTED], results
    assert "rounds_consumed: 4" in doc.read_text(encoding="utf-8")


# --- R2-4 : 레코드 형식 fail-closed ----------------------------------------


def test_duplicate_block_is_rejected(tmp_path: Path) -> None:
    doc = _doc(tmp_path, blocks=2)
    code, payload = _run(doc)
    assert code == preflight.EXIT_CONTRACT
    assert "exactly one review-budget block" in payload["reason"]


def test_missing_block_is_rejected(tmp_path: Path) -> None:
    doc = tmp_path / "PLAN.md"
    doc.write_text("# 진행 문서\n블록 없음\n", encoding="utf-8")
    code, payload = _run(doc)
    assert code == preflight.EXIT_CONTRACT
    assert "found 0" in payload["reason"]


def test_slice_id_mismatch_is_rejected(tmp_path: Path) -> None:
    """slice 재발급으로 예산을 리셋하려는 우회를 막는다."""
    doc = _doc(tmp_path, slice_id="slice-a", consumed=4)
    code, payload = _run(doc, "--slice", "slice-b")
    assert code == preflight.EXIT_CONTRACT
    assert "slice_id mismatch" in payload["reason"]


def test_unknown_work_grade_is_rejected(tmp_path: Path) -> None:
    doc = _doc(tmp_path, grade="L9")
    code, payload = _run(doc)
    assert code == preflight.EXIT_CONTRACT
    assert "unknown work_grade" in payload["reason"]


def test_budget_limit_is_derived_not_read(tmp_path: Path) -> None:
    """문서에 budget_limit 을 신고해도 무시하고 등급에서 파생한다(D2)."""
    doc = tmp_path / "PLAN.md"
    doc.write_text(
        "```review-budget\nslice_id: s\nwork_grade: L1\nbudget_limit: 99\n"
        "rounds_consumed: 3\n```\n",
        encoding="utf-8",
    )
    code, payload = _run(doc)
    assert code == preflight.EXIT_EXHAUSTED
    assert "budget_limit=3" in payload["reason"]


# --- R2-6 : 저장 실패 시 호출 금지 ------------------------------------------


def test_persist_failure_blocks_launch(tmp_path: Path, monkeypatch) -> None:
    marker = tmp_path / "ran.txt"
    doc = _doc(tmp_path, consumed=0)

    def boom(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(preflight, "persist", boom)
    code, payload = _run(doc, "--", sys.executable, "-c", f"open(r'{marker}','w').write('x')")
    assert code == preflight.EXIT_PERSIST
    assert not marker.exists(), "저장 실패인데 Reviewer 가 실행됐다"


def test_launch_failure_keeps_consumption(tmp_path: Path) -> None:
    """저장 후 launch 실패는 소비를 유지한 채 Human Gate 로 돌린다."""
    doc = _doc(tmp_path, consumed=0)
    code, payload = _run(doc, "--", str(tmp_path / "does-not-exist.exe"))
    assert code == preflight.EXIT_LAUNCH
    assert payload["round_consumed_kept"] is True
    assert "rounds_consumed: 1" in doc.read_text(encoding="utf-8")


# --- R1-P2-1 : 잠금 해제 실패를 삼키지 않는다 -------------------------------


def test_unlock_failure_blocks_launch_and_keeps_consumption(tmp_path: Path, monkeypatch) -> None:
    """해제 실패 시 성공을 반환하지 않는다 — 소비는 유지, Reviewer 는 호출 금지."""
    marker = tmp_path / "ran.txt"
    doc = _doc(tmp_path, consumed=0)

    real_unlink = preflight.os.unlink

    def flaky_unlink(path, *args, **kwargs):
        if str(path).endswith(".lock"):
            raise OSError("lock file is held by another handle")
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(preflight.os, "unlink", flaky_unlink)
    code, payload = _run(doc, "--", sys.executable, "-c", f"open(r'{marker}','w').write('x')")

    assert code == preflight.EXIT_UNLOCK
    assert payload["status"] == "BLOCKED"
    assert payload["round_consumed_kept"] is True
    assert not marker.exists(), "잠금 해제 실패인데 Reviewer 가 실행됐다"
    assert "rounds_consumed: 1" in doc.read_text(encoding="utf-8"), "소비가 되돌려졌다"


# --- R1-P2-2 : persist 는 두 번째 스냅샷을 완전히 재파싱한다 ----------------


@pytest.mark.parametrize(
    "field, doc_kwargs",
    [
        ("slice_id", {"slice_id": "slice-b"}),
        ("work_grade", {"grade": "L1"}),
        ("rounds_consumed", {"consumed": 2}),
    ],
)
def test_persist_rejects_changed_document(tmp_path: Path, field: str, doc_kwargs: dict) -> None:
    """read_state 이후 문서가 바뀌면 저장하지 않는다.

    **필드를 하나씩만** 틀리게 한다(R2-P2). 세 필드를 동시에 틀리게 하면
    비교식에서 한 필드를 빼는 mutation 이 살아남는다.
    """
    base = {"slice_id": "slice-a", "grade": "L2", "consumed": 0}
    doc = _doc(tmp_path, **{**base, **doc_kwargs})
    before = doc.read_text(encoding="utf-8")

    # reserve 는 (slice-a, L2, 0) 을 보고 들어왔다
    try:
        preflight.persist(doc, "slice-a", "L2", 0, 1)
    except preflight.ContractError as exc:
        assert "document changed between reserve and persist" in str(exc), field
    else:
        raise AssertionError(f"{field} 가 바뀐 문서인데 저장이 통과했다")

    assert doc.read_text(encoding="utf-8") == before, "계약 위반인데 문서가 수정됐다"


def test_persist_rejects_zero_substitution(tmp_path: Path) -> None:
    """`int()` 는 통과하지만 치환 정규식에는 안 맞는 값 — 저장 0건을 성공으로 넘기지 않는다.

    `+1` 은 `int('+1') == 1` 이라 3튜플 비교를 통과하지만 `\\d+` 에는 걸리지 않는다.
    `replaced != 1` 검사가 없으면 **증가하지 않은 문서로 Reviewer 를 띄우게 된다**(R2-P2).
    """
    doc = tmp_path / "PLAN.md"
    doc.write_text(
        "```review-budget\nslice_id: slice-a\nwork_grade: L2\nrounds_consumed: +1\n```\n",
        encoding="utf-8",
    )
    before = doc.read_text(encoding="utf-8")

    try:
        preflight.persist(doc, "slice-a", "L2", 1, 2)
    except preflight.ContractError as exc:
        assert "replaced 0" in str(exc)
    else:
        raise AssertionError("치환 0건인데 저장이 성공으로 처리됐다")

    assert doc.read_text(encoding="utf-8") == before


def test_persist_accepts_matching_snapshot(tmp_path: Path) -> None:
    doc = _doc(tmp_path, slice_id="slice-a", grade="L2", consumed=1)
    preflight.persist(doc, "slice-a", "L2", 1, 2)
    assert "rounds_consumed: 2" in doc.read_text(encoding="utf-8")


# --- R1-P2-4 : 닫는 fence 에 잔여물이 있으면 종결로 인정하지 않는다 ---------


def test_malformed_closing_fence_is_rejected(tmp_path: Path) -> None:
    """```junk 를 종결로 오인하면 '블록 1개' 검사가 무력해진다."""
    doc = tmp_path / "PLAN.md"
    doc.write_text(
        "```review-budget\nslice_id: s\nwork_grade: L2\nrounds_consumed: 0\n```junk\n",
        encoding="utf-8",
    )
    code, payload = _run(doc)
    assert code == preflight.EXIT_CONTRACT
    assert "found 0" in payload["reason"]


# --- A8 : 문서 축(input gate) 집행 ------------------------------------------


def test_input_gate_exhausted_does_not_launch_reviewer(tmp_path: Path) -> None:
    """L2 문서 축은 3회다. 3/3 에서 Reviewer 호출 횟수 = 0."""
    marker = tmp_path / "ran.txt"
    doc = _doc(tmp_path, grade="L2", doc_consumed=3)
    code, payload = _run(
        doc, "--gate", "input", "--", sys.executable, "-c", f"open(r'{marker}','w').write('x')"
    )
    assert code == preflight.EXIT_EXHAUSTED
    assert payload["gate"] == "input"
    assert "budget_limit=3" in payload["reason"]
    assert not marker.exists(), "문서 축 소진인데 Reviewer 가 실행됐다"


def test_input_gate_budget_is_smaller_than_output(tmp_path: Path) -> None:
    """같은 L2 문서에서 문서 축은 3, 구현 diff 축은 4."""
    doc = _doc(tmp_path, grade="L2", consumed=3, doc_consumed=2)
    code_in, pay_in = _run(doc, "--gate", "input")
    code_out, pay_out = _run(doc, "--gate", "output")
    assert (code_in, pay_in["budget_limit"]) == (0, 3)
    assert (code_out, pay_out["budget_limit"]) == (0, 4)


@pytest.mark.parametrize(
    "gate, bumped, untouched",
    [
        ("input", "doc_rounds_consumed", "rounds_consumed"),
        ("output", "rounds_consumed", "doc_rounds_consumed"),
    ],
)
def test_axes_are_isolated(tmp_path: Path, gate: str, bumped: str, untouched: str) -> None:
    """한 축을 소비해도 다른 축 카운터는 변하지 않는다(잔여를 빌려올 수 없다).

    `rounds_consumed` 는 `doc_rounds_consumed` 의 부분문자열이라 앵커가 없으면 오염된다.
    """
    doc = _doc(tmp_path, grade="L2", consumed=1, doc_consumed=1)
    code, _ = _run(doc, "--gate", gate)
    text = doc.read_text(encoding="utf-8")
    assert code == 0
    assert f"{bumped}: 2" in text, f"{gate} 축이 증가하지 않았다"
    assert f"{untouched}: 1" in text, f"{gate} 축 예약이 반대 축 카운터를 건드렸다"


def test_missing_doc_counter_is_fail_closed(tmp_path: Path) -> None:
    """카운터 줄을 지워 예산을 리셋하는 우회를 막는다."""
    doc = tmp_path / "PLAN.md"
    doc.write_text(
        "```review-budget\nslice_id: s\nwork_grade: L2\nrounds_consumed: 0\n```\n",
        encoding="utf-8",
    )
    code, payload = _run(doc, "--gate", "input")
    assert code == preflight.EXIT_CONTRACT
    assert "doc_rounds_consumed" in payload["reason"]
    assert "must not silently reset" in payload["reason"]


def test_default_gate_is_output(tmp_path: Path) -> None:
    """--gate 미지정 시 기존 동작(구현 diff 축)을 유지한다."""
    doc = _doc(tmp_path, grade="L2", consumed=0, doc_consumed=0)
    code, payload = _run(doc)
    assert code == 0 and payload["gate"] == "output"
    assert payload["budget_limit"] == 4


@pytest.mark.parametrize(
    "gate, grade, limit",
    [
        ("input", "L0", 1), ("input", "L1", 2), ("input", "L2", 3),
        ("output", "L0", 2), ("output", "L1", 3), ("output", "L2", 4),
    ],
)
def test_gate_grade_derives_budget(tmp_path: Path, gate: str, grade: str, limit: int) -> None:
    """등급에서 파생하며 문서 신고값을 읽지 않는다. **등급 사다리 전량**을 건다(R3-P2-3).

    한 등급만 걸면 그 등급 외의 예산값 mutation 이 살아남는다.
    """
    doc = _doc(tmp_path, grade=grade, consumed=limit, doc_consumed=limit)
    code, payload = _run(doc, "--gate", gate)
    assert code == preflight.EXIT_EXHAUSTED
    assert f"budget_limit={limit}" in payload["reason"], payload["reason"]


def test_legacy_document_still_works_on_output_gate(tmp_path: Path) -> None:
    """`doc_rounds_consumed` 가 **없는 기존 문서**도 기본(output) 축에서는 그대로 동작한다.

    두 카운터를 항상 요구하도록 만드는 mutation 을 잡는다(R3-P2-1).
    """
    doc = tmp_path / "PLAN.md"
    doc.write_text(
        "```review-budget\nslice_id: legacy\nwork_grade: L2\nrounds_consumed: 1\n```\n",
        encoding="utf-8",
    )
    code, payload = _run(doc)
    assert code == 0, payload
    assert payload["gate"] == "output"
    assert payload["rounds_consumed"] == 2
    assert "rounds_consumed: 2" in doc.read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "gate, present, absent",
    [
        ("input", "doc_rounds_consumed", "rounds_consumed"),
        ("output", "rounds_consumed", "doc_rounds_consumed"),
    ],
)
def test_result_json_uses_gate_specific_counter_key(
    tmp_path: Path, gate: str, present: str, absent: str
) -> None:
    """성공 JSON 의 카운터 키는 **축을 따른다**(R3-P2-2).

    고정 키로 되돌리는 mutation 은 상태 파일·한도·exit code 만 보는 테스트로는 안 잡힌다.
    """
    doc = _doc(tmp_path, grade="L2", consumed=0, doc_consumed=0)
    code, payload = _run(doc, "--gate", gate)
    assert code == 0
    assert payload[present] == 1
    assert absent not in payload, f"{gate} 축 응답에 반대 축 키가 들어 있다"


def test_unknown_gate_is_rejected(tmp_path: Path) -> None:
    doc = _doc(tmp_path)
    try:
        preflight.reserve(doc, None, timeout_s=1.0, gate="sideways")
    except preflight.ContractError as exc:
        assert "unknown gate" in str(exc)
    else:
        raise AssertionError("알 수 없는 축인데 예약이 통과했다")


# --- 실행 실측 발견 : 인코딩 때문에 exit code 계약이 깨지면 안 된다 ------------


def test_non_utf8_stdout_preserves_exit_code(tmp_path: Path, monkeypatch) -> None:
    """cp949 같은 콘솔에서 비ASCII 출력이 죽으면 정의된 exit code 대신 1 이 나간다.

    호출자는 exit code 로만 분기하므로(R5) 그 순간 계약이 깨진다.
    실행 실측에서 발견됐다 — StringIO 로 stdout 을 잡는 기존 테스트는 이걸 못 잡는다.
    """
    doc = _doc(tmp_path, grade="L2", doc_consumed=3)

    class Cp949Stdout:
        """비ASCII 를 만나면 실제 cp949 콘솔처럼 예외를 던진다."""

        def __init__(self) -> None:
            self.chunks: list[str] = []

        def write(self, text: str) -> int:
            text.encode("cp949")  # 비ASCII 면 UnicodeEncodeError
            self.chunks.append(text)
            return len(text)

        def flush(self) -> None:
            pass

    fake = Cp949Stdout()
    monkeypatch.setattr(sys, "stdout", fake)
    code = preflight.main(["--doc", str(doc), "--gate", "input"])

    assert code == preflight.EXIT_EXHAUSTED, "인코딩 실패가 exit code 를 덮어썼다"
    payload = json.loads("".join(fake.chunks).strip())
    assert payload["status"] == "BLOCKED"
    assert payload["gate"] == "input"


def test_emit_falls_back_to_ascii(monkeypatch, capsys) -> None:
    """emit 은 비ASCII 출력이 불가능해도 파싱 가능한 JSON 을 낸다."""
    class Failing:
        def __init__(self) -> None:
            self.out: list[str] = []

        def write(self, text: str) -> int:
            text.encode("ascii")
            self.out.append(text)
            return len(text)

        def flush(self) -> None:
            pass

    fake = Failing()
    monkeypatch.setattr(sys, "stdout", fake)
    preflight.emit({"reason": "예산 소진 — four-way disposition"})
    payload = json.loads("".join(fake.out).strip())
    assert payload["reason"] == "예산 소진 — four-way disposition"
