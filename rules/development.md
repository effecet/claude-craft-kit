# Development Conventions
# ~/.claude/rules/development.md
# Merged from: testing.md + validation.md

---

## Testing

### Framework

- pytest only — no unittest classes
- Run with: `pytest` (no default flags imposed — add per-project as needed)
- Target: **meaningful tests on business logic** (data pipelines, transformations, scoring, APIs). Coverage % is tracked as a signal, not enforced — infrastructure code (hooks, glue scripts, configs) is exempt from numeric coverage targets because the cost-to-value ratio is wrong there. The test-coverage-reviewer agent verifies test *quality* (do tests cover edge cases? are they meaningful?) — not a fixed percentage.

### Structure

```
tests/
  conftest.py          # shared fixtures (DB connections, sample data, temp dirs)
  test_<module>.py     # mirrors src/ structure
  fixtures/            # static fixture files (sample .parquet, .csv, .json)
```

- Shared fixtures go in `conftest.py` — DuckDB connections, sample DataFrames, Faker-seeded data
- One-off setup stays inline in the test file
- Nest `conftest.py` in subdirectories when fixtures only apply to that subdir

### Mocking Philosophy

Case-by-case. Use this decision ladder:

1. **Real thing** — default. Use real DB (DuckDB in-memory), real files (tmp_path), real DataFrames
2. **Fake/stub** — when the real thing is slow, flaky, or has side effects (external APIs, email, cloud storage)
3. **Mock (patch)** — last resort. Only for: third-party API calls, time-dependent logic, error injection

Never mock what you own — if you need to mock your own code, it's a design smell.

### Data Pipeline Tests

- Small fixture data: 5–20 rows, covering edge cases (nulls, duplicates, boundary values)
- Synthetic data via Faker: use `SEED` constant for reproducibility
- Fixture parquets in `tests/fixtures/` for complex schemas
- Assert on shape, dtypes, key columns, and row counts — not exact values unless testing specific logic
- Test each Medallion layer independently: bronze (ingestion), silver (dedup/clean), gold (features)

### Fixtures

```python
# tests/conftest.py
import duckdb
import pytest

@pytest.fixture
def con():
    """In-memory DuckDB connection with sensible defaults."""
    c = duckdb.connect(":memory:")
    c.execute("SET threads = 1")
    c.execute("SET memory_limit = '256MB'")
    yield c
    c.close()
```

### Naming

- Test files: `test_<module>.py`
- Test functions: `test_<what>_<condition>` (e.g., `test_dedup_handles_null_keys`)
- Fixtures: descriptive nouns (`sample_orders`, `con`, `tmp_lakehouse`)

### Testing — Never

- `from unittest.mock import *`
- Fixtures that hit the network without explicit marks (`@pytest.mark.integration`)
- Tests that depend on execution order
- Ignoring warnings — fix the root cause or explicitly filter with `pytest.warns`
- Empty test files or placeholder `pass` tests

---

## Validation Gate

### Environment Health Check (Session Start)

During the memory loading phase, verify all validation dependencies exist on this machine.

| Category | Items | Auto-fixable? |
|---|---|---|
| Reviewer agents | `code-review:code-reviewer`, `code-review:bug-hunter`, `code-review:security-auditor`, `code-review:contracts-reviewer`, `code-review:historical-context-reviewer`, `code-review:test-coverage-reviewer` | Yes (plugin install) |
| Skills | `brainstorming`, `writing-plans`, `wrap-up`, any skill referenced by `rules/` | Yes (plugin install) |
| CLI tools | `gitleaks`, `terminal-notifier`, `python3`, `docker`, `git` | No — provide install commands |
| MCP servers | your memory backend, git-host MCP, `context7` (all optional) | Partial — check config, flag missing containers |

**Warn and offer.** Session continues regardless. Re-surface if user starts a plan with missing deps.

### Development Workflow (Enforced Sequence)

When executing any task that produces code (new features, bug fixes, refactors), follow this sequence. Exempt: pure research, questions, documentation-only edits, config value tweaks.

**Run `validate_tier.py` FIRST and let the tier pick the sequence.** The tier is not only an agent count — it decides how much process the change gets. Picking the heavy path for a small change is the most common way this workflow wastes a session, and it has happened repeatedly.

