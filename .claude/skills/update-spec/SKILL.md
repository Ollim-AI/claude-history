---
name: update-spec
description: Audit SPEC.md against real JSONL history files — spawns parallel specialists to reverse-engineer the format and find gaps. Use when upgrading Claude Code, encountering unknown fields, or doing periodic maintenance.
argument-hint: [--fix]
allowed-tools: Agent, Bash, Read, Edit, Glob, Grep
---

# Update Spec

Audit SPEC.md against real JSONL session files by spawning parallel specialist agents, each analyzing one dimension of the format. Merges findings into a gap report. With `--fix`, edits SPEC.md directly.

## Workflow

### 1. Sample files

Select 4-6 main session JSONL files and 5-10 subagent files. Criteria:
- **Diverse projects** — at least 3 different project directories, because different projects exercise different features (teams, hooks, MCP, background agents)
- **Recent** — last 14 days (`-mtime -14`) to catch format changes
- **Under 5MB** — jq on larger files takes minutes per pass. If all recent files exceed 5MB, use `head -5000` to sample lines from larger ones
- **Include subagent files** — they have different record patterns (no progress filtering, smaller, `isSidechain: true`)

```bash
# Example: pick diverse main files under 5MB
find ~/.claude/projects/ -name "*.jsonl" -maxdepth 2 -mtime -14 2>/dev/null |
  while read f; do echo "$(stat -c %s "$f") $f"; done |
  sort -rn | awk -F/ '!seen[$(NF-1)]++ && $1 < 5000000' | head -6

# Example: recent subagent files
find ~/.claude/projects/ -path "*/subagents/agent-*.jsonl" -mtime -14 2>/dev/null | head -10
```

If fewer than 3 files are found, widen `mtime` to 30 days. If still insufficient, report the limitation and proceed with what's available.

Store the selected paths — all specialists reference the same files.

### 2. Launch parallel specialists

Spawn all 5 as background Agent calls. Each specialist:
- Receives the file list and path to SPEC.md (project root: `SPEC.md`)
- Reads SPEC.md independently to understand what's documented
- Extracts data from the sampled files
- Returns a structured report: gaps found with sample values and priority

**Every specialist prompt must include:** "Research only — do not edit files. Return: item, sample value, priority (high/medium/low), which spec section is affected."

Use `jq` for JSON extraction. Performance note: prefer single-pass `jq` over per-type loops — `jq -r 'if .type == "user" then ... elif .type == "assistant" then ... end'` is faster than separate passes.

#### Specialist 1: Record types & top-level fields

**Find:** All unique record `type` values and their top-level field sets. Compare against spec's documented record types.

**Must detect:**
- Record types present in data but absent from spec
- Top-level fields on known record types not documented in spec
- Record types documented in spec but absent from data (mark "unverified")

**Approach:** Extract `type` values, then for each type extract `keys`. Compare both sets against what SPEC.md documents. Don't hardcode which types are "known" — read the spec to determine that.

#### Specialist 2: Progress & system subtypes

**Find:** All `data.type` values on progress records and `subtype` values on system records. For each, extract the subtype-specific field set and compare against spec.

**Must detect:**
- New subtypes not in spec
- New fields on known subtypes
- Field format changes (e.g., hash length, ID format)
- Subtypes documented but not found (mark "unverified")

#### Specialist 3: Models, versions, service tiers

**Find:** All `message.model` values, model aliases in Agent tool inputs, `version` strings, `service_tier` values. Can scan more files (up to 50) since these extractions are lightweight.

**Must detect:**
- New model identifiers
- Version range changes (spec documents an upper bound)
- New service tier values

#### Specialist 4: File naming patterns

**Find:** All file naming patterns in `{UUID}/subagents/` and `{UUID}/tool-results/` directories. Scan the filesystem, not file contents.

**Must detect:**
- New subagent file prefixes (beyond `agent-`, `agent-acompact-`)
- New hash lengths
- New tool-results naming patterns or file extensions
- New subdirectory types under UUID dirs

#### Specialist 5: Content blocks & special fields

**Find:** All `message.content` block types, their field sets, `stop_reason` values, `caller` field values, and string-vs-array content patterns on user records.

**Must detect:**
- New content block types
- New fields on known block types
- New `stop_reason` values
- New user-record content shapes

### 3. Synthesize

After all specialists return, merge reports:

1. **Deduplicate** — the same gap found by multiple specialists (common for cross-cutting fields like `forkedFrom`). Keep the most detailed description with sample values from each.
2. **Prioritize:**
   - **High**: New record types, new fields on existing types, spec inaccuracies (documented behavior doesn't match data)
   - **Medium**: New file naming patterns, new subtypes, format changes
   - **Low**: Version range updates, unverified-item confirmation, cosmetic issues
3. **Cross-validate** — if a finding appears in only one specialist's report, run a targeted verification query before acting on it. Single-source findings have higher false-positive risk.

**Adequacy check before proceeding:**
- At least 3 different project dirs were sampled
- All 5 specialists returned results (if one timed out, re-run it with smaller files)
- The version range in data is at least as new as the spec's "Last audited" version — if not, the sample is too old

### 4. Apply

**Without `--fix`:** Print the merged gap report as a prioritized markdown table and stop.

**With `--fix`:** Edit SPEC.md for each confirmed gap:

| Gap type | Where to edit |
|----------|---------------|
| New record type | Add subsection under "Additional Record Types" |
| New field on existing type | Add to that type's section, or Common Optional Fields if cross-cutting |
| New subtype | Add to Progress or System section |
| New file naming pattern | Add to File Naming table |
| Version range | Update "Observed client versions" line |
| Spec inaccuracy | Fix the incorrect text in place |

After all edits, update the "Last audited" line at the bottom of SPEC.md with today's date and the new version range.

**Before editing:** If SPEC.md has uncommitted changes, warn and confirm before proceeding — edits on top of uncommitted changes are hard to untangle.

## When to ask

**Ask when:**
- `--fix` and SPEC.md has uncommitted changes
- A gap is ambiguous — could be intentional omission vs. documentation miss
- All sampled files are from the same project (limited coverage)

**Don't ask when:**
- Sample selection, specialist count, or jq approach — proceed with defaults
- Gap is clearly undocumented (new record type, new field with sample values)

## Gotchas

- **jq on large files is slow** — 15MB files take minutes per `select()` pass. Use files under 5MB, or `head -5000` for larger ones, because the audit needs breadth across projects more than depth in one file.
- **Don't hardcode known types in exclusion lists** — read the spec to determine what's documented, then compare. Hardcoded lists become stale the moment the spec is updated.
- **`--fix` must not invent** — only document what was observed with sample values. State what was seen, not what it might mean. Semantics require a second verification pass.
- **Unverified items are not gaps** — if the spec documents something not found in data, don't remove it. Mark "still unverified" with the audit date.
- **Subagent files have different patterns** — they lack progress records, are fully parsed (small), and may contain fields absent from main session files. Always include them in the sample.
