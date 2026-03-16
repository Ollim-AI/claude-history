"""Tests for chain.py functions not covered by test_chain_traversal.py."""

from claude_history.chain import (
    build_notification_map,
    build_task_agent_map,
    extract_all_text,
    extract_all_thinking,
    extract_all_tools,
    extract_ordered_content,
    extract_text_from_response,
    extract_thinking_from_response,
    extract_tools_from_response,
    find_response_for_prompt,
)
from claude_history.models import ProgressStub, ToolUseContent


def _assistant(uuid: str, parent: str, *, text: str = "", tool_name: str = "", thinking: str = "") -> dict:
    content: list = []
    if thinking:
        content.append({"type": "thinking", "thinking": thinking})
    if text:
        content.append({"type": "text", "text": text})
    if tool_name:
        content.append({"type": "tool_use", "id": f"tool_{uuid}", "name": tool_name, "input": {}})
    return {"type": "assistant", "uuid": uuid, "parentUuid": parent, "message": {"role": "assistant", "content": content}}


def _user(uuid: str, parent: str, *, text: str = "", is_tool_result: bool = False) -> dict:
    content: list = [{"type": "text", "text": text}] if text else []
    r: dict = {"type": "user", "uuid": uuid, "parentUuid": parent, "message": {"role": "user", "content": content}}
    if is_tool_result:
        r["sourceToolAssistantUUID"] = parent
    return r


class TestFindResponseForPrompt:
    def test_finds_direct_child(self) -> None:
        records: list = [
            _user("u1", "", text="hello"),
            _assistant("a1", "u1", text="world"),
        ]
        result = find_response_for_prompt(records, "u1")
        assert result is not None
        assert result["uuid"] == "a1"

    def test_returns_none_when_no_response(self) -> None:
        records: list = [_user("u1", "", text="hello")]
        assert find_response_for_prompt(records, "u1") is None

    def test_skips_progress_stubs(self) -> None:
        records: list = [
            _user("u1", "", text="hello"),
            ProgressStub(uuid="p1", parentUuid="u1", parentToolUseID=None, agentId=None),
            _assistant("a1", "u1", text="world"),
        ]
        result = find_response_for_prompt(records, "u1")
        assert result is not None
        assert result["uuid"] == "a1"


class TestExtractTextFromResponse:
    def test_extracts_text_blocks(self) -> None:
        record = _assistant("a1", "u1", text="hello world")
        assert extract_text_from_response(record) == "hello world"

    def test_string_content(self) -> None:
        record = {"message": {"content": "just a string"}}
        assert extract_text_from_response(record) == "just a string"

    def test_empty_content(self) -> None:
        record = {"message": {"content": []}}
        assert extract_text_from_response(record) == ""

    def test_multiple_text_blocks(self) -> None:
        record = {"message": {"content": [
            {"type": "text", "text": "first"},
            {"type": "tool_use", "id": "t1", "name": "X", "input": {}},
            {"type": "text", "text": "second"},
        ]}}
        result = extract_text_from_response(record)
        assert "first" in result
        assert "second" in result


class TestExtractToolsFromResponse:
    def test_extracts_tool_use(self) -> None:
        record = _assistant("a1", "u1", tool_name="Read")
        tools = extract_tools_from_response(record)
        assert len(tools) == 1
        assert tools[0]["name"] == "Read"

    def test_no_tools(self) -> None:
        record = _assistant("a1", "u1", text="just text")
        assert extract_tools_from_response(record) == []

    def test_string_content_returns_empty(self) -> None:
        record = {"message": {"content": "string"}}
        assert extract_tools_from_response(record) == []


class TestExtractThinkingFromResponse:
    def test_extracts_thinking(self) -> None:
        record = _assistant("a1", "u1", thinking="Let me think...")
        result = extract_thinking_from_response(record)
        assert result == ["Let me think..."]

    def test_no_thinking(self) -> None:
        record = _assistant("a1", "u1", text="just text")
        assert extract_thinking_from_response(record) == []


