import os
import sys
import glob
import subprocess
import readline  # Windows: pip install pyreadline3
import contextlib
import shutil
from dataclasses import dataclass, field
from typing import List, Dict, Optional, Callable, Tuple

# --- Globals ---
ALIASES: Dict[str, str] = {}
HISTORY_FILE = os.path.expanduser("~/.custom_shell_history")
BACKGROUND_JOBS: List[subprocess.Popen] = []

# --- Builtins ---
def builtin_echo(args: List[str], stdout=sys.stdout, stderr=sys.stderr):
    print(" ".join(args), file=stdout)
    return 0

def builtin_pwd(args: List[str], stdout=sys.stdout, stderr=sys.stderr):
    if args:
        print("pwd: too many arguments", file=stderr)
        return 1
    print(os.getcwd(), file=stdout)
    return 0

def builtin_cd(args: List[str], stdout=sys.stdout, stderr=sys.stderr):
    try:
        path = args[0] if args else os.getenv("HOME", "")
        os.chdir(os.path.expanduser(path))
    except Exception as e:
        print(f"cd: {e}", file=stderr)
        return 1
    return 0

def builtin_exit(args: List[str], stdout=sys.stdout, stderr=sys.stderr):
    save_history()
    code = int(args[0]) if args else 0
    sys.exit(code)

def builtin_alias(args: List[str], stdout=sys.stdout, stderr=sys.stderr):
    if not args:
        for k, v in ALIASES.items():
            print(f"alias {k}='{v}'", file=stdout)
        return 0
    for arg in args:
        if '=' in arg:
            k, v = arg.split('=', 1)
            ALIASES[k] = v
        else:
            if arg in ALIASES:
                print(f"alias {arg}='{ALIASES[arg]}'", file=stdout)
            else:
                print(f"alias: {arg}: not found", file=stderr)
                return 1
    return 0

def builtin_type(args: List[str], stdout=sys.stdout, stderr=sys.stderr):
    if not args:
        print("type: missing argument", file=stderr)
        return 1
    name = args[0]
    if name in ALIASES:
        print(f"{name} is an alias for '{ALIASES[name]}'", file=stdout)
    elif name in BUILTIN_COMMANDS:
        print(f"{name} is a shell builtin", file=stdout)
    else:
        exe = shutil.which(name)
        if exe:
            print(f"{name} is {exe}", file=stdout)
        else:
            print(f"{name}: not found", file=stderr)
            return 1
    return 0

BUILTIN_COMMANDS: Dict[str, Callable] = {
    "echo": builtin_echo,
    "pwd": builtin_pwd,
    "cd": builtin_cd,
    "alias": builtin_alias,
    "exit": builtin_exit,
    "type": builtin_type
}

# --- Lexer ---
class ShellSyntaxError(Exception):
    pass

# Token kinds
WORD, PIPE, AND, OR, SEMI, BG, REDIR = "WORD", "PIPE", "AND", "OR", "SEMI", "BG", "REDIR"

# Longest operators first so "&>>" wins over "&>" and "&".
OPERATORS: List[Tuple[str, str]] = [
    ("&>>", REDIR), ("2>>", REDIR), ("1>>", REDIR),
    ("&&", AND), ("||", OR), ("&>", REDIR), ("2>", REDIR), ("1>", REDIR),
    (">>", REDIR), ("|", PIPE), (";", SEMI), ("&", BG), (">", REDIR),
]
# Operators that start with a digit only count at the start of a word.
FD_OPERATORS = ("2>>", "1>>", "2>", "1>")

@dataclass
class Token:
    kind: str
    # Each part is (text, mode). Mode is "plain", "single" or "double".
    parts: List[Tuple[str, str]] = field(default_factory=list)

    @property
    def text(self) -> str:
        return "".join(p[0] for p in self.parts)

    @property
    def quoted(self) -> bool:
        return any(mode != "plain" for _, mode in self.parts)

