#!/usr/bin/env python3
"""
validate_tier.py — Determine which validation tier a diff requires.

Reads `git diff` for the given scope, counts changed LOC, scans for sensitive
path patterns and forbidden code markers, and prints the recommended validation
tier per rules/development.md (Skip / Light / Full), or BLOCK if forbidden
markers are present.

Usage:
    validate_tier.py [scope] [--json]

scope (optional, default HEAD):
    HEAD          diff vs HEAD (working tree changes)
    --staged      diff staged changes only
    <ref>         diff vs a specific branch or commit (e.g. main, abc123)

Exit codes:
    0  Skip / Light / Full (advisory — caller decides)
    2  BLOCK (forbidden markers present — must fix before any gate)
    1  Error (no git repo, bad scope, etc.)
"""

import json
import os
import re
import subprocess
import sys
from collections import Counter
from pathlib import Path

# Sensitive path keywords from rules/development.md — auto-escalate to Full tier.
# Common words (auth, session, schema, migration, etc.) are anchored by segment
# boundaries — `/`, `_`, `.`, `-`, or string end — to avoid overmatching
# innocuous filenames like `authority.py`, `sessionizer.py`, `schema_helpers.py`.
# Rare/distinctive words (stripe, password, rbac) use plain substring since
# false positives are unlikely.
#
# Each entry is (friendly_name, compiled_regex). The friendly name is what
# shows up in reports and what downstream consumers (tests, JSON output) see.
_BOUND = r"(?:^|[/_.\-])"
_BOUND_END = r"(?:$|[/_.\-])"


def _bounded(word: str) -> re.Pattern[str]:
    return re.compile(rf"{_BOUND}{word}{_BOUND_END}", re.IGNORECASE)


def _sub(word: str) -> re.Pattern[str]:
    return re.compile(word, re.IGNORECASE)


# Template env filename suffixes — checked in-sync by the `.env` sensitive
# pattern AND the SKIP_PATTERNS entry below. Adding a suffix here propagates
# to both sites; drifting them would silently restore the false-escalation bug.
# Underscore prefix is a convention hint — the test suite iterates this tuple
# so coverage auto-extends when a new suffix is added. A rename would surface
# as AttributeError in tests, which is the intended loud-failure behavior.
_TEMPLATE_SUFFIXES: tuple[str, ...] = ("example", "template", "sample", "dist")
_TEMPLATE_ALT = "|".join(_TEMPLATE_SUFFIXES)

SENSITIVE_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("auth", _bounded("auth")),
    ("login", _bounded("login")),
    ("session", _bounded("session")),
    ("token", _bounded("token")),
    ("password", _sub("password")),
    ("payment", _sub("payment")),
    ("stripe", _sub("stripe")),
    ("billing", _sub("billing")),
    ("migration", re.compile(rf"{_BOUND}migration", re.IGNORECASE)),
    ("schema", _bounded("schema")),
    ("alter", _bounded("alter")),
    ("secret", _sub("secret")),
    ("credential", _sub("credential")),
    # Real .env files escalate; template variants (.env.example, .env.template,
    # .env.sample, .env.dist, and multi-dot forms like .env.local.example)
    # hold placeholders and must not. Keep in sync with SKIP_PATTERNS below.
    (
        ".env",
        re.compile(
            rf"(^|/)\.env(?![\w.\-]*\.({_TEMPLATE_ALT})$)",
            re.IGNORECASE,
        ),
    ),
    ("security", _sub("security")),
    ("permission", _sub("permission")),
    ("rbac", _bounded("rbac")),
    ("rls", _bounded("rls")),
]

# The harness's SessionStart lifecycle hook and its tests: `_` is a segment
# boundary, so `session` matched the filename and every edit to one of the
# most-edited hooks cost a 6-agent Full gate. These paths skip the `session`
# pattern ONLY and are still checked against every other, so any test suffix
# may ride along: `test_session_start_token.py` still escalates on `token`,
# `test_session_start_secret_scan.py` on `secret`.
#
# A deployed `.claude/hooks/` applies in any repo. This kit keeps the hook at
# the repo ROOT (`hooks/`, `tests/`), and those are generic names, so the root
# layout applies only in a config repo (is_config_repo): this kit, or the
# checkout CRAFT_CONFIG_REPO names, under CRAFT_CONFIG_SUBDIR when that is set.
# `src/session_start.py` and a nested `app/hooks/session_start.py` always
# escalate.
_DOT_CLAUDE_SESSION_START_HOOK = re.compile(
    r"(^|/)\.claude/hooks/(session_start|tests/test_session_start(_\w+)?)\.py$"
)
_ROOT_SESSION_START_HOOK = re.compile(
    r"^(hooks/session_start|(hooks/)?tests/test_session_start(_\w+)?)\.py$"
)
KIT_REPO = "claude-craft-kit"


