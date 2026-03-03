"""Tests for hook error/context surfacing in claude-history."""

from claude_history.models import (
    HOOK_ERROR_RE,
    Session,
    extract_hook_contexts,
    strip_system_tags,
)


class TestHookErrorRegex:
    def test_matches_pretooluse_hook_error(self) -> None:
        text = "PreToolUse:Bash hook error: Command too long (253 chars)"
        assert HOOK_ERROR_RE.search(text) is not None

    def test_matches_posttooluse_hook_error(self) -> None:
        text = "PostToolUse:Read hook error: File not allowed"
        assert HOOK_ERROR_RE.search(text) is not None

    def test_no_match_on_normal_error(self) -> None:
        text = "Error: something went wrong"
        assert HOOK_ERROR_RE.search(text) is None

    def test_no_match_on_partial(self) -> None:
        text = "NotAHook:Bash hook error: nope"
        assert HOOK_ERROR_RE.search(text) is None


class TestExtractHookContexts:
    def test_extracts_single_context(self) -> None:
        text = (
            "Some response text.\n"
            "<system-reminder>\n"
            "PreToolUse:Read hook additional context: CRITICAL: cli.py is too long\n"
            "</system-reminder>"
        )
        contexts = extract_hook_contexts(text)
        assert len(contexts) == 1
        assert "cli.py is too long" in contexts[0]
        assert contexts[0].startswith("PreToolUse:Read hook additional context:")

    def test_extracts_multiple_contexts(self) -> None:
        text = (
            "<system-reminder>\n"
            "PreToolUse:Read hook additional context: first warning\n"
            "</system-reminder>\n"
            "some text\n"
            "<system-reminder>\n"
            "PostToolUse:Bash hook additional context: second warning\n"
            "</system-reminder>"
        )
        contexts = extract_hook_contexts(text)
        assert len(contexts) == 2
        assert "first warning" in contexts[0]
        assert "second warning" in contexts[1]

    def test_ignores_non_hook_system_reminders(self) -> None:
        text = (
            "<system-reminder>\n"
            "The task tools haven't been used recently.\n"
            "</system-reminder>"
        )
        contexts = extract_hook_contexts(text)
        assert len(contexts) == 0

    def test_empty_on_no_tags(self) -> None:
        assert extract_hook_contexts("plain text") == []


class TestStripSystemTagsPreservesContent:
    def test_strips_hook_context_tags(self) -> None:
        text = (
            "Real content.\n"
            "<system-reminder>\n"
            "PreToolUse:Read hook additional context: warning\n"
            "</system-reminder>"
        )
        stripped = strip_system_tags(text)
        assert "Real content." in stripped
        assert "system-reminder" not in stripped
        assert "hook additional context" not in stripped


class TestSessionHookErrorCount:
    def test_default_zero(self) -> None:
        s = Session(session_id="test")
        assert s.hook_error_count == 0

    def test_mutable(self) -> None:
        s = Session(session_id="test")
        s.hook_error_count = 5
        assert s.hook_error_count == 5


class TestAccumulateHookErrors:
    def test_counts_hook_errors_in_tool_results(self) -> None:
        from claude_history.sessions import _accumulate_session_record

        sess = Session(session_id="s1")
        record = {
            "type": "user",
            "sessionId": "s1",
            "uuid": "u1",
            "message": {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "t1",
                        "is_error": True,
                        "content": "PreToolUse:Bash hook error: Command too long",
                    }
                ],
            },
        }
        _accumulate_session_record(sess, record)
        assert sess.hook_error_count == 1

    def test_ignores_non_hook_errors(self) -> None:
        from claude_history.sessions import _accumulate_session_record

        sess = Session(session_id="s1")
        record = {
            "type": "user",
            "sessionId": "s1",
            "uuid": "u2",
            "message": {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "t2",
                        "is_error": True,
                        "content": "Error: file not found",
                    }
                ],
            },
        }
        _accumulate_session_record(sess, record)
        assert sess.hook_error_count == 0

    def test_ignores_non_error_tool_results(self) -> None:
        from claude_history.sessions import _accumulate_session_record

        sess = Session(session_id="s1")
        record = {
            "type": "user",
            "sessionId": "s1",
            "uuid": "u3",
            "message": {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "t3",
                        "is_error": False,
                        "content": "PreToolUse:Bash hook error: not really",
                    }
                ],
            },
        }
        _accumulate_session_record(sess, record)
        assert sess.hook_error_count == 0


class TestExtractHookText:
    def test_extracts_hook_context_from_chain(self) -> None:
        from claude_history.chain import extract_hook_text

        chain = [
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {
                            "type": "text",
                            "text": (
                                "response\n"
                                "<system-reminder>\n"
                                "PreToolUse:Read hook additional context: file is big\n"
                                "</system-reminder>"
                            ),
                        }
                    ]
                },
            }
        ]
        result = extract_hook_text(chain, [])
        assert "file is big" in result

    def test_extracts_hook_errors_from_tool_results(self) -> None:
        from claude_history.chain import extract_hook_text

        chain = [
            {
                "type": "assistant",
                "uuid": "a1",
                "message": {
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "tu1",
                            "name": "Bash",
                            "input": {"command": "echo hi"},
                        }
                    ]
                },
            }
        ]
        records = [
            {
                "type": "user",
                "uuid": "u1",
                "parentUuid": "a1",
                "message": {
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "tu1",
                            "is_error": True,
                            "content": "PreToolUse:Bash hook error: blocked",
                        }
                    ]
                },
            }
        ]
        result = extract_hook_text(chain, records)
        assert "hook error: blocked" in result

    def test_empty_when_no_hooks(self) -> None:
        from claude_history.chain import extract_hook_text

        chain = [
            {
                "type": "assistant",
                "message": {
                    "content": [{"type": "text", "text": "plain response"}]
                },
            }
        ]
        result = extract_hook_text(chain, [])
        assert result == ""
