"""The full-screen app: `myshell --tui` or the `tui` builtin.

Four panels: commands, files, processes and jobs, scheduled tasks. Everything is drawn by
small UIControl classes on top of prompt_toolkit, which myshell already depends on.
Commands run in a worker thread with their output captured, so the screen stays alive.
Programs that need the real terminal (edit, less, guide...) suspend the screen while they run."""
import io
import os
import re
import sys
import threading
import time
from typing import Callable, Dict, List, Optional, Tuple

from prompt_toolkit.application import Application, get_app, run_in_terminal
from prompt_toolkit.data_structures import Point
from prompt_toolkit.filters import Condition
from prompt_toolkit.formatted_text import ANSI
from prompt_toolkit.formatted_text.utils import split_lines
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.keys import Keys
from prompt_toolkit.layout import (ConditionalContainer, Float, FloatContainer, HSplit, Layout,
                                   VSplit, Window)
from prompt_toolkit.layout.controls import FormattedTextControl, UIContent, UIControl
from prompt_toolkit.layout.dimension import Dimension
from prompt_toolkit.mouse_events import MouseEventType
from prompt_toolkit.styles import Style
from prompt_toolkit.utils import get_cwidth

from . import history, procs, sched, theme, tui_theme
from .completion import complete_line
from .errors import ShellError, ShellExit
from .prompt import git_branch, short_path
from .state import ShellState

Frag = Tuple[str, str]
Line = List[Frag]

MIN_COLS, MIN_ROWS = 40, 10
WIDE, MEDIUM = 132, 96  # columns needed to show the files panel, and the right-hand panels
# programs that need the real terminal: the screen steps aside while they run
NEEDS_TERMINAL = {"edit", "nano", "vi", "vim", "nvim", "less", "more", "man", "guide", "htop",
                  "ssh", "python", "python3", "node", "irb", "top", "watch", "fzf"}
ASCII = {"tl": "+", "tr": "+", "bl": "+", "br": "+", "h": "-", "v": "|"}
ROUND = {"tl": "╭", "tr": "╮", "bl": "╰", "br": "╯", "h": "─", "v": "│"}
ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]|\x1b\][^\x07]*\x07")


# ---------------------------------------------------------------- text helpers

def cells(text: str) -> int:
    return sum(get_cwidth(c) for c in text)


def fit(frags: Line, width: int, style: str = "") -> Line:
    """Cut or pad fragments to exactly width cells. A cut line ends with an ellipsis."""
    out, used = [], 0
    total = sum(cells(t) for _, t in frags)
    limit = width if total <= width else max(0, width - 1)
    for st, text in frags:
        keep = []
        for ch in text:
            w = get_cwidth(ch)
            if used + w > limit:
                break
            keep.append(ch)
            used += w
        if keep:
            out.append((st, "".join(keep)))
        if used >= limit:
            break
    if total > width and width > 0:
        out.append((frags[-1][0] if frags else style, "…"))
        used += 1
    if used < width:
        out.append((style, " " * (width - used)))
    return out


def wrap(frags: Line, width: int) -> List[Line]:
    """Break one logical line into rows no wider than width cells."""
    rows, row, used = [], [], 0
    for st, text in frags:
        buf = ""
        for ch in text:
            w = get_cwidth(ch)
            if used + w > width and (row or buf):
                if buf:
                    row.append((st, buf))
                rows.append(row)
                row, buf, used = [], "", 0
            if ch == " " and used == 0 and rows:
                continue  # a row that continues a wrapped line does not start with a space
            buf += ch
            used += w
        if buf:
            row.append((st, buf))
    rows.append(row)
    return rows


def parse_output(text: str) -> List[Line]:
    """Command output (which may hold color codes) as lines of styled fragments."""
    try:
        frags = [(s, t) for s, t, *_ in ANSI(text.replace("\r\n", "\n")).__pt_formatted_text__()]
    except Exception:
        frags = [("", ANSI_RE.sub("", text))]
    return [list(line) for line in split_lines(frags)]


def human_size(n: int) -> str:
    for unit in ("B", "K", "M", "G"):
        if n < 1024 or unit == "G":
            return f"{n}{unit}" if unit == "B" else f"{n:.0f}{unit}" if n >= 100 else f"{n:.1f}{unit}"
        n /= 1024
    return str(n)


# ---------------------------------------------------------------- running commands

def run_captured(state: ShellState, line: str) -> Tuple[str, int]:
    """Run one line in this shell and return (everything it printed, exit status).
    File descriptors 1 and 2 point at a temp file while it runs, so child programs are caught too."""
    import tempfile
    sink = tempfile.TemporaryFile(mode="w+b")
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.flush()
        except Exception:
            pass
    keep_in = os.name != "nt"  # on Windows the screen reads the console handle, so leave stdin alone
    saved = {fd: os.dup(fd) for fd in ((0, 1, 2) if keep_in else (1, 2))}
    old = (sys.stdout, sys.stderr, sys.stdin)
    writer = io.TextIOWrapper(os.fdopen(os.dup(sink.fileno()), "wb"), encoding="utf-8",
                              errors="replace", line_buffering=True)
    null = open(os.devnull)
    status = 0
    try:
        os.dup2(sink.fileno(), 1)
        os.dup2(sink.fileno(), 2)
        if keep_in:
            os.dup2(null.fileno(), 0)
        sys.stdout = sys.stderr = writer
        sys.stdin = null
        try:
            state.execute(line)
            status = state.last_status
        except ShellError as e:
            print(e)
            status = state.last_status = 2
        except KeyboardInterrupt:
            print("^C")
            status = 130
    finally:
        try:
            writer.flush()
        except Exception:
            pass
        sys.stdout, sys.stderr, sys.stdin = old
        for fd, copy in saved.items():
            os.dup2(copy, fd)
            os.close(copy)
        null.close()
        sink.seek(0)
        data = sink.read().decode("utf-8", errors="replace")
        writer.close()
        sink.close()
    return data, status


class Entry:
    def __init__(self, kind: str, text: str, status: int = 0):
        self.kind, self.text, self.status = kind, text, status  # kind: cmd, out, note


# ---------------------------------------------------------------- the screen's state

