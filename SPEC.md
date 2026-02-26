# Claude Code History File Specification

This document describes the JSONL format used by Claude Code to persist conversation history.

## File Format

- **Format:** JSONL (JSON Lines) - one JSON object per line
- **Encoding:** UTF-8
- **Location:** `~/.claude/projects/{dash-encoded-path}/`

### File Naming

| Pattern | Description |
|---------|-------------|
| `{UUID}.jsonl` | Main session file |
| `agent-{hash}.jsonl` | Top-level agent file (7-char hex, legacy clients ≤2.0.x) |
| `{UUID}/subagents/agent-{hash}.jsonl` | Subagent file (7-char hex ≤v2.1.49, 17-char hex v2.1.50+) |
| `{UUID}/subagents/agent-acompact-{hash}.jsonl` | Compacted subagent file (6-char hex ≤v2.1.49, 16-char hex v2.1.50+) |
| `{UUID}/tool-results/{id}.txt` | Tool result output (see naming patterns below) |

Older client versions (≤2.0.x) stored subagent files directly in the project directory as `agent-{hash}.jsonl`. Newer versions use the `{UUID}/subagents/` subdirectory.

**Hash length change at v2.1.50:** Agent file hashes changed from 7-char to 17-char hex, and acompact hashes from 6-char to 16-char hex. The transition is clean at v2.1.50 with no overlap. A parser should accept variable-length hex hashes rather than hardcoding lengths.

**Tool-results file naming:** Three naming patterns exist for files in `{UUID}/tool-results/`:
- `toolu_XXXX.txt` — the `tool_use_id` directly (all versions)
- 7-char hex: e.g., `b174ed9.txt` (v2.1.49-2.1.50)
- 9-char alphanumeric: e.g., `b1rrxdut3.txt` (v2.1.53+)

**Multi-session files:** A JSONL file may contain records from multiple sessions. When a session is continued from a previous one, the new file includes a small number of records from the previous session at the start (typically the last user prompt and `turn_duration` system records). Filter by `sessionId` when processing a specific session to avoid double-counting.

### Directory Structure

Project directories use dash-encoded paths (slashes and dots replaced with dashes):
- `/home/user/Code/myapp` → `-home-user-Code-myapp/`
- `/home/user/.claude` → `-home-user--claude/` (dot becomes dash)

---

## Streaming Record Model

Claude Code writes records incrementally as the API response streams in. Each assistant record captures one content block (thinking, text, or tool_use). This has several important consequences:

- Multiple assistant records form a chain for a single API response and share the same `message.id`
- `stop_reason` is typically `null` because records are written before the final stop reason is determined. Non-null values (`tool_use`, `end_turn`, `stop_sequence`) appear on the final record of a streaming response.
- Token usage per record is incremental (small values like `output_tokens: 1`), not cumulative for the full turn
- To get total token usage for a response, sum across all assistant records in the response chain

---

## Record Types

### 1. User Record (`type: "user"`)

Human message or tool result response.

```json
{
  "type": "user",
  "uuid": "1240dbfc-07e0-4a3e-a672-4c23ff64d06d",
  "parentUuid": null,
  "sessionId": "4f4dc035-e822-4499-a17c-f00859c24d01",
  "timestamp": "2026-01-03T10:26:38.697Z",
  "cwd": "/home/user/Code/myapp",
  "version": "2.1.12",
  "gitBranch": "main",
  "isSidechain": false,
  "userType": "external",
  "slug": "keen-mapping-torvalds",
  "message": {
    "role": "user",
    "content": [...]
  }
}
```

**Additional fields for tool results:**
```json
{
  "toolUseResult": {...},
  "sourceToolAssistantUUID": "uuid-of-assistant-that-made-tool-call"
}
```

> **Version note:** `sourceToolAssistantUUID` is absent in v2.0.76 and unreliable in v2.1.12/2.1.15 (~4% missing). It is 100% reliable from v2.1.20+. For older versions, identify tool results by the presence of `toolUseResult` or `tool_result` blocks in `message.content`. To correlate a tool result with the assistant that requested it, use `parentUuid` — it always points to the preceding record in the chain (which works across all versions).

`toolUseResult` is an auxiliary metadata object whose schema varies by tool. For history navigation, this field is rarely needed — the tool name comes from the `tool_use` block's `name` field, and the result content comes from the `tool_result` block in `message.content`. The most useful `toolUseResult` is on Task/Agent results, which contains `agentId` (links to the subagent file), `totalDurationMs`, `totalTokens`, and `totalToolUseCount`.

