# myshell

A Unix-style shell written in Python. It runs on Linux and macOS with full job control. It also runs on Windows through a simpler fallback.

## Requirements

- Python 3.9 or newer. Tested on 3.9 and 3.13 on Linux, macOS and Windows (see `.github/workflows/ci.yml`).
- `prompt_toolkit` is optional. With it you get Ctrl+R history search, grey suggestions and syntax colors. Without it the shell uses plain `input()` and readline.
- pytest and pyflakes for development: `pip install -e ".[dev]"`

## Install and run

    uvx myshell-py                   run without installing
    uv tool install myshell-py       install it
    pip install myshell-py

The package is `myshell-py`. It installs the commands `myshell` and `myshell-py`.

From a clone:

    python shell.py                  interactive shell
    python shell.py -c "echo hi"     run one command string
    python shell.py script.sh a b    run a script. $1 is a, $2 is b
    python -m myshell                same thing, as a package
    python -m myshell --tui          the full-screen app
    uv run myshell                   uses pyproject.toml

`myshell -c 'cmd' NAME ARG...` sets `$0` to NAME and `$1...` to the ARGs.

## Run the tests

    pip install -e ".[dev]"
    python -m pytest
    python -m pyflakes myshell tests

With uv: `uv run --extra dev pytest`.