class Ui:
    """Everything the panels show. Worker threads write here. The screen only reads."""

    def __init__(self, state: ShellState, palette: tui_theme.Palette, rounded: bool = True):
        self.state = state
        self.palette = palette
        self.box = ROUND if rounded else ASCII
        self.lock = threading.RLock()
        self.log = [Entry("note", "Welcome to myshell. Type a command, or press F1 for the keys.")]
        self.version = 0                 # bumps when the log changes
        self.input, self.cursor = "", 0
        self.hist_pos = -1
        self.draft = ""
        self.scroll = 0                  # rows scrolled up from the newest line
        self.running: Optional[str] = None
        self.worker: Optional[threading.Thread] = None
        self.zoom: Optional[str] = None
        self.help = False
        self.help_scroll = 0
        self.message = ""                # one line in the footer
        self.message_style = "bar.muted"
        self.confirm: Optional[Tuple[str, str, str]] = None   # (action, target, question)
        self.want_exit: Optional[int] = None
        # files
        self.show_hidden = False
        self.file_index = 0
        self.file_cache: Tuple = ()
        # processes
        self.procs: List[procs.Proc] = []
        self.cpu: Dict[int, float] = {}
        self.proc_sort = "cpu"
        self.proc_pid: Optional[int] = None
        self.proc_index = 0
        self.job_lines: List[str] = []
        # schedule
        self.sched_index = 0
        self.sched_rows: List[Tuple[str, int, str, str]] = []  # (kind, id, when, command)
        self.sched_log: List[str] = []
        self.loop = None
        self.cmd_rows = 0                # rows the command log can show. Set while drawing

    # --- log
    def add(self, kind: str, text: str, status: int = 0) -> None:
        with self.lock:
            self.log.append(Entry(kind, text, status))
            if len(self.log) > 400:
                del self.log[:100]
            self.version += 1
            self.scroll = 0

    def say(self, text: str, style: str = "bar.muted") -> None:
        self.message, self.message_style = text, style

    def invalidate(self) -> None:
        try:
            app = get_app()
        except Exception:
            return
        if self.loop is not None:
            self.loop.call_soon_threadsafe(app.invalidate)
        else:
            app.invalidate()

    # --- running a line
    def submit(self, line: str) -> None:
        line = line.strip()
        if not line:
            return
        if self.running:
            self.say("A command is still running. Press Ctrl+C to stop it.", "bar.warn")
            return
        if history.records_input() or True:
            try:
                history.add(line)
            except Exception:
                pass
        self.add("cmd", line)
        first = line.lstrip("!").split(None, 1)[0] if line.lstrip("!") else ""
        if line.startswith("!") or (first in NEEDS_TERMINAL and not self._quick(line)):
            self.run_in_terminal(line.lstrip("!"))
            return
        self.running = line
        self.worker = threading.Thread(target=self._work, args=(line,), daemon=True)
        self.worker.start()

    @staticmethod
    def _quick(line: str) -> bool:
        parts = line.split()
        return parts[0] == "top" and "-n" in parts  # top -n 1 prints and returns

    def _work(self, line: str) -> None:
        started = time.monotonic()
        text, status = "", 0
        try:
            text, status = run_captured(self.state, line)
        except ShellExit as e:
            self.want_exit = e.code
        except Exception as e:  # a command must never close the screen
            text, status = f"{type(e).__name__}: {e}\n", 1
        self.state.last_status = status
        self.state.last_duration = time.monotonic() - started
        with self.lock:
            for entry in reversed(self.log):
                if entry.kind == "cmd":
                    entry.status = status
                    break
        if text.strip("\n"):
            self.add("out", text.rstrip("\n"), status)
        else:
            with self.lock:
                self.version += 1
        try:
            self.state.save_history()
        except Exception:
            pass
        self.running = None
        self.after_command()
        self.invalidate()

    def after_command(self) -> None:
        """A command may have changed folders."""
        self.file_cache = ()
        if self.state.pending_traps:
            try:
                self.state.run_pending_traps()
            except Exception:
                pass

    def run_in_terminal(self, line: str) -> None:
        app = get_app()

        def go() -> None:
            sys.stdout.write("\x1b[0m")
            status = 0
            try:
                self.state.execute(line)
                status = self.state.last_status
            except ShellError as e:
                print(e, file=sys.stderr)
                status = 2
            except ShellExit as e:
                self.want_exit = e.code
            except KeyboardInterrupt:
                status = 130
            with self.lock:
                self.log[-1].status = status
                self.version += 1
            self.after_command()

        async def runner() -> None:
            await run_in_terminal(go)

        app.create_background_task(runner())

    def cancel(self) -> bool:
        """Stop the running command by ending the processes this shell started."""
        if not self.running:
            return False
        count = 0
        try:
            for p in procs.descendants(procs.list_processes(), os.getpid()):
                if p.pid != os.getpid():
                    try:
                        procs._send(p.pid, 15)
                        count += 1
                    except OSError:
                        pass
        except Exception:
            pass
        self.say(f"Stopped {count} process(es)." if count else
                 "Nothing to stop here. This command is not a separate process.", "bar.warn")
        return True

    # --- command line editing
    def insert(self, text: str) -> None:
        self.input = self.input[:self.cursor] + text + self.input[self.cursor:]
        self.cursor += len(text)
        self.hist_pos = -1

    def history_move(self, step: int) -> None:
        try:
            items = history.entries()
        except Exception:
            items = []
        if not items:
            return
        if self.hist_pos == -1:
            if step > 0:
                return
            self.draft = self.input
            self.hist_pos = len(items)
        self.hist_pos = max(0, min(len(items), self.hist_pos + step))
        self.input = self.draft if self.hist_pos == len(items) else items[self.hist_pos]
        if self.hist_pos == len(items):
            self.hist_pos = -1
        self.cursor = len(self.input)

    def suggestion(self) -> str:
        """The rest of the newest history line that starts with what is typed."""
        if not self.input or self.cursor != len(self.input):
            return ""
        try:
            for item in reversed(history.entries()):
                if item.startswith(self.input) and item != self.input:
                    return item[len(self.input):]
        except Exception:
            pass
        return ""

    def complete(self) -> None:
        before = self.input[:self.cursor]
        start, found = complete_line(self.state, before)
        if not found:
            self.say("No completions.", "bar.muted")
            return
        common = os.path.commonprefix(found)
        word = before[start:]
        if len(found) == 1:
            self.input = before[:start] + found[0] + self.input[self.cursor:]
            self.cursor = start + len(found[0])
            self.say("")
        else:
            if len(common) > len(word):
                self.input = before[:start] + common + self.input[self.cursor:]
                self.cursor = start + len(common)
            self.say("  ".join(sorted(found)[:12]) + ("  ..." if len(found) > 12 else ""), "bar.ok")


