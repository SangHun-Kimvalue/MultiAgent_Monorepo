import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
COMPOSER = REPO_ROOT / "methodology/plugins/ai-research/scripts/compose_daily_report.py"
BRIEFING_DATE = "2026-08-10"
NONSTRUCTURAL_SEPARATORS = ("\x0b", "\x0c", "\x1c", "\x1d", "\x1e", "\x85", "\u2028", "\u2029")


def news_result(count=6, status=None):
    status = status or ("OK" if count >= 6 else "DEGRADED")
    return {
        "status": status,
        "final_count": count,
        "items": [
            {
                "id": f"news-{index:03d}",
                "claim": f"Claim {index}",
                "url": f"https://example.com/news/{index}",
                "published_date": BRIEFING_DATE,
                "event_date": None,
                "confidence": "official",
                "audit_reason_ko": "원문 확인",
            }
            for index in range(1, count + 1)
        ],
        "evidence": [],
    }


def video_item(identifier="video-001", **overrides):
    value = {
        "id": identifier,
        "url": "https://www.youtube.com/watch?v=abcdefghijk",
        "title": "A testable coding workflow",
        "channel": "Engineering Channel",
        "published_date": "2026-08-08",
        "evidence_grade": "M2",
        "creator_claim_ko": "제작자 주장",
        "confirmed_fact_ko": "직접 확인한 사실",
        "task_ko": "구체적인 구현 과제",
        "environment_ko": "도구와 버전",
        "cross_checks": [],
        "failure_conditions_ko": "실패 조건",
        "adoption_decision": "PILOT",
        "small_experiment_ko": "30분 실험",
        "score_total": 8,
    }
    value.update(overrides)
    return value


def video_result(status="READY", count=1, items=None, evidence=None):
    if items is None:
        items = [] if count == 0 else [video_item()]
    return {
        "video_status": status,
        "selected_count": count,
        "items": items,
        "evidence": evidence or [],
    }


def base_markdown(*, newline="\n", bom=False, summary="핵심 한 줄 요약"):
    lines = [
        f"# {BRIEFING_DATE} AI Briefing   STATUS: DEGRADED",
        "기준: KST · 검증 항목: N건",
        "",
        "## 0. 요약",
        summary,
    ]
    for number in range(1, 8):
        lines.extend(["", f"## {number}. 기존 장 {number}", f"본문 {number}"])
    lines.extend(["", "## 8. 오늘의 SW 아키텍처와 설계 패턴", "아키텍처 본문", ""])
    text = newline.join(lines)
    return ("\ufeff" if bom else "") + text


def write_json(path, payload):
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


def prepare_inputs(tmp_path, news=None, video=None, base=None):
    tmp_path.mkdir(parents=True, exist_ok=True)
    news_path = write_json(tmp_path / f"{BRIEFING_DATE}.news.json", news or news_result())
    video_path = write_json(tmp_path / f"{BRIEFING_DATE}.video.json", video or video_result())
    base_path = tmp_path / "base.md"
    base_path.write_bytes((base if base is not None else base_markdown()).encode("utf-8"))
    return news_path, video_path, base_path


def command_for(tmp_path, news_path, video_path, base_path, output=None, date=BRIEFING_DATE):
    output = output or tmp_path / f"{date}-ai-briefing.md"
    return [
        sys.executable,
        "-X",
        "utf8",
        str(COMPOSER),
        "--date",
        date,
        "--news",
        str(news_path),
        "--video",
        str(video_path),
        "--base-markdown",
        str(base_path),
        "--output",
        str(output),
    ], output


def invoke(tmp_path, *, news=None, video=None, base=None, output=None, date=BRIEFING_DATE):
    news_path, video_path, base_path = prepare_inputs(tmp_path, news, video, base)
    command, output = command_for(tmp_path, news_path, video_path, base_path, output, date)
    process = subprocess.run(
        command,
        cwd=REPO_ROOT,
        encoding="utf-8",
        text=True,
        capture_output=True,
        check=False,
    )
    assert process.stderr == ""
    assert process.stdout.count("\n") == 1
    return process, json.loads(process.stdout), output


