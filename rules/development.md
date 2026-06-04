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

Steps 1-4 CANNOT be skipped. If the user says "just build it", a lightweight spec + plan is still required.

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

The brainstorming → spec → plan flow is non-negotiable, but **the size of each artifact must match the size of the work**. Same checklist, same discipline, different word count. This is not an escape hatch — it's how the standard process stays standard without growing ceremonial weight on small changes.

| Work size | Brainstorm | Spec | Plan |
|---|---|---|---|
| **Trivial** (1-line config tweak, typo, single-default change) | One clarifying question max ("is this the only place X is set?") | 3 sentences: intent, change, why | 1-line: "edit `<file>` and verify `<test/check>`" |
| **Small** (≤30 LOC, single file, no sensitive paths) | 1-2 questions to confirm scope | ~100 words: intent, approach, alternatives considered (1-line each) | Numbered steps, 3-7 items |
| **Medium** (one feature, multiple files, some design choices) | Full Q&A loop, propose 2 approaches | ~300-500 words, sectioned (architecture, components, error handling) | Numbered steps with per-step exit criteria |
| **Large** (subsystem, sensitive paths, schema changes) | Full Q&A loop, propose 2-3 approaches with tradeoffs, visual companion if mockups apply | Full sectioned design, decomposed if needed | Phased plan with checkpoints between phases |

**Picking the size:** use `validate_tier.py` output as the signal — `Skip` ≈ trivial, `Light` ≈ small, `Full` ≈ medium or large (judge by whether sensitive paths or new subsystems are involved). The size is a default; the user can always say "this needs a real brainstorm" to escalate, or "tiny change" to drop a tier.

**Anti-pattern to avoid:** padding a trivial change to look medium because the skill template "expects" sections. A 3-sentence spec is a complete spec when the work is 3-sentences worth. Inflating it wastes attention and makes the user trust the process less.

**Anti-pattern in the other direction:** compressing a medium change into a "small" template to save tokens. The size is determined by the work, not by the desire to skip steps. If you find yourself wanting to drop a tier to move faster, that's a signal you're under-sizing — pick the higher tier.

### Artifact format by tier

Sizing rules (above) govern word count. This rule governs artifact shape — they are orthogonal.

**Trivial and small tier:** write ONE combined artifact at `docs/superpowers/specs/YYYY-MM-DD-<topic>.md` (or `~/.claude/specs/YYYY-MM-DD-<topic>.md` for `.claude`-scoped work) using the template at `~/.claude/templates/spec_plan_combined.md`. Sections: *Intent / Change / Why / Verify*. Do NOT invoke the `writing-plans` skill for these tiers — the combined doc IS the plan. Rationale: at 3 sentences + 1 line, a separate spec.md and plan.md say the same thing twice.

**Medium and large tier:** write separate `spec.md` (via brainstorming skill) and `plan.md` (via writing-plans skill) as today. The combined template is NOT used for medium/large — their size justifies two artifacts.

**Upgrade path:** if trivial/small work grows to medium mid-flight, copy the combined doc into a new sectioned `spec.md`, expand it, then invoke writing-plans. Do NOT retroactively merge an approved medium spec+plan into a combined doc — that would compress already-approved content.

### Tiered Validation Gate

Three tiers based on change size and risk:

| Tier | Criteria | Agents | When |
|------|----------|--------|------|
| **Skip** | Config-only, docs-only, no testable code | None | Infra changes, documentation |
| **Light** | <30 lines changed AND no sensitive paths AND no plan steps | code-reviewer, bug-hunter (2) | Bug fixes, tweaks, small features |
| **Full** | ≥30 lines OR plan steps OR sensitive paths OR explicit request | All 6 in parallel | Major features, refactors, security |

#### Tier Selection — MANDATORY

You MUST run `~/.claude/hooks/validate_tier.py` to pick the tier — never eyeball it. The script reads `git diff` for the relevant scope, counts LOC, scans sensitive paths and forbidden markers, and prints the recommended tier with a reason.

```bash
# Post-step gate (working tree changes since the step started)
~/.claude/hooks/validate_tier.py HEAD

# Pre-commit gate (staged changes only)
~/.claude/hooks/validate_tier.py --staged

# Programmatic / report-friendly
~/.claude/hooks/validate_tier.py --staged --json
```

The script accepts any git ref (`HEAD`, `main`, a commit sha) or the `--staged` flag. Exit codes:
- `0` with tier `Skip` — config/docs only, gate skipped
- `0` with tier `Light` — 2 reviewer agents
- `0` with tier `Full` — 6 reviewer agents + python-enhancer (if Python diff)
- `2` with tier `BLOCK` — forbidden markers found (list defined in `validate_tier.py:FORBIDDEN_MARKERS` — the script is source of truth); resolve before any gate
- `1` — script error (git not available, bad ref, etc.)

**Why script not eyeball:** under time pressure the temptation is to pick Light and move on. The script removes that temptation and is honest about LOC + sensitive paths. The script is the source of truth for tier selection — if you disagree with its output, fix the script, not the call site.

#### Sensitive Path Patterns (auto-escalate to Full)

Any diff touching these patterns triggers Full tier:
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

#### Gate report lifecycle

When a gate clears — all findings resolved, re-validation loop complete — collapse the validation report in the active conversation to a 2-line summary:

```
Gate passed (tier: <Skip|Light|Full>, agents: <N>). Findings: <M> fixed, 0 remaining.
```

Do NOT re-emit the full agent responses after fixes land. They are preserved in the commit message and git history, which is where they belong — the active context should hold only what's still load-bearing for the next step. This applies to both post-step and pre-commit gates.

### Post-Step Gate

#### Trigger
Tests pass for a plan step → run Lane 1 (`/simplify`) on the step's diff; mandatory on Light/Full tier, skipped on Skip tier → if simplify edited files, re-run the step's tests → then proceed to the gate. See "Review Lanes" above for the full lane topology.

#### Mandatory pre-check: run validate_tier.py
Lane 1 must have run (or been explicitly skipped on Skip tier) before this script fires. Then run `~/.claude/hooks/validate_tier.py HEAD` (or `--staged` for the pre-commit gate). It picks the tier AND scans for forbidden markers in one pass. If exit code is `2` (BLOCK), resolve the markers before any validation. The script supersedes the old "scan for `TODO`/`FIXME`/etc." manual step.

#### Scope
Full diff of the current step — all files changed since the step began.

#### Agents
Launch agents per the tier the script reported. For Full tier on a Python diff, also run the python-enhancer MCP tools in parallel (see `rules/tooling.md` § Python tooling).

#### Blocking
**Hard block.** ALL findings at any severity must be resolved before proceeding.

### Pre-Commit Gate

#### Trigger
All plan steps complete, about to commit.

#### Scope
**Delta only** — changes since the last clean validation pass.

#### Tier
Always **Full** — all 6 agents in parallel. No shortcuts at the final checkpoint.

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
- `pr_security_scan.sh` — PR security scan
