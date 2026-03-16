"""Tests for agents.py subagent metadata extraction."""

from datetime import datetime, timezone
from pathlib import Path

from claude_history.agents import (
    _count_tokens,
    _extract_error_texts,
    _extract_tools_and_text,
    extract_subagent_metadata,
)


class TestCountTokens:
    def test_basic_usage(self) -> None:
        msg = {"usage": {"input_tokens": 100, "output_tokens": 50}}
        inp, out = _count_tokens(msg)
        assert inp == 100
        assert out == 50

    def test_includes_cache_tokens(self) -> None:
        msg = {
            "usage": {
                "input_tokens": 100,
                "cache_creation_input_tokens": 200,
                "cache_read_input_tokens": 300,
                "output_tokens": 50,
            }
        }
        inp, out = _count_tokens(msg)
        assert inp == 600  # 100 + 200 + 300
        assert out == 50

    def test_missing_usage(self) -> None:
        inp, out = _count_tokens({})
        assert inp == 0
        assert out == 0

    def test_partial_usage(self) -> None:
        msg = {"usage": {"output_tokens": 10}}
        inp, out = _count_tokens(msg)
        assert inp == 0
        assert out == 10


class TestExtractToolsAndText:
    def test_extracts_tool_use(self) -> None:
        msg = {
            "content": [
                {"type": "tool_use", "id": "t1", "name": "Read", "input": {"file_path": "/tmp/x"}},
            ]
        }
        texts: list[str] = []
        tools = _extract_tools_and_text(msg, texts)
        assert len(tools) == 1
        assert tools[0].name == "Read"
        assert tools[0].id == "t1"
        assert "/tmp/x" in tools[0].arg_summary
        assert texts == []

    def test_extracts_text(self) -> None:
        msg = {"content": [{"type": "text", "text": "Hello world"}]}
        texts: list[str] = []
        tools = _extract_tools_and_text(msg, texts)
        assert tools == []
        assert texts == ["Hello world"]

    def test_skips_empty_text(self) -> None:
        msg = {"content": [{"type": "text", "text": "  "}]}
        texts: list[str] = []
        _extract_tools_and_text(msg, texts)
        assert texts == []

    def test_non_list_content(self) -> None:
        msg = {"content": "just a string"}
        texts: list[str] = []
        tools = _extract_tools_and_text(msg, texts)
        assert tools == []
        assert texts == []

    def test_mixed_content(self) -> None:
        msg = {
            "content": [
                {"type": "text", "text": "First"},
                {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "ls"}},
                {"type": "text", "text": "Second"},
            ]
        }
        texts: list[str] = []
        tools = _extract_tools_and_text(msg, texts)
        assert len(tools) == 1
        assert texts == ["First", "Second"]


class TestExtractErrorTexts:
    def test_extracts_error(self) -> None:
        record = {
            "message": {
                "content": [
                    {"type": "tool_result", "is_error": True, "content": "Command failed: exit 1"},
                ]
            }
        }
        errors = _extract_error_texts(record)
        assert len(errors) == 1
        assert "Command failed" in errors[0]

    def test_skips_non_error(self) -> None:
        record = {
            "message": {
                "content": [
                    {"type": "tool_result", "is_error": False, "content": "ok"},
                ]
            }
        }
        assert _extract_error_texts(record) == []

    def test_preserves_full_error(self) -> None:
        record = {
            "message": {
                "content": [
                    {"type": "tool_result", "is_error": True, "content": "x" * 500},
                ]
            }
        }
        errors = _extract_error_texts(record)
        assert len(errors[0]) == 500

    def test_empty_content(self) -> None:
        assert _extract_error_texts({"message": {"content": []}}) == []
        assert _extract_error_texts({}) == []


