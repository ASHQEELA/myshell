"""A small full-screen text editor: `edit FILE`, also available as `nano` when the system has none.

Keys: Ctrl+S save, Ctrl+X exit, Ctrl+W find, Ctrl+N find next, Ctrl+G go to line,
Ctrl+Z undo, Ctrl+Y redo. Arrow keys, Home, End, Page Up and Page Down work as usual."""
import os
from typing import List, Optional, TextIO, Tuple

from .state import ShellState

HELP_LINE = " ^S Save   ^X Exit   ^W Find   ^N Next   ^G Go to line   ^Z Undo   ^Y Redo "


class EditError(Exception):
    pass


class EditSession:
    """The file side of the editor: load, save and search. No screen code lives here."""

    def __init__(self, path: str):
        self.path = path
        self.eol = "\n"
        self.text = ""
        self.exists = os.path.exists(path)
        if os.path.isdir(path):
            raise EditError(f"{path}: is a directory")
        if self.exists:
            try:
                with open(path, "rb") as f:
                    raw = f.read()
            except OSError as e:
                raise EditError(f"{path}: {e.strerror}")
            if b"\0" in raw:
                raise EditError(f"{path}: looks like a binary file")
            decoded = raw.decode("utf-8", errors="replace")
            if "\r\n" in decoded:
                self.eol = "\r\n"
            self.text = decoded.replace("\r\n", "\n")
        self.saved_text = self.text

    def modified(self, current: str) -> bool:
        return current != self.saved_text

    def save(self, current: str) -> int:
        """Write current text. Returns the number of lines saved."""
        data = current.replace("\n", self.eol)
        if current and not current.endswith("\n"):
            data += self.eol  # a text file ends with a line break
        tmp = f"{self.path}.myshell-tmp"
        try:
            with open(tmp, "w", encoding="utf-8", newline="") as f:
                f.write(data)
            if self.exists:
                try:
                    os.chmod(tmp, os.stat(self.path).st_mode & 0o7777)
                except OSError:
                    pass
            os.replace(tmp, self.path)
        except OSError as e:
            try:
                os.remove(tmp)
            except OSError:
                pass
            raise EditError(f"cannot save {self.path}: {e.strerror}")
        self.exists = True
        self.saved_text = current
        return current.count("\n") + (1 if current and not current.endswith("\n") else 0)


def find_text(text: str, query: str, start: int) -> Optional[int]:
    """Index of the next match of query at or after start. Wraps to the top. Ignores case."""
    if not query:
        return None
    hay, needle = text.lower(), query.lower()
    at = hay.find(needle, start)
    if at < 0 and start > 0:
        at = hay.find(needle)
    return at if at >= 0 else None


def line_start(text: str, number: int) -> Optional[int]:
    """Index where line number (1 based) begins. None when the file is shorter."""
    if number < 1:
        return None
    index = 0
    for _ in range(number - 1):
        nxt = text.find("\n", index)
        if nxt < 0:
            return None
        index = nxt + 1
    return index


def cursor_position(text: str, index: int) -> Tuple[int, int]:
    """(line, column), both counted from 1."""
    before = text[:index]
    line = before.count("\n") + 1
    return line, index - (before.rfind("\n") + 1) + 1