def load_composer(name):
    spec = importlib.util.spec_from_file_location(name, COMPOSER)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_news_zero_video_ready_two_preserves_news_ownership(tmp_path):
    items = [
        video_item("video-001"),
        video_item("video-002", url="https://www.youtube.com/watch?v=bcdefghijkl"),
    ]
    process, envelope, output = invoke(
        tmp_path,
        news=news_result(0),
        video=video_result("READY", 2, items),
    )
    text = output.read_text(encoding="utf-8")
    assert process.returncode == 0
    assert envelope["action"] == "COMPOSED"
    assert envelope["kakao"]["status"] == "DEGRADED"
    assert envelope["kakao"]["verified_count"] == 0
    assert "STATUS: DEGRADED" in text
    assert "검증 항목: 0건" in text
    assert "VIDEO: READY" in text
    assert text.count("- 재현 상태: NOT TESTED") == 2


@pytest.mark.parametrize("count", [1, 3, 5])
def test_news_one_to_five_stays_degraded_with_ready_video(tmp_path, count):
    process, envelope, output = invoke(tmp_path, news=news_result(count))
    assert process.returncode == 0
    assert envelope["kakao"]["status"] == "DEGRADED"
    assert envelope["kakao"]["verified_count"] == count
    assert f"검증 항목: {count}건" in output.read_text(encoding="utf-8")


def test_news_six_video_empty_is_ok_and_renders_empty_literal(tmp_path):
    process, envelope, output = invoke(
        tmp_path, video=video_result("EMPTY", 0, [])
    )
    text = output.read_text(encoding="utf-8")
    assert process.returncode == 0
    assert envelope["kakao"]["status"] == "OK"
    assert envelope["kakao"]["verified_count"] == 6
    assert "STATUS: OK" in text
    assert "VIDEO: EMPTY" in text
    assert "최근 검토 창에서 선정 기준을 충족한 영상을 확인하지 못했다." in text
    assert "- 재현 상태: NOT TESTED" not in text


@pytest.mark.parametrize(
    ("mutation", "reason"),
    [
        (lambda news, video: news.update({"extra": True}), "NEWS_SCHEMA_INVALID"),
        (lambda news, video: video.update({"selected_count": "1"}), "VIDEO_SCHEMA_INVALID"),
        (lambda news, video: news.update({"status": "READY"}), "NEWS_SCHEMA_INVALID"),
    ],
)
def test_malformed_schema_blocks_without_final(tmp_path, mutation, reason):
    news = news_result()
    video = video_result()
    mutation(news, video)
    process, envelope, output = invoke(tmp_path, news=news, video=video)
    assert process.returncode == 2
    assert envelope == {"action": "BLOCKED", "reason_code": reason}
    assert not output.exists()


@pytest.mark.parametrize(
    ("target", "reason"),
    [("news", "NEWS_INPUT_INVALID"), ("video", "VIDEO_INPUT_INVALID")],
)
def test_malformed_json_blocks_without_final(tmp_path, target, reason):
    news_path, video_path, base_path = prepare_inputs(tmp_path)
    (news_path if target == "news" else video_path).write_bytes(b"{malformed")
    command, output = command_for(tmp_path, news_path, video_path, base_path)
    process = subprocess.run(
        command,
        cwd=REPO_ROOT,
        text=True,
        encoding="utf-8",
        capture_output=True,
        check=False,
    )
    assert process.returncode == 2
    assert json.loads(process.stdout) == {"action": "BLOCKED", "reason_code": reason}
    assert not output.exists()


@pytest.mark.parametrize("bad_date", ["2026-8-10", "2026-02-30", "2026-08-10T00:00:00"])
def test_invalid_date_blocks(tmp_path, bad_date):
    news_path, video_path, base_path = prepare_inputs(tmp_path)
    command, output = command_for(
        tmp_path,
        news_path,
        video_path,
        base_path,
        output=tmp_path / f"{bad_date}-ai-briefing.md",
        date=bad_date,
    )
    process = subprocess.run(command, cwd=REPO_ROOT, text=True, encoding="utf-8", capture_output=True, check=False)
    assert process.returncode == 2
    assert json.loads(process.stdout)["reason_code"] == "DATE_INVALID"
    assert not output.exists()


