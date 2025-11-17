import os
import sys
import glob
import subprocess
import readline  # Windows: pip install pyreadline3
import contextlib
import shutil
from typing import List, Dict, Optional, Callable

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
            ALIASES[k] = remove_quotes(v)
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

# --- Utilities ---
def remove_quotes(s: str) -> str:
    if (s.startswith('"') and s.endswith('"')) or (s.startswith("'") and s.endswith("'")):
        return s[1:-1]
    return s

def expand_tokens(tokens: List[str]) -> List[str]:
    result = []
    for t in tokens:
        t = os.path.expandvars(os.path.expanduser(t))
        if any(c in t for c in "*?[]"):
            matches = glob.glob(t)
            result.extend(matches if matches else [remove_quotes(t)])
        else:
            result.append(remove_quotes(t))
    return result

def apply_aliases(tokens: List[str]) -> List[str]:
    if tokens and tokens[0] in ALIASES:
        alias_tokens = ALIASES[tokens[0]].split()
        return alias_tokens + tokens[1:]
    return tokens

# --- Parsing ---
def tokenize_line(line: str) -> List[str]:
    tokens = []
    current = ""
    in_single = in_double = False
    i = 0
    while i < len(line):
        c = line[i]
        if c == "'" and not in_double:
            in_single = not in_single
        elif c == '"' and not in_single:
            in_double = not in_double
        elif c.isspace() and not in_single and not in_double:
            if current:
                tokens.append(current)
                current = ""
            i += 1
            continue
        current += c
        i += 1
    if current:
        tokens.append(current)
    return tokens

def parse_logical_ops(line: str):
    commands = []
    current = ""
    op = ";"
    i = 0
    while i < len(line):
        if line[i:i+2] == "&&":
            commands.append((op, current.strip()))
            op = "&&"
            current = ""
            i += 2
        elif line[i:i+2] == "||":
            commands.append((op, current.strip()))
            op = "||"
            current = ""
            i += 2
        elif line[i] == ";":
            commands.append((op, current.strip()))
            op = ";"
            current = ""
            i += 1
        else:
            current += line[i]
            i += 1
    if current.strip():
        commands.append((op, current.strip()))
    return commands

# --- Redirection Parsing ---
def parse_redirection(tokens: List[str]):
    cmd_tokens = []
    stdout_file = stderr_file = None
    append_stdout = append_stderr = False
    i = 0
    while i < len(tokens):
        t = tokens[i]
        if t in ('>', '1>'):
            stdout_file = tokens[i+1]; append_stdout = False; i += 2
        elif t in ('>>', '1>>'):
            stdout_file = tokens[i+1]; append_stdout = True; i += 2
        elif t == '2>':
            stderr_file = tokens[i+1]; append_stderr = False; i += 2
        elif t == '2>>':
            stderr_file = tokens[i+1]; append_stderr = True; i += 2
        elif t == '&>':
            stdout_file = stderr_file = tokens[i+1]; append_stdout = append_stderr = False; i += 2
        elif t == '&>>':
            stdout_file = stderr_file = tokens[i+1]; append_stdout = append_stderr = True; i += 2
        else:
            cmd_tokens.append(t); i += 1
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

def execute_command(cmdline: str):
    background = cmdline.endswith("&")
    if background: cmdline = cmdline[:-1].strip()
    tokens = tokenize_line(cmdline)
    tokens = apply_aliases(tokens)
    tokens = expand_tokens(tokens)
    if not tokens:
        return 0

    # Split by pipe
    pipeline = []
    current = []
    for t in tokens:
        if t == "|":
            pipeline.append(current)
            current = []
        else:
            current.append(t)
    pipeline.append(current)

    # Parse redirection on last command
    last_cmd = pipeline[-1]
    last_cmd, stdout_file, stderr_file, append_stdout, append_stderr = parse_redirection(last_cmd)
    pipeline[-1] = last_cmd

    # Builtin handling (only if single command and not piped)
    if len(pipeline) == 1 and last_cmd[0] in BUILTIN_COMMANDS:
        with contextlib.redirect_stdout(sys.stdout), contextlib.redirect_stderr(sys.stderr):
            return BUILTIN_COMMANDS[last_cmd[0]](last_cmd[1:])

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
            logical_commands = parse_logical_ops(line)
            last_code = 0
            for op, cmd in logical_commands:
                if op == "&&" and last_code != 0: continue
                if op == "||" and last_code == 0: continue
                last_code = execute_command(cmd)
        except EOFError:
            print("exit")
            break
        except KeyboardInterrupt:
            print()
            continue
    save_history()

if __name__ == "__main__":
    main()
