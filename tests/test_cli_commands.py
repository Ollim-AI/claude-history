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
            timestamps=False, since=None,
        )
        cmd_search(args)
        out = capsys.readouterr().out
        assert "s:deadbeef" in out
