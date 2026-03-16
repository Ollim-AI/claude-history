"""Tests for render.py formatting and display functions."""

from datetime import datetime, timedelta, timezone

from claude_history.render import (
    _short_model_name,
    bold,
    cyan,
    cyan_bold,
    dim,
    format_duration,
    format_relative_time,
    format_time,
    format_tokens,
    format_tool_summary,
    green,
    truncate_text,
    yellow,
)


class TestColorHelpers:
    def test_cyan_wraps_text(self) -> None:
        result = cyan("hello")
        assert "hello" in result
        assert "\033[36m" in result
        assert "\033[0m" in result

    def test_dim_wraps_text(self) -> None:
        result = dim("hello")
        assert "\033[2m" in result

    def test_yellow_wraps_text(self) -> None:
        result = yellow("hello")
        assert "\033[33m" in result

    def test_bold_wraps_text(self) -> None:
        result = bold("hello")
        assert "\033[1m" in result

    def test_cyan_bold_wraps_text(self) -> None:
        result = cyan_bold("hello")
        assert "\033[1m" in result
        assert "\033[36m" in result

    def test_green_wraps_text(self) -> None:
        result = green("hello")
        assert "\033[32m" in result

    def test_color_accepts_non_string(self) -> None:
        result = cyan(42)
        assert "42" in result


class TestTruncateText:
    def test_short_text_unchanged(self) -> None:
        assert "hello" in truncate_text("hello", 100)

    def test_newlines_replaced(self) -> None:
        result = truncate_text("line1\nline2", 100)
        assert "\n" not in result

    def test_long_text_truncated(self) -> None:
        text = "a" * 600
        result = truncate_text(text, 100)
        # Should be shorter than original (includes ANSI codes for ellipsis)
        assert len(text) > len(result.replace("\033[33m", "").replace("\033[0m", ""))

    def test_custom_max_length(self) -> None:
        text = "a" * 50
        result = truncate_text(text, 20)
        # Truncated text should contain ellipsis marker
        assert "…" in result or "\033[33m" in result


class TestFormatDuration:
    def test_seconds(self) -> None:
        assert format_duration(5) == "5s"
        assert format_duration(0) == "0s"
        assert format_duration(59) == "59s"

    def test_minutes(self) -> None:
        assert format_duration(60) == "1m0s"
        assert format_duration(90) == "1m30s"
        assert format_duration(3599) == "59m59s"

    def test_hours(self) -> None:
        assert format_duration(3600) == "1h0m"
        assert format_duration(7200) == "2h0m"
        assert format_duration(5400) == "1h30m"


class TestFormatTokens:
    def test_small_count(self) -> None:
        assert format_tokens(0) == "0"
        assert format_tokens(999) == "999"

    def test_thousands(self) -> None:
        assert format_tokens(1000) == "1.0K"
        assert format_tokens(1500) == "1.5K"
        assert format_tokens(10000) == "10.0K"

    def test_large_count(self) -> None:
        assert format_tokens(1000000) == "1000.0K"


class TestFormatRelativeTime:
    def test_just_now(self) -> None:
        now = datetime.now(timezone.utc)
        assert format_relative_time(now) == "just now"

    def test_minutes_ago(self) -> None:
        dt = datetime.now(timezone.utc) - timedelta(minutes=5)
        assert "5m ago" == format_relative_time(dt)

    def test_hours_ago(self) -> None:
        dt = datetime.now(timezone.utc) - timedelta(hours=3)
        assert "3h ago" == format_relative_time(dt)

    def test_days_ago(self) -> None:
        dt = datetime.now(timezone.utc) - timedelta(days=3)
        assert "3d ago" == format_relative_time(dt)

    def test_future_shows_date(self) -> None:
        dt = datetime.now(timezone.utc) + timedelta(days=1)
        result = format_relative_time(dt)
        # Future times show absolute date
        assert "ago" not in result

    def test_old_shows_month_day(self) -> None:
        dt = datetime.now(timezone.utc) - timedelta(days=30)
        result = format_relative_time(dt)
        assert "ago" not in result

    def test_very_old_shows_full_date(self) -> None:
        dt = datetime(2023, 1, 15, tzinfo=timezone.utc)
        result = format_relative_time(dt)
        assert "2023" in result


class TestFormatTime:
    def test_iso_mode(self) -> None:
        dt = datetime(2025, 6, 15, 10, 30, 0, tzinfo=timezone.utc)
        result = format_time(dt, use_iso=True)
        assert "2025" in result
        assert "T" in result

    def test_relative_mode(self) -> None:
        dt = datetime.now(timezone.utc)
        result = format_time(dt, use_iso=False)
        assert "just now" == result


class TestFormatToolSummary:
    def test_bash_returns_command(self) -> None:
        assert format_tool_summary("Bash", {"command": "ls -la"}) == "ls -la"

    def test_read_returns_path(self) -> None:
        assert format_tool_summary("Read", {"file_path": "/tmp/foo.py"}) == "/tmp/foo.py"

    def test_grep_returns_pattern(self) -> None:
        assert format_tool_summary("Grep", {"pattern": "TODO"}) == "TODO"

    def test_glob_returns_pattern(self) -> None:
        assert format_tool_summary("Glob", {"pattern": "*.py"}) == "*.py"

    def test_edit_returns_path(self) -> None:
        assert format_tool_summary("Edit", {"file_path": "/tmp/x.py"}) == "/tmp/x.py"

    def test_write_returns_path(self) -> None:
        assert format_tool_summary("Write", {"file_path": "/tmp/x.py"}) == "/tmp/x.py"

    def test_task_returns_description(self) -> None:
        assert format_tool_summary("Task", {"description": "do stuff"}) == "do stuff"

    def test_unknown_tool_returns_first_string(self) -> None:
        assert format_tool_summary("CustomTool", {"arg": "value"}) == "value"

    def test_unknown_tool_empty_input(self) -> None:
        assert format_tool_summary("CustomTool", {}) == ""

    def test_unknown_tool_skips_non_string(self) -> None:
        assert format_tool_summary("CustomTool", {"num": 42, "text": "hi"}) == "hi"

    def test_missing_key_returns_empty(self) -> None:
        assert format_tool_summary("Bash", {}) == ""


class TestShortModelName:
    def test_opus(self) -> None:
        assert _short_model_name("claude-opus-4-5-20251101") == "opus"

    def test_sonnet(self) -> None:
        assert _short_model_name("claude-sonnet-4-5-20250929") == "sonnet"

    def test_haiku(self) -> None:
        assert _short_model_name("claude-haiku-4-5-20251001") == "haiku"

    def test_none(self) -> None:
        assert _short_model_name(None) == "unknown"

    def test_unknown_model(self) -> None:
        assert _short_model_name("gpt-4") == "gpt-4"

    def test_synthetic(self) -> None:
        assert _short_model_name("<synthetic>") == "<synthetic>"
