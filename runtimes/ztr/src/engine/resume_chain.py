"""run-phase resume 세션 맵과 argv 변형 유틸리티."""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, cast

ResumeProfile = Literal["none", "claude", "codex"]

_POLICY_AUTO = "auto"
_POLICY_NEW = "new"

# codex-cli 0.144.6, 2026-07-20 `codex exec resume --help` 실측 기준.
# allowlist가 아니라 resume이 거부하는 exec 전용 표면만 제거해 미지 플래그는 시끄럽게 실패시킨다.
_CODEX_EXEC_ONLY_VALUE_FLAGS = (
    "-C",
    "--cd",
    "-s",
    "--sandbox",
    "--add-dir",
    "-p",
    "--profile",
    "--local-provider",
    "--color",
)
_CODEX_EXEC_ONLY_BOOLEAN_FLAGS = ("--oss", "-V", "--version")
_CODEX_EXEC_ONLY_SHORT_VALUE_FLAGS = ("-C", "-s", "-p")


class ResumePolicyError(ValueError):
    """resume 정책 위반으로 leg를 BLOCKED 처리해야 하는 오류."""


@dataclass(frozen=True)
class SessionMap:
    """역할별 외부 CLI session id를 보관하는 작은 JSON 저장소."""

    path: Path
    values: dict[str, str]

    @classmethod
    def load(cls, path: Path) -> SessionMap:
        if not path.exists():
            return cls(path=path, values={})
        try:
            data = json.loads(path.read_text(encoding="utf-8-sig"))
        except json.JSONDecodeError as exc:
            raise ValueError(f"session-map JSON 파싱 실패: {exc}") from exc
        if not isinstance(data, dict):
            raise ValueError("session-map 최상위 값은 object여야 합니다")

        values: dict[str, str] = {}
        for key, value in data.items():
            if not isinstance(key, str) or not isinstance(value, str):
                raise ValueError("session-map은 문자열 역할명과 문자열 id만 허용합니다")
            if value.strip():
                values[key] = value
        return cls(path=path, values=values)

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self.path.with_name(f".{self.path.name}.{os.getpid()}.tmp")
        try:
            tmp_path.write_text(
                json.dumps(self.values, ensure_ascii=False, indent=2, sort_keys=True)
                + "\n",
                encoding="utf-8",
            )
            os.replace(tmp_path, self.path)
        finally:
            # ReapplyLedger와 같은 원자 저장 책임: 실패 tmp를 남기지 않는다.
            try:
                tmp_path.unlink(missing_ok=True)
            except OSError:
                pass

    def get(self, role: str) -> str | None:
        return self.values.get(role)

    def set(self, role: str, session_id: str) -> None:
        self.values[role] = session_id
        self.save()


@dataclass(frozen=True)
class ResumeAttempt:
    """한 leg 실행에 적용된 resume 결정."""

    role: str
    profile: ResumeProfile
    policy: str
    requested_id: str | None
    resumed: bool
    fallback_from: str | None = None
    context_loss_warning: str | None = None
    working_dir: str | None = None
    stripped_flags: tuple[str, ...] = ()
    block_reason: str | None = None

    def as_payload(self) -> dict[str, Any]:
        return {
            "role": self.role,
            "profile": self.profile,
            "policy": self.policy,
            "requested_id": self.requested_id,
            "resumed": self.resumed,
            "fallback_from": self.fallback_from,
            "context_loss_warning": self.context_loss_warning,
            "working_dir": self.working_dir,
            "stripped_flags": list(self.stripped_flags),
            "block_reason": self.block_reason,
        }


@dataclass(frozen=True)
class ResumeArgvPlan:
    """resume argv 변환과 leg 실행 위치/차단 사실을 함께 운반한다."""

    argv: list[str]
    working_dir: str | None = None
    stripped_flags: tuple[str, ...] = ()
    block_reason: str | None = None


@dataclass(frozen=True)
class ResumeSpec:
    """leg별 resume 정책과 provider argv 변형 profile."""

    role: str
    policy: str = _POLICY_NEW
    profile: ResumeProfile = "none"

    @property
    def explicit_session_id(self) -> str | None:
        if self.policy in {_POLICY_AUTO, _POLICY_NEW}:
            return None
        return self.policy


