"""Tests for get_full_response chain traversal, especially progress stub chains."""

import time

from claude_history.chain import extract_user_prompts, get_full_response, is_user_text_prompt
from claude_history.io import parse_jsonl_file
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

    def test_parallel_tools_with_grandparent_progress_fork(self) -> None:
        """Parallel tool calls where the progress fork is on a grandparent.

        Real pattern from subagent files:
        assistant(tool_use B) -> user(tool_result C) -> ProgressStub(hook) [dead end]
                              -> ProgressStub P1 -> P2 -> assistant(tool_use D) -> user(tool_result E)
        The dead end at C's hook_progress child requires walking up TWO levels
        (C -> B) to find the progress fork P1.
        """
        records: list = [
            _user("prompt", "", text="do stuff"),
            _assistant("A", "prompt", text="Starting"),
            _assistant("B", "A", tool_name="Read"),
            _stub("P1", "B"),         # progress fork starts at B
            _stub("P2", "P1"),        # -> leads to parallel tool_use D
            _assistant("D", "P2", tool_name="Grep"),
            _user("C", "B", is_tool_result=True),   # tool_result for B
            _stub("H1", "C"),         # hook_progress child of C (dead end)
            _user("E", "D", is_tool_result=True),    # tool_result for D
            _assistant("F", "E", text="Done"),
        ]
        chain = get_full_response(records, "prompt")
        uuids = [r["uuid"] for r in chain]
        assert uuids == ["A", "B", "D", "F"]

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

    def test_string_content_is_user_prompt(self) -> None:
        # Bug 5: the dominant real format is plain-string message.content.
        record = {"type": "user", "uuid": "u1", "message": {"content": "yes"}}
        assert is_user_text_prompt(record) is True

    def test_string_content_wrapper_is_not_user_prompt(self) -> None:
        # Bug 5: system-injected wrappers (any '<'-prefixed string) excluded.
        for wrapper in (
            "<command-message>foo</command-message>",
            "<task-notification>x</task-notification>",
            "<bash-input>ls</bash-input>",
            "[Request interrupted by user]",
            "   ",
        ):
            record = {"type": "user", "uuid": "u1", "message": {"content": wrapper}}
            assert is_user_text_prompt(record) is False, wrapper


class TestGetFullResponseStringPromptBoundary:
    def test_string_prompt_stops_chain(self) -> None:
        # Bug 5: a plain-string user prompt is a turn boundary; without the fix
        # get_full_response would swallow the next turn's assistant reply.
        records = [
            {"type": "user", "uuid": "p1", "parentUuid": None,
             "message": {"content": [{"type": "text", "text": "first question"}]}},
            _assistant("a1", "p1", text="answer one"),
            {"type": "user", "uuid": "p2", "parentUuid": "a1",
             "message": {"content": "yes"}},
            _assistant("a2", "p2", text="answer two"),
        ]
        chain = get_full_response(records, "p1")
        assert [r["uuid"] for r in chain] == ["a1"]


# -- Performance tests against real session files --

_SESSIONS = {
    "parallel_agents": "2d32a582-102d-4462-bbbe-8d9ecd53dc45",
    "large": "f2c7dc51-6f01-4451-ab65-f79d3320ce0d",
    "medium": "0aa3bb6d-a9ea-46ea-b020-9fb8d181f15b",
}
_PROJECT_DIR = "/home/julius/.claude/projects/-home-julius-ollim-bot-docs"


def _load_and_traverse(session_id: str) -> tuple[float, int]:
    """Load a session and traverse all prompt chains. Returns (seconds, chain_records)."""
    from pathlib import Path

    filepath = Path(_PROJECT_DIR) / f"{session_id}.jsonl"
    if not filepath.exists():
        return 0.0, 0
    records = parse_jsonl_file(filepath, include_progress_stubs=True)
    prompts = [p for p in extract_user_prompts(records) if p.is_user_prompt]
    start = time.perf_counter()
    total_chain = 0
    for p in prompts:
        chain = get_full_response(records, p.uuid)
        total_chain += len(chain)
    elapsed = time.perf_counter() - start
    return elapsed, total_chain


class TestPerformance:
    """Ensure chain traversal stays fast on real session files."""

    def test_parallel_agents_session_under_50ms(self) -> None:
        elapsed, chain_len = _load_and_traverse(_SESSIONS["parallel_agents"])
        if chain_len == 0:
            return  # file not available
        assert chain_len > 10, f"expected substantial chain, got {chain_len}"
        assert elapsed < 0.05, f"took {elapsed:.3f}s, expected <50ms"

    def test_large_session_under_200ms(self) -> None:
        elapsed, chain_len = _load_and_traverse(_SESSIONS["large"])
        if chain_len == 0:
            return
        assert elapsed < 0.2, f"took {elapsed:.3f}s, expected <200ms"

    def test_medium_session_under_100ms(self) -> None:
        elapsed, chain_len = _load_and_traverse(_SESSIONS["medium"])
        if chain_len == 0:
            return
        assert elapsed < 0.1, f"took {elapsed:.3f}s, expected <100ms"

    def test_deep_progress_chain_linear_not_quadratic(self) -> None:
        """Verify traversal through N progress stubs is O(N), not O(N^2).
        Build chains of 100 and 1000 stubs; ratio should be ~10x, not ~100x.
        """
        def build_chain(depth: int) -> list:
            records: list = [
                _user("prompt", "", text="go"),
                _assistant("A", "prompt", tool_name="Agent"),
                _user("C", "A", is_tool_result=True),
            ]
            parent = "A"
            for i in range(depth):
                records.append(_stub(f"P{i}", parent))
                parent = f"P{i}"
            records.append(_assistant("Z", parent, text="done"))
            return records

        # Warm up
        get_full_response(build_chain(10), "prompt")

        r100 = build_chain(100)
        start = time.perf_counter()
        for _ in range(50):
            get_full_response(r100, "prompt")
        t100 = time.perf_counter() - start

        r1000 = build_chain(1000)
        start = time.perf_counter()
        for _ in range(50):
            get_full_response(r1000, "prompt")
        t1000 = time.perf_counter() - start

        ratio = t1000 / t100
        assert ratio < 20, f"scaling ratio {ratio:.1f}x for 10x input — suggests quadratic"
