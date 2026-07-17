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
