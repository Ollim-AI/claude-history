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

**Async (background) agent results (v2.1.45+):**

When a Task/Agent tool call has `run_in_background: true`, the result flow differs from synchronous agents:

1. **Immediate `tool_result`**: The `tool_result` block content is `[{"type": "text", "text": "Async agent launched successfully.\nagentId: abccd67 ..."}]`. The `toolUseResult` has a distinct schema:
   ```json
   {
     "isAsync": true,
     "status": "async_launched",
     "agentId": "abccd67",
     "description": "task description",
     "prompt": "agent prompt...",
     "outputFile": "/tmp/claude-1000/.../tasks/abccd67.output"
   }
   ```

2. **Queue enqueue**: A `queue-operation` record with `operation: "enqueue"` is written at launch, containing a JSON-encoded task descriptor: `{"task_id":"abccd67","description":"...","task_type":"local_agent"}`.

3. **Completion notification**: When the background agent finishes, two records appear:
   - A `queue-operation` with `operation: "enqueue"` containing a `<task-notification>` XML string
   - A **user record** with `message.content` as a **plain string** (not an array) containing the same `<task-notification>`:
     ```xml
     <task-notification>
     <task-id>abccd67</task-id>
     <tool-use-id>toolu_...</tool-use-id>
     <output-file>/tmp/.../tasks/abccd67.output</output-file>
     <status>completed</status>
     <summary>Agent "Batch 1" completed</summary>
     <result>Detailed result text...</result>
     <usage><total_tokens>27382</total_tokens><tool_uses>22</tool_uses><duration_ms>70993</duration_ms></usage>
     </task-notification>
     Full transcript available at: /tmp/.../tasks/abccd67.output
     ```
   > **Version note:** `<tool-use-id>` and `<output-file>` tags were added in v2.1.74. Earlier versions (v2.1.45) place them only in the `toolUseResult` metadata. The `<usage>` tag also changed from plain text (`total_tokens: 27382`) to nested XML (`<total_tokens>27382</total_tokens>`) at the same version. Parsers should handle both formats.

The `<task-id>` matches the `agentId` from step 1, linking the completion back to the original tool call. The completion user record's `parentUuid` points to the `turn_duration` system record (not the original tool_use assistant), so correlation must use `task-id` ↔ `agentId`, not `parentUuid`.

> **Gotcha for parsers:** The immediate `tool_result` only says "Async agent launched" — it does NOT contain the agent's work product. To show actual results, parsers must find the `<task-notification>` user record with matching `<task-id>` and use its `<result>` content.

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

**Teammate-message records (v2.1.63+, agent teams only):**

In agent teams sessions, messages from teammates are delivered as user records where `message.content` is a **plain string** (not an array) containing XML:

```
<teammate-message teammate_id="critic" color="yellow" summary="Risk assessment findings">
  [message body — markdown text, or JSON for system notifications]
</teammate-message>
```

XML attributes:

| Attribute | Type | Required | Description |
|-----------|------|----------|-------------|
| `teammate_id` | string | yes | Short teammate name (e.g., `"critic"`, `"architect"`) or `"team-lead"` |
| `color` | string | yes | Display color (observed: `"yellow"`, `"blue"`, `"green"`, `"purple"`) |
| `summary` | string | no | Short message preview. Absent on idle notifications and some system messages |

These records have `teamName` but do **not** have `isMeta`, `sourceToolAssistantUUID`, or `isCompactSummary`. They pass all existing user-typed prompt filters and require a fifth exclusion rule (see §Identifying User-Typed Prompts).

The message body has several content shapes:

| Shape | Description | Example |
|-------|-------------|---------|
| Markdown text | Substantive message from teammate | Reports, proposals, critiques |
| `idle_notification` JSON | Teammate went idle | `{"type":"idle_notification","from":"critic","timestamp":"...","idleReason":"available"}` |
| `task_assignment` JSON | Task dispatched to teammate | `{"type":"task_assignment","taskId":"1","subject":"...","description":"...","assignedBy":"team-lead","timestamp":"..."}` |
| `shutdown_request` JSON | Shutdown protocol | `{"type":"shutdown_request","requestId":"shutdown-{unix_ms}@{name}","from":"team-lead","timestamp":"..."}` |

`idleReason` values observed: `"available"`, `"interrupted"`.

Teammate-message records also appear in subagent files — the spawn prompt delivered to a teammate arrives wrapped in `<teammate-message teammate_id="team-lead" ...>`.

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

**Hook errors in tool_result blocks:** When a hook blocks a tool call (exit code 2), the error appears as a `tool_result` block with `is_error: true` in the subsequent user record. The content follows the pattern `{hookEvent}:{toolName} hook error: [{hook_path}]: {reason}`:

