"""Configuration: ~/.config/omAIOpanel/config.toml with sane defaults."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path

CONFIG_PATH = Path.home() / ".config" / "omAIOpanel" / "config.toml"

DEFAULTS = {
    "device": "87ad:70db",
    "fps": 15.0,
    "width": 540,
    "height": 960,
    "supersample": 2,
    "overlay_watchdog_s": 30.0,
    "wallpaper": {
        "mode": "theme",       # "theme" = follow omarchy theme, "path" = fixed file
        "path": "",
        "focus_x": 0.5,        # crop focus when the wallpaper aspect != panel
        "focus_y": 0.5,
        "scrim": 120,          # 0..255 scrim strength of the darker_background
        "brightness": 1.0,     # wallpaper multiplier: 1.0 unchanged, <1 darker
    },
    "cards": {
        "rounded": True,       # rounded vs straight widget corners
        "outline": "subtle",   # none | subtle | accent
    },
    "fonts": {
        "family": "",          # empty = follow the omarchy theme font
        "title": {"color": "", "scale": 1.0, "family": "", "size": 28},
        "data": {"color": "", "scale": 1.0, "family": "", "size": 24},
    },
    "weather": {"lat": None, "lon": None},   # None = auto-detect once via IP
    # theme sync: sync = true follows the omarchy theme live. With
    # sync = false the values below are FROZEN (explicit hexes in the config
    # win over colors.toml); the editor writes them when you turn sync off.
    "theme": {
        "sync": True,
    },
    # widgets: list of dicts, see config.example.toml. An explicit empty
    # list means a BLANK panel (nothing to draw); the key being absent means
    # "use the default layout".
    "widgets": [],
}

_ROLE_DEFAULTS = {"color": "", "scale": 1.0, "family": ""}


_FONT_ROLE_DEFAULTS = {
    "title": {"color": "", "scale": 1.0, "family": "", "size": 28},
    "data": {"color": "", "scale": 1.0, "family": "", "size": 24},
}


def _deep_merge(base: dict, user: dict) -> dict:
    out = dict(base)
    for k, v in user.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


@dataclass
class Config:
    data: dict = field(default_factory=dict)

    def __getattr__(self, name):  # top-level scalar access
        try:
            return self.data[name]
        except KeyError as e:
            raise AttributeError(name) from e

    @property
    def wallpaper(self) -> dict:
        return {**DEFAULTS["wallpaper"], **self.data.get("wallpaper", {})}

    @property
    def fonts(self) -> dict:
        merged = _deep_merge(DEFAULTS["fonts"], self.data.get("fonts", {}))
        for role, role_def in _FONT_ROLE_DEFAULTS.items():
            merged[role] = _deep_merge(role_def, merged.get(role, {}))
        return merged

    @property
    def cards(self) -> dict:
        return _deep_merge(DEFAULTS["cards"], self.data.get("cards", {}))

    @property
    def weather(self) -> dict:
        return _deep_merge(DEFAULTS["weather"], self.data.get("weather", {}))

    @property
    def theme_cfg(self) -> dict:
        return _deep_merge(DEFAULTS["theme"], self.data.get("theme", {}))

    def theme_overrides(self) -> dict:
        """Frozen palette keys (name -> "#rrggbb") that win over colors.toml.

        Empty when theme sync is on, so a live theme switch still retints.
        """
        t = self.theme_cfg
        if t.get("sync", True):
            return {}
        return {k: v for k, v in t.items()
                if k != "sync" and isinstance(v, str) and v.startswith("#")}

    @property
    def widgets(self) -> list[dict]:
        # An explicitly present (possibly empty) list wins: an empty list is
        # a blank panel, not the default layout.
        return DEFAULT_WIDGET_LAYOUT if "widgets" not in self.data else self.data["widgets"]


# Default widget layout — a starting point, fully overridable via config.
DEFAULT_WIDGET_LAYOUT = [
    {"type": "clock", "x": 40, "y": 40, "w": 460, "h": 140},
    {"type": "gauge", "sensor": "cpu", "x": 40, "y": 220, "w": 220, "h": 310, "label": "CPU",
     "color": "accent", "sub": ["cpu_freq", "cpu_load"]},
    {"type": "gauge", "sensor": "gpu", "x": 280, "y": 220, "w": 220, "h": 310, "label": "GPU",
     "color": "magenta", "sub": ["gpu_power", "gpu_vram"]},
    {"type": "stats", "x": 40, "y": 570, "w": 460, "h": 350, "title": "SYSTEM",
     "rows": ["cpu_temp", "gpu_temp", "ram", "net", "disk_root"]},
]


def load_config(path: Path = CONFIG_PATH) -> Config:
    data = dict(DEFAULTS)
    if path.exists():
        with open(path, "rb") as f:
            user = tomllib.load(f)
        data = _deep_merge(data, user)
    return Config(data)
