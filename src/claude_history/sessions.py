"""Session and compaction logic for claude-history."""

from __future__ import annotations

from claude_history.chain import extract_user_prompts
from claude_history.models import (
    DT_MIN,
    CompactBoundary,
    CompactionWindow,
    ProgressStub,
    Prompt,
    Record,
    Session,
    extract_content_text,
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
