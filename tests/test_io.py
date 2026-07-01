"""Tests for io.py file parsing functions."""

import json
from pathlib import Path

from claude_history.io import (
    _extract_progress_stub,
    get_all_conversations,
    get_session_conversations,
    parse_jsonl_file,
    parse_subagent_file,
)
from claude_history.models import ProgressStub


def _compact(obj: dict) -> str:
    """JSON with no spaces — matches Claude Code's JSONL output format."""
    return json.dumps(obj, separators=(",", ":"))


def _progress_line(uuid: str, parent: str, agent_id: str = "abc1234") -> str:
    """Build a raw JSONL line mimicking real Claude Code progress record layout.

    The function under test (_extract_progress_stub) relies on:
    - No spaces in key:value (compact JSON)
    - parentUuid in first 200 chars
    - parentToolUseID and agentId in last 350 chars
    - Top-level "uuid" is the LAST occurrence (after nested data.message.uuid)
    """
    # Build manually to control field order and ensure top-level uuid comes last
    return (
        '{"type":"progress"'
        f',"parentUuid":"{parent}"'
        ',"sessionId":"sess1"'
        ',"timestamp":"2026-01-01T00:00:00Z"'
        ',"data":{"type":"agent_progress"'
        f',"agentId":"{agent_id}"'
        ',"message":{"uuid":"nested-msg-uuid"}}'
        f',"parentToolUseID":"toolu_xyz"'
        f',"uuid":"{uuid}"'
        "}"
    )


class TestExtractProgressStub:
    def test_extracts_uuid_and_parent(self) -> None:
        line = _progress_line("top-uuid", "parent-uuid")
        stub = _extract_progress_stub(line)
        assert stub is not None
        assert stub.uuid == "top-uuid"
        assert stub.parentUuid == "parent-uuid"

    def test_extracts_agent_id(self) -> None:
        line = _progress_line("u1", "p1", "agent123")
        stub = _extract_progress_stub(line)
        assert stub is not None
        assert stub.agentId == "agent123"

    def test_extracts_parent_tool_use_id(self) -> None:
        line = _progress_line("u1", "p1")
        stub = _extract_progress_stub(line)
        assert stub is not None
        assert stub.parentToolUseID == "toolu_xyz"

    def test_returns_none_for_no_uuid(self) -> None:
        assert _extract_progress_stub('{"type":"progress"}') is None

    def test_uses_last_uuid_occurrence(self) -> None:
        """The top-level uuid should be the LAST occurrence (after nested data.message.uuid)."""
        line = _progress_line("top-level", "p1")
        stub = _extract_progress_stub(line)
        assert stub is not None
        # Should get the top-level uuid, not the nested one
        assert stub.uuid == "top-level"


class TestParseJsonlFile:
    def test_parses_non_progress_records(self, tmp_path: Path) -> None:
        f = tmp_path / "test.jsonl"
        lines = [
            _compact({"type": "user", "uuid": "u1", "sessionId": "s1"}),
            _compact({"type": "assistant", "uuid": "a1", "sessionId": "s1"}),
        ]
        f.write_text("\n".join(lines) + "\n")

        result = parse_jsonl_file(f, include_progress_stubs=False)
        assert len(result) == 2
        assert result[0]["type"] == "user"
        assert result[1]["type"] == "assistant"

    def test_filters_progress_records(self, tmp_path: Path) -> None:
        f = tmp_path / "test.jsonl"
        lines = [
            _compact({"type": "user", "uuid": "u1"}),
            _progress_line("p1", "u1"),
            _compact({"type": "assistant", "uuid": "a1"}),
        ]
        f.write_text("\n".join(lines) + "\n")

        result = parse_jsonl_file(f, include_progress_stubs=False)
        types = [r["type"] for r in result if isinstance(r, dict)]
        assert "progress" not in types
        assert len(types) == 2

    def test_includes_progress_stubs_when_requested(self, tmp_path: Path) -> None:
        f = tmp_path / "test.jsonl"
        lines = [
            _compact({"type": "user", "uuid": "u1"}),
            _progress_line("p1", "u1"),
        ]
        f.write_text("\n".join(lines) + "\n")

        result = parse_jsonl_file(f, include_progress_stubs=True)
        stubs = [r for r in result if isinstance(r, ProgressStub)]
        assert len(stubs) == 1
        assert stubs[0].uuid == "p1"

    def test_skips_malformed_json(self, tmp_path: Path) -> None:
        f = tmp_path / "test.jsonl"
        f.write_text('{"type":"user","uuid":"u1"}\nnot json\n{"type":"assistant","uuid":"a1"}\n')

        result = parse_jsonl_file(f, include_progress_stubs=False)
        assert len(result) == 2

    def test_empty_file(self, tmp_path: Path) -> None:
        f = tmp_path / "test.jsonl"
        f.write_text("")
        result = parse_jsonl_file(f)
        assert result == []

    def test_blank_lines_skipped(self, tmp_path: Path) -> None:
        f = tmp_path / "test.jsonl"
        f.write_text('\n\n{"type":"user","uuid":"u1"}\n\n')
        result = parse_jsonl_file(f, include_progress_stubs=False)
        assert len(result) == 1

    def test_unicode_line_separator_not_over_split(self, tmp_path: Path) -> None:
        # Bug 7: str.splitlines() splits on U+2028/U+2029/U+0085 (valid,
        # unescaped inside JSON strings — Node's JSON.stringify leaves them
        # literal), fragmenting one record into malformed pieces. Splitting
        # only on "\n" preserves the record.
        f = tmp_path / "test.jsonl"
        for sep in (chr(0x2028), chr(0x2029), chr(0x0085)):
            rec = {
                "type": "assistant",
                "uuid": "u1",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": f"a{sep}b SECRET"}],
                },
            }
            f.write_text(json.dumps(rec, ensure_ascii=False) + "\n", encoding="utf-8")
            result = parse_jsonl_file(f, include_progress_stubs=False)
            assert len(result) == 1, f"lost record for {sep!r}"
            assert result[0]["uuid"] == "u1"