def lex(line: str) -> List[Token]:
    tokens: List[Token] = []
    parts: List[Tuple[str, str]] = []
    buf = ""
    mode = "plain"
    has_word = False
    i, n = 0, len(line)

    def flush_buf():
        nonlocal buf
        if buf:
            parts.append((buf, mode))
            buf = ""

    def end_word():
        nonlocal parts, has_word
        flush_buf()
        if has_word:
            tokens.append(Token(WORD, parts))
        parts = []
        has_word = False

    while i < n:
        c = line[i]
        if mode == "single":
            if c == "'":
                flush_buf()
                mode = "plain"
            else:
                buf += c
            i += 1
        elif mode == "double":
            if c == '"':
                flush_buf()
                mode = "plain"
                i += 1
            elif c == "\\" and i + 1 < n and line[i + 1] in '"\\$`':
                buf += line[i + 1]
                i += 2
            else:
                buf += c
                i += 1
        else:
            if c == "'" or c == '"':
                flush_buf()
                mode = "single" if c == "'" else "double"
                has_word = True
                i += 1
            elif c == "\\":
                flush_buf()
                if i + 1 < n:
                    parts.append((line[i + 1], "single"))
                    i += 2
                else:
                    parts.append(("\\", "single"))
                    i += 1
                has_word = True
            elif c.isspace():
                end_word()
                i += 1
            else:
                matched = None
                for op, kind in OPERATORS:
                    if not line.startswith(op, i):
                        continue
                    if op in FD_OPERATORS and (has_word or buf):
                        continue
                    matched = (op, kind)
                    break
                if matched:
                    end_word()
                    op, kind = matched
                    tokens.append(Token(kind, [(op, "plain")]))
                    i += len(op)
                else:
                    buf += c
                    has_word = True
                    i += 1

    if mode != "plain":
        raise ShellSyntaxError("unterminated quote")
    end_word()
    return tokens

# --- Expansion ---
def expand_word(tok: Token) -> List[str]:
    value = ""
    pattern = ""
    has_glob = False
    for idx, (text, mode) in enumerate(tok.parts):
        if mode == "single":
            value += text
            pattern += glob.escape(text)
        elif mode == "double":
            t = os.path.expandvars(text)
            value += t
            pattern += glob.escape(t)
        else:
            t = text
            if idx == 0:
                t = os.path.expanduser(t)
            t = os.path.expandvars(t)
            value += t
            pattern += t
            if any(ch in t for ch in "*?["):
                has_glob = True
    if has_glob:
        matches = sorted(glob.glob(pattern))
        if matches:
            return matches
    return [value]

def expand_tokens(tokens: List[Token]) -> List[str]:
    result: List[str] = []
    for t in tokens:
        result.extend(expand_word(t))
    return result

def apply_aliases(tokens: List[Token]) -> List[Token]:
    if tokens and tokens[0].kind == WORD and not tokens[0].quoted:
        name = tokens[0].text
        if name in ALIASES:
            return lex(ALIASES[name]) + tokens[1:]
    return tokens

# --- Parsing ---
def parse_logical_ops(tokens: List[Token]) -> List[Tuple[str, List[Token], bool]]:
    """Split tokens into (op, tokens, background) segments.
    op is ';', '&&' or '||' and says how the segment links to the one before."""
    segments: List[Tuple[str, List[Token], bool]] = []
    op = ";"
    cur: List[Token] = []

    for t in tokens:
        if t.kind in (AND, OR, SEMI, BG):
            if not cur:
                raise ShellSyntaxError(f"syntax error near unexpected token `{t.text}'")
            if t.kind == BG:
                segments.append((op, cur, True))
                op = ";"
            else:
                segments.append((op, cur, False))
                op = t.text
            cur = []
        else:
            cur.append(t)

    if cur:
        segments.append((op, cur, False))
    elif op in ("&&", "||"):
        raise ShellSyntaxError("syntax error: unexpected end of line")
    return segments

def split_pipeline(tokens: List[Token]) -> List[List[Token]]:
    pipeline: List[List[Token]] = []
    current: List[Token] = []
    for t in tokens:
        if t.kind == PIPE:
            if not current:
                raise ShellSyntaxError("syntax error near unexpected token `|'")
            pipeline.append(current)
            current = []
        else:
            current.append(t)
    if not current:
        raise ShellSyntaxError("syntax error near unexpected token `|'")
    pipeline.append(current)
    return pipeline

# --- Redirection Parsing ---
def parse_redirection(tokens: List[Token]):
    cmd_tokens: List[Token] = []
    stdout_file = stderr_file = None
    append_stdout = append_stderr = False
    i = 0
    while i < len(tokens):
        t = tokens[i]
        if t.kind != REDIR:
            cmd_tokens.append(t)
            i += 1
            continue
        if i + 1 >= len(tokens) or tokens[i + 1].kind != WORD:
            raise ShellSyntaxError("syntax error near unexpected token `newline'")
        names = expand_word(tokens[i + 1])
        if len(names) != 1:
            raise ShellSyntaxError(f"{tokens[i + 1].text}: ambiguous redirect")
        name = names[0]
        op = t.text
        if op in (">", "1>"):
            stdout_file, append_stdout = name, False
        elif op in (">>", "1>>"):
            stdout_file, append_stdout = name, True
        elif op == "2>":
            stderr_file, append_stderr = name, False
        elif op == "2>>":
            stderr_file, append_stderr = name, True
        elif op == "&>":
            stdout_file = stderr_file = name
            append_stdout = append_stderr = False
        elif op == "&>>":
            stdout_file = stderr_file = name
            append_stdout = append_stderr = True
        i += 2
    return cmd_tokens, stdout_file, stderr_file, append_stdout, append_stderr

