#!/usr/bin/env python3
"""omAIOpanel editor — visual layout editor for the AIO panel.

Edits ~/.config/omAIOpanel/config.toml's widget layout and previews the
render live (same renderer the daemon uses). Apply = write config; the
daemon hot-reloads it onto the panel. Never touches USB itself.

Run:  /usr/bin/python editor.py   (from ~/Projects/omAIOpanel)
"""

from __future__ import annotations

import copy
import io
import json
import math
import os
import re
import sys
import threading
import time
import tomllib
import traceback
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, Gdk, GObject, Gtk, Pango  # noqa: E402

import numpy as np  # noqa: E402
import cairo  # noqa: E402
from PIL import Image  # noqa: E402

from aio.config import CONFIG_PATH, load_config  # noqa: E402
from aio.gtk_theme import OmarchyTheme as GtkOmarchyTheme  # noqa: E402
from aio.render import render_background, render_frame  # noqa: E402
from aio.sensors import DATA_LABELS, SensorHub, format_row, mounted_volumes, volume_label  # noqa: E402
from aio.theme import OmarchyTheme  # noqa: E402

APP_ID = "io.cowneko.OmAIOpanel"

GAUGE_SENSORS = ("cpu", "gpu", "ram")


# --------------------------------------------------------------------------
# config serialization (splice: everything from the first [[widgets]] on is
# regenerated; comments/keys above it survive)

def _toml_value(v) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    if isinstance(v, dict):
        return "{ " + ", ".join(f"{k} = {_toml_value(x)}" for k, x in v.items()
                                if x is not None) + " }"
    if isinstance(v, list):
        return "[" + ", ".join(_toml_value(x) for x in v) + "]"
    return '"' + str(v).replace('\\', '\\\\').replace('"', '\\"') + '"'


def _widget_toml(w: dict, table: str = "widgets") -> str:
    lines = [f"[[{table}]]"]
    for k, v in w.items():
        if k == "type":
            lines.insert(1, f'type = "{v}"')
        else:
            lines.append(f"{k} = {_toml_value(v)}")
    return "\n".join(lines)


def apply_config(widgets: list[dict], width: int, height: int, cfg_data: dict) -> None:
    """Write the whole config: canonical head (incl. fonts) + widget layout."""
    data = {k: v for k, v in cfg_data.items() if k != "widgets"}
    data["width"], data["height"] = width, height
    body = "\n\n".join(_widget_toml(w) for w in widgets)
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(serialize_head(data) + "\n" + body + "\n")


HEAD_ORDER = ["device", "fps", "width", "height", "supersample",
              "orientation", "jpeg_quality"]
HEAD_COMMENTS = {
    "orientation": "dial 0/90/180/270 · wire = (mount_base − orientation) % 360",
    "fps": "render cadence for values",
}


def serialize_head(data: dict) -> str:
    lines = ["# omAIOpanel configuration — written by the editor"]
    for k in HEAD_ORDER:
        if k in data:
            c = HEAD_COMMENTS.get(k)
            lines.append(f"{k} = {_toml_value(data[k])}" + (f"  # {c}" if c else ""))
    for k, v in data.items():
        if k in HEAD_ORDER or k == "widgets":
            continue
        if isinstance(v, dict):
            body = [(kk, vv) for kk, vv in v.items() if vv is not None]
            if not body:
                continue                 # e.g. weather left on auto (None/None)
            lines.append("")
            lines.append(f"[{k}]")
            for kk, vv in body:
                if isinstance(vv, dict):
                    lines.append("")
                    lines.append(f"[{k}.{kk}]")
                    for k3, v3 in vv.items():
                        if v3 is not None:
                            lines.append(f"{k3} = {_toml_value(v3)}")
                else:
                    lines.append(f"{kk} = {_toml_value(vv)}")
        elif isinstance(v, list):
            continue
        else:
            if v is None:
                continue
            lines.append(f"{k} = {_toml_value(v)}")
    return "\n".join(lines) + "\n"


class PopupWindow(Gtk.Window):
    """Small transient editor popup: header with title, Escape/native ✕ hide it
    (close-request is intercepted — the window is reused, never destroyed)."""

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.connect("close-request", self._on_close_request)

    def _on_close_request(self, _win) -> bool:
        self.hide()
        return True            # block destroy — present() re-shows this instance

    def _make_popup_chrome(self, title: str) -> None:
        header = Gtk.HeaderBar()
        header.set_show_title_buttons(True)   # native round close, like the main window
        header.set_title_widget(Gtk.Label(label=title,
                                          halign=Gtk.Align.START,
                                          css_classes=["omarchy-muted"]))
        self.set_titlebar(header)
        key = Gtk.EventControllerKey()
        key.connect("key-pressed", self._on_popup_key)
        self.add_controller(key)

    def _on_popup_key(self, ctrl, keyval, keycode, state) -> bool:
        if keyval == Gdk.KEY_Escape:
            self.hide()
            return True
        return False


def _fonts_data() -> dict:
    d = STORE.cfg.data.setdefault("fonts", {})
    d.setdefault("family", "")
    for role in ("title", "data"):
        d.setdefault(role, {"color": "", "scale": 1.0, "family": ""})
    return d


class FontsPanel(Gtk.Box):
    """Panel-wide typography: two roles (title / data), each with color,
    scale, family — empty = theme-synced. Lives in the panel tab."""

    def __init__(self) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        self._updating = False
        box = self

        fd = _fonts_data()

        box.append(Gtk.Label(label="base family (empty = theme font)",
                             halign=Gtk.Align.START, css_classes=["dim-label"]))
        self.e_base = Gtk.Entry(text=str(fd.get("family", "")))
        self.e_base.connect("changed", self._on_base)
        box.append(self.e_base)

        self._role_rows = {}
        for role, pretty in (("title", "titles — labels, headings, row names"),
                             ("data", "data — values, temps, clock time")):
            box.append(Gtk.Label(label=pretty, halign=Gtk.Align.START,
                                 css_classes=["omarchy-muted"]))
            grid = Gtk.Grid(row_spacing=6, column_spacing=8)
            box.append(grid)

            grid.attach(Gtk.Label(label="color", halign=Gtk.Align.START), 0, 0, 1, 1)
            e_color = Gtk.Entry(text=str(fd[role].get("color", "")))
            e_color.set_placeholder_text("empty = theme · #hex or name")
            grid.attach(e_color, 1, 0, 1, 1)

            grid.attach(Gtk.Label(label="scale", halign=Gtk.Align.START), 0, 1, 1, 1)
            adj = Gtk.Adjustment(value=float(fd[role].get("scale", 1.0)), lower=0.5,
                                 upper=2.0, step_increment=0.05, page_increment=0.2,
                                 page_size=0)
            sp = Gtk.SpinButton(adjustment=adj, digits=2)
            grid.attach(sp, 1, 1, 1, 1)

            grid.attach(Gtk.Label(label="family", halign=Gtk.Align.START), 0, 2, 1, 1)
            e_fam = Gtk.Entry(text=str(fd[role].get("family", "")))
            e_fam.set_placeholder_text("empty = base family")
            grid.attach(e_fam, 1, 2, 1, 1)

            grid.attach(Gtk.Label(label="size (px)", halign=Gtk.Align.START), 0, 3, 1, 1)
            try:
                cur_size = float(fd[role].get("size", 18 if role == "title" else 24))
            except (TypeError, ValueError):
                cur_size = 18.0 if role == "title" else 24.0
            adj_s = Gtk.Adjustment(value=cur_size, lower=6, upper=64,
                                   step_increment=1, page_increment=4, page_size=0)
            sp_size = Gtk.SpinButton(adjustment=adj_s, digits=0)
            sp_size.connect("value-changed", self._on_role_size, role)
            grid.attach(sp_size, 1, 3, 1, 1)

            e_color.connect("changed", self._on_role_text, role, "color")
            e_fam.connect("changed", self._on_role_text, role, "family")
            sp.connect("value-changed", self._on_role_scale, role)
            self._role_rows[role] = (e_color, sp, e_fam)

        box.append(Gtk.Label(
            label="changes preview instantly · Apply → panel persists",
            halign=Gtk.Align.START, css_classes=["dim-label"], wrap=True))

    def _on_base(self, entry) -> None:
        if self._updating:
            return
        _fonts_data()["family"] = entry.get_text().strip()
        STORE.dirty = True
        request_render()

    def _on_role_text(self, entry, role, key) -> None:
        if self._updating:
            return
        _fonts_data()[role][key] = entry.get_text().strip()
        STORE.dirty = True
        request_render()

    def _on_role_scale(self, sp, role) -> None:
        if self._updating:
            return
        _fonts_data()[role]["scale"] = round(sp.get_value(), 2)
        STORE.dirty = True
        request_render()

    def _on_role_size(self, sp, role) -> None:
        if self._updating:
            return
        _fonts_data()[role]["size"] = int(sp.get_value())
        STORE.dirty = True
        request_render()


