# PyShell - A Custom Python Shell

**PyShell** is a lightweight, extensible command-line shell written entirely in Python.  
It started as a simple script and now supports advanced shell-like features.

---

## Features

PyShell provides many functionalities expected from a modern terminal:

- **Command Execution**: Run both built-in commands and external programs from your system’s PATH.
- **Pipelines (`|`)**: Chain multiple commands together, feeding the output of one into the next.
- **Logical Operators (`&&`, `||`)**: Execute commands conditionally based on the previous command’s success/failure.
- **Command Chaining (`;`)**: Execute multiple commands sequentially.
- **Redirection (`>`, `>>`)**: Redirect stdout and stderr to files.
- **Background Jobs (`&`)**: Run commands in the background and get notified when they finish.
- **Tab Completion**: Context-aware completion for commands, aliases, and filenames (requires `readline` / `pyreadline3`).
- **Persistent History**: Command history saved in `~/.custom_shell_history` and automatically loaded on startup.
- **Aliases**: Create shortcuts for commands with `alias` (e.g., `alias ll="ls -l"`).

### Expansion Features

- **Variable Expansion**: Supports `$VAR` environment variables.
- **Tilde Expansion**: `~` is expanded to the user’s home directory.
- **Glob Expansion**: Supports wildcards like `*` and `?` for filenames.

---

## Built-in Commands

PyShell comes with these built-in commands:

- `alias` – Create or list command aliases.
- `cd` – Change the current working directory.
- `echo` – Print text to the terminal.
- `exit` – Exit the shell.
- `pwd` – Print the current working directory.
- `type` – Show if a command is a shell builtin, alias, or external program.

---

## Installation

1. **Clone the repository**:

```bash
git clone <your-repo-url>
cd py-shell
```
