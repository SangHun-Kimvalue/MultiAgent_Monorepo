# Nitpicker Wrapper

This folder is copied into target projects by `install.sh`. Use `run_nit.py` as
the only direct entrypoint; never pass raw Git diff text through shell arguments.

## Basic Usage

```bash
ollama list
ollama pull qwen3:8b
python nitpicker/run_nit.py --self-test
python nitpicker/run_nit.py --changed
python nitpicker/run_nit.py --provider mock --changed
```

`--evidence-file` and positional review targets accept only pure repo-relative
paths. The path portion of `--file-timeout PATH=SECONDS` uses the same validator.
Drive-relative (`C:foo`), rooted (`\foo`, `/foo`), drive-absolute, UNC, and paths
that resolve outside `--repo` are blocked before the provider is called. Aliases
inside the repo are resolved to one canonical POSIX path, then deduplicated and
sorted.

Evidence is read once as raw bytes, hashed with SHA-256, decoded as strict UTF-8,
and serialized as compact JSON without newline normalization. The default
serialized bundle budget is 30000 characters. Increase it explicitly when a
review needs a larger approved evidence set; evidence is never truncated.

Each target emits one `Nitpicker-Observation` JSON line. Control remains the
strict status enum and final exit: `ALL PASS`/0, `CHANGES_REQUESTED`/2, or
`BLOCKED`/3. A whole-scope PASS requires every canonical target in the same
invocation to return `ALL PASS`; results from earlier runs are never combined.

## P3 Direct Acceptance

The final P3 evidence bundle is 66792 characters, so this command explicitly
uses a 70000-character budget and gives each of the five sequential targets 300
seconds:

```powershell
python methodology/nitpicker/run_nit.py --model qwen3:8b --keep-going --max-evidence-chars 70000 --evidence-file methodology/docs/discovery/ai-research-skills-20260810/p3-design.md --evidence-file methodology/tests/test_ai_coding_video_benchmark.py --evidence-file methodology/tests/test_ai_research_contract.py --file-timeout methodology/plugins/ai-research/skills/ai-coding-video-benchmark/SKILL.md=300 --file-timeout methodology/plugins/ai-research/skills/ai-coding-video-benchmark/scripts/video_gate.py=300 --file-timeout methodology/plugins/ai-research/schemas/video_result.schema.json=300 --file-timeout methodology/tests/test_ai_coding_video_benchmark.py=300 --file-timeout methodology/tests/test_ai_research_contract.py=300 methodology/plugins/ai-research/skills/ai-coding-video-benchmark/SKILL.md methodology/plugins/ai-research/skills/ai-coding-video-benchmark/scripts/video_gate.py methodology/plugins/ai-research/schemas/video_result.schema.json methodology/tests/test_ai_coding_video_benchmark.py methodology/tests/test_ai_research_contract.py
```

## P3 Envelope Integration

Five worst-case inner timeouts total 1500 seconds. The outer envelope adds 300
seconds for provider startup, evidence serialization, and process overhead, so
the canonical timeout is 1800 seconds:

```powershell
python methodology/nitpicker/nit_envelope.py --backend nitpicker --model qwen3:8b --style runnit --timeout 1800 -- python methodology/nitpicker/run_nit.py --model qwen3:8b --keep-going --max-evidence-chars 70000 --evidence-file methodology/docs/discovery/ai-research-skills-20260810/p3-design.md --evidence-file methodology/tests/test_ai_coding_video_benchmark.py --evidence-file methodology/tests/test_ai_research_contract.py --file-timeout methodology/plugins/ai-research/skills/ai-coding-video-benchmark/SKILL.md=300 --file-timeout methodology/plugins/ai-research/skills/ai-coding-video-benchmark/scripts/video_gate.py=300 --file-timeout methodology/plugins/ai-research/schemas/video_result.schema.json=300 --file-timeout methodology/tests/test_ai_coding_video_benchmark.py=300 --file-timeout methodology/tests/test_ai_research_contract.py=300 methodology/plugins/ai-research/skills/ai-coding-video-benchmark/SKILL.md methodology/plugins/ai-research/skills/ai-coding-video-benchmark/scripts/video_gate.py methodology/plugins/ai-research/schemas/video_result.schema.json methodology/tests/test_ai_coding_video_benchmark.py methodology/tests/test_ai_research_contract.py
```

