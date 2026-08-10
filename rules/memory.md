# Memory Auto-Capture Rules
# ~/.claude/rules/memory.md

## Memory Naming — concise, not capped

Memory `name` should be short enough to scan in the index — aim for **under ~8 words** — but there is **no hard limit**. Prefer a name that reads as the claim itself, and put the nuance in the `description:` frontmatter field, which has no length cap and is what the index renders after the `—`.

A memory backend slugs the `name` to derive the markdown filename (`<type>_<slug>.md`). The reference backend caps that slug at **120 characters**, which no reasonable name reaches.

### Examples

| ✅ Good | ❌ Avoid |
|---|---|
| `MCP remember owns file sync` | `sync` (too vague to match on) |
| `Git API token scopes` | `stuff about tokens` |
| `Confirm before git push` | `Always ask before git push, even when a prior message authorized it and the branch is clean` (that belongs in `description:`) |

The failure mode worth avoiding is a name too vague to recall on — not a name that is a few words long.

### Enforcement

`hooks/memory_name_guard.py` ships as an **optional** convention hook, wired in `settings.example.json`. It blocks names over `MAX_WORDS` (**8**, matching the guidance above) at create time with stderr advice. Treat it as a style preference, not a correctness gate: raise `MAX_WORDS`, or drop the hook from your `settings.json`, if you would rather write longer, more descriptive names.

If you do keep it, note that it fires on create only. That is a deliberate scope choice, not a statement about renames — the reference backend **does** re-slug the filename when `update` changes the name, deleting the old-slug file so one entity never leaves two files behind.

### Why this is a top-level rule

Naming kept slipping into sessions as a parenthetical, producing entries nobody could find again. What matters is that the name states the claim; length is secondary.

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
   - `<slug>` is derived from the memory `name` — see § Memory Naming above. The slug caps at 120 chars; there is no word limit.
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
