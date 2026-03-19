---
name: audit-claude-history
description: Audit how agents use claude-history in practice — find failures, inefficiencies, and wasted tokens, then produce actionable tool improvement recommendations. Use when analyzing agent usage patterns, diagnosing recurring tool failures, or planning claude-history UX improvements.
argument-hint: <timeframe or focus area, e.g. "2w", "search failures", "ollim-bot">
allowed-tools: Agent, Bash, Read, Grep, Glob, Write
---

# Audit Claude History Usage

Analyze how agents use `claude-history` in real sessions, identify failure patterns and inefficiencies, and produce actionable recommendations for tool improvements.

## Why this skill exists

Agents make systematic mistakes with claude-history that waste tokens and produce wrong results. The most common: confusing search result IDs with session IDs, forgetting `--cwd` between commands, using invalid flags, and burning context on failed lookups. This skill finds these patterns across real sessions and turns them into concrete fix recommendations.

## Workflow

### 1. Scope the audit

Determine what to analyze based on user input:

| Input | Scope |
|-------|-------|
| A timeframe (`2w`, `1m`) | All projects, that time range |
| A project name | That project, last 4 weeks |
| A focus area (`search failures`) | All projects, filtered to that pattern |
| No input | All projects, last 2 weeks |

Identify target projects by listing directories with recent session activity:

```bash
find ~/.claude/projects/ -name "*.jsonl" -mtime -N -printf '%h\n' 2>/dev/null | sort -u | grep -v worktrees | head -20
```

### 2. Choose parallelism strategy

Before spawning agents, check which projects actually have claude-history usage:

```bash
for proj_dir in $(find ~/.claude/projects/ -name "*.jsonl" -mtime -N -printf '%h\n' 2>/dev/null | sort -u | grep -v worktrees); do
  count=$(grep -r -l "claude-history" "$proj_dir" 2>/dev/null | wc -l)
  if [ "$count" -gt 0 ]; then
    echo "$count $proj_dir"
  fi
done | sort -rn
```

| Result | Strategy |
|--------|----------|
| 0-1 projects with usage | Skip subagents — run all 3 analyses inline |
| 2-3 projects, each with 10+ hits | **Cartesian**: spawn `3 x N` agents (one per dimension per project), then merge. Cross-project patterns are synthesized in step 3 instead. |
| 4+ projects, or most have <10 hits | **Per-dimension** (default): spawn 3 agents, each iterates all projects. Better for spotting cross-project patterns and avoids agent sprawl. |

Cartesian is faster when projects are large and slow to search, but loses cross-project visibility within each agent. Only use it when N is small (2-3) and each project has substantial usage.

### 3. Launch parallel analysis agents

Default: spawn 3 subagents in parallel, one per analysis dimension. Each agent searches across all target projects independently. If using Cartesian strategy, spawn one agent per (dimension, project) pair and adjust step 4 to merge per-project results before cross-referencing.

**Agent 1: Failure Pattern Analyzer**

Prompt:
```
Analyze claude-history command failures across these projects: [PROJECT_LIST]
Timeframe: [TIMEFRAME]

For each project, run:
  claude-history search "claude-history" --cwd <project_path> --since <timeframe>

Then for sessions with substantive usage (agents actually running commands, not just
mentioning the tool), read transcripts with --show-thinking to trace the full
command sequence.

Classify every failure into these categories:
1. SESSION_ID_CONFUSION — agent used an ID from search output that doesn't resolve
   as a session (it was a response UUID, subagent ID, or from a different project)
2. CROSS_PROJECT_CONTEXT — agent forgot --cwd or used wrong --cwd on follow-up commands
3. INVALID_FLAG — agent used a flag that doesn't exist on that command
   (e.g., --size on subagents, -v on transcript)
4. STALE_REFERENCE — agent referenced a session/subagent that was cleaned up
5. PAGINATION_ERROR — agent requested a page out of range
6. OTHER — describe the failure

For each failure, record:
- Session ID where the failure occurred
- Project context
- Exact command that failed
- Error message received
- What the agent did next (retry count, eventual success/failure)
- Root cause

Output a structured summary with counts per category and the 5 most illustrative examples.
```

**Agent 2: Command Efficiency Analyzer**

Prompt:
```
Analyze claude-history command efficiency across these projects: [PROJECT_LIST]
Timeframe: [TIMEFRAME]

For each project, search for claude-history command invocations:
  claude-history search "claude-history" --cwd <project_path> --since <timeframe>

Read transcripts of sessions with heavy claude-history usage (5+ commands).
For each investigation session, measure:

1. COMMANDS_PER_INVESTIGATION — total claude-history commands run
2. WASTED_COMMANDS — commands that returned errors or useless results
3. REDUNDANT_SEARCHES — same/similar query run multiple times with different flags
4. SEARCH_TO_READ_RATIO — searches run vs transcripts actually read
5. DISCOVERY_METHOD — did the agent start with search, sessions, or prev-N?
6. GOAL_ACHIEVED — did the investigation answer the original question?

Also identify:
- Did agents follow the discover -> read -> synthesize workflow from the SKILL.md?
- Did agents fall into anti-patterns (listing-and-stopping, search-only, prompts-only)?
- What was the most efficient investigation pattern observed?
- What was the least efficient?

Output: efficiency metrics table, top 3 efficient patterns, top 3 wasteful patterns
with concrete examples.
```

