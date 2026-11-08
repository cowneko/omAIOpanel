"""Live sensor sampling: CPU, RAM, disks, network, GPU (NVML), temps.

All reads are non-blocking; rates are derived between samples and smoothed
with an EMA so the panel shows stable, readable numbers.
"""

from __future__ import annotations

import time
import threading
from pathlib import Path

import psutil

from .media import MediaMonitor

try:
    import pynvml
    _NVML = True
except Exception:
    _NVML = False


def _ema(prev: float | None, new: float, alpha: float = 0.35) -> float:
    return new if prev is None else prev + alpha * (new - prev)


def _fmt_rate(bps: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if abs(bps) < 1000 or unit == "GB":
            return f"{bps:,.0f} {unit}/s" if unit == "B" else f"{bps:,.1f} {unit}/s"
        bps /= 1000
    return f"{bps:,.1f} GB/s"


def _cpu_temp() -> float | None:
    try:
        temps = psutil.sensors_temperatures()
    except Exception:
        return None
    for src in ("k10temp", "zenpower", "coretemp", "cpu_thermal", "acpitz"):
        if src in temps:
            entries = temps[src]
            if entries:
                # prefer Tctl / Package / hottest
                best = max(entries, key=lambda e: e.current or 0)
                if best.current:
                    return float(best.current)
    return None


class SensorHub:
    def __init__(self) -> None:
        self._last_t: float | None = None
        self._net_last: tuple[float, float] | None = None
        self._io_last: tuple[float, float] | None = None
        self._smooth: dict[str, float] = {}
        self.media = MediaMonitor()      # threads start lazily via start_media()
        self.weather = None              # WeatherMonitor, via start_weather()
        self._temp_cache: float | None = None
        self._gpu_cache: dict = {}
        self._vols_cache: dict = {}
        threading.Thread(target=self._slow_loop, daemon=True).start()
        psutil.cpu_percent(interval=None, percpu=True)  # prime
        self._gpu_ok = False
        if _NVML:
            try:
                pynvml.nvmlInit()
                self._gpu = pynvml.nvmlDeviceGetHandleByIndex(0)
                self._gpu_ok = True
                self._gpu_name = pynvml.nvmlDeviceGetName(self._gpu)
                if isinstance(self._gpu_name, bytes):
                    self._gpu_name = self._gpu_name.decode()
            except Exception:
                self._gpu_ok = False

    def close(self) -> None:
        if _NVML and self._gpu_ok:
            try:
                pynvml.nvmlShutdown()
            except Exception:
                pass

    # ---- sample ----------------------------------------------------------

    def start_media(self) -> None:
        """Start MPRIS + spectrum capture (call only when a media widget exists)."""
        self.media.start()

    def start_weather(self, lat=None, lon=None) -> None:
        """Start the weather poller (call only when the clock shows weather)."""
        from aio.weather import WeatherMonitor
        if self.weather is None:
            self.weather = WeatherMonitor(lat, lon)
        self.weather.start()

    def _slow_loop(self) -> None:
        """Background refresh for sensors that can block (hwmon scans, NVML,
        USB volume stats). sample() only reads the caches — a stalled read
        here must never stall the render loop (a 1.2 s hwmon spike was
        killing the VU refresh rate)."""
        last_t = 0.0
        last_g = 0.0
        last_v = 0.0
        while True:
            now = time.monotonic()
            try:
                if now - last_t >= 1.0:
                    last_t = now
                    self._temp_cache = _cpu_temp()
            except Exception:
                pass
            try:
                if self._gpu_ok and now - last_g >= 0.5:
                    last_g = now
                    util = pynvml.nvmlDeviceGetUtilizationRates(self._gpu)
                    mem = pynvml.nvmlDeviceGetMemoryInfo(self._gpu)
                    temp = pynvml.nvmlDeviceGetTemperature(self._gpu, pynvml.NVML_TEMPERATURE_GPU)
                    g = {"util": float(util.gpu), "temp": float(temp),
                         "used": mem.used, "total": mem.total}
                    try:
                        g["power"] = pynvml.nvmlDeviceGetPowerUsage(self._gpu) / 1000
                    except Exception:
                        pass
                    try:
                        g["fan"] = float(pynvml.nvmlDeviceGetFanSpeed(self._gpu))
                    except Exception:
                        pass
                    self._gpu_cache = g
            except Exception:
                pass
            try:
                if now - last_v >= 2.0:
                    last_v = now
                    vols = {}
                    for mp in mounted_volumes():
                        try:
                            u = psutil.disk_usage(mp)
                        except Exception:
                            continue
                        vols[mp] = (u.used, u.total, u.percent)
                    self._vols_cache = vols
            except Exception:
                pass
            time.sleep(0.2)

    def sample(self) -> dict:
        now = time.monotonic()
        dt = (now - self._last_t) if self._last_t else 0.0
        self._last_t = now
        s: dict = {}

        # mounted volumes (/Volumes/* …) — cached by the slow thread
        s["volumes"] = self._vols_cache
        s["media"] = self.media.snapshot()
        s["weather"] = self.weather.snapshot() if self.weather else {}

        # CPU
        percpu = psutil.cpu_percent(interval=None, percpu=True)
        s["cpu_load"] = _ema(self._smooth.get("cpu_load"), sum(percpu) / len(percpu))
        self._smooth["cpu_load"] = s["cpu_load"]
        freq = psutil.cpu_freq()
        s["cpu_freq"] = (freq.current / 1000) if freq else None  # GHz
        t = self._temp_cache
        if t is not None:
            s["cpu_temp"] = _ema(self._smooth.get("cpu_temp"), t)
            self._smooth["cpu_temp"] = s["cpu_temp"]
        else:
            s["cpu_temp"] = self._smooth.get("cpu_temp")
        s["load1"] = psutil.getloadavg()[0]
        s["uptime"] = int(time.time() - psutil.boot_time())

        # RAM
        vm = psutil.virtual_memory()
        s["ram_used"] = vm.used
        s["ram_total"] = vm.total
        s["ram_pct"] = vm.percent

        # Network rates (all physical-ish interfaces, skip lo)
        counters = psutil.net_io_counters(pernic=True)
        sent = sum(n.bytes_sent for name, n in counters.items() if name != "lo")
        recv = sum(n.bytes_recv for name, n in counters.items() if name != "lo")
        if self._net_last and dt > 0:
            up = _ema(self._smooth.get("net_up"), max(0.0, (sent - self._net_last[0]) / dt))
            down = _ema(self._smooth.get("net_down"), max(0.0, (recv - self._net_last[1]) / dt))
            s["net_up_bps"], s["net_down_bps"] = up, down
            self._smooth["net_up"], self._smooth["net_down"] = up, down
        self._net_last = (sent, recv)

        # Disk I/O rates + root usage
        try:
            io = psutil.disk_io_counters()
            if io and self._io_last and dt > 0:
                r = _ema(self._smooth.get("io_r"), max(0.0, (io.read_bytes - self._io_last[0]) / dt))
                w = _ema(self._smooth.get("io_w"), max(0.0, (io.write_bytes - self._io_last[1]) / dt))
                s["io_read_bps"], s["io_write_bps"] = r, w
                self._smooth["io_r"], self._smooth["io_w"] = r, w
            if io:
                self._io_last = (io.read_bytes, io.write_bytes)
        except Exception:
            pass
        try:
            du = psutil.disk_usage("/")
            s["disk_root_used"], s["disk_root_total"], s["disk_root_pct"] = du.used, du.total, du.percent
        except Exception:
            pass

        # GPU (NVML) — cached by the slow thread (0.5 s cadence)
        if self._gpu_ok:
            g = self._gpu_cache
            if g:
                s["gpu_load"] = _ema(self._smooth.get("gpu_load"), g["util"])
                s["gpu_temp"] = _ema(self._smooth.get("gpu_temp"), g["temp"])
                s["gpu_vram_used"], s["gpu_vram_total"] = g["used"], g["total"]
                s["gpu_vram_pct"] = g["used"] / g["total"] * 100 if g["total"] else None
                self._smooth["gpu_load"] = s["gpu_load"]
                self._smooth["gpu_temp"] = s["gpu_temp"]
                if "power" in g:
                    s["gpu_power"] = _ema(self._smooth.get("gpu_power"), g["power"])
                    self._smooth["gpu_power"] = s["gpu_power"]
                if "fan" in g:
                    s["gpu_fan"] = g["fan"]
                s["gpu_name"] = self._gpu_name

        return s


# ---- row formatting for the stats widget ------------------------------

DATA_LABELS = {
    "cpu_temp": "CPU", "cpu_load": "CPU LOAD", "gpu_temp": "GPU",
    "gpu_power": "GPU POWER", "gpu_vram": "VRAM", "ram": "RAM",
    "net": "NET", "net_up": "NET ↑", "net_down": "NET ↓",
    "disk_io": "DISK I/O", "disk_root": "DISK", "uptime": "UPTIME",
    "load1": "LOAD",
}


_REAL_FSTYPES = {"ntfs3", "ntfs-3g", "fuseblk", "ext4", "btrfs", "xfs", "zfs",
                 "exfat", "vfat", "f2fs"}


def mounted_volumes() -> list[str]:
    """User-facing mounted partitions (/Volumes/*, /run/media/*); root excluded."""
    try:
        parts = psutil.disk_partitions(all=False)
    except Exception:
        return []
    out = []
    for p in parts:
        mp = p.mountpoint
        if mp == "/" or not mp.startswith(("/Volumes/", "/run/media/")):
            continue
        if p.fstype and p.fstype not in _REAL_FSTYPES:
            continue
        out.append(mp)
    return sorted(out)


def volume_label(mp: str) -> str:
    return (mp.rstrip("/").rsplit("/", 1)[-1] or mp).upper()


def _entry_key(entry):
    return entry.get("key") if isinstance(entry, dict) else entry


def _entry_label(entry) -> str:
    return (entry.get("label") or "").strip() if isinstance(entry, dict) else ""


def format_row(entry, s: dict) -> tuple[str, str] | None:
    """(label, value) for a line entry — str preset or {key, label} — or None."""
    key = _entry_key(entry)
    custom = _entry_label(entry)
    row = _format_key(key, s)
    if row and custom:
        row = (custom, row[1])
    return row


def _format_key(key: str, s: dict) -> tuple[str, str] | None:
    if key == "cpu_temp":
        return ("CPU", f"{s['cpu_temp']:.0f}°C" if s.get("cpu_temp") else "—")
    if key == "cpu_load":
        return ("CPU load", f"{s.get('cpu_load', 0):.0f}%")
    if key == "gpu_temp":
        return (DATA_LABELS["gpu_temp"], f"{s['gpu_temp']:.0f}°C" if s.get("gpu_temp") else "—")
    if key == "gpu_power":
        return ("GPU power", f"{s['gpu_power']:.0f} W" if s.get("gpu_power") is not None else "—")
    if key in ("gpu_vram", "vram"):
        if s.get("gpu_vram_total"):
            return ("VRAM", f"{s['gpu_vram_used']/2**30:.1f} / {s['gpu_vram_total']/2**30:.0f} GB")
        return None
    if key == "ram":
        return ("RAM", f"{s['ram_used']/2**30:.1f} / {s['ram_total']/2**30:.0f} GB  ({s['ram_pct']:.0f}%)")
    if key == "net":
        up = _fmt_rate(s.get("net_up_bps", 0))
        down = _fmt_rate(s.get("net_down_bps", 0))
        return ("NET", f"↑{up}  ↓{down}")
    if key == "net_up":
        return ("NET ↑", _fmt_rate(s.get("net_up_bps", 0)))
    if key == "net_down":
        return ("NET ↓", _fmt_rate(s.get("net_down_bps", 0)))
    if key == "disk_root":
        if s.get("disk_root_total"):
            return ("DISK", f"{s['disk_root_used']/2**30:.0f}/{s['disk_root_total']/2**30:.0f} GB ({s['disk_root_pct']:.0f}%)")
        return None
    if key == "disk_io":
        return ("DISK I/O", f"↑{_fmt_rate(s.get('io_write_bps', 0))}  ↓{_fmt_rate(s.get('io_read_bps', 0))}")
    if key == "uptime":
        m, sec = divmod(s.get("uptime", 0), 60)
        h, m = divmod(m, 60)
        return ("UPTIME", f"{h}h {m:02d}m")
    if key == "load1":
        return ("LOAD", f"{s.get('load1', 0):.2f}")
    if key in ("empty", "sep", "---"):
        return None
    if key.startswith("disk:"):
        vol = (s.get("volumes") or {}).get(key[5:])
        if vol:
            used, total, pct = vol
            return (volume_label(key[5:]),
                    f"{used/2**30:.0f}/{total/2**30:.0f} GB ({pct:.0f}%)")
        return None
    return None
