"""Command-handler regression tests for cli.py (cmd_response, cmd_sessions)."""

import argparse
import json
from pathlib import Path

import pytest

from claude_history.cli import cmd_response, cmd_sessions


def _write_session(project_dir: Path, session_id: str, records: list[dict]) -> Path:
    path = project_dir / f"{session_id}.jsonl"
    path.write_text(
        "\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8"
    )
    return path


def _sessions_args(project_dir: Path, **overrides) -> argparse.Namespace:
    base = dict(
        project=str(project_dir),
        cwd=None,
        since=None,
        page=1,
        size=None,
        timestamps=False,
    )
    base.update(overrides)
    return argparse.Namespace(**base)


class TestCmdResponse:
    def test_response_renders_without_nameerror(
        self, tmp_path: Path, capsys
    ) -> None:
        # Bug 2: cmd_response referenced an undefined `detail_hint`, crashing
        # with NameError for any prompt whose response chain is non-empty.
        records = [
            {
                "type": "user",
                "uuid": "prompt001",
                "parentUuid": None,
                "sessionId": "s1",
                "timestamp": "2026-01-01T10:00:00Z",
                "message": {"content": [{"type": "text", "text": "hello there"}]},
            },
            {
                "type": "assistant",
                "uuid": "assist001",
                "parentUuid": "prompt001",
                "sessionId": "s1",
                "timestamp": "2026-01-01T10:00:01Z",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "general kenobi"}],
                },
            },
        ]
        _write_session(tmp_path, "s1", records)
        args = argparse.Namespace(
            project=str(tmp_path),
            cwd=None,
            uuid="prompt001",
            show_thinking=False,
            hide_tools=False,
            hide_tool_results=False,
            show_hooks=False,
        )
        cmd_response(args)
        out = capsys.readouterr().out
        assert "general kenobi" in out


class TestCmdSessionsSizeValidation:
    @pytest.fixture
    def project(self, tmp_path: Path) -> Path:
        records = [
            {
                "type": "user",
                "uuid": "u1",
                "parentUuid": None,
                "sessionId": "s1",
                "timestamp": "2026-01-01T10:00:00Z",
                "message": {"content": [{"type": "text", "text": "hi"}]},
            }
        ]
        _write_session(tmp_path, "s1", records)
        return tmp_path

    def test_size_zero_rejected(self, project: Path, capsys) -> None:
        # Bug 4: --size 0 was silently swallowed by `args.size or PAGE_SIZE`.
        with pytest.raises(SystemExit):
            cmd_sessions(_sessions_args(project, size=0))
        assert "positive integer" in capsys.readouterr().err

    def test_negative_size_rejected(self, project: Path, capsys) -> None:
        # Bug 4: --size -3 produced "Page 1 out of range (1--96)".
        with pytest.raises(SystemExit):
            cmd_sessions(_sessions_args(project, size=-3))
        err = capsys.readouterr().err
        assert "positive integer" in err
        assert "out of range" not in err

    def test_default_size_when_omitted(self, project: Path, capsys) -> None:
        cmd_sessions(_sessions_args(project, size=None))
        assert "Sessions (page 1/1)" in capsys.readouterr().out


class TestErrorsGoToStderr:
    @pytest.fixture
    def project(self, tmp_path: Path) -> Path:
        records = [
            {
                "type": "user",
                "uuid": "u1",
                "parentUuid": None,
                "sessionId": "s1",
                "timestamp": "2026-01-01T10:00:00Z",
                "message": {"content": [{"type": "text", "text": "hi"}]},
            }
        ]
        _write_session(tmp_path, "s1", records)
        return tmp_path

    def test_error_leaves_stdout_clean(self, project: Path, capsys) -> None:
        # Agents parse stdout as data; errors must go to stderr exclusively.
        with pytest.raises(SystemExit):
            cmd_sessions(_sessions_args(project, page=99))
        captured = capsys.readouterr()
        assert "out of range" in captured.err
        assert captured.out == ""


