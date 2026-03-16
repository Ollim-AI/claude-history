"""File I/O operations for reading JSONL conversation files."""

from __future__ import annotations

import json
import re
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from claude_history.models import ProgressStub, Record


def _extract_progress_stub(line: str) -> ProgressStub | None:
    """Extract lightweight progress stub from a raw JSONL line.

    Uses fixed field positions for speed:
    - parentUuid: always in first 200 bytes
    - parentToolUseID, agentId: always in last 350 bytes
    - uuid: last occurrence in the line (the first is a nested UUID
      inside data.message; the top-level one comes after the data field)
    """
    # Top-level uuid is the LAST "uuid" in the line (after nested data.message.uuid).
    # rfind + short regex is faster than scanning the full line with findall.
    uuid_pos = line.rfind('"uuid":"')
    if uuid_pos == -1:
        return None
    uuid_m = re.search(r'"uuid":"([^"]+)"', line[uuid_pos:])
    if not uuid_m:
        return None
    head = line[:200]
    tail = line[-350:]
    parent_m = re.search(r'"parentUuid":"([^"]+)"', head)
    ptid_m = re.search(r'"parentToolUseID":"([^"]+)"', tail)
    aid_m = re.search(r'"agentId":"([^"]+)"', tail)
    return ProgressStub(
        uuid=uuid_m.group(1),
        parentUuid=parent_m.group(1) if parent_m else None,
        parentToolUseID=ptid_m.group(1) if ptid_m else None,
        agentId=aid_m.group(1) if aid_m else None,
    )


def parse_jsonl_file(
    filepath: Path, include_progress_stubs: bool = True
) -> list[Record]:
    """Parse a JSONL file and return list of records.

    Uses grep to filter out 'progress' records (subagent transcripts) which are
    ~99% of file size. grep is ~5x faster than Python for scanning large files.

    Args:
        filepath: Path to the JSONL file
        include_progress_stubs: If True, also extract lightweight progress stubs
            (uuid, parentUuid, parentToolUseID, agentId) for chain traversal.
            Set to False for commands that don't need response chain following
            (sessions, prompts, search -p) for a major speedup.
    """
    records = []
    try:
        # Use grep -F -f - to pipe the pattern via stdin, avoiding Windows
        # argument quoting issues with double quotes in patterns
        result = subprocess.run(
            ["grep", "-F", "-v", "-f", "-", str(filepath)],
            input='"type":"progress"\n',
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        skipped = 0
        for line in result.stdout.splitlines():
            if line.strip():
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError:
                    skipped += 1
        if skipped:
            print(
                f"Warning: skipped {skipped} malformed line(s) in {filepath}",
                file=sys.stderr,
            )

        # Add lightweight progress stubs for chain traversal
        if include_progress_stubs:
            with open(filepath, encoding="utf-8", errors="replace") as f:
                for line in f:
                    if '"type":"progress"' not in line:
                        continue
                    stub = _extract_progress_stub(line)
                    if stub:
                        records.append(stub)
    except (OSError, subprocess.SubprocessError):
        # Fallback to Python if grep is unavailable
        try:
            with open(filepath, encoding="utf-8", errors="replace") as f:
                for line in f:
                    line_stripped = line.strip()
                    if not line_stripped:
                        continue
                    if '"type":"progress"' in line:
                        if include_progress_stubs:
                            stub = _extract_progress_stub(line)
                            if stub:
                                records.append(stub)
                        continue
                    try:
                        records.append(json.loads(line_stripped))
                    except json.JSONDecodeError:
                        skipped += 1
            if skipped:
                print(
                    f"Warning: skipped {skipped} malformed line(s) in {filepath}",
                    file=sys.stderr,
                )
        except OSError as e:
            print(f"Warning: failed to read {filepath}: {e}", file=sys.stderr)
    return records


def get_all_conversations(
    project_dir: Path, include_progress_stubs: bool = True
) -> list[Record]:
    """Get all conversation records from a project directory."""
    jsonl_files = list(project_dir.glob("*.jsonl"))

    def parse_with_source(filepath):
        file_records = parse_jsonl_file(filepath, include_progress_stubs)
        for record in file_records:
            if isinstance(record, dict):
                record["_source_file"] = filepath.name
        return file_records

    if not jsonl_files:
        return []

    # Parse files in parallel (capped at 8 workers to bound memory)
    records = []
    with ThreadPoolExecutor(max_workers=min(8, len(jsonl_files))) as executor:
        for file_records in executor.map(parse_with_source, jsonl_files):
            records.extend(file_records)

    return records


def get_session_conversations(
    project_dir: Path, session_prefix: str, include_progress_stubs: bool = True
) -> list[Record] | None:
    """Get conversation records for a specific session by direct file lookup.

    The JSONL filename IS the session UUID, so we can glob directly instead
    of parsing all files. Returns None if no matching file found (caller
    should fall back to get_all_conversations).
    """
    matching = list(project_dir.glob(f"{session_prefix}*.jsonl"))
    if not matching:
        return None
    filepath = matching[0]
    records = parse_jsonl_file(filepath, include_progress_stubs)
    for r in records:
        if isinstance(r, dict):
            r["_source_file"] = filepath.name
    return records


def find_subagent_file(project_dir: Path, agent_id_prefix: str) -> Path | None:
    """Find a subagent file by agent_id prefix (hex hash).

    Returns the first matching file, or None.
    """
    for path in project_dir.glob("*/subagents/agent-*.jsonl"):
        stem_id = path.stem.replace("agent-", "")
        if stem_id.startswith(agent_id_prefix):
            return path
    return None


def parse_subagent_file(filepath: Path) -> list[dict]:
    """Parse a subagent JSONL file fully (no progress filtering).

    Subagent files are small enough to parse completely, unlike main session
    files where progress records dominate.
    """
    records = []
    skipped = 0
    try:
        with open(filepath, encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        records.append(json.loads(line))
                    except json.JSONDecodeError:
                        skipped += 1
        if skipped:
            print(
                f"Warning: skipped {skipped} malformed line(s) in {filepath}",
                file=sys.stderr,
            )
    except OSError as e:
        print(f"Warning: failed to read {filepath}: {e}", file=sys.stderr)
    return records
