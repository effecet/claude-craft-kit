# MCP Tooling Integration
# ~/.claude/rules/tooling.md

## Philosophy

It's easy to install MCP servers and then under-use them. This rule wires three of
them — `python-enhancer`, `context7`, and `memory-persistor` — into the normal dev
loop so they pull their weight instead of sitting idle.

The memory MCP referenced throughout is
[`memory-persistor`](https://github.com/effecet/memory-persistor): an optional,
PostgreSQL-backed memory server with thermal decay and a knowledge graph. Wire it up
if you want persistent cross-session memory; the rest of this rule assumes it (or an
equivalent memory MCP) is available.

---

## Deferred MCP tool schemas — pre-load before first call

The Claude harness lists deferred MCP tools by name in the `<system-reminder>` block
("deferred tools now available via ToolSearch") but does NOT load their JSONSchema.
Calling a deferred tool directly fails with `InputValidationError` or confusing
"received string" type-coercion errors — even when parameters are correct. Pre-load
schemas via `ToolSearch` before the first call.

**Pre-load eagerly at session start** for the deferred tools you'll reach for:

- **memory-persistor** — `remember`, `recall`, `update`, `relate`, `traverse`, `forget`, `conflicts`, `health`
- **python-enhancer** — `python_lint`, `python_typecheck`, `python_security`, `python_format`, `python_complexity`, `python_deps_audit`
- **harness tools** — `TaskCreate`, `TaskUpdate`, `EnterPlanMode`, `WebFetch`, `WebSearch`

Batch comma-separated in a single call — the schemas persist for the rest of the session:

```
ToolSearch(query="select:mcp__memory-persistor__remember,mcp__memory-persistor__recall", max_results=10)
```

**Skip when:** you've already pre-loaded that tool this session, OR you're certain you won't call it (don't speculate — pre-loading speculatively wastes a tool call).

**Why this exists:** the first-call failure is misleading — the harness returns a generic type error, sending you into a parameter-debugging rabbit hole on what is actually a "schema not loaded yet" problem. Pre-loading is one cheap call that prevents the whole failure mode.

---

## Python tooling (python-enhancer MCP)

### MANDATORY on post-step validation gate — Full tier

When the post-step validation gate runs on a diff that touches any `*.py` file, you MUST also invoke `python-enhancer` MCP tools on the changed Python files, in parallel with the code-review agents:

| Tool | Severity | Action if findings |
|------|----------|-------------------|
| `mcp__python-enhancer__python_lint` | Same as reviewer agents | Block, fix, re-run |
| `mcp__python-enhancer__python_typecheck` | Same as reviewer agents | Block, fix, re-run |
| `mcp__python-enhancer__python_security` | CRITICAL/HIGH block | Block, fix, re-run |
| `mcp__python-enhancer__python_deps_audit` | HIGH block | Warn on medium, block on CVE |

**How to invoke:** Pass the list of changed `.py` file paths. Run all four in parallel in a single tool call block. Treat findings with the same tier rules as the code-review agents — included in the validation report, not a separate step.

**Skip conditions (explicit):**
- Light tier gate — skip `deps_audit` and `security`; keep `lint` + `typecheck`
- Skip tier (config/docs only) — skip all four

**Hook-code ignore list (for hook scripts under `~/.claude/hooks/**`):** When invoking `python_lint` on local hook code, pass `ignore=S603,S607,S110,PTH123,PTH109,E741`. These are documented false positives for private-harness hook code:
- `S603` / `S607` — subprocess calls with list args (never shell=True), partial executable paths on system tools (`git`, `make`, `python3`). Not an attack surface; flagged ~30 times per hook file.
- `S110` — `try/except/pass` on best-effort paths (desktop notifications, git sync, logging). Swallowing failures is intentional, not a bug.
- `PTH123` / `PTH109` — `open()` → `Path.open()`, `os.getcwd()` → `Path.cwd()` modernization nits. Acceptable in hook scripts.
- `E741` — ambiguous variable name `l` in short loop comprehensions. Style nit, not an error risk at hook scope.

