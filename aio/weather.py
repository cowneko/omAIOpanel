"""Current local weather for the clock widget.

WeatherMonitor runs one daemon thread:
- resolves the location once (config lat/lon, else IP geolocation cached to
  ~/.config/omAIOpanel/location.json),
- polls Open-Meteo (no API key) every 10 min for current temperature,
  WMO weather code and day/night flag.

snapshot() never blocks. wmo_glyph() maps WMO codes to Nerd Font weather
glyphs (verified against the JetBrains Mono NF cmap / NF glyphnames.json).
"""

from __future__ import annotations

import json
import shutil
import threading
import time
import urllib.request
from pathlib import Path

POLL_S = 600
LOCATION_FILE = Path.home() / ".config" / "omAIOpanel" / "location.json"

# WMO weather_code -> (day glyph, night glyph), Nerd Font weather range
_WMO = {
    0: ("\ue30d", "\ue32b"),   # clear sky
    1: ("\ue30d", "\ue32b"),   # mainly clear
    2: ("\ue302", "\ue37b"),   # partly cloudy
    3: ("\ue312", "\ue312"),   # overcast
    45: ("\ue313", "\ue313"),  # fog
    48: ("\ue313", "\ue313"),  # rime fog
    51: ("\ue30b", "\ue336"),  # light drizzle
    53: ("\ue30b", "\ue336"),
    55: ("\ue30b", "\ue336"),
    56: ("\ue3aa", "\ue3ac"),  # freezing drizzle
    57: ("\ue3aa", "\ue3ac"),
    61: ("\ue308", "\ue333"),  # light rain
    63: ("\ue309", "\ue334"),  # rain
    65: ("\ue318", "\ue318"),  # heavy rain
    66: ("\ue3aa", "\ue3ac"),  # freezing rain
    67: ("\ue3aa", "\ue3ac"),
    71: ("\ue30a", "\ue335"),  # light snow
    73: ("\ue31a", "\ue327"),  # snow
    75: ("\ue35e", "\ue360"),  # heavy snow
    77: ("\ue36f", "\ue36f"),  # snow grains
    80: ("\ue309", "\ue334"),  # light showers
    81: ("\ue309", "\ue334"),
    82: ("\ue318", "\ue318"),  # violent showers
    85: ("\ue30a", "\ue335"),  # snow showers
    86: ("\ue35e", "\ue360"),
    95: ("\ue30f", "\ue338"),  # thunderstorm
    96: ("\ue306", "\ue322"),  # thunderstorm + hail
    99: ("\ue306", "\ue322"),
}


def wmo_glyph(code, is_day=True) -> str | None:
    pair = _WMO.get(code if code is None else int(code))
    if not pair:
        return None
    return pair[0] if is_day else pair[1]


def _http_json(url: str, timeout: float = 8.0) -> dict | None:
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "omAIOpanel"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode())
    except Exception:
        return None


class WeatherMonitor:
    def __init__(self, lat: float | None = None, lon: float | None = None) -> None:
        self.lat = lat
        self.lon = lon
        self._lock = threading.Lock()
        self._data: dict = {}
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread:
            return
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def snapshot(self) -> dict:
        with self._lock:
            return dict(self._data)

    # -- internals -----------------------------------------------------------

    def _resolve_location(self) -> tuple[float, float] | None:
        def _num(v):
            try:
                return float(v)                # rejects None, "", "None"
            except (TypeError, ValueError):
                return None
        lat, lon = _num(self.lat), _num(self.lon)
        if lat is not None and lon is not None:
            return (lat, lon)
        try:                                   # cached from a previous run
            loc = json.loads(LOCATION_FILE.read_text())
            return (float(loc["lat"]), float(loc["lon"]))
        except Exception:
            pass
        loc = _http_json("http://ip-api.com/json/?fields=lat,lon,city")
        if not loc or "lat" not in loc:
            return None
        try:
            out = {"lat": float(loc["lat"]), "lon": float(loc["lon"]),
                   "city": loc.get("city", "")}
            LOCATION_FILE.parent.mkdir(parents=True, exist_ok=True)
            LOCATION_FILE.write_text(json.dumps(out))
            return (out["lat"], out["lon"])
        except Exception:
            return None

    def _loop(self) -> None:
        loc = self._resolve_location()
        if not loc:
            return                              # no location -> no weather
        lat, lon = loc
        url = (f"https://api.open-meteo.com/v1/forecast?latitude={lat:.4f}"
               f"&longitude={lon:.4f}&current=temperature_2m,weather_code,is_day")
        while not self._stop.is_set():
            data = _http_json(url) or {}
            cur = data.get("current") or {}
            try:
                temp = round(float(cur["temperature_2m"]), 1)
                code = int(cur["weather_code"])
                is_day = int(cur.get("is_day", 1)) == 1
                with self._lock:
                    self._data = {"temp": temp, "code": code, "is_day": is_day,
                                  "glyph": wmo_glyph(code, is_day)}
            except Exception:
                pass                            # keep last good data
            self._stop.wait(POLL_S)
