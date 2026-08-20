"""Phase 5-1 단위 테스트 — ToastHook 이벤트 안전성."""
from __future__ import annotations


class TestToastHookEscape:
    """ToastHook._escape() XML 특수문자 이스케이프."""

    def test_escape_ampersand(self) -> None:
        from src.engine.hooks import ToastHook
        assert ToastHook._escape("a & b") == "a &amp; b"

    def test_escape_angle_brackets(self) -> None:
        from src.engine.hooks import ToastHook
        assert ToastHook._escape("<tag>") == "&lt;tag&gt;"

    def test_escape_quotes(self) -> None:
        from src.engine.hooks import ToastHook
        assert ToastHook._escape('say "hello"') == "say &quot;hello&quot;"

    def test_escape_apostrophe(self) -> None:
        from src.engine.hooks import ToastHook
        assert ToastHook._escape("it's") == "it&apos;s"

    def test_escape_combined(self) -> None:
        from src.engine.hooks import ToastHook
        result = ToastHook._escape('<a href="x">&</a>')
        assert "&lt;" in result
        assert "&amp;" in result
        assert "&quot;" in result
