"""CLI: run (daemon), once, preview, sleep."""

from __future__ import annotations

import argparse
import logging
import signal
import sys
import threading
import time
from pathlib import Path

from PIL import Image

from . import __version__, render
from .config import CONFIG_PATH, load_config
from .device import KEEPALIVE_S, DeviceError, GrandVisionPanel
from .sensors import SensorHub
from .theme import OmarchyTheme

log = logging.getLogger("aio")

CACHE_DIR = Path.home() / ".cache" / "omAIOpanel"


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )


def build_theme_and_bg(cfg, theme: OmarchyTheme):
    theme.load({
        "font_family": cfg.fonts.get("family"),
        "wallpaper_path": cfg.wallpaper.get("path") if cfg.wallpaper.get("mode") == "path" else None,
        "overrides": cfg.theme_overrides(),
    })
    bg = render.render_background(theme, cfg.wallpaper, int(cfg.width), int(cfg.height), int(cfg.supersample))
    return bg


def cmd_preview(args) -> int:
    cfg = load_config(Path(args.config) if args.config else CONFIG_PATH)
    theme = OmarchyTheme()
    bg = build_theme_and_bg(cfg, theme)
    hub = SensorHub()
    try:
        s = hub.sample()
        # let rates settle so the preview isn't full of zeros
        if not args.no_wait:
            time.sleep(1.2)
            s = hub.sample()
        frame = render.render_frame(theme, bg, cfg.widgets, s,
                                    int(cfg.width), int(cfg.height), int(cfg.supersample),
                                    fonts=cfg.fonts, cards=cfg.cards)
    finally:
        hub.close()
    out = Path(args.out) if args.out else Path("aio-preview.png")
    frame.save(out)
    print(out)
    return 0


def cmd_once(args) -> int:
    """Render one frame, push it, hold keepalive briefly, exit."""
    cfg = load_config(Path(args.config) if args.config else CONFIG_PATH)
    theme = OmarchyTheme()
    bg = build_theme_and_bg(cfg, theme)
    hub = SensorHub()
    panel = GrandVisionPanel(jpeg_quality=int(getattr(cfg, "jpeg_quality", 90)))
    panel.orientation = int(getattr(cfg, "orientation", 90))
    try:
        time.sleep(1.2)  # let EMA/rates settle
        s = hub.sample()
        frame = render.render_frame(theme, bg, cfg.widgets, s,
                                    int(cfg.width), int(cfg.height), int(cfg.supersample),
                                    fonts=cfg.fonts, cards=cfg.cards)
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        frame.save(CACHE_DIR / "last-preview.png")
        if not args.push:
            print("(dry run — use --push to send to the panel)")
            return 0
        panel.connect(max_attempts=3)
        wire = panel.encode_frame(frame)
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            panel.send_raw(wire)     # hold against logo reversion
            time.sleep(KEEPALIVE_S)
        log.info("pushed %d bytes and held 5 s", len(wire))
        panel.disconnect()
    finally:
        hub.close()
    return 0


def _config_mtime() -> int | None:
    try:
        return CONFIG_PATH.stat().st_mtime_ns
    except OSError:
        return None


