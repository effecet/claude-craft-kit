# npm cache EPERM workaround
# ~/.claude/rules/npm-cache-eperm.md

When any `npm` command fails with `EPERM ... ~/.npm/_cacache/tmp/...`:

```
npm error code EPERM
npm error syscall open
npm error path /Users/<you>/.npm/_cacache/tmp/eef26d84
npm error To permanently fix this problem, please run:
npm error   sudo chown -R <uid>:<gid> "/Users/<you>/.npm"
```

This is **not a sandbox issue** — it's old root-owned files from older npm
versions sitting in `~/.npm/_cacache/`. Real fix needs `sudo chown` (user
action only — don't try via Bash).

## In-session workaround (no sudo)

Point npm at a fresh, writable cache directory in a temp location:

```bash
# Portable: ${TMPDIR:-/tmp} falls back to /tmp when TMPDIR is unset (Linux)
npm install --cache "${TMPDIR:-/tmp}/npm-cache-$$"
npm audit --cache "${TMPDIR:-/tmp}/npm-cache-$$"
npx --cache "${TMPDIR:-/tmp}/npm-cache-$$" <command>
```

`$$` is the current PID — gives a unique dir per invocation. No permission
errors, no sudo, no system changes.

**Linux caveat:** `$TMPDIR` is a macOS convention and is usually **unset on
Linux**. A bare `"$TMPDIR/npm-cache-$$"` then expands to `/npm-cache-$$` — a
root-owned path at filesystem root — and npm fails with the same EPERM/`mkdir`
error you were trying to avoid. Always use the `${TMPDIR:-/tmp}` form above, or
pass an explicit writable dir. The caveat is not npm-specific; see
`rules/development.md` § Shell — `$TMPDIR` is not guaranteed.

Applies to: `npm install`, `npm audit`, `npm audit fix`, `npm view`, `npx`,
and any other command that writes to the cache.

## When to suggest the permanent fix

If you hit this 2+ times in a session, surface the `sudo chown -R <uid>:<gid> ~/.npm`
command to the user once and move on with the workaround. Don't keep
re-suggesting it.

## Why not `npm config set cache <path>`

That writes to `~/.npmrc` and changes the default for ALL future npm
invocations — too invasive. `--cache` is per-command and ephemeral.

Origin: a session hit EPERM on every single `npm install` and `npm audit`
until switching to `--cache "$TMPDIR/..."`. Same workaround applied across
all npm/npx invocations in that session.
