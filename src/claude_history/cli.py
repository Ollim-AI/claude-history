"""
Claude Code Project History Navigator

Navigate conversation histories with a hierarchical approach for efficient token usage.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal, NamedTuple

_RESET = "\033[0m"
_BOLD = "\033[1m"
_DIM = "\033[2m"
_CYAN = "\033[36m"
_YELLOW = "\033[33m"
_GREEN = "\033[32m"


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


BlockType = Literal["thinking", "text", "tool_use", "tool_result"]


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
    first_prompt: tuple[datetime, str] | None = None
    team_names: set[str] = field(default_factory=set)


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
    content: str | ToolUseContent | ToolResultContent


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
    type: Literal["prompt", "response"]
    uuid: str
    session_id: str
    timestamp: datetime | None
    text: str


Record = dict | ProgressStub

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


def parse_since(value: str) -> datetime:
    """Parse a --since value into a timezone-aware datetime.

    Supports: Nm (minutes), Nh (hours), Nd (days), Nw (weeks),
    ISO dates (2024-01-15), and named shortcuts (today, yesterday).
    """
    now = datetime.now(timezone.utc)
    # Named shortcuts
    if value == "today":
        return now.replace(hour=0, minute=0, second=0, microsecond=0)
    if value == "yesterday":
        return (now - __import__("datetime").timedelta(days=1)).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
    # Relative shorthand: Nm, Nh, Nd, Nw
    m = re.fullmatch(r"(\d+)([mhdw])", value)
    if m:
        n = int(m.group(1))
        unit = m.group(2)
        from datetime import timedelta

        deltas = {
            "m": timedelta(minutes=n),
            "h": timedelta(hours=n),
            "d": timedelta(days=n),
            "w": timedelta(weeks=n),
        }
        return now - deltas[unit]
    # ISO date
    dt = parse_timestamp(value)
    if dt:
        return dt
    print(f"Error: Cannot parse --since value '{value}'")
    print("  Examples: 3d, 1w, 24h, 30m, today, yesterday, 2024-01-15")
    sys.exit(1)


def encode_path(path: str) -> str:
    """Encode a path for use as a project directory name.

    /home/user/Code/foo -> -home-user-Code-foo
    /home/user/.claude -> -home-user--claude (dots replaced with dashes)
    """
    # Replace dots, slashes, backslashes, and colons with dashes
    return path.replace(".", "-").replace("/", "-").replace("\\", "-").replace(":", "-")


def get_project_dir(cwd: str | None = None) -> Path | None:
    """Find the Claude projects directory for a given working directory.

    Encodes the cwd and checks if a matching project directory exists.
    If not, walks up the path hierarchy and tries again until found or root reached.
    """
    if cwd is None:
        cwd = os.getcwd()

    current = Path(cwd).resolve()

    while True:
        encoded = encode_path(str(current))
        project_dir = CLAUDE_PROJECTS_DIR / encoded

        if project_dir.exists():
            return project_dir

        # Move up one level
        parent = current.parent
        if parent == current:
            # Reached filesystem root
            return None
        current = parent


def strip_system_tags(text: str) -> str:
    """Strip system-reminder and other injected tags from text."""
    text = re.sub(r"<system-reminder>.*?</system-reminder>", "", text, flags=re.DOTALL)
    # Clean up excess whitespace from removed tags
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def truncate_text(text: str, max_length: int = 500) -> str:
    """Truncate text showing first and last parts if too long."""
    text = text.replace("\n", " ").strip()
    if len(text) <= max_length:
        return text
    half = (max_length - 3) // 2
    return text[:half] + yellow("…") + text[-half:]


def _extract_progress_stub(line: str) -> ProgressStub | None:
    """Extract lightweight progress stub from a raw JSONL line.

    Uses fixed field positions for speed:
    - parentUuid: always in first 200 bytes
    - uuid, parentToolUseID, agentId: always in last 250 bytes
    The top-level uuid is at end-86 (not the first "uuid" in the line,
    which is a nested UUID inside data.message).
    """
    head = line[:200]
    tail = line[-250:]
    uuid_m = re.search(r'"uuid":"([^"]+)"', tail)
    if not uuid_m:
        return None
    parent_m = re.search(r'"parentUuid":"([^"]+)"', head)
    ptid_m = re.search(r'"parentToolUseID":"([^"]+)"', tail)
    aid_m = re.search(r'"agentId":"([^"]+)"', tail)
    return ProgressStub(
        uuid=uuid_m.group(1),
        parentUuid=parent_m.group(1) if parent_m else None,
        parentToolUseID=ptid_m.group(1) if ptid_m else None,
        agentId=aid_m.group(1) if aid_m else None,
    )


def parse_jsonl_file(
    filepath: Path, include_progress_stubs: bool = True
) -> list[Record]:
    """Parse a JSONL file and return list of records.

    Uses grep to filter out 'progress' records (subagent transcripts) which are
    ~99% of file size. grep is ~5x faster than Python for scanning large files.

    Args:
        filepath: Path to the JSONL file
        include_progress_stubs: If True, also extract lightweight progress stubs
            (uuid, parentUuid, parentToolUseID, agentId) for chain traversal.
            Set to False for commands that don't need response chain following
            (sessions, prompts, search -p) for a major speedup.
    """
    records = []
    try:
        # Use grep -F -f - to pipe the pattern via stdin, avoiding Windows
        # argument quoting issues with double quotes in patterns
        result = subprocess.run(
            ["grep", "-F", "-v", "-f", "-", str(filepath)],
            input='"type":"progress"\n',
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        skipped = 0
        for line in result.stdout.splitlines():
            if line.strip():
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError:
                    skipped += 1
        if skipped:
            print(
                f"Warning: skipped {skipped} malformed line(s) in {filepath}",
                file=sys.stderr,
            )

        # Add lightweight progress stubs for chain traversal
        if include_progress_stubs:
            with open(filepath, "r", encoding="utf-8", errors="replace") as f:
                for line in f:
                    if '"type":"progress"' not in line:
                        continue
                    stub = _extract_progress_stub(line)
                    if stub:
                        records.append(stub)
    except (OSError, subprocess.SubprocessError):
        # Fallback to Python if grep is unavailable
        try:
            with open(filepath, "r", encoding="utf-8", errors="replace") as f:
                for line in f:
                    line_stripped = line.strip()
                    if not line_stripped:
                        continue
                    if '"type":"progress"' in line:
                        if include_progress_stubs:
                            stub = _extract_progress_stub(line)
                            if stub:
                                records.append(stub)
                        continue
                    try:
                        records.append(json.loads(line_stripped))
                    except json.JSONDecodeError:
                        skipped += 1
            if skipped:
                print(
                    f"Warning: skipped {skipped} malformed line(s) in {filepath}",
                    file=sys.stderr,
                )
        except OSError as e:
            print(f"Warning: failed to read {filepath}: {e}", file=sys.stderr)
    return records


def get_all_conversations(
    project_dir: Path, include_progress_stubs: bool = True
) -> list[Record]:
    """Get all conversation records from a project directory."""
    jsonl_files = list(project_dir.glob("*.jsonl"))

    def parse_with_source(filepath):
        file_records = parse_jsonl_file(filepath, include_progress_stubs)
        for record in file_records:
            if isinstance(record, dict):
                record["_source_file"] = filepath.name
        return file_records

    # Parse files in parallel
    records = []
    with ThreadPoolExecutor(max_workers=len(jsonl_files)) as executor:
        for file_records in executor.map(parse_with_source, jsonl_files):
            records.extend(file_records)

    return records


def get_session_conversations(
    project_dir: Path, session_prefix: str, include_progress_stubs: bool = True
) -> list[Record] | None:
    """Get conversation records for a specific session by direct file lookup.

    The JSONL filename IS the session UUID, so we can glob directly instead
    of parsing all files. Returns None if no matching file found (caller
    should fall back to get_all_conversations).
    """
    matching = list(project_dir.glob(f"{session_prefix}*.jsonl"))
    if not matching:
        return None
    filepath = matching[0]
    records = parse_jsonl_file(filepath, include_progress_stubs)
    for r in records:
        if isinstance(r, dict):
            r["_source_file"] = filepath.name
    return records


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


def get_compact_boundaries(
    records: list[Record], session_id: str | None = None
) -> list[CompactBoundary]:
    """Find all compact_boundary system records."""
    boundaries: list[CompactBoundary] = []
    for r in records:
        if isinstance(r, ProgressStub):
            continue
        if r.get("type") == "system" and r.get("subtype") == "compact_boundary":
            if session_id and r.get("sessionId") != session_id:
                continue
            dt = parse_timestamp(r.get("timestamp"))
            metadata = r.get("compactMetadata", {})
            boundaries.append(
                CompactBoundary(
                    uuid=r.get("uuid"),
                    timestamp=dt,
                    trigger=metadata.get("trigger"),
                    pre_tokens=metadata.get("preTokens"),
                )
            )
    return sorted(boundaries, key=lambda x: x.timestamp or DT_MIN)


def get_compactions(records: list[Record], session_id: str) -> list[CompactionWindow]:
    """Group prompts into context windows based on compaction boundaries.

    Detects both explicit boundaries (compact_boundary records) and implicit
    boundaries (user records with parentUuid=null indicating context reset).
    """
    # Get explicit boundaries (compact_boundary records)
    explicit_boundaries = get_compact_boundaries(records, session_id)

    # Find implicit boundaries (user records with parentUuid=null)
    implicit_boundary_times: list[datetime] = []
    for record in records:
        if isinstance(record, ProgressStub):
            continue
        if record.get("sessionId") != session_id:
            continue
        if record.get("type") != "user":
            continue
        if record.get("parentUuid") is None:
            dt = parse_timestamp(record.get("timestamp"))
            if dt:
                implicit_boundary_times.append(dt)

    # Merge boundary times (explicit + implicit, skipping first implicit which is session start)
    all_boundary_times: set[datetime] = set()
    for b in explicit_boundaries:
        if b.timestamp:
            all_boundary_times.add(b.timestamp)
    for t in implicit_boundary_times[1:]:  # Skip first (session start)
        all_boundary_times.add(t)

    boundary_times = sorted(all_boundary_times)

    # Get prompts for this session
    prompts = extract_user_prompts(records)
    session_prompts = [p for p in prompts if p.session_id == session_id]
    session_prompts.sort(key=lambda x: x.timestamp or DT_MIN)

    if not session_prompts:
        return []

    # Group prompts into context windows
    compactions: list[CompactionWindow] = []
    current_window: list[Prompt] = []

    for prompt in session_prompts:
        prompt_time = prompt.timestamp

        # Check if this prompt is after any unprocessed boundary
        while (
            boundary_times
            and prompt_time
            and boundary_times[0]
            and prompt_time >= boundary_times[0]
        ):
            # Save current window before this boundary
            if current_window:
                compactions.append(
                    CompactionWindow(
                        start_time=current_window[0].timestamp,
                        end_time=current_window[-1].timestamp,
                        prompt_count=len(current_window),
                        prompts=tuple(current_window),
                    )
                )
            current_window = []
            boundary_times.pop(0)

        current_window.append(prompt)

    # Add final window
    if current_window:
        compactions.append(
            CompactionWindow(
                start_time=current_window[0].timestamp,
                end_time=current_window[-1].timestamp,
                prompt_count=len(current_window),
                prompts=tuple(current_window),
            )
        )

    return compactions


def _accumulate_session_record(sess: Session, record: dict) -> None:
    """Update a Session from a single raw record."""
    record_type = record.get("type")

    # Track explicit compaction boundaries
    if record_type == "system" and record.get("subtype") == "compact_boundary":
        dt = parse_timestamp(record.get("timestamp"))
        if dt:
            sess.explicit_boundaries.add(dt)
        return

    # Collect team names
    team_name = record.get("teamName")
    if team_name:
        sess.team_names.add(team_name)

    # Capture slug from any record
    slug = record.get("slug")
    if slug and not sess.slug:
        sess.slug = slug

    if record_type != "user":
        return
    if record.get("isCompactSummary"):
        return

    # Track implicit boundaries (null parent = new context window)
    if record.get("parentUuid") is None:
        dt = parse_timestamp(record.get("timestamp"))
        if dt:
            sess.implicit_boundaries.add(dt)

    # Count prompts with text content
    prompt_text = extract_content_text(record.get("message", {}).get("content", []))
    if not prompt_text:
        return

    is_text_prompt = (
        not record.get("isMeta") and "sourceToolAssistantUUID" not in record
    )
    if is_text_prompt:
        sess.prompt_count += 1

    dt = parse_timestamp(record.get("timestamp"))
    if dt:
        if sess.latest_timestamp is None or dt > sess.latest_timestamp:
            sess.latest_timestamp = dt
        if is_text_prompt:
            if sess.first_prompt is None or dt < sess.first_prompt[0]:
                sess.first_prompt = (dt, prompt_text)


def get_sessions(records: list[Record]) -> list[Session]:
    """Extract session metadata from conversation records.

    Groups records by sessionId and returns metadata for each session.
    Only counts user messages with actual text content (not tool_result messages).
    """
    sessions: dict[str, Session] = {}

    for record in records:
        if isinstance(record, ProgressStub):
            continue
        session_id = record.get("sessionId", "unknown")
        if session_id == "unknown":
            continue
        if session_id not in sessions:
            sessions[session_id] = Session(session_id=session_id)
        _accumulate_session_record(sessions[session_id], record)

    # Sort by latest activity descending
    session_list = list(sessions.values())
    session_list.sort(key=lambda x: x.latest_timestamp or DT_MIN, reverse=True)

    return session_list


def parse_subagent_file(filepath: Path) -> list[dict]:
    """Parse a subagent JSONL file fully (no progress filtering).

    Subagent files are small enough to parse completely, unlike main session
    files where progress records dominate.
    """
    records = []
    skipped = 0
    try:
        with open(filepath, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        records.append(json.loads(line))
                    except json.JSONDecodeError:
                        skipped += 1
        if skipped:
            print(
                f"Warning: skipped {skipped} malformed line(s) in {filepath}",
                file=sys.stderr,
            )
    except OSError as e:
        print(f"Warning: failed to read {filepath}: {e}", file=sys.stderr)
    return records


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


def render_blocks(
    blocks: list[ContentBlock],
    task_agent_map: dict[str, str],
    *,
    show_thinking: bool = False,
    show_tools: bool = True,
    show_tool_results: bool = False,
) -> bool:
    """Render content blocks with flag-controlled detail.

    Returns True if any content was printed.
    """
    has_output = False
    prev_type = None
    for block in blocks:
        block_type = block.type
        content = block.content

        if block_type == "thinking":
            if show_thinking:
                has_output = True
                assert isinstance(content, str)
                text = content.strip()
                print(dim("[thinking]"))
                if show_tool_results or len(text) <= 2000:
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
            if show_thinking:
                print(cyan("[text]"))
            print(content.strip())
            print()
            prev_type = "text"

        elif block_type == "tool_use":
            if show_tools:
                has_output = True
                assert isinstance(content, ToolUseContent)
                agent_id = task_agent_map.get(content.id)
                agent_suffix = f"  {dim(f'-> agent-{agent_id}')}" if agent_id else ""
                if not show_tool_results:
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
            if show_tool_results:
                has_output = True
                assert isinstance(content, ToolResultContent)
                if content.is_error:
                    print(yellow("[result] (error)"))
                else:
                    print(dim("[result]"))
                if isinstance(content.content, str):
                    print(
                        dim(content.content)
                        if not content.is_error
                        else content.content
                    )
                else:
                    print(dim(json.dumps(content.content, indent=2)))
                print()
                prev_type = "tool_result"

    return has_output


# Command handlers


def resolve_project_dir(args: argparse.Namespace) -> Path:
    """Resolve project directory from args (--project or --cwd). Exits on failure."""
    if hasattr(args, "project") and args.project:
        result = Path(args.project)
    else:
        cwd = getattr(args, "cwd", None)
        result = get_project_dir(cwd)
    if result is not None and result.exists():
        return result
    print("Error: No project directory found. Use --project to specify one.")
    sys.exit(1)



def cmd_response(args: argparse.Namespace) -> None:
    """Handle the 'response' command with verbosity levels.

    Default: text only (no labels)
    -v: text + tool calls, interleaved with labels
    -vv: thinking + text + tool calls, interleaved with labels
    -vvv: full output with tool results, no truncation
    """
    project_dir = resolve_project_dir(args)

    target_uuid = args.uuid
    show_thinking = args.show_thinking
    show_tools = not args.hide_tools
    show_tool_results = args.show_tool_results

    # Fast UUID lookup: load non-progress records from all files (small data),
    # find which file has the UUID, then reload that file with progress stubs.
    records = get_all_conversations(project_dir, include_progress_stubs=False)
    user_record = None
    source_file = None
    for r in records:
        if isinstance(r, ProgressStub):
            continue
        if r.get("type") == "user" and r.get("uuid", "").startswith(target_uuid):
            user_record = r
            source_file = r.get("_source_file")
            break

    if user_record and source_file:
        # Reload just that file with progress stubs for chain traversal
        filepath = project_dir / source_file
        records = parse_jsonl_file(filepath, include_progress_stubs=True)
        for r in records:
            if isinstance(r, dict):
                r["_source_file"] = source_file

    # Find matching user record (re-search after reload)
    matching = [
        r
        for r in records
        if not isinstance(r, ProgressStub)
        and r.get("type") == "user"
        and r.get("uuid", "").startswith(target_uuid)
    ]
    if not matching:
        print(f"Error: No user prompt found with UUID starting with '{target_uuid}'")
        sys.exit(1)
    user_record = matching[0]

    # Get full response chain
    chain = get_full_response(records, user_record["uuid"])

    if not chain:
        print(f"Error: No response found for prompt '{target_uuid}'")
        sys.exit(1)

    # Get prompt timestamp for header
    dt = parse_timestamp(user_record.get("timestamp"))
    ts_str = format_local(dt, default="unknown")

    print(f"Response to: {cyan(user_record['uuid'][:8])} | {dim(ts_str)}\n")

    blocks = extract_ordered_content(chain, records if show_tool_results else None)
    task_agent_map = build_task_agent_map(records) if show_tools else {}

    if not render_blocks(
        blocks,
        task_agent_map,
        show_thinking=show_thinking,
        show_tools=show_tools,
        show_tool_results=show_tool_results,
    ):
        print("No content in response.")

    # Next-action hint
    session_id = user_record.get("sessionId", "")
    if session_id:
        print(dim(f"  > transcript {session_id[:8]}"))


def cmd_subagents(args: argparse.Namespace) -> None:
    """Handle the 'subagents' command.

    Without arguments: list all subagents with summary.
    With agent_id: show detailed view of a single subagent.
    """
    project_dir = resolve_project_dir(args)

    subagents = get_subagents(project_dir)

    if not subagents:
        print("No subagent files found.")
        return

    # Detail view for a specific agent
    agent_id = getattr(args, "agent_id", None)
    if agent_id:
        # Match by agent_id prefix
        matches = [a for a in subagents if a.agent_id.startswith(agent_id)]
        if not matches:
            print(f"Error: No subagent found with ID starting with '{agent_id}'")
            sys.exit(1)
        agent = matches[0]

        # Header
        session_short = cyan(agent.session_id[:8])
        print(f"{bold(agent.filename)}  (session: {session_short})")
        print(f"Model: {agent.model_full}", end="")
        if agent.duration is not None:
            print(f" | Duration: {format_duration(agent.duration)}", end="")
        if agent.total_input_tokens or agent.total_output_tokens:
            print(
                f" | Tokens: {format_tokens(agent.total_input_tokens)} in / {format_tokens(agent.total_output_tokens)} out",
                end="",
            )
        print()

        # Prompt
        if agent.prompt:
            print(f"\n{cyan('Prompt:')}")
            prompt_lines = agent.prompt.split("\n")
            for line in prompt_lines[:20]:
                print(f"  {line}")
            if len(prompt_lines) > 20:
                print(dim(f"  ... ({len(prompt_lines) - 20} more lines)"))

        # Tool timeline
        if agent.tools:
            print(f"\n{cyan(f'Tools ({len(agent.tools)} calls):')}")
            for i, tool in enumerate(agent.tools, 1):
                arg = truncate_text(tool.arg_summary, 80) if tool.arg_summary else ""
                if arg:
                    print(f"  {dim(f'{i:>3}.')} {green(tool.name)}  {arg}")
                else:
                    print(f"  {dim(f'{i:>3}.')} {green(tool.name)}")

        # Response text
        if agent.response_text:
            print(f"\n{cyan('Response:')}")
            for line in agent.response_text.split("\n"):
                print(f"  {line}")

        # Errors
        if agent.errors:
            print(f"\n{yellow(f'Errors ({len(agent.errors)}):')}")
            for err in agent.errors:
                print(f"  {err}")
        else:
            print(f"\n{dim('Errors: none')}")

        print()
        return

    # Listing view
    print(f"Subagent threads ({len(subagents)}):\n")

    for agent in subagents:
        session_short = cyan(agent.session_id[:8])
        label = ""
        if agent.teammate_name:
            label = f"  {cyan(f'[{agent.teammate_name}]')}"
        print(f"  {bold(agent.agent_id)}{label}  (session: {session_short})")

        parts: list[str] = []
        parts.append(agent.model)
        if agent.duration is not None:
            parts.append(format_duration(agent.duration))
        parts.append(f"{agent.record_count} records")
        error_count = len(agent.errors)
        if error_count:
            parts.append(yellow(f"{error_count} errors"))
        else:
            parts.append("0 errors")

        print(f"    {' | '.join(parts)}")
        if agent.prompt:
            preview = truncate_text(agent.prompt, 80)
            print(f"    {dim(preview)}")
        print()

    if subagents:
        print(dim(f"  > subagents {subagents[0].agent_id}"))


def cmd_transcript(args: argparse.Namespace) -> None:
    """Handle the 'transcript' command.

    Default: prompts + responses + tool calls
    --prompts-only: only user prompts
    --show-thinking: + thinking blocks
    --hide-tools: hide tool calls
    --show-tool-results: + tool results (full detail, no truncation)
    """
    project_dir = resolve_project_dir(args)

    session_prefix, window_idx = resolve_session_ref(args.identifier, project_dir)

    prompts_only = args.prompts_only
    show_thinking = args.show_thinking
    show_tools = not args.hide_tools
    show_tool_results = args.show_tool_results
    show_system = getattr(args, "show_system", False)

    # Prompts-only doesn't need progress stubs (no chain traversal)
    records = get_session_conversations(
        project_dir, session_prefix, include_progress_stubs=not prompts_only
    )
    if records is None:
        records = get_all_conversations(
            project_dir, include_progress_stubs=not prompts_only
        )
    sessions = get_sessions(records)

    # Find matching session
    matching_sessions = [s for s in sessions if s.session_id.startswith(session_prefix)]
    if not matching_sessions:
        print(f"Error: No session found with ID starting with '{session_prefix}'")
        sys.exit(1)

    session = matching_sessions[0]
    session_id = session.session_id

    # Get compactions for this session
    compactions = get_compactions(records, session_id)

    if not compactions:
        print("Error: Session has no context windows")
        sys.exit(1)

    # Prompts-only with multiple windows and no specific index: compact listing
    if prompts_only and len(compactions) >= 2 and window_idx is None:
        # Pre-filter once per compaction, accumulate total
        window_prompts = [
            [p for p in c.prompts if p.is_user_prompt] for c in compactions
        ]
        total = sum(len(wp) for wp in window_prompts)
        print(
            f"Session: {cyan(session_id[:8])} ({yellow(total)} prompts across {yellow(len(compactions))} context windows)\n"
        )
        for i, (compaction, user_prompts) in enumerate(
            zip(compactions, window_prompts)
        ):
            start_str = format_local(compaction.start_time, "%Y-%m-%d %H:%M", "?")
            end_str = format_local(compaction.end_time, "%H:%M", "?")
            time_range = dim(f"{start_str} - {end_str}")
            print(
                f"{cyan_bold(f'[{i}]')} {time_range} | {yellow(len(user_prompts))} prompts"
            )
            for prompt in user_prompts[:3]:
                preview = truncate_text(prompt.text, 100)
                print(f'    "{preview}"')
            remaining = len(user_prompts) - 3
            if remaining > 0:
                print(dim(f"    ... {remaining} more"))
            print()
        print(dim(f"  > transcript {session_id[:8]}:0 --prompts-only"))
        return

    # Determine which windows to render
    if window_idx is not None:
        if window_idx < 0 or window_idx >= len(compactions):
            print(
                f"Error: Context window {window_idx} out of range (0-{len(compactions) - 1})"
            )
            sys.exit(1)
        windows = [(window_idx, compactions[window_idx])]
    else:
        windows = list(enumerate(compactions))

    # Build Task->subagent map once (not needed for prompts-only)
    task_agent_map = {} if prompts_only else build_task_agent_map(records)

    # Collect teammate messages for this session
    teammate_msgs: list[TeammateMessage] = []
    if not prompts_only:
        for r in records:
            if isinstance(r, ProgressStub):
                continue
            if r.get("type") != "user" or r.get("sessionId") != session_id:
                continue
            tm = parse_teammate_message(r)
            if tm:
                teammate_msgs.append(tm)

    if len(windows) > 1:
        print(f"Session: {cyan(session_id[:8])} ({len(windows)} context windows)\n")

    for wi, compaction in windows:
        user_prompts = [
            p
            for p in compaction.prompts
            if p.is_user_prompt
        ]

        # Merge teammate messages into timeline for this window.
        # Use boundary-based assignment: a message belongs to the last
        # window whose start_time <= message timestamp.  This handles
        # messages that arrive between user prompts (after window's
        # last prompt but before the next window's first prompt).
        timeline: list[Prompt | TeammateMessage] = list(user_prompts)
        if teammate_msgs:
            next_start = (
                compactions[wi + 1].start_time
                if wi + 1 < len(compactions)
                else None
            )
            ws = compaction.start_time
            for tm in teammate_msgs:
                if not tm.timestamp:
                    continue
                if ws and tm.timestamp < ws:
                    continue
                if next_start and tm.timestamp >= next_start:
                    continue
                timeline.append(tm)
            timeline.sort(key=lambda x: x.timestamp or DT_MIN)

        prompt_count = len(user_prompts)

        # Print header per window
        if len(windows) > 1:
            print(
                f"{bold(f'=== Context window {wi} ===')} ({prompt_count} prompts)\n"
            )
        else:
            print(f"Session: {cyan(session_id[:8])} | Context window {yellow(wi)}\n")

        active_team = ""
        for item in timeline:
            if isinstance(item, TeammateMessage):
                # Skip protocol messages unless --show-system
                if item.body_type != "text" and not show_system:
                    continue
                color_code = _TEAMMATE_COLORS.get(item.color or "", _CYAN)
                label = f"{color_code}[{item.teammate_id}]{_RESET}"
                ts_str = (
                    dim(format_local(item.timestamp, "%Y-%m-%d %H:%M"))
                    if item.timestamp
                    else ""
                )
                print(f"{label} {ts_str}")
                if item.body_type != "text":
                    # Protocol message — dim one-liner
                    print(dim(f"  {item.body_type}"))
                else:
                    print(item.body)
                print()
                if not prompts_only:
                    print(dim("---"))
                    print()
                continue

            # Regular user prompt
            prompt = item
            ts_str = (
                dim(format_local(prompt.timestamp, "%Y-%m-%d %H:%M"))
                if prompt.timestamp
                else ""
            )
            print(f"{cyan_bold('[user]')} {ts_str}")
            print(prompt.text)
            print()

            if not prompts_only:
                chain = get_full_response(records, prompt.uuid)
                if chain:
                    blocks = extract_ordered_content(chain, records if show_tool_results else None)

                    # Team phase separators
                    for block in blocks:
                        if block.type == "tool_use" and isinstance(block.content, ToolUseContent):
                            if block.content.name == "TeamCreate":
                                tn = block.content.input.get("team_name", "")
                                active_team = tn
                                print(f"  {bold(f'─── team: {tn} ───')}\n")
                            elif block.content.name == "TeamDelete":
                                tn = active_team or ""
                                active_team = ""
                                if tn:
                                    print(f"  {bold(f'─── end team: {tn} ───')}\n")
                                else:
                                    print(f"  {bold('─── end team ───')}\n")

                    print(green("[assistant]"))
                    if not render_blocks(
                        blocks,
                        task_agent_map,
                        show_thinking=show_thinking,
                        show_tools=show_tools,
                        show_tool_results=show_tool_results,
                    ):
                        print(dim("(no text content)"))
                        print()

                print(dim("---"))
                print()


def _render_session_line(session: Session, use_iso: bool) -> str:
    """Format a single session for the sessions list display."""
    session_short = cyan(session.session_id[:8])
    ts_str = (
        dim(format_time(session.latest_timestamp, use_iso=use_iso))
        if session.latest_timestamp
        else dim("unknown")
    )
    prompt_count = session.prompt_count
    prompt_word = "prompt" if prompt_count == 1 else "prompts"
    # Window count = implicit boundaries (skip first, which is session start) + explicit boundaries + 1
    all_boundaries = session.explicit_boundaries.union(session.implicit_boundaries)
    window_count = len(all_boundaries) if all_boundaries else 1
    # Session description: first prompt preview
    desc = ""
    if session.first_prompt:
        first_text = session.first_prompt[1].replace("\n", " ").strip()
        if len(first_text) > 50:
            first_text = first_text[:50] + "..."
        desc = f" | {dim(first_text)}"
    team_badge = ""
    if session.team_names:
        names = ", ".join(sorted(session.team_names))
        team_badge = f" | {yellow(f'[team: {names}]')}"
    return f"{session_short} | {ts_str} | {yellow(prompt_count)} {prompt_word} | {yellow(window_count)} ctx{team_badge}{desc}"


def cmd_sessions(args: argparse.Namespace) -> None:
    """Handle the 'sessions' command."""
    project_dir = resolve_project_dir(args)

    # Get all conversations and extract sessions (no progress stubs needed)
    records = get_all_conversations(project_dir, include_progress_stubs=False)
    sessions = get_sessions(records)

    if not sessions:
        print("No sessions found in project history.")
        return

    # Apply --since filter
    if args.since:
        since_dt = parse_since(args.since)
        sessions = [
            s for s in sessions if s.latest_timestamp and s.latest_timestamp >= since_dt
        ]
        if not sessions:
            print(f"No sessions since {since_dt.strftime('%Y-%m-%d %H:%M')}.")
            return

    # Paginate
    page = args.page
    page_size = args.size or PAGE_SIZE
    total_pages = (len(sessions) + page_size - 1) // page_size

    if page < 1 or page > total_pages:
        print(f"Error: Page {page} out of range (1-{total_pages})")
        sys.exit(1)

    start_idx = (page - 1) * page_size
    end_idx = min(start_idx + page_size, len(sessions))
    page_sessions = sessions[start_idx:end_idx]

    print(f"Sessions (page {page}/{total_pages}):\n")

    for session in page_sessions:
        print(_render_session_line(session, args.timestamps))

    print()
    if total_pages > 1:
        if page < total_pages:
            print(f"Page {page}/{total_pages}. Use --page {page + 1} for more.")
        else:
            print(f"Page {page}/{total_pages}.")
    if page_sessions:
        first_id = page_sessions[0].session_id[:8]
        print(dim(f"  > transcript {first_id}"))


def prefilter_files(
    project_dir: Path, query: str, case_sensitive: bool = False
) -> list[Path]:
    """Use grep to find JSONL files containing the query in non-progress records.

    Pipes grep -v to exclude progress records (which contain embedded conversation
    text from subagent context and cause false positives), then checks for the query.
    """
    jsonl_files = list(project_dir.glob("*.jsonl"))
    if not jsonl_files:
        return []
    matching = []
    case_flag = [] if case_sensitive else ["-i"]
    for f in jsonl_files:
        try:
            # Filter out progress records, then search for query
            # Use -f - to pipe pattern via stdin (avoids Windows quoting issues)
            p1 = subprocess.Popen(
                ["grep", "-F", "-v", "-f", "-", str(f)],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
            )
            p1.stdin.write(b'"type":"progress"\n')
            p1.stdin.close()
            p2 = subprocess.Popen(
                ["grep", "-F", "-q"] + case_flag + ["--", query],
                stdin=p1.stdout,
                stdout=subprocess.PIPE,
            )
            p1.stdout.close()
            p2.communicate()
            if p2.returncode == 0:
                matching.append(f)
        except (OSError, subprocess.SubprocessError):
            # SubprocessError catches TimeoutExpired from communicate()
            matching.append(f)  # Fallback: include file if grep fails
    return matching


def highlight_match(text: str, query: str, context_chars: int = 60) -> str:
    """Show a snippet around the first match with the query highlighted."""
    text_flat = text.replace("\n", " ").strip()
    idx = text_flat.lower().find(query.lower())
    if idx == -1:
        return truncate_text(text_flat, context_chars * 2)
    start = max(0, idx - context_chars)
    end = min(len(text_flat), idx + len(query) + context_chars)
    snippet = ""
    if start > 0:
        snippet += "..."
    snippet += text_flat[start:idx]
    snippet += _YELLOW + text_flat[idx : idx + len(query)] + _RESET
    snippet += text_flat[idx + len(query) : end]
    if end < len(text_flat):
        snippet += "..."
    return snippet


def search_records(
    records: list[Record],
    query: str,
    case_sensitive: bool,
    search_prompts: bool,
    search_responses: bool,
) -> list[SearchMatch]:
    """Search through records for matching text in prompts and/or responses."""
    matches: list[SearchMatch] = []
    compare = (lambda t: t) if case_sensitive else (lambda t: t.lower())
    q = compare(query)
    seen: set[str] = set()

    prompts = extract_user_prompts(records)
    # Filter to user-typed prompts only
    prompts = [p for p in prompts if p.is_user_prompt]

    for prompt in prompts:
        prompt_text = prompt.text
        uuid = prompt.uuid

        # Search prompt text
        if search_prompts and q in compare(prompt_text):
            if uuid not in seen:
                seen.add(uuid)
                matches.append(
                    SearchMatch(
                        type="prompt",
                        uuid=uuid,
                        session_id=prompt.session_id,
                        timestamp=prompt.timestamp,
                        text=prompt_text,
                    )
                )

        # Search response text + tool calls
        if search_responses:
            chain = get_full_response(records, uuid)
            response_text = extract_all_text(chain)
            tools = extract_all_tools(chain)
            tool_text = " ".join(
                f"{t['name']} {format_tool_summary(t['name'], t['input'])}"
                for t in tools
            )
            full_text = f"{response_text} {tool_text}".strip()
            if full_text and q in compare(full_text):
                if ("r:" + uuid) not in seen:
                    seen.add("r:" + uuid)
                    matches.append(
                        SearchMatch(
                            type="response",
                            uuid=uuid,
                            session_id=prompt.session_id,
                            timestamp=prompt.timestamp,
                            text=full_text,
                        )
                    )

    matches.sort(key=lambda m: m.timestamp or DT_MIN, reverse=True)
    return matches


def cmd_search(args: argparse.Namespace) -> None:
    """Handle the 'search' command.

    Searches across all sessions for matching text in prompts and/or responses.
    Uses grep pre-filtering for performance.
    """
    project_dir = resolve_project_dir(args)

    query = args.query
    case_sensitive = args.case_sensitive
    search_prompts = not args.responses_only
    search_responses = not args.prompts_only

    # Check for session ID matches (JSONL filename = session UUID)
    compare_q = query if case_sensitive else query.lower()
    session_matches = [
        f.stem
        for f in project_dir.glob("*.jsonl")
        if compare_q in (f.stem if case_sensitive else f.stem.lower())
    ]

    # Pre-filter files with grep for content matches
    matching_files = prefilter_files(project_dir, query, case_sensitive)
    matches = []
    if matching_files:
        need_stubs = search_responses
        records = []
        for f in matching_files:
            file_records = parse_jsonl_file(f, include_progress_stubs=need_stubs)
            for r in file_records:
                if isinstance(r, dict):
                    r["_source_file"] = f.name
            records.extend(file_records)
        matches = search_records(
            records, query, case_sensitive, search_prompts, search_responses
        )

    # Apply --since filter
    if args.since:
        since_dt = parse_since(args.since)
        matches = [m for m in matches if m.timestamp and m.timestamp >= since_dt]

    if not matches and not session_matches:
        print(f'No matches for "{query}"')
        return

    if session_matches:
        print(f'Sessions matching "{query}":\n')
        for sid in session_matches:
            print(f"  {cyan(sid[:8])}")
        print(f"\n{dim('  > transcript ' + session_matches[0][:8])}\n")

    if not matches:
        return

    print(f'Found {yellow(len(matches))} match(es) for "{query}":\n')

    for match in matches:
        uuid_short = cyan(match.uuid[:8])
        ts = (
            dim(format_time(match.timestamp, use_iso=args.timestamps))
            if match.timestamp
            else ""
        )
        match_type = green("[prompt]") if match.type == "prompt" else dim("[response]")
        snippet = highlight_match(match.text, query)
        print(f"{uuid_short} | {ts} | {match_type}")
        print(f"  {snippet}")
        print()

    # Next-action hint
    if matches:
        first = matches[0]
        print(dim(f"  > response {first.uuid[:8]}"))


def get_recent_session_ids(project_dir: Path, count: int = 10) -> list[str]:
    """Get session IDs from JSONL files sorted by modification time (most recent first).

    Extracts the first sessionId from each file without full parsing.
    Much faster than get_sessions() for just resolving IDs.
    """
    jsonl_files = sorted(
        project_dir.glob("*.jsonl"), key=lambda f: f.stat().st_mtime, reverse=True
    )
    session_ids = []
    seen = set()
    for f in jsonl_files[: count * 2]:  # Check extra files in case of duplicates
        try:
            with open(f, "r", encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    m = re.search(r'"sessionId":"([^"]+)"', line)
                    if m:
                        sid = m.group(1)
                        if sid not in seen:
                            seen.add(sid)
                            session_ids.append(sid)
                        break
        except OSError:
            continue
        if len(session_ids) >= count:
            break
    return session_ids


def resolve_session_ref(identifier: str, project_dir: Path) -> tuple[str, int | None]:
    """Resolve a session reference like 'prev', 'prev-2', 'prev-3:1', or a UUID prefix.

    Returns (session_prefix, context_window_index_or_None).
    """
    ctx_window = None
    if ":" in identifier:
        base, idx = identifier.rsplit(":", 1)
        if idx.isdigit():
            ctx_window = int(idx)
            identifier = base

    if identifier == "prev":
        n = 1
    elif identifier.startswith("prev-") and identifier[5:].isdigit():
        n = int(identifier[5:])
        if n < 1:
            print("Error: prev-N requires N >= 1 (prev-1 = previous session).")
            sys.exit(1)
    else:
        return (identifier, ctx_window)

    session_ids = get_recent_session_ids(project_dir, count=n + 1)
    if len(session_ids) <= n:
        print(
            f"Error: Only {len(session_ids)} sessions found, cannot resolve prev-{n}."
        )
        sys.exit(1)
    return (session_ids[n][:8], ctx_window)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Claude Code Project History Navigator",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    # sessions command
    sessions_parser = subparsers.add_parser(
        "sessions", help="List conversation sessions"
    )
    sessions_parser.add_argument(
        "--page", type=int, default=1, help="Page number (default: 1)"
    )
    sessions_parser.add_argument(
        "--size", type=int, help="Number of sessions per page (default: 10)"
    )
    sessions_parser.add_argument(
        "--cwd", help="Working directory path to find project for"
    )
    sessions_parser.add_argument(
        "--project", help="Direct project directory path in ~/.claude/projects/"
    )
    sessions_parser.add_argument(
        "-t",
        "--timestamps",
        action="store_true",
        help="Show ISO timestamps instead of relative times",
    )
    sessions_parser.add_argument(
        "--since", help="Show sessions since (e.g., 3d, 1w, 24h, today, 2024-01-15)"
    )

    # response command
    response_parser = subparsers.add_parser(
        "response", help="Read Claude's response for a prompt"
    )
    response_parser.add_argument("uuid", help="Prompt UUID (or prefix)")
    response_parser.add_argument(
        "--show-thinking", action="store_true", help="Include thinking blocks"
    )
    response_parser.add_argument(
        "--hide-tools", action="store_true", help="Hide tool call blocks"
    )
    response_parser.add_argument(
        "--show-tool-results",
        action="store_true",
        help="Include tool results (full detail, no truncation)",
    )
    response_parser.add_argument(
        "--cwd", help="Working directory path to find project for"
    )
    response_parser.add_argument(
        "--project", help="Direct project directory path in ~/.claude/projects/"
    )

    # subagents command
    subagents_parser = subparsers.add_parser("subagents", help="List subagent files")
    subagents_parser.add_argument(
        "agent_id", nargs="?", help="Agent ID prefix for detail view"
    )
    subagents_parser.add_argument(
        "--cwd", help="Working directory path to find project for"
    )
    subagents_parser.add_argument(
        "--project", help="Direct project directory path in ~/.claude/projects/"
    )

    # transcript command
    transcript_parser = subparsers.add_parser(
        "transcript", help="Show conversation transcript for a context window"
    )
    transcript_parser.add_argument(
        "identifier",
        help="Session ID, prev/prev-N, or session:window (e.g., prev, 977a21c6:0)",
    )
    transcript_parser.add_argument(
        "--prompts-only",
        action="store_true",
        help="Show only user prompts (no assistant responses)",
    )
    transcript_parser.add_argument(
        "--show-thinking", action="store_true", help="Include thinking blocks"
    )
    transcript_parser.add_argument(
        "--hide-tools", action="store_true", help="Hide tool call blocks"
    )
    transcript_parser.add_argument(
        "--show-tool-results",
        action="store_true",
        help="Include tool results (full detail, no truncation)",
    )
    transcript_parser.add_argument(
        "--show-system",
        action="store_true",
        help="Show team protocol messages (idle notifications, task assignments, shutdown requests)",
    )
    transcript_parser.add_argument(
        "--cwd", help="Working directory path to find project for"
    )
    transcript_parser.add_argument(
        "--project", help="Direct project directory path in ~/.claude/projects/"
    )

    # search command
    search_parser = subparsers.add_parser("search", help="Search across all sessions")
    search_parser.add_argument("query", help="Text to search for")
    search_parser.add_argument(
        "-p", "--prompts-only", action="store_true", help="Search prompts only (faster)"
    )
    search_parser.add_argument(
        "-r", "--responses-only", action="store_true", help="Search responses only"
    )
    search_parser.add_argument(
        "--case-sensitive",
        action="store_true",
        help="Case-sensitive search (default: insensitive)",
    )
    search_parser.add_argument(
        "--cwd", help="Working directory path to find project for"
    )
    search_parser.add_argument(
        "--project", help="Direct project directory path in ~/.claude/projects/"
    )
    search_parser.add_argument(
        "-t",
        "--timestamps",
        action="store_true",
        help="Show ISO timestamps instead of relative times",
    )
    search_parser.add_argument(
        "--since", help="Show matches since (e.g., 3d, 1w, 24h, today, 2024-01-15)"
    )

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(0)

    commands = {
        "sessions": cmd_sessions,
        "response": cmd_response,
        "subagents": cmd_subagents,
        "transcript": cmd_transcript,
        "search": cmd_search,
    }

    commands[args.command](args)


if __name__ == "__main__":
    main()
