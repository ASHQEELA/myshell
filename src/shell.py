import os
import sys
import glob
import subprocess
import readline  # Windows: pip install pyreadline3
import shutil
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, TextIO, Tuple


# =============================================================================
# Errors
# =============================================================================
class ShellError(Exception):
    """A user-facing error. The message is printed and the shell keeps running."""


class ShellSyntaxError(ShellError):
    def __str__(self) -> str:
        return f"syntax error: {self.args[0]}"


class ShellExit(Exception):
    """Raised by the exit builtin. The main loop catches it and shuts down."""
    def __init__(self, code: int):
        super().__init__(code)
        self.code = code


# =============================================================================
# State
# =============================================================================
@dataclass
class ShellState:
    aliases: Dict[str, str] = field(default_factory=dict)
    background_jobs: List[subprocess.Popen] = field(default_factory=list)
    history_file: str = os.path.expanduser("~/.custom_shell_history")
    last_status: int = 0

    def reap_jobs(self) -> None:
        for job in self.background_jobs[:]:
            if job.poll() is not None:
                print(f"[Done] {job.args}")
                self.background_jobs.remove(job)

    def load_history(self) -> None:
        try:
            readline.read_history_file(self.history_file)
        except FileNotFoundError:
            pass

    def save_history(self) -> None:
        try:
            readline.write_history_file(self.history_file)
        except Exception:
            pass


# =============================================================================
# Builtins
# Signature: (state, args, stdout, stderr) -> exit status
# =============================================================================
Builtin = Callable[[ShellState, List[str], TextIO, TextIO], int]


def builtin_echo(state: ShellState, args: List[str], stdout: TextIO, stderr: TextIO) -> int:
    print(" ".join(args), file=stdout)
    return 0


def builtin_pwd(state: ShellState, args: List[str], stdout: TextIO, stderr: TextIO) -> int:
    if args:
        print("pwd: too many arguments", file=stderr)
        return 1
    print(os.getcwd(), file=stdout)
    return 0


def builtin_cd(state: ShellState, args: List[str], stdout: TextIO, stderr: TextIO) -> int:
    try:
        path = args[0] if args else os.getenv("HOME", "")
        os.chdir(os.path.expanduser(path))
    except Exception as e:
        print(f"cd: {e}", file=stderr)
        return 1
    return 0


def builtin_exit(state: ShellState, args: List[str], stdout: TextIO, stderr: TextIO) -> int:
    if len(args) > 1:
        print("exit: too many arguments", file=stderr)
        return 1
    code = state.last_status
    if args:
        try:
            code = int(args[0]) & 0xFF
        except ValueError:
            print(f"exit: {args[0]}: numeric argument required", file=stderr)
            code = 2
    raise ShellExit(code)


def builtin_alias(state: ShellState, args: List[str], stdout: TextIO, stderr: TextIO) -> int:
    if not args:
        for k, v in state.aliases.items():
            print(f"alias {k}='{v}'", file=stdout)
        return 0
    status = 0
    for arg in args:
        if "=" in arg:
            k, v = arg.split("=", 1)
            state.aliases[k] = v
        elif arg in state.aliases:
            print(f"alias {arg}='{state.aliases[arg]}'", file=stdout)
        else:
            print(f"alias: {arg}: not found", file=stderr)
            status = 1
    return status


def builtin_type(state: ShellState, args: List[str], stdout: TextIO, stderr: TextIO) -> int:
    if not args:
        print("type: missing argument", file=stderr)
        return 1
    name = args[0]
    if name in state.aliases:
        print(f"{name} is an alias for '{state.aliases[name]}'", file=stdout)
    elif name in BUILTINS:
        print(f"{name} is a shell builtin", file=stdout)
    else:
        exe = shutil.which(name)
        if exe:
            print(f"{name} is {exe}", file=stdout)
        else:
            print(f"{name}: not found", file=stderr)
            return 1
    return 0


BUILTINS: Dict[str, Builtin] = {
    "echo": builtin_echo,
    "pwd": builtin_pwd,
    "cd": builtin_cd,
    "alias": builtin_alias,
    "exit": builtin_exit,
    "type": builtin_type,
}


# =============================================================================
# Lexer: text -> tokens
# =============================================================================
WORD, PIPE, AND, OR, SEMI, BG, REDIR = "WORD", "PIPE", "AND", "OR", "SEMI", "BG", "REDIR"

# Longest operators first so "&>>" wins over "&>" and "&".
OPERATORS: List[Tuple[str, str]] = [
    ("&>>", REDIR), ("2>>", REDIR), ("1>>", REDIR),
    ("&&", AND), ("||", OR), ("&>", REDIR), ("2>", REDIR), ("1>", REDIR),
    (">>", REDIR), ("|", PIPE), (";", SEMI), ("&", BG), (">", REDIR),
]
# Operators that start with a digit only count at the start of a word.
FD_OPERATORS = ("2>>", "1>>", "2>", "1>")
COMMAND_START_AFTER = (PIPE, AND, OR, SEMI, BG)


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


