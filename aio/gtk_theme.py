#!/usr/bin/env python3
"""
gtk_theme.py — vendored from omaPViewer (~/Projects/image-browser/omarchy_theme.py).

Omarchy theme integration for GTK4/libadwaita apps.

Omarchy does not ship a GTK theme: it publishes the current palette at
~/.local/state/omarchy/current/theme/colors.toml, sets Adwaita dark/light
plus a tinted icon theme via gsettings, and sets fonts via fontconfig
(monospace) and gsettings (UI font).

This module maps the Omarchy palette onto libadwaita's named colors with a
runtime CSS provider, exposes the Omarchy monospace font as a CSS class,
and watches the theme state with GLib file monitors so apps retint live
when the user switches themes.

Usage:
    from omarchy_theme import OmarchyTheme
    OmarchyTheme.apply(window)   # once, after the window exists
"""

import os
import subprocess
import sys
import tomllib

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
from gi.repository import GLib, Gio, Gdk, Gtk  # noqa: E402

STATE_DIR = os.path.expanduser("~/.local/state/omarchy/current")
THEME_DIR = os.path.join(STATE_DIR, "theme")
COLORS_FILE = os.path.join(THEME_DIR, "colors.toml")
FONTCONF_FILE = os.path.expanduser("~/.config/fontconfig/fonts.conf")


def read_palette() -> dict:
    try:
        with open(COLORS_FILE, "rb") as f:
            return tomllib.load(f)
    except Exception:
        return {}


def monospace_family() -> str:
    try:
        out = subprocess.run(
            ["fc-match", "monospace", "-f", "%{family}"],
            capture_output=True, text=True, timeout=5,
        ).stdout.strip()
        return out.split(",")[0].strip() or "monospace"
    except Exception:
        return "monospace"


def _hex_to_rgb(s: str):
    s = s.lstrip("#")
    if len(s) == 3:
        s = "".join(c * 2 for c in s)
    try:
        return tuple(int(s[i:i + 2], 16) / 255.0 for i in (0, 2, 4))
    except Exception:
        return None


def _luminance(hexcolor: str) -> float:
    rgb = _hex_to_rgb(hexcolor)
    if rgb is None:
        return 0.5
    r, g, b = rgb
    lin = [c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4
           for c in (r, g, b)]
    return 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2]


def _esc(s) -> str:
    return str(s).replace("\\", "\\\\").replace('"', '\\"')


def _mix(c1: str, c2: str, t: float) -> str:
    """Blend hex colors c1 -> c2 by t (0..1); result as #rrggbb."""
    a, b = _hex_to_rgb(c1), _hex_to_rgb(c2)
    if a is None or b is None:
        return c2
    out = [round(255 * (x * (1 - t) + y * t)) for x, y in zip(a, b)]
    return "#{:02x}{:02x}{:02x}".format(*out)


def _value_up(hexcolor: str, amount: float) -> str:
    """Raise a color's perceived VALUE (HSP brightness) by `amount`.

    value = sqrt(0.299 R² + 0.587 G² + 0.114 B²); RGB is scaled so the
    value rises by `amount` (e.g. 0.25 = "+25% visibility"), clamped so
    no channel overflows past white.
    """
    rgb = _hex_to_rgb(hexcolor)
    if rgb is None:
        return hexcolor
    v = (0.299 * rgb[0] ** 2 + 0.587 * rgb[1] ** 2 + 0.114 * rgb[2] ** 2) ** 0.5
    if v >= 1.0 - 1e-6:
        return hexcolor
    target = min(1.0, v + amount)
    k = target / max(v, 1e-6)
    out = [min(1.0, ch * k) for ch in rgb]
    return "#{:02x}{:02x}{:02x}".format(*(round(255 * x) for x in out))