# ---------------------------------------------------------------- panels

class Panel(UIControl):
    """A framed box. body(width, height) returns the inner rows, and the cursor if there is one."""

    def __init__(self, ui: Ui, name: str, title: str, body: Callable, keys: KeyBindings,
                 meta: Optional[Callable[[], str]] = None):
        self.ui, self.name, self.title, self.body, self.keys, self.meta = ui, name, title, body, keys, meta
        self.window: Optional[Window] = None

    def is_focusable(self) -> bool:
        return True

    def get_key_bindings(self):
        return self.keys

    def focused(self) -> bool:
        try:
            return get_app().layout.current_control is self
        except Exception:
            return False

    def create_content(self, width: int, height: int) -> UIContent:
        box = self.ui.box
        iw, ih = max(1, width - 2), max(1, height - 2)
        focus = self.focused()
        b = "class:border.focus" if focus else "class:border"
        t = "class:title.focus" if focus else "class:title"
        rows_in, cursor = self.body(iw, ih)
        label = f" {self.title} "
        meta = f" {self.meta()} " if self.meta and self.meta() else ""
        fill = max(0, iw - cells(label) - cells(meta) - 1)
        top = [(b, box["tl"] + box["h"]), (t, label), (b, box["h"] * fill)]
        if meta:
            top.append(("class:muted", meta))
            top.append((b, box["h"]))
        else:
            top.append((b, box["h"]))
        top.append((b, box["tr"]))
        rows = [top]
        for r in range(ih):
            inner = rows_in[r] if r < len(rows_in) else []
            rows.append([(b, box["v"])] + fit(inner, iw, "class:text") + [(b, box["v"])])
        rows.append([(b, box["bl"] + box["h"] * iw + box["br"])])
        point = Point(x=cursor[0] + 1, y=cursor[1] + 1) if (cursor and focus) else None
        return UIContent(get_line=lambda i: rows[i], line_count=len(rows), cursor_position=point)

    def mouse_handler(self, mouse_event):
        if mouse_event.event_type == MouseEventType.MOUSE_UP and self.window is not None:
            try:
                get_app().layout.focus(self.window)
            except ValueError:
                pass
            return None
        if mouse_event.event_type in (MouseEventType.SCROLL_UP, MouseEventType.SCROLL_DOWN):
            step = 3 if mouse_event.event_type == MouseEventType.SCROLL_UP else -3
            if self.name == "cmd":
                self.ui.scroll = max(0, self.ui.scroll + step)
            return None
        return NotImplemented


def sel_style(selected: bool, base: str) -> str:
    return "class:select" if selected and base.startswith("class:text") else \
        ("class:select.accent" if selected else base)


# --- command panel

def cmd_lines(ui: Ui, width: int) -> List[Line]:
    out: List[Line] = []
    with ui.lock:
        entries = list(ui.log)
    for e in entries:
        if e.kind == "note":
            for row in wrap([("class:muted", e.text)], width):
                out.append(row)
        elif e.kind == "cmd":
            for k, row in enumerate(wrap([("class:accent", "❯ "), ("class:text", e.text)], width)):
                out.append(row)
            if e.status:
                out[-1] = out[-1] + [("class:error", f"  ✗ {e.status}")]
        else:
            for line in parse_output(e.text):
                out.extend(wrap(line or [("", "")], width))
    return out


def cmd_body(ui: Ui) -> Callable:
    def body(width: int, height: int):
        view = max(1, height - 1)
        ui.cmd_rows = view
        lines = cmd_lines(ui, width)
        end = len(lines) - ui.scroll
        ui.scroll = min(ui.scroll, max(0, len(lines) - view))
        end = len(lines) - ui.scroll
        shown = lines[max(0, end - view):end]
        rows = [[]] * (view - len(shown)) + shown  # newest line sits just above the prompt
        sug = ui.suggestion()
        if ui.running:
            spin = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"[int(time.monotonic() * 10) % 10]
            prompt = [("class:warn", f"{spin} "), ("class:muted", f"running {ui.running}  (Ctrl+C stops)")]
            cur = None
        else:
            prompt = [("class:accent", "❯ "), ("class:text", ui.input), ("class:muted", sug)]
            before = cells(ui.input[:ui.cursor])
            cur = (2 + before, height - 1)
        if ui.scroll:
            prompt = [("class:warn", f"↑{ui.scroll} ")] + prompt
            cur = None if cur is None else (cur[0] + len(str(ui.scroll)) + 2, cur[1])
        rows = rows + [prompt]
        return rows, cur
    return body


