"""Color helpers, formatting, and block rendering for claude-history."""

from __future__ import annotations

import json
from datetime import datetime, timezone

from claude_history.models import (
    _BOLD,
    _CYAN,
    _DIM,
    _GREEN,
    _RESET,
    _YELLOW,
    HOOK_ERROR_RE,
    ContentBlock,
    HookEvent,
    TaskNotification,
    ToolResultContent,
    ToolUseContent,
    extract_hook_contexts,
    strip_system_tags,
)

# Tools whose results are user feedback — always shown in transcripts
FEEDBACK_TOOLS = frozenset({"AskUserQuestion", "ExitPlanMode"})


def cyan(s: object) -> str:
    return f"{_CYAN}{s}{_RESET}"


def dim(s: object) -> str:
    return f"{_DIM}{s}{_RESET}"


def yellow(s: object) -> str:
    return f"{_YELLOW}{s}{_RESET}"


def bold(s: object) -> str:
    return f"{_BOLD}{s}{_RESET}"


def cyan_bold(s: object) -> str:
    return f"{_BOLD}{_CYAN}{s}{_RESET}"


def green(s: object) -> str:
    return f"{_GREEN}{s}{_RESET}"


def truncate_text(text: str, max_length: int = 500) -> str:
    """Truncate text showing first and last parts if too long."""
    text = text.replace("\n", " ").strip()
    if len(text) <= max_length:
        return text
    half = (max_length - 3) // 2
    return text[:half] + yellow("…") + text[-half:]