The ignore list applies ONLY to hook code (`~/.claude/hooks/`). Application code, data-engineering pipelines, and anything non-hook gets the full ruleset with no ignores. Rationale: hook lint runs return 25-50 issues per file, all from this list — cutting them at the source preserves signal for real issues.

### `python_complexity` as a soft gate

On post-step gate (any tier), invoke `mcp__python-enhancer__python_complexity` on changed Python files. **Warn** (don't block) on any function with cyclomatic complexity > 10. Surface the warning in the validation report under a `## Complexity warnings` section.

### `python_format` before commit

Before proposing a commit that includes Python changes, invoke `mcp__python-enhancer__python_format` on the staged `.py` files. If the formatter produces changes, re-stage them and mention the reformat in the commit proposal:

```
📋 Proposed commit: [area] verb: description
   (python_format reformatted N file(s): <list>)
```

This happens BEFORE the commit proposal, as part of `rules/operations.md` pre-commit checks.

---

## Library docs (Context7 MCP)

### MANDATORY preemptive fetch on first library touch

When you are about to write code that imports, configures, or calls a library you haven't already looked up in the current session, you MUST first call:

1. `mcp__context7__resolve-library-id` with the library name
2. `mcp__context7__query-docs` with the resolved ID + a topic hint matching what you're about to do

**Applies to:** any library, framework, SDK, API, CLI tool, or cloud service — even well-known ones (React, Next.js, Prisma, Django, Spring Boot). Your training data may not reflect recent API changes.

**Skip only when:**
- The library is part of stdlib (Python/Node built-ins, bash)
- You've already queried Context7 for that library in the current session
- The code is a one-line obvious call (e.g., `print("hello")`)
- The user explicitly says "skip docs"

**Why:** Avoids hallucinated API signatures. Cheaper than a debugging loop triggered by an outdated method name.

**Track what you've looked up:** Keep a mental list of libraries queried this session. If unsure whether you already queried it, query again — it's cheap.

---

## Memory graph (memory-persistor MCP)

The [`memory-persistor`](https://github.com/effecet/memory-persistor) MCP exposes
`recall` / `remember` / `update` / `relate` / `traverse` (plus `forget`, `conflicts`,
`health`). The patterns below assume a `proactive_recall` hook surfaces relevant
memories on each user message — adjust to your own setup.

### Use `recall` / `remember` in the normal loop

- `recall` at the start of work on a topic to pull prior context.
- `remember` when you learn something generalizable that should survive the session (a correction, a convention, a gotcha).

### Use `traverse` when a memory surfaces and you're about to act on its topic

A keyword-match recall is good for hits, weak for breadth. When a surfaced memory is about a topic you're about to work on, call:

```
mcp__memory-persistor__traverse(entity_id=<id>, hops=1, limit=5)
```

This returns 1-hop related memories via the knowledge graph — catches connected context the flat search misses.

**When to invoke:**
- A surfaced memory is directly relevant to the next action
- User asks a broad question that a single memory only partially answers
- You're about to modify a system (hook, skill, rule) and need to know what else touches it

**When NOT to invoke:**
- Memory is tangentially mentioned but you won't act on its topic
- Already called traverse on the same entity this session
- Session is focused on a single concrete task with no ambiguity

### Use `relate` when creating new memories with known connections

When writing a new memory that explicitly references another entity (by name or topic), call `mcp__memory-persistor__relate` after `remember` to create the graph edge. This builds the graph over time instead of leaving new entries orphaned.

---

## Never

- Don't call `python-enhancer` tools on non-Python files (wastes tokens, returns errors).
- Don't call Context7 for internal project modules — only for third-party libraries.
- Don't call `memory-persistor` `forget` or `update` without explicit user approval.
- Don't run `python_format` on generated files (`*_pb2.py`, Jupyter-exported `.py`, vendored deps).