```
PreToolUse:Bash hook error: [/home/user/.claude/hooks/security_validator.py]: SECURITY: Use `uv run` instead of calling python directly.
Blocked: python3 -c "import json..."
```

These are distinct from regular tool errors. Parsers can identify them via the regex `^(PreToolUse|PostToolUse):\w+ hook error:`.

**Hook context injection in assistant text blocks:** When a hook returns `additionalContext` in its `hookSpecificOutput`, Claude Code injects it as a `<system-reminder>` tag in the next assistant text block:

```
<system-reminder>
PreToolUse:Read hook additional context: CRITICAL: cli.py is 1117 lines. This file is way too long.
</system-reminder>
```

Format: `{hookEvent}:{toolName} hook additional context: {message}`. These tags are embedded in the assistant's text content and can be extracted via regex.

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
| `stop_hook_summary` | Post-turn hook execution summary (v2.1.38+) | `hookCount`, `hookInfos`, `hookErrors`, `preventedContinuation`, `stopReason`, `hasOutput`, `level`, `toolUseID` |

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

**Stop Hook Summary Record (v2.1.38+):**
```json
{
  "type": "system",
  "subtype": "stop_hook_summary",
  "hookCount": 1,
  "hookInfos": [
    {
      "command": "/path/to/script.sh"
    }
  ],
  "hookErrors": [],
  "preventedContinuation": false,
  "stopReason": "",
  "hasOutput": false,
  "level": "suggestion",
  "toolUseID": "d3c7b71f-..."
}
```