def cmd_run(args) -> int:
    """Daemon: own the panel, render at fps, keepalive resend ~150 ms."""
    cfg = load_config(Path(args.config) if args.config else CONFIG_PATH)
    W, H, ss = int(cfg.width), int(cfg.height), int(cfg.supersample)

    theme = OmarchyTheme()
    hub = SensorHub()
    if any(w.get("type") == "media" for w in cfg.widgets):
        hub.start_media()
    wx = cfg.weather
    if any(w.get("type") == "clock" and w.get("weather", True) for w in cfg.widgets):
        hub.start_weather(wx.get("lat"), wx.get("lon"))
    panel = GrandVisionPanel()
    panel.orientation = int(getattr(cfg, "orientation", 90))
    panel.canvas = (int(cfg.width), int(cfg.height))
    bg: Image.Image | None = None
    widgets = cfg.widgets
    fps = float(cfg.fps)
    interval = 1.0 / fps

    last_fp = None
    cfg_mtime = _config_mtime()
    frame_bytes: bytes | None = None
    last_render = 0.0
    last_keepalive = 0.0
    running = True
    _fps_n = 0
    _fps_t = time.monotonic()
    _save_q: list = []

    def _preview_saver() -> None:
        """Writes last-preview.png off the render loop — a synchronous PNG
        save costs ~130 ms (zlib on photo content) and killed the fps.
        Throttled to 1/s: it is a debugging artifact, always latest frame."""
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        while running:
            if _save_q:
                f = _save_q[0]
                del _save_q[0]
                try:
                    f.save(CACHE_DIR / "last-preview.jpg", quality=85)  # JPEG: ~5 ms vs ~130 ms PNG
                except Exception:
                    pass
            time.sleep(1.0)

    threading.Thread(target=_preview_saver, daemon=True).start()

    def _stop(signum, frame):
        nonlocal running
        running = False

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)

    log.info("omAIOpanel %s running: fps=%.2f canvas=%dx%d widgets=%d",
             __version__, fps, W, H, len(widgets))

    try:
        while running:
            t0 = time.monotonic()
            changed = False
            now = time.monotonic()

            # --- hot-reload config (the editor edits this file) ----------
            m = _config_mtime()
            if m != cfg_mtime:
                cfg_mtime = m
                cfg = load_config(Path(args.config) if args.config else CONFIG_PATH)
                widgets = cfg.widgets
                W, H = int(cfg.width), int(cfg.height)   # canvas may change shape
                ss = int(cfg.supersample)
                fps = float(cfg.fps)
                if any(w.get("type") == "media" for w in widgets):
                    hub.start_media()
                wx = cfg.weather
                if any(w.get("type") == "clock" and w.get("weather", True)
                       for w in widgets):
                    hub.start_weather(wx.get("lat"), wx.get("lon"))
                interval = 1.0 / fps
                panel.jpeg_quality = int(getattr(cfg, "jpeg_quality", 90))
                panel.canvas = (int(cfg.width), int(cfg.height))
                panel.set_orientation(int(getattr(cfg, "orientation", 90)))
                panel.disconnect()      # cheap re-handshake guarantees consistent geometry
                frame_bytes = None
                bg = None
                last_fp = None
                log.info("config reloaded: fps=%.2f widgets=%d orientation=%d",
                         fps, len(widgets), panel.orientation)

            # --- theme watcher (frozen when [theme] sync = false) ---------
            try:
                sync_on = cfg.theme_cfg.get("sync", True)
                fp = theme.fingerprint({
                    "wallpaper_path": cfg.wallpaper.get("path") if cfg.wallpaper.get("mode") == "path" else None,
                })
                if bg is None or (sync_on and fp != last_fp):
                    last_fp = fp
                    bg = build_theme_and_bg(cfg, theme)
                    frame_bytes = None            # force re-render
                    log.info("theme background re-rendered (wallpaper=%s)", theme.wallpaper)
            except OSError:
                pass

            # --- render at fps -------------------------------------------
            if frame_bytes is None or now - last_render >= interval:
                try:
                    s = hub.sample()
                    frame = render.render_frame(theme, bg, widgets, s, W, H, ss,
                                                fonts=cfg.fonts, cards=cfg.cards)
                    if panel.connected:
                        frame_bytes = panel.encode_frame(frame)
                    else:
                        frame_bytes = None
                    _save_q.clear()               # saver thread writes latest only
                    _save_q.append(frame)
                    last_render = now
                    changed = True
                    _fps_n += 1
                except Exception:
                    log.exception("render error")
                    time.sleep(1.0)

            # --- connect if needed ---------------------------------------
            if not panel.connected:
                try:
                    panel.connect()
                    frame_bytes = None            # fresh handshake -> fresh frame
                except DeviceError as e:
                    log.debug("panel not available: %s", e)

            # --- send: new frame, or keepalive resend --------------------
            if panel.connected and frame_bytes is not None:
                if changed or (now - last_keepalive) >= KEEPALIVE_S:
                    if panel.send_raw(frame_bytes):
                        last_keepalive = now
                    else:
                        log.warning("send failed — reconnecting")
                        panel.disconnect()
                        frame_bytes = None

            dt = time.monotonic() - t0
            if now - _fps_t >= 5.0:
                log.info("loop: %.1f renders/s, dt=%.0f ms, interval=%.0f ms",
                         _fps_n / max(0.001, now - _fps_t), dt * 1000, interval * 1000)
                _fps_n = 0
                _fps_t = now
            time.sleep(max(0.005, interval - dt))   # sleep to the next render deadline
    finally:
        hub.close()
        panel.disconnect()
    log.info("stopped")
    return 0


def cmd_sleep(args) -> int:
    """Blank the panel."""
    panel = GrandVisionPanel()
    panel.connect(max_attempts=3)
    panel.send_black()
    panel.disconnect()
    print("panel blanked")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        prog="omAIOpanel",
        description="Render the Omarchy theme + live sensors onto a Thermalright AIO screen (direct USB).")
    ap.add_argument("--version", action="version", version=__version__)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("run", help="run the renderer loop (daemon mode)")
    p.add_argument("--config", help="alternate config.toml path")
    p.add_argument("-v", "--verbose", action="store_true")
    p.set_defaults(fn=cmd_run, verbose=False)

    p = sub.add_parser("once", help="render one frame and push it (held 5 s)")
    p.add_argument("--config")
    p.add_argument("--push", action="store_true", help="push to panel (default: cache file only)")
    p.add_argument("-v", "--verbose", action="store_true")
    p.set_defaults(fn=cmd_once, verbose=False)

    p = sub.add_parser("preview", help="render one frame to a PNG (no panel)")
    p.add_argument("--config")
    p.add_argument("--out", default="aio-preview.png")
    p.add_argument("--no-wait", action="store_true")
    p.add_argument("-v", "--verbose", action="store_true")
    p.set_defaults(fn=cmd_preview, verbose=False)

    p = sub.add_parser("sleep", help="blank the panel")
    p.add_argument("--config")
    p.add_argument("-v", "--verbose", action="store_true")
    p.set_defaults(fn=cmd_sleep, verbose=False)

    args = ap.parse_args(argv)
    _setup_logging(getattr(args, "verbose", False))
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
