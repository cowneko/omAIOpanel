"""Media widget data: MPRIS metadata + system-audio spectrum (PipeWire).

MediaMonitor runs two daemon threads:
- ``_mpris_loop``: polls every MPRIS player (org.mpris.MediaPlayer2.*) on the
  session bus via Gio sync calls; prefers whichever player is Playing.
- ``_spectrum_loop``: captures the default sink mix with ``pw-record``
  (fallback ``parecord <sink>.monitor``) and FFTs each chunk into log-spaced
  bands with fast-attack / slow-decay smoothing.

snapshot() never blocks and always returns a plain dict — safe to call from
the render tick. Threads are started lazily via start() only when a media
widget exists (no mic/audio capture otherwise).
"""

from __future__ import annotations

import math
import shutil
import subprocess
import threading
import time

MEDIA_POLL_S = 2.0
SR = 48000
CHUNK = 2048                    # frames per FFT window (f32 mono = 4 B/frame)
BANDS = 28
FLOOR_DB = -62.0                # spectrum floor (dB) mapped to 0.0


def _db_to_unit(db: float) -> float:
    return max(0.0, min(1.0, (db - FLOOR_DB) / -FLOOR_DB))


class MediaMonitor:
    def __init__(self, bands: int = BANDS) -> None:
        self.bands = bands
        self._lock = threading.Lock()
        self._meta: dict = {}
        self._bands = [0.0] * bands
        self._vu = 0.0
        self._pos_base_us = 0
        self._pos_t = 0.0
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self._edge_cache: list[tuple[int, int]] | None = None
        self._last_audio = 0.0

    # -- lifecycle ----------------------------------------------------------

    def start(self) -> None:
        if self._threads:
            return
        for fn in (self._mpris_loop, self._spectrum_loop):
            t = threading.Thread(target=fn, daemon=True)
            t.start()
            self._threads.append(t)

    def stop(self) -> None:
        self._stop.set()

    def snapshot(self) -> dict:
        with self._lock:
            d = dict(self._meta)
            d["bands"] = list(self._bands)
            d["vu"] = self._vu
            pos, t0 = self._pos_base_us, self._pos_t
        # if the sink suspended (nothing playing, no PCM chunks), fade out
        elapsed = time.monotonic() - self._last_audio
        if elapsed > 0.3:
            f = 0.5 ** (elapsed / 0.4)
            d["bands"] = [v * f for v in d["bands"]]
            d["vu"] *= f
        if d.get("status") == "Playing":
            d["position_us"] = pos + int((time.monotonic() - t0) * 1e6)
        if isinstance(d.get("length_us"), int) and isinstance(d.get("position_us"), int):
            d["position_us"] = max(0, min(d["position_us"], d["length_us"]))
        return d

    # -- MPRIS ---------------------------------------------------------------

    def _mpris_loop(self) -> None:
        import gi
        gi.require_version("Gio", "2.0")
        from gi.repository import Gio, GLib

        while not self._stop.is_set():
            meta: dict = {}
            try:
                bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
                res = bus.call_sync(
                    "org.freedesktop.DBus", "/org/freedesktop/DBus",
                    "org.freedesktop.DBus", "ListNames", None,
                    GLib.VariantType("(as)"), Gio.DBusCallFlags.NONE, 800, None)
                players = [n for n in res.unpack()[0]
                           if n.startswith("org.mpris.MediaPlayer2.")]
                meta = self._poll_players(bus, players, GLib) or {}
            except Exception:
                meta = {}
            with self._lock:
                self._meta = meta
                self._pos_base_us = int(meta.get("position_us") or 0)
                self._pos_t = time.monotonic()
            self._stop.wait(MEDIA_POLL_S)

    def _poll_players(self, bus, players, GLib) -> dict | None:
        fallback = None
        for name in sorted(players):
            try:
                info = self._query_player(bus, name, GLib)
            except Exception:
                continue
            if not info:
                continue
            if info.get("status") == "Playing":
                return info
            fallback = fallback or info
        return fallback

    def _query_player(self, bus, name: str, GLib) -> dict | None:
        from gi.repository import Gio

        def get(prop: str):
            r = bus.call_sync(
                name, "/org/mpris/MediaPlayer2",
                "org.freedesktop.DBus.Properties", "Get",
                GLib.Variant("(ss)", ("org.mpris.MediaPlayer2.Player", prop)),
                None, Gio.DBusCallFlags.NONE, 600, None)
            return r.unpack()[0]

        md = get("Metadata") or {}
        status = get("PlaybackStatus")
        if not md.get("xesam:title") and status == "Stopped":
            return None
        try:
            pos = int(get("Position"))
        except Exception:
            pos = 0
        artists = md.get("xesam:artist") or []
        return {
            "player": name.rsplit(".", 1)[-1],
            "status": status,
            "title": md.get("xesam:title"),
            "artist": ", ".join(a for a in artists if a) or None,
            "album": md.get("xesam:album"),
            "length_us": int(md["mpris:length"]) if md.get("mpris:length") else None,
            "position_us": pos,
        }

    # -- spectrum -------------------------------------------------------------

    def _spectrum_loop(self) -> None:
        """Capture via a named FIFO: parecord/pw-record treat it as a file
        (their stdout-pipe path EOFs instantly on this system), and we read
        the PCM side with select + timeouts so a dead capture never hangs."""
        import os
        import select as _select
        import tempfile

        while not self._stop.is_set():
            try:
                cmds = self._capture_cmds()
                if not cmds:
                    self._stop.wait(2.0)
                    continue
                for cmd in cmds:
                    if self._stop.is_set():
                        return
                    fifo = os.path.join(tempfile.gettempdir(),
                                        f"aio-media-{os.getpid()}.pcm")
                    try:
                        os.unlink(fifo)
                    except OSError:
                        pass
                    os.mkfifo(fifo)
                    proc = None
                    try:
                        cap = cmd[:-1] + [fifo]
                        proc = subprocess.Popen(cap, stdout=subprocess.DEVNULL,
                                                stderr=subprocess.DEVNULL)
                        fd = os.open(fifo, os.O_RDWR)   # never blocks on open
                        try:
                            while not self._stop.is_set():
                                r, _, _ = _select.select([fd], [], [], 1.0)
                                if not r:
                                    if proc.poll() is not None:
                                        break        # capture died -> next
                                    continue
                                raw = os.read(fd, CHUNK * 4)
                                if raw:
                                    self._ingest(raw)
                        finally:
                            os.close(fd)
                        if proc.poll() is None:
                            proc.terminate()
                        try:
                            proc.wait(timeout=1)
                        except Exception:
                            proc.kill()
                    finally:
                        try:
                            os.unlink(fifo)
                        except OSError:
                            pass
                self._stop.wait(2.0)      # every candidate failed — retry later
            except Exception:
                if os.environ.get("AIO_MEDIA_DEBUG"):
                    import traceback
                    traceback.print_exc()
                self._stop.wait(1.0)

    def _capture_cmds(self) -> list[list[str]]:
        """Capture commands in preference order for the current default sink."""
        sink = None
        if shutil.which("pactl"):
            try:
                sink = (subprocess.run(["pactl", "get-default-sink"],
                                       capture_output=True, text=True, timeout=3
                                       ).stdout.strip() or None)
            except Exception:
                sink = None
        if not sink:
            return []
        cmds = []
        if shutil.which("parecord"):
            cmds.append(["parecord", "-d", f"{sink}.monitor",
                         "--format=float32le", "--rate", str(SR),
                         "--channels=1", "--latency-msec=40", "--raw", "-"])
        if shutil.which("pw-record"):
            cmds.append(["pw-record", "--target", sink, "--rate", str(SR),
                         "--channels", "1", "--format", "f32",
                         "--latency-msec=40", "--raw", "-"])
        return cmds

    def _edges(self, nbins: int) -> list[tuple[int, int]]:
        if self._edge_cache is None:
            lo, hi = 2, max(nbins - 1, self.bands + 2)
            marks = [round(lo * (hi / lo) ** (i / (self.bands - 1)))
                     for i in range(self.bands)]
            out = []
            for i, a in enumerate(marks):
                b = marks[i + 1] if i + 1 < len(marks) else nbins
                a = min(a, nbins - 1)
                out.append((a, max(b, a + 1)))
            self._edge_cache = out
        return self._edge_cache

    def _ingest(self, raw: bytes) -> None:
        import numpy as np
        self._last_audio = time.monotonic()
        x = np.frombuffer(raw, dtype="<f4").astype(np.float32)
        x = np.clip(x, -4.0, 4.0)
        rms = float(np.sqrt(np.mean(x * x)) + 1e-9)
        spec = np.abs(np.fft.rfft(x * np.hanning(len(x)).astype(np.float32)))
        spec /= (len(x) / 4)
        nb = len(spec)
        vals = []
        for a, b in self._edges(nb):
            seg = spec[a:min(b, nb)]
            v = float(np.sqrt(np.mean(seg * seg)) + 1e-9)
            vals.append(_db_to_unit(20.0 * math.log10(v)))
        with self._lock:
            for i, v in enumerate(vals[:self.bands]):
                old = self._bands[i]
                self._bands[i] = v if v >= old else old * 0.80 + v * 0.20
            vu = _db_to_unit(20.0 * math.log10(rms))
            self._vu = vu if vu >= self._vu else self._vu * 0.85 + vu * 0.15
