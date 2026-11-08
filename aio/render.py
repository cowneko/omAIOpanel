"""Compose 540x960 (portrait) frames: theme wallpaper + sensor widgets.

Design language: wallpaper cover-cropped, darkened with a theme-derived scrim,
widgets drawn as translucent "glass" rounded cards with theme accents. All
geometry is authored in panel pixels; rendering happens at `supersample`x and
is downscaled with LANCZOS for crisp text.
"""

from __future__ import annotations

import time
from pathlib import Path

from PIL import Image, ImageDraw, ImageEnhance, ImageFont, ImageFilter

from .sensors import format_row, _entry_key
from .theme import fc_match_file


def _g10(v) -> int:
    """Snap to the 10px layout grid (min one cell)."""
    return max(10, int(round(v / 10.0) * 10))


def _rel_lum(c) -> float:
    def ch(v: float) -> float:
        v /= 255.0
        return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4
    r, g, b = c[:3]
    return 0.2126 * ch(r) + 0.7152 * ch(g) + 0.0722 * ch(b)


def _contrast(a, b) -> float:
    la, lb = _rel_lum(a), _rel_lum(b)
    hi, lo = max(la, lb), min(la, lb)
    return (hi + 0.05) / (lo + 0.05)


def _mix(a, b, t: float):
    return tuple(int(round(a[i] + (b[i] - a[i]) * t)) for i in range(3))


class FontBook:
    """Panel-wide typography: FIXED pixel sizes per role (title / data).

    Text size never depends on widget geometry — rescaling a widget moves
    and resizes boxes, never fonts. Roles: title → muted + size 18,
    data → foreground + size 24 by default; size/color/family overridable
    per role, and a line may override the data scale via its `scale` key.
    """

    def __init__(self, theme, fonts_cfg: dict | None, ss: int) -> None:
        self.theme = theme
        self.ss = max(1, ss)
        cfg = fonts_cfg or {}
        self.base_family = (cfg.get("family") or "").strip() or theme.font_family
        self.roles = {
            "title": cfg.get("title") or {},
            "data": cfg.get("data") or {},
        }
        self._cache: dict[tuple, ImageFont.FreeTypeFont] = {}
        self._paths: dict[tuple[str, bool], Path | None] = {}

    def _path(self, family: str, bold: bool):
        key = (family, bold)
        if key not in self._paths:
            if family and family != self.theme.font_family:
                self._paths[key] = fc_match_file(family, bold) or self.theme.font_path(bold)
            else:
                self._paths[key] = self.theme.font_path(bold)
        return self._paths[key]

    def scale(self, role: str, line_scale: float | None = None) -> float:
        try:
            own = float(self.roles.get(role, {}).get("scale", 1.0) or 1.0)
        except (TypeError, ValueError):
            own = 1.0
        return own if line_scale is None else float(line_scale)

    def px(self, role: str, line_scale: float | None = None) -> int:
        """Final panel-px size for a role, optionally overridden by a line."""
        rcfg = self.roles.get(role, {})
        try:
            size = int(rcfg.get("size") or (24 if role == "data" else 28))
        except (TypeError, ValueError):
            size = 24 if role == "data" else 28
        return max(8, int(round(size * self.scale(role, line_scale))))

    def color(self, role: str) -> tuple[int, int, int]:
        name = (self.roles.get(role, {}).get("color") or "").strip()
        if name:
            return self.theme.color(name)
        if role == "title":
            return self._contrast_title()
        return self.theme.color("foreground")

    def _contrast_title(self) -> tuple[int, int, int]:
        """Theme tint for titles/labels that actually reads on the background.

        Starts from the theme's muted color and walks it toward the theme
        foreground until it hits ~3:1 contrast against the theme background —
        so it lightens on dark backgrounds and darkens on light ones. Purely
        theme-derived (no hardcoded grays); computed once per FontBook.
        """
        if getattr(self, "_tint_title", None) is not None:
            return self._tint_title
        mu = self.theme.color("muted")
        fg = self.theme.color("foreground")
        bg = self.theme.color("background")
        tint = mu
        if _contrast(mu, bg) < 3.0:
            for t in (0.3, 0.55, 0.8, 1.0):
                cand = _mix(mu, fg, t)
                tint = cand
                if _contrast(cand, bg) >= 3.0:
                    break
        self._tint_title = tint
        return tint

    def font(self, size_px: float, role: str = "data", bold: bool = False):
        family = (self.roles.get(role, {}).get("family") or "").strip() or self.base_family
        px = max(6, int(round(size_px)) * self.ss)
        key = (px, bold, family)
        if key not in self._cache:
            path = self._path(family, bold)
            self._cache[key] = (ImageFont.truetype(str(path), px)
                                if path else ImageFont.load_default())
        return self._cache[key]


