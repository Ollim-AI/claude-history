"""The 'search' command: find text across prompts, responses, and tool data."""

from __future__ import annotations

import argparse
import sys

from claude_history.agents import search_subagent_files
from claude_history.io import parse_jsonl_file
from claude_history.models import ALL_SEARCH_TARGETS, DT_MIN, SearchTarget, die
from claude_history.render import cyan, dim, format_time, green, yellow
from claude_history.resolve import parse_since, resolve_project_dir
from claude_history.search import highlight_match, prefilter_files, search_records


def _parse_targets(args: argparse.Namespace) -> set[SearchTarget]:
    """Resolve --target, -p, -r flags into a set of SearchTarget values."""
    if args.target and (args.prompts_only or args.responses_only):
        die("Error: --target cannot be combined with -p/--prompts-only or -r/--responses-only")
    if args.prompts_only and args.responses_only:
        die(
            "Error: -p/--prompts-only cannot be combined with -r/--responses-only (use -T to search multiple targets)",
        )

    if args.target:
        raw = [t.strip() for t in args.target.split(",") if t.strip()]
        if not raw:
            valid = ", ".join(sorted(ALL_SEARCH_TARGETS))
            die(f"Error: --target requires at least one value. Valid targets: {valid}")
        targets: set[SearchTarget] = set()
        for name in raw:
            if name not in ALL_SEARCH_TARGETS:
                valid = ", ".join(sorted(ALL_SEARCH_TARGETS))
                die(f'Error: Unknown target "{name}". Valid targets: {valid}')
            targets.add(SearchTarget(name))
        return targets

    if args.prompts_only:
        return {SearchTarget.PROMPTS}
    if args.responses_only:
        return {SearchTarget.RESPONSES, SearchTarget.TOOLS, SearchTarget.HOOKS}

    valid = ", ".join(sorted(ALL_SEARCH_TARGETS))
    die(
        "Error: search requires a target. Use -T/--target, -p, or -r.",
        f"  Valid targets: {valid}",
        '  Example: claude-history search "query" -T prompts,tools',
    )


def cmd_search(args: argparse.Namespace) -> None:
    """Handle the 'search' command."""
    project_dir = resolve_project_dir(args)

    query = args.query
    if not query.strip():
        die("Error: search query must not be empty")
    if args.limit < 1:
        die(f"Error: --limit must be a positive integer (got {args.limit})")
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
    since_dt = parse_since(args.since) if args.since is not None else None

    # Pre-filter files with grep for content matches
    matching_files = prefilter_files(project_dir, query, case_sensitive, since_dt)
    if len(matching_files) > 100:
        print(
            f"Searching {len(matching_files)} matching files"
            " (narrow with --since or -p for prompts only)...",
            file=sys.stderr,
        )
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
    if subagent_files:
        matches.extend(
            search_subagent_files(subagent_files, query, case_sensitive, targets)
        )
        matches.sort(key=lambda m: m.timestamp or DT_MIN, reverse=True)

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

    total = len(matches)
    if total > args.limit:
        matches = matches[: args.limit]
        print(f'Found {yellow(total)} match(es) for "{query}", showing newest {args.limit} (raise with --limit N, narrow with --since):\n')
    else:
        print(f'Found {yellow(total)} match(es) for "{query}":\n')

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
        session_short = dim(f"s:{match.session_id[:8]}") if match.session_id else dim("s:?")
        ts = (
            dim(format_time(match.timestamp, use_iso=args.timestamps))
            if match.timestamp
            else ""
        )
        match_type = type_labels.get(match.type, dim("[response]"))
        snippet = highlight_match(match.text, query, case_sensitive=case_sensitive)
        print(f"{uuid_short} | {session_short} | {ts} | {match_type}")
        print(f"  {snippet}")
        print()

    # Next-action hint for last match
    if matches:
        last = matches[-1]
        hint = f"transcript {last.uuid[:8]}" if last.type == "subagent" else f"response {last.uuid[:8]}"
        print(dim(f"  > {hint}"))