class StylePanel(Gtk.Box):
    """Panel-wide look: card corners/outline + wallpaper choice/brightness.
    Lives in the panel tab."""

    def __init__(self) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        self._updating = False

        data = STORE.cfg.data
        cards = data.setdefault("cards", {})
        cards.setdefault("rounded", True)
        cards.setdefault("outline", "subtle")
        wp = data.setdefault("wallpaper", {})
        wp.setdefault("mode", "theme")
        wp.setdefault("path", "")
        wp.setdefault("brightness", 1.0)
        self._cards = cards
        self._wp = wp

        box = self

        box.append(Gtk.Label(label="cards", halign=Gtk.Align.START,
                             css_classes=["omarchy-muted"]))
        r = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        r.append(Gtk.Label(label="rounded corners", halign=Gtk.Align.START, hexpand=True))
        self.sw_rounded = Gtk.Switch(active=bool(cards.get("rounded", True)))
        self.sw_rounded.connect("notify::active", self._on_rounded)
        r.append(self.sw_rounded)
        box.append(r)

        r = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        r.append(Gtk.Label(label="outline", halign=Gtk.Align.START, hexpand=True))
        self.dd_outline = Gtk.DropDown(model=Gtk.StringList.new(["subtle", "accent", "none"]))
        model = self.dd_outline.get_model()
        for j in range(model.get_n_items()):
            if model.get_string(j) == cards.get("outline", "subtle"):
                self.dd_outline.set_selected(j)
                break
        self.dd_outline.connect("notify::selected-item", self._on_outline)
        r.append(self.dd_outline)
        box.append(r)

        box.append(Gtk.Label(label="wallpaper", halign=Gtk.Align.START,
                             css_classes=["omarchy-muted"]))
        box.append(Gtk.Label(label="background", halign=Gtk.Align.START,
                             css_classes=["dim-label"]))
        choices = ["follow theme"] + [p.name for p in self._list_backgrounds()]
        self.dd_bg = Gtk.DropDown(model=Gtk.StringList.new(choices))
        cur = 0
        if wp.get("mode") == "path" and wp.get("path"):
            base = Path(wp["path"]).name
            for j in range(1, len(choices)):
                if choices[j] == base:
                    cur = j
                    break
        self.dd_bg.set_selected(cur)
        self.dd_bg.connect("notify::selected-item", self._on_bg)
        box.append(self.dd_bg)

        box.append(Gtk.Label(label="brightness", halign=Gtk.Align.START,
                             css_classes=["dim-label"]))
        try:
            bright = float(wp.get("brightness", 1.0))
        except (TypeError, ValueError):
            bright = 1.0
        adj = Gtk.Adjustment(value=bright, lower=0.2, upper=1.875,
                             step_increment=0.05, page_increment=0.2, page_size=0)
        self.scale_bright = Gtk.Scale(orientation=Gtk.Orientation.HORIZONTAL,
                                      adjustment=adj, digits=2)
        self.scale_bright.set_draw_value(True)
        self.scale_bright.connect("value-changed", self._on_brightness)
        box.append(self.scale_bright)

        box.append(Gtk.Label(label="render", halign=Gtk.Align.START,
                             css_classes=["omarchy-muted"]))
        r = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        r.append(Gtk.Label(label="refresh rate (fps)", halign=Gtk.Align.START, hexpand=True))
        try:
            fps = float(data.get("fps", 15.0))
        except (TypeError, ValueError):
            fps = 15.0
        adj_fps = Gtk.Adjustment(value=fps, lower=1, upper=30,
                                 step_increment=1, page_increment=5, page_size=0)
        self.sp_fps = Gtk.SpinButton(adjustment=adj_fps, digits=0)
        self.sp_fps.set_size_request(70, -1)
        self.sp_fps.connect("value-changed", self._on_fps)
        r.append(self.sp_fps)
        box.append(r)

        r2 = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        r2.append(Gtk.Label(label="supersampling", halign=Gtk.Align.START, hexpand=True))
        self.dd_ss = Gtk.DropDown(model=Gtk.StringList.new(
            ["1× — faster", "2× — smoother"]))
        try:
            cur_ss = int(data.get("supersample", 2))
        except (TypeError, ValueError):
            cur_ss = 2
        self.dd_ss.set_selected(0 if cur_ss < 2 else 1)
        self.dd_ss.connect("notify::selected-item", self._on_ss)
        r2.append(self.dd_ss)
        box.append(r2)
        box.append(Gtk.Label(label="2× renders at double size then downscales — "
                             "smoother rings and text, ~2× CPU",
                             halign=Gtk.Align.START, wrap=True, css_classes=["dim-label"]))
        box.append(Gtk.Label(label="higher = smoother VU meter, more CPU "
                             "(~30% of a core at 15 fps)",
                             halign=Gtk.Align.START, wrap=True, css_classes=["dim-label"]))

        box.append(Gtk.Label(
            label="changes preview instantly · Apply → panel persists",
            halign=Gtk.Align.START, css_classes=["dim-label"], wrap=True))

    def _on_fps(self, sp) -> None:
        STORE.cfg.data["fps"] = float(sp.get_value())
        STORE.dirty = True
        request_render()

    def _on_ss(self, dd, _pspec) -> None:
        STORE.cfg.data["supersample"] = 1 if dd.get_selected() == 0 else 2
        STORE.bg = None            # background was rendered at the old size
        STORE.dirty = True
        request_render()

    def _list_backgrounds(self) -> list[Path]:
        """Theme backgrounds + per-theme user backgrounds (omarchy layout)."""
        from aio.theme import IMAGE_EXTS
        out, seen = [], set()
        dirs = []
        name_file = Path.home() / ".local" / "state" / "omarchy" / "current" / "theme.name"
        if name_file.exists():
            tname = name_file.read_text().strip()
            if tname:
                dirs.append(Path.home() / ".config" / "omarchy" / "backgrounds" / tname)
        if STORE.theme.dir:
            dirs.append(STORE.theme.dir.resolve() / "backgrounds")
        for d in dirs:
            if d.is_dir():
                for f in sorted(d.iterdir()):
                    if f.suffix.lower() in IMAGE_EXTS and f.name not in seen:
                        seen.add(f.name)
                        out.append(f)
        return out

    def _reload_theme_bg(self) -> None:
        STORE.theme.load({
            "font_family": STORE.cfg.fonts.get("family"),
            "wallpaper_path": STORE._wp.get("path") if STORE._wp.get("mode") == "path" else None,
        })
        STORE.bg = None

    def _changed(self) -> None:
        STORE.dirty = True
        request_render()

    def _on_rounded(self, sw, _pspec) -> None:
        if self._updating:
            return
        self._cards["rounded"] = sw.get_active()
        self._changed()

    def _on_outline(self, dd, _pspec) -> None:
        if self._updating:
            return
        item = dd.get_selected_item()
        if item is not None:
            self._cards["outline"] = item.get_string()
            self._changed()

    def _on_bg(self, dd, _pspec) -> None:
        if self._updating:
            return
        item = dd.get_selected_item()
        if item is None:
            return
        s = item.get_string()
        if s == "follow theme":
            self._wp["mode"], self._wp["path"] = "theme", ""
        else:
            match = next((p for p in self._list_backgrounds() if p.name == s), None)
            if match is None:
                return
            self._wp["mode"], self._wp["path"] = "path", str(match)
        self._reload_theme_bg()
        self._changed()

    def _on_brightness(self, scale) -> None:
        if self._updating:
            return
        self._wp["brightness"] = round(scale.get_value(), 2)
        STORE.bg = None
        self._changed()


def snap5(v: float) -> int:
    return int(round(v / 5) * 5)


# --------------------------------------------------------------------------
# full-panel presets: ONE file holds BOTH orientations

PRESETS_DIR = CONFIG_PATH.parent / "presets"

# last-used preset + its active variant — restored on launch so the editor
# opens on the exact layout (orientation) the user was last working on
SESSION_PATH = CONFIG_PATH.parent / "last-session.json"


def _save_session(preset: str | None, variant: str | None) -> None:
    """Persist {preset, variant}; best-effort, never blocks the UI."""
    try:
        SESSION_PATH.parent.mkdir(parents=True, exist_ok=True)
        SESSION_PATH.write_text(json.dumps({"preset": preset, "variant": variant}))
    except Exception:
        pass


def _load_session() -> dict:
    try:
        d = json.loads(SESSION_PATH.read_text())
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _save_session_variant(variant: str) -> None:
    """Bump the recorded variant when a preset is active; no-op otherwise."""
    d = _load_session()
    if d.get("preset"):
        d["variant"] = variant
        _save_session(d["preset"], variant)


def snapshot_variant() -> dict:
    """The active layout: widgets + canvas shape + wire dial."""
    return {"widgets": copy.deepcopy(STORE.widgets),
            "width": STORE.W, "height": STORE.H,
            "orientation": int(STORE.cfg.data.get("orientation", 90))}


def rotated_variant(v: dict) -> dict:
    """v rotated 90° to the other orientation (inverse transforms both ways)."""
    ws = copy.deepcopy(v["widgets"])
    to_landscape = int(v["width"]) < int(v["height"])
    if to_landscape:                      # portrait -> landscape: CCW
        old_w = int(v["width"])
        for w in ws:
            x, y = w.get("x", 0), w.get("y", 0)
            ww, hh = w.get("w", 10), w.get("h", 10)
            w["x"], w["y"] = snap5(y), snap5(old_w - x - ww)
            w["w"], w["h"] = hh, ww
    else:                                 # landscape -> portrait: CW
        old_h = int(v["height"])
        for w in ws:
            x, y = w.get("x", 0), w.get("y", 0)
            ww, hh = w.get("w", 10), w.get("h", 10)
            w["x"], w["y"] = snap5(old_h - y - hh), snap5(x)
            w["w"], w["h"] = hh, ww
    return {"widgets": ws, "width": int(v["height"]), "height": int(v["width"]),
            "orientation": int(v.get("orientation", 90))}


def serialize_preset(variants: dict) -> str:
    lines = ["# omAIOpanel panel preset — both orientations in one file",
             f"saved = {_toml_value(time.strftime('%Y-%m-%d %H:%M:%S'))}"]
    defaults = {"portrait": (540, 960), "landscape": (960, 540)}
    for name in ("portrait", "landscape"):
        v = variants.get(name)
        lines += ["", f"[{name}]"]
        if not v:
            dw, dh = defaults[name]
            lines += [f"width = {dw}", f"height = {dh}", "orientation = 90"]
            continue
        lines += [f"width = {int(v['width'])}", f"height = {int(v['height'])}",
                  f"orientation = {int(v.get('orientation', 90))}"]
        for w in v.get("widgets", []):
            lines += ["", _widget_toml(w, table=f"{name}.widgets")]
    return "\n".join(lines) + "\n"


def save_preset(name: str, variants: dict) -> Path:
    PRESETS_DIR.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r"[^A-Za-z0-9_-]+", "_", (name or "").strip()).strip("_") or "preset"
    p = PRESETS_DIR / f"{safe}.toml"
    p.write_text(serialize_preset(variants))
    return p


def load_preset(name: str) -> dict:
    data = tomllib.loads((PRESETS_DIR / f"{name}.toml").read_text())
    out = {}
    for key in ("portrait", "landscape"):
        v = data.get(key)
        if isinstance(v, dict) and isinstance(v.get("widgets"), list):
            out[key] = {"width": int(v.get("width", 540)),
                        "height": int(v.get("height", 960)),
                        "orientation": int(v.get("orientation", 90)),
                        "widgets": [dict(w) for w in v["widgets"]]}
    if not out:
        raise ValueError(f"preset {name!r} has no layouts")
    return out


def list_presets() -> list[str]:
    if not PRESETS_DIR.exists():
        return []
    return sorted(p.stem for p in PRESETS_DIR.glob("*.toml"))


def variants_to_save() -> dict:
    """Both orientations for a preset write: current + the other one
    (stashed if edited before, seeded by rotating otherwise)."""
    cur = STORE.variant
    other = "landscape" if cur == "portrait" else "portrait"
    snap = snapshot_variant()
    return {cur: snap,
            other: STORE.variants.get(other) or rotated_variant(snap)}


# --------------------------------------------------------------------------
# shared state

