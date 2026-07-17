"""Response chain traversal and content extraction."""

from __future__ import annotations

import re
from collections.abc import Collection, Iterator

from claude_history.models import (
    DT_MIN,
    HOOK_ERROR_RE,
    ContentBlock,
    HookEvent,
    ProgressStub,
    Prompt,
    Record,
    TaskNotification,
    ToolResultContent,
    ToolUseContent,
    extract_content_text,
    extract_hook_contexts,
    is_teammate_message_content,
    iter_user_records,
    parse_task_notification,
    parse_timestamp,
)


def _iter_turn_records(
    children_map: dict[str, list[Record]], start_uuid: str
) -> Iterator[Record]:
    """Yield every record reachable from start_uuid without crossing another
    user text prompt (the next turn).

    Responses no longer hang directly off the prompt: current clients
    (observed v2.1.156+) insert attachment/mode/etc. records between the
    prompt and the first assistant record, and older clients bridge parallel
    tool_use and Task/Skill chains through progress stubs — this traversal
    walks through all of them and explores parallel branches fully.
    """
    queue = [start_uuid]
    visited = {start_uuid}
    while queue:
        for child in children_map.get(queue.pop(), []):
            if isinstance(child, ProgressStub):
                child_uuid = child.uuid
            else:
                if is_user_text_prompt(child):
                    continue  # next turn — don't cross
                child_uuid = child.get("uuid")
            if not child_uuid or child_uuid in visited:
                continue
            visited.add(child_uuid)
            queue.append(child_uuid)
            yield child


def _has_assistant_descendant(
    uuid: str, children_map: dict[str, list[Record]]
) -> bool:
    """True if an assistant record is reachable within the prompt's turn."""
    return any(
        not isinstance(r, ProgressStub) and r.get("type") == "assistant"
        for r in _iter_turn_records(children_map, uuid)
    )


def extract_user_prompts(
    records: list[Record], indexes: RecordIndexes | None = None
) -> list[Prompt]:
    """Extract user prompts from conversation records.

    Pass pre-built indexes to avoid an extra O(N) index build when the
    caller already has them (transcript, search).
    """
    prompts: list[Prompt] = []
    seen_uuids: set[str] = set()

    # User-typed prompts have a reachable assistant response
    children_map, _ = indexes if indexes else build_record_indexes(records)

    for record in iter_user_records(records):
        # Skip compaction summaries (system-generated context summaries)
        if record.get("isCompactSummary"):
            continue

        uuid = record.get("uuid", "unknown")
        if uuid in seen_uuids:
            continue
        seen_uuids.add(uuid)

        message = record.get("message", {})
        content = message.get("content", [])

        # Skip teammate-message and task-notification records (system-injected
        # string wrappers). Other string-content records (bash-input,
        # local-command-caveat, plain text prompts) are legitimate and handled
        # by downstream classification.
        if is_teammate_message_content(content):
            continue
        if isinstance(content, str) and content.startswith("<task-notification>"):
            continue

        prompt_text = extract_content_text(content)

        if not prompt_text:
            continue

        dt = parse_timestamp(record.get("timestamp"))

        prompts.append(
            Prompt(
                uuid=uuid,
                text=prompt_text,
                timestamp=dt,
                source_file=record.get("_source_file", "unknown"),
                session_id=record.get("sessionId", "unknown"),
                slug=record.get("slug", "unknown"),
                is_tool_result="sourceToolAssistantUUID" in record,
                has_assistant_child=_has_assistant_descendant(uuid, children_map),
            )
        )

    prompts.sort(key=lambda x: x.timestamp or DT_MIN, reverse=True)

    return prompts


def find_response_for_prompt(records: list[Record], prompt_uuid: str) -> dict | None:
    """Find the assistant response for a given prompt UUID."""
    for record in records:
        if isinstance(record, ProgressStub):
            continue
        if record.get("type") != "assistant":
            continue
        if record.get("parentUuid") == prompt_uuid:
            return record
    return None


