"""Shared helpers for Claude Code hook scripts.

Keep this module dependency-free (stdlib only) — hook scripts run on a
range of Python builds and any third-party import here would have to be
mirrored in every hook entry point.
"""

import os
from pathlib import Path


def _read_database_url(path: Path) -> str | None:
    """Pull a DATABASE_URL assignment out of a dotenv-style file.

    Catches UnicodeDecodeError alongside OSError: a dotenv file that isn't
    valid UTF-8 would otherwise raise straight through and break the caller's
    "never raises" contract.
    """
    try:
        for line in path.read_text().splitlines():
            line = line.strip()
            if line.startswith("DATABASE_URL="):
                return line.split("=", 1)[1].strip().strip("\"'") or None
    except (OSError, UnicodeDecodeError):
        return None
    return None


def resolve_connection_string(repo_dir: Path | None = None) -> str | None:
    """Resolve a Postgres URL for the optional memory backend.

    Order: DATABASE_URL env var -> the file named by DOTENV_CONFIG_PATH (the
    same file the MCP server loads) -> `.env` in `repo_dir`, if given ->
    None.

    Returns None rather than raising when nothing resolves: every caller is
    expected to degrade to a non-database code path, never to fail the hook.
    """
    env_url = os.environ.get("DATABASE_URL", "").strip()
    if env_url:
        return env_url

    dotenv_path = os.environ.get("DOTENV_CONFIG_PATH")
    if dotenv_path:
        url = _read_database_url(Path(dotenv_path).expanduser())
        if url:
            return url

    if repo_dir:
        url = _read_database_url(repo_dir / ".env")
        if url:
            return url

    return None


def encode_project_path(source: str | Path) -> str:
    """Encode a filesystem path to Claude Code's project directory name.

    Claude Code replaces BOTH path separators and dots with dashes, so
    `/home/user.name` maps to `-home-user-name`, not `-home-user.name`.
    Missing the dot->dash step previously caused a path-derived hook to
    scan a stale project dir for weeks.

    Mirrors `src/file-sync.ts :: encodeProjectPath`. Update both in
    lockstep — `tests/test_common.py` enforces the canonical example.

    Examples:
        >>> encode_project_path("/home/foo.bar/baz")
        '-home-foo-bar-baz'
        >>> encode_project_path(Path("/home/user"))
        '-home-user'
    """
    return str(source).replace("/", "-").replace(".", "-")
