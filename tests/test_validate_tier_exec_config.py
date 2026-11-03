"""Executable config, CI workflows and instruction prose never tier Skip.

One defect, two shapes: a file whose NAME matches no sensitive pattern but
whose content runs, or is loaded as instructions every session, got zero
reviewers.
  - An 87-line shell + Python exemption added to a required merge gate in
    `.github/workflows/claude-review.yml` scored Skip; a manual review then
    found 3 bypasses.
  - `settings.json` (hook commands, `permissions.allow`),
    `package.json` scripts, MCP spawn templates, compose files, the gitleaks
    allowlist and dependency pins all Skipped — and so did removing the push
    gate from `rules/operations.md`.

Invariants: plain config and plain docs still Skip; a floor file never Skips,
alone or beside code; code alongside it is still judged on LOC; a workflow diff
touching `permissions:` or a secret goes Full.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "hooks"))

import validate_tier as vt  # noqa: E402

EXEC_CONFIG = [
    ".github/workflows/claude-review.yml",
    ".forgejo/workflows/lint-unit.yml",
    ".gitea/workflows/ci.yaml",
    ".gitlab-ci.yml",
    "settings.json",
    ".claude/settings.local.json",
    "package.json",
    "functions/package.json",
    "mcp/servers.template.json",
    ".mcp.json",
    "docker-compose.yml",
    "deploy/compose.yaml",
    "docker-compose.override.yml",
    ".gitleaks.toml",
    "requirements.txt",
    "requirements-dev.txt",
    # gate round 1 (bug-hunter, security-auditor):
    ".github/actions/setup/action.yml",
    ".pre-commit-config.yaml",
    ".woodpecker.yml",
    ".woodpecker/ci.yaml",
    ".claude/hooks/hooks.json",
    "pyproject.toml",
]

INSTRUCTIONS = [
    "CLAUDE.md",
    "CLAUDE.md",
    "AGENTS.md",
    "skills/wrap-up/SKILL.md",
    "rules/operations.md",
    "agents/reviewer.md",
    "commands/clear-pending.md",
    ".claude/rules/x.md",
    "skills/wrap-up/phases.md",
]


@pytest.mark.parametrize("path", EXEC_CONFIG + INSTRUCTIONS)
def test_floor_file_alone_is_light_not_skip(path: str) -> None:
    res = vt.recommend_tier([path], {path: ["x: 1"]}, 1)
    assert res["tier"] == "Light", res["reason"]


@pytest.mark.parametrize("path", EXEC_CONFIG + INSTRUCTIONS)
def test_floor_file_plus_docs_is_still_light(path: str) -> None:
    files = [path, "docs/notes.md"]
    res = vt.recommend_tier(files, {path: ["x: 1"], "docs/notes.md": ["y"]}, 1)
    assert res["tier"] == "Light"


@pytest.mark.parametrize("path", INSTRUCTIONS)
def test_instruction_prose_beside_a_comment_only_code_change_is_light(path: str) -> None:
    # Prose and comments both count 0 effective LOC; the floor must still hold.
    files = [path, "src/a.py"]
    res = vt.recommend_tier(files, {path: ["- rule"], "src/a.py": ["# note"]}, 0)
    assert res["tier"] == "Light"


@pytest.mark.parametrize(
    "path",
    [
        "config/app.yaml",
        "tsconfig.json",
        "data/fixtures.json",
        "docs/guide.md",
        "README.md",
        "notes.txt",
        "src/settings.json.md",
        "docs/rules/operations.md",
        "myCLAUDE.md",
    ],
)
def test_plain_config_and_docs_still_skip(path: str) -> None:
    assert vt.recommend_tier([path], {path: ["x"]}, 1)["tier"] == "Skip"


@pytest.mark.parametrize("path", [".github/workflows/claude-review.yml", "settings.json"])
def test_large_exec_config_alone_is_judged_on_loc_not_capped(path: str) -> None:
    # The motivating case: 87 lines of shell + Python in a merge gate.
    assert vt.recommend_tier([path], {path: ["  run: x"] * 87}, 87)["tier"] == "Full"


@pytest.mark.parametrize(
    "path",
    ["docs/main.go", "docs/lib.rs", "README_ui.tsx", "docs/x.sql", "docs/main.tf", "docs/Run.SH", "docs/bin/run"],
)
def test_more_code_extensions_under_docs_are_not_skipped(path: str) -> None:
    assert not vt.all_files_skippable([path])


# ── gate round 1 ──────────────────────────────────────────────────────────────
@pytest.mark.parametrize("floor", [".github/workflows/ci.yml", "package.json", "settings.json"])
def test_a_build_file_does_not_cap_a_large_floor_file(floor: str) -> None:
    files = ["Makefile", floor]
    added = {"Makefile": ["\tx"], floor: ["  run: x"] * 200}
    assert vt.recommend_tier(files, added, 201)["tier"] == "Full"


@pytest.mark.parametrize("path", ["package-lock.json", "uv.lock", "yarn.lock", "sub/Cargo.lock", "pnpm-lock.yaml"])
def test_a_lockfile_alone_is_capped_at_light(path: str) -> None:
    assert vt.recommend_tier([path], {path: ['"x": 1'] * 3000}, 3000)["tier"] == "Light"


def test_a_lockfile_beside_code_is_judged_on_the_code() -> None:
    files = ["package-lock.json", "src/a.ts"]
    assert vt.recommend_tier(files, {files[0]: ["x"], files[1]: ["y()"] * 40}, 41)["tier"] == "Full"


def test_lockfile_lines_are_not_effective_loc() -> None:
    # gate round 2: a 5,000-line `npm audit fix` beside 5 lines of code is Light.
    added = {"package-lock.json": ['"x": 1'] * 5000, "src/a.py": ["x = 1"] * 5}
    b = vt.loc_breakdown(added, 5005)
    assert b["lockfile"] == 5000 and b["effective"] == 5
    assert vt.recommend_tier(list(added), added, b["effective"])["tier"] == "Light"


@pytest.mark.parametrize("path", ["src/evil.lock", "data.lock", "composer.json"])
def test_unnamed_lock_and_composer_json_are_not_capped(path: str) -> None:
    # gate round 2: `*.lock` was a wildcard; composer.json is a manifest with scripts.
    assert vt.recommend_tier([path], {path: ["x"] * 40}, 40)["tier"] == "Full"


def test_lockfiles_are_not_marker_scanned() -> None:
    assert vt.recommend_tier(["uv.lock"], {"uv.lock": ["# TODO: x"]}, 1)["tier"] == "Light"


def test_a_comment_only_workflow_edit_is_light_not_skip() -> None:
    path = ".forgejo/workflows/ci.yml"
    assert vt.recommend_tier([path], {path: ["# note"]}, 0)["tier"] == "Light"


@pytest.mark.parametrize(
    "line",
    [
        "    secrets: inherit",
        "  token: ${{ secrets['TOK'] }}",
        "  run: echo '${{ toJSON(secrets) }}'",
        "      contents: write",
        '      contents: "write"',
        "      contents: write  # needed for releases",
        "  packages: write-all",
        "      id-token: write",
        "on: pull_request_target",
        "  token: ${{ github.token }}",
    ],
)
def test_more_workflow_privilege_lines_go_full(line: str) -> None:
    path = ".github/workflows/ci.yml"
    assert vt.recommend_tier([path], {path: [line]}, 1)["tier"] == "Full"


@pytest.mark.parametrize("line", ["  run: make test", "      contents: read", "  mode: writer", "  name: rewrite docs"])
def test_ordinary_workflow_lines_stay_light(line: str) -> None:
    path = ".github/workflows/ci.yml"
    assert vt.recommend_tier([path], {path: [line]}, 1)["tier"] == "Light"


def test_large_workflow_beside_code_is_judged_on_loc() -> None:
    files = [".forgejo/workflows/ci.yml", "src/a.py"]
    added = {files[0]: ["run: make test"] * 40, files[1]: ["x = 1"]}
    assert vt.recommend_tier(files, added, 41)["tier"] == "Full"


@pytest.mark.parametrize(
    "line",
    [
        "permissions:",
        "    permissions: write-all",
        "  token: ${{ secrets.DEPLOY_TOKEN }}",
        "  token: ${{secrets.X}}",
    ],
)
def test_workflow_touching_permissions_or_secrets_goes_full(line: str) -> None:
    path = ".github/workflows/ci.yml"
    res = vt.recommend_tier([path], {path: [line]}, 1)
    assert res["tier"] == "Full"
    assert "ci-workflow" in res["sensitive_patterns"]


def test_deleting_a_workflow_permissions_block_goes_full() -> None:
    path = ".forgejo/workflows/ci.yml"
    res = vt.recommend_tier([path], {path: []}, 1, {path: ["permissions:", "  contents: read"]})
    assert res["tier"] == "Full"


def test_permissions_in_a_non_workflow_yaml_does_not_escalate() -> None:
    res = vt.recommend_tier(["config/app.yaml"], {"config/app.yaml": ["permissions:"]}, 1)
    assert res["tier"] == "Skip"


def test_markers_are_not_scanned_in_exec_config() -> None:
    # package.json descriptions and workflow comments mention TODO as prose;
    # only build files carry the marker scan into config extensions.
    res = vt.recommend_tier(["package.json"], {"package.json": ['"x": "TODO: later"']}, 1)
    assert res["tier"] == "Light"


@pytest.mark.parametrize("path", ["docs/run.sh", "docs/tools/gen.py", "README_install.sh", "pkg/README_setup.js"])
def test_code_under_docs_or_readme_names_is_not_skipped(path: str) -> None:
    assert not vt.all_files_skippable([path])
    assert vt.recommend_tier([path], {path: ["echo hi"]}, 1)["tier"] == "Light"


@pytest.mark.parametrize("path", ["docs/guide.md", "docs/diagram.mmd", "README", "README.rst", "pkg/README.txt"])
def test_docs_and_readme_prose_still_skip(path: str) -> None:
    assert vt.all_files_skippable([path])