**Skip / Light tier — one pass, no documents, no skill invocation:**

```
implement → tests pass (TDD) → mutation-check the new assertions → ONE GATE → commit
```

No brainstorming skill, no spec, no plan, no design message. State in one or two sentences what you are about to change and why, then do it. If the change turns out to touch a sensitive path or grow past the Light threshold, re-run the script — it will say Full, and you step up mid-task.

**Full tier — the full sequence:**

```
1. Brainstorm / Clarifying Questions
2. Write Spec (design doc)
3. Spec review & user approval
4. Write Implementation Plan
5. Execute Plan
   └─ Per step:
        implement → tests pass → POST-STEP GATE → next step
6. PRE-COMMIT GATE
7. Commit
```

Steps 1-4 cannot be skipped **at Full tier**. If the user says "just build it" on Full-tier work, a lightweight spec + plan is still required.

**Exempt from all of it:** pure research, questions, documentation-only edits, config value tweaks.

⚠️ **Reaching for the heavy path on a small change is a failure mode, not caution.** A ten-line fix that gets the brainstorming skill, a design-approval round trip and a six-agent review is the typical shape: the sizing table below already says not to, and it gets ignored because heavy feels safe. It is not safe, it is slow, and the user notices before you do.

### Review Lanes

Code review flows through two distinct lanes, each covering a different slice of the diff lifecycle. No double-coverage — if Lane 1 already reviewed a concern, Lane 2 doesn't revisit it.

| Lane | Tool | Where | When | Blocking? |
|---|---|---|---|---|
| **1 — Simplify** | `/simplify` skill | Local, per-step | After tests pass, BEFORE the post-step gate | Mandatory on Light/Full; skipped on Skip |
| **2 — Gate** | 6 reviewer agents + python-enhancer | Local | Post-step + pre-commit | Hard block on any finding |

**Lane 1 — Simplify.** Cheap local self-review for reuse, quality, and efficiency. Fixes in place. Scope is the current step's diff. If `/simplify` edits files, re-run the step's tests before proceeding to Lane 2 — if tests newly fail, fix and re-run Lane 1. Rationale: the gate (Lane 2) is expensive; running `/simplify` first means the 6 agents see a cleaner diff and flag less. Leverage compounds across plan steps.

**Lane 2 — Gate.** Deep analysis via the tiered validation gate (see "Tiered Validation Gate" below). Lane 2 is unchanged by this rule — it is the existing post-step and pre-commit gate, driven by `validate_tier.py`. Lane 1 is a precondition, not a replacement.

**Why no third lane.** An external PR-level AI reviewer (e.g. CodeRabbit) was considered but dropped: it adds a paid dependency, may not integrate with your git host, and Lane 2's 6-agent gate already provides 6 distinct specialized perspectives on the same diff. Its only unique value — a non-Claude model perspective — is a small delta given Lane 2's breadth. If a future external reviewer integrates cleanly with your host AND has a wide free tier, reconsider adding it as an optional Lane 3 — but do not block on that.

**Graceful degradation.** If the `/simplify` skill is unavailable (plugin not installed, transient failure), log the skip to the validation report and proceed to Lane 2. Never block the gate on missing tooling — this matches the "verify CLI tools before shell-out" pattern.

### Scale to Complexity (MANDATORY sizing rule)

**The size of the process must match the size of the work.** At Full tier the brainstorming → spec → plan flow is non-negotiable. Below it, the artifacts are not miniature — they are absent, and the discipline lives in the tests instead.

| Work size | Brainstorm | Spec | Plan |
|---|---|---|---|
| **Trivial** (1-line config tweak, typo, single-default change) | none — say what you are changing in one sentence | none | none |
| **Small** (≤30 LOC effective, no sensitive paths) | none — one sentence of intent, then implement | none | none |
| **Medium** (one feature, multiple files, some design choices) | Full Q&A loop, propose 2 approaches | ~300-500 words, sectioned (architecture, components, error handling) | Numbered steps with per-step exit criteria |
| **Large** (subsystem, sensitive paths, schema changes) | Full Q&A loop, propose 2-3 approaches with tradeoffs, visual companion if mockups apply | Full sectioned design, decomposed if needed | Phased plan with checkpoints between phases |