def _font_cache(theme, ss: int) -> dict:
    """Legacy single-family cache — kept for callers that don't pass fonts."""
    return FontBook(theme, None, ss)


def render_background(theme, wcfg: dict, W: int, H: int, ss: int) -> Image.Image:
    """Wallpaper cover-cropped to the panel + theme scrim. Cached between frames."""
    Wss, Hss = W * ss, H * ss
    src_path = wcfg.get("_resolved_wallpaper") or theme.wallpaper
    if src_path and Path(src_path).exists():
        with Image.open(src_path) as im:
            im = im.convert("RGB")
            # cover-crop with focus point
            scale = max(Wss / im.width, Hss / im.height)
            nw, nh = int(im.width * scale + 0.5), int(im.height * scale + 0.5)
            im = im.resize((nw, nh), Image.LANCZOS)
            fx = (nw - Wss) * float(wcfg.get("focus_x", 0.5))
            fy = (nh - Hss) * float(wcfg.get("focus_y", 0.5))
            bg = im.crop((int(fx), int(fy), int(fx) + Wss, int(fy) + Hss))
            try:
                bright = float(wcfg.get("brightness", 1.0) or 1.0)
            except (TypeError, ValueError):
                bright = 1.0
            if bright != 1.0:
                # gamma mapping instead of multiplicative brightness:
                # >1 lifts dark values while highlights compress (never clip),
                # <1 darkens shadows first. Slider stays 1.0 = neutral.
                p = 1.0 / max(0.05, bright)
                lut = [min(255, round(255 * (i / 255.0) ** p)) for i in range(256)] * 3
                bg = bg.point(lut)
    else:
        bg = Image.new("RGB", (Wss, Hss), theme.color("background"))

    scrim = int(wcfg.get("scrim", 120))
    if scrim > 0:
        dark = theme.color("darker_background") if "darker_background" in theme.raw else theme.color("background")
        grad = Image.new("L", (1, Hss))
        # stronger at top and bottom, lighter in the middle
        for y in range(Hss):
            t = y / Hss
            a = scrim * (0.55 + 0.75 * abs(t - 0.45) ** 1.4)
            grad.putpixel((0, y), min(255, int(a)))
        alpha = grad.resize((Wss, Hss))
        overlay = Image.new("RGB", (Wss, Hss), dark)
        bg = Image.composite(overlay, bg, alpha)
    return bg


def _card(draw, x, y, w, h, ss, theme, rounded=True, outline="subtle") -> None:
    bgc = theme.color("background")
    ltc = theme.color("lighter_background")
    if outline == "accent":
        edge = (*theme.color("accent"), 170)
    elif outline == "none":
        edge = None
    else:
        edge = (*ltc, 70)
    draw.rounded_rectangle(
        [x * ss, y * ss, (x + w) * ss, (y + h) * ss],
        radius=(26 * ss) if rounded else 0,
        fill=(*bgc, 150),
        outline=edge,
        width=max(1, round(1.5 * ss)) if edge else 0,
    )


def _arc_caps(draw, cx, cy, r, start_deg, end_deg, ss, color) -> None:
    import math
    for ang in (start_deg, end_deg):
        rad = math.radians(ang)
        x = (cx + r * math.cos(rad)) * ss
        y = (cy + r * math.sin(rad)) * ss
        cr = 2.5 * ss
        draw.ellipse([x - cr, y - cr, x + cr, y + cr], fill=color)