## Release

    uv build                         writes dist/*.whl and dist/*.tar.gz
    uv publish                       needs a PyPI token or trusted publishing

Pushing a tag such as `v0.2.0` runs `.github/workflows/release.yml`, which builds and publishes with trusted publishing. Add the repository as a trusted publisher on pypi.org first (project `myshell`, workflow `release.yml`, environment `pypi`). Bump `version` in `pyproject.toml` before each release. If the name `myshell` is taken when you publish, change `name` in `pyproject.toml`. The command stays `myshell`.

## Features

Commands and operators
- Pipes: `a | b`
- Lists: `a; b`, `a && b`, `a || b`
- Background jobs: `a &`
- Comments with `#`

Quoting and expansion
- Single quotes, double quotes and backslash escapes
- Variables: `$X`, `${X}`, `$?`, `$$`, and `$0 $1 ... $# $@ $*`
- Parameter expansion: `${X:-default}` `${X:=v}` `${X:?msg}` `${X:+alt}` `${#X}` `${X%.txt}` `${X%%p}` `${X#p}` `${X##p}` `${X/old/new}` `${X//old/new}` `${X:off:len}` `${X^^}` `${X,,}`
- Brace expansion: `file.{txt,md}`, `{1..5}`, `{a..e}`, `{01..10}`, `{1..10..2}`
- Command substitution: `$(cmd)` and backticks
- Arithmetic: `$((1 + 2 * x))`, including `+=`, `?:`, hex and octal
- Word splitting, globbing (`*`, `?`, `[ab]`) and tilde (`~`)
- Assignments: `NAME=value`, and `NAME=value cmd` for one command

Redirection
- `< > >> 2> 2>> &> &>>`
- `2>&1` and `1>&2`, applied left to right
- Here-strings: `<<<`
- Here-documents: `<<EOF` and `<<-EOF`. A quoted delimiter such as `<<'EOF'` turns expansion off.

Control flow
- `if`, `elif`, `else`, `fi`
- `while`, `until`, `for NAME in WORDS`, with `do` and `done`. A bare `for x; do` loops over `"$@"`.
- `case WORD in a|b) ...;; *.txt) ...;; *) ...;; esac`
- Functions: `name() { ...; }` or `function name { ...; }`, with `$1`, `$@`, `$#`, `return` and `local`
- Groups: `{ a; b; }`
- `break N` and `continue N`
- Blocks work on one line or across many lines

Builtins
- `echo` (with `-n -e -E`), `pwd cd exit alias unalias type which export unset source .`
- `read` (`-r -p -d -n`), `printf` (`-v`), `set` (`-e -u -x`, `-o`, `--`), `shift local return`
- `env` (`-i -u NAME=value`), `exec`, `trap` (EXIT, INT, TERM, HUP, USR1, USR2)
- `true false :`
- `jobs fg bg wait`
- `test` and `[ ]`: file, string and number checks, and `!`
- `history`: `N`, `-c`, `-d N`, `-w [file]`, `-r [file]`
- `guide`: lessons, cheat sheet and tips
- `kill [-s SIGNAL | -SIGNAL] PID|%JOB`, `kill -l`: stop a process or a job. On Windows every signal ends the process.
- `edit FILE`: a small full-screen editor. `^S` saves, `^X` exits, `^W` finds, `^N` finds the next match, `^G` goes to a line, `^Z` undoes. `nano` runs the same editor when your system has no `nano`.
- `cron` and `at`: scheduled tasks, see Scheduler below

Commands that work on every system
- `ls cat head tr sleep mkdir rm cp mv touch wc grep ps top nano` are built in. A real program of the same name on your PATH wins, so on Linux and macOS you still get GNU or BSD tools. Where there is none (plain Windows), the built-in one runs. Set `MYSHELL_PORTABLE=1` to always use the built-in ones.
- They also work in pipes and `$(...)` without fork. This is what makes Pip's lessons run on Windows.

Interactive use
- Job control: Ctrl+C, Ctrl+Z, `fg`, `bg`
- Tab completion for commands, paths and function names
- With `prompt_toolkit`: Ctrl+R reverse search, grey suggestion from history (Right arrow or End takes it), colors (green known command, red unknown, yellow strings, cyan variables). `MYSHELL_EDITOR=basic` turns it off.
- After a command that takes longer than 2 seconds the shell prints `took 3.2s`. Set `SLOW_COMMAND_SECS` to change the limit, or to `off`.
- Traps: `trap 'cmd' EXIT` runs when the shell ends
- History saved in `~/.custom_shell_history`, limited to 1000 entries
- Startup file `~/.myshellrc`. `myshell --norc` skips it, `myshell --rcfile FILE` or `MYSHELLRC=FILE` reads another one.
- Prompt set with `PS1`. Continuation lines use `PS2`.

## Pip, the guide

Pip is a small original character who lives in the shell.

    ,---.   .--------------------------------------.
   ( ^ ^ )  | Hi, I am Pip, your shell guide.       |
    \ v /   '--------------------------------------'
    (_@_)>

- Greets you at startup with a tip. Only on a real terminal.
- `guide` opens a menu of 13 lessons, with `[x]` next to the ones you finished. Each step shows a command. Press Enter to run it, `s` to skip, `q` to stop.
- `guide TOPIC` jumps to one lesson. Names and numbers both work, for example `guide pipes` or `guide 2`.
- `guide cheat` prints a cheat sheet. `guide tip` prints one tip. `guide list` lists the topics.
- `guide quiz [TOPIC] [N]` asks N multiple-choice questions (default 5). `guide progress` shows finished lessons and best scores. `guide progress reset` clears them. Progress lives in `~/.myshell_progress.json`.
- Explains mistakes. A mistyped command gets "Did you mean ...?". An unclosed quote, `$(` or block gets a plain explanation.
- `guide off` hides Pip for the session. Put `GUIDE=off` in `~/.myshellrc` to hide Pip always.
- Scripts, `-c` and piped input never show Pip's greeting or hints.

## Scripts

Save commands in a file and run it with arguments. A first line such as `#!/usr/bin/env myshell` is a comment, so it is safe.

    myshell backup.msh /data /backup      # $0 is backup.msh, $1 is /data, $2 is /backup
    myshell -c 'echo $1' name value       # one command, with arguments

## Processes

- `ps` lists your shell's processes. `ps -e` lists all of them.
- `top` shows a live list sorted by CPU. Options: `-n COUNT`, `-d SECONDS`, `-s cpu|mem|pid`, `-l ROWS`. Press Ctrl+C to stop.
- `kill PID`, `kill -9 PID`, `kill -s TERM %1`, `kill -l`.
- These work on Linux, macOS and Windows. On Windows `ps` and `top` read `tasklist`, so they show memory but no parent or CPU columns.

## Scheduler

Tasks run in the background while an interactive myshell is open. Nothing runs while no myshell is open. Output goes to `~/.myshell_cron.log`. Tasks are saved in `~/.myshell_sched.json`. Set `MYSHELL_NOCRON=1` to turn the background checks off.

    cron add '*/5 * * * *' echo hi     # every 5 minutes
    cron add '0 9 * * 1-5' backup.msh  # weekdays at 09:00
    cron add @daily 'rm -rf ~/tmp/*'   # macros: @hourly @daily @weekly @monthly @yearly
    cron                               # list tasks with the next run time
    cron check '30 4 1,15 * *'         # test a schedule
    cron log                           # show what the tasks printed
    cron rm 1                          # remove task 1

    at 17:30 echo hi                   # once, today (or tomorrow if it has passed)
    at +10m echo hi                    # in 10 minutes. Units: m, h, d, w
    at now + 2 hours echo hi
    at 2026-12-01 09:30 backup.msh
    at -l                              # list waiting tasks
    at -r 1                            # remove task 1

