"""File I/O operations for reading JSONL conversation files."""

from __future__ import annotations

import json
import re
import subprocess
import sys
from collections.abc import Iterator
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


_FIRST_TYPE_RE = re.compile(r'"type":"([^"]*)"')


def _is_progress_line(line: str) -> bool:
    """A record is a progress record iff its own type field says so.

    The record's top-level "type" is always the line's first occurrence
    (verified against real data); a non-progress record can contain the
    marker nested in toolUseResult JSON, and substring checks alone
    silently dropped those records.
    """
    m = _FIRST_TYPE_RE.search(line)
    return bool(m) and m.group(1) == "progress"


def _parse_record_line(line: str) -> dict | None:
    """Parse one JSONL line into a record dict, or None if unusable.

    Valid JSON that is not an object (42, "str", []) or whose 'message' is
    not an object would crash every downstream .get() — treat as malformed.
    """
    try:
        obj = json.loads(line)
    except json.JSONDecodeError:
        return None
    if not isinstance(obj, dict):
        return None
    msg = obj.get("message")
    if msg is not None and not isinstance(msg, dict):
        return None
    return obj


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
    skipped = 0
    try:
        # -F -f -: pattern via stdin avoids Windows argument quoting issues.
        # -a: a single NUL byte otherwise makes grep declare the file binary
        # and swallow every record in it.
        result = subprocess.run(
            ["grep", "-a", "-F", "-v", "-f", "-", "--", str(filepath)],
            input='"type":"progress"\n',
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        if result.returncode > 1:
            # 0 = matches, 1 = no lines left; >1 = grep failed (bad args,
            # unreadable file) — fall back to the Python parser
            raise subprocess.SubprocessError(f"grep exited {result.returncode}")
        for line in result.stdout.split("\n"):
            if line.strip():
                record = _parse_record_line(line)
                if record is not None:
                    records.append(record)
                else:
                    skipped += 1

        # Second pass over the marker lines grep -v dropped: extract stubs
        # from genuine progress records, and recover normal records that
        # merely contain the marker nested in their JSON.
        selected = subprocess.run(
            ["grep", "-a", "-F", "-f", "-", "--", str(filepath)],
            input='"type":"progress"\n',
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        if selected.returncode > 1:
            raise subprocess.SubprocessError(f"grep exited {selected.returncode}")
        for line in selected.stdout.split("\n"):
            if not line.strip():
                continue
            if _is_progress_line(line):
                if include_progress_stubs:
                    stub = _extract_progress_stub(line)
                    if stub:
                        records.append(stub)
            else:
                record = _parse_record_line(line)
                if record is not None:
                    records.append(record)
                else:
                    skipped += 1
        if skipped:
            print(
                f"Warning: skipped {skipped} malformed line(s) in {filepath}",
                file=sys.stderr,
            )
    except (OSError, subprocess.SubprocessError):
        # Fallback to Python if grep is unavailable
        try:
            with open(filepath, encoding="utf-8", errors="replace") as f:
                for line in f:
                    line_stripped = line.strip()
                    if not line_stripped:
                        continue
                    if '"type":"progress"' in line and _is_progress_line(line):
                        if include_progress_stubs:
                            stub = _extract_progress_stub(line)
                            if stub:
                                records.append(stub)
                        continue
                    record = _parse_record_line(line_stripped)
                    if record is not None:
                        records.append(record)
                    else:
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
    matching = sorted(
        project_dir.glob(f"{session_prefix}*.jsonl"),
        key=lambda f: f.stat().st_mtime,
        reverse=True,
    )
    if not matching:
        return None
    # Ambiguous prefixes resolve to the most recently active session
    filepath = matching[0]
    records = parse_jsonl_file(filepath, include_progress_stubs)
    for r in records:
        if isinstance(r, dict):
            r["_source_file"] = filepath.name
    return records


def iter_subagent_files(project_dir: Path) -> Iterator[Path]:
    """Yield subagent JSONL files across both storage layouts.

    Agent-tool subagents: {session}/subagents/agent-{hash}.jsonl
    Workflow subagents (v2.1.79+): {session}/subagents/workflows/{run}/agent-{hash}.jsonl
    """
    yield from project_dir.glob("*/subagents/agent-*.jsonl")
    yield from project_dir.glob("*/subagents/workflows/*/agent-*.jsonl")


def subagent_session_id(filepath: Path) -> str:
    """Derive the session ID from a subagent file path (dir above 'subagents')."""
    for parent in filepath.parents:
        if parent.name == "subagents":
            return parent.parent.name
    return filepath.parent.parent.name


def find_subagent_file(project_dir: Path, agent_id_prefix: str) -> Path | None:
    """Find a subagent file by agent_id prefix (hex hash).

    Returns the first matching file, or None.
    """
    for path in iter_subagent_files(project_dir):
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
                    record = _parse_record_line(line)
                    if record is not None:
                        records.append(record)
                    else:
                        skipped += 1
        if skipped:
            print(
                f"Warning: skipped {skipped} malformed line(s) in {filepath}",
                file=sys.stderr,
            )
    except OSError as e:
        print(f"Warning: failed to read {filepath}: {e}", file=sys.stderr)
    return records
