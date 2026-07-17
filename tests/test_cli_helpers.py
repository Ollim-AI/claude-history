"""Tests for cli.py helper functions (parse_since, encode_path, highlight_match, etc.)."""

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from claude_history.cli import highlight_match, parse_since
from claude_history.resolve import (
    encode_path,
    resolve_project_dir,
    find_session_across_projects,
    find_subagent_across_projects,
    find_prompt_across_projects,
    resolve_session_ref,
    resolve_slug,
)


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

    def test_today_is_local_midnight(self) -> None:
        # Bug 3: named shortcuts mark LOCAL calendar-day boundaries, so
        # `today` equals local midnight of the current local date.
        result = parse_since("today")
        local_now = datetime.now().astimezone()
        expected = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
        assert result == expected
        assert result.utcoffset() == local_now.utcoffset()

    def test_yesterday_is_local_midnight(self) -> None:
        # Bug 3: yesterday is exactly one calendar day before today's local
        # midnight (and stays local-midnight, not shifted by the UTC offset).
        result = parse_since("yesterday")
        assert result.hour == 0 and result.minute == 0 and result.second == 0
        assert (parse_since("today") - result).days == 1
        assert result.utcoffset() == datetime.now().astimezone().utcoffset()


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

    def test_length_changing_lower_char_before_match(self) -> None:
        # Bug 8: a preceding char whose .lower() changes length (İ -> 2 chars)
        # must not shift the highlighted span onto the wrong substring.
        result = highlight_match(
            "İİİİİİİİİİ hello WORLD after", "world", context_chars=5
        )
        assert "\033[33mWORLD\033[0m" in result

    def test_case_sensitive_query_length_differs(self) -> None:
        # The highlighted slice uses the matched span length, not len(query).
        result = highlight_match("say HELLO now", "hello")
        assert "\033[33mHELLO\033[0m" in result


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

    def test_latest_resolves_to_most_recent(self, tmp_path: Path) -> None:
        self._write_session(tmp_path, "aaa", 3000)
        self._write_session(tmp_path, "bbb", 2000)
        self._write_session(tmp_path, "ccc", 1000)

        prefix, window = resolve_session_ref("latest", tmp_path)
        assert window is None
        assert prefix == "aaa"[:8]

    def test_latest_with_window(self, tmp_path: Path) -> None:
        self._write_session(tmp_path, "aaa", 3000)
        self._write_session(tmp_path, "bbb", 2000)

        prefix, window = resolve_session_ref("latest:2", tmp_path)
        assert window == 2
        assert prefix == "aaa"[:8]

    def test_latest_no_sessions_exits(self, tmp_path: Path) -> None:
        with pytest.raises(SystemExit):
            resolve_session_ref("latest", tmp_path)

    def test_prev_n_too_large_exits(self, tmp_path: Path) -> None:
        self._write_session(tmp_path, "aaa", 1000)

        with pytest.raises(SystemExit):
            resolve_session_ref("prev-5", tmp_path)

    def test_slug_resolved(self, tmp_path: Path) -> None:
        sid = "abcd1234-0000-0000-0000-000000000000"
        f = tmp_path / f"{sid}.jsonl"
        f.write_text(json.dumps({"sessionId": sid, "type": "user", "slug": "keen-mapping-torvalds"}, separators=(",", ":")) + "\n")

        prefix, window = resolve_session_ref("keen-mapping-torvalds", tmp_path)
        assert prefix == sid[:8]
        assert window is None

    def test_slug_with_window(self, tmp_path: Path) -> None:
        sid = "abcd1234-0000-0000-0000-000000000000"
        f = tmp_path / f"{sid}.jsonl"
        f.write_text(json.dumps({"sessionId": sid, "type": "user", "slug": "keen-mapping-torvalds"}, separators=(",", ":")) + "\n")

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
        f.write_text(json.dumps({"sessionId": sid, "type": "user", "slug": "playful-rolling-falcon"}, separators=(",", ":")) + "\n")

        result = resolve_slug("playful-rolling-falcon", tmp_path)
        assert result == sid

    def test_returns_none_when_not_found(self, tmp_path: Path) -> None:
        f = tmp_path / "session.jsonl"
        f.write_text(json.dumps({"sessionId": "aaa", "type": "user", "slug": "other-slug"}, separators=(",", ":")) + "\n")

        result = resolve_slug("nonexistent-slug", tmp_path)
        assert result is None

    def test_slug_on_later_line(self, tmp_path: Path) -> None:
        """Slug may not be on the first line (e.g., file-history-snapshot first)."""
        sid = "bbbb2222-3333-4444-5555-666677778888"
        f = tmp_path / f"{sid}.jsonl"
        lines = [
            json.dumps({"type": "file-history-snapshot", "messageId": "xyz"}),
            json.dumps({"sessionId": sid, "type": "user", "slug": "snug-drifting-muffin"}, separators=(",", ":")),
        ]
        f.write_text("\n".join(lines) + "\n")

        result = resolve_slug("snug-drifting-muffin", tmp_path)
        assert result == sid

    def test_empty_dir(self, tmp_path: Path) -> None:
        result = resolve_slug("any-slug", tmp_path)
        assert result is None


