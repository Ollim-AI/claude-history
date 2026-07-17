"""The 'response' command: full-detail drill-down into one prompt's response."""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

from claude_history.chain import (
    build_task_agent_map,
    extract_ordered_content,
    get_full_response,
)
from claude_history.io import get_all_conversations, parse_jsonl_file
from claude_history.models import ProgressStub, parse_timestamp
from claude_history.render import cyan, dim, format_local, render_blocks
from claude_history.resolve import (
    find_prompt_across_projects,
    note_cross_project,
    resolve_project_dir,
)


def _load_prompt_file(filepath: Path, target_uuid: str) -> tuple[list, dict | None]:
    """Parse one session file (with stubs) and find the prompt by UUID prefix."""
    records = parse_jsonl_file(filepath, include_progress_stubs=True)
    for r in records:
        if isinstance(r, dict):
            r["_source_file"] = filepath.name
    matching = [
        r
        for r in records
        if not isinstance(r, ProgressStub)
        and r.get("type") == "user"
        and r.get("uuid", "").startswith(target_uuid)
    ]
    return records, matching[0] if matching else None


def _find_prompt_records(
    project_dir: Path, target_uuid: str
) -> tuple[list, dict | None]:
    """Locate the session file containing the prompt via grep, parse only it.

    Greps session files newest-first instead of parsing the whole project
    (a large project takes ~20s to parse; one file takes milliseconds).
    Returns (records_with_stubs, user_record) or ([], None) if not found.
    """
    files = sorted(
        project_dir.glob("*.jsonl"), key=lambda f: f.stat().st_mtime, reverse=True
    )
    if not files:
        return [], None
    try:
        result = subprocess.run(
            ["grep", "-l", "-s", "-F", "-m", "1", "--", target_uuid,
             *(str(f) for f in files)],
            capture_output=True, text=True,
        )
        hits = [Path(line) for line in result.stdout.strip().splitlines()]
    except OSError:
        # grep unavailable — fall back to parsing the whole project
        records = get_all_conversations(project_dir, include_progress_stubs=False)
        for r in records:
            if isinstance(r, ProgressStub):
                continue
            if r.get("type") == "user" and r.get("uuid", "").startswith(target_uuid):
                source = r.get("_source_file")
                if source:
                    return _load_prompt_file(project_dir / source, target_uuid)
        return [], None

    # The grep needle is a prefix that may also occur in non-user records;
    # parse hits (newest first) until one holds a matching user prompt.
    for filepath in hits:
        records, user_record = _load_prompt_file(filepath, target_uuid)
        if user_record:
            return records, user_record
    return [], None


def cmd_response(args: argparse.Namespace) -> None:
    """Handle the 'response' command: full detail for one prompt's response."""
    project_dir = resolve_project_dir(args)

    target_uuid = args.uuid.lower()
    show_thinking = args.show_thinking
    show_tools = not args.hide_tools
    show_tool_results = not args.hide_tool_results
    full_detail = True  # response is a drill-down command — full detail by default
    show_hooks = args.show_hooks

    records, user_record = _find_prompt_records(project_dir, target_uuid)
    if not user_record:
        found = find_prompt_across_projects(target_uuid, exclude_dir=project_dir)
        if found:
            alt_project, alt_file = found
            note_cross_project(alt_project)
            project_dir = alt_project
            records, user_record = _load_prompt_file(alt_file, target_uuid)
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
