# Operations & Safety
# ~/.claude/rules/operations.md
# Merged from: safety.md + git.md + agents.md

---

## Proposal Gate

Before executing any of the following, always present a plan and wait for explicit approval:
- File writes, renames, or deletions (destructive ops like `rm` always need approval — see § Destructive Operations)
- Git **pushes**, force-pushes, branch deletes, or other shared-state operations (see § Git — commits flow vs pushes pause)
- Deploy commands
- Package installs
- API calls with side effects (including MCP tools with mutation)
- Database mutations (any local or remote DB)
- Anything in a production environment

Format:
```
📋 Proposed: <action>
📁 Scope: <files/dirs affected>
⚠️  Risks: <any detected risks or "none">

Proceed? [yes / no / modify]
```

## Destructive Operations

Flag these with ⚠️ DESTRUCTIVE before proposing:
- `rm -rf`
- Database mutations (DROP, DELETE, TRUNCATE)
- Production deploys
- `git reset --hard`
- `git push --force`
- Database migration rollbacks

## Permission Grants — Always Ask for Delete/Remove

NEVER request a standing-approval permission (entry in `permissions.allow`)
for any tool invocation that deletes or removes something. This includes:
- `Bash(rm ...)` — file deletion
- `Bash(pkill -f ...)` — process termination
- `Bash(kill ...)` — process termination
- `Bash(docker rm ...)`, `Bash(docker volume rm ...)`, `Bash(docker network rm ...)`
- Any MCP tool whose name starts with `delete_` / `remove_` / `forget` / `drop_`

These tools must be invoked one-off each time and prompt the user. Standing
approval defeats the proposal gate — by the time the user notices the perm,
the action has already been silently re-runnable.

Origin: a dev session accumulated several `pkill -f "..."` standing
permissions during dev-cycle restarts (stop server, kill stuck process,
etc.). These were flagged at commit-review time and stripped. The standing
rule: remove delete/remove permissions added by mistake, and always ask for
those instead.

A counter-example: a `Bash(rm -f .../some-cleanup-*.png)` perm was kept once
because it was an intended grant for a one-time cleanup at commit time —
explicit grant by the user, with a fully-qualified path. That's the threshold:
only fully-qualified, single-target delete perms should ever be approved
(and even then, only when the user has explicitly chosen to keep it).

## Environment Awareness

Before any operation, confirm which environment is active:
- Check for `.env`, `.env.<environment>`, `docker-compose.yml`, or env vars indicating dev/stg/prd
- Check whether the database target is a cloud instance or a local container
- Never assume dev — ask if ambiguous

---

## Git

### Commit Format

Use conventional commits scoped to the area of change:

```
[area] verb: what changed (why if non-obvious)
```

Examples:
- `[bot] feat: add Greek language support`
- `[pipeline] fix: handle null entity IDs in RFM segmentation`
- `[infra] chore: update docker-compose healthcheck intervals`

### Multi-Machine Repos

For repos shared across machines, append hostname in brackets:

```
[area] verb: description [hostname]
```

Detect with `$(hostname)`. Examples:
- `[hooks] fix: notification text [$(hostname)]`
- `[brain] auto-sync: session 6 [$(hostname)]`

### Pre-Commit Checks (always run before proposing a commit)

0. **Re-stage if files were edited after `git add`.** When a reviewer fix, formatter auto-write, `/simplify` pass, or any other edit lands AFTER the initial `git add`, run `git add -A` again (or `git add <specific files>` for tighter scope). Then `git status` to confirm the working tree is clean. Skip this step and the commit will silently include only the original snapshot — a real incident dropped ~150 lines of post-review fixes this way.
1. `git diff --stat` — show what changed
2. Scan for: `TODO`, `FIXME`, `HACK`, `TEMP`, `XXX`
3. Scan for: `.env`, `*.key`, `*.pem`, `secret`, `password`, `token` in staged files
4. Check `.gitignore` coverage for sensitive paths

If any risk found → present warning, do not proceed until user acknowledges.

### Commits flow vs pushes pause

**Commits run automatically** as part of normal task flow — no proposal block, no per-commit yes/no. Conventional-commit message format still applies (see § Commit Format above) and the pre-commit checks below are still mandatory (they're quality checks, not approval gates).

**Pushes always pause for explicit approval**, every time, regardless of how recently the user approved the overall intent. Show the proposal in this shape and wait for an explicit "yes" / "push" / "go":

```
📋 Proposed: git push origin <branch>  (<repo>)
📁 Commits to push:
   <sha> <subject>
📐 Diff:  <±lines, scope>
⚠️  Risks: <production-impact / blast radius / "none">

Proceed? [yes / no / hold]
```

Why the asymmetry: commits are local and reversible (`git reset`, `git revert`); pushes touch shared state, are visible to others, and are much harder to walk back.

Destructive local ops (rm, file deletion, force-overwrite) still need explicit approval — that part of § Proposal Gate above is unchanged.

### Pre-Push Checks (always run before proposing a push)

0. **Doc-hygiene gate (do this first).** Before proposing any push, verify docs, READMEs, badges, and diagrams reflect what's being pushed — run the `project-hygiene.md` § Post-Work checklist. The push is the last cheaply-reversible moment; don't push docs that lie. Pay special attention to badges: CI/workflow status badges break silently when a workflow is renamed or removed, and version badges (node, language, DB) drift on stack bumps.
1. `git fetch --quiet origin <branch>` — refresh remote refs
2. `git log HEAD..origin/<branch> --oneline` — list remote commits we don't have locally
3. If non-empty → `git pull --no-rebase -X ours origin <branch>` FIRST, then propose the push
4. If empty → propose the push normally

Rationale: repos with auto-backup hooks (e.g. a hook that pushes every N messages or on `SessionEnd`) can land commits between a local commit and the push from the same machine if another session is also active. Fetching first avoids the "remote contains other work" rejection, keeps the push flow linear, and makes the merge explicit when needed.

### Autostash trap (do NOT use `--autostash` for in-progress feature edits)

`git pull --autostash` silently drops working-tree changes when the merge strategy resolves the same hunk the autostash is trying to re-apply. Observed in practice: `git pull --no-rebase -X ours --autostash` dropped uncommitted settings.json edits; the "Applied autostash" log line printed but the working tree was not re-mutated.

Rule: BEFORE running any `git pull` on a branch with uncommitted edits to feature files, COMMIT them first (or stash + manually re-apply post-merge). `--autostash` is acceptable ONLY for trivial harness dirt (session-log.json artifacts, transient state) where silent loss is recoverable. Never for in-progress code edits.

### Git — Never

- Stage or commit `.env` files
- Amend public commits without explicit instruction
- Force push without explicit instruction
- Run `git clean -fd` without confirmation
- Run `git push` without explicit user approval — always ask first
- Use `git pull --autostash` on uncommitted feature edits (silent-drop trap above)

---

## Agent Dispatch

### Background Agents Are Read-Only

Background subagents (`Agent` tool with `run_in_background: true`) do **not** inherit
Edit/Write/Bash permissions from the main session — and MCP tools are also blocked.

**What they CAN do:**
- Read files, Grep, Glob
- Research and analysis
- Return findings to the main session

**What they CANNOT do:**
- Edit or Write files
- Run Bash commands
- Call MCP tools (memory backends, git-host MCPs, etc.)

**Rule:** Use background agents **only** for research, exploration, and analysis.
Apply all file modifications and MCP calls directly in the main session.

**Evidence:** A session dispatched 5 parallel background agents for a refactor.
All 4 that needed file writes were denied permissions and had to be redone in
the main session. Only the research-only agent succeeded.
