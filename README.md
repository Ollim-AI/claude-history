# claude-history

CLI navigator for Claude Code conversation history. Browse sessions, search
across conversations, read transcripts, and inspect subagent activity.

## Install

```bash
pip install claude-history
```

## Usage

```bash
claude-history sessions                        # List recent sessions
claude-history sessions --since 3d             # Sessions from last 3 days
claude-history prompts <session>               # List prompts in a session
claude-history response <uuid>                 # Show Claude's response
claude-history transcript <session>            # Full conversation
claude-history transcript <session> -v         # Include tool calls
claude-history search "error handling"         # Search across all sessions
claude-history search -p "deploy" --since 7d   # Search prompts only, recent
claude-history subagents                       # List subagent transcripts
```

### Working directory

By default, `claude-history` reads history for the current working directory.
Use `--cwd` to target a different project:

```bash
claude-history sessions --cwd ~/my-project
```

### Verbosity

`response` and `transcript` support escalating verbosity:

| Flag | Shows |
|------|-------|
| (none) | Text only |
| `-v` | Text + tool calls |
| `-vv` | Text + tools + thinking |
| `-vvv` | Full detail + tool results |

## How it works

Claude Code stores conversation history as JSONL files in
`~/.claude/projects/`. Each project directory contains session files that
record every message, tool call, and response. `claude-history` parses these
files and presents them in a navigable format.

## License

[MIT](LICENSE.md)