class TestColorDetection:
    """Colors must vanish when output is piped or NO_COLOR is set (agents
    parse stdout; ANSI codes are pure token waste), and stay available via
    FORCE_COLOR. Constants are computed at import, so test via subprocess."""

    def _run_sessions(self, project: Path, env_extra: dict) -> str:
        import os
        import subprocess
        import sys

        env = {**os.environ, **env_extra}
        env.pop("NO_COLOR", None)
        env.pop("FORCE_COLOR", None)
        env.pop("CLICOLOR_FORCE", None)
        env.update(env_extra)
        result = subprocess.run(
            [sys.executable, "-c",
             "from claude_history.cli import main; main()",
             "sessions", "--project", str(project)],
            capture_output=True, text=True, env=env,
        )
        assert result.returncode == 0, result.stderr
        return result.stdout

    @pytest.fixture
    def project(self, tmp_path: Path) -> Path:
        records = [
            {
                "type": "user",
                "uuid": "u1",
                "parentUuid": None,
                "sessionId": "s1",
                "timestamp": "2026-01-01T10:00:00Z",
                "message": {"content": [{"type": "text", "text": "hi"}]},
            },
            {
                "type": "assistant",
                "uuid": "a1",
                "parentUuid": "u1",
                "sessionId": "s1",
                "timestamp": "2026-01-01T10:00:01Z",
                "message": {"content": [{"type": "text", "text": "hello"}]},
            },
        ]
        _write_session(tmp_path, "s1", records)
        return tmp_path

    def test_piped_output_has_no_ansi(self, project: Path) -> None:
        out = self._run_sessions(project, {})
        assert "\x1b[" not in out

    def test_no_color_env_disables_ansi(self, project: Path) -> None:
        out = self._run_sessions(project, {"NO_COLOR": "1", "FORCE_COLOR": "1"})
        assert "\x1b[" not in out

    def test_force_color_enables_ansi(self, project: Path) -> None:
        out = self._run_sessions(project, {"FORCE_COLOR": "1"})
        assert "\x1b[" in out


