import json
import re
from pathlib import Path

import yaml


REPO_ROOT = Path(__file__).resolve().parents[2]
PLUGIN_ROOT = REPO_ROOT / "methodology" / "plugins" / "ai-research"
SKILLS_ROOT = PLUGIN_ROOT / "skills"

EXPECTED_SKILLS = {
    "daily-ai-news": {
        "required": {"status", "final_count", "items", "evidence"},
        "token": "$daily-ai-news",
        "positive": {
            "데일리 뉴스",
            "오늘 AI 데일리 뉴스",
            "AI 데일리 뉴스 만들어줘",
        },
        "negative": {"일반 금융/스포츠", "단순 기사 요약", "`브리핑` 단독"},
    },
    "ai-coding-video-benchmark": {
        "required": {"video_status", "selected_count", "items", "evidence"},
        "token": "$ai-coding-video-benchmark",
        "positive": {
            "AI 코딩 영상 벤치마킹",
            "코딩 영상 벤치마킹",
            "AI 코딩 유튜브 비교",
            "코딩 에이전트 영상 검증",
        },
        "negative": {"단순 영상 요약", "일반 유튜브 추천", "비-AI 제품 벤치마크"},
    },
}


def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def load_skill(skill_name: str) -> tuple[dict, str]:
    text = (SKILLS_ROOT / skill_name / "SKILL.md").read_text(encoding="utf-8")
    match = re.match(r"^---\r?\n(.*?)\r?\n---\r?\n", text, re.DOTALL)
    assert match, f"{skill_name}: invalid YAML frontmatter"
    metadata = yaml.safe_load(match.group(1))
    return metadata, text


def test_manifests_have_p1_shape() -> None:
    claude = load_json(PLUGIN_ROOT / ".claude-plugin" / "plugin.json")
    codex = load_json(PLUGIN_ROOT / ".codex-plugin" / "plugin.json")

    assert set(claude) == {"name", "version", "description", "author", "keywords"}
    assert claude["name"] == codex["name"] == "ai-research"
    assert claude["version"] == codex["version"] == "0.1.0"
    assert codex["skills"] == "./skills/"
    assert "hooks" not in codex
    assert set(codex["interface"]) == {
        "displayName",
        "shortDescription",
        "longDescription",
        "developerName",
        "category",
        "capabilities",
        "defaultPrompt",
    }


def test_exactly_two_skills_are_discoverable() -> None:
    discovered = {
        path.parent.name
        for path in SKILLS_ROOT.glob("*/SKILL.md")
        if (path.parent / "agents" / "openai.yaml").is_file()
    }
    assert discovered == set(EXPECTED_SKILLS)


def test_trigger_and_non_trigger_corpus_is_in_description() -> None:
    for skill_name, expected in EXPECTED_SKILLS.items():
        frontmatter, _ = load_skill(skill_name)
        assert frontmatter["name"] == skill_name
        description = frontmatter["description"]
        for phrase in expected["positive"] | expected["negative"]:
            assert phrase in description, f"{skill_name}: missing description phrase {phrase!r}"


def test_openai_default_prompt_names_each_skill() -> None:
    for skill_name, expected in EXPECTED_SKILLS.items():
        openai = yaml.safe_load(
            (SKILLS_ROOT / skill_name / "agents" / "openai.yaml").read_text(
                encoding="utf-8"
            )
        )
        assert expected["token"] in openai["interface"]["default_prompt"]


def test_news_schema_contract() -> None:
    schema = load_json(PLUGIN_ROOT / "schemas" / "news_result.schema.json")
    assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    assert set(schema["required"]) == EXPECTED_SKILLS["daily-ai-news"]["required"]
    assert schema["additionalProperties"] is False
    assert schema["properties"]["status"]["enum"] == ["OK", "DEGRADED"]
    assert schema["properties"]["final_count"]["minimum"] == 0
    assert schema["properties"]["items"]["type"] == "array"
    assert "url" in schema["properties"]["items"]["items"]["required"]
    assert schema["properties"]["evidence"]["type"] == "array"


def test_video_schema_contract() -> None:
    schema = load_json(PLUGIN_ROOT / "schemas" / "video_result.schema.json")
    assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    expected = EXPECTED_SKILLS["ai-coding-video-benchmark"]["required"]
    assert set(schema["required"]) == expected
    assert schema["additionalProperties"] is False
    assert schema["properties"]["video_status"]["enum"] == [
        "READY",
        "PARTIAL",
        "EMPTY",
    ]
    assert schema["properties"]["selected_count"] == {
        "type": "integer",
        "minimum": 0,
        "maximum": 3,
    }
    item_schema = schema["properties"]["items"]["items"]
    assert item_schema["additionalProperties"] is False
    assert set(item_schema["required"]) == {
        "id",
        "url",
        "title",
        "channel",
        "published_date",
        "evidence_grade",
        "creator_claim_ko",
        "confirmed_fact_ko",
        "task_ko",
        "environment_ko",
        "cross_checks",
        "failure_conditions_ko",
        "adoption_decision",
        "small_experiment_ko",
        "score_total",
    }
    grades = item_schema["properties"]["evidence_grade"]["enum"]
    assert grades == ["M2", "X"]
    assert "M1" not in grades
    assert "R" not in grades
    assert item_schema["allOf"] == [
        {
            "if": {"properties": {"evidence_grade": {"const": "X"}}},
            "then": {"properties": {"cross_checks": {"minItems": 1}}},
        }
    ]
    cross_check_url = item_schema["properties"]["cross_checks"]["items"]["properties"]["url"]
    assert cross_check_url["format"] == "uri"
    assert cross_check_url["pattern"].startswith("^https://")
    assert "\\u0000-\\u001F\\u007F" in cross_check_url["pattern"]
    assert " " in cross_check_url["pattern"]
    assert "\\\\" in cross_check_url["pattern"]
    assert "HTTPS" not in cross_check_url["pattern"]
    evidence_schema = schema["properties"]["evidence"]
    assert evidence_schema["type"] == "array"
    assert (
        evidence_schema["items"]["properties"]["url"]
        == item_schema["properties"]["url"]
    )


def test_skill_required_field_marker_matches_machine_schema() -> None:
    schema_by_skill = {
        "daily-ai-news": "news_result.schema.json",
        "ai-coding-video-benchmark": "video_result.schema.json",
    }
    for skill_name, schema_name in schema_by_skill.items():
        frontmatter, _ = load_skill(skill_name)
        marker = frontmatter["metadata"]["result_contract"]["required_fields"]
        schema = load_json(PLUGIN_ROOT / "schemas" / schema_name)
        assert set(marker) == set(schema["required"])


def test_skills_have_no_runtime_plugin_root_dependency() -> None:
    forbidden = ("plugin-root", "plugin_root", "methodology/plugins/ai-research", "/schemas/")
    for skill_name in EXPECTED_SKILLS:
        _, text = load_skill(skill_name)
        normalized = text.lower().replace("\\", "/")
        for token in forbidden:
            assert token not in normalized, f"{skill_name}: runtime dependency {token!r}"
