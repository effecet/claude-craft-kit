"""Shared helpers for Claude Code hook scripts.

Keep this module dependency-free (stdlib only) — hook scripts run on a
range of Python builds and any third-party import here would have to be
mirrored in every hook entry point.
"""

from pathlib import Path


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