def is_session_start_hook(path: str, config_repo: bool = False) -> bool:
    """True for the SessionStart hook or one of its tests.

    The root layout counts only when `config_repo` is true (is_config_repo())."""
    if _DOT_CLAUDE_SESSION_START_HOOK.search(path):
        return True
    return config_repo and _is_root_session_start_hook(path)


def _is_root_session_start_hook(path: str) -> bool:
    """The root layout, tests under `tests/` or `hooks/tests/`.

    With CRAFT_CONFIG_SUBDIR set, both `<subdir>/...` and the checkout root
    count (tests often stay at the root)."""
    subdir = os.environ.get("CRAFT_CONFIG_SUBDIR", "").strip("/")
    if subdir and path.startswith(f"{subdir}/"):
        path = path[len(subdir) + 1 :]
    return bool(_ROOT_SESSION_START_HOOK.search(path))


# The session_start paths are also content-guarded. The hook really does hold
# credential plumbing (the optional memory backend's DSN handling), and its
# tests exercise it, so a changed line — added OR deleted — that touches that
# plumbing reports `credential-content` and goes Full.
#
# Except an import line that only gains or loses NON-credential names
# (`from common import resolve_connection_string` gaining `home_memory_dir`):
# the scan would match the helper's NAME, not plumbing. Import lines are
# compared as (module, name, alias) triples, keeping those where any of the
# three is credential-shaped, plus star imports: a different module, name or
# alias counts. A line with `;` is never treated as an import, and a
# parenthesised multi-line import is scanned like any other line (both fail
# toward Full).
_IMPORT_LINE_RE = re.compile(r"^\s*(?:from\s+(\S+)\s+)?import\s+([^;]*)$")
_IMPORT_ITEM_RE = re.compile(r"^([\w.]+)(?:\s+as\s+(\w+))?$")
CREDENTIAL_CONTENT_RE = re.compile(
    r"psql|dsn|database_url|pgpassword|password|secret|token|credential|\.env\b"
    r"|connection_string|\bconn\b|\benv\s*=",
    re.IGNORECASE,
)

# File-level exemption (different from `.env`-style in-pattern lookahead in
# SENSITIVE_PATTERNS): an exempt path is dropped from ALL sensitive checks,
# not just one. Each entry must be paired with tests proving the false
# positive is cured AND a near-miss path still escalates.
SENSITIVE_EXEMPT_PATHS: list[re.Pattern[str]] = [
    re.compile(r"(^|/)\.claude/session/[^/]+\.json$"),
    # Calendar/booking session files (schedule-session.*, booking-session.*,
    # calendar-session.*) are NOT auth sessions and must not trigger the
    # `session` sensitive escalation. Example origin: a calendar availability
    # handler (schedule-session.js) once auto-escalated a 6-LOC CSS-class
    # rename to Full tier.
    re.compile(r"(^|/)(schedule|booking|calendar)-session\.[a-z]+$"),
]

# Forbidden code markers — block before any gate runs
FORBIDDEN_MARKERS = ["TODO", "FIXME", "FIX", "HACK", "TEMP", "XXX"]

# Prose: read by people, never executed, so its lines — added OR deleted —
# never count toward the tier, and its NAME is never a risk signal: docs/security.md
# neither escalates a diff nor stops a docs-only one Skipping. Prose is decided by
# extension alone; a non-prose file under docs/ still gets the sensitive check.
# `.txt` is NOT here: requirements.txt pins
# and CMakeLists.txt are real changes. Config (.yml, .json…) keeps counting too.
# One pattern feeds both SKIP_PATTERNS and PROSE_RE so they cannot drift.
_PROSE_PATTERN = r"(?i)\.(md|rst|adoc)$"
PROSE_RE = re.compile(_PROSE_PATTERN)

# Code extensions a docs/ or README path match must not swallow: docs/run.sh
# and README_install.sh run, whatever directory or prefix they sit under.
_NOT_CODE = (
    r"(?!.*\.(?i:sh|bash|zsh|ps1|bat|cmd|py|ipynb|js|mjs|cjs|ts|tsx|jsx|vue|svelte"
    r"|rb|pl|php|lua|go|rs|java|kt|c|h|cc|cpp|hpp|cs|swift|sql|tf)$)"
)

# Skip-tier file patterns: config / docs only — no testable code
SKIP_PATTERNS = [
    _PROSE_PATTERN,
    r"\.txt$",
    r"\.json$",
    r"\.yaml$",
    r"\.yml$",
    r"\.toml$",
    r"\.ini$",
    r"\.cfg$",
    r"\.gitignore$",
    rf"\.env[\w.\-]*\.({_TEMPLATE_ALT})$",
    # Under docs/ only a file WITH an extension: an extensionless docs/bin/run
    # is as likely a script as a page.
    rf"(^|/)docs/{_NOT_CODE}.*\.\w+$",
    rf"(^|/)README{_NOT_CODE}",
]

