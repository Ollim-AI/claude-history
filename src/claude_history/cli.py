"""
Claude Code Project History Navigator

Navigate conversation histories with a hierarchical approach for efficient token usage.
"""

from __future__ import annotations

import argparse
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from claude_history.agents import get_subagents, render_subagent_transcript, search_subagent_files
from claude_history.resolve import (
    find_prompt_across_projects,
    find_session_across_projects,
    find_subagent_across_projects,
    note_cross_project,
    resolve_project_dir,
    resolve_session_ref,
)
from claude_history.chain import (
    build_notification_map,
    build_record_indexes,
    build_task_agent_map,
    extract_ordered_content,
    extract_user_prompts,
    get_full_response,
)
from claude_history.io import (
    find_subagent_file,
    get_all_conversations,
    get_session_conversations,
    parse_jsonl_file,
)
from claude_history.models import (
    ALL_SEARCH_TARGETS,
    _CYAN,
    _RESET,
    _TEAMMATE_COLORS,
    _YELLOW,
    CLAUDE_PROJECTS_DIR,
    DT_MIN,
    PAGE_SIZE,
    ProgressStub,
    Record,
    SearchTarget,
    TeammateMessage,
    ToolUseContent,
    iter_user_records,
    parse_teammate_message,
    parse_timestamp,
)
from claude_history.render import (
    bold,
    cyan,
    cyan_bold,
    dim,
    format_duration,
    format_local,
    format_time,
    format_tokens,
    green,
    render_blocks,
    truncate_text,
    yellow,
)
from claude_history.search import highlight_match, prefilter_files, search_records
from claude_history.sessions import get_compactions, get_sessions, get_sessions_from_dir


def parse_since(value: str) -> datetime:
    """Parse a --since value into a timezone-aware datetime.

    Supports: Nm (minutes), Nh (hours), Nd (days), Nw (weeks),
    ISO dates (2024-01-15), and named shortcuts (today, yesterday).
    """
    now = datetime.now(timezone.utc)
    # Named shortcuts mark LOCAL calendar-day boundaries
    if value == "today":
        return datetime.now().astimezone().replace(
            hour=0, minute=0, second=0, microsecond=0
        )
    if value == "yesterday":
        return (datetime.now().astimezone() - timedelta(days=1)).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
    # Relative shorthand: Nm, Nh, Nd, Nw
    m = re.fullmatch(r"(\d+)([mhdw])", value)
    if m:
        n = int(m.group(1))
        unit = m.group(2)
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
    print(f"Error: Cannot parse --since value '{value}'", file=sys.stderr)
    print("  Examples: 3d, 1w, 24h, 30m, today, yesterday, 2024-01-15", file=sys.stderr)
    sys.exit(1)


# Command handlers


def _find_prompt_records(
    project_dir: Path, target_uuid: str
) -> tuple[list, dict | None]:
    """Two-pass load: find prompt UUID across files, reload with progress stubs.

    Returns (records_with_stubs, user_record) or (records, None) if not found.
    """
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
        filepath = project_dir / source_file
        records = parse_jsonl_file(filepath, include_progress_stubs=True)
        for r in records:
            if isinstance(r, dict):
                r["_source_file"] = source_file

    matching = [
        r
        for r in records
        if not isinstance(r, ProgressStub)
        and r.get("type") == "user"
        and r.get("uuid", "").startswith(target_uuid)
    ]
    return records, matching[0] if matching else None


