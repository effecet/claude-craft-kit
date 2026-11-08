"""
~/.claude/hooks/shell_words.py

The shell-command parser used by the PreToolUse:Bash guard
hook_bypass_guard.py. It turns a Bash command into
commands split on `;`, `&&`, `||`, `|`, `&`, `(`, `)` and newlines, and finds
the `git` subcommand in each.

Handled: backslash-newline, `bash -c` bodies (walked inline), wrapper words
(`env`, `sudo -u x`, `nice -n 5`, ...), `VAR=value` prefixes, `#` comments
and heredoc bodies (dropped: a commit message is data). A quote-aware
pre-pass keeps quoted or `$((...))` text from reading as an operator, comment
or heredoc marker. Not seen: `eval`, shell functions or aliases, variables
holding the command, and quotes nested inside `$(...)` inside double quotes
(`"$(echo ")")"`). Not a full shell parser: the guards built on it are
backstops against accidents, not against deliberate evasion.

Every Bash call runs a guard that imports this, so it imports only stdlib
modules, lazily where they are not needed on every call.
"""

from __future__ import annotations

SEPARATORS = frozenset({";", "&&", "||", "|", "&", "(", ")"})
GIT_OPTS_WITH_VALUE = frozenset(
    {
        "-C",
        "-c",
        "--git-dir",
        "--work-tree",
        "--namespace",
        "--exec-path",
        "--config-env",
    }
)
# Wrapper words, each with its options that take a separate value. Per
# wrapper, because the same letter differs: `nice -n 5` vs `sudo -n`.
WRAPPERS: dict[str, frozenset[str]] = {
    "env": frozenset({"-u", "-C", "-S"}),
    "sudo": frozenset({"-u", "-g", "-C", "-D", "-h", "-p", "-U"}),
    "nice": frozenset({"-n"}),
    "command": frozenset(),
    "builtin": frozenset(),
    "time": frozenset(),
    "nohup": frozenset(),
    "exec": frozenset({"-a"}),
}
# Shell-special characters, and private-use stand-ins for them when they are
# quoted data rather than syntax. Written as escapes: the raw characters are
# invisible, and an editor or a copy that drops them breaks every guard.
_STAND_INS = ""
MASK = str.maketrans(";&|()#<", _STAND_INS)
UNMASK = str.maketrans(_STAND_INS, ";&|()#<")
NEWLINE = ""  # a line end: closes a comment, unlike `;`
SHELL_KEYWORDS = frozenset(
    {"then", "do", "else", "elif", "if", "while", "until", "{", "!"}
)
SHELLS = frozenset({"bash", "sh", "zsh"})


def _prepare(text: str) -> str:
    """Mask characters that are data, not syntax, and drop heredoc bodies.

    Inside quotes and `$((...))`, `; & | ( ) # <` are swapped for private-use
    stand-ins so the lexer cannot read them as operators, comments or heredoc
    markers. A heredoc marker counts only outside quotes and only if its
    closing line exists. Its body is dropped, so an apostrophe in a commit
    message cannot unbalance the lexer.
    """
    import re

    marker_re = re.compile(r"<<-?\s*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\1")
    lines = text.split("\n")
    out: list[str] = []
    quote, subst, arith = "", False, 0
    pending: list[str] = []
    for n, line in enumerate(lines):
        if pending:
            if line.strip() == pending[0]:
                pending.pop(0)
            continue
        buf, markers, i = [], [], 0
        later = {r.strip() for r in lines[n + 1 :]}  # possible closing lines
        while i < len(line):
            c = line[i]
            if quote:
                if c == "\\" and quote == '"' and i + 1 < len(line):
                    buf.append(line[i : i + 2].translate(MASK))
                    i += 2
                    continue
                if c == quote:
                    quote, subst = "", False
                    buf.append(c)
                    i += 1
                    continue
                if quote == '"' and line.startswith("$(", i):
                    subst = True
                # `-m "$(cat <<'EOF' ...)"`: a heredoc inside a command
                # substitution inside double quotes (not a literal "<<EOF").
                m = subst and not line.startswith("<<<", i) and marker_re.match(line, i)
                if m and m.group(2) in later:
                    markers.append(m.group(2))
                    buf.append(m.group(0).translate(MASK))
                    i = m.end()
                    continue
                buf.append(c.translate(MASK))
            elif c == "\\" and i + 1 < len(line):
                buf.append(line[i : i + 2].translate(MASK))
                i += 2
                continue
            elif line.startswith("$((", i):
                arith += 1
                buf.append("$((")
                i += 3
                continue
            elif arith and line.startswith("))", i):
                arith -= 1
                buf.append("))")
                i += 2
                continue
            elif arith:
                buf.append(c.translate(MASK))
            elif c == "&" and (
                line[i - 1 : i] in ("<", ">") or line[i + 1 : i + 2] == ">"
            ):
                buf.append(c.translate(MASK))  # `2>&1`, `&>`: a redirect, not `&`
            elif c in "'\"":
                quote = c
                buf.append(c)
            elif c == "#" and (i == 0 or line[i - 1] in " \t;&|("):
                # A comment: the rest of the line is data (no heredoc markers).
                buf.append("#" + line[i + 1 :].translate(MASK))
                break
            elif line.startswith("<<<", i):
                buf.append("<<<")
                i += 3
                continue
            elif line.startswith("<<", i):
                m = marker_re.match(line, i)
                if m and m.group(2) in later:
                    markers.append(m.group(2))
                    buf.append(m.group(0))
                    i = m.end()
                    continue
                buf.append("<<")
                i += 2
                continue
            else:
                buf.append(c)
            i += 1
        out.append("".join(buf))
        pending = markers
    return "\n".join(out)


