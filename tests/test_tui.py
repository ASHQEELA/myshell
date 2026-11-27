import asyncio
import os
import sys
import threading
import time

import pytest
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput

from myshell import sched, tui, tui_theme

KEYS = {"F2": "\x1bOQ", "F3": "\x1bOR", "F4": "\x1bOS", "F5": "\x1b[15~", "esc": "\x1b",
        "up": "\x1b[A", "down": "\x1b[B", "enter": "\r", "ctrl_q": "\x11"}


def contrast(a: str, b: str) -> float:
    def lin(c):
        c /= 255
        return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4

    def lum(h):
        h = h.lstrip("#")
        r, g, b_ = (int(h[i:i + 2], 16) for i in (0, 2, 4))
        return 0.2126 * lin(r) + 0.7152 * lin(g) + 0.0722 * lin(b_)
    hi, lo = sorted([lum(a), lum(b)], reverse=True)
    return (hi + 0.05) / (lo + 0.05)


# ---------------------------------------------------------------- colors

@pytest.mark.parametrize("name", sorted(tui_theme.PALETTES))
def test_every_text_color_passes_aa_on_its_background(name):
    p = tui_theme.PALETTES[name]
    for role in tui_theme.TEXT_ROLES:
        assert contrast(getattr(p, role), p.panel_bg) >= 4.5, (name, role)
    for role in ("text", "muted", "accent", "success", "warn", "error"):
        assert contrast(getattr(p, role), p.app_bg) >= 4.5, (name, role, "app")
    assert contrast(p.text, p.select_bg) >= 4.5
    assert contrast(p.accent, p.select_bg) >= 4.5
    assert contrast(p.on_accent, p.accent) >= 4.5


def test_every_theme_has_a_palette():
    from myshell import theme
    assert set(theme.THEMES) <= set(tui_theme.PALETTES)


def test_no_color_uses_the_mono_palette(monkeypatch):
    monkeypatch.setenv("NO_COLOR", "1")
    assert tui_theme.active_palette().name == "mono"


def test_the_style_table_covers_every_class_the_screen_uses():
    rules = tui_theme.style_rules(tui_theme.DARK)
    src = open(tui.__file__, encoding="utf-8").read()
    import re
    used = set(re.findall(r"class:([a-z.]+)", src))
    assert used <= set(rules), used - set(rules)


# ---------------------------------------------------------------- text helpers

def test_fit_pads_and_cuts_to_the_width():
    assert tui.cells("".join(t for _, t in tui.fit([("", "abc")], 6))) == 6
    cut = tui.fit([("", "abcdefghij")], 5)
    assert "".join(t for _, t in cut) == "abcd…"


def test_fit_counts_wide_characters_as_two_cells():
    cut = tui.fit([("", "日本語日本語")], 6)
    assert tui.cells("".join(t for _, t in cut)) == 6


def test_wrap_breaks_long_lines_and_drops_a_leading_space_on_the_next_row():
    rows = tui.wrap([("", "aaaa bbbb")], 5)
    assert ["".join(t for _, t in r) for r in rows] == ["aaaa ", "bbbb"]


def test_parse_output_keeps_lines_and_reads_color_codes():
    lines = tui.parse_output("one\n\x1b[31mred\x1b[0m\n")
    assert "".join(t for _, t in lines[0]) == "one"
    assert any("red" in s for s, _ in lines[1]) or "".join(t for _, t in lines[1]) == "red"


@pytest.mark.parametrize("n,text", [(0, "0B"), (900, "900B"), (2048, "2.0K"), (5 * 1024 ** 2, "5.0M")])
def test_human_size(n, text):
    assert tui.human_size(n) == text


@pytest.mark.parametrize("name,quoted", [("a.txt", "a.txt"), ("my file.txt", '"my file.txt"'),
                                         ("it's", '"it\'s"')])
def test_quote(name, quoted):
    assert tui.quote(name) == quoted


def test_kind_of_uses_the_extension(tmp_path):
    assert tui.kind_of("x.py", False) == "py"
    assert tui.kind_of("x.md", False) == "text"
    assert tui.kind_of("x.zip", False) == "archive"
    assert tui.kind_of("anything", True) == "dir"


