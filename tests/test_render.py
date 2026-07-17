"""Tests for render.py formatting and display functions."""

from datetime import datetime, timedelta, timezone

from claude_history.models import ContentBlock, ToolResultContent, ToolUseContent
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
    render_blocks,
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


def _tool_result_blocks(content: str, is_error: bool = False) -> list[ContentBlock]:
    """Helper: tool_use + tool_result pair (tool_use needed for last_tool_name tracking)."""
    return [
        ContentBlock(type="tool_use", content=ToolUseContent(name="Bash", id="t1", input={"command": "ls"})),
        ContentBlock(type="tool_result", content=ToolResultContent(content=content, is_error=is_error)),
    ]


class TestRenderBlocksToolResults:
    def test_default_shows_tool_results(self, capsys) -> None:
        blocks = _tool_result_blocks("file1.py\nfile2.py")
        render_blocks(blocks, {})
        out = capsys.readouterr().out
        assert "file1.py" in out
        assert "file2.py" in out

    def test_hide_tool_results_suppresses(self, capsys) -> None:
        blocks = _tool_result_blocks("file1.py")
        render_blocks(blocks, {}, show_tool_results=False)
        out = capsys.readouterr().out
        assert "file1.py" not in out

    def test_error_results_shown_in_full(self, capsys) -> None:
        error_text = "\n".join(f"error line {i}" for i in range(30))
        blocks = _tool_result_blocks(error_text, is_error=True)
        render_blocks(blocks, {})
        out = capsys.readouterr().out
        assert "error line 29" in out
        assert "more lines" not in out

    def test_long_success_results_truncated(self, capsys) -> None:
        long_text = "\n".join(f"line {i}" for i in range(40))
        blocks = _tool_result_blocks(long_text)
        render_blocks(blocks, {}, detail_hint="claude-history response abc --show-tool-results")
        out = capsys.readouterr().out
        assert "line 0" in out
        assert "line 19" in out
        assert "line 20" not in out
        assert "20 more lines" in out
        assert "claude-history response abc --show-tool-results" in out

    def test_hint_printed_once_with_multiple_truncations(self, capsys) -> None:
        blocks = (
            _tool_result_blocks("\n".join(f"a{i}" for i in range(30)))
            + _tool_result_blocks("\n".join(f"b{i}" for i in range(30)))
        )
        render_blocks(blocks, {}, detail_hint="claude-history transcript x --show-tool-results")
        out = capsys.readouterr().out
        assert out.count("--show-tool-results") == 1

    def test_no_hint_when_full_detail(self, capsys) -> None:
        long_text = "\n".join(f"line {i}" for i in range(40))
        blocks = _tool_result_blocks(long_text)
        render_blocks(blocks, {}, full_detail=True, detail_hint="should not appear")
        out = capsys.readouterr().out
        assert "should not appear" not in out

    def test_full_detail_disables_truncation(self, capsys) -> None:
        long_text = "\n".join(f"line {i}" for i in range(40))
        blocks = _tool_result_blocks(long_text)
        render_blocks(blocks, {}, full_detail=True)
        out = capsys.readouterr().out
        assert "line 39" in out
        assert "more lines" not in out

    def test_short_success_results_not_truncated(self, capsys) -> None:
        short_text = "\n".join(f"line {i}" for i in range(10))
        blocks = _tool_result_blocks(short_text)
        render_blocks(blocks, {})
        out = capsys.readouterr().out
        assert "line 9" in out
        assert "more lines" not in out

    def test_full_detail_shows_tool_inputs(self, capsys) -> None:
        blocks = [
            ContentBlock(type="tool_use", content=ToolUseContent(name="Read", id="t1", input={"file_path": "/tmp/foo.py"})),
        ]
        render_blocks(blocks, {}, full_detail=True)
        out = capsys.readouterr().out
        assert '"file_path"' in out
        assert "/tmp/foo.py" in out

    def test_default_shows_tool_summaries(self, capsys) -> None:
        blocks = [
            ContentBlock(type="tool_use", content=ToolUseContent(name="Read", id="t1", input={"file_path": "/tmp/foo.py"})),
        ]
        render_blocks(blocks, {})
        out = capsys.readouterr().out
        assert "/tmp/foo.py" in out
        assert '"file_path"' not in out


class TestResultCharCap:
    def _blocks(self, content) -> list[ContentBlock]:
        return [
            ContentBlock(type="tool_use", content=ToolUseContent(
                id="t1", name="Bash", input={"command": "x"})),
            ContentBlock(type="tool_result", content=ToolResultContent(
                content=content, is_error=False)),
        ]

    def test_single_line_giant_result_clipped(self, capsys) -> None:
        render_blocks(self._blocks("y" * 300_000), {})
        out = capsys.readouterr().out
        assert len(out) < 10_000
        assert "more chars" in out

    def test_list_content_clipped(self, capsys) -> None:
        giant = [{"type": "text", "text": "z" * 300_000}]
        render_blocks(self._blocks(giant), {})
        out = capsys.readouterr().out
        assert len(out) < 10_000
        assert "more chars" in out

    def test_error_results_never_clipped(self, capsys) -> None:
        blocks = [
            ContentBlock(type="tool_use", content=ToolUseContent(
                id="t1", name="Bash", input={"command": "x"})),
            ContentBlock(type="tool_result", content=ToolResultContent(
                content="e" * 10_000, is_error=True)),
        ]
        render_blocks(blocks, {})
        out = capsys.readouterr().out
        assert "e" * 10_000 in out
