"""Search logic for claude-history."""

from __future__ import annotations

import re
import subprocess
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

from claude_history.chain import (
    build_record_indexes,
    collect_tool_results,
    extract_all_text,
    extract_all_thinking,
    extract_all_tools,
    extract_hook_text,
    extract_user_prompts,
    get_full_response,
)
from claude_history.io import iter_subagent_files
from claude_history.models import (
    DT_MIN,
    Record,
    SearchMatch,
    SearchTarget,
)
from claude_history.render import format_tool_summary, truncate_text, yellow


def prefilter_files(
    project_dir: Path,
    query: str,
    case_sensitive: bool = False,
    since_dt: datetime | None = None,
) -> list[Path]:
    """Use grep to find JSONL files containing the query in non-progress records.

    Pipes grep -v to exclude progress records (which contain embedded conversation
    text from subagent context and cause false positives), then checks for the query.

    Args:
        since_dt: If provided, skip files whose mtime is before this datetime.
            Applied before grep to avoid spawning subprocesses for old files.
    """
    jsonl_files = list(project_dir.glob("*.jsonl"))
    jsonl_files.extend(iter_subagent_files(project_dir))
    if since_dt:
        cutoff = since_dt.timestamp()
        jsonl_files = [f for f in jsonl_files if f.stat().st_mtime >= cutoff]
    if not jsonl_files:
        return []
    case_flag = [] if case_sensitive else ["-i"]

    def _check_file(f: Path) -> Path | None:
        try:
            p1 = subprocess.Popen(
                ["grep", "-a", "-F", "-v", "-f", "-", "--", str(f)],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
            )
            try:
                p1.stdin.write(b'"type":"progress"\n')
                p1.stdin.close()
                p2 = subprocess.Popen(
                    ["grep", "-a", "-F", "-q", *case_flag, "--", query],
                    stdin=p1.stdout,
                    stdout=subprocess.PIPE,
                )
                p1.stdout.close()
                p2.communicate()
                if p2.returncode == 0:
                    return f
            finally:
                p1.wait()
        except (OSError, subprocess.SubprocessError):
            return f  # Fallback: include file if grep fails
        return None

    with ThreadPoolExecutor(max_workers=8) as pool:
        return [f for f in pool.map(_check_file, jsonl_files) if f is not None]


def highlight_match(text: str, query: str, context_chars: int = 60, case_sensitive: bool = False) -> str:
    """Show a snippet around the first match with the query highlighted."""
    text_flat = text.replace("\n", " ").strip()
    flags = 0 if case_sensitive else re.IGNORECASE
    m = re.search(re.escape(query), text_flat, flags)
    if m is None:
        return truncate_text(text_flat, context_chars * 2)
    idx, match_len = m.start(), m.end() - m.start()
    start = max(0, idx - context_chars)
    end = min(len(text_flat), idx + match_len + context_chars)
    snippet = ""
    if start > 0:
        snippet += "..."
    snippet += text_flat[start:idx]
    snippet += yellow(text_flat[idx : idx + match_len])
    snippet += text_flat[idx + match_len : end]
    if end < len(text_flat):
        snippet += "..."
    return snippet


def search_records(
    records: list[Record],
    query: str,
    case_sensitive: bool,
    targets: set[SearchTarget],
) -> list[SearchMatch]:
    """Search through records for matching text in the specified targets."""
    matches: list[SearchMatch] = []
    compare = (lambda t: t) if case_sensitive else (lambda t: t.lower())
    q = compare(query)
    seen: set[str] = set()

    prompts = extract_user_prompts(records)
    # Filter to user-typed prompts only
    prompts = [p for p in prompts if p.is_user_prompt]

    search_prompts = SearchTarget.PROMPTS in targets
    need_chain = targets & {
        SearchTarget.RESPONSES,
        SearchTarget.TOOLS,
        SearchTarget.TOOL_RESULTS,
        SearchTarget.THINKING,
        SearchTarget.HOOKS,
    }
    indexes = build_record_indexes(records) if need_chain else None

    for prompt in prompts:
        prompt_text = prompt.text
        uuid = prompt.uuid

        # Search prompt text
        if search_prompts and q in compare(prompt_text):
            if uuid not in seen:
                seen.add(uuid)
                matches.append(
                    SearchMatch(
                        type="prompt",
                        uuid=uuid,
                        session_id=prompt.session_id,
                        timestamp=prompt.timestamp,
                        text=prompt_text,
                    )
                )

        # Search response chain targets
        if need_chain:
            chain = get_full_response(records, uuid, indexes=indexes)
            _search_chain(
                chain, records, uuid, prompt, q, compare, targets, seen, matches
            )

    matches.sort(key=lambda m: m.timestamp or DT_MIN, reverse=True)
    return matches


def _search_chain(
    chain: list[dict],
    records: list[Record],
    uuid: str,
    prompt,
    q: str,
    compare,
    targets: set[SearchTarget],
    seen: set[str],
    matches: list[SearchMatch],
) -> None:
    """Check each target against the response chain and append matches."""

    def _add(match_type: str, text: str) -> None:
        key = f"{match_type}:{uuid}"
        if key not in seen and text and q in compare(text):
            seen.add(key)
            matches.append(
                SearchMatch(
                    type=match_type,
                    uuid=uuid,
                    session_id=prompt.session_id,
                    timestamp=prompt.timestamp,
                    text=text,
                )
            )

    if SearchTarget.RESPONSES in targets:
        _add("response", extract_all_text(chain))

    if SearchTarget.TOOLS in targets:
        tools = extract_all_tools(chain)
        tool_text = " ".join(
            f"{t['name']} {format_tool_summary(t['name'], t['input'])}" for t in tools
        )
        _add("tools", tool_text)

    if SearchTarget.HOOKS in targets:
        _add("hooks", extract_hook_text(chain, records))

    if SearchTarget.THINKING in targets:
        thinking_parts = extract_all_thinking(chain)
        _add("thinking", " ".join(thinking_parts))

    if SearchTarget.TOOL_RESULTS in targets:
        # Collect tool_use IDs from the chain, then fetch their results
        tool_use_ids: set[str] = set()
        for record in chain:
            content = record.get("message", {}).get("content", [])
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "tool_use":
                        tool_id = block.get("id", "")
                        if tool_id:
                            tool_use_ids.add(tool_id)
        if tool_use_ids:
            results = collect_tool_results(records, tool_use_ids)
            result_text = " ".join(
                r.content if isinstance(r.content, str) else str(r.content)
                for r in results.values()
            )
            _add("tool-results", result_text)
