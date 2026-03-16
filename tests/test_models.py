"""Tests for models.py edge cases not covered by other test files."""

from datetime import datetime, timezone

from claude_history.models import (
    extract_content_text,
    iter_user_records,
    parse_timestamp,
    strip_system_tags,
    ProgressStub,
)


class TestParseTimestamp:
    def test_iso_with_z(self) -> None:
        dt = parse_timestamp("2026-01-01T10:30:00Z")
        assert dt is not None
        assert dt.year == 2026
        assert dt.hour == 10
        assert dt.minute == 30
        assert dt.tzinfo is not None

    def test_iso_with_offset(self) -> None:
        dt = parse_timestamp("2026-01-01T10:30:00+05:00")
        assert dt is not None
        assert dt.tzinfo is not None

    def test_none_input(self) -> None:
        assert parse_timestamp(None) is None

    def test_empty_string(self) -> None:
        assert parse_timestamp("") is None

    def test_invalid_string(self) -> None:
        assert parse_timestamp("not-a-date") is None

    def test_date_only(self) -> None:
        dt = parse_timestamp("2026-01-15")
        assert dt is not None
        assert dt.year == 2026
        assert dt.month == 1
        assert dt.day == 15

    def test_result_is_timezone_aware(self) -> None:
        dt = parse_timestamp("2026-01-01T00:00:00Z")
        assert dt is not None
        assert dt.tzinfo is not None


class TestExtractContentText:
    def test_string_content(self) -> None:
        assert extract_content_text("hello world") == "hello world"

    def test_list_with_text_blocks(self) -> None:
        content = [
            {"type": "text", "text": "hello"},
            {"type": "text", "text": "world"},
        ]
        result = extract_content_text(content)
        assert "hello" in result
        assert "world" in result

    def test_strips_system_tags(self) -> None:
        content = [{"type": "text", "text": "before <system-reminder>hidden</system-reminder> after"}]
        result = extract_content_text(content)
        assert "hidden" not in result
        assert "before" in result
        assert "after" in result

    def test_skips_non_text_blocks(self) -> None:
        content = [
            {"type": "tool_result", "content": "result"},
            {"type": "text", "text": "visible"},
        ]
        result = extract_content_text(content)
        assert "result" not in result
        assert "visible" in result

    def test_string_blocks(self) -> None:
        content = ["just a string"]
        result = extract_content_text(content)
        assert "just a string" in result

    def test_empty_list(self) -> None:
        assert extract_content_text([]) == ""

    def test_non_list_non_string(self) -> None:
        assert extract_content_text(42) == ""  # type: ignore[arg-type]


class TestStripSystemTags:
    def test_removes_system_reminder(self) -> None:
        text = "before <system-reminder>secret</system-reminder> after"
        result = strip_system_tags(text)
        assert "secret" not in result
        assert "before" in result
        assert "after" in result

    def test_no_tags(self) -> None:
        assert strip_system_tags("plain text") == "plain text"

    def test_multiple_tags(self) -> None:
        text = "a <system-reminder>x</system-reminder> b <system-reminder>y</system-reminder> c"
        result = strip_system_tags(text)
        assert "x" not in result
        assert "y" not in result
        assert "a" in result
        assert "b" in result
        assert "c" in result

    def test_collapses_excess_newlines(self) -> None:
        text = "a\n\n\n\n\nb"
        result = strip_system_tags(text)
        assert "\n\n\n" not in result


class TestIterUserRecords:
    def test_yields_user_records(self) -> None:
        records: list = [
            {"type": "user", "uuid": "u1"},
            {"type": "assistant", "uuid": "a1"},
            {"type": "user", "uuid": "u2"},
        ]
        result = list(iter_user_records(records))
        assert len(result) == 2
        assert result[0]["uuid"] == "u1"
        assert result[1]["uuid"] == "u2"

    def test_skips_progress_stubs(self) -> None:
        records: list = [
            {"type": "user", "uuid": "u1"},
            ProgressStub(uuid="p1", parentUuid=None, parentToolUseID=None, agentId=None),
        ]
        result = list(iter_user_records(records))
        assert len(result) == 1

    def test_empty_list(self) -> None:
        assert list(iter_user_records([])) == []