# Build tooling: a diff touching ONLY these (plus docs/config) is capped at
# Light — two reviewers, never six — but never Skip. A Makefile can leak a DSN
# into argv, carry a load-bearing `set -e` or run every migration; none of
# those defects fails a `make` run. Anchored to the
# whole basename, so `Makefile.generator.py` does not match. `pyproject.toml`
# is deliberately absent: it can carry [project] / [build-system] pins. Build
# files stay IN the forbidden-marker scan, unlike SKIP_PATTERNS.
BUILD_PATTERNS = [
    r"(^|/)(GNU)?[Mm]akefile$",
    r"(^|/)[Jj]ustfile$",
    r"(^|/)Taskfile\.ya?ml$",
    r"\.mk$",
]

# Lockfiles: machine-written, so a reviewer reads them for one thing — a
# tampered `resolved` URL or hash changes what gets installed. Never Skip, but
# capped at Light like build tooling, and their lines are not effective LOC:
# an `npm audit fix` rewrites thousands of lines and must not cost six
# reviewers. Not marker-scanned. Named, not `*.lock`: `src/evil.lock` is code.
LOCKFILE_PATTERNS = [
    r"(^|/)(package-lock|npm-shrinkwrap)\.json$",
    r"(^|/)pnpm-lock\.ya?ml$",
    r"(^|/)(uv|yarn|poetry|Cargo|Gemfile|composer|Pipfile|bun|flake)\.lock$",
]
CAP_PATTERNS = BUILD_PATTERNS + LOCKFILE_PATTERNS

# Executable config: a skip pattern matches the extension, but the content runs
# (CI `run:` blocks, hook commands and `permissions.allow`, npm scripts, MCP
# spawn commands, compose services) or decides what is scanned and installed
# (the gitleaks allowlist, dependency pins). Light FLOOR, not a cap: beside code
# the LOC threshold still applies.
CI_WORKFLOW_PATTERNS = [
    r"(^|/)\.(github|forgejo|gitea)/workflows/[^/]+\.ya?ml$",
    r"(^|/)\.gitlab-ci\.ya?ml$",
    r"(^|/)\.woodpecker(\.ya?ml$|/[^/]+\.ya?ml$)",
    r"(^|/)action\.ya?ml$",  # composite actions: shell a workflow runs
]
EXEC_CONFIG_PATTERNS = [
    *CI_WORKFLOW_PATTERNS,
    r"(^|/)settings(\.local)?\.json$",
    r"(^|/)hooks/[^/]+\.json$",
    r"(^|/)(package|composer)\.json$",
    r"(^|/)pyproject\.toml$",
    r"(^|/)mcp/[^/]+\.json$",
    r"(^|/)\.mcp\.json$",
    r"(^|/)(docker-)?compose(\.[\w-]+)?\.ya?ml$",
    r"(^|/)\.pre-commit-config\.ya?ml$",
    r"(^|/)\.gitleaks\.toml$",
    r"(^|/)requirements[\w.\-]*\.txt$",
]

# Instruction prose: loaded into every session as instructions, so deleting the
# push gate from rules/operations.md is a behaviour change, not a doc edit.
# Prose counts 0 effective LOC, so for these the floor is also the ceiling.
INSTRUCTION_PATTERNS = [
    r"(^|/)(CLAUDE(\.local)?|AGENTS|SKILL)\.md$",
    r"^(rules|agents|commands|skills)/.+\.md$",  # this kit's own layout
    r"(^|/)\.claude/(rules|agents|commands|skills)/.+\.md$",
]

# Never Skip. Build files and lockfiles are additionally CAPPED at Light
# (only_capped_and_skippable).
LIGHT_FLOOR_PATTERNS = CAP_PATTERNS + EXEC_CONFIG_PATTERNS + INSTRUCTION_PATTERNS

# A changed workflow line that widens what the job may do or reach → Full: a
# `permissions:` block or a scope inside one, any reference to `secrets`
# (`secrets.X`, `secrets['X']`, `toJSON(secrets)`, `secrets: inherit`), the
# job token, OIDC, or a trigger that runs fork code with secrets.
CI_ESCALATE_RE = re.compile(
    r"^\s*permissions\s*:|^\s*[\w-]+\s*:\s*[\"']?write(-all)?[\"']?\s*(#.*)?$|\bsecrets\b"
    r"|\bgithub\.token\b|\bid-token\b|\bpull_request_target\b"
)


def _matches(path: str, patterns: list[str]) -> bool:
    return any(re.search(p, path) for p in patterns)


