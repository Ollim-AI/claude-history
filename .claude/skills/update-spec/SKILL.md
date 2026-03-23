---
name: update-spec
description: Audit SPEC.md against real JSONL history files — spawns parallel specialists to reverse-engineer the format and find gaps. Use when upgrading Claude Code, encountering unknown fields, or doing periodic maintenance.
argument-hint: [--fix]
allowed-tools: Agent, Bash, Read, Edit, Glob, Grep
---

# Update Spec

Audit SPEC.md against real JSONL session files. Spawns parallel specialists to find format changes, then filters findings by relevance to claude-history before applying.

**Purpose:** SPEC.md exists to support claude-history development — it documents the JSONL format so the parser, chain walker, renderer, and file discovery code can be built and maintained correctly. It is not a comprehensive field catalog. Every section should help someone understand how to parse, traverse, render, or locate session data.

## Relevance filter

Every finding must pass this filter before being added to the spec:

1. **Does claude-history's code read or depend on this?** Grep `src/claude_history/` for the field name, record type, or pattern. If the code uses it, document it.
2. **Could this break parsing or chain traversal if misunderstood?** New record types that appear in the chain, new content block types that affect rendering, new file patterns that affect discovery — these matter even if the code doesn't handle them yet, because they represent parser gaps.
3. **Is this just metadata the parser passes through?** Fields like `inference_geo`, `iterations`, `speed` that no parser logic depends on — omit or mention once in a "Known but unused" note, not in detailed sections.

