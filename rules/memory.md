# Memory Auto-Capture Rules
# ~/.claude/rules/memory.md

## Memory Naming — ≤5 words (HARD RULE)

**Every memory `name` field MUST be ≤5 whitespace-separated words.** Applies to ALL memory types (feedback, project, user, reference) and ALL entry points (create, update, wrap-up, in-session auto-capture).

A memory backend typically slugs the `name` to derive the markdown filename (`<type>_<slug>.md`). Long names produce ugly truncated kebab filenames that pollute the index and the project memory dir.

### Examples

| ✅ Good (≤5 words) | ❌ Bad (6+ words) |
|---|---|
| `MCP remember owns file sync` | `MCP remember owns file sync — don't manually Write` |
| `Git API token scopes` | `Git API token scopes per account verified` |
| `Confirm before git push` | `Always ask before git push, even when a prior message authorized it` |
| `Flip plan boxes per task` | `When shipping a phased implementation plan, flip the per-task plan-doc checkboxes` |

If the topic genuinely needs more nuance, put it in the `description:` frontmatter field (one line, no length cap, used for the index hover). The `name` is the slug source; `description` is the human label.

### Enforcement

A PreToolUse hook can guard the create call: if `name` has >5 tokens, block it with stderr advice — shorten the name and retry. The hook need not fire on `update` (the slug is locked at create time; `update` keys on an ID and doesn't rename the file).

### Why this is a top-level rule

Kept slipping into sessions as a parenthetical, producing entries with long truncated kebab filenames. Promoting it to a top-level rule plus structural enforcement (the PreToolUse hook above) stops the drift at the source.

---

## In-Session Feedback Capture

When the user corrects your approach or gives guidance that would apply to future sessions,
**save it immediately as a `type: feedback` memory** — do not wait for wrap-up.

### Trigger Patterns

Auto-save feedback when the user says things like:
- "no, don't do that" / "not that, instead..."
- "always use X" / "never use Y"
- "stop doing Z" / "that's not how we..."
- "lets not..." / "prefer X over Y"
- "we got burned by..." / "last time that broke..."

> This section assumes a memory MCP / backend that owns markdown file-sync (an optional integration — wire one up only if you use it). Tool names below are illustrative examples, not a requirement.

### How to Auto-Save

1. **Call your memory backend's create tool** (e.g. a `remember` call), or its update tool if the memory already exists — check first with a recall/search. A well-behaved backend handles the markdown write automatically:
   - Writes `~/.claude/projects/<encoded-cwd>/memory/<type>_<slug>.md`
   - Auto-updates the `MEMORY.md` index
   - `<slug>` is derived from the memory `name` — see § Memory Naming above for the ≤5-word rule.
2. **Append a dedup record** to `~/.claude/session/feedback_captured.json` (create the file if missing). Format:
   ```json
   {"saved_at": "<iso ts>", "filename": "feedback_<slug>.md", "trigger": "<one-line summary of what user said>"}
   ```
   Wrap-up reads this file and skips memories that were already saved during the session, preventing the "propose to save the same insight twice" double-write.
3. Briefly confirm: "Saved that as a feedback memory."

**Do NOT manually `Write` the memory file in addition to calling the backend** — that creates a duplicate file (your chosen name + the backend's slug of `name`), plus two entries in the index. The backend owns the file-sync. Only touch memory files manually for cleanup (deleting orphans, fixing dedup).

### What NOT to Auto-Save

- One-off task instructions ("put the file here") — ephemeral
- Code corrections that are visible in the diff — derivable from code
- Preferences already captured in rules/ or CLAUDE.md — would be duplicate

### Quality Bar

Only auto-save if the correction is:
- **Generalizable** — applies beyond this single instance
- **Non-obvious** — not something you'd derive from reading the codebase
- **Actionable** — changes how you should behave in future sessions

## Wrap-Up Memory

Wrap-up captures broader insights (user profile, project context, references).
The auto-capture rule above handles the highest-value items (feedback) in real-time,
so they're never lost even if wrap-up doesn't run.

## Wrap-Up Dual-Write

> Applies only if you wire up a memory MCP / backend (optional). If you have no
> backend, the markdown files on disk are the source of truth and this section
> is a no-op.

When a memory MCP / backend is in use, the dual-write rule in "How to Auto-Save"
above applies **identically** to memories created or modified during wrap-up —
not just to in-session feedback auto-capture. Every wrap-up memory that lands on
disk MUST also hit the backend in the same pass:

- **New memory file** → call the backend's create tool with the memory's
  `name`, `type`, `observations` (file body), `tags`, `importance`, and
  `source` (the project CWD).
- **Updated memory file** → call the backend's update tool with the
  existing entity's ID (from frontmatter) and the new `observations`.
  If the file has no ID, treat it as new and create it.
- **Deleted memory file** → call the backend's delete tool with the ID.

**Why:** the filesystem is a dual-write mirror, not the source of truth. A
backup hook captures filesystem state; it does NOT replay missed backend
calls. If you only write the file, the memory will never appear in a recall
on any machine — which is the whole point of a memory backend. Missing this
step is a common drift source.

**Self-audit gate (mandatory before exiting wrap-up):** after all writes,
recall each new memory by name and confirm it's in the backend. If any are
missing, loop back to dual-write before proceeding. This takes ~one extra
call per memory — cheap insurance against silent drift.