def run_editor(path: str, input=None, output=None) -> int:
    """Show the editor until the person exits. input and output are for tests."""
    from prompt_toolkit.application import Application
    from prompt_toolkit.buffer import Buffer
    from prompt_toolkit.filters import Condition
    from prompt_toolkit.key_binding import KeyBindings
    from prompt_toolkit.layout import (ConditionalContainer, HSplit, Layout, Window,
                                       BufferControl, FormattedTextControl)
    from prompt_toolkit.layout.dimension import Dimension
    from prompt_toolkit.widgets import TextArea

    session = EditSession(path)
    area = TextArea(text=session.text, scrollbar=False, line_numbers=True, wrap_lines=False,
                    focus_on_click=True)
    prompt_buffer = Buffer(multiline=False)
    ui = {"mode": None, "message": "", "query": "", "quit_armed": False}

    def current() -> str:
        return area.text

    def status_text():
        line, col = cursor_position(area.text, area.buffer.cursor_position)
        mark = " [modified]" if session.modified(area.text) else ""
        name = path + ("" if session.exists else " (new file)")
        left = f" {name}{mark}"
        right = f"Ln {line}, Col {col} "
        msg = f"  {ui['message']}" if ui["message"] else ""
        return [("reverse", f"{left}{msg}  {right}")]

    def prompt_label():
        return [("bold", "Find: " if ui["mode"] == "find" else "Go to line: ")]

    prompting = Condition(lambda: ui["mode"] is not None)
    status = Window(FormattedTextControl(status_text), height=1, style="reverse")
    help_bar = Window(FormattedTextControl(lambda: [("reverse", HELP_LINE)]), height=1,
                      style="reverse")
    prompt_row = ConditionalContainer(
        HSplit([Window(height=1, content=BufferControl(prompt_buffer, focusable=True),
                       get_line_prefix=lambda n, w: prompt_label())]),
        filter=prompting)
    layout = Layout(HSplit([area, status, prompt_row, help_bar],
                           height=Dimension(min=3)), focused_element=area)

    kb = KeyBindings()

    def say(text: str) -> None:
        ui["message"] = text

    @kb.add("c-s", eager=True)
    def _save(event):
        ui["quit_armed"] = False
        try:
            lines = session.save(current())
            say(f"saved {lines} lines")
        except EditError as e:
            say(str(e))

    @kb.add("c-x", eager=True)
    def _exit(event):
        if session.modified(current()) and not ui["quit_armed"]:
            ui["quit_armed"] = True
            say("Unsaved changes. Press ^X again to discard them, or ^S to save.")
            return
        event.app.exit(result=0)

    def begin(mode: str) -> None:
        ui["mode"] = mode
        prompt_buffer.text = ui["query"] if mode == "find" else ""
        prompt_buffer.cursor_position = len(prompt_buffer.text)
        event_app().layout.focus(prompt_buffer)

    def event_app():
        from prompt_toolkit.application import get_app
        return get_app()

    @kb.add("c-w", eager=True)
    def _find(event):
        ui["quit_armed"] = False
        begin("find")

    @kb.add("c-g", eager=True)
    def _goto(event):
        ui["quit_armed"] = False
        begin("goto")

    def search(start: int) -> None:
        at = find_text(area.text, ui["query"], start)
        if at is None:
            say(f"'{ui['query']}' was not found")
            return
        area.buffer.cursor_position = at
        say("")

    @kb.add("c-n", eager=True)
    def _next(event):
        if not ui["query"]:
            say("Nothing to find yet. Press ^W first.")
            return
        search(area.buffer.cursor_position + 1)

    @kb.add("c-z", eager=True)
    def _undo(event):
        area.buffer.undo()

    @kb.add("c-y", eager=True)
    def _redo(event):
        area.buffer.redo()

    @kb.add("enter", filter=prompting, eager=True)
    def _accept(event):
        mode, value = ui["mode"], prompt_buffer.text.strip()
        ui["mode"] = None
        event.app.layout.focus(area)
        if mode == "find":
            if value:
                ui["query"] = prompt_buffer.text
            search(area.buffer.cursor_position)
        elif value.isdigit():
            at = line_start(area.text, int(value))
            if at is None:
                say(f"the file has only {area.text.count(chr(10)) + 1} lines")
            else:
                area.buffer.cursor_position = at
                say("")
        elif value:
            say(f"'{value}' is not a line number")

    @kb.add("escape", filter=prompting, eager=True)
    @kb.add("c-c", filter=prompting, eager=True)
    def _cancel(event):
        ui["mode"] = None
        event.app.layout.focus(area)

    def on_change(_buf) -> None:
        ui["quit_armed"] = False

    area.buffer.on_text_changed += on_change

    app = Application(layout=layout, key_bindings=kb, full_screen=True, input=input,
                      output=output, mouse_support=False)
    app.run()
    return 0


def builtin_edit(state: ShellState, args: List[str], stdout: TextIO, stderr: TextIO,
                 name: str = "edit") -> int:
    """edit FILE: change a text file. ^S saves, ^X exits."""
    if args and args[0] in ("-h", "--help"):
        print(f"{name} FILE: a small text editor. {HELP_LINE.strip()}", file=stdout)
        return 0
    if len(args) != 1:
        print(f"{name}: usage: {name} FILE", file=stderr)
        return 2
    import sys
    if not (sys.stdin.isatty() and hasattr(stdout, "isatty") and stdout.isatty()):
        print(f"{name}: needs a terminal", file=stderr)
        return 1
    path = os.path.expanduser(args[0])
    try:
        return run_editor(path)
    except EditError as e:
        print(f"{name}: {e}", file=stderr)
        return 1
    except KeyboardInterrupt:
        return 130


def builtin_nano(state: ShellState, args: List[str], stdout: TextIO, stderr: TextIO) -> int:
    return builtin_edit(state, args, stdout, stderr, "nano")


BUILTINS = {"edit": builtin_edit}
PORTABLE = {"nano": builtin_nano}