# Comment syntax by file type (see _comment_mask). Unknown types never discount.
_HASH_RE = re.compile(
    r"\.(py|pyi|sh|bash|zsh|rb|pl|r|toml|ya?ml|cfg|ini|mk)$"
    r"|(^|/)((GNU)?[Mm]akefile|[Jj]ustfile|Dockerfile)$"
)
_SLASH_RE = re.compile(r"\.(ts|tsx|js|jsx|mjs|cjs|go|rs|java|kt|c|h|cc|cpp|hpp|cs|swift|scss|css)$")
_SQL_RE = re.compile(r"\.sql$")

# Marker-scan exemptions: files that legitimately contain forbidden marker
# strings as DATA (not as leftover code markers). validate_tier.py defines the
# marker list; its test files use them as fixture input. Both would otherwise
# cause the scanner to block on itself. Distinct from SKIP_PATTERNS because
# these files ARE real code and should affect tier selection normally.
MARKER_SCAN_EXEMPT = [
    r"(^|/)validate_tier\.py$",
    r"(^|/)test_validate_tier\.py$",
    r"(^|/)(hooks/)?tests/test_validate_tier_[a-z_]+\.py$",
]

LIGHT_THRESHOLD = 30  # LOC — boundary between Light and Full tiers

# Pinned so a user's git config cannot change the text parsed below:
# diff.noprefix / mnemonicPrefix would rename every header, an external diff
# driver or colour would replace the format, and quotePath would hide paths.
_GIT_DIFF_OPTS = [
    "-c", "core.quotePath=off", "diff",
    "--no-ext-diff", "--no-color", "--src-prefix=a/", "--dst-prefix=b/",
]


def _git_diff(extra: list[str]) -> str:
    """Run the pinned `git diff` with `extra` appended.

    The literal "git" argv[0] means it can only ever run `git diff`.
    """
    try:
        proc = subprocess.run(
            ["git", *_GIT_DIFF_OPTS, *extra], capture_output=True, text=True, check=True
        )
    except FileNotFoundError:
        print("error: git not found in PATH", file=sys.stderr)
        sys.exit(1)
    except subprocess.CalledProcessError as e:
        print(
            f"error: git diff failed ({e.returncode}): {e.stderr.strip()}",
            file=sys.stderr,
        )
        sys.exit(1)
    return proc.stdout


UNKNOWN_PATH = "<unknown path>"

# (sign, path) → indexes into added[path] / deleted[path] where a run of
# CONSECUTIVE changed lines begins. Filled by get_diff, read by _comment_mask:
# block-comment state must not span unchanged lines, or the opener of one doc
# comment pairs with the closer of another and masks the code between them.
RUN_STARTS: dict[tuple[str, str], set[int]] = {}


def _header_path(line: str, prefix: str) -> str | None:
    """The path in a `--- a/x` / `+++ b/x` header, or None for /dev/null.

    Git C-quotes a path containing `"`, `\\`, a tab or a newline even with
    core.quotePath=off (`+++ "b/we\\"ird.py"`). Missing that dropped every line
    of such a file from the count, so 41 lines of exec() scored Skip.
    """
    rest = line[4:].rstrip("\t")
    if rest.startswith('"') and rest.endswith('"') and len(rest) >= 2:
        raw = rest[1:-1].encode("latin-1", "backslashreplace")
        try:
            rest = raw.decode("unicode_escape").encode("latin-1").decode("utf-8")
        except (UnicodeDecodeError, UnicodeEncodeError):
            return UNKNOWN_PATH
    if rest.startswith(prefix):
        return rest[len(prefix):]
    return None


