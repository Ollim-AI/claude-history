"""Subagent metadata extraction and listing for claude-history."""

from __future__ import annotations

import argparse
from datetime import datetime
from pathlib import Path

from claude_history.io import parse_jsonl_file, parse_subagent_file
from claude_history.models import (
    DT_MIN,
    STOP_HOOK_FEEDBACK_PREFIX,
    SearchMatch,
    SearchTarget,
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
                errors.append(result_text)
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
            # Detect stop hook feedback (plain-string user records from SubagentStop hooks)
            user_content = record.get("message", {}).get("content", "")
            if isinstance(user_content, str) and user_content.startswith(STOP_HOOK_FEEDBACK_PREFIX):
                errors.append(user_content)

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


def _search_subagent_chain_targets(
    records: list[dict],
    q: str,
    compare,
    chain_targets: set[SearchTarget],
) -> str:
    """Extract tool/tool-result/thinking/hook text from a subagent's records.

    Mirrors search.py._search_chain: extract_content_text only keeps type=='text'
    blocks, so these targets must be pulled from their real block types. Returns
    the first matching extracted text, or "" if none match.
    """
    from claude_history.chain import (
        collect_tool_results,
        extract_all_thinking,
        extract_all_tools,
        extract_hook_text,
    )

    if SearchTarget.TOOLS in chain_targets:
        tools = extract_all_tools(records)
        tool_text = " ".join(
            f"{t['name']} {format_tool_summary(t['name'], t['input'])}" for t in tools
        )
        if tool_text and q in compare(tool_text):
            return tool_text

    if SearchTarget.THINKING in chain_targets:
        thinking_text = " ".join(extract_all_thinking(records))
        if thinking_text and q in compare(thinking_text):
            return thinking_text

    if SearchTarget.HOOKS in chain_targets:
        hook_text = extract_hook_text(records, records)
        if hook_text and q in compare(hook_text):
            return hook_text

    if SearchTarget.TOOL_RESULTS in chain_targets:
        tool_use_ids: set[str] = set()
        for record in records:
            content = record.get("message", {}).get("content", [])
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "tool_use":
                        tool_id = block.get("id", "")
                        if tool_id:
                            tool_use_ids.add(tool_id)
        if tool_use_ids:
            results = collect_tool_results(records, tool_use_ids)
            result_text = " ".join(
                r.content if isinstance(r.content, str) else str(r.content)
                for r in results.values()
            )
            if result_text and q in compare(result_text):
                return result_text

    return ""


def search_subagent_files(
    files: list[Path],
    query: str,
    case_sensitive: bool,
    targets: set[SearchTarget],
) -> list[SearchMatch]:
    """Search subagent files for matching text in the specified targets.

    Searches record-by-record and stores only a snippet around the match
    to avoid unbounded memory from concatenating all text.
    """
    compare = (lambda t: t) if case_sensitive else (lambda t: t.lower())
    q = compare(query)
    matches: list[SearchMatch] = []
    # Plain-text targets extractable via extract_content_text (type=='text' only)
    search_user = SearchTarget.PROMPTS in targets
    search_assistant = SearchTarget.RESPONSES in targets
    # Block-level targets whose content extract_content_text drops (tool_use,
    # tool_result, thinking, hooks) — extracted separately over the whole file.
    chain_targets = targets & {
        SearchTarget.TOOLS,
        SearchTarget.TOOL_RESULTS,
        SearchTarget.THINKING,
        SearchTarget.HOOKS,
    }

    for filepath in files:
        records = parse_subagent_file(filepath)
        if not records:
            continue
        agent_id = filepath.stem.replace("agent-", "")
        session_id = records[0].get("sessionId", "")
        earliest_ts = parse_timestamp(records[0].get("timestamp"))
        matched_text = ""

        for r in records:
            if matched_text:
                break

            rtype = r.get("type")
            if (rtype == "user" and search_user) or (rtype == "assistant" and search_assistant):
                content = r.get("message", {}).get("content", "")
                text = content if isinstance(content, str) else extract_content_text(content)
                if text and q in compare(text):
                    matched_text = text

        # Tool/tool-result/thinking/hook content lives in non-text blocks that
        # extract_content_text discards; extract it the same way session search
        # does so subagent hits on these targets are not silently missed.
        if not matched_text and chain_targets:
            matched_text = _search_subagent_chain_targets(
                records, q, compare, chain_targets
            )

        if matched_text:
            matches.append(
                SearchMatch(
                    type="subagent",
                    uuid=agent_id,
                    session_id=session_id,
                    timestamp=earliest_ts,
                    text=matched_text,
                )
            )

    matches.sort(key=lambda m: m.timestamp or DT_MIN, reverse=True)
    return matches


def render_subagent_transcript(filepath: Path, args: argparse.Namespace) -> None:
    """Render a full subagent transcript using the same display logic as session transcripts."""
    from claude_history.chain import (
        build_record_indexes,
        extract_ordered_content,
        extract_user_prompts,
        get_full_response,
    )
    from claude_history.render import (
        bold,
        cyan,
        cyan_bold,
        dim,
        format_local,
        green,
        render_blocks,
        yellow,
    )

    all_records = parse_subagent_file(filepath)
    if not all_records:
        print("No records found in subagent file.")
        return

    # Separate hook_progress records from the rest for rendering
    hook_records = [
        r for r in all_records
        if r.get("type") == "progress" and r.get("data", {}).get("type") == "hook_progress"
    ]
    # Build record list compatible with chain traversal (non-progress + progress stubs)
    from claude_history.models import ProgressStub
    records: list[dict | object] = []
    for r in all_records:
        if r.get("type") == "progress":
            records.append(ProgressStub(
                uuid=r.get("uuid", ""),
                parentUuid=r.get("parentUuid"),
                parentToolUseID=r.get("parentToolUseID"),
                agentId=r.get("data", {}).get("agentId"),
            ))
        else:
            records.append(r)

    agent_id = filepath.stem.replace("agent-", "")
    show_thinking = args.show_thinking
    show_tools = not args.hide_tools
    show_tool_results = not getattr(args, "hide_tool_results", False)
    full_detail = getattr(args, "show_tool_results", False)
    show_hooks = getattr(args, "show_hooks", False)
    prompts_only = args.prompts_only

    # Header
    model = ""
    for r in records:
        if isinstance(r, dict) and r.get("type") == "assistant":
            model = r.get("message", {}).get("model", "")
            break
    print(f"Subagent: {cyan(agent_id)}  |  {dim(model)}")
    print(f"File: {dim(str(filepath))}\n")

    # In subagent files, the first user record is the spawn prompt (string content).
    # extract_user_prompts filters it out. Handle it directly.
    first_user = None
    for r in records:
        if isinstance(r, dict) and r.get("type") == "user":
            first_user = r
            break

    if first_user is None:
        print("No records found.")
        return

    # Show the spawn prompt
    prompt_uuid = first_user.get("uuid", "")
    msg = first_user.get("message", {})
    content = msg.get("content", "")
    if isinstance(content, str):
        prompt_text = content
    else:
        prompt_text = extract_content_text(content)
    ts = parse_timestamp(first_user.get("timestamp"))
    ts_str = dim(format_local(ts, "%Y-%m-%d %H:%M")) if ts else ""
    print(f"{cyan_bold('[prompt]')} {ts_str}")
    print(prompt_text)
    print()

    if not prompts_only:
        indexes = build_record_indexes(records)
        chain = get_full_response(records, prompt_uuid, indexes=indexes)
        # Some subagent files have two consecutive user records at the start
        # (spawn prompt + system context). If the first has no assistant child,
        # try the next user record.
        if not chain:
            for r in records:
                if isinstance(r, dict) and r.get("type") == "user" and r.get("uuid") != prompt_uuid:
                    chain = get_full_response(records, r["uuid"], indexes=indexes)
                    if chain:
                        break
        if chain:
            blocks = extract_ordered_content(
                chain, records,
                include_tool_results=show_tool_results or show_hooks,
                hook_records=hook_records if show_hooks else None,
            )
            print(green("[assistant]"))
            agent_hint = f"claude-history transcript {agent_id} --show-tool-results" if not full_detail else ""
            if not render_blocks(
                blocks,
                {},
                show_thinking=show_thinking,
                show_tools=show_tools,
                show_tool_results=show_tool_results,
                full_detail=full_detail,
                show_hooks=show_hooks,
                detail_hint=agent_hint,
            ):
                print(dim("(no text content)"))
                print()

        print(dim("---"))
        print()