# =============================================================================
# Alias expansion: tokens -> tokens
# Runs before parsing, so an alias value may hold pipes, redirects and operators.
# =============================================================================
def expand_aliases(tokens: List[Token], aliases: Dict[str, str],
                   active: frozenset = frozenset()) -> List[Token]:
    result: List[Token] = []
    at_start = True
    for t in tokens:
        name = t.text
        if (at_start and t.kind == WORD and not t.quoted
                and name in aliases and name not in active):
            # Expand chains (a -> b -> ls). `active` blocks recursion (ls -> ls --color).
            result.extend(expand_aliases(lex(aliases[name]), aliases, active | {name}))
            at_start = False
            continue
        result.append(t)
        at_start = t.kind in COMMAND_START_AFTER
    return result


# =============================================================================
# Parser: tokens -> syntax tree
#
#   list     := pipeline ( ('&&' | '||' | ';' | '&') pipeline )* [';' | '&']
#   pipeline := command ( '|' command )*
#   command  := ( WORD | REDIR WORD )+
# =============================================================================
@dataclass
class Redirect:
    op: str
    target: Token


@dataclass
class Command:
    argv: List[Token]
    redirects: List[Redirect] = field(default_factory=list)


@dataclass
class Pipeline:
    commands: List[Command]
    background: bool = False


@dataclass
class ListItem:
    op: str  # how this item links to the one before it: ';', '&&' or '||'
    pipeline: Pipeline


class Parser:
    def __init__(self, tokens: List[Token]):
        self.tokens = tokens
        self.pos = 0

    def peek(self) -> Optional[Token]:
        return self.tokens[self.pos] if self.pos < len(self.tokens) else None

    def unexpected(self) -> ShellSyntaxError:
        t = self.peek()
        if t is None:
            return ShellSyntaxError("unexpected end of line")
        return ShellSyntaxError(f"unexpected token `{t.text}'")

    def parse_list(self) -> List[ListItem]:
        items: List[ListItem] = []
        op = ";"
        while self.peek() is not None:
            pipeline = self.parse_pipeline()
            t = self.peek()
            if t is not None and t.kind == BG:
                self.pos += 1
                pipeline.background = True
                items.append(ListItem(op, pipeline))
                op = ";"
                continue
            items.append(ListItem(op, pipeline))
            if t is None:
                break
            self.pos += 1  # consume && || or ;
            op = t.text
            if self.peek() is None:
                if op in ("&&", "||"):
                    raise ShellSyntaxError("unexpected end of line")
                break  # trailing ';'
        return items

    def parse_pipeline(self) -> Pipeline:
        commands = [self.parse_command()]
        while self.peek() is not None and self.peek().kind == PIPE:
            self.pos += 1
            commands.append(self.parse_command())
        return Pipeline(commands)

    def parse_command(self) -> Command:
        argv: List[Token] = []
        redirects: List[Redirect] = []
        while True:
            t = self.peek()
            if t is None or t.kind not in (WORD, REDIR):
                break
            self.pos += 1
            if t.kind == WORD:
                argv.append(t)
                continue
            target = self.peek()
            if target is None or target.kind != WORD:
                raise self.unexpected()
            self.pos += 1
            redirects.append(Redirect(t.text, target))
        if not argv and not redirects:
            raise self.unexpected()
        return Command(argv, redirects)


def parse(tokens: List[Token]) -> List[ListItem]:
    return Parser(tokens).parse_list()


# =============================================================================
# Expansion: word tokens -> strings
# =============================================================================
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


# =============================================================================
# Executor: syntax tree -> running processes
# =============================================================================
@dataclass
class Redirections:
    stdout_file: Optional[str] = None
    stderr_file: Optional[str] = None
    append_stdout: bool = False
    append_stderr: bool = False

    def open(self):
        """Open the targets. Returns (out, err, opened). The caller closes `opened`."""
        opened = []
        out = err = None
        try:
            if self.stdout_file:
                out = open(self.stdout_file, "a" if self.append_stdout else "w")
                opened.append(out)
            if self.stderr_file:
                if (self.stderr_file == self.stdout_file
                        and self.append_stderr == self.append_stdout):
                    err = out  # &> and &>> share one handle
                else:
                    err = open(self.stderr_file, "a" if self.append_stderr else "w")
                    opened.append(err)
        except OSError:
            for f in opened:
                f.close()
            raise
        return out, err, opened


def exit_status(returncode: int) -> int:
    # Popen reports death by signal as a negative number. Shells use 128 + signal.
    return 128 + -returncode if returncode < 0 else returncode


