# Project Hygiene
# ~/.claude/rules/project-hygiene.md

## Workflow: Pre → Work → Post

### Pre-Work (before implementation)

Check for project scaffolding:
1. `Makefile` in the project root
2. `README.md` in the project root

If either is missing or clearly stale, **propose** creation/update through the safety.md proposal gate before starting work.

### Post-Work (before committing)

After implementing code and before proposing a commit, run this checklist:

1. **File tree** — if README has a "Project Structure" / worktree section:
   - Verify it matches the actual file layout
   - Add new files/dirs, remove deleted ones
   - If no such section exists and the project has 5+ source files, **create one**
2. **Mermaid diagrams** — if README or `docs/` contain architecture diagrams:
   - Check if the change affects depicted components (new modules, changed data flows, removed services)
   - Update stale diagrams or propose new ones for new subsystems
   - If no diagram exists and the project has 3+ interacting components, **propose one**

   **Phase 0 default:** When scaffolding a NEW project (not just touching an existing one), ship a Mermaid architecture diagram in `README.md` or `docs/architecture.md` BY DEFAULT. Only skip for trivial single-file scripts. Don't wait for the project to grow to "3+ components" — by then it's harder to retrofit accurately and the diagram becomes a chore. The right time to draw the architecture is when you're holding it in your head during scaffolding.
3. **Documentation** — if the change affects any of these, update them:
   - Setup steps (new deps, new env vars, new `make` targets)
   - API surface (new tools, changed endpoints, new CLI flags)
   - Configuration (new config keys, changed defaults)
4. **CLAUDE.md** — if the project has one, verify it reflects new conventions, commands, or architecture introduced by the change
5. **Badges** — if README has status/version badges:
   - CI/workflow badges: confirm the referenced workflow file still exists at that name (badges break silently on workflow rename or removal)
   - Version badges (node, language, DB, license): bump if the stack version changed

Skip steps 1-2 if the change only modifies file contents without structural impact (bug fixes, config value tweaks, test additions).

---

## Phased Plan Tasks

When executing a phased implementation plan (e.g., a `docs/superpowers/plans/*.md` file with numbered tasks and `- [ ]` step-checkboxes), the canonical source of "what's done" is the plan doc itself — NOT the memory summary that wraps around it. Memory files (`project_*.md`) are a derived layer for future-session context; they are not a substitute for updating the source docs.

### Per-task post-commit checklist

After each task's tests go GREEN → commit lands → project memory updates, run these three additional steps in the SAME pass:

1. **Flip plan-doc checkboxes** — in the task's section of the plan, change every `- [ ]` step-level box to `- [x]`. This is the visible signal that a task is done; leaving boxes unchecked while the code is shipped creates a silent lie the next operator has to sort out.
2. **Update spec Status** — if a companion spec doc exists (e.g., `docs/superpowers/specs/...-design.md`) and has a `**Status:**` frontmatter field, refresh it to reflect incremental progress: "In Progress — Phase N at X/Y tasks complete as of YYYY-MM-DD".
3. **Refresh plan-top status header** — if the plan has a top-of-file status block with branch + latest commit, update the commit hash and task count. Keeps the doc's first screen truthful.

Bundle all three into a single commit on the docs repo — usually separate from the code commit, or combined if the plan doc lives in the same repo as the code.

### Bulk catch-up (when the rule was missed for multiple tasks)

Shortcut to flip all unchecked boxes in a contiguous task range (macOS BSD sed):

```bash
sed -i '' '<start_line>,<end_line> { s/^- \[ \]/- [x]/; }' <plan>.md
# Verify count delta:
grep -cE "^- \[ \]" <plan>.md  # before + after to confirm
```

Find the line-range by grepping `^### Task N:` and `^### Task N+K:` (the next task header AFTER your range) — the range is start-of-first-task to line-just-before-next-task.

### Why this exists

Without this rule: ten consecutive tasks once shipped to `main` with full project-memory updates but with ~99 plan-doc boxes untouched and a stale spec Status field. The plan still read "Phase 0 plan to follow" while most of Phase 0 was already shipped. It required a mid-session bulk catch-up.

---

## Makefile

### When to Propose

- Project has runnable code (scripts, services, pipelines, containers)
- Project has tests, linting, or formatting that benefit from one-command shortcuts
- Project uses Docker Compose, deploy scripts, or multi-step build processes

### When to Skip

- Trivial single-file scripts or configs with no build/test/deploy cycle
- Project already uses a task runner (e.g., `just`, `task`, `npm scripts` covering the same ground)
- User has explicitly declined

### Standard Targets

Propose targets that make sense for the project. Common ones:

```makefile
.PHONY: help install test lint format build deploy clean

help:           ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-15s\033[0m %s\n", $$1, $$2}'

install:        ## Install dependencies
test:           ## Run tests (must use pytest per testing.md)
lint:           ## Run linter
format:         ## Auto-format code
build:          ## Build artifacts
deploy:         ## Deploy (with environment check)
clean:          ## Remove generated files
```

### Conventions

- `make test` MUST invoke `pytest` (per `testing.md` — never a custom runner)
- `make deploy` MUST include environment confirmation (per `safety.md`)
- Use `.PHONY` for all non-file targets
- Include `help` as the default target with self-documenting `## ` comments
- Use variables at the top for paths, versions, image names — no hardcoded values
- Keep it simple — only add targets the project actually needs

## README.md

### When to Propose

- README is missing entirely
- README exists but is a bare placeholder (empty or just a title)
- Significant project changes have made the README outdated (new services, changed setup steps, new env vars)

### When to Skip

- README is accurate and covers current state
- User has explicitly declined
- It's a throwaway/scratch directory

### Standard Sections

Propose sections relevant to the project:

```markdown
# Project Name

One-line description.

## Quick Start

How to get running (install, configure, run).

## Usage

Key commands, API endpoints, or workflows.

## Development

How to test, lint, format (`make` targets if Makefile exists).

## Architecture

Brief overview if non-trivial (layers, services, data flow).
Include a Mermaid diagram if 3+ components interact.

## Project Structure

File tree showing key directories and files with brief descriptions.

## Configuration

Environment variables, config files, secrets setup.
```

### Conventions

- Keep it concise — ship over perfection
- Reference `make help` if a Makefile exists (avoid duplicating target docs)
- Do not document `.env` values — reference `.env.example` instead
- Update README when proposing changes that affect setup, usage, or architecture
