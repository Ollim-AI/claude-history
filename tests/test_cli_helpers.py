"""Tests for cli.py helper functions (parse_since, encode_path, highlight_match, etc.)."""

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from claude_history.cli import encode_path, highlight_match, parse_since, resolve_session_ref, resolve_slug


class TestParseSince:
    def test_minutes(self) -> None:
        result = parse_since("30m")
        now = datetime.now(timezone.utc)
        # Should be ~30 minutes ago
        delta = now - result
        assert 29 * 60 <= delta.total_seconds() <= 31 * 60

    def test_hours(self) -> None:
        result = parse_since("2h")
        now = datetime.now(timezone.utc)
        delta = now - result
        assert 119 * 60 <= delta.total_seconds() <= 121 * 60

    def test_days(self) -> None:
        result = parse_since("3d")
        now = datetime.now(timezone.utc)
        delta = now - result
        assert 2.9 <= delta.days <= 3.1 or delta.days == 3

    def test_weeks(self) -> None:
        result = parse_since("1w")
        now = datetime.now(timezone.utc)
        delta = now - result
        assert 6.9 <= delta.days <= 7.1 or delta.days == 7

    def test_today(self) -> None:
        result = parse_since("today")
        assert result.hour == 0
        assert result.minute == 0
        assert result.second == 0
        assert result.tzinfo is not None

    def test_yesterday(self) -> None:
        result = parse_since("yesterday")
        today = parse_since("today")
        delta = today - result
        assert delta.days == 1

    def test_iso_date(self) -> None:
        result = parse_since("2024-06-15")
        assert result.year == 2024
        assert result.month == 6
        assert result.day == 15

    def test_invalid_exits(self) -> None:
        with pytest.raises(SystemExit):
            parse_since("invalid")

    def test_result_is_timezone_aware(self) -> None:
        result = parse_since("1d")
        assert result.tzinfo is not None


class TestEncodePath:
    def test_basic_path(self) -> None:
        assert encode_path("/home/user/Code/foo") == "-home-user-Code-foo"

    def test_dot_replaced(self) -> None:
        assert encode_path("/home/user/.claude") == "-home-user--claude"

    def test_backslash_replaced(self) -> None:
        assert encode_path("C:\\Users\\foo") == "C--Users-foo"

    def test_colon_replaced(self) -> None:
        assert encode_path("C:/Users/foo") == "C--Users-foo"


class TestHighlightMatch:
    def test_highlights_match(self) -> None:
        result = highlight_match("hello world foo bar", "world")
        assert "world" in result
        # Should contain ANSI yellow around the match
        assert "\033[33m" in result

    def test_no_match_truncates(self) -> None:
        result = highlight_match("hello world", "xyz")
        assert "hello world" in result

    def test_multiline_flattened(self) -> None:
        result = highlight_match("line1\nline2\nline3", "line2")
        assert "\n" not in result
        assert "line2" in result

    def test_context_around_match(self) -> None:
        text = "a" * 100 + "MATCH" + "b" * 100
        result = highlight_match(text, "MATCH", context_chars=10)
        # Should show ellipsis for truncated parts
        assert "..." in result

    def test_case_insensitive(self) -> None:
        result = highlight_match("Hello World", "hello")
        # Should find the match (case-insensitive search)
        assert "\033[33m" in result


