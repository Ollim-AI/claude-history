---
name: update-spec
description: Audit SPEC.md against real JSONL history files — spawns parallel specialists to reverse-engineer the format and find gaps. Use when upgrading Claude Code, encountering unknown fields, or doing periodic maintenance.
argument-hint: [--fix]
allowed-tools: Agent, Bash, Read, Edit, Glob, Grep
---

# Update Spec

Audit SPEC.md against real JSONL session files. Spawns parallel specialist agents to analyze different dimensions of the format, compares findings against the documented spec, and reports gaps. With `--fix`, edits SPEC.md directly.

## Workflow

### 1. Sample recent JSONL files

Select 4-6 files: recent, from diverse projects, under 5MB each. Prefer variety over size — a 2MB file from a different project beats a 15MB file from the same one.

```bash
# Find recent files, sorted by size, pick from different project dirs
find ~/.claude/projects/ -name "*.jsonl" -maxdepth 2 -mtime -14 2>/dev/null |
  while read f; do echo "$(stat -c %s "$f") $f"; done |
  sort -rn | awk -F/ '!seen[$(NF-1)]++ && $1 < 5000000' | head -6
```

Store the selected file paths in a bash array for the specialists to reference.

Also sample subagent files:
```bash
find ~/.claude/projects/ -path "*/subagents/agent-*.jsonl" -mtime -14 2>/dev/null | head -10
```

### 2. Launch 5 parallel specialists

Spawn all 5 as background agents. Each gets the same file list and reads SPEC.md independently. Each returns a structured gap report.