def get_diff(
    scope: str,
) -> tuple[list[str], dict[str, list[str]], int, dict[str, list[str]]]:
    """Return (changed_files, added_lines_by_file, total_loc_changed, deleted_lines_by_file).

    The FILE LIST comes from `--name-status -z -M`, not from patch headers: a
    pure rename or a binary change has no `---`/`+++` lines at all, and a file
    list read from headers silently dropped them (an empty list is Skip). Both
    sides of a rename are listed, so renaming INTO a sensitive path escalates.

    LINE CONTENT comes from the patch, parsed with header state: `---`/`+++` are
    headers only between `diff --git` and the first `@@`. Inside a hunk, a
    deleted SQL comment `-- x` reads as `--- x` and an added `++ x` as `+++ x`;
    treating those as headers dropped lines and re-attributed everything after
    them to whatever path the fake header named.
    """
    scope_args = ["--staged"] if scope == "--staged" else [scope]
    files: list[str] = []
    fields = _git_diff([*scope_args, "--name-status", "-z", "-M"]).split("\0")
    i = 0
    while i < len(fields) and fields[i]:
        status = fields[i]
        n_paths = 2 if status[:1] in ("R", "C") else 1
        for path in fields[i + 1 : i + 1 + n_paths]:
            if path and path not in files:
                files.append(path)
        i += 1 + n_paths

    added: dict[str, list[str]] = {f: [] for f in files}
    deleted: dict[str, list[str]] = {f: [] for f in files}
    loc = 0
    current: str | None = None
    old: str | None = None
    in_header = False
    prev_sign = ""  # sign of the previous hunk line: "+", "-", or "" for context
    RUN_STARTS.clear()  # module state: never carry one diff's runs into the next
    for f in files:
        RUN_STARTS.setdefault(("+", f), set())
        RUN_STARTS.setdefault(("-", f), set())
    for line in _git_diff(scope_args).splitlines():
        if line.startswith("diff --git "):
            in_header, current, old, prev_sign = True, None, None, ""
        elif in_header:
            if line.startswith("@@"):
                in_header = False
            elif line.startswith("--- "):
                old = _header_path(line, "a/")
            elif line.startswith("+++ "):
                # A deleted file's new side is /dev/null: attribute to the old path.
                current = _header_path(line, "b/") or old
        elif line.startswith("@@"):
            prev_sign = ""
        elif line.startswith(("+", "-")):
            # Fail closed: a hunk line whose file could not be named still
            # counts, under a placeholder no skip pattern matches.
            target = current if current is not None else UNKNOWN_PATH
            if target not in files:
                files.append(target)
            bucket = added if line[0] == "+" else deleted
            lines_so_far = bucket.setdefault(target, [])
            if prev_sign != line[0]:
                RUN_STARTS.setdefault((line[0], target), set()).add(len(lines_so_far))
            lines_so_far.append(line[1:])
            prev_sign = line[0]
            loc += 1
        else:
            prev_sign = ""  # context line (or `\ No newline`) ends a run
    return files, added, loc, deleted


MIGRATION_RE = re.compile(r"(?:^|/)drizzle/\d+[^/]*\.sql$")


def _sibling_migration_lines(exclude: set[str]) -> set[str]:
    """Every stripped line already present in some OTHER committed migration."""
    corpus: set[str] = set()
    drizzle = Path.cwd() / "drizzle"
    if not drizzle.is_dir():
        return corpus
    for path in drizzle.glob("*.sql"):
        rel = f"drizzle/{path.name}"
        if rel in exclude:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        corpus.update(line.strip() for line in text.splitlines() if line.strip())
    return corpus


def _comment_mask(path: str, lines: list[str], run_starts: set[int] | None = None) -> list[bool]:
    """Per line: True if it is blank or comment-only for the file's language.

    A line counts as a comment only when NOTHING executable is on it, so a
    trailing comment after code is code. For C-style languages this tracks
    block-comment state across the given lines instead of trusting a prefix:
    `* evil()` outside a block is a multiplication continuation or a pointer
    write, `*[Symbol.iterator]()` a generator, `/* x */ evil()` code after a
    comment — prefix matching discounted all three. A block opened in unchanged
    context is not seen, so its body counts as code: that errs toward review.

    Accepted limit: this reads lines, not a parse tree, so a deliberate payload
    inside a string whose lines begin with `#` or `//` would be discounted. The
    gate reviews our own changes; it is not an adversarial boundary.
    """
    hash_lang = bool(_HASH_RE.search(path))
    slash_lang = bool(_SLASH_RE.search(path))
    sql = bool(_SQL_RE.search(path))
    mask: list[bool] = []
    in_block = False
    block_start = 0

    def uncomment_open_block(end: int) -> None:
        # The block never closed inside its run: its `*/` is in unchanged
        # context, so what follows the opening is unknown. Count it as code
        # rather than mask it (edit the first line of an existing doc comment
        # and every later added line vanished).
        mask[block_start:end] = [not ln.strip() for ln in lines[block_start:end]]

    for i, line in enumerate(lines):
        if in_block and run_starts and i in run_starts:
            uncomment_open_block(i)
            in_block = False
        s = line.strip()
        if not s:
            mask.append(True)
        elif slash_lang:
            if in_block:
                close = s.find("*/")
                if close >= 0:
                    in_block = False
                    mask.append(not s[close + 2 :].strip())
                else:
                    mask.append(True)
            elif s.startswith("//"):
                mask.append(True)
            elif s.startswith("/*"):
                close = s.find("*/", 2)
                if close < 0:
                    in_block, block_start = True, len(mask)
                    mask.append(True)
                else:
                    mask.append(not s[close + 2 :].strip())
            else:
                mask.append(False)
        elif hash_lang:
            mask.append(s.startswith("#") and not s.startswith("#!"))  # a shebang is code
        elif sql:
            mask.append(s.startswith("--"))
        else:
            mask.append(False)
    if in_block:
        uncomment_open_block(len(lines))
    return mask


