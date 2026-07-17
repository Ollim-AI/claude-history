"""Session and compaction logic for claude-history."""

from __future__ import annotations

from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from pathlib import Path

from claude_history.chain import extract_user_prompts, is_user_text_prompt
from claude_history.io import parse_jsonl_file
from claude_history.models import (
    DT_MIN,
    CompactBoundary,
    CompactionWindow,
    ProgressStub,
    Prompt,
    Record,
    Session,
    extract_content_text,
    is_hook_error_block,
    parse_timestamp,
)


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
    implicit_boundary_times: list = []
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
    all_boundary_times: set = set()
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
        # Teammate-driven or tool-only sessions have records but no typed
        # prompts; give them one synthetic window so transcript can render
        # the timeline (teammate messages) instead of erroring while the
        # sessions listing advertises '1 ctx'.
        has_records = any(
            not isinstance(r, ProgressStub) and r.get("sessionId") == session_id
            for r in records
        )
        if not has_records:
            return []
        return [
            CompactionWindow(
                start_time=None, end_time=None, prompt_count=0, prompts=()
            )
        ]

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

    # Last activity = max timestamp across every record (independent of prompts)
    dt = parse_timestamp(record.get("timestamp"))
    if dt and (sess.latest_timestamp is None or dt > sess.latest_timestamp):
        sess.latest_timestamp = dt

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

    # Capture custom title (user-set session name via --resume/--name)
    if record_type == "custom-title":
        sess.custom_title = record.get("customTitle")
        return

    if record_type != "user":
        return
    if record.get("isCompactSummary"):
        return

    # Count hook errors in tool_result blocks
    content = record.get("message", {}).get("content", [])
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and is_hook_error_block(block):
                sess.hook_error_count += 1

    # Track implicit boundaries (null parent = new context window)
    if record.get("parentUuid") is None:
        dt = parse_timestamp(record.get("timestamp"))
        if dt:
            sess.implicit_boundaries.add(dt)

    if not is_user_text_prompt(record):
        return
    prompt_text = extract_content_text(record.get("message", {}).get("content", []))
    if not prompt_text:
        return
    sess.prompt_count += 1

    dt = parse_timestamp(record.get("timestamp"))
    if dt:
        if sess.first_prompt is None or dt < sess.first_prompt[0]:
            sess.first_prompt = (dt, prompt_text)


def _accumulate_records(
    records: Iterable[Record], sessions: dict[str, Session]
) -> None:
    """Accumulate records into session metadata dict."""
    for record in records:
        if isinstance(record, ProgressStub):
            continue
        session_id = record.get("sessionId", "unknown")
        if session_id == "unknown":
            continue
        if session_id not in sessions:
            sessions[session_id] = Session(session_id=session_id)
        _accumulate_session_record(sessions[session_id], record)


def _sorted_sessions(sessions: dict[str, Session]) -> list[Session]:
    session_list = list(sessions.values())
    session_list.sort(key=lambda x: x.latest_timestamp or DT_MIN, reverse=True)
    return session_list


def get_sessions(records: list[Record]) -> list[Session]:
    """Extract session metadata from conversation records.

    Groups records by sessionId and returns metadata for each session.
    Only counts user messages with actual text content (not tool_result messages).
    """
    sessions: dict[str, Session] = {}
    _accumulate_records(records, sessions)
    return _sorted_sessions(sessions)


def get_sessions_from_dir(project_dir: Path) -> list[Session]:
    """Build session metadata by streaming files one at a time.

    Unlike get_sessions() which requires all records in memory at once,
    this processes files via executor.map — each file's records are parsed,
    accumulated into Session objects, then discarded before the next file.

    Peak memory: O(largest_single_file) instead of O(all_files).
    """
    files = list(project_dir.glob("*.jsonl"))
    if not files:
        return []

    sessions: dict[str, Session] = {}

    with ThreadPoolExecutor(max_workers=min(8, len(files))) as executor:
        for file_records in executor.map(
            partial(parse_jsonl_file, include_progress_stubs=False), files
        ):
            _accumulate_records(file_records, sessions)

    return _sorted_sessions(sessions)