def cmd_response(args: argparse.Namespace) -> None:
    """Handle the 'response' command with verbosity levels.

    Default: text only (no labels)
    -v: text + tool calls, interleaved with labels
    -vv: thinking + text + tool calls, interleaved with labels
    -vvv: full output with tool results, no truncation
    """
    project_dir = resolve_project_dir(args)

    target_uuid = args.uuid.lower()
    show_thinking = args.show_thinking
    show_tools = not args.hide_tools
    show_tool_results = not args.hide_tool_results
    full_detail = True  # response is a drill-down command — full detail by default
    show_hooks = args.show_hooks

    records, user_record = _find_prompt_records(project_dir, target_uuid)
    if not user_record:
        alt_project = find_prompt_across_projects(target_uuid, exclude_dir=project_dir)
        if alt_project:
            note_cross_project(alt_project)
            project_dir = alt_project
            records, user_record = _find_prompt_records(project_dir, target_uuid)
    if not user_record:
        print(f"Error: No user prompt found with UUID starting with '{target_uuid}' in any project", file=sys.stderr)
        print(f"  Hint: Try: claude-history transcript {target_uuid} (session or subagent)", file=sys.stderr)
        sys.exit(1)

    # Get full response chain
    chain = get_full_response(records, user_record["uuid"])

    if not chain:
        print(f"Error: No response found for prompt '{target_uuid}'", file=sys.stderr)
        sys.exit(1)

    # Get prompt timestamp for header
    dt = parse_timestamp(user_record.get("timestamp"))
    ts_str = format_local(dt, default="unknown")

    print(f"Response to: {cyan(user_record['uuid'][:8])} | {dim(ts_str)}\n")

    blocks = extract_ordered_content(chain, records, include_tool_results=True)
    task_agent_map = build_task_agent_map(records) if show_tools else {}
    detail_hint = ""

    if not render_blocks(
        blocks,
        task_agent_map,
        show_thinking=show_thinking,
        show_tools=show_tools,
        show_tool_results=show_tool_results,
        full_detail=full_detail,
        show_hooks=show_hooks,
        detail_hint=detail_hint,
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

    # Apply filters
    session_filter = getattr(args, "session", None)
    if session_filter:
        session_filter = session_filter.lower()
        subagents = [a for a in subagents if a.session_id.startswith(session_filter)]
    since = getattr(args, "since", None)
    if since:
        since_dt = parse_since(since)
        subagents = [a for a in subagents if a.earliest_timestamp and a.earliest_timestamp >= since_dt]

    if not subagents:
        print("No subagent files found.")
        return

    # Detail view for a specific agent
    agent_id = getattr(args, "agent_id", None)
    if agent_id:
        agent_id = agent_id.lower()
        # Match by agent_id prefix
        matches = [a for a in subagents if a.agent_id.startswith(agent_id)]
        if not matches:
            found = find_subagent_across_projects(agent_id, exclude_dir=project_dir)
            if found:
                note_cross_project(found[0])
                project_dir = found[0]
                subagents = get_subagents(project_dir)
                matches = [a for a in subagents if a.agent_id.startswith(agent_id)]
        if not matches:
            print(f"Error: No subagent found with ID starting with '{agent_id}' in any project", file=sys.stderr)
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
            preview = truncate_text(agent.prompt.replace("\n", " "), 80)
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

    # Detect agent ID (hex hash without dashes, not 'prev'); stored IDs are lowercase
    raw_id = args.identifier.split(":")[0].lower()
    if re.fullmatch(r'[0-9a-f]+', raw_id) and not raw_id.startswith("prev"):
        agent_file = find_subagent_file(project_dir, raw_id)
        if not agent_file:
            found = find_subagent_across_projects(raw_id, exclude_dir=project_dir)
            if found:
                project_dir, agent_file = found
                note_cross_project(project_dir)
        if agent_file:
            render_subagent_transcript(agent_file, args)
            return

    session_prefix, window_idx = resolve_session_ref(args.identifier, project_dir)

    prompts_only = args.prompts_only
    show_thinking = args.show_thinking
    show_tools = not args.hide_tools
    show_tool_results = not args.hide_tool_results
    full_detail = args.show_tool_results
    show_hooks = args.show_hooks
    show_system = getattr(args, "show_system", False)

    # Prompts-only doesn't need progress stubs (no chain traversal)
    include_stubs = not prompts_only
    records = get_session_conversations(project_dir, session_prefix, include_stubs)
    if records is None:
        # Filename IS the session UUID — if glob missed, try other projects before
        # expensive get_all_conversations fallback.
        alt_project = find_session_across_projects(session_prefix, exclude_dir=project_dir)
        if alt_project:
            note_cross_project(alt_project)
            project_dir = alt_project
            records = get_session_conversations(project_dir, session_prefix, include_stubs)
        if records is None:
            records = get_all_conversations(project_dir, include_stubs)
    sessions = get_sessions(records)

    matching_sessions = [s for s in sessions if s.session_id.startswith(session_prefix)]
    if not matching_sessions:
        if find_subagent_file(project_dir, session_prefix):
            print(f"Error: '{session_prefix}' is a subagent ID, not a session", file=sys.stderr)
            print(f"  Try: claude-history transcript {session_prefix}", file=sys.stderr)
        else:
            print(f"Error: No session found with ID starting with '{session_prefix}' in any project", file=sys.stderr)
        sys.exit(1)

    session = matching_sessions[0]
    session_id = session.session_id

    # Get compactions for this session
    compactions = get_compactions(records, session_id)

    if not compactions:
        print("Error: Session has no context windows", file=sys.stderr)
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
            zip(compactions, window_prompts, strict=False)
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
                f"Error: Context window {window_idx} out of range (0-{len(compactions) - 1})",
                file=sys.stderr,
            )
            sys.exit(1)
        windows = [(window_idx, compactions[window_idx])]
    else:
        windows = list(enumerate(compactions))

    # Build maps once for the entire session (not needed for prompts-only)
    task_agent_map = {} if prompts_only else build_task_agent_map(records)
    notif_map = {} if prompts_only else build_notification_map(records)

    # Collect teammate messages for this session
    teammate_msgs: list[TeammateMessage] = []
    if not prompts_only:
        for r in iter_user_records(records):
            if r.get("sessionId") != session_id:
                continue
            tm = parse_teammate_message(r)
            if tm:
                teammate_msgs.append(tm)

    if len(windows) > 1:
        print(f"Session: {cyan(session_id[:8])} ({len(windows)} context windows)\n")

    indexes = build_record_indexes(records) if not prompts_only else None

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
        timeline: list = list(user_prompts)
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
                chain = get_full_response(records, prompt.uuid, indexes=indexes)
                if chain:
                    blocks = extract_ordered_content(chain, records, include_tool_results=True, notification_map=notif_map)

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
                    detail_hint = f"claude-history response {prompt.uuid[:8]}" if not full_detail else ""
                    if not render_blocks(
                        blocks,
                        task_agent_map,
                        show_thinking=show_thinking,
                        show_tools=show_tools,
                        show_tool_results=show_tool_results,
                        full_detail=full_detail,
                        show_hooks=show_hooks,
                        detail_hint=detail_hint,
                    ):
                        print(dim("(no text content)"))
                        print()

                print(dim("---"))
                print()


