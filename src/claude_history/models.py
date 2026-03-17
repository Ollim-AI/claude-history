"""Data models, constants, and leaf-level utilities for claude-history."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from collections.abc import Iterator
from typing import Literal, NamedTuple

_RESET = "\033[0m"
_BOLD = "\033[1m"
_DIM = "\033[2m"
_CYAN = "\033[36m"
_YELLOW = "\033[33m"
_GREEN = "\033[32m"

BlockType = Literal["thinking", "text", "tool_use", "tool_result", "notification", "hook"]

HOOK_ERROR_RE = re.compile(r"^(PreToolUse|PostToolUse):\w+ hook error:", re.MULTILINE)
_HOOK_CONTEXT_RE = re.compile(
    r"<system-reminder>\s*"
    r"((?:PreToolUse|PostToolUse):\S+ hook additional context:.*?)"
    r"\s*</system-reminder>",
    re.DOTALL,
)


def extract_hook_contexts(text: str) -> list[str]:
    """Extract hook context strings from <system-reminder> tags in text."""
    return [m.group(1).strip() for m in _HOOK_CONTEXT_RE.finditer(text)]


def is_hook_error_block(block: dict) -> bool:
    """Check if a tool_result content block is a hook error."""
    if block.get("type") != "tool_result" or not block.get("is_error"):
        return False
    text = block.get("content", "")
    return isinstance(text, str) and HOOK_ERROR_RE.search(text) is not None


@dataclass(frozen=True, slots=True)
class ProgressStub:
    uuid: str
    parentUuid: str | None
    parentToolUseID: str | None
    agentId: str | None


@dataclass(frozen=True, slots=True)
class Prompt:
    uuid: str
    text: str
    timestamp: datetime | None
    source_file: str
    session_id: str
    slug: str
    is_tool_result: bool
    has_assistant_child: bool

    @property
    def is_user_prompt(self) -> bool:
        return self.has_assistant_child and not self.is_tool_result


@dataclass(frozen=True, slots=True)
class CompactBoundary:
    uuid: str | None
    timestamp: datetime | None
    trigger: str | None
    pre_tokens: int | None


@dataclass(frozen=True, slots=True)
class CompactionWindow:
    start_time: datetime | None
    end_time: datetime | None
    prompt_count: int
    prompts: tuple[Prompt, ...]


@dataclass(slots=True)
class Session:
    session_id: str
    prompt_count: int = 0
    latest_timestamp: datetime | None = None
    implicit_boundaries: set[datetime] = field(default_factory=set)
    explicit_boundaries: set[datetime] = field(default_factory=set)
    slug: str | None = None
    custom_title: str | None = None
    first_prompt: tuple[datetime, str] | None = None
    team_names: set[str] = field(default_factory=set)
    hook_error_count: int = 0


@dataclass(frozen=True, slots=True)
class HookEvent:
    hook_name: str  # e.g. "PreToolUse:Read", "SubagentStop"
    hook_event: str  # e.g. "PreToolUse", "SubagentStop"
    command: str  # hook command or prompt text


STOP_HOOK_FEEDBACK_PREFIX = "Stop hook feedback:"


@dataclass(frozen=True, slots=True)
class ToolUseContent:
    id: str
    name: str
    input: dict


@dataclass(frozen=True, slots=True)
class ToolResultContent:
    content: str | list
    is_error: bool = False


@dataclass(frozen=True, slots=True)
class ContentBlock:
    type: BlockType
    content: str | ToolUseContent | ToolResultContent | TaskNotification | HookEvent


@dataclass(frozen=True, slots=True)
class ToolInfo:
    name: str
    id: str
    arg_summary: str


@dataclass(frozen=True, slots=True)
class SubagentMetadata:
    filepath: Path
    filename: str
    agent_id: str
    session_id: str
    slug: str
    prompt: str
    model: str
    model_full: str
    record_count: int
    earliest_timestamp: datetime | None
    latest_timestamp: datetime | None
    duration: float | None
    total_input_tokens: int
    total_output_tokens: int
    tools: tuple[ToolInfo, ...]
    errors: tuple[str, ...]
    response_text: str
    teammate_name: str | None = None


@dataclass(frozen=True, slots=True)
class SearchMatch:
    type: Literal["prompt", "response", "subagent"]
    uuid: str
    session_id: str
    timestamp: datetime | None
    text: str


@dataclass(frozen=True, slots=True)
class TaskNotification:
    task_id: str
    status: str
    summary: str
    result: str
    usage: str


_TASK_NOTIFICATION_RE = re.compile(
    r"<task-notification>\s*"
    r"<task-id>([^<]+)</task-id>"
    r".*?"  # optional tags: <tool-use-id>, <output-file> (v2.1.74+)
    r"<status>([^<]+)</status>\s*"
    r"<summary>([^<]*)</summary>\s*"
    r"<result>(.*?)</result>\s*"
    r"(?:<usage>(.*?)</usage>\s*)?"
    r"</task-notification>",
    re.DOTALL,
)


def parse_task_notification(text: str) -> TaskNotification | None:
    """Parse a <task-notification> XML string into a TaskNotification."""
    m = _TASK_NOTIFICATION_RE.search(text)
    if not m:
        return None
    return TaskNotification(
        task_id=m.group(1).strip(),
        status=m.group(2).strip(),
        summary=m.group(3).strip(),
        result=m.group(4).strip(),
        usage=(m.group(5) or "").strip(),
    )


Record = dict | ProgressStub


def iter_user_records(records: list[Record]) -> Iterator[dict]:
    """Yield non-ProgressStub user records."""
    for record in records:
        if isinstance(record, ProgressStub):
            continue
        if record.get("type") == "user":
            yield record

CLAUDE_PROJECTS_DIR = Path.home() / ".claude" / "projects"
PAGE_SIZE = 10
DT_MIN = datetime.min.replace(tzinfo=timezone.utc)

# --- Agent teams support ---

_BLUE = "\033[34m"
_PURPLE = "\033[35m"
_TEAMMATE_COLORS: dict[str, str] = {
    "yellow": _YELLOW,
    "blue": _BLUE,
    "green": _GREEN,
    "purple": _PURPLE,
}


class TeammateMessage(NamedTuple):
    teammate_id: str
    color: str | None
    summary: str | None
    body: str
    body_type: str  # "text", "idle", "task", "shutdown"
    uuid: str
    timestamp: datetime | None


_TEAMMATE_MSG_RE = re.compile(
    r"<teammate-message\s+"
    r'teammate_id="([^"]*)"'
    r'(?:\s+color="([^"]*)")?'
    r'(?:\s+summary="([^"]*)")?'
    r"[^>]*>"
    r"(.*?)"
    r"</teammate-message>",
    re.DOTALL,
)


# --- Leaf utilities (no internal module dependencies) ---


def parse_timestamp(ts: str | None) -> datetime | None:
    """Parse an ISO timestamp string into a timezone-aware datetime."""
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except (ValueError, AttributeError):
        return None


def strip_system_tags(text: str) -> str:
    """Strip system-reminder and other injected tags from text."""
    text = re.sub(r"<system-reminder>.*?</system-reminder>", "", text, flags=re.DOTALL)
    # Clean up excess whitespace from removed tags
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def extract_content_text(content: str | list) -> str:
    """Extract text from content (can be string or list of blocks).

    Strips system-reminder tags that get injected into user messages
    (e.g., slash command expansions) to show only the actual prompt text.
    """
    if isinstance(content, str):
        return strip_system_tags(content)

    if not isinstance(content, list):
        return ""

    text_parts = []
    for block in content:
        if isinstance(block, dict) and block.get("type") == "text":
            text = block.get("text", "")
            text = strip_system_tags(text)
            if text:
                text_parts.append(text)
        elif isinstance(block, str):
            text = strip_system_tags(block)
            if text:
                text_parts.append(text)

    return " ".join(text_parts)


def parse_teammate_message(record: dict) -> TeammateMessage | None:
    """Parse a teammate-message user record into a TeammateMessage."""
    content = record.get("message", {}).get("content", "")
    if not isinstance(content, str):
        return None
    m = _TEAMMATE_MSG_RE.search(content)
    if not m:
        return None
    body = m.group(4).strip()
    body_type = "text"
    if body.startswith("{"):
        try:
            j = json.loads(body)
        except json.JSONDecodeError:
            j = None
        if isinstance(j, dict):
            t = j.get("type", "")
            if t == "idle_notification":
                body_type = "idle"
            elif t == "task_assignment":
                body_type = "task"
            elif t in ("shutdown_request", "shutdown_response", "shutdown_approved"):
                body_type = "shutdown"
    return TeammateMessage(
        teammate_id=m.group(1),
        color=m.group(2),
        summary=m.group(3),
        body=body,
        body_type=body_type,
        uuid=record.get("uuid", ""),
        timestamp=parse_timestamp(record.get("timestamp")),
    )