def test_list_dir_puts_the_parent_then_folders_then_files(tmp_path):
    (tmp_path / "b_dir").mkdir()
    (tmp_path / "a.txt").write_text("x")
    (tmp_path / ".hidden").write_text("x")
    names = [n for n, _, _ in tui.list_dir(str(tmp_path), False)]
    assert names == ["..", "b_dir/", "a.txt"]
    assert ".hidden" in [n for n, _, _ in tui.list_dir(str(tmp_path), True)]


# ---------------------------------------------------------------- running commands

def test_run_captured_catches_builtin_output_and_status(state):
    text, status = tui.run_captured(state, "echo hello")
    assert (text, status) == ("hello\n", 0)


def test_run_captured_catches_errors_and_a_failing_status(state):
    text, status = tui.run_captured(state, "cat /no/such/file")
    assert status != 0 and text.strip()


def test_run_captured_reports_a_syntax_error_without_raising(state):
    text, status = tui.run_captured(state, 'echo "open')
    assert status == 2 and text.strip()


@pytest.mark.skipif(os.name == "nt", reason="Unix child process capture")
def test_run_captured_catches_what_child_programs_print(state):
    text, _ = tui.run_captured(state, f'"{sys.executable}" -c "print(42)"')
    assert text.strip() == "42"


def test_run_captured_puts_the_streams_back(state):
    before = (sys.stdout, sys.stderr, sys.stdin)
    tui.run_captured(state, "echo hi")
    assert (sys.stdout, sys.stderr, sys.stdin) == before


# ---------------------------------------------------------------- driving the whole screen

class Session:
    """Run the app against a pipe and send keys from a second thread."""

    def __init__(self, state):
        self.state = state

    def run(self, script, size=None):
        with create_pipe_input() as inp:
            app, ui = tui.build_app(self.state, input=inp, output=DummyOutput())
            self.app, self.ui = app, ui
            errors = []

            def drive():
                try:
                    time.sleep(0.4)
                    script(inp, ui)
                except Exception as e:  # a failed wait must end the app, not hang the test
                    errors.append(e)
                finally:
                    inp.send_text(KEYS["ctrl_q"])
            t = threading.Thread(target=drive, daemon=True)
            t.start()

            def pre():
                ui.loop = asyncio.get_event_loop()
            code = app.run(pre_run=pre)
            t.join(5)
            if errors:
                raise errors[0]
            return code


def wait(cond, seconds=6.0, what="condition"):
    end = time.time() + seconds
    while time.time() < end:
        if cond():
            return
        time.sleep(0.05)
    raise AssertionError(f"timed out waiting for {what}")


def idle(ui):
    return lambda: ui.running is None and ui.worker is not None and not ui.worker.is_alive()


def test_a_typed_command_runs_and_its_output_shows_in_the_log(state):
    def script(inp, ui):
        inp.send_text("echo hello world\r")
        wait(lambda: any(e.kind == "out" for e in ui.log), what="output")
    s = Session(state)
    s.run(script)
    kinds = [(e.kind, e.text) for e in s.ui.log]
    assert ("cmd", "echo hello world") in kinds and ("out", "hello world") in kinds


def test_a_failed_command_marks_its_line_with_the_status(state):
    def script(inp, ui):
        inp.send_text("cat /no/such/file\r")
        wait(lambda: any(e.kind == "out" for e in ui.log), what="error text")
    s = Session(state)
    s.run(script)
    cmd = [e for e in s.ui.log if e.kind == "cmd"][0]
    assert cmd.status != 0


def test_cd_changes_the_folder_the_files_panel_shows(state, dirs):
    _, work = dirs
    (work / "inner").mkdir()

    def script(inp, ui):
        inp.send_text("cd inner\r")
        wait(lambda: os.path.basename(os.getcwd()) == "inner", what="cd")
        wait(lambda: ui.running is None, what="idle")
    Session(state).run(script)


def test_exit_command_closes_the_screen_with_its_status(state):
    def script(inp, ui):
        inp.send_text("exit 3\r")
        time.sleep(1.5)
    assert Session(state).run(script) == 3


def test_ctrl_q_quits(state):
    assert Session(state).run(lambda inp, ui: None) == 0