@pytest.mark.parametrize(
    ("target", "name", "reason"),
    [
        ("news", "wrong.news.json", "NEWS_BASENAME_INVALID"),
        ("video", "wrong.video.json", "VIDEO_BASENAME_INVALID"),
        ("output", "wrong.md", "OUTPUT_BASENAME_INVALID"),
    ],
)
def test_exact_basenames_are_required(tmp_path, target, name, reason):
    news_path, video_path, base_path = prepare_inputs(tmp_path)
    output = tmp_path / f"{BRIEFING_DATE}-ai-briefing.md"
    if target == "news":
        renamed = tmp_path / name
        news_path.rename(renamed)
        news_path = renamed
    elif target == "video":
        renamed = tmp_path / name
        video_path.rename(renamed)
        video_path = renamed
    else:
        output = tmp_path / name
    command, _ = command_for(tmp_path, news_path, video_path, base_path, output=output)
    process = subprocess.run(command, cwd=REPO_ROOT, text=True, encoding="utf-8", capture_output=True, check=False)
    assert process.returncode == 2
    assert json.loads(process.stdout) == {"action": "BLOCKED", "reason_code": reason}
    assert not output.exists()


@pytest.mark.parametrize(
    "mutate",
    [
        lambda value: value.update({"status": "DEGRADED"}),
        lambda value: value.update({"final_count": 5}),
        lambda value: value["items"].__setitem__(1, {**value["items"][1], "id": "news-001"}),
        lambda value: value["items"].__setitem__(1, {**value["items"][1], "url": value["items"][0]["url"]}),
    ],
)
def test_news_logic_mismatch_blocks(tmp_path, mutate):
    news = news_result()
    mutate(news)
    process, envelope, output = invoke(tmp_path, news=news)
    assert process.returncode == 2
    assert envelope["reason_code"] == "NEWS_LOGIC_INVALID"
    assert not output.exists()


@pytest.mark.parametrize("field", ["id", "url"])
def test_duplicate_video_identity_blocks(tmp_path, field):
    first = video_item("video-001")
    second = video_item("video-002", url="https://www.youtube.com/watch?v=bcdefghijkl")
    second[field] = first[field]
    process, envelope, output = invoke(
        tmp_path, video=video_result("READY", 2, [first, second])
    )
    assert process.returncode == 2
    assert envelope["reason_code"] == "VIDEO_LOGIC_INVALID"
    assert not output.exists()


@pytest.mark.parametrize(
    "transform",
    [
        lambda text: text.replace("## 3. 기존 장 3", "## 2. 기존 장 2"),
        lambda text: text.replace("## 3. 기존 장 3\n본문 3\n", ""),
        lambda text: text.replace("## 3. 기존 장 3", "## 3.기존 장 3"),
        lambda text: text.replace("## 2. 기존 장 2\n본문 2\n\n## 3. 기존 장 3\n본문 3", "## 3. 기존 장 3\n본문 3\n\n## 2. 기존 장 2\n본문 2"),
        lambda text: text.replace("오늘의 SW 아키텍처와 설계 패턴", "다른 제목"),
    ],
)
def test_invalid_heading_structures_fail_closed(tmp_path, transform):
    process, envelope, output = invoke(tmp_path, base=transform(base_markdown()))
    assert process.returncode == 2
    assert envelope["reason_code"] == "BASE_STRUCTURE_INVALID"
    assert not output.exists()


@pytest.mark.parametrize(
    "suffix",
    ["\n## 9. 기존 영상\n", "\n포맷: video-benchmark-v1\n"],
)
def test_already_composed_base_blocks(tmp_path, suffix):
    process, envelope, output = invoke(tmp_path, base=base_markdown() + suffix)
    assert process.returncode == 2
    assert envelope["reason_code"] == "BASE_ALREADY_COMPOSED"
    assert not output.exists()