def panel_bg_color(palette: dict) -> str:
    """Background for content panels (grid / image preview).

    Requirements: at least +25% relative luminance over the darkest
    background tone, but still clearly darker than the sidebar (window
    background). Blends darker_background -> background, pushing toward
    the foreground only if the palette's two darkest tones are too close.
    """
    dark = str(palette.get("darker_background") or palette.get("background") or "#1b1b1b")
    base = str(palette.get("background") or dark)
    fg = str(palette.get("foreground") or "#e0e0e0")
    sidebar_lum = _luminance(base)
    dark_lum = _luminance(dark)

    # Sit at 85% of the sidebar's luminance: visibly lighter than the old
    # near-black panel, still clearly darker than the left panel. If the
    # palette's sidebar barely differs from its darkest tone, prioritize
    # the >=25% lift instead.
    target = sidebar_lum * 0.85
    if target <= dark_lum * 1.25:
        target = dark_lum * 1.35

    out = _mix(dark, base, 0.30)
    if _luminance(out) < target:
        lo, hi = 0.0, 1.0
        for _ in range(18):
            mid = (lo + hi) / 2
            m = _mix(dark, base, 0.30 + 0.70 * mid)
            if _luminance(m) < target:
                lo = mid
            else:
                hi = mid
        out = _mix(dark, base, 0.30 + 0.70 * (lo + hi) / 2)
        # Extremely close tones: fall back to lifting toward the foreground.
        if _luminance(out) < target:
            lo, hi = 0.0, 1.0
            for _ in range(18):
                mid = (lo + hi) / 2
                m = _mix(out, fg, mid)
                if _luminance(m) < target:
                    lo = mid
                else:
                    hi = mid
            out = _mix(out, fg, (lo + hi) / 2)
    return out


def chrome_bg_color(palette: dict) -> str:
    """Background for chrome surfaces (header bar, footer, folders panel).

    User: chrome shall match the image grid background, "eventually a
    little lighter if there's an accent color available for background".
    Same tone as the grid's composited surface (the translucent panel over
    the window background), lifted a touch toward the palette's
    lighter_background when one exists.
    """
    p = palette or {}
    bg = str(p.get("background") or "#1b1b1b")
    grid = _mix(panel_bg_color(p), bg, 0.25)  # alpha(panel, 0.75) over bg
    lighter = p.get("lighter_background")
    if lighter:
        return _mix(grid, str(lighter), 0.30)
    return grid


