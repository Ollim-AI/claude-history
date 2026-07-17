"""The 'subagents' command: list subagent threads or show one in detail."""

from __future__ import annotations

import argparse
import sys

from claude_history.agents import extract_subagent_metadata, get_subagents
from claude_history.io import iter_subagent_files, parse_subagent_file
from claude_history.render import (
    bold,
    cyan,
    dim,
    format_duration,
    format_tokens,
    green,
    truncate_text,
    yellow,
)
from claude_history.resolve import (
    find_subagent_across_projects,
    note_cross_project,
    parse_since,
    resolve_project_dir,
    resolve_session_ref,
)

# Agent-heavy projects hold thousands of subagents; an unpaged listing
# floods 15k+ lines
SUBAGENT_PAGE_SIZE = 20


def _subagent_detail(project_dir, agent_id: str) -> None:
    """Render the detail view for one subagent by direct file lookup.

    Parses only the matching file — the listing path parses every agent
    file in the project (minutes on agent-heavy projects).
    """
    matches = [
        p for p in iter_subagent_files(project_dir)
        if p.stem.replace("agent-", "").startswith(agent_id)
    ]
    agent_file = matches[0] if matches else None
    if len(matches) > 1:
        others = ", ".join(p.stem.replace("agent-", "") for p in matches[1:4])
        print(
            f"Note: {len(matches)} subagents match '{agent_id}';"
            f" showing {matches[0].stem.replace('agent-', '')} (others: {others})",
            file=sys.stderr,
        )
    if not agent_file:
        found = find_subagent_across_projects(agent_id, exclude_dir=project_dir)
        if found:
            note_cross_project(found[0])
            agent_file = found[1]
    if not agent_file:
        print(f"Error: No subagent found with ID starting with '{agent_id}' in any project", file=sys.stderr)
        sys.exit(1)
    records = parse_subagent_file(agent_file)
    if not records:
        print(f"Error: Subagent file has no readable records: {agent_file}", file=sys.stderr)
        sys.exit(1)
    agent = extract_subagent_metadata(agent_file, records)

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


def cmd_subagents(args: argparse.Namespace) -> None:
    """Handle the 'subagents' command.

    Without arguments: list all subagents with summary.
    With agent_id: show detailed view of a single subagent (listing
    filters like --session/--since do not apply to the detail view).
    """
    project_dir = resolve_project_dir(args)

    agent_id = getattr(args, "agent_id", None)
    if agent_id:
        _subagent_detail(project_dir, agent_id.lower())
        return

    subagents = get_subagents(project_dir)

    # Apply filters
    session_filter = getattr(args, "session", None)
    if session_filter:
        # Resolve latest/prev-N/slug references the same way transcript does
        session_prefix, _ = resolve_session_ref(session_filter, project_dir)
        subagents = [a for a in subagents if a.session_id.startswith(session_prefix)]
    since = getattr(args, "since", None)
    if since is not None:
        since_dt = parse_since(since)
        subagents = [a for a in subagents if a.earliest_timestamp and a.earliest_timestamp >= since_dt]

    if not subagents:
        print("No subagent files found.")
        return

    # Paginate (newest first, like sessions)
    if args.size is not None and args.size < 1:
        print(f"Error: --size must be a positive integer (got {args.size})", file=sys.stderr)
        sys.exit(1)
    page = args.page
    page_size = args.size if args.size is not None else SUBAGENT_PAGE_SIZE
    total_pages = (len(subagents) + page_size - 1) // page_size
    if page < 1 or page > total_pages:
        print(f"Error: Page {page} out of range (1-{total_pages})", file=sys.stderr)
        sys.exit(1)
    start_idx = (page - 1) * page_size
    page_agents = subagents[start_idx : start_idx + page_size]

    # Listing view
    print(f"Subagent threads (page {page}/{total_pages}, {len(subagents)} total):\n")

    for agent in page_agents:
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

    if page < total_pages:
        print(f"Page {page}/{total_pages}. Use --page {page + 1} for more.")
    if page_agents:
        print(dim(f"  > subagents {page_agents[0].agent_id}"))
