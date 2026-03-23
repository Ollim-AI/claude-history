@README.md - understand the project goals
@SKILL.md - update only when necessary, eval on real agent usage and human user feedback only
SPEC.md - JSONL format specification; always keep updated

Primary consumers are LLM agents; human use is secondary. Design for three agent constraints:

- **Reliability**: CLI behavior matches documented contracts exactly — no silent failures, no ambiguous exits, no output surprises. Bugs that an agent can't self-diagnose are highest priority.
- **Legibility**: Command structure, flag names, and error messages must be self-evident from `--help` alone. Output and errors should be actionable without requiring prior context — an agent seeing a message for the first time must know what to do next.
- **Token economy**: Default output is minimal and progressive — summary first, detail on request. No decorative formatting, no redundant headers, no verbose confirmation messages. Subagent encapsulation keeps context windows small. Performance matters because agents pay for wall-clock time in context and latency. Display default litmus test: would hiding this cause an issue to go unnoticed? If yes, show by default; if no, opt-in.

## Implementation

Before committing, every change goes through these gates:

1. **One logical change per commit** — split unrelated changes into separate commits.
2. **Review for complexity** (`/simplify`) — run on all code changes. Skip for docs-only or config-only changes.
3. **Review CLI-facing text** (`/improve-prompt`) — run when a commit adds or modifies error messages, help text, or output formatting, because agents treat CLI output as instructions for what to do next. Skip when changes don't alter user/agent-visible text.
4. **Add behavior tests** — for any change that adds, removes, or alters CLI behavior. Skip for internal refactors with existing test coverage.

## Codebase notes

- `models.py` — Leaf module with zero internal imports; all other modules depend on it. ANSI codes are module-level constants shared via re-export.
- `io.py` — Shells out to `grep -F -v` to skip progress records (~99% of file size) before JSON parsing; Python fallback exists if grep unavailable.
- `chain.py` — `get_full_response` recovers from dead-end chains by following ProgressStub siblings/children (needed for parallel agent tool_use chains in v2.1.76+).
- `sessions.py` — `get_sessions_from_dir` streams files one-at-a-time via executor.map to stay O(largest_file) memory, unlike `get_sessions` which needs all records in memory.
- `render.py` — `render_blocks` is the single display engine for both transcript and response commands; all output filtering (thinking/tools/hooks) is flag-driven, not caller-driven. Tool results are shown by default (truncated to ~20 lines for success, full for errors); `full_detail=True` removes truncation and shows full tool inputs.
- `agents.py` — Subagent files are parsed fully (no progress filtering) because they're small; uses lazy imports from chain/render to avoid circular deps. `search_subagent_files` must search record-by-record (not concatenate) to avoid OOM.
- `cli.py:prefilter_files` — `since_dt` param filters by mtime before spawning grep; without it, 3000+ subagent files spawn 6000+ subprocesses.
- `cli.py` — `cmd_response` does a two-pass file load: first without progress stubs to find the UUID's file, then reloads just that file with stubs for chain traversal.
