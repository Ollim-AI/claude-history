"""Resolution helpers for project directories, session references, and slugs."""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import time
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from pathlib import Path

from claude_history.io import agent_id_from_path, iter_subagent_files, newest_first
from claude_history.models import CLAUDE_PROJECTS_DIR, die, parse_timestamp


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
    # Date-only: local midnight, consistent with today/yesterday
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        try:
            return datetime.fromisoformat(value).astimezone()
        except ValueError:
            pass
    # Full ISO timestamp
    dt = parse_timestamp(value)
    if dt:
        return dt
    die(
        f"Error: Cannot parse --since value '{value}'",
        "  Examples: 3d, 1w, 24h, 30m, today, yesterday, 2024-01-15",
    )


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
        # Absolute path: encoded project names start with '-', and a relative
        # dash-leading path downstream reads as an option cluster to grep
        result = Path(args.project).expanduser()
        if result.exists():
            if not result.is_dir():
                die(f"Error: --project is not a directory: {args.project}")
            return result.resolve()
        if "/" not in args.project:
            named = CLAUDE_PROJECTS_DIR / args.project
            if named.exists():
                return named
            die(
                f"Error: Project directory does not exist: {args.project}",
                f"  Tried: {result.absolute()} and {named}",
            )
        die(f"Error: Project directory does not exist: {args.project}")

    cwd = getattr(args, "cwd", None) or os.getcwd()
    result = get_project_dir(cwd)
    if result is not None and result.exists():
        return result
    encoded = encode_path(str(Path(cwd).resolve()))
    die(
        f"Error: No project directory found for '{cwd}'",
        f"  Searched: ~/.claude/projects/{encoded}",
        "  Hint: list projects with: ls ~/.claude/projects/  then pass --project=NAME",
    )


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
            ["grep", "-a", "-F", "-r", "-m", "1", "--include=*.jsonl",
             needle, str(project_dir)],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode == 0 and result.stdout:
            m = re.search(r'"sessionId":"([^"]+)"', result.stdout)
            if m:
                return m.group(1)
    return None


def resolve_session_ref(identifier: str, project_dir: Path) -> tuple[str, int | None]:
    """Resolve a session reference like 'latest', 'prev', 'prev-2', 'prev-3:1', or a UUID prefix.

    Returns (session_prefix, context_window_index_or_None).
    """
    ctx_window = None
    if ":" in identifier:
        base, idx = identifier.rsplit(":", 1)
        if idx.isdigit():
            ctx_window = int(idx)
            identifier = base
        elif base in ("latest", "prev") or re.fullmatch(
            r"prev-\d+|[0-9a-fA-F-]+", base
        ):
            # Session-like base with a bad window suffix — error now instead of
            # falling through to a misleading slug lookup. Slugs/custom titles
            # containing ':' still reach resolve_slug untouched.
            die(
                f"Error: Invalid context window ':{idx}' in '{identifier}' — "
                f"expected a non-negative integer (e.g. {base}:0)"
            )

    if identifier == "latest":
        n = 0
    elif identifier == "prev":
        n = 1
    elif identifier.startswith("prev-") and identifier[5:].isdigit():
        n = int(identifier[5:])
        if n < 1:
            die("Error: prev-N requires N >= 1 (prev-1 = previous session).")
    else:
        # If it doesn't look like a hex UUID prefix, try slug resolution
        if not re.fullmatch(r"[0-9a-fA-F-]+", identifier):
            sid = resolve_slug(identifier, project_dir)
            if sid:
                return (sid[:8], ctx_window)
            die(f"Error: No session found with slug '{identifier}'")
        # Stored session IDs are lowercase hex
        return (identifier.lower(), ctx_window)

    session_ids = get_recent_session_ids(project_dir, count=n + 1)
    if len(session_ids) <= n:
        label = "latest" if n == 0 else f"prev-{n}"
        die(f"Error: Only {len(session_ids)} sessions found, cannot resolve {label}.")
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
    agent_id_prefix = agent_id_prefix.lower()  # stored IDs are lowercase hex
    for d in _iter_other_projects(exclude_dir):
        for path in iter_subagent_files(d):
            if agent_id_from_path(path).startswith(agent_id_prefix):
                return (d, path)
    return None


def find_prompt_across_projects(
    prompt_uuid: str, exclude_dir: Path | None = None
) -> tuple[Path, Path] | None:
    """Grep project dirs for a session file containing the prompt UUID.

    Searches newest-first with early exit, so hits (usually in recently
    active projects) return fast instead of scanning everything. A miss
    scans all projects within a 30s budget; hitting the budget prints a
    note so truncation is never silent. Returns (project_dir, file) or None.
    """
    # Only top-level session files: that is all cmd_response can display,
    # and it skips the ~2x-larger set of subagent files.
    candidates = newest_first(
        f for d in _iter_other_projects(exclude_dir) for f in d.glob("*.jsonl")
    )

    deadline = time.monotonic() + 30
    chunk_size = 200
    for i in range(0, len(candidates), chunk_size):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            print(
                f"Note: UUID search stopped after 30s ({i}/{len(candidates)}"
                " files searched); use --project to target a specific project",
                file=sys.stderr,
            )
            break
        chunk = [str(f) for f in candidates[i : i + chunk_size]]
        try:
            result = subprocess.run(
                ["grep", "-a", "-l", "-s", "-F", "-m", "1", "--", prompt_uuid, *chunk],
                capture_output=True, text=True, timeout=remaining,
            )
        except subprocess.TimeoutExpired:
            continue
        except OSError as e:
            print(
                f"Warning: cross-project UUID search skipped (grep failed: {e})",
                file=sys.stderr,
            )
            break
        if result.stdout.strip():
            # Output order follows arg order (newest first); paths were built
            # as <project>/<session>.jsonl, so parent is the project dir
            hit = Path(result.stdout.strip().splitlines()[0])
            return (hit.parent, hit)
    return None


def note_cross_project(project_dir: Path) -> None:
    """Print a note that a session was found in a different project."""
    print(f"Note: Found in other project (--project {project_dir})", file=sys.stderr)
