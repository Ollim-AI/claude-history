"""Tests for sessions.py — compaction boundaries, context windows, session extraction."""

from datetime import datetime, timezone

from claude_history.models import ProgressStub
from claude_history.sessions import (
    _accumulate_session_record,
    get_compact_boundaries,
    get_compactions,
    get_sessions,
    Session,
)


def _ts(minute: int) -> str:
    return f"2026-01-01T10:{minute:02d}:00Z"


def _dt(minute: int) -> datetime:
    return datetime(2026, 1, 1, 10, minute, tzinfo=timezone.utc)


def _user(uuid: str, parent: str | None, minute: int, *, session_id: str = "s1", text: str = "prompt") -> dict:
    r: dict = {
        "type": "user",
        "uuid": uuid,
        "parentUuid": parent,
        "sessionId": session_id,
        "timestamp": _ts(minute),
        "message": {"content": [{"type": "text", "text": text}]},
    }
    return r


def _assistant(uuid: str, parent: str, minute: int, *, session_id: str = "s1") -> dict:
    return {
        "type": "assistant",
        "uuid": uuid,
        "parentUuid": parent,
        "sessionId": session_id,
        "timestamp": _ts(minute),
        "message": {"content": [{"type": "text", "text": "response"}]},
    }


def _compact_boundary(uuid: str, minute: int, *, session_id: str = "s1") -> dict:
    return {
        "type": "system",
        "subtype": "compact_boundary",
        "uuid": uuid,
        "parentUuid": None,
        "sessionId": session_id,
        "timestamp": _ts(minute),
        "compactMetadata": {"trigger": "auto", "preTokens": 100000},
    }


class TestGetCompactBoundaries:
    def test_finds_explicit_boundaries(self) -> None:
        records: list = [
            _user("u1", None, 0),
            _compact_boundary("cb1", 5),
            _user("u2", "cb1", 6),
        ]
        boundaries = get_compact_boundaries(records, "s1")
        assert len(boundaries) == 1
        assert boundaries[0].uuid == "cb1"
        assert boundaries[0].trigger == "auto"
        assert boundaries[0].pre_tokens == 100000

    def test_filters_by_session(self) -> None:
        records: list = [
            _compact_boundary("cb1", 5, session_id="s1"),
            _compact_boundary("cb2", 10, session_id="s2"),
        ]
        boundaries = get_compact_boundaries(records, "s1")
        assert len(boundaries) == 1

    def test_sorted_by_timestamp(self) -> None:
        records: list = [
            _compact_boundary("cb2", 10),
            _compact_boundary("cb1", 5),
        ]
        boundaries = get_compact_boundaries(records)
        assert boundaries[0].uuid == "cb1"
        assert boundaries[1].uuid == "cb2"

    def test_skips_progress_stubs(self) -> None:
        records: list = [
            ProgressStub(uuid="p1", parentUuid=None, parentToolUseID=None, agentId=None),
            _compact_boundary("cb1", 5),
        ]
        boundaries = get_compact_boundaries(records)
        assert len(boundaries) == 1


class TestGetCompactions:
    def test_single_window(self) -> None:
        records: list = [
            _user("u1", None, 0),
            _assistant("a1", "u1", 1),
            _user("u2", "a1", 2, text="second"),
            _assistant("a2", "u2", 3),
        ]
        windows = get_compactions(records, "s1")
        assert len(windows) == 1
        assert windows[0].prompt_count >= 2

    def test_explicit_boundary_splits_windows(self) -> None:
        records: list = [
            _user("u1", None, 0),
            _assistant("a1", "u1", 1),
            _compact_boundary("cb1", 5),
            _user("u2", "cb1", 6, text="after compaction"),
            _assistant("a2", "u2", 7),
        ]
        windows = get_compactions(records, "s1")
        assert len(windows) == 2

    def test_implicit_boundary_splits_windows(self) -> None:
        """A user record with parentUuid=None after the first one = implicit boundary."""
        records: list = [
            _user("u1", None, 0),
            _assistant("a1", "u1", 1),
            _user("u2", None, 10),  # implicit boundary (second null parent)
            _assistant("a2", "u2", 11),
        ]
        windows = get_compactions(records, "s1")
        assert len(windows) == 2

    def test_empty_session(self) -> None:
        records: list = [_user("u1", None, 0, session_id="other")]
        windows = get_compactions(records, "s1")
        assert windows == []