def _render_session_line(session, use_iso: bool) -> str:
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
    name = session.custom_title or session.slug
    slug_str = f" {bold(name)}" if name else ""
    team_badge = ""
    if session.team_names:
        names = ", ".join(sorted(session.team_names))
        team_badge = f" | {yellow(f'[team: {names}]')}"
    hook_badge = ""
    if session.hook_error_count:
        hook_badge = f" | {yellow(f'[{session.hook_error_count} hook err]')}"
    return f"{session_short}{slug_str} | {ts_str} | {yellow(prompt_count)} {prompt_word} | {yellow(window_count)} ctx{team_badge}{hook_badge}{desc}"


def cmd_sessions(args: argparse.Namespace) -> None:
    """Handle the 'sessions' command."""
    project_dir = resolve_project_dir(args)

    # Stream files one at a time to avoid loading all records into memory
    sessions = get_sessions_from_dir(project_dir)

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
    if args.size is not None and args.size < 1:
        print(f"Error: --size must be a positive integer (got {args.size})", file=sys.stderr)
        sys.exit(1)
    page = args.page
    page_size = args.size if args.size is not None else PAGE_SIZE
    total_pages = (len(sessions) + page_size - 1) // page_size

    if page < 1 or page > total_pages:
        print(f"Error: Page {page} out of range (1-{total_pages})", file=sys.stderr)
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