**Picking the size:** use `validate_tier.py` output as the signal — `Skip` ≈ trivial, `Light` ≈ small, `Full` ≈ medium or large (judge by whether sensitive paths or new subsystems are involved). The size is a default; the user can always say "this needs a real brainstorm" to escalate, or "tiny change" to drop a tier.

**Anti-pattern to avoid:** padding a trivial change to look medium because the skill template "expects" sections. A 3-sentence spec is a complete spec when the work is 3-sentences worth. Inflating it wastes attention and makes the user trust the process less.

**Anti-pattern in the other direction:** compressing a medium change into a "small" template to save tokens. The size is determined by the work, not by the desire to skip steps. If you find yourself wanting to drop a tier to move faster, that's a signal you're under-sizing — pick the higher tier.

### Artifact format by tier

Sizing rules (above) govern word count. This rule governs artifact shape — they are orthogonal.

**Trivial and small tier: NO artifact.** No spec file, no plan file, no combined doc, and no `brainstorming` / `writing-plans` skill invocation. State the intent in a sentence, write the test, make it pass, mutation-check it, commit. The commit message is the record — it is durable, it is read in `git log`, and unlike a `docs/superpowers/specs/` file for a ten-line change, somebody will actually see it.

This replaces the earlier "write ONE combined artifact" rule, which was itself a compromise and still cost a file, a commit and a review round-trip on changes that did not earn one.

**Medium and large tier:** write separate `spec.md` (via brainstorming skill) and `plan.md` (via writing-plans skill) as today. The combined template is NOT used for medium/large — their size justifies two artifacts.

**Upgrade path:** if trivial/small work grows past the Light threshold or touches a sensitive path mid-flight, re-run `validate_tier.py`, say out loud that the tier changed, and start the Full sequence from step 1 — there is no artifact to carry forward, which is the point: nothing was written that now has to be reconciled. The ratchet is one-way; nothing downgrades mid-task.

Do NOT retroactively compress an approved Full-tier spec+plan into a smaller form — that would discard approved content.

### Tiered Validation Gate

Three tiers based on change size and risk:

| Tier | Criteria | Agents | When |
|------|----------|--------|------|
| **Skip** | Docs-only (`.md`/`.rst`/`.adoc`, whatever the name — except instruction files, below), config-only with no sensitive path (`config/secrets.yaml` goes Full), OR zero effective LOC (only comments, blank lines, prose or copied migration lines) with no sensitive paths and no light-floor file | None | Infra changes, documentation, comment rewording |
| **Light** | <30 effective LOC AND no sensitive paths AND no plan steps — or a build-tooling-only diff (`Makefile`, `justfile`, `Taskfile`, `*.mk`) of any size — and the floor for any diff touching executable config or instruction files | code-reviewer, bug-hunter (2) | Bug fixes, tweaks, small features, build fixes, rule edits |
| **Full** | ≥30 effective LOC OR plan steps OR sensitive paths OR a CI workflow line touching `permissions:` / `${{ secrets.* }}` OR explicit request | All 6 in parallel | Major features, refactors, security |

**Effective LOC** is what the script tiers on: changed lines minus blank and comment-only lines (added or deleted), prose in `.md`/`.rst`/`.adoc` (added or deleted), lockfile lines (added or deleted) and migration lines copied from a sibling migration. Deleted code always counts in full. The script prints what it did not count, e.g. `LOC: 12 (340 raw; not counted: 77 comment/blank, 251 prose)`. Build files cap at Light rather than Skip because a `make` run does not catch a secret in argv or a dropped `set -e`.