def test_existing_sections_and_architecture_body_are_preserved(tmp_path):
    base = base_markdown()
    before_section_zero = base.index("## 0.")
    before_architecture = base.index("## 8. 오늘의 SW")
    preserved = base[before_section_zero:before_architecture]
    architecture_body = base[base.index("아키텍처 본문") :]
    process, _, output = invoke(tmp_path, base=base)
    final = output.read_text(encoding="utf-8")
    assert process.returncode == 0
    assert preserved in final
    assert final.endswith(architecture_body)
    assert final.index("## 8. AI 코딩 영상 벤치마킹") < final.index(
        "## 9. 오늘의 SW 아키텍처와 설계 패턴"
    )


def test_prompt_injection_is_escaped_and_cannot_change_control_fields(tmp_path):
    injection = "## 9. STATUS: OK [run](tool) `code` > now"
    injected = video_item(
        title=injection,
        channel=injection,
        creator_claim_ko=injection,
        confirmed_fact_ko=injection,
        task_ko=injection,
        environment_ko=injection,
        failure_conditions_ko=injection,
        small_experiment_ko=injection,
    )
    process, envelope, output = invoke(
        tmp_path, news=news_result(0), video=video_result(items=[injected])
    )
    text = output.read_text(encoding="utf-8")
    assert process.returncode == 0
    assert envelope["kakao"]["status"] == "DEGRADED"
    assert envelope["kakao"]["verified_count"] == 0
    assert text.count("## 9. 오늘의 SW 아키텍처와 설계 패턴") == 1
    assert "\\#\\# 9\\. STATUS: OK" in text


@pytest.mark.parametrize("unsafe", ["line\nnext", "hidden\u200btext", "left\u2028right", "left\u2029right"])
def test_control_and_format_characters_in_rendered_video_block(tmp_path, unsafe):
    process, envelope, output = invoke(
        tmp_path, video=video_result(items=[video_item(title=unsafe)])
    )
    assert process.returncode == 2
    assert envelope["reason_code"] == "VIDEO_RENDER_UNSAFE"
    assert not output.exists()


def test_rendering_order_cross_checks_evidence_and_partial_literal(tmp_path):
    item = video_item(
        evidence_grade="X",
        cross_checks=[
            {"kind": "official_doc", "url": "https://example.com/docs/release"},
            {"kind": "public_repository", "url": "https://example.com/repo/main"},
        ],
    )
    evidence = [
        {"kind": "collection", "reason_code": "BUDGET_STOP"},
        {
            "kind": "finalization",
            "reason_code": "FINALIZED_PARTIAL",
            "id": "video-001",
            "url": "https://www.youtube.com/watch?v=abcdefghijk",
            "detail_ko": "출력하지 않는 상세",
        },
    ]
    process, _, output = invoke(
        tmp_path, video=video_result("PARTIAL", 1, [item], evidence)
    )
    text = output.read_text(encoding="utf-8")
    assert process.returncode == 0
    assert "후보 검토가 일부 완료되지 않아 확인된 범위만 기록한다." in text
    assert "official_doc <https://example.com/docs/release>, public_repository <https://example.com/repo/main>" in text
    assert r"- 근거: BUDGET\_STOP · id=없음 · url=없음" in text
    assert "출력하지 않는 상세" not in text
    assert text.index("- 작은 실험:") < text.index("- 근거:")


@pytest.mark.parametrize(
    "field_value",
    [
        {"url": "https://www.youtube.com/watch?v=abcdefghijk>"},
        {"cross_checks": [{"kind": "official_doc", "url": "https://example.com/path>"}]},
    ],
)
def test_unsafe_or_noncanonical_urls_block(tmp_path, field_value):
    item = video_item(**field_value)
    if "cross_checks" in field_value:
        item["evidence_grade"] = "X"
    process, envelope, output = invoke(tmp_path, video=video_result(items=[item]))
    assert process.returncode == 2
    assert envelope["reason_code"] in {"VIDEO_SCHEMA_INVALID", "VIDEO_RENDER_UNSAFE"}
    assert not output.exists()


