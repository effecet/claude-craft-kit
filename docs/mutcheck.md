# mutcheck

Run hand-written mutants against a scoped pytest selection, in a private copy of
the repo, and get a verdict per mutant you can trust.

A mutation check answers "would my test notice if this line were wrong?": break
the code on purpose, run the tests, expect red. Done by hand it has quiet ways to
test nothing. A `sed` that no longer matches leaves the code unmutated. A
baseline that collects 0 tests is "green". A syntax error counts as a kill. A
stale `.pyc` runs the old code. And mutating the working tree under a reviewer
agent (or under you) leaves the tree in a state nobody chose. mutcheck turns each
of those into a loud failure.

## Usage

```bash
~/.claude/tools/mutcheck.py MUTANTS.toml [--module PKG] [--timeout S] \
    [--baseline-timeout S] [--repo DIR] [--python PY] [--keep] -- <pytest args>
```

Run it from the repo (or pass `--repo`). Everything after `--` goes to pytest, so
keep the selection scoped to the tests that should catch the mutants.

```toml
# mutants.toml — keep it outside the repo
[[mutant]]
label = "drop rollback"
file  = "pkg/db.py"                 # relative to the repo root
old   = 'con.execute("ROLLBACK")'
new   = 'con.execute("COMMIT")'
count = 1                           # optional: occurrences of `old` that must exist
```

Use TOML literal strings (`'...'` or `'''...'''`) so backslashes and quotes land
verbatim; `'''` strings span lines for multi-line mutants.

## What it does

1. Builds a `git archive HEAD` copy in a private temp dir, then overlays your
   staged, unstaged and untracked work. Gitignored files (`.env*`, caches) stay
   out. Your working tree is never written.
2. Validates every mutant before running anything: `old` must occur exactly
   `count` times (default 1), `file` must resolve inside the copy, and an
   unknown key (a typo like `cout`) is rejected.
3. Runs the baseline. It aborts (exit 2) if the baseline collects nothing, is
   red, passes nothing because everything skipped, or times out.
4. Applies each mutant alone, runs the selection with bytecode writing off, and
   restores the file before the next one.

## Verdicts

| Verdict | Meaning |
|---|---|
| `KILLED` | The selection went red on an assertion. The first `E` line is shown. |
| `SURVIVED` | The selection stayed green. Either the tests miss it, or the mutant is equivalent; judge which. |
| `ERRORED` | Import, syntax or collection error, or a timeout. Not a kill: the test never got to judge the behaviour. |

Exit codes: `0` all killed, `1` any survived or errored, `2` usage, setup or
validation error (nothing was mutated, or the run cannot be trusted).

## Options worth knowing

- `--module PKG` asserts the package under test imports from the copy. Without
  it, an import that resolves to your real tree looks exactly like a set of
  equivalent mutants; mutcheck warns when every mutant survives.
- `--timeout S` is the hang budget per mutant run. `--baseline-timeout S` is the
  baseline's own budget (default: the larger of 600 and `--timeout`), so a short
  hang budget never aborts the run before a mutant executes.
- `--keep` leaves the copy in place and prints its path, for debugging.
- `--python` defaults to `<repo>/.venv/bin/python` when it exists, else the
  interpreter running mutcheck. A path containing `/` is resolved where you
  run mutcheck; a bare name is looked up on `PATH`. `--timeout` defaults to 600.
- Keep pytest at `-q` at most: `-qq` (say, `-q` in `addopts` plus one in the
  args) hides the summary line mutcheck reads, and the baseline aborts.
- `--tb=line`, `--tb=no` and `--tb=native` are refused: they print no `E`
  lines, so an import error could not be told from a kill.

## Limits

- pytest only.
- The copy has no `.git`, so a suite that shells out to `git` cannot run there.
  Hand-roll the loop for those (`git archive` + `git init -q` in the copy).
- Needs Python 3.11+ (`tomllib`), unlike the hooks (3.10+). Run it with a
  3.11+ interpreter if your `python3` is older.
- The copy is a `git archive`, so `.gitattributes` `export-ignore` and
  `export-subst` apply: an export-ignored file is missing from the copy.
- A tracked file that became a directory in your worktree is not supported
  (setup aborts).