**Light floor — never Skip.** Executable config (CI workflows incl. woodpecker and composite `action.yml`, `.gitlab-ci.yml`, `settings.json`, `hooks/*.json`, `package.json`, `composer.json`, `pyproject.toml`, MCP server JSON, compose files, `.pre-commit-config.yaml`, `.gitleaks.toml`, `requirements*.txt`) and instruction prose (`CLAUDE.md`, `AGENTS.md`, `SKILL.md`, and `.md` under `rules/`, `agents/`, `commands/`, `skills/` — at this kit's root or under `.claude/`) match a skip extension but run or instruct: removing the push gate from `rules/operations.md` used to get 0 reviewers. Unlike build files they are a floor, not a cap — alone or beside code (a Makefile beside them does not cap them), ≥30 effective LOC still goes Full. Instruction prose counts 0 effective LOC, so for it Light is also the ceiling. A CI workflow line touching `permissions:` or a write scope, any `secrets` reference (`secrets.X`, `secrets: inherit`, `toJSON(secrets)`), `github.token`, `id-token` or `pull_request_target` goes Full. **Lockfiles** (`package-lock.json`, `pnpm-lock.yaml`, and named `uv/yarn/poetry/Cargo/Gemfile/composer/Pipfile/bun/flake.lock`) are never Skip but are capped at Light like build files, and their lines are not effective LOC (reported as `lockfile` in the breakdown). Code under `docs/` or behind a `README` prefix (`docs/run.sh`, `README_install.sh`) is no longer skipped by path. `validate_tier.py`'s `EXEC_CONFIG_PATTERNS` / `INSTRUCTION_PATTERNS` are the source of truth.

#### Tier Selection — MANDATORY

You MUST run `~/.claude/hooks/validate_tier.py` to pick the tier — never eyeball it. The script reads `git diff` for the relevant scope, counts effective LOC, scans sensitive paths and forbidden markers, and prints the recommended tier with a reason.

```bash
# Post-step gate (working tree changes since the step started)
~/.claude/hooks/validate_tier.py HEAD

# Pre-commit gate (staged changes only)
~/.claude/hooks/validate_tier.py --staged

# Programmatic / report-friendly
~/.claude/hooks/validate_tier.py --staged --json
```

The script accepts any git ref (`HEAD`, `main`, a commit sha) or the `--staged` flag. Exit codes:
- `0` with tier `Skip` — docs only, non-sensitive config only, or zero effective LOC; gate skipped
- `0` with tier `Light` — 2 reviewer agents (also every build-tooling-only diff)
- `0` with tier `Full` — 6 reviewer agents + python-enhancer (if Python diff)
- `2` with tier `BLOCK` — forbidden markers found (list defined in `validate_tier.py:FORBIDDEN_MARKERS` — the script is source of truth); resolve before any gate
- `1` — script error (git not available, bad ref, etc.)

**Why script not eyeball:** under time pressure the temptation is to pick Light and move on. The script removes that temptation and is honest about LOC + sensitive paths. The script is the source of truth for tier selection — if you disagree with its output, fix the script, not the call site.

#### Sensitive Path Patterns (auto-escalate to Full)

Any diff touching these patterns triggers Full tier — config included. The one
exception is prose (`.md`/`.rst`/`.adoc`): a doc's name is never a signal, so
`docs/security.md` neither escalates a diff nor stops a docs-only one Skipping.
A short list of named, tested false positives (harness session state, the
SessionStart hook, calendar `*-session.*` files) is exempt — see
`validate_tier.py:SENSITIVE_EXEMPT_PATHS`; the script is the source of truth.
- `auth`, `login`, `session`, `token`, `password`
- `payment`, `stripe`, `billing`
- `migration`, `schema`, `ALTER`
- `secret`, `credential`, `.env`
- `security`, `permission`, `rbac`, `rls`

#### Project Stack Hint

Project-level CLAUDE.md can optionally declare a `## Stack:` hint (e.g. `## Stack: data-engineering, web`) to inform gate behavior:
- `data-engineering` → skip contracts-reviewer (no public API)
- `bot` → skip historical-context-reviewer (small codebase)
- No hint → full agent set for that tier

#### TDD enforcement by stack hint

When the project's `CLAUDE.md` declares `## Stack: data-engineering`, the `test-coverage-reviewer` dispatch in the Full-tier gate receives an additional TDD rider (literal text below). The rider instructs the agent to flag any new business-logic Python file that lacks a matching `test_<name>.py` or `<name>_test.py` in the same diff as HIGH severity — which blocks via the existing re-validation loop. Detected at dispatch time by grepping the project's `CLAUDE.md` for the stack marker; no new script, no new agent.

**Business-logic code** = any new `.py` file NOT under an infrastructure carve-out (`hooks/`, `scripts/`, `config/`, `migrations/`, `infra/`, `ops/`, `tests/`) and NOT matching `conftest.py`, `__init__.py`, `setup.py`, `_version.py`, `*_test.py`, `test_*.py`.

**Matching test** = for a new `<path>/<name>.py`, any `test_<name>.py` or `<name>_test.py` file ADDED or MODIFIED anywhere in the same diff. Not required to live in a specific directory — same-stem match anywhere in the project passes.

**Scope:** ADDITIONS only. Pure renames (git diff status `R`) and modifications to existing files are exempt — the existing tests are assumed to cover them, and the reviewer's usual "meaningful tests" quality checks catch drift.

**Other stacks** (e.g. `web`, `bot`) do not trigger the TDD rider today. Add them to this rule when the project's testing discipline justifies the block.

**Rider text** — append verbatim to the `test-coverage-reviewer` dispatch prompt when the stack condition matches:

```
TDD RIDER — this project declares `## Stack: data-engineering`.

Enforce test-first discipline on new business-logic files:

1. For every Python file ADDED in this diff, check whether its path is
   under an infrastructure carve-out: hooks/, scripts/, config/,
   migrations/, infra/, ops/, tests/, or matches conftest.py, __init__.py,
   setup.py, _version.py, *_test.py, test_*.py. If yes → skip, not subject
   to TDD.

2. For every remaining new Python file <path>/<name>.py, search the diff
   for any file named test_<name>.py or <name>_test.py (anywhere in the
   project) that was also added or modified in the same diff.

3. If a new business-logic file has NO matching test file in the diff,
   flag as HIGH severity with message:
   "TDD violation: <path>/<name>.py added without a matching test file
   in the same diff. Write test_<name>.py first, confirm it fails, then
   implement the code."

4. Pure renames (git diff status R) and modifications to existing files
   are NOT subject to this rule — only ADDITIONS.
```

#### Coverage Enforcement

The test-coverage-reviewer agent (in Full tier) verifies test *quality* and meaningful coverage of business logic — NOT a fixed percentage. Per the testing section above, coverage % is a signal, not a gate. For infrastructure code (hooks, glue scripts, configs), the agent should focus on whether the critical paths (the parts that would actually break in production) have any test coverage at all, and skip percentage demands.

#### Agent output budgets

Every reviewer agent dispatched from the gate MUST be prompted with an explicit word cap. This is non-negotiable — the agent's job is to surface blockers, not produce a full audit report. Longer responses indicate prompt drift; tighten on next dispatch.

- **Initial review** (first dispatch on a diff): append to prompt — *"Under 400 words. Report by severity (CRITICAL/HIGH/MEDIUM/LOW) with file:line references. Empty finding lists are fine and welcome."*
- **Scoped re-run** (verifying specific prior findings after fixes): append to prompt — *"Under 200 words. Format: 'Finding N: RESOLVED / REMAINING / NEW' one line each. Spot-check for new issues introduced by the fixes."*
- **Python-enhancer tools** (lint/typecheck/security/deps_audit/complexity): no word cap needed — output is structured. But see `rules/tooling.md` § Python tooling for the hook-code ignore list that cuts lint noise at the source.

Rationale: in practice a single 6-agent Full-tier dispatch can consume 40-80k tokens in agent responses. Word budgets alone cut that by ~30-40% without changing what agents look at.

#### Evidence requirements for external claims

Reviewer agents sometimes cite "I queried X.com and found Y" findings, but the agent's tooling may not have been able to actually reach X (sandbox allowlist, MCP availability for the domain, etc.). Without the response inline, the operator can't tell if the finding is verified, training-data inference, or hallucination.

Every reviewer-agent dispatch (initial review AND scoped re-runs) MUST append this rider after the word-cap clause:

> *"For any finding that depends on external data (web fetch, API check, library docs lookup), inline the URL queried and the first 200 chars of the response. If you couldn't actually reach the source (sandbox restriction, MCP unavailable, training data only), say so and label the finding as a hypothesis — not a verified fact."*

Treat findings without that evidence block as hypotheses — verify with a tool that CAN reach the source (curl with sandbox-disabled, Context7 MCP, etc.), or downgrade severity until verified. Do NOT promote network-dependent agent findings to facts by default; they may be correct (training data sometimes is) but they may be hallucinated.

Origin: a bug-hunter agent once "queried" an external API to flag wrong series IDs, but that host was NOT in the sandbox allowlist, so the fetch could not have happened. The flag may have been correct from training data or hallucinated entirely — we couldn't tell from the agent's output alone.

#### Reviewer agents mutate the SHARED working tree

Gate agents run against the same checkout as the main session, and the good ones mutation-test: they break a guard and re-run the suite. That is exactly the behaviour you want — it often produces a gate's highest-value finding — but done in the shared tree it means **another agent (or you) can read the tree mid-mutation and report a phantom defect, and an agent's "restore" can revert edits made after its backup.** The dispatch rider below sends mutation testing to a private copy; the three rules here stay as the backstop for an agent that ignores it.

A typical case: `test-coverage-reviewer` deletes a migration's `WHERE` guard to prove the idempotency tests are tautological; `code-reviewer`, reading concurrently, reports a HIGH "unstaged mutation probe is live in your working tree"; by the time anyone checks, `git diff` is empty and the guard is intact. The report was accurate for a window that had already closed.

Three rules follow:

1. **`git add -A` before dispatching a gate.** The index becomes a known-good restore point, so `git checkout -- <file>` after any mutation returns to the state under review rather than to the last commit. This also makes "commit before mutation testing" cheap to honour mid-gate rather than only before it.
2. **Verify any agent claim about working-tree state yourself before acting.** Run `git diff` / `git status`. Never revert, re-apply, or "fix" a file on the strength of an agent's tree observation — it is a snapshot, and concurrent agents invalidate snapshots. Acting on such a finding unverified means re-editing correct code.
3. **Check the tree yourself when the gate ends, before you commit.** Rule 2
   covers agent *claims*; this covers agent *leftovers*. Run `git diff` after
   the last agent reports and confirm it shows only your own work — an agent
   that says "restored, verified" can still be wrong, and a mutation left in a
   file you are about to commit is indistinguishable from a deliberate edit
   once it is in the index.

   Not hypothetical, and not cheap to miss: an agent can leave a mutation
   live — for example a guard script made to exit non-zero, which would block
   a service on every boot once committed. It is separable from your own
   legitimate unstaged edits *only* if rule 1 was honoured.

   Two corollaries:

   - **A full-suite green that overlapped a mutation window proves nothing.**
     A wide run can pass with a mutation live in the tree, because pytest had
     already finished the relevant test file before the mutation landed. Re-run **scoped** to the affected test files — short enough
     that the tree cannot shift under it — rather than trusting the wide green.
   - **Restore surgically, not wholesale.** `git checkout -- <file>` discards
     your own unstaged work along with the agent's mutation. Diff against the
     index, identify which hunks are yours, and put back only the agent's.

Append to every gate dispatch that may mutate: *"Do NOT mutate files in the shared tree. To mutation-test, make a private copy: `C=$(mktemp -d "${TMPDIR:-/tmp}/<name>.XXXXXX") && git archive HEAD | tar -x -C "$C"`, then copy in any staged or uncommitted files under review. Tracked files only, so no `.env*` and no `node_modules`; if the suite needs dependencies, point it at the original's read-only (`ln -s <repo>/node_modules "$C/"`, or run the original's `.venv/bin/python` from inside `$C`). Before trusting any green, confirm one known-bad mutant goes red in the copy — a copy whose imports or symlinks still resolve to the shared tree tests the unmutated original. If the copy cannot run the suite (an archive has no `.git`, so tests that shell out to `git rev-parse` may need `git init -q` in the copy first), say so and skip mutation testing rather than touching the shared tree. Name the files you mutated, and delete `$C` when done (it is yours). Any other scratch files also go in a private `mktemp -d "${TMPDIR:-/tmp}/<name>.XXXXXX"` dir (a bare `mktemp -d` targets /var/folders on macOS and fails in the sandbox), never a fixed path under $TMPDIR; never `rm -rf` a path you did not create in this run."* Parallel agents share `$TMPDIR`, so one agent's `rm -rf` of a fixed-name directory can delete another's work in progress. A private copy also beats "restore every file you touch": an agent restoring from its own early backup can silently revert an edit the main session made in the meantime. Use `git archive` rather than `cp -R`, because a full copy also carries `node_modules` and gitignored secrets.

#### Gate report lifecycle

When a gate clears — all findings resolved, re-validation loop complete — collapse the validation report in the active conversation to a 2-line summary:

```
Gate passed (tier: <Skip|Light|Full>, agents: <N>). Findings: <M> fixed, 0 remaining.
```

Do NOT re-emit the full agent responses after fixes land. They are preserved in the commit message and git history, which is where they belong — the active context should hold only what's still load-bearing for the next step. This applies to both post-step and pre-commit gates.

### Post-Step Gate

#### Trigger

**Step gate vs branch gate — decide this BEFORE running Lane 1.** Run the reviewer gate per step only when that step could merge to `main` on its own. When the plan says the phase merges as one unit — or an intermediate commit is knowingly broken until a later one lands — keep the cheap per-step checks (tests, lint, `/simplify`) and run the **full reviewer gate once against `main...HEAD`** before opening the PR.

This **moves** the gate, it does not thin it. Interactions between tasks are visible only in the branch diff, so for a multi-task phase the single branch gate is the higher-value one. Typical catches: a timezone skew introduced in one task and visible only against data another task writes, or a guard exemption written in one task that goes stale in a later one — none of which a per-task gate can see. What it removes is real cost: repeated `/simplify` + gate rounds over overlapping diffs.

Then: tests pass for the step → run Lane 1 (`/simplify`) on the step's diff; mandatory on Light/Full tier, skipped on Skip tier → if simplify edited files, re-run the step's tests → then proceed to the gate. See "Review Lanes" above for the full lane topology.

#### Mandatory pre-check: run validate_tier.py
Lane 1 must have run (or been explicitly skipped on Skip tier) before this script fires. Then run `~/.claude/hooks/validate_tier.py HEAD` (or `--staged` for the pre-commit gate). It picks the tier AND scans for forbidden markers in one pass. If exit code is `2` (BLOCK), resolve the markers before any validation. The script supersedes the old "scan for `TODO`/`FIXME`/etc." manual step.

#### Scope
Full diff of the current step — all files changed since the step began.

#### Agents
Launch agents per the tier the script reported. For Full tier on a Python diff, also run the python-enhancer MCP tools in parallel (see `rules/tooling.md` § Python tooling).

#### Blocking
**Hard block.** ALL findings at any severity must be resolved before proceeding.

### Pre-Commit Gate

⚠️ **ONE GATE PER DIFF. Skip this gate entirely when the work was a single step** — the post-step gate already reviewed that exact diff, and re-running it is the same agents reading the same code to reach the same conclusion. The pre-commit gate exists to catch what only appears once several steps are *combined*; with one step there is nothing to combine.

So:
- **Single-step change** (every Skip/Light change, and Full-tier work that was one task) → post-step gate only. Commit on green.
- **Multi-step plan** → post-step gate per step, then this gate over the delta.

#### Trigger
All plan steps complete, about to commit, **and the plan had more than one step**.

#### Scope
**Delta only** — changes since the last clean validation pass. When the phase merged as one unit and no step gate ever ran (see § Post-Step Gate § Trigger), there is no prior clean pass, so the delta IS `main...HEAD` — that branch-wide run is the gate this section describes, not an extra one stacked on top of it.

#### Tier
Always **Full** — all 6 agents in parallel. No shortcuts at the final checkpoint of a multi-step plan.

#### Blocking
**Hard block.** Same as post-step gate.

### Re-Validation Loop — MANDATORY

When a gate blocks, follow this exact ritual:

1. **Read all findings** in the validation report. Group by severity (CRITICAL → LOW).
2. **Fix all CRITICAL/HIGH findings first**, then MEDIUM, then LOW. Do not start fixing LOW while CRITICAL is unresolved.
3. **Re-run the test suite** for the affected component to ensure fixes didn't introduce regressions.
4. **Re-run ONLY the agents that flagged issues** — never re-run all six. The validation report's "BLOCK" agents form the re-run set. If `python-enhancer` flagged issues, re-run only those tools too.
5. **Re-run `validate_tier.py`** if your fix touched additional files — the tier may have shifted.
6. **If new findings appear**, go to step 1. Loop until all agents PASS.
7. **All clear** → record the validation pass timestamp (mentally, or in the report file) and proceed to the next plan step / commit.

**Anti-pattern to avoid:** "I fixed the obvious one, the others are minor, let me proceed." NO. Hard block means hard block — every finding gets fixed or explicitly justified before moving on.

### Validation Report Format

Use the template at `~/.claude/templates/validation_report.md` verbatim. It has placeholders for tier, agents, python-enhancer, complexity warnings, and the final status block. Fill in placeholders, delete sections that don't apply (e.g. complexity warnings on a non-Python diff), keep agent results in the listed order.

Severity levels (also defined in the template):
- **CRITICAL** — security vulnerability, data loss risk, crash
- **HIGH** — bug, contract breakage, significant quality issue
- **MEDIUM** — code smell, minor quality issue, weak test coverage
- **LOW** — style nit, minor improvement suggestion

### Edge Cases

| Scenario | Behavior |
|---|---|
| Plan step has no tests (infra/config) | Post-step gate skipped for that step |
| Agent fails with genuine error (crash, no response) | Block — do not proceed with partial validation |
| 2+ agents fail with org/quota/transient errors | Graceful-degrade allowed (see below) |
| Fix introduces new issues | Re-validation loop continues until clean |
| User wants to skip validation | Not allowed during plan execution |

#### Graceful-degrade clause (transient agent failures only)

When 2+ reviewer agents fail with **transient infrastructure errors** (org usage limit, rate-limit, network timeout, plugin not loaded), the gate MAY proceed if ALL of these hold:

1. At least 4 reviewer agents completed successfully and all cleared (no findings or all findings resolved).
2. The cleared set MUST include both `code-review:bug-hunter` AND `code-review:security-auditor`. These two cover the highest-blast-radius concerns; degrading without them is not graceful, it's reckless.
3. The commit body explicitly notes the degradation: agents that failed transiently, why they failed, and what was NOT covered.

Example commit-body trailer:
```
Validation gate: graceful-degrade. 4/6 reviewers cleared
(bug-hunter, security-auditor, code-reviewer, contracts-reviewer).
Skipped: historical-context-reviewer + test-coverage-reviewer (org
usage limit mid-gate). Re-run scheduled for next session.
```

Genuine agent failures (crash, hallucinated output, no response) are NOT eligible — those still block. The distinction matters: hallucinations ARE found in real reviewer responses (e.g. a bug-hunter false-stale-diff claim) — those count as genuine failure, not transient. When in doubt, treat as genuine and block.

Origin: a Full-tier gate once lost 3 of 6 reviewers to org limits mid-gate; the strict "block on partial validation" rule forced a wait-and-retry loop costing ~30 min per cycle. Graceful-degrade with the bug-hunter + security-auditor floor preserves the highest-signal safety nets while letting the plan move.

### Does NOT Replace

- `prd_guard.sh` — pre-tool write guard
- `gitleaks_guard.sh` — pre-bash secrets scan
- an optional global git `pre-commit` secret-scan hook, if you run one (`session_start.py` checks it when `CRAFT_GIT_HOOKS_DIR` is set)
- `pr_security_scan.sh` — PR security scan

---

## Shell — `$TMPDIR` is not guaranteed

Write temp paths as `"${TMPDIR:-/tmp}/x"`, or as an explicit scratchpad path, never as a bare `"$TMPDIR/x"`. `$TMPDIR` is set by macOS and by the Claude Code Bash sandbox. It is usually **unset on Linux and in any `dangerouslyDisableSandbox: true` call**, and then `"$TMPDIR/x"` expands to `/x`, a root-owned path.

The failure is quiet: a heredoc writes to `/newblock.txt` (Permission denied) and the paired Python dies on `KeyError: 'TMPDIR'`, or a backup `cp` to `/x.bak` fails while the script carries on and can no longer restore. The same goes for `mktemp`: give it a template under `${TMPDIR:-/tmp}`, not a bare call, which lands in `/var/folders` on macOS outside the sandbox allowlist.