class Store:
    def __init__(self) -> None:
        self.cfg = load_config()
        self.widgets: list[dict] = [dict(w) for w in self.cfg.widgets]
        self.theme = OmarchyTheme()
        try:
            self.theme.load({
                "font_family": self.cfg.fonts.get("family"),
                "wallpaper_path": self.cfg.wallpaper.get("path")
                if self.cfg.wallpaper.get("mode") == "path" else None,
            })
        except FileNotFoundError:
            pass
        self.bg = None
        self.frame = None              # last rendered PIL image
        self.selected: int = -1
        self.dirty = False             # layout edited since last Apply
        self.hub: SensorHub | None = None
        self.render_gen = 0
        self.variant = ("landscape" if int(self.cfg.width) > int(self.cfg.height)
                        else "portrait")
        self.variants: dict[str, dict | None] = {"portrait": None, "landscape": None}
        self.preset_origin: str | None = None    # preset this layout was loaded from

    @property
    def W(self) -> int:
        return int(self.cfg.width)

    @property
    def H(self) -> int:
        return int(self.cfg.height)

    def rebuild_bg(self) -> None:
        try:
            self.bg = render_background(
                self.theme, self.cfg.wallpaper, self.W, self.H,
                int(self.cfg.supersample))
        except Exception:
            traceback.print_exc()
            from PIL import Image
            self.bg = Image.new("RGB", (self.W, self.H), (10, 10, 10))

    def stash_current(self) -> None:
        self.variants[self.variant] = snapshot_variant()

    def apply_variant(self, v: dict) -> None:
        """Make v the active layout (widgets + canvas shape + wire dial)."""
        self.widgets = copy.deepcopy(v["widgets"])
        self.cfg.data["width"] = int(v["width"])
        self.cfg.data["height"] = int(v["height"])
        self.cfg.data["orientation"] = int(v.get("orientation", 90))
        self.selected = -1
        self.bg = None
        self.dirty = True
        request_render()


STORE = Store()


# --------------------------------------------------------------------------
# worker: render the preview frame off the UI thread

_render_lock = threading.Lock()
_pending_edit = threading.Event()


def render_worker() -> None:
    """Render loop thread — started after the app is up."""
    if STORE.hub is None:
        STORE.hub = SensorHub()
    while True:
        _pending_edit.wait(timeout=0.1)     # ~10 fps editor preview cadence
        _pending_edit.clear()
        if STORE.hub and any(w.get("type") == "media" for w in STORE.widgets):
            STORE.hub.start_media()
        if STORE.hub and any(w.get("type") == "clock" and w.get("weather", True)
                             for w in STORE.widgets):
            wx = STORE.cfg.weather
            STORE.hub.start_weather(wx.get("lat"), wx.get("lon"))
        gen = STORE.render_gen
        try:
            with _render_lock:
                if STORE.bg is None:
                    STORE.rebuild_bg()
                bg = STORE.bg          # local ref: watcher may null STORE.bg
                s = STORE.hub.sample() if STORE.hub else {}
                frame = render_frame(STORE.theme, bg, STORE.widgets, s,
                                     STORE.W, STORE.H, int(STORE.cfg.supersample),
                                     fonts=STORE.cfg.fonts, cards=STORE.cfg.cards)
            STORE.frame = frame
            GLib.idle_add(on_frame_ready, gen)
        except Exception:
            traceback.print_exc()
            GLib.idle_add(on_frame_ready, gen)


def on_frame_ready(_gen) -> bool:
    win = _window_ref[0]
    if win is not None:
        win.canvas.queue_draw()
    return False


_window_ref: list = [None]


def request_render() -> None:
    _pending_edit.set()


_debounce_render_id = 0


def _debounce_render() -> None:
    """request_render() coalesced — entries/spins fire per keystroke/step.

    (This was called by the old property panel but never defined: edits
    raised NameError inside the signal handler, which GTK swallows, so the
    fields silently did nothing until the canvas re-render picked them up.)
    """
    global _debounce_render_id
    if _debounce_render_id:
        GLib.source_remove(_debounce_render_id)
    _debounce_render_id = GLib.timeout_add(150, _do_debounce_render)


def _do_debounce_render() -> bool:
    global _debounce_render_id
    _debounce_render_id = 0
    request_render()
    return False


# --------------------------------------------------------------------------
# canvas

def _argb_surface(img: Image.Image) -> cairo.ImageSurface:
    """PIL image -> cairo ARGB32 surface (LE memory order: B, G, R, A)."""
    data = np.asarray(img.convert("RGB"), dtype=np.uint8)
    argb = bytearray(np.dstack([
        data[..., 2], data[..., 1], data[..., 0],
        np.full((*data.shape[:2],), 255, dtype=np.uint8),
    ]).tobytes())
    return cairo.ImageSurface.create_for_data(
        argb, cairo.FORMAT_ARGB32, img.width, img.height)


class PanelCanvas(Gtk.DrawingArea):
    """Draws the live frame + widget rectangles; click selects, drag moves."""

    SNAP = 5              # panel px — snap-to grid for move/resize
    DRAG_THRESHOLD = 4.0  # screen px of movement before a drag engages
    HANDLE_PX = 10.0      # resize handle size on screen
    MIN_SIZE = 20         # smallest allowed widget w/h

    def __init__(self) -> None:
        super().__init__()
        self.scale = 0.5
        self.ox = 0.0
        self.oy = 0.0
        self._mode = None        # "move" | "resize" | None
        self._engaged = False
        self._orig = None        # widget (x, y, w, h) at press
        self.set_draw_func(self._draw)
        self.set_size_request(160, 160)
        self.set_hexpand(True)
        self.set_vexpand(True)

        drag = Gtk.GestureDrag()
        drag.set_button(1)
        drag.connect("drag-begin", self._on_drag_begin)
        drag.connect("drag-update", self._on_drag_update)
        drag.connect("drag-end", self._on_drag_end)
        self.add_controller(drag)

        motion = Gtk.EventControllerMotion()
        motion.connect("motion", self._on_motion)
        self.add_controller(motion)

    # -- coordinate mapping --

    def _metrics(self) -> tuple[float, float, float, float, float, float]:
        """(scale, ox, oy, theta, disp_w, disp_h) of the ROTATED display box."""
        aw, ah = self.get_width(), self.get_height()
        fw, fh = STORE.W, STORE.H
        dial = int(STORE.cfg.data.get("orientation", 90)) % 360
        angle = (dial - 90) % 360           # 0 portrait · 90 landscape · …
        theta = 180 if angle >= 180 else 0  # 180/−90 display upside-down
        dw, dh = (fh, fw) if theta % 180 else (fw, fh)
        s = min(max(aw, 100) / dw, max(ah, 100) / dh)
        ox = (aw - dw * s) / 2
        oy = (ah - dh * s) / 2
        return s, ox, oy, theta, dw * s, dh * s

    def _to_panel(self, x: float, y: float) -> tuple[float, float]:
        s, ox, oy, theta, dw, dh = self._metrics()
        dx = x - (ox + dw / 2)
        dy = y - (oy + dh / 2)
        a = math.radians(theta)             # inverse of a +theta (CW) rotation
        rx = dx * math.cos(a) + dy * math.sin(a)
        ry = -dx * math.sin(a) + dy * math.cos(a)
        return rx / s + STORE.W / 2, ry / s + STORE.H / 2

    # -- hit test / selection --

    def _hit(self, px: float, py: float) -> int:
        for i in range(len(STORE.widgets) - 1, -1, -1):
            w = STORE.widgets[i]
            x, y = int(w.get("x", 0)), int(w.get("y", 0))
            ww, wh = int(w.get("w", 10)), int(w.get("h", 10))
            if x <= px <= x + ww and y <= py <= y + wh:
                return i
        return -1

    def _handle_at(self, ex: float, ey: float) -> int:
        """Index of the widget whose bottom-right handle is under the cursor."""
        s, ox, oy, theta, dw, dh = self._metrics()
        a = math.radians(theta)
        for i in range(len(STORE.widgets) - 1, -1, -1):
            w = STORE.widgets[i]
            vx = (w.get("x", 0) + w.get("w", 0) - STORE.W / 2) * s
            vy = (w.get("y", 0) + w.get("h", 0) - STORE.H / 2) * s
            hx = ox + dw / 2 + vx * math.cos(a) - vy * math.sin(a)
            hy = oy + dh / 2 + vx * math.sin(a) + vy * math.cos(a)
            if abs(ex - hx) <= self.HANDLE_PX and abs(ey - hy) <= self.HANDLE_PX:
                return i
        return -1

    def _snap(self, v: float) -> int:
        return int(round(v / self.SNAP) * self.SNAP)

    def _on_motion(self, ctrl, ex, ey) -> None:
        if self._handle_at(ex, ey) != -1:
            name = "nwse-resize"
        elif self._hit(*self._to_panel(ex, ey)) != -1:
            name = "move"
        else:
            name = "default"
        self.set_cursor(Gdk.Cursor.new_from_name(name))

    def _on_drag_begin(self, gesture, ex, ey) -> None:
        self._mode = None
        self._engaged = False
        px, py = self._to_panel(ex, ey)
        handle_i = self._handle_at(ex, ey)
        i = handle_i if handle_i != -1 else self._hit(px, py)
        if i != STORE.selected:
            STORE.selected = i
            win = _window_ref[0]
            if win:
                win.on_selection_changed()
        if i == -1:
            self._orig = None
            self.queue_draw()
            return
        w = STORE.widgets[i]
        self._orig = (w.get("x", 0), w.get("y", 0), w.get("w", 10), w.get("h", 10))
        self._mode = "resize" if handle_i != -1 else "move"

    def _on_drag_update(self, gesture, dx, dy) -> None:
        if self._mode is None or STORE.selected < 0 or self._orig is None:
            return
        if not self._engaged:
            if (dx * dx + dy * dy) ** 0.5 < self.DRAG_THRESHOLD:
                return          # a click: select only, nothing moves
            self._engaged = True
        s = self._metrics()[0]
        w = STORE.widgets[STORE.selected]
        x0, y0, w0, h0 = self._orig
        if self._mode == "move":
            w["x"] = max(0, min(STORE.W - w0, self._snap(x0 + dx / s)))
            w["y"] = max(0, min(STORE.H - h0, self._snap(y0 + dy / s)))
        else:  # resize from the bottom-right handle
            w["w"] = max(self.MIN_SIZE, min(STORE.W - w["x"], self._snap(w0 + dx / s)))
            w["h"] = max(self.MIN_SIZE, min(STORE.H - w["y"], self._snap(h0 + dy / s)))
        STORE.dirty = True
        self.queue_draw()
        win = _window_ref[0]
        if win:
            win.props_panel.refresh()

    def _on_drag_end(self, gesture, dx, dy, *args) -> None:
        if self._engaged:
            request_render()
            win = _window_ref[0]
            if win:
                win.left_panel.refresh()
        self._mode = None
        self._engaged = False
        self._orig = None

    # -- painting --

    def _draw(self, area, ctx, w, h) -> None:
        s, ox, oy, theta, dw, dh = self._metrics()
        _canvas_bg(ctx)
        ctx.paint()

        # everything below lives in CANVAS coordinates; the ctx carries the
        # rotation so frame and widget overlay rotate together
        ctx.save()
        ctx.translate(ox + dw / 2, oy + dh / 2)
        ctx.rotate(math.radians(theta))
        ctx.scale(s, s)
        ctx.translate(-STORE.W / 2, -STORE.H / 2)

        frame = STORE.frame
        if frame is not None:
            try:
                surface = _argb_surface(frame)
                ctx.set_source_surface(surface, 0, 0)
                ctx.paint()
            except Exception:
                traceback.print_exc()

        # widget overlay
        accent = _accent_gdk()
        for i, wg in enumerate(STORE.widgets):
            x = wg.get("x", 0)
            y = wg.get("y", 0)
            ww = wg.get("w", 10)
            wh = wg.get("h", 10)
            sel = i == STORE.selected
            ctx.set_source_rgba(*accent, 0.85 if sel else 0.30)
            ctx.set_line_width(2.0 / s if sel else 1.0 / s)
            ctx.rectangle(x, y, ww, wh)
            ctx.stroke()
            if sel:
                ctx.set_source_rgba(*accent, 0.08)
                ctx.rectangle(x, y, ww, wh)
                ctx.fill()
            # label tag
            label = f"{i + 1}·{wg.get('type', '?')}"
            ctx.set_source_rgba(*accent, 0.9 if sel else 0.55)
            ctx.set_font_size(11 / s)
            ctx.move_to(x + 4, y + 13)
            ctx.show_text(label)

            # bottom-right resize handle (screen-constant size)
            hs = self.HANDLE_PX / s
            ctx.set_source_rgba(*accent, 0.95 if sel else 0.45)
            ctx.rectangle(x + ww - hs, y + wh - hs, hs, hs)
            ctx.fill()
            ctx.set_source_rgba(0, 0, 0, 0.55)
            ctx.set_line_width(1.0 / s)
            ctx.rectangle(x + ww - hs, y + wh - hs, hs, hs)
            ctx.stroke()

        ctx.restore()