@pytest.mark.parametrize("surrogate", ["\ud800", "\udfff"])
@pytest.mark.parametrize(
    "url_prefix",
    ["https://www.youtube.com/watch?v=abcdefghijk", "https://example.com/docs/release"],
)
def test_safe_url_rejects_lone_surrogate_url_shapes(surrogate, url_prefix):
    module = load_composer(f"composer_url_surrogate_{ord(surrogate):x}_{len(url_prefix)}")
    with pytest.raises(module.BlockedError, match="VIDEO_RENDER_UNSAFE"):
        module.safe_url(f"{url_prefix}{surrogate}")


@pytest.mark.parametrize("surrogate", ["\ud800", "\udfff"])
def test_schema_valid_cross_check_lone_surrogate_blocks_without_final(tmp_path, surrogate):
    news_path, video_path, base_path = prepare_inputs(tmp_path)
    payload = video_result()
    payload["items"][0]["evidence_grade"] = "X"
    payload["items"][0]["cross_checks"] = [
        {"kind": "official_doc", "url": f"https://example.com/docs/{surrogate}"}
    ]
    video_path.write_text(json.dumps(payload, ensure_ascii=True), encoding="ascii")
    command, output = command_for(tmp_path, news_path, video_path, base_path)
    process = subprocess.run(
        command,
        cwd=REPO_ROOT,
        text=True,
        encoding="utf-8",
        capture_output=True,
        check=False,
    )
    assert process.returncode == 2
    assert json.loads(process.stdout) == {
        "action": "BLOCKED",
        "reason_code": "VIDEO_RENDER_UNSAFE",
    }
    assert not output.exists()


def test_output_exists_and_missing_parent_are_fail_closed(tmp_path):
    existing = tmp_path / f"{BRIEFING_DATE}-ai-briefing.md"
    existing.write_bytes(b"original")
    process, envelope, output = invoke(tmp_path, output=existing)
    assert process.returncode == 2
    assert envelope["reason_code"] == "OUTPUT_EXISTS"
    assert output.read_bytes() == b"original"

    missing_output = tmp_path / "missing" / f"{BRIEFING_DATE}-ai-briefing.md"
    process, envelope, output = invoke(tmp_path / "second", output=missing_output)
    assert process.returncode == 2
    assert envelope["reason_code"] == "OUTPUT_PARENT_MISSING"
    assert not output.exists()


def test_parent_symlink_or_reparse_boundary_blocks(tmp_path, monkeypatch):
    module = load_composer("composer_parent_boundary")
    parent = tmp_path / "parent"
    parent.mkdir()
    output = parent / f"{BRIEFING_DATE}-ai-briefing.md"
    original = module._is_reparse_or_symlink

    def mark_parent_unsafe(path, metadata):
        return path == parent or original(path, metadata)

    monkeypatch.setattr(module, "_is_reparse_or_symlink", mark_parent_unsafe)
    with pytest.raises(module.BlockedError, match="OUTPUT_PARENT_UNSAFE"):
        module.preflight_output(output, BRIEFING_DATE)

    class ReparseMetadata:
        st_mode = module.stat.S_IFDIR
        st_file_attributes = module.stat.FILE_ATTRIBUTE_REPARSE_POINT

    assert module._is_reparse_or_symlink(parent, ReparseMetadata())


def test_publish_failure_removes_temp_and_leaves_no_final(tmp_path, monkeypatch):
    module = load_composer("composer_publish_failure")
    destination = tmp_path / f"{BRIEFING_DATE}-ai-briefing.md"
    monkeypatch.setattr(module.os, "link", lambda *_: (_ for _ in ()).throw(OSError("no hard links")))
    exit_code, envelope = module.publish(
        destination,
        b"complete",
        news_result(),
        video_result(),
        "summary",
    )
    assert exit_code == 2
    assert envelope == {"action": "BLOCKED", "reason_code": "PUBLISH_UNAVAILABLE"}
    assert not destination.exists()
    assert list(tmp_path.glob("*.tmp")) == []


