# Claude History

Navigate Claude Code conversation histories from the command line. Browse sessions, read transcripts, inspect responses and subagents.

## Installation

### Claude Code Skill

Clone into your Claude Code skills directory:

```bash
git clone https://github.com/Ollim-AI/claude-history.git ~/.claude/skills/claude-history
```

This makes the skill available as `/claude-history` in Claude Code conversations.

### CLI Command

Requires [uv](https://docs.astral.sh/uv/).

```bash
uv tool install --editable ~/.claude/skills/claude-history
```

This installs `claude-history` on your PATH. Editable mode means changes to the source are picked up immediately.

Verify:

```bash
claude-history sessions
```

### Preserve History

Claude Code deletes session history after 30 days by default. To keep older sessions accessible, increase `cleanupPeriodDays` in `~/.claude/settings.json`:

```json
{
  "cleanupPeriodDays": 365
}
```

> **Note:** Heavy Claude Code usage can accumulate significant storage in `~/.claude/projects/`.

## Usage

```
claude-history <command> [options]
```

### Commands

| Command | Description |
|---------|-------------|
| `sessions` | List recent sessions with prompt and context window counts |
| `transcript SESSION` | Full conversation: prompts + responses + tool calls |
| `transcript SESSION:N` | Transcript for a specific context window |
| `transcript SESSION --prompts-only` | View user prompts only |
| `transcript AGENT_ID` | Full subagent transcript (accepts hex agent ID) |
| `response UUID` | Read Claude's response to a prompt |
| `search QUERY -T targets` | Search specific content types (comma-separated: prompts,responses,tools,tool-results,thinking,hooks) |
| `search -p QUERY` | Search prompts only (faster) |
| `subagents` | List subagent threads with model, duration, and errors |
| `subagents AGENT_ID` | Detail view: prompt, tool timeline, tokens, errors |

### Session References

Anywhere a `SESSION` is accepted, you can use `latest` or `prev` to reference recent sessions instead of a UUID prefix:

| Reference | Resolves to |
|-----------|-------------|
| `latest` | Most recently updated session |
| `prev` or `prev-1` | Previous session |
| `prev-2` | Two sessions ago |
| `prev-N` | N sessions ago |

Append `:N` for a specific context window: `latest:0`, `prev-2:0`

### Display Flags

The `transcript` and `response` commands support these flags. `transcript` truncates tool results by default; `response` shows full detail (it's a drill-down command).

| Flag | Effect |
|------|--------|
| *(default)* | Prompts + responses + tool calls + tool results |
| `--prompts-only` | User prompts only (transcript only) |
| `--show-thinking` | Include thinking blocks |
| `--hide-tools` | Hide tool call blocks |
| `--show-tool-results` | Full tool results and inputs (no truncation) |
| `--hide-tool-results` | Hide tool result blocks |
| `--show-hooks` | Show hook errors and hook context inline |
| `--show-system` | Show team protocol messages (transcript only) |

### Options

| Option | Description |
|--------|-------------|
| `-T`, `--target TARGETS` | Content types to search (comma-separated: `prompts,responses,tools,tool-results,thinking,hooks`) |
| `--page N` | Page number for sessions listing |
| `--size N` | Sessions per page (default: 10) |
| `--since WHEN` | Filter by time (e.g., `3d`, `1w`, `24h`, `today`, `2024-01-15`) |
| `-t`, `--timestamps` | Show ISO timestamps instead of relative times |
| `--cwd PATH` | Look up project for a different directory |
| `--project PATH` | Directly specify project directory in `~/.claude/projects/` |

### Example Workflow

```bash
# Full transcript (session IDs are prefix-matched)
claude-history transcript 9aaedc03

# Specific context window with thinking
claude-history transcript 9aaedc03:0 --show-thinking

# Two sessions ago, text only
claude-history transcript prev-2 --hide-tools

# List subagents from a specific session
claude-history subagents --session 9aaedc03

# Read a subagent's full transcript
claude-history transcript a63fc3a --show-thinking
```