**Agent 3: Token Usage Analyzer**

Prompt:
```
Analyze token efficiency of claude-history usage across these projects: [PROJECT_LIST]
Timeframe: [TIMEFRAME]

For each project, search for claude-history command invocations:
  claude-history search "claude-history" --cwd <project_path> --since <timeframe>

Focus on sessions where agents consumed large amounts of output. Look for:

1. HEAD_PIPE_PATTERN — commands piped through "| head -N"
   Count how many, what N values, estimate wasted subprocess work
2. FULL_TRANSCRIPT_READS — transcript commands without --hide-tools or --prompts-only
   when the agent only needed specific information
3. SEARCH_FALSE_POSITIVES — search results dominated by boilerplate/template matches
   rather than relevant content (e.g., SDK examples, commit message templates)
4. FAILED_LOOKUP_COST — commands that returned "No session found" errors
   Count total failed lookups and estimate wasted tokens per failure
5. REPEATED_CONTEXT_LOADING — same transcript read multiple times across
   retries or with different flags

For each pattern, estimate relative token cost:
- Small: <500 tokens wasted
- Medium: 500-2000 tokens wasted
- Large: 2000+ tokens wasted

Output: token waste patterns ranked by severity, with concrete examples
and suggested tool changes to prevent each pattern.
```

### 4. Synthesize agent results

Once all 3 agents return, merge their findings:

1. **Cross-reference failures with efficiency** — failures that also caused retry loops are highest priority
2. **Cross-reference failures with token waste** — failures that burned the most tokens are highest priority
3. **Deduplicate** — same root cause may appear in multiple analyses
4. **Rank by impact** — frequency x token_cost x user_frustration

### 5. Generate recommendations

For each finding, produce a specific recommendation:

| Finding type | Recommendation format |
|-------------|----------------------|
| Error message is unhelpful | Exact new error message text |
| Missing flag | Flag name, behavior, which commands |
| Search returns wrong ID types | Output format change with example |
| Agents forget --cwd | Behavioral change (sticky context, auto-detect) |
| Output too large | New limiting mechanism |

Categorize recommendations by implementation effort:
- **Quick wins** — error message improvements, flag aliases
- **Medium** — new flags, output format changes
- **Large** — behavioral changes, new commands

### 6. Write the audit report

Write to `/tmp/claude-history-audit-<date>.md`:

```markdown
# Claude History Usage Audit — <date>

## Summary
<3-5 bullet executive summary>

## Failure Patterns
<ranked list with counts and examples>

## Efficiency Analysis
<metrics table, best/worst patterns>

## Token Waste Analysis
<ranked patterns with estimated cost>

## Recommendations
### Quick Wins
<list>
### Medium Effort
<list>
### Large Effort
<list>

## Raw Data
<session IDs analyzed, commands counted, projects covered>
```

Present the summary and recommendations to the user. Link to the full report.

## Known failure patterns to look for

These patterns were identified in the initial audit (March 2026) and should be tracked for recurrence:

| Pattern | Root cause | Observed frequency |
|---------|-----------|-------------------|
| Search IDs don't resolve as sessions | Search returns session IDs from context that doesn't match transcript's resolution | 30+ errors across 3 agents |
| `--cwd` forgotten on follow-up commands | Agent sets --cwd on search but not on transcript | ~20 errors |
| `subagents --size N` | --size only exists on sessions command | Observed once |
| `transcript -v` | Old SKILL.md referenced -v flag that doesn't exist | Multiple prompt references |
| Page N out of range | No way to know total pages before first query | Observed once |
| `--project` quoting issues | Project paths with dashes need quoting | 3 errors |
| Redundant search loops | Agent tries 5+ --cwd/--project combos for same query | 20+ searches in one agent |
| `| head -N` on every command | No built-in output limiting | Universal pattern |
| Search matches boilerplate | "authentication" matches SDK examples, not actual auth work | 89 false positives in one search |

## Metrics to track across audits

Track these over time to measure whether tool improvements are working:

- **Failure rate**: failed commands / total commands
- **Commands per investigation**: total commands to answer one question
- **Retry rate**: retries after first failure / total failures
- **Search precision**: relevant results / total results (sample-based)
- **Resolution rate**: IDs from search that successfully resolve in transcript