def test_publish_race_preserves_competitor_and_cleans_temp(tmp_path, monkeypatch):
    module = load_composer("composer_publish_race")
    destination = (tmp_path / f"{BRIEFING_DATE}-ai-briefing.md").resolve()
    competitor = b"competitor-final"
    original_link = module.os.link

    def create_competitor_then_link(source, target):
        Path(target).write_bytes(competitor)
        return original_link(source, target)

    monkeypatch.setattr(module.os, "link", create_competitor_then_link)
    exit_code, envelope = module.publish(
        destination,
        b"composer-final",
        news_result(),
        video_result(),
        "summary",
    )
    assert exit_code == 2
    assert envelope == {"action": "BLOCKED", "reason_code": "OUTPUT_EXISTS"}
    assert destination.read_bytes() == competitor
    assert list(tmp_path.iterdir()) == [destination]


def test_cleanup_failure_reports_published_final_and_temp_paths(tmp_path, monkeypatch):
    module = load_composer("composer_cleanup_failure")
    destination = (tmp_path / f"{BRIEFING_DATE}-ai-briefing.md").resolve()
    original_unlink = Path.unlink

    def fail_temp_cleanup(path, *args, **kwargs):
        if path.suffix == ".tmp":
            raise OSError("simulated cleanup failure")
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_temp_cleanup)
    exit_code, envelope = module.publish(
        destination,
        b"complete-final",
        news_result(),
        video_result(),
        "summary",
    )
    assert exit_code == 2
    assert envelope["action"] == "BLOCKED"
    assert envelope["reason_code"] == "PUBLISHED_CLEANUP_FAILED"
    assert envelope["output_path"] == str(destination)
    assert Path(envelope["cleanup_path"]).is_absolute()
    assert destination.read_bytes() == b"complete-final"


def test_cleanup_failure_does_not_requery_temp_metadata(tmp_path, monkeypatch):
    module = load_composer("composer_cleanup_metadata_failure")
    destination = (tmp_path / f"{BRIEFING_DATE}-ai-briefing.md").resolve()
    original_mkstemp = module.tempfile.mkstemp
    original_unlink = Path.unlink
    original_resolve = Path.resolve
    captured = {}

    def capture_temp(*args, **kwargs):
        descriptor, name = original_mkstemp(*args, **kwargs)
        captured["path"] = os.path.abspath(name)
        return descriptor, name

    def fail_temp_cleanup(path, *args, **kwargs):
        if path.suffix == ".tmp":
            raise OSError("simulated cleanup failure")
        return original_unlink(path, *args, **kwargs)

    def fail_temp_resolve(path, *args, **kwargs):
        if path.suffix == ".tmp":
            raise OSError("simulated metadata failure")
        return original_resolve(path, *args, **kwargs)

    monkeypatch.setattr(module.tempfile, "mkstemp", capture_temp)
    monkeypatch.setattr(Path, "unlink", fail_temp_cleanup)
    monkeypatch.setattr(Path, "resolve", fail_temp_resolve)
    exit_code, envelope = module.publish(
        destination,
        b"complete-final",
        news_result(),
        video_result(),
        "summary",
    )
    assert exit_code == 2
    assert envelope == {
        "action": "BLOCKED",
        "reason_code": "PUBLISHED_CLEANUP_FAILED",
        "output_path": str(destination),
        "cleanup_path": captured["path"],
    }
    assert destination.read_bytes() == b"complete-final"


def test_success_envelope_mapping_and_utf16_summary_limit(tmp_path):
    summary = "😀" * 300
    process, envelope, output = invoke(tmp_path, base=base_markdown(summary=summary))
    kakao = envelope["kakao"]
    assert process.returncode == 0
    assert set(envelope) == {"action", "output_path", "kakao"}
    assert set(kakao) == {"status", "verified_count", "summary", "file_path"}
    assert Path(envelope["output_path"]).is_absolute()
    assert envelope["output_path"] == kakao["file_path"] == str(output.resolve())
    assert len(kakao["summary"].encode("utf-16-le")) // 2 <= 200
    assert not kakao["summary"].endswith("\ud83d")


