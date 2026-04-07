---
name: claude-history
description: Deep investigation tool for past Claude Code sessions. Use when you need to understand what work was done, reconstruct decisions, find prior implementations, or build context from previous sessions. Goes beyond listing — reads full transcripts.
allowed-tools: Bash(claude-history:*), Read
context: fork
---

# Claude History

Run commands with `claude-history <command>`.

## Investigation Workflow

Key insight: **listing and searching are navigation — reading transcripts is the actual work.**

### Step 1: Discover relevant sessions

| What you know | Command | Purpose |
|--------------|---------|---------|
| A topic or keyword | `search QUERY` | Find sessions mentioning the topic |
| Rough timeframe | `sessions --since 3d` | Filter sessions by time range |
| "What did we just do?" | `transcript latest --prompts-only` | See user prompts from most recent session |

`--since` works on both `sessions` and `search`. Accepts:
- Relative: `3d` (days), `1w` (weeks), `24h` (hours), `30m` (minutes — not months)
- Named: `today`, `yesterday`
- ISO date: `2024-01-15`

`--since` has no end-date filter — for narrow windows, combine with `--size`.

Discovery gives you session IDs, not understanding — that comes from Step 2. If nothing matches, broaden: try synonyms, different targets (`-T tools` catches file paths), or wider time ranges.

### Step 2: Read full transcripts (the core activity)

```bash
claude-history transcript SESSION_ID
claude-history transcript SESSION_ID --show-thinking   # see reasoning
claude-history transcript SESSION_ID:0 --show-thinking  # specific context window
```

`--prompts-only` is useful for orientation, but always read the full transcript.

For many matches, read the 3-5 most relevant first (most recent, closest match), then decide if more are needed.

### Step 3: Follow threads when needed

If a transcript references prior work, follow that thread:
- Note session IDs mentioned in conversation
- Use `search` to find related sessions by file paths, function names, or error messages
- Read those transcripts too

### Step 4: Synthesize and verify

Summarize concrete changes made — not what was attempted or discussed.

**For multi-session investigations, verify before presenting:**
- Every discovered session read and accounted for — list unread sessions and why skipped
- Changes anchored to evidence (file paths, commands, commits) — not descriptions of intent
- If prompted by a question, state whether it was answered or remains open

## Anti-patterns

1. **Listing and stopping** — `sessions` output is navigation, not investigation. Read transcripts.
2. **Search-only** — search snippets are breadcrumbs. Read the full transcript of each match.
3. **Prompts-only** — prompts show requests, not work done. Read full transcripts.

## Command Reference

### Search Targets

| Command | Description |
|---------|-------------|
| `search QUERY -T prompts` | Search user prompts |
| `search QUERY -T responses` | Search assistant text |
| `search QUERY -T tools` | Search tool names and inputs |
| `search QUERY -T tool-results` | Search tool output content |
| `search QUERY -T thinking` | Search Claude's reasoning |
| `search QUERY -T hooks` | Search hook errors and context |
| `search -p QUERY` | Shortcut for `-T prompts` |

### Display Flags

| Flag | Effect |
|------|--------|
| `--show-tool-results` | Full tool results and inputs (no truncation) |
| `--hide-tool-results` | Hide tool result blocks |
| `--show-hooks` | Show hook errors and hook context inline |
| `--show-system` | Show team protocol messages (transcript only) |

### Other Commands

| Command | Description |
|---------|-------------|
| `response UUID` | Claude's response with full tool results (drill-down from transcript) |
| `sessions --page N --size N` | Paginate sessions (default: 10 per page) |

### Session References

- `latest` — most recently updated session
- `prev` or `prev-N` — reference recent sessions (1-indexed)
- Append `:W` for a specific context window: `latest:0`, `prev-2:0`

### Targeting Other Projects

- `--cwd PATH` — resolve project from a different directory
- `--project PATH` — specify project directory directly (under `~/.claude/projects/`)