def is_user_text_prompt(record: dict) -> bool:
    """Check if a user record is a text prompt (not a tool_result).

    Tool result records have sourceToolAssistantUUID set and should not
    be treated as user-typed prompts, even if they contain text blocks
    (e.g., system-reminder injections).
    """
    if record.get("type") != "user":
        return False

    # Tool result records are not user-typed prompts
    if "sourceToolAssistantUUID" in record:
        return False

    # System-injected messages (e.g., <local-command-caveat> wrappers)
    if record.get("isMeta"):
        return False

    # Compaction summaries ("This session is being continued...")
    if record.get("isCompactSummary"):
        return False

    content = record.get("message", {}).get("content", [])

    # Teammate-message wrapper records (possibly prefixed by client text)
    if is_teammate_message_content(content):
        return False

    # Plain string content is an ordinary typed prompt (the dominant string
    # format), unless it is a system-injected wrapper (<command-message>,
    # <task-notification>, <bash-input>, <local-command-*>, etc.) or an
    # interruption notice.
    if isinstance(content, str):
        stripped = content.strip()
        if not stripped:
            return False
        if stripped.startswith("<") or stripped.startswith("[Request interrupted"):
            return False
        return True

    if isinstance(content, list):
        for block in content:
            if isinstance(block, str) and block.strip():
                return True
            if isinstance(block, dict) and block.get("type") == "text":
                text = block.get("text", "")
                # System-injected interruption messages (not user-typed)
                if text.strip().startswith("[Request interrupted"):
                    return False
                return True

    return False


RecordIndexes = tuple[dict[str, list[Record]], dict[str, Record]]


def build_record_indexes(records: list[Record]) -> RecordIndexes:
    """Build children_map and record_map for chain traversal.

    Returns (children_map, record_map) for use with get_full_response.
    Build once before a loop, pass to each call.
    """
    children_map: dict[str, list[Record]] = {}
    record_map: dict[str, Record] = {}
    for r in records:
        if isinstance(r, ProgressStub):
            uuid = r.uuid
            if uuid:
                record_map[uuid] = r
            if r.parentUuid:
                children_map.setdefault(r.parentUuid, []).append(r)
        else:
            uuid = r.get("uuid")
            if uuid:
                record_map[uuid] = r
            parent = r.get("parentUuid")
            if parent:
                children_map.setdefault(parent, []).append(r)
    return children_map, record_map


def get_full_response(
    records: list[Record],
    prompt_uuid: str,
    indexes: RecordIndexes | None = None,
) -> list[dict]:
    """Get all assistant records in the response chain for a prompt.

    Collects every assistant record within the prompt's turn (see
    _iter_turn_records), ordered by timestamp so parallel branches and a
    dead-end branch cannot truncate the turn.
    """
    if indexes:
        children_map, _record_map = indexes
    else:
        children_map, _record_map = build_record_indexes(records)

    chain = [
        r
        for r in _iter_turn_records(children_map, prompt_uuid)
        if not isinstance(r, ProgressStub) and r.get("type") == "assistant"
    ]
    chain.sort(key=lambda r: parse_timestamp(r.get("timestamp")) or DT_MIN)
    return chain


def extract_text_from_response(response: dict) -> str:
    message = response.get("message", {})
    content = message.get("content", [])

    if isinstance(content, str):
        return content

    text_parts = []
    for block in content:
        if isinstance(block, dict) and block.get("type") == "text":
            text_parts.append(block.get("text", ""))

    return "\n\n".join(text_parts)


def extract_tools_from_response(response: dict) -> list[dict]:
    message = response.get("message", {})
    content = message.get("content", [])

    if not isinstance(content, list):
        return []

    tools = []
    for block in content:
        if isinstance(block, dict) and block.get("type") == "tool_use":
            tools.append(
                {
                    "name": block.get("name", "unknown"),
                    "input": block.get("input", {}),
                }
            )

    return tools


def extract_thinking_from_response(response: dict) -> list[str]:
    message = response.get("message", {})
    content = message.get("content", [])

    if not isinstance(content, list):
        return []

    thinking = []
    for block in content:
        if isinstance(block, dict) and block.get("type") == "thinking":
            thinking.append(block.get("thinking", ""))

    return thinking


def extract_all_text(chain: list[dict]) -> str:
    text_parts = []
    for record in chain:
        text = extract_text_from_response(record)
        if text:
            text_parts.append(text)
    return "\n\n".join(text_parts)


def extract_all_tools(chain: list[dict]) -> list[dict]:
    tools = []
    for record in chain:
        tools.extend(extract_tools_from_response(record))
    return tools


def extract_all_thinking(chain: list[dict]) -> list[str]:
    thinking = []
    for record in chain:
        thinking.extend(extract_thinking_from_response(record))
    return thinking


