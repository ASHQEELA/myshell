"""Color themes for the prompt, the syntax colors in the editor and Pip.

A theme is a few ANSI color codes. The active theme comes from MYSHELL_THEME, then from
~/.myshell_theme (written by the theme builtin), then it is "default". Nothing here imports
prompt_toolkit or the shell state."""
import os
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from .paths import home_file

RESET = "\x1b[0m"


@dataclass(frozen=True)
class Theme:
    name: str
    about: str
    dir: str       # SGR code for the folder in the prompt, such as "1;32"
    fail: str      # the [status] shown after a failed command
    branch: str    # the git branch
    pip: str       # Pip's face
    syntax: Dict[str, str] = field(default_factory=dict)  # prompt_toolkit styles by class name
    suggestion: str = "fg:#888888"
    colorful: bool = True  # False means no color anywhere
    text: str = ""  # SGR code for ordinary text: what you type and what commands print. "" keeps the terminal's own

    def sgr(self, code: str) -> str:
        return f"\x1b[{code}m" if code else ""


def _syntax(command, bad, keyword, string, variable, operator, comment):
    return {"command": command, "badcommand": bad, "keyword": keyword, "string": string,
            "variable": variable, "operator": operator, "comment": comment, "plain": ""}


THEMES: Dict[str, Theme] = {t.name: t for t in [
    Theme("default", "green folder, red failures", "1;32", "1;31", "35", "33",
          _syntax("fg:ansigreen", "fg:ansired", "fg:ansiblue bold", "fg:ansiyellow",
                  "fg:ansicyan", "fg:ansimagenta", "fg:ansibrightblack")),
    Theme("ocean", "blues and cyans", "1;36", "1;31", "34", "36",
          _syntax("fg:ansicyan", "fg:ansired", "fg:ansiblue bold", "fg:ansibrightcyan",
                  "fg:ansibrightwhite", "fg:ansibrightmagenta", "fg:ansibrightblack"), "fg:#6b8ba4",
          text="94"),
    Theme("forest", "greens and earth tones", "1;33", "1;31", "36", "32",
          _syntax("fg:ansibrightgreen", "fg:ansired", "fg:ansiyellow bold", "fg:ansibrightyellow",
                  "fg:ansicyan", "fg:ansibrightblack", "fg:ansibrightblack"), "fg:#7a8f6b",
          text="32"),
    Theme("sunset", "warm reds and yellows", "1;93", "1;31", "35", "31",
          _syntax("fg:ansibrightyellow", "fg:ansired", "fg:ansibrightred bold", "fg:ansibrightmagenta",
                  "fg:ansimagenta", "fg:ansired", "fg:ansibrightblack"), "fg:#a08070",
          text="33"),
    Theme("light", "darker colors for a white background", "1;35", "1;31", "32", "34",
          _syntax("fg:ansigreen", "fg:ansired", "fg:ansimagenta bold", "fg:ansired",
                  "fg:ansicyan", "fg:ansibrightblack", "fg:ansibrightblack"), "fg:#999999",
          text="34"),
    Theme("mono", "no colors", "", "", "", "", _syntax("", "", "bold", "", "", "", ""),
          "fg:#888888", colorful=False),
]}
DEFAULT = "default"

_ANSI_NAMES = ["black", "red", "green", "yellow", "blue", "magenta", "cyan", "white"]


def _ansi_style(code: str) -> str:
    """prompt_toolkit style for a plain SGR color such as "32" or "94". "" when it is not one."""
    if code.isdigit():
        n = int(code)
        if 30 <= n <= 37:
            return "fg:ansi" + _ANSI_NAMES[n - 30]
        if 90 <= n <= 97:
            return "fg:ansibright" + _ANSI_NAMES[n - 90]
    return ""


for _t in THEMES.values():  # typed words that are not a command or a string take the text color
    if _t.text:
        _t.syntax["plain"] = _ansi_style(_t.text)