def _gauge(draw, theme, fbook, widget, s, x, y, w, h, ss) -> None:
    label = widget.get("label", widget.get("sensor", "").upper())
    color = theme.color(widget.get("color", "accent"))
    fg = fbook.color("data")
    muted = fbook.color("title")
    title_px = fbook.px("title")
    data_px = fbook.px("data")

    # content box: 10px margins; label + sub lines claim their natural text
    # height (+ leading) FIRST, the ring shrinks to whatever remains
    X0, X1 = x + 10, x + w - 10
    Y0, Y1 = y + 10, y + h - 10
    cw, ch = X1 - X0, Y1 - Y0

    subs = []
    for entry in widget.get("sub", []):
        row = format_row(entry, s) or _sub_value(_entry_key(entry), s)
        if row:
            line_scale = entry.get("scale") if isinstance(entry, dict) else None
            sub_px = fbook.px("data", line_scale)
            subs.append((row[1], sub_px, sub_px + 6))   # text px, slot (text + leading)

    label_f = fbook.font(title_px, "title", bold=True)
    label_slot = int(label_f.size / ss) + 8
    subs_block = sum(slot for _, _, slot in subs)
    gaps = 10 * (2 if subs else 1)
    ring_h = max(60, ch - label_slot - subs_block - gaps)
    ring_box = max(60, (min(cw, ring_h) // 10) * 10)   # floor: ring absorbs slack

    cx = (X0 + X1) / 2
    cy = Y0 + ring_box / 2
    r = max(30.0, ring_box / 2 - 7)
    width = max(6, round(10 * ss))
    track = (*theme.color("lighter_background"), 90)
    draw.arc([(cx - r) * ss, (cy - r) * ss, (cx + r) * ss, (cy + r) * ss],
             0, 360, fill=track, width=width)

    load = s.get(f"{widget.get('sensor')}_load")
    temp = s.get(f"{widget.get('sensor')}_temp")
    frac = (load or 0) / 100.0
    if frac > 0.005:
        start, end = -90, -90 + 360 * frac
        draw.arc([(cx - r) * ss, (cy - r) * ss, (cx + r) * ss, (cy + r) * ss],
                 start, end, fill=color, width=width)
        if frac > 0.03:
            _arc_caps(draw, cx, cy, r, start, end, ss, (*color, 255))

    # center readout sized to the ring box
    temp_s = f"{temp:.0f}" if temp else "--"
    vsize = ring_box * 0.28 / max(0.5, fbook.scale("data"))
    vf = fbook.font(vsize, "data", bold=True)
    draw.text((cx * ss, cy * ss), temp_s, font=vf, fill=fg, anchor="mm")
    if temp_s != "--":
        half = draw.textlength(temp_s, font=vf) / 2
        uf = fbook.font(vsize * 0.42, "title")
        draw.text((cx * ss + half + 3 * ss, cy * ss + vsize * 0.22 * ss),
                  "°C", font=uf, fill=muted, anchor="lm")

    # label line, then sub lines in their own slots — no overlap possible
    label_cy = Y0 + ring_box + 10 + label_slot / 2
    draw.text((cx * ss, label_cy * ss), label, font=label_f, fill=muted, anchor="mm")
    if subs:
        top = label_cy + label_slot / 2 + 10
        for i, (value_text, sub_px, slot) in enumerate(subs):
            cy_i = top + sum(s for _, _, s in subs[:i]) + slot / 2
            sf = fbook.font(sub_px, "data")
            draw.text((cx * ss, cy_i * ss), value_text, font=sf, fill=fg, anchor="mm")


def _sub_value(key: str, s: dict):
    if key == "cpu_freq" and s.get("cpu_freq"):
        return ("", f"{s['cpu_freq']:.2f} GHz")
    if key == "cpu_load":
        return ("", f"{s.get('cpu_load', 0):.0f}% load")
    if key == "gpu_power" and s.get("gpu_power") is not None:
        return ("", f"{s['gpu_power']:.0f} W")
    if key == "gpu_vram" and s.get("gpu_vram_total"):
        return ("", f"{s['gpu_vram_used']/2**30:.1f} GB VRAM")
    if key == "gpu_fan" and s.get("gpu_fan") is not None:
        return ("", f"fan {s['gpu_fan']:.0f}%")
    return None


def _clock(draw, theme, fbook, widget, s, x, y, w, h, ss) -> None:
    tcol = fbook.color("data")
    titcol = fbook.color("title")
    now = time.localtime()
    t = time.strftime(widget.get("format", "%H:%M"), now)
    d = time.strftime(widget.get("date_format", "%a %d %b"), now)
    wx = s.get("weather") or {}
    if widget.get("weather", True) and wx.get("temp") is not None and wx.get("glyph"):
        d = f"{d}   {wx['glyph']} {wx['temp']:.0f}°"

    X0, X1 = x + 10, x + w - 10
    Y0, Y1 = y + 10, y + h - 10
    ch = Y1 - Y0
    cx = (X0 + X1) / 2

    # Budget stack (time / hostname / date): role sizes stay FIXED — the time
    # is the flexible display element (like the gauge ring) and shrinks to
    # keep the whole block inside the card; block is centered vertically.
    date_px = fbook.px("title")
    host = widget.get("hostname")
    host_px = int(date_px * 0.8) if host else 0
    other = 8 + host_px + (6 if host else 0) + date_px
    time_px = max(24.0, min(fbook.px("data") * 2.5, ch - other - 20))
    tf = fbook.font(time_px, "data", bold=True)
    time_px = tf.size / ss
    block = time_px + other
    top = Y0 + max(0.0, (ch - block) / 2)
    cy_time = top + time_px / 2
    cy_date = top + time_px + other - date_px / 2
    draw.text((cx * ss, cy_time * ss), t, font=tf, fill=tcol, anchor="mm")
    df = fbook.font(date_px, "title")
    draw.text((cx * ss, cy_date * ss), d, font=df, fill=titcol, anchor="mm")
    if host:
        hf = fbook.font(host_px, "title")
        cy_host = top + time_px + 8 + host_px / 2
        draw.text((cx * ss, cy_host * ss), host, font=hf, fill=titcol, anchor="mm")


def _stats(draw, theme, fbook, widget, s, x, y, w, h, ss) -> None:
    fg = fbook.color("data")
    muted = fbook.color("title")
    ltc = theme.color("lighter_background")

    X0, X1 = x + 10, x + w - 10
    Y0, Y1 = y + 10, y + h - 10
    avail_w = (X1 - X0) * ss

    rows, pinned = [], []
    for entry in widget.get("rows", []):
        key = _entry_key(entry)
        if key in ("empty", "sep", "---"):
            rows.append(("__sep__", "", fbook.px("data")))   # blank spacer slot
            continue
        row = format_row(entry, s)
        if not row:
            continue
        line_scale = entry.get("scale") if isinstance(entry, dict) else None
        item = (row[0], row[1], fbook.px("data", line_scale))
        if key == "uptime":
            pinned.append(item)          # uptime is BOTTOM-ANCHORED (user spec)
        else:
            rows.append(item)

    import os
    dbg = os.environ.get("AIO_DEBUG_FONTS")

    # FIXED natural line height — lines never move or resize with the widget;
    # resizing only changes how many lines fit (and the gap before the pinned block).
    data_px = fbook.px("data")
    row_h = max(10, _g10(data_px + 8))
    up_block = row_h * len(pinned)
    max_rows = max(0, ((Y1 - Y0) - up_block) // row_h)
    rows = rows[:max_rows]
    n = len(rows)
    if n == 0 and not pinned:
        return

    def _put(cy, label, value, line_px):
        if label == "__sep__":
            return   # Empty line: blank slot, draws nothing
        rf = fbook.font(line_px, "data")
        while rf.size / ss > row_h * 0.62 and (
            draw.textlength(value, font=rf) + draw.textlength(label.upper() + "  ", font=rf) > avail_w
        ):
            rf = fbook.font(rf.size / ss - 1, "data")
        if dbg:
            print(f"[stats] cy={cy:.0f} px={rf.size / ss:.0f} label={label!r}")
        draw.text((X0 * ss, cy * ss), label.upper(), font=rf, fill=muted, anchor="lm")
        draw.text((X1 * ss, cy * ss), value, font=rf, fill=fg, anchor="rm")

    def _div(y_div):
        draw.rectangle([X0 * ss, y_div * ss - ss, X1 * ss, y_div * ss], fill=(*ltc, 60))

    for i in range(n):
        label, value, line_px = rows[i]
        if dbg:
            print(f"[fonts] row {i} data_px={line_px} row_h={row_h}")
        _put(Y0 + i * row_h + row_h / 2, label, value, line_px)
        if i < n - 1 or pinned:
            if rows[i][0] == "__sep__" or (i + 1 < n and rows[i + 1][0] == "__sep__"):
                pass   # a separator draws its own rule; no double ruling
            else:
                _div(Y0 + (i + 1) * row_h)

    np_ = len(pinned)
    for j in range(np_):
        label, value, line_px = pinned[j]
        if dbg:
            print(f"[fonts] pinned {j} data_px={line_px} row_h={row_h}")
        _put(Y1 - (np_ - j) * row_h + row_h / 2, label, value, line_px)
        if j < np_ - 1:
            _div(Y1 - (np_ - j - 1) * row_h)


def _ellipsize(draw, text: str, font, max_w: float) -> str:
    """Truncate to fit max_w (screen px) with a trailing ellipsis."""
    if draw.textlength(text, font=font) <= max_w:
        return text
    while text and draw.textlength(text + "…", font=font) > max_w:
        text = text[:-1]
    return text + "…"


def _marquee(draw, text: str, font, x: float, y: float, avail_w: float,
             line_h: float, ss: int, fill) -> None:
    """Draw `text` at (x, y); if wider than avail_w (panel px) it scrolls
    right-to-left with reading pauses at both ends. Stateless — driven by
    time.monotonic(), so speed is identical at any render fps. The text is
    clipped to the avail_w box (tile + paste), never bleeding outside."""
    now = time.monotonic()
    tw = draw.textlength(text, font=font)
    if tw <= avail_w * ss:
        draw.text((x * ss, y * ss), text, font=font, fill=fill, anchor="la")
        return
    gap = 12.0                                    # tail padding, panel px
    overflow = (tw - avail_w * ss) / ss + gap
    speed = 40.0                                  # panel px / s
    pause_a, pause_b = 1.5, 1.2
    cycle = pause_a + overflow / speed + pause_b
    t = now % cycle
    shift = min(max(0.0, t - pause_a), overflow / speed) * speed
    tile_w = max(4, int(avail_w * ss))
    tile_h = max(4, int(line_h * ss))
    tile = Image.new("RGBA", (tile_w, tile_h), (0, 0, 0, 0))
    td = ImageDraw.Draw(tile)
    td.text((-shift * ss, tile_h / 2), text, font=font, fill=fill, anchor="lm")
    try:
        draw._image.paste(tile, (int(x * ss), int(y * ss)), tile)
    except Exception:                             # PIL internals changed
        draw.text((x * ss, y * ss), text[:1], font=font, fill=fill, anchor="la")


def _fmt_us(us) -> str:
    sec = max(0, int(us / 1e6))
    if sec >= 3600:
        return f"{sec // 3600}:{sec % 3600 // 60:02d}:{sec % 60:02d}"
    return f"{sec // 60}:{sec % 60:02d}"


_MEDIA_PEAKS: dict = {}


def _media(draw, theme, fbook, widget, s, x, y, w, h, ss) -> None:
    m = s.get("media") or {}
    status = m.get("status")
    playing = status == "Playing"
    accent = theme.color("accent")
    fg = fbook.color("data")
    muted = fbook.color("title")
    ltc = theme.color("lighter_background")

    X0, X1 = x + 10, x + w - 10
    Y0, Y1 = y + 10, y + h - 10
    cw, ch = X1 - X0, Y1 - Y0

    # fixed role-derived text sizes; the spectrum is the flexible element
    track_px = fbook.px("data") * 1.2
    artist_px = fbook.px("data") * 0.85
    time_px = fbook.px("data") * 0.7

    title = m.get("title") or "Nothing playing"
    idle = not m.get("title")
    track_f = fbook.font(track_px, "data", bold=True)
    _marquee(draw, title, track_f, X0, Y0, cw, track_px * 1.4, ss,
             muted if idle else fg)
    text_h = track_px
    artist = m.get("artist")
    if artist:
        artist_f = fbook.font(artist_px, "data")
        draw.text((X0 * ss, (Y0 + track_px + 2) * ss),
                  _ellipsize(draw, artist, artist_f, cw * ss),
                  font=artist_f, fill=muted, anchor="la")
        text_h = track_px + 2 + artist_px

    # timeline: only when the player reports a duration (slot at the bottom)
    length_us = m.get("length_us")
    pos_us = m.get("position_us") or 0
    ty1 = Y1
    if isinstance(length_us, int) and length_us > 0:
        tf_ = fbook.font(time_px, "data", bold=True)
        bar_h = max(3, int(round(time_px * 0.3)))
        gap = 3
        slot_h = _g10(bar_h + gap + int(round(time_px * 1.25)) + 2)
        ty1 = Y1 - slot_h
        frac = max(0.0, min(1.0, pos_us / length_us)) if playing or pos_us else 0.0
        ybar = ty1
        draw.rectangle([X0 * ss, ybar * ss, X1 * ss, (ybar + bar_h) * ss],
                       fill=(*ltc, 110))
        if frac > 0:
            fill_x = X0 + (X1 - X0) * frac
            draw.rectangle([X0 * ss, ybar * ss, fill_x * ss, (ybar + bar_h) * ss],
                           fill=(*accent, 255))
        text_y = ybar + bar_h + gap
        draw.text((X0 * ss, text_y * ss), _fmt_us(pos_us),
                  font=tf_, fill=muted, anchor="la")
        draw.text((X1 * ss, text_y * ss), "-" + _fmt_us(length_us - pos_us),
                  font=tf_, fill=muted, anchor="ra")

    # spectrum fills the remaining band (flat when idle — bands decay to 0)
    bands = m.get("bands") or []
    n = len(bands) or int(widget.get("bands", 24))
    bands = list(bands) + [0.0] * (n - len(bands))
    sy0 = Y0 + text_h + 8
    sy1 = ty1 - 4
    maxh = max(4.0, sy1 - sy0)
    gap = 2
    bar_w = max(1.0, (cw - (n - 1) * gap) / n)
    peaks = _MEDIA_PEAKS.setdefault((x, y, w, n), [0.0] * n)
    if len(peaks) != n:
        peaks = [0.0] * n
        _MEDIA_PEAKS[(x, y, w, n)] = peaks
    for i in range(n):
        v = max(0.0, min(1.0, bands[i]))
        peaks[i] = max(v, peaks[i] * 0.93)
        bx0 = X0 + i * (bar_w + gap)
        hgt = v * maxh
        if hgt >= 1:
            draw.rectangle([bx0 * ss, (sy1 - hgt) * ss,
                            (bx0 + bar_w) * ss, sy1 * ss], fill=(*accent, 225))
        ph = peaks[i] * maxh
        if ph >= 2:
            draw.rectangle([bx0 * ss, (sy1 - ph - 2) * ss,
                            (bx0 + bar_w) * ss, (sy1 - ph) * ss], fill=(*ltc, 210))


_WIDGETS = {
    "clock": _clock,
    "gauge": _gauge,
    "stats": _stats,
    "media": _media,
}


def render_frame(theme, bg: Image.Image, widgets: list[dict], s: dict,
                 W: int, H: int, ss: int, fonts: dict | None = None,
                 cards: dict | None = None) -> Image.Image:
    Wss, Hss = W * ss, H * ss
    base = bg.convert("RGBA").resize((Wss, Hss), Image.NEAREST) if bg.size != (Wss, Hss) else bg.convert("RGBA")
    overlay = Image.new("RGBA", (Wss, Hss), (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    fbook = FontBook(theme, fonts, ss)
    cards_cfg = cards or {}
    card_rounded = bool(cards_cfg.get("rounded", True))
    card_outline = (cards_cfg.get("outline") or "subtle").strip()

    for widget in widgets:
        wtype = widget.get("type", "stats")
        x, y = int(widget.get("x", 40)), int(widget.get("y", 40))
        w, h = int(widget.get("w", 460)), int(widget.get("h", 200))
        if wtype in _WIDGETS and widget.get("card", True):
            _card(draw, x, y, w, h, ss, theme, rounded=card_rounded,
                  outline=card_outline)
        # uniform card title (all widget types): blank = no title, content
        # shifts down into the freed band; 10px card margins everywhere
        title_h = 0
        title_text = (widget.get("title") or "").strip()
        if title_text:
            tfont = fbook.font(fbook.px("title"), "title", bold=True)
            tpx = tfont.size / ss
            title_h = _g10(10 + tpx + 10)
            txx, tyy = (x + 10) * ss, (y + 10) * ss
            label = title_text.upper()
            draw.text((txx, tyy), label, font=tfont,
                      fill=fbook.color("title"), anchor="la")
            tw = draw.textlength(label, font=tfont)   # underline = word length
            draw.rectangle([txx, (y + 10 + tpx + 4) * ss,
                            txx + tw, (y + 10 + tpx + 6) * ss],
                           fill=(*theme.color("accent"), 255))
        fn = _WIDGETS.get(wtype)
        if fn:
            fn(draw, theme, fbook, widget, s, x, y + title_h, w, h - title_h, ss)

    frame = Image.alpha_composite(base, overlay).convert("RGB")
    if ss != 1:
        frame = frame.resize((W, H), Image.BOX)   # exact 2:1 average, ~3x faster than LANCZOS
    return frame
