# 🛠️ claude-craft-kit

[![▶ Live demo](https://img.shields.io/badge/▶_Live_demo-effecet.github.io-d97757)](https://effecet.github.io/claude-craft-kit/)
[![ci](https://github.com/effecet/claude-craft-kit/actions/workflows/ci.yml/badge.svg)](https://github.com/effecet/claude-craft-kit/actions/workflows/ci.yml)
[![license: MIT](https://img.shields.io/badge/license-MIT-blue)](LICENSE)
[![Claude Code](https://img.shields.io/badge/Claude%20Code-hooks%20%2B%20rules-d97757)](https://docs.claude.com/en/docs/claude-code)
[![hooks](https://img.shields.io/badge/hooks-17-5ddf7a)](hooks/)
[![Python](https://img.shields.io/badge/python-3.10%2B-3776ab?logo=python&logoColor=white)](#)
[![shell](https://img.shields.io/badge/shell-bash-4eaa25?logo=gnubash&logoColor=white)](#)

An opinionated, batteries-included **workflow harness for [Claude Code](https://docs.claude.com/en/docs/claude-code)** —
the hooks, rules, and lifecycle that turn an agent session into a disciplined
engineering loop: **pre-tool safety guards → a tiered validation gate →
an exit/wrap-up gate**, all driven by a set of behavior rules.

It's a **template**: fork it, drop the hooks into your `~/.claude`, wire them in
`settings.json`, and adapt the rules to your stack. Everything runs locally with
no dependencies beyond Python 3 + bash. An optional memory backend is supported
but **off by default**.

## The lifecycle

**▶ [See it animated →](https://effecet.github.io/claude-craft-kit/)** — an interactive, click-through version of the flow below (auto-plays a session through the hooks).

```mermaid
flowchart TD
    A([Session starts]) --> B[SessionStart hook<br/>session_start.py<br/>· init/resume state · git context · open specs]
    B --> C{User message}
    C --> D[UserPromptSubmit hooks<br/>track.py · consume_prompt.py<br/>· count msgs · token report · friction/goodbye]
    D --> E[Claude works:<br/>plan → implement → test]
    E --> F[PreToolUse guards]
    F -->|Bash: git commit / gh pr| G[gitleaks · pr_security_scan · ruff_workflow_guard]
    F -->|Write/Edit| H[prd_guard<br/>warn on prod paths]
    F -->|memory write| I[memory_name_guard<br/>keep memory names concise]
    G & H & I --> J[/Tiered validation gate<br/>validate_tier.py/]
    J -->|Skip| K[config/docs only]
    J -->|Light| L[2 reviewers · <30 LOC]
    J -->|Full| M[6 reviewers · sensitive paths / features]
    K & L & M --> N{More work?}
    N -->|yes| C
    N -->|no| O[Stop hook<br/>stop_gate.py<br/>· block exit until wrap-up if session was substantial]
    O --> P([Session ends])

    style J fill:#ffd23f,color:#150f2e
    style O fill:#ff5d8f,color:#fff
    style B fill:#5ddf7a,color:#150f2e
```

See **[docs/WORKFLOW.md](docs/WORKFLOW.md)** for the full walkthrough and the validation-tier rules.

## What's inside

**Hooks** (`hooks/`) — the lifecycle machinery:

| Stage | Hook | Does |
|---|---|---|
| SessionStart | `session_start.py` | Init/resume session state, git context, surface open specs + pending work |
| SessionStart | `mcp_restore_guard.py` | Self-heal: re-register local stdio MCP servers that got dropped from `~/.claude.json` (opt-in via `mcp-restore.json`) |
| UserPromptSubmit | `track.py`, `consume_prompt.py` | Message counting, token report, friction/goodbye detection, pending prompts |
| PreToolUse | `gitleaks_guard.sh`, `pr_security_scan.sh` | Block commits/PRs that leak secrets |
| PreToolUse | `prd_guard.sh` | Warn before editing prod-looking paths |
| PreToolUse | `ruff_workflow_guard.py` | Ruff check before committing in CI repos |
| PreToolUse | `memory_name_guard.py` | Keep memory names concise (optional convention hook) |
| (gate) | `validate_tier.py` | Pick the review tier from diff size + sensitive paths |
| Stop | `stop_gate.py` | Block a clean exit until wrap-up runs on substantial sessions |
| idle | `on_idle.sh` | Nudge a wrap-up after inactivity |
| helpers | `_state.py`, `common.py`, `spec_audit.py` | Shared state schema, path helpers, spec reporting |

**Rules** (`rules/`) — the behavior the hooks enforce: `development.md` (the
validation gate + TDD), `operations.md` (proposal gate, git safety), `memory.md`,
`project-hygiene.md`, `context7.md`, `data-engineering.md`, `typescript.md`,
`supabase.md`, `tooling.md`, `npm-cache-eperm.md`.

**Commands** (`commands/`) — slash commands installed into `~/.claude/commands/`:
`/clear-pending` reviews the open pending queue and resolves what's already done
(batched behind the proposal gate — nothing is resolved without approval).

**Optional memory backend** (`hooks/optional/`) — `proactive_recall.py` and
`backup_hook.py`, inert unless `BRAIN_ENABLED=true`. Wire them to any
Postgres-backed memory store; a reference implementation is
[**memory-persistor**](https://github.com/effecet/memory-persistor).

**Pending work queue** — `session_start.py` surfaces deferred work in the session
brief: the top open items, priority-ordered. It reads a plain `pending.md` by
default, and with `BRAIN_ENABLED=true` reads the backend's `pending` table instead
(written via memory-persistor's `pending_add` / `pending_resolve` tools). Both paths
fail open — a missing file or unreachable database never blocks session start.

## Quick start

```bash
# 1. Install hooks + rules into ~/.claude (override with CLAUDE_DIR=...)
make install

# 2. Configure (edit paths/flags to taste)
cp config.example.sh ~/.claude/config.local.sh   # then source it from your shell profile

# 3. Wire the hooks — merge settings.example.json's "hooks" block into ~/.claude/settings.json

# 4. (optional) adopt the global instructions
#    review CLAUDE.example.md, then save it as ~/.claude/CLAUDE.md
```

Other targets: `make syntax` (check all hooks), `make lint` (ruff + shellcheck),
`make scan` (gitleaks), `make help`.

Nothing phones home; the brain-coupled features stay dormant until you opt in.

## Project structure

```
claude-craft-kit/
├── README.md
├── index.html                  # interactive animated lifecycle (the Pages site)
├── LICENSE                     # MIT
├── Makefile                    # install / syntax / lint / scan
├── CLAUDE.example.md           # generified global-instructions skeleton
├── config.example.sh           # env config (paths, BRAIN_ENABLED, backend)
├── settings.example.json       # Claude Code hook wiring
├── mcp-restore.example.json    # example: local MCP servers to self-heal on SessionStart
├── .gitleaks.toml              # secret-scan config
├── ruff.toml                   # lint config
├── .github/workflows/ci.yml    # CI: ruff · shellcheck · syntax · pytest
├── docs/
│   └── WORKFLOW.md             # the full lifecycle + validation-tier rules
├── hooks/
│   ├── session_start.py        # SessionStart: state, git context, specs
│   ├── mcp_restore_guard.py    # SessionStart: self-heal missing local MCP servers
│   ├── track.py                # UserPromptSubmit: counting, tokens, friction
│   ├── consume_prompt.py       # UserPromptSubmit: surface pending prompts
│   ├── validate_tier.py        # the tiered validation gate
│   ├── stop_gate.py            # Stop: wrap-up exit gate
│   ├── gitleaks_guard.sh       # PreToolUse: secret scan before commit
│   ├── pr_security_scan.sh     # PreToolUse: secret scan before PR
│   ├── prd_guard.sh            # PreToolUse: prod-path warning
│   ├── ruff_workflow_guard.py  # PreToolUse: ruff before commit
│   ├── memory_name_guard.py    # PreToolUse: memory-name convention
│   ├── on_idle.sh              # idle: wrap-up nudge
│   ├── spec_audit.py           # report open specs
│   ├── _state.py               # session-state schema
│   ├── common.py               # path + connection-string helpers
│   └── optional/               # backend-coupled, BRAIN_ENABLED=false by default
│       ├── proactive_recall.py # surface relevant memories per prompt
│       └── backup_hook.py      # generic git backup of your config
├── commands/
│   └── clear-pending.md        # /clear-pending — triage the pending work queue
├── rules/                      # the 10 behavior rules
└── tests/                      # hook unit tests (pytest)
```

## License

MIT — see [LICENSE](LICENSE).
