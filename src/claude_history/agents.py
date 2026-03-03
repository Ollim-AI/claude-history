"""Subagent metadata extraction and listing for claude-history."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from claude_history.io import parse_subagent_file
from claude_history.models import (
    DT_MIN,
    SubagentMetadata,
    ToolInfo,
    extract_content_text,
    parse_teammate_message,
    parse_timestamp,
)
from claude_history.render import _short_model_name, format_tool_summary


def _count_tokens(msg: dict) -> tuple[int, int]:
    """Return (input_tokens, output_tokens) from an assistant message."""
    usage = msg.get("usage", {})
    input_tok = (
        usage.get("input_tokens", 0)
        + usage.get("cache_creation_input_tokens", 0)
        + usage.get("cache_read_input_tokens", 0)
    )
    return input_tok, usage.get("output_tokens", 0)


def _extract_tools_and_text(msg: dict, response_texts: list[str]) -> list[ToolInfo]:
    """Extract ToolInfo items from assistant content, appending text to response_texts."""
    content = msg.get("content", [])
    if not isinstance(content, list):
        return []
    tools: list[ToolInfo] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "tool_use":
            tool_input = block.get("input", {})
            tools.append(
                ToolInfo(
                    name=block.get("name", "unknown"),
                    id=block.get("id", ""),
                    arg_summary=format_tool_summary(block.get("name", ""), tool_input),
                )
            )
        elif block.get("type") == "text":
            text = block.get("text", "").strip()
            if text:
                response_texts.append(text)
    return tools


def _extract_error_texts(record: dict) -> list[str]:
    """Extract truncated error texts from tool_result blocks in a user record."""
    msg = record.get("message", {})
    content = msg.get("content", [])
    if not isinstance(content, list):
        return []
    errors: list[str] = []
    for block in content:
        if (
            isinstance(block, dict)
            and block.get("type") == "tool_result"
            and block.get("is_error")
        ):
            result_text = block.get("content", "")
            if isinstance(result_text, str):
                errors.append(result_text[:200])
    return errors


def extract_subagent_metadata(filepath: Path, records: list[dict]) -> SubagentMetadata:
    """Extract metadata from a subagent's parsed records."""
    session_id = filepath.parent.parent.name
    agent_id = filepath.stem.replace("agent-", "")

    # First record metadata
    first = records[0] if records else {}
    slug = first.get("slug", "")
    prompt = ""
    teammate_name: str | None = None
    if first.get("type") == "user":
        msg = first.get("message", {})
        content = msg.get("content", "")
        if isinstance(content, str):
            tm = parse_teammate_message(first)
            if tm:
                teammate_name = tm.teammate_id
                prompt = tm.body[:200] if tm.body else ""
            else:
                prompt = content
        else:
            prompt = extract_content_text(content)

    # Scan records for model, timestamps, tokens, tools, errors
    model = None
    earliest_ts: datetime | None = None
    latest_ts: datetime | None = None
    total_input_tokens = 0
    total_output_tokens = 0
    tools: list[ToolInfo] = []
    errors: list[str] = []
    response_texts: list[str] = []

    for record in records:
        dt = parse_timestamp(record.get("timestamp"))
        if dt:
            if earliest_ts is None or dt < earliest_ts:
                earliest_ts = dt
            if latest_ts is None or dt > latest_ts:
                latest_ts = dt

        if record.get("type") == "assistant":
            msg = record.get("message", {})
            if not model:
                model = msg.get("model", "")
            in_tok, out_tok = _count_tokens(msg)
            total_input_tokens += in_tok
            total_output_tokens += out_tok
            tools.extend(_extract_tools_and_text(msg, response_texts))

        elif record.get("type") == "user":
            errors.extend(_extract_error_texts(record))

    duration = None
    if earliest_ts and latest_ts:
        duration = (latest_ts - earliest_ts).total_seconds()

    return SubagentMetadata(
        filepath=filepath,
        filename=filepath.name,
        agent_id=agent_id,
        session_id=session_id,
        slug=slug,
        prompt=prompt,
        model=_short_model_name(model),
        model_full=model or "unknown",
        record_count=len(records),
        earliest_timestamp=earliest_ts,
        latest_timestamp=latest_ts,
        duration=duration,
        total_input_tokens=total_input_tokens,
        total_output_tokens=total_output_tokens,
        tools=tuple(tools),
        errors=tuple(errors),
        response_text="\n\n".join(response_texts),
        teammate_name=teammate_name,
    )


def get_subagents(project_dir: Path) -> list[SubagentMetadata]:
    """List subagent files in the project directory with metadata."""
    subagents: list[SubagentMetadata] = []

    for jsonl_file in project_dir.glob("*/subagents/agent-*.jsonl"):
        records = parse_subagent_file(jsonl_file)
        if not records:
            continue
        subagents.append(extract_subagent_metadata(jsonl_file, records))

    subagents.sort(key=lambda x: x.latest_timestamp or DT_MIN, reverse=True)
    return subagents