class OmarchyTheme:
    """Applies and live-tracks the Omarchy theme for GTK4/Adw windows."""

    _instance = None

    @classmethod
    def apply(cls, window) -> "OmarchyTheme":
        if cls._instance is None:
            cls._instance = cls()
        cls._instance._attach(window)
        return cls._instance

    def __init__(self):
        self.palette = {}
        self._windows = []
        self._css_provider = None
        self._font_provider = None
        self._monitors = []
        self._debounce_id = 0

    # ------------------------------------------------------------- setup

    def _attach(self, window):
        if window not in self._windows:
            self._windows.append(window)
        self._refresh()
        self._ensure_monitors()

    # ------------------------------------------------------------ reload

    def _refresh(self):
        self.palette = read_palette()
        self._apply_css()
        self._apply_fonts()

    def _schedule_refresh(self):
        # Debounce: theme switches touch several files in quick succession.
        if self._debounce_id:
            GLib.source_remove(self._debounce_id)
        self._debounce_id = GLib.timeout_add(300, self._do_refresh)
        return False

    def _do_refresh(self):
        self._debounce_id = 0
        self._refresh()
        return False

    # ---------------------------------------------------------------- css

    def _apply_css(self):
        css = self._build_css()
        if not css:
            return
        display = Gdk.Display.get_default()
        if self._css_provider is None:
            self._css_provider = Gtk.CssProvider()
            Gtk.StyleContext.add_provider_for_display(
                display, self._css_provider,
                Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION + 10)
        try:
            self._css_provider.load_from_string(css)
        except GLib.Error as e:
            print(f"[omarchy-theme] CSS error: {e}", file=sys.stderr)

    def _build_css(self) -> str:
        p = self.palette
        if not p:
            return ""

        def c(key, fallback):
            v = p.get(key)
            if v is None:
                return fallback
            return _esc(v)

        bg = c("background", None)
        darker = c("darker_background", None) or bg
        lighter = c("lighter_background", None) or bg
        fg = c("foreground", None)
        bright = c("bright_foreground", None) or fg
        accent = c("accent", "#3584e4")
        selection = c("selection", None) or accent
        # user: texts and icons +25% more visible (header, footer, preview
        # caption). Lift the VALUE of fg and everything derived from it;
        # bright_foreground lifts proportionally less (already bright).
        fg = _value_up(fg, 0.25) if fg else fg
        _m = c("muted", None)
        muted = _value_up(_m, 0.25) if _m else fg  # fg already lifted
        red = c("red", "#e01b24")
        green = c("green", "#2ec27e")
        yellow = c("yellow", "#e5a50a")

        # Pick readable text on top of the accent color.
        if _luminance(str(p.get("accent", ""))) > 0.45:
            on_accent = darker or "#111111"
        else:
            on_accent = bright or "#ffffff"

        if bg is None or fg is None:
            return ""  # incomplete palette: keep stock Adwaita

        lines = [
            f"@define-color accent_color {accent};",
            f"@define-color accent_bg_color {accent};",
            f"@define-color accent_fg_color {on_accent};",
            f"@define-color window_bg_color {bg};",
            f"@define-color window_fg_color {fg};",
            # content panels (grid / preview): lifted surface between the
            # darkest tone and the window background (>=25% luminance lift)
            f"@define-color view_bg_color {panel_bg_color(p)};",
            f"@define-color view_fg_color {fg};",
            # image preview frame uses the same lifted surface
            f"@define-color thumbnail_bg_color {panel_bg_color(p)};",
            f"@define-color thumbnail_fg_color {fg};",
            f"@define-color headerbar_bg_color {bg};",
            f"@define-color headerbar_fg_color {fg};",
            f"@define-color card_bg_color {lighter};",
            f"@define-color card_fg_color {fg};",
            f"@define-color popover_bg_color {lighter};",
            f"@define-color popover_fg_color {fg};",
            f"@define-color dialog_bg_color {lighter};",
            f"@define-color dialog_fg_color {fg};",
            f"@define-color sidebar_bg_color {bg};",
            f"@define-color sidebar_fg_color {fg};",
            f"@define-color sidebar_backdrop_color {darker};",
            f"@define-color sidebar_shade_color {darker};",
            f"@define-color success_bg_color {green};",
            f"@define-color success_color {green};",
            f"@define-color warning_bg_color {yellow};",
            f"@define-color warning_color {yellow};",
            f"@define-color error_bg_color {red};",
            f"@define-color error_color {red};",
            f"@define-color destructive_bg_color {red};",
            f"@define-color destructive_color {red};",
            # selection follows the Omarchy selection color
            "gridview child:selected {"
            f" background-color: {selection};"
            "}",
            # translucent content panel: 25% more transparent than the
            # opaque panel color (alpha 0.75) over the window background
            f".omarchy-panel {{ background-color: alpha({panel_bg_color(p)}, 0.75); }}",
            # chrome surfaces (header bar, footer, Folders panel): same
            # tone as the grid panel, lifted a touch toward the theme's
            # lighter_background (user: "shall be the same as the image
            # grid background, eventually a little lighter")
            f".ib-folders-bg, headerbar, .ib-footer-bg,"
            f".ib-folders-bg .omarchy-panel,"
            f".ib-folders-bg navigation-sidebar,"
            f".ib-folders-bg treeview, .ib-folders-bg .view,"
            f".ib-preview-bg {{ background-color: {chrome_bg_color(p)}; }}"
            f".ib-folders-bg navigation-sidebar > row {{"
            f" background-color: {chrome_bg_color(p)}; }}"
            # tree rows: SQUARE highlight corners + tight vertical rhythm
            # (user: too much space between root folders, rounded selection)
            f".ib-folders-bg list > row {{"
            f" border-radius: 0px; min-height: 0px;"
            f" padding-top: 1px; padding-bottom: 1px; }}"
            # omasettings row-cursor: a quiet full-width accent band (their
            # SettingRow.qml uses accent at 0.14 alpha, radius 0), with the
            # text kept at normal foreground (no GTK selection recolor).
            # The tree is a ListBox now: rows carry the selection style.
            f".ib-folders-bg list > row:selected,"
            f".ib-folders-bg list > row:selected:hover,"
            f".ib-folders-bg list > row:selected:active {{"
            f" background-color: alpha({accent}, 0.14);"
            f" color: {fg}; }}",
            # chevron: muted (fg darkened), icon-only styling
            f".ib-chev {{ color: {muted}; }}",
            # tree separator hairline between roots and favorites
            f".ib-sep-line {{"
            f" background-color: alpha({fg}, 0.22);"
            f" min-height: 1px; }}",
            # header view toggle: quiet buttons; active = accent tint
            f".ib-view-btn {{"
            f" background-color: transparent;"
            f" border: none; border-radius: 0px;"
            f" padding: 4px 8px; min-height: 0px; }}"
            f".ib-view-btn:hover:not(.ib-view-active) {{"
            f" background-color: alpha({fg}, 0.08); }}"
            f".ib-view-btn.ib-view-active {{"
            f" background-color: alpha({accent}, 0.14);"
            f" color: {fg}; }}"
            f".ib-view-btn:checked {{"
            f" background-color: alpha({accent}, 0.14);"
            f" color: {fg}; }}",
            # floating strip/pill used by overlay widgets (slider, preview
            # caption + favorite): dimmed panel surface with a hairline
            f".omarchy-strip, .omarchy-preview-strip {{"
            f" background-color: alpha({bg}, 0.82);"
            f" border: 1px solid alpha({accent}, 0.35);"
            " border-radius: 6px; padding: 3px 8px; }",
            # separator between panels: thin highlighted line, omasettings
            f".ib-paned > separator {{ background: alpha({fg}, 0.22);"
            " background-image: none; min-width: 1px; border: none;"
            " box-shadow: none; outline: none; opacity: 1; }",
            # horizontal hairlines: under the header, above the footer;
            # header + footer strips also compact (user request)
            f"headerbar {{ border-bottom: 1px solid alpha({fg}, 0.22);"
            f" min-height: 30px; }}"
            f"headerbar button {{ min-height: 0px; margin-top: 0px;"
            f" margin-bottom: 0px; }}"
            f"headerbar entry {{ min-height: 24px;"
            f" padding-top: 1px; padding-bottom: 1px; }}",
            f".ib-footer-bg {{ border-top: 1px solid alpha({fg}, 0.22);"
            f" min-height: 0px; padding-top: 1px; padding-bottom: 1px; }}",
            # omasettings-style legibility: muted TEXT COLOR carries the
            # dimming, not label opacity (dim-label would multiply against
            # the +25% lifted colors and cancel it). Footer status and
            # preview caption both carry .dim-label.
            f"label.dim-label {{ opacity: 1; }}",
            # header path entry: square corners (user request), quiet
            # hairline border so the cell reads as one flat field
            f".ib-path-entry {{ border-radius: 0px;"
            f" border: 1px solid alpha({fg}, 0.22);"
            f" box-shadow: none; }}",
            # subtle muted style for meta text
            f".omarchy-muted {{ color: {muted}; }}",
            # image viewer canvas: fully opaque surface (user request — the
            # compositor's window opacity rule must have nothing to blend)
            f".ib-viewer-canvas {{ background-color: {darker}; }}",
        ]
        return "\n".join(lines)

    # -------------------------------------------------------------- fonts

    def _apply_fonts(self):
        """Apply the Omarchy monospace font app-wide, foot-terminal style.

        foot uses `JetBrainsMono Nerd Font:size=9`; we mirror that face for
        the whole window, with a slightly larger default (10pt) for general
        UI text and foot's exact 9pt for `.omarchy-tree` (file tree panel).
        """
        family = monospace_family()
        # omasettings typography: monospace family (Style.qml), body 12px,
        # caption 10px, subtitle 13px (Qt pixel sizes → pt at 96dpi: x0.75)
        css = (
            f'window {{ font-family: "{_esc(family)}", monospace; '
            f'font-size: 9pt; }}\n'                       # body 12px
            f'.omarchy-tree {{ font-size: 9pt; }}\n'      # 12px body
            f'.omarchy-mono {{ font-family: "{_esc(family)}", monospace; }}\n'
            f'.omarchy-small {{ font-size: 7.5pt; }}'     # caption 10px
        )
        display = Gdk.Display.get_default()
        if self._font_provider is None:
            self._font_provider = Gtk.CssProvider()
            Gtk.StyleContext.add_provider_for_display(
                display, self._font_provider,
                Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION + 10)
        try:
            self._font_provider.load_from_string(css)
        except GLib.Error as e:
            print(f"[omarchy-theme] font CSS error: {e}", file=sys.stderr)

    # ------------------------------------------------------------ monitors

    def _ensure_monitors(self):
        if self._monitors:
            return
        for path in {STATE_DIR, THEME_DIR, os.path.dirname(FONTCONF_FILE)}:
            if not os.path.isdir(path):
                continue
            gfile = Gio.File.new_for_path(path)
            try:
                mon = gfile.monitor_directory(
                    Gio.FileMonitorFlags.WATCH_MOVES, None)
            except Exception:
                continue
            mon.connect("changed", self._on_change)
            self._monitors.append(mon)
        # Re-arm periodically in case watched dirs were replaced while the
        # app is closed/awake (cheap safety net).
        GLib.timeout_add_seconds(30, self._rearm_if_needed)
        return None

    def _on_change(self, _mon, _f1, _f2, _event):
        name1 = _f1.get_basename() if _f1 is not None else ""
        interesting = (
            name1 in ("colors.toml", "theme", "fonts.conf")
            or _event == Gio.FileMonitorEvent.DELETED
            or _event == Gio.FileMonitorEvent.MOVED_OUT
        )
        if interesting:
            self._schedule_refresh()

    def _rearm_if_needed(self):
        # If the theme dir was swapped out, the dir monitor dies; recreate.
        alive = False
        for mon in list(self._monitors):
            try:
                mon.is_cancelled()
                alive = True
            except Exception:
                self._monitors.remove(mon)
        if not alive:
            self._monitors.clear()
            self._ensure_monitors()
        return True