def format_duration(seconds: float) -> str:
    if seconds < 60:
        return f"{int(seconds)}s"
    minutes = int(seconds // 60)
    secs = int(seconds % 60)
    if minutes < 60:
        return f"{minutes}m{secs}s"
    hours = int(minutes // 60)
    mins = minutes % 60
    return f"{hours}h{mins}m"


def format_tokens(count: int) -> str:
    if count < 1000:
        return str(count)
    return f"{count / 1000:.1f}K"


def to_local(dt: datetime) -> datetime:
    return dt.astimezone()


def format_local(
    dt: datetime | None, fmt: str = "%Y-%m-%d %H:%M:%S", default: str = ""
) -> str:
    """Format a datetime in local timezone, returning default if None."""
    return to_local(dt).strftime(fmt) if dt else default


def format_relative_time(dt: datetime) -> str:
    """Format datetime as relative time for recent, absolute for older."""
    now = datetime.now(timezone.utc)
    dt_utc = (
        dt.astimezone(timezone.utc) if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    )
    local = to_local(dt)
    delta = now - dt_utc
    seconds = delta.total_seconds()
    if seconds < 0:
        return local.strftime("%Y-%m-%d")
    if seconds < 60:
        return "just now"
    if seconds < 3600:
        return f"{int(seconds // 60)}m ago"
    if seconds < 86400:
        return f"{int(seconds // 3600)}h ago"
    if delta.days < 7:
        return f"{delta.days}d ago"
    if delta.days < 365:
        return local.strftime("%b %d")
    return local.strftime("%Y-%m-%d")


def format_time(dt: datetime, use_iso: bool = False) -> str:
    """Format datetime as ISO or relative, depending on flag."""
    if use_iso:
        return format_local(dt, "%Y-%m-%dT%H:%M:%S")
    return format_relative_time(dt)


def format_tool_summary(tool_name: str, tool_input: dict) -> str:
    """Build a concise one-line summary of a tool call's key argument."""
    if tool_name == "Bash":
        return tool_input.get("command", "")
    elif tool_name == "Read":
        return tool_input.get("file_path", "")
    elif tool_name in ("Grep", "Glob"):
        return tool_input.get("pattern", "")
    elif tool_name in ("Edit", "Write"):
        return tool_input.get("file_path", "")
    elif tool_name == "Task":
        return tool_input.get("description", "")
    else:
        for v in tool_input.values():
            if isinstance(v, str) and v:
                return v
    return ""


def _short_model_name(model: str | None) -> str:
    """Map a full model identifier to a short display name."""
    name = model or "unknown"
    if "opus" in name:
        return "opus"
    if "sonnet" in name:
        return "sonnet"
    if "haiku" in name:
        return "haiku"
    return name


def render_blocks(
    blocks: list[ContentBlock],
    task_agent_map: dict[str, str],
    *,
    show_thinking: bool = False,
    show_tools: bool = True,
    show_tool_results: bool = True,
    full_detail: bool = False,
    show_hooks: bool = False,
    detail_hint: str = "",
) -> bool:
    """Render content blocks with flag-controlled detail.

    Returns True if any content was printed.
    """
    has_output = False
    hint_printed = False
    prev_type = None
    last_tool_name = ""
    for block in blocks:
        block_type = block.type
        content = block.content

        if block_type == "thinking":
            if show_thinking:
                has_output = True
                assert isinstance(content, str)
                text = content.strip()
                print(dim("[thinking]"))
                if full_detail or len(text) <= 2000:
                    print(dim(text))
                else:
                    print(dim(text[:2000] + "\n... (truncated)"))
                print()
                prev_type = "thinking"

        elif block_type == "text":
            if prev_type == "text":
                print(dim("---"))
            has_output = True
            assert isinstance(content, str)
            # Extract hook contexts before stripping tags
            if show_hooks:
                for ctx in extract_hook_contexts(content):
                    print(yellow("[hook context]"))
                    print(yellow(ctx))
                    print()
            cleaned = strip_system_tags(content)
            if show_thinking:
                print(cyan("[text]"))
            if cleaned:
                print(cleaned)
                print()
            prev_type = "text"

        elif block_type == "tool_use":
            assert isinstance(content, ToolUseContent)
            last_tool_name = content.name
            if show_tools:
                has_output = True
                agent_id = task_agent_map.get(content.id)
                agent_suffix = f"  {dim(f'-> agent-{agent_id}')}" if agent_id else ""
                if not full_detail:
                    summary = format_tool_summary(content.name, content.input)
                    summary_display = (
                        f" {dim(truncate_text(summary, 80))}" if summary else ""
                    )
                    print(green(f"[{content.name}]") + summary_display + agent_suffix)
                else:
                    print(green(f"[tool] {content.name}") + agent_suffix)
                    input_str = json.dumps(content.input, indent=2)
                    for line in input_str.split("\n"):
                        print(f"  {line}")
                    print()
                prev_type = "tool_use"

        elif block_type == "tool_result":
            is_feedback = last_tool_name in FEEDBACK_TOOLS
            if not show_tool_results and not show_hooks and not is_feedback:
                continue
            assert isinstance(content, ToolResultContent)
            is_hook_error = (
                show_hooks
                and content.is_error
                and isinstance(content.content, str)
                and HOOK_ERROR_RE.search(content.content) is not None
            )
            if show_tool_results or is_hook_error or is_feedback:
                has_output = True
                if is_feedback and not show_tools:
                    print(green(f"[{last_tool_name}]"))
                if is_hook_error:
                    print(yellow("[hook error]"))
                elif content.is_error:
                    print(yellow("[result] (error)"))
                else:
                    print(dim("[result]"))
                if isinstance(content.content, str):
                    text = content.content
                    show_full = full_detail or content.is_error or is_feedback
                    if show_full:
                        print(text if content.is_error else dim(text))
                    else:
                        lines = text.split("\n")
                        if len(lines) <= 20:
                            print(dim(text))
                        else:
                            print(dim("\n".join(lines[:20])))
                            print(dim(f"... ({len(lines) - 20} more lines)"))
                            if detail_hint and not hint_printed:
                                print(dim(f"  > {detail_hint}"))
                                hint_printed = True
                else:
                    print(dim(json.dumps(content.content, indent=2)))
                print()
                prev_type = "tool_result"

        elif block_type == "hook":
            if show_hooks:
                has_output = True
                assert isinstance(content, HookEvent)
                print(yellow(f"[hook: {content.hook_name}]"))
                if full_detail:
                    print(dim(content.command))
                    print()
                prev_type = "hook"

        elif block_type == "notification":
            has_output = True
            assert isinstance(content, TaskNotification)
            print(dim(f"[system] {content.summary}"))
            if full_detail and content.result:
                print(dim("[result]"))
                print(dim(content.result))
            print()
            prev_type = "notification"

    return has_output