def _accent_gdk() -> tuple[float, float, float]:
    try:
        c = STORE.theme.color("accent")
        return (c[0] / 255, c[1] / 255, c[2] / 255)
    except Exception:
        return (0.65, 0.44, 0.40)


# --------------------------------------------------------------------------
# widget settings (edit mode: identity fields + lines editor)

class WidgetSettingsView(Gtk.Box):
    """Settings for ONE widget: title/label/color/sensor + its data lines."""

    def __init__(self) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self._updating = False
        self._lines_for: int | None = None
        self._drag_src = None

        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        back = Gtk.Button.new_from_icon_name("go-previous-symbolic")
        back.set_tooltip_text("back to the editor menu")
        back.connect("clicked",
                     lambda b: _window_ref[0] and _window_ref[0]._show_view("main"))
        row.append(back)
        self.title = Gtk.Label(label="widget settings", halign=Gtk.Align.START)
        self.title.set_css_classes(["aio-section"])
        row.append(self.title)
        self.append(row)

        grid = Gtk.Grid(row_spacing=6, column_spacing=8)
        self.append(grid)

        lab = Gtk.Label(label="title", halign=Gtk.Align.START)
        self.entry_title = Gtk.Entry()
        self.entry_title.connect("changed", self._on_entry, "title")
        grid.attach(lab, 0, 0, 1, 1)
        grid.attach(self.entry_title, 1, 0, 1, 1)

        lab = Gtk.Label(label="label", halign=Gtk.Align.START)
        self.entry_label = Gtk.Entry()
        self.entry_label.connect("changed", self._on_entry, "label")
        grid.attach(lab, 0, 1, 1, 1)
        grid.attach(self.entry_label, 1, 1, 1, 1)

        lab = Gtk.Label(label="color", halign=Gtk.Align.START)
        self.entry_color = Gtk.Entry()
        self.entry_color.set_placeholder_text("accent | magenta | #rrggbb")
        self.entry_color.connect("changed", self._on_entry, "color")
        grid.attach(lab, 0, 2, 1, 1)
        grid.attach(self.entry_color, 1, 2, 1, 1)

        lab = Gtk.Label(label="sensor", halign=Gtk.Align.START)
        self.dd_sensor = Gtk.DropDown(model=Gtk.StringList.new([*GAUGE_SENSORS]))
        self.dd_sensor.connect("notify::selected-item", self._on_sensor)
        grid.attach(lab, 0, 3, 1, 1)
        grid.attach(self.dd_sensor, 1, 3, 1, 1)

        self.lines_title = Gtk.Label(label="lines", halign=Gtk.Align.START,
                                     css_classes=["omarchy-muted"])
        self.lines_title.set_tooltip_text(ROW_HINT)
        self.append(self.lines_title)
        self.lines_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        scroll = Gtk.ScrolledWindow(vexpand=True)
        scroll.set_child(self.lines_box)
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.append(scroll)
        self.btn_add_line = Gtk.Button(label="＋ Add line")
        self.btn_add_line.connect("clicked", self._on_add_line)
        self.append(self.btn_add_line)

        self.refresh()

    # -- context ------------------------------------------------------------

    def _selected_widget(self):
        if 0 <= STORE.selected < len(STORE.widgets):
            return STORE.widgets[STORE.selected]
        return None

    @staticmethod
    def _lines_spec(w: dict):
        """(attr, entries, shows_labels) for widgets that have data lines."""
        t = w.get("type")
        if t == "stats":
            return ("rows", w.get("rows", []), True)
        if t == "gauge":
            return ("sub", w.get("sub", []), False)
        return None

    def _after_edit(self) -> None:
        STORE.dirty = True
        self._lines_for = None             # force a lines rebuild
        GLib.idle_add(self.refresh)
        request_render()
        win = _window_ref[0]
        if win:
            win.canvas.queue_draw()

    # -- binding ------------------------------------------------------------

    def refresh(self) -> None:
        self._updating = True
        try:
            w = self._selected_widget()
            if w is None:
                self.title.set_text("widget settings")
                for e in (self.entry_label, self.entry_color, self.entry_title):
                    e.set_sensitive(False)
                self.dd_sensor.set_sensitive(False)
                self.btn_add_line.set_sensitive(False)
                self._clear_lines()
                self.lines_box.append(Gtk.Label(
                    label="no widget selected",
                    halign=Gtk.Align.START, css_classes=["dim-label"]))
                self._lines_for = None
                return
            t = w.get("type", "?")
            self.title.set_text(f"widget {STORE.selected + 1} · {t}")
            for e in (self.entry_label, self.entry_color, self.entry_title):
                e.set_sensitive(True)
            self.entry_label.set_text(str(w.get("label", "")))
            self.entry_color.set_text(str(w.get("color", "")))
            self.entry_title.set_text(str(w.get("title", "")))
            is_gauge = t == "gauge"
            self.dd_sensor.set_sensitive(is_gauge)
            self.entry_label.set_sensitive(t in ("gauge", "clock", "stats"))
            if is_gauge:
                sensor = w.get("sensor", "cpu")
                model = self.dd_sensor.get_model()
                for i in range(model.get_n_items()):
                    if model.get_string(i) == sensor:
                        self.dd_sensor.set_selected(i)
                        break
            if self._lines_for != STORE.selected:
                self._refresh_lines(w)
                self._lines_for = STORE.selected
        finally:
            self._updating = False

    def _clear_lines(self) -> None:
        child = self.lines_box.get_first_child()
        while child is not None:
            nxt = child.get_next_sibling()
            self.lines_box.remove(child)
            child = nxt

    def _refresh_lines(self, w: dict) -> None:
        self._clear_lines()
        spec = self._lines_spec(w)
        if spec is None:
            self.lines_box.append(Gtk.Label(
                label="this widget has no data lines",
                halign=Gtk.Align.START, css_classes=["dim-label"]))
            self.btn_add_line.set_sensitive(False)
            return
        self.btn_add_line.set_sensitive(True)
        key_name, entries, with_labels = spec
        for i, entry in enumerate(entries):
            self.lines_box.append(
                self._build_line_row(w, key_name, entries, i, with_labels))

    def _build_line_row(self, w, key_name, entries, i, with_labels) -> Gtk.Widget:
        entry = entries[i]
        key = _entry_key(entry)
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)

        grip = Gtk.Label(label="⠿", css_classes=["dim-label"])
        grip.set_size_request(14, -1)
        src = Gtk.DragSource(actions=Gdk.DragAction.MOVE)
        src.connect("prepare", self._on_line_drag_prepare, i)
        src.connect("drag-begin", self._on_line_drag_begin, row)
        grip.add_controller(src)
        row.prepend(grip)

        tgt = Gtk.DropTarget.new(GObject.TYPE_STRING, Gdk.DragAction.MOVE)
        tgt.connect("motion", self._on_line_drag_motion)
        tgt.connect("drop", self._on_line_drop, w, key_name, entries, i)
        row.add_controller(tgt)

        dd = Gtk.DropDown(model=Gtk.StringList.new(data_key_choices()))
        dd.set_size_request(120, -1)
        model = dd.get_model()
        for j in range(model.get_n_items()):
            if model.get_string(j) == key:
                dd.set_selected(j)
                break
        dd.connect("notify::selected-item", self._on_line_key, w, key_name, i)
        row.append(dd)

        if with_labels:
            e = Gtk.Entry(text=_entry_label(entry), hexpand=True)
            e.set_placeholder_text(f"{data_key_name(key)} (preset)")
            e.connect("changed", self._on_line_label, w, key_name, i)
            row.append(e)

        init_scale = float(entry.get("scale", 1.0)) if isinstance(entry, dict) else 1.0
        adj = Gtk.Adjustment(value=init_scale, lower=0.5, upper=2.0,
                             step_increment=0.05, page_increment=0.2, page_size=0)
        sp = Gtk.SpinButton(adjustment=adj, digits=2)
        sp.set_size_request(64, -1)
        sp.connect("value-changed", self._on_line_scale, w, key_name, i)
        row.append(sp)

        x = Gtk.Button(label="✕", css_classes=["flat"])
        x.connect("clicked", self._on_line_remove, w, key_name, i)
        row.append(x)
        return row

    # -- line drag & drop reorder ------------------------------------------

    def _on_line_drag_prepare(self, src, x, y, i):
        self._drag_src = i
        val = GObject.Value(GObject.TYPE_STRING, f"line:{i}")
        return Gdk.ContentProvider.new_for_value(val)

    def _on_line_drag_begin(self, src, drag, row):
        Gtk.DragIcon.set_from_paintable(drag, Gtk.WidgetPaintable.new(row), 0, 12)

    def _on_line_drag_motion(self, tgt, x, y):
        return Gdk.DragAction.MOVE

    def _on_line_drop(self, tgt, value, x, y, w, key_name, entries, dest_i):
        src = getattr(self, "_drag_src", None)
        self._drag_src = None
        if src is None or src >= len(entries):
            return False
        after = y > max(1, tgt.get_widget().get_height()) / 2
        dest = dest_i + (1 if after else 0)
        item = entries.pop(src)
        if src < dest:
            dest -= 1
        entries.insert(dest, item)
        self._after_edit()
        return True

    # -- line handlers ------------------------------------------------------

    def _on_line_key(self, dd, _pspec, w, key_name, i) -> None:
        item = dd.get_selected_item()
        entries = w.setdefault(key_name, [])
        if item is None or i >= len(entries):
            return
        entries[i] = _norm_line(entries[i], key=item.get_string())
        STORE.dirty = True
        GLib.idle_add(self.refresh)
        request_render()

    def _on_line_label(self, entry, w, key_name, i) -> None:
        entries = w.setdefault(key_name, [])
        if i >= len(entries):
            return
        entries[i] = _norm_line(entries[i], label=entry.get_text())
        STORE.dirty = True
        request_render()

    def _on_line_scale(self, sp, w, key_name, i) -> None:
        entries = w.setdefault(key_name, [])
        if i >= len(entries):
            return
        entries[i] = _norm_line(entries[i], scale=sp.get_value())
        STORE.dirty = True
        request_render()

    def _on_line_remove(self, btn, w, key_name, i) -> None:
        entries = w.setdefault(key_name, [])
        if i < len(entries):
            del entries[i]
        self._after_edit()

    def _on_add_line(self, btn) -> None:
        w = self._selected_widget()
        if w is None:
            return
        spec = self._lines_spec(w)
        if spec is None:
            return
        key_name, entries, _ = spec
        used = {_entry_key(e) for e in entries}
        choices = data_key_choices()
        key = next((k for k in choices if k not in used), choices[0])
        entries.append(key)
        self._after_edit()

    # -- identity field handlers --------------------------------------------

    def _on_entry(self, entry, key) -> None:
        if self._updating or STORE.selected < 0:
            return
        w = STORE.widgets[STORE.selected]
        text = entry.get_text()
        if key == "label" and w.get("type") == "stats" and not text:
            w.pop("label", None)
        else:
            w[key] = text
        STORE.dirty = True
        _debounce_render()

    def _on_sensor(self, dd, _pspec) -> None:
        if self._updating or STORE.selected < 0:
            return
        item = dd.get_selected_item()
        if item is not None:
            STORE.widgets[STORE.selected]["sensor"] = item.get_string()
            STORE.dirty = True
            _debounce_render()