def test_up_arrow_brings_back_the_last_command(state):
    def script(inp, ui):
        inp.send_text("echo again\r")
        wait(lambda: any(e.kind == "out" for e in ui.log), what="output")
        wait(lambda: ui.running is None, what="idle")
        inp.send_text(KEYS["up"])
        wait(lambda: ui.input == "echo again", what="history")
    Session(state).run(script)


def test_tab_completes_a_command_name(state):
    def script(inp, ui):
        inp.send_text("ech\t")
        wait(lambda: ui.input == "echo", what="completion")
    Session(state).run(script)


def test_f2_opens_the_files_panel_and_enter_opens_a_folder(state, dirs):
    _, work = dirs
    (work / "sub").mkdir()
    (work / "sub" / "x.txt").write_text("x")

    def script(inp, ui):
        inp.send_text(KEYS["F2"])
        wait(lambda: ui.zoom == "files", what="files focus")
        inp.send_text(KEYS["down"])
        wait(lambda: ui.file_index == 1, what="move")
        inp.send_text(KEYS["enter"])
        wait(lambda: os.path.basename(os.getcwd()) == "sub", what="open folder")
    Session(state).run(script)


def test_escape_returns_to_the_commands(state):
    def script(inp, ui):
        inp.send_text(KEYS["F3"])
        wait(lambda: ui.zoom == "procs", what="procs")
        inp.send_text(KEYS["esc"])
        wait(lambda: ui.zoom is None, what="back")
    Session(state).run(script)


def test_f1_toggles_the_help(state):
    def script(inp, ui):
        inp.send_text("\x1bOP")
        wait(lambda: ui.help, what="help open")
        inp.send_text("\x1bOP")
        wait(lambda: not ui.help, what="help closed")
    Session(state).run(script)


def test_removing_a_task_asks_first_and_y_removes_it(state):
    import io
    sched.builtin_cron(state, ["add", "* * * * *", "echo", "x"], io.StringIO(), io.StringIO())

    def script(inp, ui):
        sched_refresh = lambda: tui.refresh_schedule(ui)  # noqa: E731
        sched_refresh()
        inp.send_text(KEYS["F4"])
        wait(lambda: ui.zoom == "cron", what="cron focus")
        inp.send_text("d")
        wait(lambda: ui.confirm is not None, what="question")
        inp.send_text("y")
        wait(lambda: ui.confirm is None, what="answer")
        wait(lambda: not sched.load()["cron"], what="removal")
    Session(state).run(script)


def test_n_cancels_the_question(state):
    import io
    sched.builtin_cron(state, ["add", "* * * * *", "echo", "x"], io.StringIO(), io.StringIO())

    def script(inp, ui):
        tui.refresh_schedule(ui)
        inp.send_text(KEYS["F4"])
        wait(lambda: ui.zoom == "cron", what="cron focus")
        inp.send_text("d")
        wait(lambda: ui.confirm is not None, what="question")
        inp.send_text("n")
        wait(lambda: ui.confirm is None, what="answer")
    Session(state).run(script)
    assert len(sched.load()["cron"]) == 1


def test_the_log_is_capped(state):
    ui = tui.Ui(state, tui_theme.DARK)
    for i in range(700):
        ui.add("out", f"line {i}")
    assert len(ui.log) < 450


def test_scroll_resets_when_new_output_arrives(state):
    ui = tui.Ui(state, tui_theme.DARK)
    ui.scroll = 5
    ui.add("out", "new")
    assert ui.scroll == 0


def test_cpu_percent_feeds_the_process_panel(state):
    ui = tui.Ui(state, tui_theme.DARK)
    a = [tui.procs.Proc(1, 0, "x", 100, "R", 100)]
    b = [tui.procs.Proc(1, 0, "x", 100, "R", 200)]
    ui.procs = b
    ui.cpu = tui.procs.cpu_percent(a, b, 1.0)
    assert tui.sorted_procs(ui)[0].pid == 1


def test_run_tui_needs_a_terminal(state, capsys):
    assert tui.run_tui(state) == 1
    assert "needs a terminal" in capsys.readouterr().err


def test_the_flag_and_the_builtin_exist():
    from myshell import builtins, cli
    assert "tui" in builtins.BUILTINS
    assert "--tui" in cli.USAGE
