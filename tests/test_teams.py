"""Tests for agent teams support in claude-history."""

import json
from pathlib import Path

from claude_history.chain import extract_user_prompts, is_user_text_prompt
from claude_history.models import TeammateMessage, parse_teammate_message

FIXTURE_PATH = Path(__file__).parent / "fixture_team_session.jsonl"


def _load_fixture() -> list[dict]:
    records = []
    with open(FIXTURE_PATH) as f:
        for line in f:
            if line.strip():
                records.append(json.loads(line))
    return records


def _teammate_records(records: list[dict]) -> list[dict]:
    return [
        r
        for r in records
        if r.get("type") == "user"
        and isinstance(r.get("message", {}).get("content", ""), str)
        and "teammate-message" in r["message"]["content"]
    ]


class TestParseTeammateMessage:
    def test_parses_text_message(self) -> None:
        records = _load_fixture()
        text_msgs = [
            r
            for r in _teammate_records(records)
            if "idle_notification" not in r["message"]["content"]
        ]
        assert text_msgs, "fixture must contain a text teammate message"

        tm = parse_teammate_message(text_msgs[0])
        assert tm is not None
        assert isinstance(tm, TeammateMessage)
        assert tm.teammate_id == "critic"
        assert tm.color == "yellow"
        assert tm.summary is not None
        assert tm.body_type == "text"
        assert len(tm.body) > 0

    def test_parses_idle_message(self) -> None:
        records = _load_fixture()
        idle_msgs = [
            r
            for r in _teammate_records(records)
            if "idle_notification" in r["message"]["content"]
        ]
        assert idle_msgs, "fixture must contain an idle teammate message"

        tm = parse_teammate_message(idle_msgs[0])
        assert tm is not None
        assert tm.body_type == "idle"
        assert tm.teammate_id == "critic"

    def test_returns_none_for_normal_record(self) -> None:
        record = {
            "type": "user",
            "message": {
                "role": "user",
                "content": [{"type": "text", "text": "hello"}],
            },
        }
        assert parse_teammate_message(record) is None

    def test_returns_none_for_non_xml_string(self) -> None:
        record = {
            "type": "user",
            "message": {"role": "user", "content": "just a plain string"},
        }
        assert parse_teammate_message(record) is None

    def test_fields_are_accessible(self) -> None:
        records = _load_fixture()
        text_msgs = [
            r
            for r in _teammate_records(records)
            if "idle_notification" not in r["message"]["content"]
        ]
        tm = parse_teammate_message(text_msgs[0])
        assert tm is not None
        # NamedTuple field access
        assert tm.uuid == text_msgs[0].get("uuid", "")
        assert tm.timestamp is not None


class TestExtractUserPrompts:
    def test_excludes_teammate_messages(self) -> None:
        records = _load_fixture()
        prompts = extract_user_prompts(records)
        for p in prompts:
            assert "teammate-message" not in p.text, (
                f"teammate message leaked into prompts: {p.text[:80]}"
            )

    def test_includes_normal_prompts(self) -> None:
        records = _load_fixture()
        prompts = extract_user_prompts(records)
        # Fixture has normal user prompts (Create/Delete the team, etc.)
        assert len(prompts) > 0


class TestIsUserTextPrompt:
    def test_false_for_teammate_message(self) -> None:
        records = _load_fixture()
        for r in _teammate_records(records):
            assert is_user_text_prompt(r) is False

    def test_true_for_normal_prompt(self) -> None:
        record = {
            "type": "user",
            "message": {
                "role": "user",
                "content": [{"type": "text", "text": "hello world"}],
            },
        }
        assert is_user_text_prompt(record) is True

    def test_false_for_tool_result(self) -> None:
        record = {
            "type": "user",
            "sourceToolAssistantUUID": "some-uuid",
            "message": {
                "role": "user",
                "content": [{"type": "text", "text": "tool output"}],
            },
        }
        assert is_user_text_prompt(record) is False


class TestPrefixedTeammateWrapper:
    """Newer clients prefix the wrapper: 'Another Claude session sent a
    message:\\n<teammate-message ...>'. Detection must not rely on startswith,
    or the record renders twice (raw [user] block + parsed teammate block)."""

    def _record(self) -> dict:
        content = (
            "Another Claude session sent a message:\n"
            '<teammate-message teammate_id="lead" color="blue">hello team</teammate-message>\n\n'
            "This came from another Claude session."
        )
        return {
            "type": "user",
            "uuid": "tm1",
            "parentUuid": None,
            "sessionId": "s1",
            "timestamp": "2026-01-01T10:00:00Z",
            "message": {"role": "user", "content": content},
        }

    def test_not_extracted_as_user_prompt(self) -> None:
        from claude_history.chain import extract_user_prompts

        prompts = extract_user_prompts([self._record()])
        assert prompts == []

    def test_not_a_user_text_prompt(self) -> None:
        from claude_history.chain import is_user_text_prompt

        assert not is_user_text_prompt(self._record())

    def test_still_parses_as_teammate_message(self) -> None:
        from claude_history.models import parse_teammate_message

        tm = parse_teammate_message(self._record())
        assert tm is not None
        assert tm.teammate_id == "lead"
        assert tm.body == "hello team"