**Specialist prompts must include:**
- The exact file paths to analyze (from step 1)
- Instruction to read `/home/julius/claude-history/SPEC.md` (or the project's SPEC.md path)
- Instruction to return: gaps found, sample values, priority (high/medium/low)
- Instruction: research only, do not edit files

#### Specialist 1: Record types & top-level fields

Extract all unique `type` values and their top-level keys. Compare against spec's documented record types and fields.

```bash
# All record types
for f in "${FILES[@]}"; do jq -r '.type' "$f" 2>/dev/null; done | sort -u

# Keys per record type
for t in user assistant progress system file-history-snapshot queue-operation summary custom-title; do
  echo "=== $t ==="
  for f in "${FILES[@]}"; do jq -c "select(.type == \"$t\") | keys" "$f" 2>/dev/null; done | sort -u | head -5
done

# New record types
for f in "${FILES[@]}"; do jq -r '.type' "$f" 2>/dev/null; done | sort -u |
  grep -v -E '^(user|assistant|progress|system|file-history-snapshot|queue-operation|summary|custom-title)$'
```

#### Specialist 2: Progress & system subtypes

Extract all `data.type` values from progress records and `subtype` values from system records. For each, extract the full set of subtype-specific keys and compare against spec.

```bash
# Progress subtypes with counts
for f in "${FILES[@]}"; do jq -r 'select(.type == "progress") | .data.type' "$f" 2>/dev/null; done | sort | uniq -c | sort -rn

# System subtypes with counts
for f in "${FILES[@]}"; do jq -r 'select(.type == "system") | .subtype' "$f" 2>/dev/null; done | sort | uniq -c | sort -rn

# Keys per subtype
for st in agent_progress bash_progress hook_progress mcp_progress; do
  echo "=== $st ==="
  for f in "${FILES[@]}"; do jq -c "select(.type == \"progress\" and .data.type == \"$st\") | .data | keys" "$f" 2>/dev/null; done | sort -u | head -3
done
```

#### Specialist 3: Models, versions, service tiers

Broader scan — use more files (up to 50) since these extractions are lightweight.

```bash
# Models from assistant records
find ~/.claude/projects/ -name "*.jsonl" -maxdepth 2 -mtime -30 2>/dev/null | head -50 |
  while read f; do jq -r 'select(.type == "assistant") | .message.model // empty' "$f" 2>/dev/null; done |
  sort | uniq -c | sort -rn

# Version range
find ~/.claude/projects/ -name "*.jsonl" -maxdepth 2 -mtime -30 2>/dev/null | head -50 |
  while read f; do jq -r '.version // empty' "$f" 2>/dev/null; done |
  sort -V | uniq -c | sort -rn | head -20

# Service tier values
for f in "${FILES[@]}"; do jq -r 'select(.type == "assistant") | .message.usage.service_tier // empty' "$f" 2>/dev/null; done | sort | uniq -c
```

#### Specialist 4: File naming patterns

Scan the filesystem (not file contents) for naming patterns in subagent and tool-results directories.

```bash
# Subagent file naming patterns and hash lengths
find ~/.claude/projects/ -path "*/subagents/agent-*.jsonl" 2>/dev/null |
  while read f; do name=$(basename "$f" .jsonl); hash=${name#agent-}; hash=${hash#acompact-}; hash=${hash#aside_question-}; echo "${#hash} $name"; done |
  sort | uniq -c | sort -rn | head -20

# Tool-results file patterns
find ~/.claude/projects/ -path "*/tool-results/*" 2>/dev/null | while read f; do basename "$f"; done | sort -u | head -30

# UUID subdirectory types
find ~/.claude/projects/ -mindepth 3 -maxdepth 3 -type d 2>/dev/null | while read d; do basename "$d"; done | sort -u
```

#### Specialist 5: Content blocks & special fields

Extract content block types, tool_use/tool_result field sets, stop_reason values, caller values, and string-vs-array content patterns.

```bash
# Content block types
for f in "${FILES[@]}"; do jq -r '.message.content[]?.type // empty' "$f" 2>/dev/null; done | sort | uniq -c | sort -rn

# tool_use block keys
for f in "${FILES[@]}"; do jq -c 'select(.type == "assistant") | .message.content[]? | select(.type == "tool_use") | keys' "$f" 2>/dev/null; done | sort -u

# stop_reason values
for f in "${FILES[@]}"; do jq -r 'select(.type == "assistant") | .message.stop_reason // empty' "$f" 2>/dev/null; done | sort | uniq -c | sort -rn
```

### 3. Synthesize findings

After all specialists return, merge their reports:

1. **Deduplicate** — the same gap (e.g., `forkedFrom`) may be found by multiple specialists. Keep the most detailed description.
2. **Prioritize**:
   - **High**: New record types, new fields on existing types, spec inaccuracies
   - **Medium**: New file naming patterns, new subtypes, format changes
   - **Low**: Version range updates, unverified items, cosmetic issues
3. **Cross-validate** — if a finding appears in only one specialist's report, verify it with a targeted query before acting on it.

### 4. Report or fix

**Without `--fix`**: Print the merged gap report as a markdown table and stop.

**With `--fix`**: Apply each gap as an Edit to SPEC.md:
- New record types: Add a new subsection under "Additional Record Types"
- New fields: Add to the relevant record type section or Common Optional Fields
- Version range: Update the "Observed client versions" line and "Last audited" line
- New file naming patterns: Add to the File Naming table
- New subtypes: Add to the relevant Progress or System section
- Update the "Last audited" date to today and the current version range

After all edits, update the "Last audited" line at the bottom of SPEC.md with today's date and the new version range.

## Gotchas

- **jq on large files is slow.** A 15MB JSONL file takes minutes per `jq select()` pass. Cap file size at 5MB or pipe through `head -5000` first.
- **Single-pass jq is faster.** Combine multiple extractions into one `jq` invocation where possible instead of looping per-type.
- **Subagent files are small** — no size cap needed. But there can be thousands; limit to recent ones with `-mtime`.
- **`--fix` must not invent.** Only document what was observed with sample values. Never speculate about field semantics beyond what the data shows.
- **Unverified items are not gaps.** If the spec documents something not found in data, mark it "still unverified" — don't remove it.