ROW_HINT = ("row keys: " + ", ".join([
    "cpu_temp", "cpu_load", "gpu_temp", "gpu_power", "gpu_vram", "vram",
    "ram", "net", "net_up", "net_down", "disk_io", "disk_root",
    "uptime", "load1"]))


# --------------------------------------------------------------------------
# widget presets & line data keys

WIDGET_PRESETS = {
    "clock": {"type": "clock", "w": 460, "h": 130},
    "cpu": {"type": "gauge", "sensor": "cpu", "label": "CPU", "color": "accent",
            "sub": ["cpu_freq", "cpu_load"], "w": 220, "h": 300},
    "gpu": {"type": "gauge", "sensor": "gpu", "label": "GPU", "color": "magenta",
            "sub": ["gpu_power", "gpu_vram", "gpu_fan"], "w": 220, "h": 300},
    "system": {"type": "stats", "title": "SYSTEM", "w": 460, "h": 350,
               "rows": ["cpu_temp", "gpu_temp", "ram", "net", "disk_io", "disk_root"]},
    "media": {"type": "media", "w": 460, "h": 220, "bands": 28},
}

WIDGET_CUSTOM_PATH = CONFIG_PATH.parent / "widget_presets.json"


def load_custom_widgets() -> dict:
    try:
        data = json.loads(WIDGET_CUSTOM_PATH.read_text())
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_custom_widgets(customs: dict) -> None:
    WIDGET_CUSTOM_PATH.parent.mkdir(parents=True, exist_ok=True)
    WIDGET_CUSTOM_PATH.write_text(json.dumps(customs, indent=1))

DATA_KEYS = ["cpu_temp", "cpu_load", "cpu_freq", "gpu_temp", "gpu_power",
             "gpu_vram", "gpu_fan", "ram", "net", "net_up", "net_down",
             "disk_io", "disk_root", "uptime", "load1"]


def data_key_choices() -> list[str]:
    """Preset keys + empty spacer + one entry per mounted volume (disk:/Volumes/X)."""
    return [*DATA_KEYS, "empty"] + [f"disk:{mp}" for mp in mounted_volumes()]


def data_key_name(key: str) -> str:
    """Friendly name for placeholder / display."""
    if key in ("empty", "sep", "---"):
        return "Empty"
    if key.startswith("disk:"):
        return volume_label(key[5:])
    return DATA_LABELS.get(key, key)


def _entry_key(entry):
    return entry.get("key") if isinstance(entry, dict) else entry


def _entry_label(entry) -> str:
    return (entry.get("label") or "").strip() if isinstance(entry, dict) else ""


def _norm_line(entry, key=None, label=None, scale=None) -> dict:
    """Normalize a line to {key, label?, scale?}; empty label stays label-free."""
    k = key if key is not None else _entry_key(entry)
    lbl = _entry_label(entry) if label is None else label
    cur_scale = entry.get("scale") if isinstance(entry, dict) else None
    if scale is not None:
        cur_scale = scale
    d = {"key": k}
    if lbl:
        d["label"] = lbl
    if cur_scale is not None and float(cur_scale) != 1.0:
        d["scale"] = round(float(cur_scale), 2)
    return d


class WidgetsPanel(Gtk.Box):
    """Widgets section: template picker, custom presets, the widget list."""

    BUILTINS = (("Clock", "clock"), ("CPU gauge", "cpu"), ("GPU gauge", "gpu"),
                ("Stats", "system"), ("Media", "media"))

    def __init__(self) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self._customs = load_custom_widgets()
        self._tpl_kind = "builtin"
        self._tpl_key = "clock"
        self._tpl_label = "Clock"

        self.btn_template = Gtk.MenuButton(hexpand=True)
        self._rebuild_template_menu()
        self.btn_add = Gtk.Button(label="Add Widget",
                                  css_classes=["suggested-action"])
        self.btn_add.connect("clicked", self._on_add_widget)
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        row.append(self.btn_template)
        row.append(self.btn_add)
        self.append(row)

        row2 = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self.preset_entry = Gtk.Entry(hexpand=True)
        self.preset_entry.set_placeholder_text("new widget preset name…")
        btn_preset = Gtk.Button(label="Add preset")
        btn_preset.connect("clicked", self._on_add_preset)
        row2.append(self.preset_entry)
        row2.append(btn_preset)
        self.append(row2)

        self.hint = Gtk.Label(label="", halign=Gtk.Align.START, wrap=True,
                              css_classes=["dim-label"])
        self.append(self.hint)

        self.append(Gtk.Label(label="on this panel", halign=Gtk.Align.START,
                              css_classes=["omarchy-muted"]))
        self.wlist = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        scroll = Gtk.ScrolledWindow(vexpand=True)
        scroll.set_child(self.wlist)
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.append(scroll)

        self.refresh()

    # -- template dropdown (MenuButton + popover: pills + separator) --------

    def _template_items(self):
        items = [(label, "builtin", key) for label, key in self.BUILTINS]
        items.append((None, "sep", None))
        items.append(("Blank Widget", "blank", None))
        for name in sorted(self._customs):
            items.append((name, "custom", name))
        return items

    def _rebuild_template_menu(self) -> None:
        pop = Gtk.Popover()
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        for label, kind, key in self._template_items():
            if kind == "sep":
                box.append(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL))
                continue
            row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
            lab = Gtk.Label(label=label, halign=Gtk.Align.START, hexpand=True)
            row.append(lab)
            if kind == "custom":
                pill = Gtk.Label(label="custom")
                pill.set_css_classes(["aio-pill"])
                row.append(pill)
            btn = Gtk.Button()
            btn.set_child(row)
            btn.add_css_class("flat")
            btn.connect("clicked", self._on_template_picked, kind, key, label)
            box.append(btn)
        pop.set_child(box)
        self.btn_template.set_popover(pop)
        self._set_menu_button_label()

    def _set_menu_button_label(self) -> None:
        self.btn_template.set_child(
            Gtk.Label(label=self._tpl_label, halign=Gtk.Align.START))

    def _on_template_picked(self, btn, kind, key, label) -> None:
        self._tpl_kind, self._tpl_key, self._tpl_label = kind, key, label
        self._set_menu_button_label()
        if self.btn_template.get_popover() is not None:
            self.btn_template.get_popover().popdown()

    def _current_template(self) -> dict:
        if self._tpl_kind == "builtin":
            return copy.deepcopy(WIDGET_PRESETS.get(self._tpl_key, {}))
        if self._tpl_kind == "custom":
            return copy.deepcopy(self._customs.get(self._tpl_key, {}))
        return {"type": "stats", "title": "", "rows": []}   # blank

    # -- adding --------------------------------------------------------------

    def _on_add_widget(self, btn) -> None:
        w = self._current_template()
        if not w:
            return
        n = len(STORE.widgets)
        w["x"] = 20 + (n % 6) * 25
        w["y"] = 20 + (n % 6) * 25
        w.setdefault("w", 220)
        w.setdefault("h", 200)
        STORE.widgets.append(w)
        STORE.selected = len(STORE.widgets) - 1
        STORE.dirty = True
        self.refresh()
        request_render()
        win = _window_ref[0]
        if win:
            win.props_panel.refresh()
            win.canvas.queue_draw()

    def _on_add_preset(self, btn) -> None:
        name = self.preset_entry.get_text().strip()
        if not name:
            self.hint.set_text("give the widget preset a name first")
            return
        if not (0 <= STORE.selected < len(STORE.widgets)):
            self.hint.set_text("select a widget first (canvas or list) — "
                               "it gets saved as the preset")
            return
        safe = re.sub(r"[^A-Za-z0-9_-]+", "_", name).strip("_") or "preset"
        self._customs[safe] = copy.deepcopy(STORE.widgets[STORE.selected])
        save_custom_widgets(self._customs)
        self.preset_entry.set_text("")
        self.hint.set_text(f"widget preset {safe!r} saved")
        self._rebuild_template_menu()

    # -- widget list ---------------------------------------------------------

    def refresh(self) -> None:
        child = self.wlist.get_first_child()
        while child is not None:
            nxt = child.get_next_sibling()
            self.wlist.remove(child)
            child = nxt
        for i, w in enumerate(STORE.widgets):
            self.wlist.append(self._build_row(i, w))
        if not STORE.widgets:
            self.wlist.append(Gtk.Label(
                label="(no widgets yet — add one above)",
                halign=Gtk.Align.START, css_classes=["dim-label"]))

    def _build_row(self, i: int, w: dict) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        sel = i == STORE.selected

        head = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        name = w.get("title") or w.get("label") or w.get("type", "?")
        lab = Gtk.Label(label=f"{i + 1}·{name}", halign=Gtk.Align.START,
                        hexpand=True)
        lab.set_tooltip_text(w.get("type", "?"))
        if sel:
            lab.set_css_classes(["aio-sel"])
        head.append(lab)
        edit = Gtk.Button(label="✎", css_classes=["flat"])
        edit.set_tooltip_text("edit this widget (lines, title, colors…)")
        edit.connect("clicked", self._on_edit, i)
        head.append(edit)
        rem = Gtk.Button(label="✕", css_classes=["flat", "destructive-action"])
        rem.set_tooltip_text("remove this widget")
        rem.connect("clicked", self._on_remove, i)
        head.append(rem)
        box.append(head)

        grid = Gtk.Grid(column_spacing=4)
        for col, (nm, upper) in enumerate((("x", 2000), ("y", 2000),
                                           ("w", 2000), ("h", 2000))):
            cell = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
            cl = Gtk.Label(label=nm, halign=Gtk.Align.START,
                           css_classes=["omarchy-small"])
            cell.append(cl)
            adj = Gtk.Adjustment(value=float(w.get(nm, 0)), lower=0,
                                 upper=upper, step_increment=1,
                                 page_increment=10, page_size=0)
            sp = Gtk.SpinButton(adjustment=adj, digits=0)
            sp.set_size_request(56, -1)
            sp.connect("value-changed", self._on_num, nm, i)
            cell.append(sp)
            grid.attach(cell, col, 0, 1, 1)
        box.append(grid)
        return box

    def _on_num(self, sp, name, i) -> None:
        if 0 <= i < len(STORE.widgets):
            STORE.widgets[i][name] = int(sp.get_value())
            STORE.dirty = True
            win = _window_ref[0]
            if win:
                win.canvas.queue_draw()

    def _on_edit(self, btn, i) -> None:
        STORE.selected = i
        win = _window_ref[0]
        if win:
            win._show_view("widget")
            win.props_panel.refresh()
            win.left_panel.refresh()
            win.canvas.queue_draw()

    def _on_remove(self, btn, i) -> None:
        if 0 <= i < len(STORE.widgets):
            del STORE.widgets[i]
            STORE.selected = min(STORE.selected, len(STORE.widgets) - 1)
            STORE.dirty = True
            self.refresh()
            request_render()
            win = _window_ref[0]
            if win:
                win.props_panel.refresh()
                win.canvas.queue_draw()