def loc_breakdown(
    added: dict[str, list[str]], raw_loc: int, deleted: dict[str, list[str]] | None = None
) -> dict[str, int]:
    """raw_loc minus the lines no reviewer needs to read, itemised.

      migration_copies — added `drizzle/NNNN_*.sql` lines copied verbatim from a
        SIBLING migration. This repo supersedes a migration by reproducing it
        (0014 is 0013 plus two lines); counted naively that escalated a ten-line
        fix to Full. Never discounted against the file itself, so a genuinely new
        migration still counts in full.
      comments_blank — blank or comment-only lines, added or deleted (see
        _comment_mask). One real line among 200 comments still counts as one.
      prose — lines in .md/.rst/.adoc files, added or deleted.
      lockfile — lines in a lockfile (LOCKFILE_PATTERNS), added or deleted:
        machine-written, so 5,000 of them beside 5 lines of code must not
        make the diff Full. The lockfile itself still lifts Skip to Light.

    Deleted CODE is never discounted: a removed line is real work regardless of
    where the text also appears. `effective` is the number the tier is judged
    on and is what the JSON `loc` field reports; `raw` is `raw_loc`.
    """
    deleted = deleted or {}
    migration_files = {f for f in added if MIGRATION_RE.search(f)}
    corpus = _sibling_migration_lines(exclude=migration_files) if migration_files else set()
    copies = comments = prose = lockfile = 0
    for path in set(added) | set(deleted):
        adds, dels = added.get(path, []), deleted.get(path, [])
        if PROSE_RE.search(path):
            prose += len(adds) + len(dels)
            continue
        if _matches(path, LOCKFILE_PATTERNS):
            lockfile += len(adds) + len(dels)
            continue
        add_mask = _comment_mask(path, adds, RUN_STARTS.get(("+", path)))
        for line, is_comment in zip(adds, add_mask):
            if path in migration_files and line.strip() and line.strip() in corpus:
                copies += 1
            elif is_comment:
                comments += 1
        comments += sum(_comment_mask(path, dels, RUN_STARTS.get(("-", path))))
    effective = max(0, raw_loc - copies - comments - prose - lockfile)
    return {
        "raw": raw_loc,
        "migration_copies": copies,
        "comments_blank": comments,
        "prose": prose,
        "lockfile": lockfile,
        "effective": effective,
    }


def effective_loc(
    added: dict[str, list[str]], raw_loc: int, deleted: dict[str, list[str]] | None = None
) -> int:
    """The LOC the tier is judged on — see loc_breakdown."""
    return loc_breakdown(added, raw_loc, deleted)["effective"]


def all_files_skippable(files: list[str]) -> bool:
    """True iff every changed file is config/docs — and none is a light-floor
    file, which a skip pattern can also match (`Taskfile.yml` is .yml,
    `package.json` is .json) but runs or instructs."""
    if not files:
        return True
    return all(_matches(f, SKIP_PATTERNS) and not _matches(f, LIGHT_FLOOR_PATTERNS) for f in files)


def only_capped_and_skippable(files: list[str]) -> bool:
    """True iff every file is build tooling, a lockfile, or plain config/docs,
    and at least one is build tooling or a lockfile. A floor file alongside
    (workflow, package.json, rules .md) lifts the cap: it is judged on LOC."""
    is_cap = [_matches(f, CAP_PATTERNS) for f in files]
    return any(is_cap) and all(c or all_files_skippable([f]) for f, c in zip(files, is_cap))


def ci_workflow_hits(
    files: list[str], added: dict[str, list[str]], deleted: dict[str, list[str]]
) -> list[str]:
    """Return ["ci-workflow"] if a workflow file changes a line — added or
    deleted — touching `permissions:` or a `${{ secrets.* }}` expression."""
    for f in files:
        if not _matches(f, CI_WORKFLOW_PATTERNS):
            continue
        if any(CI_ESCALATE_RE.search(ln) for ln in added.get(f, []) + deleted.get(f, [])):
            return ["ci-workflow"]
    return []


def matched_sensitive_paths(files: list[str], config_repo: bool = False) -> list[str]:
    """Return the unique set of sensitive pattern NAMES that matched any file path."""
    hits: set[str] = set()
    for f in files:
        if any(p.search(f) for p in SENSITIVE_EXEMPT_PATHS):
            continue
        session_start = is_session_start_hook(f, config_repo)
        for name, pattern in SENSITIVE_PATTERNS:
            if pattern.search(f) and not (session_start and name == "session"):
                hits.add(name)
    return sorted(hits)