class TestExtractSubagentMetadata:
    def _make_records(self) -> list[dict]:
        return [
            {
                "type": "user",
                "uuid": "u1",
                "slug": "test-slug",
                "timestamp": "2026-01-01T10:00:00Z",
                "message": {"content": [{"type": "text", "text": "Do something"}]},
            },
            {
                "type": "assistant",
                "uuid": "a1",
                "timestamp": "2026-01-01T10:00:05Z",
                "message": {
                    "model": "claude-opus-4-5-20251101",
                    "content": [
                        {"type": "text", "text": "Working on it"},
                        {"type": "tool_use", "id": "t1", "name": "Read", "input": {"file_path": "/tmp/x"}},
                    ],
                    "usage": {"input_tokens": 100, "output_tokens": 50},
                },
            },
            {
                "type": "user",
                "uuid": "u2",
                "timestamp": "2026-01-01T10:00:10Z",
                "message": {
                    "content": [
                        {"type": "tool_result", "tool_use_id": "t1", "is_error": True, "content": "File not found"},
                    ]
                },
            },
            {
                "type": "assistant",
                "uuid": "a2",
                "timestamp": "2026-01-01T10:00:15Z",
                "message": {
                    "model": "claude-opus-4-5-20251101",
                    "content": [{"type": "text", "text": "Done"}],
                    "usage": {"input_tokens": 200, "output_tokens": 30},
                },
            },
        ]

    def test_basic_metadata(self, tmp_path: Path) -> None:
        session_dir = tmp_path / "session-uuid" / "subagents"
        session_dir.mkdir(parents=True)
        filepath = session_dir / "agent-abc1234.jsonl"
        filepath.touch()

        meta = extract_subagent_metadata(filepath, self._make_records())
        assert meta.agent_id == "abc1234"
        assert meta.session_id == "session-uuid"
        assert meta.slug == "test-slug"
        assert meta.model == "opus"
        assert meta.model_full == "claude-opus-4-5-20251101"
        assert meta.record_count == 4

    def test_prompt_extraction(self, tmp_path: Path) -> None:
        session_dir = tmp_path / "sess" / "subagents"
        session_dir.mkdir(parents=True)
        filepath = session_dir / "agent-x.jsonl"
        filepath.touch()

        meta = extract_subagent_metadata(filepath, self._make_records())
        assert "Do something" in meta.prompt

    def test_token_counting(self, tmp_path: Path) -> None:
        session_dir = tmp_path / "sess" / "subagents"
        session_dir.mkdir(parents=True)
        filepath = session_dir / "agent-x.jsonl"
        filepath.touch()

        meta = extract_subagent_metadata(filepath, self._make_records())
        assert meta.total_input_tokens == 300  # 100 + 200
        assert meta.total_output_tokens == 80  # 50 + 30

    def test_duration(self, tmp_path: Path) -> None:
        session_dir = tmp_path / "sess" / "subagents"
        session_dir.mkdir(parents=True)
        filepath = session_dir / "agent-x.jsonl"
        filepath.touch()

        meta = extract_subagent_metadata(filepath, self._make_records())
        assert meta.duration == 15.0  # 10:00:15 - 10:00:00

    def test_tools_extracted(self, tmp_path: Path) -> None:
        session_dir = tmp_path / "sess" / "subagents"
        session_dir.mkdir(parents=True)
        filepath = session_dir / "agent-x.jsonl"
        filepath.touch()

        meta = extract_subagent_metadata(filepath, self._make_records())
        assert len(meta.tools) == 1
        assert meta.tools[0].name == "Read"

    def test_errors_extracted(self, tmp_path: Path) -> None:
        session_dir = tmp_path / "sess" / "subagents"
        session_dir.mkdir(parents=True)
        filepath = session_dir / "agent-x.jsonl"
        filepath.touch()

        meta = extract_subagent_metadata(filepath, self._make_records())
        assert len(meta.errors) == 1
        assert "File not found" in meta.errors[0]

    def test_empty_records(self, tmp_path: Path) -> None:
        session_dir = tmp_path / "sess" / "subagents"
        session_dir.mkdir(parents=True)
        filepath = session_dir / "agent-x.jsonl"
        filepath.touch()

        meta = extract_subagent_metadata(filepath, [])
        assert meta.record_count == 0
        assert meta.model == "unknown"
        assert meta.duration is None