def test_ascii_envelope_survives_cp949_with_astral_summary_and_nonascii_path(tmp_path):
    run_root = tmp_path / "한글경로"
    news_path, video_path, base_path = prepare_inputs(
        run_root, base=base_markdown(summary="😀" * 300)
    )
    command, output = command_for(run_root, news_path, video_path, base_path)
    command = [command[0], *command[3:]]
    environment = os.environ.copy()
    environment.update({"PYTHONUTF8": "0", "PYTHONIOENCODING": "cp949"})
    process = subprocess.run(
        command,
        cwd=REPO_ROOT,
        env=environment,
        capture_output=True,
        check=False,
    )
    assert process.returncode == 0
    assert process.stderr == b""
    assert process.stdout.isascii()
    assert process.stdout.count(b"\n") == 1
    envelope = json.loads(process.stdout.decode("ascii"))
    assert envelope["action"] == "COMPOSED"
    assert envelope["output_path"] == str(output.resolve())
    assert "😀" in envelope["kakao"]["summary"]
    assert output.exists()


def test_missing_or_spoofed_section_zero_summary_blocks(tmp_path):
    missing = base_markdown(summary="### heading only")
    process, envelope, output = invoke(tmp_path, base=missing)
    assert process.returncode == 2
    assert envelope["reason_code"] == "BASE_SUMMARY_MISSING"
    assert not output.exists()

    spoof = base_markdown(summary="safe\u200bspoof")
    process, envelope, output = invoke(tmp_path / "spoof", base=spoof)
    assert process.returncode == 2
    assert envelope["reason_code"] == "BASE_SUMMARY_UNSAFE"
    assert not output.exists()


@pytest.mark.parametrize("separator", NONSTRUCTURAL_SEPARATORS)
def test_nonstructural_line_separators_reach_summary_sanitizer(tmp_path, separator):
    process, envelope, output = invoke(
        tmp_path, base=base_markdown(summary=f"safe{separator}spoof")
    )
    assert process.returncode == 2
    assert envelope == {"action": "BLOCKED", "reason_code": "BASE_SUMMARY_UNSAFE"}
    assert not output.exists()


@pytest.mark.parametrize("separator", NONSTRUCTURAL_SEPARATORS)
@pytest.mark.parametrize("position", ["prefix", "suffix"])
def test_summary_edge_separators_are_checked_before_trim(tmp_path, separator, position):
    summary = f"{separator}safe" if position == "prefix" else f"safe{separator}"
    process, envelope, output = invoke(tmp_path, base=base_markdown(summary=summary))
    assert process.returncode == 2
    assert envelope == {"action": "BLOCKED", "reason_code": "BASE_SUMMARY_UNSAFE"}
    assert not output.exists()


@pytest.mark.parametrize("surrogate", ["\ud800", "\udfff"])
@pytest.mark.parametrize("field", ["title", "reason_code", "id"])
def test_lone_surrogate_display_fields_are_rejected(tmp_path, surrogate, field):
    news_path, video_path, base_path = prepare_inputs(tmp_path)
    payload = video_result()
    if field == "title":
        payload["items"][0]["title"] = surrogate
    else:
        evidence = {
            "kind": "finalization",
            "reason_code": "FINALIZED_READY",
            "id": "video-001",
        }
        evidence[field] = surrogate
        payload["evidence"] = [evidence]
    video_path.write_text(json.dumps(payload, ensure_ascii=True), encoding="ascii")
    command, output = command_for(tmp_path, news_path, video_path, base_path)
    process = subprocess.run(
        command,
        cwd=REPO_ROOT,
        text=True,
        encoding="utf-8",
        capture_output=True,
        check=False,
    )
    assert process.returncode == 2
    assert json.loads(process.stdout) == {
        "action": "BLOCKED",
        "reason_code": "VIDEO_RENDER_UNSAFE",
    }
    assert not output.exists()