def cmd_keys(ui: Ui, go_next: Callable) -> KeyBindings:
    kb = KeyBindings()

    @kb.add(Keys.Any)
    def _(event):
        data = event.data
        if data and data.isprintable():
            ui.insert(data)

    @kb.add(Keys.BracketedPaste)
    def _(event):
        ui.insert(event.data.replace("\r\n", "\n").replace("\n", "; ").replace("\t", "    "))

    @kb.add("enter")
    def _(event):
        line, ui.input, ui.cursor, ui.hist_pos = ui.input, "", 0, -1
        ui.submit(line)

    @kb.add("backspace")
    def _(event):
        if ui.cursor:
            ui.input = ui.input[:ui.cursor - 1] + ui.input[ui.cursor:]
            ui.cursor -= 1

    @kb.add("delete")
    def _(event):
        ui.input = ui.input[:ui.cursor] + ui.input[ui.cursor + 1:]

    @kb.add("left")
    def _(event):
        ui.cursor = max(0, ui.cursor - 1)

    @kb.add("right")
    def _(event):
        if ui.cursor == len(ui.input) and ui.suggestion():
            ui.insert(ui.suggestion())
        else:
            ui.cursor = min(len(ui.input), ui.cursor + 1)

    @kb.add("home")
    @kb.add("c-a")
    def _(event):
        ui.cursor = 0

    @kb.add("end")
    @kb.add("c-e")
    def _(event):
        if ui.cursor == len(ui.input) and ui.suggestion():
            ui.insert(ui.suggestion())
        ui.cursor = len(ui.input)

    @kb.add("c-u")
    def _(event):
        ui.input, ui.cursor = ui.input[ui.cursor:], 0

    @kb.add("c-k")
    def _(event):
        ui.input = ui.input[:ui.cursor]

    @kb.add("c-w")
    def _(event):
        left = ui.input[:ui.cursor].rstrip()
        cut = left.rfind(" ") + 1
        ui.input = ui.input[:cut] + ui.input[ui.cursor:]
        ui.cursor = cut

    @kb.add("up")
    def _(event):
        ui.history_move(-1)

    @kb.add("down")
    def _(event):
        ui.history_move(1)

    @kb.add("tab")
    def _(event):
        ui.complete()

    @kb.add("pageup")
    def _(event):
        ui.scroll += max(1, ui.cmd_rows - 2)

    @kb.add("pagedown")
    def _(event):
        ui.scroll = max(0, ui.scroll - max(1, ui.cmd_rows - 2))

    @kb.add("c-l")
    def _(event):
        with ui.lock:
            ui.log = [Entry("note", "Cleared. Press F1 for the keys.")]
            ui.version += 1
        ui.scroll = 0

    @kb.add("c-c")
    def _(event):
        if not ui.cancel():
            ui.input, ui.cursor = "", 0

    @kb.add("c-d")
    def _(event):
        if not ui.input and not ui.running:
            ui.want_exit = ui.state.last_status
    return kb


# --- files panel

ICONS = {"dir": "▸", "py": "◆", "code": "◇", "text": "≡", "data": "▤", "image": "▣",
         "archive": "▥", "exec": "●", "file": "·", "up": "◂"}
CODE = {".js", ".ts", ".tsx", ".jsx", ".go", ".rs", ".c", ".h", ".cpp", ".java", ".sh", ".php",
        ".rb", ".css", ".html", ".vue", ".msh", ".sql"}