class TestFindSessionAcrossProjects:
    def _setup_projects(self, tmp_path: Path) -> Path:
        """Create a mock CLAUDE_PROJECTS_DIR with multiple project dirs."""
        projects = tmp_path / "projects"
        projects.mkdir()

        # Project A has session "aaaa1111..."
        proj_a = projects / "-home-user-project-a"
        proj_a.mkdir()
        (proj_a / "aaaa1111-0000-0000-0000-000000000000.jsonl").write_text(
            '{"sessionId":"aaaa1111-0000-0000-0000-000000000000","type":"user"}\n'
        )

        # Project B has session "bbbb2222..."
        proj_b = projects / "-home-user-project-b"
        proj_b.mkdir()
        (proj_b / "bbbb2222-0000-0000-0000-000000000000.jsonl").write_text(
            '{"sessionId":"bbbb2222-0000-0000-0000-000000000000","type":"user"}\n'
        )

        return projects

    def test_finds_session_in_other_project(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        projects = self._setup_projects(tmp_path)
        monkeypatch.setattr("claude_history.resolve.CLAUDE_PROJECTS_DIR", projects)

        proj_a = projects / "-home-user-project-a"
        result = find_session_across_projects("bbbb2222", exclude_dir=proj_a)
        assert result is not None
        assert result.name == "-home-user-project-b"

    def test_skips_excluded_dir(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        projects = self._setup_projects(tmp_path)
        monkeypatch.setattr("claude_history.resolve.CLAUDE_PROJECTS_DIR", projects)

        proj_a = projects / "-home-user-project-a"
        result = find_session_across_projects("aaaa1111", exclude_dir=proj_a)
        assert result is None

    def test_returns_none_when_not_found(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        projects = self._setup_projects(tmp_path)
        monkeypatch.setattr("claude_history.resolve.CLAUDE_PROJECTS_DIR", projects)

        result = find_session_across_projects("cccc3333")
        assert result is None

    def test_returns_none_for_nonexistent_projects_dir(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("claude_history.resolve.CLAUDE_PROJECTS_DIR", tmp_path / "nonexistent")
        result = find_session_across_projects("aaaa")
        assert result is None


class TestFindSubagentAcrossProjects:
    def test_finds_subagent_in_other_project(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        projects = tmp_path / "projects"
        projects.mkdir()

        proj_a = projects / "-home-user-project-a"
        proj_a.mkdir()

        proj_b = projects / "-home-user-project-b"
        proj_b.mkdir()
        session_dir = proj_b / "bbbb2222-0000-0000-0000-000000000000"
        subagents_dir = session_dir / "subagents"
        subagents_dir.mkdir(parents=True)
        agent_file = subagents_dir / "agent-abc123def456.jsonl"
        agent_file.write_text('{"type":"user"}\n')

        monkeypatch.setattr("claude_history.resolve.CLAUDE_PROJECTS_DIR", projects)

        result = find_subagent_across_projects("abc123", exclude_dir=proj_a)
        assert result is not None
        found_dir, found_file = result
        assert found_dir.name == "-home-user-project-b"
        assert found_file == agent_file

    def test_returns_none_when_not_found(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        projects = tmp_path / "projects"
        projects.mkdir()
        proj = projects / "-home-user-project-a"
        proj.mkdir()

        monkeypatch.setattr("claude_history.resolve.CLAUDE_PROJECTS_DIR", projects)

        result = find_subagent_across_projects("nonexistent")
        assert result is None


class TestFindPromptAcrossProjects:
    def test_finds_prompt_in_other_project(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        projects = tmp_path / "projects"
        projects.mkdir()

        proj_a = projects / "-home-user-project-a"
        proj_a.mkdir()

        proj_b = projects / "-home-user-project-b"
        proj_b.mkdir()
        (proj_b / "session.jsonl").write_text(
            '{"type":"user","uuid":"deadbeef-1234-5678-9abc-def012345678"}\n'
        )

        monkeypatch.setattr("claude_history.resolve.CLAUDE_PROJECTS_DIR", projects)

        result = find_prompt_across_projects("deadbeef-1234", exclude_dir=proj_a)
        assert result is not None
        project_dir, session_file = result
        assert project_dir.name == "-home-user-project-b"
        assert session_file.name == "session.jsonl"

    def test_returns_none_when_not_found(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        projects = tmp_path / "projects"
        projects.mkdir()
        proj = projects / "-home-user-project-a"
        proj.mkdir()
        (proj / "session.jsonl").write_text('{"type":"user","uuid":"other-uuid"}\n')

        monkeypatch.setattr("claude_history.resolve.CLAUDE_PROJECTS_DIR", projects)

        result = find_prompt_across_projects("nonexistent-uuid")
        assert result is None


class TestResolveProjectByName:
    def test_bare_name_resolves_under_projects_dir(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        # All encoded project names start with '-'; README documents passing
        # the name directly (--project=-Users-me-repo), not only a full path.
        projects = tmp_path / "projects"
        target = projects / "-Users-me-repo"
        target.mkdir(parents=True)
        monkeypatch.setattr("claude_history.resolve.CLAUDE_PROJECTS_DIR", projects)
        args = argparse.Namespace(project="-Users-me-repo", cwd=None)
        assert resolve_project_dir(args) == target

    def test_missing_name_reports_both_paths_tried(
        self, tmp_path: Path, monkeypatch, capsys
    ) -> None:
        monkeypatch.setattr(
            "claude_history.resolve.CLAUDE_PROJECTS_DIR", tmp_path / "projects"
        )
        args = argparse.Namespace(project="-no-such-project", cwd=None)
        with pytest.raises(SystemExit):
            resolve_project_dir(args)
        err = capsys.readouterr().err
        assert "Tried:" in err
        assert "-no-such-project" in err


class TestInvalidWindowSuffix:
    def test_latest_with_non_numeric_window_errors(self, tmp_path: Path, capsys) -> None:
        with pytest.raises(SystemExit):
            resolve_session_ref("latest:abc", tmp_path)
        err = capsys.readouterr().err
        assert "Invalid context window" in err
        assert "latest:0" in err

    def test_hex_prefix_with_negative_window_errors(self, tmp_path: Path, capsys) -> None:
        with pytest.raises(SystemExit):
            resolve_session_ref("9aaedc03:-1", tmp_path)
        assert "Invalid context window" in capsys.readouterr().err

    def test_trailing_bare_colon_errors(self, tmp_path: Path, capsys) -> None:
        with pytest.raises(SystemExit):
            resolve_session_ref("prev:", tmp_path)
        assert "Invalid context window" in capsys.readouterr().err


class TestCaseInsensitiveHexIds:
    def test_uppercase_prefix_lowered(self, tmp_path: Path) -> None:
        # Stored session IDs are lowercase hex; uppercase input previously
        # fell through to slug resolution and errored.
        assert resolve_session_ref("9AAEDC03", tmp_path) == ("9aaedc03", None)

    def test_uppercase_prefix_with_window(self, tmp_path: Path) -> None:
        assert resolve_session_ref("9AAEDC03:2", tmp_path) == ("9aaedc03", 2)


class TestSearchTargetConflicts:
    def _args(self, **kw) -> argparse.Namespace:
        base = dict(target=None, prompts_only=False, responses_only=False)
        base.update(kw)
        return argparse.Namespace(**base)

    def test_p_and_r_together_rejected(self, capsys) -> None:
        # Previously -p silently won and -r was ignored.
        from claude_history.cli import _parse_targets

        with pytest.raises(SystemExit):
            _parse_targets(self._args(prompts_only=True, responses_only=True))
        assert "cannot be combined" in capsys.readouterr().err

    def test_target_with_p_rejected(self, capsys) -> None:
        from claude_history.cli import _parse_targets

        with pytest.raises(SystemExit):
            _parse_targets(self._args(target="tools", prompts_only=True))
        assert "cannot be combined" in capsys.readouterr().err


class TestFindPromptAcrossProjectsExclude:
    def _projects(self, tmp_path: Path, monkeypatch) -> Path:
        projects = tmp_path / "projects"
        projects.mkdir()
        monkeypatch.setattr("claude_history.resolve.CLAUDE_PROJECTS_DIR", projects)
        return projects

    def test_match_in_other_project_wins_over_excluded_hit(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        # Previously only the FIRST grep hit was inspected: if it happened to
        # be in exclude_dir, a valid match elsewhere was never returned.
        projects = self._projects(tmp_path, monkeypatch)
        for name in ("-proj-a", "-proj-b"):
            d = projects / name
            d.mkdir()
            (d / "s1.jsonl").write_text('{"uuid":"promptuuid42"}\n')
        excluded = projects / "-proj-a"
        found = find_prompt_across_projects("promptuuid42", exclude_dir=excluded)
        assert found is not None
        project_dir, session_file = found
        assert project_dir == projects / "-proj-b"
        assert session_file == projects / "-proj-b" / "s1.jsonl"

    def test_only_excluded_match_returns_none(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        projects = self._projects(tmp_path, monkeypatch)
        d = projects / "-proj-a"
        d.mkdir()
        (d / "s1.jsonl").write_text('{"uuid":"promptuuid42"}\n')
        assert find_prompt_across_projects("promptuuid42", exclude_dir=d) is None
