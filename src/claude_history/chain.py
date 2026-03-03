"""Response chain traversal and content extraction."""

from __future__ import annotations

import re

from claude_history.models import (
    DT_MIN,
    HOOK_ERROR_RE,
    ContentBlock,
    ProgressStub,
    Prompt,
    Record,
    ToolResultContent,
    ToolUseContent,
    extract_content_text,
    extract_hook_contexts,
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

    for record in records:
        if isinstance(record, ProgressStub):
            continue
        if record.get("type") != "user":
            continue

        # Skip compaction summaries (system-generated context summaries)
        if record.get("isCompactSummary"):
            continue

        uuid = record.get("uuid", "unknown")
        if uuid in seen_uuids:
            continue
        seen_uuids.add(uuid)

        message = record.get("message", {})
        content = message.get("content", [])

        # Skip teammate-message records (string content = XML, not user-typed)
        if isinstance(content, str):
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

    # String content = teammate-message record (not user-typed)
    if isinstance(content, str):
        return False

    if isinstance(content, list):
        for block in content:
            if isinstance(block, str) and block.strip():
                return True
            if isinstance(block, dict) and block.get("type") == "text":
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


def get_full_response(records: list[Record], prompt_uuid: str) -> list[dict]:
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

    # Build indexes for efficient chain traversal
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
            # When a tool_result has no continuation but its parent (the tool_use
            # assistant) has a progress sibling, follow through it. This handles
            # Skill/Task tool responses where the chain bridges through subagents.
            current_record = record_map.get(current_uuid)
            if current_record is None:
                break
            parent_uuid = (
                current_record.parentUuid
                if isinstance(current_record, ProgressStub)
                else current_record.get("parentUuid")
            )
            if not parent_uuid:
                break
            progress_uuid = _find_progress_sibling(
                children_map.get(parent_uuid, []), visited
            )
            if not progress_uuid:
                break
            visited.add(progress_uuid)
            current_uuid = progress_uuid
            continue

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
        tool_results = _collect_tool_results(records, tool_use_ids)
        for tr in tool_results.values():
            if tr.is_error and isinstance(tr.content, str) and HOOK_ERROR_RE.search(tr.content):
                parts.append(tr.content)

    return " ".join(parts)


def _collect_tool_results(
    records: list[Record], tool_use_ids: set[str]
) -> dict[str, ToolResultContent]:
    """Collect tool results from user records that match given tool_use IDs."""
    results: dict[str, ToolResultContent] = {}
    for record in records:
        if isinstance(record, ProgressStub):
            continue
        if record.get("type") != "user":
            continue
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


def extract_ordered_content(
    chain: list[dict], records: list[Record] | None = None
) -> list[ContentBlock]:
    """Extract all content blocks from chain in order.

    If records is provided, also extracts tool_result blocks by finding
    user records that respond to tool_use blocks in the chain.
    """
    blocks: list[ContentBlock] = []
    tool_use_ids: dict[str, int] = {}  # Map tool_use_id to index in blocks list

    for record in chain:
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

    # If records provided, find tool results and insert after their tool_use
    if records and tool_use_ids:
        tool_results = _collect_tool_results(records, set(tool_use_ids))

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