def extract_hook_text(chain: list[dict], records: list[Record]) -> str:
    """Extract searchable text from hook errors and hook contexts.

    Collects hook error content from tool_result blocks in user records
    that correspond to tool_use blocks in the chain, plus hook context
    strings from assistant text blocks.
    """
    parts: list[str] = []

    # Hook contexts from assistant text blocks
    for record in chain:
        text = extract_text_from_response(record)
        if text and "<system-reminder>" in text:
            parts.extend(extract_hook_contexts(text))

    # Hook errors from tool_result blocks matching this chain's tool_use IDs
    tool_use_ids: set[str] = set()
    for record in chain:
        message = record.get("message", {})
        content = message.get("content", [])
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    tool_id = block.get("id", "")
                    if tool_id:
                        tool_use_ids.add(tool_id)

    if tool_use_ids:
        tool_results = collect_tool_results(records, tool_use_ids)
        for tr in tool_results.values():
            if tr.is_error and isinstance(tr.content, str) and HOOK_ERROR_RE.search(tr.content):
                parts.append(tr.content)

    return " ".join(parts)


def collect_tool_results(
    records: list[Record], tool_use_ids: Collection[str]
) -> dict[str, ToolResultContent]:
    """Collect tool results from user records that match given tool_use IDs."""
    results: dict[str, ToolResultContent] = {}
    for record in iter_user_records(records):
        message = record.get("message", {})
        content = message.get("content", [])
        if not isinstance(content, list):
            continue
        for block in content:
            if isinstance(block, dict) and block.get("type") == "tool_result":
                tool_id = block.get("tool_use_id", "")
                if tool_id in tool_use_ids:
                    results[tool_id] = ToolResultContent(
                        content=block.get("content", ""),
                        is_error=block.get("is_error", False),
                    )
    return results


def _split_thinking_tags(text: str) -> list[ContentBlock]:
    """Split text that may contain <thinking> tags into typed content blocks.

    Some models output <thinking> tags as raw text inside text blocks.
    This separates them into proper thinking + text blocks so verbosity filtering works.
    """
    if "<thinking>" not in text:
        return [ContentBlock(type="text", content=text)]
    blocks: list[ContentBlock] = []
    for part in re.split(r"(<thinking>.*?</thinking>)", text, flags=re.DOTALL):
        part = part.strip()
        if not part:
            continue
        m = re.match(r"<thinking>(.*)</thinking>", part, re.DOTALL)
        if m:
            blocks.append(ContentBlock(type="thinking", content=m.group(1).strip()))
        else:
            blocks.append(ContentBlock(type="text", content=part))
    return blocks


def build_notification_map(records: list[Record]) -> dict[str, TaskNotification]:
    """Map record UUID → TaskNotification for task-notification user records.

    Notifications arrive as plain-string user records in older files and
    inside text/tool_result blocks of array content in v2.1.19x+ files.
    """
    notifications: dict[str, TaskNotification] = {}
    for record in iter_user_records(records):
        content = record.get("message", {}).get("content")
        if isinstance(content, str):
            text = content
        elif isinstance(content, list):
            parts = []
            for block in content:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "text":
                    parts.append(block.get("text", ""))
                elif block.get("type") == "tool_result" and isinstance(
                    block.get("content"), str
                ):
                    parts.append(block["content"])
            text = " ".join(parts)
        else:
            continue
        if "<task-notification>" not in text:
            continue
        notif = parse_task_notification(text)
        if notif:
            uuid = record.get("uuid", "")
            if uuid:
                notifications[uuid] = notif
    return notifications