class Executor:
    def __init__(self, state: ShellState):
        self.state = state

    # --- lists ---
    def run_list(self, items: List[ListItem]) -> int:
        status = self.state.last_status
        for item in items:
            if item.op == "&&" and status != 0:
                continue
            if item.op == "||" and status == 0:
                continue
            try:
                status = self.run_pipeline(item.pipeline)
            except ShellError as e:
                print(e, file=sys.stderr)
                status = 2
            self.state.last_status = status
        return status

    # --- pipelines ---
    def run_pipeline(self, pipeline: Pipeline) -> int:
        commands = pipeline.commands
        for cmd in commands[:-1]:
            if cmd.redirects:
                raise ShellError("redirection is only supported on the last command")

        argvs = [expand_tokens(c.argv) for c in commands]
        redir = self.resolve_redirects(commands[-1].redirects)

        try:
            if len(argvs) == 1:
                argv = argvs[0]
                if not argv:  # a bare redirect such as `> file` creates the file
                    _, _, opened = redir.open()
                    for f in opened:
                        f.close()
                    return 0
                if argv[0] in BUILTINS:
                    return self.run_builtin(argv[0], argv[1:], redir)
            if any(not argv for argv in argvs):
                return 0
            return self.run_external(argvs, redir, pipeline.background)
        except OSError as e:
            # A redirect target could not be opened.
            print(f"{e.filename}: {e.strerror}", file=sys.stderr)
            return 1

    def resolve_redirects(self, redirects: List[Redirect]) -> Redirections:
        r = Redirections()
        for red in redirects:
            names = expand_word(red.target)
            if len(names) != 1:
                raise ShellError(f"{red.target.text}: ambiguous redirect")
            name = names[0]
            if red.op in (">", "1>"):
                r.stdout_file, r.append_stdout = name, False
            elif red.op in (">>", "1>>"):
                r.stdout_file, r.append_stdout = name, True
            elif red.op == "2>":
                r.stderr_file, r.append_stderr = name, False
            elif red.op == "2>>":
                r.stderr_file, r.append_stderr = name, True
            elif red.op == "&>":
                r.stdout_file = r.stderr_file = name
                r.append_stdout = r.append_stderr = False
            elif red.op == "&>>":
                r.stdout_file = r.stderr_file = name
                r.append_stdout = r.append_stderr = True
        return r

    # --- builtins run inside the shell process ---
    def run_builtin(self, name: str, args: List[str], redir: Redirections) -> int:
        out, err, opened = redir.open()
        try:
            return BUILTINS[name](self.state, args, out or sys.stdout, err or sys.stderr)
        finally:
            for f in opened:
                f.close()

    # --- external programs ---
    def run_external(self, cmds: List[List[str]], redir: Redirections,
                     background: bool) -> int:
        out, err, opened = redir.open()
        processes: List[subprocess.Popen] = []
        prev = None          # stdin for the next command
        last_failure = None  # status when the last command could not start
        try:
            for i, cmd in enumerate(cmds):
                is_last = i == len(cmds) - 1
                try:
                    p = subprocess.Popen(
                        cmd,
                        stdin=prev,
                        stdout=out if is_last else subprocess.PIPE,
                        stderr=err if is_last else None,
                    )
                except (FileNotFoundError, PermissionError, NotADirectoryError) as e:
                    if isinstance(e, FileNotFoundError):
                        reason = ("No such file or directory" if "/" in cmd[0]
                                  else "command not found")
                        status = 127
                    else:
                        reason = "Permission denied"
                        status = 126
                    print(f"{cmd[0]}: {reason}", file=sys.stderr)
                    p, last_failure = None, (status if is_last else None)
                if hasattr(prev, "close"):
                    prev.close()
                if p is None:
                    prev = subprocess.DEVNULL  # next stage reads empty input
                else:
                    prev = p.stdout
                    processes.append(p)
        finally:
            if hasattr(prev, "close"):
                prev.close()
            for f in opened:
                f.close()

        if background:
            if processes:
                self.state.background_jobs.append(processes[-1])
                print(f"[{len(self.state.background_jobs)}] {processes[-1].pid}")
            return last_failure or 0

        try:
            for p in processes:
                p.wait()
        except KeyboardInterrupt:
            for p in processes:
                p.wait()
            print()
            return 130

        if last_failure is not None:
            return last_failure
        return exit_status(processes[-1].returncode)


# =============================================================================
# Main shell
# =============================================================================
def run_line(line: str, state: ShellState, executor: Executor) -> int:
    tokens = expand_aliases(lex(line), state.aliases)
    return executor.run_list(parse(tokens))


def main() -> int:
    state = ShellState()
    executor = Executor(state)
    state.load_history()
    code = 0
    try:
        while True:
            state.reap_jobs()
            try:
                line = input("$ ")
            except EOFError:
                print("exit")
                code = state.last_status
                break
            except KeyboardInterrupt:
                print()
                continue
            if not line.strip():
                continue
            readline.add_history(line)
            try:
                run_line(line, state, executor)
            except ShellError as e:
                print(e, file=sys.stderr)
                state.last_status = 2
            except KeyboardInterrupt:
                print()
    except ShellExit as e:
        code = e.code
    state.save_history()
    return code


if __name__ == "__main__":
    sys.exit(main())
