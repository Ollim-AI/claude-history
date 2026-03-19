---
name: claude-history
description: Deep investigation tool for past Claude Code sessions. Use when you need to understand what work was done, reconstruct decisions, find prior implementations, or build context from previous sessions. Goes beyond listing — reads full transcripts.
allowed-tools: Bash(claude-history:*), Read
context: fork
---

# Claude History

Investigate past Claude Code sessions to understand what work was actually performed. This is a research tool — the goal is always to **read and understand transcript content**, not just list sessions or search for keywords.

Run commands with `claude-history <command>`.

## Investigation Workflow

Follow this workflow when investigating past sessions. The key insight: **listing sessions and searching are just navigation — reading transcripts is the actual work.**

### Step 1: Discover relevant sessions

Start with one of these discovery methods depending on what you know:

| What you know | Command | Purpose |
|--------------|---------|---------|
| A topic or keyword | `search QUERY` | Find sessions mentioning the topic |
| Rough timeframe | `sessions --since 3d` | Filter sessions by time range |
| "What did we just do?" | `transcript prev --prompts-only` | See user prompts from last session |

**Scoping by time range:** Use `--since` to filter by time. Works on both `sessions` and `search`. Accepts:
- Relative: `3d` (days), `1w` (weeks), `24h` (hours), `30m` (minutes — not months)
- Named: `today`, `yesterday`
- ISO date: `2024-01-15`

Note: `--since` filters from a point forward to now — there's no end-date/range filter. For a narrow historical window, combine with `--size` to keep output manageable.

Discovery gives you **session IDs** to investigate. It does not give you understanding — that comes from Step 2.

### Step 2: Read transcripts deeply (this is the core activity)

Once you have a session ID, **read the full transcript** to understand what happened:

```bash
# Default shows prompts + responses + tool calls
claude-history transcript SESSION_ID

# Add --show-thinking to see Claude's reasoning behind decisions
claude-history transcript SESSION_ID --show-thinking
```

For sessions with multiple context windows, read each one:

```bash
claude-history transcript SESSION_ID:0 --show-thinking
claude-history transcript SESSION_ID:1 --show-thinking
```

Use `--prompts-only` for a quick orientation before deep reading. **But always read the full transcript** — prompts show what was *requested*, the transcript body reveals the actual work: file edits, debugging steps, architectural decisions.

### Step 3: Follow threads when needed

If a transcript references prior work, follow that thread:
- Note session IDs mentioned in conversation
- Use `search` to find related sessions by file paths, function names, or error messages
- Read those transcripts too

### Step 4: Synthesize and verify

After reading transcripts, summarize what was actually accomplished — not what was attempted or discussed, but what concrete changes were made.

**Verify the synthesis before presenting:**
- Every session ID discovered in step 1 has been read and accounted for — list any unread sessions and why they were skipped
- Concrete changes are anchored to evidence (file paths modified, commands run, commits made) — not just descriptions of intent
- If the investigation was prompted by a specific question, state whether the question was answered or remains open

## Investigating Team Sessions

Team sessions involve a lead agent coordinating teammates. Investigation has three layers:

### Layer 1: Coordination (main transcript)

Start with the main session transcript. Team sessions show `[team: name]` in `sessions` output. The transcript renders teammate messages inline with colored `[teammate_id]` labels — these show what each teammate reported back to the lead.

```bash
claude-history transcript SESSION_ID --hide-tools
```

Protocol messages (idle notifications, shutdown requests) are hidden by default. Use `--show-system` to reveal them.

### Layer 2: Teammate work (subagent transcripts)

Each teammate runs as a subagent. Use `subagents` to find them — team subagents show `[teammate_name]` labels:

```bash
claude-history subagents SESSION_ID
claude-history transcript AGENT_ID   # read a specific teammate's full work
```

Focus on teammates with longer durations — they did the substantive work. Short-duration subagents are often shutdown acknowledgments.

### Layer 3: Synthesis (last context window)

The lead's final context window typically contains the synthesis — merged conclusions from all teammates. Read it last to see the converged outcome.

## Anti-patterns — DO NOT do these

1. **Listing and stopping.** Running `sessions` and reporting the list is not investigation. Session listings show timestamps and first-prompt previews — they don't tell you what work was done. Always proceed to reading transcripts.

2. **Search-only investigation.** Running `search "keyword"` returns snippets with session IDs. These snippets are breadcrumbs, not answers. You must follow up by reading the full transcript of matching sessions to understand the context around each match.

3. **Reading only prompts.** User prompts show what was *requested*, not what was *done*. The transcript body contains the actual work: files edited, code written, bugs found, decisions made. Always read full transcripts, not just `--prompts-only`.

## Command Reference

### Discovery Commands

| Command | Description |
|---------|-------------|
| `sessions` | List recent sessions with prompt/context-window counts |
| `sessions --since 3d` | Filter by time (`3d`, `1w`, `24h`, `today`, `2024-01-15`) |
| `sessions --page N --size N` | Paginate (default: 10 per page) |
| `search QUERY` | Search prompts + responses + tools across all sessions and subagents |
| `search QUERY -T tools` | Search tool names and inputs only |
| `search QUERY -T tool-results` | Search tool output content |
| `search QUERY -T thinking` | Search thinking blocks |
| `search QUERY -T responses,tools` | Combine multiple targets (comma-separated) |
| `search QUERY --since 1w` | Search with time filter |
| `search -p QUERY` | Search prompts only (faster) |

### Deep Reading Commands (use these — they're the point)

| Command | Description |
|---------|-------------|
| `transcript SESSION` | Full conversation: prompts + responses + tool calls |
| `transcript SESSION:N` | Single context window (0-indexed) |
| `transcript SESSION --prompts-only` | User prompts only (good for orientation) |
| `transcript SESSION --show-thinking` | **Recommended**: + thinking blocks |
| `transcript SESSION --show-tool-results` | Everything including tool results (no truncation) |
| `transcript SESSION --show-hooks` | Show hook errors and hook context inline |
| `transcript SESSION --hide-tools` | Text only (no tool calls) |
| `transcript SESSION --show-system` | + team protocol messages (idle, shutdown) |
| `transcript AGENT_ID` | Full subagent transcript (accepts hex agent ID) |

### Individual Response Commands

| Command | Description |
|---------|-------------|
| `response UUID` | Claude's response with tool calls |
| `response UUID --show-thinking` | + thinking blocks |
| `response UUID --show-tool-results` | + tool results (full detail) |
| `response UUID --show-hooks` | Show hook errors and hook context inline |

### Subagent Commands

| Command | Description |
|---------|-------------|
| `subagents` | List subagent threads (model, duration, errors) |
| `subagents AGENT_ID` | Detail: prompt, tool timeline, tokens, errors |
| `subagents --session PREFIX` | Filter subagents by session ID prefix |
| `subagents --since 1d` | Filter subagents by time |

## Session References

Anywhere a SESSION_ID is accepted, you can use `prev` to reference recent sessions:

- `prev` or `prev-1` — previous session
- `prev-2` — two sessions ago
- `prev-N` — N sessions ago

Append `:W` for a specific context window: `prev-2:0`

## Targeting Other Projects

- `--cwd PATH` — resolve project from a different directory
- `--project PATH` — specify project directory directly (under `~/.claude/projects/`)