class ResumeCoordinator:
    """PhaseRelay가 호출 전후로 사용하는 resume 영속화 계층."""

    def __init__(
        self,
        *,
        session_map: SessionMap | None,
        specs: dict[str, ResumeSpec],
    ) -> None:
        self._session_map = session_map
        self._specs = specs
        self.fallback_used = False
        self.warnings: list[str] = []

    def prepare(self, argv: list[str], *, role: str) -> tuple[list[str], ResumeAttempt]:
        spec = self._specs.get(role, ResumeSpec(role=role))
        requested_id = self._requested_id(spec)
        plan = build_resume_argv_plan(
            argv,
            profile=spec.profile,
            session_id=requested_id,
        )
        if plan.stripped_flags:
            warning = (
                f"{role} codex resume이 거부하는 exec 전용 플래그를 제거했습니다: "
                f"{', '.join(plan.stripped_flags)}."
            )
            if any(flag in {"-s", "--sandbox"} for flag in plan.stripped_flags):
                warning += " resume 기본 sandbox 등급은 NOT CLAIMED입니다."
            self.warnings.append(warning)
        return plan.argv, ResumeAttempt(
            role=role,
            profile=spec.profile,
            policy=spec.policy,
            requested_id=requested_id,
            resumed=requested_id is not None,
            working_dir=plan.working_dir,
            stripped_flags=plan.stripped_flags,
            block_reason=plan.block_reason,
        )

    def should_fallback(self, attempt: ResumeAttempt) -> bool:
        if not attempt.resumed:
            return False
        if attempt.policy not in {_POLICY_AUTO, _POLICY_NEW}:
            return False
        return True

    def should_block_failure(self, attempt: ResumeAttempt) -> bool:
        return attempt.resumed and attempt.policy not in {_POLICY_AUTO, _POLICY_NEW}

    def block_warning(self, attempt: ResumeAttempt) -> str:
        warning = (
            f"{attempt.role} 명시 세션 {attempt.requested_id} resume 실패 - "
            "사용자 의도 보존을 위해 BLOCKED 처리합니다."
        )
        self.warnings.append(warning)
        return warning

    def fallback(
        self,
        argv: list[str],
        *,
        failed_attempt: ResumeAttempt,
    ) -> tuple[list[str], ResumeAttempt]:
        self.fallback_used = True
        warning = (
            f"{failed_attempt.role} resume id가 실패하여 새 세션으로 폴백했습니다. "
            "이 leg는 이전 맥락을 잃었을 수 있습니다."
        )
        self.warnings.append(warning)
        transformed = build_resume_argv(
            argv,
            profile=failed_attempt.profile,
            session_id=None,
        )
        return transformed, ResumeAttempt(
            role=failed_attempt.role,
            profile=failed_attempt.profile,
            policy=failed_attempt.policy,
            requested_id=None,
            resumed=False,
            fallback_from=failed_attempt.requested_id,
            context_loss_warning=warning,
        )

    def capture(self, stdout_text: str, *, attempt: ResumeAttempt) -> str | None:
        if attempt.profile == "none":
            return None
        session_id = extract_session_id(stdout_text, profile=attempt.profile)
        if (
            self.should_block_failure(attempt)
            and attempt.requested_id is not None
            and session_id != attempt.requested_id
        ):
            warning = (
                f"{attempt.role} 명시 세션 {attempt.requested_id} resume 결과가 "
                f"다른 세션 {session_id}로 캡처되었습니다 - 조용한 맥락 손실 방지를 위해 "
                "BLOCKED 처리합니다."
            )
            self.warnings.append(warning)
            raise ResumePolicyError(warning)
        if self._session_map is not None:
            self._session_map.set(attempt.role, session_id)
        return session_id

    def _requested_id(self, spec: ResumeSpec) -> str | None:
        if spec.policy == _POLICY_NEW:
            return None
        if spec.policy == _POLICY_AUTO:
            return self._session_map.get(spec.role) if self._session_map else None
        return spec.policy


def normalize_policy(value: str) -> str:
    policy = value.strip()
    if not policy:
        raise ValueError("resume 정책은 비어 있을 수 없습니다")
    return policy


def build_resume_argv(
    argv: list[str],
    *,
    profile: ResumeProfile,
    session_id: str | None,
) -> list[str]:
    """profile에 맞게 JSON 출력과 resume id를 결정론적으로 주입한다."""
    return build_resume_argv_plan(
        argv, profile=profile, session_id=session_id
    ).argv


def build_resume_argv_plan(
    argv: list[str],
    *,
    profile: ResumeProfile,
    session_id: str | None,
) -> ResumeArgvPlan:
    """argv와 resume leg 전용 cwd/표면화/차단 계획을 산출한다."""
    if profile == "none":
        return ResumeArgvPlan(argv=list(argv))
    if profile == "claude":
        return ResumeArgvPlan(argv=_build_claude_argv(argv, session_id=session_id))
    if profile == "codex":
        return _build_codex_plan(argv, session_id=session_id)
    raise ValueError(f"지원하지 않는 resume profile입니다: {profile}")