When `--fix` is used, also scan existing spec sections for content that fails this filter and flag it for pruning (don't auto-remove — flag for review).

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

**Every specialist prompt must include:** "Research only — do not edit files. For each finding, return: item, sample value, priority (high/medium/low), which spec section is affected, and **parser impact** — grep `src/claude_history/` for the code that handles this field/type/pattern and note how the code would behave given the new data. If the code would produce wrong output, silently skip the data, or fail, say so with the function name and line."

Use `jq` for JSON extraction. Performance note: prefer single-pass `jq` over per-type loops — `jq -r 'if .type == "user" then ... elif .type == "assistant" then ... end'` is faster than separate passes.

#### Specialist 1: Record types & top-level fields

**Find:** All unique record `type` values and their top-level field sets. Compare against spec's documented record types.

**Must detect:**
- Record types present in data but absent from spec
- Top-level fields on known record types not documented in spec
- Record types documented in spec but absent from data (mark "unverified")

**Approach:** Extract `type` values, then for each type extract `keys`. Compare both sets against what SPEC.md documents. Don't hardcode which types are "known" — read the spec to determine that.

**Parser impact check:** For new record types, check whether `chain.py` would encounter them during chain traversal (do they have `uuid`/`parentUuid`?) and whether `render.py` would attempt to render them. Note if a new type would be silently skipped or cause a KeyError.

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

**Parser impact check:** For each new file naming pattern, trace how it flows through the discovery and parsing code. Specifically:
- `io.py`: `find_subagent_file` uses `path.stem.replace('agent-', '')` to extract agent IDs — would a new prefix produce the correct stem?
- `agents.py`: `search_subagent_files` globs `agent-*.jsonl` — would new patterns be found or missed?
- Report any case where a new naming pattern would cause incorrect ID extraction, missed file discovery, or parsing failure.

#### Specialist 5: Content blocks & special fields

**Find:** All `message.content` block types, their field sets, `stop_reason` values, `caller` field values, and string-vs-array content patterns on user records.

**Must detect:**
- New content block types
- New fields on known block types
- New `stop_reason` values
- New user-record content shapes

**Parser impact check:** For new content block types, check `render.py:render_blocks` — does it iterate content blocks with type-specific handling (text, thinking, tool_use, tool_result)? A new block type that isn't handled would be silently skipped. For new user-record content shapes, check `chain.py:is_user_typed_prompt` to see if the detection logic would misclassify them.

### 3. Filter and synthesize

After all specialists return:

1. **Deduplicate** — the same gap found by multiple specialists. Keep the most detailed description.
2. **Apply the relevance filter** to each finding:
   - Grep `src/claude_history/` for the field/type/pattern name
   - Classify: parser-relevant (document in detail), chain/render-relevant (document), or metadata-only (omit or note briefly)
   - Drop findings that are pure metadata with no parsing impact
3. **Prioritize** remaining findings:
   - **High**: New record types in the chain, spec inaccuracies affecting parsing, new content shapes that break rendering
   - **Medium**: New file naming patterns, new subtypes the code should handle
   - **Low**: Version range updates, new models, unverified-item confirmation
4. **Cross-validate** — if a finding appears in only one specialist's report, verify with a targeted query.
5. **Parser impact sweep** — for each remaining finding, confirm the parser impact was assessed. If a specialist reported a new pattern without tracing it through the code, do it now:
   - File naming findings → check `io.py:find_subagent_file` and `agents.py:search_subagent_files`
   - New record types → check `chain.py` traversal and `render.py` display logic
   - New content block types → check `render.py:render_blocks` iteration
   - New user-record content shapes → check `chain.py:is_user_typed_prompt`
   If the code would silently skip, misparse, or fail on the new pattern, that's a high-priority finding even if the specialist rated it lower.

**Adequacy check before proceeding:**
- At least 3 different project dirs were sampled
- All 5 specialists returned results (if one timed out, re-run with smaller files)
- The version range in data is at least as new as the spec's "Last audited" version

### 4. Apply

**Without `--fix`:** Print the filtered gap report as a prioritized markdown table and stop.

**With `--fix`:** Edit SPEC.md for each relevant, confirmed gap:

| Gap type | Where to edit |
|----------|---------------|
| New record type (parser-relevant) | Add subsection under "Additional Record Types" |
| New field affecting parsing/chain/render | Add to that type's section, or Common Optional Fields if cross-cutting |
| New subtype the code handles | Add to Progress or System section |
| New file naming pattern | Add to File Naming table |
| Version range | Update "Observed client versions" line |
| Spec inaccuracy | Fix the incorrect text in place |
| Metadata-only field | Mention once in the relevant record type section as "also present but not used by parsers", or omit |

Also flag existing spec sections that document fields no code path uses — these are pruning candidates. Don't auto-remove; list them at the end of the report for manual review.

After all edits, update the "Last audited" line at the bottom of SPEC.md with today's date and the new version range.

**Before editing:** If SPEC.md has uncommitted changes, warn and confirm before proceeding.

## When to ask

**Ask when:**
- `--fix` and SPEC.md has uncommitted changes
- A gap is ambiguous — could be intentional omission vs. documentation miss
- All sampled files are from the same project (limited coverage)
- A finding is borderline relevant — parser doesn't use it now but might need to

**Don't ask when:**
- Sample selection, specialist count, or jq approach — proceed with defaults
- Gap is clearly parser-relevant (new record type in chain, new content block type)
- Finding clearly fails the relevance filter (pure metadata)

## Gotchas

- **jq on large files is slow** — 15MB files take minutes per `select()` pass. Use files under 5MB, or `head -5000` for larger ones, because the audit needs breadth across projects more than depth in one file.
- **Don't hardcode known types in exclusion lists** — read the spec to determine what's documented, then compare. Hardcoded lists become stale the moment the spec is updated.
- **`--fix` must not invent** — only document what was observed with sample values. State what was seen, not what it might mean.
- **Unverified items are not gaps** — if the spec documents something not found in data, don't remove it. Mark "still unverified" with the audit date.
- **Subagent files have different patterns** — they lack progress records, are fully parsed (small), and may contain fields absent from main session files. Always include them in the sample.
- **The spec serves the parser, not completeness** — a field that exists in every record but no code path reads is less important than a rare field that the chain walker must handle correctly. Prioritize accordingly.