# --- Execution ---
def run_pipeline(cmds: List[List[str]], stdout_file=None, stderr_file=None,
                 append_stdout=False, append_stderr=False, background=False):
    prev_pipe = None
    processes = []
    for i, cmd in enumerate(cmds):
        stdin = prev_pipe
        stdout = subprocess.PIPE if i < len(cmds) - 1 else None
        p = subprocess.Popen(cmd, stdin=stdin, stdout=stdout, stderr=subprocess.PIPE, shell=False)
        if prev_pipe: prev_pipe.close()
        prev_pipe = p.stdout
        processes.append(p)

    # Handle redirection for last process
    if stdout_file:
        mode = 'a' if append_stdout else 'w'
        with open(stdout_file, mode) as f:
            out, _ = processes[-1].communicate()
            if out: f.write(out.decode())
    else:
        out, _ = processes[-1].communicate()
        if out: print(out.decode(), end="")

    if stderr_file:
        mode = 'a' if append_stderr else 'w'
        with open(stderr_file, mode) as f:
            _, err = processes[-1].communicate()
            if err: f.write(err.decode())
    else:
        _, err = processes[-1].communicate()
        if err: print(err.decode(), end="", file=sys.stderr)

    if background:
        BACKGROUND_JOBS.append(processes[-1])
        print(f"[{len(BACKGROUND_JOBS)}] {processes[-1].pid}")
        return 0

    return processes[-1].returncode

def execute_command(tokens: List[Token], background: bool = False):
    pipeline_tokens = split_pipeline(tokens)
    pipeline_tokens = [apply_aliases(cmd) for cmd in pipeline_tokens]

    # Redirection is only supported on the last command for now.
    for cmd in pipeline_tokens[:-1]:
        if any(t.kind == REDIR for t in cmd):
            raise ShellSyntaxError("redirection is only supported on the last command")

    last_cmd, stdout_file, stderr_file, append_stdout, append_stderr = \
        parse_redirection(pipeline_tokens[-1])
    pipeline_tokens[-1] = last_cmd

    pipeline = [expand_tokens(cmd) for cmd in pipeline_tokens]
    if any(not cmd for cmd in pipeline):
        return 0

    # Builtin handling (only if single command and not piped)
    if len(pipeline) == 1 and pipeline[0][0] in BUILTIN_COMMANDS:
        with contextlib.redirect_stdout(sys.stdout), contextlib.redirect_stderr(sys.stderr):
            return BUILTIN_COMMANDS[pipeline[0][0]](pipeline[0][1:])

    return run_pipeline(pipeline, stdout_file, stderr_file, append_stdout, append_stderr, background=background)

# --- Background Jobs ---
def check_background_jobs():
    for job in BACKGROUND_JOBS[:]:
        if job.poll() is not None:
            print(f"[Done] {job.args}")
            BACKGROUND_JOBS.remove(job)

# --- History ---
def load_history():
    try: readline.read_history_file(HISTORY_FILE)
    except FileNotFoundError: pass

def save_history():
    try: readline.write_history_file(HISTORY_FILE)
    except Exception: pass

# --- Main Shell ---
def main():
    load_history()
    while True:
        check_background_jobs()
        try:
            line = input("$ ")
            if not line.strip(): continue
            readline.add_history(line)
            try:
                logical_commands = parse_logical_ops(lex(line))
            except ShellSyntaxError as e:
                print(f"syntax error: {e}", file=sys.stderr)
                continue
            last_code = 0
            for op, cmd, bg in logical_commands:
                if op == "&&" and last_code != 0: continue
                if op == "||" and last_code == 0: continue
                try:
                    last_code = execute_command(cmd, bg)
                except ShellSyntaxError as e:
                    print(f"syntax error: {e}", file=sys.stderr)
                    last_code = 2
        except EOFError:
            print("exit")
            break
        except KeyboardInterrupt:
            print()
            continue
    save_history()

if __name__ == "__main__":
    main()