Quote the schedule so the shell does not expand the stars. If both day and weekday are set, the task runs when either matches, as in classic cron.

## Prompt escapes

| Escape | Meaning |
| --- | --- |
| `\w` | current directory, `~` for home |
| `\W` | last part of the directory |
| `\u` `\h` | user name, host name |
| `\$` | `$`, or `#` for root |
| `\j` | running jobs |
| `\?` | exit status of the last command |
| `\n` `\e` `\\` | newline, escape, backslash |
| `\t` `\T` `\A` `\@` `\d` | time (24-hour, 12-hour, HH:MM, am/pm), date |
| `\g` | git branch of the current folder, empty outside a repository |
| `\[` `\]` | wrap colors so line editing counts width correctly |

The default prompt adds the branch in brackets inside a git repository.

### Themes

A theme sets the colors of the default prompt, the syntax colors in the editor and Pip. Every theme except `default` and `mono` also colors the text you type and the text commands print.

    theme            list the themes with a preview
    theme ocean      switch and remember (saved in ~/.myshell_theme)
    theme reset      back to the default

Themes: `default`, `ocean`, `forest`, `sunset`, `light` (for a white background) and `mono` (no colors). `MYSHELL_THEME=ocean` picks one at start and wins over the saved choice. A `PS1` you set yourself keeps its own colors, and `NO_COLOR` turns colors off.

Example `~/.myshellrc`:

    PS1='\u@\h \W \$ '
    PS2='more> '
    alias ll='ls -l'

## Full-screen app

    myshell --tui      or type  tui  inside the shell

Four framed panels: Commands, Files, Processes and Schedule. The colors follow your `theme`.
A narrow window shows fewer panels. Under 96 columns only Commands shows. F2, F3 and F4 open a hidden panel full screen.

| Key | Action |
| --- | --- |
| `F1` | help |
| `F2` `F3` `F4` | files, processes, schedule |
| `Tab` `Esc` `F5` | next panel, back to commands |
| `z` | zoom the panel |
| `Ctrl+Q` | quit |

Commands: Enter runs, Up and Down walk the history, Tab completes, Right accepts the grey hint, Ctrl+C stops a running command. Output stays in the panel. `edit`, `less`, `guide` and `top` pause the screen and use the real terminal. Start a line with `!` to do the same for any program.
Files: Enter opens a folder or edits a file, `h` goes up, `i` puts the name on the command line, `.` shows hidden files.
Processes: `x` stops the process, `X` force kills it, `s` changes the sort.
Schedule: `a` adds a task, `d` removes it, `r` runs a cron task now.

## Layout

| File | Job |
| --- | --- |
| `cli.py` | entry point, interactive loop |
| `lexer.py` | text to tokens |
| `parser.py` | tokens to a tree |
| `expand.py` | variables, substitution, math, globbing |
| `arith.py` | the `$((...))` evaluator |
| `heredoc.py` | here-document bodies |
| `executor.py` | runs the tree with fork and pipes |
| `jobs.py` | process groups and job control |
| `builtins.py` | core builtin commands, lookup of builtins |
| `more_builtins.py` | read, printf, set, env, which, trap, exec and friends |
| `coreutils.py` | ls, cat, head, tr, sleep, mkdir, rm, cp, mv, touch, wc, grep |
| `editor.py` `highlight.py` | optional prompt_toolkit line editor and syntax colors |
| `progress.py` `quiz.py` | Pip's progress file and quiz questions |
| `completion.py` | tab completion |
| `prompt.py` | prompt escapes |
| `theme.py` | color themes |
| `procs.py` | ps, top, kill |
| `textedit.py` | the `edit` and `nano` editor |
| `sched.py` | cron and at |
| `tui.py` `tui_theme.py` | the full-screen app and its colors |
| `guide.py` | Pip: lessons, hints, greeting |
| `state.py` | variables, aliases, history, status |
| `compat.py` | platform checks |

## Limits

- Only file descriptors 0, 1 and 2 are redirected.
- Here-documents do not work inside multi-line `if`, loop and function blocks.
- Pipes, redirects and `&` after `fi`, `done`, `esac` or `}` are errors. So `cmd | while read x; do ...; done` does not work yet.
- Function bodies must be `{ ...; }` groups. There are no arrays, `eval`, `select` or `[[ ]]`.
- `test` has no `-a`, `-o` or parentheses.
- On Windows there is no job control: `&` runs in the foreground and `fg`, `bg` and `wait` are unavailable. Pipelines that include built-in commands run one stage after the other, with each stage's output held in memory.
- Signal traps run at the next command boundary, not in the middle of a command.
