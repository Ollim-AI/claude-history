"""
Claude Code Project History Navigator

Argument parsing and dispatch; command implementations live in
claude_history.commands (one module per subcommand).
"""

from __future__ import annotations

import argparse
import os
import sys

from claude_history.commands import (
    cmd_response,
    cmd_search,
    cmd_sessions,
    cmd_subagents,
    cmd_transcript,
)

# Re-exports for compatibility: these previously lived in this module
from claude_history.commands.response import _find_prompt_records, _load_prompt_file  # noqa: F401
from claude_history.commands.search import _parse_targets  # noqa: F401
from claude_history.resolve import parse_since  # noqa: F401
from claude_history.search import highlight_match  # noqa: F401


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
        "--page", type=int, default=1, help="Page number (default: 1)"
    )
    subagents_parser.add_argument(
        "--size", type=int, help="Number of subagents per page (default: 20)"
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
    search_parser.add_argument(
        "--limit",
        type=int,
        default=50,
        help="Maximum matches to display, newest first (default: 50)",
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
    except BrokenPipeError:
        # Downstream closed the pipe (e.g. | head) — normal truncation, not an
        # error. Redirect stdout to devnull so the interpreter's final flush
        # doesn't print "Exception ignored"; exit 141 = 128+SIGPIPE convention.
        try:
            os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        except OSError:
            pass
        sys.exit(141)
    except KeyboardInterrupt:
        print(file=sys.stderr)
        sys.exit(130)


if __name__ == "__main__":
    main()