_TEXT_STYLE = {"94": "fg:ansibrightblue", "32": "fg:ansigreen", "33": "fg:ansiyellow", "34": "fg:ansiblue"}
for _t in THEMES.values():
    if _t.text:
        _t.syntax["plain"] = _TEXT_STYLE[_t.text]

_active: Optional[Theme] = None


def theme_file() -> str:
    return home_file(".myshell_theme")


def names() -> List[str]:
    return list(THEMES)


def _read_saved() -> Optional[str]:
    try:
        with open(theme_file(), encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return None


def active() -> Theme:
    """The theme in use now. An unknown name falls back to the default."""
    global _active
    if _active is None:
        name = os.environ.get("MYSHELL_THEME") or _read_saved() or DEFAULT
        _active = THEMES.get(name, THEMES[DEFAULT])
    return _active


def select(name: str, save: bool = True) -> Theme:
    """Switch theme. Raises KeyError for an unknown name."""
    global _active
    chosen = THEMES[name]
    _active = chosen
    if save:
        try:
            if name == DEFAULT:
                os.remove(theme_file())
            else:
                with open(theme_file(), "w", encoding="utf-8") as f:
                    f.write(name + "\n")
        except OSError:
            pass
    return chosen


def reset() -> None:
    """Forget the cached theme so the next call reads the environment again."""
    global _active
    _active = None


def sample(t: Theme, color: bool) -> str:
    """A short preview: how the folder, branch and a failure look."""
    parts = [("~/proj", t.dir), ("(main)", t.branch), ("[1]", t.fail), ("text", t.text)]
    if not (color and t.colorful):
        return " ".join(text for text, _ in parts)
    back = text_start()  # after each sample, the text goes back to the theme in use
    return " ".join(f"{t.sgr(code)}{text}{RESET}{back}" for text, code in parts)


def text_start() -> str:
    """The escape code that starts the theme's text color, or an empty string."""
    t = active()
    return t.sgr(t.text) if t.text and t.colorful and terminal_colors() else ""


def text_reset() -> str:
    """A reset code followed by the theme's text color, so text after it keeps the theme."""
    return RESET + text_start()


def _is_windows() -> bool:
    return os.name == "nt"


_vt_ok: Optional[bool] = None


def _enable_windows_vt() -> bool:
    """Turn on ANSI color codes in the Windows console. True when the console accepts them."""
    global _vt_ok
    if _vt_ok is None:
        try:
            import ctypes
            kernel = ctypes.windll.kernel32  # type: ignore[attr-defined]
            handle = kernel.GetStdHandle(-11)
            mode = ctypes.c_uint32()
            _vt_ok = bool(kernel.GetConsoleMode(handle, ctypes.byref(mode))) and bool(
                kernel.SetConsoleMode(handle, mode.value | 0x0004))
        except Exception:
            _vt_ok = False
    return _vt_ok


def terminal_colors() -> bool:
    """Does the terminal show colors? NO_COLOR and TERM=dumb say no. Windows usually has no TERM
    but the console shows colors, so there the console itself is asked."""
    if "NO_COLOR" in os.environ:
        return False
    term = os.environ.get("TERM", "")
    if term == "dumb":
        return False
    if term:
        return True
    return _is_windows() and _enable_windows_vt()


def colors_off_reason(t: Optional[Theme] = None) -> str:
    """Why the prompt shows no colors, or an empty string when it does."""
    t = t or active()
    if not t.colorful:
        return f"the {t.name} theme has no colors"
    if "NO_COLOR" in os.environ:
        return "NO_COLOR is set"
    if not terminal_colors():
        term = os.environ.get("TERM", "")
        if _is_windows() and not term:
            return "this Windows console does not accept color codes. Try Windows Terminal"
        return "TERM is " + (f"'{term}'" if term else "not set") + ". Try: export TERM=xterm-256color"
    return ""
