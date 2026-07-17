"""The 'transcript' command: render session or subagent conversations."""

from __future__ import annotations

import argparse
import re
import sys

from claude_history.agents import render_subagent_transcript
from claude_history.chain import (
    build_notification_map,
    build_record_indexes,
    build_task_agent_map,
    extract_ordered_content,
    get_full_response,
)
from claude_history.io import (
    find_subagent_file,
    get_all_conversations,
    get_session_conversations,
)
from claude_history.models import (
    _CYAN,
    _RESET,
    _TEAMMATE_COLORS,
    DT_MIN,
    TeammateMessage,
    ToolUseContent,
    iter_user_records,
    parse_teammate_message,
)
from claude_history.render import (
    bold,
    cyan,
    cyan_bold,
    dim,
    format_local,
    green,
    render_blocks,
    truncate_text,
    yellow,
)
from claude_history.resolve import (
    find_session_across_projects,
    find_subagent_across_projects,
    note_cross_project,
    resolve_project_dir,
    resolve_session_ref,
)
from claude_history.sessions import get_compactions, get_sessions


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

        if not timeline:
            print(dim("(no user prompts in this window; team protocol messages may exist — try --show-system)"))
            print()

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