def _lex(prepared: str) -> list[str]:
    import shlex

    lex = shlex.shlex(
        prepared.replace("\\\n", " ").replace("\n", f" {NEWLINE} "),
        posix=True,
        punctuation_chars=";&|()",
    )
    lex.whitespace_split = True
    lex.commenters = ""
    return list(lex)


def _tokens(text: str) -> list[str]:
    """Lexed tokens, still masked. `bash -c` bodies are expanded inline."""
    import os

    prepared = _prepare(text)
    try:
        toks = _lex(prepared)
    except ValueError:  # an unbalanced quote: lex line by line instead
        toks = []
        for line in prepared.translate(UNMASK).split("\n"):
            try:
                toks += [*_lex(_prepare(line)), NEWLINE]
            except ValueError:
                toks += [*_prepare(line).split(), NEWLINE]
    out: list[str] = []
    for k, t in enumerate(toks):
        # `bash -c "cd x && git push"`: walk the inner command inline.
        is_shell_c = t == "-c" and k > 0 and os.path.basename(toks[k - 1]) in SHELLS
        if is_shell_c and k + 1 < len(toks):
            out += [";", *_tokens(toks[k + 1].translate(UNMASK)), ";"]
            toks[k + 1] = ";"
            continue
        out.append(t)
    return out


def segments(command: str) -> list[list[str]]:
    """Commands split on separators, with "(" and ")" kept as their own segments.

    shlex lexes a run of operators as one token (`);`, `&&(`), so an
    operator-only token is split into its parts. An unquoted `#` word starts a
    comment that runs to the end of its line.
    """
    import re

    op_run = re.compile(r"(?:&&|\|\||[;&|()])+")
    op = re.compile(r"&&|\|\||[;&|()]")
    out: list[list[str]] = [[]]
    in_comment = False
    for t in _tokens(command):
        parts = op.findall(t) if op_run.fullmatch(t) else [t]
        for part in parts:
            if part == NEWLINE:
                in_comment = False
                out.append([])
            elif in_comment:
                continue
            elif part in SEPARATORS:
                if part in "()":
                    out += [[part], []]
                else:
                    out.append([])
            elif part.startswith("#"):
                in_comment = True
            else:
                out[-1].append(part.translate(UNMASK))
    return [s for s in out if s]


def assignment(word: str) -> tuple[str, str] | None:
    """(name, value) when `word` is a shell `NAME=value` assignment."""
    name, eq, value = word.partition("=")
    return (name, value) if eq and name.isidentifier() else None


def command_words(words: list[str]) -> list[str]:
    """Strip `VAR=value` prefixes, shell keywords and wrapper words (with flags)."""
    i = 0
    while i < len(words):
        w = words[i]
        if w in SHELL_KEYWORDS or assignment(w):
            i += 1
        elif w in WRAPPERS:
            takes_value = WRAPPERS[w]
            i += 1
            while i < len(words) and words[i].startswith("-"):
                i += 2 if words[i] in takes_value else 1
        else:
            break
    return words[i:]


def without_redirects(words: list[str]) -> list[str]:
    """`words` minus shell redirections and their targets.

    The lexer keeps a redirect as an ordinary word (`2>&1`, `>/dev/null`,
    `&>log`), so a guard reading arguments would take it for a path; `> log`
    and `2 > log` spread over two or three words.
    """
    import re

    redirect = re.compile(r"^(?:\d+|&)?(?:>>|>&|<&|>\||&>|[<>])(.*)$")
    out: list[str] = []
    skip_target = False
    for i, w in enumerate(words):
        if skip_target:
            skip_target = False
            continue
        if w.isdigit() and i + 1 < len(words) and redirect.match(words[i + 1]):
            continue  # the fd of `2 > log`
        m = redirect.match(w)
        if m:
            skip_target = not m.group(1)  # `>` alone: the target is the next word
            continue
        out.append(w)
    return out


GitOpt = tuple[str, "str | None"]


def git_subcommand(words: list[str]) -> tuple[list[GitOpt], str, list[str]] | None:
    """(global options, subcommand, its args) when `words` runs git, else None.

    `words` is a command_words() result. Each global option comes with its
    separate value, or None (`--git-dir=x` keeps its value inside the option):
    `git -C repo -p log` → ([("-C", "repo"), ("-p", None)], "log", []).
    """
    import os

    if not words or os.path.basename(words[0]) != "git":
        return None
    opts: list[GitOpt] = []
    i = 1
    while i < len(words) and words[i].startswith("-"):
        if words[i] in GIT_OPTS_WITH_VALUE and i + 1 < len(words):
            opts.append((words[i], words[i + 1]))
            i += 2
        else:
            opts.append((words[i], None))
            i += 1
    sub = words[i] if i < len(words) else ""
    return opts, sub, words[i + 1 :]