def _parse_targets(args: argparse.Namespace) -> set[SearchTarget]:
    """Resolve --target, -p, -r flags into a set of SearchTarget values."""
    if args.target and (args.prompts_only or args.responses_only):
        print("Error: --target cannot be combined with -p/--prompts-only or -r/--responses-only", file=sys.stderr)
        sys.exit(1)

    if args.target:
        raw = [t.strip() for t in args.target.split(",") if t.strip()]
        if not raw:
            valid = ", ".join(sorted(ALL_SEARCH_TARGETS))
            print(f"Error: --target requires at least one value. Valid targets: {valid}", file=sys.stderr)
            sys.exit(1)
        targets: set[SearchTarget] = set()
        for name in raw:
            if name not in ALL_SEARCH_TARGETS:
                valid = ", ".join(sorted(ALL_SEARCH_TARGETS))
                print(f'Unknown target "{name}". Valid targets: {valid}', file=sys.stderr)
                sys.exit(1)
            targets.add(SearchTarget(name))
        return targets

    if args.prompts_only:
        return {SearchTarget.PROMPTS}
    if args.responses_only:
        return {SearchTarget.RESPONSES, SearchTarget.TOOLS, SearchTarget.HOOKS}

    valid = ", ".join(sorted(ALL_SEARCH_TARGETS))
    print("Error: search requires a target. Use -T/--target, -p, or -r.", file=sys.stderr)
    print(f"  Valid targets: {valid}", file=sys.stderr)
    print('  Example: claude-history search "query" -T prompts,tools', file=sys.stderr)
    sys.exit(1)