The `tool_result` block's `content` field can be either a string or an array of content blocks (e.g., `[{"type": "text", "text": "..."}]`). The `is_error` field is optional.

**Meta message linking (v2.1.38+):**

`sourceToolUseID` (string) appears on user records with `isMeta: true`. It contains a `toolu_` ID linking the meta message to the tool_use that triggered it (e.g., a skill loading message triggered by a Skill tool call). This field is not needed for identifying user-typed prompts (those already filter on `isMeta`), but is useful for tracing system-injected content back to its trigger.

**Optional fields on user records:**

`thinkingMetadata` appears on user records only (not observed on assistant records). It has two shapes:
- `{"level": "high", "disabled": false, "triggers": []}` — thinking configuration
- `{"maxThinkingTokens": 31999}` — token budget variant

`todos` appears on user records (almost always an empty array):
```json
{
  "thinkingMetadata": {"level": "high", "disabled": false, "triggers": []},
  "todos": [],
  "permissionMode": "default"
}
```

`permissionMode` (string, v2.1.15+) indicates the permission mode for the interaction. Values: `bypassPermissions`, `plan`, `default`, `acceptEdits`.

**Agent-specific fields:**
```json
{
  "agentId": "a63fc3a",
  "isSidechain": true
}
```

**Compact summary fields:**
```json
{
  "isCompactSummary": true,
  "isVisibleInTranscriptOnly": true
}
```

---

### 2. Assistant Record (`type: "assistant"`)

Claude's response message. Each record typically contains a single content block (rare exceptions with 2-3 blocks have been observed in <0.1% of records).

```json
{
  "type": "assistant",
  "uuid": "03f65326-739d-4f28-8eb6-e4ec687282d8",
  "parentUuid": "1240dbfc-07e0-4a3e-a672-4c23ff64d06d",
  "sessionId": "4f4dc035-e822-4499-a17c-f00859c24d01",
  "timestamp": "2026-01-03T10:26:40.147Z",
  "cwd": "/home/user/Code/myapp",
  "version": "2.1.12",
  "gitBranch": "main",
  "isSidechain": false,
  "userType": "external",
  "slug": "keen-mapping-torvalds",
  "requestId": "req_011CWkHTYk7EcMm8dLq17LP7",
  "message": {
    "role": "assistant",
    "type": "message",
    "id": "msg_013RF4742cUJWRwmtzzgX7HY",
    "model": "claude-opus-4-5-20251101",
    "content": [...],
    "stop_reason": null,
    "stop_sequence": null,
    "usage": {
      "input_tokens": 9,
      "output_tokens": 1,
      "cache_creation_input_tokens": 573,
      "cache_read_input_tokens": 10026,
      "cache_creation": {
        "ephemeral_5m_input_tokens": 573,
        "ephemeral_1h_input_tokens": 0
      },
      "service_tier": "standard"
    }
  }
}
```

`service_tier` is a string indicating the API service tier, not a token count.

**Additional usage fields (v2.1.50+):**

The `usage` object may also contain:
- `server_tool_use`: `{"web_search_requests": 0, "web_fetch_requests": 0}` — server-side tool usage counters
- `inference_geo`: string (observed `""`, `"not_available"`, `null`)
- `iterations`: array (observed empty)
- `speed`: string (observed `"standard"`)

**Additional message fields (v2.1.50+):**

The inner `message` object may also contain:
- `context_management`: usually `null`, occasionally `{"applied_edits": []}`
- `container`: always `null` in observed data

**API Error Messages (v2.1.38+):**

When an API error is surfaced to the user, a synthetic assistant record is created:

```json
{
  "type": "assistant",
  "isApiErrorMessage": true,
  "error": "unknown",
  "message": {
    "model": "<synthetic>",
    "role": "assistant",
    "stop_reason": "stop_sequence",
    "container": null,
    "context_management": null,
    "usage": {"input_tokens": 0, "output_tokens": 0, ...},
    "content": [{"type": "text", "text": "API Error: 500 {...}"}]
  }
}
```

These are distinct from `system/api_error` records (which track retry metadata). Key markers: `isApiErrorMessage: true`, `model: "<synthetic>"`, zero token usage, `error` field with values like `"unknown"` or `"invalid_request"`.