# --------------------------------------------------------------------------
# panel templates (Panel editor → "Create panel")

ASPECTS = (("960 × 540 · landscape", 960, 540),
           ("1280 × 480 · ultrawide", 1280, 480),
           ("1024 × 600", 1024, 600),
           ("800 × 480", 800, 480),
           ("720 × 720 · square", 720, 720),
           ("480 × 480 · square", 480, 480),
           ("320 × 320 · square", 320, 320),
           ("540 × 960 · portrait", 540, 960),
           ("240 × 240 · square", 240, 240))


class EditorWindow(Adw.ApplicationWindow):
    def __init__(self, app) -> None:
        super().__init__(application=app, title="omAIOpanel editor")
        self.set_default_size(1180, 760)

        header = Gtk.HeaderBar()
        header.set_title_widget(Gtk.Label(label="omAIOpanel editor",
                                          css_classes=["omarchy-muted"]))

        # -- tab 1: Panel editor — options left, live preview right --------
        self.canvas = PanelCanvas()
        preview_live = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        preview_live.set_margin_top(10)
        preview_live.set_margin_bottom(10)
        preview_live.set_margin_start(6)
        preview_live.set_margin_end(10)
        plab = Gtk.Label(label="preview", halign=Gtk.Align.START)
        plab.set_css_classes(["omarchy-muted"])
        preview_live.append(plab)
        preview_live.append(self.canvas)     # expands; the draw always fits
        self.preview_live = preview_live
        self.preview_presets = self._build_preset_grid_view()
        self.preview_presets.set_visible(False)

        # options column: main menu <-> template picker <-> preset manager
        self.side_main = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)

        btn_presets = Gtk.Button(label="Panel presets")
        btn_presets.connect("clicked", self._on_panel_presets)
        self.side_main.append(btn_presets)

        btn_template = Gtk.Button(label="Create panel")
        btn_template.connect("clicked", self._on_create_panel)
        self.side_main.append(btn_template)

        # layout variants — hidden for square templates
        self.layout_section = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self.layout_section.append(_section_header("layout"))
        vlink = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        vlink.add_css_class("linked")
        self.variant_btns: dict[str, Gtk.ToggleButton] = {}
        for key in ("portrait", "landscape"):
            b = Gtk.ToggleButton(label=key)
            b.connect("toggled", self._on_variant_toggled, key)
            self.variant_btns[key] = b
            vlink.append(b)
        self.layout_section.append(vlink)
        self.side_main.append(self.layout_section)

        self.side_main.append(_section_header("widgets"))
        self.left_panel = WidgetsPanel()
        self.side_main.append(self.left_panel)

        self.side_widget = WidgetSettingsView()
        self.props_panel = self.side_widget   # legacy alias: refresh() rebinds
        self.side_templates = self._build_template_picker()
        self.side_presets = self._build_preset_panel()
        self.side_templates.set_visible(False)
        self.side_presets.set_visible(False)
        self.side_widget.set_visible(False)

        side_stack = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        side_stack.append(self.side_main)
        side_stack.append(self.side_templates)
        side_stack.append(self.side_presets)
        side_stack.append(self.side_widget)
        side_scroll = Gtk.ScrolledWindow(vexpand=True)
        side_scroll.set_child(side_stack)
        side_scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)

        # Apply + rotate pinned at the bottom of the options column
        apply_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        apply_row.set_margin_top(6)
        apply_row.set_margin_bottom(10)
        apply_row.set_margin_start(10)
        apply_row.set_margin_end(10)
        self.btn_apply = Gtk.Button(label="Apply → panel",
                                    css_classes=["suggested-action"],
                                    hexpand=True)
        self.btn_apply.connect("clicked", self._on_apply)
        apply_row.append(self.btn_apply)
        btn_rotate = Gtk.Button()
        rbox = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        rbox.append(Gtk.Image.new_from_icon_name("object-rotate-right-symbolic"))
        self.rotate_label = Gtk.Label(label="0°")
        rbox.append(self.rotate_label)
        btn_rotate.set_child(rbox)
        btn_rotate.set_tooltip_text(
            "rotate 90° — 0° portrait · 90° landscape · 180° portrait down · −90° landscape")
        btn_rotate.connect("clicked", self._on_rotate)
        apply_row.append(btn_rotate)
        self.apply_status = Gtk.Label(label="", wrap=True,
                                      css_classes=["dim-label"])
        self.apply_status.set_margin_start(10)
        self.apply_status.set_margin_end(10)
        self.apply_status.set_margin_bottom(10)

        side_col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        side_col.set_size_request(300, -1)
        side_col.append(side_scroll)
        side_col.append(apply_row)
        side_col.append(self.apply_status)

        editor_paned = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL, position=320)
        editor_paned.set_start_child(side_col)
        editor_paned.set_shrink_start_child(False)
        preview_stack = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        preview_stack.append(preview_live)
        preview_stack.append(self.preview_presets)
        editor_paned.set_end_child(preview_stack)
        editor_paned.set_shrink_end_child(False)
        editor_paned.set_vexpand(True)

        # -- tab 2: Global settings -----------------------------------------
        global_col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        global_col.set_margin_top(12)
        global_col.set_margin_bottom(12)
        global_col.set_margin_start(12)
        global_col.set_margin_end(12)
        for title, widget in (
                ("theme sync", self._build_theme_sync()),
                ("panel style", StylePanel()),
                ("fonts", FontsPanel())):
            global_col.append(_section_header(title))
            global_col.append(widget)
        global_scroll = Gtk.ScrolledWindow(vexpand=True)
        global_scroll.set_child(global_col)
        global_scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)

        notebook = Gtk.Notebook(vexpand=True)
        notebook.append_page(editor_paned, Gtk.Label(label="Panel editor"))
        notebook.append_page(global_scroll, Gtk.Label(label="Global settings"))
        if os.environ.get("AIO_START_TAB"):     # debug: open on a given tab
            try:
                notebook.set_current_page(int(os.environ["AIO_START_TAB"]))
            except ValueError:
                pass

        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        root.append(header)
        root.append(notebook)
        self.set_content(root)

        self._sync_variant_buttons()
        self._restore_last_session()

        if os.environ.get("AIO_START_VIEW") in ("templates", "presets"):
            self._show_view(os.environ["AIO_START_VIEW"])   # debug: start view
        if os.environ.get("AIO_START_TEMPLATE"):            # debug: apply template
            try:
                tw, th = (int(x) for x in os.environ["AIO_START_TEMPLATE"].split("x"))
                self._apply_template(tw, th)
            except ValueError:
                traceback.print_exc()
        if os.environ.get("AIO_START_WIDGET"):              # debug: edit widget N
            try:
                STORE.selected = int(os.environ["AIO_START_WIDGET"]) - 1
                self._show_view("widget")
            except ValueError:
                traceback.print_exc()
        self.props_panel.refresh()
        request_render()

    def _restore_last_session(self) -> None:
        """Re-open the last-used preset in the variant that was active — the
        portrait/landscape toggle, preview and wire dial all follow it."""
        sess = _load_session()
        preset = sess.get("preset")
        if not preset:
            return
        variant = sess.get("variant")
        if preset not in list_presets():
            _save_session(None, None)     # gone on disk — stop trying to restore
            return
        try:
            data = load_preset(preset)
        except Exception:
            return
        if not data:
            return
        if variant not in data:           # fall back to a variant that is stored
            variant = next(iter(data), None)
        if variant is None:
            return
        cur = STORE.variant
        other = "landscape" if cur == "portrait" else "portrait"
        STORE.stash_current()
        if variant not in data and other in data:
            data[variant] = rotated_variant(data[other])
        STORE.variants.update(data)
        STORE.frame = None
        STORE.apply_variant(data[variant])
        STORE.variant = variant
        STORE.preset_origin = preset
        STORE.selected = 0 if STORE.widgets else -1
        self._sync_preset_buttons()
        self._sync_variant_buttons()
        self.left_panel.refresh()
        self.props_panel.refresh()
        self.canvas.queue_draw()
        self.apply_status.set_text(f"restored {preset!r} · {variant} — Apply to push")
        request_render()

    def on_selection_changed(self) -> None:
        self.left_panel.refresh()
        self.props_panel.refresh()

    def _build_theme_sync(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        row.append(Gtk.Label(label="follow omarchy theme (colors + wallpaper)",
                             halign=Gtk.Align.START, hexpand=True))
        self.sw_sync = Gtk.Switch(active=bool(STORE.cfg.theme_cfg.get("sync", True)))
        self.sw_sync.connect("notify::active", self._on_theme_sync)
        row.append(self.sw_sync)
        box.append(row)
        box.append(Gtk.Label(
            label="off = freeze the current palette, font and wallpaper into "
                  "the config — Apply persists the freeze",
            halign=Gtk.Align.START, wrap=True, css_classes=["dim-label"]))
        return box

    def _on_theme_sync(self, sw, _pspec) -> None:
        on = sw.get_active()
        th = STORE.cfg.data.setdefault("theme", {})
        wp = STORE.cfg.data.setdefault("wallpaper", {})
        if on:
            for k in [k for k in th if k != "sync"]:
                th.pop(k, None)
            wp["mode"], wp["path"] = "theme", ""
        else:
            th["sync"] = False
            for k, v in STORE.theme.raw.items():
                th[k] = v                  # frozen hexes win over colors.toml
            STORE.cfg.data.setdefault("fonts", {})["family"] = STORE.theme.font_family
            if STORE.theme.wallpaper is not None:
                wp["mode"], wp["path"] = "path", str(STORE.theme.wallpaper)
        try:
            STORE.theme.load({
                "font_family": STORE.cfg.fonts.get("family"),
                "wallpaper_path": wp.get("path") if wp.get("mode") == "path" else None,
                "overrides": STORE.cfg.theme_overrides(),
            })
        except FileNotFoundError:
            pass
        STORE.bg = None
        self._thumb_cache.clear()
        self._thumb_bg.clear()
        STORE.dirty = True
        request_render()
        self.apply_status.set_text(
            "theme sync " + ("on" if on else "off (frozen) — Apply to persist"))

    def _on_variant_toggled(self, btn, tgt: str) -> None:
        if getattr(self, "_syncing", False):
            return          # programmatic sync — no cascade, no switching
        if not btn.get_active():
            if not any(b.get_active() for b in self.variant_btns.values()):
                btn.set_active(True)      # a group always has one position
            return
        if STORE.variant == tgt:
            return
        self._switch_variant(tgt)

    def _current_angle(self) -> int:
        """User-facing panel angle derived from the raw wire dial."""
        dial = int(STORE.cfg.data.get("orientation", 90)) % 360
        angle = (dial - 90) % 360
        return (round(angle / 90) * 90) % 360     # snap hand-edited dials

    @staticmethod
    def _angle_name(angle: int) -> str:
        return {0: "0°", 90: "90°", 180: "180°", 270: "−90°"}[angle]

    def _on_rotate(self, btn) -> None:
        angle = (self._current_angle() + 90) % 360
        want_portrait = angle in (0, 180)
        if want_portrait != (STORE.H > STORE.W):
            self._switch_variant("portrait" if want_portrait else "landscape")
        STORE.cfg.data["orientation"] = (angle + 90) % 360
        STORE.dirty = True
        self.canvas.queue_draw()
        self._sync_variant_buttons()
        self.apply_status.set_text(
            f"orientation {self._angle_name(angle)} — Apply to push")

    def _switch_variant(self, tgt: str) -> None:
        """Independent layout per orientation: stash current, apply/seed other."""
        STORE.stash_current()
        STORE.frame = None               # stale old-shape frame draws misplaced
        stash = STORE.variants.get(tgt)
        if stash is None:
            seed = rotated_variant(snapshot_variant())   # first visit: rotate
            STORE.widgets = seed["widgets"]
            STORE.cfg.data["width"], STORE.cfg.data["height"] = seed["width"], seed["height"]
            STORE.cfg.data["orientation"] = seed["orientation"]
            STORE.bg = None
            STORE.dirty = True
            request_render()
        else:
            STORE.apply_variant(stash)
        STORE.cfg.data["orientation"] = 90 if STORE.H > STORE.W else 180
        STORE.variant = tgt
        self.left_panel.refresh()
        self.props_panel.refresh()
        self.canvas.queue_draw()
        self._sync_variant_buttons()
        _save_session_variant(tgt)
        self.apply_status.set_text(f"{tgt} layout — Apply to push")

    def _sync_variant_buttons(self) -> None:
        self._syncing = True
        try:
            self.layout_section.set_visible(STORE.W != STORE.H)
            self.rotate_label.set_text(self._angle_name(self._current_angle()))
            for k, b in self.variant_btns.items():
                want = (k == STORE.variant)
                if b.get_active() != want:
                    b.set_active(want)
        finally:
            self._syncing = False

    # -- panel editor side panel: main menu <-> template picker ------------

    def _build_template_picker(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        back = Gtk.Button.new_from_icon_name("go-previous-symbolic")
        back.set_tooltip_text("back to the editor menu")
        back.connect("clicked", lambda b: self._show_main_menu())
        row.append(back)
        t = Gtk.Label(label="panel templates", halign=Gtk.Align.START)
        t.set_css_classes(["aio-section"])
        row.append(t)
        box.append(row)
        box.append(Gtk.Label(
            label="create a blank panel at the chosen aspect — the layout is "
                  "cleared; save a preset first to keep it",
            halign=Gtk.Align.START, wrap=True, css_classes=["dim-label"]))
        self.template_list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.SINGLE)
        self.template_list.set_activate_on_single_click(True)
        for name, w, h in ASPECTS:
            r = Gtk.ListBoxRow()
            r.template_dims = (w, h)
            cell = Gtk.Label(label=name, halign=Gtk.Align.START)
            cell.set_margin_top(4)
            cell.set_margin_bottom(4)
            cell.set_margin_start(6)
            cell.set_css_classes(["omarchy-mono"])
            r.set_child(cell)
            self.template_list.append(r)
        self.template_list.connect("row-activated", self._on_template_activated)
        scroll = Gtk.ScrolledWindow(vexpand=True)
        scroll.set_child(self.template_list)
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        box.append(scroll)
        return box

    def _show_view(self, view: str) -> None:
        """main | templates | presets | widget — side panel and preview swap."""
        self.side_main.set_visible(view == "main")
        self.side_templates.set_visible(view == "templates")
        self.side_presets.set_visible(view == "presets")
        self.side_widget.set_visible(view == "widget")
        self.preview_live.set_visible(view != "presets")
        self.preview_presets.set_visible(view == "presets")
        if view == "presets":
            self._refresh_preset_grid()

    def _on_create_panel(self, btn) -> None:
        self._show_view("templates")

    def _on_panel_presets(self, btn) -> None:
        self._show_view("presets")

    def _show_main_menu(self) -> None:
        self._show_view("main")

    def _on_template_activated(self, listbox, row) -> None:
        w, h = row.template_dims
        self._apply_template(w, h)

    def _apply_template(self, w: int, h: int) -> None:
        STORE.variants[STORE.variant] = snapshot_variant()   # session stash
        STORE.widgets = []
        STORE.cfg.data["width"], STORE.cfg.data["height"] = w, h
        STORE.cfg.data["orientation"] = 90 if h > w else 180
        STORE.variant = "portrait" if h > w else "landscape"
        STORE.variants[STORE.variant] = None
        STORE.selected = -1
        STORE.bg = None
        STORE.frame = None               # stale old-shape frame draws misplaced
        STORE.dirty = True
        STORE.preset_origin = None
        self._show_main_menu()
        self.left_panel.refresh()
        self.props_panel.refresh()
        self.canvas.queue_draw()
        self._sync_variant_buttons()
        _save_session(None, None)          # blank panel: stop auto-restoring a preset
        self.apply_status.set_text(f"blank {w}×{h} panel — Apply to push")
        request_render()

    # -- preset manager (Panel presets) --------------------------------------

    def _build_preset_panel(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        back = Gtk.Button.new_from_icon_name("go-previous-symbolic")
        back.set_tooltip_text("back to the editor menu")
        back.connect("clicked", lambda b: self._show_view("main"))
        row.append(back)
        t = Gtk.Label(label="panel presets", halign=Gtk.Align.START)
        t.set_css_classes(["aio-section"])
        row.append(t)
        box.append(row)

        self.btn_preset_save = Gtk.Button(label="Save new preset")
        self.btn_preset_save.connect("clicked", self._on_preset_save_reveal)
        box.append(self.btn_preset_save)

        self.preset_save_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL,
                                       spacing=6)
        self.preset_save_row.set_visible(False)
        self.preset_name_entry = Gtk.Entry(hexpand=True)
        self.preset_name_entry.set_placeholder_text("preset name…")
        self.preset_name_entry.connect("activate", self._on_preset_save_confirm)
        btn_ok = Gtk.Button(label="Save", css_classes=["suggested-action"])
        btn_ok.connect("clicked", self._on_preset_save_confirm)
        self.preset_save_row.append(self.preset_name_entry)
        self.preset_save_row.append(btn_ok)
        box.append(self.preset_save_row)

        self.btn_preset_update = Gtk.Button(label="Update preset")
        self.btn_preset_update.connect("clicked", self._on_preset_update)
        box.append(self.btn_preset_update)

        self.btn_preset_load = Gtk.Button(label="Load selected")
        self.btn_preset_load.connect("clicked", self._on_preset_load)
        box.append(self.btn_preset_load)

        self.preset_hint = Gtk.Label(label="", halign=Gtk.Align.START, wrap=True,
                                     css_classes=["dim-label"])
        box.append(self.preset_hint)
        self._sync_preset_buttons()
        return box

    def _sync_preset_buttons(self) -> None:
        origin = STORE.preset_origin
        self.btn_preset_update.set_sensitive(origin is not None)
        self.btn_preset_update.set_tooltip_text(
            f"overwrite preset {origin!r}" if origin else
            "load a preset first — Update overwrites the one you loaded")

    def _on_preset_save_reveal(self, btn) -> None:
        self.preset_save_row.set_visible(not self.preset_save_row.get_visible())
        if self.preset_save_row.get_visible():
            self.preset_name_entry.grab_focus()

    def _on_preset_save_confirm(self, *args) -> None:
        name = self.preset_name_entry.get_text().strip()
        if not name:
            self.preset_hint.set_text("give the preset a name first")
            return
        try:
            save_preset(name, variants_to_save())
        except Exception as e:
            self.preset_hint.set_text(f"save failed: {e}")
            return
        STORE.preset_origin = name
        _save_session(name, STORE.variant)
        self._thumb_cache.pop(name, None)
        self.preset_name_entry.set_text("")
        self.preset_save_row.set_visible(False)
        self._sync_preset_buttons()
        self._refresh_preset_grid()
        self.apply_status.set_text(f"preset {name!r} saved (both orientations)")

    def _on_preset_update(self, btn) -> None:
        name = STORE.preset_origin
        if not name:
            return
        try:
            save_preset(name, variants_to_save())
        except Exception as e:
            self.preset_hint.set_text(f"update failed: {e}")
            return
        self._thumb_cache.pop(name, None)
        self._refresh_preset_grid()
        _save_session(name, STORE.variant)
        self.apply_status.set_text(f"preset {name!r} updated")

    def _on_preset_load(self, btn) -> None:
        name = self._selected_preset_name()
        if not name:
            self.preset_hint.set_text("select a preset thumbnail first")
            return
        try:
            data = load_preset(name)
        except Exception as e:
            self.preset_hint.set_text(f"load failed: {e}")
            return
        cur = STORE.variant
        other = "landscape" if cur == "portrait" else "portrait"
        STORE.stash_current()          # keep this orientation's unsaved edits
        if cur not in data and other in data:
            data[cur] = rotated_variant(data[other])
        STORE.variants.update(data)
        STORE.apply_variant(data[cur])
        STORE.preset_origin = name
        self._sync_preset_buttons()
        self._sync_variant_buttons()
        self.left_panel.refresh()
        self.props_panel.refresh()
        self.canvas.queue_draw()
        self._show_view("main")            # back to editor + live preview
        _save_session(name, STORE.variant)
        self.apply_status.set_text(f"preset {name!r} loaded — Apply to push")

    def _selected_preset_name(self) -> str | None:
        children = self.preset_flow.get_selected_children()
        if not children:
            return None
        return getattr(children[0].get_child(), "preset_name", None)

    # -- preset thumbnails (rendered with the real pipeline) -----------------

    def _build_preset_grid_view(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        box.set_margin_top(10)
        box.set_margin_bottom(10)
        box.set_margin_start(6)
        box.set_margin_end(10)
        lab = Gtk.Label(label="presets", halign=Gtk.Align.START)
        lab.set_css_classes(["omarchy-muted"])
        box.append(lab)
        self._thumb_cache: dict[str, Image.Image] = {}
        self._thumb_bg: dict[tuple[int, int], Image.Image] = {}
        scroll = Gtk.ScrolledWindow(vexpand=True)
        scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        self.preset_flow = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.SINGLE,
                                       activate_on_single_click=False)
        self.preset_flow.set_min_children_per_line(2)
        self.preset_flow.set_max_children_per_line(4)
        self.preset_flow.set_homogeneous(True)
        self.preset_flow.set_column_spacing(10)
        self.preset_flow.set_row_spacing(10)
        scroll.set_child(self.preset_flow)
        box.append(scroll)
        return box

    def _refresh_preset_grid(self) -> None:
        child = self.preset_flow.get_first_child()
        while child is not None:
            nxt = child.get_next_sibling()
            self.preset_flow.remove(child)
            child = nxt
        self._thumb_bg.clear()          # backgrounds follow theme + wallpaper
        if STORE.hub is None:           # startup race: worker may not be up yet
            STORE.hub = SensorHub()
        try:
            sample = STORE.hub.sample()
        except Exception:
            traceback.print_exc()
            sample = {}
        names = list_presets()
        for name in names:
            self.preset_flow.append(self._build_preset_tile(name, sample))
        if not names:
            self.preset_flow.append(Gtk.Label(
                label="(no presets yet — Save new preset)",
                halign=Gtk.Align.START, css_classes=["dim-label"]))
        self._sync_preset_buttons()

    def _build_preset_tile(self, name: str, sample: dict) -> Gtk.Widget:
        tile = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        tile.preset_name = name
        overlay = Gtk.Overlay()
        frame = self._thumb_cache.get(name)
        if frame is None:
            try:
                frame = self._render_preset_thumb(name, sample)
                self._thumb_cache[name] = frame
            except Exception:
                traceback.print_exc()
        if frame is not None:
            pic = Gtk.Picture()
            pic.set_size_request(105, 62)
            pic.set_content_fit(Gtk.ContentFit.CONTAIN)
            buf = io.BytesIO()
            frame.save(buf, "JPEG", quality=80)
            tex = Gdk.Texture.new_from_bytes(GLib.Bytes.new(buf.getvalue()))
            pic.set_paintable(tex)
            overlay.set_child(pic)
        else:
            overlay.set_child(Gtk.Label(label="render failed",
                                        css_classes=["dim-label"]))
        x = Gtk.Button(label="✕", css_classes=["destructive-action"])
        x.set_halign(Gtk.Align.END)
        x.set_valign(Gtk.Align.START)
        x.set_margin_top(2)
        x.set_margin_end(2)
        x.connect("clicked", self._on_preset_delete, name)
        overlay.add_overlay(x)
        tile.append(overlay)
        cap = Gtk.Label(label=name, halign=Gtk.Align.START)
        cap.set_ellipsize(Pango.EllipsizeMode.END)
        cap.set_css_classes(["omarchy-muted"])
        tile.append(cap)
        return tile

    def _render_preset_thumb(self, name: str, sample: dict) -> Image.Image:
        data = load_preset(name)
        want = "landscape" if STORE.W >= STORE.H else "portrait"
        v = data.get(want) or next(iter(data.values()))
        w, h = int(v["width"]), int(v["height"])
        bg = self._thumb_bg.get((w, h))
        if bg is None:
            bg = render_background(STORE.theme, STORE.cfg.wallpaper, w, h, 1)
            self._thumb_bg[(w, h)] = bg
        return render_frame(STORE.theme, bg, v["widgets"], sample, w, h, 1,
                            fonts=STORE.cfg.fonts, cards=STORE.cfg.cards)

    def _on_preset_delete(self, btn, name: str) -> None:
        dlg = Adw.MessageDialog.new(self, "Delete preset?",
                                    f"{name!r} will be removed from disk.")
        dlg.add_response("cancel", "Cancel")
        dlg.add_response("delete", "Delete")
        dlg.set_response_appearance("delete", Adw.ResponseAppearance.DESTRUCTIVE)
        dlg.connect("response",
                    lambda d, r: self._preset_delete_confirmed(r, name))
        dlg.present()

    def _preset_delete_confirmed(self, response: str, name: str) -> None:
        if response != "delete":
            return
        (PRESETS_DIR / f"{name}.toml").unlink(missing_ok=True)
        self._thumb_cache.pop(name, None)
        if STORE.preset_origin == name:
            STORE.preset_origin = None
            self._sync_preset_buttons()
        if _load_session().get("preset") == name:
            _save_session(None, None)      # the auto-restore target vanished
        self._refresh_preset_grid()
        self.apply_status.set_text(f"preset {name!r} deleted")

    def _on_apply(self, btn) -> None:
        try:
            apply_config(STORE.widgets, STORE.W, STORE.H, STORE.cfg.data)
            self.apply_status.set_text(
                f"applied {time.strftime('%H:%M:%S')} — daemon hot-reloads")
            STORE.dirty = False
        except Exception as e:
            self.apply_status.set_text(f"apply failed: {e}")


def _style_section_label(label: Gtk.Label) -> None:
    label.set_css_classes(["aio-section"])


def _section_header(title: str) -> Gtk.Widget:
    """omasettings-style group header: subtitle text over a hairline."""
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
    lab = Gtk.Label(label=title, halign=Gtk.Align.START)
    lab.set_css_classes(["aio-section"])
    box.append(lab)
    sep = Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL)
    sep.set_css_classes(["aio-hairline"])
    box.append(sep)
    return box


