# codex 픽스처 — **합성 데이터**

이 디렉터리의 `rollout-*.jsonl`은 수집기 경로를 돌리기 위해 **손으로 만든 합성 파일**이다.
실제 codex 로그가 아니며, 여기 등장하는 event type이 실제 어휘라는 근거가 되지 못한다.

> 과거에 이 구분이 없어서 결함이 생겼다: 픽스처가 `task_aborted`·`exec_approval_request`
> 같은 **실제로는 관측되지 않는 어휘**를 담고 있었고, 그 픽스처로 초록이 뜨는 동안
> 실데이터의 `turn_aborted`는 어느 분기에도 닿지 않았다. 테스트가 갭을 가린 것이다
> (LESSON-002 rule 7).

**실측 어휘의 단일 출처**는 `tests/fixtures/vocab/codex_event_types.json`이며,
`tests/vocab_extract.py`가 실제 rollout 로그에서 결정론적으로 추출한다.
분기 상수와 어휘의 관계는 `tests/test_event_vocabulary.py`가 검사한다.

따라서:

- 이 디렉터리의 파일은 **어휘 계약 테스트의 대상이 아니다**(합성이므로).
- 새 픽스처를 만들 때 실어휘를 흉내 내되, 그것을 근거로 분기를 설계하지 말 것.
