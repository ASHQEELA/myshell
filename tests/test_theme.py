import os

import pytest

from conftest import unix_only
from myshell import theme
from myshell.prompt import build_prompt
from myshell.more_builtins import builtin_theme
import io

CH = "#" if hasattr(os, "geteuid") and os.geteuid() == 0 else "$"


@pytest.fixture
def home(dirs, state):
    """The temporary home folder. The state fixture points HOME at it."""
    return dirs[0]


@pytest.fixture
def st(state):
    return state


def run(st, *args, tty=False):
    out, err = io.StringIO(), io.StringIO()
    if tty:
        out.isatty = lambda: True
    code = builtin_theme(st, list(args), out, err)
    return code, out.getvalue(), err.getvalue()


class TestThemes:
    def test_every_theme_is_complete(self):
        keys = {"command", "badcommand", "keyword", "string", "variable", "operator", "comment", "plain"}
        for t in theme.THEMES.values():
            assert set(t.syntax) == keys and t.about
            if t.colorful:
                assert t.dir and t.fail and t.branch and t.pip

    def test_default_is_the_current_look(self):
        t = theme.THEMES["default"]
        assert (t.dir, t.fail, t.branch, t.pip) == ("1;32", "1;31", "35", "33")

    def test_default_when_nothing_is_set(self, home):
        assert theme.active().name == "default"

    def test_environment_chooses_the_theme(self, home, monkeypatch):
        monkeypatch.setenv("MYSHELL_THEME", "ocean")
        theme.reset()
        assert theme.active().name == "ocean"

    def test_unknown_name_in_environment_falls_back(self, home, monkeypatch):
        monkeypatch.setenv("MYSHELL_THEME", "nope")
        theme.reset()
        assert theme.active().name == "default"

    def test_saved_theme_is_read_back(self, home):
        theme.select("forest")
        theme.reset()
        assert theme.active().name == "forest"
        assert (home / ".myshell_theme").read_text().strip() == "forest"

    def test_environment_beats_the_saved_file(self, home, monkeypatch):
        theme.select("forest")
        monkeypatch.setenv("MYSHELL_THEME", "sunset")
        theme.reset()
        assert theme.active().name == "sunset"

    def test_selecting_default_removes_the_file(self, home):
        theme.select("ocean")
        theme.select("default")
        assert not (home / ".myshell_theme").exists()

    def test_unknown_select_raises(self, home):
        with pytest.raises(KeyError):
            theme.select("nope")


class TestPrompt:
    def test_prompt_uses_the_theme_colors(self, home, st, monkeypatch):
        monkeypatch.setenv("TERM", "xterm")
        theme.select("ocean", save=False)
        assert "\x1b[1;36m" in build_prompt(st) and "\x1b[1;32m" not in build_prompt(st)

    def test_failure_color_follows_the_theme(self, home, st, monkeypatch):
        monkeypatch.setenv("TERM", "xterm")
        st.last_status = 3
        assert "[3]" in build_prompt(st)
        assert "\x1b[1;31m" in build_prompt(st)

    def test_prompt_ends_with_the_text_color(self, home, st, monkeypatch):
        monkeypatch.setenv("TERM", "xterm")
        theme.select("ocean", save=False)
        assert build_prompt(st).endswith("\x01\x1b[94m\x02") or build_prompt(st).endswith("\x1b[94m")

    def test_default_leaves_the_text_color_alone(self, home, st, monkeypatch):
        monkeypatch.setenv("TERM", "xterm")
        assert build_prompt(st).endswith(f"{CH} ")

    def test_mono_has_no_escape_codes(self, home, st, monkeypatch):
        monkeypatch.setenv("TERM", "xterm")
        theme.select("mono", save=False)
        st.last_status = 1
        assert "\x1b" not in build_prompt(st) and "\x01" not in build_prompt(st)


class TestBuiltin:
    def test_lists_all_and_marks_the_current(self, home, st):
        code, out, _ = run(st)
        assert code == 0
        for name in theme.names():
            assert name in out
        assert "* default" in out

    def test_switch_saves(self, home, st):
        code, out, _ = run(st, "ocean")
        assert code == 0 and "ocean" in out
        assert theme.active().name == "ocean"
        assert (home / ".myshell_theme").read_text().strip() == "ocean"
        assert "* ocean" in run(st)[1]

    def test_name_is_not_case_sensitive(self, home, st):
        assert run(st, "OCEAN")[0] == 0 and theme.active().name == "ocean"

    def test_reset(self, home, st):
        run(st, "forest")
        assert run(st, "reset")[0] == 0
        assert theme.active().name == "default" and not (home / ".myshell_theme").exists()

    def test_unknown_name_suggests_a_close_one(self, home, st):
        code, _, err = run(st, "ocea")
        assert code == 1 and "unknown theme" in err and "'ocean'" in err
        assert theme.active().name == "default"

    def test_too_many_arguments(self, home, st):
        assert run(st, "a", "b")[0] == 2

    def test_help(self, home, st):
        assert "usage: theme" in run(st, "--help")[1]

    def test_colored_preview_only_on_a_terminal(self, home, st, monkeypatch):
        monkeypatch.setenv("TERM", "xterm")
        monkeypatch.delenv("NO_COLOR", raising=False)
        assert "\x1b[" not in run(st)[1]
        assert "\x1b[1;32m" in run(st, tty=True)[1]
        monkeypatch.setenv("NO_COLOR", "1")
        assert "\x1b[" not in run(st, tty=True)[1]

    def test_runs_from_the_shell(self, sh):
        r = sh("theme sunset\ntheme")
        assert "* sunset" in r.out