class TestExtractAll:
    def test_extract_all_text(self) -> None:
        chain = [
            _assistant("a1", "u1", text="first"),
            _assistant("a2", "a1", text="second"),
        ]
        result = extract_all_text(chain)
        assert "first" in result
        assert "second" in result

    def test_extract_all_tools(self) -> None:
        chain = [
            _assistant("a1", "u1", tool_name="Read"),
            _assistant("a2", "a1", tool_name="Bash"),
        ]
        tools = extract_all_tools(chain)
        assert len(tools) == 2
        names = [t["name"] for t in tools]
        assert "Read" in names
        assert "Bash" in names

    def test_extract_all_thinking(self) -> None:
        chain = [
            _assistant("a1", "u1", thinking="thought 1"),
            _assistant("a2", "a1", thinking="thought 2"),
        ]
        result = extract_all_thinking(chain)
        assert len(result) == 2


class TestExtractOrderedContent:
    def test_text_and_tool_blocks(self) -> None:
        chain = [
            _assistant("a1", "u1", text="hello"),
            _assistant("a2", "a1", tool_name="Read"),
        ]
        blocks = extract_ordered_content(chain)
        types = [b.type for b in blocks]
        assert "text" in types
        assert "tool_use" in types

    def test_thinking_blocks_included(self) -> None:
        chain = [_assistant("a1", "u1", thinking="hmm", text="ok")]
        blocks = extract_ordered_content(chain)
        types = [b.type for b in blocks]
        assert "thinking" in types
        assert "text" in types

    def test_tool_use_content_is_typed(self) -> None:
        chain = [_assistant("a1", "u1", tool_name="Bash")]
        blocks = extract_ordered_content(chain)
        tool_blocks = [b for b in blocks if b.type == "tool_use"]
        assert len(tool_blocks) == 1
        assert isinstance(tool_blocks[0].content, ToolUseContent)
        assert tool_blocks[0].content.name == "Bash"

    def test_splits_thinking_tags_in_text(self) -> None:
        record = {"type": "assistant", "uuid": "a1", "parentUuid": "u1", "message": {
            "content": [{"type": "text", "text": "before <thinking>inner</thinking> after"}]
        }}
        blocks = extract_ordered_content([record])
        types = [b.type for b in blocks]
        assert "thinking" in types
        assert "text" in types


class TestBuildNotificationMap:
    def test_builds_map_from_task_notifications(self) -> None:
        records: list = [
            {
                "type": "user",
                "uuid": "n1",
                "message": {
                    "content": "<task-notification><task-id>abc</task-id><status>completed</status><summary>Done</summary><result>All good</result></task-notification>"
                },
            }
        ]
        notif_map = build_notification_map(records)
        assert "n1" in notif_map
        assert notif_map["n1"].task_id == "abc"

    def test_skips_non_notification_records(self) -> None:
        records: list = [
            {"type": "user", "uuid": "u1", "message": {"content": [{"type": "text", "text": "hello"}]}},
        ]
        assert build_notification_map(records) == {}

    def test_skips_progress_stubs(self) -> None:
        records: list = [
            ProgressStub(uuid="p1", parentUuid=None, parentToolUseID=None, agentId=None),
        ]
        assert build_notification_map(records) == {}


class TestBuildTaskAgentMap:
    def test_maps_tool_use_to_agent(self) -> None:
        records: list = [
            ProgressStub(uuid="p1", parentUuid=None, parentToolUseID="toolu_123", agentId="abc1234"),
        ]
        mapping = build_task_agent_map(records)
        assert mapping["toolu_123"] == "abc1234"

    def test_skips_non_stubs(self) -> None:
        records: list = [
            {"type": "assistant", "uuid": "a1", "parentUuid": "u1"},
        ]
        assert build_task_agent_map(records) == {}

    def test_first_mapping_wins(self) -> None:
        records: list = [
            ProgressStub(uuid="p1", parentUuid=None, parentToolUseID="toolu_1", agentId="first"),
            ProgressStub(uuid="p2", parentUuid=None, parentToolUseID="toolu_1", agentId="second"),
        ]
        mapping = build_task_agent_map(records)
        assert mapping["toolu_1"] == "first"