class TestResolveSessionRef:
    def test_uuid_passthrough(self) -> None:
        prefix, window = resolve_session_ref("abc123", Path("/nonexistent"))
        assert prefix == "abc123"
        assert window is None

    def test_uuid_with_window(self) -> None:
        prefix, window = resolve_session_ref("abc123:2", Path("/nonexistent"))
        assert prefix == "abc123"
        assert window == 2

    def _write_session(self, tmp_path: Path, sid: str, mtime: float) -> None:
        """Write a JSONL file with compact JSON (no spaces) to match grep pattern."""
        import os
        f = tmp_path / f"{sid}.jsonl"
        # Compact JSON: "sessionId":"..." with no space after colon
        f.write_text(f'{{"sessionId":"{sid}","type":"user"}}\n')
        os.utime(f, (mtime, mtime))

    def test_prev_format_parsed(self, tmp_path: Path) -> None:
        # Need at least 2 sessions: prev resolves to index 1
        self._write_session(tmp_path, "aaa", 3000)
        self._write_session(tmp_path, "bbb", 2000)
        self._write_session(tmp_path, "ccc", 1000)

        prefix, window = resolve_session_ref("prev", tmp_path)
        assert window is None
        assert prefix == "bbb"[:8]

    def test_prev_with_window(self, tmp_path: Path) -> None:
        self._write_session(tmp_path, "aaa", 3000)
        self._write_session(tmp_path, "bbb", 2000)

        prefix, window = resolve_session_ref("prev:3", tmp_path)
        assert window == 3
        assert prefix == "bbb"[:8]

    def test_prev_n_too_large_exits(self, tmp_path: Path) -> None:
        self._write_session(tmp_path, "aaa", 1000)

        with pytest.raises(SystemExit):
            resolve_session_ref("prev-5", tmp_path)

    def test_slug_resolved(self, tmp_path: Path) -> None:
        sid = "abcd1234-0000-0000-0000-000000000000"
        f = tmp_path / f"{sid}.jsonl"
        f.write_text(json.dumps({"sessionId": sid, "type": "user", "slug": "keen-mapping-torvalds"}) + "\n")

        prefix, window = resolve_session_ref("keen-mapping-torvalds", tmp_path)
        assert prefix == sid[:8]
        assert window is None

    def test_slug_with_window(self, tmp_path: Path) -> None:
        sid = "abcd1234-0000-0000-0000-000000000000"
        f = tmp_path / f"{sid}.jsonl"
        f.write_text(json.dumps({"sessionId": sid, "type": "user", "slug": "keen-mapping-torvalds"}) + "\n")

        prefix, window = resolve_session_ref("keen-mapping-torvalds:2", tmp_path)
        assert prefix == sid[:8]
        assert window == 2

    def test_slug_not_found_exits(self, tmp_path: Path) -> None:
        with pytest.raises(SystemExit):
            resolve_session_ref("nonexistent-slug-name", tmp_path)

    def test_hex_identifier_not_treated_as_slug(self) -> None:
        """Identifiers with only hex chars should pass through as UUID prefixes."""
        prefix, window = resolve_session_ref("abcdef12", Path("/nonexistent"))
        assert prefix == "abcdef12"
        assert window is None


class TestResolveSlug:
    def test_finds_slug(self, tmp_path: Path) -> None:
        sid = "aaaa1111-2222-3333-4444-555566667777"
        f = tmp_path / f"{sid}.jsonl"
        f.write_text(json.dumps({"sessionId": sid, "type": "user", "slug": "playful-rolling-falcon"}) + "\n")

        result = resolve_slug("playful-rolling-falcon", tmp_path)
        assert result == sid

    def test_returns_none_when_not_found(self, tmp_path: Path) -> None:
        f = tmp_path / "session.jsonl"
        f.write_text(json.dumps({"sessionId": "aaa", "type": "user", "slug": "other-slug"}) + "\n")

        result = resolve_slug("nonexistent-slug", tmp_path)
        assert result is None

    def test_slug_on_later_line(self, tmp_path: Path) -> None:
        """Slug may not be on the first line (e.g., file-history-snapshot first)."""
        sid = "bbbb2222-3333-4444-5555-666677778888"
        f = tmp_path / f"{sid}.jsonl"
        lines = [
            json.dumps({"type": "file-history-snapshot", "messageId": "xyz"}),
            json.dumps({"sessionId": sid, "type": "user", "slug": "snug-drifting-muffin"}),
        ]
        f.write_text("\n".join(lines) + "\n")

        result = resolve_slug("snug-drifting-muffin", tmp_path)
        assert result == sid

    def test_empty_dir(self, tmp_path: Path) -> None:
        result = resolve_slug("any-slug", tmp_path)
        assert result is None