class TestWhyNoColor:
    def test_reasons(self, home, monkeypatch):
        monkeypatch.setattr(theme, "_is_windows", lambda: False)  # Windows words its message differently
        monkeypatch.setenv("TERM", "xterm")
        monkeypatch.delenv("NO_COLOR", raising=False)
        assert theme.colors_off_reason() == ""
        monkeypatch.setenv("NO_COLOR", "1")
        assert "NO_COLOR" in theme.colors_off_reason()
        monkeypatch.delenv("NO_COLOR")
        monkeypatch.setenv("TERM", "dumb")
        assert "TERM" in theme.colors_off_reason()
        monkeypatch.delenv("TERM")
        assert "not set" in theme.colors_off_reason()
        theme.select("mono", save=False)
        assert "mono" in theme.colors_off_reason()

    def test_list_explains_on_a_terminal(self, home, st, monkeypatch):
        monkeypatch.setenv("TERM", "dumb")
        assert "Colors are off: TERM is 'dumb'" in run(st, tty=True)[1]
        assert "Colors are off" not in run(st)[1]  # not a terminal, no note
        assert "Colors are off" in run(st, "ocean", tty=True)[1]


class TestTerminalColors:
    def test_term_and_no_color(self, monkeypatch):
        monkeypatch.delenv("NO_COLOR", raising=False)
        monkeypatch.setenv("TERM", "xterm")
        assert theme.terminal_colors()
        monkeypatch.setenv("TERM", "dumb")
        assert not theme.terminal_colors()
        monkeypatch.setenv("TERM", "xterm")
        monkeypatch.setenv("NO_COLOR", "1")
        assert not theme.terminal_colors()

    def test_no_term_means_no_color_on_unix(self, monkeypatch):
        monkeypatch.delenv("NO_COLOR", raising=False)
        monkeypatch.delenv("TERM", raising=False)
        monkeypatch.setattr(theme, "_is_windows", lambda: False)
        assert not theme.terminal_colors()

    def test_windows_asks_the_console(self, monkeypatch):
        monkeypatch.delenv("NO_COLOR", raising=False)
        monkeypatch.delenv("TERM", raising=False)
        monkeypatch.setattr(theme, "_is_windows", lambda: True)
        monkeypatch.setattr(theme, "_enable_windows_vt", lambda: True)
        assert theme.terminal_colors()
        monkeypatch.setattr(theme, "_enable_windows_vt", lambda: False)
        assert not theme.terminal_colors()
        assert "Windows" in theme.colors_off_reason()

    def test_windows_console_can_still_say_no_color(self, monkeypatch):
        monkeypatch.setenv("NO_COLOR", "1")
        monkeypatch.setattr(theme, "_is_windows", lambda: True)
        monkeypatch.setattr(theme, "_enable_windows_vt", lambda: True)
        assert not theme.terminal_colors()


class TestTextColor:
    def test_every_colored_theme_but_default_tints_text(self):
        for name, t in theme.THEMES.items():
            assert bool(t.text) == (name not in ("default", "mono"))
            if t.text:
                assert t.syntax["plain"].startswith("fg:")

    def test_text_start_follows_the_terminal(self, home, monkeypatch):
        monkeypatch.setenv("TERM", "xterm")
        monkeypatch.delenv("NO_COLOR", raising=False)
        theme.select("forest", save=False)
        assert theme.text_start() == "\x1b[32m"
        monkeypatch.setenv("NO_COLOR", "1")
        assert theme.text_start() == ""
        monkeypatch.delenv("NO_COLOR")
        theme.select("default", save=False)
        assert theme.text_start() == ""


@unix_only
def test_command_output_is_tinted_in_a_real_terminal(pty_shell):
    s = pty_shell({"TERM": "xterm", "MYSHELL_THEME": "forest", "MYSHELL_EDITOR": "basic"})
    seen = s.expect(f"{CH} ")
    assert "\x1b[32m" in seen.rsplit("\x1b[0m", 1)[-1]  # the prompt switches to the theme's text color
    s.send("echo hello\n")
    s.expect("\x1b[32mhello")  # the command's output starts in the theme's text color
    rest = s.expect(f"{CH} ")
    assert "\x1b[0m" in rest


class TestTextEverywhere:
    def test_prompt_returns_to_the_text_color_after_each_colored_part(self, home, st, monkeypatch):
        monkeypatch.setenv("TERM", "xterm")
        monkeypatch.setattr("myshell.prompt.MARKERS", False)
        theme.select("forest", save=False)
        st.last_status = 2
        p = build_prompt(st)
        assert p.count("\x1b[0m\x1b[32m") >= 2 and p.endswith("\x1b[32m")

    def test_pip_keeps_the_text_color_after_the_face(self, home, monkeypatch):
        from myshell import guide
        monkeypatch.setenv("TERM", "xterm")
        theme.select("ocean", save=False)
        first = guide.render("hello", color=True).splitlines()[0]
        assert "\x1b[0m\x1b[94m" in first

    def test_preview_keeps_the_text_color_after_each_sample(self, home, monkeypatch):
        monkeypatch.setenv("TERM", "xterm")
        theme.select("sunset", save=False)
        shown = theme.sample(theme.THEMES["ocean"], True)
        assert shown.count("\x1b[0m\x1b[33m") == 4

    def test_switching_starts_the_new_text_color_first(self, home, st, monkeypatch):
        monkeypatch.setenv("TERM", "xterm")
        monkeypatch.delenv("NO_COLOR", raising=False)
        out = run(st, "forest", tty=True)[1]
        assert out.startswith("\x1b[32mTheme: forest")

    def test_typed_words_use_the_text_color_in_the_editor(self):
        assert theme.THEMES["forest"].syntax["plain"] == "fg:ansigreen"
        assert theme.THEMES["ocean"].syntax["plain"] == "fg:ansibrightblue"