def credential_content_hits(
    files: list[str],
    added: dict[str, list[str]],
    deleted: dict[str, list[str]],
    config_repo: bool = False,
) -> list[str]:
    """Return ["credential-content"] if a session_start file changes a line
    touching credential plumbing — added or deleted, since removing a
    credential-handling call is the regression this exists to catch."""
    for f in files:
        if not is_session_start_hook(f, config_repo):
            continue
        added_lines, deleted_lines = added.get(f, []), deleted.get(f, [])
        touches_plumbing = any(
            CREDENTIAL_CONTENT_RE.search(ln)
            for ln in added_lines + deleted_lines
            if not _IMPORT_LINE_RE.match(ln)
        )
        imports_changed = _credential_imports(added_lines) != _credential_imports(deleted_lines)
        if touches_plumbing or imports_changed:
            return ["credential-content"]
    return []


def _credential_imports(lines: list[str]) -> Counter[tuple[str, str, str]]:
    """(module, name, alias) for each import item whose module, name or alias
    is credential-shaped, plus every star import.

    An item the parser cannot read counts as credential-shaped when its raw
    text is, so it can only push toward Full."""
    found: Counter[tuple[str, str, str]] = Counter()
    for ln in lines:
        m = _IMPORT_LINE_RE.match(ln)
        if not m:
            continue
        module = m.group(1) or ""
        for raw in m.group(2).strip().strip("()").split(","):
            item = raw.strip()
            parsed = _IMPORT_ITEM_RE.match(item)
            name, alias = (parsed.group(1), parsed.group(2) or "") if parsed else (item, "")
            # The module counts too (`from secrets import choice`), and a star
            # import can shadow any credential helper.
            if name == "*" or CREDENTIAL_CONTENT_RE.search(f"{module} {name} {alias}"):
                found[(module, name, alias)] += 1
    return found


def find_forbidden_markers(added: dict[str, list[str]]) -> list[str]:
    """Return list of 'MARKER in file: <line snippet>' for any forbidden marker hit.

    Only scans files that DON'T match SKIP_PATTERNS — markdown docs, JSON/YAML
    configs, .gitignore etc. legitimately mention 'TODO' as prose. Forbidden
    markers apply to code, not documentation about code.

    Also skips MARKER_SCAN_EXEMPT files — the scanner's own source and its
    test files, which legitimately contain marker strings as data.
    """
    hits: list[str] = []
    for file, lines in added.items():
        if _matches(file, LOCKFILE_PATTERNS):
            continue  # machine-written; a dependency named "todo" is not a marker
        if not _matches(file, BUILD_PATTERNS) and _matches(file, SKIP_PATTERNS):
            continue  # docs/config — skip marker scan (build files, e.g. Taskfile.yml, stay in)
        if _matches(file, MARKER_SCAN_EXEMPT):
            continue  # scanner's own source/tests — marker strings are data
        for line in lines:
            for marker in FORBIDDEN_MARKERS:
                # A marker is a note-to-self, and in practice is always written
                # as `# TODO` / `// TODO` / `TODO:`. A BARE occurrence of the
                # word is something else entirely: an identifier, a constant, or
                # drawn UI text. `TEMP` can be a display label and `FIX` is a
                # substring of ordinary English, so `\bTEMP\b` once blocked a
                # legitimate test assertion on `("CPU", "41%", "TEMP", "57C", ...)`.
                #
                # Requiring the comment prefix or the trailing colon keeps every
                # real marker -- none of them are written bare -- and drops the
                # whole false-positive class.
                if re.search(rf"(#\s*|//\s*){marker}\b|\b{marker}:", line):
                    hits.append(f"{marker} in {file}: {line.strip()[:80]}")
                    break  # one finding per line
    return hits


