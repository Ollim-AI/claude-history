"""The 'sessions' command: list conversation sessions with metadata."""

from __future__ import annotations

import argparse
import sys

from claude_history.models import PAGE_SIZE
from claude_history.render import bold, cyan, dim, format_time, yellow
from claude_history.resolve import parse_since, resolve_project_dir
from claude_history.sessions import get_sessions_from_dir


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

    # Apply --since filter ('' must reach parse_since and error, not no-op)
    if args.since is not None:
        since_dt = parse_since(args.since)
        sessions = [
            s for s in sessions if s.latest_timestamp and s.latest_timestamp >= since_dt
        ]
        if not sessions:
            local = since_dt.astimezone().strftime("%Y-%m-%d %H:%M")
            print(f"No sessions since {local}.")
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