class TestParseSubagentFile:
    def test_parses_all_records_including_progress(self, tmp_path: Path) -> None:
        f = tmp_path / "agent-abc1234.jsonl"
        records = [
            {"type": "user", "uuid": "u1"},
            {"type": "progress", "uuid": "p1", "data": {"type": "agent_progress"}},
            {"type": "assistant", "uuid": "a1"},
        ]
        f.write_text("\n".join(json.dumps(r) for r in records) + "\n")

        result = parse_subagent_file(f)
        assert len(result) == 3
        types = [r["type"] for r in result]
        assert "progress" in types

    def test_handles_missing_file(self, tmp_path: Path) -> None:
        f = tmp_path / "nonexistent.jsonl"
        result = parse_subagent_file(f)
        assert result == []

    def test_skips_malformed_lines(self, tmp_path: Path) -> None:
        f = tmp_path / "agent.jsonl"
        f.write_text('{"type":"user"}\nbad line\n{"type":"assistant"}\n')
        result = parse_subagent_file(f)
        assert len(result) == 2


class TestGetAllConversations:
    def test_loads_all_jsonl_files(self, tmp_path: Path) -> None:
        for name, sid in [("a.jsonl", "s1"), ("b.jsonl", "s2")]:
            f = tmp_path / name
            f.write_text(json.dumps({"type": "user", "uuid": f"u-{sid}", "sessionId": sid}) + "\n")

        result = get_all_conversations(tmp_path, include_progress_stubs=False)
        assert len(result) == 2
        session_ids = {r["sessionId"] for r in result if isinstance(r, dict)}
        assert session_ids == {"s1", "s2"}

    def test_adds_source_file(self, tmp_path: Path) -> None:
        f = tmp_path / "test.jsonl"
        f.write_text(json.dumps({"type": "user", "uuid": "u1"}) + "\n")

        result = get_all_conversations(tmp_path, include_progress_stubs=False)
        assert result[0]["_source_file"] == "test.jsonl"

    def test_empty_directory(self, tmp_path: Path) -> None:
        result = get_all_conversations(tmp_path)
        assert result == []


class TestGetSessionConversations:
    def test_finds_file_by_prefix(self, tmp_path: Path) -> None:
        f = tmp_path / "abc12345-full-uuid.jsonl"
        f.write_text(json.dumps({"type": "user", "uuid": "u1"}) + "\n")

        result = get_session_conversations(tmp_path, "abc12345", include_progress_stubs=False)
        assert result is not None
        assert len(result) == 1

    def test_returns_none_when_not_found(self, tmp_path: Path) -> None:
        result = get_session_conversations(tmp_path, "nonexistent")
        assert result is None

    def test_adds_source_file(self, tmp_path: Path) -> None:
        f = tmp_path / "abc123.jsonl"
        f.write_text(json.dumps({"type": "user", "uuid": "u1"}) + "\n")

        result = get_session_conversations(tmp_path, "abc123", include_progress_stubs=False)
        assert result is not None
        assert result[0]["_source_file"] == "abc123.jsonl"