`hookInfos` is an array of objects. Each has a `command` field (always present). Prompt/agent hooks also include `promptText` with the prompt string. `hookErrors` is an array of error strings (empty when no errors). `preventedContinuation` is `true` when a Stop hook blocked Claude from stopping. `level` is typically `"suggestion"`.

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
  },
  "caller": {"type": "direct"}
}
```

`caller` (object|null, v2.1.38+) is present on tool_use blocks. Only observed value: `{"type": "direct"}` or `null`.

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

#### Agent/Subagent Parallel Calls (v2.1.76+)

When parallel Agent tool calls are made, the second (and subsequent) `tool_use` assistant records do NOT chain directly from the first. Instead, the second tool_use's `parentUuid` points to a progress record deep in an `agent_progress` chain descending from the first tool_use:

```
ASSISTANT (uuid: A, text)
├── ASSISTANT (uuid: B, tool_use Agent1)
│   ├── USER (uuid: C, tool_result)           ← dead end for chain walker
│   └── PROGRESS (uuid: P1, parentUuid: B)
│       └── PROGRESS (uuid: P2, parentUuid: P1)
│           └── PROGRESS (uuid: P3, parentUuid: P2)
│               └── ASSISTANT (uuid: D, tool_use Agent2, parentUuid: P3)
│                   └── USER (uuid: E, tool_result)
│                       └── ASSISTANT (uuid: F, continues...)
```

> **Critical for chain walkers:** A naive traversal that skips progress records entirely will stop at C (dead end) and miss D, E, F — truncating the transcript. Parsers must follow through progress child chains at dead ends to reach the continuation. The progress chain depth varies (observed 2–5 stubs).

### Identifying User-Typed Prompts

User records include both user-typed prompts and system-generated messages. To distinguish:

| Message Type | Has Assistant Child | Has `sourceToolAssistantUUID` | `isMeta` | `isCompactSummary` | `message.content` type |
|--------------|---------------------|-------------------------------|----------|---------------------|------------------------|
| User-typed prompt | yes | no | no | no | array or plain string |
| Tool result | yes | yes | no | no | array |
| Compact summary | varies | no | no | **yes** | array |
| System-injected (isMeta) | varies | no | yes | no | array or string |
| Teammate message | yes | no | no | no | **string** (starts with `<teammate-message`) |
| Local command artifact | varies | no | varies | no | **string** (`<bash-input>`, `<bash-stdout>`, `<local-command-caveat>`) |
| Task notification | varies | no | no | no | **string** (starts with `<task-notification>`) |
| Interruption message | yes | no | no | no | array (text starts with `[Request interrupted`) |

**String content shapes:** Multiple user record types use string `message.content` (not just teammate messages):

| String prefix | Record type | Has `isMeta` | Has assistant child |
|---------------|-------------|--------------|---------------------|
| `<teammate-message` | Teammate message (v2.1.63+) | no | yes |
| `<bash-input>` | CLI command input | no | no |
| `<bash-stdout>` / `<bash-stderr>` | CLI command output | no | no |
| `<local-command-caveat>` | Local command warning | yes | no |
| `<task-notification>` | Background task result | no | varies |
| *(plain text)* | User-typed prompt | no | yes |

**Rule:** A user message is user-typed if and only if:
1. It has an `assistant` record as its child (check `parentUuid` of other records)
2. It does NOT have `sourceToolAssistantUUID` field
3. It does NOT have `isMeta: true`
4. It does NOT have `isCompactSummary: true`
5. Its `message.content` is NOT a string starting with `<teammate-message` (v2.1.63+: see §Teammate-message records)
6. Its text content does NOT start with `[Request interrupted` (system-injected when the user interrupts a tool call — lacks `isMeta` and `sourceToolAssistantUUID`)

> **Gotcha:** Compact summary records (`isCompactSummary: true`) contain long system-generated context summaries starting with "This session is being continued from a previous conversation...". They lack both `sourceToolAssistantUUID` and `isMeta`, so they will pass through a filter that only checks those two fields. Always filter on `isCompactSummary` as well.

> **Gotcha (v2.1.63+):** Teammate-message records lack `isMeta`, `sourceToolAssistantUUID`, and `isCompactSummary`, and they DO have an assistant child. They pass all four original filters. To exclude them, check whether `message.content` is a string starting with `<teammate-message`. Do NOT exclude all string content — plain-text user prompts and local command artifacts also use string content but are distinguished by other fields (no assistant child, `isMeta`, etc.).

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

### 8. Custom Title Record (`type: "custom-title"`)

User-set session name via `claude --resume "name"` or `claude --name "name"`. Minimal structure with only `type`, `customTitle`, and `sessionId`.

```json
{
  "type": "custom-title",
  "customTitle": "mintlify-skill-auto-improve",
  "sessionId": "9414f68c-5327-4dda-a1c9-2c4421a1172d"
}
```

**Notes:**
- `customTitle` is user-chosen (not auto-generated like `slug`)
- May appear multiple times in a file (observed duplicates)
- Used by `claude --resume` to find sessions by friendly name

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
| `teamName` | string | Active team name during agent teams sessions (v2.1.63+). Present on user, assistant, hook_progress, and system records while a team is active. Absent on agent_progress, queue-operation, and file-history-snapshot records. See §Agent Teams. |
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

## Agent Teams (v2.1.63+, experimental)

Agent teams sessions introduce team coordination records into the JSONL format. A team session has one lead (the main session file) and multiple teammates (each with their own subagent file). All findings below are from N=1 observed sessions — patterns may evolve.

### teamName Field

`teamName` (string) is a top-level field on records written while a team is active. It identifies which team was active when the record was created.

**Transition mechanics:**
- `teamName` first appears on the user `tool_result` record AFTER the `TeamCreate` assistant record (the TeamCreate assistant record itself lacks `teamName`)
- `teamName` drops from the user `tool_result` record AFTER the `TeamDelete` assistant record (the TeamDelete assistant record still carries `teamName`)
- `teamName` blocks are contiguous within a team lifecycle (excluding queue-operation records, which never carry `teamName`)

**Which record types carry `teamName`:**

| Record type | Gets `teamName`? | Notes |
|-------------|-----------------|-------|
| `user` | Yes | During team phase |
| `assistant` | Yes | During team phase |
| `progress` (hook_progress) | Yes | Only hook_progress |
| `progress` (agent_progress) | **No** | Never, even during team phase |
| `system` | Yes | If compaction/turn occurs during team phase |
| `queue-operation` | **No** | Never |
| `file-history-snapshot` | **No** | Never |

**Compaction during team phases:** When a `compact_boundary` occurs during an active team phase, both the compact_boundary and the subsequent `isCompactSummary` user record inherit the active `teamName`.

### Agent Tool: Teammate Spawn Variant

When the Agent tool spawns a teammate (vs a regular subagent), the `tool_use` input includes additional fields:

| Field | Type | Description |
|-------|------|-------------|
| `name` | string | Teammate short name (e.g., `"architect"`, `"critic"`) |
| `team_name` | string | Team the agent joins |
| `run_in_background` | boolean | Optional. Agent runs without blocking lead |

The `toolUseResult` has a completely different schema from regular Agent results:

**Regular Agent `toolUseResult` (`status: "completed"`):**
```json
{
  "status": "completed",
  "agentId": "a3f89066590741a24",
  "prompt": "...",
  "content": [...],
  "totalDurationMs": 64383,
  "totalTokens": 61470,
  "totalToolUseCount": 30,
  "usage": {...}
}
```

**Teammate spawn `toolUseResult` (`status: "teammate_spawned"`):**
```json
{
  "status": "teammate_spawned",
  "prompt": "...",
  "teammate_id": "architect@agent-refactor",
  "agent_id": "architect@agent-refactor",
  "agent_type": "general-purpose",
  "model": "opus",
  "name": "architect",
  "color": "blue",
  "tmux_session_name": "in-process",
  "tmux_window_name": "in-process",
  "tmux_pane_id": "in-process",
  "team_name": "agent-refactor",
  "is_splitpane": false,
  "plan_mode_required": false
}
```

> **Critical for parsers:** The `teammate_spawned` variant lacks `agentId` (the hex identifier used to locate subagent files). The `agent_id` field uses `name@team` format instead. Parsers relying on `toolUseResult.agentId` to find subagent files will not find team member files through this mechanism. Team subagent files can be located via `data.agentId` in `agent_progress` records — but see the note below about missing progress records.

### No agent_progress for Team Subagents

Regular subagents produce `agent_progress` records in the main session file, allowing the lead to track their work. **Team subagents produce no `agent_progress` records in the main file.** During team phases, the main file shows a gap in records while teammates work in their subagent files.

This means team subagent files cannot be linked from the main file via the normal `agent_progress → data.agentId → agent-{hash}.jsonl` path. The teammate subagent files exist in `{UUID}/subagents/` with standard hex naming but are not referenced from the main session's record chain.

### Team-Specific Tools

These tool names appear exclusively in agent teams sessions. They follow the standard `tool_use`/`tool_result` pattern documented in §Message Content Blocks.

#### TeamCreate

**Input:** `{"team_name": "agent-refactor", "description": "Parallel investigation..."}`

**`toolUseResult`:**
```json
{
  "team_name": "agent-refactor",
  "team_file_path": "/home/user/.claude/teams/agent-refactor/config.json",
  "lead_agent_id": "team-lead@agent-refactor"
}
```

#### TeamDelete

**Input:** `{}` (no parameters)

**`toolUseResult` (success):**
```json
{"success": true, "message": "Cleaned up directories and worktrees for team \"agent-refactor\"", "team_name": "agent-refactor"}
```

**`toolUseResult` (failure — active members):**
```json
{"success": false, "message": "Cannot cleanup team with 1 active member(s): free-fn. Use requestShutdown to gracefully terminate teammates first.", "team_name": "stream-design"}
```

#### SendMessage

**Input (message):** `{"type": "message", "recipient": "critic", "content": "...", "summary": "Cross-review findings"}`

**Input (shutdown_request):** `{"type": "shutdown_request", "recipient": "architect", "content": "Investigation complete"}`

`type` values: `"message"`, `"broadcast"`, `"shutdown_request"`, `"shutdown_response"`, `"plan_approval_response"`

**`toolUseResult`:**
```json
{
  "success": true,
  "message": "Message sent to critic's inbox",
  "routing": {
    "sender": "team-lead",
    "target": "@critic",
    "targetColor": "yellow",
    "summary": "Cross-review findings",
    "content": "Full message text..."
  }
}
```

Shutdown request ID format: `shutdown-{unix_ms}@{recipient_name}`

#### TaskCreate

**Input:** `{"subject": "Analyze module coupling", "description": "Read all 6 files...", "activeForm": "Analyzing module coupling"}`

#### TaskUpdate

**Input:** `{"taskId": "1", "status": "in_progress"}` or `{"taskId": "4", "addBlockedBy": ["1", "2", "3"]}` or `{"taskId": "1", "owner": "architect"}`

**`toolUseResult`:**
```json
{"success": true, "taskId": "4", "updatedFields": ["status"], "statusChange": {"from": "pending", "to": "in_progress"}}
```

`statusChange` is present only when status changed.

#### TaskList

**`toolUseResult`:**
```json
{
  "tasks": [
    {"id": "1", "subject": "Propose design", "status": "in_progress", "owner": "architect", "blockedBy": []},
    {"id": "2", "subject": "Review design", "status": "pending", "blockedBy": ["1"]}
  ]
}
```

---

## Version History

Observed client versions: `2.0.64` through `2.1.76`

Notable changes by version:
- **v2.1.38**: `sourceToolUseID` on meta user records, `isApiErrorMessage` assistant records, `stop_hook_summary` system subtype, `caller` field on tool_use blocks
- **v2.1.42**: `data.resume` on agent_progress
- **v2.1.45**: `mcp_progress` type
- **v2.1.50**: Agent hash length changed from 7-char to 17-char hex; acompact hash from 6-char to 16-char hex; new `message.usage` fields (`server_tool_use`, `inference_geo`, `iterations`, `speed`); new message fields (`context_management`, `container`)
- **v2.1.63**: Agent teams support: `teamName` field, teammate-message user records, TeamCreate/TeamDelete/SendMessage/TaskCreate/TaskUpdate/TaskList tools, `teammate_spawned` Agent toolUseResult variant, no `agent_progress` for team subagents
- **v2.1.76**: `slug` field on progress records (was previously only on user/assistant records); parallel Agent tool_use records chain through deep progress stub chains instead of directly (see §Parallel Tool Calls); `[Request interrupted by user for tool use]` user records lack `isMeta` flag

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

**Last audited:** 2026-03-15 against versions 2.0.64–2.1.76 (v2.1.76: parallel agent chains, slug on progress, interruption messages)
