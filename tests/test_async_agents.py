"""Tests for async/background agent result resolution."""

from claude_history.chain import extract_ordered_content
from claude_history.models import (
    TaskNotification,
    ToolResultContent,
    parse_task_notification,
)


class TestParseTaskNotification:
    def test_parses_complete_notification(self) -> None:
        text = (
            "<task-notification>\n"
            "<task-id>abccd67</task-id>\n"
            "<status>completed</status>\n"
            "<summary>Agent \"Batch 1\" completed</summary>\n"
            "<result>All 5 files reformatted.</result>\n"
            "<usage>total_tokens: 27382\ntool_uses: 22\nduration_ms: 70993</usage>\n"
            "</task-notification>"
        )
        notif = parse_task_notification(text)
        assert notif is not None
        assert notif.task_id == "abccd67"
        assert notif.status == "completed"
        assert notif.summary == 'Agent "Batch 1" completed'
        assert notif.result == "All 5 files reformatted."
        assert "27382" in notif.usage

    def test_parses_without_usage(self) -> None:
        text = (
            "<task-notification>\n"
            "<task-id>abc123</task-id>\n"
            "<status>completed</status>\n"
            "<summary>Done</summary>\n"
            "<result>Success</result>\n"
            "</task-notification>"
        )
        notif = parse_task_notification(text)
        assert notif is not None
        assert notif.task_id == "abc123"
        assert notif.result == "Success"
        assert notif.usage == ""

    def test_returns_none_for_non_notification(self) -> None:
        assert parse_task_notification("just some text") is None
        assert parse_task_notification("<task-notification>incomplete") is None

    def test_parses_v2_1_74_format_with_extra_tags(self) -> None:
        text = (
            "<task-notification>\n"
            "<task-id>a202ec1ce64a04601</task-id>\n"
            "<tool-use-id>toolu_017xiP76MibBoiZnbV2vv9Wt</tool-use-id>\n"
            "<output-file>/tmp/tasks/a202ec1ce64a04601.output</output-file>\n"
            "<status>completed</status>\n"
            '<summary>Agent "Audit hooks" completed</summary>\n'
            "<result>Found 1 violation.</result>\n"
            "<usage><total_tokens>47769</total_tokens>"
            "<tool_uses>11</tool_uses>"
            "<duration_ms>30011</duration_ms></usage>\n"
            "</task-notification>"
        )
        notif = parse_task_notification(text)
        assert notif is not None
        assert notif.task_id == "a202ec1ce64a04601"
        assert notif.status == "completed"
        assert notif.result == "Found 1 violation."
        assert "47769" in notif.usage

    def test_multiline_result(self) -> None:
        text = (
            "<task-notification>\n"
            "<task-id>x1</task-id>\n"
            "<status>completed</status>\n"
            "<summary>Done</summary>\n"
            "<result>Line 1\nLine 2\nLine 3</result>\n"
            "</task-notification>"
        )
        notif = parse_task_notification(text)
        assert notif is not None
        assert "Line 1\nLine 2\nLine 3" == notif.result



class TestExtractOrderedContentAsync:
    """Integration test: extract_ordered_content resolves async results."""

    def test_inserts_notification_before_responding_assistant(self) -> None:
        # Chain: assistant with tool_use, assistant with "launched" text,
        # then assistant responding to the notification
        chain = [
            {
                "uuid": "a1",
                "parentUuid": "prompt1",
                "message": {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "toolu_bg1",
                            "name": "Task",
                            "input": {
                                "description": "bg work",
                                "prompt": "do stuff",
                                "run_in_background": True,
                            },
                        }
                    ],
                },
            },
            {
                "uuid": "a2",
                "parentUuid": "u1",
                "message": {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "text",
                            "text": "Agent launched. I'll report back.",
                        }
                    ],
                },
            },
            {
                "uuid": "a3",
                "parentUuid": "u2",  # parent is the notification record
                "message": {
                    "role": "assistant",
                    "content": [
                        {"type": "text", "text": "Here are the results."},
                    ],
                },
            },
        ]
        records = [
            # The immediate "launched" tool_result
            {
                "type": "user",
                "uuid": "u1",
                "message": {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "toolu_bg1",
                            "content": [
                                {
                                    "type": "text",
                                    "text": "Async agent launched.\nagentId: agent99",
                                }
                            ],
                        }
                    ],
                },
                "toolUseResult": {
                    "isAsync": True,
                    "status": "async_launched",
                    "agentId": "agent99",
                },
            },
            # The completion notification (string content)
            {
                "type": "user",
                "uuid": "u2",
                "message": {
                    "role": "user",
                    "content": (
                        "<task-notification>\n"
                        "<task-id>agent99</task-id>\n"
                        "<status>completed</status>\n"
                        "<summary>Agent \"bg work\" completed</summary>\n"
                        "<result>Did all the work successfully.</result>\n"
                        "<usage>total_tokens: 1000</usage>\n"
                        "</task-notification>"
                    ),
                },
            },
        ]

        # Default: no tool_results, just text + notification + text
        blocks = extract_ordered_content(chain, records)
        assert len(blocks) == 4
        assert blocks[0].type == "tool_use"
        assert blocks[1].type == "text"
        assert blocks[1].content == "Agent launched. I'll report back."
        assert blocks[2].type == "notification"
        assert isinstance(blocks[2].content, TaskNotification)
        assert blocks[2].content.summary == 'Agent "bg work" completed'
        assert blocks[2].content.result == "Did all the work successfully."
        assert blocks[3].type == "text"
        assert blocks[3].content == "Here are the results."

        # With include_tool_results: adds tool_result after tool_use
        blocks = extract_ordered_content(chain, records, include_tool_results=True)
        assert len(blocks) == 5
        assert blocks[0].type == "tool_use"
        assert blocks[1].type == "tool_result"
        assert isinstance(blocks[1].content, ToolResultContent)
        assert blocks[2].type == "text"
        assert blocks[3].type == "notification"
        assert blocks[4].type == "text"

    def test_no_notification_when_agent_incomplete(self) -> None:
        chain = [
            {
                "uuid": "a1",
                "parentUuid": "prompt1",
                "message": {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "toolu_bg2",
                            "name": "Task",
                            "input": {"run_in_background": True},
                        }
                    ],
                },
            },
        ]
        records = [
            {
                "type": "user",
                "uuid": "u1",
                "message": {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "toolu_bg2",
                            "content": [
                                {"type": "text", "text": "Async agent launched.\nagentId: nofinish"}
                            ],
                        }
                    ],
                },
                "toolUseResult": {
                    "isAsync": True,
                    "status": "async_launched",
                    "agentId": "nofinish",
                },
            },
            # No task-notification record
        ]

        # No notification, no tool_results by default
        blocks = extract_ordered_content(chain, records)
        assert len(blocks) == 1
        assert blocks[0].type == "tool_use"

        # With include_tool_results: original launch content preserved
        blocks = extract_ordered_content(chain, records, include_tool_results=True)
        assert len(blocks) == 2
        assert blocks[1].type == "tool_result"
        assert isinstance(blocks[1].content, ToolResultContent)
        assert isinstance(blocks[1].content.content, list)