An outer process timeout is envelope `BLOCKED` with exit 124 and
`nitpicker-timeout`. An inner typed file timeout is a `TIMEOUT` observation and
`run_nit.py` `BLOCKED`/3; `--style runnit` maps that to envelope `BLOCKED`/2.
These are distinct outcomes and must be reported separately.

## PASS 판정 계약 — exit code 단독 금지

`nit_envelope.py`는 **exit code만으로 PASS를 주장하지 않는다.** exit 0은 "검토했고
통과"와 "아무것도 검토하지 않았다"를 구분하지 못하기 때문이다(`LESSON-M046`). 게이트
도구가 자체 필터(경로 스코프·타 저장소·변경 감지)로 대상을 0건으로 줄이고 exit 0을 내면
fail-open이 *판정*이 아니라 *입력 선별* 단계에서 일어나, 판정 로직을 아무리 엄격히
만들어도 잡히지 않는다.

`--style mini` 호출 규약:

> **exit 0 + `MINI_NITPICKER_STATUS=REVIEWED` 정확히 한 줄
> + `MINI_NITPICKER_TALLY` 정확히 한 줄 + 그 집계가 전량 리뷰를 증명 = PASS**

집계 증명은 세 필드(`requested`/`reviewed`/`unreviewed`)가 모두 있고, 셋 다 비음수 정수이며
(`bool`은 `int`의 서브클래스라 따로 거른다), `requested > 0` · `unreviewed == 0` ·
`requested == reviewed + unreviewed` 를 만족하는 것이다. 일부 필드만 보면
`{"requested":5,"reviewed":0,"unreviewed":0}` 같은 **모순된 집계가 완전 리뷰 증거로 통과**한다.

증거가 없으면 PASS를 `BLOCKED`/2로 닫고 사유를 `not_claimed`에 남긴다:

| `not_claimed` | 뜻 |
|---|---|
| `nitpicker-status-token-missing` | 토큰이 없다 — 리뷰 수행을 확인할 수 없다(구버전 닛피커 등) |
| `nitpicker-status-token-ambiguous` | STATUS 줄이 2개 이상 — 어느 게 진짜인지 모른다 |
| `nitpicker-status-not-reviewed-<X>` | `NO_TARGETS`/`NO_DIFF`/`SKIPPED`/`UNRESOLVED_REPO`/`ERROR` |
| `nitpicker-tally-missing` | TALLY가 없다 — "미검토 0건"을 증명할 수 없다 |
| `nitpicker-tally-ambiguous` | TALLY 줄이 2개 이상 — 서로 모순돼도 각각은 통과하므로 닫는다 |
| `nitpicker-tally-field-invalid-<F>` | 필드 누락·음수·bool·비정수 |
| `nitpicker-tally-inconsistent` | `requested != reviewed + unreviewed` — 집계를 믿을 수 없다 |
| `nitpicker-partial-review-unreviewed-<N>` | 일부만 리뷰됐다 — 나머지는 한 번도 안 봤다 |
| `nitpicker-tally-requested-0` | 대상 0건. 통과가 아니라 무검토다 |
| `nitpicker-no-review-exit-<N>` | 닛피커가 무검토를 뜻하는 exit(mini: 3)로 끝났다 |

읽는 것은 stdout의 **machine token(enum·정수)** 뿐이다. 리뷰 산문은 여전히 불투명
payload이며 코드가 의미를 해석하지 않는다(R5). 줄 접두사가 아닌 산문 속 토큰 문자열은
무시한다.

`--allow-missing-status-token`은 토큰 이전 닛피커를 **의도적으로** 허용하는 escape
hatch다. 기본은 fail-closed이고, 허용해도 통과가 조용해지지 않는다 —
`nitpicker-status-token-missing-allowed`가 envelope에 남는다.

### `--style runnit`은 아직 튜플 판정이 불가능하다

`run_nit.py`는 STATUS 토큰을 내보내지 않는다. 없는 계약을 지어내 막지는 않되, 모든
runnit PASS에 `nitpicker-review-unverified-no-status-token`을 남겨 **검증하지 못했다는
사실**을 기록한다. ⚠ `run_nit.py`는 대상 0건일 때 `ALL PASS`/exit 0을 반환하므로
(`run_nit.py` `if not targets:`) `LESSON-M046`의 결함이 그대로 남아 있다 — 별도 과제다.

Keep secrets and provider tokens out of the repository. Edit
`nitpicker.config.json`, not the example file, for local persistent settings.