class TestBrokenPipe:
    def test_pipe_closed_early_exits_141_without_traceback(
        self, tmp_path: Path
    ) -> None:
        import subprocess
        import sys

        # Output must exceed the OS pipe buffer (64KB) so the CLI blocks
        # writing and hits EPIPE when we close the read end early.
        big_text = "x" * 300_000
        records = [
            {
                "type": "user",
                "uuid": "u1",
                "parentUuid": None,
                "sessionId": "abc123de",
                "timestamp": "2026-01-01T10:00:00Z",
                "message": {"content": [{"type": "text", "text": "hi"}]},
            },
            {
                "type": "assistant",
                "uuid": "a1",
                "parentUuid": "u1",
                "sessionId": "abc123de",
                "timestamp": "2026-01-01T10:00:01Z",
                "message": {"content": [{"type": "text", "text": big_text}]},
            },
        ]
        _write_session(tmp_path, "abc123de", records)
        proc = subprocess.Popen(
            [sys.executable, "-c",
             "from claude_history.cli import main; main()",
             "transcript", "abc123de", "--project", str(tmp_path)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        proc.stdout.read(1024)
        proc.stdout.close()
        rc = proc.wait(timeout=30)
        stderr = proc.stderr.read().decode()
        proc.stderr.close()
        assert rc == 141, f"exit {rc}, stderr: {stderr}"
        assert "Traceback" not in stderr
        assert "BrokenPipeError" not in stderr


class TestSearchShowsSessionId:
    def test_match_line_includes_session_id(self, tmp_path: Path, capsys) -> None:
        # SKILL.md's workflow is search -> transcript SESSION; without the
        # session id in results that hand-off is impossible.
        records = [
            {
                "type": "user",
                "uuid": "aaaa1111",
                "parentUuid": None,
                "sessionId": "deadbeef-0000-1111-2222-333344445555",
                "timestamp": "2026-01-01T10:00:00Z",
                "message": {"content": [{"type": "text", "text": "find me quokka"}]},
            },
            {
                "type": "assistant",
                "uuid": "bbbb2222",
                "parentUuid": "aaaa1111",
                "sessionId": "deadbeef-0000-1111-2222-333344445555",
                "timestamp": "2026-01-01T10:00:01Z",
                "message": {"content": [{"type": "text", "text": "found"}]},
            },
        ]
        _write_session(tmp_path, "deadbeef-0000-1111-2222-333344445555", records)
        from claude_history.cli import cmd_search

        args = argparse.Namespace(
            project=str(tmp_path), cwd=None, query="quokka", target="prompts",
            prompts_only=False, responses_only=False, case_sensitive=False,
            timestamps=False, since=None, limit=50,
        )
        cmd_search(args)
        out = capsys.readouterr().out
        assert "s:deadbeef" in out


class TestSubagentDetail:
    def _project(self, tmp_path: Path) -> Path:
        agent = (tmp_path / "11111111-2222-3333-4444-555555555555" / "subagents"
                 / "agent-abc123f.jsonl")
        agent.parent.mkdir(parents=True)
        agent.write_text(json.dumps({
            "type": "user", "uuid": "u1",
            "sessionId": "11111111-2222-3333-4444-555555555555",
            "timestamp": "2020-01-01T00:00:00Z",
            "message": {"role": "user", "content": "do the thing"},
        }) + "\n")
        return tmp_path

    def _args(self, project: Path, **kw) -> argparse.Namespace:
        base = dict(project=str(project), cwd=None, agent_id=None,
                    session=None, since=None)
        base.update(kw)
        return argparse.Namespace(**base)

    def test_detail_ignores_listing_filters(self, tmp_path: Path, capsys) -> None:
        # An old agent must stay reachable by ID even when --since would
        # filter it from the listing (previously: 'not found in any project').
        from claude_history.cli import cmd_subagents

        project = self._project(tmp_path)
        cmd_subagents(self._args(project, agent_id="abc123f", since="1d"))
        out = capsys.readouterr().out
        assert "agent-abc123f.jsonl" in out
        assert "do the thing" in out

    def test_missing_agent_errors_on_stderr(self, tmp_path: Path, capsys) -> None:
        from claude_history.cli import cmd_subagents

        project = self._project(tmp_path)
        with pytest.raises(SystemExit):
            cmd_subagents(self._args(project, agent_id="ffffffff"))
        captured = capsys.readouterr()
        assert "No subagent found" in captured.err
        assert captured.out == ""


class TestSubagentListingPagination:
    def _project(self, tmp_path: Path, count: int) -> Path:
        base = tmp_path / "11111111-2222-3333-4444-555555555555" / "subagents"
        base.mkdir(parents=True)
        for i in range(count):
            (base / f"agent-aa{i:04d}f.jsonl").write_text(json.dumps({
                "type": "user", "uuid": f"u{i}",
                "sessionId": "11111111-2222-3333-4444-555555555555",
                "timestamp": f"2026-01-01T{i:02d}:00:00Z",
                "message": {"role": "user", "content": f"task {i}"},
            }) + "\n")
        return tmp_path

    def _args(self, project: Path, **kw) -> argparse.Namespace:
        base = dict(project=str(project), cwd=None, agent_id=None,
                    session=None, since=None, page=1, size=None)
        base.update(kw)
        return argparse.Namespace(**base)

    def test_listing_capped_at_default_page_size(self, tmp_path: Path, capsys) -> None:
        from claude_history.cli import cmd_subagents

        project = self._project(tmp_path, 23)
        cmd_subagents(self._args(project))
        out = capsys.readouterr().out
        assert "page 1/2, 23 total" in out
        assert out.count("(session:") == 20
        assert "--page 2" in out

    def test_second_page_shows_remainder(self, tmp_path: Path, capsys) -> None:
        from claude_history.cli import cmd_subagents

        project = self._project(tmp_path, 23)
        cmd_subagents(self._args(project, page=2))
        out = capsys.readouterr().out
        assert out.count("(session:") == 3


class TestLocalSessionOutranksForeignSubagent:
    def test_prefix_matching_local_session_skips_cross_project_scan(
        self, tmp_path: Path, capsys, monkeypatch
    ) -> None:
        # A subagent in ANOTHER project sharing the prefix must not shadow a
        # local session.
        projects = tmp_path / "projects"
        local = projects / "-local"
        local.mkdir(parents=True)
        _write_session(local, "abc12345-1111-2222-3333-444455556666", [
            {"type": "user", "uuid": "u1", "parentUuid": None,
             "sessionId": "abc12345-1111-2222-3333-444455556666",
             "timestamp": "2026-01-01T10:00:00Z",
             "message": {"content": [{"type": "text", "text": "local prompt"}]}},
            {"type": "assistant", "uuid": "a1", "parentUuid": "u1",
             "sessionId": "abc12345-1111-2222-3333-444455556666",
             "timestamp": "2026-01-01T10:00:01Z",
             "message": {"content": [{"type": "text", "text": "local answer"}]}},
        ])
        foreign = (projects / "-other" / "99999999-0000-0000-0000-000000000000"
                   / "subagents" / "agent-abc12345678901234.jsonl")
        foreign.parent.mkdir(parents=True)
        foreign.write_text(json.dumps({
            "type": "user", "uuid": "fu1",
            "sessionId": "99999999-0000-0000-0000-000000000000",
            "timestamp": "2026-01-01T10:00:00Z",
            "message": {"role": "user", "content": "FOREIGN AGENT"},
        }) + "\n")
        monkeypatch.setattr("claude_history.resolve.CLAUDE_PROJECTS_DIR", projects)
        from claude_history.cli import cmd_transcript

        args = argparse.Namespace(
            project=str(local), cwd=None, identifier="abc12345",
            prompts_only=False, show_thinking=False, hide_tools=False,
            show_tool_results=False, hide_tool_results=False,
            show_hooks=False, show_system=False,
        )
        cmd_transcript(args)
        out = capsys.readouterr().out
        assert "local prompt" in out
        assert "FOREIGN AGENT" not in out


class TestSubagentSessionRefFilter:
    def test_session_filter_resolves_latest(self, tmp_path: Path, capsys) -> None:
        session = "abc12345-1111-2222-3333-444455556666"
        # Compact JSON: latest/prev resolution greps '"sessionId":"..."'
        (tmp_path / f"{session}.jsonl").write_text(json.dumps(
            {"type": "user", "uuid": "u1", "parentUuid": None,
             "sessionId": session, "timestamp": "2026-01-01T10:00:00Z",
             "message": {"content": [{"type": "text", "text": "hi"}]}},
            separators=(",", ":"),
        ) + "\n")
        agent = tmp_path / session / "subagents" / "agent-def456a.jsonl"
        agent.parent.mkdir(parents=True)
        agent.write_text(json.dumps({
            "type": "user", "uuid": "au1", "sessionId": session,
            "timestamp": "2026-01-01T10:00:00Z",
            "message": {"role": "user", "content": "subtask"},
        }) + "\n")
        from claude_history.cli import cmd_subagents

        args = argparse.Namespace(project=str(tmp_path), cwd=None,
                                  agent_id=None, session="latest", since=None,
                                  page=1, size=None)
        cmd_subagents(args)
        out = capsys.readouterr().out
        assert "def456a" in out


class TestSearchLimit:
    def _project(self, tmp_path: Path, count: int) -> Path:
        records = []
        for i in range(count):
            records.append({
                "type": "user", "uuid": f"u{i:03d}", "parentUuid": None,
                "sessionId": "abc12345-1111-2222-3333-444455556666",
                "timestamp": f"2026-01-01T10:{i:02d}:00Z",
                "message": {"content": [{"type": "text", "text": f"needle {i}"}]},
            })
            records.append({
                "type": "assistant", "uuid": f"a{i:03d}", "parentUuid": f"u{i:03d}",
                "sessionId": "abc12345-1111-2222-3333-444455556666",
                "timestamp": f"2026-01-01T10:{i:02d}:01Z",
                "message": {"content": [{"type": "text", "text": "ok"}]},
            })
        _write_session(tmp_path, "abc12345-1111-2222-3333-444455556666", records)
        return tmp_path

    def _args(self, project: Path, **kw) -> argparse.Namespace:
        base = dict(project=str(project), cwd=None, query="needle",
                    target="prompts", prompts_only=False, responses_only=False,
                    case_sensitive=False, timestamps=False, since=None, limit=50)
        base.update(kw)
        return argparse.Namespace(**base)

    def test_matches_capped_at_limit_newest_first(self, tmp_path: Path, capsys) -> None:
        import re

        from claude_history.cli import cmd_search

        cmd_search(self._args(self._project(tmp_path, 8), limit=3))
        out = re.sub(r"\x1b\[[0-9;]*m", "", capsys.readouterr().out)
        assert "showing newest 3" in out
        assert out.count("[prompt]") == 3
        assert "needle 7" in out  # newest kept
        assert "needle 0" not in out  # oldest dropped

    def test_invalid_limit_rejected(self, tmp_path: Path, capsys) -> None:
        from claude_history.cli import cmd_search

        with pytest.raises(SystemExit):
            cmd_search(self._args(tmp_path, limit=0))
        assert "positive integer" in capsys.readouterr().err


class TestAmbiguityAndErrorClarity:
    def test_ambiguous_session_prefix_warns_on_stderr(
        self, tmp_path: Path, capsys
    ) -> None:
        for sid in ("abc11111-0000-0000-0000-000000000000",
                    "abc22222-0000-0000-0000-000000000000"):
            _write_session(tmp_path, sid, [
                {"type": "user", "uuid": f"u-{sid[:5]}", "parentUuid": None,
                 "sessionId": sid, "timestamp": "2026-01-01T10:00:00Z",
                 "message": {"content": [{"type": "text", "text": "hi"}]}},
                {"type": "assistant", "uuid": f"a-{sid[:5]}",
                 "parentUuid": f"u-{sid[:5]}", "sessionId": sid,
                 "timestamp": "2026-01-01T10:00:01Z",
                 "message": {"content": [{"type": "text", "text": "yo"}]}},
            ])
        from claude_history.cli import cmd_transcript

        args = argparse.Namespace(
            project=str(tmp_path), cwd=None, identifier="abc",
            prompts_only=False, show_thinking=False, hide_tools=False,
            show_tool_results=False, hide_tool_results=False,
            show_hooks=False, show_system=False,
        )
        cmd_transcript(args)
        captured = capsys.readouterr()
        assert "2 sessions match 'abc'" in captured.err

    def test_unreadable_session_file_has_distinct_error(
        self, tmp_path: Path, capsys
    ) -> None:
        (tmp_path / "abc12345-0000-0000-0000-000000000000.jsonl").write_text(
            "not json at all\n"
        )
        from claude_history.cli import cmd_transcript

        args = argparse.Namespace(
            project=str(tmp_path), cwd=None, identifier="abc12345",
            prompts_only=False, show_thinking=False, hide_tools=False,
            show_tool_results=False, hide_tool_results=False,
            show_hooks=False, show_system=False,
        )
        with pytest.raises(SystemExit):
            cmd_transcript(args)
        err = capsys.readouterr().err
        assert "no readable records" in err