class TestGetSessions:
    def test_groups_by_session_id(self) -> None:
        records: list = [
            _user("u1", None, 0, session_id="s1"),
            _assistant("a1", "u1", 1, session_id="s1"),
            _user("u2", None, 5, session_id="s2"),
            _assistant("a2", "u2", 6, session_id="s2"),
        ]
        sessions = get_sessions(records)
        assert len(sessions) == 2

    def test_sorted_by_latest_timestamp(self) -> None:
        records: list = [
            _user("u1", None, 0, session_id="s1"),
            _assistant("a1", "u1", 1, session_id="s1"),
            _user("u2", None, 10, session_id="s2"),
            _assistant("a2", "u2", 11, session_id="s2"),
        ]
        sessions = get_sessions(records)
        assert sessions[0].session_id == "s2"  # more recent

    def test_counts_prompts(self) -> None:
        records: list = [
            _user("u1", None, 0),
            _assistant("a1", "u1", 1),
            _user("u2", "a1", 2, text="second"),
            _assistant("a2", "u2", 3),
        ]
        sessions = get_sessions(records)
        assert sessions[0].prompt_count == 2

    def test_skips_tool_results(self) -> None:
        records: list = [
            _user("u1", None, 0),
            _assistant("a1", "u1", 1),
            {
                "type": "user",
                "uuid": "tr1",
                "parentUuid": "a1",
                "sessionId": "s1",
                "timestamp": _ts(2),
                "sourceToolAssistantUUID": "a1",
                "message": {"content": [{"type": "text", "text": "tool result"}]},
            },
        ]
        sessions = get_sessions(records)
        assert sessions[0].prompt_count == 1

    def test_skips_progress_stubs(self) -> None:
        records: list = [
            _user("u1", None, 0),
            ProgressStub(uuid="p1", parentUuid="u1", parentToolUseID=None, agentId=None),
        ]
        sessions = get_sessions(records)
        assert len(sessions) == 1

    def test_captures_first_prompt(self) -> None:
        records: list = [
            _user("u1", None, 0, text="hello world"),
            _assistant("a1", "u1", 1),
        ]
        sessions = get_sessions(records)
        assert sessions[0].first_prompt is not None
        assert "hello world" in sessions[0].first_prompt[1]

    def test_tracks_team_names(self) -> None:
        records: list = [
            {**_user("u1", None, 0), "teamName": "my-team"},
            _assistant("a1", "u1", 1),
        ]
        sessions = get_sessions(records)
        assert "my-team" in sessions[0].team_names

    def test_zero_prompt_session_has_timestamp(self) -> None:
        # Bug 1: a session with no user-text prompt (only assistant/subagent
        # activity) must still get a latest_timestamp, not None -> "unknown".
        records: list = [
            _assistant("a1", "root", 5, session_id="s1"),
            _assistant("a2", "a1", 40, session_id="s1"),
        ]
        sessions = get_sessions(records)
        assert len(sessions) == 1
        assert sessions[0].prompt_count == 0
        assert sessions[0].latest_timestamp == _dt(40)

    def test_latest_timestamp_is_last_activity_not_last_prompt(self) -> None:
        # Bug 1: last activity should reflect the max timestamp across ALL
        # records, not the timestamp of the last user-text prompt.
        records: list = [
            _user("u1", None, 0, session_id="s1"),
            _assistant("a1", "u1", 30, session_id="s1"),
        ]
        sessions = get_sessions(records)
        assert sessions[0].prompt_count == 1
        assert sessions[0].latest_timestamp == _dt(30)

    def test_zero_prompt_session_sorts_by_activity(self) -> None:
        # Bug 1: a 0-prompt session that ran later must sort above an earlier
        # prompted session rather than being dumped at the bottom under DT_MIN.
        records: list = [
            _user("u1", None, 0, session_id="prompted"),
            _assistant("a1", "u1", 1, session_id="prompted"),
            _assistant("a2", "root", 50, session_id="silent"),
        ]
        sessions = get_sessions(records)
        assert sessions[0].session_id == "silent"


class TestAccumulateSessionRecord:
    def test_tracks_explicit_boundary(self) -> None:
        sess = Session(session_id="s1")
        _accumulate_session_record(sess, _compact_boundary("cb1", 5, session_id="s1"))
        assert len(sess.explicit_boundaries) == 1

    def test_tracks_implicit_boundary(self) -> None:
        sess = Session(session_id="s1")
        _accumulate_session_record(sess, _user("u1", None, 0))
        assert len(sess.implicit_boundaries) == 1

    def test_captures_slug(self) -> None:
        sess = Session(session_id="s1")
        record = {**_user("u1", None, 0), "slug": "test-slug"}
        _accumulate_session_record(sess, record)
        assert sess.slug == "test-slug"

    def test_skips_compact_summary(self) -> None:
        sess = Session(session_id="s1")
        record = {**_user("u1", None, 0), "isCompactSummary": True}
        _accumulate_session_record(sess, record)
        assert sess.prompt_count == 0