def cmd_search(args: argparse.Namespace) -> None:
    """Handle the 'search' command."""
    project_dir = resolve_project_dir(args)

    query = args.query
    case_sensitive = args.case_sensitive
    targets = _parse_targets(args)

    # Check for session ID matches (JSONL filename = session UUID)
    compare_q = query if case_sensitive else query.lower()
    session_matches = [
        f.stem
        for f in project_dir.glob("*.jsonl")
        if compare_q in (f.stem if case_sensitive else f.stem.lower())
    ]

    # Apply --since early to avoid scanning old files
    since_dt = parse_since(args.since) if args.since else None

    # Pre-filter files with grep for content matches
    matching_files = prefilter_files(project_dir, query, case_sensitive, since_dt)
    session_files = [f for f in matching_files if "/subagents/" not in str(f)]
    subagent_files = [f for f in matching_files if "/subagents/" in str(f)]
    matches = []
    if session_files:
        need_stubs = targets != {SearchTarget.PROMPTS}
        records = []
        for f in session_files:
            file_records = parse_jsonl_file(f, include_progress_stubs=need_stubs)
            for r in file_records:
                if isinstance(r, dict):
                    r["_source_file"] = f.name
            records.extend(file_records)
        matches = search_records(records, query, case_sensitive, targets)
    # Only search subagents when targets go beyond prompts-only
    if subagent_files and targets != {SearchTarget.PROMPTS}:
        matches.extend(
            search_subagent_files(subagent_files, query, case_sensitive, targets)
        )

    # Apply --since to parsed record timestamps (mtime was a coarse pre-filter)
    if since_dt:
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

    type_labels = {
        "prompt": green("[prompt]"),
        "tools": cyan("[tools]"),
        "tool-results": dim("[tool-result]"),
        "thinking": dim("[thinking]"),
        "hooks": yellow("[hooks]"),
        "subagent": yellow("[subagent]"),
    }
    for match in matches:
        uuid_short = cyan(match.uuid[:8])
        ts = (
            dim(format_time(match.timestamp, use_iso=args.timestamps))
            if match.timestamp
            else ""
        )
        match_type = type_labels.get(match.type, dim("[response]"))
        snippet = highlight_match(match.text, query, case_sensitive=case_sensitive)
        print(f"{uuid_short} | {ts} | {match_type}")
        print(f"  {snippet}")
        print()

    # Next-action hint for last match
    if matches:
        last = matches[-1]
        hint = f"transcript {last.uuid[:8]}" if last.type == "subagent" else f"response {last.uuid[:8]}"
        print(dim(f"  > {hint}"))




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
        "--project", help="Project directory name or path in ~/.claude/projects/ (names start with '-', so use --project=NAME)"
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
    response_detail_group = response_parser.add_mutually_exclusive_group()
    response_detail_group.add_argument(
        "--show-tool-results",
        action="store_true",
        help="Show full tool results and inputs (no truncation)",
    )
    response_detail_group.add_argument(
        "--hide-tool-results",
        action="store_true",
        help="Hide tool result blocks",
    )
    response_parser.add_argument(
        "--show-hooks", action="store_true", help="Show hook errors and context inline"
    )
    response_parser.add_argument(
        "--cwd", help="Working directory path to find project for"
    )
    response_parser.add_argument(
        "--project", help="Project directory name or path in ~/.claude/projects/ (names start with '-', so use --project=NAME)"
    )

    # subagents command
    subagents_parser = subparsers.add_parser("subagents", help="List subagent files")
    subagents_parser.add_argument(
        "agent_id", nargs="?", help="Agent ID prefix for detail view"
    )
    subagents_parser.add_argument(
        "--session", help="Filter by session ID prefix"
    )
    subagents_parser.add_argument(
        "--since", help="Show subagents since (e.g., 3d, 1w, 24h, today, 2024-01-15)"
    )
    subagents_parser.add_argument(
        "--cwd", help="Working directory path to find project for"
    )
    subagents_parser.add_argument(
        "--project", help="Project directory name or path in ~/.claude/projects/ (names start with '-', so use --project=NAME)"
    )

    # transcript command
    transcript_parser = subparsers.add_parser(
        "transcript", help="Show conversation transcript for a context window"
    )
    transcript_parser.add_argument(
        "identifier",
        help="Session ID, agent ID, latest, prev/prev-N, or session:window (e.g., latest, prev, 977a21c6:0)",
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
    transcript_detail_group = transcript_parser.add_mutually_exclusive_group()
    transcript_detail_group.add_argument(
        "--show-tool-results",
        action="store_true",
        help="Show full tool results and inputs (no truncation)",
    )
    transcript_detail_group.add_argument(
        "--hide-tool-results",
        action="store_true",
        help="Hide tool result blocks",
    )
    transcript_parser.add_argument(
        "--show-hooks", action="store_true", help="Show hook errors and context inline"
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
        "--project", help="Project directory name or path in ~/.claude/projects/ (names start with '-', so use --project=NAME)"
    )

    # search command
    search_parser = subparsers.add_parser("search", help="Search across all sessions")
    search_parser.add_argument("query", help="Text to search for")
    search_parser.add_argument(
        "-T",
        "--target",
        help="Content types to search, comma-separated: prompts,responses,tools,tool-results,thinking,hooks",
    )
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
        "--project", help="Project directory name or path in ~/.claude/projects/ (names start with '-', so use --project=NAME)"
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

    try:
        commands[args.command](args)
    except MemoryError:
        print(
            "Error: Out of memory. Try --since to limit scope (e.g., --since 1w).",
            file=sys.stderr,
        )
        sys.exit(137)


if __name__ == "__main__":
    main()