def _canvas_bg(ctx) -> None:
    """Paint the canvas/preview backdrop in the theme's background tone."""
    try:
        c = STORE.theme.color("background")
        ctx.set_source_rgb(c[0] / 255, c[1] / 255, c[2] / 255)
    except Exception:
        ctx.set_source_rgb(0.06, 0.05, 0.045)


# --------------------------------------------------------------------------
# CSS (editor-specific classes on top of aio.gtk_theme's palette mapping)

EDITOR_CSS = b"""
label.dim-label { opacity: 1; color: alpha(@window_fg_color, 0.66); }
.aio-section { font-size: 10pt; }
.aio-hairline { min-height: 1px; background-color: alpha(@window_fg_color, 0.22); }
.aio-pill { background: alpha(@accent_color, 0.16); border-radius: 999px;
            padding: 1px 8px; font-size: 7.5pt; }
.aio-sel { font-weight: bold; }
"""


def _load_editor_css() -> None:
    provider = Gtk.CssProvider()
    provider.load_from_data(EDITOR_CSS, len(EDITOR_CSS))
    Gtk.StyleContext.add_provider_for_display(
        Gdk.Display.get_default(), provider,
        Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION + 11)


# --------------------------------------------------------------------------

class EditorApp(Adw.Application):
    def __init__(self) -> None:
        super().__init__(application_id=APP_ID)
        self._tick_id = 0

    def do_activate(self):
        try:
            Adw.StyleManager.get_default().set_color_scheme(
                Adw.ColorScheme.PREFER_DARK)
            win = EditorWindow(self)
            self._gtk_theme = GtkOmarchyTheme.apply(win)   # palette + fonts + live retint
            _load_editor_css()
            _window_ref[0] = win
            win.present()

            threading.Thread(target=render_worker, daemon=True).start()
            GLib.timeout_add(2000, self._theme_watch)
        except Exception:
            traceback.print_exc()
            self.quit()

    def _theme_watch(self) -> bool:
        """Follow Omarchy theme changes like the daemon does."""
        try:
            if not STORE.cfg.theme_cfg.get("sync", True):
                return True              # theme sync off: frozen, follow nothing
            fp = STORE.theme.fingerprint({
                "wallpaper_path": STORE.cfg.wallpaper.get("path")
                if STORE.cfg.wallpaper.get("mode") == "path" else None,
            })
            if fp != getattr(STORE, "_fp", None):
                STORE._fp = fp
                STORE.theme.load({
                    "font_family": STORE.cfg.fonts.get("family"),
                    "wallpaper_path": STORE.cfg.wallpaper.get("path")
                    if STORE.cfg.wallpaper.get("mode") == "path" else None,
                })
                STORE.bg = None
                win = _window_ref[0]
                if win is not None:
                    win._thumb_cache.clear()
                    win._thumb_bg.clear()
                request_render()
        except OSError:
            pass
        return True


def main() -> int:
    return EditorApp().run(None)


if __name__ == "__main__":
    sys.exit(main())
