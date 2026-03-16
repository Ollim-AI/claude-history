"""Tests for subagent search to prevent memory regressions."""

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

from claude_history.agents import search_subagent_files
from claude_history.cli import prefilter_files


def _write_subagent_file(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")


def _make_subagent_records(
    text: str = "hello world",
    session_id: str = "sess-1",
    timestamp: str = "2026-01-01T10:00:00Z",
) -> list[dict]:
    return [
        {
            "type": "user",
            "uuid": "u1",
            "sessionId": session_id,
            "timestamp": timestamp,
            "message": {"content": text},
        },
        {
            "type": "assistant",
            "uuid": "a1",
            "sessionId": session_id,
            "timestamp": timestamp,
            "message": {
                "model": "claude-haiku-4-5-20251001",
                "content": [{"type": "text", "text": f"response about {text}"}],
            },
        },
    ]


class TestSearchSubagentFiles:
    def test_finds_match_in_prompt(self, tmp_path: Path) -> None:
        f = tmp_path / "agent-abc.jsonl"
        _write_subagent_file(f, _make_subagent_records("find this keyword"))
        matches = search_subagent_files([f], "keyword", case_sensitive=False)
        assert len(matches) == 1
        assert matches[0].uuid == "abc"
        assert matches[0].type == "subagent"

    def test_finds_match_in_response(self, tmp_path: Path) -> None:
        f = tmp_path / "agent-def.jsonl"
        _write_subagent_file(f, _make_subagent_records("something"))
        matches = search_subagent_files([f], "response about", case_sensitive=False)
        assert len(matches) == 1

    def test_no_match(self, tmp_path: Path) -> None:
        f = tmp_path / "agent-ghi.jsonl"
        _write_subagent_file(f, _make_subagent_records("nothing here"))
        matches = search_subagent_files([f], "nonexistent", case_sensitive=False)
        assert len(matches) == 0

    def test_case_insensitive(self, tmp_path: Path) -> None:
        f = tmp_path / "agent-x.jsonl"
        _write_subagent_file(f, _make_subagent_records("Hello World"))
        matches = search_subagent_files([f], "hello world", case_sensitive=False)
        assert len(matches) == 1

    def test_case_sensitive(self, tmp_path: Path) -> None:
        f = tmp_path / "agent-x.jsonl"
        _write_subagent_file(f, _make_subagent_records("Hello World"))
        assert search_subagent_files([f], "hello world", case_sensitive=True) == []
        assert len(search_subagent_files([f], "Hello World", case_sensitive=True)) == 1

    def test_stores_snippet_not_full_text(self, tmp_path: Path) -> None:
        """Match text should be the individual record's text, not all records concatenated."""
        records = _make_subagent_records("prompt text")
        # Add many large assistant records that DON'T contain the query
        for i in range(100):
            records.append({
                "type": "assistant",
                "uuid": f"a{i + 2}",
                "sessionId": "sess-1",
                "timestamp": "2026-01-01T10:00:00Z",
                "message": {
                    "model": "claude-haiku-4-5-20251001",
                    "content": [{"type": "text", "text": "x" * 10000}],
                },
            })
        f = tmp_path / "agent-big.jsonl"
        _write_subagent_file(f, records)
        matches = search_subagent_files([f], "prompt text", case_sensitive=False)
        assert len(matches) == 1
        # Match text should be just the matching record, not 100 * 10KB concatenated
        assert len(matches[0].text) < 1000

    def test_extracts_metadata(self, tmp_path: Path) -> None:
        f = tmp_path / "agent-meta123.jsonl"
        _write_subagent_file(
            f, _make_subagent_records("query", session_id="sess-42", timestamp="2026-03-15T08:00:00Z")
        )
        matches = search_subagent_files([f], "query", case_sensitive=False)
        assert matches[0].session_id == "sess-42"
        assert matches[0].timestamp == datetime(2026, 3, 15, 8, 0, tzinfo=timezone.utc)

    def test_multiple_files(self, tmp_path: Path) -> None:
        f1 = tmp_path / "agent-a.jsonl"
        f2 = tmp_path / "agent-b.jsonl"
        f3 = tmp_path / "agent-c.jsonl"
        _write_subagent_file(f1, _make_subagent_records("match here"))
        _write_subagent_file(f2, _make_subagent_records("no match"))
        _write_subagent_file(f3, _make_subagent_records("match here too"))
        matches = search_subagent_files([f1, f2, f3], "match here", case_sensitive=False)
        assert len(matches) == 2


class TestPrefilterFilesMtime:
    def test_skips_old_files(self, tmp_path: Path) -> None:
        """Files older than since_dt should not be scanned."""
        # Create a session file
        old_file = tmp_path / "old-session.jsonl"
        old_file.write_text('{"type":"user","message":{"content":"findme"}}\n')
        # Set mtime to 2 days ago
        old_mtime = time.time() - 2 * 86400
        os.utime(old_file, (old_mtime, old_mtime))

        new_file = tmp_path / "new-session.jsonl"
        new_file.write_text('{"type":"user","message":{"content":"findme"}}\n')

        since = datetime.fromtimestamp(time.time() - 86400, tz=timezone.utc)
        result = prefilter_files(tmp_path, "findme", since_dt=since)
        assert new_file in result
        assert old_file not in result

    def test_no_since_includes_all(self, tmp_path: Path) -> None:
        f = tmp_path / "session.jsonl"
        f.write_text('{"type":"user","message":{"content":"findme"}}\n')
        result = prefilter_files(tmp_path, "findme", since_dt=None)
        assert f in result

    def test_skips_old_subagent_files(self, tmp_path: Path) -> None:
        """Subagent files older than since_dt should not be scanned."""
        subdir = tmp_path / "sess-uuid" / "subagents"
        subdir.mkdir(parents=True)
        old_agent = subdir / "agent-old.jsonl"
        old_agent.write_text('{"type":"user","message":{"content":"findme"}}\n')
        old_mtime = time.time() - 2 * 86400
        os.utime(old_agent, (old_mtime, old_mtime))

        new_agent = subdir / "agent-new.jsonl"
        new_agent.write_text('{"type":"user","message":{"content":"findme"}}\n')

        since = datetime.fromtimestamp(time.time() - 86400, tz=timezone.utc)
        result = prefilter_files(tmp_path, "findme", since_dt=since)
        assert new_agent in result
        assert old_agent not in result
