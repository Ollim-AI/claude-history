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

> **Note:** Heavy Claude Code usage can accumulate significant storage in `~/.claude/projects/`. Adjust the retention period to suit your needs.

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
| `response UUID` | Read Claude's response to a prompt |
| `search QUERY` | Search for text across all sessions (prompts + responses) |
| `search -p QUERY` | Search prompts only (faster) |
| `subagents` | List subagent threads with model, duration, and errors |
| `subagents AGENT_ID` | Detail view: prompt, tool timeline, tokens, errors |

### Session References

Anywhere a `SESSION` is accepted, you can use `prev` to reference recent sessions instead of a UUID prefix:

| Reference | Resolves to |
|-----------|-------------|
| `prev` or `prev-1` | Previous session |
| `prev-2` | Two sessions ago |
| `prev-N` | N sessions ago |

Append `:N` for a specific context window: `prev-2:0`

### Display Flags

The `transcript` and `response` commands support these flags:

| Flag | Effect |
|------|--------|
| *(default)* | Prompts + responses + tool calls |
| `--prompts-only` | User prompts only (transcript only) |
| `--show-thinking` | Include thinking blocks |
| `--hide-tools` | Hide tool call blocks |
| `--show-tool-results` | Include tool results (full detail, no truncation) |

### Options

| Option | Description |
|--------|-------------|
| `--page N` | Page number for sessions listing |
| `--size N` | Sessions per page (default: 10) |
| `--cwd PATH` | Look up project for a different directory |
| `--project PATH` | Directly specify project directory in `~/.claude/projects/` |

### Example Workflow

```bash
# Search across all sessions for a topic
claude-history search "authentication"

# List recent sessions
claude-history sessions

# View prompts from the previous session
claude-history transcript prev --prompts-only

# Full transcript (prompts + responses + tool calls)
claude-history transcript 9aaedc03

# Drill into a specific context window with thinking
claude-history transcript 9aaedc03:0 --show-thinking

# Read Claude's response to a specific prompt
claude-history response 1240dbfc

# Full transcript from 2 sessions ago, text only
claude-history transcript prev-2 --hide-tools
```
