@README.md - understand the project goals
@SKILL.md - update only when necessary, eval on real agent usage and human user feedback only
SPEC.md - JSONL format specification; always keep updated

Primary consumers are LLM agents; human use is secondary. Check every design decision and plan against these constraints:

- **Reliability**: CLI behavior matches documented contracts exactly — no silent failures, no ambiguous exits, no output surprises. Bugs that an agent can't self-diagnose are highest priority.
- **Legibility**: Command structure, flag names, and error messages must be self-evident from `--help` alone. Output and errors should be actionable without requiring prior context — an agent seeing a message for the first time must know what to do next.
- **Token economy**: Default output is progressive — summary first, detail on request. No decorative formatting, no redundant headers. Display default litmus test: would hiding this cause an issue to go unnoticed? If yes, show by default; if no, opt-in. Performance matters — agents pay for wall-clock time in context and latency.

## Implementation

Before committing, every change goes through these gates:

1. **One logical change per commit** — split unrelated changes into separate commits.
2. **Review for complexity** (`/simplify`) — run on all code changes. Skip for docs-only or config-only changes.
3. **Review CLI-facing text** (`/improve-prompt`) — run when a commit adds or modifies error messages, help text, or output formatting, because agents treat CLI output as instructions for what to do next. Skip when changes don't alter user/agent-visible text.
4. **Add behavior tests** — for any change that adds, removes, or alters CLI behavior. Skip for internal refactors with existing test coverage.

## Codebase notes

- `models.py` — Leaf module with zero internal imports; all other modules depend on it. ANSI constants bake at import (off when piped/NO_COLOR, on for tty/FORCE_COLOR); `die()` owns errors-to-stderr; shared `paginate`/`warn_ambiguous` live here.
- `io.py` — Single-pass in-process JSONL parse (a grep fast path was removed after measuring 8x slower); progress records identified by the line's first `"type"` field, not bare substring. `iter_subagent_files` covers flat and workflows layouts.
- `chain.py` — `_iter_turn_records` BFS collects a whole turn through bridge records (attachments v2.1.156+, progress stubs ≤v2.1.8x); `get_full_response` sorts collected assistants by timestamp. Notification/agent maps read both string-content (legacy) and toolUseResult/array-content (current) formats.
- `sessions.py` — `get_sessions_from_dir` streams files one-at-a-time via executor.map to stay O(largest_file) memory, unlike `get_sessions` which needs all records in memory. Promptless sessions get one synthetic window so transcript never contradicts the listing.
- `render.py` — `render_blocks` is the single display engine for transcript and response; filtering is flag-driven, not caller-driven. Success tool results clip at 20 lines / 4000 chars (errors full); base64 image payloads always elided; `full_detail=True` lifts clipping.
- `agents.py` — Subagent files are parsed fully (small); lazy imports from chain/render avoid circular deps. `search_subagent_files` searches record-by-record (not concatenated) to avoid OOM. Hook display normalizes attachment-era hook records to the legacy shape.
- `search.py:prefilter_files` — `since_dt` filters by mtime before spawning grep; without it, 7000+ subagent files spawn 14000+ subprocesses.
- `cli.py` — argparse wiring + dispatch only; handlers live in `commands/` (one module per subcommand). `cmd_response` grep-locates the prompt's file newest-first and parses only that file.
