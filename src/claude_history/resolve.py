"""Resolution helpers for project directories, session references, and slugs."""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

from claude_history.models import CLAUDE_PROJECTS_DIR


def encode_path(path: str) -> str:
    """Encode a path for use as a project directory name.

    /home/user/Code/foo -> -home-user-Code-foo
    /home/user/.claude -> -home-user--claude (dots replaced with dashes)
    """
    # Replace dots, slashes, backslashes, and colons with dashes
    return path.replace(".", "-").replace("/", "-").replace("\\", "-").replace(":", "-")


def get_project_dir(cwd: str | None = None) -> Path | None:
    """Find the Claude projects directory for a given working directory.

    Encodes the cwd and checks if a matching project directory exists.
    If not, walks up the path hierarchy and tries again until found or root reached.
    """
    if cwd is None:
        cwd = os.getcwd()

    current = Path(cwd).resolve()

    while True:
        encoded = encode_path(str(current))
        project_dir = CLAUDE_PROJECTS_DIR / encoded

        if project_dir.exists():
            return project_dir

        # Move up one level
        parent = current.parent
        if parent == current:
            # Reached filesystem root
            return None
        current = parent


def resolve_project_dir(args: argparse.Namespace) -> Path:
    """Resolve project directory from args (--project or --cwd). Exits on failure."""
    if hasattr(args, "project") and args.project:
        result = Path(args.project)
        if result.exists():
            return result
        print(f"Error: Project directory does not exist: {args.project}")
        sys.exit(1)

    cwd = getattr(args, "cwd", None) or os.getcwd()
    result = get_project_dir(cwd)
    if result is not None and result.exists():
        return result
    encoded = encode_path(str(Path(cwd).resolve()))
    print(f"Error: No project directory found for '{cwd}'")
    print(f"  Searched: ~/.claude/projects/{encoded}")
    print("  Hint: Use --project <path> to specify the project directory directly")
    sys.exit(1)


def get_recent_session_ids(project_dir: Path, count: int = 10) -> list[str]:
    """Get session IDs from JSONL files sorted by modification time (most recent first).

    Extracts the first sessionId from each file without full parsing.
    Much faster than get_sessions() for just resolving IDs.
    """
    jsonl_files = sorted(
        project_dir.glob("*.jsonl"), key=lambda f: f.stat().st_mtime, reverse=True
    )
    session_ids = []
    seen = set()
    for f in jsonl_files[: count * 2]:  # Check extra files in case of duplicates
        try:
            with open(f, encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    m = re.search(r'"sessionId":"([^"]+)"', line)
                    if m:
                        sid = m.group(1)
                        if sid not in seen:
                            seen.add(sid)
                            session_ids.append(sid)
                        break
        except OSError:
            continue
        if len(session_ids) >= count:
            break
    return session_ids


def resolve_slug(name: str, project_dir: Path) -> str | None:
    """Find a session ID by slug or custom title. Single recursive grep for speed."""
    for field in ["customTitle", "slug"]:
        needle = f'"{field}":"{name}"'
        result = subprocess.run(
            ["grep", "-F", "-r", "-m", "1", "--include=*.jsonl",
             needle, str(project_dir)],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode == 0 and result.stdout:
            m = re.search(r'"sessionId":"([^"]+)"', result.stdout)
            if m:
                return m.group(1)
    return None


def resolve_session_ref(identifier: str, project_dir: Path) -> tuple[str, int | None]:
    """Resolve a session reference like 'prev', 'prev-2', 'prev-3:1', or a UUID prefix.

    Returns (session_prefix, context_window_index_or_None).
    """
    ctx_window = None
    if ":" in identifier:
        base, idx = identifier.rsplit(":", 1)
        if idx.isdigit():
            ctx_window = int(idx)
            identifier = base

    if identifier == "prev":
        n = 1
    elif identifier.startswith("prev-") and identifier[5:].isdigit():
        n = int(identifier[5:])
        if n < 1:
            print("Error: prev-N requires N >= 1 (prev-1 = previous session).")
            sys.exit(1)
    else:
        # If it doesn't look like a hex UUID prefix, try slug resolution
        if not re.fullmatch(r'[0-9a-f-]+', identifier):
            sid = resolve_slug(identifier, project_dir)
            if sid:
                return (sid[:8], ctx_window)
            print(f"Error: No session found with slug '{identifier}'")
            sys.exit(1)
        return (identifier, ctx_window)

    session_ids = get_recent_session_ids(project_dir, count=n + 1)
    if len(session_ids) <= n:
        print(
            f"Error: Only {len(session_ids)} sessions found, cannot resolve prev-{n}."
        )
        sys.exit(1)
    return (session_ids[n][:8], ctx_window)


def _iter_other_projects(exclude_dir: Path | None = None) -> Iterator[Path]:
    """Iterate project directories under CLAUDE_PROJECTS_DIR, skipping exclude_dir."""
    try:
        dirs = sorted(CLAUDE_PROJECTS_DIR.iterdir())
    except OSError:
        return
    for d in dirs:
        if d.is_dir() and d != exclude_dir:
            yield d


def find_session_across_projects(
    session_prefix: str, exclude_dir: Path | None = None
) -> Path | None:
    """Search all project dirs for a session file matching the prefix.

    Returns the project_dir containing the match, or None.
    """
    for d in _iter_other_projects(exclude_dir):
        if any(d.glob(f"{session_prefix}*.jsonl")):
            return d
    return None


def find_subagent_across_projects(
    agent_id_prefix: str, exclude_dir: Path | None = None
) -> tuple[Path, Path] | None:
    """Search all project dirs for a subagent file matching the prefix.

    Returns (project_dir, agent_file_path) or None.
    """
    for d in _iter_other_projects(exclude_dir):
        for path in d.glob("*/subagents/agent-*.jsonl"):
            if path.stem.replace("agent-", "").startswith(agent_id_prefix):
                return (d, path)
    return None


def find_prompt_across_projects(
    prompt_uuid: str, exclude_dir: Path | None = None
) -> Path | None:
    """Grep all project dirs for a file containing the prompt UUID.

    Returns project_dir or None.
    """
    try:
        result = subprocess.run(
            ["grep", "-r", "-l", "-m", "1", "--include=*.jsonl",
             prompt_uuid, str(CLAUDE_PROJECTS_DIR)],
            capture_output=True, text=True, timeout=30,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    if result.returncode != 0 or not result.stdout.strip():
        return None
    # Extract project dir from the matching file path
    match_path = Path(result.stdout.strip().splitlines()[0])
    # File is under CLAUDE_PROJECTS_DIR/<project>/ — walk up to find project dir
    for parent in match_path.parents:
        if parent.parent == CLAUDE_PROJECTS_DIR:
            if exclude_dir and parent == exclude_dir:
                return None
            return parent
    return None


def note_cross_project(project_dir: Path) -> None:
    """Print a note that a session was found in a different project."""
    print(f"Note: Found in other project (--project {project_dir})", file=sys.stderr)
