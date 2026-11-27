"""Colors for the full-screen app. One palette per myshell theme, built from named roles.

Every text role is checked against the panel background in tests/test_tui.py, so each pair
meets the WCAG AA ratio of 4.5:1. Borders and the selection bar are not text."""
import os
from dataclasses import dataclass
from typing import Dict

from . import theme


@dataclass(frozen=True)
class Palette:
    name: str
    app_bg: str      # header, footer and the gaps around panels
    panel_bg: str    # inside every panel
    text: str
    muted: str
    border: str      # panel frame when it does not have focus
    accent: str      # focus frame, titles, the prompt
    success: str
    warn: str
    error: str
    info: str
    select_bg: str   # the selected row
    on_accent: str   # text drawn on a filled accent badge


DARK = Palette("default", "#0b0e14", "#131923", "#c8d0e0", "#8590a6", "#3a4357", "#7aa2f7",
               "#9ece6a", "#e0af68", "#f7768e", "#7dcfff", "#263044", "#0b0e14")

PALETTES: Dict[str, Palette] = {p.name: p for p in [
    DARK,
    Palette("ocean", "#08121a", "#0e1c27", "#c5d8e6", "#84a0b4", "#2f4a5e", "#7dcfff",
            "#8fd6b0", "#e0c07a", "#f28fa0", "#9ad7ff", "#1b3347", "#08121a"),
    Palette("forest", "#0c130d", "#121c14", "#cfdcc8", "#8da58a", "#34503a", "#9ece6a",
            "#a8d98a", "#e0c070", "#f08a7a", "#8fd9c0", "#223a27", "#0c130d"),
    Palette("sunset", "#150e0c", "#1d1411", "#e6d3c5", "#ab9080", "#5a3d33", "#ff9e64",
            "#b5d68a", "#f0c070", "#ff8a8a", "#f0a8c0", "#3a2620", "#150e0c"),
    Palette("light", "#e9ecf3", "#ffffff", "#1f2430", "#586075", "#9aa3b8", "#2f5bd0",
            "#1f7a3a", "#8a5a00", "#b3261e", "#0b6a8f", "#dfe6f7", "#ffffff"),
    Palette("mono", "#0a0a0a", "#141414", "#d8d8d8", "#9a9a9a", "#4a4a4a", "#f0f0f0",
            "#d8d8d8", "#d8d8d8", "#f0f0f0", "#d8d8d8", "#2c2c2c", "#0a0a0a"),
]}

TEXT_ROLES = ("text", "muted", "accent", "success", "warn", "error", "info")


def active_palette() -> Palette:
    if os.environ.get("NO_COLOR"):
        return PALETTES["mono"]
    return PALETTES.get(theme.active().name, DARK)


def style_rules(p: Palette) -> Dict[str, str]:
    """The prompt_toolkit style table for one palette."""
    return {
        "": f"fg:{p.text} bg:{p.app_bg}",
        "panel": f"fg:{p.text} bg:{p.panel_bg}",
        "border": f"fg:{p.border} bg:{p.panel_bg}",
        "border.focus": f"fg:{p.accent} bg:{p.panel_bg}",
        "title": f"fg:{p.muted} bg:{p.panel_bg}",
        "title.focus": f"fg:{p.accent} bg:{p.panel_bg} bold",
        "text": f"fg:{p.text} bg:{p.panel_bg}",
        "muted": f"fg:{p.muted} bg:{p.panel_bg}",
        "accent": f"fg:{p.accent} bg:{p.panel_bg} bold",
        "success": f"fg:{p.success} bg:{p.panel_bg}",
        "warn": f"fg:{p.warn} bg:{p.panel_bg}",
        "error": f"fg:{p.error} bg:{p.panel_bg}",
        "info": f"fg:{p.info} bg:{p.panel_bg}",
        "dir": f"fg:{p.accent} bg:{p.panel_bg}",
        "select": f"bg:{p.select_bg} fg:{p.text}",
        "select.accent": f"bg:{p.select_bg} fg:{p.accent} bold",
        "head": f"fg:{p.muted} bg:{p.panel_bg} underline",
        "bar.brand": f"fg:{p.on_accent} bg:{p.accent} bold",
        "bar": f"fg:{p.text} bg:{p.app_bg}",
        "bar.muted": f"fg:{p.muted} bg:{p.app_bg}",
        "bar.key": f"fg:{p.accent} bg:{p.app_bg} bold",
        "bar.ok": f"fg:{p.success} bg:{p.app_bg}",
        "bar.bad": f"fg:{p.error} bg:{p.app_bg} bold",
        "bar.warn": f"fg:{p.warn} bg:{p.app_bg}",
    }
