"""The 'subagents' command: list subagent threads or show one in detail."""

from __future__ import annotations

import argparse
import sys

from claude_history.agents import extract_subagent_metadata, get_subagents
from claude_history.io import find_subagent_file, parse_subagent_file
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
)


def _subagent_detail(project_dir, agent_id: str) -> None:
    """Render the detail view for one subagent by direct file lookup.

    Parses only the matching file — the listing path parses every agent
    file in the project (minutes on agent-heavy projects).
    """
    agent_file = find_subagent_file(project_dir, agent_id)
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
        session_filter = session_filter.lower()
        subagents = [a for a in subagents if a.session_id.startswith(session_filter)]
    since = getattr(args, "since", None)
    if since:
        since_dt = parse_since(since)
        subagents = [a for a in subagents if a.earliest_timestamp and a.earliest_timestamp >= since_dt]

    if not subagents:
        print("No subagent files found.")
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