TEXT = {".md", ".txt", ".rst", ".toml", ".ini", ".cfg", ".yml", ".yaml", ".log"}
DATA = {".json", ".csv", ".xml", ".db", ".sqlite"}
IMAGE = {".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".ico", ".bmp"}
ARCHIVE = {".zip", ".tar", ".gz", ".tgz", ".7z", ".whl", ".bz2"}


def kind_of(path: str, is_dir: bool) -> str:
    if is_dir:
        return "dir"
    ext = os.path.splitext(path)[1].lower()
    if ext == ".py":
        return "py"
    for name, group in (("code", CODE), ("text", TEXT), ("data", DATA), ("image", IMAGE),
                        ("archive", ARCHIVE)):
        if ext in group:
            return name
    if os.name != "nt" and os.access(path, os.X_OK):
        return "exec"
    return "file"


KIND_STYLE = {"dir": "class:dir", "py": "class:success", "code": "class:info", "text": "class:text",
              "data": "class:warn", "image": "class:info", "archive": "class:warn",
              "exec": "class:success", "file": "class:muted", "up": "class:muted"}


def list_dir(path: str, hidden: bool) -> List[Tuple[str, str, int]]:
    """(name, kind, size) for the folder: parent link, folders, then files."""
    rows = []
    try:
        names = os.listdir(path)
    except OSError:
        names = []
    dirs, files = [], []
    for name in sorted(names, key=str.lower):
        if not hidden and name.startswith("."):
            continue
        full = os.path.join(path, name)
        is_dir = os.path.isdir(full)
        size = 0
        if not is_dir:
            try:
                size = os.path.getsize(full)
            except OSError:
                pass
        (dirs if is_dir else files).append((name + ("/" if is_dir else ""), kind_of(full, is_dir), size))
    if os.path.dirname(os.path.abspath(path)) != os.path.abspath(path):
        rows.append(("..", "up", 0))
    return rows + dirs + files


def files_rows(ui: Ui) -> List[Tuple[str, str, int]]:
    cwd = os.getcwd()
    try:
        stamp = os.stat(cwd).st_mtime
    except OSError:
        stamp = 0
    key = (cwd, stamp, ui.show_hidden)
    if ui.file_cache[:1] != (key,):
        ui.file_cache = (key, list_dir(cwd, ui.show_hidden))
    return ui.file_cache[1]


def files_body(ui: Ui) -> Callable:
    def body(width: int, height: int):
        rows = files_rows(ui)
        ui.file_index = max(0, min(ui.file_index, len(rows) - 1))
        top = max(0, min(ui.file_index - height // 2, len(rows) - height))
        out: List[Line] = []
        head = short_path(os.getcwd())
        out.append([("class:muted", head)])
        for i in range(top, min(len(rows), top + height - 1)):
            name, kind, size = rows[i]
            selected = i == ui.file_index
            st = sel_style(selected, KIND_STYLE[kind])
            sz = "" if kind in ("dir", "up") else human_size(size)
            room = max(1, width - 3 - len(sz) - 1)
            label = name if cells(name) <= room else name[:room - 1] + "…"
            gap = " " * max(1, width - 3 - cells(label) - len(sz))
            tail = "class:select" if selected else "class:muted"
            out.append([(st, f" {ICONS[kind]} {label}"), (tail, gap), (tail, sz)])
        return out, None
    return body


def to_cmd(ui: Ui) -> None:
    _focus(ui, "cmd")


def files_keys(ui: Ui) -> KeyBindings:
    kb = KeyBindings()

    def count() -> int:
        return len(files_rows(ui))

    @kb.add("up")
    @kb.add("k")
    def _(event):
        ui.file_index = max(0, ui.file_index - 1)

    @kb.add("down")
    @kb.add("j")
    def _(event):
        ui.file_index = min(count() - 1, ui.file_index + 1)

    @kb.add("pageup")
    def _(event):
        ui.file_index = max(0, ui.file_index - 10)

    @kb.add("pagedown")
    def _(event):
        ui.file_index = min(count() - 1, ui.file_index + 10)

    @kb.add("home")
    def _(event):
        ui.file_index = 0

    @kb.add("end")
    def _(event):
        ui.file_index = count() - 1

    def go(path: str) -> None:
        try:
            old = os.getcwd()
            os.chdir(path)
            os.environ["OLDPWD"], os.environ["PWD"] = old, os.getcwd()
            ui.file_index = 0
            ui.file_cache = ()
        except OSError as e:
            ui.say(f"{path}: {e.strerror}", "bar.bad")

    @kb.add("left")
    @kb.add("h")
    @kb.add("backspace")
    def _(event):
        came = os.path.basename(os.getcwd()) + "/"
        go("..")
        names = [r[0] for r in files_rows(ui)]
        ui.file_index = names.index(came) if came in names else 0

    @kb.add("enter")
    @kb.add("right")
    @kb.add("l")
    def _(event):
        rows = files_rows(ui)
        if not rows:
            return
        name, kind, _size = rows[ui.file_index]
        if kind in ("dir", "up"):
            go(name.rstrip("/"))
        else:
            ui.submit(f"edit {quote(name)}")

    @kb.add("e")
    def _(event):
        rows = files_rows(ui)
        if rows and rows[ui.file_index][1] not in ("dir", "up"):
            ui.submit(f"edit {quote(rows[ui.file_index][0])}")

    @kb.add("i")
    def _(event):
        rows = files_rows(ui)
        if rows and rows[ui.file_index][1] != "up":
            ui.insert(quote(rows[ui.file_index][0].rstrip("/")) + " ")
            to_cmd(ui)

    @kb.add(".")
    def _(event):
        ui.show_hidden = not ui.show_hidden
        ui.file_cache = ()
        ui.say("Hidden files " + ("shown" if ui.show_hidden else "hidden"))

    @kb.add("r")
    def _(event):
        ui.file_cache = ()
        ui.say("Refreshed")
    return kb


def quote(name: str) -> str:
    if not name or re.search(r"[^\w@%+=:,./~-]", name):
        return '"' + name.replace('"', '\\"') + '"'
    return name


# --- processes panel

def sample_processes(ui: Ui, previous: List[procs.Proc], gap: float) -> List[procs.Proc]:
    now = procs.list_processes()
    ui.cpu = procs.cpu_percent(previous, now, gap) if previous and gap > 0 else {}
    ui.procs = now
    return now


def sorted_procs(ui: Ui) -> List[procs.Proc]:
    key = {"cpu": lambda p: (-ui.cpu.get(p.pid, 0.0), -p.rss_kb),
           "mem": lambda p: -p.rss_kb, "pid": lambda p: p.pid}
    mode = ui.proc_sort if (ui.proc_sort != "cpu" or ui.cpu) else "mem"
    return sorted(ui.procs, key=key[mode])


def meter(value: float, cells_: int = 4) -> str:
    filled = int(round(min(100.0, value) / 100 * cells_))
    return "▮" * filled + "▯" * (cells_ - filled)


def procs_body(ui: Ui) -> Callable:
    def body(width: int, height: int):
        rows: List[Line] = []
        for line in ui.job_lines[:3]:
            rows.append([("class:info", " ◷ "), ("class:text", line)])
        listing = sorted_procs(ui)
        ui.proc_index = max(0, min(ui.proc_index, len(listing) - 1))
        if ui.proc_pid is not None:
            for k, p in enumerate(listing):
                if p.pid == ui.proc_pid:
                    ui.proc_index = k
                    break
        has_cpu = bool(ui.cpu)
        head = f"{'PID':>7} " + (f"{'CPU':>5} {'':4} " if has_cpu else "") + f"{'MEM':>6}  NAME"
        rows.append([("class:head", head)])
        room = height - len(rows)
        top = max(0, min(ui.proc_index - room // 2, len(listing) - room))
        for k in range(top, min(len(listing), top + room)):
            p = listing[k]
            sel = k == ui.proc_index
            base = "class:select" if sel else "class:text"
            usage = ui.cpu.get(p.pid, 0.0)
            hot = "class:select.accent" if sel else ("class:warn" if usage >= 50 else "class:info")
            frags = [(base, f"{p.pid:>7} ")]
            if has_cpu:
                bar = meter(usage)
                dim = "class:select" if sel else "class:muted"
                frags += [(base, f"{usage:>5.1f} "), (hot, bar.rstrip("▯")), (dim, bar[len(bar.rstrip("▯")):]), (base, " ")]
            frags += [(base, f"{procs._format_mem(p.rss_kb):>6}  {p.name}")]
            rows.append(frags)
        return rows, None
    return body


def selected_proc(ui: Ui) -> Optional[procs.Proc]:
    listing = sorted_procs(ui)
    return listing[ui.proc_index] if 0 <= ui.proc_index < len(listing) else None


def procs_keys(ui: Ui) -> KeyBindings:
    kb = KeyBindings()

    def move(step: int) -> None:
        listing = sorted_procs(ui)
        if not listing:
            return
        ui.proc_index = max(0, min(len(listing) - 1, ui.proc_index + step))
        ui.proc_pid = listing[ui.proc_index].pid

    @kb.add("up")
    @kb.add("k")
    def _(event):
        move(-1)

    @kb.add("down")
    @kb.add("j")
    def _(event):
        move(1)

    @kb.add("pageup")
    def _(event):
        move(-10)

    @kb.add("pagedown")
    def _(event):
        move(10)

    @kb.add("x")
    @kb.add("delete")
    def _(event):
        p = selected_proc(ui)
        if p:
            ui.confirm = ("term", str(p.pid), f"Stop {p.name} (pid {p.pid})?")

    @kb.add("X")
    def _(event):
        p = selected_proc(ui)
        if p:
            ui.confirm = ("kill", str(p.pid), f"Force kill {p.name} (pid {p.pid})?")

    @kb.add("s")
    def _(event):
        order = ["cpu", "mem", "pid"]
        ui.proc_sort = order[(order.index(ui.proc_sort) + 1) % 3]
        ui.say(f"Sorted by {ui.proc_sort}")
    return kb


# --- schedule panel

def refresh_schedule(ui: Ui) -> None:
    import datetime as dt
    data = sched.load()
    rows = []
    now = dt.datetime.now()
    for t in data["cron"]:
        try:
            nxt = sched.CronSpec(t["expr"]).next_after(now)
            when = nxt.strftime("%a %H:%M") if nxt else "never"
        except sched.CronError:
            when = "bad"
        rows.append(("cron", t["id"], when, f"{t['expr']}  {t['cmd']}"))
    for j in sorted(data["at"], key=lambda j: j["when"]):
        rows.append(("at", j["id"], dt.datetime.fromtimestamp(j["when"]).strftime("%a %H:%M"), j["cmd"]))
    ui.sched_rows = rows
    try:
        with open(sched.log_file(), encoding="utf-8", errors="replace") as f:
            ui.sched_log = f.read().splitlines()[-4:]
    except OSError:
        ui.sched_log = []


def sched_body(ui: Ui) -> Callable:
    def body(width: int, height: int):
        rows: List[Line] = []
        ui.sched_index = max(0, min(ui.sched_index, len(ui.sched_rows) - 1))
        if not ui.sched_rows:
            rows.append([("class:muted", " No tasks yet.")])
            rows.append([("class:muted", " Press a to add one.")])
        for k, (kind, tid, when, cmd) in enumerate(ui.sched_rows):
            sel = k == ui.sched_index
            base = "class:select" if sel else "class:text"
            tag = "class:select.accent" if sel else ("class:info" if kind == "cron" else "class:warn")
            rows.append([(tag, f" {kind:<5}"), (base, f"#{tid:<3}"), ("class:select.accent" if sel else "class:accent", f"{when:<10} "),
                         (base, cmd)])
        room = height - len(rows)
        if ui.sched_log and room >= 3:
            rows.append([])
            rows.append([("class:head", " Last output")])
            for line in ui.sched_log[-(room - 2):]:
                rows.append([("class:muted", " " + line)])
        return rows[:height], None
    return body


def sched_keys(ui: Ui) -> KeyBindings:
    kb = KeyBindings()

    @kb.add("up")
    @kb.add("k")
    def _(event):
        ui.sched_index = max(0, ui.sched_index - 1)

    @kb.add("down")
    @kb.add("j")
    def _(event):
        ui.sched_index = min(max(0, len(ui.sched_rows) - 1), ui.sched_index + 1)

    @kb.add("d")
    @kb.add("delete")
    def _(event):
        if ui.sched_rows:
            kind, tid, _w, cmd = ui.sched_rows[ui.sched_index]
            ui.confirm = ("remove", f"{kind} {tid}", f"Remove {kind} #{tid} ({cmd[:30]})?")

    @kb.add("a")
    def _(event):
        ui.input = "cron add '*/5 * * * *' "
        ui.cursor = len(ui.input)
        to_cmd(ui)
        ui.say("Edit the schedule and the command, then press Enter. at +10m CMD runs once.", "bar.ok")

    @kb.add("r")
    def _(event):
        if ui.sched_rows and ui.sched_rows[ui.sched_index][0] == "cron":
            ui.submit(f"cron run {ui.sched_rows[ui.sched_index][1]}")
    return kb


def confirm_keys(ui: Ui) -> KeyBindings:
    """While a question is open, y and n answer it and every other key is ignored."""
    kb = KeyBindings()
    active = Condition(lambda: ui.confirm is not None)

    @kb.add("y", filter=active, eager=True)
    def _(event):
        action, target, _q = ui.confirm
        ui.confirm = None
        if action in ("term", "kill"):
            try:
                import signal
                sig = signal.SIGTERM if action == "term" else getattr(signal, "SIGKILL", signal.SIGTERM)
                procs._send(int(target), int(sig))
                ui.say(f"Sent {'SIGTERM' if action == 'term' else 'SIGKILL'} to {target}", "bar.ok")
            except OSError as e:
                ui.say(f"kill {target}: {e.strerror or e}", "bar.bad")
        elif action == "remove":
            kind, tid = target.split()
            ui.submit(f"{'cron' if kind == 'cron' else 'at'} rm {tid}")

    @kb.add("n", filter=active, eager=True)
    @kb.add("escape", filter=active, eager=True)
    @kb.add("c-c", filter=active, eager=True)
    def _(event):
        ui.confirm = None
        ui.say("Cancelled")

    @kb.add(Keys.Any, filter=active)
    def _(event):
        pass
    return kb


# ---------------------------------------------------------------- the application

PANELS = ["cmd", "files", "procs", "cron"]
HELP = [
    ("Everywhere", ""),
    ("F1", "this help"), ("F2 F3 F4", "files, processes, schedule"),
    ("F5 or Esc", "back to the commands"), ("Tab", "next panel"),
    ("Ctrl+Q", "quit"), ("z", "zoom the panel full screen"),
    ("Commands", ""),
    ("Enter", "run"), ("Up Down", "history"), ("Tab", "complete"), ("Right or End", "accept the grey hint"),
    ("PgUp PgDn", "scroll the output"), ("Ctrl+C", "stop a running command"), ("Ctrl+L", "clear"),
    ("!cmd", "run in the real terminal"),
    ("Files", ""),
    ("Enter l", "open a folder or edit a file"), ("h Backspace", "parent folder"),
    ("i", "put the name on the command line"), ("e", "edit"), (".", "hidden files"),
    ("Processes", ""),
    ("x", "stop the process"), ("X", "force kill"), ("s", "sort by cpu, memory or pid"),
    ("Schedule", ""),
    ("a", "add a task"), ("d", "remove"), ("r", "run a cron task now"),
]
_APP: Dict[str, object] = {}


def _focus(ui: Ui, name: str) -> None:
    """Move focus to a panel. A panel the screen is too small to show goes full screen."""
    app = get_app()
    panel_windows = _APP["windows"]
    cols = app.output.get_size().columns
    if name != "cmd" and not visible_normally(name, cols):
        ui.zoom = name
    elif ui.zoom not in (None, name):
        ui.zoom = None if name != "cmd" or ui.zoom != "cmd" else ui.zoom
    if name == "cmd" and ui.zoom not in (None, "cmd"):
        ui.zoom = None
    ui.help = False
    app.layout.focus(panel_windows[name])


def visible_normally(name: str, cols: int) -> bool:
    if name == "cmd":
        return True
    if name == "files":
        return cols >= WIDE
    return cols >= MEDIUM


def build_app(state: ShellState, input=None, output=None, rounded: Optional[bool] = None) -> Tuple[Application, Ui]:
    palette = tui_theme.active_palette()
    if rounded is None:
        rounded = os.environ.get("MYSHELL_ASCII") != "1" and os.environ.get("TERM") != "dumb"
    ui = Ui(state, palette, rounded)
    windows: Dict[str, Window] = {}
    _APP["windows"] = windows

    def nxt(step: int) -> None:
        cols = get_app().output.get_size().columns
        names = [n for n in PANELS if visible_normally(n, cols)] if ui.zoom is None else PANELS
        here = get_app().layout.current_control
        cur = next((n for n in names if windows[n].content is here), "cmd")
        _focus(ui, names[(names.index(cur) + step) % len(names)] if cur in names else "cmd")

    controls = {
        "cmd": Panel(ui, "cmd", "Commands", cmd_body(ui), cmd_keys(ui, nxt),
                     lambda: f"✓ {ui.state.last_status}" if not ui.state.last_status else f"✗ {ui.state.last_status}"),
        "files": Panel(ui, "files", "Files", files_body(ui), files_keys(ui),
                       lambda: f"{len(files_rows(ui))} items"),
        "procs": Panel(ui, "procs", "Processes", procs_body(ui), procs_keys(ui),
                       lambda: f"{len(ui.procs)} · by {ui.proc_sort}"),
        "cron": Panel(ui, "cron", "Schedule", sched_body(ui), sched_keys(ui),
                      lambda: f"{len(ui.sched_rows)} tasks"),
    }
    for name, ctl in controls.items():
        if name == "cmd":
            size = Dimension(min=40, preferred=1000)
        else:
            size = Dimension.exact(30 if name == "files" else 42)
        win = Window(content=ctl, style="class:panel", always_hide_cursor=(name != "cmd"), width=size)
        ctl.window = win
        windows[name] = win

    cols = lambda: get_app().output.get_size().columns  # noqa: E731
    rows_ = lambda: get_app().output.get_size().rows  # noqa: E731
    zoom = lambda: ui.zoom  # noqa: E731
    too_small = Condition(lambda: cols() < MIN_COLS or rows_() < MIN_ROWS)
    show_files = Condition(lambda: (zoom() is None and cols() >= WIDE) or zoom() == "files")
    show_cmd = Condition(lambda: zoom() in (None, "cmd"))
    show_procs = Condition(lambda: (zoom() is None and cols() >= MEDIUM) or zoom() == "procs")
    show_cron = Condition(lambda: (zoom() is None and cols() >= MEDIUM) or zoom() == "cron")
    show_right = show_procs | show_cron

    def header():
        c = cols()
        left = [("class:bar.brand", " ❯ myshell "), ("class:bar", "  " + short_path(os.getcwd()))]
        try:
            branch = git_branch()
        except Exception:
            branch = ""
        if branch:
            left.append(("class:bar.warn", f"  ⎇ {branch}"))
        right = f"{theme.active().name}  {time.strftime('%H:%M')} "
        pad = max(1, c - sum(cells(t) for _, t in left) - cells(right))
        return left + [("class:bar", " " * pad), ("class:bar.muted", right)]

    def footer():
        c = cols()
        if ui.confirm:
            return [("class:bar.warn", f" {ui.confirm[2]} "), ("class:bar.key", "y"), ("class:bar.muted", " yes   "),
                    ("class:bar.key", "n"), ("class:bar.muted", " no")]
        here = get_app().layout.current_control
        name = next((n for n in PANELS if windows[n].content is here), "cmd")
        hints = {
            "cmd": [("Enter", "run"), ("Tab", "complete"), ("↑↓", "history"), ("F2-F4", "panels"), ("Ctrl+Q", "quit")],
            "files": [("↑↓", "move"), ("Enter", "open"), ("h", "up"), ("i", "insert"), (".", "hidden"), ("Esc", "commands")],
            "procs": [("↑↓", "move"), ("x", "stop"), ("X", "kill"), ("s", "sort"), ("Esc", "commands")],
            "cron": [("↑↓", "move"), ("a", "add"), ("d", "remove"), ("r", "run now"), ("Esc", "commands")],
        }[name]
        out: Line = []
        for key, text in hints:
            out += [("class:bar.key", f" {key}"), ("class:bar.muted", f" {text} ")]
        if ui.message:
            out += [("class:bar.muted", "│ "), ("class:" + ui.message_style, ui.message)]
        out = fit(out, c, "class:bar")
        return out

    too_small_text = FormattedTextControl(lambda: [("class:bar.warn", "\n  Make the window bigger (at least 40 x 10).")])
    right_col = HSplit([ConditionalContainer(windows["procs"], filter=show_procs),
                        ConditionalContainer(windows["cron"], filter=show_cron)])
    body = VSplit([ConditionalContainer(windows["files"], filter=show_files),
                   ConditionalContainer(windows["cmd"], filter=show_cmd),
                   ConditionalContainer(right_col, filter=show_right)])
    root = HSplit([Window(FormattedTextControl(header), height=1, style="class:bar"),
                   ConditionalContainer(body, filter=~too_small),
                   ConditionalContainer(Window(too_small_text), filter=too_small),
                   Window(FormattedTextControl(footer), height=1, style="class:bar")])

    help_ctl = Panel(ui, "help", "Keys", lambda w, h: (help_rows(ui, w, h), None), KeyBindings())
    help_win = Window(content=help_ctl, style="class:panel", always_hide_cursor=True,
                      width=lambda: Dimension.exact(min(64, cols() - 2)),
                      height=lambda: Dimension.exact(max(5, min(len(HELP) + 4, rows_() - 2))))
    float_root = FloatContainer(root, floats=[
        Float(content=ConditionalContainer(help_win, filter=Condition(lambda: ui.help)))])

    kb = KeyBindings()
    help_open = Condition(lambda: ui.help)

    @kb.add("c-q", eager=True)
    def _(event):
        ui.want_exit = state.last_status

    @kb.add("f1", eager=True)
    def _(event):
        ui.help = not ui.help

    @kb.add("escape", filter=help_open, eager=True)
    @kb.add("q", filter=help_open, eager=True)
    def _(event):
        ui.help = False

    @kb.add("down", filter=help_open, eager=True)
    @kb.add("pagedown", filter=help_open, eager=True)
    def _(event):
        ui.help_scroll += 1 if event.key_sequence[0].key == "down" else 8

    @kb.add("up", filter=help_open, eager=True)
    @kb.add("pageup", filter=help_open, eager=True)
    def _(event):
        ui.help_scroll = max(0, ui.help_scroll - (1 if event.key_sequence[0].key == "up" else 8))

    @kb.add("f2", eager=True)
    def _(event):
        _focus(ui, "files")

    @kb.add("f3", eager=True)
    def _(event):
        _focus(ui, "procs")

    @kb.add("f4", eager=True)
    def _(event):
        _focus(ui, "cron")

    @kb.add("f5", eager=True)
    def _(event):
        ui.zoom = None
        _focus(ui, "cmd")

    in_cmd = Condition(lambda: get_app().layout.current_control is controls["cmd"])
    not_cmd = ~in_cmd

    @kb.add("escape", filter=not_cmd & ~help_open, eager=True)
    def _(event):
        ui.zoom = None
        _focus(ui, "cmd")

    @kb.add("tab", filter=not_cmd, eager=True)
    def _(event):
        nxt(1)

    @kb.add("s-tab", eager=True)
    def _(event):
        nxt(-1)

    @kb.add("z", filter=not_cmd & ~Condition(lambda: ui.confirm is not None))
    def _(event):
        here = get_app().layout.current_control
        name = next((n for n in PANELS if windows[n].content is here), "cmd")
        ui.zoom = None if ui.zoom == name else name

    kb = _merge(kb, confirm_keys(ui))

    app = Application(layout=Layout(float_root, focused_element=windows["cmd"]), key_bindings=kb,
                      style=Style.from_dict(tui_theme.style_rules(palette)), full_screen=True,
                      mouse_support=True, refresh_interval=0.5, input=input, output=output)
    app.ttimeoutlen = 0.05

    def tick(_app) -> None:
        if ui.want_exit is not None and not getattr(ui, "exiting", False):
            ui.exiting = True
            _app.exit(result=ui.want_exit)
        for note in sched.SCHEDULER.take_notices():
            ui.add("note", note)
    app.before_render += tick
    return app, ui


def _merge(first: KeyBindings, second: KeyBindings) -> KeyBindings:
    from prompt_toolkit.key_binding import merge_key_bindings
    return merge_key_bindings([second, first])  # the open question gets the keys first


def help_rows(ui: Ui, width: int, height: int) -> List[Line]:
    rows: List[Line] = []
    for key, text in HELP:
        if not text:
            rows.append([("class:accent", f" {key}")])
        else:
            rows.append([("class:info", f"   {key:<13}"), ("class:text", text)])
    room = max(1, height - 1)
    ui.help_scroll = max(0, min(ui.help_scroll, max(0, len(rows) - room)))
    shown = rows[ui.help_scroll:ui.help_scroll + room]
    more = "  ↑↓ scroll" if len(rows) > room else ""
    return shown + [[("class:muted", f" Esc or F1 closes{more}")]]


# ---------------------------------------------------------------- background sampling

class Sampler(threading.Thread):
    """Reads processes, jobs and scheduled tasks now and then, away from the screen's drawing."""

    def __init__(self, ui: Ui):
        super().__init__(daemon=True)
        self.ui = ui
        self.stop_flag = threading.Event()
        self.gap = 1.5 if os.name != "nt" else 4.0

    def run(self) -> None:
        previous: List[procs.Proc] = []
        last = time.monotonic()
        while not self.stop_flag.is_set():
            try:
                now = time.monotonic()
                previous = sample_processes(self.ui, previous, now - last)
                last = now
                self.ui.job_lines = self._jobs()
                refresh_schedule(self.ui)
                self.ui.invalidate()
            except Exception:
                pass
            self.stop_flag.wait(self.gap)

    def _jobs(self) -> List[str]:
        from .builtins import builtin_jobs
        buf = io.StringIO()
        try:
            builtin_jobs(self.ui.state, [], buf, buf)
        except Exception:
            return []
        return [ANSI_RE.sub("", line) for line in buf.getvalue().splitlines() if line.strip()]


def run_tui(state: ShellState, use_rc: bool = True, input=None, output=None) -> int:
    """Open the full-screen app. Returns the exit status."""
    if input is None and not (sys.stdin.isatty() and sys.stdout.isatty()):
        print("myshell: --tui needs a terminal", file=sys.stderr)
        return 1
    if use_rc and os.path.isfile(state.rc_file):
        from .builtins import source_file
        buf = io.StringIO()
        try:
            source_file(state, state.rc_file, buf)
        except Exception:
            pass
        state.last_status = 0
    state.load_history()
    if input is None:
        from prompt_toolkit.input import create_input
        from prompt_toolkit.output import create_output
        # the screen keeps its own copies of the terminal handles, because commands
        # temporarily point file descriptors 0, 1 and 2 somewhere else
        input = create_input(stdin=os.fdopen(os.dup(0), "r"))
        output = create_output(stdout=os.fdopen(os.dup(1), "w"))
    app, ui = build_app(state, input=input, output=output)
    sched.start_background()
    sampler = Sampler(ui)
    sampler.start()
    import asyncio

    def pre_run() -> None:
        ui.loop = asyncio.get_event_loop()
    try:
        code = app.run(pre_run=pre_run)
    finally:
        sampler.stop_flag.set()
        sched.SCHEDULER.stop()
        try:
            state.save_history()
        except Exception:
            pass
    return code if isinstance(code, int) else 0
