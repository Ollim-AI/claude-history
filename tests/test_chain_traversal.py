"""Tests for get_full_response chain traversal, especially progress stub chains."""

from claude_history.chain import get_full_response, is_user_text_prompt
from claude_history.models import ProgressStub


def _user(uuid: str, parent: str, *, text: str = "", is_tool_result: bool = False) -> dict:
    """Build a minimal user record."""
    content: list | str = [{"type": "text", "text": text}] if text else []
    r: dict = {"type": "user", "uuid": uuid, "parentUuid": parent, "message": {"role": "user", "content": content}}
    if is_tool_result:
        r["sourceToolAssistantUUID"] = parent
    return r


def _assistant(uuid: str, parent: str, *, tool_name: str = "", text: str = "") -> dict:
    """Build a minimal assistant record."""
    content: list = []
    if text:
        content.append({"type": "text", "text": text})
    if tool_name:
        content.append({"type": "tool_use", "id": f"tool_{uuid}", "name": tool_name, "input": {}})
    return {"type": "assistant", "uuid": uuid, "parentUuid": parent, "message": {"role": "assistant", "content": content}}


def _stub(uuid: str, parent: str) -> ProgressStub:
    return ProgressStub(uuid=uuid, parentUuid=parent, parentToolUseID=None, agentId=None)


class TestProgressChainTraversal:
    """Parallel Agent tool calls linked through deep ProgressStub chains."""

    def test_traverses_deep_progress_chain_to_second_agent(self) -> None:
        """Simulates the real structure:
        assistant(text A) -> assistant(tool_use B) -> user(tool_result C) [dead end]
                          -> ProgressStub P1 -> P2 -> P3 -> assistant(tool_use D) -> user(tool_result E) -> assistant(continues F)
        """
        records: list = [
            _user("prompt", "", text="do stuff"),
            _assistant("A", "prompt", text="Starting"),
            _assistant("B", "A", tool_name="Agent"),
            _user("C", "B", is_tool_result=True),
            _stub("P1", "B"),
            _stub("P2", "P1"),
            _stub("P3", "P2"),
            _assistant("D", "P3", tool_name="Agent"),
            _user("E", "D", is_tool_result=True),
            _assistant("F", "E", text="Done"),
        ]
        chain = get_full_response(records, "prompt")
        uuids = [r["uuid"] for r in chain]
        assert uuids == ["A", "B", "D", "F"]

    def test_single_level_progress_sibling_still_works(self) -> None:
        """Original Skill/Task pattern: progress sibling of parent bridges the gap.
        P1 is a sibling of C (both children of B), so the parent-sibling
        fallback finds it.
        """
        records: list = [
            _user("prompt", "", text="run skill"),
            _assistant("A", "prompt", text="OK"),
            _assistant("B", "A", tool_name="Skill"),
            _user("C", "B", is_tool_result=True),
            _stub("P1", "B"),  # sibling of C, child of B
            _assistant("D", "P1", text="continued"),
        ]
        chain = get_full_response(records, "prompt")
        uuids = [r["uuid"] for r in chain]
        assert uuids == ["A", "B", "D"]

    def test_stops_at_user_text_prompt(self) -> None:
        """Chain stops when a real user prompt is encountered."""
        records: list = [
            _user("prompt", "", text="first"),
            _assistant("A", "prompt", text="response"),
            _user("next", "A", text="second prompt"),
        ]
        chain = get_full_response(records, "prompt")
        uuids = [r["uuid"] for r in chain]
        assert uuids == ["A"]


class TestIsUserTextPrompt:
    def test_interrupted_request_is_not_user_prompt(self) -> None:
        record = _user("u1", "a1", text="[Request interrupted by user for tool use]")
        assert is_user_text_prompt(record) is False

    def test_real_text_is_user_prompt(self) -> None:
        record = _user("u1", "a1", text="please fix this")
        assert is_user_text_prompt(record) is True

    def test_tool_result_is_not_user_prompt(self) -> None:
        record = _user("u1", "a1", text="result", is_tool_result=True)
        assert is_user_text_prompt(record) is False
