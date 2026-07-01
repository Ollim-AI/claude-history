"""Response chain traversal and content extraction."""

from __future__ import annotations

import re
from collections.abc import Collection

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
    iter_user_records,
    parse_task_notification,
    parse_timestamp,
)


def extract_user_prompts(records: list[Record]) -> list[Prompt]:
    """Extract user prompts from conversation records."""
    prompts: list[Prompt] = []
    seen_uuids: set[str] = set()

    # Build parent->child type map to identify user-typed prompts
    # User-typed prompts have an assistant child (Claude responds to them)
    child_types: dict[str, str] = {}
    for r in records:
        if isinstance(r, ProgressStub):
            continue
        parent = r.get("parentUuid")
        rtype = r.get("type")
        if parent and rtype:
            child_types[parent] = rtype

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
        if isinstance(content, str) and content.startswith(
            ("<teammate-message", "<task-notification>")
        ):
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
                has_assistant_child=child_types.get(uuid) == "assistant",
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

    # Teammate-message records have string content starting with <teammate-message
    if isinstance(content, str) and content.startswith("<teammate-message"):
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


def _find_progress_sibling(siblings: list[Record], visited: set[str]) -> str | None:
    """Find the first unvisited ProgressStub among siblings."""
    for sibling in siblings:
        if not isinstance(sibling, ProgressStub):
            continue
        if sibling.uuid and sibling.uuid not in visited:
            return sibling.uuid
    return None


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

    The chain includes both assistant and user (tool_result) records.
    We follow all records but only return assistant records.
    Stops at the next user prompt with text content.

    Handles subagent chains where progress records bridge the gap between
    tool_result records and subsequent assistant responses.
    """
    # Find first assistant record (direct child of prompt)
    first = None
    for r in records:
        if isinstance(r, ProgressStub):
            continue
        if r.get("type") == "assistant" and r.get("parentUuid") == prompt_uuid:
            first = r
            break

    if not first:
        return []

    if indexes:
        children_map, record_map = indexes
    else:
        children_map, record_map = build_record_indexes(records)

    chain = [first]
    current_uuid = first.get("uuid", "")
    visited: set[str] = {prompt_uuid, current_uuid}

    while True:
        children = children_map.get(current_uuid, [])

        # Find first non-progress child
        next_record = None
        for child in children:
            if isinstance(child, ProgressStub):
                continue
            if child.get("uuid") not in visited:
                next_record = child
                break

        if not next_record:
            # Dead end - try following through progress records.
            # 1. Follow progress children (traverses agent progress chains
            #    where parallel tool_use records are linked through deep
            #    ProgressStub chains).
            progress_child = _find_progress_sibling(children, visited)
            if progress_child:
                visited.add(progress_child)
                current_uuid = progress_child
                continue

            # 2. Walk up parent chain looking for unvisited progress siblings.
            #    One level handles Skill/Task bridges; multiple levels handle
            #    parallel tool_use chains where the progress fork is on a
            #    grandparent (e.g., tool_result → assistant → progress chain).
            ancestor = record_map.get(current_uuid)
            found_progress = False
            for _ in range(10):  # bounded walk-up
                if ancestor is None:
                    break
                parent_uuid = (
                    ancestor.parentUuid
                    if isinstance(ancestor, ProgressStub)
                    else ancestor.get("parentUuid")
                )
                if not parent_uuid:
                    break
                progress_uuid = _find_progress_sibling(
                    children_map.get(parent_uuid, []), visited
                )
                if progress_uuid:
                    visited.add(progress_uuid)
                    current_uuid = progress_uuid
                    found_progress = True
                    break
                ancestor = record_map.get(parent_uuid)
            if found_progress:
                continue
            break

        # Stop at user prompts with text content (new conversation turn)
        if is_user_text_prompt(next_record):
            break
        # Only add assistant records to chain
        visited.add(next_record["uuid"])
        if next_record.get("type") == "assistant":
            chain.append(next_record)
        current_uuid = next_record.get("uuid")

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
    """Map record UUID → TaskNotification for task-notification user records."""
    notifications: dict[str, TaskNotification] = {}
    for record in iter_user_records(records):
        content = record.get("message", {}).get("content")
        if not isinstance(content, str):
            continue
        if "<task-notification>" not in content:
            continue
        notif = parse_task_notification(content)
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
    """Build mapping from tool_use ID to agentId for Task tool calls.

    Scans progress record stubs for parentToolUseID -> agentId pairs.
    """
    mapping: dict[str, str] = {}
    for r in records:
        if not isinstance(r, ProgressStub):
            continue
        if r.parentToolUseID and r.agentId and r.parentToolUseID not in mapping:
            mapping[r.parentToolUseID] = r.agentId
    return mapping