def extract_ordered_content(
    chain: list[dict],
    records: list[Record] | None = None,
    *,
    include_tool_results: bool = False,
    notification_map: dict[str, TaskNotification] | None = None,
    hook_records: list[dict] | None = None,
) -> list[ContentBlock]:
    """Extract all content blocks from chain in order.

    If records is provided, inserts async notification summaries at their
    chronological position (always — they're lightweight system events).
    Tool results are verbose, so they require explicit ``include_tool_results=True``.

    Pass a pre-built notification_map to avoid rebuilding it on every call
    (useful when calling in a loop over the same records).
    """
    blocks: list[ContentBlock] = []
    tool_use_ids: dict[str, int] = {}  # Map tool_use_id to index in blocks list

    # Build notification map: uuid → summary for task-notification user records.
    # When an assistant record's parent is a notification, insert the summary
    # before that assistant's content (matching Claude Code's display order).
    if notification_map is None:
        notification_map = build_notification_map(records) if records else {}

    for record in chain:
        # Insert notification before assistant records that respond to one
        parent_uuid = record.get("parentUuid", "")
        if parent_uuid in notification_map:
            blocks.append(
                ContentBlock(
                    type="notification",
                    content=notification_map[parent_uuid],
                )
            )

        message = record.get("message", {})
        content = message.get("content", [])

        if not isinstance(content, list):
            continue

        for block in content:
            if not isinstance(block, dict):
                continue

            block_type = block.get("type")
            if block_type == "thinking":
                blocks.append(
                    ContentBlock(type="thinking", content=block.get("thinking", ""))
                )
            elif block_type == "text":
                text = block.get("text", "")
                blocks.extend(_split_thinking_tags(text))
            elif block_type == "tool_use":
                tool_id = block.get("id", "")
                blocks.append(
                    ContentBlock(
                        type="tool_use",
                        content=ToolUseContent(
                            id=tool_id,
                            name=block.get("name", "unknown"),
                            input=block.get("input", {}),
                        ),
                    )
                )
                if tool_id:
                    tool_use_ids[tool_id] = len(blocks) - 1

    # Insert hook events after their corresponding tool_use blocks
    if hook_records and tool_use_ids:
        hooks_by_tool: dict[str, list[HookEvent]] = {}
        trailing_hooks: list[HookEvent] = []
        for hr in hook_records:
            data = hr.get("data", {})
            event = HookEvent(
                hook_name=data.get("hookName", ""),
                hook_event=data.get("hookEvent", ""),
                command=data.get("command", "") or data.get("promptText", ""),
            )
            ptid = hr.get("parentToolUseID", "")
            if ptid in tool_use_ids:
                hooks_by_tool.setdefault(ptid, []).append(event)
            else:
                trailing_hooks.append(event)

        hook_inserts: list[tuple[int, ContentBlock]] = []
        for tool_id, idx in tool_use_ids.items():
            for he in hooks_by_tool.get(tool_id, []):
                hook_inserts.append((idx + 1, ContentBlock(type="hook", content=he)))
        for idx, block in sorted(hook_inserts, key=lambda x: x[0], reverse=True):
            blocks.insert(idx, block)

        for he in trailing_hooks:
            blocks.append(ContentBlock(type="hook", content=he))

        # Recompute tool_use_ids indices — hook insertions shifted positions
        tool_use_ids = {
            b.content.id: i
            for i, b in enumerate(blocks)
            if b.type == "tool_use" and isinstance(b.content, ToolUseContent)
        }

    # If requested, find tool results and insert after their tool_use
    if include_tool_results and records and tool_use_ids:
        tool_results = collect_tool_results(records, tool_use_ids.keys())

        # Insert tool results after their corresponding tool_use (in reverse order to maintain indices)
        inserts: list[tuple[int, ContentBlock]] = []
        for tool_id, idx in tool_use_ids.items():
            if tool_id in tool_results:
                inserts.append(
                    (
                        idx + 1,
                        ContentBlock(type="tool_result", content=tool_results[tool_id]),
                    )
                )

        for idx, block in sorted(inserts, key=lambda x: x[0], reverse=True):
            blocks.insert(idx, block)

    return blocks


def build_task_agent_map(records: list[Record]) -> dict[str, str]:
    """Build mapping from tool_use ID to agentId for Task/Agent tool calls.

    Old files link via progress-stub parentToolUseID → agentId; files
    without progress records (v2.1.15x+) carry agentId in the Agent tool
    result's toolUseResult.
    """
    mapping: dict[str, str] = {}
    for r in records:
        if isinstance(r, ProgressStub):
            if r.parentToolUseID and r.agentId and r.parentToolUseID not in mapping:
                mapping[r.parentToolUseID] = r.agentId
            continue
        tool_use_result = r.get("toolUseResult")
        if not isinstance(tool_use_result, dict):
            continue
        agent_id = tool_use_result.get("agentId")
        if not agent_id:
            continue
        content = r.get("message", {}).get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if isinstance(block, dict) and block.get("type") == "tool_result":
                tool_id = block.get("tool_use_id", "")
                if tool_id and tool_id not in mapping:
                    mapping[tool_id] = agent_id
    return mapping