def extract_session_id(stdout_text: str, *, profile: ResumeProfile) -> str:
    """구조화 stdout에서 provider session id 문자열만 추출한다."""
    if profile == "claude":
        return _extract_claude_session_id(stdout_text)
    if profile == "codex":
        return _extract_codex_thread_id(stdout_text)
    raise ValueError(f"id 추출을 지원하지 않는 resume profile입니다: {profile}")


def _build_claude_argv(argv: list[str], *, session_id: str | None) -> list[str]:
    result = list(argv)
    if session_id is not None and "--resume" not in result:
        insert_at = 1
        if len(result) > 1 and result[1] in {"-p", "--print"}:
            insert_at = 2
        result[insert_at:insert_at] = ["--resume", session_id]
    result = _ensure_option_value(result, "--output-format", "json")
    return result


def _build_codex_argv(argv: list[str], *, session_id: str | None) -> list[str]:
    return _build_codex_plan(argv, session_id=session_id).argv


def _build_codex_plan(argv: list[str], *, session_id: str | None) -> ResumeArgvPlan:
    result = list(argv)
    working_dir: str | None = None
    stripped_flags: list[str] = []
    block_reason: str | None = None
    if session_id is not None and "resume" not in result[1:3]:
        if len(result) < 2 or result[1] != "exec":
            raise ValueError("codex resume profile은 'codex exec ...' argv가 필요합니다")
        result[2:2] = ["resume", session_id]
        result, working_dir, stripped_flags, block_reason = _strip_codex_exec_only_flags(
            result
        )
    if "--json" not in result:
        insert_at = 2
        if len(result) >= 4 and result[1] == "exec" and result[2] == "resume":
            insert_at = 4
        result.insert(insert_at, "--json")
    return ResumeArgvPlan(
        argv=result,
        working_dir=working_dir,
        stripped_flags=tuple(stripped_flags),
        block_reason=block_reason,
    )


def _strip_codex_exec_only_flags(
    argv: list[str],
) -> tuple[list[str], str | None, list[str], str | None]:
    result: list[str] = []
    stripped: list[str] = []
    working_dir: str | None = None
    block_reason: str | None = None
    index = 0
    while index < len(argv):
        token = argv[index]
        if token in _CODEX_EXEC_ONLY_BOOLEAN_FLAGS:
            stripped.append(token)
            index += 1
            continue

        flag: str | None = None
        value: str | None = None
        if token in _CODEX_EXEC_ONLY_VALUE_FLAGS:
            flag = token
            nxt = argv[index + 1] if index + 1 < len(argv) else None
            if nxt is not None and not nxt.startswith("-"):
                value = nxt
                index += 2
            else:
                index += 1
        else:
            for candidate in _CODEX_EXEC_ONLY_VALUE_FLAGS:
                prefix = f"{candidate}="
                if token.startswith(prefix):
                    flag = candidate
                    value = token[len(prefix) :]
                    index += 1
                    break
            if flag is None:
                for candidate in _CODEX_EXEC_ONLY_SHORT_VALUE_FLAGS:
                    if token.startswith(candidate) and token != candidate:
                        flag = candidate
                        value = token[len(candidate) :]
                        index += 1
                        break

        if flag is None:
            result.append(token)
            index += 1
            continue

        stripped.append(flag)
        if not value:
            block_reason = f"codex resume argv: {flag} 값 누락"
            continue
        if flag in {"-C", "--cd"}:
            working_dir = value

    return result, working_dir, stripped, block_reason


def _ensure_option_value(argv: list[str], option: str, value: str) -> list[str]:
    result = list(argv)
    if option in result:
        index = result.index(option)
        if index + 1 < len(result):
            result[index + 1] = value
        else:
            result.append(value)
        return result
    result.extend([option, value])
    return result


def _extract_claude_session_id(stdout_text: str) -> str:
    try:
        data = json.loads(stdout_text)
    except json.JSONDecodeError as exc:
        raise ValueError("claude stdout JSON에서 session_id를 읽지 못했습니다") from exc
    if not isinstance(data, dict) or not isinstance(data.get("session_id"), str):
        raise ValueError("claude stdout JSON에 session_id 문자열이 없습니다")
    session_id = cast(str, data["session_id"])
    return session_id


def _extract_codex_thread_id(stdout_text: str) -> str:
    for line in stdout_text.splitlines():
        if not line.strip():
            continue
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            continue
        if (
            isinstance(data, dict)
            and data.get("type") == "thread.started"
            and isinstance(data.get("thread_id"), str)
        ):
            thread_id = cast(str, data["thread_id"])
            return thread_id
    raise ValueError("codex stdout JSONL에 thread.started.thread_id가 없습니다")