---

### 3. Progress Record (`type: "progress"`)

Progress tracking for subagent conversations, bash commands, hooks, and other async operations.

**Note:** Progress records form chains via `parentUuid`. The first progress record in a chain typically has the same `parentUuid` as the corresponding user tool_result record (both pointing to the assistant's tool_use record). Subsequent progress records chain to the previous progress record (~88% of progress records point to another progress record). When walking the response chain, skip progress records to follow the main conversation flow.

#### Agent Progress (`data.type: "agent_progress"`)

```json
{
  "type": "progress",
  "uuid": "unique-id",
  "parentUuid": "parent-id",
  "sessionId": "session-id",
  "timestamp": "2026-01-03T10:26:38.697Z",
  "cwd": "/path",
  "version": "2.1.12",
  "data": {
    "type": "agent_progress",
    "agentId": "a63fc3a",
    "prompt": "Original prompt text",
    "message": {...},
    "normalizedMessages": [...]
  },
  "toolUseID": "agent_msg_...",
  "parentToolUseID": "toolu_..."
}
```

The `normalizedMessages` array contains compacted context for token management. `agentId` is nested inside `data`, not at the top level.

When an agent is resumed, the `data` object also contains `"resume": "agent-id"` matching the `agentId` (v2.1.42+).

#### Bash Progress (`data.type: "bash_progress"`)

```json
{
  "type": "progress",
  "data": {
    "type": "bash_progress",
    "output": "latest output chunk",
    "fullOutput": "complete output so far",
    "totalLines": 42,
    "totalBytes": 1024,
    "elapsedTimeSeconds": 3.5,
    "taskId": "b8e6941",
    "timeoutMs": 30000
  },
  "toolUseID": "bash-progress-0",
  "parentToolUseID": "toolu_..."
}
```

`taskId` (7-char hex), `timeoutMs` (integer), and `totalBytes` (integer) were added in later versions. Older records may lack these fields.

#### Hook Progress (`data.type: "hook_progress"`)

```json
{
  "type": "progress",
  "data": {
    "type": "hook_progress",
    "hookName": "PostToolUse:Read",
    "hookEvent": "PostToolUse",
    "command": "npm run lint"
  },
  "toolUseID": "toolu_...",
  "parentToolUseID": "toolu_..."
}
```

`hookName` follows the format `{hookEvent}:{toolName}` (e.g., `PostToolUse:Edit`). For hook_progress records, `toolUseID` and `parentToolUseID` are typically identical.

#### MCP Progress (`data.type: "mcp_progress"`, v2.1.45+)

Tracks MCP server tool invocations:

```json
{
  "type": "progress",
  "data": {
    "type": "mcp_progress",
    "status": "started",
    "serverName": "context7",
    "toolName": "resolve-library-id"
  },
  "toolUseID": "toolu_...",
  "parentToolUseID": "toolu_..."
}
```

`status` is `"started"` or `"completed"`. The `"completed"` variant adds `elapsedTimeMs` (integer). `toolUseID` and `parentToolUseID` are identical (same pattern as hook_progress).

#### Other Progress Types

Less common progress types observed in real data:

| `data.type` | Description | Key fields |
|-------------|-------------|------------|
| `query_update` | WebSearch query tracking | `query` |
| `search_results_received` | WebSearch results | `resultCount`, `query` |
| `waiting_for_task` | Task tool waiting | `taskDescription`, `taskType` |

**`toolUseID` formats by progress type:**
- `agent_msg_...` — agent_progress records
- `bash-progress-N` — bash_progress records
- `toolu_...` — hook_progress, mcp_progress, and other records

`parentToolUseID` is always format `toolu_...` (the parent tool_use block that triggered this progress).

---

### 4. System Record (`type: "system"`)

System metadata records.

```json
{
  "type": "system",
  "subtype": "turn_duration",
  "uuid": "a35483c9-cc73-4e27-844d-14b9c744073d",
  "parentUuid": "39267496-0dea-46d7-ab14-6799219c08b5",
  "sessionId": "68d8c2a4-73f9-4e9e-90c1-3ee0ad63be1b",
  "timestamp": "2026-01-20T05:51:20.820Z",
  "cwd": "/home/user/.claude",
  "version": "2.1.12",
  "gitBranch": "",
  "isSidechain": false,
  "userType": "external",
  "slug": "keen-mapping-torvalds",
  "isMeta": false,
  "durationMs": 94154
}
```

**Known subtypes:**

| Subtype | Description | Key Fields |
|---------|-------------|------------|
| `turn_duration` | Records interaction duration | `durationMs` |
| `compact_boundary` | Context window boundary marker | `compactMetadata`, `logicalParentUuid`, `level` |
| `api_error` | API error with retry info | `error`, `level`, `retryInMs`, `retryAttempt`, `maxRetries` |
| `local_command` | Slash command execution | `content`, `level` |

**Compact Boundary Record:**
```json
{
  "type": "system",
  "subtype": "compact_boundary",
  "uuid": "0cd68c8a-...",
  "parentUuid": null,
  "logicalParentUuid": "uuid-of-last-record-before-compaction",
  "sessionId": "...",
  "timestamp": "2026-01-20T08:52:45.123Z",
  "level": "info",
  "content": "Conversation compacted",
  "compactMetadata": {
    "trigger": "manual",
    "preTokens": 150000
  },
  "isMeta": false
}
```

**API Error Record:**
```json
{
  "type": "system",
  "subtype": "api_error",
  "level": "error",
  "error": {
    "status": 529,
    "error": {"type": "overloaded_error", "message": "Overloaded"}
  },
  "retryInMs": 556.59,
  "retryAttempt": 1,
  "maxRetries": 10
}
```

---

### 5. File History Snapshot (`type: "file-history-snapshot"`)

Tracks file modifications for undo functionality. `isSnapshotUpdate` is `false` for initial snapshots, `true` for updates to existing snapshots.

```json
{
  "type": "file-history-snapshot",
  "messageId": "25415a73-17e7-4f0b-b108-23abb918e794",
  "isSnapshotUpdate": true,
  "snapshot": {
    "messageId": "1d8c063c-6640-495e-a554-a4bf51f069e5",
    "timestamp": "2026-01-20T06:11:26.656Z",
    "trackedFileBackups": {
      "plans/keen-mapping-torvalds.md": {
        "backupFileName": null,
        "version": 1,
        "backupTime": "2026-01-20T06:13:24.820Z"
      }
    }
  }
}
```

---

## Message Content Blocks

The `message.content` array contains content blocks of various types.

### Text Block

```json
{
  "type": "text",
  "text": "Here is my response..."
}
```

### Tool Use Block

```json
{
  "type": "tool_use",
  "id": "toolu_012h5WU9ikhtKZPxtzcpxd3d",
  "name": "Read",
  "input": {
    "file_path": "/path/to/file.py"
  }
}
```

### Tool Result Block

In user messages responding to tool calls:

```json
{
  "type": "tool_result",
  "tool_use_id": "toolu_012h5WU9ikhtKZPxtzcpxd3d",
  "content": "file contents here...",
  "is_error": false
}
```

**Persisted output:** When a tool result exceeds a size threshold, the full output is saved to `{UUID}/tool-results/{tool_use_id}.txt` and the inline `content` is replaced with a `<persisted-output>` tag:

```
<persisted-output>
Output too large (36.3KB). Full output saved to: /path/to/tool-results/toolu_012xxx.txt

Preview (first 2KB):
[truncated content preview]
```

A parser displaying full transcript content should detect this tag and optionally read the referenced file for the complete output.

### Thinking Block

Extended thinking content:

```json
{
  "type": "thinking",
  "thinking": "Let me analyze this problem...",
  "signature": "base64-encoded-signature"
}
```

---

## Record Chain Structure

Records form a linked chain via `uuid` and `parentUuid`:

```
USER (uuid: A, parentUuid: null)
  ↓
ASSISTANT (uuid: B, parentUuid: A) [thinking block]
  ↓
ASSISTANT (uuid: C, parentUuid: B) [text block]
  ↓
ASSISTANT (uuid: D, parentUuid: C) [tool_use block]
  ↓
USER (uuid: E, parentUuid: D) [tool_result block]
  ↓
ASSISTANT (uuid: F, parentUuid: E) [thinking block]
  ↓
ASSISTANT (uuid: G, parentUuid: F) [text block]
  ↓
...
```

**Key observations:**
- First user message has `parentUuid: null`
- Each record points to its immediate predecessor
- Assistant responses may span multiple records (thinking → text → tool_use), all sharing the same `message.id`
- User tool_result records link back to the assistant tool_use record
- The chain alternates: USER → ASSISTANT(s) → USER → ASSISTANT(s) → ...

### Parallel Tool Calls

When Claude makes parallel tool calls, multiple `tool_use` blocks are produced in a single API response. Due to the streaming record model, each `tool_use` gets its own assistant record (all sharing the same `message.id`). The corresponding `tool_result` user records chain sequentially:

```
ASSISTANT (uuid: X, tool_use A, message.id: msg_1)
  ↓
ASSISTANT (uuid: Y, tool_use B, message.id: msg_1)  ← same API response
  ↓
USER (uuid: Z, tool_result for B, parentUuid: Y)
  ↓
USER (uuid: W, tool_result for A, parentUuid: Z)
```

> **Important:** A `tool_result`'s `parentUuid` points to its predecessor in the chain, NOT necessarily to the assistant record that made that `tool_use` call. To correlate a `tool_result` with its `tool_use`, match on `tool_use_id` rather than relying on `parentUuid`.

### Identifying User-Typed Prompts

User records include both user-typed prompts and system-generated messages. To distinguish:

| Message Type | Has Assistant Child | Has `sourceToolAssistantUUID` | `isMeta` | `isCompactSummary` |
|--------------|---------------------|-------------------------------|----------|---------------------|
| User-typed prompt | yes | no | no | no |
| Tool result | yes | yes | no | no |
| Compact summary | varies | no | no | **yes** |
| System-injected (isMeta) | varies | no | yes | no |

**Rule:** A user message is user-typed if and only if:
1. It has an `assistant` record as its child (check `parentUuid` of other records)
2. It does NOT have `sourceToolAssistantUUID` field
3. It does NOT have `isMeta: true`
4. It does NOT have `isCompactSummary: true`

> **Gotcha:** Compact summary records (`isCompactSummary: true`) contain long system-generated context summaries starting with "This session is being continued from a previous conversation...". They lack both `sourceToolAssistantUUID` and `isMeta`, so they will pass through a filter that only checks those two fields. Always filter on `isCompactSummary` as well.

### Context Windows

Sessions are divided into context windows by compaction boundaries. A new context window starts when:

1. **Explicit boundary:** A `system` record with `subtype: "compact_boundary"` exists
2. **Implicit boundary:** A `user` record has `parentUuid: null` (not the first message)

```
[Context Window 0]
USER (parentUuid: null) ← session start
  ↓ chain continues...
  ↓
SYSTEM (subtype: compact_boundary, parentUuid: null) ← explicit boundary
  ↓
[Context Window 1]
USER (isCompactSummary: true, parentUuid: compact_boundary_uuid) ← skip this
  ↓
USER (parentUuid: isCompactSummary_uuid) ← real first prompt
  ↓ chain continues...
  ↓
[Context Window 2]
USER (parentUuid: null) ← implicit boundary (no preceding compact_boundary)
  ↓ chain continues...
```

Every explicit `compact_boundary` is followed by an `isCompactSummary` user record containing the compacted context summary. This record should be skipped when listing user prompts.

---

## Additional Record Types

### 6. Queue-Operation Record (`type: "queue-operation"`)

Tracks queued prompts/operations. Minimal structure without standard fields.

```json
{
  "type": "queue-operation",
  "operation": "enqueue",
  "timestamp": "2026-01-20T09:32:57.331Z",
  "sessionId": "session-id",
  "content": "queued prompt text"
}
```

**Operations:** `enqueue`, `dequeue`, `popAll`, `remove`

The `content` field is present for `enqueue` and `popAll` operations but absent for `dequeue` and `remove`.

`content` can be plain text (a queued user prompt) or a JSON-encoded string containing a structured task object: `{"task_id":"aaa918e","description":"Explore code","task_type":"local_agent"}`. It may also contain XML `<task-notification>` blocks.

---

### 7. Summary Record (`type: "summary"`)

Short session title used for terminal window names. Minimal structure without uuid/timestamp/sessionId.

```json
{
  "type": "summary",
  "summary": "Session History Navigation with Compaction Support",
  "leafUuid": "uuid-of-last-user-message-in-previous-session"
}
```

**Notes:**
- `summary` is a short title (typically 40-55 chars), not a detailed description
- `leafUuid` is a **cross-file reference** pointing to the last user message in a previous session
- Written when continuing from a previous session - describes what that session was about
- Multiple summaries in one file = session was continued from multiple previous sessions

> **Unverified as of 2026-02-24:** Zero summary records were found across all local project data. This record type may be rare, workflow-dependent, or no longer emitted. The structure above is from earlier observations and should be re-verified.

---

## Common Fields Reference

### Required on Most Records

| Field | Type | Description |
|-------|------|-------------|
| `type` | string | Record type identifier |
| `uuid` | string | Unique record ID (RFC 4122 UUID) |
| `parentUuid` | string\|null | Parent record UUID |
| `sessionId` | string | Session identifier (UUID) |
| `timestamp` | string | ISO 8601 datetime |

**Exceptions:** `file-history-snapshot`, `summary`, and `queue-operation` records use minimal field structures and may lack uuid, parentUuid, sessionId, or timestamp.

### Common Optional Fields

| Field | Type | Description |
|-------|------|-------------|
| `cwd` | string | Current working directory |
| `version` | string | Claude Code version (e.g., "2.1.12") |
| `gitBranch` | string | Current git branch (empty if not in repo) |
| `isSidechain` | boolean | True for records in subagent threads (always false in main session file) |
| `isMeta` | boolean | On system records: `false` when present (absent on some `api_error` records). On user records: `true` for system-injected messages (e.g., local commands) |
| `userType` | string | User type (typically "external") |
| `slug` | string | Human-readable conversation identifier |
| `agentId` | string | Hex agent identifier (7-char ≤v2.1.49, 17-char v2.1.50+) |
| `requestId` | string | API request ID |
| `thinkingMetadata` | object | Extended thinking config (user records only, see User Record section) |

---

## Models

Known model identifiers:
- `claude-opus-4-6`
- `claude-sonnet-4-6`
- `claude-opus-4-5-20251101`
- `claude-sonnet-4-5-20250929`
- `claude-haiku-4-5-20251001`

Short aliases (observed in subagent `data.model` or tool inputs, not in `message.model`):
- `sonnet`, `haiku`, `opus`

Other observed values:
- `<synthetic>` — system-generated (non-API) assistant records (see API Error Messages)

---

## Version History

Observed client versions: `2.0.64` through `2.1.53`

Notable changes by version:
- **v2.1.38**: `sourceToolUseID` on meta user records, `isApiErrorMessage` assistant records
- **v2.1.42**: `data.resume` on agent_progress
- **v2.1.45**: `mcp_progress` type
- **v2.1.50**: Agent hash length changed from 7-char to 17-char hex; acompact hash from 6-char to 16-char hex; new `message.usage` fields (`server_tool_use`, `inference_geo`, `iterations`, `speed`); new message fields (`context_management`, `container`)

---

## Keeping This Spec Updated

This spec is reverse-engineered from observed JSONL data. Claude Code does not publish a schema for its history format, so the spec must be periodically audited against real session files.

### When to audit

- After upgrading Claude Code to a new minor version (e.g., 2.1.x to 2.2.x)
- When a parser encounters unknown record types or fields
- Quarterly as a maintenance check

### How to audit

1. **Check the changelog** for hints about format changes: https://github.com/anthropics/claude-code/blob/main/CHANGELOG.md — look for mentions of history, JSONL, session files, subagents, compaction, or tool results.

2. **Sample recent JSONL files** across multiple projects:
   ```bash
   # Find recent session files
   find ~/.claude/projects/ -name "*.jsonl" -mtime -7 -maxdepth 2 | head -10

   # Check for new record types
   jq -r '.type' FILE.jsonl | sort -u

   # Check for new fields on a record type
   jq -c 'select(.type == "assistant") | keys' FILE.jsonl | head -5

   # Check current version range
   grep -roh '"version":"[^"]*"' ~/.claude/projects/ | sort -u
   ```

3. **Compare against the spec** — look for:
   - New record types not documented
   - New fields on existing record types
   - Changed field formats or value ranges
   - New file naming patterns in `{UUID}/subagents/` or `{UUID}/tool-results/`
   - New model identifiers

4. **Verify with a second pass** — audit findings should be independently confirmed against real data before being committed to the spec. Use a separate grep/jq query for each claimed finding.

### What to update

- Add new record types, fields, and progress subtypes with the version they first appeared
- Update version range bounds
- Add new model identifiers
- Mark unverifiable claims (e.g., record types with zero occurrences in local data)
- Update this "Version History" section with notable changes per version

**Last audited:** 2026-02-24 against versions 2.0.64–2.1.53