def recommend_tier(
    files: list[str],
    added: dict[str, list[str]],
    loc: int,
    deleted: dict[str, list[str]] | None = None,
    config_repo: bool = False,
) -> dict:
    """Decide tier. Marker BLOCK > sensitive (incl. CI workflow permissions/
    secrets) Full > docs/config Skip > build-tooling Light (a cap) >
    zero-effective-LOC Light-if-floor-else-Skip > LOC threshold.

    Sensitivity is judged on non-prose paths only (see PROSE_RE).
    `config_repo` (is_config_repo()) enables the root session_start layout.

    Return schema is stable across all tiers — `markers` and `sensitive_patterns`
    are always present (possibly empty lists), so JSON consumers can rely on a
    single shape instead of probing for keys.
    """
    base = {
        "loc": loc,
        "files": len(files),
        "markers": [],
        "sensitive_patterns": [],
    }
    markers = find_forbidden_markers(added)
    if markers:
        return {
            **base,
            "tier": "BLOCK",
            "reason": f"{len(markers)} forbidden marker(s) found — resolve before any gate",
            "markers": markers[:10],
        }

    # Before the config/docs Skip, so config/secrets.yaml cannot Skip; over
    # non-prose paths, so docs/security.md never escalates.
    sensitive = matched_sensitive_paths(
        [f for f in files if not PROSE_RE.search(f)], config_repo
    )
    sensitive += credential_content_hits(files, added, deleted or {}, config_repo)
    sensitive += ci_workflow_hits(files, added, deleted or {})
    if sensitive:
        return {
            **base,
            "tier": "Full",
            "reason": f"Sensitive paths or content detected ({', '.join(sensitive)}) — auto-escalate",
            "sensitive_patterns": sensitive,
        }

    if all_files_skippable(files):
        return {
            **base,
            "tier": "Skip",
            "reason": "All changed files match config/docs patterns — no testable code",
        }

    if only_capped_and_skippable(files):
        return {
            **base,
            "tier": "Light",
            "reason": "Only build tooling or lockfiles (plus docs/config) changed — capped at Light",
        }

    if loc == 0 and any(_matches(f, LIGHT_FLOOR_PATTERNS) for f in files):
        return {
            **base,
            "tier": "Light",
            "reason": "No effective LOC, but executable config or instruction files changed — never Skip",
        }

    if loc == 0:
        return {
            **base,
            "tier": "Skip",
            "reason": "Only comments, blank lines, prose or copied migration lines changed",
        }

    if loc < LIGHT_THRESHOLD:
        return {
            **base,
            "tier": "Light",
            "reason": f"{loc} LOC changed (< {LIGHT_THRESHOLD}), no sensitive paths",
        }

    return {
        **base,
        "tier": "Full",
        "reason": f"{loc} LOC changed (≥ {LIGHT_THRESHOLD})",
    }


def print_human(result: dict) -> None:
    icons = {"Skip": "⏭️ ", "Light": "🟡", "Full": "🔴", "BLOCK": "🛑"}
    icon = icons.get(result["tier"], "❓")
    print(f"{icon}  Tier: {result['tier']}")
    print(f"   {result['reason']}")
    loc_line = f"   Files: {result['files']}, LOC: {result['loc']}"
    b = result.get("loc_breakdown")
    if b and b["raw"] != b["effective"]:
        parts = [
            f"{b[k]} {label}"
            for k, label in (
                ("comments_blank", "comment/blank"),
                ("prose", "prose"),
                ("lockfile", "lockfile"),
                ("migration_copies", "copied from a sibling migration"),
            )
            if b.get(k)
        ]
        loc_line += f" ({b['raw']} raw; not counted: {', '.join(parts)})"
    print(loc_line)
    if result.get("markers"):
        print("   Markers:")
        for m in result["markers"]:
            print(f"     - {m}")
    if result.get("sensitive_patterns"):
        print(f"   Sensitive: {', '.join(result['sensitive_patterns'])}")


def repo_from_url(url: str) -> str | None:
    """Repo name from a remote URL (https, ssh or scp form), else None."""
    name = url.strip().rstrip("/").rsplit("/", 1)[-1].rsplit(":", 1)[-1]
    name = name.removesuffix(".git")
    return name or None


def _git(*args: str) -> str | None:
    try:
        proc = subprocess.run(
            ["git", *args], capture_output=True, text=True, timeout=10
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return proc.stdout.strip() if proc.returncode == 0 else None


def is_config_repo() -> bool:
    """True in this kit's own checkout (origin named KIT_REPO) or in the
    checkout CRAFT_CONFIG_REPO names; False outside git."""
    url = _git("remote", "get-url", "origin")
    if url and repo_from_url(url) == KIT_REPO:
        return True
    configured = os.environ.get("CRAFT_CONFIG_REPO")
    toplevel = _git("rev-parse", "--show-toplevel")
    if not (configured and toplevel):
        return False
    return Path(os.path.expanduser(configured)).resolve() == Path(toplevel).resolve()


def main() -> None:
    args = [a for a in sys.argv[1:] if a != "--json"]
    json_mode = "--json" in sys.argv
    scope = args[0] if args else "HEAD"

    files, added, raw_loc, deleted = get_diff(scope)
    breakdown = loc_breakdown(added, raw_loc, deleted)
    # is_config_repo() costs two git calls, and only the root layout reads it.
    config_repo = any(map(_is_root_session_start_hook, files)) and is_config_repo()
    result = recommend_tier(files, added, breakdown["effective"], deleted, config_repo)
    result["raw_loc"] = raw_loc
    result["loc_breakdown"] = breakdown

    if json_mode:
        print(json.dumps(result, indent=2))
    else:
        print_human(result)

    sys.exit(2 if result["tier"] == "BLOCK" else 0)


if __name__ == "__main__":
    main()