@pytest.mark.parametrize(
    ("target", "reason"),
    [("news", "NEWS_INPUT_INVALID"), ("video", "VIDEO_INPUT_INVALID")],
)
def test_oversized_json_integer_is_an_input_error(tmp_path, target, reason):
    news_path, video_path, base_path = prepare_inputs(tmp_path)
    oversized = "9" * 5000
    if target == "news":
        news_path.write_text(
            f'{{"status":"OK","final_count":{oversized},"items":[],"evidence":[]}}',
            encoding="utf-8",
        )
    else:
        video_path.write_text(
            f'{{"video_status":"READY","selected_count":{oversized},"items":[],"evidence":[]}}',
            encoding="utf-8",
        )
    command, output = command_for(tmp_path, news_path, video_path, base_path)
    process = subprocess.run(
        command,
        cwd=REPO_ROOT,
        text=True,
        encoding="utf-8",
        capture_output=True,
        check=False,
    )
    assert process.returncode == 2
    assert json.loads(process.stdout) == {"action": "BLOCKED", "reason_code": reason}
    assert not output.exists()


def test_optional_bom_is_preserved_and_embedded_bom_is_rejected(tmp_path):
    process, _, output = invoke(tmp_path, base=base_markdown(bom=True))
    assert process.returncode == 0
    assert output.read_bytes().startswith(b"\xef\xbb\xbf")

    embedded = base_markdown().replace("본문 3", "본문\ufeff 3")
    process, envelope, output = invoke(tmp_path / "embedded", base=embedded)
    assert process.returncode == 2
    assert envelope["reason_code"] == "BASE_STRUCTURE_INVALID"
    assert not output.exists()


def test_first_nonempty_title_allows_leading_blank_line(tmp_path):
    base = "\n" + base_markdown()
    process, _, output = invoke(tmp_path, base=base)
    assert process.returncode == 0
    assert output.read_bytes().startswith(b"\n# 2026-08-10 AI Briefing   STATUS: OK")


@pytest.mark.parametrize("separator", NONSTRUCTURAL_SEPARATORS)
def test_separator_only_preamble_is_not_structurally_blank(tmp_path, separator):
    process, envelope, output = invoke(tmp_path, base=f"{separator}\n{base_markdown()}")
    assert process.returncode == 2
    assert envelope == {"action": "BLOCKED", "reason_code": "BASE_STRUCTURE_INVALID"}
    assert not output.exists()


def test_mixed_line_endings_preserve_untouched_slices_and_anchor_style(tmp_path):
    base = base_markdown(newline="\n")
    base = base.replace("## 0. 요약\n핵심 한 줄 요약\n", "## 0. 요약\r\n핵심 한 줄 요약\r\n")
    base = base.replace(
        "## 8. 오늘의 SW 아키텍처와 설계 패턴\n아키텍처 본문\n",
        "## 8. 오늘의 SW 아키텍처와 설계 패턴\r\n아키텍처 본문\n",
    )
    process, _, output = invoke(tmp_path, base=base)
    final = output.read_bytes().decode("utf-8")
    assert process.returncode == 0
    assert "## 0. 요약\r\n핵심 한 줄 요약\r\n" in final
    inserted = final[final.index("## 8. AI") : final.index("## 9. 오늘의")]
    assert "\r\n" in inserted
    assert "\n" not in inserted.replace("\r\n", "")
    assert final.endswith("아키텍처 본문\n")


def test_blocked_and_internal_error_envelopes_use_exit_two_and_three(tmp_path, monkeypatch, capsys):
    process, envelope, output = invoke(tmp_path, news={"malformed": True})
    assert process.returncode == 2
    assert envelope["action"] == "BLOCKED"
    assert not output.exists()

    module = load_composer("composer_internal_error")
    news_path, video_path, base_path = prepare_inputs(tmp_path / "internal")
    command, _ = command_for(tmp_path, news_path, video_path, base_path)
    monkeypatch.setattr(module, "run_composer", lambda _args: (_ for _ in ()).throw(RuntimeError("bug")))
    assert module.main(command[4:]) == 3
    assert json.loads(capsys.readouterr().out) == {
        "action": "BLOCKED",
        "reason_code": "INTERNAL_ERROR",
    }
