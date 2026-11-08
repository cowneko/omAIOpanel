"""Read the active Omarchy theme: palette, wallpaper, fonts.

Single source of truth: ~/.local/state/omarchy/current/theme/ (a symlink that
Omarchy repoints on every `omarchy theme set`). We poll its fingerprint.
"""

from __future__ import annotations

import subprocess
import tomllib
from pathlib import Path

THEME_LINK = Path.home() / ".local" / "state" / "omarchy" / "current" / "theme"
BACKGROUND_LINK = Path.home() / ".local" / "state" / "omarchy" / "current" / "background"

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".bmp"}


_FC_CACHE: dict[tuple[str, bool], Path | None] = {}


def fc_match_file(family: str, bold: bool) -> Path | None:
    """Resolve a font family to a concrete file path via fontconfig (cached)."""
    key = (family, bool(bold))
    if key in _FC_CACHE:
        return _FC_CACHE[key]
    try:
        spec = f"{family}:style={'Bold' if bold else 'Regular'}"
        out = subprocess.run(
            ["fc-match", "--format=%{file}", spec],
            capture_output=True, text=True, timeout=5,
        ).stdout.strip()
        p = Path(out)
        result = p if out and p.exists() else None
    except Exception:
        result = None
    _FC_CACHE[key] = result
    return result


class OmarchyTheme:
    """Snapshot of the active theme. Reload when fingerprint() changes."""

    def __init__(self) -> None:
        self.dir: Path | None = None
        self.colors: dict[str, tuple[int, int, int]] = {}
        self.raw: dict[str, str] = {}
        self.mode: str = "dark"
        self.wallpaper: Path | None = None
        self.font_family: str = "JetBrains Mono NF"
        self._font_files: dict[bool, Path | None] = {}

    # ---- discovery -------------------------------------------------------

    def load(self, cfg: dict | None = None) -> None:
        cfg = cfg or {}
        self.dir = Path(cfg["theme_dir"]) if "theme_dir" in cfg else THEME_LINK
        target = self.dir.resolve()
        if not target.is_dir():
            raise FileNotFoundError(f"omarchy theme dir not found: {self.dir}")
        self.raw = {}
        colors_file = target / "colors.toml"
        if colors_file.exists():
            with open(colors_file, "rb") as f:
                data = tomllib.load(f)
            self.mode = data.get("mode", "dark")
            for k, v in data.items():
                if isinstance(v, str) and v.startswith("#"):
                    self.raw[k.lower()] = v
        # Frozen palette wins over colors.toml (theme sync off): an explicit
        # hex in [theme] overrides the live theme's value for that key.
        for k, v in (cfg.get("overrides") or {}).items():
            if isinstance(v, str) and v.startswith("#"):
                self.raw[k.lower()] = v
        self.font_family = cfg.get("font_family") or "JetBrains Mono NF"
        self._font_files = {
            False: fc_match_file(self.font_family, False),
            True: fc_match_file(self.font_family, True),
        }
        self.wallpaper = self._find_wallpaper(cfg)

    def _find_wallpaper(self, cfg: dict) -> Path | None:
        if cfg.get("wallpaper_path"):
            p = Path(cfg["wallpaper_path"]).expanduser()
            return p if p.exists() else None
        # Omarchy's authoritative pointer: `current/background` symlink
        # (repointed by `omarchy theme set` and `omarchy theme bg next`).
        try:
            if BACKGROUND_LINK.exists():
                p = BACKGROUND_LINK.resolve()
                if p.is_file():
                    return p
        except OSError:
            pass
        bgs = (self.dir.resolve()) / "backgrounds"
        if bgs.is_dir():
            for f in sorted(bgs.iterdir()):
                if f.suffix.lower() in IMAGE_EXTS:
                    return f
        return None

    def fingerprint(self, cfg: dict | None = None) -> tuple:
        """Cheap identity of everything that affects rendering."""
        cfg = cfg or {}
        try:
            target = Path(self.dir or THEME_LINK).resolve()
            if not target.is_dir():
                return ("missing", 0, "", 0)
            cols = target / "colors.toml"
            col_m = cols.stat().st_mtime_ns if cols.exists() else 0
            wp = self._find_wallpaper(cfg)
            wp_m = wp.stat().st_mtime_ns if wp else 0
            return (str(target), col_m, str(wp), wp_m)
        except OSError:
            return ("missing", 0, "", 0)

    # ---- palette ---------------------------------------------------------

    def color(self, name: str, fallback: str = "#ffffff") -> tuple[int, int, int]:
        """Named theme color ('accent', 'muted', 'bright_blue'...), a hex
        literal ('#rrggbb'), or falls back."""
        if isinstance(name, str) and name.startswith("#"):
            return _hex(name)
        v = self.raw.get(name.lower())
        if v is None:
            v = fallback if fallback.startswith("#") else self.raw.get(fallback, fallback)
        return _hex(v if isinstance(v, str) and v.startswith("#") else fallback)

    def rgba(self, name: str, alpha: int) -> tuple[int, int, int, int]:
        r, g, b = self.color(name)
        return (r, g, b, alpha)

    def font_path(self, bold: bool = False) -> Path | None:
        return self._font_files.get(bool(bold)) or self._font_files.get(False)


def _hex(s: str) -> tuple[int, int, int]:
    s = s.strip().lstrip("#")
    if len(s) == 3:
        s = "".join(c * 2 for c in s)
    return tuple(int(s[i : i + 2], 16) for i in (0, 2, 4))  # type: ignore[return-value]
